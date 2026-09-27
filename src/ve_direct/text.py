"""VE.Direct Text-mode parser: framing, checksum, semantic decoding.

Wire format: ``\\r\\nLABEL\\tVALUE`` lines grouped in blocks, one block/sec,
terminated by a ``Checksum`` line. A block is valid iff the modulo-256 sum of
*all* bytes in the block (including newlines and the checksum byte) == 0.

Spec implementation guidelines (p.11): reserve 9 bytes for the field label,
33 bytes for the field value, max 22 fields/block. This parser is liberal
(arbitrary label/value lengths) but caps buffered input at 8 KiB for safety;
see :class:`TextParser` (oversized HEX candidates and Text backlog are
truncated, never grown unbounded).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .tables import (
    AR_MAP,
    CAP_BLE_MAP,
    CS_MAP,
    ERR_MAP,
    LABEL_INFO,
    MODE_MAP,
    MON_MAP,
    MPPT_MAP,
    OR_MAP,
    WARN_MAP,
    decode_ar,
    decode_cap_ble,
    decode_or,
    pid_name,
)

_MISSING_DASH = ("---",)
# Spec defines "---" only for T (fn8) and CE/SOC/TTG (fn10,11), but the parser
# accepts it liberally for any label (except LOAD/Alarm/Relay). Empty "" is
# NOT in the spec; treat it as missing only for the spec'd labels above.
_MISSING_EMPTY_LABELS = frozenset({"T", "CE", "SOC", "TTG"})
# Victron's FAQ framehandler uppercases labels before matching CHECKSUM.
_CHECKSUM_TAB = b"checksum\t"


def _endswith_checksum_tab(buf: bytes | bytearray) -> bool:
    n = len(_CHECKSUM_TAB)
    return len(buf) >= n and bytes(buf[-n:]).lower() == _CHECKSUM_TAB


def _find_checksum_tab(buf: bytes | bytearray, start: int = 0) -> int:
    n = len(_CHECKSUM_TAB)
    limit = len(buf) - n + 1
    for i in range(start, limit):
        if bytes(buf[i:i + n]).lower() == _CHECKSUM_TAB:
            return i
    return -1


def _decode_fw(raw: str) -> str | None:
    """Format Text FW values such as ``208`` and ``C208``.

    Note: C208 means "release candidate C for v2.08", rendered as
    ``v2.08-RCC`` (RC acronym + revision C). This matches the HEX
    AppVersion decoder (``-RC{letter}``) and is intentional, not a
    duplicated C.
    """
    s = raw.strip().upper()
    rc = s[0] if s and s[0].isalpha() else None
    digits = s[1:] if rc else s
    if not digits.isdigit() or len(digits) < 3:
        return None
    version = f"v{int(digits[:-2])}.{digits[-2:]}"
    return f"{version}-RC{rc}" if rc else version


def _decode_fwe(raw: str) -> str | None:
    """Format Text FWE ``HHMMLL`` values, where FF denotes release."""
    s_raw = raw.strip().upper()
    # All leading zeroes are optional, including those in versions below 1.00.
    if not 1 <= len(s_raw) <= 6 or any(c not in "0123456789ABCDEF" for c in s_raw):
        return None
    s = s_raw.zfill(6)
    major = int(s[:2], 16)
    minor = s[2:4]
    build = s[4:6]
    return (
        f"v{major}.{minor}"
        if build == "FF"
        else f"v{major}.{minor}-beta.{int(build, 16):02d}"
    )


@dataclass
class TypedValue:
    label: str
    raw: str
    value: int | float | str | None
    unit: str | None
    desc: str
    human: str | None = None

    @property
    def volts(self) -> float | None:
        if isinstance(self.value, (int, float)) and self.unit in ("mV", "0.01V"):
            return self.value / 1000 if self.unit == "mV" else self.value / 100
        return None

    @property
    def amps(self) -> float | None:
        if isinstance(self.value, (int, float)) and self.unit in ("mA", "0.1A"):
            return self.value / 1000 if self.unit == "mA" else self.value / 10
        return None

    @property
    def percent(self) -> float | None:
        if self.label in ("SOC", "DM") and isinstance(self.value, (int, float)):
            return self.value / 10
        return None


def verify_block_checksum(block: bytes) -> bool:
    """True iff sum(block) % 256 == 0."""
    return sum(block) % 256 == 0


def _decode_label(label: str, raw: str) -> TypedValue:
    info = LABEL_INFO.get(label, {"unit": None, "desc": f"Unknown label {label}"})
    unit = info["unit"]  # type: ignore[typeddict-item]
    desc = info["desc"]  # type: ignore[typeddict-item]
    if raw in _MISSING_DASH and label not in ("LOAD", "Alarm", "Relay"):
        return TypedValue(label, raw, None, unit, desc, "No data (---)")
    if raw == "" and label in _MISSING_EMPTY_LABELS:
        return TypedValue(label, raw, None, unit, desc, "No data (---)")

    human: str | None = None
    value: int | float | str | None = raw
    try:
        if label == "PID":
            # PID is hex per the spec PID list (e.g. 0xA04A). Accept both
            # "0xA04A" and bare "A04A"; int(x, 16) handles the 0x prefix.
            value = int(raw.strip(), 16)
            human = pid_name(value)  # type: ignore[arg-type]
        elif label == "OR":
            # Devices send a 0x-prefixed mask. Keep accepting decimal values
            # used by callers and older recordings of this library.
            value = int(raw, 16 if raw.lower().startswith("0x") else 10)
            human = ", ".join(decode_or(value))
        elif label in ("CS", "ERR", "MODE", "MPPT", "MON", "AR", "WARN",
                       "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9",
                       "H10", "H11", "H12", "H13", "H14", "H15", "H16",
                       "H17", "H18", "H19", "H20", "H21", "H22", "H23",
                       "HSDS", "V", "V2", "V3", "VS", "VM", "DM", "VPV", "PPV",
                       "I", "I2", "I3", "IL", "P", "CE", "SOC", "TTG",
                       "AC_OUT_V", "AC_OUT_I", "AC_OUT_S",
                       "DC_IN_V", "DC_IN_I", "DC_IN_P", "CAP_BLE"):
            value = int(raw)
            if label == "CS":
                human = CS_MAP.get(value, f"Unknown state ({value})")
            elif label == "ERR":
                human = ERR_MAP.get(value, f"Unknown error ({value})")
            elif label == "MODE":
                human = MODE_MAP.get(value, f"Unknown mode ({value})")
            elif label == "MPPT":
                human = MPPT_MAP.get(value, f"Unknown tracker ({value})")
            elif label == "MON":
                human = MON_MAP.get(value, f"Unknown monitor ({value})")
            elif label in ("AR", "WARN"):
                human = ", ".join(decode_ar(value))
            elif label == "CAP_BLE":
                human = ", ".join(decode_cap_ble(value))
            elif label == "TTG" and value == -1:
                # Spec fn12: -1 means infinite (battery not discharging).
                human = "Infinite (not discharging)"
        elif label == "T":
            value = int(raw)
            human = f"{value} °C"
        elif label in ("LOAD", "Alarm", "Relay"):
            # Old fw (BMV-600 v2.09-) sent "On"/"Off"; normalize for comparison.
            value = raw.upper()
            human = raw.upper()
        elif label == "FW":
            value = raw
            human = _decode_fw(raw)
        elif label == "FWE":
            value = raw
            human = _decode_fwe(raw)
        elif label in ("SER#", "BMV"):
            value = raw
    except ValueError:
        value = raw
    return TypedValue(label, raw, value, unit, desc, human)


@dataclass
class Frame:
    """One checksum-validated Text block."""

    fields: dict[str, TypedValue] = field(default_factory=dict)

    def __getitem__(self, label: str) -> TypedValue:
        return self.fields[label]

    def __contains__(self, label: str) -> bool:
        return label in self.fields

    def get(self, label: str) -> TypedValue | None:
        return self.fields.get(label)

    def describe(self, label: str) -> str:
        tv = self.fields[label]
        u = f" {tv.unit}" if tv.unit else ""
        h = f" [{tv.human}]" if tv.human else ""
        return f"{label}: {tv.raw}{u} ({tv.desc}){h}"

    def to_si(self) -> dict[str, float | int | str | None]:
        """Wire units converted to SI-ish floats (V, A, kWh, ...)."""
        out: dict[str, float | int | str | None] = {}
        for label, tv in self.fields.items():
            v = tv.value
            if v is None:
                out[label] = None
            elif label in ("V", "V2", "V3", "VS", "VM", "VPV", "H7", "H8", "H15", "H16"):
                out[label] = v / 1000  # type: ignore[operator]
            elif label in ("I", "I2", "I3", "IL"):
                out[label] = v / 1000  # type: ignore[operator]
            elif label in ("CE", "H1", "H2", "H3", "H6"):
                out[label] = v / 1000  # type: ignore[operator]  # mAh -> Ah
            elif label in ("H17", "H18", "H19", "H20", "H22"):
                out[label] = v / 100  # type: ignore[operator]  # 0.01kWh -> kWh
            elif label == "SOC":
                out[label] = v / 10  # type: ignore[operator]  # -> %
            elif label == "DM":
                out[label] = v / 10  # type: ignore[operator]  # per mille -> %
            elif label == "AC_OUT_V":
                out[label] = v / 100  # type: ignore[operator]
            elif label == "AC_OUT_I":
                out[label] = v / 10  # type: ignore[operator]
            elif label == "DC_IN_V":
                out[label] = v / 100  # type: ignore[operator]
            elif label == "DC_IN_I":
                out[label] = v / 10  # type: ignore[operator]
            else:
                out[label] = v  # type: ignore[assignment]
        return out


class TextParser:
    """Incremental parser. Feed raw serial bytes, pop validated Frames.

    Handles: interspersed HEX lines (starting with ':'), multi-block devices
    (BMV sends PID-block + history-block alternately), partial reads.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._hex_buf: bytearray | None = None
        self._seen_checksum = False
        self._discard_block = False

    def feed(self, data: bytes) -> list[Frame]:
        """Feed a mixed VE.Direct stream and return validated Text frames.

        HEX frames are removed wherever they occur, as required by Victron's
        reference frame handler.  Use :meth:`feed_events` when the caller also
        needs the extracted HEX lines.
        """
        frames, _ = self.feed_events(data)
        return frames

    def feed_events(self, data: bytes) -> tuple[list[Frame], list[bytes]]:
        """Feed mixed bytes and return ``(text_frames, complete_hex_lines)``.

        A HEX frame can begin immediately after a binary Text checksum or can
        interrupt a Text record.  Looking only for ``^:`` lines is therefore
        incorrect; this byte-state demultiplexer recognizes ``:`` everywhere
        except when it is the raw checksum byte following ``Checksum\\t``.
        """
        hex_lines: list[bytes] = []
        for byte in data:
            if self._hex_buf is not None:
                self._hex_buf.append(byte)
                if byte == 0x0A:
                    hex_lines.append(bytes(self._hex_buf))
                    self._hex_buf = None
                elif len(self._hex_buf) > 8192:
                    # A missing LF must not grow memory forever.  Abandon the
                    # malformed HEX candidate and resynchronize on Text/colon.
                    self._hex_buf = None
                continue

            checksum_byte_expected = _endswith_checksum_tab(self._buf)
            if byte == ord(":") and not checksum_byte_expected:
                self._hex_buf = bytearray(b":")
                continue
            self._buf.append(byte)

        frames = self._extract_frames()
        return frames, hex_lines

    def _extract_frames(self) -> list[Frame]:
        frames: list[Frame] = []
        while True:
            idx = _find_checksum_tab(self._buf)
            if idx == -1:
                break
            cs_pos = idx + len(_CHECKSUM_TAB)
            if len(self._buf) <= cs_pos:
                break

            candidate = bytes(self._buf[: cs_pos + 1])
            if not self._seen_checksum:
                # A capture may start with bytes before the first record.
                # Only this initial prefix can be discarded; trying later
                # record boundaries can make corrupted measurements appear
                # checksum-valid by dropping part of their block.
                start = candidate.find(b"\r\n")
                candidate = candidate[start:] if start >= 0 else b""
            if (not self._discard_block and candidate.startswith(b"\r\n")
                    and verify_block_checksum(candidate)):
                frames.append(self._parse_block(candidate))
            # Even an invalid block establishes where the next block starts.
            # If a checksum marker was lost, drop through the next marker.
            self._seen_checksum = True
            self._discard_block = False
            del self._buf[: cs_pos + 1]

        if len(self._buf) > 8192:
            # Preserve the most recent record boundary where possible.
            cut = self._buf.rfind(b"\r\n", 0, len(self._buf) - 4096)
            del self._buf[: cut if cut >= 0 else len(self._buf) - 4096]
            # Truncated bytes still belong to the current block. Never
            # validate a suffix after losing its checksum contribution.
            self._discard_block = True
        return frames

    def _parse_block(self, block: bytes) -> Frame:
        # Decode latin-1 so checksum byte never breaks decoding; split lines.
        text = block.decode("latin-1")
        fields: dict[str, TypedValue] = {}
        for line in text.split("\r\n"):
            if not line or "\t" not in line:
                continue
            label, _, raw = line.partition("\t")
            label = label.strip()
            # Checksum value is the raw checksum byte; skip storing as measurement
            # but keep a marker.
            if label.lower() == "checksum":
                continue
            if label.startswith(":"):
                continue  # interspersed HEX line
            fields[label] = _decode_label(label, raw.strip())
        return Frame(fields)

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)
