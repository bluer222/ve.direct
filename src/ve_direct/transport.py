"""Transports: in-memory simulator for tests/examples (no hardware needed)."""
from __future__ import annotations

import asyncio


def make_text_block(fields: dict[str, str]) -> bytes:
    """Build a checksum-valid Text block from {label: raw_value}."""
    prefix = b"".join(
        b"\r\n" + k.encode("latin-1") + b"\t" + v.encode("latin-1")
        for k, v in fields.items()
    ) + b"\r\nChecksum\t"
    return prefix + bytes([(-sum(prefix)) % 256])


class SimulatedTransport:
    """Async read/write pair replaying Text blocks + canned HEX replies.

    hex_replies: {register_id: value_bytes} — answered to Get with a Get
      response (like real HW); Set writes are acked with a Set response
      echoing the written value. Ping/AppVersion/ProductId answered with
      canned version/product frames; Restart is acked silently.
    """

    def __init__(self, text_chunks: list[bytes] | None = None,
                   hex_replies: dict[int, bytes] | None = None,
                   chunk_size: int = 64, delay: float = 0.0,
                   product_id: int = 0xA04A,
                   app_version: bytes = b"\x16\x41",
                   flavor: str = "MPPT", boot_fails: bool = False,
                   parameter_error: bool | set[int] | list[int] | tuple | frozenset = False,
                   parameter_error_registers: set[int] | list[int] | tuple | frozenset | None = None) -> None:
        from . import hex as _hex  # local import: keeps module import light
        self._hex = _hex
        self._stream = b"".join(text_chunks or [])
        self._hex_replies = dict(hex_replies or {})
        self._chunk_size = chunk_size
        self._delay = delay
        self.product_id = product_id
        # BlueSolar-HEX §1.3 example is 0x4116 application (not bootloader).
        self.app_version = bytes(app_version)
        self.flavor = flavor.upper()
        self.boot_fails = bool(boot_fails)
        # BlueSolar-HEX §1 / Orion-XS-HEX §1 reply flags: 0x01 Unknown Id,
        # 0x02 Not supported, 0x04 Parameter Error. Parallels boot_fails:
        # parameter_error=True fails every Get/Set with 0x04, or pass a
        # collection of register ids (also via parameter_error_registers)
        # to fail only those registers with 0x04.
        self._parameter_error_all = parameter_error is True
        _regs_set: set[int] = set()
        if parameter_error is not True and parameter_error is not False \
                and parameter_error is not None:
            _regs_set.update(parameter_error)  # type: ignore[arg-type]
        if parameter_error_registers:
            _regs_set.update(parameter_error_registers)
        self._parameter_error_registers = frozenset(_regs_set)
        # Public mirror of the global flag (parallels boot_fails).
        self.parameter_error = bool(self._parameter_error_all)
        self.written: list[bytes] = []
        self._pending_out = bytearray()

    def _is_parameter_error(self, register_id: int) -> bool:
        return (self._parameter_error_all
                or register_id in self._parameter_error_registers)

    def _framing_error_reply(self) -> bytes:
        # Orion-XS §1 response 2 is :2AAAAFF; BlueSolar §1.3 invalid-frame
        # example is :4AAAAFD. Emit the flavor-documented form.
        if self.flavor == "ORION":
            return self._hex.encode_frame(
                self._hex.CMD_CHECKSUM_ERROR, bytes.fromhex("AAAA"))
        return self._hex.encode_frame(
            self._hex.CMD_ERROR, bytes.fromhex("AAAA"))

    @staticmethod
    def _zero_payload(dtype: str) -> bytes:
        d = (dtype or "").lower()
        if "32" in d:
            return b"\x00\x00\x00\x00"
        if "24" in d:
            return b"\x00\x00\x00"
        if "16" in d:
            return b"\x00\x00"
        if d.startswith("uint8") or d.startswith("int8") or d == "uint8_t":
            return b"\x00"
        if "8" in d:
            return b"\x00"
        return b"\x00\x00"

    def inject_async(self, register_id: int, value: bytes = b"",
                     flags: int = 0) -> None:
        """Queue one unsolicited :A frame for async_notifications() tests."""
        import struct as _s
        payload = _s.pack("<H", register_id) + bytes([flags]) + bytes(value)
        self._pending_out += self._hex.encode_frame(
            self._hex.CMD_ASYNC, payload)

    def inject_raw(self, line: bytes) -> None:
        """Queue a raw wire line (must already include trailing newline)."""
        self._pending_out += line if line.endswith(b"\n") else line + b"\n"

    async def read_chunk(self, n: int) -> bytes:
        # Block like a real serial read (b"" means EOF only if asked to close).
        for _ in range(2000):
            if self._pending_out:
                out = bytes(self._pending_out[:n])
                del self._pending_out[:n]
                return out
            if self._stream:
                out = self._stream[: min(n, self._chunk_size)]
                self._stream = self._stream[len(out):]
                return out
            if self._delay:
                await asyncio.sleep(self._delay)
            else:
                await asyncio.sleep(0.005)
        return b""  # genuine timeout -> caller treats as EOF

    async def write(self, data: bytes) -> None:
        self.written.append(bytes(data))
        # Answer HEX Get/Set immediately by queueing a reply.
        for line in data.split(b"\n"):
            if not line.startswith(b":"):
                continue
            try:
                frame = self._hex.decode_line(line + b"\n")
            except Exception:
                # Malformed line: documented framing-error reply.
                self._pending_out += self._framing_error_reply()
                continue
            if not frame.valid:
                # Corrupt checksum: :2AAAAFF (Orion-XS §1 response 2) /
                # :4AAAAFD (BlueSolar §1.3). Never answer normally.
                self._pending_out += self._framing_error_reply()
                continue
            if frame.cmd == self._hex.CMD_GET and frame.register_id is not None:
                rid = frame.register_id
                flags = 0
                reg = None
                if self._is_parameter_error(rid):
                    flags = self._hex.HEX_FLAG_PARAMETER_ERROR
                    try:
                        from . import registers as _regs
                        reg = _regs.lookup(rid, flavor=self.flavor)
                    except KeyError:
                        reg = None
                else:
                    try:
                        from . import registers as _regs
                        reg = _regs.lookup(rid, flavor=self.flavor)
                        if reg.access == "WRITE_ONLY":
                            flags = self._hex.HEX_FLAG_NOT_SUPPORTED
                    except KeyError:
                        reg = None
                        if rid not in self._hex_replies:
                            flags = self._hex.HEX_FLAG_UNKNOWN_ID
                if rid in self._hex_replies:
                    val = self._hex_replies[rid]
                elif reg is not None:
                    val = self._zero_payload(reg.dtype)
                else:
                    val = b"\x00\x00"
                reply = self._hex.encode_frame(
                    self._hex.CMD_GET, frame.data[:2] + bytes([flags]) + val)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_SET and frame.register_id is not None:
                rid = frame.register_id
                val = frame.data[3:]  # echo written value
                flags = 0
                if self._is_parameter_error(rid):
                    flags = self._hex.HEX_FLAG_PARAMETER_ERROR
                    try:
                        from . import registers as _regs
                        reg = _regs.lookup(rid, flavor=self.flavor)
                    except KeyError:
                        reg = None
                else:
                    try:
                        from . import registers as _regs
                        reg = _regs.lookup(rid, flavor=self.flavor)
                        if reg.access in ("READ_ONLY", "CONSTANT"):
                            flags = self._hex.HEX_FLAG_NOT_SUPPORTED
                    except KeyError:
                        reg = None
                        if rid not in self._hex_replies:
                            flags = self._hex.HEX_FLAG_UNKNOWN_ID
                if not flags:
                    self._hex_replies[rid] = val
                reply = self._hex.encode_frame(
                    self._hex.CMD_SET, frame.data[:2] + bytes([flags]) + val)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_DONE and not frame.data:
                # Ping :154 -> PingResp :5... BlueSolar §1.3 example is
                # 0x4116 application; Orion §1.3 is 0x7FFF (no version).
                ping_data = (b"\xFF\x7F" if self.flavor == "ORION"
                             else bytes(self.app_version))
                reply = self._hex.encode_frame(self._hex.CMD_PING, ping_data)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_APP_VERSION and not frame.data:
                if self.flavor == "ORION":
                    # Deprecated on Orion-XS (§1 Note 1): answer Unknown.
                    # Correct encoding of Unknown(03 00) is :303004F
                    # (the §1.3 print :30300F4 fails checksum — doc typo).
                    reply = self._hex.encode_frame(
                        self._hex.CMD_UNKNOWN, b"\x03\x00")
                else:
                    reply = self._hex.encode_frame(
                        self._hex.CMD_DONE, bytes(self.app_version))
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_PRODUCT_ID and not frame.data:
                import struct as _s
                if self.flavor in ("BMV", "BMV71", "INV"):
                    payload = _s.pack("<H", self.product_id)
                else:
                    payload = b"\x00" + _s.pack("<H", self.product_id) + b"\xFF"
                reply = self._hex.encode_frame(
                    self._hex.CMD_DONE, payload)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_BOOT:
                # Boot :0 with magic 51FA*5 reboots into bootloader with no
                # HEX reply on success. With boot_fails=True emit the
                # documented Error payload 0x00 = "unable to enter
                # bootloader" (BlueSolar §1 response 4; Orion-XS §1
                # response 4): :40051.
                if self.boot_fails:
                    self._pending_out += self._hex.encode_frame(
                        self._hex.CMD_ERROR, b"\x00")
            elif frame.cmd == self._hex.CMD_RESTART:
                # Restart :64F sends no response (BlueSolar §1 / Orion §1).
                pass
            else:
                # Reserved/unknown commands (2,5,9,B-F, ...) -> Unknown
                # reply (BlueSolar §1 response 3; Orion-XS §1.3 :2000152 ->
                # :3020050). Payload echoes the unknown command nibble.
                reply = self._hex.encode_frame(
                    self._hex.CMD_UNKNOWN, bytes([frame.cmd, 0x00]))
                self._pending_out += reply
