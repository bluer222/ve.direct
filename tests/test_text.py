import sys
import unittest

sys.path.insert(0, "src")

from ve_direct.text import TextParser, verify_block_checksum
from ve_direct.transport import make_text_block


class TestText(unittest.TestCase):
    def test_checksum_roundtrip(self):
        blk = make_text_block({"PID": "0xA04A", "V": "13830", "CS": "3"})
        self.assertTrue(verify_block_checksum(blk))

    def test_parse_mppt_block(self):
        blk = make_text_block({
            "PID": "0xA04A", "FW": "116", "SER#": "HQ1750YFN5R",
            "V": "27690", "I": "4400", "VPV": "31300", "PPV": "125",
            "CS": "3", "MPPT": "2", "OR": "0", "ERR": "0",
            "LOAD": "ON", "H19": "6741", "HSDS": "84",
        })
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

    def test_missing_sensor(self):
        blk = make_text_block({"PID": "0xA04A", "T": "---"})
        f = TextParser().feed(blk)[0]
        self.assertIsNone(f["T"].value)

    def test_corrupt_dropped(self):
        blk = bytearray(make_text_block({"PID": "0x204", "V": "1"}))
        blk[10] ^= 0xFF
        self.assertEqual(TextParser().feed(bytes(blk)), [])

    def test_corrupt_then_valid_same_feed(self):
        good = make_text_block({"PID": "0x204", "V": "1"})
        bad = bytearray(make_text_block({"PID": "0x204", "V": "2"}))
        bad[10] ^= 0xFF
        frames = TextParser().feed(bytes(bad) + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 1)

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
        frames = TextParser().feed(corrupt_without_marker + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["V"].value, 123)

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

    def test_checksum_tag_case_insensitive(self):
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

    def test_fwe_short_rejected(self):
        f = TextParser().feed(make_text_block({"FWE": "FF"}))[0]
        self.assertIsNone(f["FWE"].human)
        # Short-but-valid 5-char form still decodes (208FF -> 0208FF).
        f2 = TextParser().feed(make_text_block({"FWE": "208FF"}))[0]
        self.assertEqual(f2["FWE"].human, "v2.08")
        f3 = TextParser().feed(make_text_block({"FWE": "020801"}))[0]
        self.assertIn("beta", f3["FWE"].human)

    def test_ttg_minus_one_infinite(self):
        f = TextParser().feed(make_text_block({"TTG": "-1"}))[0]
        self.assertEqual(f["TTG"].value, -1)
        self.assertIsNotNone(f["TTG"].human)
        self.assertIn("infinite", f["TTG"].human.lower())

    def test_checksum_not_in_label_info(self):
        from ve_direct.tables import LABEL_INFO
        self.assertNotIn("Checksum", LABEL_INFO)
        self.assertNotIn("CHECKSUM", LABEL_INFO)

    def test_percent_covers_dm(self):
        f = TextParser().feed(make_text_block({"SOC": "997", "DM": "15"}))[0]
        self.assertAlmostEqual(f["SOC"].percent, 99.7)
        self.assertAlmostEqual(f["DM"].percent, 1.5)

    def test_coverage_cap_ble_warn_mode_mon(self):
        blk = make_text_block({
            "CAP_BLE": "3", "WARN": "5", "MODE": "2", "MON": "-9",
            "AC_OUT_V": "23000", "AC_OUT_I": "15", "AC_OUT_S": "100",
            "DC_IN_V": "1200", "DC_IN_I": "50", "DC_IN_P": "60",
            "V2": "13800", "V3": "13800", "I2": "100", "I3": "-50",
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
