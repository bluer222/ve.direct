#!/usr/bin/env python3
"""
Parsers for official Victron Energy VE.Direct protocol specifications.

Reads extracted text files for:
  1. BlueSolar MPPT HEX protocol (Rev 18)
  2. Orion-XS DC-DC charger HEX protocol
  3. BMV-7xx & SmartShunt HEX protocol (Rev 7)
  4. Phoenix Inverter HEX protocol (Rev 5)
  5. VE.Direct Text Mode protocol (Rev 3.34)

Conventions used below (also relied upon by tests/test_spec_parsers.py):
  * ``scale=None`` means the document genuinely gives no numeric factor
    (mode/bitmask/struct/write-only rows). It never means "not parsed".
  * ``data_type`` outside {un8,un16,un24,un32,sn8,sn16,sn32,string*} means the
    document gives no single wire width (``struct`` for byte-layout records,
    ``write_only`` for command-only registers, ``unknown`` otherwise).
  * ``notes`` carries provenance flags: ``range-expanded``, ``table-derived``,
    ``ID-only``, ``detail-mention``, ``doc-typo``.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class ParsedRegister:
    id: int
    name: str
    description: str
    scale: Optional[float] = None
    data_type: str = "unknown"
    unit: Optional[str] = None
    access: str = "R/W"
    source: str = ""
    notes: str = ""
    sentinels: Set[int] = field(default_factory=set)


@dataclass
class ParsedTextField:
    label: str
    units: str
    description: str
    devices: List[str] = field(default_factory=list)


@dataclass
class ParsedPID:
    pid: int
    model_name: str
    phased_out: bool = False


#: "(register 0xEFF0)" appears in BlueSolar-HEX §Charge current limit and in
#: Orion-XS-HEX §Charge current limit; both are typos for 0xEDF0 (the battery
#: maximum current setting both paragraphs describe). Never a real register.
DOC_TYPO_REGISTER_IDS = frozenset({0xEFF0})

#: Descriptions implying command-only (write) semantics for ID-only rows.
_WRITE_HINTS = ("restore", "clear history", "zero current", "synchronize")

#: Device columns of the VE.Direct Text Mode fields tables, left to right.
TEXT_DEVICES = ["BMV60x", "BMV70x", "BMV71x", "MPPT", "PhoenixInverter", "PhoenixCharger", "BuckBoost"]

_DTYPE_RE = re.compile(r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*)$", re.IGNORECASE)


def _register_name(desc: str) -> str:
    base = re.sub(r"\s*\(\*.*?\)", "", desc).strip()
    base = base.split("(")[0].strip()
    return base.upper().replace(" ", "_").replace("-", "_").replace(".", "_")


def _expand_id_range(a: int, b: int, cap: int = 64) -> List[int]:
    """Expand 0xAAAA..0xBBBB into individual IDs (guarded against huge spans)."""
    if 0 < b - a <= cap:
        return list(range(a, b + 1))
    return []


def _is_write_desc(desc: str) -> bool:
    return any(h in desc.lower() for h in _WRITE_HINTS)


class BlueSolarHEXParser:
    """Parser for BlueSolar / SmartSolar MPPT HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> BlueSolarHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self._parse_section_tables(regs)
        self._parse_id_ranges(regs)
        self._parse_tracker_mirrors(regs)
        self._parse_history_restore_id_rows(regs)
        self._parse_detail_mentions(regs)
        return regs

    def _section_text(self) -> str:
        start = self.text.find("1.1 Get and Set items")
        end = self.text.find("1.2 Asynchronous items")
        return self.text[start:end] if end > start else self.text[start:]

    def _parse_section_tables(self, regs: Dict[int, ParsedRegister]) -> None:
        section = self._section_text()

        pattern_std = re.compile(
            r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#]+?)\s{2,}([0-9.]+|\-)\s+([a-z0-9*()]+)\s*(\S.*)?$"
        )
        pattern_display = re.compile(
            r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#=]+?)\s{2,}(un8|un16|un32|sn16|sn32|string)\s*(\S.*)?$"
        )

        for line in section.splitlines():
            if not re.match(r"^\s*0x[0-9A-Fa-f]{4}\b", line):
                continue
            if any(skip in line for skip in ["0xEDEF will always", "A VE.Direct TX", "firmware version"]):
                continue

            m = pattern_std.match(line)
            if m:
                rid = int(m.group(1), 16)
                desc = m.group(2).strip()
                scale_str = m.group(3).strip()
                dtype = m.group(4).strip()
                unit_str = m.group(5).strip() if m.group(5) else None

                scale = float(scale_str) if scale_str != "-" else None
                sentinels = set()
                if unit_str and "0xFFFF" in unit_str:
                    sentinels.add(0xFFFF)

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=_register_name(desc),
                    description=desc,
                    scale=scale,
                    data_type=dtype,
                    unit=unit_str,
                    source="BlueSolar-HEX",
                    sentinels=sentinels,
                )
                continue

            m_disp = pattern_display.match(line)
            if m_disp:
                rid = int(m_disp.group(1), 16)
                desc = m_disp.group(2).strip()
                dtype = m_disp.group(3).strip()
                unit_str = m_disp.group(4).strip() if m_disp.group(4) else None

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=_register_name(desc),
                    description=desc,
                    scale=None,
                    data_type=dtype,
                    unit=unit_str,
                    source="BlueSolar-HEX",
                )
                continue

    def _parse_id_ranges(self, regs: Dict[int, ParsedRegister]) -> None:
        """Expand multi-line ``0xAAAA ...`` / bare ``0xBBBB`` pairs (timer
        events 0xEDA0..0xEDA5, daily history, MPPT history) and inline
        ``0xAAAA..0xBBBB`` history headers into struct records."""
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            for m in re.finditer(r"0x([0-9A-Fa-f]{4})\s*\.\.\s*0x([0-9A-Fa-f]{4})", line):
                a, b = int(m.group(1), 16), int(m.group(2), 16)
                # Only register-listing contexts (history/timer/cycle/vreg
                # headers); never settings Range columns.
                if not re.search(r"[Rr]egister|vreg|history|History|Timer|Cycle|0x104F|0x1050", line):
                    continue
                for rid in _expand_id_range(a, b):
                    if rid not in regs:
                        regs[rid] = ParsedRegister(
                            id=rid,
                            name=f"HISTORY_{rid:04X}",
                            description=f"History record 0x{rid:04X} (range 0x{a:04X}..0x{b:04X})",
                            scale=None,
                            data_type="struct",
                            unit=None,
                            access="R",
                            source="BlueSolar-HEX",
                            notes=f"range-expanded from 0x{a:04X}..0x{b:04X}; byte-field layout, no single wire type",
                        )
            m = re.match(r"^\s*0x([0-9A-Fa-f]{4})\s*\.\.\.\s*$", line)
            if m:
                a = int(m.group(1), 16)
                for j in range(i + 1, min(i + 4, len(lines))):
                    m2 = re.match(r"^\s*0x([0-9A-Fa-f]{4})\s*$", lines[j])
                    if m2:
                        b = int(m2.group(1), 16)
                        kind = "Timer event" if 0xEDA0 <= a <= b <= 0xEDA5 else "History record"
                        for rid in _expand_id_range(a, b):
                            if rid not in regs:
                                regs[rid] = ParsedRegister(
                                    id=rid,
                                    name=f"TIMER_EVENT_{rid - a}" if kind == "Timer event" else f"HISTORY_{rid:04X}",
                                    description=f"{kind} 0x{rid:04X} (range 0x{a:04X}...0x{b:04X})",
                                    scale=None,
                                    data_type="struct",
                                    unit=None,
                                    access="R",
                                    source="BlueSolar-HEX",
                                    notes=f"range-expanded multi-line 0x{a:04X}...0x{b:04X}; doc Type is a 32-bit word/bit-field layout" if kind == "Timer event" else f"range-expanded multi-line 0x{a:04X}...0x{b:04X}; byte-field layout, no single wire type",
                                )
                        break

    def _parse_tracker_mirrors(self, regs: Dict[int, ParsedRegister]) -> None:
        """Parse the 4-column per-tracker table (no scale/type columns):
        inherit scale/dtype/unit from the referenced base register."""
        for line in self.text.splitlines():
            m = re.match(
                r"^\s*([A-Za-z][A-Za-z0-9 _\-/()]*?)\(see\s+(0x[0-9A-Fa-f]{4})\)\s+((?:0x[0-9A-Fa-f]{4}\s*)+)\s*$",
                line,
            )
            if not m:
                continue
            base_id = int(m.group(2), 16)
            ids = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{4}", m.group(3))]
            base = regs.get(base_id)
            for idx, rid in enumerate(ids, start=1):
                if rid in regs:
                    continue
                regs[rid] = ParsedRegister(
                    id=rid,
                    name=f"{base.name}_{idx}" if base else f"TRACKER_{rid:04X}_{idx}",
                    description=m.group(1).strip() + f" tracker {idx}",
                    scale=base.scale if base else None,
                    data_type=base.data_type if base else "unknown",
                    unit=base.unit if base else None,
                    access=base.access if base else "R",
                    source="BlueSolar-HEX",
                    notes=f"table-derived: per-tracker mirror of 0x{base_id:04X}",
                )

    def _parse_history_restore_id_rows(self, regs: Dict[int, ParsedRegister]) -> None:
        """Parse ID+description-only rows of the Restore/History tables
        (0x0004, 0x1030, 0x104F): write-only for command verbs, struct for
        history records."""
        start = self.text.find("Restore factory defaults")
        end = self.text.find("1.2 Asynchronous items")
        region = self.text[start:end] if 0 <= start < end else self.text[start:]
        for line in region.splitlines():
            if "..." in line or ".." in line or "|" in line or "=" in line:
                continue
            m = re.match(r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Z][A-Za-z0-9 _\-/().*]*?)\s*$", line)
            if not m:
                continue
            rid = int(m.group(1), 16)
            desc = m.group(2).strip()
            if any(w in desc for w in ("MPPT", "BlueSolar", "SmartSolar", "VE.Can", "Inverter", "Charger")):
                continue
            if rid in regs or rid in DOC_TYPO_REGISTER_IDS:
                continue
            write_only = _is_write_desc(desc)
            regs[rid] = ParsedRegister(
                id=rid,
                name=_register_name(desc),
                description=desc,
                scale=None,
                data_type="write_only" if write_only else "struct",
                unit=None,
                access="W" if write_only else "R",
                source="BlueSolar-HEX",
                notes="ID-only row; no scale/type columns in doc",
            )

    def _parse_detail_mentions(self, regs: Dict[int, ParsedRegister]) -> None:
        """Collect IDs referenced as ``(register 0xXXXX)`` detail headers so
        nothing described outside the 1.1 table is silently dropped."""
        for m in re.finditer(
            r"[Rr]egisters?\s+0x[0-9A-Fa-f]{4}(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0x[0-9A-Fa-f]{4})*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{4}", m.group(0))]
            for rid in ids:
                if rid in regs or rid in DOC_TYPO_REGISTER_IDS:
                    continue
                line_start = self.text.rfind("\n", 0, m.start()) + 1
                line_end = self.text.find("\n", m.end())
                header = self.text[line_start:line_end if line_end > 0 else len(self.text)].strip()
                desc = re.sub(r"\s*\(registers?.*?\)", "", header).strip(" :-.") or f"Register 0x{rid:04X}"
                write_only = _is_write_desc(desc)
                regs[rid] = ParsedRegister(
                    id=rid,
                    name=_register_name(desc),
                    description=desc,
                    scale=None,
                    data_type="write_only" if write_only else "unknown",
                    unit=None,
                    access="W" if write_only else "R",
                    source="BlueSolar-HEX",
                    notes="detail-mention: described outside the 1.1 table; no scale/type in doc",
                )

    def parse_capabilities(self) -> Dict[int, str]:
        caps: Dict[int, str] = {}
        pos = self.text.find("Capabilities (register 0x0140)")
        if pos < 0:
            return caps
        section = self.text[pos : pos + 1500]
        for line in section.splitlines():
            m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z0-9_()./,\-\s]+?)(?:\s*\(0=no.*)?$", line)
            if m:
                bit = int(m.group(1))
                name = m.group(2).strip()
                if "Bit" not in name and "register" not in name:
                    caps[bit] = name
        return caps


class OrionXSHEXParser:
    """Parser for Orion-XS HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> OrionXSHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self._parse_section_tables(regs)
        self._parse_id_ranges(regs)
        self._parse_history_restore_id_rows(regs)
        self._parse_detail_mentions(regs)
        return regs

    def _parse_section_tables(self, regs: Dict[int, ParsedRegister]) -> None:
        start = self.text.find("1.1 Get and Set items")
        end = self.text.find("1.2 Asynchronous items")
        section = self.text[start:end] if end > start else self.text[start:]

        pattern = re.compile(
            r"^\s*(?:0x|x)([0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#]+?)\s{2,}([0-9.]+|\-)\s+([a-z0-9]+)\s*(\S.*)?$"
        )

        for line in section.splitlines():
            m = pattern.match(line)
            if m:
                rid = int(m.group(1), 16)
                # Doc typo: "xEDE0 Battery low temperature level" (missing
                # leading 0; cf. BlueSolar-HEX 0xEDE0 with identical metadata).
                typo = line.lstrip().startswith("x") and not line.lstrip().startswith("0x")
                desc = m.group(2).strip()
                scale_str = m.group(3).strip()
                dtype = m.group(4).strip()
                unit_str = m.group(5).strip() if m.group(5) else None

                scale = float(scale_str) if scale_str != "-" else None
                sentinels = set()
                if unit_str and "0xFFFF" in unit_str:
                    sentinels.add(0xFFFF)

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=_register_name(desc),
                    description=desc,
                    scale=scale,
                    data_type=dtype,
                    unit=unit_str,
                    source="Orion-XS-HEX",
                    sentinels=sentinels,
                    notes="doc-typo: ID printed as xEDE0, read as 0xEDE0" if typo else "",
                )

    def _parse_id_ranges(self, regs: Dict[int, ParsedRegister]) -> None:
        """Expand inline ``0x1070..0x1098`` cycle-history ranges."""
        for line in self.text.splitlines():
            for m in re.finditer(r"0x([0-9A-Fa-f]{4})\s*\.\.\s*0x([0-9A-Fa-f]{4})", line):
                a, b = int(m.group(1), 16), int(m.group(2), 16)
                if not re.search(r"[Rr]egister|history|History|Cycle", line):
                    continue
                for rid in _expand_id_range(a, b):
                    if rid not in regs:
                        regs[rid] = ParsedRegister(
                            id=rid,
                            name=f"CYCLE_HISTORY_{rid - a}",
                            description=f"Cycle history 0x{rid:04X} (range 0x{a:04X}..0x{b:04X})",
                            scale=None,
                            data_type="struct",
                            unit=None,
                            access="R",
                            source="Orion-XS-HEX",
                            notes=f"range-expanded from 0x{a:04X}..0x{b:04X}; byte-field layout, no single wire type",
                        )

    def _history_region(self) -> Tuple[int, int]:
        start = self.text.find("Restore factory defaults")
        if start < 0:
            start = self.text.find("History data")
        end = self.text.find("1.2 Asynchronous items")
        return start, end if 0 <= start < end else len(self.text)

    def _parse_history_restore_id_rows(self, regs: Dict[int, ParsedRegister]) -> None:
        """Parse the ID+description-only history block (0x1030, 0x106F,
        0x1099, 0x1043, 0x1042) and 0x0004 with documented access defaults."""
        start, end = self._history_region()
        region = self.text[start:end] if start >= 0 else self.text
        for line in region.splitlines():
            if "..." in line or ".." in line or "|" in line or "=" in line:
                continue
            m = re.match(r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Z][A-Za-z0-9 _\-/().*]*?)\s*$", line)
            if not m:
                # Single-line ".." range already handled; dotted triple
                # ("0x1070..0x1098  Cycle history ...") is range-expanded.
                continue
            rid = int(m.group(1), 16)
            desc = m.group(2).strip()
            if any(w in desc for w in ("MPPT", "BlueSolar", "SmartSolar", "Orion", "VE.Can")):
                continue
            if rid in regs or rid in DOC_TYPO_REGISTER_IDS:
                continue
            write_only = _is_write_desc(desc)
            is_history = "history" in desc.lower() or "cycle" in desc.lower()
            regs[rid] = ParsedRegister(
                id=rid,
                name=_register_name(desc),
                description=desc,
                scale=None,
                data_type="write_only" if write_only else ("struct" if is_history else "unknown"),
                unit=None,
                access="W" if write_only else "R",
                source="Orion-XS-HEX",
                notes="ID-only row; no scale/type columns in doc, access default documented here",
            )

    def _parse_detail_mentions(self, regs: Dict[int, ParsedRegister]) -> None:
        """Extract IDs described outside the 1.1 table: 0x0206 (device
        function), 0xEDDA (error/warning table), 0x2013 (prose-only),
        0x0150/0x0090 (single-field Bytes records), 0x0004."""
        hist_start = self.text.find("History data")
        if hist_start < 0:
            hist_start = self.text.find("Historical data")
        remote_start = self.text.find("Remote control")
        for m in re.finditer(
            r"[Rr]egisters?\s+0x[0-9A-Fa-f]{4}(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0x[0-9A-Fa-f]{4})*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{4}", m.group(0))]
            # Inline ".." ranges inside mentions are expanded by
            # _parse_id_ranges; keep only single IDs here.
            if ".." in m.group(0) or "..." in m.group(0):
                continue
            for rid in ids:
                if rid in regs or rid in DOC_TYPO_REGISTER_IDS:
                    continue
                line_start = self.text.rfind("\n", 0, m.start()) + 1
                line_end = self.text.find("\n", m.end())
                header = self.text[line_start:line_end if line_end > 0 else len(self.text)].strip()
                desc = re.sub(r"\s*\(registers?.*?\)", "", header).strip(" :-.") or f"Register 0x{rid:04X}"
                write_only = _is_write_desc(desc)
                in_history = 0 <= hist_start < m.start() < remote_start
                if in_history and not write_only:
                    # History-block records (0x1042/0x1043/0x106F/0x1099):
                    # documented as byte-field layouts, modelled as structs.
                    dtype, scale = "struct", None
                    note = "detail-mention: history-block record; byte-field layout, no single wire type"
                else:
                    dtype, scale = self._single_field_bytes_width(m.end())
                    if dtype is None:
                        dtype = "write_only" if write_only else "unknown"
                    note = (
                        "detail-mention: described outside the 1.1 table; single-field Bytes record width"
                        if dtype not in ("unknown", "write_only")
                        else "detail-mention: described outside the 1.1 table; no scale/type in doc"
                    )
                regs[rid] = ParsedRegister(
                    id=rid,
                    name=_register_name(desc),
                    description=desc,
                    scale=scale,
                    data_type=dtype,
                    unit=None,
                    access="W" if write_only else "R",
                    source="Orion-XS-HEX",
                    notes=note,
                )

    def _single_field_bytes_width(self, pos: int) -> Tuple[Optional[str], Optional[float]]:
        """If the mention is followed by a single-field Bytes layout table
        (BLE capabilities 0x0150, BLE mode 0x0090, cycle count 0x106F),
        return its (dtype, scale); else (None, None)."""
        lines = self.text[pos:].splitlines()[:12]
        rows: List[Tuple[str, Optional[float]]] = []
        in_table = False
        for line in lines:
            if re.match(r"^\s*Bytes\s+Description", line):
                in_table = True
                continue
            if in_table:
                m = re.match(
                    r"^\s*\d+\s+.+?\s+(-|[0-9.]+)\s+(un\d+|sn\d+|string\d*)\s*(\S.*)?$", line
                )
                if m:
                    scale = float(m.group(1)) if m.group(1) != "-" else None
                    rows.append((m.group(2).lower(), scale))
                elif line.strip() == "":
                    continue
                else:
                    break
        if len(rows) == 1:
            return rows[0]
        return None, None

    def parse_capabilities(self) -> Dict[int, str]:
        caps: Dict[int, str] = {}
        pos = self.text.find("Capabilities1 (register 0x0140)")
        if pos < 0:
            return caps
        section = self.text[pos : pos + 1800]
        for line in section.splitlines():
            m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z0-9_()./,\-\s]+?)(?:\s*\(0=no.*)?$", line)
            if m:
                bit = int(m.group(1))
                name = m.group(2).strip()
                if "Bit" not in name and "register" not in name:
                    caps[bit] = name
        return caps


class BMV7xxHEXParser:
    """Parser for BMV-7xx and SmartShunt HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> BMV7xxHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        for line in self.text.splitlines():
            if not re.match(r"^\s*0x[0-9A-Fa-f]{4}\b", line):
                continue
            if any(skip in line for skip in ["talking to", "BMV712 = 0xA381", "0xA381 = BMV712"]):
                continue

            parts = re.split(r"\s{2,}", line.strip())
            if not parts or not re.match(r"^0x[0-9A-Fa-f]{4}$", parts[0]):
                continue

            rid = int(parts[0], 16)
            desc = parts[1] if len(parts) > 1 else ""
            dtype = "unknown"
            scale: Optional[float] = None
            unit: Optional[str] = None

            for p in parts[2:]:
                if _DTYPE_RE.match(p.strip()):
                    dtype = p.strip().lower()
                    break

            # Settings layout: ID, Description, Display, Type, Range(a..b),
            # Default, Step, Unit. The scale factor is the Step column
            # (including integer steps such as "1", which carry scale 1.0).
            range_idx = next((i for i, p in enumerate(parts) if i >= 2 and ".." in p), None)
            if range_idx is not None and len(parts) > range_idx + 2:
                step = parts[range_idx + 2].strip()
                if re.fullmatch(r"\d+(\.\d+)?", step):
                    scale = float(step)
                if len(parts) > range_idx + 3:
                    u = parts[range_idx + 3].strip()
                    if u == "-" or "(" in u:
                        unit = None
                    else:
                        unit = u or None
            else:
                # Short layout (product info / monitor / historic): the scale
                # is embedded in the Unit column ("0.01 V", "0.001A").
                is_write_only = "(write only)" in desc.lower()
                for p in parts[2:]:
                    p_clean = p.strip()
                    if not is_write_only and re.match(r"^(0?\.\d+)\s*([A-Za-z%°]+)?$", p_clean):
                        sm = re.match(r"^(0?\.\d+)\s*([A-Za-z%°]+)?$", p_clean)
                        if sm:
                            scale = float(sm.group(1))
                            unit = sm.group(2)
                    elif p_clean in ["V", "A", "W", "Ah", "seconds", "minutes", "min.", "K", "%"]:
                        unit = p_clean

            if "(write only)" in desc.lower():
                access = "WRITE_ONLY"
                scale = None
            elif "(read-only)" in desc.lower() or "(read only)" in desc.lower():
                access = "READ_ONLY"
            else:
                access = "READ_WRITE"
            clean_desc = re.sub(r"\s*\(read-only\)|\s*\(read only\)|\s*\(\*\)|\s*\(\d+\)", "", desc).strip()
            clean_desc = re.sub(r"\s*\*$", "", clean_desc).strip()
            name = "BMV_" + clean_desc.upper().replace(" ", "_").replace("-", "_").replace(".", "_")

            regs[rid] = ParsedRegister(
                id=rid,
                name=name,
                description=desc,
                scale=scale,
                data_type=dtype,
                unit=unit,
                access=access,
                source="BMV-7xx-HEX",
            )
        return regs


class PhoenixInverterHEXParser:
    """Parser for Phoenix Inverter HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> PhoenixInverterHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        for line in self.text.splitlines():
            # Token-split (not 2+-whitespace split): rows such as 0xEB99 use
            # single spaces between columns. Register names contain no spaces.
            if "VE_REG_" not in line:
                continue
            tokens = line.split()
            if len(tokens) < 3 or not re.fullmatch(r"0x[0-9A-Fa-f]{4}", tokens[0]):
                continue

            rid = int(tokens[0], 16)
            name = tokens[1].strip()
            access = tokens[2].strip()
            dtype = "unknown"
            if len(tokens) >= 4 and re.match(r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*|-)$", tokens[3], re.IGNORECASE):
                dtype = tokens[3].strip().lower()
                if dtype == "-":
                    dtype = "unknown"

            scale = None
            unit = None
            if len(tokens) >= 5:
                p4 = tokens[4].strip()
                if not p4.endswith(")") and p4 != "-":
                    try:
                        scale = float(p4)
                    except ValueError:
                        scale = None
            if len(tokens) >= 6:
                p5 = tokens[5].strip()
                if not re.fullmatch(r"\d+\)", p5) and p5 != "-":
                    unit = re.sub(r"\s*\d+\)", "", p5).strip()
                    if unit in ("-", ""):
                        unit = None

            regs[rid] = ParsedRegister(
                id=rid,
                name=name,
                description=name,
                scale=scale,
                data_type=dtype,
                unit=unit,
                access=access,
                source="Phoenix-HEX",
            )
        return regs

    def parse_ble_header_evidence(self) -> Dict[int, str]:
        """Record section headers claiming a BLE-capabilities register ID.

        §2.2.5 is headed "Capabilities BLE (register 0x0160)" while its
        product-info table (and the BMV doc) use 0x0150; the code DB treats
        the heading as a Victron typo for 0x0150. This returns the header ID
        as evidence instead of silently dropping it.
        """
        evidence: Dict[int, str] = {}
        for m in re.finditer(r"Capabilities BLE\s*\(register\s+(0x[0-9A-Fa-f]{4})\)", self.text):
            hid = int(m.group(1), 16)
            if hid != 0x0150:
                evidence[hid] = (
                    "section-header ID; product-info table uses 0x0150 "
                    "(known Victron typo, not a second register)"
                )
        return evidence


class VEDirectTextParser:
    """Parser for VE.Direct Text Mode Protocol 3.34 documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> VEDirectTextParser:
        return cls(path.read_text(errors="ignore"))

    def _fields_section(self) -> str:
        start = self.text.find("The units used by the serial interface are as follows:")
        end = self.text.find("Table I Supported Text-mode fields")
        return self.text[start:end] if end > start else ""

    def _device_column_starts(self, lines: List[str]) -> List[Tuple[int, List[int]]]:
        """Locate each fields-table header ("BuckBoost ... 60x" line) and
        derive the 7 device-column x-starts from it + the header line above
        (which holds the MPPT title)."""
        tables: List[Tuple[int, List[int]]] = []
        for i, line in enumerate(lines):
            if "BuckBoost" in line and "60x" in line and i > 0:
                prev = lines[i - 1]
                starts = [
                    line.find("60x"),
                    line.find("70x"),
                    line.find("SmartShunt"),
                    prev.find("MPPT"),
                    line.find("Inverter"),
                    line.find("Charger"),
                    line.find("BuckBoost"),
                ]
                if all(s >= 0 for s in starts):
                    tables.append((i, starts))
        return tables

    def parse_fields(self) -> Dict[str, ParsedTextField]:
        fields: Dict[str, ParsedTextField] = {}
        section = self._fields_section()
        lines = section.splitlines()
        tables = self._device_column_starts(lines)

        label_re = re.compile(
            r"^\s{10,25}([A-Za-z0-9_#]+)\s{2,}([A-Za-z0-9/%#\-\.\s]+?)\s{2,}(.+)$"
        )
        stoplist = {
            "Label", "Table", "The", "Note", "Victron", "General", "E-mail",
            "When", "Both", "Available", "60x",
        }

        pending: Optional[List[Any]] = None  # [label, units, desc, raw_lines]

        def table_starts(idx: int) -> List[int]:
            cur: List[int] = []
            for hidx, starts in tables:
                if hidx < idx:
                    cur = starts
            return cur

        def devices_for(raw_lines: List[str], starts: List[int]) -> List[str]:
            found: Set[int] = set()
            for raw in raw_lines:
                for m in re.finditer("•", raw):
                    p = m.start()
                    col = 0
                    for s, start in enumerate(starts):
                        if start <= p + 1:
                            col = s
                    found.add(col)
            return [TEXT_DEVICES[c] for c in sorted(found)] if starts else []

        def flush(idx: int) -> None:
            if pending is None:
                return
            label, units, desc, raw_lines = pending
            fields[label] = ParsedTextField(
                label=label, units=units, description=desc,
                devices=devices_for(raw_lines, table_starts(idx)),
            )

        for idx, line in enumerate(lines):
            m = label_re.match(line)
            if m:
                label = m.group(1).strip()
                if label in stoplist:
                    flush(idx)
                    pending = None
                    continue
                flush(idx)
                pending = [label, m.group(2).strip(), m.group(3).strip(), [line]]
            elif pending is not None:
                pending[3].append(line)
        flush(len(lines))
        return fields

    def parse_pids(self) -> Dict[int, ParsedPID]:
        pids: Dict[int, ParsedPID] = {}
        pos = self.text.find("PID\n                       The product id:")
        if pos < 0:
            pos = self.text.find("The product id:")
        section = self.text[pos:]

        for line in section.splitlines():
            if "Implementation guidelines" in line or "Connector types" in line:
                break
            # Name class includes '*', '.', '|', '+' so phased-out ("70|15*"),
            # dotted ("VE.Can"), and suffixed ("(1+1)") models all match.
            m = re.search(r"^\s*([A-Za-z0-9\s|/\-().*+]+?)\s{2,}(0x[0-9A-Fa-f]{3,4})(\*)?", line)
            if m:
                name_raw = m.group(1).strip()
                # Prose cross-references ("From 0xA10B to ...") are not models.
                if "0x" in name_raw or "0X" in name_raw:
                    continue
                pid_hex = m.group(2).strip()
                phased = bool(m.group(3)) or name_raw.endswith("*")
                name = name_raw.rstrip("*").strip()
                if not any(kw in name for kw in ["Product", "register", "vreg", "Command", "Table"]):
                    pid = int(pid_hex, 16)
                    pids[pid] = ParsedPID(pid=pid, model_name=name, phased_out=phased)
        return pids

    def parse_cs_states(self) -> Dict[int, str]:
        states: Dict[int, str] = {}
        pos = self.text.find("The state of operation. See the table below")
        if pos < 0:
            return states
        # Bound the section at the next field header instead of truncating
        # after a fixed character window (which dropped 11/245-248/252).
        tail = self.text[pos:]
        m_end = re.search(r"^\s*ERR\s*$", tail, re.MULTILINE)
        section = tail[: m_end.start()] if m_end else tail
        for line in section.splitlines():
            m = re.match(r"^\s*([A-Za-z][A-Za-z\s/()\-]*?)\s+(\d{1,3})\s+(?:•|\d)", line)
            if m:
                name = m.group(1).strip()
                code = int(m.group(2))
                states[code] = name
        return states

    def parse_errors(self) -> Dict[int, str]:
        errors: Dict[int, str] = {}
        pos = self.text.find("The error code of the device (relevant when the device is in the fault state)")
        if pos < 0:
            return errors
        # Bound the section at HSDS/notes instead of a fixed window (which
        # dropped 66,67,68,116,117,119).
        tail = self.text[pos:]
        ends = [
            m.start()
            for pat in (r"^\s*HSDS\s*$", r"^Note1: Error 19")
            for m in [re.search(pat, tail, re.MULTILINE)]
            if m
        ]
        section = tail[: min(ends)] if ends else tail
        for line in section.splitlines():
            m = re.match(r"^\s*([A-Za-z0-9\s/(),\-]+?)\s{2,}(\d{1,3})\s*$", line)
            if m:
                desc = m.group(1).strip()
                code = int(m.group(2))
                errors[code] = desc
        return errors


def load_all_specs(specs_dir: Path) -> Dict[str, Any]:
    return {
        "bluesolar": BlueSolarHEXParser.from_file(specs_dir / "BlueSolar-HEX-protocol.txt"),
        "orion": OrionXSHEXParser.from_file(specs_dir / "Orion-XS-HEX-protocol.txt"),
        "bmv": BMV7xxHEXParser.from_file(specs_dir / "BMV-7xx-HEX-Protocol.txt"),
        "phoenix": PhoenixInverterHEXParser.from_file(specs_dir / "VE.Direct-HEX-Protocol-Phoenix-Inverter.txt"),
        "text": VEDirectTextParser.from_file(specs_dir / "VE.Direct-Protocol-3.34.txt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse official Victron Energy specifications.")
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "specs",
        help="Directory containing extracted specification text files.",
    )
    args = parser.parse_args()

    specs = load_all_specs(args.specs_dir)
    bs_regs = specs["bluesolar"].parse_registers()
    or_regs = specs["orion"].parse_registers()
    bmv_regs = specs["bmv"].parse_registers()
    ph_regs = specs["phoenix"].parse_registers()
    text_fields = specs["text"].parse_fields()
    pids = specs["text"].parse_pids()
    caps_bs = specs["bluesolar"].parse_capabilities()
    caps_or = specs["orion"].parse_capabilities()

    print(f"Parsed Specifications Summary ({args.specs_dir}):")
    print(f"  BlueSolar MPPT HEX: {len(bs_regs)} registers, {len(caps_bs)} capability bits")
    print(f"  Orion-XS HEX:      {len(or_regs)} registers, {len(caps_or)} capability bits")
    print(f"  BMV-7xx HEX:        {len(bmv_regs)} registers")
    print(f"  Phoenix Inverter:   {len(ph_regs)} registers")
    print(f"  Text Mode Protocol: {len(text_fields)} fields, {len(pids)} product IDs")

    # Detect aliased IDs across specs
    all_spec_regs: Dict[int, List[ParsedRegister]] = {}
    for r in list(bs_regs.values()) + list(or_regs.values()) + list(bmv_regs.values()) + list(ph_regs.values()):
        all_spec_regs.setdefault(r.id, []).append(r)

    aliased = {rid: rlist for rid, rlist in all_spec_regs.items() if len(rlist) > 1}
    print(f"\nDiscovered {len(aliased)} shared/aliased register IDs across HEX specs:")
    for rid, rlist in sorted(aliased.items()):
        sources = ", ".join(r.source for r in rlist)
        scales = "/".join(str(r.scale) for r in rlist)
        print(f"  0x{rid:04X}: scales=[{scales}], sources=[{sources}]")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
