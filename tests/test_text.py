import sys
import unittest

sys.path.insert(0, "src")

from ve_direct.text import TextParser, verify_block_checksum
from ve_direct.transport import make_text_block


class TestText(unittest.TestCase):
    def test_si_units_match_converted_values_and_preserve_wire_units(self):
        cases = [
            ("V", "12340", 12.34, "mV", "V"),
            ("AC_OUT_V", "23000", 230, "0.01V", "V"),
            ("DC_IN_V", "1420", 14.2, "0.01V", "V"),
            ("I", "-1500", -1.5, "mA", "A"),
            ("AC_OUT_I", "13", 1.3, "0.1A", "A"),
            ("DC_IN_I", "1700", 170, "0.1A", "A"),
            ("CE", "-1719", -1.719, "mAh", "Ah"),
            ("H19", "6741", 67.41, "0.01kWh", "kWh"),
            ("SOC", "997", 99.7, "‰", "%"),
            ("DM", "15", 1.5, "‰", "%"),
            ("TTG", "120", 120, "min", "min"),
            ("PPV", "125", 125, "W", "W"),
            ("T", "25", 25, "°C", "°C"),
            ("CS", "3", 3, None, None),
            ("UNKNOWN", "raw", "raw", None, None),
        ]
        cases.extend((label, "12000", 12, "mV", "V") for label in ("H7", "H8", "H15", "H16"))
        cases.extend(("V", raw, value, "mV", "V") for raw, value in (("---", None), ("invalid", "invalid")))
        for label, raw, value, wire_unit, si_unit in cases:
            with self.subTest(label=label, raw=raw):
                frame = TextParser().feed(make_text_block({label: raw}))[0]
                self.assertEqual(frame.to_si()[label], value)
                self.assertEqual(frame[label].unit, wire_unit)
                self.assertEqual(frame[label].si_unit, si_unit)

    def test_parse_mppt_block(self):
        blk = make_text_block({
            "PID": "0xA04A", "FW": "116", "SER#": "HQ1750YFN5R",
            "V": "27690", "I": "4400", "VPV": "31300", "PPV": "125",
            "CS": "3", "MPPT": "2", "OR": "0", "ERR": "0",
            "LOAD": "ON", "H19": "6741", "HSDS": "84",
        })
        # Former test_checksum_roundtrip checked the block checksum helper
        # directly on a generated Text block.
        self.assertTrue(verify_block_checksum(blk))
        frames = TextParser().feed(blk)
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f["V"].value, 27690)
        self.assertEqual(f["CS"].human, "Bulk")
        self.assertEqual(f["MPPT"].human, "MPP Tracker active")
        self.assertEqual(f["ERR"].human, "No error")
        self.assertAlmostEqual(f.to_si()["V"], 27.69)

    def test_parse_bmv_block(self):
        blk = make_text_block({
            "PID": "0x204", "V": "25803", "VS": "11576", "I": "-897",
            "P": "-23", "CE": "-1719", "SOC": "997", "TTG": "14400",
            "Alarm": "OFF", "Relay": "OFF", "AR": "5",
        })
        f = TextParser().feed(blk)[0]
        # Former test_percent_covers_dm also checked SOC percentage scaling.
        self.assertAlmostEqual(f["SOC"].percent, 99.7)
        self.assertEqual(f["I"].value, -897)
        # AR=5 => Low voltage + Low SOC
        self.assertIn("Low voltage", f["AR"].human)
        self.assertIn("Low SOC", f["AR"].human)

    def test_split_feed_and_history_block(self):
        p = TextParser()
        blk = make_text_block({"H1": "-167452", "H2": "-1719"})
        out = []
        for i in range(0, len(blk), 5):
            out += p.feed(blk[i:i + 5])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["H1"].value, -167452)

    def test_corrupt_then_valid_same_feed(self):
        # Former test_corrupt_dropped checked that a bad checksum emits no
        # frame. This stronger case also checks that the next frame survives.
        good = make_text_block({"PID": "0x204", "V": "1"})
        bad = bytearray(make_text_block({"PID": "0x204", "V": "2"}))
        bad[10] ^= 0xFF
        frames = TextParser().feed(bytes(bad) + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 1)
        # Former test_corrupt_history_does_not_drop_next_pid used a history
        # block with its data byte corrupted and the Checksum tag intact.
        history = bytearray(make_text_block({"H1": "-5000", "H2": "-100"}))
        history[6] ^= 0xFF
        recovered = TextParser().feed(bytes(history) + good)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["V"].value, 1)

    def test_hex_interleaved(self):
        blk = make_text_block({"PID": "0xA04A", "V": "100", "I": "5"})
        # splice a HEX async line between Text lines (older fw behavior:
        # HEX arrives at a line boundary, ending with \n)
        head, tail = blk.split(b"V\t100\r\n", 1)
        mixed = head + b"V\t100\r\n:A0102000543\n" + tail
        # parser must still yield the text frame (HEX stripped before checksum)
        frames = TextParser().feed(mixed)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 100)

    def test_hex_can_interrupt_mid_record(self):
        blk = make_text_block({"PID": "0xA04A", "V": "100", "I": "5"})
        head, tail = blk.split(b"V\t100", 1)
        mixed = head + b"V\t10:A0102000543\n0" + tail
        frames, hex_lines = TextParser().feed_events(mixed)
        self.assertEqual(hex_lines, [b":A0102000543\n"])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 100)

    def test_hex_immediately_after_binary_checksum(self):
        blk = make_text_block({"PID": "0xA04A", "V": "100"})
        frames, hex_lines = TextParser().feed_events(blk + b":154\n")
        self.assertEqual(len(frames), 1)
        self.assertEqual(hex_lines, [b":154\n"])

    def test_resyncs_past_leading_garbage_and_missing_marker(self):
        good = make_text_block({"PID": "0x204", "V": "123"})
        self.assertEqual(TextParser().feed(b"garbage" + good)[0]["V"].value, 123)
        corrupt_without_marker = b"\r\nPID\t0x204\r\nV\t999\r\n"
        parser = TextParser()
        # A missing marker loses the boundary. Do not salvage an apparently
        # valid suffix: discard through the next checksum and resume there.
        self.assertEqual(parser.feed(corrupt_without_marker + good), [])
        frames = parser.feed(good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 123)

    def test_invalid_suffix_is_not_accepted_at_startup_or_after_truncation(self):
        good = make_text_block({"PID": "0xA04A", "V": "10069", "I": "100"})
        bad = good.replace(b"I\t100", b"I\t101")
        self.assertFalse(verify_block_checksum(bad))
        self.assertEqual(TextParser().feed(bad), [])
        parser = TextParser()
        self.assertEqual(parser.feed(b"\r\nX\t" + b"x" * 9000), [])
        self.assertEqual(parser.feed(good), [])
        self.assertEqual(parser.feed(good)[0]["I"].value, 100)

    def test_firmware_semantics_and_si_conversions(self):
        blk = make_text_block({
            "FW": "C208", "FWE": "0208FF", "CE": "-1719",
            "H1": "-167452", "DM": "15",
        })
        frame = TextParser().feed(blk)[0]
        self.assertEqual(frame["FW"].human, "v2.08-RCC")
        self.assertEqual(frame["FWE"].human, "v2.08")
        self.assertAlmostEqual(frame.to_si()["CE"], -1.719)
        self.assertAlmostEqual(frame.to_si()["H1"], -167.452)
        self.assertEqual(frame.to_si()["DM"], 1.5)
        # Former test_percent_covers_dm checked DM percentage scaling too.
        self.assertAlmostEqual(frame["DM"].percent, 1.5)

    def test_checksum_tag_case_insensitive(self):
        # Former test_checksum_not_in_label_info checked that neither spelling
        # is a measurement label, in addition to its absence from frames.
        from ve_direct.tables import LABEL_INFO
        self.assertNotIn("Checksum", LABEL_INFO)
        self.assertNotIn("CHECKSUM", LABEL_INFO)
        prefix = b"\r\nPID\t0xA2B1\r\nV\t100\r\nCHECKSUM\t"
        blk = prefix + bytes([(-sum(prefix)) % 256])
        frames, hex_lines = TextParser().feed_events(blk + b":154\n")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 100)
        self.assertEqual(frames[0]["PID"].human, "Phoenix Inverter 12V 5000VA 230V")
        self.assertEqual(hex_lines, [b":154\n"])
        self.assertNotIn("CHECKSUM", frames[0].fields)
        self.assertNotIn("Checksum", frames[0].fields)

    def test_bare_hex_pid(self):
        # "A04A" without 0x prefix must decode per the spec PID list.
        f = TextParser().feed(make_text_block({"PID": "A04A"}))[0]
        self.assertEqual(f["PID"].value, 0xA04A)
        self.assertEqual(f["PID"].human, "BlueSolar MPPT 100|30 rev2")
        f2 = TextParser().feed(make_text_block({"PID": "0xA04A"}))[0]
        self.assertEqual(f2["PID"].value, 0xA04A)
        f3 = TextParser().feed(make_text_block({"PID": "0204"}))[0]
        self.assertEqual(f3["PID"].value, 0x204)
        self.assertIn("BMV-702", f3["PID"].human)

    def test_empty_string_missing_restricted(self):
        # "" is missing only for spec'd labels T/CE/SOC/TTG; "---" stays liberal.
        blk = make_text_block({
            "V": "", "T": "", "CE": "", "SOC": "", "TTG": "",
        })
        f = TextParser().feed(blk)[0]
        self.assertIsNotNone(f["V"].value)  # V "" is not missing per spec
        self.assertEqual(f["V"].value, "")
        self.assertIsNone(f["T"].value)
        self.assertIsNone(f["CE"].value)
        self.assertIsNone(f["SOC"].value)
        self.assertIsNone(f["TTG"].value)
        # "---" still treated as missing even for non-spec labels (liberal).
        f2 = TextParser().feed(make_text_block({"V": "---"}))[0]
        self.assertIsNone(f2["V"].value)
        # Former test_missing_sensor checked T="---" specifically.
        f3 = TextParser().feed(make_text_block({"PID": "0xA04A", "T": "---"}))[0]
        self.assertIsNone(f3["T"].value)

    def test_fwe_optional_padding_and_invalid_input(self):
        for raw, human in (
            ("FF", "v0.00"), ("8FF", "v0.08"), ("0008FF", "v0.08"),
            ("208FF", "v2.08"), ("020801", "v2.08-beta.01"),
            ("1", "v0.00-beta.01"),
        ):
            with self.subTest(raw=raw):
                frame = TextParser().feed(make_text_block({"FWE": raw}))[0]
                self.assertEqual(frame["FWE"].human, human)
        for raw in ("", "0208FFF", "ZZ", "+12345", "12_345"):
            with self.subTest(raw=raw):
                frame = TextParser().feed(make_text_block({"FWE": raw}))[0]
                self.assertIsNone(frame["FWE"].human)

    def test_or_hex_and_decimal_masks(self):
        for raw in ("0x00000081", "0X00000081", "129", "000129"):
            with self.subTest(raw=raw):
                frame = TextParser().feed(make_text_block({"OR": raw}))[0]
                self.assertEqual(frame["OR"].value, 129)
                self.assertEqual(frame["OR"].human, "No input power, Engine shutdown detection")

    def test_ttg_minus_one_infinite(self):
        f = TextParser().feed(make_text_block({"TTG": "-1"}))[0]
        self.assertEqual(f["TTG"].value, -1)
        self.assertIsNotNone(f["TTG"].human)
        self.assertIn("infinite", f["TTG"].human.lower())

    def test_coverage_cap_ble_warn_mode_mon(self):
        blk = make_text_block({
            "CAP_BLE": "3", "WARN": "5", "MODE": "2", "MON": "-9",
            "AC_OUT_V": "23000", "AC_OUT_I": "15", "AC_OUT_S": "100",
            "DC_IN_V": "1200", "DC_IN_I": "50", "DC_IN_P": "60",
            "V2": "13800", "V3": "13800", "I2": "100", "I3": "-50",
            # Restore the H13/H14 decoding assertions removed from this test.
            "H13": "2", "H14": "3",
        })
        f = TextParser().feed(blk)[0]
        self.assertIn("BLE supports switching off", f["CAP_BLE"].human)
        self.assertIn("permanent", f["CAP_BLE"].human)
        self.assertIn("Low voltage", f["WARN"].human)
        self.assertIn("Inverter", f["MODE"].human)
        self.assertEqual(f["MON"].value, -9)
        self.assertEqual(f["MON"].human, "Solar charger")
        f_mon0 = TextParser().feed(make_text_block({"MON": "0"}))[0]
        self.assertEqual(f_mon0["MON"].human, "Battery monitor (BMV)")
        f_mon8 = TextParser().feed(make_text_block({"MON": "8"}))[0]
        self.assertEqual(f_mon8["MON"].human, "Water heater")
        self.assertAlmostEqual(f.to_si()["AC_OUT_V"], 230.0)
        self.assertAlmostEqual(f.to_si()["AC_OUT_I"], 1.5)
        self.assertEqual(f.to_si()["AC_OUT_S"], 100)
        self.assertAlmostEqual(f.to_si()["DC_IN_V"], 12.0)
        self.assertAlmostEqual(f.to_si()["DC_IN_I"], 5.0)
        self.assertEqual(f.to_si()["DC_IN_P"], 60)
        self.assertAlmostEqual(f.to_si()["V2"], 13.8)
        self.assertAlmostEqual(f.to_si()["V3"], 13.8)
        self.assertAlmostEqual(f.to_si()["I2"], 0.1)
        self.assertAlmostEqual(f.to_si()["I3"], -0.05)
        self.assertEqual(f["H13"].value, 2)
        self.assertEqual(f["H14"].value, 3)

    def test_ar_unknown_bits_not_no_alarm(self):
        # Spec AR table defines bits 1..8192; AR=16384 is out-of-range and
        # must not decode as "No alarm".
        from ve_direct.tables import decode_ar
        self.assertEqual(decode_ar(0), ["No alarm"])
        self.assertEqual(decode_ar(16384), ["Unknown bits 0x00004000"])
        mixed = decode_ar(1 | 16384)
        self.assertIn("Low voltage", mixed)
        self.assertTrue(any("Unknown bits" in s for s in mixed))
        f = TextParser().feed(make_text_block({"AR": "16384"}))[0]
        self.assertIn("Unknown bits", f["AR"].human)
        self.assertNotIn("No alarm", f["AR"].human)

    def test_oversized_input_safety(self):
        # Spec guideline: 9B label, 33B value, max 22 fields/block; parser is
        # liberal but caps buffered input at 8 KiB.
        p = TextParser()
        self.assertEqual(p.feed(b"X" * 20000), [])
        self.assertLess(p.pending_bytes, 8192)
        good = make_text_block({"PID": "0x204", "V": "123"})
        self.assertEqual(TextParser().feed(b"Y" * 9000 + good)[0]["V"].value, 123)


if __name__ == "__main__":
    unittest.main()
