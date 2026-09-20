import struct
import sys
import unittest

sys.path.insert(0, "src")

from ve_direct import registers as R


class TestRegisters(unittest.TestCase):
    def test_lookup_by_name_and_id(self):
        from ve_direct.exceptions import AmbiguousRegisterError
        self.assertEqual(R.lookup("PANEL_VOLTAGE").id, 0xEDBB)
        # Unambiguous ID lookup by int:
        self.assertEqual(R.lookup(0xEDD5).name, "CHARGER_VOLTAGE")
        # Aliased ID requires flavor to disambiguate:
        self.assertEqual(R.lookup(0xEDBB, flavor="MPPT").name, "PANEL_VOLTAGE")
        self.assertEqual(R.lookup(0xEDBB, flavor="ORION").name, "INPUT_VOLTAGE")
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0xEDBB)
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0xED8E)
        with self.assertRaises(KeyError):
            R.lookup("NOPE_NOPE")
        with self.assertRaises(KeyError):
            R.lookup(0xFFFF)

    def test_numeric_roundtrip(self):
        reg = R.lookup("PANEL_VOLTAGE")  # uint16 V x0.01
        raw = R.encode_value(reg, 31.3)
        self.assertEqual(raw, struct.pack("<H", 3130))
        self.assertAlmostEqual(R.decode_value(reg, raw), 31.3)
        # Product revision is identifier byte + LE uint16 revision.
        rev = R.lookup("PRODUCT_REVISION")
        self.assertEqual(
            R.decode_value(rev, bytes.fromhex("003412"))["revision"],
            0x1234,
        )

    def test_enum_roundtrip(self):
        reg = R.lookup("DEVICE_STATE")
        self.assertEqual(R.decode_value(reg, b"\x03"), "bulk")
        self.assertEqual(R.encode_value(reg, "bulk"), b"\x03")
        # Signed 16-bit enum (BMV-712 DC_MONITOR_MODE)
        mon = R.lookup("DC_MONITOR_MODE")
        self.assertEqual(R.decode_value(mon, bytes.fromhex("F7FF")), "Solar charger")
        self.assertEqual(R.encode_value(mon, "Solar charger"), bytes.fromhex("f7ff"))
        self.assertEqual(R.encode_value(mon, -9), bytes.fromhex("f7ff"))

    def test_boolean(self):
        reg = R.lookup("LOAD_OUTPUT_STATE")
        self.assertTrue(R.decode_value(reg, b"\x01"))
        self.assertEqual(R.encode_value(reg, True), b"\x01")

    def test_signed(self):
        reg = R.lookup("DC_CHANNEL1_CURRENT")  # int16 A x0.1
        raw = R.encode_value(reg, -0.4)
        self.assertAlmostEqual(R.decode_value(reg, raw), -0.4, places=6)

    def test_bitmask_names(self):
        reg = R.lookup("DEVICE_OFF_REASON_2", flavor="MPPT")
        self.assertEqual(R.decode_value(reg, b"\x01\x00\x00\x00"), ["no_input_power"])
        orion = R.lookup("DEVICE_OFF_REASON_2", flavor="ORION")
        self.assertEqual(
            R.decode_value(orion, b"\x00\x01\x00\x00"), ["error"])
        inv = R.lookup("INV_DEVICE_OFF_REASON")
        self.assertEqual(
            R.decode_value(inv, b"\x01\x00\x00\x00"), ["no_input_power"])
        self.assertEqual(
            R.decode_value(inv, b"\x30\x00\x00\x00"),
            ["internal_reason", "paygo"])
        self.assertEqual(R.decode_value(inv, b"\x02\x00\x00\x00"), ["bit1"])
        reg = R.lookup("WARNING_REASON")
        self.assertEqual(R.decode_value(reg, b"\x05\x00"),
                         ["low_battery_voltage", "low_soc"])

    def test_full_error_map(self):
        m = R.ENUM_MAPS["CHR_ERROR_CODE"]
        self.assertEqual(m[57], "inverter_ac")
        self.assertEqual(m[114], "cpu_temperature_high")
        self.assertGreater(len(m), 50)
        self.assertEqual(R.DEVICE_STATE_MAP[250], "blocked")
        self.assertEqual(R.LINK_DEVICE_STATE_MAP[250], "blocked")
        self.assertEqual(R.RELAY_MODE_MAP[1], "charger")

    def test_bmv71_flavor(self):
        self.assertIn("BMV71", R.lookup("DC_MONITOR_MODE").flavors)
        self.assertNotIn("DC_MONITOR_MODE", R.for_flavor("BMV"))

    def test_ble_mode_is_scalar_enum(self):
        reg = R.lookup("BLE_MODE")
        self.assertEqual(R.decode_value(reg, b"\x00"), "off")
        self.assertEqual(R.decode_value(reg, b"\x01"), "on")

    def test_flavors(self):
        mppt = R.for_flavor("MPPT")
        self.assertIn("PANEL_VOLTAGE", mppt)
        self.assertIn("DEVICE_STATE", mppt)  # ANY always included
        self.assertNotIn("AC_OUT_VOLTAGE", mppt)
        self.assertIn("AC_OUT_VOLTAGE", R.for_flavor("INV"))

    def test_count(self):
        self.assertGreater(len(R.REGISTERS), 90)


if __name__ == "__main__":
    unittest.main()
