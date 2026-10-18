import struct
import sys
import unittest

sys.path.insert(0, "src")

from ve_direct import registers as R


class TestRegisters(unittest.TestCase):
    def test_shared_charger_units_preserve_wire_values(self):
        cases = [
            ("TAIL_CURRENT", "A", [(0, b"\x00\x00"), (1.5, b"\x0f\x00"), (6553.5, b"\xff\xff")]),
            ("AUTOMATIC_EQUALISATION_MODE", "days", [(0, b"\x00"), (2, b"\x02"), (250, b"\xfa")]),
        ]
        for flavor in ("CHG", "MPPT", "MPPT_SMALL", "MPPT_RS", "ORION"):
            for name, unit, values in cases:
                with self.subTest(flavor=flavor, name=name):
                    reg = R.lookup(name, flavor=flavor)
                    self.assertEqual(reg.unit, unit)
                    for value, raw in values:
                        self.assertEqual(R.encode_value(reg, value), raw)
                        self.assertAlmostEqual(R.decode_value(reg, raw), value)
        self.assertEqual(R.lookup("BMV_TAIL_CURRENT", flavor="BMV").unit, "%")

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
        # Both colliding IDs must reject an unflavored lookup.
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

    def test_bmv71_flavor(self):
        # BMV-712 v4.07+ monitor register is visible in the BMV view too.
        self.assertIn("BMV", R.lookup("DC_MONITOR_MODE").flavors)
        self.assertIn("BMV71", R.lookup("DC_MONITOR_MODE").flavors)
        self.assertIn("DC_MONITOR_MODE", R.for_flavor("BMV"))
        self.assertIn("DC_MONITOR_MODE", R.for_flavor("BMV71"))
        self.assertEqual(R.lookup(0xEEB8, flavor="BMV").name, "DC_MONITOR_MODE")

    def test_ble_mode_is_scalar_enum(self):
        reg = R.lookup("BLE_MODE")
        self.assertEqual(R.decode_value(reg, b"\x00"), "off")
        self.assertEqual(R.decode_value(reg, b"\x01"), "on")

    # --- phantom registers 0x0209/0x020A (absent from all snapshots) ---
    def test_phantom_registers_removed(self):
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
        # Includes the relay entry checked by former test_full_error_map.
        self.assertEqual(R.RELAY_MODE_MAP[1], "charger")
        self.assertEqual(R.decode_value(bmv, b"\x02"), "remote")
        self.assertEqual(R.decode_value(bmv, b"\x03"), "unknown(3)")
        self.assertEqual(R.encode_value(bmv, "default"), b"\x00")
        with self.assertRaises(ValueError):
            R.encode_value(bmv, "warnings")  # Phoenix-family name rejected
        with self.assertRaises(ValueError):
            R.encode_value(bmv, "fan")  # undocumented name rejected
        # Named lookup with flavor resolves to the BMV override as well.
        self.assertEqual(R.lookup("BMV_RELAY_MODE", flavor="BMV").name, "BMV_RELAY_MODE")
        self.assertEqual(R.lookup(0x034F, flavor="BMV").name, "BMV_RELAY_MODE")
        # No generic MPPT 0x034F: no official doc gives 0x034F to MPPT
        # (BlueSolar-HEX relay § lists only 0xEDD9/0x0350-0x0353/0xEDBA/
        # 0xEDB9/0x100A; 0x034F is Phoenix Rev 5 §2.8.1 + BMV Rev 7).
        with self.assertRaises(KeyError):
            R.lookup(0x034F, flavor="MPPT")
        self.assertNotIn("RELAY_MODE", R.for_flavor("MPPT"))
        self.assertNotIn("RELAY_MODE", R.REGISTERS)
        # INV/BMV overrides survive independently.
        self.assertEqual(R.lookup(0x034F, flavor="INV").name, "INV_RELAY_MODE")
        self.assertIn("INV_RELAY_MODE", R.for_flavor("INV"))

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
        # Includes former test_device_state_0_not_charging and
        # test_device_state_245_wake_up: generic, MPPT and Orion state values.
        st = R.lookup(0x0201, flavor="ORION")
        self.assertEqual(st.name, "ORION_DEVICE_STATE")
        self.assertEqual(R.decode_value(st, b"\x00"), "not_charging")
        self.assertEqual(R.decode_value(st, b"\x0b"), "power_supply")
        self.assertEqual(R.decode_value(st, bytes([245])), "wake_up")
        self.assertEqual(R.decode_value(st, bytes([255])), "unavailable")
        self.assertEqual(R.decode_value(st, b"\x03"), "bulk")
        # Generic map follows BlueSolar-HEX §1.1 (0 = NOT_CHARGING).
        self.assertEqual(R.decode_value(R.lookup("DEVICE_STATE"), b"\x00"), "not_charging")
        self.assertEqual(R.decode_value(R.lookup("DEVICE_STATE"), bytes([245])), "wake_up")
        self.assertEqual(R.decode_value(R.lookup(0x0201, flavor="MPPT"), b"\x00"),
                         "not_charging")
        self.assertEqual(R.DEVICE_STATE_MAP[0], "not_charging")
        # Includes the 245 entry checked by former test_full_error_map.
        self.assertEqual(R.DEVICE_STATE_MAP[0xF5], "wake_up")

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
        # Former test_device_state_0_not_charging and
        # test_device_state_245_wake_up also checked the Phoenix exceptions.
        st = R.lookup(0x0201, flavor="INV")
        self.assertEqual(st.name, "INV_DEVICE_STATE")
        self.assertEqual(R.decode_value(st, b"\x00"), "off")
        self.assertEqual(R.DEVICE_STATE_INV_MAP[0], "off")
        self.assertEqual(R.decode_value(st, b"\x09"), "inverting")
        # Generic charger states never occur on Phoenix (Rev 5 §2.3.1).
        self.assertEqual(R.decode_value(st, b"\x03"), "unknown(3)")
        self.assertEqual(R.decode_value(st, bytes([245])), "unknown(245)")
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
        self.assertEqual(R.lookup(0x031E, flavor="INV").name, "INV_ALARM_REASON")
        # Generic map unchanged.
        self.assertEqual(R.decode_value(R.lookup("WARNING_REASON"), b"\x04\x00"), ["low_soc"])

    # --- BMV alarm acknowledge is get+set ---
    def test_bmv_alarm_acknowledge_access(self):
        reg = R.lookup("BMV_ALARM_ACKNOWLEDGE")
        self.assertEqual(reg.id, 0x031F)
        self.assertEqual(reg.access, "READ_WRITE")

    # --- MPPT off-reason bits: 7..8 reserved, 9 battery-temp-low ---
    def test_mppt_off_reason_bits(self):
        # Keep both the map entries and their decoded wire behavior.
        bits = R.OFF_REASON_MPPT_BITS
        self.assertNotIn(7, bits)
        self.assertNotIn(8, bits)
        self.assertEqual(bits[9], "battery_temperature")
        reg = R.lookup("DEVICE_OFF_REASON_2", flavor="MPPT")
        self.assertEqual(R.decode_value(reg, b"\x80\x00\x00\x00"), ["bit7"])
        self.assertEqual(
            R.decode_value(reg, b"\x00\x01\x00\x00"), ["bit8"])
        self.assertEqual(
            R.decode_value(reg, b"\x00\x02\x00\x00"), ["battery_temperature"])

    # --- NETWORK_INFO is an 8-bit mask ---
    def test_network_info_named_bits(self):
        # Includes former test_network_info_bit8_falls_back: an 8-bit
        # NETWORK_INFO register has no named bit 8 and decodes bit 0.
        reg = R.lookup("NETWORK_INFO")
        self.assertEqual(reg.dtype, "uint8_t")
        self.assertNotIn(8, R.ENUM_MAPS["NETWORK_INFO"])
        self.assertEqual(R.decode_value(reg, b"\x01"), ["bms_controlled"])
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

    # --- charger error names, including 19 (disabled since fw 1.15) ---
    def test_charger_error_names(self):
        # Includes the charger entries and map size from former
        # test_full_error_map, plus former test_chr_error_19's wire round-trip.
        reg = R.lookup("CHR_ERROR_CODE")
        error_map = R.ENUM_MAPS["CHR_ERROR_CODE"]
        self.assertEqual(error_map[57], "inverter_ac")
        self.assertEqual(error_map[114], "cpu_temperature_high")
        self.assertGreater(len(error_map), 50)
        self.assertEqual(R.decode_value(reg, b"\x13"), "charger_current_polarity_reversed")
        self.assertEqual(R.encode_value(reg, "charger_current_polarity_reversed"), b"\x13")
        self.assertEqual(R.decode_value(reg, b"\x39"), "inverter_ac")
        self.assertEqual(R.decode_value(reg, b"\x72"), "cpu_temperature_high")

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
    def test_boolean_register_wire_values(self):
        # Includes former test_boolean_wire_values_roundtrip for every
        # listed Boolean register, including both wire values and Python bools.
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
                self.assertIs(R.decode_value(reg, b"\x00"), False)
                self.assertIs(R.decode_value(reg, b"\x01"), True)

    def test_register_metadata(self):
        # Includes former test_count's register catalog size check.
        self.assertGreater(len(R.REGISTERS), 90)
        # Includes former test_doc_aligned_access for all three read-only entries.
        for name in ("CAPABILITIES_BLE", "VOLTAGE_RANGE_MIN", "VOLTAGE_RANGE_MAX"):
            with self.subTest(name=name):
                self.assertEqual(R.lookup(name).access, "READ_ONLY")
        # Includes former test_bmv_temperature_coefficient_unit.
        self.assertEqual(R.lookup("BMV_TEMPERATURE_COEFFICIENT").unit, "%CAP/degC")

    # --- ADJUSTABLE_VOLTAGE_MIN/MAX visible under MPPT (BlueSolar-HEX §1.1
    # Charger-data Note 6: fw 1.30, no RS-only marker) ---
    def test_adjustable_voltage_mppt_flavor(self):
        # Includes former test_flavors: MPPT has panel voltage and the ANY
        # device state, excludes the INV AC voltage, and INV includes it.
        mppt = R.for_flavor("MPPT")
        self.assertIn("PANEL_VOLTAGE", mppt)
        self.assertIn("DEVICE_STATE", mppt)
        self.assertNotIn("AC_OUT_VOLTAGE", mppt)
        self.assertIn("AC_OUT_VOLTAGE", R.for_flavor("INV"))
        for name in ("ADJUSTABLE_VOLTAGE_MIN", "ADJUSTABLE_VOLTAGE_MAX"):
            reg = R.lookup(name)
            self.assertIn("MPPT", reg.flavors)
            self.assertIn("MPPT_RS", reg.flavors)
        self.assertIn("ADJUSTABLE_VOLTAGE_MIN", mppt)
        self.assertIn("ADJUSTABLE_VOLTAGE_MAX", mppt)
        self.assertIn("ADJUSTABLE_VOLTAGE_MIN", R.for_flavor("MPPT_RS"))
        self.assertEqual(R.lookup(0x2211, flavor="MPPT").name, "ADJUSTABLE_VOLTAGE_MIN")
        self.assertEqual(R.lookup(0x2211, flavor="INV").name, "VOLTAGE_RANGE_MIN")

    def test_bat_type_generic_uses_large_charger_map(self):
        # The generic register retains the large-model table. Small chargers
        # select MPPT_SMALL, or use the standalone decoding helper.
        self.assertEqual(R.decode_bat_type(1, "small"), "type_2")
        self.assertEqual(R.decode_bat_type(1, "large"), "type_1")
        self.assertEqual(R.decode_value(R.lookup("BAT_TYPE"), b"\x01"), "type_1")
        self.assertEqual(R.decode_value(R.lookup("BAT_TYPE"), b"\xFF"), "user")

    def test_mppt_small_battery_type_and_inherited_registers(self):
        small = R.lookup(0xEDF1, flavor="MPPT_SMALL")
        self.assertEqual(small.name, "MPPT_SMALL_BAT_TYPE")
        self.assertEqual(R.lookup("BAT_TYPE", flavor="mppt_small"), small)
        self.assertEqual(R.decode_value(small, b"\x01"), "type_2")
        self.assertEqual(R.encode_value(small, "type_2"), b"\x01")
        self.assertEqual(R.decode_value(small, b"\xFF"), "user")
        self.assertEqual(R.encode_value(small, "user"), b"\xFF")
        self.assertEqual(R.decode_value(small, b"\x02"), "unknown(2)")
        with self.assertRaises(ValueError):
            R.encode_value(small, "type_1")
        # The small variant still exposes inherited MPPT, CHG and ANY data.
        visible = R.for_flavor("MPPT_SMALL")
        for name in ("MPPT_SMALL_BAT_TYPE", "PANEL_VOLTAGE",
                     "BAT_ABSORPTION_VOLTAGE", "PRODUCT_ID"):
            self.assertIn(name, visible)
            R.lookup(name, flavor="MPPT_SMALL")
        self.assertEqual(R.decode_value(R.lookup(0xEDF1, flavor="MPPT"), b"\x01"), "type_1")
        self.assertEqual(R.decode_value(R.lookup(0xEDF1, flavor="MPPT_RS"), b"\x01"), "agm_normal")
        self.assertEqual(R.decode_value(R.lookup(0xEDF1, flavor="ORION"), b"\x01"), "lifepo4")

    def test_bmv_description_string20_wire_limit(self):
        reg = R.lookup("BMV_DESCRIPTION", flavor="BMV71")
        self.assertEqual(reg.max_length, 20)
        self.assertEqual(R.encode_value(reg, ""), b"\0")
        self.assertEqual(R.encode_value(reg, "x" * 19), b"x" * 19 + b"\0")
        for value in ("x" * 20, "x" * 21):
            with self.subTest(characters=len(value)):
                with self.assertRaisesRegex(ValueError, "20 ASCII bytes"):
                    R.encode_value(reg, value)
        # Strings are ASCII on this interface, so a multibyte Unicode value
        # cannot bypass the wire-byte bound or get silently truncated.
        with self.assertRaises(UnicodeEncodeError):
            R.encode_value(reg, "\u00e9" * 10)

    def test_phoenix_app_version_is_family_specific(self):
        from ve_direct.exceptions import AmbiguousRegisterError
        inv = R.lookup("APP_VER", flavor="INV")
        self.assertEqual(inv.name, "INV_APP_VER")
        self.assertEqual(R.lookup(0x0102, flavor="INV"), inv)
        for software_type, name in enumerate(("bootloader", "application", "tester", "release_candidate")):
            raw = bytes([0, 0x17, 1 | software_type << 6, 0])
            with self.subTest(software_type=name):
                self.assertEqual(R.decode_value(inv, raw), {
                    "version": "1.17", "type": name, "raw": raw.hex().upper(),
                })
        # Only bits6..7 are software type in the register's Phoenix layout.
        self.assertEqual(R.decode_value(inv, bytes.fromhex("00175100"))["version"], "17.17")
        self.assertEqual(R.decode_value(inv, bytes.fromhex("001741"))["version"], "1.17")
        # The fourth byte has no published meaning; keep it in raw only.
        self.assertEqual(R.decode_value(inv, bytes.fromhex("001741FE")), {
            "version": "1.17", "type": "application", "raw": "001741FE",
        })
        for raw in (b"", b"\0", b"\0\x17", bytes.fromhex("01174100"), bytes.fromhex("0017410000")):
            with self.subTest(raw=raw):
                self.assertIsNone(R.decode_value(inv, raw)["version"])
        orion = R.lookup(0x0102, flavor="ORION")
        self.assertEqual(R.decode_value(orion, bytes.fromhex("00FF1201"))["version"], "1.12")
        self.assertEqual(R.decode_value(orion, bytes.fromhex("00FF1201"))["type"], "release")
        with self.assertRaises(AmbiguousRegisterError):
            R.lookup(0x0102)

    # --- 0x200C LINK map has no 250 (absent from both 0x200C tables) ---
    def test_link_device_state_no_250(self):
        # Includes former test_full_error_map's distinction between LINK
        # state 250 and the blocked value in the two 0x0201 state maps.
        self.assertNotIn(250, R.LINK_DEVICE_STATE_MAP)
        link = R.lookup("LINK_DEVICE_STATE")
        self.assertEqual(R.decode_value(link, bytes([250])), "unknown(250)")
        self.assertEqual(R.DEVICE_STATE_MAP[250], "blocked")
        self.assertEqual(R.DEVICE_STATE_ORION_MAP[250], "blocked")

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
        # Each of the eight registers still checks disabled-value round-trip
        # and its own nonzero scale, as before the test cleanup.
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
                self.assertIsNone(R.decode_value(reg, R.encode_value(reg, None)))
                # Keep the original nonzero check for each register. Their
                # individual scales could differ even with a shared codec.
                raw = struct.pack("<H", 17400)
                self.assertAlmostEqual(R.decode_value(reg, raw), 174.0)
                self.assertEqual(R.encode_value(reg, 174.0), raw)

    # --- cycle-5 issue 1: shared 0x0201 state 255 is UNAVAILABLE ---
    def test_device_state_255_unavailable(self):
        # BlueSolar-HEX §1.1 Device-state-values + Orion-XS-HEX
        # Device-state-values both list 255 as UNAVAILABLE (LINK 0x200C
        # agrees); no snapshot defines 0x0201=255 differently.
        self.assertEqual(
            R.decode_value(R.lookup("DEVICE_STATE"), bytes([255])),
            "unavailable")
        self.assertEqual(R.DEVICE_STATE_MAP[0xFF], "unavailable")
        self.assertEqual(
            R.decode_value(R.lookup(0x0201, flavor="MPPT"), bytes([255])),
            "unavailable")
        self.assertEqual(
            R.decode_value(R.lookup(0x0201, flavor="ORION"), bytes([255])),
            "unavailable")
        self.assertEqual(R.DEVICE_STATE_ORION_MAP[255], "unavailable")
        self.assertEqual(R.LINK_DEVICE_STATE_MAP[255], "unavailable")
        self.assertEqual(
            R.decode_value(R.lookup("LINK_DEVICE_STATE"), bytes([255])),
            "unavailable")
        # Phoenix Rev 5 §2.3.1 defines only 0,1,2,9: 255 stays unknown there.
        self.assertEqual(
            R.decode_value(R.lookup(0x0201, flavor="INV"), bytes([255])),
            "unknown(255)")

    # --- cycle-5 issue 2: BAT_VOLTAGE 0xEDEF raw 0 is Auto ---
    def test_bat_voltage_zero_auto(self):
        # BlueSolar-HEX Battery-voltage-setting values table covers
        # registers 0xEDEF and 0xEDEA: 0 = Auto detection at startup.
        # NUMERIC has no human-string channel (decode returns float), so
        # the documented special value follows the _NONE_SENTINELS
        # convention (cf. BMV 0=disabled): decode 0 -> None.
        reg = R.lookup("BAT_VOLTAGE")
        self.assertIsNone(R.decode_value(reg, b"\x00"))
        self.assertEqual(R.encode_value(reg, None), b"\x00")
        # Preserve the composed None round-trip from the original test.
        self.assertIsNone(R.decode_value(reg, R.encode_value(reg, None)))
        # Nonzero behavior unchanged.
        self.assertAlmostEqual(R.decode_value(reg, b"\x0c"), 12.0)
        self.assertAlmostEqual(R.decode_value(reg, b"\x18"), 24.0)
        self.assertEqual(R.encode_value(reg, 12), b"\x0c")
        self.assertEqual(R.encode_value(reg, 24), b"\x18")
        # Sibling ENUM still decodes 0 as auto_detect.
        self.assertEqual(
            R.decode_value(R.lookup("BAT_VOLTAGE_SETTING"), b"\x00"),
            "auto_detect")

    # --- cycle-5 issue 4: pluggable-display 0x0401-0x0404 enums ---
    def test_display_pluggable_enums(self):
        # Every documented value still checks decode, encode and their
        # composed wire round-trip, as before the test cleanup.
        # BlueSolar-HEX Pluggable display settings (same values in the
        # MPPT RS Internal display table for 0x0401/0x0402/0x0404).
        cases = {
            "DISPLAY_BACKLIGHT_INTENSITY": {0: "off", 1: "on"},
            "DISPLAY_SCROLL_SPEED": {1: "slow", 2: "speed_2", 3: "speed_3", 4: "speed_4", 5: "fast"},
            "DISPLAY_SETUP_LOCK": {0: "unlocked", 1: "locked"},
            "DISPLAY_TEMPERATURE_UNIT": {0: "celsius", 1: "fahrenheit"},
        }
        for name, mapping in cases.items():
            with self.subTest(name=name):
                reg = R.lookup(name)
                self.assertEqual(reg.cls, "ENUM")
                self.assertEqual(reg.dtype, "uint8_t")
                for raw, label in mapping.items():
                    self.assertEqual(
                        R.decode_value(reg, bytes([raw])), label)
                    self.assertEqual(R.encode_value(reg, label), bytes([raw]))
                    self.assertEqual(
                        R.decode_value(reg, R.encode_value(reg, label)), label)
        # Intermediate speeds preserve their number without inventing timings.
        reg = R.lookup("DISPLAY_SCROLL_SPEED")
        for raw in (0, 6, 255):
            self.assertEqual(R.decode_value(reg, bytes([raw])), f"unknown({raw})")
        with self.assertRaises(ValueError):
            R.encode_value(reg, "medium")
        # Flavors unchanged (liberal ANY); MPPT flavor resolves by ID.
        self.assertIn("ANY", R.lookup("DISPLAY_BACKLIGHT_INTENSITY").flavors)
        self.assertEqual(
            R.lookup(0x0401, flavor="MPPT").name, "DISPLAY_BACKLIGHT_INTENSITY")

    # --- cycle-5 issue 5: MPPT off-reason bit 4 is Internal reason ---
    def test_mppt_off_reason_internal_reason(self):
        # BlueSolar-HEX §1.1 Device-off-reason table: Bit 4 Internal reason
        # (ORION/INV maps already use "internal_reason").
        self.assertEqual(R.OFF_REASON_MPPT_BITS[4], "internal_reason")
        reg = R.lookup("DEVICE_OFF_REASON_2", flavor="MPPT")
        self.assertEqual(
            R.decode_value(reg, b"\x10\x00\x00\x00"), ["internal_reason"])
        self.assertEqual(
            R.encode_value(reg, ["internal_reason"]),
            b"\x10\x00\x00\x00")

    # --- cycle-4 issue 2: 0x0350-0x0353 generic scoped to MPPT family ---
    def test_relay_bat_scoped_out_of_bmv(self):
        bmv = R.for_flavor("BMV")
        for name in ("RELAY_BAT_LOW_SET", "RELAY_BAT_LOW_CLEAR",
                     "RELAY_BAT_HIGH_SET", "RELAY_BAT_HIGH_CLEAR"):
            self.assertNotIn(name, bmv, name)
            reg = R.lookup(name)
            self.assertEqual(reg.flavors, frozenset({"MPPT", "MPPT_RS"}))
        # BMV flavor resolves the 0.1 V BMV meanings (10x apart).
        reg_bmv = R.lookup(0x0350, flavor="BMV")
        self.assertEqual(reg_bmv.name, "BMV_RELAY_LOW_VOLTAGE")
        self.assertEqual(reg_bmv.scale, 0.1)
        self.assertEqual(
            R.lookup("RELAY_BAT_LOW_SET", flavor="BMV").name,
            "BMV_RELAY_LOW_VOLTAGE")
        self.assertEqual(R.lookup("RELAY_BAT_LOW_SET", flavor="BMV").scale, 0.1)
        # Scale contrast proves the collision mattered: same raw = 10x value.
        raw = struct.pack("<H", 1200)
        self.assertAlmostEqual(
            R.decode_value(R.lookup("RELAY_BAT_LOW_SET"), raw), 12.0)
        self.assertAlmostEqual(R.decode_value(reg_bmv, raw), 120.0)
        self.assertIn("RELAY_BAT_LOW_SET", R.for_flavor("MPPT"))
        self.assertIn("RELAY_BAT_LOW_SET", R.for_flavor("MPPT_RS"))
        self.assertEqual(
            R.lookup(0x0350, flavor="MPPT").name, "RELAY_BAT_LOW_SET")
        self.assertEqual(
            R.lookup(0x0350, flavor="MPPT_RS").name, "RELAY_BAT_LOW_SET")
        self.assertEqual(R.lookup(0x0350, flavor="MPPT").scale, 0.01)

    # --- cycle-4 issue 3: CAPABILITIES_BLE bits 2-8 kept best-effort ---
    def test_capabilities_ble_reserved_best_effort(self):
        # Single ANY register (no BMV override → no new ambiguity).
        reg = R.lookup("CAPABILITIES_BLE")
        self.assertEqual(reg.id, 0x0150)
        self.assertIn("ANY", reg.flavors)
        self.assertEqual(R.lookup(0x0150, flavor="BMV").name, "CAPABILITIES_BLE")
        self.assertEqual(R.lookup(0x0150, flavor="INV").name, "CAPABILITIES_BLE")
        # Orion-family names are used for bits BMV reserves.
        self.assertEqual(R.ENUM_MAPS["CAPABILITIES_BLE"][2], "ble_off_recovery_time")
        self.assertEqual(R.ENUM_MAPS["CAPABILITIES_BLE"][8], "settings_lock")
        # Reserved bits decode best-effort; documented bits 0-1 unchanged.
        self.assertEqual(
            R.decode_value(reg, (1 << 2).to_bytes(4, "little")),
            ["ble_off_recovery_time"])
        self.assertEqual(
            R.decode_value(reg, (1).to_bytes(4, "little")), ["ble_mode"])
        self.assertEqual(R.decode_value(reg, (0).to_bytes(4, "little")), [])


if __name__ == "__main__":
    unittest.main()
