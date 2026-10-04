"""Description coverage and standalone generator output."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from ve_direct.descriptions import HEX_DESCRIPTIONS, STREAM_DESCRIPTIONS
from ve_direct.registers import (
    EXPERIMENTAL_MULTI_RS_REGISTERS, REGISTERS, _FLAVOR_EXPAND, for_flavor,
)
from ve_direct.tables import LABEL_INFO
from generate_descriptions import DEFAULT_OUTPUT, main, render_module


class TestDescriptions(unittest.TestCase):
    def test_every_known_label_and_register_has_a_description(self):
        self.assertEqual(set(STREAM_DESCRIPTIONS), set(LABEL_INFO))
        self.assertEqual(set(HEX_DESCRIPTIONS), set(_FLAVOR_EXPAND))
        self.assertEqual(set().union(*(set(d) for d in HEX_DESCRIPTIONS.values())),
                         set(REGISTERS))
        for label, info in LABEL_INFO.items():
            self.assertEqual(STREAM_DESCRIPTIONS[label], info["desc"])
        for flavor, mapping in HEX_DESCRIPTIONS.items():
            self.assertEqual(set(mapping), set(for_flavor(flavor)))
            for name, description in mapping.items():
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip(), name)
        self.assertEqual(HEX_DESCRIPTIONS["MPPT"]["PANEL_VOLTAGE"], "Panel voltage")
        self.assertEqual(HEX_DESCRIPTIONS["MPPT"]["BAT_TYPE"], "Battery type")
        # Both interpretations of 0xEDBB must survive generation.
        self.assertIn("PANEL_VOLTAGE", HEX_DESCRIPTIONS["MPPT"])
        self.assertIn("INPUT_VOLTAGE", HEX_DESCRIPTIONS["ORION"])
        self.assertIn("DAILY_HISTORY_30", HEX_DESCRIPTIONS["MPPT"])
        for name in EXPERIMENTAL_MULTI_RS_REGISTERS:
            self.assertIn("experimental", HEX_DESCRIPTIONS["MULTI_RS"][name])

    def test_flavors_retain_shared_and_inherited_entries(self):
        for mapping in HEX_DESCRIPTIONS.values():
            self.assertEqual(mapping["PRODUCT_ID"], "Product ID")
        for child, parent in (("BMV71", "BMV"), ("MPPT_SMALL", "MPPT"),
                              ("MPPT_RS", "MPPT"), ("MPPT", "CHG"),
                              ("ORION", "CHG"), ("MULTI_RS", "MPPT_RS"),
                              ("MULTI_RS", "INV")):
            self.assertTrue(HEX_DESCRIPTIONS[parent].items() <= HEX_DESCRIPTIONS[child].items())
            self.assertIsNot(HEX_DESCRIPTIONS[parent], HEX_DESCRIPTIONS[child])
        self.assertNotIn("PANEL_VOLTAGE", HEX_DESCRIPTIONS["BMV"])
        self.assertNotIn("BMV_BATTERY_CAPACITY", HEX_DESCRIPTIONS["MPPT"])

    def test_checked_in_module_is_current(self):
        self.assertEqual(DEFAULT_OUTPUT.read_text(encoding="utf-8"), render_module())
        self.assertEqual(main(["--check"]), 0)

    def test_generator_produces_standalone_importable_dictionaries(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "labels.py"
            with mock.patch("builtins.print"):
                self.assertEqual(main(["-o", str(output)]), 0)
            # Output executes without importing the library or any other module.
            with mock.patch("builtins.__import__", side_effect=AssertionError("Unexpected import")):
                namespace = {}
                exec(compile(output.read_text(encoding="utf-8"), str(output), "exec"), namespace)
            self.assertEqual(namespace["STREAM_DESCRIPTIONS"], STREAM_DESCRIPTIONS)
            self.assertEqual(namespace["HEX_DESCRIPTIONS"], HEX_DESCRIPTIONS)
            output.write_text("# stale\n", encoding="utf-8")
            with mock.patch("builtins.print"):
                self.assertEqual(main(["--check", "-o", str(output)]), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "# stale\n")
