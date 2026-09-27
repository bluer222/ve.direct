"""Async VE.Direct client: Text streaming + HEX Get/Set over one wire.

Usage:
    async with await AsyncVEDirect.from_port("/dev/ttyUSB0") as dev:
        async for frame in dev.frames():
            print(frame.to_si())
        print(await dev.hex_get("PANEL_VOLTAGE"))
        await dev.hex_set("BAT_MAX_CURRENT", 35.0)

Concurrency: one shared wire, so all reads go through a single read lock and
HEX request/response transactions are serialized.
Text frames arriving during a HEX transaction are queued, never dropped;
HEX replies arriving while frames() drives the reads still resolve penders.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from . import hex as _hex
from . import registers as _regs
from .exceptions import ChecksumError, RegisterError, TimeoutError, TransportError
from .hex import HexFrame, decode_line
from .text import Frame, TextParser

BAUDRATE = 19200
_LOG = logging.getLogger(__name__)


class AsyncVEDirect:
    def __init__(self, read_chunk, write, *, read_size: int = 1024,
                 flavor: str | None = None) -> None:
        """read_chunk(n) -> await bytes, write(bytes) -> await None."""
        self._read_chunk = read_chunk
        self._write = write
        self._read_size = read_size
        self.flavor = flavor
        self._text = TextParser()
        self._register_waiter: tuple[int, int, asyncio.Future] | None = None
        self._command_waiter: tuple[int, asyncio.Future] | None = None
        self._boot_waiter: asyncio.Future | None = None
        self._text_queue: asyncio.Queue[Frame] = asyncio.Queue()
        self.async_frames: asyncio.Queue[HexFrame] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._data_pumped = asyncio.Event()
        self._hex_synchronized = True
        self._closed = False

    async def _do_write(self, payload: bytes) -> None:
        try:
            res = self._write(payload)
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                await res
        except (Exception, asyncio.CancelledError):
            # A transport may send bytes before drain() fails or is cancelled.
            # Its eventual reply cannot safely belong to a later transaction.
            self._hex_synchronized = False
            raise

    # ---- construction ----
    @classmethod
    def from_streams(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     *, flavor: str | None = None):
        async def read_chunk(n: int) -> bytes:
            return await reader.read(n)

        async def write(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        dev = cls(read_chunk, write, flavor=flavor)
        dev._writer = writer  # type: ignore[attr-defined]
        return dev

    @classmethod
    async def from_port(cls, port: str, baudrate: int = BAUDRATE,
                        *, flavor: str | None = None,
                        dtr: bool = True, rts: bool = False):
        try:
            import serial_asyncio  # type: ignore[import-not-found]
        except ImportError as e:
            raise TransportError(
                "pyserial-asyncio not installed; pip install 've-direct[serial]'"
            ) from e
        reader, writer = await serial_asyncio.open_serial_connection(
            url=port, baudrate=baudrate)
        serial_obj = getattr(getattr(writer, "transport", None), "serial", None)
        if serial_obj is not None:
            with suppress(Exception):
                serial_obj.dtr = dtr
                serial_obj.rts = rts
        return cls.from_streams(reader, writer, flavor=flavor)

    async def close(self) -> None:
        self._closed = True
        w = getattr(self, "_writer", None)
        if w is not None:
            with suppress(Exception):
                w.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError("device is closed")

    # ---- shared read pump (single reader for the shared wire) ----
    async def _pump_once(self, *, require_text: bool = False,
                         require_async: bool = False) -> None:
        """Read one chunk (under the read lock), demux HEX/Text.

        Text frames are queued for frames()/read_one(); HEX replies resolve
        penders even when someone else drives the reads.
        """
        async with self._read_lock:
            # Another reader may have filled this consumer's queue while it
            # waited for the lock. Recheck before blocking on more wire data.
            if require_text and not self._text_queue.empty():
                return
            if require_async and not self.async_frames.empty():
                return
            self._check_open()
            chunk = await self._read_chunk(self._read_size)
            if chunk == b"":
                raise TransportError("connection closed / EOF on transport")
            for frame in self._pump(chunk):
                self._text_queue.put_nowait(frame)
            self._data_pumped.set()
            self._data_pumped.clear()

    # ---- Text ----
    async def read_one(self, timeout: float = 5.0) -> Frame:
        async def _next() -> Frame:
            while True:
                try:
                    return self._text_queue.get_nowait()
                except asyncio.QueueEmpty:
                    await self._pump_once(require_text=True)

        return await asyncio.wait_for(_next(), timeout)

    async def frames(self):
        """Yield checksum-validated Text Frames forever."""
        while True:
            self._check_open()
            try:
                yield self._text_queue.get_nowait()
            except asyncio.QueueEmpty:
                await self._pump_once(require_text=True)

    def _pump(self, chunk: bytes) -> list[Frame]:
        """Incrementally demultiplex HEX and checksum-validated Text."""
        frames, hex_lines = self._text.feed_events(chunk)
        for line in hex_lines:
            try:
                self._handle_hex(decode_line(line, strict=True))
            except Exception as exc:
                # Corrupt HEX must not kill Text reception, but silently
                # swallowing it makes field diagnosis unnecessarily difficult.
                _LOG.warning("discarding invalid VE.Direct HEX line %r: %s",
                             line, exc)
        return frames

    # ---- HEX ----
    def _active_future(self) -> asyncio.Future | None:
        if self._boot_waiter is not None:
            return self._boot_waiter
        if self._register_waiter is not None:
            return self._register_waiter[2]
        if self._command_waiter is not None:
            return self._command_waiter[1]
        return None

    def reset_hex_state(self) -> None:
        """Mark HEX request routing synchronized after an external link reset.

        A timed-out request cannot be safely distinguished from a later reply
        because VE.Direct has no transaction identifier.  The client therefore
        fails closed after timeout/cancellation.  Call this only after the
        device/link has been reset and stale host input has been drained.
        A restart command alone cannot remove buffered host input.
        Discard partial parser state from the old connection; already queued
        complete Text frames and notifications remain available.
        """
        fut = self._active_future()
        if fut is not None and not fut.done():
            fut.cancel()
        self._register_waiter = None
        self._command_waiter = None
        self._boot_waiter = None
        self._text = TextParser()
        self._hex_synchronized = True

    def _check_hex_synchronized(self) -> None:
        if not self._hex_synchronized:
            raise TransportError(
                "HEX response routing is desynchronized; reset the device/link, "
                "drain stale host input, then call reset_hex_state()"
            )

    def _handle_hex(self, frame: HexFrame) -> None:
        # Contract: callers decode with strict=True so checksum failures raise
        # in decode_line before reaching here. A frame with valid=False must
        # never be silently dropped — fail loudly instead.
        if not frame.valid:
            raise ChecksumError(f"HEX checksum failed for {frame.raw!r}")
        if frame.cmd == _hex.CMD_ASYNC:
            self.async_frames.put_nowait(frame)
            return
        if frame.cmd in (_hex.CMD_GET, _hex.CMD_SET):
            # BlueSolar-HEX §1 / Orion-XS-HEX §1 Get/Set response =
            # u16 id + u8 flags + value (minimum 3 bytes). A shorter
            # payload (e.g. 2-byte id-only) has flags None->0 and would
            # otherwise resolve with id bytes as a bogus value: reject
            # as malformed without touching the waiter (caller times out).
            if len(bytes(frame.data)) < 3:
                raise RegisterError(
                    f"truncated {frame.name} response, want id+flags+value: "
                    f"{frame.raw!r}")
            rid = frame.register_id
            waiter = self._register_waiter
            if (waiter is not None and rid == waiter[0]
                    and frame.cmd == waiter[1] and not waiter[2].done()):
                fut = waiter[2]
                flags = frame.flags or 0
                if flags:
                    reasons = [name for bit, name in sorted(
                        _hex.HEX_FLAG_DESCRIPTIONS.items()) if flags & bit]
                    detail = ", ".join(reasons) if reasons else f"flags=0x{flags:02X}"
                    fut.set_exception(RegisterError(
                        f"device rejected {frame.name} for 0x{rid:04X}: {detail}"))
                else:
                    fut.set_result(frame)
            return
        if frame.cmd in (_hex.CMD_DONE, _hex.CMD_PING):
            # Both docs define these responses with fixed content
            # (BlueSolar §1 resp 5 = 2-byte version; resp 1 carries
            # version/product-id; Orion §1 resp 5 = 0xYFFF 2 bytes).
            # Empty/short payloads are malformed: fail the waiter with
            # a typed error instead of resolving with b"" / unknown.
            if len(bytes(frame.data)) < 2:
                exc = RegisterError(
                    f"truncated {frame.name} response, want >=2 bytes: "
                    f"{frame.raw!r}")
                waiter = self._command_waiter
                if (waiter is not None and frame.cmd == waiter[0]
                        and not waiter[1].done()):
                    waiter[1].set_exception(exc)
                    return
                raise exc
            waiter = self._command_waiter
            if (waiter is not None and frame.cmd == waiter[0]
                    and not waiter[1].done()):
                waiter[1].set_result(frame)
            return
        if frame.cmd == _hex.CMD_UNKNOWN:
            fut = self._active_future()
            if fut is not None and not fut.done():
                fut.set_exception(RegisterError(
                    f"device reports Unknown (unsupported): {frame.raw!r}"))
            return
        if frame.cmd in (_hex.CMD_ERROR, _hex.CMD_CHECKSUM_ERROR):
            if (frame.cmd == _hex.CMD_ERROR
                    and frame.data in (b"\x00", b"\x00\x00")):
                # Payload zero specifically means boot entry failed. It can
                # arrive after boot's observation window, so it must never
                # reject a later Get, Set, Ping or other unrelated command.
                exc = RegisterError(
                    f"device could not enter bootloader: {frame.raw!r}")
                fut = self._boot_waiter
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
                else:
                    _LOG.warning("late or unsolicited boot failure: %s", exc)
                return
            fut = self._active_future()
            if fut is not None and not fut.done():
                # BlueSolar-HEX §1 / Orion-XS-HEX §1 response 4 carries two
                # distinct meanings: payload 0xAAAA = framing error (wire
                # checksum/structure) vs payload 0 = unable to enter
                # bootloader. Response 2 (Orion) is always a framing error.
                if frame.cmd == _hex.CMD_CHECKSUM_ERROR:
                    exc: Exception = ChecksumError(
                        f"device reports {frame.name}: {frame.raw!r}")
                elif bytes(frame.data) == bytes.fromhex("AAAA"):
                    exc = ChecksumError(
                        f"device reports Error (framing error): {frame.raw!r}")
                else:
                    exc = RegisterError(
                        f"device reports {frame.name}: {frame.raw!r}")
                fut.set_exception(exc)

    async def _wait_future(self, fut: asyncio.Future, timeout: float) -> HexFrame:
        """Wait for fut to be resolved by any read pump, pumping ourselves if idle."""
        async def _wait() -> HexFrame:
            while not fut.done():
                if self._read_lock.locked():
                    # Another coroutine (e.g. frames()) is holding the read lock and pumping.
                    # Wait for fut to complete or for a data pump cycle.
                    pump_wait_task = asyncio.create_task(self._data_pumped.wait())
                    try:
                        await asyncio.wait(
                            [fut, pump_wait_task],
                            timeout=0.05,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        pump_wait_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await pump_wait_task
                    if fut.done():
                        break
                    continue

                async with self._read_lock:
                    if fut.done():
                        break
                    self._check_open()
                    chunk = await self._read_chunk(self._read_size)
                    if chunk == b"":
                        raise TransportError("connection closed / EOF on transport")
                    for frame in self._pump(chunk):
                        self._text_queue.put_nowait(frame)
                    self._data_pumped.set()
                    self._data_pumped.clear()
            return fut.result()

        return await asyncio.wait_for(_wait(), timeout)

    async def _transact(self, payload: bytes, expect_reg: int,
                        expect_cmd: int, timeout: float) -> HexFrame:
        async with self._transaction_lock:
            self._check_hex_synchronized()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._register_waiter = (expect_reg, expect_cmd, fut)
            wrote = False
            try:
                async with self._write_lock:
                    self._check_open()
                    wrote = True
                    await self._do_write(payload)
                return await self._wait_future(fut, timeout)
            except asyncio.TimeoutError as e:
                self._hex_synchronized = False
                raise TimeoutError(f"no HEX reply within {timeout}s") from e
            except asyncio.CancelledError:
                if wrote:
                    self._hex_synchronized = False
                raise
            finally:
                if self._register_waiter is not None and self._register_waiter[2] is fut:
                    self._register_waiter = None

    async def hex_get_raw(self, register_id: int, timeout: float = 2.0) -> HexFrame:
        return await self._transact(
            _hex.build_get(register_id), register_id, _hex.CMD_GET, timeout)

    async def hex_set_raw(self, register_id: int, value: bytes, timeout: float = 2.0) -> HexFrame:
        return await self._transact(
            _hex.build_set(register_id, value), register_id, _hex.CMD_SET, timeout)

    async def hex_get(self, name_or_id: str | int, timeout: float = 2.0,
                      flavor: str | None = None):
        target_flavor = flavor or self.flavor
        reg = _regs.lookup(name_or_id, flavor=target_flavor)
        if reg.access == "WRITE_ONLY":
            raise RegisterError(f"{reg.name} is WRITE_ONLY")
        if reg.access == "CONSTANT":
            # CONSTANT (product id, app version, serial, model, ranges) is
            # readable via Get; keep allowed (matches Victron docs).
            pass
        frame = await self.hex_get_raw(reg.id, timeout)
        return _regs.decode_value(reg, frame.value_bytes)

    async def hex_set(self, name_or_id: str | int, value, timeout: float = 2.0,
                      flavor: str | None = None):
        target_flavor = flavor or self.flavor
        reg = _regs.lookup(name_or_id, flavor=target_flavor)
        if reg.access == "READ_ONLY":
            raise RegisterError(f"{reg.name} is READ_ONLY")
        if reg.access == "CONSTANT":
            raise RegisterError(f"{reg.name} is CONSTANT")
        raw = _regs.encode_value(reg, value)
        frame = await self.hex_set_raw(reg.id, raw, timeout)
        return _regs.decode_value(reg, frame.value_bytes) if frame.value_bytes else None

    def decode_async_frame(self, frame: HexFrame,
                           *, flavor: str | None = None) -> dict:
        """Decode one unsolicited ``:A`` frame using product metadata."""
        if frame.cmd != _hex.CMD_ASYNC:
            raise RegisterError(f"expected Async frame, got {frame.name}")
        # BlueSolar-HEX §1 / Orion-XS-HEX §1 Async = u16 id + u8 flags
        # + value. A shorter payload is truncated: reject instead of
        # surfacing id bytes as value with masked flags=0.
        if len(bytes(frame.data)) < 3:
            raise RegisterError(
                f"truncated Async frame, want id+flags+value: {frame.raw!r}")
        rid = frame.register_id
        if rid is None:
            raise RegisterError("Async frame has no register id")
        target_flavor = flavor or self.flavor
        try:
            reg = _regs.lookup(rid, flavor=target_flavor)
        except KeyError:
            return {
                "register_id": rid,
                "name": None,
                "flags": frame.flags or 0,
                "value": frame.value_bytes,
            }
        return {
            "register_id": rid,
            "name": reg.name,
            "flags": frame.flags or 0,
            "value": _regs.decode_value(reg, frame.value_bytes),
            "register": reg,
        }

    async def async_notifications(self, *, flavor: str | None = None):
        """Yield typed unsolicited HEX register notifications forever."""
        while True:
            self._check_open()
            try:
                frame = self.async_frames.get_nowait()
            except asyncio.QueueEmpty:
                await self._pump_once(require_async=True)
                continue
            yield self.decode_async_frame(frame, flavor=flavor)

    async def _command_transact(self, payload: bytes, expect_cmd: int,
                                timeout: float) -> HexFrame:
        """Transact one command whose response has no register identifier."""
        async with self._transaction_lock:
            self._check_hex_synchronized()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._command_waiter = (expect_cmd, fut)
            wrote = False
            try:
                async with self._write_lock:
                    self._check_open()
                    wrote = True
                    await self._do_write(payload)
                return await self._wait_future(fut, timeout)
            except asyncio.TimeoutError as e:
                self._hex_synchronized = False
                raise TimeoutError(f"no HEX reply within {timeout}s") from e
            except asyncio.CancelledError:
                if wrote:
                    self._hex_synchronized = False
                raise
            finally:
                if self._command_waiter is not None and self._command_waiter[1] is fut:
                    self._command_waiter = None

    async def ping(self, timeout: float = 2.0) -> HexFrame:
        frame = await self._command_transact(
            _hex.build_ping(), _hex.CMD_PING, timeout)
        if len(bytes(frame.data)) < 2:
            raise RegisterError(
                f"truncated PingResp, want >=2 bytes: {frame.raw!r}")
        return frame

    async def get_app_version(self, timeout: float = 2.0) -> bytes:
        """``:352`` — returns raw version bytes (e.g. b'\\x16\\x41' = v1.16).

        Use :func:`ve_direct.hex.decode_app_version` (or
        :meth:`get_app_version_info`) to decode firmware type/version.
        """
        frame = await self._command_transact(
            _hex.build_app_version(), _hex.CMD_DONE, timeout)
        if len(bytes(frame.data)) < 2:
            raise RegisterError(
                f"truncated Done (app version), want >=2 bytes: {frame.raw!r}")
        return frame.data

    async def get_app_version_info(self, timeout: float = 2.0) -> dict:
        """Decode application version info.

        For Orion XS (which uses 24-bit version encoding where command :3 is
        deprecated), reads register 0x0102 (Orion-XS §1.1). BlueSolar
        product-info (§1.1: 0x0100/0x0104/0x010A/0x010B/0x0140) has no
        0x0102, so non-Orion flavors use :352 only and any Unknown
        propagates as RegisterError (no undocumented 0x0102 fallback).
        """
        if self.flavor == "ORION":
            frame = await self.hex_get_raw(0x0102, timeout=timeout)
            return _hex.decode_app_version_24(frame.value_bytes)
        raw = await self.get_app_version(timeout)
        return _hex.decode_app_version(raw)

    async def get_product_id(self, timeout: float = 2.0) -> int:
        """``:451`` — decode the two-byte little-endian Done payload.

        Command :4 uses this form in every published family example,
        including Orion XS :1F0A3C1. The four-byte instance/PID/reserved
        layout and legacy endianness warning apply to register 0x0100,
        not this command. No flavor is needed to interpret the reply.
        """
        frame = await self._command_transact(
            _hex.build_product_id(), _hex.CMD_DONE, timeout)
        data = bytes(frame.data)
        if len(data) == 2:
            return int.from_bytes(data, "little")
        raise RegisterError(
            f"invalid product-id command reply, want 2 bytes: {frame.raw!r}"
        )

    async def boot(self, timeout: float = 0.2) -> None:
        """Send the bootloader entry command (:0<51FA*5>).

        Observe incoming traffic for up to ``timeout`` seconds to catch a
        boot failure and raise RegisterError. Successful entry has no reply;
        silence does not confirm the device's mode. Boot failures received
        after this observation window are logged when the stream is read.
        """
        if self.flavor in ("BMV", "BMV71"):
            raise RegisterError("boot command 0 is reserved on BMV devices")
        if timeout < 0:
            raise ValueError("boot observation timeout must be nonnegative")
        async with self._transaction_lock:
            self._check_hex_synchronized()
            fut = asyncio.get_running_loop().create_future()
            self._boot_waiter = fut
            wrote = False
            try:
                async with self._write_lock:
                    self._check_open()
                    wrote = True
                    await self._do_write(_hex.build_boot())
                try:
                    await self._wait_future(fut, timeout)
                except asyncio.TimeoutError:
                    # There is no success response for this command.
                    pass
            except asyncio.CancelledError:
                if wrote:
                    self._hex_synchronized = False
                raise
            except Exception:
                if wrote and not fut.done():
                    self._hex_synchronized = False
                raise
            finally:
                if self._boot_waiter is fut:
                    self._boot_waiter = None
                if not fut.done():
                    fut.cancel()

    async def restart(self) -> None:
        """Send ``:64F`` without waiting for a reply.

        HEX routing stays disabled until the caller resets/drains the link
        and calls reset_hex_state(). Restart cannot clear stale host input.
        Text reception continues while HEX routing is disabled.
        """
        async with self._transaction_lock:
            async with self._write_lock:
                self._check_open()
                self._hex_synchronized = False
                await self._do_write(_hex.build_restart())


def decode_hex_value(name_or_id: str | int, raw: bytes,
                     *, flavor: str | None = None):
    return _regs.decode_value(_regs.lookup(name_or_id, flavor=flavor), raw)


def encode_hex_value(name_or_id: str | int, value,
                     *, flavor: str | None = None) -> bytes:
    return _regs.encode_value(
        _regs.lookup(name_or_id, flavor=flavor), value)
