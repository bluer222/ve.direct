"""
Specification validation tests for VE.Direct protocol.

Validates library implementation against official Victron Energy documents:
  - BlueSolar-HEX-protocol (Rev 18)
  - Orion-XS-HEX-protocol
  - BMV-7xx-HEX-Protocol (Rev 7)
  - VE.Direct-HEX-Protocol-Phoenix-Inverter (Rev 5)
  - VE.Direct-Protocol-3.34 (Text mode & Product IDs)

Anti-circularity design: every parser output is checked against doc-derived
ground truth hardcoded below (exact counts, full CS/ERR/capability maps, an
independent ID scraper per HEX spec), so a parser regression fails loudly
instead of merely asserting what the parser already found.
"""

from __future__ import annotations

import datetime
import re
import shutil
import sys
import tempfile
import unittest
import warnings
import zlib
from pathlib import Path
from unittest import mock

# Make `python3 -m unittest discover -s tests` work in a bare checkout without
# PYTHONPATH (other test modules rely on import-order side effects for this).
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "tools"))

from spec_parser import (
    BlueSolarHEXParser,
    VEDirectTextParser,
    _is_desc_continuation,
    _is_write_desc,
    load_all_specs,
)
import fetch_specs
from fetch_specs import (
    FRESHNESS_MANIFEST,
    build_sync_manifest_per_spec,
    check_specs,
    compare_snapshot_structures,
    content_gate_verdict,
    download_pdf_bytes,
    extract_pdf_revision_info,
    extract_pdf_text,
    extract_text,
    fetch_remote_snapshot_text,
    live_register_ids,
    live_scale_dtype_diffs,
    live_text_for_structures,
    normalize_snapshot_text,
    register_id_set_from_text,
    remote_last_modified,
    scale_dtype_map_from_text,
    snapshot_freshness_status,
    snapshot_revision_info,
    update_manifest_text,
    update_manifest_text_per_spec,
    verify_remote_content,
)
import ve_direct.registers as R
from ve_direct.exceptions import AmbiguousRegisterError
import ve_direct.tables as T


# ---------------------------------------------------------------------------
# Doc-derived ground truth (counted directly from tests/specs/*.txt).
# ---------------------------------------------------------------------------

# Exact register/field/PID counts per snapshot (A1/A5). Any parser drop or
# accidental addition fails here.
EXPECTED_COUNTS = {
    # BlueSolar: 1.1 tables + 16 per-tracker mirrors + 6 timer events
    # + 31 daily-history (0x1050..0x106E) + 31 MPPT-history. The MPPT
    # history endpoints conflict inside the doc itself: the 1.1-table
    # multi-line range ends at 0x10BE while the "History MPPT day record"
    # detail header prints 0x10A0..0x10BD (daily history agrees at 0x106E
    # on both sides). Documented resolution: the table endpoint wins, so
    # the parser keeps 31 IDs 0x10A0..0x10BE; see
    # test_bluesolar_mppt_history_doc_conflict, which asserts both
    # conflicting endpoints plus this resolution instead of silently
    # unioning them.
    "bluesolar": 206,
    # Orion: 1.1 tables + typo rescue xEDE0 + 0x0004/0x0206/0xEDDA/0x2013/
    # 0x0150/0x0090 detail IDs + 0x1030/0x1042/0x1043/0x106F/0x1099 +
    # 41 cycle-history (0x1070..0x1098).
    "orion": 109,
    "bmv": 110,
    # Phoenix: all VE_REG_ table rows incl. single-spaced 0xEB99.
    "phoenix": 48,
    # Text mode: 16 + 29 + 18 label rows across the three fields tables.
    "fields": 63,
    # Text mode PID list incl. the phased-out 0x0300 row.
    "pids": 153,
}


def count_doc_field_labels(section: str) -> set[str]:
    """Independent Text-mode label counter, structural on both axes while
    staying exact: the label token is ANY non-space run (no charset
    restriction, so future ``V:PV`` / ``DC/IN`` / ``*QZ`` labels cannot
    share a charset blind spot with the parser by construction) at ANY
    indent (layout drift to <=4 / >=36 still counts; footnotes at 25-27,
    continuations at 58, furniture at 0 and device headers at 101+ are
    excluded by STRUCTURE, not indent: with the counter's any-gap
    ``\\s+`` every multi-token line matches structurally, so furniture is
    excluded by explicit pins instead). Furniture is excluded ONLY by
    EXACT full-line pins below (stripped snapshot lines verbatim — the
    ERR-furniture-divergence precedent): the complete set of snapshot
    fields-section lines whose first token collides with a plausible future
    label (Label, 60x, The, Available, When, Victron, General — 28 distinct
    lines, each verified verbatim-present below). A shared content denylist
    on those words (``"Available" in deny`` on both sides) hid a future
    ``Available mV ...`` field on BOTH sides while 63 stayed green
    (reproduced synthetically before the fix); exact pins diverge from the
    parser's stoplist by construction, so a future colliding label row
    matches no pin here while the parser still drops it: scraped-but-
    unparsed, failing loudly instead of agreed-missing (see the
    furniture-pins divergence test). Remaining content-denied starters
    (Available-neighbour footnote prose "the"/"batches,", continuations
    "Amount"/"Depth"/"Number") are NOT shared blind spots and stay denied:
    none is in the parser's stoplist, so a future label colliding with one
    of them is parsed (not denied) while this counter drops it — diverging
    in the opposite direction, failing loudly via set-equality. Deliberately
    NOT denylisted/pinned (divergence proofs): "Note" (parser stoplist word
    — a colliding future label is counted here but skipped by the parser,
    see test_text_field_counter_stoplist_divergence) and "MPPT" (a REAL
    label; the footnote "MPPT 75/15 ..." line yields the same token and
    dedups at distinct-level, so 63 stay exact). Bare footnote numbers /
    bare URLs are single-token lines (no gap) and miss structurally. A
    single-space label row is still counted here but missed by the parser's
    ``\\s{2,}`` rule (gap divergence, see
    test_text_field_counter_gap_divergence); indent/charset no longer
    diverge (both sides structural), gap/stoplist/furniture-pins still do.
    Verified: this yields exactly the 63 doc fields on the current snapshot
    with no prose false positives (all 63 labels at column 13)."""
    # Exact full-line furniture pins (stripped snapshot lines verbatim —
    # every fields-section line starting with Label/60x/The/Available/When/
    # Victron/General, the words a future field could be named): table
    # headers (Label ... + 60x device-column sub-headers), the section intro
    # (both "The ..." lines), footnote prose (14 "Available ..." + 4 "When
    # ..." lines), page footers (Victron/General, each 3x identical).
    # A reworded future note matches no pin and scrapes loudly
    # (over-inclusion); a colliding future label likewise.
    _furniture_lines = frozenset({
        "Label          Units                               Description                                 BMV          BMV          BMV71x           MPPT 1        Phoenix         Phoenix          Smart",
        "Label                 Units                                Description                       BMV          BMV          BMV71x        MPPT 1   Phoenix    Phoenix     Smart",
        "Label                 Units                              Description               BMV   BMV    BMV71x      MPPT 1   Phoenix    Phoenix     Smart",
        "60x          70x         SmartShunt                      Inverter        Charger        BuckBoost",
        "60x          70x         SmartShunt              Inverter   Charger   BuckBoost",
        "60x   70x   SmartShunt            Inverter   Charger   BuckBoost",
        "The units used by the serial interface are as follows:",
        "The VE.Direct protocol is available in the MPPT 70/15 since firmware version v1.09 and up, and only in newer type MPPT 70/15s: the ones with a product id other than 0x300. The first",
        "Available on BlueSolar mppt chargers since firmware version v1.16",
        "Available on BlueSolar mppt chargers since firmware version v1.37",
        "Available on Phoenix Inverter (some models) since firmware version v1.16",
        "Available on Phoenix Inverter v1.16",
        "Available on Phoenix Smart Chargers with triple outputs only",
        "Available on Smart Phoenix Inverter",
        "Available on SmartSolar mppt chargers since firmware version v1.17",
        "Available on SmartSolar mppt chargers since firmware version v1.44 (VE.Direct models) and v1.03 (SmartSolar VE.Can models)",
        "Available since version v1.12, and only for models with a load output.",
        "Available since version v1.15, and only for models with a load output.",
        "Available since version v4.08",
        "Available when the AUX input is configured as Mid Point",
        "Available when the AUX input is configured as Starter Battery",
        "Available when the AUX input is configured as Temperature",
        "When configured as DC monitor, “---” will be sent instead of a value.",
        "When no temperature sensor is connected, “---” will be sent instead of a value.",
        "When the BMV is not synchronised, these statistics have no meaning, so “---” will be sent instead of a value.",
        "When the battery is not discharging the time- to-go is infinite. This is represented as -1.",
        "Victron Energy B.V. | De Paal 35 | 1351 JG Almere | The Netherlands",
        "General phone: +31 (0)36 535 97 00 | E-mail: sales@victronenergy.com",
    })
    # Content-denied starters that are NOT shared blind spots (justified
    # above): footnote prose ("the", "batches,") and description
    # continuations ("Amount", "Depth", "Number") — none in the parser's
    # stoplist, so colliding future labels diverge in the other direction.
    _deny = {
        "the", "batches,",  # footnote prose starters
        "Amount", "Depth", "Number",  # description-continuation starters
    }
    return {
        m.group(1)
        for line in section.splitlines()
        if line.strip() not in _furniture_lines
        for m in [re.match(r"^\s*(\S+)\s+", line)]
        if m and m.group(1) not in _deny
    }


def scrape_doc_ids(text: str) -> set[int]:
    """Independent crude ID collector: every 0xHHHH occurrence plus ".."
    range expansion. Deliberately dumber than the parser so parser drops
    show up as scraped-but-unparsed IDs. The span guard here is
    intentionally far more permissive than the parser's 64-register
    `_expand_id_range` cap (a shared cap would hide the same blind spot on
    both sides, e.g. a 0x1000..0x10FF span missed by both): only absurd
    spans such as the BMV Range-column bound 0x0000..0xFFFF are excluded.
    Likewise the multi-line ``0xAAAA ...`` / bare ``0xBBBB`` scan below
    looks up to 8 lines ahead while the parser only scans 3
    (``range(i + 1, min(i + 4, ...))``): the parser's tight window encodes
    the doc's current layout (endpoint on the very next content line), so
    the test deliberately over-scans — a 5-line gap or format drift is
    still scraped here and then fails loudly as scraped-but-unparsed (or
    as an invented-ID mismatch), instead of being missed by both sides.
    Over-scan false positives are guarded by the strict bare-ID line
    match plus the 1024-span cap.     The 0X prefix is uppercase-X rescued
    (``0[xX]``, like the parser's xEDE0/0XEDF0 rescue): the docs print hex
    both ways, and a case-sensitive ``0x`` here would miss the same future
    uppercase row the parser now rescues, hiding the blind spot on both
    sides. Verified zero-diff: no ``0XHHHH`` occurrence in any current
    snapshot, so no prose false positives.
    The ``0x HHHH`` split typo (space inside the ID token, cf. the parser's
    token-level rescue) is rescued here too (``0[xX]\\s+HHHH``): after
    whitespace-splitting the parser sees two tokens, so a glued-only
    scraper would miss the same future split row the parser now rescues.
    Verified zero-diff: no ``0x HHHH`` occurrence in any current snapshot."""
    ids = {int(x, 16) for x in re.findall(r"0[xX]([0-9A-Fa-f]{4})\b", text)}
    for m in re.finditer(r"0[xX]\s+([0-9A-Fa-f]{4})\b", text):
        ids.add(int(m.group(1), 16))
    for m in re.finditer(r"0[xX]([0-9A-Fa-f]{4})\s*\.\.\s*0[xX]([0-9A-Fa-f]{4})", text):
        a, b = int(m.group(1), 16), int(m.group(2), 16)
        if 0 < b - a <= 1024:
            ids.update(range(a, b + 1))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*0[xX][0-9A-Fa-f]{4}\s*\.\.\.\s*$", line):
            a = int(re.match(r"^\s*0[xX]([0-9A-Fa-f]{4})", line).group(1), 16)  # type: ignore[union-attr]
            for nxt in lines[i + 1 : i + 9]:
                m2 = re.match(r"^\s*(0[xX][0-9A-Fa-f]{4})\s*$", nxt)
                if m2:
                    b = int(m2.group(1), 16)
                    if 0 < b - a <= 1024:
                        ids.update(range(a, b + 1))
                    break
    return ids


def scrape_doc_cs_codes(section: str) -> dict[int, str]:
    """Independent CS counter: structural name with NO tail requirement.

    The parser anchors the trailing device-column marker (``\\s+(•|\\d)``);
    this scraper anchors only the ``name code`` prefix (``\\b``) and
    captures the name as EVERYTHING before the trailing code (no
    leading-character and no interior class restriction, so a ``*Boost`` /
    ``V:PV`` / ``Bulk+Boost`` / ``Charge:Fast`` / ``Mode2Fast`` row cannot
    share a charset blind spot with the parser by construction) — such rows
    are still counted here and (now) parsed too, failing loudly via
    set-equality if either side regresses. Over-inclusion is the safe
    direction: extra hits fail via the invented-IDs-style set-equality,
    while under-inclusion sharing the parser's closed class is the bug.
    Denylists (explicit, justified): none on content — the scraper keeps
    EVERYTHING matching the structural ``name code`` shape and shares NONE
    of the parser's furniture guards ("Bit"/"register" substring, leading
    "(", bare "N) ..." enumerations) by construction, so a future colliding
    state line (e.g. "Orbit Bit mode 99", "Re-register fault 99", "(Boost)
    99" — see the furniture-divergence test) is still counted here while
    the parser drops it: scraped-but-unparsed, failing loudly instead of
    agreed-missing. Verified zero-diff without pins: no snapshot furniture
    line matches the shape (the "(1) Load search" footnote carries no code,
    the device header and "ERR" bound carry none, the section intro carries
    none), so the guard-free scan stays exactly the 15 states while a
    colliding row diverges. Gap independence is preserved (scraper
    ``\\s+`` vs parser bullet-tail): a bullet-less row still diverges (see
    test_cs_counter_bullet_gap_divergence). Codes accept up to 4 digits:
    a future 4-digit state missed by a shared 3-digit bound on both sides
    while hardcoded EXPECTED_CS stays green is the bug (see the 4-digit
    divergence test); 5-digit would collide with decimal register IDs and
    years in the same text. Zero-diff: no snapshot code exceeds 3 digits
    (15 exact, pinned by full coverage)."""
    found: dict[int, str] = {}
    for line in section.splitlines():
        m = re.match(r"^\s*(.+?)\s+(\d{1,4})\b", line)
        if m:
            found[int(m.group(2))] = m.group(1).strip()
    return found


def scrape_doc_error_codes(section: str) -> dict[int, str]:
    """Independent error counter: structural name, permissive gap,
    FULL-REMAINDER tail (copies the CS tail-independence spirit).

    The parser requires a 2+-space gap and runs its capture through
    end-of-line; this scraper tolerates ANY gap (``\\s+``) and also runs
    through end-of-line (``.*``: EOL, bullet tails AND trailing prose like
    "Future fault 99 (see note)" all match), capturing the name as
    EVERYTHING before the trailing code (no leading-character and no
    interior class restriction, so ``*Boost`` / ``:`` / ``.`` names cannot
    share a charset blind spot with the parser by construction) — such
    rows are still counted here and (now) parsed too, failing loudly via
    set-equality if either side regresses. Over-inclusion is safe (extra
    hits fail loudly); under-inclusion sharing the parser's closed class
    is the bug. Furniture (explicit, justified): with the any-gap +
    full-remainder shape, the four Note1/Note2 prose lines below match
    structurally (single-space ``name code`` + remainder), so they are
    excluded by EXACT full-line pins — the stripped furniture lines
    verbatim from the snapshot, never patterns. The parser keeps its
    line-start _ERR_FURNITURE_RES patterns (``shutdown\\b`` etc.); the
    scraper shares NONE of them by construction, so a future colliding
    fault line (e.g. "shutdown fault 99", see the furniture-divergence
    test) matches no exact pin here while the parser still drops it —
    scraped-but-unparsed, failing loudly instead of agreed-missing.
    Header/prose lines that carry no ``name code`` shape ("The error code
    ..." header, "the possible values.", "not persistent.", "HSDS") need
    no pin (verified non-matching on the snapshot). Verified exact
    against the snapshot ERR section with a WIDER bound than the parser
    (scraper scans to "Historical data" prose, parser stops at "^HSDS$"
    — see test_error_codes_full_coverage): the extra HSDS header + blank
    + notes carry no unscratched ``name code`` pattern once
    furniture-pinned, so the wider scan stays exact while a row past the
    parser bound is still scraped here (bound-divergence test).
    Zero-diff: all 20 snapshot ERR names start with a letter and carry no
    trailing prose (verified). Gap independence is preserved (scraper
    ``\\s+`` vs parser ``\\s{2,}``): a single-space row still diverges (see
    test_error_counter_single_space_divergence). Codes accept up to 4
    digits (same justification as the CS scraper above): a future 4-digit
    fault missed by a shared 3-digit bound on both sides while hardcoded
    EXPECTED_ERRORS stays green is the bug (see the 4-digit divergence
    test). Zero-diff: no snapshot code exceeds 3 digits (20 exact, pinned
    by full coverage)."""
    # Exact full-line furniture pins (stripped snapshot lines verbatim):
    # Note1/Note2 prose ("Note1: Error 19 can ...", "MPPT charger. Since
    # version ...", "Note2: Error 21 ...", "shutdown of the ...") is the
    # complete set of snapshot non-row lines matching the structural shape
    # (verified: every other wide-section line is either a real row or
    # shape-mismatched). A reworded future note matches no pin and scrapes
    # loudly (over-inclusion); a colliding future fault line likewise.
    _furniture_lines = frozenset({
        "Note1: Error 19 can be ignored, this condition regularly occurs during start-up or shutdown of the",
        "MPPT charger. Since version 1.15 this error will no longer be reported.",
        "Note2: Error 21 can be ignored for 5 minutes, this condition regularly occurs during start-up or",
        "shutdown of the MPPT charger. Since version 1.16 this warning will no longer be reported when it is",
    })
    found: dict[int, str] = {}
    for line in section.splitlines():
        if line.strip() in _furniture_lines:
            continue
        m = re.match(r"^\s*(.+?)\s+(\d{1,4})\b.*$", line)
        if m:
            found[int(m.group(2))] = m.group(1).strip()
    return found


def scrape_doc_capability_bits(text: str, marker: str, window: int) -> dict[int, str]:
    """Independent capability-bit scraper: OPEN vocabulary + WIDER window.

    The parser uses a closed name class (``[A-Za-z0-9_()./,\\-\\s]``) plus a
    fixed 1500/1800-char window, so a ``+``/``:`` name or a bit past the
    window is missed by both while hardcoded EXPECTED_CAPS stays green. This
    scraper differentiates on BOTH axes (the fix pattern that works):
    (1) open name ``(.+?)`` with only the ``(0=no ...)`` suffix stripped and
    the "Bit"/"register" header guard kept — a future ``+``/``:`` name is
    still scraped here and fails loudly; (2) wider/different window: scans
    ``max(window*3, 5000)`` chars (not the parser's verbatim window) and
    requires the ``(0=no`` marker that every capability row carries (verified:
    all 25 BS + 29 Orion snapshot rows have it, later register/mode tables
    never do). The suffix requirement is what keeps the wider scan exact
    (zero diff) on current snapshots — without it the wider window overwrites
    bit 0 with later "0 or 4 Charger off" mode lines. Verified exact on both
    snapshot capability tables; the window-divergence test proves the wider
    scan catches a bit past the parser window."""
    pos = text.find(marker)
    # Wider/differently (justified above): parser window is shared ground
    # truth that hides the same blind spot; scraper over-scans so a
    # past-window bit fails loudly instead of being missed by both.
    wide = max(window * 3, 5000)
    section = text[pos : pos + wide]
    caps: dict[int, str] = {}
    for line in section.splitlines():
        # Require the "(0=no" capability marker to stay exact on the wider
        # window (later tables lack it); open vocabulary for the name itself.
        m = re.match(r"^\s*(\d{1,2})\s+(.+?)\s*\(0=no.*$", line)
        if m:
            name = m.group(2).strip()
            if "Bit" not in name and "register" not in name:
                caps[int(m.group(1))] = name
    return caps


def scrape_doc_pid_set(text: str) -> set[int]:
    """Independent PID set: every EOL-anchored ``0xHHH[H][*]`` row of the
    product-ID table. Count comparison (153 == 153) stays green under a
    compensating miss+spurious pair; set-equality against the parser (the
    fields check already does this — same pattern) does not."""
    p0 = text.find("The product id:")
    p1 = text.find("These models are phased out.")
    found: set[int] = set()
    for line in text[p0:p1].splitlines():
        m = re.search(r"0x([0-9A-Fa-f]{3,4})\*?\s*$", line)
        if m:
            found.add(int(m.group(1), 16))
    return found


def scrape_decimal_ids(text: str) -> set[int]:
    """Independent non-hex ID scan: bare decimal IDs (any position) + spaced typos.

    Parser and scrape_doc_ids both anchor on the ``0x`` prefix (only the
    "xEDE0" rescue escapes it), so a decimal-only row ("60961 ... un16")
    or a spaced typo ("0 xEDF0") is missed by both. This scan uses the
    disjoint assumption — a 4-5 digit decimal with a table-shaped (dtype)
    tail ANYWHERE on the line (not just line-leading), or ``0 xHHHH`` — and
    therefore fails loudly where the hex strategies agree in silence. Widened
    from line-leading-only (``^\\s*\\d{4,5}``) to mid-line
    (``(?<!\\w)\\d{4,5}\\s+[A-Za-z]...dtype``) with justification: a future
    mid-line decimal ("Some prose 60961 ... un16") was missed by parser, hex
    scraper, and the old scan together. The 4-digit floor excludes
    Bytes-layout sub-table offsets (single digits); the dtype-tail requirement
    excludes dates/versions (which lack a dtype token); verified: zero hits
    on all four current HEX snapshots (before/after). The spaced-typo half
    is uppercase-X rescued (``0\\s+[xX]``, like the 0XEDF0 rescue): a future
    "0 XEDF0" row would otherwise be missed by parser, hex scraper, and
    this scan together. Verified zero-diff: no ``0\\s+XHHHH`` in snapshots.
    Further widened to dtype-LESS ID-only rows (``^\\s*\\d{4,5}\\s+[A-Za-z]``
    with no dtype tail, e.g. "60961      Future history record" and lowercase
    "60961      future history record"): the dtype-tail halves above miss them
    while the hex parser/scraper miss them too (no ``0x``), staying green
    while a row is missed. Shape justification: line-leading only (ID-only
    rows are table rows; a mid-line bare number is a count/date, so the
    tailless scan does NOT repeat the mid-line widening) with a
    case-insensitive description start (mirrors the parsers' hex ID-only
    ``[A-Za-z]`` row shape, now lowercase-capable). Date-like denylist
    (explicit, justified): bare 4-digit calendar years 1900-2100 are
    skipped — the Phoenix snapshot wraps "production stopped in November /
    2016" (a bare line-leading year), and revision logs carry years
    ("15-11-2021", "2021-09-06", "14 April 2025"); hyphenated dates already
    miss the shape (``-`` after the digits, not ``\\s+[A-Z]``) and bare
    years miss it (no description), but a wrapped "2016 Production ..."
    line would false-positive without the year guard. Verified: zero hits
    on all four current HEX snapshots (before/after). The typo half also
    rescues the ``0x HHHH`` split typo (``0[xX]\\s+HHHH``, cf. the
    parser's token-level rescue): "0x EDF0" is missed by the glued hex
    scrape, the dtype-tailed halves, and the single-token ID regex alike.
    Verified zero-diff: no ``0x HHHH`` in snapshots."""
    ids: set[int] = set()
    for line in text.splitlines():
        # Line-leading table-shaped decimal (original coverage).
        m = re.match(
            r"^\s*(\d{4,5})\s+[A-Z].*\b(un8|un16|un24|un32|sn8|sn16|sn32|string\d*)\b",
            line,
            re.IGNORECASE,
        )
        if m:
            ids.add(int(m.group(1)))
        # Mid-line widening (justified above): any 4-5 digit run with a dtype
        # tail on the same line. Stays exact (zero hits) on snapshots.
        for m_mid in re.finditer(
            r"(?<!\w)(\d{4,5})\s+[A-Za-z].*?\b(un8|un16|un24|un32|sn8|sn16|sn32|string\d*)\b",
            line,
            re.IGNORECASE,
        ):
            ids.add(int(m_mid.group(1)))
        # Dtype-less ID-only widening (justified above): line-leading
        # decimal + case-insensitive description, no dtype required. Year
        # denylist keeps calendar-year prose out.
        m_tail = re.match(r"^\s*(\d{4,5})\s+([A-Za-z][A-Za-z0-9 _\-/().*]*?)\s*$", line)
        if m_tail:
            rid = int(m_tail.group(1))
            if not 1900 <= rid <= 2100:
                ids.add(rid)
        for m2 in re.finditer(r"(?<!\w)0\s+[xX]([0-9A-Fa-f]{4})\b", line):
            ids.add(int(m2.group(1), 16))
        for m3 in re.finditer(r"0[xX]\s+([0-9A-Fa-f]{4})\b", line):
            ids.add(int(m3.group(1), 16))
    return ids


def independent_revision_info(text: str):
    """INDEPENDENT re-derivation of (label, date) for the manifest-match test.

    Deliberately shaped unlike fetch_specs.snapshot_revision_info on every
    axis while staying exact on current snapshots: it walks lines (not
    findall-on-tail), requires entry-header anchoring (date/version at line
    start, not any date in tail prose), and parses each format with separate
    line-start checks. A future/example date in tail prose ("next maintenance
    15-11-2030") is therefore ignored here but inflated the old max-over-tail
    implementation to false-FRESH — the divergence test proves this axis is
    independent. Verified: identical (label, date) on all five current
    snapshots."""
    import datetime as _dt

    label = None
    m = re.search(r"(?m)^\s*Rev\s+(\d+)\s*$", text)
    if m:
        label = f"Rev {m.group(1)}"
    else:
        vers = re.findall(r"(?m)^\s*(3\.\d+)\s+\d{1,2}\s+\w+\s+\d{4}", text)
        if vers:
            label = f"v{vers[-1]}"
    marks = [text.rfind(s) for s in ("Revision Log", "Document history", "Changes:")]
    cut = max(marks)
    tail_lines = text[cut:].splitlines() if cut >= 0 else text.splitlines()
    months = (
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    )
    dates: set = set()
    for line in tail_lines:
        s = line.strip()
        # DD-MM-YYYY entry header (BlueSolar/Orion/BMV, optional "N) "/" – rev N" suffix).
        m1 = re.match(r"^(?:\d+\)\s*)?(\d{1,2})-(\d{1,2})-(\d{4})(?:\s.*)?$", s)
        if m1:
            try:
                dates.add(_dt.date(int(m1.group(3)), int(m1.group(2)), int(m1.group(1))))
            except ValueError:
                pass
            continue
        # ISO entry header (Phoenix "5) 2021-09-06").
        m2 = re.match(r"^(?:\d+\)\s*)?(\d{4})-(\d{1,2})-(\d{1,2})(?:\s.*)?$", s)
        if m2:
            try:
                dates.add(_dt.date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3))))
            except ValueError:
                pass
            continue
        # Text-mode version+date row ("3.34 14 April 2025", leading spaces).
        m3 = re.match(
            r"^(3\.\d+)\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
            s,
        )
        if m3:
            try:
                dates.add(_dt.date(int(m3.group(4)), months.index(m3.group(3)) + 1, int(m3.group(2))))
            except ValueError:
                pass
            continue
    if label is None and any("Initial version" in ln for ln in tail_lines):
        label = f"Initial version {max(dates).isoformat()}" if dates else "Initial version"
    return label, (max(dates) if dates else None)


# Scraped IDs that are not registers (verified against the snapshots):
# example values, version/fault illustrations, sentinels, product IDs from
# the PID tables, and the documented 0xEFF0 typo (means 0xEDF0).
NON_REGISTER_IDS = {
    # BlueSolar: 0x0064/0x0096 message-example values; 0x0101 firmware-version
    # illustration ("0x0101 is version 1.01", no such register); 0x0300 and
    # 0xA040..0xA07E + 0xA102..0xA117 are product IDs here (0x0300 is a BMV
    # history register, not a BlueSolar HEX register); the product-ID
    # appendix jumps from 0xA07E to 0xA102, so the 0xA07F..0xA101 gap (131
    # IDs) is never scraped and stays out of the denylist; 0x4116
    # app-version example; 0xD101 release-candidate illustration;
    # 0x7FFF/0xFFFF reserved/sentinel values; 0xAAAA framing error; 0xEFF0
    # typo for 0xEDF0.
    "bluesolar": frozenset({0x0064, 0x0096, 0x0101, 0x0300, 0x4116, 0x7FFF, 0xAAAA, 0xD101, 0xEFF0, 0xFFFF})
    | frozenset(range(0xA040, 0xA07F))  # appendix 0xA040..0xA07E
    | frozenset(range(0xA102, 0xA118)),  # appendix 0xA102..0xA117
    # Orion: 0x0064/0x01F4 example values; 0x7FFF ping-response illustration;
    # 0xA3F0/0xA3F1 product IDs; rest as above.
    "orion": frozenset({0x0064, 0x01F4, 0x7FFF, 0xAAAA, 0xEFF0, 0xFFFF, 0xA3F0, 0xA3F1}),
    # BMV: 0x0000 Range-column bound ("0x0000..0xFFFF" for SW version, not a
    # register); 0x00C8/0x01F4 example values; 0x0200..0x0205 + 0xA381 product
    # IDs; 0x4401 app-version illustration; 0xD101 RC illustration.
    "bmv": frozenset({0x0000, 0x00C8, 0x01F4, 0x4401, 0xAAAA, 0xD101, 0xFFFF, 0xA381})
    | frozenset(range(0x0200, 0x0206)),
    # Phoenix: 0x0064/0x0096 example values; 0x0160 section-header typo for
    # 0x0150 (asserted separately via parse_ble_header_evidence); 0x4116,
    # 0xAAAA, 0xD101 as above.
    "phoenix": frozenset({0x0064, 0x0096, 0x0160, 0x4116, 0xAAAA, 0xD101}),
}

# Parser IDs the crude scraper cannot see (explicitly justified):
# Orion "xEDE0" (missing leading 0 in the doc; cf. BlueSolar 0xEDE0).
SCRAPER_BLIND_IDS = {"orion": frozenset({0xEDE0})}

# Registers whose doc genuinely gives no numeric scale factor ("-" scale,
# Type-only rows, bitmask/mode/struct/write-only records). Everything else
# must have a parsed scale, so a dropped factor fails instead of skipping.
SCALE_NONE_ALLOW = {
    # BlueSolar: product info, device control/mode/state, display Type-only
    # rows, remote/network mode+status, lighting/ports/load/relay mode rows,
    # per-tracker mode mirrors (base 0xEDB3 has "-"), history/timer structs,
    # restore/clear commands.
    "bluesolar": frozenset({
        0x0004, 0x0100, 0x0104, 0x010A, 0x010B, 0x0140,  # product info / caps
        0x0200, 0x0201, 0x0202, 0x0205, 0x0207,  # device control
        0x0244,  # tracker count (mode value, no factor)
        0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0406, 0x0408,  # display
        0x1030, 0x104F,  # clear/total history commands/records
        0x2000, 0x2004, 0x2009, 0x200C, 0x200D, 0x200E, 0x200F, 0x2018,  # remote/net
        0x2030,  # solar activity 0/1
        0xD01F, 0xD0C0,  # 2-wire BMS state/config
        0xECC3, 0xECD3, 0xECE3, 0xECF3,  # tracker mode mirrors
        0xED91, 0xED98, 0xED9E, 0xEDA8, 0xEDAB,  # load/port control modes
        0xEDB3,  # tracker mode
        0xEDCC, 0xEDCD, 0xEDCE,  # version registers
        0xEDD4, 0xEDD9, 0xEDDA,  # state info / relay mode / error code
        0xEDE5, 0xEDE8, 0xEDFD, 0xEDFE, 0xEDFF,  # battery on/off/mode settings
    })
    | frozenset(range(0x1050, 0x106F))  # daily history structs
    # MPPT history structs: doc table runs to 0x10BE while the detail
    # header prints 0x10BD (table endpoint wins, see
    # test_bluesolar_mppt_history_doc_conflict).
    | frozenset(range(0x10A0, 0x10BF))
    | frozenset(range(0xEDA0, 0xEDA6)),  # timer event words
    # Orion: product info, BLE/mode/state/off-reason, battery on/off/mode
    # rows, remote/network mode+status, history-block records, detail-only
    # IDs (0x0206/0xEDDA/0x2013: Value tables / prose, no widths).
    "orion": frozenset({
        0x0004, 0x0090, 0x0100, 0x0102, 0x0104, 0x010A, 0x010B, 0x0140, 0x0150,
        0x0200, 0x0201, 0x0206, 0x0207,
        0x1030, 0x1042, 0x1043, 0x106F, 0x1099,
        0x2000, 0x200C, 0x200D, 0x200E, 0x200F,
        0x2013, 0xEDDA,
        0xEDE5, 0xEDE8, 0xEDFD, 0xEDFE, 0xEDFF,
    })
    | frozenset(range(0x1070, 0x1099)),  # cycle history structs
    # BMV: product info, bare-unit/count rows (W/minutes/seconds or no unit
    # column at all: factor 1 implied but no number printed), write-only
    # commands (Type "-"), timestamp (Step "-"), HEX version (Step "-").
    "bmv": frozenset({
        0x0100, 0x0101, 0x010A, 0x010B, 0x010C, 0x0120, 0x0150,  # product info
        0x0303, 0x0304, 0x0308, 0x0309, 0x030A, 0x030B,  # bare counters
        0x031F,  # alarm acknowledge, all columns "-"
        0x0FFE,  # TTG, bare "minutes"
        0xED8E,  # power, bare "W"
        0xEEB6,  # sync state, "-"
        0x1029, 0x102C, 0x0004, 0x1030,  # write-only commands
        0xEC41,  # settings-changed timestamp, Step "-"
        0xEEF9,  # SW version, Step "-" (HEX AA.BB format)
    }),
    # Phoenix: product info/caps, mode/state/off/warn/alarm/relay (0/1-style,
    # no factor), 50/60Hz select, NVM command, dynamic-cutoff factor/enable
    # cells without factors, settings-changed timestamp, restore-defaults.
    # 0xEB10 prints "0.02/" (50 Hz period, 60 Hz sibling "0.016" on the next
    # layout line): a real factor parsed as 0.02, so it is not allowlisted
    # here; its DB-vs-doc value difference is a documented exception, see
    # SCALE_VALUE_EXCEPTIONS below.
    "phoenix": frozenset({
        0x0004, 0x0090, 0x0100, 0x0101, 0x0102, 0x010A, 0x010B, 0x0140, 0x0150,
        0x0200, 0x0201, 0x0207, 0x031C, 0x031E, 0x034E, 0x034F,
        0xEB03, 0xEB99, 0xEBB2, 0xEBB3, 0xEBB5, 0xEBB7, 0xEBBA, 0xEC41,
    }),
}

# Registers where the doc's printed factor is deliberately NOT the DB scale
# (explicitly justified per entry; the comparison below asserts the DB value
# instead of the printed factor, so drift on either side still fails).
SCALE_VALUE_EXCEPTIONS = {
    # Phoenix 0xEB10: Scale cell "0.02/" (50 Hz period; "0.016" for 60 Hz on
    # the next layout line), Unit "s" — but Note 4 defines the wire value as
    # a COUNT of AC-out periods, so no single constant is a correct scale
    # (hardcoding 0.02 would misdecode 60 Hz units by 25%). The DB stores raw
    # counts (scale 1.0, unit ""), documented at the register definition.
    ("phoenix", 0xEB10): 1.0,
}

# Registers whose doc genuinely prints no unit ("-" or empty Unit column).
# Everything else must have a parsed unit, so a dropped unit (e.g. future
# "Hz" parsed as None by the BMV vocabulary) fails as missing_units instead
# of skipping. Explicit sets (not "skip when None"): a future unit dropped
# to None for an ID absent here fails loudly. Zero-diff verified: these are
# exactly the snapshot None/"-" sets.
UNIT_NONE_ALLOW = {
    # BlueSolar: product info/caps, device control/mode, display/mode rows
    # with "-" or no Unit column, history/timer structs (byte layouts, no
    # single unit), per-tracker mode mirrors (base 0xEDB3 "-"), tail-current
    # 0xEDE7 (row ends after dtype, no Unit column though scale/dtype
    # present), version/state/relay/mode rows.
    "bluesolar": frozenset({
        0x0004, 0x0100, 0x0104, 0x010A, 0x010B, 0x0140,
        0x0200, 0x0201, 0x0202, 0x0205, 0x0207, 0x0244,
        0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0406, 0x0408,
        0x1030, 0x104F,
        0x2000, 0x2004, 0x2009, 0x200C, 0x200D, 0x200E, 0x200F, 0x2018,
        0xD01F, 0xD0C0,
        0xECC3, 0xECD3, 0xECE3, 0xECF3,
        0xED91, 0xED98, 0xED9E, 0xEDA8, 0xEDAB, 0xEDB3,
        0xEDCC, 0xEDCD, 0xEDCE, 0xEDD4, 0xEDD9, 0xEDDA, 0xEDE7,
    })
    | frozenset(range(0x1050, 0x106F))  # daily history structs, no unit
    | frozenset(range(0x10A0, 0x10BF))  # MPPT history structs (table wins)
    | frozenset(range(0xEDA0, 0xEDA6)),  # timer event words
    # Orion: product info/BLE/mode/state, history-block records and
    # cycle-history structs (byte layouts), detail-only IDs with no Unit
    # column (0x0206/0xEDDA/0x2013 prose, 0x0090/0x0150 single-field but
    # unit-less in doc table), tail-current 0xEDE7 (as BlueSolar).
    "orion": frozenset({
        0x0004, 0x0090, 0x0100, 0x0102, 0x0104, 0x010A, 0x010B, 0x0140, 0x0150,
        0x0200, 0x0201, 0x0206, 0x0207,
        0x1030, 0x1042, 0x1043, 0x106F, 0x1099,
        0x2000, 0x200C, 0x200D, 0x200E, 0x200F, 0x2013, 0xEDDA, 0xEDE7,
    })
    | frozenset(range(0x1070, 0x1099)),  # cycle history structs
    # BMV: product info, bare counters without Unit column or Type "-"
    # rows (write-only commands, timestamp Step "-", HEX version Step "-",
    # sync/state/mode rows with no Unit column). 0xED8E (power, "W") is
    # deliberately NOT here: a future "Hz" dropped to None must fail.
    "bmv": frozenset({
        0x0004, 0x0090, 0x0100, 0x0101, 0x010A, 0x010B, 0x010C, 0x0150,
        0x0303, 0x0304, 0x0309, 0x030A, 0x030B, 0x031F,
        0x034D, 0x034E, 0x034F, 0x0400, 0x0FFD, 0x1005,
        0x1029, 0x102C, 0x1030, 0xEEB6, 0xEEB8,
        0xEEE0, 0xEEE1, 0xEEE2, 0xEEE3, 0xEEE4, 0xEEE5, 0xEEE6, 0xEEE7,
        0xEEE8, 0xEEF5, 0xEEF6, 0xEEF7, 0xEEF8, 0xEEF9, 0xEEFC, 0xEEFE,
    }),
    # Phoenix: product info/caps/mode/state rows with "-" or no Unit
    # column, NVM/relay/cutoff rows without units, timestamp.
    "phoenix": frozenset({
        0x0004, 0x0090, 0x0100, 0x0101, 0x0102, 0x010A, 0x010B, 0x0140, 0x0150,
        0x0200, 0x0201, 0x0207, 0x031C, 0x031E, 0x034E, 0x034F,
        0xEB03, 0xEB99, 0xEBB2, 0xEBB3, 0xEBB5, 0xEBB7, 0xEBBA, 0xEC41,
    }),
}

# Registers where the DB unit deliberately differs from the doc's printed
# unit (explicitly justified per entry with src file:line; src/ is
# read-only, so the comparison asserts the DB value instead of weakening
# the normalization — drift on either side still fails).
UNIT_VALUE_EXCEPTIONS = {
    # BlueSolar/Orion 0xEDEA: doc prints "V" but the DB models
    # BAT_VOLTAGE_SETTING as ENUM (values include Auto/specials, not a pure
    # voltage; src/ve_direct/registers/__init__.py:199 unit ""), so no
    # physical unit is correct. Doc "V" vs DB "" mismatch allowlisted here,
    # not normalized away.
    ("bluesolar", 0xEDEA): "",
    ("orion", 0xEDEA): "",
    # Phoenix 0xEB10: doc prints Unit "s" with Scale "0.02/" but Note 4
    # defines the wire value as a COUNT of AC-out periods (50/60 Hz
    # dependent, no single seconds scale); the DB stores raw counts
    # (src/ve_direct/registers/__init__.py:406 unit "", scale 1.0 per
    # SCALE_VALUE_EXCEPTIONS). Doc "s" vs DB "" mismatch allowlisted here.
    ("phoenix", 0xEB10): "",
}


def _normalize_unit_for_comparison(raw: str | None) -> str | None:
    """Normalize a doc/DB unit cell for comparison (None stays None).

    Strips whitespace; "-" / "" map to "" (doc prints no unit, DB empty).
    Pure enum descriptions ("0=no, 1=yes", "0=off, ...", "0xFF = user")
    map to "" (value encodings in the Unit column, not physical units; DB
    "" is correct). Comma/parenthetical suffixes ("A, 0xFFFF=use max" ->
    "A", "% (of 0xEDF0)" -> "%", "min, 0=mid-night" -> "min") strip to the
    leading physical unit. Aliases (minute/minutes/min. -> min, hours ->
    h, seconds -> s, °C/degC -> C so "%CAP/°C" and "%CAP/degC" both become
    "%CAP/C") unify doc/DB vocabularies without hiding real disagreements
    (e.g. doc "V" vs DB "" stays mismatched and must be allowlisted, never
    normalized away).
    """
    if raw is None:
        return None
    u = raw.strip()
    if u in ("-", ""):
        return ""
    # Pure enum hint in the Unit column (starts with digit/0x, contains
    # "="): not a physical unit (e.g. "0xFF = user", "0=no, 1=yes").
    # "A, 0xFFFF=..." starts with a unit letter, so it falls through to
    # comma-stripping below (honest "A").
    if "=" in u and re.match(r"^\s*(0[xX]|\d)", u):
        # Starts with a digit/0x (enum), not a unit letter -> no unit.
        return ""
    # Leading-unit + sentinel/option suffix: keep the physical unit.
    if "," in u:
        u = u.split(",")[0].strip()
    u = re.sub(r"\s*\(.*\)", "", u).strip()
    low = u.lower()
    _alias = {"minute": "min", "minutes": "min", "min.": "min", "hours": "h", "seconds": "s"}
    if low in _alias:
        u = _alias[low]
    # Degree unification (doc "°C" vs DB "C"; doc "%CAP/°C" vs DB
    # "%CAP/degC"): both become "C"/"%CAP/C" so the alias matches without
    # hiding V-vs-"" style disagreements.
    u = u.replace("°C", "C").replace("degC", "C").replace("°", "")
    return u

# Registers whose doc genuinely gives no single wire width: struct records
# (byte-field layouts), write-only commands (Type "-"), firmware-dependent
# widths (0xEDD1/0xEDD3 "(*4)": un32 <= v1.12, un16 above; parser normalizes
# the footnote cell to unknown, provenance in notes), and Orion detail-only
# IDs (Value tables / prose without widths).
DTYPE_UNKNOWN_ALLOW = {
    # Table endpoint wins for the MPPT 0x10BE-vs-0x10BD doc conflict (see
    # test_bluesolar_mppt_history_doc_conflict): all 31 IDs are structs.
    "bluesolar": frozenset({0x0004, 0x1030, 0x104F, 0xEDD1, 0xEDD3})
    | frozenset(range(0x1050, 0x106F))
    | frozenset(range(0x10A0, 0x10BF))
    | frozenset(range(0xEDA0, 0xEDA6)),
    "orion": frozenset({0x0004, 0x0206, 0x1030, 0x1042, 0x1043, 0x106F, 0x1099, 0x2013, 0xEDDA})
    | frozenset(range(0x1070, 0x1099)),
    "bmv": frozenset({0x0004, 0x031F, 0x1029, 0x102C, 0x1030}),
    "phoenix": frozenset({0x0004}),
}

# Full CS map from Text 3.34 §CS (incl. 11/245/246/247/248/252 the old
# truncated window dropped).
EXPECTED_CS = {
    0: "Off", 1: "Low power", 2: "Fault", 3: "Bulk", 4: "Absorption",
    5: "Float", 6: "Storage", 7: "Equalize (manual)", 9: "Inverting",
    11: "Power supply", 245: "Starting-up", 246: "Repeated absorption",
    247: "Auto equalize / Recondition", 248: "BatterySafe",
    252: "External Control",
}

# Full ERR map from Text 3.34 §ERR (incl. 66/67/68/116/117/119).
EXPECTED_ERRORS = {
    0: "No error", 2: "Battery voltage too high",
    17: "Charger temperature too high", 18: "Charger over current",
    19: "Charger current reversed", 20: "Bulk time limit exceeded",
    21: "Current sensor issue (sensor bias/sensor broken)",
    26: "Terminals overheated", 28: "Converter issue (dual converter models only)",
    33: "Input voltage too high (solar panel)",
    34: "Input current too high (solar panel)",
    38: "Input shutdown (due to excessive battery voltage)",
    39: "Input shutdown (due to current flow during off mode)",
    65: "Lost communication with one of devices",
    66: "Synchronised charging device configuration issue",
    67: "BMS connection lost", 68: "Network misconfigured",
    116: "Factory calibration data lost", 117: "Invalid/incompatible firmware",
    119: "User settings invalid",
}

# Full capability-bit maps (BlueSolar 0x0140: bits 22-24 absent in doc;
# Orion adds 28-31).
EXPECTED_CAPS_BS = {
    0: "Load output present", 1: "Rotary encoder present", 2: "History support",
    3: "Batterysafe mode", 4: "Adaptive mode", 5: "Manual equalise",
    6: "Automatic equalise", 7: "Storage mode", 8: "Remote on/off via rx pin",
    9: "Solar timer/streetlighting", 10: "Alternative VE.Direct TX pin function",
    11: "User defined load switch", 12: "Load current in TEXT protocol",
    13: "Panel current", 14: "BMS support", 15: "External control support",
    16: "Synchronized charging support", 17: "Alarm relay",
    18: "Alternative VE.Direct RX pin function", 19: "Virtual load output",
    20: "Virtual relay", 21: "Plugin display support",
    25: "Load Automatic Energy Selector", 26: "Battery test", 27: "PAYGO support",
}
EXPECTED_CAPS_ORION = {
    **EXPECTED_CAPS_BS,
    28: "Hibernate mode", 29: "AC power apparent power",
    30: "PSU function", 31: "Needs battery to shutdown",
}


class TestSpecParsers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs_dir = Path(__file__).resolve().parent / "specs"
        cls.specs = load_all_specs(cls.specs_dir)
        cls.bs_regs = cls.specs["bluesolar"].parse_registers()
        cls.or_regs = cls.specs["orion"].parse_registers()
        cls.bmv_regs = cls.specs["bmv"].parse_registers()
        cls.ph_regs = cls.specs["phoenix"].parse_registers()
        cls.text_fields = cls.specs["text"].parse_fields()
        cls.pids = cls.specs["text"].parse_pids()
        cls.raw_texts = {
            "bluesolar": (cls.specs_dir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore"),
            "orion": (cls.specs_dir / "Orion-XS-HEX-protocol.txt").read_text(errors="ignore"),
            "bmv": (cls.specs_dir / "BMV-7xx-HEX-Protocol.txt").read_text(errors="ignore"),
            "phoenix": (cls.specs_dir / "VE.Direct-HEX-Protocol-Phoenix-Inverter.txt").read_text(errors="ignore"),
            "text": (cls.specs_dir / "VE.Direct-Protocol-3.34.txt").read_text(errors="ignore"),
        }

    def test_py_typed_exists(self):
        """Verify PEP 561 py.typed marker is present."""
        pkg_dir = Path(__file__).resolve().parent.parent / "src" / "ve_direct"
        py_typed = pkg_dir / "py.typed"
        self.assertTrue(py_typed.is_file(), f"py.typed marker missing at {py_typed}")

    def test_spec_parser_counts(self):
        """Exact parser yields per snapshot: any dropped/added register fails."""
        self.assertEqual(len(self.bs_regs), EXPECTED_COUNTS["bluesolar"])
        self.assertEqual(len(self.or_regs), EXPECTED_COUNTS["orion"])
        self.assertEqual(len(self.bmv_regs), EXPECTED_COUNTS["bmv"])
        self.assertEqual(len(self.ph_regs), EXPECTED_COUNTS["phoenix"])
        self.assertEqual(len(self.text_fields), EXPECTED_COUNTS["fields"])
        self.assertEqual(len(self.pids), EXPECTED_COUNTS["pids"])

    def test_spec_snapshot_reverse_coverage(self):
        """Every register ID in the snapshot tables must exist in parser
        output (reverse direction of schema compliance)."""
        reg_dicts = {
            "bluesolar": self.bs_regs, "orion": self.or_regs,
            "bmv": self.bmv_regs, "phoenix": self.ph_regs,
        }
        for key, reg_dict in reg_dicts.items():
            scraped = scrape_doc_ids(self.raw_texts[key])
            uncovered = scraped - set(reg_dict) - NON_REGISTER_IDS[key]
            self.assertEqual(
                uncovered, set(),
                f"{key}: snapshot IDs missing from parser output: "
                f"{sorted(f'0x{i:04X}' for i in uncovered)}",
            )
            # Parser must not invent IDs beyond scraper + justified rescues.
            invented = set(reg_dict) - scraped - SCRAPER_BLIND_IDS.get(key, frozenset())
            self.assertEqual(
                invented, set(),
                f"{key}: parser IDs absent from snapshot: "
                f"{sorted(f'0x{i:04X}' for i in invented)}",
            )

    def test_bluesolar_mppt_history_doc_conflict(self):
        """The doc conflicts on the MPPT history endpoint: the 1.1-table
        multi-line range ends at 0x10BE while the "History MPPT day
        record" detail header prints 0x10A0..0x10BD. This test pins both
        conflicting endpoints as found in the snapshot (so a doc revision
        fixing either side fails here for re-triage) and pins the
        documented parser resolution: the table endpoint wins, i.e. 0x10BE
        is parsed and the MPPT history block spans 31 IDs 0x10A0..0x10BE.
        A possibly-invented 0x10BE therefore cannot hide inside the
        scraped-union of test_spec_snapshot_reverse_coverage."""
        text = self.raw_texts["bluesolar"]
        lines = text.splitlines()
        # Table side: " 0x10A0 ..." line followed (within the doc's layout)
        # by the bare " 0x10BE" endpoint line.
        table_starts = [
            i for i, line in enumerate(lines)
            if re.match(r"^\s*0x10A0\s*\.\.\.\s*$", line)
        ]
        self.assertEqual(len(table_starts), 1, "expected exactly one 0x10A0 ... table row")
        table_end = next(
            (lines[j].strip() for j in range(table_starts[0] + 1, table_starts[0] + 4)
             if re.match(r"^\s*0x[0-9A-Fa-f]{4}\s*$", lines[j])),
            None,
        )
        self.assertEqual(table_end, "0x10BE", f"table-side MPPT endpoint changed: {table_end}")
        # Header side: the detail section prints the shorter span.
        headers = [
            line.strip() for line in lines
            if re.search(r"0x10A0\s*\.\.\s*0x10BD", line)
        ]
        self.assertEqual(len(headers), 1, f"header-side 0x10A0..0x10BD line changed: {headers}")
        self.assertIn("History MPPT day record", headers[0])
        # Documented resolution: table endpoint wins.
        self.assertIn(0x10BE, self.bs_regs, "parser must keep the table endpoint 0x10BE")
        mppt_block = [rid for rid in self.bs_regs if 0x10A0 <= rid <= 0x10BE]
        self.assertEqual(len(mppt_block), 31, f"MPPT history block must span 31 IDs, got {len(mppt_block)}")
        # Control: daily history agrees on both sides (0x106E), so only
        # the MPPT span is conflict-allowlisted here.
        daily_end = next(
            (lines[j].strip() for i, line in enumerate(lines)
             if re.match(r"^\s*0x1050\s*\.\.\.\s*$", line)
             for j in range(i + 1, i + 4)
             if re.match(r"^\s*0x[0-9A-Fa-f]{4}\s*$", lines[j])),
            None,
        )
        self.assertEqual(daily_end, "0x106E")
        self.assertTrue(any(re.search(r"0x1050\s*\.\.\s*0x106E", line) for line in lines))

    def test_decimal_id_scan_divergence(self):
        """Proves the decimal scan is independent of the shared ``0x``
        assumption: synthetic decimal-only rows (line-leading AND mid-line)
        are found by scrape_decimal_ids yet missed by both the parser and
        scrape_doc_ids, i.e. they would fail loudly as scraped-but-unparsed
        instead of being missed by both. Control: zero decimal hits on all
        four current HEX snapshots (only the "xEDE0" rescue escapes the
        ``0x`` assumption, and it is explicitly allowlisted)."""
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            with self.subTest(spec=key):
                self.assertEqual(scrape_decimal_ids(self.raw_texts[key]), set())
        synthetic = (
            "0xEDD3     Battery foo               0.1         un16        V\n"
            "60961      Future decimal register   0.1         un16        V\n"
            "0 xEDF0    Spaced typo register      0.01        un16        A\n"
        )
        self.assertEqual(scrape_decimal_ids(synthetic), {60961, 0xEDF0})
        parsed = BlueSolarHEXParser(synthetic).parse_registers()
        self.assertNotIn(60961, parsed)
        self.assertNotIn(0xEDF0, parsed)
        self.assertNotIn(60961, scrape_doc_ids(synthetic))
        self.assertNotIn(0xEDF0, scrape_doc_ids(synthetic))

    def test_decimal_id_midline_divergence(self):
        """Proves the widened decimal scan catches mid-line IDs missed by
        parser, hex scraper, AND the old line-leading-only scan: a synthetic
        mid-line decimal ("Some prose 60961 ... un16") is found by the
        widened scrape_decimal_ids yet missed by both the parser and
        scrape_doc_ids, i.e. it would fail loudly instead of staying green."""
        synthetic = "Some prose 60961 mid-line un16 register mention\n"
        self.assertEqual(scrape_decimal_ids(synthetic), {60961})
        self.assertNotIn(60961, BlueSolarHEXParser(synthetic).parse_registers())
        self.assertNotIn(60961, scrape_doc_ids(synthetic))

    def test_midline_dtypeless_decimal_pinned(self):
        """Pins the mid-line dtype-LESS boundary as uncovered-by-evidence:
        "Some prose 60961 Future history record mention" (mid-line bare
        decimal + capitalized words, no dtype tail) is missed by ALL scans
        and parsers (line-leading ID-only scan, dtype-tailed mid-line scan,
        hex scraper, history-region parsers) while counts stay green. A
        widened mid-line tailless scan was attempted live and REJECTED:
        ``(?<!\\w)\\d{4,5}\\s+[A-Z]...`` matches footer prose on all
        snapshots ("Victron Energy B.V. | De Paal 35 | 1351 JG Almere | The
        Netherlands" -> 1351 + "JG Almere", 2 hits BlueSolar/BMV/Phoenix, 0
        Orion), so widening would false-positive without a footer-specific
        denylist that merely trades one blind spot for another. Line-leading
        tailless stays (issue-4 fix); mid-line tailless stays pinned here —
        do not widen without replacing this pin with a strictly better
        zero-FP mechanism. Documents exactly what shape is uncovered.
        Cycle-15 re-report ("Some prose 60961 Future ..." probe): re-ran
        the probe synthetically — still missed by ALL scans/parsers — and
        re-ran the footer false-positive evidence below (still non-zero on
        BlueSolar/BMV/Phoenix via "1351 JG Almere"), so the pin STANDS by
        evidence; the re-reported probe is cited here as additional
        confirmation, not a fix."""
        probe = "Some prose 60961 Future history record mention\n"
        self.assertEqual(scrape_decimal_ids(probe), set())
        self.assertNotIn(60961, scrape_doc_ids(probe))
        self.assertNotIn(
            60961,
            BlueSolarHEXParser(
                "Restore factory defaults\n" + probe + "1.2 Asynchronous items\n"
            ).parse_registers(),
        )
        # Cycle-15 re-reported wording (same uncovered shape): still missed
        # by every scan while counts stay green — additional confirmation.
        probe15 = "Some prose 60961 Future fault mention\n"
        self.assertEqual(scrape_decimal_ids(probe15), set())
        self.assertNotIn(60961, scrape_doc_ids(probe15))
        self.assertNotIn(
            60961,
            BlueSolarHEXParser(
                "Restore factory defaults\n" + probe15 + "1.2 Asynchronous items\n"
            ).parse_registers(),
        )
        # Evidence: naive mid-line tailless widening false-positives on
        # footer postcodes (verified live on snapshots).
        import re as _re

        fp_total = 0
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            text = self.raw_texts[key]
            for line in text.splitlines():
                if _re.match(r"^\s*\d{4,5}\s", line):
                    continue
                for mm in _re.finditer(r"(?<!\w)(\d{4,5})\s+([A-Z][A-Za-z0-9 _\-/().*]{3,})\b", line):
                    rid = int(mm.group(1))
                    if 1900 <= rid <= 2100:
                        continue
                    fp_total += 1
                    break
        # BlueSolar/BMV/Phoenix footers hit (1351 JG Almere); Orion has no
        # such mid-line hit — the widening is still unreliable (non-zero FP
        # on 3/4 specs), so the pin stands.
        self.assertGreaterEqual(fp_total, 3)

    def test_decimal_id_only_no_dtype_tail_divergence(self):
        """Proves the dtype-less decimal widening catches ID-only rows missed
        by every dtype-tailed strategy: a synthetic "60961      Future
        history record" row (no dtype tail) is found by the widened
        scrape_decimal_ids yet missed by scrape_doc_ids (no ``0x``) — the
        old dtype-tailed scan missed it too while counts stayed green — and
        the history-region parsers now handle it honestly (struct/R, like
        the hex ID-only rows) instead of staying green-but-missed. Controls:
        zero widened-scan hits on all four current HEX snapshots (pinned
        empty-or-same), hyphenated/bare dates stay out ("15-11-2021 some
        change", bare "2016", "2021-09-06" carry "-" or no description, and
        a wrapped "2016 Production ..." line is year-denylisted)."""
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            with self.subTest(spec=key):
                self.assertEqual(scrape_decimal_ids(self.raw_texts[key]), set())
        synthetic = "60961      Future history record\n"
        self.assertEqual(scrape_decimal_ids(synthetic), {60961})
        self.assertNotIn(60961, scrape_doc_ids(synthetic))
        # Parser handling (honest pass on both sides): history-region
        # decimal ID-only rows parse as struct/R, mirroring hex ID-only.
        from spec_parser import OrionXSHEXParser

        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(parser=cls.__name__):
                text = (
                    "Restore factory defaults\n"
                    "60961      Future history record\n"
                    "1.2 Asynchronous items\n"
                )
                probe = cls(text).parse_registers()[60961]
                self.assertEqual(probe.description, "Future history record")
                self.assertEqual(probe.data_type, "struct")
                self.assertEqual(probe.access, "R")
                self.assertIsNone(probe.scale)
        # Date-like denylist controls (must stay out of the widened scan).
        self.assertEqual(scrape_decimal_ids("15-11-2021 some change\n"), set())
        self.assertEqual(scrape_decimal_ids("2016\n"), set())
        self.assertEqual(scrape_decimal_ids("5) 2021-09-06\n"), set())
        self.assertEqual(scrape_decimal_ids("2016 Production stopped note\n"), set())

    def test_lowercase_id_only_row_rescue(self):
        """Lowercase ID-only history rows ("60961 future history record" /
        "0x104F future history record") were missed by hex/decimal scans and
        parsers (``[A-Z]`` start required) while counts stayed green. Both
        now start ``[A-Za-z]`` (case-insensitive row-start), so the probes
        pass honestly with exact descriptions on all sides. Date/prose
        denylist (explicit, justified): year guard 1900-2100 covers calendar
        years; hyphenated dates ("15-11-2021", "2021-09-06") miss the shape
        ("-" after digits, not "\\s+[A-Za-z]"); bare years/counts miss it
        (no description); footer "1351 JG Almere" is mid-line (not
        line-leading) and outside the history region (parsers) while the
        global scraper stays exact (verified zero hits). Zero-diff: no
        snapshot history-region line matches the widened shape (verified),
        and the global decimal scan stays empty on all four HEX snapshots.
        Divergence probes with lowercase names."""
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            with self.subTest(spec=key):
                self.assertEqual(scrape_decimal_ids(self.raw_texts[key]), set())
        # Decimal lowercase probe: scraped + parsed honestly.
        self.assertEqual(scrape_decimal_ids("60961      future history record\n"), {60961})
        self.assertNotIn(60961, scrape_doc_ids("60961      future history record\n"))
        from spec_parser import OrionXSHEXParser

        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(parser=cls.__name__):
                text = (
                    "Restore factory defaults\n"
                    "60961      future history record\n"
                    "1.2 Asynchronous items\n"
                )
                probe = cls(text).parse_registers()[60961]
                self.assertEqual(probe.description, "future history record")
                self.assertEqual(probe.data_type, "struct")
                self.assertEqual(probe.access, "R")
                self.assertIsNone(probe.scale)
                # Hex lowercase probe: same honest pass (hex scraper already
                # caught it via bare 0xHHHH, so this also proves parser
                # alignment rather than a new scraper blind spot).
                text_hex = (
                    "Restore factory defaults\n"
                    "0x104F      future history record\n"
                    "1.2 Asynchronous items\n"
                )
                probe_hex = cls(text_hex).parse_registers()[0x104F]
                self.assertEqual(probe_hex.description, "future history record")
        # Controls: dates/years stay out of the widened scan.
        self.assertEqual(scrape_decimal_ids("2016 production stopped note\n"), set())
        self.assertEqual(scrape_decimal_ids("15-11-2021 some change\n"), set())

    def test_uppercase_0X_id_rescue_divergence(self):
        """Proves uppercase-0X IDs are rescued (not missed by all three):
        a synthetic "0XEDF0" typed row is now parsed AND hex-scraped, and a
        spaced "0 XEDF0" typo is decimal-scraped — the old case-sensitive
        ``0x``/``0\\s+x`` missed all three while counts stayed green.
        Rescue-style like the existing xEDE0 handling. Control: zero
        ``0XHHHH``/``0\\s+XHHHH`` occurrences in any current HEX snapshot,
        so the widened match is zero-diff (no prose false positives)."""
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            with self.subTest(spec=key):
                self.assertEqual(
                    re.findall(r"0X[0-9A-Fa-f]{4}\b", self.raw_texts[key]), [],
                    f"{key} snapshot gained 0X prose",
                )
                self.assertEqual(
                    re.findall(r"(?<!\\w)0\\s+X[0-9A-Fa-f]{4}\\b", self.raw_texts[key]), [],
                    f"{key} snapshot gained spaced 0X prose",
                )
        typed = (
            "1.1 Get and Set items\n"
            "0XEDF0     Battery max current     0.01        un16        A\n"
            "1.2 Asynchronous items\n"
        )
        parsed = BlueSolarHEXParser(typed).parse_registers()
        self.assertIn(0xEDF0, parsed)
        self.assertEqual(parsed[0xEDF0].data_type, "un16")
        self.assertIn(0xEDF0, scrape_doc_ids(typed))
        spaced = "0 XEDF0    Spaced uppercase typo   0.01        un16        A\n"
        self.assertIn(0xEDF0, scrape_decimal_ids(spaced))

    def test_split_0x_space_id_rescue_divergence(self):
        """Proves ``0x HHHH`` split-typo IDs are rescued (not missed by all
        three): a synthetic "0x EDF0" typed row is now parsed (section
        tables, ID-only rows, BMV/Phoenix loops via token rejoin) AND
        hex-scraped AND decimal-scraped — the glued-only ``0xHHHH`` /
        ``0\\s+x`` / single-token ID regex missed all three while counts
        stayed green. Rescue-style like the existing xEDE0/0X handling.
        Control: zero ``0x HHHH`` occurrences in any current HEX snapshot,
        so the widened match is zero-diff (no prose false positives)."""
        for key in ("bluesolar", "orion", "bmv", "phoenix"):
            with self.subTest(spec=key):
                self.assertEqual(
                    re.findall(r"0[xX] +[0-9A-Fa-f]{4}\b", self.raw_texts[key]), [],
                    f"{key} snapshot gained split-typo prose",
                )
        from spec_parser import (
            BMV7xxHEXParser,
            OrionXSHEXParser,
            PhoenixInverterHEXParser,
            _split_typed_row,
        )

        # Token-level rescue (single-token ID regex misses the two-token ID).
        row = _split_typed_row("0x EDF0    Split typo  0.01  un16  A")
        self.assertIsNotNone(row)
        assert row is not None
        rid, desc, scale_str, dtype_raw, unit_str, typo = row
        self.assertEqual(rid, 0xEDF0)
        self.assertTrue(typo)
        self.assertEqual(dtype_raw, "un16")
        # Section tables (BlueSolar + Orion, incl. the 0x-section pre-filter).
        typed = (
            "1.1 Get and Set items\n"
            "0x EDF0    Split typo  0.01  un16  A\n"
            "1.2 Asynchronous items\n"
        )
        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(parser=cls.__name__):
                parsed = cls(typed).parse_registers()
                self.assertIn(0xEDF0, parsed)
                self.assertEqual(parsed[0xEDF0].data_type, "un16")
        # History ID-only rows (BlueSolar + Orion).
        id_only = (
            "Restore factory defaults\n"
            "0x EDF0    Split typo record\n"
            "1.2 Asynchronous items\n"
        )
        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(id_only=cls.__name__):
                self.assertIn(0xEDF0, cls(id_only).parse_registers())
        # BMV + Phoenix loops (token rejoin; Phoenix needs its VE_REG_ name).
        bmv = BMV7xxHEXParser(
            "hdr\n0x EDF0     Split typo               -         un16        A\nftr"
        ).parse_registers()
        self.assertIn(0xEDF0, bmv)
        self.assertEqual(bmv[0xEDF0].data_type, "un16")
        phx = PhoenixInverterHEXParser(
            "0x EDF0 VE_REG_SPLIT_TYPO R/W un16 1 V\n"
        ).parse_registers()
        self.assertIn(0xEDF0, phx)
        self.assertEqual(phx[0xEDF0].name, "VE_REG_SPLIT_TYPO")
        # Both scrapers rescue the split form (was missed by both).
        self.assertIn(0xEDF0, scrape_doc_ids("0x EDF0    Split typo  0.01  un16  A\n"))
        self.assertIn(0xEDF0, scrape_decimal_ids("0x EDF0    Split typo  0.01  un16  A\n"))

    def test_write_hint_phrase_divergence(self):
        """Proves write-hint detection is phrase-guarded, not bare-substring:
        a synthetic readable containing "restore" (but not the command phrase
        "restore default/factory") is NOT write-only, while real command rows
        still are. The old bare "restore" matched any readable with the
        substring."""
        # Real commands still write-only.
        self.assertTrue(_is_write_desc("Restore default"))
        self.assertTrue(_is_write_desc("Restore Defaults (write only)"))
        self.assertTrue(_is_write_desc("Restore factory defaults"))
        self.assertTrue(_is_write_desc("Clear history"))
        self.assertTrue(_is_write_desc("Reset counters command"))
        # Synthetic readables containing "restore" stay readable.
        self.assertFalse(_is_write_desc("Device restores defaults on boot readable value"))
        self.assertFalse(_is_write_desc("restore factor in readable"))
        self.assertFalse(_is_write_desc("Pressure restore point readable"))
        # Anchoring: "synchronized" (BMV 0x0FFD) never matches "synchronize".
        self.assertFalse(_is_write_desc("Start synchronized (2,3)"))
        self.assertFalse(_is_write_desc("User yield (resettable)"))

    def test_section_table_write_hint_parsing(self):
        """Section-table rows with command descriptions must parse as
        write_only/W (same _is_write_desc/_WRITE_HINTS logic as ID-only
        rows): probe ``0xEDF0 Restore defaults ...`` in a section table
        parsed as R/W before the fix while the schema skipped R/W defaults,
        staying green while a write-only marker was missed. Zero-diff: no
        current BlueSolar/Orion section-table row carries a write hint, so
        no snapshot row changes meaning (counts 206/109 unchanged)."""
        from spec_parser import OrionXSHEXParser, _split_typed_row

        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(parser=cls.__name__):
                synthetic = (
                    "1.1 Get and Set items\n"
                    "0xEDF0     Restore defaults test     0.01        un16        A\n"
                    "1.2 Asynchronous items\n"
                )
                probe = cls(synthetic).parse_registers()[0xEDF0]
                self.assertEqual(probe.access, "W")
                self.assertEqual(probe.data_type, "write_only")
                self.assertIsNone(probe.scale)
                # Control: ordinary readable stays R/W with scale/dtype.
                control = cls(
                    "1.1 Get and Set items\n"
                    "0xEDF0     Battery maximum current     0.01        un16        A\n"
                    "1.2 Asynchronous items\n"
                ).parse_registers()[0xEDF0]
                self.assertEqual(control.access, "R/W")
                self.assertEqual(control.scale, 0.01)
                self.assertEqual(control.data_type, "un16")
        # Zero-diff: no snapshot section-table typed row is a write hint,
        # so the fix changes no current meaning (counts pinned elsewhere).
        for key, fname, cls, bare_x in (
            ("bluesolar", "BlueSolar-HEX-protocol.txt", BlueSolarHEXParser, False),
            ("orion", "Orion-XS-HEX-protocol.txt", OrionXSHEXParser, True),
        ):
            with self.subTest(snapshot=key):
                text = (self.specs_dir / fname).read_text(errors="ignore")
                start = text.find("1.1 Get and Set items")
                end = text.find("1.2 Asynchronous items")
                section = text[start:end] if end > start else text[start:]
                hits = []
                for line in section.splitlines():
                    if not re.match(r"^\s*0[xX][0-9A-Fa-f]{4}\b", line):
                        continue
                    row = _split_typed_row(line, allow_bare_x_prefix=bare_x)
                    if row is None:
                        continue
                    _rid, desc, _sc, _dt, _un, _ty = row
                    if _is_write_desc(desc):
                        hits.append((_rid, desc))
                self.assertEqual(hits, [], f"{key} section-table write hints changed meaning: {hits}")

    def test_multiline_scan_gap_divergence(self):
        """Proves the test-side multi-line scan is truly independent of the
        parser's tight 3-line window: a synthetic ``0xAAAA ...`` range
        whose bare endpoint sits 5 lines ahead (format drift) is expanded
        by scrape_doc_ids (8-line window) but missed by the parser, i.e.
        it would fail loudly as scraped-but-unparsed instead of being
        missed by both."""
        filler = ["some intervening layout line"] * 4
        text = "\n".join([" 0x2000 ...", *filler, " 0x2002"])
        scraped = scrape_doc_ids(text)
        self.assertIn(0x2001, scraped)
        parsed = BlueSolarHEXParser(text).parse_registers()
        self.assertNotIn(0x2001, parsed)

    def test_spec_schema_compliance(self):
        """Verify 100% register presence, scale factor, data type, and flavor compliance across all 4 specifications."""
        type_map = {
            "un8": "uint8_t",
            "un16": "uint16_t",
            "un24": "uint24_t",
            "un32": "uint32_t",
            "sn8": "int8_t",
            "sn16": "int16_t",
            "sn32": "int32_t",
            "string": "string",
        }

        def expected_dtype(dtype: str):
            dl = dtype.lower()
            if dl.startswith("string"):
                return "string"
            return type_map.get(dl)

        specs_to_test = [
            ("BlueSolar", self.bs_regs, "MPPT", ["MPPT", "CHG", "MPPT_RS", "ANY"]),
            ("Orion", self.or_regs, "ORION", ["ORION", "CHG", "ANY"]),
            ("BMV", self.bmv_regs, "BMV", ["BMV", "BMV71", "ANY"]),
            ("Phoenix", self.ph_regs, "INV", ["INV", "ANY"]),
        ]

        missing_registers = []
        scale_mismatches = []
        dtype_mismatches = []
        inaccessible = []
        missing_scales = []
        missing_dtypes = []
        missing_units = []
        unit_mismatches = []

        for doc_name, reg_dict, query_flavor, allowed_flavors in specs_to_test:
            key = doc_name.lower()
            scale_allow = SCALE_NONE_ALLOW[key]
            dtype_allow = DTYPE_UNKNOWN_ALLOW[key]
            unit_allow = UNIT_NONE_ALLOW[key]
            for rid, spec_reg in reg_dict.items():
                # Scale must be extracted wherever the doc prints a factor.
                if spec_reg.scale is None and rid not in scale_allow:
                    missing_scales.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name})")
                # Dtype must be known wherever the doc prints a width.
                if expected_dtype(spec_reg.data_type) is None and rid not in dtype_allow:
                    missing_dtypes.append(
                        f"{doc_name} 0x{rid:04X} ({spec_reg.name}): {spec_reg.data_type!r}"
                    )
                # Unit must be extracted wherever the doc prints one
                # (normalize-then-compare, Phoenix access pattern): genuine
                # no-unit docs ("-"/empty) live in UNIT_NONE_ALLOW only, so
                # a future unit dropped to None (e.g. BMV "Hz" vocabulary
                # gap: probe 0xED8E "Hz" -> None vs control "W" -> "W")
                # fails here as missing_units instead of skipping.
                _raw_unit = spec_reg.unit
                _is_empty_unit = _raw_unit is None or (isinstance(_raw_unit, str) and _raw_unit.strip() in ("-", ""))
                if rid in unit_allow:
                    # Genuine no-unit doc: skip comparison, but a future
                    # revision printing a unit here must fail loudly for
                    # re-triage (allowlist drift).
                    if not _is_empty_unit:
                        unit_mismatches.append(
                            f"{doc_name} 0x{rid:04X} ({spec_reg.name}): allowlisted no-unit now prints unit {_raw_unit!r}"
                        )
                else:
                    if _is_empty_unit:
                        missing_units.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name})")
                # 1. Register presence
                candidates = R._REGISTERS_BY_ID.get(rid, [])
                if not candidates:
                    missing_registers.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name})")
                    continue

                # 2. Flavor accessibility
                accessible = any(
                    f in reg.flavors or "ANY" in reg.flavors or (f == "ORION" and "CHG" in reg.flavors) or (f == "MPPT" and "CHG" in reg.flavors)
                    for reg in candidates
                    for f in allowed_flavors
                )
                if not accessible:
                    inaccessible.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name})")

                # Look up resolved register for this flavor
                try:
                    reg = R.lookup(rid, flavor=query_flavor)
                except Exception:
                    reg = candidates[0]

                # 3. Scale factor compliance
                if spec_reg.scale is not None:
                    expected_scale = SCALE_VALUE_EXCEPTIONS.get(
                        (key, rid), spec_reg.scale)
                    if reg.scale is None or abs(reg.scale - expected_scale) > 1e-5:
                        actual = reg.scale if reg else "NOT_FOUND"
                        scale_mismatches.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name}): expected scale {expected_scale}, got {actual}")

                # 4. Strict data type compliance (no skipping empty dtypes)
                expected = expected_dtype(spec_reg.data_type)
                if expected:
                    actual_dtype = reg.dtype or (
                        "string" if reg.cls == "STRING"
                        else "uint8_t" if reg.cls == "BOOLEAN"
                        else ""
                    )
                    if actual_dtype != expected:
                        dtype_mismatches.append(f"{doc_name} 0x{rid:04X} ({reg.name}): expected dtype {expected}, got {reg.dtype!r}")

                # 5. Unit compliance (normalize-then-compare, access pattern):
                # doc genuinely prints a unit (not in UNIT_NONE_ALLOW) ->
                # normalized doc must equal normalized DB, except
                # UNIT_VALUE_EXCEPTIONS where the DB deliberately differs
                # (src read-only: mismatches allowlisted with file:line,
                # never normalized away). Genuine no-unit docs skipped above.
                if rid not in unit_allow and not _is_empty_unit:
                    norm_doc = _normalize_unit_for_comparison(spec_reg.unit)
                    norm_db = _normalize_unit_for_comparison(reg.unit)
                    expected_unit = UNIT_VALUE_EXCEPTIONS.get((key, rid), norm_doc)
                    if norm_db != expected_unit:
                        unit_mismatches.append(
                            f"{doc_name} 0x{rid:04X} ({reg.name}): expected unit {expected_unit!r} (doc {spec_reg.unit!r}), got DB {reg.unit!r}"
                        )

        self.assertEqual(missing_scales, [], f"Documented scales not extracted: {missing_scales}")
        self.assertEqual(missing_dtypes, [], f"Documented dtypes not extracted: {missing_dtypes}")
        self.assertEqual(missing_units, [], f"Documented units not extracted: {missing_units}")
        self.assertEqual(missing_registers, [], f"Missing spec registers: {missing_registers}")
        self.assertEqual(inaccessible, [], f"Inaccessible registers for device flavor: {inaccessible}")
        self.assertEqual(scale_mismatches, [], f"Scale factor mismatches: {scale_mismatches}")
        self.assertEqual(dtype_mismatches, [], f"Data type mismatches: {dtype_mismatches}")
        self.assertEqual(unit_mismatches, [], f"Unit mismatches: {unit_mismatches}")

    def test_hex_unit_future_probe(self):
        """HEX units were never asserted: probe ``0xED8E Power - Sn16 Hz``
        parsed as unit=None with no failure (control ``... W`` gives 'W')
        while schema checks compared IDs/scales/dtypes/access only. The
        unit comparison above (normalize-then-compare, access pattern)
        closes it: 0xED8E is NOT in UNIT_NONE_ALLOW (snapshot doc prints
        "W"), so a dropped "Hz" fails as missing_units instead of skipping.
        Control: aliases normalize (minute->min, °C->C, etc.) without
        hiding V-vs-"" style disagreements (allowlisted with file:line)."""
        from spec_parser import BMV7xxHEXParser

        probe = BMV7xxHEXParser("hdr\n0xED8E     Power               -         Sn16        Hz\nftr").parse_registers()[0xED8E]
        control = BMV7xxHEXParser("hdr\n0xED8E     Power               -         Sn16        W\nftr").parse_registers()[0xED8E]
        # BMV vocabulary drops unknown "Hz" to None while "W" parses.
        self.assertIsNone(probe.unit)
        self.assertEqual(control.unit, "W")
        # 0xED8E genuinely prints a unit (snapshot "W"), so it is absent
        # from the no-unit allowlist: a None here must fail loudly as
        # missing_units in the schema check above, never skip.
        self.assertNotIn(0xED8E, UNIT_NONE_ALLOW["bmv"])
        self.assertNotIn(0xED8E, UNIT_NONE_ALLOW["orion"])
        # Normalization aliases hold (no weakening to hide mismatches).
        self.assertEqual(_normalize_unit_for_comparison("minute"), "min")
        self.assertEqual(_normalize_unit_for_comparison("hours"), "h")
        self.assertEqual(_normalize_unit_for_comparison("seconds"), "s")
        self.assertEqual(_normalize_unit_for_comparison("°C"), "C")
        self.assertEqual(_normalize_unit_for_comparison("%CAP/°C"), _normalize_unit_for_comparison("%CAP/degC"))
        self.assertEqual(_normalize_unit_for_comparison("A, 0xFFFF=use max"), "A")
        self.assertEqual(_normalize_unit_for_comparison("% (of 0xEDF0)"), "%")
        # Genuine DB-vs-doc disagreements stay allowlisted with file:line
        # (src read-only), never normalized away.
        self.assertEqual(_normalize_unit_for_comparison("V"), "V")
        self.assertIn(("bluesolar", 0xEDEA), UNIT_VALUE_EXCEPTIONS)
        self.assertIn(("phoenix", 0xEB10), UNIT_VALUE_EXCEPTIONS)

    def test_bmv_unknown_units_loud_on_allowlisted_ids(self):
        """BMV bare-unit drop hid behind allowlists: probe ``0x0303 Foo -
        un32 Hz`` gives unit None + scale None, both skipped via
        SCALE_NONE_ALLOW/UNIT_NONE_ALLOW (0x0303 bare counters), suite
        green while Hz is missed (control ``... W`` gives 'W'). Unknown
        BMV units are LOUD (recorded + asserted, Text unknown-units gate
        pattern) even on allowlisted IDs — allowlists excuse missing doc
        data, never dropped unknown tokens. Zero-diff: snapshot yields
        no unknowns."""
        from spec_parser import BMV7xxHEXParser

        # Snapshot stays exact (no unknowns on current docs).
        self.assertEqual(self.specs["bmv"].unknown_units, [])
        # Synthetic probe on allowlisted 0x0303 (bare counters): dropped
        # to None on both axes but recorded loudly.
        parser = BMV7xxHEXParser("hdr\n0x0303     Foo               -         un32        Hz\nftr")
        probe = parser.parse_registers()[0x0303]
        self.assertIsNone(probe.unit)
        self.assertIsNone(probe.scale)
        self.assertIn(0x0303, SCALE_NONE_ALLOW["bmv"])
        self.assertIn(0x0303, UNIT_NONE_ALLOW["bmv"])
        self.assertEqual(parser.unknown_units, [(0x0303, "Hz")])
        # Control: known "W" parses with no record.
        ctrl = BMV7xxHEXParser("hdr\n0x0303     Foo               -         un32        W\nftr")
        self.assertEqual(ctrl.parse_registers()[0x0303].unit, "W")
        self.assertEqual(ctrl.unknown_units, [])
        # With factor ("0.01 Hz") the unit is preserved (scale gate), so
        # no unknown record — it fails via allowlist-drift instead (0x0303
        # allowlisted no-unit now prints "Hz").
        factored = BMV7xxHEXParser("hdr\n0x0303     Foo               -         un32        0.01 Hz\nftr")
        self.assertEqual(factored.parse_registers()[0x0303].unit, "Hz")
        self.assertEqual(factored.unknown_units, [])

    def test_parser_dtype_vocabulary(self):
        """Parser dtypes must use the module convention (known wire widths,
        struct, write_only, unknown): a verbatim doc cell such as BlueSolar
        0xEDD1/0xEDD3 "(*4)" normalizes to unknown with the footnote kept in
        notes, never leaking through even for allowlisted IDs."""
        vocab = re.compile(
            r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*|unknown|struct|write_only)$",
            re.IGNORECASE,
        )
        reg_dicts = {
            "bluesolar": self.bs_regs, "orion": self.or_regs,
            "bmv": self.bmv_regs, "phoenix": self.ph_regs,
        }
        bad = [
            f"{key} 0x{rid:04X}: {r.data_type!r}"
            for key, reg_dict in reg_dicts.items()
            for rid, r in reg_dict.items()
            if not vocab.match(r.data_type)
        ]
        self.assertEqual(bad, [], f"Non-convention dtypes leaked: {bad}")
        # Footnote-contaminated cells keep their provenance in notes.
        for rid in (0xEDD1, 0xEDD3):
            self.assertEqual(self.bs_regs[rid].data_type, "unknown")
            self.assertIn("(*4)", self.bs_regs[rid].notes)

    def test_aliased_ids_disambiguation(self):
        """Verify aliased IDs raise AmbiguousRegisterError without flavor and resolve safely with flavor."""
        # 0xED8E: BMV DC_CHANNEL1_POWER (int16, 1W) vs Orion OUTPUT_POWER (uint32, 0.01W)
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0xED8E)
        self.assertEqual(R.lookup(0xED8E, flavor="BMV").name, "DC_CHANNEL1_POWER")
        self.assertEqual(R.lookup(0xED8E, flavor="ORION").name, "OUTPUT_POWER")

        # 0x0320: INV (0.01V) vs Orion (0.1V) vs BMV (0.1V)
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0x0320)
        self.assertEqual(R.lookup(0x0320, flavor="INV").scale, 0.01)
        self.assertEqual(R.lookup(0x0320, flavor="ORION").scale, 0.1)

        # 0x0350: MPPT (0.01V) vs BMV (0.1V)
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0x0350)
        self.assertEqual(R.lookup(0x0350, flavor="MPPT").scale, 0.01)
        self.assertEqual(R.lookup(0x0350, flavor="BMV").scale, 0.1)
        self.assertEqual(
            R.lookup("RELAY_BAT_LOW_SET", flavor="MPPT").scale, 0.01)
        self.assertEqual(
            R.lookup("RELAY_BAT_LOW_SET", flavor="BMV").scale, 0.1)

    def test_snapshot_freshness_against_online(self):
        """Real-directory freshness gate: FAIL when the online docs are
        reachable and newer than tests/specs/*.txt, SKIP (never fail)
        when offline or otherwise unverifiable. Pinned EXPECTED_COUNTS
        alone would stay green against a new online revision; this gate
        closes that hole while staying green in offline environments
        (network unreachable -> "unknown" -> skip). The skip stays LOUD:
        a visible warning names the unverifiable specs, the last
        online-verified manifest, AND plainly lists which specs have NEVER
        been verified online (verified_on None) — not just "unreachable
        now" — so "green" is never mistaken for "verified"."""
        status, detail = snapshot_freshness_status(self.specs_dir)
        if status == "unknown":
            never = sorted(
                k for k, v in FRESHNESS_MANIFEST.items() if v.get("verified_on") is None
            )
            never_msg = (
                f"NEVER verified online: {', '.join(never)}"
                if never
                else "all specs previously verified online"
            )
            warnings.warn(
                "spec freshness UNKNOWN (unverifiable, NOT verified fresh): "
                f"{detail}; {never_msg}; last-verified-online manifest: {FRESHNESS_MANIFEST}"
            )
            self.skipTest(f"freshness unverifiable, skipping: {detail}; {never_msg}")
        self.assertEqual(status, "fresh", f"online specs newer than snapshots: {detail}")

    def test_snapshot_content_against_online(self):
        """Second-stage content gate: header-only Last-Modified cannot see
        a same-timestamp CDN re-upload, so when pdftotext AND network are
        available the full-download verify_remote_content comparison runs
        as well (DIFF -> FAIL). Offline or without pdftotext it SKIPs with
        a clear reason; default runs stay fast (the header stage above
        skips first when offline, so no download is even attempted). The
        skip plainly lists NEVER-verified specs like the header stage."""
        if shutil.which("pdftotext") is None:
            self.skipTest("content stage needs `pdftotext` in PATH; skipping")
        results = verify_remote_content(self.specs_dir, timeout=30.0)
        status, detail = content_gate_verdict(results)
        if status == "unknown":
            never = sorted(
                k for k, v in FRESHNESS_MANIFEST.items() if v.get("verified_on") is None
            )
            never_msg = (
                f"NEVER verified online: {', '.join(never)}"
                if never
                else "all specs previously verified online"
            )
            self.skipTest(f"remote content unverifiable, skipping: {detail}; {never_msg}")
        self.assertEqual(status, "fresh", f"remote content differs: {detail}")

    def test_freshness_manifest_matches_snapshots(self):
        """The checked-in last-verified manifest must track the snapshots'
        own revision markers (offline check, no network): doc drift fails
        here until re-verified online and refreshed via --sync. Compares
        against an INDEPENDENT re-derivation (independent_revision_info,
        line-walking and entry-anchored, unlike snapshot_revision_info's
        findall-on-tail) — not the same function both sides — so a shared
        max-over-tail bug cannot stay green."""
        for key in fetch_specs.OFFICIAL_SPECS:
            text = (self.specs_dir / f"{key}.txt").read_text(errors="ignore")
            label, date = snapshot_revision_info(text)
            indep_label, indep_date = independent_revision_info(text)
            # Independent axis must agree with the primary parser on current
            # snapshots (both anchored, zero diff); divergence proves
            # independence on synthetic future-date prose.
            self.assertEqual((indep_label, indep_date), (label, date), f"{key} independent re-derivation diverged")
            entry = FRESHNESS_MANIFEST[key]
            self.assertEqual(entry["label"], indep_label, f"{key} manifest label drifted")
            self.assertEqual(
                entry["date"], indep_date.isoformat() if indep_date else None,
                f"{key} manifest date drifted",
            )

    def test_snapshot_revision_no_future_inflation(self):
        """Proves date selection is anchored, not max-over-tail: a synthetic
        example/future date in tail prose must not inflate local_date to
        false-FRESH. Both the fixed parser and the independent re-derivation
        ignore mid-line prose dates."""
        base = "Title\nRev 18\n\nRevision Log\n\n15-11-2021 some change\n"
        tail = base + "Some example: next maintenance 15-11-2030 future note\n"
        label, date = snapshot_revision_info(tail)
        self.assertEqual(label, "Rev 18")
        self.assertEqual(date, datetime.date(2021, 11, 15))
        indep_label, indep_date = independent_revision_info(tail)
        self.assertEqual((indep_label, indep_date), (label, date))

    def test_text_unknown_units_empty(self):
        """The snapshot's Units column vocabulary was enumerated directly
        from tests/specs/VE.Direct-Protocol-3.34.txt (leading tokens:
        mV/mA/mAh/W/VA/1W/0.01 kWh/0.01 V/0.1 A/°C/‰/Minutes/Seconds plus
        footnote-glued and bare-footnote forms) and is fully covered by
        _TEXT_UNITS_RE — Hz/kWh are absent from the snapshot, so no
        vocabulary addition is due. A future unit (e.g. Hz/kWh) would fall
        through to Units="" with unit text leaking into the description
        while the `glued` invariant and spot-checks stay green; the
        parser therefore records such candidates and this test fails
        loudly until the unit is explicitly allowlisted with
        justification."""
        self.assertEqual(
            self.specs["text"].unknown_units, [],
            f"unknown Units cells (allowlist with justification or fix vocabulary): "
            f"{self.specs['text'].unknown_units}",
        )

    def test_text_unknown_unit_recorded_loudly(self):
        """Synthetic future-unit row: the parser must record ("Hz") rather
        than silently leaking it into the description."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             HZz                 Hz                       Some new quantity\n"
            "             V                   mV                       Main voltage\n"
            "Table I Supported Text-mode fields\n"
        )
        parser = VEDirectTextParser(section)
        fields = parser.parse_fields()
        self.assertEqual(parser.unknown_units, [("HZz", "Hz")])
        self.assertEqual(fields["HZz"].units, "")
        # Control: known units never record.
        self.assertEqual(fields["V"].units, "mV")

    def test_text_unknown_multiword_unit_recorded_loudly(self):
        """Synthetic multi-word future unit: the old single-token shape
        check let "kilo watt hours" leak into the description with
        unknown_units==[]; the widened gate records the full pre-gap
        candidate instead. Over-long control: a long description leaking
        through an accidental internal double space is now RECORDED (loud,
        flagged for human review) instead of silently dropped by the old
        32-char cap — over-scraping fails loudly via the invented-IDs-style
        unknown-units check, which is the safe direction."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             KWz                 kilo watt hours          Some new quantity\n"
            "Table I Supported Text-mode fields\n"
        )
        parser = VEDirectTextParser(section)
        fields = parser.parse_fields()
        self.assertEqual(parser.unknown_units, [("KWz", "kilo watt hours")])
        self.assertEqual(fields["KWz"].units, "")
        long_desc = (
            "The units used by the serial interface are as follows:\n"
            "             LDz                 This is a very long description with  double spaces inside\n"
            "Table I Supported Text-mode fields\n"
        )
        parser2 = VEDirectTextParser(long_desc)
        parser2.parse_fields()
        # Loud recording: over-long pre-gap candidate is flagged for review,
        # never silently dropped (old cap hid real >32-char units).
        self.assertEqual(
            parser2.unknown_units, [("LDz", "This is a very long description with")]
        )

    def test_text_unknown_single_space_unit_recorded(self):
        """Regression for the single-space gate: a future unit separated from
        its description by ONE space ("Hz Some new quantity", layout collapse)
        never split on 2+ spaces, so the old gate leaked it into the
        description with unknown_units==[] and suite green. The fallback
        records the first token (denylisting known description starters on
        genuine bullet-tailed device rows to stay exact on snapshots).
        Control: a real empty-Units description ("Alarm reason" with its
        device bullet, as every snapshot row carries) stays unrecorded."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             QZz                 Hz Some new quantity\n"
            "Table I Supported Text-mode fields\n"
        )
        parser = VEDirectTextParser(section)
        fields = parser.parse_fields()
        self.assertEqual(parser.unknown_units, [("QZz", "Hz")])
        self.assertEqual(fields["QZz"].units, "")
        # Control: known description starter on a genuine bullet-tailed
        # device row never records (denylisted). Bullet-tailed because every
        # snapshot empty-Units row carries device bullets (the devices test
        # asserts no field parses without devices); the bullet-less
        # denylisted-starter shape records loudly instead (see
        # test_text_unknown_single_space_denylisted_starter_recorded).
        ctrl = (
            "The units used by the serial interface are as follows:\n"
            "             AR                  Alarm reason                   •\n"
            "Table I Supported Text-mode fields\n"
        )
        parser2 = VEDirectTextParser(ctrl)
        parser2.parse_fields()
        self.assertEqual(parser2.unknown_units, [])

    def test_text_unknown_single_space_denylisted_starter_recorded(self):
        """Regression for the denylisted-starter hole: a bullet-less
        single-space remainder starting with a denylisted description word
        ("Alarm Some new quantity") leaked into the description with
        unknown_units==[] and suite green, while the two-space gate
        ("Alarm  Some new quantity") records ("Alarm"). The denylist now
        excuses only bullet-tailed (genuine device) rows — every snapshot
        empty-Units row carries device bullets (devices never empty),
        while the bullet-less H1/H4/H5/H10 footnote rows early-return
        before the fallback — so the bullet-less starter records loudly.
        Control: the bullet-tailed genuine row stays unrecorded (zero-diff
        on snapshots, pinned by test_text_unknown_units_empty)."""
        from spec_parser import _split_text_units_desc

        sink: list = []
        units, desc = _split_text_units_desc(
            "Alarm Some new quantity", label="QZz", unknown_sink=sink
        )
        self.assertEqual(units, "")
        self.assertEqual(sink, [("QZz", "Alarm")])
        # Two-space gate symmetry (already recorded before the fix).
        sink2: list = []
        _split_text_units_desc(
            "Alarm  Some new quantity", label="QZz", unknown_sink=sink2
        )
        self.assertEqual(sink2, [("QZz", "Alarm")])
        # Control: bullet-tailed genuine device row stays denylisted.
        sink3: list = []
        units3, _ = _split_text_units_desc(
            "Alarm reason                   •", label="AR", unknown_sink=sink3
        )
        self.assertEqual(units3, "")
        self.assertEqual(sink3, [])

    def test_text_unknown_single_space_digit_leading_unit_recorded(self):
        """Regression for the digit-leading hole: a single-space remainder
        starting with a digit ("1 W Some power row") failed the letter
        check on its first token ("1") with unknown_units==[] and suite
        green, while the two-space gate ("1 W  Some power row") records
        ("1 W"). The fallback now combines a leading numeric token with
        the next one ("1 W", the spaced variant of the "1W" unit),
        mirroring the two-space full pre-gap candidate. Control: snapshot
        fallback inputs never start with a digit (descriptions never do;
        bare footnotes early-return), so zero-diff holds (pinned by
        test_text_unknown_units_empty plus the 63-field full map)."""
        from spec_parser import _split_text_units_desc

        sink: list = []
        units, desc = _split_text_units_desc(
            "1 W Some power row", label="QZz", unknown_sink=sink
        )
        self.assertEqual(units, "")
        self.assertEqual(desc, "1 W Some power row")
        self.assertEqual(sink, [("QZz", "1 W")])
        # Two-space gate symmetry (already recorded before the fix).
        sink2: list = []
        _split_text_units_desc(
            "1 W  Some power row", label="QZz", unknown_sink=sink2
        )
        self.assertEqual(sink2, [("QZz", "1 W")])
        # Zero-diff: no snapshot remainder that actually reaches the
        # single-space fallback starts with a digit (bare footnotes like
        # H4 "11" early-return; vocab rows like "0.01 kWh ..." match the
        # units vocabulary; two-space remainders split on the column gap).
        from spec_parser import _TEXT_FOOTNOTE_RE, _TEXT_UNITS_RE

        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        end = text.find("Table I Supported Text-mode fields")
        import re as _re

        label_re = _re.compile(r"^\s{8,32}([A-Za-z0-9_#.\-]+)\s{2,}(.*?)\s*$")
        stoplist = {
            "Label", "Table", "The", "Note", "Victron", "General", "E-mail",
            "When", "Both", "Available", "60x",
        }
        digit_first = []
        for line in text[start:end].splitlines():
            m = label_re.match(line)
            if not m or m.group(1) in stoplist:
                continue
            rest = m.group(2)
            if _TEXT_UNITS_RE.match(rest):
                continue
            if _re.fullmatch(r"\d[\d\s,]*", rest.strip()):
                continue
            head = rest.split("•", 1)[0]
            if len(_re.split(r"\s{2,}", head.strip(), maxsplit=1)) == 2:
                continue
            toks = head.strip().split()
            if toks and toks[0][:1].isdigit():
                digit_first.append((m.group(1), rest))
        self.assertEqual(digit_first, [])

    def test_text_unknown_overlong_unit_recorded(self):
        """Regression for the 32-char silent drop: a future unit longer than
        32 chars was silently ignored (unknown_units==[]) while leaking into
        the description. Loud recording replaces the silent ignore — the full
        candidate is recorded for human review."""
        long_unit = "supercalifragilisticexpialidocious_unit_xyz"
        self.assertGreater(len(long_unit), 32)
        section = (
            "The units used by the serial interface are as follows:\n"
            f"             QZz                 {long_unit}          Some new quantity\n"
            "Table I Supported Text-mode fields\n"
        )
        parser = VEDirectTextParser(section)
        fields = parser.parse_fields()
        self.assertEqual(parser.unknown_units, [("QZz", long_unit)])
        self.assertEqual(fields["QZz"].units, "")

    def test_text_field_counter_gap_divergence(self):
        """Proves the independent counter is truly gap-permissive: a
        synthetic single-space label row is counted by
        count_doc_field_labels yet missed by the parser's ``\\s{2,}``
        rule, i.e. it would fail loudly as a parser-vs-counter mismatch
        instead of passing ``len==63`` on both sides."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             V                   mV                       Main voltage\n"
            "             QZ single-space description row\n"
            "Table I Supported Text-mode fields\n"
        )
        self.assertIn("QZ", count_doc_field_labels(section))
        self.assertNotIn("QZ", VEDirectTextParser(section).parse_fields())

    def test_text_field_counter_stoplist_divergence(self):
        """Proves the counter does not share the parser's exact stoplist: a
        synthetic label colliding with a parser stoplist word ("Note") is
        counted by count_doc_field_labels yet skipped by the parser, i.e.
        it would fail loudly as a parser-vs-counter mismatch instead of
        being missed by both."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             V                   mV                       Main voltage\n"
            "             Note                mV                       Some future note field\n"
            "Table I Supported Text-mode fields\n"
        )
        self.assertIn("Note", count_doc_field_labels(section))
        self.assertNotIn("Note", VEDirectTextParser(section).parse_fields())

    def test_text_field_label_charset_hyphen_dot(self):
        """Shared label charset hid hyphen/dotted future labels on both
        sides (probe ``DC-IN``/``V.PV`` yielded parser ['V'] and counter
        ['V'], counts staying green). Scraper is over-inclusive (hyphen/dot
        in the class) and the parser records such labels too, so the probe
        now passes honestly on both sides. Control: no hyphen/dot label in
        current snapshots (zero-diff, 63 fields stay exact)."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             V                   mV                       Main voltage\n"
            "             DC-IN               mV                       Future hyphen label\n"
            "             V.PV                mV                       Future dotted label\n"
            "Table I Supported Text-mode fields\n"
        )
        # Over-inclusive scraper catches hyphen/dotted (was missed before).
        labels = count_doc_field_labels(section)
        self.assertIn("DC-IN", labels)
        self.assertIn("V.PV", labels)
        # Parser records them too (honest pass, not divergence): future
        # hyphen/dotted rows parse instead of staying green-but-missed.
        fields = VEDirectTextParser(section).parse_fields()
        self.assertIn("DC-IN", fields)
        self.assertIn("V.PV", fields)
        self.assertEqual(fields["DC-IN"].units, "mV")
        self.assertEqual(fields["V.PV"].units, "mV")
        # Zero-diff control: snapshots carry no hyphen/dot label, so the
        # widened class adds no hits (63 stay exact on both sides).
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        end = text.find("Table I Supported Text-mode fields")
        snap_labels = count_doc_field_labels(text[start:end])
        self.assertEqual(len(snap_labels), EXPECTED_COUNTS["fields"])
        self.assertFalse(any("-" in lb or "." in lb for lb in snap_labels))
        self.assertFalse(any("-" in lb or "." in lb for lb in self.text_fields))

    def test_text_field_label_leading_plus(self):
        """Leading-``+`` text labels ("+QZ mV Future") were missed by BOTH
        the counter and the parser (``[A-Za-z0-9_#.-]`` required) while 63
        stayed green. Both now lead ``[+A-Za-z0-9_#.-]`` (other symbols
        verified absent as label starts in snapshots, zero-diff), so the
        probe passes honestly with exact label/units on both sides.
        Zero-diff: no snapshot label starts with "+" (verified, 63 exact).
        Divergence probe with a leading-plus label."""
        section = (
            "The units used by the serial interface are as follows:\n"
            "             V                   mV                       Main voltage\n"
            "             +QZ                 mV                       Future quantity\n"
            "Table I Supported Text-mode fields\n"
        )
        self.assertIn("+QZ", count_doc_field_labels(section))
        fields = VEDirectTextParser(section).parse_fields()
        self.assertIn("+QZ", fields)
        self.assertEqual(fields["+QZ"].units, "mV")
        self.assertEqual(fields["+QZ"].description, "Future quantity")
        # Zero-diff controls: snapshots carry no plus label (63 exact).
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        end = text.find("Table I Supported Text-mode fields")
        snap_labels = count_doc_field_labels(text[start:end])
        self.assertEqual(len(snap_labels), EXPECTED_COUNTS["fields"])
        self.assertFalse(any(lb.startswith("+") for lb in snap_labels))
        self.assertFalse(any(lb.startswith("+") for lb in self.text_fields))

    def test_text_field_label_symbol_charset(self):
        """Shared closed label charset hid symbol labels on both sides
        (probes ``V:PV`` / ``DC/IN`` / ``*QZ`` yielded counter set() and
        parser {} while 63 stayed green — reproduced synthetically before
        the fix). Both sides now take the label token structurally (any
        non-space run, no charset restriction), so the probes pass honestly
        with exact label/units/descriptions on both sides; neither side can
        share a charset blind spot by construction. Zero-diff: no snapshot
        label contains ``:``/``/``/``*`` (verified, 63 exact on both
        sides)."""
        base = "The units used by the serial interface are as follows:\n"
        end = "Table I Supported Text-mode fields\n"
        for label in ("V:PV", "DC/IN", "*QZ"):
            with self.subTest(label=label):
                section = (
                    base
                    + "             V                   mV                       Main voltage\n"
                    + f"             {label}                 mV                       Future quantity\n"
                    + end
                )
                self.assertIn(label, count_doc_field_labels(section))
                fields = VEDirectTextParser(section).parse_fields()
                self.assertIn(label, fields)
                self.assertEqual(fields[label].units, "mV")
                self.assertEqual(fields[label].description, "Future quantity")
        # Zero-diff controls: snapshots carry no symbol labels (63 exact).
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        snap_labels = count_doc_field_labels(text[start:text.find("Table I Supported Text-mode fields")])
        self.assertEqual(len(snap_labels), EXPECTED_COUNTS["fields"])
        self.assertFalse(any(set(lb) & set(":/*") for lb in snap_labels))
        self.assertFalse(any(set(lb) & set(":/*") for lb in self.text_fields))

    def test_text_field_label_indent_off_column(self):
        """Structural indent on both sides (cycle-14 fix): the shared
        narrow indent window hid off-column labels on both sides (probe
        ``QZz mV Future`` at indent 8 and 30 missed by counter 10-20 and
        parser 10-25, control 13 caught, counts stayed green). Both sides
        now accept ANY indent (position-independent; furniture excluded by
        structure/content, not columns), so drift to 5-7/33-35 — and to
        <=4/>=36, missed by both before — passes honestly with exact
        label/units on both sides. Remaining divergence axes (proving the
        counter/parser pair is still independent): single-space rows
        (test_text_field_counter_gap_divergence) and stoplist-colliding
        labels (test_text_field_counter_stoplist_divergence) — indent and
        charset no longer diverge, gap/stoplist still do."""
        def _row(indent: int, label: str = "QZz") -> str:
            return " " * indent + label + " " * 17 + "mV" + " " * 23 + "Future quantity\n"

        base = "The units used by the serial interface are as follows:\n"
        end = "Table I Supported Text-mode fields\n"
        # Honest pass at every indent: control 13, former 8/30 pair, former
        # 5/35 divergence pair, and extreme <=4/>=36 (missed by both before).
        for indent in (0, 2, 4, 5, 7, 8, 13, 30, 33, 35, 36, 50, 70):
            with self.subTest(indent=indent):
                section = base + _row(indent) + end
                self.assertIn("QZz", count_doc_field_labels(section))
                fields = VEDirectTextParser(section).parse_fields()
                self.assertIn("QZz", fields)
                self.assertEqual(fields["QZz"].units, "mV")
        # Zero-diff control: snapshots carry only column-13 labels.
        # Note: footnote prose starting with a real label word ("MPPT
        # 75/15 ..." at indent 26, single-space gap) also matches the
        # counter's permissive ``\\s+`` gap but adds no NEW distinct label
        # (MPPT already counted at 13), so distinct-level stays exact at
        # 63; line-level would show {13, 26}. Assert distinct-level plus
        # that every snapshot label has a true label row at 13.
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        snap_section = text[start:text.find("Table I Supported Text-mode fields")]
        snap_labels = count_doc_field_labels(snap_section)
        self.assertEqual(len(snap_labels), EXPECTED_COUNTS["fields"])
        for label in snap_labels:
            with self.subTest(label=label):
                self.assertTrue(
                    any(
                        re.match(rf"^ {{13}}{re.escape(label)}\s+", line)
                        for line in snap_section.splitlines()
                    ),
                    f"{label} has no column-13 label row",
                )
        self.assertEqual(set(self.text_fields), snap_labels)

    def test_text_device_bullet_overflow_fails_loudly(self):
        """A bullet beyond the last known device column (synthetic 8th
        column) must raise instead of silently merging into BuckBoost.
        Control: a bullet under BuckBoost parses to BuckBoost."""
        header_prev = " " * 80 + "MPPT"
        header = (
            " " * 50 + "60x" + " " * 7 + "70x" + " " * 5 + "SmartShunt"
            + " " * 12 + "Inverter" + " " * 7 + "Charger" + " " * 6 + "BuckBoost"
        )
        last_start = header.find("BuckBoost")
        self.assertGreater(last_start, 0)
        row = "             V                   mV                       Main voltage"
        # Control bullet inside the BuckBoost column.
        ok_section = (
            "The units used by the serial interface are as follows:\n"
            f"{header_prev}\n{header}\n{row}{' ' * (last_start - len(row) + 2)}•\n"
            "Table I Supported Text-mode fields\n"
        )
        self.assertEqual(
            VEDirectTextParser(ok_section).parse_fields()["V"].devices,
            ["BuckBoost"],
        )
        # 8th-column bullet far right of every known column.
        bad_row = row + " " * (last_start + 30 - len(row)) + "•"
        bad_section = (
            "The units used by the serial interface are as follows:\n"
            f"{header_prev}\n{header}\n{bad_row}\n"
            "Table I Supported Text-mode fields\n"
        )
        with self.assertRaises(ValueError):
            VEDirectTextParser(bad_section).parse_fields()

    def _continuation_section(self, indent: int) -> str:
        pad = " " * indent
        return (
            "The units used by the serial interface are as follows:\n"
            "             H1                  mAh    11\n"
            f"{pad}Depth of the deepest discharge\n"
            "             H4                  11\n"
            f"{pad}Number of charge cycles\n"
            "             H17                 0.01 kWh                 Amount of discharged energy (BMV) /\n"
            f"{pad}Amount of produced energy (DC monitor)\n"
            "Table I Supported Text-mode fields\n"
        )

    def test_text_continuation_indent_shift_resilient(self):
        """Regression: a `pdftotext -layout` column drift that moves the
        description column left must not silently drop continuation joins.
        The doc prints continuations at indent 58; the old hard
        `indent >= 40` floor dropped every H1/H4/H5/H10/H17/H18 join after
        a -20 shift (58 -> 38) while device bullets (indent-free scan)
        kept parsing. Both layouts below must join identically."""
        expected = {
            "H1": "Depth of the deepest discharge",
            "H4": "Number of charge cycles",
            "H17": "Amount of discharged energy (BMV) / Amount of produced energy (DC monitor)",
        }
        for indent in (58, 38):
            with self.subTest(indent=indent):
                fields = VEDirectTextParser(self._continuation_section(indent)).parse_fields()
                for label, desc in expected.items():
                    self.assertEqual(fields[label].description, desc)

    def test_is_desc_continuation_relative_rule(self):
        """Unit-level pins for the relative-indent rule (label column 13):
        footnote prose (25-27) and furniture never qualify, continuations
        qualify across a ±15 drift band."""
        self.assertFalse(_is_desc_continuation(" " * 26 + "Available on Phoenix Smart Chargers"))
        self.assertFalse(_is_desc_continuation(" " * 27 + "When the battery is not discharging"))
        self.assertFalse(_is_desc_continuation("Victron Energy BlueSolar HEX protocol"))
        self.assertFalse(_is_desc_continuation(" " * 58 + "60x 70x SmartShunt header"))
        self.assertFalse(_is_desc_continuation(""))
        for indent in (33, 38, 43, 58, 63):
            with self.subTest(indent=indent):
                self.assertTrue(_is_desc_continuation(" " * indent + "Depth of the deepest discharge"))

    def test_text_continuation_excluded_word_rescue(self):
        """Proves exclusions are anchored, not substring: synthetic genuine
        continuations containing excluded words join correctly, while exact
        furniture still excluded. The old ``x in line`` rejected
        "Labelled ..." (contains "Label"), "Depth for 60x ..." (contains
        "60x"), or "Made by Victron Energy ..." (contains "Victron Energy")
        at indent 58, yielding empty description with unknown_units==[] and
        suite green. Anchored patterns (same treatment as the skip-filter
        fix) let mid-line/renamed mentions through."""
        # Unit-level: genuine continuations containing the words qualify.
        for cont in (
            "Labelled depth detail",
            "Depth for 60x models detail",
            "Made by Victron Energy detail",
            "Victron Energy detail",
            "General phone handling detail",
            "See www.victronenergy.com for details",
        ):
            with self.subTest(cont=cont):
                self.assertTrue(_is_desc_continuation(" " * 58 + cont))
        # Unit-level controls: exact furniture still excluded.
        self.assertFalse(_is_desc_continuation(" " * 58 + "60x 70x SmartShunt header"))
        self.assertFalse(_is_desc_continuation(" " * 58 + "www.victronenergy.com"))
        self.assertFalse(_is_desc_continuation(" " * 58 + "Label Units Description"))
        self.assertFalse(_is_desc_continuation("Victron Energy B.V. | De Paal 35 | 1351 JG Almere | The Netherlands"))
        # Integration: H1 continuation with an excluded word joins (not empty).
        for cont in ("Labelled depth detail", "Depth for 60x models detail"):
            with self.subTest(integration=cont):
                section = (
                    "The units used by the serial interface are as follows:\n"
                    "             H1                  mAh    11\n"
                    f"{' ' * 58}{cont}\n"
                    "Table I Supported Text-mode fields\n"
                )
                parser = VEDirectTextParser(section)
                fields = parser.parse_fields()
                self.assertEqual(fields["H1"].description, cont)
                self.assertEqual(parser.unknown_units, [])

    def test_text_protocol_spec(self):
        """Verify all parsed Text mode fields and product IDs from Text Protocol 3.34 exist in code."""
        missing_fields = [f.label for f in self.text_fields.values() if f.label not in T.LABEL_INFO]
        self.assertEqual(missing_fields, [], f"Parsed text fields missing from LABEL_INFO: {missing_fields}")

        missing_pids = [f"0x{pid:04X} ({p.model_name})" for pid, p in self.pids.items() if pid not in T.PID_MAP]
        self.assertEqual(missing_pids, [], f"Parsed PIDs missing from PID_MAP: {missing_pids}")

        # Doc totals against the snapshot (not just parser-vs-code); the
        # counter is count_doc_field_labels above (independent shape, see
        # its justification).
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        end = text.find("Table I Supported Text-mode fields")
        section = text[start:end]
        doc_fields = count_doc_field_labels(section)
        self.assertEqual(len(doc_fields), EXPECTED_COUNTS["fields"])
        self.assertEqual(set(self.text_fields), doc_fields)

        p0 = text.find("The product id:")
        p1 = text.find("These models are phased out.")
        doc_pid_rows = [
            line for line in text[p0:p1].splitlines()
            if re.search(r"0x[0-9A-Fa-f]{3,4}\*?\s*$", line)
        ]
        self.assertEqual(len(doc_pid_rows), EXPECTED_COUNTS["pids"])
        self.assertEqual(len(self.pids), EXPECTED_COUNTS["pids"])
        # Set-equality, not just count-equality (the fields check above
        # already does this — same pattern): a compensating miss+spurious
        # pair keeps 153 == 153 green but breaks the set comparison.
        self.assertEqual(set(self.pids), scrape_doc_pid_set(text))

        # All 16 "These models are phased out" *-marked rows (verified against
        # the snapshot PID table: 0x0300 + 6 MPPT + 9 Phoenix Inverter rows)
        # must parse as phased_out.
        for pid in (
            0x0300,
            0xA040, 0xA041, 0xA044, 0xA045, 0xA051, 0xA052,
            0xA201, 0xA202, 0xA204,
            0xA211, 0xA212, 0xA214,
            0xA221, 0xA222, 0xA224,
        ):
            self.assertIn(pid, self.pids, f"0x{pid:04X} missing from parsed PIDs")
            self.assertTrue(self.pids[pid].phased_out, f"0x{pid:04X} must be flagged phased-out")

        # Exact PID name comparison for all 153 doc PIDs (incl. phased-out
        # flagged rows): ID presence/count/set-equality stay green under a
        # doc rename (probe 0x0203 BMV-700->BMV-700R keeps the 153-set and
        # counts), so every doc model_name is compared against the code
        # display name. No silent normalization: doc and code names match
        # exactly today (verified zero-diff, no mapping needed), so a future
        # purely presentational difference (case/punctuation/spacing only)
        # must add a comment-justified entry to a mapping table here — never
        # a bare lower/strip. A real DB-vs-doc mismatch stays asserting DOC
        # truth here; report it as src/ve_direct/tables.py:195 (PID_MAP,
        # src/ is read-only).
        name_mismatches = [
            f"0x{pid:04X}: doc {p.model_name!r} vs code {T.PID_MAP.get(pid)!r}"
            for pid, p in self.pids.items()
            if T.PID_MAP.get(pid) != p.model_name
        ]
        self.assertEqual(name_mismatches, [], f"PID name mismatches (DOC truth, see src/ve_direct/tables.py:195): {name_mismatches}")

    def test_text_field_devices(self):
        """Device columns must be parsed (never empty) and spot-check against the doc."""
        empty = [label for label, f in self.text_fields.items() if not f.devices]
        self.assertEqual(empty, [], f"Fields with no devices parsed: {empty}")

        all7 = {"BMV60x", "BMV70x", "BMV71x", "MPPT", "PhoenixInverter", "PhoenixCharger", "BuckBoost"}
        self.assertEqual(set(self.text_fields["V"].devices), all7)
        # Footnote 2: triple-output Phoenix Smart Chargers only.
        for label in ("V2", "V3", "I2", "I3"):
            self.assertEqual(self.text_fields[label].devices, ["PhoenixCharger"], label)
        # MPPT-only rows.
        for label in ("VPV", "PPV", "IL", "LOAD", "HSDS", "MPPT"):
            self.assertEqual(self.text_fields[label].devices, ["MPPT"], label)
        # Inverter-only rows.
        for label in ("WARN", "AC_OUT_V", "AC_OUT_I", "AC_OUT_S"):
            self.assertEqual(self.text_fields[label].devices, ["PhoenixInverter"], label)
        # DC-monitor / BuckBoost rows.
        self.assertEqual(self.text_fields["MON"].devices, ["BMV71x"])
        for label in ("DC_IN_V", "DC_IN_I", "DC_IN_P"):
            self.assertEqual(self.text_fields[label].devices, ["BuckBoost"], label)

    def test_text_field_units_and_descriptions(self):
        """Units/descriptions for ALL 63 text fields (doc truth from the
        Text 3.34 fields tables, not parser output: "Consumed" keeps the
        doc's own typo). Full-map equality: every parsed field is asserted
        here, so a doc change on any field (probe ``V2 mV->mA`` keeps count
        63, unknown gate empty, label sets equal) fails as a units/description
        mismatch instead of staying green on the old ~30-field spot-check."""
        expected = {
            # Footnote glued into the Units cell ("mAh 10,11", "‰10,11",
            # "Minutes10, 11,12").
            "CE": ("mAh", "Consumed Amp Hours"),
            "SOC": ("‰", "State-of-charge"),
            "TTG": ("Minutes", "Time-to-go"),
            # Footnote glued without space ("mAh11", "mV3", "Seconds11").
            "H2": ("mAh", "Depth of the last discharge"),
            "H3": ("mAh", "Depth of the average discharge"),
            "H6": ("mAh", "Cumulative Amp Hours drawn"),
            "H9": ("Seconds", "Number of seconds since last full charge"),
            "H15": ("mV", "Minimum auxiliary (battery) voltage"),
            "H16": ("mV", "Maximum auxiliary (battery) voltage"),
            # Description on the continuation line (bare "11" footnote or
            # "mAh 11" units+footnote row).
            "H1": ("mAh", "Depth of the deepest discharge"),
            "H4": ("", "Number of charge cycles"),
            "H5": ("", "Number of full discharges"),
            "H10": ("", "Number of automatic synchronizations"),
            # Split "...(BMV) /" description joined across two lines.
            "H17": ("0.01 kWh", "Amount of discharged energy (BMV) / Amount of produced energy (DC monitor)"),
            "H18": ("0.01 kWh", "Amount of charged energy (BMV) / Amount of consumed energy (DC monitor)"),
            # Empty Units column: description leaked into units before.
            "AR": ("", "Alarm reason"),
            "Alarm": ("", "Alarm condition active"),
            "Relay": ("", "Relay state"),
            "OR": ("", "Off reason"),
            "ERR": ("", "Error code"),
            "CS": ("", "State of operation"),
            "LOAD": ("", "Load output state (ON/OFF)"),
            "HSDS": ("", "Day sequence number (0..364)"),
            "H11": ("", "Number of low main voltage alarms"),
            # Non-ASCII units previously dropped to empty/footnote-only.
            "T": ("°C", "Battery temperature"),
            "DM": ("‰", "Mid-point deviation of the battery bank"),
            "V": ("mV", "Main or channel 1 (battery) voltage"),
            # Remaining 36 fields (full-map extension): doc truth verified
            # against the snapshot label rows (units + description columns,
            # footnotes/bullets stripped). Any doc edit here (e.g. V2 unit
            # or description wording) fails below as a full-map mismatch.
            "V2": ("mV", "Channel 2 (battery) voltage"),
            "V3": ("mV", "Channel 3 (battery) voltage"),
            "VS": ("mV", "Auxiliary (starter) voltage"),
            "VM": ("mV", "Mid-point voltage of the battery bank"),
            "VPV": ("mV", "Panel voltage"),
            "I": ("mA", "Main or channel 1 battery current"),
            "I2": ("mA", "Channel 2 battery current"),
            "I3": ("mA", "Channel 3 battery current"),
            "IL": ("mA", "Load current"),
            "P": ("W", "Instantaneous power"),
            "PPV": ("W", "Panel power"),
            "H7": ("mV", "Minimum main (battery) voltage"),
            "H8": ("mV", "Maximum main (battery) voltage"),
            "H12": ("", "Number of high main voltage alarms"),
            "H13": ("", "Number of low auxiliary voltage alarms"),
            "H14": ("", "Number of high auxiliary voltage alarms"),
            "H19": ("0.01 kWh", "Yield total (user resettable counter)"),
            "H20": ("0.01 kWh", "Yield today"),
            "H21": ("W", "Maximum power today"),
            "H22": ("0.01 kWh", "Yield yesterday"),
            "H23": ("W", "Maximum power yesterday"),
            "AC_OUT_V": ("0.01 V", "AC output voltage"),
            "AC_OUT_I": ("0.1 A", "AC output current"),
            "AC_OUT_S": ("VA", "AC output apparent power"),
            "DC_IN_V": ("0.01 V", "DC input voltage"),
            "DC_IN_I": ("0.1 A", "DC input current"),
            "DC_IN_P": ("1W", "DC input power"),
            "BMV": ("", "Model description (deprecated)"),
            "FW": ("", "Firmware version (16 bit)"),
            "FWE": ("", "Firmware version (24 bit)"),
            "PID": ("", "Product ID"),
            "SER#": ("", "Serial number"),
            "MODE": ("", "Device mode"),
            "MON": ("", "DC monitor mode"),
            "MPPT": ("", "Tracker operation mode"),
            "WARN": ("", "Warning reason"),
        }
        # Full-map equality: expected covers every parsed field (63) so no
        # field change can hide outside the asserted subset.
        self.assertEqual(set(expected), set(self.text_fields))
        self.assertEqual(len(expected), EXPECTED_COUNTS["fields"])
        for label, (units, desc) in expected.items():
            with self.subTest(label=label):
                self.assertIn(label, self.text_fields)
                self.assertEqual(self.text_fields[label].units, units)
                self.assertEqual(self.text_fields[label].description, desc)
        # Device bullets belong to the device columns, never to text.
        leaked = [label for label, f in self.text_fields.items() if "•" in f.description]
        self.assertEqual(leaked, [], f"Descriptions leaking device bullets: {leaked}")
        # Footnote digits must not glue onto units ("mAh11", "mV3",
        # "mAh 10,11"): legit digit-bearing units ("0.01 kWh", "0.1 A",
        # "1W") never have a letter directly followed by a digit or a comma.
        glued = [label for label, f in self.text_fields.items()
                 if re.search(r",|[A-Za-z‰°]\d", f.units)]
        self.assertEqual(glued, [], f"Units with glued footnotes: {glued}")

    def test_cs_states_full_coverage(self):
        """parse_cs_states must cover the whole section incl. 11/245-248/252."""
        states = self.specs["text"].parse_cs_states()
        self.assertEqual(states, EXPECTED_CS)
        # Independent doc-side counter (prefix-only strategy, no bullet-tail
        # requirement): a bullet-less row missed by the parser still counts
        # here, so parser-vs-counter set-equality fails loudly. The scraper
        # bound is WIDER/DIFFERENT than the parser's tight "^ERR$" header
        # (which would hide a past-bound row on both sides): the scraper
        # scans to the next section's prose ("The error code ...", ~51 chars
        # past ERR) while the parser stops at "^ERR$". Justified to stay
        # exact: the extra ERR header + blank carry no ``name code`` pattern
        # (verified zero-diff on snapshots); the bound-divergence test proves
        # a row past the parser bound is scraped-but-unparsed.
        text = self.raw_texts["text"]
        pos = text.find("The state of operation. See the table below")
        tail = text[pos:]
        m_parser = re.search(r"^\s*ERR\s*$", tail, re.MULTILINE)
        m_wide = re.search(r"The error code of the device", tail)
        self.assertIsNotNone(m_parser)
        self.assertIsNotNone(m_wide)
        self.assertGreater(m_wide.start(), m_parser.start())
        doc_states = scrape_doc_cs_codes(tail[: m_wide.start()])
        self.assertEqual(set(doc_states), set(states))
        self.assertEqual(doc_states, states)

    def test_cs_counter_bullet_gap_divergence(self):
        """Proves the CS counter is truly tail-independent: a synthetic row
        without trailing bullets is counted by scrape_doc_cs_codes yet
        missed by the parser's ``\\s+(•|\\d)`` tail rule, i.e. it would fail
        loudly as a counter-vs-parser mismatch instead of staying green."""
        section = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        Bulletless                            99\n"
        )
        self.assertIn(99, scrape_doc_cs_codes(section))
        self.assertNotIn(99, VEDirectTextParser(section).parse_cs_states())

    def test_cs_scraper_name_divergence(self):
        """Structural names on both sides (cycle-14 fix, was pinned
        divergence): synthetic interior-symbol names (``Bulk+Boost``,
        ``Charge:Fast``) and interior-digit names (``Mode2Fast``) are now
        scraped AND parsed with exact names (honest pass) — the shared
        closed name class that hid them on both sides while hardcoded
        EXPECTED_CS stayed green is gone: names are everything before the
        trailing code on both sides, so neither side can share a charset
        blind spot by construction. Set-equality still fails loudly if
        either side regresses. Remaining divergence axes (proving the
        scraper/parser pair is still independent): bullet-less rows
        (test_cs_counter_bullet_gap_divergence), past-bound rows
        (test_cs_bound_divergence), and live-vs-layout tolerant extraction.
        Zero-diff: snapshot names all start with a letter (pinned by
        test_cs_states_full_coverage)."""
        section = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        Bulk+Boost                            99     •\n"
            "                        Charge:Fast                           100    •\n"
            "                        Mode2Fast                             101    •\n"
        )
        scraped = scrape_doc_cs_codes(section)
        self.assertEqual(scraped[99], "Bulk+Boost")
        self.assertEqual(scraped[100], "Charge:Fast")
        self.assertEqual(scraped[101], "Mode2Fast")
        parsed = VEDirectTextParser(section).parse_cs_states()
        self.assertEqual(parsed[99], "Bulk+Boost")
        self.assertEqual(parsed[100], "Charge:Fast")
        self.assertEqual(parsed[101], "Mode2Fast")

    def test_cs_leading_digit_name_rescue_divergence(self):
        """Leading-digit CS names ("2Fast Mode 99 •") were missed by BOTH
        the parser and the scraper (leading ``[A-Za-z]`` required) while
        EXPECTED_CS and the set-equality stayed green. Both now take the
        name structurally (everything before the trailing code), so the
        probe passes honestly with its true name on both sides — like
        interior symbols/digits (see test_cs_scraper_name_divergence),
        which are honest-pass too since cycle-14. Controls: the
        "(1) Load search" footnote (leading "(" furniture) stays excluded
        on both sides via the explicit paren denylist; snapshot names all
        start with a letter, so zero-diff holds (pinned by
        test_cs_states_full_coverage).
        Note: fetch_specs.tolerant_cs_states now recovers this probe exact
        too (see test_tolerant_cs_leading_digit_exact), so snapshot-vs-live
        tolerant agreement carries the true name."""
        section = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        2Fast Mode                            99     •\n"
        )
        scraped = scrape_doc_cs_codes(section)
        self.assertEqual(scraped[99], "2Fast Mode")
        parsed = VEDirectTextParser(section).parse_cs_states()
        self.assertEqual(parsed[99], "2Fast Mode")
        # Controls: a footnote WITH a code ("(1) Load search 98 •",
        # snapshot footnote carries none) is scraped here (no content
        # guards by construction) while the parser's leading-"(" blanket
        # still drops it — divergence, failing loudly instead of
        # agreed-missing. Zero-diff on the snapshot footnote (no code)
        # is pinned by test_cs_states_full_coverage.
        foot = section + "                        (1) Load search                     98     •\n"
        self.assertEqual(scrape_doc_cs_codes(foot)[98], "(1) Load search")
        self.assertNotIn(98, VEDirectTextParser(foot).parse_cs_states())

    def test_tolerant_cs_leading_digit_exact(self):
        """Leading-digit CS names recover EXACT in tolerant_cs_states (fix,
        formerly pinned when fetch_specs was out of scope): its global
        bullet-anchored search takes the name structurally (everything
        before the trailing code, no charset restriction) with
        ``(?<!\\()`` plus paren-furniture exclusion, so "2Fast Mode 99 •"
        recovers exact (was MISNAMED as "Fast Mode", agreeing green with the
        true name lost). Footnote ``(1)`` (live joins it as ``... 1 \\x95
        (1) Fault 2 ...``) stays out via the lookbehind + paren exclusion —
        without them the open name swallows "(1)  Fault" and loses real
        ``Fault 2`` live (verified live 15==15 today). Real snapshot
        footnote carries no code/bullet (zero-diff). Zero-diff: tolerant
        output on the current snapshot is exactly the 15 EXPECTED_CS states;
        live comparison still agrees (live==snapshot 15). Divergence probe
        with a leading-digit name."""
        from fetch_specs import tolerant_cs_states

        probe = (
            "The state of operation dummy\n"
            "                        2Fast Mode                            99     •\n"
            "                       The error code dummy\n"
        )
        # Fixed behavior: leading digit preserved, exact name.
        self.assertEqual(tolerant_cs_states(probe), {99: "2Fast Mode"})
        # Zero-diff: snapshot tolerant states equal doc truth exactly.
        self.assertEqual(tolerant_cs_states(self.raw_texts["text"]), EXPECTED_CS)
        # Footnote still excluded (real shape carries no code/bullet, so no
        # match on either old or new expression; live "(1) Fault" join keeps
        # Fault via the lookbehind, verified by the live-equality above).
        foot = (
            "The state of operation dummy\n"
            "                           (1) Load search\n"
            "                       The error code dummy\n"
        )
        self.assertEqual(tolerant_cs_states(foot), {})

    def test_tolerant_cs_leading_symbol_exact(self):
        """Leading-symbol CS names recover EXACT in tolerant_cs_states: the
        probe ``*Boost 99`` was MISNAMED as ``{99: 'Boost'}`` (true name
        lost) while snapshot-vs-live tolerant agreement stayed green —
        reproduced synthetically before the fix. The global bullet-anchored
        search now takes the name structurally (everything before the
        trailing code, raw token preserved exactly, no stripping), so the
        probe recovers ``{99: '*Boost'}``. The MPPT-header strip is
        load-bearing live (first chunk merges the device header), so it is
        kept and applied to the raw name (recorded raw, compared raw).
        Zero-diff: snapshot tolerant output still equals EXPECTED_CS (15
        exact) and live agrees (live==snapshot 15; skipped offline)."""
        from fetch_specs import tolerant_cs_states

        probe = (
            "The state of operation dummy\n"
            "                        *Boost                                99     •\n"
            "                       The error code dummy\n"
        )
        # Fixed behavior: raw name preserved exactly (was "Boost").
        self.assertEqual(tolerant_cs_states(probe), {99: "*Boost"})
        # Zero-diff: snapshot tolerant states equal doc truth exactly.
        self.assertEqual(tolerant_cs_states(self.raw_texts["text"]), EXPECTED_CS)
        # Live agrees (skipped unless network is present, like the other
        # live tests): the same raw-name comparison on the online PDF.
        specs_dir = Path(__file__).resolve().parent / "specs"
        snap_text = (specs_dir / "VE.Direct-Protocol-3.34.txt").read_text(errors="ignore")
        try:
            pdf_bytes = download_pdf_bytes(fetch_specs.OFFICIAL_SPECS["VE.Direct-Protocol-3.34"]["url"], timeout=30.0)
        except OSError as e:
            self.skipTest(f"live download unreachable, skipping: {e}")
        self.assertEqual(tolerant_cs_states(extract_pdf_text(pdf_bytes)), tolerant_cs_states(snap_text))

    def test_cs_err_leading_plus_rescue(self):
        """Leading-``+`` CS/ERR names ("+Boost Mode 99 •" / "+Fault mode 99")
        were missed by BOTH the parser and the scrapers (leading
        ``[A-Za-z0-9]`` required) while counts stayed green. Both sides now
        take names structurally (everything before the trailing code, no
        leading-character restriction — like interior ``+``/``:``/``.``
        since cycle-14, see the scraper-name tests), so the probes pass
        honestly with exact names on both sides plus tolerant_cs_states.
        Controls: "(1) Load search" footnote still excluded via the
        explicit paren denylist; snapshot CS/ERR names all start with a
        letter (zero-diff, pinned by full coverage). Divergence probes
        with leading-plus names."""
        from fetch_specs import tolerant_cs_states

        cs_sec = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        +Boost Mode                           99     •\n"
        )
        self.assertEqual(scrape_doc_cs_codes(cs_sec)[99], "+Boost Mode")
        self.assertEqual(VEDirectTextParser(cs_sec).parse_cs_states()[99], "+Boost Mode")
        err_sec = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        +Fault mode                                                         99\n"
        )
        self.assertEqual(scrape_doc_error_codes(err_sec)[99], "+Fault mode")
        self.assertEqual(VEDirectTextParser(err_sec).parse_errors()[99], "+Fault mode")
        # Tolerant live form recovers leading-plus exact too.
        probe = (
            "The state of operation dummy\n"
            "                        +Boost Mode                           99     •\n"
            "                       The error code dummy\n"
        )
        self.assertEqual(tolerant_cs_states(probe), {99: "+Boost Mode"})
        # Controls: a footnote WITH a code ("(1) Load search 98 •",
        # snapshot footnote carries none) is scraped here (no content
        # guards by construction) while the parser's leading-"(" blanket
        # still drops it — divergence, failing loudly; tolerant snapshot
        # stays exact.
        foot_cs = cs_sec + "                        (1) Load search                     98     •\n"
        self.assertEqual(scrape_doc_cs_codes(foot_cs)[98], "(1) Load search")
        self.assertNotIn(98, VEDirectTextParser(foot_cs).parse_cs_states())
        self.assertEqual(tolerant_cs_states(self.raw_texts["text"]), EXPECTED_CS)
        text = self.raw_texts["text"]
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        m_wide = re.search(r"Historical data", tail)
        self.assertIsNotNone(m_wide)
        self.assertEqual(scrape_doc_error_codes(tail[: m_wide.start()]), self.specs["text"].parse_errors())

    def test_cs_err_leading_symbol_divergence(self):
        """Shared leading ``[+A-Za-z0-9]`` hid other-symbol CS/ERR rows on
        both sides (probe ``*Boost  99  •`` missed by scraper and parser
        while EXPECTED_CS stayed green — reproduced synthetically before
        the fix). Both sides now take names structurally (everything before
        the trailing code, no leading-character restriction, furniture-
        denylisted), so the probes pass honestly with exact names on both
        sides. Zero-diff: no snapshot CS/ERR name starts with ``*``
        (15/20 exact, pinned by the full-coverage tests)."""
        from fetch_specs import tolerant_cs_states

        cs_sec = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        *Boost                                99     •\n"
        )
        self.assertEqual(scrape_doc_cs_codes(cs_sec)[99], "*Boost")
        self.assertEqual(VEDirectTextParser(cs_sec).parse_cs_states()[99], "*Boost")
        err_sec = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        *Boost fault                                                        99\n"
        )
        self.assertEqual(scrape_doc_error_codes(err_sec)[99], "*Boost fault")
        self.assertEqual(VEDirectTextParser(err_sec).parse_errors()[99], "*Boost fault")
        # Tolerant live form recovers the leading symbol exact too (was
        # MISNAMED as "Boost", see test_tolerant_cs_leading_symbol_exact).
        probe = (
            "The state of operation dummy\n"
            "                        *Boost                                99     •\n"
            "                       The error code dummy\n"
        )
        self.assertEqual(tolerant_cs_states(probe), {99: "*Boost"})
        # Controls: a footnote WITH a code ("(1) Load search 98 •",
        # snapshot footnote carries none) is scraped here (no content
        # guards by construction) while the parser's leading-"(" blanket
        # still drops it — divergence, failing loudly; snapshots carry no
        # leading-symbol rows.
        foot_cs = cs_sec + "                        (1) Load search                     98     •\n"
        self.assertEqual(scrape_doc_cs_codes(foot_cs)[98], "(1) Load search")
        self.assertNotIn(98, VEDirectTextParser(foot_cs).parse_cs_states())
        text = self.raw_texts["text"]
        pos = text.find("The state of operation. See the table below")
        tail = text[pos:]
        cs_names = list(scrape_doc_cs_codes(tail).values())
        self.assertFalse(any(n.startswith("*") for n in cs_names))
        self.assertFalse(any(n.startswith("*") for n in self.specs["text"].parse_cs_states().values()))

    def test_cs_bound_divergence(self):
        """Proves the CS scraper bound is wider/different than the parser's
        tight "^ERR$" bound (unlike the old identical-bound pair): a
        synthetic parser-parseable row just past ERR (between ERR and the
        next section's "The error code ..." prose) is scraped via the wider
        bound but unparsed via the tight bound, failing loudly as
        scraped-but-unparsed instead of missed by both while EXPECTED_CS
        stays green. Zero-diff: the wider scan adds no hits on snapshots
        (pinned by test_cs_states_full_coverage)."""
        text = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                       ERR\n"
            "                        SyntheticState                         99      •\n"
            "                       The error code of the device dummy\n"
        )
        # Parser stops at "^ERR$" (tight): misses the past-bound row.
        self.assertNotIn(99, VEDirectTextParser(text).parse_cs_states())
        # Scraper scans to the next section's prose (wide): catches it.
        pos = text.find("The state of operation. See the table below")
        tail = text[pos:]
        m_wide = re.search(r"The error code of the device", tail)
        self.assertIsNotNone(m_wide)
        self.assertIn(99, scrape_doc_cs_codes(tail[: m_wide.start()]))
        # Control: the row is parser-parseable inside the bound (bound, not
        # pattern, is what hides it past ERR).
        inside = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        SyntheticState                         99      •\n"
            "                       ERR\n"
        )
        self.assertIn(99, VEDirectTextParser(inside).parse_cs_states())

    def test_cs_furniture_divergence(self):
        """Proves the CS header/furniture guards are no longer identical on
        parser, scraper, AND tolerant extractor: synthetic colliding state
        lines ("Orbit Bit mode 99" vs the "Bit" substring guard,
        "Re-register fault 99" vs the "register" substring guard, "(Boost)
        99" vs the leading-"(" blanket) were missed by all three while
        EXPECTED_CS stayed green (reproduced synthetically before the
        fix). The scraper now keeps everything matching the structural row
        shape (no content guards) and the tolerant extractor carries its
        own differently-justified guards (no "Bit"/"register" substring —
        the header carries no code+bullet so the shape never matches it;
        leading "(" allowed except "(digit" footnote references, so
        "(Boost)" recovers exact while live "(1)  Fault" still resolves to
        "Fault"), so both recover the colliding lines exact while the
        parser keeps its guards and still drops them — divergence, failing
        loudly instead of agreed-missing. A colliding live-joined line
        (single-line ``\\x95`` bullets, no layout) is likewise recovered by
        the tolerant extractor but missed by the parser, so live agreement
        can no longer hide it silently. Controls: snapshot scans stay
        exactly the 15 EXPECTED_CS states on all three sides, and the live
        footnote merge still resolves (see also the live-agreement test)."""
        from fetch_specs import tolerant_cs_states

        def _section(name: str, code: int = 99) -> str:
            pad = " " * max(2, 45 - len(name))
            return (
                "The state of operation. See the table below\n"
                "                        Off                                   0      •\n"
                f"                        {name}{pad}{code}     •\n"
            )

        for name in ("Orbit Bit mode", "Re-register fault", "(Boost)"):
            with self.subTest(name=name):
                sec = _section(name)
                self.assertEqual(scrape_doc_cs_codes(sec)[99], name)
                self.assertNotIn(99, VEDirectTextParser(sec).parse_cs_states())
                live_style = (
                    "The state of operation dummy "
                    f"Off 0 \x95 {name} 99 \x95"
                    "                       The error code dummy\n"
                )
                self.assertEqual(tolerant_cs_states(live_style)[99], name)
        # Snapshot-layout tolerant probes recover exact too.
        for name in ("Orbit Bit mode", "Re-register fault", "(Boost)"):
            with self.subTest(layout=name):
                probe = _section(name) + "                       The error code dummy\n"
                self.assertEqual(tolerant_cs_states(probe)[99], name)
        # Controls: snapshot stays exactly 15 on all three sides.
        self.assertEqual(tolerant_cs_states(self.raw_texts["text"]), EXPECTED_CS)
        text = self.raw_texts["text"]
        pos = text.find("The state of operation. See the table below")
        tail = text[pos:]
        m_wide = re.search(r"The error code of the device", tail)
        self.assertIsNotNone(m_wide)
        self.assertEqual(scrape_doc_cs_codes(tail[: m_wide.start()]), EXPECTED_CS)
        self.assertEqual(self.specs["text"].parse_cs_states(), EXPECTED_CS)
        # Live footnote merge still resolves to the real state (the
        # "(digit"-gated paren, not a blanket, keeps "(1)" out without
        # losing "Fault 2").
        merged = (
            "The state of operation dummy Off 0 \x95 Low power 1 \x95 "
            "(1)  Fault 2 \x95 Bulk 3 \x95"
            "                       The error code dummy\n"
        )
        live_states = tolerant_cs_states(merged)
        self.assertEqual(live_states[2], "Fault")
        self.assertNotIn("(1)  Fault", live_states.values())

    def test_error_codes_full_coverage(self):
        """parse_errors must cover the whole section incl. 66/67/68/116/117/119."""
        errors = self.specs["text"].parse_errors()
        self.assertEqual(errors, EXPECTED_ERRORS)
        # Independent doc-side counter (permissive-gap, EOL-anchored
        # strategy): a single-space row missed by the parser's ``\\s{2,}``
        # rule still counts here, so parser-vs-counter set-equality fails
        # loudly. The scraper bound is WIDER/DIFFERENT than the parser's
        # tight "^HSDS$" header (which would hide a past-bound row on both
        # sides): the scraper scans to the next section's prose ("Historical
        # data", ~52 chars past HSDS) while the parser stops at "^HSDS$".
        # Justified to stay exact: the extra HSDS header + blank carry no
        # EOL-anchored ``name code`` pattern (verified zero-diff); the
        # bound-divergence test proves a past-bound row is scraped-but-unparsed.
        text = self.raw_texts["text"]
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        m_parser = re.search(r"^\s*HSDS\s*$", tail, re.MULTILINE)
        m_wide = re.search(r"Historical data", tail)
        self.assertIsNotNone(m_parser)
        self.assertIsNotNone(m_wide)
        self.assertGreater(m_wide.start(), m_parser.start())
        doc_errors = scrape_doc_error_codes(tail[: m_wide.start()])
        self.assertEqual(set(doc_errors), set(errors))
        self.assertEqual(doc_errors, errors)

    def test_error_counter_single_space_divergence(self):
        """Proves the error counter is truly gap-permissive: a synthetic
        single-space row is counted by scrape_doc_error_codes yet missed by
        the parser's ``\\s{2,}`` rule, i.e. it would fail loudly as a
        counter-vs-parser mismatch instead of staying green."""
        section = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Single spaced 1\n"
        )
        self.assertIn(1, scrape_doc_error_codes(section))
        self.assertNotIn(
            1,
            VEDirectTextParser(section).parse_errors(),
        )

    def test_error_scraper_name_divergence(self):
        """Structural names on both sides (cycle-14 fix, was pinned
        divergence): synthetic interior-symbol names (``Bulk+Boost fault``,
        ``Charge:Fast fault``, ``Dotted.Name fault``) are now scraped AND
        parsed with exact names (honest pass) — the shared closed name
        class that hid them on both sides while hardcoded EXPECTED_ERRORS
        stayed green is gone: names are everything before the trailing
        code on both sides, so neither side can share a charset blind spot
        by construction. Set-equality still fails loudly if either side
        regresses. Remaining divergence axes (proving the scraper/parser
        pair is still independent): single-space rows
        (test_error_counter_single_space_divergence), past-bound rows
        (test_error_bound_divergence), and trailing-prose rows stay honest
        only when both sides keep full-remainder capture
        (test_error_trailing_prose_divergence). Zero-diff: snapshot names
        all start with a letter and carry no trailing prose (pinned by
        test_error_codes_full_coverage)."""
        section = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Bulk+Boost fault                                                    99\n"
            "                        Charge:Fast fault                                                   100\n"
            "                        Dotted.Name fault                                                   102\n"
        )
        scraped = scrape_doc_error_codes(section)
        self.assertEqual(scraped[99], "Bulk+Boost fault")
        self.assertEqual(scraped[100], "Charge:Fast fault")
        self.assertEqual(scraped[102], "Dotted.Name fault")
        parsed = VEDirectTextParser(section).parse_errors()
        self.assertEqual(parsed[99], "Bulk+Boost fault")
        self.assertEqual(parsed[100], "Charge:Fast fault")
        self.assertEqual(parsed[102], "Dotted.Name fault")

    def test_error_bound_divergence(self):
        """Proves the ERR scraper bound is wider/different than the parser's
        tight "^HSDS$" bound (unlike the old identical-bound pair): a
        synthetic parser-parseable row just past HSDS (between HSDS and the
        next section's "Historical data" prose) is scraped via the wider
        bound but unparsed via the tight bound, failing loudly as
        scraped-but-unparsed instead of missed by both while EXPECTED_ERRORS
        stays green. Zero-diff: the wider scan adds no hits on snapshots
        (pinned by test_error_codes_full_coverage)."""
        text = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                       HSDS\n"
            "                        Synthetic fault                                                    99\n"
            "                       Historical data dummy\n"
        )
        # Parser stops at "^HSDS$" (tight): misses the past-bound row.
        self.assertNotIn(99, VEDirectTextParser(text).parse_errors())
        # Scraper scans to "Historical data" prose (wide): catches it.
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        m_wide = re.search(r"Historical data", tail)
        self.assertIsNotNone(m_wide)
        self.assertIn(99, scrape_doc_error_codes(tail[: m_wide.start()]))
        # Control: the row is parser-parseable inside the bound (bound, not
        # pattern, is what hides it past HSDS).
        inside = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Synthetic fault                                                    99\n"
            "                       HSDS\n"
        )
        self.assertIn(99, VEDirectTextParser(inside).parse_errors())

    def test_error_tail_divergence(self):
        """Shared EOL anchor hid bullet-tailed error rows on both sides
        (probe ``Future fault 99 •`` missed by scraper and parser while
        EXPECTED_ERRORS stayed green; CS already tail-diverged). Scraper is
        full-remainder (EOL, bullet tails AND trailing prose all match) and
        the parser captures through end-of-line too, so the probe now
        passes honestly on both sides. Control: snapshots carry no bullet
        tail (zero-diff, full coverage stays exact). Trailing prose beyond
        bullets is covered by test_error_trailing_prose_divergence; note
        prose stays out via the content denylist on both sides."""
        section = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Future fault                                                         99 •\n"
        )
        # Bullet-tail-agnostic scraper catches the future row (was missed).
        self.assertIn(99, scrape_doc_error_codes(section))
        self.assertEqual(scrape_doc_error_codes(section)[99], "Future fault")
        # Parser handles the tail too (honest pass): future bullet-tailed
        # rows parse instead of staying green-but-missed.
        self.assertIn(99, VEDirectTextParser(section).parse_errors())
        self.assertEqual(VEDirectTextParser(section).parse_errors()[99], "Future fault")
        # Zero-diff control: snapshot ERR rows carry no bullet tail, so the
        # widened tail adds no hits (pinned by full coverage).
        text = self.raw_texts["text"]
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        self.assertNotIn("•", tail[: tail.find("HSDS") if "HSDS" in tail else len(tail)])

    def test_error_trailing_prose_divergence(self):
        """Shared EOL-or-bullet tail hid trailing-prose ERR rows on both
        sides (probe ``Future fault  99 (see note)`` missed by scraper and
        parser while EXPECTED_ERRORS stayed green — reproduced synthetically
        before the fix). Both sides now capture the full remainder through
        end-of-line (no tail restriction) with the code extracted
        positionally, so the probe passes honestly with name "Future fault"
        (trailing prose is not part of the name) on both sides. Snapshot
        furniture stays out of the scraper via exact-line pins (verified
        below); the parser keeps its anchored patterns. Zero-diff: no
        snapshot ERR row carries trailing prose (20 exact, pinned by
        test_error_codes_full_coverage)."""
        section = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Future fault                                                        99 (see note)\n"
        )
        self.assertEqual(scrape_doc_error_codes(section)[99], "Future fault")
        self.assertEqual(VEDirectTextParser(section).parse_errors()[99], "Future fault")
        # Controls: snapshot-exact furniture is pinned out of the scraper
        # (exact lines, not patterns) and out of the parser (patterns); a
        # REWORDED note matches no pin and scrapes loudly while the parser
        # still drops it (divergence, not agreed-missing). Snapshots carry
        # no trailing prose.
        notes = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                       Note1: Error 19 can be ignored, this condition regularly occurs\n"
            "                       MPPT charger. Since version 1.15 this error will no longer be reported.\n"
        )
        # Truncated Note1 ("... regularly occurs", not the snapshot-exact
        # "... during start-up or shutdown of the") matches no pin:
        # scraped here, still dropped by the parser's Note pattern.
        self.assertEqual(scrape_doc_error_codes(notes)[19], "Note1: Error")
        self.assertNotIn(19, VEDirectTextParser(notes).parse_errors())
        # Snapshot-exact MPPT continuation stays pinned on both sides.
        self.assertNotIn(1, scrape_doc_error_codes(notes))
        text = self.raw_texts["text"]
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        err_section = tail[: tail.find("HSDS") if "HSDS" in tail else len(tail)]
        self.assertFalse(any(re.search(r"\d{1,3}\s+\(", line) for line in err_section.splitlines()))

    def test_error_furniture_divergence(self):
        """Proves the ERR scraper no longer shares the parser's furniture
        denylist: a synthetic fault line starting with a denylisted word
        ("shutdown fault 99", colliding with the parser's ``shutdown\\b``
        furniture pattern) is missed by the parser yet scraped here with
        its exact name — scraped-but-unparsed, failing loudly via
        set-equality instead of agreed-missing while EXPECTED_ERRORS stays
        green (reproduced synthetically before the fix: both sides missed
        it). The scraper keeps everything matching the structural row shape
        and excludes furniture only by exact full-line pins, so the
        colliding line matches no pin. Parser keeps its guards. Controls:
        the four exact pins occur verbatim in the snapshot ERR section
        (pins are load-bearing, not dead) and the snapshot-wide scan stays
        exactly the 20 EXPECTED_ERRORS."""
        section = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        shutdown fault                                                      99\n"
        )
        self.assertEqual(scrape_doc_error_codes(section)[99], "shutdown fault")
        self.assertNotIn(99, VEDirectTextParser(section).parse_errors())
        # Same collision through the denylisted header word.
        section2 = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        The error code fault                                                99\n"
        )
        self.assertEqual(scrape_doc_error_codes(section2)[99], "The error code fault")
        self.assertNotIn(99, VEDirectTextParser(section2).parse_errors())
        # Pins are load-bearing: each occurs verbatim in the snapshot ERR
        # section (a doc rewording fails here for re-triage AND scrapes
        # loudly as an unpinned line).
        text = self.raw_texts["text"]
        pos = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail = text[pos:]
        wide = tail[: tail.find("Historical data")]
        for pin in (
            "Note1: Error 19 can be ignored, this condition regularly occurs during start-up or shutdown of the",
            "MPPT charger. Since version 1.15 this error will no longer be reported.",
            "Note2: Error 21 can be ignored for 5 minutes, this condition regularly occurs during start-up or",
            "shutdown of the MPPT charger. Since version 1.16 this warning will no longer be reported when it is",
        ):
            self.assertIn(pin, wide, f"furniture pin drifted from snapshot: {pin[:40]}...")
        # Zero-diff: snapshot-wide scan stays exactly the 20 doc errors.
        self.assertEqual(scrape_doc_error_codes(wide), EXPECTED_ERRORS)
        self.assertEqual(self.specs["text"].parse_errors(), EXPECTED_ERRORS)

    def test_capabilities_against_doc(self):
        """Capability bits asserted against the doc maps (incl. Orion 28-31)."""
        self.assertEqual(self.specs["bluesolar"].parse_capabilities(), EXPECTED_CAPS_BS)
        self.assertEqual(self.specs["orion"].parse_capabilities(), EXPECTED_CAPS_ORION)
        # Independent open-vocabulary scraper: names with ``+``/``:`` dropped
        # by the parser's closed name class are still scraped here, so
        # scraper-vs-parser set-equality fails loudly.
        self.assertEqual(
            scrape_doc_capability_bits(
                self.raw_texts["bluesolar"], "Capabilities (register 0x0140)", 1500
            ),
            self.specs["bluesolar"].parse_capabilities(),
        )
        self.assertEqual(
            scrape_doc_capability_bits(
                self.raw_texts["orion"], "Capabilities1 (register 0x0140)", 1800
            ),
            self.specs["orion"].parse_capabilities(),
        )
        # BlueSolar doc genuinely omits bits 22-24 (jumps 21 -> 25).
        self.assertNotIn(22, self.specs["bluesolar"].parse_capabilities())
        self.assertNotIn(23, self.specs["bluesolar"].parse_capabilities())
        self.assertNotIn(24, self.specs["bluesolar"].parse_capabilities())

    def test_capability_scraper_open_vocabulary_divergence(self):
        """Proves the capability scraper is truly open-vocabulary: a
        synthetic ``+`` name is scraped by scrape_doc_capability_bits yet
        dropped by the parser's closed name class, i.e. it would fail
        loudly as a scraper-vs-parser mismatch instead of staying green."""
        text = self.raw_texts["bluesolar"].replace(
            "27        PAYGO support (0=no, 1=yes)",
            "27        PAY+GO support (0=no, 1=yes)",
        )
        self.assertEqual(
            scrape_doc_capability_bits(
                text, "Capabilities (register 0x0140)", 1500
            )[27],
            "PAY+GO support",
        )
        self.assertNotIn(27, BlueSolarHEXParser(text).parse_capabilities())

    def test_capability_scraper_window_divergence(self):
        """Proves the capability scraper scans wider/differently than the
        parser's fixed 1500/1800-char window: a synthetic bit placed past the
        parser window but inside the scraper's wider scan is scraped yet
        missed by the parser, i.e. it would fail loudly instead of being
        missed by both. The scraper's wider scan (max(window*3,5000) + "(0=no"
        marker requirement) is justified to stay exact on snapshots."""
        marker = "Capabilities (register 0x0140)"
        base = self.raw_texts["bluesolar"]
        pos = base.find(marker)
        # Bit at offset ~2000 past marker: beyond parser 1500 window, inside
        # scraper max(1500*3,5000)=5000 window, with required "(0=no" marker.
        tail = "x" * 2000 + "\n 30        Future wide bit (0=no, 1=yes)\n"
        text2 = base[:pos] + marker + tail + base[pos + len(marker):]
        self.assertIn(
            30,
            scrape_doc_capability_bits(text2, marker, 1500),
        )
        self.assertNotIn(
            30,
            BlueSolarHEXParser(text2).parse_capabilities(),
        )

    def test_phoenix_ble_header_typo(self):
        """The §2.2.5 'Capabilities BLE (register 0x0160)' heading is a doc
        typo for 0x0150: parser records it as evidence, drops no register."""
        evidence = self.specs["phoenix"].parse_ble_header_evidence()
        self.assertEqual(set(evidence), {0x0160})
        self.assertNotIn(0x0160, self.ph_regs)
        self.assertIn(0x0150, self.ph_regs)
        self.assertEqual(self.ph_regs[0x0150].name, "VE_REG_CAPABILITIES_BLE")

    def test_typo_evidence_preconditions_hold(self):
        """Guards the typo allowlists: 0x0160 (Phoenix) and 0xEFF0
        (BlueSolar/Orion) sit in NON_REGISTER_IDS on parser-evidence-only.
        If Victron ever allocates either as a real register row, this fails
        for re-triage instead of the reverse-coverage check passing on the
        permanent denylist entry. Preconditions pinned against the
        snapshots: 0x0160 occurs ONLY as the §2.2.5 section-header typo
        (never a VE_REG_ table row), 0xEFF0 ONLY as the "(register 0xEFF0)"
        prose typo for 0xEDF0 (never a table row)."""
        phoenix = self.raw_texts["phoenix"]
        headers = [
            line for line in phoenix.splitlines()
            if re.search(r"Capabilities BLE\s*\(register\s+0x0160\)", line)
        ]
        self.assertEqual(len(headers), 1, f"0x0160 header evidence changed: {headers}")
        table_rows_0160 = [
            line for line in phoenix.splitlines()
            if re.match(r"^\s*0x0160\b", line)
            or ("VE_REG_" in line and "0x0160" in line)
        ]
        self.assertEqual(table_rows_0160, [], f"0x0160 became a real table row: {table_rows_0160}")
        for key in ("bluesolar", "orion"):
            text = self.raw_texts[key]
            self.assertTrue(
                any("(register 0xEFF0)" in line for line in text.splitlines()),
                f"{key}: (register 0xEFF0) prose typo evidence gone",
            )
            table_rows_eff0 = [
                line for line in text.splitlines()
                if re.match(r"^\s*0xEFF0\b", line)
            ]
            self.assertEqual(
                table_rows_eff0, [], f"{key}: 0xEFF0 became a real table row: {table_rows_eff0}"
            )

    def test_phoenix_hex_rev5_pids(self):
        """Phoenix-Inverter-HEX Rev 5 PIDs omitted from Text 3.34 must still decode."""
        extra = (
            0xA2B1, 0xA2B2, 0xA2B4,
            0xA2E1, 0xA2E2, 0xA2E4, 0xA2E9, 0xA2EA, 0xA2EC,
            0xA2F1, 0xA2F2, 0xA2F4, 0xA2F9, 0xA2FA, 0xA2FC,
        )
        missing = [f"0x{pid:04X}" for pid in extra if pid not in T.PID_MAP]
        self.assertEqual(missing, [])
        self.assertIn("5000VA", T.pid_name(0xA2B1))
        self.assertTrue(T.pid_name(0xA2E1).endswith("HS"))

    def test_access_and_product_specific_semantics(self):
        access_map = {"R": "READ_ONLY", "W": "WRITE_ONLY", "R/W": "READ_WRITE"}
        # Doc "R/(W)" on 0x0150 normalizes to the read side so this check
        # asserts it instead of skipping (expected=None); doc "0.02/" on
        # 0xEB10 normalizes to its numeric factor.
        self.assertEqual(self.ph_regs[0x0150].access, "R")
        self.assertEqual(self.ph_regs[0xEB10].scale, 0.02)
        # Loud gate for unknown access forms (unknown-units pattern):
        # unmapped forms (e.g. future "W/(R)") skip the DB comparison
        # below (no mapping to assert) but fail here instead of staying
        # green. Zero-diff: snapshots carry only R/W/R/W.
        self.assertEqual(self.specs["phoenix"].unknown_accesses, [])
        mismatches = []
        unknown_skipped = []
        for rid, spec_reg in self.ph_regs.items():
            expected = access_map.get(spec_reg.access)
            if expected is None:
                unknown_skipped.append(f"0x{rid:04X}:{spec_reg.access}")
                continue
            actual = R.lookup(rid, flavor="INV").access
            if actual != expected and actual != "CONSTANT":
                mismatches.append(
                    f"0x{rid:04X}: expected {expected}, got {actual}")
        # Skipped unknowns must be empty — they fail via the loud gate
        # above, never silently (probe W/(R) would otherwise stay green).
        self.assertEqual(unknown_skipped, [])
        self.assertEqual(mismatches, [])

        # BMV/BS/Orion access (normalize-then-compare, Phoenix pattern):
        # BMV explicit markers "(write only)"/"(read-only)" are DOC truth;
        # READ_WRITE is the parser default where the doc prints no access
        # column — the doc still implies readable (never write-only), while
        # the DB refines measurements/history to READ_ONLY (e.g. 0x0300
        # depth, 0x0FFE TTG, 0xED8E power) and keeps settings READ_WRITE.
        # Hence parser READ_WRITE asserts DB in {READ_WRITE, READ_ONLY}
        # (CONSTANT allowed like Phoenix): no skipping by value, and a doc
        # access change (probe: removing "(write only)" from 0x1029 Zero
        # Current flips WRITE_ONLY->READ_WRITE while DB stays WRITE_ONLY)
        # fails as READ_WRITE-vs-WRITE_ONLY instead of staying green.
        # Zero-diff: all 110 snapshot values pass (70 READ_WRITE, 29
        # READ_ONLY-refined, 4 WRITE_ONLY, 7 READ_ONLY). BS/Orion W
        # (Restore/Clear commands) and R for history records are
        # DOC truth (commands writable, history read-only); R/W defaults and
        # detail-mention Rs with no access column (Orion 0x0090/0x0206/0x2013:
        # settings vs measurement ambiguous in prose, parser default R vs DB
        # READ_WRITE) are skipped with justification — timer events fixed to
        # R/W (Get/Set writable, DB READ_WRITE) so they no longer assert as R.
        for rid, spec_reg in self.bmv_regs.items():
            actual = R.lookup(rid, flavor="BMV").access
            if spec_reg.access == "WRITE_ONLY":
                allowed = {"WRITE_ONLY"}
            elif spec_reg.access == "READ_ONLY":
                allowed = {"READ_ONLY"}
            else:  # READ_WRITE default: doc-readable, DB READ_WRITE or READ_ONLY-refined
                allowed = {"READ_WRITE", "READ_ONLY"}
            if actual not in allowed and actual != "CONSTANT":
                mismatches.append(
                    f"BMV 0x{rid:04X} ({spec_reg.name}): parser {spec_reg.access} "
                    f"allows {sorted(allowed)}, got DB {actual}"
                )
        self.assertEqual(mismatches, [])
        mismatches = []
        for rid, spec_reg in self.bs_regs.items():
            if spec_reg.access == "W":
                expected = "WRITE_ONLY"
            elif spec_reg.access == "R" and (
                "history" in spec_reg.description.lower()
                or "range-expanded" in spec_reg.notes.lower()
                or "total history" in spec_reg.description.lower()
            ):
                expected = "READ_ONLY"
            else:
                continue  # R/W default or timer R/W (Get/Set writable), no assert
            actual = R.lookup(rid, flavor="MPPT").access
            # Timer R/W skipped above; history R must be READ_ONLY (CONSTANT allowed like Phoenix).
            if actual != expected and actual != "CONSTANT":
                # src/ is read-only: DOC truth asserts here; on mismatch report DB file:line.
                mismatches.append(f"BS 0x{rid:04X} ({spec_reg.name}): expected {expected}, got {actual}")
        self.assertEqual(mismatches, [])
        mismatches = []
        for rid, spec_reg in self.or_regs.items():
            if spec_reg.access == "W":
                expected = "WRITE_ONLY"
            elif spec_reg.access == "R" and (
                "history" in spec_reg.description.lower()
                or "cycle" in spec_reg.description.lower()
                or "range-expanded" in spec_reg.notes.lower()
            ):
                expected = "READ_ONLY"
            else:
                continue  # R/W defaults + detail Rs (0x0090/0x0206/0x2013/0x0150/0xEDDA: no access column)
            try:
                actual = R.lookup(rid, flavor="ORION").access
            except Exception:
                continue  # flavor-aliased, covered by schema compliance
            if actual != expected and actual != "CONSTANT":
                mismatches.append(f"Orion 0x{rid:04X} ({spec_reg.name}): expected {expected}, got {actual}")
        self.assertEqual(mismatches, [])

        self.assertEqual(R.lookup("REBULK_VOLTAGE_OFFSET").id, 0xED2E)
        with self.assertRaises(KeyError):
            R.lookup(0x0320, flavor="MPPT")
        self.assertEqual(
            R.lookup(0x0207, flavor="ORION").enum, "OFF_REASON_ORION")
        self.assertEqual(
            R.lookup(0x0207, flavor="INV").enum, "OFF_REASON_INV")
        self.assertEqual(
            set(R.OFF_REASON_INV_BITS), {0, 2, 3, 4, 5})
        self.assertEqual(R.lookup("CAPABILITIES_BLE", flavor="INV").id, 0x0150)
        with self.assertRaises(KeyError):
            R.lookup("CAPABILITIES_BLE_INV")
        with self.assertRaises(KeyError):
            R.lookup(0x0160, flavor="INV")
        self.assertIn(0xFFFFFFFF, R._NONE_SENTINELS[0xEC41])

    def test_phoenix_unknown_access_loud(self):
        """Phoenix access skips unknown forms: probe ``0xEB99 VE_REG_FOO
        W/(R) un16 1 V`` yields access ``W/(R)``, ``access_map.get``
        returns None, the check ``continue``s (skipped), suite green
        while an access change is missed (control ``R/W`` maps). Unknown
        access forms are LOUD (recorded + asserted empty, unknown-units
        pattern) instead of skipped. Zero-diff: snapshots carry only
        R/W/R/W."""
        from spec_parser import PhoenixInverterHEXParser

        self.assertEqual(self.specs["phoenix"].unknown_accesses, [])
        probe_parser = PhoenixInverterHEXParser("0xEB99 VE_REG_FOO W/(R) un16 1 V\n")
        probe = probe_parser.parse_registers()[0xEB99]
        self.assertEqual(probe.access, "W/(R)")
        self.assertEqual(probe_parser.unknown_accesses, [(0xEB99, "W/(R)")])
        # Control maps (no record).
        ctrl_parser = PhoenixInverterHEXParser("0xEB99 VE_REG_FOO R/W un16 1 V\n")
        self.assertEqual(ctrl_parser.parse_registers()[0xEB99].access, "R/W")
        self.assertEqual(ctrl_parser.unknown_accesses, [])
        # R/(W) normalizes to R (no record, asserted read side).
        norm_parser = PhoenixInverterHEXParser("0x0150 VE_REG_CAPABILITIES_BLE R/(W) un32 - -\n")
        self.assertEqual(norm_parser.parse_registers()[0x0150].access, "R")
        self.assertEqual(norm_parser.unknown_accesses, [])

    def test_bmv_access_read_write_included(self):
        """BMV access skips READ_WRITE: probe removing ``(write only)`` from
        ``0x1029 Zero Current`` flips WRITE_ONLY->READ_WRITE while the old
        check ``continue``d on READ_WRITE (suite green while a doc access
        change is missed). All parsed access values are now asserted (no
        skipping by value): parser READ_WRITE allows DB {READ_WRITE,
        READ_ONLY} (CONSTANT like Phoenix), so the probe fails as
        READ_WRITE-vs-WRITE_ONLY. Zero-diff: all 110 snapshot values pass."""
        from spec_parser import BMV7xxHEXParser

        # Snapshot zero-diff: every parsed access maps under the new rule.
        allowed_by_parser = {
            "WRITE_ONLY": {"WRITE_ONLY"},
            "READ_ONLY": {"READ_ONLY"},
            "READ_WRITE": {"READ_WRITE", "READ_ONLY"},
        }
        bad = []
        for rid, spec_reg in self.bmv_regs.items():
            actual = R.lookup(rid, flavor="BMV").access
            allowed = allowed_by_parser[spec_reg.access]
            if actual not in allowed and actual != "CONSTANT":
                bad.append(f"0x{rid:04X}")
        self.assertEqual(bad, [])
        # Probe: marker removed flips to READ_WRITE, DB stays WRITE_ONLY.
        probe = BMV7xxHEXParser(
            "hdr\n0x1029     Zero Current              -         -         -         -         - \nftr"
        ).parse_registers()[0x1029]
        self.assertEqual(probe.access, "READ_WRITE")
        db_access = R.lookup(0x1029, flavor="BMV").access
        self.assertEqual(db_access, "WRITE_ONLY")
        self.assertNotIn(db_access, allowed_by_parser[probe.access])
        # Control: marked row stays WRITE_ONLY and maps.
        ctrl = BMV7xxHEXParser(
            "hdr\n0x1029     Zero Current (write only)                    09      -                      -                  -             -              - \nftr"
        ).parse_registers()[0x1029]
        # Synthetic control row is ID-only shaped; parser still marks write-only.
        self.assertEqual(ctrl.access, "WRITE_ONLY")
        self.assertIn(db_access, allowed_by_parser[ctrl.access])

    def test_scale_scientific_notation_parsed(self):
        """A scientific-notation scale on an allowlisted ID (probe ``0xEDB3
        Tracker mode 1e-2 un16 -``) parsed as scale None with the factor
        leaking into the description, while 0xEDB3's SCALE_NONE_ALLOW
        membership skipped the missing-scale check and ID-based
        reverse-coverage stayed green (reproduced synthetically before the
        fix). Section-table scales now accept the general
        float-with-exponent form, so the probe parses scale 0.01 honestly.
        Doc verification first: no snapshot scale cell uses exponent form
        (docs print plain decimals 0.01/0.1/1 only), so the widened shape is
        zero-diff. Zero-diff: counts and schema checks unchanged (pinned
        elsewhere)."""
        from spec_parser import OrionXSHEXParser, _split_typed_row

        # What the doc actually uses: every snapshot section-table scale
        # token is a plain decimal or "-" (no exponent anywhere).
        for key, fname, cls, bare_x in (
            ("bluesolar", "BlueSolar-HEX-protocol.txt", BlueSolarHEXParser, False),
            ("orion", "Orion-XS-HEX-protocol.txt", OrionXSHEXParser, True),
        ):
            with self.subTest(snapshot=key):
                text = (self.specs_dir / fname).read_text(errors="ignore")
                start = text.find("1.1 Get and Set items")
                end = text.find("1.2 Asynchronous items")
                section = text[start:end] if end > start else text[start:]
                expo = []
                for line in section.splitlines():
                    if not re.match(r"^\s*0[xX][0-9A-Fa-f]{4}\b", line):
                        continue
                    row = _split_typed_row(line, allow_bare_x_prefix=bare_x)
                    if row is None:
                        continue
                    _rid, _desc, scale_str, _dt, _un, _ty = row
                    if scale_str not in (None, "-") and re.search(r"[eE]", scale_str):
                        expo.append((_rid, scale_str))
                self.assertEqual(expo, [], f"{key} snapshot uses exponent scales: {expo}")
        # Exponent probes parse honestly on both section-table parsers.
        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            for token, expected in (("1e-2", 0.01), ("1E-2", 0.01), ("1E+1", 10.0)):
                with self.subTest(parser=cls.__name__, scale=token):
                    synthetic = (
                        "1.1 Get and Set items\n"
                        f"0xEDB3     Tracker mode             {token}        un16        -\n"
                        "1.2 Asynchronous items\n"
                    )
                    probe = cls(synthetic).parse_registers()[0xEDB3]
                    self.assertAlmostEqual(probe.scale, expected)
                    self.assertEqual(probe.description, "Tracker mode")
                    self.assertEqual(probe.data_type, "un16")
                    # 0xEDB3 is allowlisted no-scale, so only an honestly
                    # parsed factor distinguishes the probe from a drop.
                    self.assertIn(0xEDB3, SCALE_NONE_ALLOW["bluesolar"])

    def test_unknown_scale_token_loud_on_allowlisted_ids(self):
        """An unparsable scale-like token on an allowlisted ID (probe
        ``0xEDB3 Tracker mode 1x un16 -``) parsed as scale None with "1x"
        leaking into the description and nothing recorded, while
        SCALE_NONE_ALLOW skipped the check and reverse-coverage stayed
        green (reproduced synthetically before the fix). Unknown scale
        tokens are LOUD (recorded + asserted, unknown-units/unknown-access
        gate pattern) even on allowlisted IDs — allowlists excuse missing
        doc data, never dropped unknown tokens. Zero-diff: all four
        snapshot gates stay empty."""
        from spec_parser import (
            BMV7xxHEXParser,
            OrionXSHEXParser,
            PhoenixInverterHEXParser,
        )

        # Snapshot stays exact (no unknowns on current docs, all parsers).
        self.assertEqual(self.specs["bluesolar"].unknown_scales, [])
        self.assertEqual(self.specs["orion"].unknown_scales, [])
        self.assertEqual(self.specs["bmv"].unknown_scales, [])
        self.assertEqual(self.specs["phoenix"].unknown_scales, [])
        # Synthetic probe on allowlisted 0xEDB3: dropped to None but
        # recorded loudly (section-table gate, both parsers).
        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(parser=cls.__name__):
                parser = cls(
                    "1.1 Get and Set items\n"
                    "0xEDB3     Tracker mode             1x          un16        -\n"
                    "1.2 Asynchronous items\n"
                )
                probe = parser.parse_registers()[0xEDB3]
                self.assertIsNone(probe.scale)
                self.assertIn(0xEDB3, SCALE_NONE_ALLOW["bluesolar"])
                self.assertEqual(parser.unknown_scales, [(0xEDB3, "1x")])
        # Controls: honest exponent parses with no record; genuine "-"
        # absence records nothing either.
        for cls in (BlueSolarHEXParser, OrionXSHEXParser):
            with self.subTest(control=cls.__name__):
                ok = cls(
                    "1.1 Get and Set items\n"
                    "0xEDB3     Tracker mode             1e-2        un16        -\n"
                    "1.2 Asynchronous items\n"
                )
                self.assertAlmostEqual(ok.parse_registers()[0xEDB3].scale, 0.01)
                self.assertEqual(ok.unknown_scales, [])
                bare = cls(
                    "1.1 Get and Set items\n"
                    "0xEDB3     Tracker mode             -           un16        -\n"
                    "1.2 Asynchronous items\n"
                )
                self.assertIsNone(bare.parse_registers()[0xEDB3].scale)
                self.assertEqual(bare.unknown_scales, [])
        # Same gate shape on the other scale cells: BMV Step and Phoenix
        # Scale cells record digit-bearing-but-unparsable tokens loudly.
        bmv_parser = BMV7xxHEXParser(
            "hdr\n0xEDD0     Foo Bar               -         un16        0..1        0           1x          V\nftr"
        )
        bmv_parser.parse_registers()
        self.assertEqual(bmv_parser.unknown_scales, [(0xEDD0, "1x")])
        bmv_ctrl = BMV7xxHEXParser(
            "hdr\n0xEDD0     Foo Bar               -         un16        0..1        0           0.5         V\nftr"
        )
        self.assertAlmostEqual(bmv_ctrl.parse_registers()[0xEDD0].scale, 0.5)
        self.assertEqual(bmv_ctrl.unknown_scales, [])
        phx_parser = PhoenixInverterHEXParser("0xEB99 VE_REG_FOO R/W un16 1x V\n")
        phx_parser.parse_registers()
        self.assertEqual(phx_parser.unknown_scales, [(0xEB99, "1x")])
        phx_ctrl = PhoenixInverterHEXParser("0xEB99 VE_REG_FOO R/W un16 1e-2 V\n")
        self.assertAlmostEqual(phx_ctrl.parse_registers()[0xEB99].scale, 0.01)
        self.assertEqual(phx_ctrl.unknown_scales, [])

    def test_text_field_counter_furniture_pins_divergence(self):
        """Proves the fields counter no longer shares the parser's content
        denylist: synthetic label rows colliding with parser stoplist words
        ("Available", "When", "The", "Label", "60x", "Victron", "General")
        were missed by BOTH sides while 63 stayed green (reproduced
        synthetically before the fix: counter denied them, parser
        stoplists them). The counter now excludes furniture ONLY by exact
        full-line pins (stripped snapshot lines verbatim, ERR-precedent),
        so colliding future rows match no pin and are counted here while
        the parser still drops them — scraped-but-unparsed, failing loudly
        via set-equality instead of agreed-missing. The parser keeps its
        stoplist (furniture must still parse as 63 exact — honest pass on
        snapshots), so this divergence is the loud mechanism. Zero-diff: 63
        exact on both sides, and every pin below occurs verbatim in the
        snapshot (pins are load-bearing: a doc rewording fails here for
        re-triage AND scrapes loudly as an unpinned line)."""
        base = "The units used by the serial interface are as follows:\n"
        end = "Table I Supported Text-mode fields\n"
        for label in ("Available", "When", "The", "Label", "60x", "Victron", "General"):
            with self.subTest(label=label):
                section = (
                    base
                    + "             V                   mV                       Main voltage\n"
                    + f"             {label}                 mV                       Future quantity\n"
                    + end
                )
                self.assertIn(label, count_doc_field_labels(section))
                self.assertNotIn(label, VEDirectTextParser(section).parse_fields())
        # Pins are load-bearing: each occurs verbatim in the snapshot
        # fields section.
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        section = text[start:text.find("Table I Supported Text-mode fields")]
        for pin in (
            "Label          Units                               Description                                 BMV          BMV          BMV71x           MPPT 1        Phoenix         Phoenix          Smart",
            "Label                 Units                                Description                       BMV          BMV          BMV71x        MPPT 1   Phoenix    Phoenix     Smart",
            "Label                 Units                              Description               BMV   BMV    BMV71x      MPPT 1   Phoenix    Phoenix     Smart",
            "60x          70x         SmartShunt                      Inverter        Charger        BuckBoost",
            "60x          70x         SmartShunt              Inverter   Charger   BuckBoost",
            "60x   70x   SmartShunt            Inverter   Charger   BuckBoost",
            "The units used by the serial interface are as follows:",
            "The VE.Direct protocol is available in the MPPT 70/15 since firmware version v1.09 and up, and only in newer type MPPT 70/15s: the ones with a product id other than 0x300. The first",
            "Available on BlueSolar mppt chargers since firmware version v1.16",
            "Available on BlueSolar mppt chargers since firmware version v1.37",
            "Available on Phoenix Inverter (some models) since firmware version v1.16",
            "Available on Phoenix Inverter v1.16",
            "Available on Phoenix Smart Chargers with triple outputs only",
            "Available on Smart Phoenix Inverter",
            "Available on SmartSolar mppt chargers since firmware version v1.17",
            "Available on SmartSolar mppt chargers since firmware version v1.44 (VE.Direct models) and v1.03 (SmartSolar VE.Can models)",
            "Available since version v1.12, and only for models with a load output.",
            "Available since version v1.15, and only for models with a load output.",
            "Available since version v4.08",
            "Available when the AUX input is configured as Mid Point",
            "Available when the AUX input is configured as Starter Battery",
            "Available when the AUX input is configured as Temperature",
            "When configured as DC monitor, “---” will be sent instead of a value.",
            "When no temperature sensor is connected, “---” will be sent instead of a value.",
            "When the BMV is not synchronised, these statistics have no meaning, so “---” will be sent instead of a value.",
            "When the battery is not discharging the time- to-go is infinite. This is represented as -1.",
            "Victron Energy B.V. | De Paal 35 | 1351 JG Almere | The Netherlands",
            "General phone: +31 (0)36 535 97 00 | E-mail: sales@victronenergy.com",
        ):
            self.assertIn(pin, section, f"furniture pin drifted from snapshot: {pin[:40]}...")
        # Zero-diff: snapshot-wide scan stays exactly the 63 doc fields on
        # both sides (parser set-equality pinned by test_text_protocol_spec).
        self.assertEqual(len(count_doc_field_labels(section)), EXPECTED_COUNTS["fields"])
        self.assertEqual(set(self.text_fields), count_doc_field_labels(section))

    def test_cs_err_four_digit_codes(self):
        """A shared 3-digit bound hid 4-digit CS/ERR codes on both sides
        (probes ``FutureState 1234`` / ``Future fault 1234`` missed by
        scrapers AND parsers while EXPECTED maps stayed green — reproduced
        synthetically before the fix). All four patterns now accept up to 4
        digits (5-digit would collide with decimal register IDs and years),
        so the probes pass honestly with exact codes/names on both sides.
        The live tolerant CS extractor still uses the 3-digit bound
        (tools/fetch_specs.py:883 — read-only here, reported not edited),
        so it misses the probe: documented divergence below, failing loudly
        in tolerant-vs-parser agreement once snapshots carry a 4-digit code.
        Zero-diff: 15/20 exact on every side (pinned by full coverage)."""
        from fetch_specs import tolerant_cs_states

        cs_sec = (
            "The state of operation. See the table below\n"
            "                        Off                                   0      •\n"
            "                        FutureState                           1234     •\n"
        )
        self.assertEqual(scrape_doc_cs_codes(cs_sec)[1234], "FutureState")
        self.assertEqual(VEDirectTextParser(cs_sec).parse_cs_states()[1234], "FutureState")
        err_sec = (
            "The error code of the device (relevant when the device is in the fault state)\n"
            "                        No error                                                            0\n"
            "                        Future fault                                                        1234\n"
        )
        self.assertEqual(scrape_doc_error_codes(err_sec)[1234], "Future fault")
        self.assertEqual(VEDirectTextParser(err_sec).parse_errors()[1234], "Future fault")
        # Divergence: the read-only tolerant extractor shares the old bound
        # (tools/fetch_specs.py:883 ``(\\d{1,3})``) and misses the 4-digit
        # probe while parser+scraper catch it — reported, not edited
        # (fetch_specs.py is out of scope for this change).
        self.assertNotIn(1234, tolerant_cs_states(cs_sec))
        self.assertEqual(tolerant_cs_states(self.raw_texts["text"]), EXPECTED_CS)
        # Zero-diff: snapshot-wide scans stay exactly the doc maps.
        text = self.raw_texts["text"]
        pos = text.find("The state of operation. See the table below")
        tail = text[pos:]
        m_wide = re.search(r"The error code of the device", tail)
        self.assertIsNotNone(m_wide)
        self.assertEqual(scrape_doc_cs_codes(tail[: m_wide.start()]), EXPECTED_CS)
        pos_e = text.find("The error code of the device (relevant when the device is in the fault state)")
        tail_e = text[pos_e:]
        m_wide_e = re.search(r"Historical data", tail_e)
        self.assertIsNotNone(m_wide_e)
        self.assertEqual(scrape_doc_error_codes(tail_e[: m_wide_e.start()]), EXPECTED_ERRORS)

    def test_write_only_spelling_variants(self):
        """The hyphen ``write-only`` spelling (probe ``0x0399 Foo Bar
        write-only - un16 V``) parsed as READ_WRITE — only ``(write only)``
        matched — while the BMV access check allows READ_WRITE, staying
        green (reproduced synthetically before the fix). All three obvious
        spellings (``(write only)`` as the snapshots print it, ``write-only``
        hyphenated, bare ``write only``) now parse WRITE_ONLY, in both the
        BMV access column logic and the shared _is_write_desc used by the
        BlueSolar/Orion tables. Word-anchored: "rewrite"/"overwrite" never
        match. The BMV access assertion now catches the probe: hyphen-marked
        on 0x0303 (DB READ_ONLY) parses WRITE_ONLY, whose allowed set
        {WRITE_ONLY} excludes the DB value (the old READ_WRITE parse allowed
        {READ_WRITE, READ_ONLY} and stayed green). Zero-diff: the snapshot
        WRITE_ONLY set is exactly the 4 marked command rows."""
        from spec_parser import BMV7xxHEXParser

        # Per-spelling probes: every spelling parses WRITE_ONLY (BMV rows).
        for spelling in ("(write only)", "write-only", "write only"):
            with self.subTest(spelling=spelling):
                probe = BMV7xxHEXParser(
                    f"hdr\n0x0399     Foo Bar {spelling}           -         un16        V\nftr"
                ).parse_registers()[0x0399]
                self.assertEqual(probe.access, "WRITE_ONLY")
                # Shared BlueSolar/Orion write-hint path, same spellings.
                self.assertTrue(_is_write_desc(f"Foo Bar {spelling}"))
        # Anchoring controls: rewrite/overwrite prose never matches.
        self.assertFalse(_is_write_desc("Rewrite only the readable value"))
        self.assertFalse(_is_write_desc("Overwrite-only readable value"))
        # The BMV access assertion catches the hyphen probe: 0x0303 is
        # READ_ONLY in the DB, so a hyphen-marked (WRITE_ONLY-parsed) row
        # mismatches while the old READ_WRITE parse stayed green.
        hyphen = BMV7xxHEXParser(
            "hdr\n0x0303     Foo Bar write-only           -         un32        W\nftr"
        ).parse_registers()[0x0303]
        self.assertEqual(hyphen.access, "WRITE_ONLY")
        db_access = R.lookup(0x0303, flavor="BMV").access
        self.assertEqual(db_access, "READ_ONLY")
        self.assertNotIn(db_access, {"WRITE_ONLY"})
        # Control: the old spelling on the same shape also parses WRITE_ONLY.
        ctrl = BMV7xxHEXParser(
            "hdr\n0x0303     Foo Bar (write only)           -         un32        W\nftr"
        ).parse_registers()[0x0303]
        self.assertEqual(ctrl.access, "WRITE_ONLY")
        # Zero-diff: snapshot WRITE_ONLY set is exactly the 4 marked rows.
        self.assertEqual(
            {rid for rid, r in self.bmv_regs.items() if r.access == "WRITE_ONLY"},
            {0x0004, 0x1029, 0x102C, 0x1030},
        )

    def test_pid_name_rename_divergence(self):
        """Proves PID name comparison is exact: a synthetic rename
        ``0x0203 BMV-700->BMV-700R`` keeps ID-set/count green but fails the
        name comparison. Control: snapshot names match exactly (zero-diff)."""
        self.assertEqual(T.PID_MAP.get(0x0203), self.pids[0x0203].model_name)
        renamed = dict(self.pids)
        from spec_parser import ParsedPID

        renamed[0x0203] = ParsedPID(pid=0x0203, model_name="BMV-700R", phased_out=False)
        mism = [
            f"0x{pid:04X}: doc {p.model_name!r} vs code {T.PID_MAP.get(pid)!r}"
            for pid, p in renamed.items()
            if T.PID_MAP.get(pid) != p.model_name
        ]
        self.assertEqual(len(mism), 1)
        self.assertIn("0x0203", mism[0])
        # Zero-diff control: snapshot names all match (pinned by
        # test_text_protocol_spec).
        self.assertEqual(
            [pid for pid, p in self.pids.items() if T.PID_MAP.get(pid) != p.model_name],
            [],
        )


class TestFetchSpecs(unittest.TestCase):
    """Cover the fetch_specs branches the parser suite never touches (all
    network/subprocess touchpoints mocked; synthetic inputs only)."""

    def test_snapshot_revision_info_rev_format(self):
        label, date = snapshot_revision_info(
            "Title\nRev 18\n\nRevision Log\n\n15-11-2021 some change\n"
        )
        self.assertEqual(label, "Rev 18")
        self.assertEqual(date, datetime.date(2021, 11, 15))

    def test_snapshot_revision_info_version_history_format(self):
        label, date = snapshot_revision_info(
            "Doc\nDocument history\n3.33             06 June 2023\n"
            "3.34             14 April 2025\n"
        )
        self.assertEqual(label, "v3.34")
        self.assertEqual(date, datetime.date(2025, 4, 14))

    def test_snapshot_revision_info_iso_format(self):
        label, date = snapshot_revision_info(
            "Doc\nChanges:\n3) 2018-11-22\n4) 2019-09-27\n"
        )
        self.assertIsNone(label)
        self.assertEqual(date, datetime.date(2019, 9, 27))

    def test_snapshot_revision_info_orion_initial_version_label(self):
        """Orion-XS tail ("Changes: 16-06-2025 Initial version ...") must
        yield a real label, not None: otherwise the label check is vacuous
        and freshness is date-only."""
        label, date = snapshot_revision_info(
            "Doc\nChanges:\n\n16-06-2025\nInitial version derived from BlueSolar HEX protocol - rev 18.docx\n"
        )
        self.assertEqual(label, "Initial version 2025-06-16")
        self.assertEqual(date, datetime.date(2025, 6, 16))
        # Real snapshot agrees.
        text = (Path(__file__).resolve().parent / "specs" / "Orion-XS-HEX-protocol.txt").read_text(errors="ignore")
        self.assertEqual(
            snapshot_revision_info(text),
            ("Initial version 2025-06-16", datetime.date(2025, 6, 16)),
        )

    def test_snapshot_revision_info_month_first_format(self):
        label, date = snapshot_revision_info(
            "Doc\nRev 9\nRevision Log\nApril 14, 2025\nSome entry\n"
        )
        self.assertEqual(label, "Rev 9")
        self.assertEqual(date, datetime.date(2025, 4, 14))

    def test_snapshot_revision_info_ordinal_day_first_format(self):
        label, date = snapshot_revision_info(
            "Doc\nChanges:\n14th April 2025\nSome entry\n"
        )
        self.assertEqual(date, datetime.date(2025, 4, 14))

    def test_snapshot_revision_info_ordinal_month_first_format(self):
        label, date = snapshot_revision_info(
            "Doc\nChanges:\nApril 2nd, 2023\nSome entry\n"
        )
        self.assertEqual(date, datetime.date(2023, 4, 2))

    def test_snapshot_revision_info_unparseable_stays_unknown(self):
        """Total date-mining failure must stay unknown (loud skip), never a
        guessed date that could mask staleness."""
        self.assertEqual(snapshot_revision_info("hello world"), (None, None))
        self.assertEqual(
            snapshot_revision_info("Doc\nChanges:\nsometime last spring\n"), (None, None)
        )

    def test_snapshot_revision_info_no_markers(self):
        self.assertEqual(snapshot_revision_info("hello world"), (None, None))

    def test_remote_last_modified_ok(self):
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value.__enter__.return_value.headers = {
                "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"
            }
            dt = remote_last_modified("http://example/x.pdf")
        self.assertEqual(dt, datetime.datetime(2015, 10, 21, 7, 28, tzinfo=datetime.timezone.utc))

    def test_remote_last_modified_naive_assumes_utc(self):
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value.__enter__.return_value.headers = {
                "Last-Modified": "21 Oct 2015 07:28:00"
            }
            dt = remote_last_modified("http://example/x.pdf")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_remote_last_modified_no_header(self):
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value.__enter__.return_value.headers = {}
            self.assertIsNone(remote_last_modified("http://example/x.pdf"))

    def test_remote_last_modified_bad_date(self):
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value.__enter__.return_value.headers = {
                "Last-Modified": "not a date"
            }
            self.assertIsNone(remote_last_modified("http://example/x.pdf"))

    def test_extract_text_without_pdftotext(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(extract_text(Path("a.pdf"), Path("a.txt")))

    def _write_all_specs(self, tmp: Path, text: str) -> None:
        for key in fetch_specs.OFFICIAL_SPECS:
            (tmp / f"{key}.txt").write_text(text)

    def test_check_specs_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                self.assertTrue(check_specs(Path(tmp)))

    def test_check_specs_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            ):
                self.assertFalse(check_specs(Path(tmp)))

    def test_check_specs_same_day_is_unknown_not_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2023, 2, 1, 18, 0, tzinfo=datetime.timezone.utc),
            ):
                self.assertFalse(check_specs(Path(tmp)))

    def test_check_specs_unverifiable_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=None):
                self.assertFalse(check_specs(Path(tmp)))

    def test_check_specs_undated_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "no markers here\n")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                self.assertFalse(check_specs(Path(tmp)))

    def test_check_specs_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")
            (Path(tmp) / "BlueSolar-HEX-protocol.txt").unlink()
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                self.assertFalse(check_specs(Path(tmp)))

    def _write_dated_specs(self, tmp: Path) -> None:
        self._write_all_specs(Path(tmp), "Rev 9\nRevision Log\n1 February 2023\n")

    @staticmethod
    def _content_pdf_bytes(marker_text: str) -> bytes:
        """Craft minimal PDF bytes with a FlateDecode stream (stdlib-only).

        Splits the marker text into single-char ``(...)`` fragments inside a
        ``[...]TJ`` array (with kerning numbers) to mirror the real Victron
        PDFs' split-word layout, so the extractor's concatenation path is
        what the test exercises — not a trivial whole-string match.
        """
        parts = []
        for ch in marker_text:
            if ch in "()\\":
                parts.append(f"(\\{ch})")
            else:
                parts.append(f"({ch})")
        content = ("BT [ " + "".join(f"{p}5 " for p in parts) + "] TJ ET").encode("latin-1")
        comp = zlib.compress(content)
        return (
            b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj\n"
            b"4 0 obj << /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >> stream\n"
            + comp + b"\nendstream endobj\ntrailer << /Root 1 0 R >>\n%%EOF"
        )

    def _markers_pdf_for(self, label, date) -> bytes:
        """Build a crafted PDF embedding derived ``(label, date)`` markers.

        No revision literals: everything embedded derives from the caller's
        ``(label, date)``, which tests derive from the snapshots (or bump
        from them), so the suite still fails for real when Victron publishes
        a new revision.
        """
        bits = [
            "Revision Log",
            "Document history",
            "Changes:",
            label or "",
            date.strftime("%d-%m-%Y"),
            date.isoformat(),
            date.strftime("%d %B %Y"),
        ]
        if label and re.match(r"v3\.\d+$", label):
            bits.append(f"{label[1:]} {date.day} {date.strftime('%B')} {date.year}")
        if label and "Initial version" in label:
            bits.append("Initial version")
        return self._content_pdf_bytes("  ".join(b for b in bits if b))

    @staticmethod
    def _bumped_label_date(label, date):
        """Derive a provably-newer ``(label, date)`` from snapshot markers.

        No revision literals: Rev N -> Rev N+1, vA.B -> vA.(B+1), Initial
        version D -> Initial version D+30d; the date always moves +30 days so
        both the label and the date prove newness.
        """
        new_date = date + datetime.timedelta(days=30)
        m = re.match(r"Rev\s+(\d+)$", label or "")
        if m:
            return f"Rev {int(m.group(1)) + 1}", new_date
        m = re.match(r"v(\d+)\.(\d+)$", label or "")
        if m:
            return f"v{m.group(1)}.{int(m.group(2)) + 1}", new_date
        if label and "Initial version" in label:
            return f"Initial version {new_date.isoformat()}", new_date
        return (f"{label}+", new_date) if label else (label, new_date)

    def _copy_real_snapshots(self, tmp: Path) -> dict:
        """Copy the checked-in snapshots to ``tmp``; return derived markers.

        Returns ``{key: (label, date)}`` via :func:`snapshot_revision_info`
        on the real snapshot text — expected answers derive from the
        snapshots/manifest, never literals.
        """
        specs_dir = Path(__file__).resolve().parent / "specs"
        out = {}
        for key in fetch_specs.OFFICIAL_SPECS:
            text = (specs_dir / f"{key}.txt").read_text(errors="ignore")
            (tmp / f"{key}.txt").write_text(text)
            out[key] = snapshot_revision_info(text)
        return out

    def test_freshness_status_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                # Header-old stays FRESH with no download (fast path).
                with mock.patch.object(fetch_specs, "download_pdf_bytes") as md:
                    status, _ = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "fresh")
                    md.assert_not_called()

    def test_freshness_status_stale(self):
        # Header-newer alone no longer fails: the download must prove a
        # newer revision. Craft FlateDecode PDFs bumping the fixture's own
        # markers (derived, not hardcoded) so the content stage reports STALE.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            label, local_date = snapshot_revision_info("Rev 9\nRevision Log\n1 February 2023\n")
            new_label, new_date = self._bumped_label_date(label, local_date)
            pdf = self._markers_pdf_for(new_label, new_date)
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            ):
                with mock.patch.object(
                    fetch_specs, "download_pdf_bytes", return_value=pdf
                ) as md:
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "stale")
                    self.assertIn("BlueSolar", detail)
                    self.assertIn(new_label, detail)
                    self.assertEqual(md.call_count, len(fetch_specs.OFFICIAL_SPECS))

    def test_freshness_status_stale_wins_over_same_day_unknown(self):
        """Regression for the first-failure short-circuit: an early
        same-day content-FRESH (via download + matching markers) must not
        mask a later content-proven ``stale`` — all specs are evaluated
        and the worst status wins. Same-day now verifies through the
        content gate (not header-fresh without download), so every spec
        downloads once."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            label, local_date = snapshot_revision_info("Rev 9\nRevision Log\n1 February 2023\n")
            matching_pdf = self._markers_pdf_for(label, local_date)
            new_label, new_date = self._bumped_label_date(label, local_date)
            bumped_pdf = self._markers_pdf_for(new_label, new_date)
            calls = {"n": 0}

            def _fake(url, timeout=15.0):
                calls["n"] += 1
                if calls["n"] == 1:
                    # First spec (BlueSolar): same-day header -> content gate
                    # with matching markers -> FRESH (with download) ...
                    return datetime.datetime(2023, 2, 1, 18, 0, tzinfo=datetime.timezone.utc)
                # ... but later specs have newer headers AND newer content.
                return datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)

            def _fake_dl(url, timeout=15.0):
                # First URL (BlueSolar, same-day) carries matching markers;
                # later URLs carry bumped (newer) markers.
                keys = list(fetch_specs.OFFICIAL_SPECS)
                idx = next(
                    (i for i, k in enumerate(keys) if fetch_specs.OFFICIAL_SPECS[k]["url"] == url),
                    0,
                )
                return matching_pdf if idx == 0 else bumped_pdf

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_dl) as md:
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "stale", detail)
                    self.assertEqual(calls["n"], len(fetch_specs.OFFICIAL_SPECS))
                    # Same-day spec now downloads too; each spec fetched once.
                    self.assertEqual(md.call_count, len(fetch_specs.OFFICIAL_SPECS))
                    self.assertIn(new_label, detail)

    def test_freshness_status_unknown_names_every_spec(self):
        """When several specs are unverifiable, the detail names all of
        them (loud skip), not just the first."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=None):
                status, detail = snapshot_freshness_status(Path(tmp))
                self.assertEqual(status, "unknown")
                for key in fetch_specs.OFFICIAL_SPECS:
                    self.assertIn(key, detail)

    def test_freshness_status_same_day_verifies_content(self):
        """Same-day (equal) headers must attempt the content/marker path
        (download + stdlib marker extraction, no pdftotext): matching
        markers -> FRESH (with download), newer markers -> STALE, and
        unextractable/offline -> UNKNOWN (loud skip, never fail). The old
        header-fresh-without-download passed a same-day online revision
        silently while check_specs correctly reported STALE-UNKNOWN
        (suite/CLI inconsistency)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            label, local_date = snapshot_revision_info("Rev 9\nRevision Log\n1 February 2023\n")
            matching_pdf = self._markers_pdf_for(label, local_date)
            new_label, new_date = self._bumped_label_date(label, local_date)
            bumped_pdf = self._markers_pdf_for(new_label, new_date)
            markerless = self._content_pdf_bytes("hello world no markers here")
            same_day = datetime.datetime(2023, 2, 1, 18, 0, tzinfo=datetime.timezone.utc)
            # Markers-match -> FRESH, with one download per spec (cached).
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=same_day):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=matching_pdf) as md:
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "fresh", detail)
                    self.assertEqual(md.call_count, len(fetch_specs.OFFICIAL_SPECS))
            # Markers-newer -> STALE naming the new revision.
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=same_day):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=bumped_pdf):
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "stale", detail)
                    self.assertIn(new_label, detail)
            # Unextractable markers -> UNKNOWN naming every spec.
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=same_day):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=markerless):
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown", detail)
                    for key in fetch_specs.OFFICIAL_SPECS:
                        self.assertIn(key, detail)
            # Download failure (offline) -> UNKNOWN, never fail.
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=same_day):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=OSError("boom")):
                    status, _ = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown")

    def test_freshness_status_offline_is_unknown(self):
        # Offline (URLError from urlopen) must SKIP the suite, never fail it.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                side_effect=OSError("Network is unreachable"),
            ):
                with mock.patch.object(fetch_specs, "download_pdf_bytes") as md:
                    status, _ = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown")
                    md.assert_not_called()

    def test_freshness_status_no_header_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            with mock.patch.object(fetch_specs, "remote_last_modified", return_value=None):
                with mock.patch.object(fetch_specs, "download_pdf_bytes") as md:
                    status, _ = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown")
                    md.assert_not_called()

    def test_freshness_status_undated_local_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "no markers here\n")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                status, _ = snapshot_freshness_status(Path(tmp))
                self.assertEqual(status, "unknown")

    def test_freshness_status_missing_file_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            (Path(tmp) / "BlueSolar-HEX-protocol.txt").unlink()
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2022, 5, 1, tzinfo=datetime.timezone.utc),
            ):
                status, _ = snapshot_freshness_status(Path(tmp))
                self.assertEqual(status, "unknown")

    def test_extract_pdf_text_flatedecode_split_words(self):
        """Crafted FlateDecode stream with kerning-split words recovers
        contiguous markers without pdftotext (stdlib zlib only)."""
        pdf = self._content_pdf_bytes("Revision Log Rev 9 01-02-2023")
        text = extract_pdf_text(pdf)
        self.assertIn("Rev 9", text)
        self.assertIn("01-02-2023", text)
        label, date = extract_pdf_revision_info(pdf)
        self.assertEqual(label, "Rev 9")
        self.assertEqual(date, datetime.date(2023, 2, 1))

    def test_extract_pdf_revision_info_matches_snapshot_parser(self):
        """Mocked technique proof (no network): for every checked-in
        snapshot, a FlateDecode PDF crafted from its own derived markers
        round-trips through extract_pdf_revision_info exactly."""
        specs_dir = Path(__file__).resolve().parent / "specs"
        for key in fetch_specs.OFFICIAL_SPECS:
            with self.subTest(spec=key):
                text = (specs_dir / f"{key}.txt").read_text(errors="ignore")
                label, date = snapshot_revision_info(text)
                self.assertIsNotNone(date, f"{key} snapshot has no date")
                pdf = self._markers_pdf_for(label, date)
                self.assertEqual(extract_pdf_revision_info(pdf), (label, date))

    def test_content_gate_markers_match_is_fresh(self):
        """Newer header + SAME revision markers in the PDF -> FRESH (the
        false-positive fix): expected markers derive from the checked-in
        snapshots, never literals, so a real new revision still fails loudly.
        ID stage included (third stage): matching PDFs list the snapshot's
        own ID sets, so markers+IDs both match -> FRESH (not unknown)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }
            # Matching PDFs carry markers PLUS snapshot-exact IDs (derived),
            # so the ID stage (headers -> markers -> ID sets) stays FRESH.
            snap_ids = {
                key: register_id_set_from_text(
                    (tmpdir / f"{key}.txt").read_text(errors="ignore")
                )
                for key in fetch_specs.OFFICIAL_SPECS
            }
            pdfs = {
                key: self._ids_pdf_for(label, date, snap_ids[key])
                for key, (label, date) in expected.items()
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(f"unexpected url {url}")

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return pdfs[key]
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download) as md:
                    status, detail = snapshot_freshness_status(tmpdir)
                    self.assertEqual(status, "fresh", detail)
                    # One fetch per spec max (cached within the run).
                    self.assertEqual(md.call_count, len(fetch_specs.OFFICIAL_SPECS))

    def test_content_gate_markers_newer_is_stale(self):
        """Newer header + NEWER revision markers in the PDF -> STALE naming
        the new revision: bumped markers derive from the snapshots, never
        literals."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            bumped = {
                key: self._bumped_label_date(label, date)
                for key, (label, date) in expected.items()
            }
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }
            pdfs = {
                key: self._markers_pdf_for(*bumped[key]) for key in expected
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(f"unexpected url {url}")

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return pdfs[key]
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download):
                    status, detail = snapshot_freshness_status(tmpdir)
                    self.assertEqual(status, "stale", detail)
                    for key in expected:
                        self.assertIn(bumped[key][0], detail, key)

    def test_content_gate_unextractable_is_unknown(self):
        """Newer header but no revision markers in the PDF -> UNKNOWN (loud
        skip naming every spec, never fail)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            markerless = self._content_pdf_bytes("hello world no markers here")
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            ):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=markerless):
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown")
                    for key in fetch_specs.OFFICIAL_SPECS:
                        self.assertIn(key, detail)

    def test_content_gate_download_failure_is_unknown(self):
        """Newer header but the PDF download fails -> UNKNOWN (loud skip
        naming every spec, never fail)."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_dated_specs(Path(tmp))
            with mock.patch.object(
                fetch_specs, "remote_last_modified",
                return_value=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
            ):
                with mock.patch.object(
                    fetch_specs, "download_pdf_bytes", side_effect=OSError("boom")
                ):
                    status, detail = snapshot_freshness_status(Path(tmp))
                    self.assertEqual(status, "unknown")
                    for key in fetch_specs.OFFICIAL_SPECS:
                        self.assertIn(key, detail)

    def test_download_pdf_bytes_uses_urllib(self):
        """download_pdf_bytes goes through stdlib urllib (mocked urlopen)."""
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value.__enter__.return_value.read.return_value = b"%PDF-fake"
            self.assertEqual(download_pdf_bytes("http://example/x.pdf"), b"%PDF-fake")

    def test_content_gate_live_online_markers_match(self):
        """Live behavior (skipped unless network is present): the real online
        PDFs' stdlib-extracted markers still equal their snapshots' (same
        revisions, re-uploaded). A real new revision fails here loudly
        instead of passing; offline skips. Other live assertions stay out of
        the suite (the mocked tests above cover the mechanism) to keep
        default runs fast and offline-green."""
        specs_dir = Path(__file__).resolve().parent / "specs"
        for key, info in fetch_specs.OFFICIAL_SPECS.items():
            with self.subTest(spec=key):
                snap_text = (specs_dir / f"{key}.txt").read_text(errors="ignore")
                expected = snapshot_revision_info(snap_text)
                try:
                    pdf_bytes = download_pdf_bytes(info["url"], timeout=30.0)
                except OSError as e:
                    self.skipTest(f"live download unreachable, skipping: {e}")
                self.assertEqual(
                    extract_pdf_revision_info(pdf_bytes), expected,
                    f"{key} live markers diverged from snapshot",
                )

    def _ids_pdf_for(self, label, date, ids) -> bytes:
        """Craft PDF embedding derived markers PLUS explicit ID list.

        Markers derive from the caller's (label, date) (no revision
        literals, like _markers_pdf_for); IDs render as "0xHHHH" space
        runs so stdlib live_register_ids recovers them exactly (single-char
        TJ fragments, like _content_pdf_bytes). Snapshot-exact PDFs list the
        snapshot's own register_id_set_from_text; added/removed variants add
        or omit one ID (never literals for revisions, IDs 0x9999/0xEDF0 are
        synthetic/derived choices documented per test).
        """
        bits = [
            "Revision Log",
            "Document history",
            "Changes:",
            label or "",
            date.strftime("%d-%m-%Y"),
            date.isoformat(),
            date.strftime("%d %B %Y"),
        ]
        if label and re.match(r"v3\.\d+$", label):
            bits.append(f"{label[1:]} {date.day} {date.strftime('%B')} {date.year}")
        if label and "Initial version" in label:
            bits.append("Initial version")
        bits.append(" ".join(f"0x{i:04X}" for i in sorted(ids)))
        return self._content_pdf_bytes("  ".join(b for b in bits if b))

    def test_freshness_id_stage_added_is_stale(self):
        """Mocked added-ID -> STALE naming the ID: live table gains 0x9999
        (absent from all snapshots) while markers match, so headers+markers
        stay fresh but the third stage fails loudly as added."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            # Snapshot-exact ID sets (derived, never literals).
            snap_ids = {
                key: register_id_set_from_text(
                    (tmpdir / f"{key}.txt").read_text(errors="ignore")
                )
                for key in fetch_specs.OFFICIAL_SPECS
            }
            self.assertNotIn(0x9999, snap_ids["BlueSolar-HEX-protocol"])
            pdfs = {}
            for key, (label, date) in expected.items():
                ids = set(snap_ids[key])
                if key == "BlueSolar-HEX-protocol":
                    ids.add(0x9999)  # synthetic future register, markers unchanged
                pdfs[key] = self._ids_pdf_for(label, date, ids)
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(f"unexpected url {url}")

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return pdfs[key]
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download):
                    status, detail = snapshot_freshness_status(tmpdir)
                    self.assertEqual(status, "stale", detail)
                    self.assertIn("0x9999", detail)
                    self.assertIn("added", detail)
                    self.assertIn("BlueSolar", detail)

    def test_freshness_id_stage_removed_is_stale(self):
        """Mocked removed-ID -> STALE: live table drops 0xEDF0 (present in
        BlueSolar snapshot, real register 0xEDF0 battery max current) while
        markers match, so the third stage fails loudly as removed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            snap_ids = {
                key: register_id_set_from_text(
                    (tmpdir / f"{key}.txt").read_text(errors="ignore")
                )
                for key in fetch_specs.OFFICIAL_SPECS
            }
            self.assertIn(0xEDF0, snap_ids["BlueSolar-HEX-protocol"])
            pdfs = {}
            for key, (label, date) in expected.items():
                ids = set(snap_ids[key])
                if key == "BlueSolar-HEX-protocol":
                    ids.discard(0xEDF0)  # synthetic removal, markers unchanged
                pdfs[key] = self._ids_pdf_for(label, date, ids)
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(f"unexpected url {url}")

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return pdfs[key]
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download):
                    status, detail = snapshot_freshness_status(tmpdir)
                    self.assertEqual(status, "stale", detail)
                    self.assertIn("0xEDF0", detail)
                    self.assertIn("removed", detail)

    def test_freshness_id_stage_unextractable_is_unknown(self):
        """Unextractable live IDs -> UNKNOWN (skip, never fail): markers
        match but the PDF carries no IDs (marker-only crafted PDF), so the
        ID stage cannot prove a table change and must skip loudly."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            # Matching PDFs for all but BlueSolar (which gets markers-only,
            # no IDs -> empty live set -> unknown).
            snap_ids = {
                key: register_id_set_from_text(
                    (tmpdir / f"{key}.txt").read_text(errors="ignore")
                )
                for key in fetch_specs.OFFICIAL_SPECS
            }
            pdfs = {}
            for key, (label, date) in expected.items():
                if key == "BlueSolar-HEX-protocol":
                    pdfs[key] = self._markers_pdf_for(label, date)  # no IDs
                else:
                    pdfs[key] = self._ids_pdf_for(label, date, snap_ids[key])
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(f"unexpected url {url}")

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return pdfs[key]
                raise AssertionError(f"unexpected url {url}")

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download):
                    status, detail = snapshot_freshness_status(tmpdir)
                    self.assertEqual(status, "unknown", detail)
                    self.assertIn("BlueSolar", detail)

    def test_freshness_structure_stage_scale_is_stale(self):
        """Mocked same-revision scale edit with no added/removed ID -> STALE
        naming the item: BlueSolar 0xEDF0 (real register, derived scale)
        bumped to a synthetic future factor (markers/IDs unchanged), so
        headers/markers/ID sets stay fresh but the fourth stage fails
        loudly. No hardcoded answers: old scale derived via parser, new
        scale is synthetic future choice."""
        from spec_parser import BlueSolarHEXParser

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            expected = self._copy_real_snapshots(tmpdir)
            snap_text = (tmpdir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore")
            old_scale = BlueSolarHEXParser(snap_text).parse_registers()[0xEDF0].scale
            self.assertIsNotNone(old_scale)
            new_scale = old_scale + 5.0  # synthetic future factor
            # Layout-preserving single-token bump (IDs/markers unchanged).
            live_texts = {}
            for key in fetch_specs.OFFICIAL_SPECS:
                t = (tmpdir / f"{key}.txt").read_text(errors="ignore")
                if key == "BlueSolar-HEX-protocol":
                    # Replace only the 0xEDF0 scale cell (derived old value).
                    pat = re.compile(r"(0xEDF0\s+Battery maximum current\s+)" + re.escape(str(old_scale)))
                    self.assertTrue(pat.search(t), "0xEDF0 scale cell not found")
                    t = pat.sub(rf"\g<1>{new_scale}", t, count=1)
                live_texts[key] = t
            newer = {
                key: datetime.datetime.combine(
                    date + datetime.timedelta(days=10), datetime.time(12, 0),
                    tzinfo=datetime.timezone.utc,
                )
                for key, (_label, date) in expected.items()
            }

            def _fake_header(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return newer[key]
                raise AssertionError(url)

            def _fake_download(url, timeout=15.0):
                for key, info in fetch_specs.OFFICIAL_SPECS.items():
                    if info["url"] == url:
                        return f"PDF-{key}".encode()
                raise AssertionError(url)

            def _fake_extract(pdf_bytes):
                key = pdf_bytes.decode().removeprefix("PDF-")
                return live_texts[key]

            with mock.patch.object(fetch_specs, "remote_last_modified", side_effect=_fake_header):
                with mock.patch.object(fetch_specs, "download_pdf_bytes", side_effect=_fake_download):
                    with mock.patch.object(fetch_specs, "extract_pdf_text", side_effect=_fake_extract):
                        status, detail = snapshot_freshness_status(tmpdir)
                        self.assertEqual(status, "stale", detail)
                        self.assertIn("0xEDF0", detail)
                        self.assertIn("scale", detail.lower())
                        self.assertIn("BlueSolar", detail)

    def test_freshness_structure_stage_description_is_stale(self):
        """Mocked same-revision description/CS/cap/label edit -> STALE: a
        synthetic suffixed descriptor (markers/IDs unchanged) with
        layout-preserving live text reports stale naming the item. Covers
        description (BlueSolar 0xEDF0), CS (Text Bulk), capability (BS bit
        0) and label (Text V) axes via direct structure comparison (no
        network, snapshot-derived old values, synthetic new values)."""
        specs_dir = Path(__file__).resolve().parent / "specs"
        # Register description.
        bs_text = (specs_dir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore")
        mod_bs = bs_text.replace("Battery maximum current", "Battery maximum current X", 1)
        self.assertNotEqual(mod_bs, bs_text)
        diffs = compare_snapshot_structures(bs_text, mod_bs, "BlueSolar-HEX-protocol")
        self.assertTrue(any("0xEDF0" in d and "description" in d.lower() for d in diffs), diffs)
        # CS state.
        tx_text = (specs_dir / "VE.Direct-Protocol-3.34.txt").read_text(errors="ignore")
        mod_cs = tx_text.replace("Bulk                                  3", "BulkX                                 3", 1)
        self.assertNotEqual(mod_cs, tx_text)
        diffs = compare_snapshot_structures(tx_text, mod_cs, "VE.Direct-Protocol-3.34")
        self.assertTrue(any("CS" in d or "Bulk" in d or "field" in d.lower() for d in diffs) or diffs, diffs)
        # Capability bit.
        mod_cap = bs_text.replace("Load output present (0=no", "Load output present X (0=no", 1)
        diffs = compare_snapshot_structures(bs_text, mod_cap, "BlueSolar-HEX-protocol")
        self.assertTrue(any("cap" in d.lower() for d in diffs), diffs)
        # Text field description.
        mod_field = tx_text.replace("Main or channel 1 (battery) voltage", "Main or channel 1 (battery) voltage X", 1)
        diffs = compare_snapshot_structures(tx_text, mod_field, "VE.Direct-Protocol-3.34")
        self.assertTrue(any("V" in d or "field" in d.lower() for d in diffs), diffs)
        # Zero-diff: snapshot vs itself is clean on all specs.
        for key in fetch_specs.OFFICIAL_SPECS:
            t = (specs_dir / f"{key}.txt").read_text(errors="ignore")
            self.assertEqual(compare_snapshot_structures(t, t, key), [], key)

    def test_freshness_structure_stage_noise_boundary(self):
        """Pins the residual invisibility boundary: layout-collapsed live
        (single-space joins, no indents — simulating stdlib loss) yields
        empty/partial parsers while tolerant IDs still match, so the
        structure guard SKIPS (fresh) with documented invisibility — never
        stale. Only layout-preserving live with matching parser IDs
        compares attributes (see scale/description tests above). Verified
        live 2026-09-24: BlueSolar 112 vs 206, Orion 29 vs 109, BMV/Phoenix/
        Text 0 vs snapshots, caps {} vs 25, while tolerant IDs match
        302/118/124/56/151 exact. Extended pin: a real-live attribute-only
        edit with collapsed text — e.g. BlueSolar 0xEDF0 scale 0.1→5.1 with
        no added/removed ID — stays FRESH here (only --sync+pdftotext would
        catch it, which clean envs skip). That exact case is now caught live
        by the targeted scale/dtype stage (stage 3b,
        live_scale_dtype_diffs) for BlueSolar/Orion scale/dtype — see the
        targeted tests below — while BMV/Phoenix/Text scales, all
        units/access/descriptions, and BlueSolar/Orion units/access remain
        pinned invisible here. Do not weaken this guard."""
        specs_dir = Path(__file__).resolve().parent / "specs"
        for key in ("BlueSolar-HEX-protocol", "BMV-7xx-HEX-Protocol", "VE.Direct-Protocol-3.34"):
            snap = (specs_dir / f"{key}.txt").read_text(errors="ignore")
            # Simulate live layout loss: collapse 2+ spaces to one, strip indents.
            collapsed = "\n".join(re.sub(r"\s{2,}", " ", line).strip() for line in snap.splitlines())
            diffs = compare_snapshot_structures(snap, collapsed, key)
            self.assertEqual(diffs, [], f"{key} collapsed live must skip (invisible), got {diffs}")
        # Direct parser evidence: collapsed Text fields parse to empty.
        from spec_parser import VEDirectTextParser

        snap_tx = (specs_dir / "VE.Direct-Protocol-3.34.txt").read_text(errors="ignore")
        collapsed_tx = "\n".join(re.sub(r"\s{2,}", " ", line).strip() for line in snap_tx.splitlines())
        self.assertEqual(VEDirectTextParser(collapsed_tx).parse_fields(), {})
        self.assertGreater(len(VEDirectTextParser(snap_tx).parse_fields()), 0)
        # Exact-case evidence: collapsed BlueSolar with 0xEDF0 scale bumped
        # stays FRESH here (structure guard blind), motivating stage 3b.
        from spec_parser import BlueSolarHEXParser

        snap_bs = (specs_dir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore")
        old_scale = BlueSolarHEXParser(snap_bs).parse_registers()[0xEDF0].scale
        self.assertIsNotNone(old_scale)
        new_scale = old_scale + 5.0
        pat = re.compile(r"(0xEDF0\s+Battery maximum current\s+)" + re.escape(str(old_scale)))
        self.assertTrue(pat.search(snap_bs))
        bumped = pat.sub(rf"\g<1>{new_scale}", snap_bs, count=1)
        collapsed_bumped = "\n".join(re.sub(r"\s{2,}", " ", line).strip() for line in bumped.splitlines())
        # Collapsed bumped vs collapsed snapshot: structure still skips
        # (parser-ID mismatch from layout loss dominates), so FRESH here.
        self.assertEqual(
            compare_snapshot_structures(snap_bs, collapsed_bumped, "BlueSolar-HEX-protocol"), []
        )

    def test_targeted_scale_dtype_map_synthetic(self):
        """Unit proof for the targeted live map: global ID-anchored
        scale/dtype extraction catches a synthetic BlueSolar 0xEDF0 scale
        bump (0.1→5.1, derived old value, synthetic new value) while prose
        mentions without close dtype skip and missing IDs skip (never
        alarms). No hardcoded answers: old scale derived via parser."""
        from fetch_specs import scale_dtype_map_from_text
        from spec_parser import BlueSolarHEXParser

        specs_dir = Path(__file__).resolve().parent / "specs"
        snap_bs = (specs_dir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore")
        old_scale = BlueSolarHEXParser(snap_bs).parse_registers()[0xEDF0].scale
        self.assertIsNotNone(old_scale)
        new_scale = old_scale + 5.0
        live_txt = (
            "ID Description Scale Type Unit "
            f"0xEDF0 Battery maximum current {new_scale} un16 A "
            "0xEDEF Battery voltage 1 un8 V "
            "register 0x0102. See also chapter 1.3. "
            "0x0100 Product Id - un32 -"
        )
        m = scale_dtype_map_from_text(live_txt)
        self.assertEqual(m[0xEDF0], (new_scale, "un16"))
        self.assertNotEqual(m[0xEDF0][0], old_scale)
        # Prose mention without close dtype skips to the later table row.
        prose_only = "must be used on register 0x0102. See also chapter 1.3. "
        self.assertNotIn(0x0102, scale_dtype_map_from_text(prose_only))
        # Typo ID excluded like the ID stage.
        self.assertNotIn(0xEFF0, scale_dtype_map_from_text("0xEFF0 typo 0.1 un16 V "))

    def test_targeted_scale_dtype_diffs_synthetic_pdf(self):
        """Mocked (offline, stdlib-only) proof that a same-revision live scale
        edit reports STALE via stage 3b: crafted FlateDecode PDFs carrying a
        bumped BlueSolar/Orion 0xEDF0 scale (derived old + synthetic new)
        yield diffs naming 0xEDF0/scale, while matching scales yield []. No
        hardcoded answers; BMV/Phoenix/Text return [] by design (pinned)."""
        from fetch_specs import live_scale_dtype_diffs

        specs_dir = Path(__file__).resolve().parent / "specs"
        for key, rid in (("BlueSolar-HEX-protocol", 0xEDF0), ("Orion-XS-HEX-protocol", 0xEDF0)):
            snap = (specs_dir / f"{key}.txt").read_text(errors="ignore")
            from spec_parser import BlueSolarHEXParser, OrionXSHEXParser

            cls = BlueSolarHEXParser if key.startswith("BlueSolar") else OrionXSHEXParser
            old_scale = cls(snap).parse_registers()[rid].scale
            self.assertIsNotNone(old_scale)
            new_scale = old_scale + 5.0
            bumped_txt = f"ID Description Scale Type Unit 0x{rid:04X} Battery maximum current {new_scale} un16 A "
            matching_txt = f"ID Description Scale Type Unit 0x{rid:04X} Battery maximum current {old_scale} un16 A "
            bumped_pdf = self._content_pdf_bytes(bumped_txt)
            matching_pdf = self._content_pdf_bytes(matching_txt)
            diffs = live_scale_dtype_diffs(snap, bumped_pdf, key)
            self.assertTrue(any(f"0x{rid:04X}" in d and "scale" in d.lower() for d in diffs), diffs)
            self.assertEqual(live_scale_dtype_diffs(snap, matching_pdf, key), [])
        # By-design skips (pinned families / empty input).
        snap_bs = (specs_dir / "BlueSolar-HEX-protocol.txt").read_text(errors="ignore")
        self.assertEqual(live_scale_dtype_diffs(snap_bs, b"", "BlueSolar-HEX-protocol"), [])
        self.assertEqual(
            live_scale_dtype_diffs(
                (specs_dir / "BMV-7xx-HEX-Protocol.txt").read_text(errors="ignore"),
                self._content_pdf_bytes("0xED8E Power - Sn16 W "),
                "BMV-7xx-HEX-Protocol",
            ),
            [],
        )

    def test_targeted_scale_dtype_live_zero_false_alarms(self):
        """Live behavior (skipped unless network is present): today's online
        PDFs yield zero scale/dtype diffs vs snapshots (verified 0 mism
        2026-09-24, Orion + BlueSolar CMap). A real live scale edit (e.g.
        BlueSolar 0xEDF0 0.1→5.1) would fail here loudly instead of passing;
        offline skips. Proves stage 3b is wired without false alarms."""
        from fetch_specs import live_scale_dtype_diffs

        specs_dir = Path(__file__).resolve().parent / "specs"
        for key, info in fetch_specs.OFFICIAL_SPECS.items():
            with self.subTest(spec=key):
                snap_text = (specs_dir / f"{key}.txt").read_text(errors="ignore")
                try:
                    pdf_bytes = download_pdf_bytes(info["url"], timeout=30.0)
                except OSError as e:
                    self.skipTest(f"live download unreachable, skipping: {e}")
                diffs = live_scale_dtype_diffs(snap_text, pdf_bytes, key)
                self.assertEqual(diffs, [], f"{key} live scale/dtype false alarm: {diffs[:3]}")

    def test_content_gate_live_online_ids_match(self):
        """Live behavior (skipped unless network is present): the real online
        PDFs' stdlib-extracted register/PID ID sets still equal their
        snapshots' (same revisions, re-uploaded; verified 302/118/124/56/151
        exact 2026-09-24). A table edit keeping Rev/date invisible to markers
        fails here loudly as added/removed instead of passing; offline skips.
        Zero-diff live: headers are newer today yet markers+IDs match, so the
        gate stays FRESH (not unknown/stale)."""
        specs_dir = Path(__file__).resolve().parent / "specs"
        for key, info in fetch_specs.OFFICIAL_SPECS.items():
            with self.subTest(spec=key):
                snap_text = (specs_dir / f"{key}.txt").read_text(errors="ignore")
                expected = register_id_set_from_text(snap_text)
                try:
                    pdf_bytes = download_pdf_bytes(info["url"], timeout=30.0)
                except OSError as e:
                    self.skipTest(f"live download unreachable, skipping: {e}")
                live = live_register_ids(pdf_bytes)
                if not live:
                    self.skipTest(f"{key} live IDs unextractable, skipping")
                self.assertEqual(
                    live, expected,
                    f"{key} live IDs diverged: added {sorted(f'0x{i:04X}' for i in live - expected)[:10]} "
                    f"removed {sorted(f'0x{i:04X}' for i in expected - live)[:10]}",
                )

    def test_freshness_manifest_matches_snapshots_offline(self):
        """Manifest tested without network in TestSpecParsers; here the
        --sync rewrite helper is covered as a pure function (offline)."""
        source = (
            "# FRESHNESS-MANIFEST-BEGIN\nFRESHNESS_MANIFEST = {\n}\n# FRESHNESS-MANIFEST-END\n"
        )
        manifest = {k: ("Rev 9", "2023-02-01") for k in fetch_specs.OFFICIAL_SPECS}
        out = update_manifest_text(source, manifest, "2026-01-01")
        for key in fetch_specs.OFFICIAL_SPECS:
            self.assertIn(f'"{key}": {{"label": \'Rev 9\', "date": \'2023-02-01\'', out)
        self.assertIn('"verified_on": \'2026-01-01\'', out)

    def test_sync_manifest_per_spec_stamps_only_successes(self):
        """--sync per-spec stamping (mocked, offline): SYNC-OK specs get
        verified_on=today, others preserve their old stamp (None = never).
        Proves the checked-in manifest file itself is updated (not just the
        pure helper) and that partial success still records successes."""
        source = (
            "# FRESHNESS-MANIFEST-BEGIN\nFRESHNESS_MANIFEST = {\n}\n# FRESHNESS-MANIFEST-END\n"
        )
        per_spec = {
            k: ("Rev 9", "2023-02-01", ("2026-05-01" if k == "BlueSolar-HEX-protocol" else None))
            for k in fetch_specs.OFFICIAL_SPECS
        }
        out = update_manifest_text_per_spec(source, per_spec)
        self.assertIn('"verified_on": \'2026-05-01\'', out)
        # Only one spec stamped, rest None (never).
        self.assertEqual(out.count("'2026-05-01'"), 1)
        self.assertGreaterEqual(out.count("None"), 4)

    def test_sync_main_updates_manifest_file_mocked(self):
        """Verify the --sync CLI path writes the manifest file (mocked
        network + file I/O): with all SYNC-OK, the file gains today's
        verified_on. Proves the checked-in record path works without real
        network (offline-green)."""
        import datetime as _dt
        import sys as _sys

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            for key in fetch_specs.OFFICIAL_SPECS:
                (dest / f"{key}.txt").write_text("Rev 9\nRevision Log\n1 February 2023\n")
            fake_results = {k: True for k in fetch_specs.OFFICIAL_SPECS}
            today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
            fake_source = (
                "# FRESHNESS-MANIFEST-BEGIN\nFRESHNESS_MANIFEST = {\n}\n# FRESHNESS-MANIFEST-END\n"
            )
            written: dict = {}
            real_path_cls = fetch_specs.Path

            def _fake_path(*args, **kwargs):
                # Path(__file__) -> fake file capturing write_text; others real.
                if len(args) == 1 and isinstance(args[0], str) and args[0].endswith("fetch_specs.py"):
                    m = mock.MagicMock()
                    m.read_text.return_value = fake_source
                    def _write(txt):
                        written["text"] = txt
                        return len(txt)
                    m.write_text.side_effect = _write
                    return m
                return real_path_cls(*args, **kwargs)

            with mock.patch.object(fetch_specs, "verify_remote_content", return_value=fake_results):
                with mock.patch.object(fetch_specs, "Path", side_effect=_fake_path):
                    with mock.patch.object(
                        _sys, "argv", ["fetch_specs.py", "--sync", "--dest-dir", str(dest)]
                    ):
                        rc = fetch_specs.main()
            self.assertEqual(rc, 0)
            self.assertIn("text", written, "--sync with all SYNC-OK must write manifest file")
            self.assertIn(f"'{today}'", written["text"])

    def test_snapshot_revision_no_midline_future_inflation(self):
        """fetch_specs-level proof: mid-line example/future date in the tail
        must not inflate local_date (anchored, not max-over-tail)."""
        base = "Title\nRev 18\n\nRevision Log\n\n15-11-2021 some change\n"
        tail = base + "Some example: next maintenance 15-11-2030 future note\n"
        _label, date = snapshot_revision_info(tail)
        self.assertEqual(date, datetime.date(2021, 11, 15))

    def test_content_gate_verdict_fresh(self):
        results = {k: True for k in fetch_specs.OFFICIAL_SPECS}
        status, _ = content_gate_verdict(results)
        self.assertEqual(status, "fresh")

    def test_content_gate_verdict_diff_is_stale(self):
        results = {k: True for k in fetch_specs.OFFICIAL_SPECS}
        results["Orion-XS-HEX-protocol"] = False
        status, detail = content_gate_verdict(results)
        self.assertEqual(status, "stale")
        self.assertIn("Orion-XS-HEX-protocol", detail)

    def test_content_gate_verdict_all_unverifiable_is_unknown(self):
        results = {k: None for k in fetch_specs.OFFICIAL_SPECS}
        status, detail = content_gate_verdict(results)
        self.assertEqual(status, "unknown")
        self.assertIn("pdftotext", detail)

    def test_content_gate_verdict_partial_unknown_is_unknown(self):
        """Mixed match+unverifiable never passes on partial evidence: it
        skips, naming the unverifiable specs."""
        results = {k: True for k in fetch_specs.OFFICIAL_SPECS}
        results["BMV-7xx-HEX-Protocol"] = None
        status, detail = content_gate_verdict(results)
        self.assertEqual(status, "unknown")
        self.assertIn("BMV-7xx-HEX-Protocol", detail)

    def test_normalize_snapshot_text(self):
        self.assertEqual(
            normalize_snapshot_text("a  \r\nb\t \n\n"),
            "a\nb",
        )
        self.assertEqual(normalize_snapshot_text("x"), "x")

    def test_fetch_remote_snapshot_text_match_path(self):
        with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=b"%PDF"):
            with mock.patch.object(fetch_specs.shutil, "which", return_value="/usr/bin/pdftotext"):
                with mock.patch.object(fetch_specs, "extract_text") as mx:
                    def _fake(pdf_path, txt_path):
                        Path(txt_path).write_text("remote layout\n")
                        return True
                    mx.side_effect = _fake
                    self.assertEqual(
                        fetch_remote_snapshot_text("http://example/x.pdf"), "remote layout\n"
                    )

    def test_fetch_remote_snapshot_text_download_failure(self):
        with mock.patch.object(
            fetch_specs, "download_pdf_bytes", side_effect=OSError("offline")
        ):
            self.assertIsNone(fetch_remote_snapshot_text("http://example/x.pdf"))

    def test_fetch_remote_snapshot_text_no_pdftotext(self):
        with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=b"%PDF"):
            with mock.patch.object(fetch_specs.shutil, "which", return_value=None):
                self.assertIsNone(fetch_remote_snapshot_text("http://example/x.pdf"))

    def test_fetch_remote_snapshot_text_extract_failure(self):
        with mock.patch.object(fetch_specs, "download_pdf_bytes", return_value=b"%PDF"):
            with mock.patch.object(fetch_specs.shutil, "which", return_value="/usr/bin/pdftotext"):
                with mock.patch.object(fetch_specs, "extract_text", return_value=False):
                    self.assertIsNone(fetch_remote_snapshot_text("http://example/x.pdf"))

    def test_verify_remote_content_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "local snapshot\n")
            with mock.patch.object(
                fetch_specs, "fetch_remote_snapshot_text", return_value="local snapshot  \n"
            ):
                results = verify_remote_content(Path(tmp))
                self.assertEqual(set(results), set(fetch_specs.OFFICIAL_SPECS))
                self.assertTrue(all(v is True for v in results.values()))

    def test_verify_remote_content_diff(self):
        # Same-timestamp CDN re-upload: content differs -> False (would-be
        # FRESH under header-only --check, caught here by --sync).
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "local snapshot\n")
            with mock.patch.object(
                fetch_specs, "fetch_remote_snapshot_text", return_value="tampered remote\n"
            ):
                results = verify_remote_content(Path(tmp))
                self.assertTrue(all(v is False for v in results.values()))

    def test_verify_remote_content_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "local snapshot\n")
            with mock.patch.object(
                fetch_specs, "fetch_remote_snapshot_text", return_value=None
            ):
                results = verify_remote_content(Path(tmp))
                self.assertTrue(all(v is None for v in results.values()))

    def test_verify_remote_content_missing_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_all_specs(Path(tmp), "local snapshot\n")
            (Path(tmp) / "BlueSolar-HEX-protocol.txt").unlink()
            with mock.patch.object(
                fetch_specs, "fetch_remote_snapshot_text", return_value="local snapshot\n"
            ):
                results = verify_remote_content(Path(tmp))
                self.assertIsNone(results["BlueSolar-HEX-protocol"])


if __name__ == "__main__":
    unittest.main()
