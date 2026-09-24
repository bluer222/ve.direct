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
#: Verbs taken from the docs' own write-only markers: the BMV spec marks
#: command rows explicitly as "(write only)" ("Zero Current", "Synchronize",
#: "Restore Defaults", "Clear History"), and BlueSolar/Orion use the same
#: command verbs in their ID+description tables ("Restore default",
#: "Clear history"). All hints are phrase-guarded / word-anchored (never a
#: bare substring): bare "restore" also matched readables such as "Device
#: restores defaults" or "restore factor", and bare "synchronize" matched
#: "synchronized" (e.g. BMV 0x0FFD "Start synchronized"). "restore default(s)"
#: covers 0x0004 "Restore default" / "Restore Defaults"; "restore factory"
#: covers the "Restore factory defaults" header variant; "reset counter(s)"
#: is plural-aware so "Reset counters" matches while BlueSolar 0xEDDC "User
#: yield (resettable)" never does (space vs "table"). "(write only)" itself
#: is included so any row carrying the doc's literal marker is caught.
_WRITE_HINTS = ("restore default", "restore factory", "clear history", "zero current", "synchronize", "reset counter", "(write only)")

#: Word-anchored regexes for _is_write_desc (plural-aware where the doc
#: uses both forms). Substring matching ("restore" in desc) is the bug:
#: it misclassifies any readable containing the substring. Anchored phrases
#: fail loudly via the write-only divergence test instead.
_WRITE_HINT_RES = (
    re.compile(r"\brestore\s+defaults?\b", re.IGNORECASE),
    re.compile(r"\brestore\s+factory\b", re.IGNORECASE),
    re.compile(r"\bclear\s+history\b", re.IGNORECASE),
    re.compile(r"\bzero\s+current\b", re.IGNORECASE),
    re.compile(r"\bsynchronize\b", re.IGNORECASE),
    re.compile(r"\breset\s+counters?\b", re.IGNORECASE),
    re.compile(r"\(write only\)", re.IGNORECASE),
)

#: Device columns of the VE.Direct Text Mode fields tables, left to right.
TEXT_DEVICES = ["BMV60x", "BMV70x", "BMV71x", "MPPT", "PhoenixInverter", "PhoenixCharger", "BuckBoost"]

_DTYPE_RE = re.compile(r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*)$", re.IGNORECASE)

#: Footnote-only "dtype" cells (BlueSolar 0xEDD1/0xEDD3 "(*4)"): the doc
#: prints a footnote reference where the wire width belongs (the width
#: depends on firmware version, per the doc's own revision note on vregs
#: 0xEDD3/0xEDD1), i.e. no single width is given.
_FOOTNOTE_DTYPE_RE = re.compile(r"^\(\*\d+\)$")

#: A scale-column token: a decimal factor or "-" (doc gives no factor).
_SCALE_TOKEN_RE = re.compile(r"^(?:\d+(?:\.\d+)?|-)$")

#: A bare register-ID token (Orion-XS allows the doc-typo form "xHHHH").
#: The 0X prefix is uppercase-X rescue (e.g. "0XEDF0" for 0xEDF0, "XEDE0"
#: for xEDE0): the docs print hex both ways, and the crude scrapers share
#: the same blind spot, so all three must be case-insensitive for the X
#: (rescue-style like the existing xEDE0 handling). Verified zero-diff:
#: no "0XHHHH"/"XHHHH" occurrence in any current snapshot, so no prose
#: false positives; a future uppercase row is now rescued instead of
#: missed by both sides while counts stay green.
_ID_TOKEN_RE = re.compile(r"^0[xX][0-9A-Fa-f]{4}$")
_ID_TOKEN_BARE_X_RE = re.compile(r"^[xX][0-9A-Fa-f]{4}$")


def _normalize_dtype(raw: str) -> Tuple[str, str]:
    """Normalize a dtype-column token to the module convention.

    Returns ``(dtype, note)``. Known wire widths pass through lowercased
    (matching is case-insensitive, so ``UN16``/``Un32`` normalize); a bare
    ``-`` or footnote-contaminated cell such as BlueSolar ``(*4)`` (no
    single wire width in the doc) normalizes to ``unknown`` with the raw
    cell preserved in the note.
    """
    t = raw.strip()
    if _DTYPE_RE.match(t):
        return t.lower(), ""
    if t in ("-", ""):
        return "unknown", ""
    return "unknown", f"doc dtype cell {t!r} gives no single wire width"


def _split_typed_row(line: str, allow_bare_x_prefix: bool = False) -> Optional[Tuple[int, str, Optional[str], str, Optional[str], bool]]:
    """Token-split one HEX table row into ``(rid, desc, scale_token, dtype_token, unit, typo)``.

    Column gaps may be single spaces (cf. BlueSolar 0xEDE0/0xEDCA single
    space after the ID, Phoenix 0xEB99 single-spaced throughout) and dtype
    matching is case-insensitive, so the split anchors on the scale/dtype
    tokens instead of ``\\s{2,}`` gaps: descriptions may themselves contain
    spaces and numbers. Returns ``None`` when the line is not a typed table
    row (prose mentions, product-ID rows, range headers). ``unit`` is the
    raw remainder (may be ``-`` or contain sentinels, as the callers expect).
    """
    tokens = line.split()
    if len(tokens) < 3:
        return None
    idtok = tokens[0]
    typo = False
    if _ID_TOKEN_RE.match(idtok):
        rid = int(idtok, 16)
    elif allow_bare_x_prefix and _ID_TOKEN_BARE_X_RE.match(idtok):
        # Doc typo: "xEDE0 Battery low temperature level" (missing leading
        # 0; cf. BlueSolar-HEX 0xEDE0 with identical metadata).
        rid = int(idtok[1:], 16)
        typo = True
    else:
        return None
    if tokens[1] == "=":
        # Product-ID prose ("0xA3F0 = Orion XS ..."), not a table row.
        return None
    rest = tokens[1:]

    def is_dtype_like(tok: str) -> bool:
        return bool(_DTYPE_RE.match(tok) or _FOOTNOTE_DTYPE_RE.match(tok))

    # Scale-anchored: "<desc...> <scale> <dtype> [unit...]". Footnote cells
    # ("(*4)") only anchor when no real wire width follows: a footnote
    # inside the description (e.g. "Display ... (*2) un8") must not split it.
    for footnote_ok in (False, True):
        for i, tok in enumerate(rest):
            if not _SCALE_TOKEN_RE.match(tok) or i + 1 >= len(rest):
                continue
            nxt = rest[i + 1]
            if _DTYPE_RE.match(nxt) or (
                footnote_ok
                and _FOOTNOTE_DTYPE_RE.match(nxt)
                and not any(_DTYPE_RE.match(t) for t in rest[i + 2:])
            ):
                desc = " ".join(rest[:i]).strip()
                if not desc:
                    return None
                unit = " ".join(rest[i + 2:]).strip() or None
                return rid, desc, tok, nxt, unit, typo
    # Dtype-anchored (scaleless display/mode rows): "<desc...> <dtype>
    # [unit...]". Real widths win over footnotes for the same reason.
    for footnote_ok in (False, True):
        for i, tok in enumerate(rest):
            if (_DTYPE_RE.match(tok) or (footnote_ok and _FOOTNOTE_DTYPE_RE.match(tok))):
                desc = " ".join(rest[:i]).strip()
                if not desc:
                    return None
                unit = " ".join(rest[i + 1:]).strip() or None
                return rid, desc, None, tok, unit, typo
    return None


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
    return any(rx.search(desc) for rx in _WRITE_HINT_RES)


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

        for line in section.splitlines():
            if not re.match(r"^\s*0[xX][0-9A-Fa-f]{4}\b", line):
                continue
            # Skip known prose-note lines that happen to start with a
            # register-looking ID. Each pattern is anchored to its exact note
            # text: a bare "firmware version" substring would also drop any
            # future table row whose description mentions firmware versions.
            if re.match(r"^\s*0[xX]EDEF will always\b", line):
                continue  # Note 4 continuation on 0xEDEA ("will always reports ...")
            if re.match(r"^\s*0[xX]A046 and higher\)", line):
                continue  # Note 3 continuation ("0xA046 and higher). A VE.Direct TX ...")
            if re.match(r"^\s*0[xX]0300\)", line):
                continue  # History intro ("0x0300). The history registers are ...")

            # Token-anchored split (not \s{2,} gaps): tolerates single-space
            # columns and any-case dtypes; footnote dtypes ("(*4)") fall out
            # of _normalize_dtype as unknown with the cell kept in notes.
            row = _split_typed_row(line)
            if row is None:
                continue
            rid, desc, scale_str, dtype_raw, unit_str, _typo = row

            scale = None if scale_str in (None, "-") else float(scale_str)
            dtype, dtype_note = _normalize_dtype(dtype_raw)
            sentinels = set()
            # Sentinel may print uppercase 0XFFFF; case-insensitive like IDs.
            if unit_str and "0XFFFF" in unit_str.upper():
                sentinels.add(0xFFFF)

            regs[rid] = ParsedRegister(
                id=rid,
                name=_register_name(desc),
                description=desc,
                scale=scale,
                data_type=dtype,
                unit=unit_str,
                source="BlueSolar-HEX",
                notes=dtype_note,
                sentinels=sentinels,
            )

    def _parse_id_ranges(self, regs: Dict[int, ParsedRegister]) -> None:
        """Expand multi-line ``0xAAAA ...`` / bare ``0xBBBB`` pairs (timer
        events 0xEDA0..0xEDA5, daily history, MPPT history) and inline
        ``0xAAAA..0xBBBB`` history headers into struct records.

        Known doc conflict (verified against tests/specs snapshot): the
        1.1-table multi-line MPPT range ends at 0x10BE while the
        "History MPPT day record" detail header prints 0x10A0..0x10BD
        (daily history agrees at 0x106E on both sides; only MPPT
        conflicts). Resolution: the table endpoint wins (0x10BE is a
        real register), so the union below intentionally keeps 31 IDs
        0x10A0..0x10BE; tests/test_spec_parsers.py asserts both
        conflicting endpoints plus this resolution instead of silently
        unioning them."""
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})", line):
                a, b = int(m.group(1), 16), int(m.group(2), 16)
                # Only register-listing contexts (history/timer/cycle/vreg
                # headers); never settings Range columns.
                if not re.search(r"[Rr]egister|vreg|history|History|Timer|Cycle|0[xX]104F|0[xX]1050", line):
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
            m = re.match(r"^\s*0[xX]([0-9A-Fa-f]{4})\s*\.\.\.\s*$", line)
            if m:
                a = int(m.group(1), 16)
                for j in range(i + 1, min(i + 4, len(lines))):
                    m2 = re.match(r"^\s*0[xX]([0-9A-Fa-f]{4})\s*$", lines[j])
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
                                    # Timer events live in the 1.1 Get and Set table (streetlighting
                                    # settings, writable) while history records are read-only;
                                    # the DB models timers as READ_WRITE (see src/ve_direct/registers/__init__.py:373).
                                    access="R/W" if kind == "Timer event" else "R",
                                    source="BlueSolar-HEX",
                                    notes=f"range-expanded multi-line 0x{a:04X}...0x{b:04X}; doc Type is a 32-bit word/bit-field layout" if kind == "Timer event" else f"range-expanded multi-line 0x{a:04X}...0x{b:04X}; byte-field layout, no single wire type",
                                )
                        break

    def _parse_tracker_mirrors(self, regs: Dict[int, ParsedRegister]) -> None:
        """Parse the 4-column per-tracker table (no scale/type columns):
        inherit scale/dtype/unit from the referenced base register."""
        for line in self.text.splitlines():
            m = re.match(
                r"^\s*([A-Za-z][A-Za-z0-9 _\-/()]*?)\(see\s+(0[xX][0-9A-Fa-f]{4})\)\s+((?:0[xX][0-9A-Fa-f]{4}\s*)+)\s*$",
                line,
            )
            if not m:
                continue
            base_id = int(m.group(2), 16)
            ids = [int(x, 16) for x in re.findall(r"0[xX][0-9A-Fa-f]{4}", m.group(3))]
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
            m = re.match(r"^\s*(0[xX][0-9A-Fa-f]{4})\s+([A-Z][A-Za-z0-9 _\-/().*]*?)\s*$", line)
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
            r"[Rr]egisters?\s+0[xX][0-9A-Fa-f]{4}(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0[xX][0-9A-Fa-f]{4})*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0[xX][0-9A-Fa-f]{4}", m.group(0))]
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

        for line in section.splitlines():
            # Token-anchored split (not \s{2,} gaps): tolerates single-space
            # columns and any-case dtypes, like the BlueSolar tables.
            row = _split_typed_row(line, allow_bare_x_prefix=True)
            if row is None:
                continue
            rid, desc, scale_str, dtype_raw, unit_str, typo = row

            scale = None if scale_str in (None, "-") else float(scale_str)
            dtype, dtype_note = _normalize_dtype(dtype_raw)
            sentinels = set()
            # Sentinel may print uppercase 0XFFFF; case-insensitive like IDs.
            if unit_str and "0XFFFF" in unit_str.upper():
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
                notes="doc-typo: ID printed as xEDE0, read as 0xEDE0" if typo else dtype_note,
            )

    def _parse_id_ranges(self, regs: Dict[int, ParsedRegister]) -> None:
        """Expand inline ``0x1070..0x1098`` cycle-history ranges."""
        for line in self.text.splitlines():
            for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})", line):
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
            m = re.match(r"^\s*(0[xX][0-9A-Fa-f]{4})\s+([A-Z][A-Za-z0-9 _\-/().*]*?)\s*$", line)
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
            r"[Rr]egisters?\s+0[xX][0-9A-Fa-f]{4}(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0[xX][0-9A-Fa-f]{4})*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0[xX][0-9A-Fa-f]{4}", m.group(0))]
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
            # Token-anchored split (not 2+-whitespace gaps): rows such as the
            # monitor short-layout lines may use single spaces between
            # columns, and dtypes print capitalized (Un32/Sn16). The Type
            # column anchors the split: Display is the token before it, the
            # description is everything between the ID and Display.
            tokens = line.split()
            if len(tokens) < 3 or not _ID_TOKEN_RE.match(tokens[0]):
                continue
            if tokens[1] == "=":
                # Product-ID prose ("0xA381 = BMV712"), not a table row.
                continue
            # Prose guards case-insensitive for the ID (0X rescue, zero-diff:
            # snapshots carry only lowercase 0xA381, so no new exclusions).
            if any(skip in line or skip.replace("0x", "0X") in line for skip in ["talking to", "BMV712 = 0xA381", "0xA381 = BMV712"]):
                continue

            rid = int(tokens[0], 16)
            anchor = next(
                (i for i, t in enumerate(tokens) if i >= 2 and _DTYPE_RE.match(t)),
                None,
            )
            dtype = "unknown"
            if anchor is None:
                # Write-only command rows ("... (write only) ... - - - -"):
                # no Type width; the Type column is "-" followed by "-"s. The
                # Display column is numeric (or "-"); if the token before the
                # dash run is not Display-shaped it belongs to the
                # description instead (e.g. 0x031F "Alarm Acknowledge - ...").
                dash = next(
                    (i for i, t in enumerate(tokens)
                     if i >= 2 and t == "-" and all(u == "-" for u in tokens[i + 1:])),
                    None,
                )
                if dash is None:
                    continue
                if dash >= 3 and re.fullmatch(r"\d+|-|MAIN|AUX|MID|SOC|[A-H]", tokens[dash - 1]):
                    display_idx, desc_toks = dash - 1, tokens[1:dash - 1]
                else:
                    display_idx, desc_toks = dash, tokens[1:dash]
                rest = []
            else:
                if anchor < 3:
                    continue
                dtype = tokens[anchor].lower()
                display_idx, desc_toks, rest = anchor - 1, tokens[1:anchor - 1], tokens[anchor + 1:]
            if not desc_toks:
                continue
            desc = " ".join(desc_toks)
            display = tokens[display_idx]
            _ = display  # Display column carries no register semantics.
            scale: Optional[float] = None
            unit: Optional[str] = None

            range_idx = next((i for i, p in enumerate(rest) if ".." in p), None)
            if range_idx is not None and len(rest) > range_idx + 2:
                # Settings layout: ID, Description, Display, Type, Range(a..b),
                # Default, Step, Unit. The Range cell may carry parenthesized
                # options ("0..1 (OFF, ON)"), token-split across gaps: skip a
                # "(...)" group after Range to find Default/Step/Unit. The
                # scale factor is the Step column (including integer steps
                # such as "1", which carry scale 1.0).
                col = range_idx + 1
                if col < len(rest) and rest[col].startswith("("):
                    while col < len(rest) and not rest[col].endswith(")"):
                        col += 1
                    col += 1
                step = rest[col + 1].strip() if len(rest) > col + 1 else ""
                if re.fullmatch(r"\d+(\.\d+)?", step):
                    scale = float(step)
                if len(rest) > col + 2:
                    u = " ".join(rest[col + 2:]).strip()
                    if u == "-" or "(" in u:
                        unit = None
                    else:
                        unit = u or None
            elif anchor is not None:
                # Short layout (product info / monitor / historic): the scale
                # is embedded in the Unit column ("0.01 V", "0.001A").
                is_write_only = "(write only)" in desc.lower()
                for j, p in enumerate(rest):
                    p_clean = p.strip()
                    if not is_write_only:
                        # Unit column holds the factor ("0.01 V" split across
                        # tokens by single-space gaps, or combined "0.001A").
                        sm = re.match(r"^(0?\.\d+)([A-Za-z%°]+)?$", p_clean)
                        if sm:
                            scale = float(sm.group(1))
                            if sm.group(2):
                                unit = sm.group(2)
                            elif j + 1 < len(rest) and re.fullmatch(r"[A-Za-z%°]+", rest[j + 1].strip()):
                                unit = rest[j + 1].strip()
                            continue
                    if p_clean in ["V", "A", "W", "Ah", "seconds", "minutes", "min.", "K", "%"]:
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
            if len(tokens) < 3 or not re.fullmatch(r"0[xX][0-9A-Fa-f]{4}", tokens[0]):
                continue

            rid = int(tokens[0], 16)
            name = tokens[1].strip()
            access = tokens[2].strip()
            if access == "R/(W)":
                # 0x0150 BLE capabilities prints "R/(W)" (parenthesised W =
                # conditional write); the library models CAPABILITIES_BLE as
                # read-only, so the read side is the comparable access.
                access = "R"
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
                    # Trailing-slash scale cell (0xEB10 "0.02/ s", with the
                    # 60 Hz sibling "0.016" on the next layout line; Note 4
                    # expresses the period count for 50 or 60 Hz AC-out): the
                    # slash marks the 50/60 Hz alternation, the numeric part
                    # is the doc's scale factor, not a missing value.
                    try:
                        scale = float(p4.rstrip("/"))
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
        for m in re.finditer(r"Capabilities BLE\s*\(register\s+(0[xX][0-9A-Fa-f]{4})\)", self.text):
            hid = int(m.group(1), 16)
            if hid != 0x0150:
                evidence[hid] = (
                    "section-header ID; product-info table uses 0x0150 "
                    "(known Victron typo, not a second register)"
                )
        return evidence


#: Units vocabulary printed in the Text-mode fields tables' Units column
#: (doc-derived: mV/mA/W/mAh/°C/‰/Minutes/Seconds bare units, "0.01 kWh"
#: energy counters, "0.01 V"/"0.1 A" AC/DC rows, "VA" apparent power, "1W"
#: DC input power). The lookahead keeps a description starting with "W"
#: (e.g. "Warning reason") from matching the "W" unit. Anything else
#: starting a row's remainder is the description (Units column empty:
#: Alarm/Relay/AR/OR/ERR/CS/LOAD/FW/...).
_TEXT_UNITS_RE = re.compile(
    r"^(0\.01 kWh|0\.01 V|0\.1 A|mAh|mV|mA|VA|1W|W|°C|‰|Minutes|Seconds)(?=$|[\s\d])"
)

#: Footnote reference glued after the units cell (doc footnotes 1..20 are
#: defined below the tables): "mAh11", "mV3", "‰10,11",
#: "Minutes10, 11,12", "°C 8", "mAh 10,11", or a bare "11" where the Units
#: column is empty (H4/H5/H10). ParsedTextField has no footnote slot, so
#: these references are stripped, never stored.
_TEXT_FOOTNOTE_RE = re.compile(r"^\s*\d[\d\s,]*")

#: Lines that can never be a description continuation (page furniture and
#: the device-column header of the next table). Each pattern is anchored to
#: its exact furniture text (same treatment as the BlueSolar skip-filter
#: fix: ``re.match(r"^\\s*0xEDEF will always\\b", line)``): a bare substring
#: (``"Label" in "Labelled ..."``, ``"60x" in "Depth for 60x ..."``,
#: ``"Victron Energy" in "Made by Victron Energy ..."``) would also drop
#: genuine continuations containing those words. Anchored patterns let
#: mid-line mentions through while still excluding furniture:
#: - device header starts with "60x" after spaces (indent 101-113);
#: - footer "Victron Energy B.V. | ..." (indent 0, B.V. keeps a leading
#:   "Victron Energy foo" continuation joinable);
#: - footer "General phone: +31 ..." (indent 0, number keeps a leading
#:   "General phone foo" continuation joinable);
#: - bare footer URL alone (indent 0/198, trailing ``\\s*$`` keeps a leading
#:   "www... foo" continuation joinable; https prose lives at indent 23
#:   and already misses the indent rule);
#: - fields-table header "Label Units ..." (indent 18, ``\\b`` lets
#:   "Labelled ..." through and ``\\s+Units`` lets "Label foo" through).
#: Verified zero-diff: only the 60x headers sit at indent >= 33 in the
#: fields section, and they still match; no prose false positives.
_TEXT_CONT_EXCLUDE = (
    re.compile(r"^\s*60x\b"),
    re.compile(r"^\s*Victron Energy B\.V\."),
    re.compile(r"^\s*General phone: \+31"),
    re.compile(r"^\s*www\.victronenergy\.com\s*$"),
    re.compile(r"^\s*Label\s+Units\b"),
)

#: Shape of a would-be Units cell the closed _TEXT_UNITS_RE vocabulary did
#: not recognize: anything letter-bearing before the 2+-space column gap.
#: Multi-word candidates ("kilo watt hours") are recorded as well as single
#: tokens: the old single-token shape check let a future multi-word unit
#: leak into the description with unknown_units==[]. Loud recording (no
#: length drop): the longest vocabulary unit is 8 chars ("0.01 kWh"), but a
#: future unit longer than 32 chars must still fail loudly instead of being
#: silently ignored — over-long candidates are recorded with their full text
#: so the suite flags them for human review. Verified: no snapshot row yields
#: a two-part pre-gap split here, so the widened gate stays empty on current
#: docs.
_UNKNOWN_UNIT_CAND_RE = re.compile(r"[A-Za-z°‰%Ωμ]")
#: Retained for triage documentation only (4x headroom over the 8-char
#: longest vocabulary unit); NOT used to drop candidates — dropping hid real
#: units. See _split_text_units_desc: all letter-bearing candidates are
#: recorded.
_UNKNOWN_UNIT_MAX_LEN = 32

#: First-words of current snapshot empty-Units descriptions (AR/Alarm/Relay/
#: OR/ERR/CS/LOAD/HSDS/H11-H14/BMV/FW/PID/SER#/MODE/WARN/MPPT/MON): the
#: single-space fallback below takes the first whitespace-separated token as
#: the unit candidate, so these description starters must be denylisted to
#: stay exact (zero diff) on current docs. Over-inclusion principle: a future
#: unit colliding with one of these words would be missed here, but the list
#: is explicit and reviewable, and the single-space divergence test proves the
#: fallback catches non-denylisted units (e.g. "Hz"). Verified: every
#: snapshot empty-Units remainder's first token is in this set, so the
#: fallback stays empty on current docs.
_SINGLE_SPACE_DESC_DENY = frozenset({
    "Alarm", "Model", "State", "Error", "Firmware", "Number", "Day", "Load",
    "Device", "DC", "Tracker", "Off", "Product", "Relay", "Serial", "Warning",
})


def _split_text_units_desc(
    rest: str, label: str = "", unknown_sink: Optional[List[Tuple[str, str]]] = None
) -> Tuple[str, str]:
    """Split a fields-table row remainder into ``(units, description)``.

    Strips footnote references and the device-bullet tail (bullets belong to
    the device columns, parsed separately). Returns ``("", "")`` when the
    row carries only units+footnote (H1) or a bare footnote (H4/H5/H10):
    the description then lives on the continuation line. No description in
    the doc starts with a digit, so a leading digit run is always a
    footnote, never description text.

    Unknown units fail loudly instead of leaking into the description: when
    the closed _TEXT_UNITS_RE vocabulary does not match but the remainder
    holds a letter-bearing candidate before the 2+-space column gap (e.g. a
    future "Hz" or "kilo watt hours" row), the raw candidate is appended to
    ``unknown_sink`` as ``(label, candidate)`` (when a sink is given) and
    the row still parses as ``("", rest)`` so the suite — which asserts the
    sink is empty — breaks until the unit is explicitly allowlisted. A
    single-space layout collapse ("Hz Some new quantity" with one space
    between unit and description) has no 2+-space gap, so a fallback takes
    the first whitespace-separated token as the candidate (denylisting known
    description starters to stay exact on current docs). Over-long
    candidates are recorded, never dropped.
    """
    units = ""
    m = _TEXT_UNITS_RE.match(rest)
    if m:
        units = m.group(1)
        rest = rest[m.end():]
        fm = _TEXT_FOOTNOTE_RE.match(rest)
        if fm:
            rest = rest[fm.end():]
    elif re.fullmatch(r"\d[\d\s,]*", rest.strip()):
        return "", ""  # bare footnote cell, description on continuation line
    else:
        head = rest.split("•", 1)[0]
        parts = re.split(r"\s{2,}", head.strip(), maxsplit=1)
        if len(parts) == 2:
            cand = parts[0].strip()
            # Footnote-glued unknowns ("Hz11", "kWh10,11"): a trailing
            # footnote run must not defeat the letter check, so it is
            # stripped for the check but the raw candidate is recorded.
            shape = re.sub(r"\d[\d\s,]*$", "", cand).strip() or cand
            if _UNKNOWN_UNIT_CAND_RE.search(shape):
                if unknown_sink is not None:
                    unknown_sink.append((label, cand))
        else:
            # Single-space fallback: no column gap (layout collapse or future
            # single-spaced unit). Record the first token unless it is a known
            # description starter (denylisted above to stay exact).
            toks = head.strip().split()
            if toks:
                cand1 = toks[0].strip()
                shape1 = re.sub(r"\d[\d\s,]*$", "", cand1).strip() or cand1
                if cand1 not in _SINGLE_SPACE_DESC_DENY and _UNKNOWN_UNIT_CAND_RE.search(shape1):
                    if unknown_sink is not None:
                        unknown_sink.append((label, cand1))
    return units, rest.split("•", 1)[0].strip()


def _is_desc_continuation(line: str, base_indent: int = 13) -> bool:
    """True for description-column continuation lines.

    Relative-indent rule: the continuation must sit at least 20 columns
    right of its label row (``base_indent`` + 20; the doc prints labels at
    13 and continuations at 58, footnotes at 25-27, so the threshold 33
    tolerates a ±15-column ``pdftotext -layout`` drift in either direction
    while footnote prose still misses it). The previous hard ``indent >=
    40`` floor silently dropped every H1/H4/H5/H10/H17/H18 join after a
    -20 shift while device bullets (position-scanned, indent-free) kept
    parsing. Footnote prose (indent <= ~27), page furniture (indent 0) and
    the next table's device-column header never qualify.
    """
    s = line.strip()
    if not s:
        return False
    if len(line) - len(line.lstrip(" ")) < base_indent + 20:
        return False
    # Anchored furniture check (not bare substring): a genuine continuation
    # containing "Labelled ..."/"60x"/"Victron Energy" mid-line must join.
    return not any(rx.match(line) for rx in _TEXT_CONT_EXCLUDE)


class VEDirectTextParser:
    """Parser for VE.Direct Text Mode Protocol 3.34 documentation."""

    def __init__(self, text: str):
        self.text = text
        #: ``(label, candidate)`` rows whose Units cell did not match the
        #: closed _TEXT_UNITS_RE vocabulary (see _split_text_units_desc).
        #: Empty for the current snapshot; the suite asserts it stays
        #: empty so a newly introduced unit breaks loudly until
        #: explicitly allowlisted with justification.
        self.unknown_units: List[Tuple[str, str]] = []

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
        self.unknown_units = []

        label_re = re.compile(r"^\s{10,25}([A-Za-z0-9_#.\-]+)\s{2,}(.*?)\s*$")
        # Hyphen/dot in the label class (DC-IN, V.PV): the closed
        # [A-Za-z0-9_#] missed future hyphen/dotted labels on BOTH sides
        # (parser and count_doc_field_labels) while counts stayed green.
        # Over-inclusive scraper + parser record both (zero-diff: no
        # hyphen/dot label in current snapshots, verified).
        stoplist = {
            "Label", "Table", "The", "Note", "Victron", "General", "E-mail",
            "When", "Both", "Available", "60x",
        }

        pending: Optional[List[Any]] = None  # [label, units, desc, raw_lines, label_indent]

        def table_starts(idx: int) -> List[int]:
            cur: List[int] = []
            for hidx, starts in tables:
                if hidx < idx:
                    cur = starts
            return cur

        def devices_for(raw_lines: List[str], starts: List[int], label: str = "?") -> List[str]:
            # A bullet past the last known column start belongs to the last
            # device only while it still sits under that column's own header
            # print width; anything further right is an Nth device column the
            # 7-entry TEXT_DEVICES cannot name, so it fails loudly instead of
            # silently merging into BuckBoost. Tolerance == len("BuckBoost"):
            # bullets are printed under their headers, and the measured
            # snapshot maximum overhang past the BuckBoost start is +8 (I/P
            # rows, layout drift), so 9 keeps every current row green while
            # an 8th column (next pitch >= ~+10) trips loudly for triage.
            found: Set[int] = set()
            for raw in raw_lines:
                for m in re.finditer("•", raw):
                    p = m.start()
                    if starts and p > starts[-1] + len("BuckBoost"):
                        raise ValueError(
                            f"text field {label!r}: device bullet at column {p} "
                            f"beyond known device columns (last start {starts[-1]}); "
                            "extend TEXT_DEVICES instead of merging into BuckBoost"
                        )
                    col = 0
                    for s, start in enumerate(starts):
                        if start <= p + 1:
                            col = s
                    found.add(col)
            return [TEXT_DEVICES[c] for c in sorted(found)] if starts else []

        def flush(idx: int) -> None:
            if pending is None:
                return
            label, units, desc, raw_lines, _indent = pending
            fields[label] = ParsedTextField(
                label=label, units=units, description=desc,
                devices=devices_for(raw_lines, table_starts(idx), label),
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
                units, desc = _split_text_units_desc(
                    m.group(2), label=label, unknown_sink=self.unknown_units
                )
                pending = [label, units, desc, [line], len(line) - len(line.lstrip(" "))]
            elif pending is not None:
                pending[3].append(line)
                # Continuation description: rows whose remainder held only a
                # footnote (H1/H4/H5/H10) carry the description on the next
                # description-column line; H17/H18 split "...(BMV) /" across
                # two lines. Other lines only extend the device-bullet scan.
                # The indent anchor is this row's own label column (relative
                # rule), never a hard absolute floor.
                if (pending[2] == "" or pending[2].endswith("/")) and _is_desc_continuation(line, pending[4]):
                    cont = line.split("•", 1)[0].strip()
                    pending[2] = cont if pending[2] == "" else pending[2] + " " + cont
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
            # PID prefix case-insensitive like register IDs (0X rescue,
            # zero-diff: no 0X PIDs in snapshots).
            m = re.search(r"^\s*([A-Za-z0-9\s|/\-().*+]+?)\s{2,}(0[xX][0-9A-Fa-f]{3,4})(\*)?", line)
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
            # Optional bullet tail (future "Future fault 99 •" rows): the
            # shared EOL anchor hid bullet-tailed rows on both sides while
            # EXPECTED_ERRORS stayed green (CS already tail-diverged). The
            # scraper is tail-agnostic (\\b), so the parser records
            # bullet-tailed rows too — zero-diff (snapshots carry no bullet
            # tail, verified).
            m = re.match(r"^\s*([A-Za-z0-9\s/(),\-]+?)\s{2,}(\d{1,3})(?:\s+•.*)?\s*$", line)
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
