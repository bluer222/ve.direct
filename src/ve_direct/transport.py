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
                  flavor: str = "MPPT") -> None:
        from . import hex as _hex  # local import: keeps module import light
        self._hex = _hex
        self._stream = b"".join(text_chunks or [])
        self._hex_replies = dict(hex_replies or {})
        self._chunk_size = chunk_size
        self._delay = delay
        self.product_id = product_id
        self.app_version = bytes(app_version)
        self.flavor = flavor.upper()
        self.written: list[bytes] = []
        self._pending_out = bytearray()

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
                continue
            if frame.cmd == self._hex.CMD_GET and frame.register_id is not None:
                rid = frame.register_id
                flags = 0
                try:
                    from . import registers as _regs
                    reg = _regs.lookup(rid, flavor=self.flavor)
                    if reg.access == "WRITE_ONLY":
                        flags = self._hex.HEX_FLAG_NOT_SUPPORTED
                except KeyError:
                    reg = None
                    if rid not in self._hex_replies:
                        flags = self._hex.HEX_FLAG_UNKNOWN_ID
                val = self._hex_replies.get(rid, b"\x00\x00")
                reply = self._hex.encode_frame(
                    self._hex.CMD_GET, frame.data[:2] + bytes([flags]) + val)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_SET and frame.register_id is not None:
                rid = frame.register_id
                val = frame.data[3:]  # echo written value
                flags = 0
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
                ping_data = b"\xFF\x7F" if self.flavor == "ORION" else b"\x01\x01"
                reply = self._hex.encode_frame(self._hex.CMD_PING, ping_data)
                self._pending_out += reply
            elif frame.cmd == self._hex.CMD_APP_VERSION and not frame.data:
                if self.flavor == "ORION":
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
            elif frame.cmd == self._hex.CMD_RESTART:
                pass  # no response, like real HW
