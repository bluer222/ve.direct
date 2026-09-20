import struct
import sys
import unittest

sys.path.insert(0, "src")

from ve_direct import hex as H


class TestHex(unittest.TestCase):
    def test_checksum_roundtrip(self):
        for cmd, payload in [(0x7, b"\x01\x02\x00"), (0x8, b"\xF0\xED\x00\x64\x00"),
                             (0x1, b""), (0xA, b"\x01\x02\x00\x05")]:
            line = H.encode_frame(cmd, payload)
            f = H.decode_line(line)
            self.assertTrue(f.valid, line)
            self.assertEqual((f.cmd, f.data), (cmd, payload))

    def test_known_set_example(self):
        # From BlueSolar-HEX docs: set 10.0A -> :8F0ED0064000C
        line = H.build_set(0xEDF0, struct.pack("<H", 100))
        self.assertEqual(line, b":8F0ED0064000C\n")
        f = H.decode_line(line)
        self.assertTrue(f.valid)
        self.assertEqual(f.register_id, 0xEDF0)
        self.assertEqual(f.value_bytes, struct.pack("<H", 100))

    def test_get_shape(self):
        line = H.build_get(0xEDBB)
        f = H.decode_line(line)
        self.assertTrue(f.valid)
        self.assertEqual(f.cmd, H.CMD_GET)
        self.assertEqual(f.register_id, 0xEDBB)

    def test_bad_checksum_flagged(self):
        line = bytearray(H.build_get(0x0201))
        line[-3] = ord("0") if line[-3] != ord("0") else ord("1")
        f = H.decode_line(bytes(line))
        self.assertFalse(f.valid)

    def test_malformed_rejected(self):
        for bad in [b"nope\n", b":ZZ\n", b":7\n", b"70010052\n"]:
            with self.assertRaises(Exception, msg=bad):
                H.decode_line(bad)


if __name__ == "__main__":
    unittest.main()
