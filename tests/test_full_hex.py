import struct
import sys
import unittest

sys.path.insert(0, "src")

from ve_direct import hex as H
from ve_direct import registers as R
from ve_direct.text import TextParser
from ve_direct.transport import make_text_block


class TestFullHex(unittest.TestCase):
    def test_count_full(self):
        self.assertGreaterEqual(len(R.REGISTERS), 300)

    def test_new_bluesolar_registers_present(self):
        for name in ["GROUP_ID", "REMOTE_CONTROL_USED", "BATTERYSAFE_MODE",
                     "CHARGER_MAXIMUM_CURRENT", "ADDITIONAL_CHARGER_STATE",
                     "VOLTAGE_SETTINGS_RANGE", "HISTORY_VERSION",
                     "LOAD_OUTPUT_CONTROL", "LOAD_OUTPUT_OFF_REASON",
                     "RELAY_OPERATION_MODE", "RELAY_BAT_LOW_SET",
                     "TX_PORT_MODE", "RX_PORT_MODE", "TOTAL_HISTORY",
                     "CHARGE_VOLTAGE_SETPOINT", "NETWORK_MODE",
                     "RESTORE_DEFAULTS", "CLEAR_HISTORY",
                     "DISPLAY_BACKLIGHT_MODE", "PANEL_STARTING_VOLTAGE"]:
            self.assertIn(name, R.REGISTERS, name)

    def test_orion_registers_present(self):
        for name in ["SHUTDOWN_VOLTAGE", "START_VOLTAGE", "ORION_BATTERY_VOLTAGE",
                     "CYCLE_COUNT", "CUMULATIVE_USER_HISTORY", "CYCLE_HISTORY_0",
                     "INPUT_VOLTAGE_LOCKOUT", "INPUT_VOLTAGE", "OUTPUT_VOLTAGE"]:
            self.assertIn(name, R.REGISTERS, name)

    def test_sentinels_decode_none(self):
        self.assertIsNone(R.decode_value(R.lookup("BAT_TEMPERATURE"), bytes.fromhex("FFFF")))
        self.assertIsNone(R.decode_value(R.lookup("TIME_OF_DAY"), bytes.fromhex("FFFF")))
        self.assertIsNone(R.decode_value(R.lookup("BATTERY_VOLTAGE_SENSE"), bytes.fromhex("FFFF")))
        self.assertIsNone(R.decode_value(R.lookup("BATTERY_TEMPERATURE_SENSE"), bytes.fromhex("FF7F")))
        self.assertIsNone(R.decode_value(R.lookup("ORION_BATTERY_VOLTAGE"), bytes.fromhex("FFFFFF7F")))
        self.assertIsNone(R.decode_value(R.lookup("LOW_TEMP_CHARGE_CURRENT"), bytes.fromhex("FFFF")))
        self.assertIsNone(R.decode_value(
            R.lookup("SETTINGS_CHANGED_TIMESTAMP"), bytes.fromhex("FFFFFFFF")))
        # encode None back to sentinel
        self.assertEqual(R.encode_value(R.lookup("BAT_TEMPERATURE"), None), bytes.fromhex("FFFF"))
        self.assertEqual(R.encode_value(R.lookup("LOW_TEMP_CHARGE_CURRENT"), None), bytes.fromhex("FFFF"))
        self.assertEqual(R.encode_value(R.lookup("TIME_OF_DAY"), None), bytes.fromhex("FFFF"))

    def test_timer_event_roundtrip(self):
        reg = R.lookup("TIMER_EVENT_0")
        d = R.decode_value(reg, bytes.fromhex("10000132"))
        self.assertEqual(d["offset_min"], 16)
        self.assertEqual(d["anchor_name"], "sunset")
        self.assertEqual(d["dim_pct"], 50)
        raw = R.encode_value(reg, {"offset_min": 16, "anchor": 1, "dim_pct": 50})
        self.assertEqual(raw, bytes.fromhex("10000132"))

    def test_voltage_range(self):
        reg = R.lookup("VOLTAGE_SETTINGS_RANGE")
        d = R.decode_value(reg, bytes.fromhex("0C18"))
        self.assertEqual((d["min_V"], d["max_V"]), (12, 24))
        self.assertEqual(R.encode_value(reg, {"min_V": 12, "max_V": 24}), bytes.fromhex("0C18"))

    def test_load_control(self):
        reg = R.lookup("LOAD_OUTPUT_CONTROL")
        d = R.decode_value(reg, bytes.fromhex("85"))
        self.assertEqual(d["mode_name"], "user1")
        self.assertTrue(d["lighting_timer"])
        self.assertEqual(R.encode_value(reg, {"mode": 5, "lighting_timer": True}), bytes.fromhex("85"))

    def test_network_status(self):
        reg = R.lookup("NETWORK_STATUS")
        d = R.decode_value(reg, bytes.fromhex("F4"))
        self.assertEqual(d["mode_name"], "standalone")

    def test_history_day_consumed_none(self):
        # consumed 0xFFFFFFFF -> None (no load output)
        raw = (b"\x00" + struct.pack("<I", 100) + struct.pack("<I", 0xFFFFFFFF)
               + struct.pack("<H", 2800) + struct.pack("<H", 2700) + b"\x00"
               + bytes([1, 2, 3, 4]) + struct.pack("<HHH", 60, 30, 10)
               + struct.pack("<I", 500) + struct.pack("<H", 100)
               + struct.pack("<H", 3200) + struct.pack("<H", 7))
        d = R.decode_value(R.lookup("DAILY_HISTORY_0"), raw)
        self.assertAlmostEqual(d["yield_kwh"], 1.0)
        self.assertIsNone(d["consumed_kwh"])
        self.assertEqual(d["day_seq"], 7)

    def test_history_mppt_day_none(self):
        raw = (b"\x00" + struct.pack("<H", 9) + struct.pack("<H", 100)
               + struct.pack("<H", 0xFFFF) + struct.pack("<H", 0xFFFF)
               + struct.pack("<H", 0xFFFF) + struct.pack("<H", 50)
               + struct.pack("<H", 60) + struct.pack("<H", 0xFFFF)
               + struct.pack("<H", 0xFFFF) + struct.pack("<H", 3200)
               + struct.pack("<H", 0xFFFF) * 3 + bytes([0xFF] * 9))
        d = R.decode_value(R.lookup("MPPT_HISTORY_0"), raw)
        self.assertAlmostEqual(d["energy_1_kwh"], 1.0)
        self.assertIsNone(d["energy_2_kwh"])
        self.assertEqual(d["peak_1_W"], 50)

    def test_app_version_decode(self):
        self.assertEqual(H.decode_app_version(bytes.fromhex("1641"))["version"], "1.16")
        self.assertEqual(H.decode_app_version(bytes.fromhex("1641"))["type"], "application")
        self.assertEqual(H.decode_app_version(bytes.fromhex("0111"))["type"], "bootloader")
        # 3-byte input is undocumented: no invented "application24" type.
        undoc3 = H.decode_app_version(bytes.fromhex("010203"))
        self.assertIsNone(undoc3["version"])
        self.assertEqual(undoc3["type"], "unknown")
        rel = H.decode_app_version_24(bytes.fromhex("00FF0201"))
        self.assertEqual(rel["version"], "1.02")
        self.assertEqual(rel["type"], "release")
        # Non-0xFF LL is undocumented (Orion-XS §1.1 Notes 1-2 define only
        # LL==0xFF = release): never an invented "beta" label.
        undoc = H.decode_app_version_24(bytes.fromhex("00030201"))
        self.assertIsNone(undoc["version"])
        self.assertTrue(undoc["type"].startswith("unknown"))

    def test_boot_name(self):
        # Nibbles 0x0/0x6 are requests only (neither doc defines
        # Boot/Restart responses; both send no reply).
        f = H.decode_line(H.encode_frame(0, H.BOOT_MAGIC))
        self.assertEqual(f.request_name, "Boot")
        self.assertEqual(f.name, "Cmd(0)")
        self.assertEqual(H.command_name(0, is_request=True), "Boot")
        r = H.decode_line(H.encode_frame(6, b""))
        self.assertEqual(r.request_name, "Restart")
        self.assertEqual(r.name, "Cmd(6)")

    def test_product_information_structures(self):
        product = R.decode_value(
            R.lookup("PRODUCT_ID"), bytes.fromhex("0042A0FF"))
        self.assertEqual(product["product_id"], 0xA042)
        app = R.decode_value(
            R.lookup("APP_VER"), bytes.fromhex("00FF0201"))
        self.assertEqual(app["version"], "1.02")

    def test_bat_type_helper(self):
        self.assertEqual(R.decode_bat_type(1, "orion"), "lifepo4")
        self.assertEqual(R.decode_bat_type(1, "small"), "type_2")
        self.assertEqual(R.decode_bat_type(1, "large"), "type_1")
        self.assertEqual(R.decode_bat_type(1, "rs"), "agm_normal")
        self.assertEqual(R.decode_bat_type(255), "user")
        self.assertEqual(R.decode_value(R.lookup("BAT_TYPE"), b"\xFF"), "user")
        self.assertEqual(
            R.decode_value(R.lookup("BAT_TYPE", flavor="ORION"), b"\x01"),
            "lifepo4",
        )
        self.assertEqual(
            R.decode_value(R.lookup("BAT_TYPE", flavor="MPPT_RS"), b"\x01"),
            "agm_normal",
        )

    def test_firmware_dependent_yield_width(self):
        reg = R.lookup("YIELD_TODAY")
        self.assertAlmostEqual(R.decode_value(reg, struct.pack("<H", 123)), 1.23)
        self.assertAlmostEqual(
            R.decode_value(reg, struct.pack("<I", 100000)), 1000.0)

    def test_phoenix_access_modes(self):
        self.assertEqual(
            R.lookup("AC_OUT_VOLTAGE_SETPOINT", flavor="INV").access,
            "WRITE_ONLY",
        )
        self.assertEqual(
            R.lookup("BAT_VOLTAGE", flavor="INV").access, "READ_ONLY")
        self.assertEqual(
            R.lookup("INV_RELAY_MODE", flavor="INV").access, "WRITE_ONLY")

    def test_capabilities_full_bits(self):
        m = R.ENUM_MAPS["CAPABILITIES"]
        for bit, name in [(9, "solar_timer"), (14, "bms"), (19, "virtual_load_output"),
                          (21, "plugin_display"), (25, "aes"), (26, "battery_test"),
                          (27, "paygo"), (28, "hibernate")]:
            self.assertEqual(m[bit], name, bit)
        ble = R.ENUM_MAPS["CAPABILITIES_BLE"]
        self.assertEqual(ble[7], "soc_sync_via_trend")
        self.assertEqual(ble[8], "settings_lock")

    def test_flavor_hierarchy(self):
        self.assertIn("BAT_MAX_CURRENT", R.for_flavor("MPPT"))  # CHG via MPPT
        self.assertIn("BAT_MAX_CURRENT", R.for_flavor("ORION"))  # CHG via ORION
        self.assertNotIn("SHUTDOWN_VOLTAGE", R.for_flavor("MPPT"))
        self.assertIn("SHUTDOWN_VOLTAGE", R.for_flavor("ORION"))
        self.assertIn("PANEL_VOLTAGE", R.for_flavor("MPPT_RS"))
        self.assertIn("BAT_MAX_CURRENT", R.for_flavor("MPPT_RS"))

    def test_write_only_guard(self):
        import asyncio
        from ve_direct.device import AsyncVEDirect
        from ve_direct.exceptions import RegisterError
        from ve_direct.transport import SimulatedTransport

        async def go():
            sim = SimulatedTransport(text_chunks=[], hex_replies={})
            dev = AsyncVEDirect(sim.read_chunk, sim.write)
            with self.assertRaises(RegisterError):
                await dev.hex_get("CLEAR_HISTORY")
            with self.assertRaises(RegisterError):
                await dev.hex_get("RESTORE_DEFAULTS")
        asyncio.run(go())

    def test_corrupt_history_does_not_drop_next_pid(self):
        good_pid = make_text_block({"PID": "0x204", "V": "1"})
        hist = make_text_block({"H1": "-5000", "H2": "-100"})
        bad_hist = bytearray(hist)
        bad_hist[6] ^= 0xFF  # corrupt data byte, keep Checksum marker intact
        frames = TextParser().feed(bytes(bad_hist) + good_pid)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 1)


if __name__ == "__main__":
    unittest.main()
