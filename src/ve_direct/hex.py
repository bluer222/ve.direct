"""VE.Direct HEX framing: encode/decode, checksum, Get/Set builders.

Frame: ``:<CMD:1 nibble><DATA:hex pairs><CHECK:2 hex>\\n``.
All bytes are sent as UPPERCASE hex ASCII. Checksum rule::

    (command + sum(data bytes) + checksum) & 0xFF == 0x55

Multi-byte values are little-endian on the wire.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .exceptions import ChecksumError, FrameError

CMD_BOOT = 0x0       # Enter boot (payload 0x51FA... magic)
CMD_DONE = 0x1      # Ping request / Done response
CMD_CHECKSUM_ERROR = 0x2  # Checksum error response (payload 0xAAAA, e.g. Orion XS)
CMD_APP_VERSION = 0x3  # AppVersion request (response is Done with version)
CMD_UNKNOWN = 0x3   # ... same nibble as Unknown response (direction differs)
CMD_PRODUCT_ID = 0x4  # ProductId request (response is Done with product id)
CMD_ERROR = 0x4     # ... same nibble as Error response (direction differs)
CMD_PING = 0x5      # PingResp response
CMD_RESTART = 0x6   # Restart request (no response)
CMD_GET = 0x7       # Get request/response
CMD_SET = 0x8       # Set request/response
CMD_ASYNC = 0xA     # asynchronous notification

# Magic payload enabling bootloader mode (BlueSolar-HEX §1: command 0).
BOOT_MAGIC = bytes.fromhex("51FA" * 5)

# Flags byte in Done/Get/Set *responses* (BlueSolar-HEX §1): nonzero = failure.
HEX_FLAG_UNKNOWN_ID = 0x01
HEX_FLAG_NOT_SUPPORTED = 0x02
HEX_FLAG_PARAMETER_ERROR = 0x04
HEX_FLAG_DESCRIPTIONS = {
    HEX_FLAG_UNKNOWN_ID: "unknown register id",
    HEX_FLAG_NOT_SUPPORTED: "not supported by this device",
    HEX_FLAG_PARAMETER_ERROR: "parameter error",
}

RESPONSE_NAMES = {
    CMD_BOOT: "Boot",
    CMD_DONE: "Done",
    CMD_CHECKSUM_ERROR: "ChecksumError",
    CMD_UNKNOWN: "Unknown",
    CMD_ERROR: "Error",
    CMD_PING: "PingResp",
    CMD_RESTART: "Restart",
    CMD_GET: "Get",
    CMD_SET: "Set",
    CMD_ASYNC: "Async",
}


def decode_app_version_24(raw: bytes | int | object) -> dict:
    if hasattr(raw, "value_bytes"):
        raw = getattr(raw, "value_bytes")
    """Decode 24-bit firmware version (Orion XS §1 Note 1 & register 0x0102).

    Orion XS uses 24-bit firmware version format:
    Bytes: byte 0 = identifier (0x00), bytes 1..3 = version (0xHHMMLL in LE).
    LL == 0xFF denotes an official release build (vHH.MM).
    Other values indicate a beta or test build (vHH.MM-beta.LL).
    """
    if isinstance(raw, int):
        raw_b = raw.to_bytes(4 if raw > 0xFFFFFF else 3, "little")
    else:
        raw_b = bytes(raw)
    h = raw_b.hex().upper()
    if len(raw_b) >= 4:
        identifier = raw_b[0]
        ll = raw_b[1]
        mm = raw_b[2]
        hh = raw_b[3]
    elif len(raw_b) == 3:
        identifier = 0
        ll = raw_b[0]
        mm = raw_b[1]
        hh = raw_b[2]
    else:
        return {"version": None, "type": "unknown", "raw": h}

    major = f"{hh}"
    minor = f"{mm:02X}"
    if ll == 0xFF:
        tname = "release"
        ver = f"{major}.{minor}"
    else:
        tname = "beta"
        ver = f"{major}.{minor}-beta.{ll:02d}"
    return {"version": ver, "type": tname, "build": ll, "raw": h}


def decode_app_version(raw: bytes) -> dict:
    """Decode AppVersion Done / Ping payload (BlueSolar-HEX §1, Orion-XS-HEX §1).

    2-byte form: ``0x4116`` = v1.16 (minor is BCD/hex digits, e.g. 0x16->"16");
    top 2 bits = firmware type (00 bootloader, 01 application, 10 tester,
    11 release candidate). E.g. ``0xD101`` = Release Candidate D of v1.01.
    Orion XS ping returns ``0xYFFF`` (no version in ping; Y indicates fw type).
    Returns ``{"version": "1.16", "type": "application", "raw": ...}``.
    """
    h = bytes(raw).hex().upper()
    if len(raw) == 3:
        return {"version": None, "type": "application24", "raw": h}
    if len(raw) < 2:
        return {"version": None, "type": "unknown", "raw": h}
    v = int.from_bytes(raw[:2], "little")
    
    # Orion XS Ping response: 0xYFFF where lower 12 bits are 0xFFF
    if (v & 0x0FFF) == 0x0FFF:
        y = (v >> 12) & 0x0F
        orion_types = {0b0011: "bootloader", 0b0111: "application",
                       0b1011: "tester", 0b1111: "release_candidate"}
        tname = orion_types.get(y, f"unknown_{y:X}")
        return {"version": None, "type": tname, "raw": h}

    fw_type = (v >> 14) & 0x03
    tname = {0: "bootloader", 1: "application", 2: "tester", 3: "release_candidate"}[fw_type]
    major = (v >> 8) & 0x0F
    minor_hex = f"{v & 0xFF:02X}"
    if fw_type == 3:
        rc_letter = f"{v >> 12:X}"
        ver = f"{major}.{minor_hex}-RC{rc_letter}"
        return {"version": ver, "type": tname, "rc": rc_letter, "raw": h}
    ver = f"{major}.{minor_hex}"
    return {"version": ver, "type": tname, "raw": h}


def decode_ping_version(raw: bytes) -> dict:
    """Ping response payload decodes like AppVersion (same version encoding)."""
    return decode_app_version(raw)


def compute_checksum(cmd: int, data: bytes) -> int:
    """Checksum byte making (cmd + sum(data) + checksum) & 0xFF == 0x55."""
    return (0x55 - cmd - sum(data)) & 0xFF


def verify_frame(cmd: int, data: bytes, checksum: int) -> bool:
    return (cmd + sum(data) + checksum) & 0xFF == 0x55


def encode_frame(cmd: int, payload: bytes = b"") -> bytes:
    """Encode ``:<cmd><payload hex><checksum>\\n`` (uppercase)."""
    if not 0x0 <= cmd <= 0xF:
        raise FrameError(f"command must be one nibble, got {cmd:#x}")
    cs = compute_checksum(cmd, payload)
    return f":{cmd:X}{payload.hex().upper()}{cs:02X}\n".encode("ascii")


@dataclass
class HexFrame:
    cmd: int
    data: bytes
    checksum: int
    valid: bool
    raw: bytes = b""

    @property
    def name(self) -> str:
        return RESPONSE_NAMES.get(self.cmd, f"Cmd({self.cmd:X})")

    @property
    def register_id(self) -> int | None:
        """LE u16 register id (first 2 data bytes), if present."""
        if len(self.data) >= 2:
            return struct.unpack("<H", self.data[:2])[0]
        return None

    @property
    def flags(self) -> int | None:
        if len(self.data) >= 3 and self.cmd in (CMD_GET, CMD_SET, CMD_ASYNC):
            return self.data[2]
        return None

    @property
    def value_bytes(self) -> bytes:
        """Payload after register id and flags for Get/Set/Async."""
        if len(self.data) >= 3 and self.cmd in (CMD_GET, CMD_SET, CMD_ASYNC):
            return self.data[3:]
        return self.data


def decode_line(line: bytes | str, *, strict: bool = False) -> HexFrame:
    """Parse one ``:...\\n`` line. Raises FrameError (always) and
    ChecksumError (only when ``strict=True``).

    Default (streaming) returns ``HexFrame(valid=False)`` on bad checksum so
    one corrupt line never kills the Text/HEX stream; pass ``strict=True``
    for request/response validation where a bad checksum must raise.
    """
    raw = line.encode("ascii") if isinstance(line, str) else bytes(line)
    s = raw.strip().decode("ascii", errors="strict")
    if not s.startswith(":"):
        raise FrameError(f"HEX line must start with ':', got {s!r}")
    hexpart = s[1:].strip()
    if len(hexpart) < 3 or len(hexpart) % 2 == 0:
        # cmd(1 nibble) + data pairs + check(2) => odd total length, min 3
        raise FrameError(f"malformed HEX line: {s!r}")
    try:
        cmd = int(hexpart[0], 16)
        all_bytes = bytes(int(hexpart[i : i + 2], 16) for i in range(1, len(hexpart), 2))
    except ValueError as e:
        raise FrameError(f"non-hex in {s!r}: {e}") from e
    data, checksum = all_bytes[:-1], all_bytes[-1]
    valid = verify_frame(cmd, data, checksum)
    if not valid and strict:
        raise ChecksumError(f"HEX checksum failed for {s!r}")
    return HexFrame(cmd, data, checksum, valid, raw)


def build_get(register_id: int) -> bytes:
    """``:7<id LE>00<check>`` — confirmed against BlueSolar-HEX examples."""
    payload = struct.pack("<H", register_id) + b"\x00"
    return encode_frame(CMD_GET, payload)


def build_set(register_id: int, value: bytes) -> bytes:
    """``:8<id LE>00<value LE><check>`` — e.g. ``:8F0ED0064000C``."""
    payload = struct.pack("<H", register_id) + b"\x00" + bytes(value)
    return encode_frame(CMD_SET, payload)


def build_ping() -> bytes:
    """Version ping ``:154``. Device answers Done/Ping with version bytes."""
    return encode_frame(CMD_DONE, b"")


def build_app_version() -> bytes:
    """``:352`` — firmware version (answers Done with version bytes)."""
    return encode_frame(CMD_APP_VERSION, b"")


def build_product_id() -> bytes:
    """``:451`` — product id (answers Done with LE product-id bytes)."""
    return encode_frame(CMD_PRODUCT_ID, b"")


def build_restart() -> bytes:
    """``:64F`` — restart device (no response is sent)."""
    return encode_frame(CMD_RESTART, b"")


def build_boot() -> bytes:
    """Enter bootloader (payload 0x51FA x5). Use with care."""
    return encode_frame(CMD_BOOT, BOOT_MAGIC)
