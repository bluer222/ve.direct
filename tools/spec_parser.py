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
#: Hyphen/bare spellings ("write-only", "write only") match too (see
#: _WRITE_ONLY_RE): the snapshots use "(write only)" only (BMV 0x0004/
#: 0x1029/0x102C/0x1030), but all three spellings carry the same command
#: semantics, so a hyphen-spelled row parses WRITE_ONLY instead of
#: READ_WRITE. Word-anchored (never a bare substring): "rewrite only" /
#: "overwrite-only" must not match (no boundary inside "rewrite").
_WRITE_ONLY_RE = re.compile(r"\(?\bwrite[\s\-]+only\b\)?", re.IGNORECASE)

#: Word-anchored regexes for _is_write_desc (plural-aware where the doc
#: uses both forms). Substring matching ("restore" in desc) is the bug:
#: it misclassifies any readable containing the substring. Anchored phrases
#: fail loudly via the write-only divergence test instead. Hyphen-spelled
#: command hints ("Restore-defaults", "Clear-history", "Zero-current",
#: "Reset-counters", "Restore-factory") match too via [\s\-]+ (snapshots
#: use bare spaces only — BMV "(write only)", BlueSolar/Orion "Restore
#: default"/"Clear history" — verified zero hyphen rows, so zero-diff):
#: a future hyphen-spelled typed row parses write_only/W instead of R/W.
#: Bare "synchronize" needs no hyphen form (single word); "(write only)"
#: hyphen/bare spellings already via _WRITE_ONLY_RE below.
_WRITE_HINT_RES = (
    re.compile(r"\brestore[\s\-]+defaults?\b", re.IGNORECASE),
    re.compile(r"\brestore[\s\-]+factory\b", re.IGNORECASE),
    re.compile(r"\bclear[\s\-]+history\b", re.IGNORECASE),
    re.compile(r"\bzero[\s\-]+current\b", re.IGNORECASE),
    re.compile(r"\bsynchronize\b", re.IGNORECASE),
    re.compile(r"\breset[\s\-]+counters?\b", re.IGNORECASE),
    _WRITE_ONLY_RE,
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
#: Exponent form ("1e-2", "1E-2", "1E+1") is accepted too: no snapshot
#: scale cell uses it (verified: no ``[eE]``-exponent scale token in any
#: HEX snapshot — the docs print plain decimals like 0.01/0.1/1 only), so
#: the general float-with-exponent shape is zero-diff while a future
#: scientific-notation factor parses instead of leaking into the
#: description with scale None (cf. the 0xEDB3 "1e-2" probe, hidden behind
#: SCALE_NONE_ALLOW while ID-based reverse-coverage stays green).
#: European comma-decimal ("0,1" for 0.1) is accepted too: no snapshot
#: scale cell uses it (verified: no comma scale token in any HEX snapshot,
#: only prose commas like "12,24" and "(2,3)" footnotes), so zero-diff
#: while a future comma factor parses via comma->dot before float().
#: ``float(comma->dot)`` parses every accepted shape (callers replace).
_SCALE_TOKEN_RE = re.compile(r"^(?:\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?|-)$")

#: A scale-CELL-shaped token that the strict _SCALE_TOKEN_RE rejects:
#: digit-bearing, built only from scale punctuation (digits, ".", ",", "eE",
#: "+", "-", "/", "x"). Used ONLY as the loud gate below (unknown-scale
#: tokens are recorded even on allowlisted IDs — allowlists excuse absent
#: doc data, never dropped unknown tokens; unknown-units/unknown-access
#: gate pattern): genuine descriptions ("mode", "Tracker") carry no digits
#: and miss it, while a future "1x"-style cell records loudly instead of
#: leaking into the description with scale None. Verified zero-diff: no
#: snapshot dtype-anchored row ends its description with such a token
#: (checked on both section-table snapshots). Comma included so a
#: comma-bearing but unparsable cell (e.g. "1,x") still records loudly.
_SCALE_LIKE_RE = re.compile(r"^(?=.*\d)[0-9eE.,+\-/xX]+$")

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

#: Split-typo ID "0x HHHH" (space inside the token, cf. the "0 xHHHH"
#: spaced typo and the "xHHHH" missing-0 typo): after whitespace-splitting
#: this is TWO tokens ("0x", "HHHH"), so the single-token _ID_TOKEN_RE
#: above misses it everywhere (section tables, ID-only rows, BMV/Phoenix
#: loops, both test-side scrapers) while counts stay green. Rescued at the
#: token level wherever a table row is split (see _split_typed_row and the
#: BMV/Phoenix token-merge below) plus the line-level pre-filters and both
#: scrapers. Verified zero-diff: no "0x HHHH" occurrence in any current
#: snapshot, so no prose false positives; a future split row is now
#: rescued instead of missed by all sides. Prose "(register 0x...)"
#: mentions and ".." range headers keep the glued shape (pinned by the
#: typo-evidence test); only single-row table paths rescue the split form.
_ID_TOKEN_SPLIT_PREFIXES = ("0x", "0X")
_HEX4_RE = re.compile(r"^[0-9A-Fa-f]{4}$")

#: Mid-token split "0xED E0" (layout glitch splitting the hex run itself,
#: cf. the "0x HHHH" prefix split above): after whitespace-splitting this
#: is TWO tokens ("0xED", "E0") rejoining to exactly 4 hex digits, so both
#: the single-token _ID_TOKEN_RE and the prefix-split rescue above miss it
#: everywhere while counts stay green. Rescued at the token level wherever
#: a table row is split (see _split_typed_row and the BMV/Phoenix
#: token-merge below) plus the line-level pre-filters and both test-side
#: scrapers. Shape guards (explicit, justified): exactly one single space
#: between the runs (a column gap is 2+, enforced on the raw line since
#: token-split collapses gaps), runs of 1-3 hex digits each rejoining to
#: exactly 4, line-leading table-row shape, and the scale/dtype anchors
#: below as the table-row guard (prose never carries a dtype tail).
#: Verified zero-diff: no "0xHH HHHH" mid-split occurrence in any current
#: snapshot, so no prose false positives; a future split row is now
#: rescued instead of missed by all sides.
_MID_SPLIT_HEAD_RE = re.compile(r"^0[xX][0-9A-Fa-f]{1,3}$")
_MID_SPLIT_TAIL_RE = re.compile(r"^[0-9A-Fa-f]{1,3}$")
_MID_SPLIT_LINE_RE = re.compile(r"0[xX][0-9A-Fa-f]{1,3} [0-9A-Fa-f]{1,3}\b")


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


def _strip_one_paren(tok: str) -> str:
    """Strip one outer paren pair (Phoenix Scale-cell treatment).

    ``"(0.1)"`` -> ``"0.1"``; anything else passes through. Used by the
    section-table split and the BMV Step cell so parenthesized factors
    parse honestly instead of leaking into the description with scale
    None (cf. Phoenix ``p4_try`` handling).
    """
    t = tok.strip()
    if len(t) >= 2 and t.startswith("(") and t.endswith(")"):
        return t[1:-1].strip()
    return tok


def _split_typed_row(line: str, allow_bare_x_prefix: bool = False, unknown_sink: Optional[List[Tuple[int, str]]] = None) -> Optional[Tuple[int, str, Optional[str], str, Optional[str], bool]]:
    """Token-split one HEX table row into ``(rid, desc, scale_token, dtype_token, unit, typo)``.

    Column gaps may be single spaces (cf. BlueSolar 0xEDE0/0xEDCA single
    space after the ID, Phoenix 0xEB99 single-spaced throughout) and dtype
    matching is case-insensitive, so the split anchors on the scale/dtype
    tokens instead of ``\\s{2,}`` gaps: descriptions may themselves contain
    spaces and numbers. Returns ``None`` when the line is not a typed table
    row (prose mentions, product-ID rows, range headers). ``unit`` is the
    raw remainder (may be ``-`` or contain sentinels, as the callers expect).
    When the row is dtype-anchored (no strict scale token) but the token
    immediately before the dtype is scale-CELL-shaped yet unparsable
    (e.g. a future "1x"), ``(rid, token)`` is appended to ``unknown_sink``
    (when given) — the loud unknown-scale gate (unknown-units pattern):
    allowlists excuse absent doc data, never dropped unknown tokens.
    """
    tokens = line.split()
    if len(tokens) < 3:
        return None
    idtok = tokens[0]
    typo = False
    if _ID_TOKEN_RE.match(idtok):
        rid = int(idtok, 16)
        rest = tokens[1:]
    elif allow_bare_x_prefix and _ID_TOKEN_BARE_X_RE.match(idtok):
        # Doc typo: "xEDE0 Battery low temperature level" (missing leading
        # 0; cf. BlueSolar-HEX 0xEDE0 with identical metadata).
        rid = int(idtok[1:], 16)
        typo = True
        rest = tokens[1:]
    elif (
        idtok in _ID_TOKEN_SPLIT_PREFIXES
        and len(tokens) >= 4
        and _HEX4_RE.match(tokens[1])
    ):
        # Doc typo: "0x EDF0 ..." (space inside the ID token; cf. the
        # "0 xHHHH" spaced typo). Two tokens after splitting, so the
        # single-token rescues above never fire; the remainder starts at
        # tokens[2:]. Typo-flagged like the xEDE0 rescue.
        rid = int(tokens[1], 16)
        typo = True
        rest = tokens[2:]
    elif (
        len(tokens) >= 4
        and _MID_SPLIT_HEAD_RE.match(idtok)
        and _MID_SPLIT_TAIL_RE.match(tokens[1])
        and len(idtok[2:]) + len(tokens[1]) == 4
        and _MID_SPLIT_LINE_RE.search(line)
    ):
        # Layout glitch splitting the hex run itself ("0xED E0" for
        # 0xEDE0): two tokens rejoining to exactly 4 hex digits. The
        # single literal space in _MID_SPLIT_LINE_RE is the shape (a
        # column gap is 2+; token-split collapses gaps, so the raw-line
        # check keeps the rescue to true mid-token splits). The
        # scale/dtype anchors below are the table-row guard: prose never
        # reaches here with a dtype tail. Typo-flagged like the prefix
        # rescues; the remainder starts at tokens[2:].
        rid = int(idtok[2:] + tokens[1], 16)
        typo = True
        rest = tokens[2:]
    else:
        return None
    if tokens[1] == "=":
        # Product-ID prose ("0xA3F0 = Orion XS ..."), not a table row.
        return None
    if not rest:
        return None

    def is_dtype_like(tok: str) -> bool:
        return bool(_DTYPE_RE.match(tok) or _FOOTNOTE_DTYPE_RE.match(tok))

    # Scale-anchored: "<desc...> <scale> <dtype> [unit...]". Footnote cells
    # ("(*4)") only anchor when no real wire width follows: a footnote
    # inside the description (e.g. "Display ... (*2) un8") must not split it.
    # Parenthesized scale cells ("(0.1)", cf. Phoenix p4_try) anchor via
    # the one-paren-stripped form (comma->dot at the callers); a
    # parenthesized-but-unparsable cell ("(1x)") falls through to the
    # loud gate below. Zero-diff: no snapshot scale cell is parenthesized.
    for footnote_ok in (False, True):
        for i, tok in enumerate(rest):
            tok_try = _strip_one_paren(tok)
            if not _SCALE_TOKEN_RE.match(tok_try) or i + 1 >= len(rest):
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
                return rid, desc, tok_try, nxt, unit, typo
    # Dtype-anchored (scaleless display/mode rows): "<desc...> <dtype>
    # [unit...]". Real widths win over footnotes for the same reason.
    # Loud gate covers parenthesized candidates too (Phoenix treatment):
    # "(1x)" strips to scale-like-but-unparsable and records raw, while
    # double-wrapped ("((0.1))") records via the digit-bearing paren
    # fallback; honestly parsable ("(0.1)" -> "0.1") never reaches here
    # (scale-anchored above) and never records.
    for footnote_ok in (False, True):
        for i, tok in enumerate(rest):
            if (_DTYPE_RE.match(tok) or (footnote_ok and _FOOTNOTE_DTYPE_RE.match(tok))):
                desc = " ".join(rest[:i]).strip()
                if not desc:
                    return None
                if unknown_sink is not None and i >= 1:
                    cand = rest[i - 1]
                    # Footnote references ("(*2)", "(*1)") live inside the
                    # description (e.g. BlueSolar 0x0403/0x0404/0x0406) and
                    # are never Scale cells: excluded here (like the
                    # scale-anchored footnote handling above) so stripping
                    # their parens ("*2") does not record loudly.
                    if _FOOTNOTE_DTYPE_RE.match(cand):
                        pass
                    else:
                        cand_try = _strip_one_paren(cand)
                        if _SCALE_LIKE_RE.match(cand_try) and not _SCALE_TOKEN_RE.match(cand_try):
                            unknown_sink.append((rid, cand))
                        elif (
                            cand_try != cand
                            and re.search(r"\d", cand)
                            and "*" not in cand
                            and not _SCALE_TOKEN_RE.match(cand_try)
                        ):
                            unknown_sink.append((rid, cand))
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
        #: ``(rid, token)`` rows whose scale cell was scale-shaped but
        #: unparsable (see _split_typed_row). Empty for the current
        #: snapshot; the suite asserts it stays empty so a newly
        #: introduced scale token breaks loudly until explicitly
        #: triaged (unknown-units/unknown-access gate pattern).
        #: Allowlists excuse missing doc data, never dropped unknown
        #: tokens — even allowlisted IDs (e.g. 0xEDB3) record here.
        self.unknown_scales: List[Tuple[int, str]] = []
        #: ``(rid, unit)`` section-table rows whose Unit remainder carries
        #: a parenthesized qualifier (e.g. future "V (peak)"): the raw
        #: remainder is kept as unit (normalizer strips to "V", matching
        #: DB) while the qualifier is recorded loudly here even on
        #: allowlisted IDs (BMV/Text unknown-units pattern — allowlists
        #: excuse missing data, never dropped qualifiers). Empty for the
        #: current snapshots except the denylisted "% (of 0xEDF0)"
        #: register reference below; the suite asserts the remainder stays
        #: empty so a new qualifier breaks loudly until triaged.
        self.unknown_units: List[Tuple[int, str]] = []

    @classmethod
    def from_file(cls, path: Path) -> BlueSolarHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self.unknown_scales = []
        self.unknown_units = []
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
            # Split-typo IDs ("0x HHHH") pass the same gate: the optional
            # inner whitespace mirrors the _split_typed_row rescue (zero-diff:
            # no snapshot line matches the split form, verified). Mid-token
            # splits ("0xED E0", see _MID_SPLIT_LINE_RE) pass it too; the
            # exact-4 rejoin plus the scale/dtype anchors in
            # _split_typed_row stay the strict gate (this pre-filter is
            # deliberately looser). Bare-x
            # typo IDs ("xEDE0", missing leading 0) pass it too, symmetric
            # with the Orion section-table rescue (same doc-typo
            # justification: cf. BlueSolar-HEX 0xEDE0 with identical
            # metadata): a future bare-x row here parses instead of being
            # missed by the parser while counts stay green. Zero-diff: no
            # bare-x line-start in the BlueSolar snapshot (verified), so no
            # prose false positives; the 0x prose-note guards below are
            # unaffected (bare-x lines never match them).
            if not re.match(r"^\s*(?:0[xX](?:[0-9A-Fa-f]{4}|[0-9A-Fa-f]{1,3} [0-9A-Fa-f]{1,3}|\s+[0-9A-Fa-f]{4})|[xX][0-9A-Fa-f]{4})\b", line):
                continue
            # Skip known prose-note lines that happen to start with a
            # register-looking ID. Each pattern is anchored to its exact note
            # text: a bare "firmware version" substring would also drop any
            # future table row whose description mentions firmware versions.
            # The optional inner whitespace keeps split-typo prose excluded
            # the same way (no snapshot split prose, zero-diff).
            if re.match(r"^\s*0[xX]\s*EDEF will always\b", line):
                continue  # Note 4 continuation on 0xEDEA ("will always reports ...")
            if re.match(r"^\s*0[xX]\s*A046 and higher\)", line):
                continue  # Note 3 continuation ("0xA046 and higher). A VE.Direct TX ...")
            if re.match(r"^\s*0[xX]\s*0300\)", line):
                continue  # History intro ("0x0300). The history registers are ...")

            # Token-anchored split (not \s{2,} gaps): tolerates single-space
            # columns and any-case dtypes; footnote dtypes ("(*4)") fall out
            # of _normalize_dtype as unknown with the cell kept in notes.
            # The unknown-scale sink records scale-shaped-but-unparsable
            # cells even on allowlisted IDs (loud gate, see _SCALE_LIKE_RE).
            # Bare-x rescue (allow_bare_x_prefix), symmetric with the Orion
            # section-table rescue and its doc-typo justification.
            row = _split_typed_row(line, allow_bare_x_prefix=True, unknown_sink=self.unknown_scales)
            if row is None:
                continue
            rid, desc, scale_str, dtype_raw, unit_str, typo = row

            scale = None if scale_str in (None, "-") else float(scale_str.replace(",", "."))
            dtype, dtype_note = _normalize_dtype(dtype_raw)
            sentinels = set()
            # Sentinel may print uppercase 0XFFFF; case-insensitive like IDs.
            if unit_str and "0XFFFF" in unit_str.upper():
                sentinels.add(0xFFFF)
            # Write-hint detection for section-table rows (same _is_write_desc
            # / _WRITE_HINT_RES logic as ID-only/detail rows): a command
            # description (e.g. "Restore defaults ...") in a typed table
            # parses as write_only/W instead of the R/W default, so the
            # schema W assertion catches it. Zero-diff: no current
            # BlueSolar section-table row carries a write hint (verified),
            # so no snapshot row changes meaning.
            write_only = _is_write_desc(desc)
            if write_only:
                scale = None
                dtype = "write_only"
                unit_str = None
                sentinels = set()
            # Loud unknown-unit gate for parenthesized qualifiers (BMV/Text
            # pattern): probe "0xEDF0 ... V (peak)" kept unit "V (peak)"
            # (normalizer strips to "V", matching DB) with empty gates while
            # the qualifier was silently lost (reproduced before fix). Any
            # "(" remainder now records loudly even on allowlisted IDs
            # (allowlists excuse missing data, never dropped qualifiers)
            # while unit stays as today (schema side unchanged, gate is loud).
            # Denylist (explicit, justified): "% (of 0xEDF0)" is the doc's
            # register-reference notation on 0xEDE4 (not a physical-unit
            # qualifier) — verified the ONLY parenthesized Unit remainder in
            # either snapshot, so zero-diff while future "V (peak)" records.
            if unit_str and "(" in unit_str and unit_str.strip() != "% (of 0xEDF0)":
                self.unknown_units.append((rid, unit_str))

            regs[rid] = ParsedRegister(
                id=rid,
                name=_register_name(desc),
                description=desc,
                scale=scale,
                data_type=dtype,
                unit=unit_str,
                access="W" if write_only else "R/W",
                source="BlueSolar-HEX",
                # Typo provenance mirrors the Orion rescue note (generic
                # over the rescued ID, not hardcoded to xEDE0).
                notes=f"doc-typo: ID printed as x{rid:04X}, read as 0x{rid:04X}" if typo else dtype_note,
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
            # Exact-4 bound ((?![0-9A-Fa-f]) on both endpoints): the HEX
            # Get/Set framing carries the register ID as a 2-byte
            # little-endian field (src/ve_direct/hex.py "<H"; BlueSolar-HEX
            # §1 ":7F0ED0071"), so a 5-hex-digit endpoint is
            # protocol-impossible and must not prefix-expand a range (a
            # "0x12345..0x12346" line yields nothing instead of silently
            # expanding 0x1234..0x1234). Zero-diff: no snapshot range
            # endpoint carries 5+ hex digits (verified).
            for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})(?![0-9A-Fa-f])\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})(?![0-9A-Fa-f])", line):
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
            # Decimal range mirror of the hex inline branch above (same
            # context guard, same 64-register _expand_id_range cap): a future
            # decimal-only range ("History registers 60961-60999 table",
            # "60961..60999", "60961...60999") was missed by every parser and
            # scan while counts stayed green (all sides shared the 0x-only
            # range assumption — reproduced synthetically before the fix).
            # Shape guards (explicit, justified): 3-7 digit groups (widened
            # from 4-5: calendar years are exactly 4-digit so 3-/6-/7-digit
            # carry no year collision; probes "999 ..." / "123456 ..." /
            # "1234567 ..." now expand honestly; 7-digit false-positive
            # denylist: HEX-frame colon-led runs (":3020050", ":1000351")
            # miss the line-leading ``^`` shape and carry no history word,
            # Text-mode PCB part numbers ("9492437") live outside the HEX
            # snapshots entirely — verified zero 7-digit hits on snapshots),
            # separators ".." / "..." / single "-"
            # (mirroring the hex ".." plus the natural decimal "-" of the
            # probe; "--" never a range), ``(?<![\w\-])`` so signed Range
            # cells (BMV 0x1034 "-32768..32767") and hex tails ("0x1000" —
            # "1000" is ``\w``-preceded) never match, ``(?!\w)`` so trailing
            # letters never match. Context guard doubles as the Range-column
            # exclusion (that BMV line carries no history/register word —
            # likewise the 3-digit "174..372" temperature Range cells on BMV
            # alarm/relay rows, verified no history word so still excluded);
            # the span cap is the third guard. Zero-diff: no decimal range
            # line with history context exists in any snapshot (verified),
            # so nothing expands today. Decimal ranges are always history
            # records here (timer events are hex-documented Get/Set words,
            # never decimal).
            for m in re.finditer(r"(?<![\w\-])(\d{3,7})\s*(?:\.\.\.|\.\.|-(?!-))\s*(\d{3,7})(?!\w)", line):
                a, b = int(m.group(1)), int(m.group(2))
                if not re.search(r"[Rr]egister|vreg|history|History|Timer|Cycle|0[xX]104F|0[xX]1050", line):
                    continue
                for rid in _expand_id_range(a, b):
                    if rid not in regs:
                        regs[rid] = ParsedRegister(
                            id=rid,
                            name=f"HISTORY_{rid}",
                            description=f"History record {rid} (range {a}..{b})",
                            scale=None,
                            data_type="struct",
                            unit=None,
                            access="R",
                            source="BlueSolar-HEX",
                            notes=f"range-expanded decimal {a}..{b}; byte-field layout, no single wire type",
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
            # Decimal multi-line mirror ("60961 ..." + bare "60999" within 3
            # lines, same tight window as the hex branch above — the decimal
            # scraper over-scans 8 lines so gap drift diverges loudly there).
            # No timer kind (decimal timers are not a doc shape); no context
            # guard (mirroring the hex multi-line branch, which has none —
            # the "..." start shape itself is the guard; the ``^`` anchor
            # already excludes signed cells like "-32768..."). Widened to
            # 3-/6-/7-digit (same year-collision-free justification as inline
            # above; probes "999 ..." / "123456 ..." / "1234567 ..." expand
            # honestly).
            # Zero-diff: no decimal "..." start line exists in any snapshot
            # (verified, including 3-/6-/7-digit).
            dm = re.match(r"^\s*(\d{3,7})\s*\.\.\.\s*$", line)
            if dm:
                a = int(dm.group(1))
                for j in range(i + 1, min(i + 4, len(lines))):
                    m2 = re.match(r"^\s*(\d{3,7})\s*$", lines[j])
                    if m2:
                        b = int(m2.group(1))
                        for rid in _expand_id_range(a, b):
                            if rid not in regs:
                                regs[rid] = ParsedRegister(
                                    id=rid,
                                    name=f"HISTORY_{rid}",
                                    description=f"History record {rid} (range {a}...{b})",
                                    scale=None,
                                    data_type="struct",
                                    unit=None,
                                    access="R",
                                    source="BlueSolar-HEX",
                                    notes=f"range-expanded multi-line decimal {a}...{b}; byte-field layout, no single wire type",
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
            # Split-typo IDs ("0x HHHH", see _ID_TOKEN_SPLIT_PREFIXES) share
            # this row shape; the inner whitespace is stripped for int().
            # Zero-diff: no snapshot ID-only row uses the split form.
            # Lowercase descriptions ("0x104F future history record"): the
            # old [A-Z] start missed them in parser AND decimal scans while
            # counts stayed green. Both now start [A-Za-z] (case-insensitive
            # row-start). Date/prose denylist (explicit, justified): year
            # guard for decimal below covers calendar years; hyphenated
            # dates ("15-11-2021", "2021-09-06") miss the shape ("-" after
            # digits, not "\\s+[A-Za-z]"); bare years/counts miss it (no
            # description); footer "1351 JG Almere" is mid-line (not
            # line-leading) and outside this history region; device-word
            # guard below excludes product prose. Zero-diff: no snapshot
            # history-region line matches the widened shape (verified).
            # Open description charset ("[A-Za-z].*?": any chars after a
            # letter start, label-charset precedent): the closed class
            # ("[A-Za-z0-9 _\\-/().*]") hid symbol descriptions on every
            # side at once (probes "60961 Future: history record" /
            # "Future+history" / "Future#1" / "A&B" missed by decimal
            # scan, hex scrape, and both parsers while counts stayed
            # green — reproduced synthetically before the fix). Denylists
            # stay structural (... / .. / | / = skips above, device-word
            # guard below, DOC_TYPO, decimal year guard) so no content
            # word is shared-blind; zero-diff: the only newly matching
            # snapshot history-region lines are already-parsed typed rows
            # (comma/percent/degree cells), skipped via rid-in-regs below.
            # Symbol-first descriptions ("*Boost record": symbol, not letter,
            # first) were still missed on every side after that fix
            # (letter-start required — reproduced synthetically before this
            # fix). Both branches below now take the description as
            # ``\\S.*?`` (any non-space start, CS/ERR structural-name
            # precedent): a symbol-first row parses honestly with its exact
            # description. Denylists stay structural (the ... / .. / | / =
            # skips above exclude range/table/prose shapes; the device-word
            # guard below excludes product prose; DOC_TYPO/rid-in-regs
            # below exclude known IDs), so no content word is shared-blind.
            # Zero-diff: no snapshot history-region ID-only line starts
            # with a symbol (verified — every such line starts with a
            # letter or is a "..." range), so only letter-start rows match
            # today and all are already parsed (rid-in-regs skips them).
            m = re.match(r"^\s*(0[xX](?:[0-9A-Fa-f]{4}|\s+[0-9A-Fa-f]{4}))\s+(\S.*?)\s*$", line)
            dm = None
            if m:
                rid = int(m.group(1).replace(" ", ""), 16)
                desc = m.group(2).strip()
            else:
                # Decimal ID-only rows without dtype tail ("60961 Future
                # history record"): no 0x prefix and no scale/type columns,
                # so the hex paths above miss them while the dtype-tailed
                # decimal scan misses them too (no dtype token). Same
                # table shape as the hex ID-only rows (mirrors that regex,
                # now [A-Za-z] start for lowercase "60961 future history
                # record" probes).
                # No year guard here by design (region-scoped): this loop
                # scans ONLY the history region ("Restore factory defaults"
                # .. "1.2 Asynchronous items"), while calendar-year prose
                # (Phoenix bare "2016" line-wrap, revision-log "15-11-2021"
                # / "2021-09-06", footer "1351 JG Almere" mid-line) lives
                # OUTSIDE it — and hyphenated/bare dates already miss the
                # shape ("-" after digits, no description). Verified
                # zero-diff: no snapshot history-region line matches even
                # without the guard, so a future year-shaped ID (e.g. 2000)
                # parses honestly here while outside-region year prose stays
                # denied by the global decimal scan's region-aware guard.
                # Open description charset (same "[A-Za-z].*?" widening and
                # justification as the hex branch above: probes "60961
                # Future: history record" / "Future+history" / "Future#1" /
                # "A&B" now parse honestly; zero-diff via rid-in-regs).
                # Symbol-first ("60961 *Boost record") takes the same
                # ``\S.*?`` widening as the hex branch above (same
                # justification and zero-diff there). Widened to 3-/6-/7-digit
                # (probes "999 Future history record" / "123456 Future
                # record" / "1234567 Future record" missed everywhere while
                # "60961" hit — reproduced before fix): calendar years are
                # exactly 4-digit so 3-/6-/7-digit carry no year collision;
                # 7-digit false-positive denylist: HEX-frame colon-led runs
                # (":3020050") miss the ``^``-anchored shape and Text-mode
                # PCB part numbers live outside the HEX snapshots and outside
                # this history region — verified zero 7-digit hits here.
                # 0-255 (.e.g snapshot
                # history-region "245 WAKE-UP" state rows) are CS/ERR/value
                # domain, never decimal register IDs (verified: all snapshot
                # 3-digit line-leading hits fall in 0-255), so 3-digit IDs
                # <=255 skip here while 256-999 and all 6-/7-digit parse
                # honestly. Zero-diff: history-region 3-digit hits are all
                # <=255 (skipped) and no 6-/7-digit line exists (verified).
                dm = re.match(r"^\s*(\d{3,7})\s+(\S.*?)\s*$", line)
                if not dm:
                    continue
                rid = int(dm.group(1))
                if rid <= 255:
                    continue  # state/error/value domain, never a decimal register ID
                desc = dm.group(2).strip()
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
                notes=("ID-only decimal row; no scale/type columns in doc" if dm is not None else "ID-only row; no scale/type columns in doc"),
            )

    def _parse_detail_mentions(self, regs: Dict[int, ParsedRegister]) -> None:
        """Collect IDs referenced as ``(register 0xXXXX)`` detail headers so
        nothing described outside the 1.1 table is silently dropped."""
        # Exact-4 bound on every ID (outer and inner): the HEX Get/Set
        # framing carries the register ID as a 2-byte little-endian field
        # (src/ve_direct/hex.py "<H"; BlueSolar-HEX §1 ":7F0ED0071"), so a
        # 5-hex-digit mention is protocol-impossible and must not
        # prefix-collect ``0x1234`` out of "register 0x12345". Zero-diff:
        # no snapshot detail mention carries 5+ hex digits (verified).
        for m in re.finditer(
            r"[Rr]egisters?\s+0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f])(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f]))*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f])", m.group(0))]
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
        #: ``(rid, token)`` rows whose scale cell was scale-shaped but
        #: unparsable (see _split_typed_row). Empty for the current
        #: snapshot; the suite asserts it stays empty (unknown-units
        #: gate pattern — allowlists never hide dropped unknown tokens,
        #: even on allowlisted IDs).
        self.unknown_scales: List[Tuple[int, str]] = []
        #: ``(rid, unit)`` section-table rows whose Unit remainder carries
        #: a parenthesized qualifier (e.g. future "V (peak)"): raw kept
        #: (normalizer strips to base, matching DB) while the qualifier
        #: records loudly here even on allowlisted IDs (BMV/Text pattern).
        #: Empty for snapshots except denylisted "% (of 0xEDF0)".
        self.unknown_units: List[Tuple[int, str]] = []

    @classmethod
    def from_file(cls, path: Path) -> OrionXSHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self.unknown_scales = []
        self.unknown_units = []
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
            # columns and any-case dtypes, like the BlueSolar tables. The
            # unknown-scale sink records scale-shaped-but-unparsable cells
            # even on allowlisted IDs (loud gate, see _SCALE_LIKE_RE).
            row = _split_typed_row(line, allow_bare_x_prefix=True, unknown_sink=self.unknown_scales)
            if row is None:
                continue
            rid, desc, scale_str, dtype_raw, unit_str, typo = row

            scale = None if scale_str in (None, "-") else float(scale_str.replace(",", "."))
            dtype, dtype_note = _normalize_dtype(dtype_raw)
            sentinels = set()
            # Sentinel may print uppercase 0XFFFF; case-insensitive like IDs.
            if unit_str and "0XFFFF" in unit_str.upper():
                sentinels.add(0xFFFF)
            # Write-hint detection for section-table rows (same _is_write_desc
            # / _WRITE_HINT_RES logic as ID-only/detail rows): a command
            # description in a typed table parses as write_only/W instead of
            # the R/W default, so the schema W assertion catches it.
            # Zero-diff: no current Orion section-table row carries a write
            # hint (verified), so no snapshot row changes meaning.
            write_only = _is_write_desc(desc)
            if write_only:
                scale = None
                dtype = "write_only"
                unit_str = None
                sentinels = set()
            # Loud unknown-unit gate (same pattern/justification as the
            # BlueSolar branch above: probe "V (peak)" lost silently;
            # denylisted "% (of 0xEDF0)" register reference only).
            if unit_str and "(" in unit_str and unit_str.strip() != "% (of 0xEDF0)":
                self.unknown_units.append((rid, unit_str))

            regs[rid] = ParsedRegister(
                id=rid,
                name=_register_name(desc),
                description=desc,
                scale=scale,
                data_type=dtype,
                unit=unit_str,
                access="W" if write_only else "R/W",
                source="Orion-XS-HEX",
                sentinels=sentinels,
                notes="doc-typo: ID printed as xEDE0, read as 0xEDE0" if typo else dtype_note,
            )

    def _parse_id_ranges(self, regs: Dict[int, ParsedRegister]) -> None:
        """Expand inline ``0x1070..0x1098`` cycle-history ranges."""
        for line in self.text.splitlines():
            # Exact-4 bound on both endpoints (same protocol justification
            # as the BlueSolar branch: 2-byte LE ID field, so a 5-hex-digit
            # endpoint is impossible and must not prefix-expand).
            # Zero-diff: no snapshot range endpoint carries 5+ hex digits.
            for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})(?![0-9A-Fa-f])\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})(?![0-9A-Fa-f])", line):
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
            # Decimal range mirror (same context guard, same 64-register
            # cap; see the BlueSolar branch for the full shape-guard
            # justification, widened to 3-/6-/7-digit there). Probe "History
            # registers 60961-60999 table" was missed on all sides before
            # the fix. Zero-diff: no decimal range line with history context
            # exists in any snapshot (verified, incl. 3-digit "174..372"
            # Range cells which lack the context guard).
            for m in re.finditer(r"(?<![\w\-])(\d{3,7})\s*(?:\.\.\.|\.\.|-(?!-))\s*(\d{3,7})(?!\w)", line):
                a, b = int(m.group(1)), int(m.group(2))
                if not re.search(r"[Rr]egister|history|History|Cycle", line):
                    continue
                for rid in _expand_id_range(a, b):
                    if rid not in regs:
                        regs[rid] = ParsedRegister(
                            id=rid,
                            name=f"CYCLE_HISTORY_{rid - a}",
                            description=f"Cycle history {rid} (range {a}..{b})",
                            scale=None,
                            data_type="struct",
                            unit=None,
                            access="R",
                            source="Orion-XS-HEX",
                            notes=f"range-expanded decimal {a}..{b}; byte-field layout, no single wire type",
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
            # Split-typo IDs ("0x HHHH", see _ID_TOKEN_SPLIT_PREFIXES) share
            # this row shape; the inner whitespace is stripped for int().
            # Zero-diff: no snapshot ID-only row uses the split form.
            # Lowercase descriptions (same widening/justification as the
            # BlueSolar handler above: [A-Za-z] start, year guard for
            # decimal, hyphenated/bare dates miss shape, footer mid-line
            # and outside region, device-word guard below). Zero-diff: no
            # snapshot history-region line matches.
            # Open description charset ("[A-Za-z].*?", label-charset
            # precedent — same widening/justification as the BlueSolar
            # handler: symbol probes "60961 Future: history record" /
            # "Future+history" / "Future#1" / "A&B" now parse honestly;
            # zero-diff via rid-in-regs below).
            # Symbol-first ("*Boost record") takes the same ``\S.*?``
            # widening as the BlueSolar handler (same justification and
            # zero-diff there: no snapshot history-region ID-only line
            # starts with a symbol).
            m = re.match(r"^\s*(0[xX](?:[0-9A-Fa-f]{4}|\s+[0-9A-Fa-f]{4}))\s+(\S.*?)\s*$", line)
            dm = None
            if m:
                rid = int(m.group(1).replace(" ", ""), 16)
                desc = m.group(2).strip()
            else:
                # Decimal ID-only rows without dtype tail (same shape as the
                # BlueSolar handler above: now [A-Za-z] start for lowercase
                # probes). No year guard here by design (region-scoped, see
                # the BlueSolar handler: this loop scans ONLY the history
                # region, year prose lives outside it; verified zero-diff).
                # Zero-diff: no snapshot history-region line matches.
                # Open description charset (same "[A-Za-z].*?" widening as
                # the hex branch above and the BlueSolar handler).
                # Symbol-first takes the same ``\S.*?`` widening (same
                # justification and zero-diff as the hex branch above).
                # Widened to 3-/6-/7-digit like the BlueSolar handler (same
                # 0-255 state-domain skip; probes "999 ..."/"123456 ..."/
                # "1234567 ..." parse honestly, snapshot "245 WAKE-UP" rows
                # skip).
                dm = re.match(r"^\s*(\d{3,7})\s+(\S.*?)\s*$", line)
                if not dm:
                    # Single-line ".." range already handled; dotted triple
                    # ("0x1070..0x1098  Cycle history ...") is range-expanded.
                    continue
                rid = int(dm.group(1))
                if rid <= 255:
                    continue  # state/error/value domain, never a decimal register ID
                desc = dm.group(2).strip()
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
                notes=("ID-only decimal row; no scale/type columns in doc, access default documented here" if dm is not None else "ID-only row; no scale/type columns in doc, access default documented here"),
            )

    def _parse_detail_mentions(self, regs: Dict[int, ParsedRegister]) -> None:
        """Extract IDs described outside the 1.1 table: 0x0206 (device
        function), 0xEDDA (error/warning table), 0x2013 (prose-only),
        0x0150/0x0090 (single-field Bytes records), 0x0004."""
        hist_start = self.text.find("History data")
        if hist_start < 0:
            hist_start = self.text.find("Historical data")
        remote_start = self.text.find("Remote control")
        # Exact-4 bound on every ID (outer and inner): same protocol
        # justification as the BlueSolar detail handler above (2-byte LE ID
        # field — a 5-hex-digit mention is impossible and must not
        # prefix-collect). Zero-diff: no snapshot mention carries 5+ hex.
        for m in re.finditer(
            r"[Rr]egisters?\s+0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f])(?:\s*(?:and/or|and|or|/|,|\.\.)\s*0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f]))*",
            self.text,
        ):
            ids = [int(x, 16) for x in re.findall(r"0[xX][0-9A-Fa-f]{4}(?![0-9A-Fa-f])", m.group(0))]
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
                # Scale accepts the same exponent form as _SCALE_TOKEN_RE
                # (no snapshot single-field scale uses it — plain decimals
                # only — so zero-diff; a future "1e-2" parses via float()).
                # Comma-decimal ("0,1") parses via comma->dot; no snapshot
                # single-field scale uses it (verified), so zero-diff.
                # Dtype alternation is case-insensitive like every other dtype
                # match (_DTYPE_RE, Phoenix dtypes): "UN16" normalizes to
                # "un16" instead of demoting the detail ID. Zero-diff: no
                # snapshot single-field Type cell is uppercase (verified).
                m = re.match(
                    r"^\s*\d+\s+.+?\s+(-|\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s+(un\d+|sn\d+|string\d*)\s*(\S.*)?$", line,
                    re.IGNORECASE,
                )
                if m:
                    scale = float(m.group(1).replace(",", ".")) if m.group(1) != "-" else None
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


def _bmv_dtype_tail_valid(rest: List[str]) -> bool:
    """True when the tokens after a BMV dtype candidate look like a real
    Type-column tail (structural, not first-dtype-wins).

    A genuine Type cell is followed by the settings Range cell (carries
    ``..``), a short-layout scale factor (plain/decimal/exponent, possibly
    unit-glued like ``0.001A``), a bare unit-vocabulary token, or a lone
    ``-`` unit with nothing after it. A dtype-like word *inside* the
    description (probe ``0xEDD0 Battery voltage un16 type - un16 0.01 V``:
    interior ``un16`` followed by ``type``, not a tail) fails all four, so
    the anchor falls through to the later candidate that carries the tail.
    """
    if not rest:
        return False
    if any(".." in p for p in rest):
        return True  # settings Range cell (Default/Step/Unit follow)
    first = rest[0].strip()
    # Parenthesized scale cell ("(0.1)", cf. Phoenix p4_try and the BMV
    # Step cell): strip one outer pair before the strict shape so a
    # parenthesized factor counts as a valid tail (anchor selection).
    # Zero-diff: no snapshot tail is parenthesized (verified).
    first_try = _strip_one_paren(first)
    if re.match(r"^(0?[.,]\d+|\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)([A-Za-z%°]+)?$", first_try):
        return True  # short-layout scale factor, possibly unit-glued
    if first in ("V", "A", "W", "Ah", "seconds", "minutes", "min.", "K", "%"):
        return True  # short-layout bare unit (same vocabulary as below)
    if rest == ["-"]:
        return True  # unit-less short row ("-" Unit column, nothing after)
    return False


class BMV7xxHEXParser:
    """Parser for BMV-7xx and SmartShunt HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text
        #: ``(rid, candidate)`` short-layout rows whose Unit cell did not
        #: match the closed vocabulary (see parse_registers). Empty for the
        #: current snapshot; the suite asserts it stays empty so a newly
        #: introduced bare unit (e.g. Hz) breaks loudly until explicitly
        #: allowlisted with justification (Text unknown-units gate pattern).
        #: Allowlists excuse missing doc data, never dropped unknown tokens.
        self.unknown_units: List[Tuple[int, str]] = []
        #: ``(rid, token)`` rows whose Step (scale) cell was scale-shaped
        #: but unparsable (same loud-gate pattern as unknown_units and the
        #: section-table unknown_scales gates): recorded even on allowlisted
        #: IDs. Empty for the current snapshot (verified: no snapshot
        #: digit-bearing Step cell misses the strict shape below).
        self.unknown_scales: List[Tuple[int, str]] = []

    @classmethod
    def from_file(cls, path: Path) -> BMV7xxHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self.unknown_units = []
        self.unknown_scales = []
        for line in self.text.splitlines():
            # Token-anchored split (not 2+-whitespace gaps): rows such as the
            # monitor short-layout lines may use single spaces between
            # columns, and dtypes print capitalized (Un32/Sn16). The Type
            # column anchors the split: Display is the token before it, the
            # description is everything between the ID and Display.
            tokens = line.split()
            # Split-typo ID ("0x HHHH", see _ID_TOKEN_SPLIT_PREFIXES): rejoin
            # the two ID tokens so the column math below is unchanged.
            # Zero-diff: no snapshot line starts with a bare "0x" token.
            if (
                len(tokens) >= 4
                and tokens[0] in _ID_TOKEN_SPLIT_PREFIXES
                and _HEX4_RE.match(tokens[1])
            ):
                tokens = [tokens[0] + tokens[1]] + tokens[2:]
            # Mid-token split ("0xED E0" for 0xEDE0, see
            # _MID_SPLIT_LINE_RE): rejoin the two hex runs so the column
            # math below is unchanged. The raw-line single space keeps
            # true splits apart from column gaps; the dtype/dash anchors
            # below are the table-row guard. Zero-diff: no snapshot line
            # matches (verified).
            if (
                len(tokens) >= 4
                and _MID_SPLIT_HEAD_RE.match(tokens[0])
                and _MID_SPLIT_TAIL_RE.match(tokens[1])
                and len(tokens[0][2:]) + len(tokens[1]) == 4
                and _MID_SPLIT_LINE_RE.search(line)
            ):
                tokens = [tokens[0] + tokens[1]] + tokens[2:]
            if len(tokens) < 3 or not _ID_TOKEN_RE.match(tokens[0]):
                continue
            if tokens[1] == "=":
                # Product-ID prose ("0xA381 = BMV712"), not a table row.
                continue
                continue
            if tokens[1] == "=":
                # Product-ID prose ("0xA381 = BMV712"), not a table row.
                continue
            # Prose guards case-insensitive for the ID (0X rescue, zero-diff:
            # snapshots carry only lowercase 0xA381, so no new exclusions).
            if any(skip in line or skip.replace("0x", "0X") in line for skip in ["talking to", "BMV712 = 0xA381", "0xA381 = BMV712"]):
                continue

            rid = int(tokens[0], 16)
            # Structural Type anchor (not first-dtype-wins): a dtype-like
            # word may live inside the description itself (probe "0xEDD0
            # Battery voltage un16 type - un16 0.01 V" anchored the interior
            # "un16", yielding desc 'Battery' while dtype/scale/unit parsed
            # fine and every schema assert stayed green — HEX descriptions
            # asserted nowhere). Prefer the first candidate followed by a
            # valid scale+unit tail (see _bmv_dtype_tail_valid); fallback to
            # the first candidate (current behavior) where no tail matches.
            # Zero-diff: no snapshot row carries 2+ dtype tokens (verified),
            # so single-candidate rows anchor identically and the fallback
            # never fires on current docs. (No full HEX description maps:
            # the anchor fix + existing gates are the proportionate scope.)
            cands = [i for i, t in enumerate(tokens) if i >= 2 and _DTYPE_RE.match(t)]
            anchor = next((i for i in cands if _bmv_dtype_tail_valid(tokens[i + 1:])), cands[0] if cands else None)
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
                # Exponent Step form ("1e-2") parses via float(); no snapshot
                # Step uses it (plain decimals/ints only), so zero-diff.
                # Comma-decimal ("0,1") parses via comma->dot; no snapshot
                # Step uses it (verified), so zero-diff.
                # Parenthesized Step cell ("(0.1)", cf. Phoenix p4_try):
                # strip one outer pair and parse honestly; a
                # parenthesized-but-unparsable cell ("(1x)") records via
                # the loud gate below instead of dropping silently (probe
                # Step "(0.1)" gave scale None with empty unknown_scales
                # while SCALE_NONE_ALLOW kept the suite green). No snapshot
                # Step cell is parenthesized (verified), so zero-diff.
                step_try = _strip_one_paren(step)
                if re.fullmatch(r"\d+([.,]\d+)?([eE][+-]?\d+)?", step_try):
                    scale = float(step_try.replace(",", "."))
                elif step and re.search(r"\d", step) and "*" not in step and (
                    (_SCALE_LIKE_RE.match(step_try) and not re.fullmatch(r"\d+([.,]\d+)?([eE][+-]?\d+)?", step_try))
                    or (step_try != step and not re.fullmatch(r"\d+([.,]\d+)?([eE][+-]?\d+)?", step_try))
                ):
                    # Loud unknown-scale gate (unknown-units pattern): a
                    # scale-like Step cell the strict shape rejects records
                    # loudly even on allowlisted IDs. The paren fallback
                    # covers double-wrapped cells ("((0.1))") whose stripped
                    # form still carries parens and misses _SCALE_LIKE_RE.
                    self.unknown_scales.append((rid, step))
                if len(rest) > col + 2:
                    u = " ".join(rest[col + 2:]).strip()
                    if u == "-" or u == "":
                        unit = None
                    elif "(" in u:
                        # Loud unknown-unit gate (short-layout unknown_units
                        # and Step unknown_scales pattern): a parenthesized
                        # Unit remainder ("V (peak)") was dropped to None
                        # silently with both gates empty while
                        # SCALE_NONE_ALLOW/UNIT_NONE_ALLOW excused the
                        # allowlisted ID — reproduced synthetically before
                        # the fix. The parenthesized content is recorded
                        # loudly even on allowlisted IDs (allowlists excuse
                        # missing doc data, never dropped unknown tokens);
                        # unit stays None (as today) so the schema side is
                        # unchanged and the gate is the loud mechanism.
                        # Denylist (explicit, justified): "HEX (AA.BB)" is
                        # the doc's hex-version format notation on the
                        # SW-version row (0xEEF9, Step "-"), not a physical
                        # unit with a qualifier — verified the ONLY
                        # parenthesized settings-Unit cell in any snapshot,
                        # so the gate stays zero-diff while a future "V
                        # (peak)"-style qualifier records loudly.
                        if u != "HEX (AA.BB)":
                            self.unknown_units.append((rid, u))
                        unit = None
                    else:
                        unit = u or None
            elif anchor is not None:
                # Short layout (product info / monitor / historic): the scale
                # is embedded in the Unit column ("0.01 V", "0.001A").
                # Closed vocabulary (V/A/W/Ah/seconds/minutes/min./K/% plus
                # scale-embedded "0.01 V"/"0.001A"/"0.01 kWh" via the
                # scale+next-token gate): a future bare unit (e.g. "Hz" with
                # no numeric factor, probe 0x0303 "Hz" -> None vs control
                # "W" -> "W") would drop to None while SCALE_NONE_ALLOW and
                # UNIT_NONE_ALLOW excuse the allowlisted ID, staying green.
                # Loud gate (Text unknown-units pattern): bare letter-bearing
                # Unit tokens that survive neither gate are recorded as
                # (rid, candidate) even on allowlisted IDs — allowlists
                # excuse missing doc data, never dropped unknown tokens.
                # "0.01 Hz" (with factor) is preserved as "Hz" via the
                # scale gate (fails via allowlist-drift when allowlisted),
                # so only factor-less drops record here. Zero-diff: all
                # snapshot short-layout rows either capture a unit or carry
                # "-" / empty (no letters), verified.
                is_write_only = bool(_WRITE_ONLY_RE.search(desc))
                for j, p in enumerate(rest):
                    p_clean = p.strip()
                    # Parenthesized scale cell ("(0.1)", cf. Phoenix p4_try
                    # and the BMV Step cell): strip one outer pair and parse
                    # honestly instead of leaking into scale None with empty
                    # gates (probe "0xED8E Power 1 un16 (0.1) W" gave
                    # scale None while SCALE_NONE_ALLOW kept the suite
                    # green). No snapshot short-layout cell is
                    # parenthesized (verified), so zero-diff.
                    p_try = _strip_one_paren(p_clean)
                    if not is_write_only:
                        # Unit column holds the factor ("0.01 V" split across
                        # tokens by single-space gaps, or combined "0.001A").
                        # Exponent factors ("1e-2", "1E+1 V") parse via
                        # float(); no snapshot row uses them (plain decimals
                        # only), so zero-diff. Comma-decimal ("0,1 V",
                        # "0,001A") parses via comma->dot; no snapshot row
                        # uses it (verified), so zero-diff. Bare integers
                        # ("1", "10") and decimal ints ("1.5") parse too
                        # (exponent optional like the tail-validator and
                        # Step siblings): probes "1"/"1.5 V"/"10 V" gave
                        # scale None with empty gates while "0.01 V"
                        # parsed (reproduced before fix); SCALE_NONE_ALLOW
                        # excused the allowlisted ID. Zero-diff: no snapshot
                        # short-layout rest token newly matches (verified).
                        sm = re.match(r"^(0?[.,]\d+|\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)([A-Za-z%°]+)?$", p_try)
                        # Bare-int glued units ("1x") never parse as scale:
                        # "1" alone parses (issue-1 fix) and decimal/exponent
                        # glued ("0.001A", "1e-2V") parse, but bare-int plus
                        # letters ("1x") is not a factor — it records via the
                        # loud gates below instead (probe "(1x)" must stay
                        # unknown_scales, not scale 1.0 unit "x"). Zero-diff:
                        # no snapshot token is bare-int glued (verified).
                        if sm and not (sm.group(2) and re.fullmatch(r"\d+", sm.group(1))):
                            scale = float(sm.group(1).replace(",", "."))
                            if sm.group(2):
                                unit = sm.group(2)
                            elif j + 1 < len(rest) and re.fullmatch(r"[A-Za-z%°]+", rest[j + 1].strip()):
                                unit = rest[j + 1].strip()
                            continue
                        # Loud unknown-scale gate (Step/_split_typed_row
                        # pattern): a parenthesized-but-unparsable
                        # digit-bearing cell ("(1x)", double-wrapped
                        # "((0.1))") records loudly even on allowlisted IDs
                        # instead of dropping silently. Non-parenthesized
                        # letter-bearing tokens (e.g. "1x") stay with the
                        # unknown-units gate below; digit-less parens
                        # ("(foo)") are description furniture, never a
                        # scale. Bare-int glued ("1x") counts as unparsable
                        # here too (same exclusion as the scale gate above),
                        # so "(1x)" records instead of parsing as 1.0/x.
                        # Zero-diff: no snapshot short-layout rest token is
                        # parenthesized-digit-bearing (verified).
                        _gm = re.match(r"^(0?[.,]\d+|\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)([A-Za-z%°]+)?$", p_try)
                        _gvalid = bool(_gm) and not (_gm.group(2) and re.fullmatch(r"\d+", _gm.group(1)))
                        if (
                            p_try != p_clean
                            and re.search(r"\d", p_clean)
                            and "*" not in p_clean
                            and not _gvalid
                        ):
                            if (rid, p_clean) not in self.unknown_scales:
                                self.unknown_scales.append((rid, p_clean))
                            continue
                    if p_clean in ["V", "A", "W", "Ah", "seconds", "minutes", "min.", "K", "%"]:
                        unit = p_clean
                if unit is None:
                    for p in rest:
                        cand = p.strip()
                        # Scale-gated parenthesized tokens (recorded above)
                        # belong to the scale gate, never the units gate
                        # ("(1x)" would otherwise record in both).
                        if (rid, cand) in self.unknown_scales:
                            continue
                        # Footnote-glued unknowns ("Hz11"): strip trailing
                        # footnote run for the letter check but record raw.
                        shape = re.sub(r"\d[\d\s,]*$", "", cand).strip() or cand
                        if _UNKNOWN_UNIT_CAND_RE.search(shape):
                            self.unknown_units.append((rid, cand))
                            break
                else:
                    # Trailing-qualifier loud gate (BlueSolar/Orion
                    # section-table and BMV settings pattern): a
                    # parenthesized qualifier after a captured unit ("0.01 V
                    # (peak)") was silently lost — unit "V" with both gates
                    # empty, byte-identical to the qualifier-less control —
                    # because the scan above is guarded by `if unit is
                    # None`. Trailing qualifier tokens are examined even
                    # when a unit was captured; the parenthesized remainder
                    # (from the first paren token onward, covering
                    # multi-word "(peak load)") records loudly while unit
                    # stays as today (schema side unchanged, gate is loud).
                    # Honest scale cells ("(0.1)" parsed above) and
                    # scale-gated unknowns ("(1x)" in unknown_scales) are
                    # never qualifiers: skipped, never recorded here (pinned
                    # by test_bmv_short_layout_parenthesized_scale).
                    # Zero-diff: no snapshot short-layout rest carries
                    # parens (verified), so no snapshot row records.
                    for qi, p in enumerate(rest):
                        if "(" not in p and ")" not in p:
                            continue
                        cand = p.strip()
                        if (rid, cand) in self.unknown_scales:
                            break
                        tried = _strip_one_paren(cand)
                        sm = re.match(
                            r"^(0?[.,]\d+|\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)([A-Za-z%°]+)?$",
                            tried,
                        )
                        if sm and not (sm.group(2) and re.fullmatch(r"\d+", sm.group(1))):
                            continue  # honest factor (e.g. "(0.1)"), not a qualifier
                        if cand in ("V", "A", "W", "Ah", "seconds", "minutes", "min.", "K", "%"):
                            continue
                        qual = " ".join(q.strip() for q in rest[qi:]).strip()
                        if qual and (rid, qual) not in self.unknown_units:
                            self.unknown_units.append((rid, qual))
                        break

            if _WRITE_ONLY_RE.search(desc):
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
        #: ``(rid, access)`` rows whose Access cell is not in the closed
        #: {R, W, R/W} vocabulary (R/(W) normalizes to R). Empty for the
        #: current snapshot; the suite asserts it stays empty so a newly
        #: introduced form (e.g. W/(R)) breaks loudly until explicitly
        #: allowlisted (unknown-units gate pattern). Allowlists never hide
        #: dropped tokens — here the access check ``continue``s on unknown
        #: forms, staying green while a change is missed.
        self.unknown_accesses: List[Tuple[int, str]] = []
        #: ``(rid, token)`` rows whose Scale cell was scale-shaped but
        #: unparsable via ``float()`` (same loud-gate pattern: recorded even
        #: on allowlisted IDs such as 0xEB99). Empty for the current
        #: snapshot (verified: every parsed snapshot row's Scale cell either
        #: parses or carries no digit; prose lines never reach the gate —
        #: their tokens[0] is not an ID).
        self.unknown_scales: List[Tuple[int, str]] = []

    @classmethod
    def from_file(cls, path: Path) -> PhoenixInverterHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        self.unknown_accesses = []
        self.unknown_scales = []
        for line in self.text.splitlines():
            # Token-split (not 2+-whitespace split): rows such as 0xEB99 use
            # single spaces between columns. Register names contain no spaces.
            if "VE_REG_" not in line:
                continue
            tokens = line.split()
            # Split-typo ID ("0x HHHH", see _ID_TOKEN_SPLIT_PREFIXES): rejoin
            # the two ID tokens so the column math below is unchanged.
            # Zero-diff: no snapshot line starts with a bare "0x" token.
            if (
                len(tokens) >= 4
                and tokens[0] in _ID_TOKEN_SPLIT_PREFIXES
                and _HEX4_RE.match(tokens[1])
            ):
                tokens = [tokens[0] + tokens[1]] + tokens[2:]
            # Mid-token split ("0xED E0" for 0xEDE0, see
            # _MID_SPLIT_LINE_RE): rejoin the two hex runs so the column
            # math below is unchanged. The raw-line single space keeps
            # true splits apart from column gaps; the VE_REG_/access
            # columns below are the table-row guard. Zero-diff: no
            # snapshot line matches (verified).
            if (
                len(tokens) >= 4
                and _MID_SPLIT_HEAD_RE.match(tokens[0])
                and _MID_SPLIT_TAIL_RE.match(tokens[1])
                and len(tokens[0][2:]) + len(tokens[1]) == 4
                and _MID_SPLIT_LINE_RE.search(line)
            ):
                tokens = [tokens[0] + tokens[1]] + tokens[2:]
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
            # Loud gate for unknown access forms (e.g. future "W/(R)"):
            # the access check ``continue``s on unmapped forms, staying
            # green while a change is missed. Recorded here even though
            # the comparison skips them — skipping excuses no DB mapping,
            # never a dropped doc token. Zero-diff: snapshots carry only
            # R/W/R/W (R/(W) normalized above), verified.
            if access not in ("R", "W", "R/W"):
                self.unknown_accesses.append((rid, access))
            dtype = "unknown"
            if len(tokens) >= 4 and re.match(r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*|-)$", tokens[3], re.IGNORECASE):
                dtype = tokens[3].strip().lower()
                if dtype == "-":
                    dtype = "unknown"

            scale = None
            unit = None
            if len(tokens) >= 5:
                p4 = tokens[4].strip()
                # Parenthesized scale cell ("(0.1)"): strip one outer pair
                # and parse honestly; a parenthesized-but-unparsable cell
                # ("(1x)") records via the loud gate below instead of
                # dropping silently (probe 0xEB99 "(0.1)" gave scale None
                # with empty unknown_scales while SCALE_NONE_ALLOW kept the
                # suite green). Footnote-only tails ("1)") keep the old
                # skip (no leading "(", never a scale). Comma-decimal
                # ("0,1") parses via comma->dot; no snapshot Scale cell
                # uses parens or commas (verified), so zero-diff.
                p4_try = p4
                if p4_try.startswith("(") and p4_try.endswith(")"):
                    p4_try = p4_try[1:-1].strip()
                if p4_try != "-" and not p4_try.endswith(")"):
                    # Trailing-slash scale cell (0xEB10 "0.02/ s", with the
                    # 60 Hz sibling "0.016" on the next layout line; Note 4
                    # expresses the period count for 50 or 60 Hz AC-out): the
                    # slash marks the 50/60 Hz alternation, the numeric part
                    # is the doc's scale factor, not a missing value.
                    # Exponent form ("1e-2") parses via float() with no
                    # change needed here.
                    try:
                        scale = float(p4_try.rstrip("/").replace(",", "."))
                    except ValueError:
                        # Loud unknown-scale gate (unknown-units pattern): a
                        # digit-bearing but unparsable Scale cell records
                        # loudly even on allowlisted IDs (e.g. 0xEB99).
                        if re.search(r"\d", p4):
                            self.unknown_scales.append((rid, p4))
                        scale = None
                elif re.search(r"\d", p4):
                    # Bare footnotes ("1)", "3)") in Scale position are
                    # furniture (Unit-column footnote run, no scale) — skip,
                    # never record (zero-diff: snapshots carry these on
                    # scale-less rows like 0xEC41/0x034E). Anything else
                    # digit-bearing ending with ")" (e.g. double-wrapped
                    # "((0.1))") records loudly instead of dropping silently.
                    if re.fullmatch(r"\d+\)", p4):
                        pass
                    else:
                        self.unknown_scales.append((rid, p4))
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
#: stay exact (zero diff) on current docs. The denylist excuses only
#: bullet-tailed remainders (genuine device rows: every fields-table row
#: carries >= 1 device bullet, asserted by the devices test, and the
#: bullet-less H1/H4/H5/H10 footnote rows early-return before the fallback):
#: a bullet-less remainder starting with one of these words ("Alarm Some new
#: quantity" with single spaces) is layout collapse or a future shape, so it
#: records loudly instead of leaking into the description silently. Likewise
#: a future unit colliding with one of these words on a genuine
#: bullet-tailed row would still be missed here, but the list is explicit
#: and reviewable, and the single-space divergence test proves the fallback
#: catches non-denylisted units (e.g. "Hz"). Verified: every snapshot
#: empty-Units fallback input carries a bullet tail and its first token is
#: in this set, so the fallback stays empty on current docs.
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
            # description starter on a bullet-tailed (genuine device) row
            # (denylisted above to stay exact); a bullet-less denylisted
            # starter ("Alarm Some new quantity") records loudly instead of
            # leaking into the description silently. A leading numeric token
            # ("1 W Some power row") never letter-matches alone, so it is
            # combined with the next token ("1 W", the spaced variant of the
            # "1W" unit) mirroring the two-space gate's full pre-gap
            # candidate, which already records "1 W". Descriptions never
            # start with a digit (bare footnotes early-return above), so the
            # digit path stays empty on current docs.
            toks = head.strip().split()
            if toks:
                cand1 = toks[0].strip()
                shape1 = re.sub(r"\d[\d\s,]*$", "", cand1).strip() or cand1
                if _UNKNOWN_UNIT_CAND_RE.search(shape1):
                    if cand1 in _SINGLE_SPACE_DESC_DENY and "•" in rest:
                        pass  # genuine empty-Units device row, exact on docs
                    elif unknown_sink is not None:
                        unknown_sink.append((label, cand1))
                elif (
                    len(toks) >= 2
                    and re.fullmatch(r"\d[\d.,]*", cand1)
                ):
                    combined = f"{cand1} {toks[1].strip()}"
                    shape_c = re.sub(r"\d[\d\s,]*$", "", combined).strip() or combined
                    if _UNKNOWN_UNIT_CAND_RE.search(shape_c):
                        if unknown_sink is not None:
                            unknown_sink.append((label, combined))
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


#: Anchored content denylist for VE.Direct Text-mode ERR note/prose lines.
#: With full-remainder ERR capture (no tail restriction) these lines would
#: otherwise match structurally once layout drift gives them a 2+-space gap
#: (today they miss via single-space gaps, so the list is zero-diff
#: drift-defense, never load-bearing on snapshots): Note1/Note2 prose
#: ("Note1: Error 19 can ...", "Note2: Error 21 can ..."), their wrapped
#: continuations ("MPPT charger. Since version ...", "shutdown of the ...",
#: "not persistent."), the section intro ("The error code ..." header and
#: its "the possible values." continuation), and the next field header
#: ("HSDS"). Each pattern is line-start anchored (never a bare substring):
#: a future fault DESCRIPTION containing "shutdown"/"MPPT" mid-line (e.g.
#: "Input shutdown ... 38") must still parse. The test-side ERR scraper
#: mirrors this furniture list with its own copy (kept separate so the
#: scraper stays independently shaped; both lists are pinned exact by the
#: full-coverage tests).
_ERR_FURNITURE_RES = (
    re.compile(r"^\s*Note\d+:"),
    re.compile(r"^\s*MPPT charger"),
    re.compile(r"^\s*shutdown\b"),
    re.compile(r"^\s*not persistent"),
    re.compile(r"^\s*the possible values"),
    re.compile(r"^\s*The error code"),
    re.compile(r"^\s*HSDS\b"),
)


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

        label_re = re.compile(r"^\s*(\S+)\s{2,}(.*?)\s*$")
        # Structural label match (position, not charset/indent): the token
        # is ANY non-space run (no character-class restriction, so future
        # V:PV / DC/IN / *QZ labels cannot share a charset blind spot with
        # the counter by construction) at ANY indent (layout drift to
        # <=4 / >=36 is still a label row; footnotes at 25-27,
        # continuations at 58, furniture at 0 and device headers at 101+
        # are excluded by STRUCTURE, not indent: they carry only single
        # spaces after their first token, never the 2+-space column gap a
        # label row requires). Content denylist below covers only lines
        # that structurally look like label rows: the three "Label Units
        # ..." table headers (indent 18, 2+ gaps) and the "60x ..."
        # device-column headers. Zero-diff: all 63 snapshot labels sit at
        # column 13 with 2+ gaps (verified); footnote/cont/footer lines
        # carry single-space gaps and miss the pattern entirely. The
        # counter (tests/test_spec_parsers.py) is WIDER (any gap): a
        # single-space label row still diverges loudly there, and
        # stoplist-colliding labels ("Note") diverge too — indent/charset
        # no longer diverge, gap/stoplist still do.
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
            # Structural CS row: name is EVERYTHING before the trailing
            # code (no leading-character and no interior class restriction,
            # so a future "*Boost 99" / "V:PV 99" / "DC/IN 99" row cannot
            # share a charset blind spot with the scraper by construction);
            # the code is extracted positionally (name/code gap, then a
            # device-bullet or digit tail). Content denylists for known
            # furniture: the "Bit ..."/"register ..." table header, the
            # "(1) Load search" footnote (starts with "(", never a real
            # state), and bare "N) ..." enumerations. Zero-diff: all 15
            # snapshot names start with a letter (pinned by full coverage).
            # Codes accept up to 4 digits (``\d{1,4}``): current maxima are
            # 252 (CS) / 119 (ERR), so 4 digits give headroom for future
            # extended codes while 5-digit would collide with decimal
            # register IDs (60961-style) and calendar years in the same
            # text. Zero-diff: no snapshot code exceeds 3 digits (15 exact,
            # pinned by full coverage). The live tolerant extractor
            # (tools/fetch_specs.py:889) accepts the same ``\d{1,4}``
            # since round 16, so 4-digit states agree on both sides.
            m = re.match(r"^\s*(.+?)\s+(\d{1,4})\s+(?:•|\d)", line)
            if m:
                name = m.group(1).strip()
                if "Bit" in name or "register" in name:
                    continue
                if name.startswith("(") or re.match(r"^\d+\)", name):
                    continue
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
            # Structural ERR row: name is EVERYTHING before the trailing
            # code (no leading-character and no interior class restriction,
            # so a future "*Boost fault 99" / "DC/IN fault 99" row cannot
            # share a charset blind spot with the scraper by construction);
            # the code is extracted positionally (name/code 2+-space column
            # gap) and the capture runs through end-of-line (NO tail
            # restriction, so a future "Future fault 99 (see note)" row
            # with trailing prose parses with name "Future fault" instead
            # of being missed by a shared EOL-or-bullet tail on both sides
            # while EXPECTED_ERRORS stays green). Content denylist: note /
            # prose / header lines (see _ERR_FURNITURE_RES) can never be
            # rows. Zero-diff: all 20 snapshot names start with a letter
            # and carry no trailing prose (pinned by full coverage). The
            # scraper (tests/test_spec_parsers.py) is WIDER (any gap): a
            # single-space row still diverges loudly there. Codes accept up
            # to 4 digits (``\d{1,4}``, justification as in parse_cs_states
            # above); zero-diff: no snapshot ERR code exceeds 3 digits (20
            # exact, pinned by full coverage).
            if any(rx.match(line) for rx in _ERR_FURNITURE_RES):
                continue
            m = re.match(r"^\s*(.+?)\s{2,}(\d{1,4})\b.*$", line)
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
