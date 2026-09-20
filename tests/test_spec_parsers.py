"""
Specification validation tests for VE.Direct protocol.

Validates library implementation against official Victron Energy documents:
  - BlueSolar-HEX-protocol (Rev 18)
  - Orion-XS-HEX-protocol
  - BMV-7xx-HEX-Protocol (Rev 7)
  - VE.Direct-HEX-Protocol-Phoenix-Inverter (Rev 5)
  - VE.Direct-Protocol-3.34 (Text mode & Product IDs)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add tools to path for spec parser
tools_dir = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(tools_dir))

from spec_parser import load_all_specs
import ve_direct.registers as R
from ve_direct.exceptions import AmbiguousRegisterError
import ve_direct.tables as T


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

    def test_py_typed_exists(self):
        """Verify PEP 561 py.typed marker is present."""
        pkg_dir = Path(__file__).resolve().parent.parent / "src" / "ve_direct"
        py_typed = pkg_dir / "py.typed"
        self.assertTrue(py_typed.is_file(), f"py.typed marker missing at {py_typed}")

    def test_spec_parser_counts(self):
        """Verify each parser extracts the expected non-zero counts."""
        self.assertGreaterEqual(len(self.bs_regs), 115)
        self.assertGreaterEqual(len(self.or_regs), 50)
        self.assertGreaterEqual(len(self.bmv_regs), 105)
        self.assertGreaterEqual(len(self.ph_regs), 45)
        self.assertGreaterEqual(len(self.text_fields), 40)
        self.assertGreaterEqual(len(self.pids), 100)

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

        for doc_name, reg_dict, query_flavor, allowed_flavors in specs_to_test:
            for rid, spec_reg in reg_dict.items():
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
                expected_dtype = type_map.get(spec_reg.data_type.lower())
                if expected_dtype:
                    actual_dtype = reg.dtype or (
                        "string" if reg.cls == "STRING"
                        else "uint8_t" if reg.cls == "BOOLEAN"
                        else ""
                    )
                    if actual_dtype != expected_dtype:
                        dtype_mismatches.append(f"{doc_name} 0x{rid:04X} ({reg.name}): expected dtype {expected_dtype}, got {reg.dtype!r}")

        self.assertEqual(missing_registers, [], f"Missing spec registers: {missing_registers}")
        self.assertEqual(inaccessible, [], f"Inaccessible registers for device flavor: {inaccessible}")
        self.assertEqual(scale_mismatches, [], f"Scale factor mismatches: {scale_mismatches}")
        self.assertEqual(dtype_mismatches, [], f"Data type mismatches: {dtype_mismatches}")

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
