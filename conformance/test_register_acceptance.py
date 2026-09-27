"""Register acceptance examples transcribed from the four pinned HEX documents.

Expected bytes, offsets and enum meanings are independent of the register DB.
These tests retain the independently specified behavior behind audit findings;
no expectedFailure or skip is used. Hardware behavior and caller scheduling are classified in the JSON ledger.
"""
import struct
import unittest

from ve_direct import registers as R


def decode(rid, raw, flavor):
    return R.decode_value(R.lookup(rid, flavor=flavor), raw)


def encode(rid, value, flavor):
    return R.encode_value(R.lookup(rid, flavor=flavor), value)


class RegisterAcceptance(unittest.TestCase):
    def assert_fields(self, actual, expected):
        for key, value in expected.items():
            with self.subTest(field=key):
                if isinstance(value, float):
                    self.assertAlmostEqual(actual[key], value)
                else:
                    self.assertEqual(actual[key], value)

    def check_enum(self, rid, flavor, meanings, width=1, signed=False):
        for number, name in meanings.items():
            with self.subTest(register=hex(rid), flavor=flavor, number=number):
                raw = number.to_bytes(width, "little", signed=signed)
                self.assertEqual(decode(rid, raw, flavor), name)
                # Numeric writes preserve alternate encodings with the same name.
                self.assertEqual(encode(rid, number, flavor), raw)

    def check_bits(self, rid, flavor, meanings, width):
        for bit, name in meanings.items():
            with self.subTest(register=hex(rid), flavor=flavor, bit=bit):
                raw = (1 << bit).to_bytes(width, "little")
                self.assertEqual(decode(rid, raw, flavor), [name])
                self.assertEqual(encode(rid, [name], flavor), raw)
        self.assertEqual(decode(rid, bytes(width), flavor), [])

    def test_product_id_register_layouts(self):
        for flavor, raw, pid in [("MPPT", "0042A0FF", 0xA042),
                                 ("ORION", "00F0A3FF", 0xA3F0),
                                 ("INV", "0003A2FE", 0xA203)]:
            with self.subTest(flavor=flavor):
                self.assertEqual(decode(0x0100, bytes.fromhex(raw), flavor)["product_id"], pid)

    def test_phoenix_product_revision(self):
        self.assert_fields(decode(0x0101, bytes.fromhex("003412"), "INV"),
                           {"identifier": 0, "revision": 0x1234})

    def test_orion_application_version(self):
        # Orion section 1.1: identifier then LE 0xHHMMLL, LL=FF release.
        self.assert_fields(decode(0x0102, bytes.fromhex("00FF1201"), "ORION"),
                           {"version": "1.12", "type": "release"})

    def test_phoenix_application_version(self):
        # Phoenix 2.2.3: byte1 minor, byte2 major plus software type.
        # Its table says un32; byte3 is unspecified and deliberately zero here.
        self.assert_fields(decode(0x0102, bytes.fromhex("00174100"), "INV"),
                           {"version": "1.17", "type": "application"})

    def test_bmv_version_and_string_reads(self):
        self.assert_fields(decode(0xEEF9, bytes.fromhex("0704"), "BMV"),
                           {"raw": 0x0407, "version": "4.07"})
        for size in (0, 1, 19, 31, 32):
            text = "x" * size
            raw = text.encode() + (b"\0" if size < 32 else b"")
            self.assertEqual(decode(0x010A, raw, "BMV"), text)
        self.assertEqual(encode(0x010C, "cabinet", "BMV"), b"cabinet\0")

    def test_bmv_string20_write_boundary(self):
        # Whatever policy is chosen for a full-width input, do not send >20B.
        # A clear ValueError is permitted if the implementation requires a NUL.
        for size in (19, 20, 21):
            with self.subTest(characters=size):
                try:
                    raw = encode(0x010C, "x" * size, "BMV")
                except ValueError:
                    self.assertGreaterEqual(size, 20)
                else:
                    self.assertLessEqual(len(raw), 20, "BMV String20 maximum wire width")

    def test_small_charger_battery_type_normal_decode(self):
        # Acceptance contract: MPPT_SMALL selects the small-model table while
        # preserving the existing MPPT default for large rotary-switch models.
        # The explicit subtype selects the documented small-model table.
        self.assertEqual(R.decode_bat_type(1, "small"), "type_2")
        try:
            reg = R.lookup(0xEDF1, flavor="MPPT_SMALL")
        except KeyError:
            self.fail("normal register API lacks a selectable small-model battery table")
        self.assertEqual(R.decode_value(reg, b"\x01"), "type_2")

    def test_small_charger_battery_type_normal_encode(self):
        # Same explicit subtype contract; its TYPE_2 is raw1, not large-model2.
        try:
            reg = R.lookup(0xEDF1, flavor="MPPT_SMALL")
        except KeyError:
            self.fail("normal register API lacks a selectable small-model battery table")
        self.assertEqual(R.encode_value(reg, "type_2"), b"\x01")

    def test_other_battery_type_tables(self):
        self.check_enum(0xEDF1, "MPPT", {1: "type_1", 3: "type_4", 5: "type_5",
                        6: "type_6", 7: "type_7", 8: "type_8", 255: "user"})
        # Large raw2 is excluded here: the official table assigns two names.
        self.check_enum(0xEDF1, "MPPT_RS", {1: "agm_normal", 2: "agm_high", 3: "lithium", 255: "user"})
        self.check_enum(0xEDF1, "ORION", {1: "lifepo4", 255: "user"})

    def test_battery_voltage_settings(self):
        self.check_enum(0xEDEA, "MPPT", {0: "auto_detect", 12: "v12", 24: "v24", 36: "v36", 48: "v48"})
        self.check_enum(0xEDEA, "ORION", {12: "v12", 24: "v24", 48: "v48"})
        self.assertEqual(encode(0xEDEF, None, "MPPT"), b"\0")
        self.assertEqual(decode(0xEDEF, b"\x18", "MPPT"), 24)

    def test_device_state_and_mode_tables(self):
        self.check_enum(0x0200, "MPPT", {0: "off", 1: "charger_on", 4: "device_off"})
        self.check_enum(0x0200, "ORION", {0: "off", 4: "off", 1: "on", 3: "on"})
        self.check_enum(0x0200, "INV", {2: "inverter_on", 3: "device_on", 4: "device_off", 5: "eco", 253: "hibernate"})
        common = {0: "not_charging", 2: "fault", 3: "bulk", 4: "absorption",
                  6: "storage", 245: "wake_up", 247: "auto_equalize", 250: "blocked",
                  252: "external_control", 255: "unavailable"}
        self.check_enum(0x0201, "MPPT", common | {5: "floating", 7: "equalize_manual"})
        self.check_enum(0x0201, "ORION", common | {5: "float", 7: "manual_equalise",
                        11: "power_supply", 246: "repeated_absorption", 248: "battery_safe"})
        self.check_enum(0x0201, "INV", {0: "off", 1: "low_power", 2: "fault", 9: "inverting"})

    def test_off_reasons_and_warning_bits(self):
        common = {0: "no_input_power", 2: "soft_power_switch", 3: "remote_input", 4: "internal_reason", 5: "paygo"}
        self.check_bits(0x0207, "INV", common, 4)
        self.check_bits(0x0207, "ORION", common | {6: "bms_shutdown", 7: "engine_shutdown", 8: "error"}, 4)
        self.check_bits(0x0207, "MPPT", common | {1: "physical_power_switch", 6: "bms_shutdown", 9: "battery_temperature"}, 4)
        self.check_bits(0x0205, "MPPT", common | {1: "physical_power_switch", 6: "bms_shutdown"}, 1)
        for rid in (0x031C, 0x031E):
            self.check_bits(rid, "INV", {0: "low_battery_voltage", 1: "high_battery_voltage",
                            5: "low_temperature", 6: "high_temperature", 8: "overload", 9: "dc_ripple",
                            10: "low_ac_out_voltage", 11: "high_ac_out_voltage"}, 2)
        self.check_bits(0x0202, "MPPT", {1: "remote_on_off"}, 4)

    def test_capability_bits(self):
        common = {0: "load_output", 1: "rotary_encoder", 2: "history", 3: "batterysafe", 4: "adaptive",
                  5: "manual_equalise", 6: "automatic_equalise", 7: "storage", 8: "remote_on_off",
                  9: "solar_timer", 10: "alt_tx_pin", 11: "user_load_switch", 12: "load_current_text",
                  13: "panel_current", 14: "bms", 15: "external_control", 16: "synchronized_charging",
                  17: "relay", 18: "alt_rx_pin", 19: "virtual_load_output", 20: "virtual_relay",
                  21: "plugin_display", 25: "aes", 26: "battery_test", 27: "paygo"}
        self.check_bits(0x0140, "MPPT", common, 4)
        self.check_bits(0x0140, "ORION", common | {28: "hibernate", 29: "ac_apparent_power", 30: "psu_function", 31: "needs_battery_to_shutdown"}, 4)
        self.check_bits(0x0140, "INV", {8: "remote_on_off", 17: "relay", 27: "paygo", 28: "hibernate", 29: "ac_apparent_power"}, 4)
        ble = {0: "ble_mode", 1: "permanent_off"}
        for flavor in ("BMV", "INV"):
            self.check_bits(0x0150, flavor, ble, 4)
        self.check_bits(0x0150, "ORION", ble | {2: "ble_off_recovery_time", 3: "recovery_time_zero_permanent",
                        4: "service_mode", 5: "trends", 6: "ad_key", 7: "soc_sync_via_trend", 8: "settings_lock"}, 4)

    def test_bmv_monitor_modes_and_signed_measurements(self):
        modes = {-9: "Solar charger", -8: "Wind turbine", -7: "Shaft generator", -6: "Alternator",
                 -5: "Fuel cell", -4: "Water generator", -3: "DC/DC charger", -2: "AC charger",
                 -1: "Generic source", 0: "Battery monitor (BMV)", 1: "Generic load", 2: "Electric drive",
                 3: "Fridge", 4: "Water pump", 5: "Bilge pump", 6: "DC system", 7: "Inverter", 8: "Water heater"}
        self.check_enum(0xEEB8, "BMV", modes, 2, True)
        self.assertAlmostEqual(decode(0xED8F, bytes.fromhex("85FF"), "BMV"), -12.3)
        self.assertAlmostEqual(decode(0xED8C, bytes.fromhex("C7CFFFFF"), "BMV"), -12.345)
        self.assertEqual(decode(0xED8E, bytes.fromhex("85FF"), "BMV"), -123)

    def test_charger_error_tables(self):
        common = {0: "no_error", 2: "battery_voltage_high", 17: "charger_temperature_high",
                  21: "current_sensor", 26: "overheated", 28: "converter_issue", 33: "input_voltage",
                  67: "bms", 116: "calibration_lost", 117: "invalid_firmware", 119: "settings_lost"}
        bs = {3: "remote_temperature_a", 4: "remote_temperature_b", 5: "remote_temperature_c",
              6: "remote_voltage_a", 7: "remote_voltage_b", 8: "remote_voltage_c", 14: "battery_temperature_low",
              18: "over_current", 19: "charger_current_polarity_reversed", 20: "bulk_time",
              22: "internal_temperature_a", 23: "internal_temperature_b", 27: "short_circuit", 29: "over_charge",
              34: "input_current", 38: "input_shutdown_voltage", 39: "input_shutdown_current",
              66: "synchronisation", 68: "network_a"}
        self.check_enum(0xEDDA, "MPPT", common | bs)
        self.check_enum(0xEDDA, "ORION", common | {1: "battery_temperature_high", 120: "reference_voltage_failure",
                        150: "battery_temperature_high_warn", 151: "battery_temperature_low_warn",
                        160: "charger_overtemperature_derating", 161: "short_circuit_detected", 162: "converter_issue_warn"})

    def test_rs_inputs_tracker_modes_and_additional_state(self):
        self.check_enum(0xD0C0, "MPPT_RS", {0: "remote_on_off", 1: "two_wire_bms"})
        self.check_bits(0xD01F, "MPPT_RS", {0: "two_wire_bms_enabled", 1: "allow_to_discharge", 2: "allow_to_charge"}, 1)
        self.check_bits(0xEDD4, "MPPT", {0: "safe_mode", 1: "auto_equalise", 4: "temp_dimming", 6: "input_current_dimming"}, 1)
        for rid in (0xEDB3, 0xECC3, 0xECD3, 0xECE3, 0xECF3):
            self.check_enum(rid, "MPPT_RS", {0: "off", 1: "v_i_limited", 2: "mppt"})
        for base in (0xECCB, 0xECDB, 0xECEB, 0xECFB):
            self.assertAlmostEqual(decode(base, b"\x39\x30", "MPPT_RS"), 123.45)
            self.assertAlmostEqual(decode(base + 1, bytes.fromhex("40E20100"), "MPPT_RS"), 1234.56)
            self.assertAlmostEqual(decode(base + 2, b"\x7B\0", "MPPT_RS"), 12.3)

    def test_load_control_and_off_reasons(self):
        names = ["off", "auto", "alt1", "alt2", "on", "user1", "user2", "aes"]
        for mode, name in enumerate(names):
            for timer in (False, True):
                raw = bytes([mode + (128 if timer else 0)])
                self.assert_fields(decode(0xEDAB, raw, "MPPT"),
                                   {"mode": mode, "mode_name": name, "lighting_timer": timer})
                self.assertEqual(encode(0xEDAB, {"mode": mode, "lighting_timer": timer}, "MPPT"), raw)
        self.check_bits(0xED91, "MPPT", {0: "battery_low", 1: "short_circuit", 2: "timer",
                        3: "remote_input", 4: "paygo", 7: "starting_up"}, 1)

    def test_relay_port_and_display_enums(self):
        self.check_enum(0xEDD9, "MPPT", dict(enumerate(["always_off", "panel_high", "temp_high", "battery_low",
                        "equalisation_active", "error", "temp_low", "battery_high", "float_or_storage", "day_detection", "load_control"])))
        self.check_enum(0xED9E, "MPPT", {0: "normal", 1: "pulse_001kwh", 2: "pwm_normal", 3: "pwm_inverted", 4: "virtual_load"})
        self.check_enum(0xED98, "MPPT", {0: "remote_on_off", 1: "load_config", 2: "load_on_off_inverted", 3: "load_on_off"})
        self.check_enum(0x0400, "MPPT", {0: "keypress", 1: "on", 2: "auto"})
        self.check_enum(0x0408, "MPPT_RS", {0: "off", 1: "on", 2: "auto"})
        self.check_enum(0x0401, "MPPT", {0: "off", 1: "on"})
        self.check_enum(0x0402, "MPPT", {1: "slow", 5: "fast"})
        self.check_enum(0x0403, "MPPT", {0: "unlocked", 1: "locked"})
        self.check_enum(0x0404, "MPPT", {0: "celsius", 1: "fahrenheit"})
        self.check_enum(0x034F, "BMV", {0: "default", 1: "charger", 2: "remote"})
        self.check_enum(0x034F, "INV", {0: "warnings", 2: "remote", 3: "disabled", 4: "normal", 5: "battery_low", 6: "fan"})
        self.check_enum(0xEEF7, "BMV", {0: "celsius", 1: "fahrenheit"})
        self.check_enum(0xEEF8, "BMV", {0: "starter_battery", 1: "mid_voltage", 2: "temperature"})

    def test_boolean_settings(self):
        cases = {"BMV": [0x034D, 0x034E, 0x0400, 0xEEFC, 0xEEF6, 0x0FFD] + list(range(0xEEE0, 0xEEE9)),
                 "INV": [0x034E, 0xEBBA], "MPPT": [0xEDFF, 0xEDFE, 0xEDE8, 0xEDE5, 0xEDA8, 0x2030, 0x2018]}
        for flavor, ids in cases.items():
            for rid in ids:
                for number in (0, 1):
                    with self.subTest(flavor=flavor, rid=rid, value=number):
                        self.assertEqual(decode(rid, bytes([number]), flavor), bool(number))
                        self.assertEqual(encode(rid, bool(number), flavor), bytes([number]))
        for flavor in ("BMV", "INV", "ORION"):
            self.check_enum(0x0090, flavor, {0: "off", 1: "on"})
        self.check_enum(0x0206, "ORION", {0: "charger", 1: "psu"})

    def test_timer_events(self):
        for rid in range(0xEDA0, 0xEDA6):
            for offset, anchor, name, dim in [(-32768, 1, "sunset", 0), (-123, 2, "midnight", 37), (32767, 3, "sunrise", 100)]:
                raw = struct.pack("<hBB", offset, anchor, dim)
                expected = {"offset_min": offset, "anchor": anchor, "anchor_name": name, "dim_pct": dim}
                self.assert_fields(decode(rid, raw, "MPPT"), expected)
                self.assertEqual(encode(rid, expected, "MPPT"), raw)

    def test_voltage_range_and_yield_widths(self):
        self.assertEqual(decode(0xEDCE, b"\x0C\x30", "MPPT"), {"min_V": 12, "max_V": 48})
        for rid in (0xEDD1, 0xEDD3):
            self.assertAlmostEqual(decode(rid, bytes.fromhex("3930"), "MPPT"), 123.45)
            self.assertAlmostEqual(decode(rid, bytes.fromhex("39300100"), "MPPT"), 778.81)

    def test_scalar_sentinels(self):
        cases = [("MPPT", 0xEDEC, "FFFF"), ("MPPT", 0xEDE6, "FFFF"), ("MPPT", 0x2031, "FFFF"),
                 ("MPPT", 0x2002, "FFFF"), ("MPPT", 0x2003, "FF7F"), ("ORION", 0xD18D, "FFFFFF7F"),
                 ("BMV", 0xEC41, "FFFFFFFF")]
        cases += [("BMV", rid, "0000") for rid in list(range(0x032A, 0x032E)) + list(range(0x035A, 0x035E))]
        for flavor, rid, raw in cases:
            with self.subTest(flavor=flavor, rid=hex(rid)):
                self.assertIsNone(decode(rid, bytes.fromhex(raw), flavor))
                self.assertEqual(encode(rid, None, flavor), bytes.fromhex(raw))
        self.assertEqual(decode(0xEC41, b"\0" * 4, "BMV"), 0)
        self.assertAlmostEqual(decode(0x2003, bytes.fromhex("18FC"), "MPPT"), -10.0)
        self.assertAlmostEqual(decode(0xEDEC, bytes.fromhex("626F"), "MPPT"), 285.14)

    def test_history_total_both_versions(self):
        raw = struct.pack("<6BIIHHB", 0, 0, 17, 18, 19, 20, 12345, 23456, 5678, 1456, 23)
        expected = {"reserved": 0, "error_db": 0, "error_0": 17, "error_1": 18, "error_2": 19,
                    "error_3": 20, "yield_user_kwh": 123.45, "yield_system_kwh": 234.56,
                    "panel_max_V": 56.78, "battery_max_V": 14.56, "days_available": 23}
        self.assertEqual(len(raw), 19)
        self.assert_fields(decode(0x104F, raw, "MPPT"), expected)
        new = b"\x01" + raw[1:] + struct.pack("<H", 1023) + b"\xff" * 13
        self.assertEqual(len(new), 34)
        self.assert_fields(decode(0x104F, new, "MPPT"), expected | {"reserved": 1, "battery_min_V": 10.23})

    def test_daily_history_offsets(self):
        raw = struct.pack("<BIIHH5BHHHIHHH", 0, 123, 456, 1456, 1123,
                          0, 17, 18, 19, 20, 101, 202, 303, 123456, 234, 5678, 364)
        self.assertEqual(len(raw), 34)
        expected = {"yield_kwh": 1.23, "consumed_kwh": 4.56, "battery_max_V": 14.56,
                    "battery_min_V": 11.23, "error_0": 17, "error_1": 18, "error_2": 19, "error_3": 20,
                    "time_bulk_min": 101, "time_absorption_min": 202, "time_float_min": 303,
                    "power_max_W": 123456, "battery_max_A": 23.4, "panel_max_V": 56.78, "day_seq": 364}
        for rid in (0x1050, 0x106E):
            self.assert_fields(decode(rid, raw, "MPPT"), expected)
        unavailable_load = raw[:5] + b"\xff" * 4 + raw[9:]
        self.assert_fields(decode(0x1050, unavailable_load, "MPPT"), expected | {"consumed_kwh": None})

    def test_tracker_history_offsets_and_absent_trackers(self):
        raw = struct.pack("<B13H", 0, 364, 101, 202, 303, 404, 501, 602, 703, 804, 901, 1002, 1103, 1204) + b"\xff" * 9
        self.assertEqual(len(raw), 36)
        expected = {"day_seq": 364}
        for tracker in range(1, 5):
            expected[f"energy_{tracker}_kwh"] = tracker * 1.01
            expected[f"peak_{tracker}_W"] = 400 + tracker * 101
            expected[f"voc_max_{tracker}_V"] = 8 + tracker * 1.01
        self.assert_fields(decode(0x10A0, raw, "MPPT_RS"), expected)
        absent = b"\0\x6C\x01" + b"\xff" * 33
        out = decode(0x10BD, absent, "MPPT_RS")
        self.assertEqual(out["day_seq"], 364)
        for key in expected:
            if key != "day_seq":
                self.assertIsNone(out[key])

    def test_orion_cumulative_history_offsets(self):
        raw = struct.pack("<B6I", 1, 123456, 234567, 34, 45, 56, 67)
        expected = {"version": 1, "operation_time_s": 123456, "charged_Ah": 23456.7,
                    "cycles_started": 34, "cycles_completed": 45, "power_ups": 56, "deep_discharges": 67}
        self.assertEqual(len(raw), 25)
        for rid in (0x1042, 0x1043):
            self.assert_fields(decode(rid, raw, "ORION"), expected)

    def test_orion_cycle_history_offsets_and_sentinels(self):
        raw = struct.pack("<B11IHHBB", 1, 123456, 101, 202, 303, 404, 505,
                          611, 722, 833, 944, 1055, 1234, 1456, 1, 150)
        expected = {"version": 1, "start_time_s": 123456, "time_bulk_s": 101, "time_absorption_s": 202,
                    "time_recondition_s": 303, "time_float_s": 404, "time_storage_s": 505,
                    "ah_bulk": 61.1, "ah_absorption": 72.2, "ah_recondition": 83.3,
                    "ah_float": 94.4, "ah_storage": 105.5, "voltage_start_V": 12.34,
                    "voltage_end_V": 14.56, "battery_type": 1, "error": 150}
        self.assertEqual(len(raw), 51)
        for rid in (0x1070, 0x1098):
            self.assert_fields(decode(rid, raw, "ORION"), expected)
        out = decode(0x1070, b"\x01" + b"\xff" * 50, "ORION")
        self.assertEqual(out["version"], 1)
        for key in expected:
            if key != "version":
                self.assertIsNone(out[key], key)
        self.assertEqual(decode(0x106F, b"\x29", "ORION"), 41)
        self.assertEqual(decode(0x1099, bytes.fromhex("78563412"), "ORION"), {"start_time_s": 0x12345678})

    def test_network_mode_info_status_and_state(self):
        for flavor in ("MPPT", "ORION"):
            self.check_bits(0x200E, flavor, {0: "networked", 1: "slave", 2: "external_control", 3: "bms",
                            4: "charge_group_master", 5: "charge_instance_master", 6: "standby"}, 1)
            self.check_bits(0x200D, flavor, {0: "bms_controlled", 1: "vset_remote", 2: "charge_slave", 3: "charge_master",
                            4: "using_icharge", 5: "using_isense", 6: "using_tsense", 7: "using_vsense"}, 1)
            names = ["slave", "group_master", "instance_master", "group_and_instance_master", "standalone"]
            for mode, name in enumerate(names):
                for bit, key in [(4, "using_icharge"), (5, "using_isense"), (6, "using_tsense"), (7, "using_vsense")]:
                    self.assert_fields(decode(0x200F, bytes([mode | (1 << bit)]), flavor), {"mode": mode, "mode_name": name, key: True})
            self.check_enum(0x200C, flavor, {0: "not_charging", 2: "fault", 3: "bulk", 4: "absorption", 5: "float",
                            6: "storage", 7: "manual_equalise", 11: "power_supply", 245: "wake_up", 246: "repeated_absorption",
                            247: "auto_equalize", 248: "battery_safe", 249: "load_detect", 252: "external_control", 255: "unavailable"})
        self.check_enum(0x2004, "MPPT", {1: "start_equalise", 2: "stop_equalise", 3: "sync_ui", 4: "sync_day"})

    def test_remote_control_value_encodings(self):
        for flavor in ("MPPT", "ORION"):
            self.assertEqual(encode(0x2001, 14.4, flavor), bytes.fromhex("A005"))
            self.assertEqual(encode(0x2015, 12.3, flavor), bytes.fromhex("7B00"))
            self.assertEqual(encode(0x200A, -12.345, flavor), bytes.fromhex("C7CFFFFF"))
            self.assertEqual(encode(0x200E, ["networked", "external_control"], flavor), b"\x05")
            self.assertEqual(encode(0x200E, ["networked", "bms"], flavor), b"\x09")
        self.assertEqual(encode(0x2007, 123456, "MPPT"), bytes.fromhex("40E20100"))
        self.assertEqual(encode(0x2008, 1.23, "MPPT"), bytes.fromhex("7B00"))
        self.assertEqual(encode(0x2009, 67, "MPPT"), b"\x43")
        self.assertEqual(encode(0x2027, 1234.56, "MPPT"), bytes.fromhex("40E20100"))

    def test_phoenix_settings_and_nvm_commands(self):
        self.check_enum(0xEB99, "INV", {0: "no_command", 1: "save", 2: "revert", 3: "backup", 4: "default"})
        self.check_enum(0xEB03, "INV", {0: "60Hz", 1: "50Hz"})
        self.assertEqual(encode(0xEB06, 1.25, "INV"), b"\x05")
        self.assertEqual(encode(0xEB10, 25, "INV"), b"\x19")
        self.assertAlmostEqual(decode(0xEBB1, bytes.fromhex("C82C"), "INV"), 11.464)
        for flavor in ("MPPT", "ORION", "BMV", "INV"):
            self.assertEqual(encode(0x0004, b"", flavor), b"")
        for rid in (0x1029, 0x102C, 0x1030):
            self.assertEqual(encode(rid, b"", "BMV"), b"")


if __name__ == "__main__":
    unittest.main()
