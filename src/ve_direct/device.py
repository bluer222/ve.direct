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
        self._text_queue: asyncio.Queue[Frame] = asyncio.Queue()
        self.async_frames: asyncio.Queue[HexFrame] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._data_pumped = asyncio.Event()
        self._hex_synchronized = True
        self._closed = False

    async def _do_write(self, payload: bytes) -> None:
        res = self._write(payload)
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            await res

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
    async def _pump_once(self, *, require_text: bool = False) -> None:
        """Read one chunk (under the read lock), demux HEX/Text.

        Text frames are queued for frames()/read_one(); HEX replies resolve
        penders even when someone else drives the reads.
        """
        async with self._read_lock:
            # Another reader may have queued Text while this coroutine waited
            # for the lock.  Do not block on another serial read in that case.
            if require_text and not self._text_queue.empty():
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
        device/link has been reset or stale input has otherwise been drained.
        """
        fut = self._active_future()
        if fut is not None and not fut.done():
            fut.cancel()
        self._register_waiter = None
        self._command_waiter = None
        self._hex_synchronized = True

    def _check_hex_synchronized(self) -> None:
        if not self._hex_synchronized:
            raise TransportError(
                "HEX response routing is desynchronized after a timeout or "
                "cancellation; reset the device/link, then call reset_hex_state()"
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
                    done, _ = await asyncio.wait(
                        [fut, pump_wait_task],
                        timeout=0.05,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    pump_wait_task.cancel()
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
                    await self._do_write(payload)
                    wrote = True
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
                await self._pump_once()
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
                    await self._do_write(payload)
                    wrote = True
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
        return await self._command_transact(
            _hex.build_ping(), _hex.CMD_PING, timeout)

    async def get_app_version(self, timeout: float = 2.0) -> bytes:
        """``:352`` — returns raw version bytes (e.g. b'\\x16\\x41' = v1.16).

        Use :func:`ve_direct.hex.decode_app_version` (or
        :meth:`get_app_version_info`) to decode firmware type/version.
        """
        frame = await self._command_transact(
            _hex.build_app_version(), _hex.CMD_DONE, timeout)
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
        """``:451`` — returns product id as int.

        Primary path is the standardized 4-byte Done form (BlueSolar-HEX
        §1.1 / Orion-XS-HEX §1.1: byte0 instance 0x00, bytes1-2 PID,
        byte3 reserved 0xFF); malformed 4-byte payloads are rejected.
        Legacy fallback: BlueSolar-HEX §1.1 Note 1 says product info is
        2-byte since fw 1.12 and 4-byte only since fw 1.30, and §1.3 shows
        2-byte Done replies (e.g. ``:1000351``). Pre-1.30 MPPTs can never
        return the 4-byte form, so a 2-byte reply is accepted as the PID
        for MPPT (and BMV/INV, whose specs use 2-byte LE). MPPT_RS is
        excluded: RS content needs much newer fw than 1.30 so real RS
        hardware can never send pre-1.30 2-byte, and §1.1 says the
        2-byte form "should be ignored". ORION/MULTI_RS likewise excluded
        (Orion-XS §1.1 carries the same "should be ignored" note).
        """
        import struct as _s
        frame = await self._command_transact(
            _hex.build_product_id(), _hex.CMD_DONE, timeout)
        data = bytes(frame.data)
        if len(data) == 4:
            if data[0] != 0x00 or data[3] != 0xFF:
                raise RegisterError(
                    f"malformed 4-byte product-id reply (want 00 <PID LE> FF): "
                    f"{frame.raw!r}")
            return _s.unpack("<H", data[1:3])[0]
        if len(data) == 2 and self.flavor in (
                "BMV", "BMV71", "INV", "MPPT"):
            return _s.unpack("<H", data)[0]
        if len(data) == 2 and self.flavor is None:
            raise RegisterError(
                "2-byte product-id reply is valid for BMV/Phoenix and legacy "
                "pre-1.30 MPPT but unsafe without a flavor; set flavor")
        raise RegisterError(
            f"invalid product-id reply for flavor {self.flavor!r}: {frame.raw!r}"
        )

    async def boot(self) -> None:
        """Send the bootloader entry command (:0<51FA*5>).

        Puts the device into bootloader mode for firmware updates.
        Like restart, the device immediately reboots and sends no HEX reply.
        """
        if self.flavor in ("BMV", "BMV71"):
            raise RegisterError("boot command 0 is reserved on BMV devices")
        async with self._transaction_lock:
            async with self._write_lock:
                self._check_open()
                await self._do_write(_hex.build_boot())

    async def restart(self) -> None:
        """``:64F`` — restart device (no response is sent)."""
        async with self._transaction_lock:
            async with self._write_lock:
                self._check_open()
                await self._do_write(_hex.build_restart())
            self.reset_hex_state()


def decode_hex_value(name_or_id: str | int, raw: bytes,
                     *, flavor: str | None = None):
    return _regs.decode_value(_regs.lookup(name_or_id, flavor=flavor), raw)


def encode_hex_value(name_or_id: str | int, value,
                     *, flavor: str | None = None) -> bytes:
    return _regs.encode_value(
        _regs.lookup(name_or_id, flavor=flavor), value)
