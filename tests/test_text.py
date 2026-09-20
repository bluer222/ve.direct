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


if __name__ == "__main__":
    unittest.main()
