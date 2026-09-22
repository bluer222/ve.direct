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

import re
import sys
import unittest
from pathlib import Path

# Make `python3 -m unittest discover -s tests` work in a bare checkout without
# PYTHONPATH (other test modules rely on import-order side effects for this).
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "tools"))

from spec_parser import load_all_specs
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
    # + 31 daily-history (0x1050..0x106E) + 31 MPPT-history (0x10A0..0x10BE).
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


def scrape_doc_ids(text: str) -> set[int]:
    """Independent crude ID collector: every 0xHHHH occurrence plus ".."
    range expansion. Deliberately dumber than the parser so parser drops
    show up as scraped-but-unparsed IDs. The span guard here is
    intentionally far more permissive than the parser's 64-register
    `_expand_id_range` cap (a shared cap would hide the same blind spot on
    both sides, e.g. a 0x1000..0x10FF span missed by both): only absurd
    spans such as the BMV Range-column bound 0x0000..0xFFFF are excluded."""
    ids = {int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{4})\b", text)}
    for m in re.finditer(r"0x([0-9A-Fa-f]{4})\s*\.\.\s*0x([0-9A-Fa-f]{4})", text):
        a, b = int(m.group(1), 16), int(m.group(2), 16)
        if 0 < b - a <= 1024:
            ids.update(range(a, b + 1))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*0x[0-9A-Fa-f]{4}\s*\.\.\.\s*$", line):
            a = int(re.match(r"^\s*0x([0-9A-Fa-f]{4})", line).group(1), 16)  # type: ignore[union-attr]
            for nxt in lines[i + 1 : i + 4]:
                m2 = re.match(r"^\s*(0x[0-9A-Fa-f]{4})\s*$", nxt)
                if m2:
                    b = int(m2.group(1), 16)
                    if 0 < b - a <= 1024:
                        ids.update(range(a, b + 1))
                    break
    return ids


# Scraped IDs that are not registers (verified against the snapshots):
# example values, version/fault illustrations, sentinels, product IDs from
# the PID tables, and the documented 0xEFF0 typo (means 0xEDF0).
NON_REGISTER_IDS = {
    # BlueSolar: 0x0064/0x0096 message-example values; 0x0101 firmware-version
    # illustration ("0x0101 is version 1.01", no such register); 0x0300 and
    # 0xA040..0xA117 are product IDs here (0x0300 is a BMV history register,
    # not a BlueSolar HEX register); 0x4116 app-version example; 0xD101
    # release-candidate illustration; 0x7FFF/0xFFFF reserved/sentinel values;
    # 0xAAAA framing error; 0xEFF0 typo for 0xEDF0.
    "bluesolar": frozenset({0x0064, 0x0096, 0x0101, 0x0300, 0x4116, 0x7FFF, 0xAAAA, 0xD101, 0xEFF0, 0xFFFF})
    | frozenset(range(0xA040, 0xA118)),
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
    | frozenset(range(0x10A0, 0x10BF))  # MPPT history structs (doc table runs to 0x10BE)
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
    # no factor), 50/60Hz select, eco retry (note-garbled "0.02/" scale cell),
    # NVM command, dynamic-cutoff factor/enable cells without factors,
    # settings-changed timestamp, restore-defaults.
    "phoenix": frozenset({
        0x0004, 0x0090, 0x0100, 0x0101, 0x0102, 0x010A, 0x010B, 0x0140, 0x0150,
        0x0200, 0x0201, 0x0207, 0x031C, 0x031E, 0x034E, 0x034F,
        0xEB03, 0xEB10, 0xEB99, 0xEBB2, 0xEBB3, 0xEBB5, 0xEBB7, 0xEBBA, 0xEC41,
    }),
}

# Registers whose doc genuinely gives no single wire width: struct records
# (byte-field layouts), write-only commands (Type "-"), firmware-dependent
# widths (0xEDD1/0xEDD3 "(*4)": un32 <= v1.12, un16 above; parser normalizes
# the footnote cell to unknown, provenance in notes), and Orion detail-only
# IDs (Value tables / prose without widths).
DTYPE_UNKNOWN_ALLOW = {
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

        for doc_name, reg_dict, query_flavor, allowed_flavors in specs_to_test:
            key = doc_name.lower()
            scale_allow = SCALE_NONE_ALLOW[key]
            dtype_allow = DTYPE_UNKNOWN_ALLOW[key]
            for rid, spec_reg in reg_dict.items():
                # Scale must be extracted wherever the doc prints a factor.
                if spec_reg.scale is None and rid not in scale_allow:
                    missing_scales.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name})")
                # Dtype must be known wherever the doc prints a width.
                if expected_dtype(spec_reg.data_type) is None and rid not in dtype_allow:
                    missing_dtypes.append(
                        f"{doc_name} 0x{rid:04X} ({spec_reg.name}): {spec_reg.data_type!r}"
                    )
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
                    if reg.scale is None or abs(reg.scale - spec_reg.scale) > 1e-5:
                        actual = reg.scale if reg else "NOT_FOUND"
                        scale_mismatches.append(f"{doc_name} 0x{rid:04X} ({spec_reg.name}): expected scale {spec_reg.scale}, got {actual}")

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

        self.assertEqual(missing_scales, [], f"Documented scales not extracted: {missing_scales}")
        self.assertEqual(missing_dtypes, [], f"Documented dtypes not extracted: {missing_dtypes}")
        self.assertEqual(missing_registers, [], f"Missing spec registers: {missing_registers}")
        self.assertEqual(inaccessible, [], f"Inaccessible registers for device flavor: {inaccessible}")
        self.assertEqual(scale_mismatches, [], f"Scale factor mismatches: {scale_mismatches}")
        self.assertEqual(dtype_mismatches, [], f"Data type mismatches: {dtype_mismatches}")

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

    def test_text_protocol_spec(self):
        """Verify all parsed Text mode fields and product IDs from Text Protocol 3.34 exist in code."""
        missing_fields = [f.label for f in self.text_fields.values() if f.label not in T.LABEL_INFO]
        self.assertEqual(missing_fields, [], f"Parsed text fields missing from LABEL_INFO: {missing_fields}")

        missing_pids = [f"0x{pid:04X} ({p.model_name})" for pid, p in self.pids.items() if pid not in T.PID_MAP]
        self.assertEqual(missing_pids, [], f"Parsed PIDs missing from PID_MAP: {missing_pids}")

        # Doc totals against the snapshot (not just parser-vs-code). The indent
        # window here is intentionally wider than the parser's ^\s{10,25} so
        # a shared-indent blind spot (e.g. an 8-space label row missed by
        # both) fails loudly as a parser-vs-counter mismatch instead.
        text = self.raw_texts["text"]
        start = text.find("The units used by the serial interface are as follows:")
        end = text.find("Table I Supported Text-mode fields")
        section = text[start:end]
        stoplist = {
            "Label", "Table", "The", "Note", "Victron", "General", "E-mail",
            "When", "Both", "Available", "60x",
        }
        doc_fields = {
            m.group(1)
            for line in section.splitlines()
            for m in [re.match(r"^\s{4,40}([A-Za-z0-9_#]+)\s{2,}", line)]
            if m and m.group(1) not in stoplist
        }
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

        # The phased-out BlueSolar MPPT 70|15 row must be present and flagged.
        self.assertIn(0x0300, self.pids)
        self.assertTrue(self.pids[0x0300].phased_out, "0x0300 must be flagged phased-out")

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

    def test_cs_states_full_coverage(self):
        """parse_cs_states must cover the whole section incl. 11/245-248/252."""
        states = self.specs["text"].parse_cs_states()
        self.assertEqual(states, EXPECTED_CS)

    def test_error_codes_full_coverage(self):
        """parse_errors must cover the whole section incl. 66/67/68/116/117/119."""
        errors = self.specs["text"].parse_errors()
        self.assertEqual(errors, EXPECTED_ERRORS)

    def test_capabilities_against_doc(self):
        """Capability bits asserted against the doc maps (incl. Orion 28-31)."""
        self.assertEqual(self.specs["bluesolar"].parse_capabilities(), EXPECTED_CAPS_BS)
        self.assertEqual(self.specs["orion"].parse_capabilities(), EXPECTED_CAPS_ORION)
        # BlueSolar doc genuinely omits bits 22-24 (jumps 21 -> 25).
        self.assertNotIn(22, self.specs["bluesolar"].parse_capabilities())
        self.assertNotIn(23, self.specs["bluesolar"].parse_capabilities())
        self.assertNotIn(24, self.specs["bluesolar"].parse_capabilities())

    def test_phoenix_ble_header_typo(self):
        """The §2.2.5 'Capabilities BLE (register 0x0160)' heading is a doc
        typo for 0x0150: parser records it as evidence, drops no register."""
        evidence = self.specs["phoenix"].parse_ble_header_evidence()
        self.assertEqual(set(evidence), {0x0160})
        self.assertNotIn(0x0160, self.ph_regs)
        self.assertIn(0x0150, self.ph_regs)
        self.assertEqual(self.ph_regs[0x0150].name, "VE_REG_CAPABILITIES_BLE")

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
        mismatches = []
        for rid, spec_reg in self.ph_regs.items():
            expected = access_map.get(spec_reg.access)
            if expected is None:
                continue
            actual = R.lookup(rid, flavor="INV").access
            if actual != expected and actual != "CONSTANT":
                mismatches.append(
                    f"0x{rid:04X}: expected {expected}, got {actual}")
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


if __name__ == "__main__":
    unittest.main()
