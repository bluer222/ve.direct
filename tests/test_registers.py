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
        self.assertNotIn(250, R.LINK_DEVICE_STATE_MAP)
        self.assertEqual(R.DEVICE_STATE_MAP[0xF5], "wake_up")
        self.assertEqual(R.RELAY_MODE_MAP[1], "charger")

    def test_bmv71_flavor(self):
        self.assertIn("BMV71", R.lookup("DC_MONITOR_MODE").flavors)
        # BMV-712 v4.07+ monitor register is visible in the BMV view too.
        self.assertIn("BMV", R.lookup("DC_MONITOR_MODE").flavors)
        self.assertIn("DC_MONITOR_MODE", R.for_flavor("BMV"))
        self.assertIn("DC_MONITOR_MODE", R.for_flavor("BMV71"))
        self.assertEqual(R.lookup(0xEEB8, flavor="BMV").name, "DC_MONITOR_MODE")

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

    # --- phantom registers 0x0209/0x020A (absent from all snapshots) ---
    def test_phantom_registers_removed(self):
        from ve_direct.exceptions import AmbiguousRegisterError  # noqa: F401
        self.assertNotIn("INVERTER_DEVICE_STATE", R.REGISTERS)
        self.assertNotIn("CHARGER_DEVICE_STATE", R.REGISTERS)
        with self.assertRaises(KeyError):
            R.lookup(0x0209)
        with self.assertRaises(KeyError):
            R.lookup(0x020A)
        with self.assertRaises(KeyError):
            R.lookup("INVERTER_DEVICE_STATE")
        with self.assertRaises(KeyError):
            R.lookup("CHARGER_DEVICE_STATE")

    # --- BMV relay mode 0x034F: Un8 0..2 (DFLT, CHRG, REM) ---
    def test_bmv_relay_mode_override(self):
        bmv = R.lookup(0x034F, flavor="BMV")
        self.assertEqual(bmv.name, "BMV_RELAY_MODE")
        self.assertEqual(R.decode_value(bmv, b"\x00"), "default")
        self.assertEqual(R.decode_value(bmv, b"\x01"), "charger")
        self.assertEqual(R.decode_value(bmv, b"\x02"), "remote")
        self.assertEqual(R.decode_value(bmv, b"\x03"), "unknown(3)")
        self.assertEqual(R.encode_value(bmv, "default"), b"\x00")
        with self.assertRaises(ValueError):
            R.encode_value(bmv, "warnings")  # Phoenix-family name rejected
        with self.assertRaises(ValueError):
            R.encode_value(bmv, "fan")  # undocumented name rejected
        # Raw ints pass through unwidened, like every other ENUM register;
        # decoding them back surfaces unknown(n) instead of a wrong name.
        # Named lookup with flavor resolves to the BMV override as well.
        self.assertEqual(R.lookup("RELAY_MODE", flavor="BMV").name, "BMV_RELAY_MODE")
        # MPPT keeps the shared superset map (unchanged).
        mppt = R.lookup(0x034F, flavor="MPPT")
        self.assertEqual(mppt.name, "RELAY_MODE")
        self.assertEqual(R.decode_value(mppt, b"\x00"), "warnings")

    def test_inv_relay_mode_no_value_1(self):
        inv = R.lookup(0x034F, flavor="INV")
        self.assertEqual(inv.name, "INV_RELAY_MODE")
        self.assertEqual(inv.access, "WRITE_ONLY")
        self.assertEqual(R.decode_value(inv, b"\x00"), "warnings")
        self.assertEqual(R.decode_value(inv, b"\x04"), "normal")
        # Phoenix Rev 5 §2.8.1 defines no value 1.
        self.assertEqual(R.decode_value(inv, b"\x01"), "unknown(1)")
        with self.assertRaises(ValueError):
            R.encode_value(inv, "charger")

    # --- Orion/Inv device state + mode overrides ---
    def test_orion_device_state_override(self):
        st = R.lookup(0x0201, flavor="ORION")
        self.assertEqual(st.name, "ORION_DEVICE_STATE")
        self.assertEqual(R.decode_value(st, b"\x00"), "not_charging")
        self.assertEqual(R.decode_value(st, b"\x0b"), "power_supply")
        self.assertEqual(R.decode_value(st, bytes([245])), "wake_up")
        self.assertEqual(R.decode_value(st, bytes([255])), "unavailable")
        self.assertEqual(R.decode_value(st, b"\x03"), "bulk")
        # Generic map is unchanged for other families.
        self.assertEqual(R.decode_value(R.lookup("DEVICE_STATE"), b"\x00"), "off")
        self.assertEqual(R.decode_value(R.lookup("DEVICE_STATE"), bytes([245])), "wake_up")

    def test_orion_device_mode_override(self):
        mo = R.lookup(0x0200, flavor="ORION")
        self.assertEqual(mo.name, "ORION_DEVICE_MODE")
        self.assertEqual(R.decode_value(mo, b"\x00"), "off")
        self.assertEqual(R.decode_value(mo, b"\x04"), "off")
        self.assertEqual(R.decode_value(mo, b"\x01"), "on")
        self.assertEqual(R.decode_value(mo, b"\x03"), "on")
        self.assertEqual(R.encode_value(mo, 0), b"\x00")
        self.assertEqual(R.encode_value(mo, 4), b"\x04")

    def test_inv_device_state_mode_overrides(self):
        st = R.lookup(0x0201, flavor="INV")
        self.assertEqual(st.name, "INV_DEVICE_STATE")
        self.assertEqual(R.decode_value(st, b"\x09"), "inverting")
        # Generic charger states never occur on Phoenix (Rev 5 §2.3.1).
        self.assertEqual(R.decode_value(st, b"\x03"), "unknown(3)")
        mo = R.lookup(0x0200, flavor="INV")
        self.assertEqual(mo.name, "INV_DEVICE_MODE")
        self.assertEqual(R.decode_value(mo, b"\x02"), "inverter_on")
        self.assertEqual(R.decode_value(mo, b"\x05"), "eco")
        self.assertEqual(R.decode_value(mo, bytes([0xFD])), "hibernate")
        self.assertEqual(R.decode_value(mo, b"\x00"), "unknown(0)")

    def test_inv_warning_reason_override(self):
        w = R.lookup(0x031C, flavor="INV")
        self.assertEqual(w.name, "INV_WARNING_REASON")
        self.assertEqual(R.decode_value(w, b"\x01\x00"), ["low_battery_voltage"])
        # Bits undocumented for Phoenix (Rev 5 §2.3.3) stay bitN.
        self.assertEqual(R.decode_value(w, b"\x04\x00"), ["bit2"])
        self.assertEqual(R.decode_value(w, b"\x80\x00"), ["bit7"])
        a = R.lookup(0x031E, flavor="INV")
        self.assertEqual(a.name, "INV_ALARM_REASON")
        # Generic map unchanged.
        self.assertEqual(R.decode_value(R.lookup("WARNING_REASON"), b"\x04\x00"), ["low_soc"])

    # --- BMV alarm acknowledge is get+set ---
    def test_bmv_alarm_acknowledge_access(self):
        reg = R.lookup("BMV_ALARM_ACKNOWLEDGE")
        self.assertEqual(reg.id, 0x031F)
        self.assertEqual(reg.access, "READ_WRITE")

    # --- MPPT off-reason bits: 7..8 reserved, 9 battery-temp-low ---
    def test_mppt_off_reason_bits(self):
        bits = R.OFF_REASON_MPPT_BITS
        self.assertNotIn(7, bits)
        self.assertNotIn(8, bits)
        self.assertIn(9, bits)
        reg = R.lookup("DEVICE_OFF_REASON_2", flavor="MPPT")
        self.assertEqual(R.decode_value(reg, b"\x80\x00\x00\x00"), ["bit7"])
        self.assertEqual(
            R.decode_value(reg, b"\x00\x02\x00\x00"), [bits[9]])

    # --- NETWORK_INFO bit 8 (doc lists it under un8: undecodable) ---
    def test_network_info_bit8_falls_back(self):
        reg = R.lookup("NETWORK_INFO")
        self.assertEqual(reg.dtype, "uint8_t")
        self.assertNotIn(8, R.ENUM_MAPS["NETWORK_INFO"])
        self.assertEqual(R.decode_value(reg, b"\x01"), ["bms_controlled"])
        # A hypothetical bit-8 payload still surfaces instead of vanishing.
        self.assertEqual(
            R.decode_value(reg, b"\xff"),
            ["bms_controlled", "vset_remote", "charge_slave", "charge_master",
             "using_icharge", "using_isense", "using_tsense", "using_vsense"])

    # --- Orion total charge current 0x2013 ---
    def test_total_charge_current_orion(self):
        reg = R.lookup(0x2013, flavor="ORION")
        self.assertEqual(reg.name, "TOTAL_CHARGE_CURRENT")
        self.assertIn("TOTAL_CHARGE_CURRENT", R.for_flavor("ORION"))
        self.assertAlmostEqual(
            R.decode_value(reg, struct.pack("<i", 12345)), 12.345)

    # --- charger error 19 (disabled since fw 1.15, still decodes) ---
    def test_chr_error_19(self):
        reg = R.lookup("CHR_ERROR_CODE")
        self.assertEqual(R.decode_value(reg, b"\x13"), "charger_current_polarity_reversed")
        self.assertEqual(R.encode_value(reg, "charger_current_polarity_reversed"), b"\x13")

    # --- BMV SW version AA.BB ---
    def test_bmv_sw_version_aa_bb(self):
        reg = R.lookup("BMV_SW_VERSION")
        d = R.decode_value(reg, bytes.fromhex("0704"))
        self.assertEqual(d["raw"], 0x0407)
        self.assertEqual(d["version"], "4.07")
        self.assertEqual(R.encode_value(reg, 0x0407), bytes.fromhex("0704"))
        self.assertEqual(R.encode_value(reg, {"raw": 0x0407}), bytes.fromhex("0704"))
        self.assertEqual(R.encode_value(reg, "4.07"), bytes.fromhex("0704"))

    # --- Un8 0..1 (OFF,ON) settings stay BOOLEAN: wire 0/1 round-trip ---
    def test_boolean_wire_values_roundtrip(self):
        for name in ["ALARM_BUZZER", "BMV_RELAY_INVERT", "BMV_BACKLIGHT_ALWAYS_ON",
                     "BMV_SHOW_VOLTAGE", "BMV_SHOW_POWER", "BMV_SETUP_LOCK",
                     "BMV_START_SYNCHRONIZED", "RELAY_CONTROL", "INV_DYN_CUTOFF_ENABLE"]:
            with self.subTest(name=name):
                reg = R.lookup(name)
                self.assertEqual(reg.cls, "BOOLEAN")
                self.assertEqual(R.encode_value(reg, 0), b"\x00")
                self.assertEqual(R.encode_value(reg, 1), b"\x01")
                self.assertEqual(R.encode_value(reg, False), b"\x00")
                self.assertEqual(R.encode_value(reg, True), b"\x01")
                self.assertEqual(R.decode_value(reg, b"\x00"), 0)  # False == 0
                self.assertEqual(R.decode_value(reg, b"\x01"), 1)  # True == 1
                self.assertIs(R.decode_value(reg, b"\x00"), False)
                self.assertIs(R.decode_value(reg, b"\x01"), True)

    # --- doc-aligned access labels (no behavior change) ---
    def test_doc_aligned_access(self):
        self.assertEqual(R.lookup("CAPABILITIES_BLE").access, "READ_ONLY")
        self.assertEqual(R.lookup("VOLTAGE_RANGE_MIN").access, "READ_ONLY")
        self.assertEqual(R.lookup("VOLTAGE_RANGE_MAX").access, "READ_ONLY")

    def test_bmv_temperature_coefficient_unit(self):
        self.assertEqual(R.lookup("BMV_TEMPERATURE_COEFFICIENT").unit, "%CAP/degC")

    # --- ADJUSTABLE_VOLTAGE_MIN/MAX visible under MPPT (BlueSolar-HEX §1.1
    # Charger-data Note 6: fw 1.30, no RS-only marker) ---
    def test_adjustable_voltage_mppt_flavor(self):
        for name in ("ADJUSTABLE_VOLTAGE_MIN", "ADJUSTABLE_VOLTAGE_MAX"):
            reg = R.lookup(name)
            self.assertIn("MPPT", reg.flavors)
            self.assertIn("MPPT_RS", reg.flavors)
        mppt = R.for_flavor("MPPT")
        self.assertIn("ADJUSTABLE_VOLTAGE_MIN", mppt)
        self.assertIn("ADJUSTABLE_VOLTAGE_MAX", mppt)
        self.assertIn("ADJUSTABLE_VOLTAGE_MIN", R.for_flavor("MPPT_RS"))
        self.assertEqual(R.lookup(0x2211, flavor="MPPT").name, "ADJUSTABLE_VOLTAGE_MIN")
        self.assertEqual(R.lookup(0x2211, flavor="INV").name, "VOLTAGE_RANGE_MIN")

    # --- shared 0x0201 state 245 is WAKE-UP; INV override unaffected ---
    def test_device_state_245_wake_up(self):
        self.assertEqual(R.DEVICE_STATE_MAP[0xF5], "wake_up")
        self.assertEqual(
            R.decode_value(R.lookup("DEVICE_STATE"), bytes([245])), "wake_up")
        # Orion override agrees; Phoenix defines only 0,1,2,9 so 245 stays unknown.
        self.assertEqual(
            R.decode_value(R.lookup(0x0201, flavor="ORION"), bytes([245])), "wake_up")
        self.assertEqual(
            R.decode_value(R.lookup(0x0201, flavor="INV"), bytes([245])), "unknown(245)")

    # --- 0x200C LINK map has no 250 (absent from both 0x200C tables) ---
    def test_link_device_state_no_250(self):
        self.assertNotIn(250, R.LINK_DEVICE_STATE_MAP)
        link = R.lookup("LINK_DEVICE_STATE")
        self.assertEqual(R.decode_value(link, bytes([250])), "unknown(250)")
        # 250 BLOCKED survives only in the 0x0201 maps.
        self.assertEqual(R.DEVICE_STATE_MAP[250], "blocked")
        self.assertEqual(R.DEVICE_STATE_ORION_MAP[250], "blocked")

    # --- BAT_TYPE small-charger limitation (no small-charger flavor) ---
    def test_bat_type_small_charger_limitation(self):
        # Helper decodes small models correctly...
        self.assertEqual(R.decode_bat_type(1, "small"), "type_2")
        self.assertEqual(R.decode_bat_type(1, "large"), "type_1")
        # ...but the generic register binds the large table, so value 1 on a
        # 10/15/20A model reads type_1 via hex_get (documented limitation).
        self.assertEqual(R.decode_value(R.lookup("BAT_TYPE"), b"\x01"), "type_1")
        self.assertEqual(R.decode_value(R.lookup("BAT_TYPE"), b"\xFF"), "user")

    # --- 0x0400 semantic collision (BlueSolar ENUM vs BMV BOOLEAN) ---
    def test_0400_flavor_disambiguation(self):
        from ve_direct.exceptions import AmbiguousRegisterError
        # BMV flavor resolves to exactly one meaning (BOOLEAN).
        bmv = R.lookup(0x0400, flavor="BMV")
        self.assertEqual(bmv.name, "BMV_BACKLIGHT_ALWAYS_ON")
        # Named lookup with BMV flavor re-resolves by ID to the BMV register.
        self.assertEqual(
            R.lookup("DISPLAY_BACKLIGHT_MODE", flavor="BMV").name,
            "BMV_BACKLIGHT_ALWAYS_ON",
        )
        self.assertNotIn("DISPLAY_BACKLIGHT_MODE", R.for_flavor("BMV"))
        self.assertIn("BMV_BACKLIGHT_ALWAYS_ON", R.for_flavor("BMV"))
        # MPPT flavor resolves to the mode enum (BlueSolar-HEX 0=keypress/1=on/2=auto).
        mppt = R.lookup(0x0400, flavor="MPPT")
        self.assertEqual(mppt.name, "DISPLAY_BACKLIGHT_MODE")
        self.assertEqual(R.decode_value(mppt, b"\x01"), "on")
        self.assertIn("DISPLAY_BACKLIGHT_MODE", R.for_flavor("MPPT"))
        self.assertIn("DISPLAY_BACKLIGHT_MODE", R.for_flavor("MPPT_RS"))
        # Unflavored int lookup stays ambiguous.
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0x0400)

    # --- BMV temp 0=disabled (0x032A-032D/0x035A-035D, valid 174..372 K) ---
    def test_bmv_temp_zero_disabled(self):
        for name in [
            "BMV_ALARM_LOW_TEMPERATURE",
            "BMV_ALARM_LOW_TEMPERATURE_CLEAR",
            "BMV_ALARM_HIGH_TEMPERATURE",
            "BMV_ALARM_HIGH_TEMPERATURE_CLEAR",
            "BMV_RELAY_LOW_TEMPERATURE",
            "BMV_RELAY_LOW_TEMPERATURE_CLEAR",
            "BMV_RELAY_HIGH_TEMPERATURE",
            "BMV_RELAY_HIGH_TEMPERATURE_CLEAR",
        ]:
            with self.subTest(name=name):
                reg = R.lookup(name)
                self.assertIsNone(R.decode_value(reg, bytes.fromhex("0000")))
                self.assertEqual(
                    R.encode_value(reg, None), bytes.fromhex("0000"))
                # Round-trip disabled state.
                self.assertIsNone(
                    R.decode_value(reg, R.encode_value(reg, None)))
                # Nonzero behavior unchanged (174 K = 17400 raw).
                import struct as _s
                raw = _s.pack("<H", 17400)
                self.assertAlmostEqual(R.decode_value(reg, raw), 174.0)
                self.assertEqual(R.encode_value(reg, 174.0), raw)


if __name__ == "__main__":
    unittest.main()
