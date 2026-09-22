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

    def test_request_response_names(self):
        # Nibble 0x3/0x4 double as request/response (BlueSolar §1, Orion §1).
        self.assertEqual(H.REQUEST_NAMES[0x3], "AppVersion")
        self.assertEqual(H.RESPONSE_NAMES[0x3], "Unknown")
        self.assertEqual(H.REQUEST_NAMES[0x4], "ProductId")
        self.assertEqual(H.RESPONSE_NAMES[0x4], "Error")
        self.assertEqual(H.command_name(0x3, is_request=True), "AppVersion")
        self.assertEqual(H.command_name(0x3, is_request=False), "Unknown")
        f = H.decode_line(H.encode_frame(0x3, b""))
        self.assertEqual(f.request_name, "AppVersion")
        self.assertEqual(f.name, "Unknown")

    def test_non_ascii_maps_to_frame_error(self):
        from ve_direct.exceptions import FrameError
        with self.assertRaises(FrameError):
            H.decode_line(b":\xFF\xFE\n")
        with self.assertRaises(FrameError):
            H.decode_line(b":7BBED00\xFF\n")

    def test_app_version_ffff_is_bluesolar(self):
        # 2-byte FFFF must decode as BlueSolar RC, never Orion ping.
        d = H.decode_app_version(bytes.fromhex("FFFF"))
        self.assertEqual(d["type"], "release_candidate")
        self.assertEqual(d["version"], "15.FF-RCF")
        # Orion ping lives only in decode_ping_version.
        p = H.decode_ping_version(bytes.fromhex("FF7F"))
        self.assertIsNone(p["version"])
        self.assertEqual(p["type"], "application")
        p2 = H.decode_ping_version(bytes.fromhex("FFFF"))
        self.assertIsNone(p2["version"])
        self.assertEqual(p2["type"], "release_candidate")
        # Release-candidate 0xD101 -> 1.01-RCD (BlueSolar §1 response 5).
        rc = H.decode_ping_version(bytes.fromhex("01D1"))
        self.assertEqual(rc["version"], "1.01-RCD")
        self.assertEqual(rc["type"], "release_candidate")

    def test_ping_ff1f_falls_through_to_version(self):
        # 0x?FFF with Y=0x1 is not a documented Orion ping (Orion-XS §1
        # response 5 allows only Y in {3,7,B,F}); must fall through to
        # normal BlueSolar version decode, not version-None.
        d = H.decode_ping_version(bytes.fromhex("FF1F"))
        self.assertIsNotNone(d["version"])
        self.assertEqual(d, H.decode_app_version(bytes.fromhex("FF1F")))
        self.assertEqual(d["type"], "bootloader")
        # Documented Orion pings still decode as no-version.
        for hx, want in (("FF3F", "bootloader"), ("FF7F", "application"),
                         ("FFBF", "tester"), ("FFFF", "release_candidate")):
            p = H.decode_ping_version(bytes.fromhex(hx))
            self.assertIsNone(p["version"], hx)
            self.assertEqual(p["type"], want, hx)

    def test_app_version_24_undocumented_ll(self):
        d = H.decode_app_version_24(bytes.fromhex("00030201"))
        self.assertIsNone(d["version"])
        self.assertTrue(d["type"].startswith("unknown"))
        self.assertNotEqual(d["type"], "beta")

    def test_app_version_3byte_unknown(self):
        d = H.decode_app_version(bytes.fromhex("010203"))
        self.assertIsNone(d["version"])
        self.assertEqual(d["type"], "unknown")

    def test_register_id_restricted(self):
        # Done payload is not a register id (e.g. product-id Done 0x4200).
        done = H.decode_line(H.encode_frame(H.CMD_DONE, bytes.fromhex("42A0")))
        self.assertIsNone(done.register_id)
        ping = H.decode_line(H.encode_frame(H.CMD_PING, bytes.fromhex("FF7F")))
        self.assertIsNone(ping.register_id)
        unk = H.decode_line(H.encode_frame(H.CMD_UNKNOWN, b"\x03\x00"))
        self.assertIsNone(unk.register_id)
        err = H.decode_line(H.encode_frame(H.CMD_ERROR, bytes.fromhex("AAAA")))
        self.assertIsNone(err.register_id)
        get = H.decode_line(H.build_get(0xEDBB))
        self.assertEqual(get.register_id, 0xEDBB)

    def test_unknown_encoding_doc_typo(self):
        # Orion-XS §1.3 prints :30300F4 but it fails checksum; :303004F is right.
        self.assertEqual(H.encode_frame(0x3, b"\x03\x00"), b":303004F\n")
        bad = H.decode_line(b":30300F4\n")
        self.assertFalse(bad.valid)
        good = H.decode_line(b":303004F\n")
        self.assertTrue(good.valid)


if __name__ == "__main__":
    unittest.main()
