"""Finite acceptance cases for the pinned VE.Direct Text 3.34 specification.

Wire fixtures and expected values are independent of the production builders,
maps and specification scraper. The ledger identifies device obligations and
document ambiguities separately. Failing cases are ordinary tests.
"""
from __future__ import annotations

import unittest

from ve_direct.text import TextParser


def wire_block(records: list[tuple[str, str]]) -> bytes:
    """Encode the documented CRLF/TAB grammar, without production helpers."""
    body = b"".join(
        b"\r\n" + label.encode("ascii") + b"\t" + raw.encode("ascii")
        for label, raw in records
    ) + b"\r\nChecksum\t"
    return body + bytes([(-sum(body)) & 255])


def values(frames):
    return [{label: tv.value for label, tv in frame.fields.items()} for frame in frames]


class TextAcceptance(unittest.TestCase):
    def one(self, records):
        frames = TextParser().feed(wire_block(records))
        self.assertEqual(len(frames), 1)
        return frames[0]

    def test_documented_frame_fixture_and_every_split(self):
        # Victron FAQ Q8 raw capture, including its independently supplied D8.
        block = (
            b"\r\nPID\t0x203\r\nV\t26201\r\nI\t0\r\nP\t0\r\nCE\t0"
            b"\r\nSOC\t1000\r\nTTG\t-1\r\nAlarm\tOFF\r\nRelay\tOFF"
            b"\r\nAR\t0\r\nBMV\t700\r\nFW\t0307\r\nChecksum\t\xd8"
        )
        expected = {
            "PID": 0x203, "V": 26201, "I": 0, "P": 0, "CE": 0,
            "SOC": 1000, "TTG": -1, "Alarm": "OFF", "Relay": "OFF",
            "AR": 0, "BMV": "700", "FW": "0307",
        }
        self.assertEqual(sum(block) & 255, 0)
        for split in range(len(block) + 1):
            with self.subTest(split=split):
                parser = TextParser()
                first = parser.feed(block[:split])
                if split < len(block):
                    self.assertEqual(first, [], "unvalidated fields escaped")
                self.assertEqual(values(first + parser.feed(block[split:])), [expected])

    def test_all_256_binary_checksum_bytes(self):
        # Three printable, non-space characters span every checksum residue.
        # ':' is excluded because the mixed protocol reserves it for HEX.
        alphabet = [n for n in range(33, 127) if n != ord(":")]
        residues = {}
        for first in alphabet:
            for second in alphabet:
                for third in alphabet:
                    raw = bytes((first, second, third))
                    residues.setdefault(sum(raw) & 255, raw)
        prefix, suffix = b"\r\nX\t", b"\r\nChecksum\t"
        self.assertEqual(len(residues), 256)
        for checksum in range(256):
            raw = residues[(-checksum - sum(prefix + suffix)) & 255]
            block = prefix + raw + suffix + bytes([checksum])
            self.assertEqual(sum(block) & 255, 0)
            for bytewise in (False, True):
                with self.subTest(checksum=checksum, bytewise=bytewise):
                    parser = TextParser()
                    chunks = [bytes([byte]) for byte in block] if bytewise else [block]
                    frames = [frame for chunk in chunks for frame in parser.feed(chunk)]
                    self.assertEqual(values(frames), [{"X": raw.decode("ascii")}])
                    self.assertEqual(parser.pending_bytes, 0)

    def test_corrupted_measurement_rejected_after_known_boundary(self):
        parser = TextParser()
        self.assertEqual(len(parser.feed(wire_block([("PID", "0xA04A"), ("V", "12000")]))), 1)
        good = wire_block([("PID", "0xA04A"), ("V", "10069"), ("I", "100")])
        bad = good.replace(b"I\t100", b"I\t101")
        self.assertEqual(sum(a ^ b for a, b in zip(good, bad)), 1)
        self.assertEqual(sum(bad) & 255, 1)
        self.assertEqual(values(parser.feed(bad)), [])

    def test_invalid_block_does_not_contaminate_following_block(self):
        bad = bytearray(wire_block([("PID", "0x204"), ("V", "12000")]))
        bad[bad.index(b"12000")] = ord("2")
        good = wire_block([("H1", "-1700"), ("H2", "-100")])
        self.assertNotEqual(sum(bad) & 255, 0)
        self.assertEqual(values(TextParser().feed(bytes(bad) + good)), [{"H1": -1700, "H2": -100}])

    def test_alternating_blocks_stay_independent_at_every_split(self):
        blocks = [
            wire_block([("PID", "0x204"), ("V", "25803"), ("I", "-897")]),
            wire_block([("H1", "-167452"), ("H2", "-1719")]),
            wire_block([("PID", "0x204"), ("V", "25804")]),
        ]
        expected = [
            {"PID": 0x204, "V": 25803, "I": -897},
            {"H1": -167452, "H2": -1719},
            {"PID": 0x204, "V": 25804},
        ]
        stream = b"".join(blocks)
        for split in range(len(stream) + 1):
            with self.subTest(split=split):
                parser = TextParser()
                self.assertEqual(values(parser.feed(stream[:split]) + parser.feed(stream[split:])), expected)

    def test_hex_between_records_and_after_checksum_at_every_split(self):
        text = wire_block([("PID", "0xA04A"), ("V", "12000")])
        boundary = text.index(b"\r\nV")
        mixed = text[:boundary] + b":A0102000543\n" + text[boundary:] + b":154\n"
        for split in range(len(mixed) + 1):
            with self.subTest(split=split):
                parser = TextParser()
                first_frames, first_hex = parser.feed_events(mixed[:split])
                last_frames, last_hex = parser.feed_events(mixed[split:])
                self.assertEqual(values(first_frames + last_frames), [{"PID": 0xA04A, "V": 12000}])
                self.assertEqual(first_hex + last_hex, [b":A0102000543\n", b":154\n"])

    def test_hex_midrecord_resume_policy(self):
        # 3.34 says "interrupt", without specifying a byte-level resume rule.
        # This tests the library's existing insertion/resume policy, not a
        # stronger assertion that all devices generate this precise sequence.
        text = wire_block([("V", "12000"), ("I", "100")])
        mixed = text.replace(b"12000", b"12:A0102000543\n000")
        parser = TextParser()
        frames, lines = parser.feed_events(mixed)
        self.assertEqual(values(frames), [{"V": 12000, "I": 100}])
        self.assertEqual(lines, [b":A0102000543\n"])

    def test_unknown_fields_and_arbitrary_label_lengths(self):
        frame = self.one([
            ("V", "12000"), ("XTRA", "vendor-value"),
            ("ManufacturerSpecificField", "12345678901234567890123456789012"),
        ])
        self.assertEqual(frame["XTRA"].value, "vendor-value")
        self.assertIsNone(frame["XTRA"].unit)
        self.assertEqual(frame["ManufacturerSpecificField"].raw, "12345678901234567890123456789012")
        self.assertEqual(frame.to_si()["V"], 12.0)

    def test_recommended_capacity_accepts_22_records(self):
        # 8 label bytes + terminator; 32 value bytes + terminator. Use 22
        # data records, which also covers a limit that counts Checksum itself.
        records = [(f"FIELD{i:03d}", "12345678901234567890123456789012") for i in range(22)]
        frame = self.one(records)
        self.assertEqual({key: tv.raw for key, tv in frame.fields.items()}, dict(records))

    def test_numeric_units_and_si_conventions_for_all_measurements(self):
        groups = [
            ("V V2 V3 VS VM VPV H7 H8 H15 H16", "mV", 12345, 12.345),
            ("I I2 I3 IL", "mA", -1234, -1.234),
            ("CE H1 H2 H3 H6", "mAh", -1719, -1.719),
            ("DM SOC", "‰", 123, 12.3),
            ("T", "°C", -5, -5),
            ("PPV P H21 H23 DC_IN_P", "W", 125, 125),
            ("H17 H18 H19 H20 H22", "0.01kWh", 123, 1.23),
            ("AC_OUT_V DC_IN_V", "0.01V", 1234, 12.34),
            ("AC_OUT_I DC_IN_I", "0.1A", 123, 12.3),
            ("AC_OUT_S", "VA", 123, 123),
            ("TTG", "min", 123, 123),
            ("H9", "s", 123, 123),
            ("H4 H5 H10 H11 H12 H13 H14 HSDS", None, 123, 123),
        ]
        for names, unit, raw, converted in groups:
            for name in names.split():
                with self.subTest(label=name):
                    frame = self.one([(name, str(raw))])
                    self.assertEqual(frame[name].raw, str(raw))
                    self.assertEqual(frame[name].value, raw)
                    self.assertEqual(frame[name].unit, unit)
                    self.assertAlmostEqual(frame.to_si()[name], converted)

    def test_current_sign_is_preserved(self):
        for raw, expected in (("-897", -0.897), ("0", 0), ("+897", 0.897)):
            with self.subTest(raw=raw):
                frame = self.one([("I", raw)])
                self.assertAlmostEqual(frame["I"].amps, expected)
                self.assertAlmostEqual(frame.to_si()["I"], expected)

    def test_missing_sensor_unsynchronised_and_dc_monitor_fields(self):
        # Footnotes 8, 10, 11 apply to these rows, including history fields.
        for label in "T CE SOC TTG H1 H2 H3 H4 H5 H6 H9 H10".split():
            with self.subTest(label=label):
                frame = self.one([(label, "---")])
                self.assertEqual(frame[label].raw, "---")
                self.assertIsNone(frame[label].value)
                self.assertIsNone(frame.to_si()[label])

    def test_time_to_go_infinity_preserves_sentinel(self):
        frame = self.one([("TTG", "-1")])
        self.assertEqual(frame["TTG"].value, -1)
        self.assertEqual(frame.to_si()["TTG"], -1)
        self.assertIn("infinite", frame["TTG"].human.lower())

    def test_alarm_relay_and_load_states(self):
        for label in ("Alarm", "Relay", "LOAD"):
            for raw in ("ON", "OFF", "On", "Off"):
                with self.subTest(label=label, raw=raw):
                    frame = self.one([(label, raw)])
                    self.assertEqual(frame[label].raw, raw)
                    self.assertEqual(frame[label].value, raw.upper())

    def test_fw_release_and_candidate_examples(self):
        for raw, human in (("208", "v2.08"), ("C208", "v2.08-RCC")):
            with self.subTest(raw=raw):
                frame = self.one([("FW", raw)])
                self.assertEqual(frame["FW"].value, raw)
                self.assertEqual(frame["FW"].human, human)

    def test_fwe_documented_examples(self):
        for raw, human in (("0208FF", "v2.08"), ("208FF", "v2.08"), ("020801", "v2.08-beta.01")):
            with self.subTest(raw=raw):
                frame = self.one([("FWE", raw)])
                self.assertEqual(frame["FWE"].value, raw)
                self.assertEqual(frame["FWE"].human, human)

    def test_fwe_leading_zero_padding_is_optional(self):
        # The document specifies up to six digits and no mandatory leading
        # zero padding. It specifies no five-character minimum.
        padded = self.one([("FWE", "0008FF")])["FWE"].human
        unpadded = self.one([("FWE", "8FF")])["FWE"].human
        self.assertEqual(padded, "v0.08")
        self.assertEqual(unpadded, padded)

    def test_all_alarm_and_warning_bits_and_combination(self):
        bits = {
            1: "Low voltage", 2: "High voltage", 4: "Low SOC",
            8: "Low starter voltage", 16: "High starter voltage",
            32: "Low temperature", 64: "High temperature", 128: "Mid voltage",
            256: "Overload", 512: "DC-ripple", 1024: "Low V AC out",
            2048: "High V AC out", 4096: "Short circuit", 8192: "BMS lockout",
        }
        for label in ("AR", "WARN"):
            for raw, human in [(0, "No alarm"), *bits.items(), (5, "Low voltage, Low SOC")]:
                with self.subTest(label=label, raw=raw):
                    frame = self.one([(label, str(raw))])
                    self.assertEqual(frame[label].value, raw)
                    self.assertEqual(frame[label].human, human)

    def test_hexadecimal_off_reason_wire_value(self):
        # Table spelling is confirmed by a first-hand MPPT wire capture:
        # communityarchive.victronenergy.com/questions/272347/
        # vedirect-framehandler-wrong-on-15070.html
        frame = self.one([("OR", "0x00000010")])
        self.assertEqual(frame["OR"].value, 16)
        self.assertEqual(frame["OR"].human, "Protection active")

    def test_off_reason_map_all_bits(self):
        # Tests meanings independently of the failing hex wire-format case.
        from ve_direct.tables import decode_or
        bits = {
            1: "No input power", 2: "Switched off (power switch)",
            4: "Switched off (device mode register)", 8: "Remote input",
            16: "Protection active", 32: "Paygo", 64: "BMS",
            128: "Engine shutdown detection", 256: "Analysing input voltage",
        }
        for bit, meaning in bits.items():
            with self.subTest(bit=bit):
                self.assertEqual(decode_or(bit), [meaning])
        self.assertEqual(decode_or(0), ["No off reason"])
        self.assertEqual(decode_or(129), ["No input power", "Engine shutdown detection"])

    def test_ble_capability_meanings(self):
        # CAP_BLE's radix and availability are ambiguous in 3.34. Test the
        # documented bit meanings directly, without assuming a wire radix.
        from ve_direct.tables import decode_cap_ble
        self.assertEqual(decode_cap_ble(1), ["BLE supports switching off"])
        self.assertEqual(decode_cap_ble(2), ["BLE switching off is permanent"])
        self.assertEqual(decode_cap_ble(3), ["BLE supports switching off", "BLE switching off is permanent"])

    def test_all_operation_states(self):
        expected = {
            0: "Off", 1: "Low power", 2: "Fault", 3: "Bulk", 4: "Absorption",
            5: "Float", 6: "Storage", 7: "Equalize (manual)", 9: "Inverting",
            11: "Power supply", 245: "Starting-up", 246: "Repeated absorption",
            247: "Auto equalize / Recondition", 248: "BatterySafe", 252: "External control",
        }
        for raw, meaning in expected.items():
            with self.subTest(raw=raw):
                frame = self.one([("CS", str(raw))])
                self.assertEqual(frame["CS"].value, raw)
                self.assertEqual(frame["CS"].human, meaning)

    def test_all_error_codes(self):
        # Meaning fragments are copied from the document, allowing the
        # library's longer notes for errors 19 and 21.
        expected = {
            0: "No error", 2: "Battery voltage too high", 17: "Charger temperature too high",
            18: "Charger over current", 19: "Charger current reversed", 20: "Bulk time limit exceeded",
            21: "Current sensor issue", 26: "Terminals overheated", 28: "Converter issue",
            33: "Input voltage too high", 34: "Input current too high", 38: "Input shutdown",
            39: "Input shutdown", 65: "Lost communication with one of devices",
            66: "Synchronised charging device configuration issue", 67: "BMS connection lost",
            68: "Network misconfigured", 116: "Factory calibration data lost",
            117: "Invalid/incompatible firmware", 119: "User settings invalid",
        }
        for raw, meaning in expected.items():
            with self.subTest(raw=raw):
                frame = self.one([("ERR", str(raw))])
                self.assertEqual(frame["ERR"].value, raw)
                self.assertIn(meaning, frame["ERR"].human)

    def test_modes_trackers_and_dc_monitor_types(self):
        expected = {
            "MODE": {1: "Charger", 2: "Inverter", 4: "Off", 5: "Eco", 253: "Hibernate"},
            "MPPT": {0: "Off", 1: "Voltage or current limited", 2: "MPP Tracker active"},
            "MON": {
                -9: "Solar charger", -8: "Wind turbine", -7: "Shaft generator", -6: "Alternator",
                -5: "Fuel cell", -4: "Water generator", -3: "DC/DC charger", -2: "AC charger",
                -1: "Generic source", 0: "Battery monitor (BMV)", 1: "Generic load",
                2: "Electric drive", 3: "Fridge", 4: "Water pump", 5: "Bilge pump",
                6: "DC system", 7: "Inverter", 8: "Water heater",
            },
        }
        for label, entries in expected.items():
            for raw, meaning in entries.items():
                with self.subTest(label=label, raw=raw):
                    frame = self.one([(label, str(raw))])
                    self.assertEqual(frame[label].value, raw)
                    self.assertIn(meaning, frame[label].human)

    def test_unknown_enum_and_bit_values_are_preserved(self):
        # Forward-compatible behavior is library policy. 3.34 provides no
        # mandated handling for values added by later firmware.
        for label in ("CS", "ERR", "MODE", "MPPT", "MON", "AR", "WARN", "OR", "CAP_BLE"):
            with self.subTest(label=label):
                frame = self.one([(label, "16384")])
                self.assertEqual(frame[label].value, 16384)
                self.assertIn("Unknown", frame[label].human)

    def test_product_id_serial_and_legacy_model(self):
        for raw, product, name in (("0x203", 0x203, "BMV-700"), ("0xA3F1", 0xA3F1, "Orion XS 1400")):
            with self.subTest(raw=raw):
                frame = self.one([("PID", raw), ("SER#", "HQ1328Y6TF6"), ("BMV", "602S")])
                self.assertEqual(frame["PID"].value, product)
                self.assertEqual(frame["PID"].human, name)
                self.assertEqual(frame["SER#"].value, "HQ1328Y6TF6")
                self.assertEqual(frame["BMV"].value, "602S")
        self.assertEqual(self.one([("PID", "0xFFFF")])["PID"].value, 65535)
        self.assertIn("Unknown", self.one([("PID", "0xFFFF")])["PID"].human)

    def test_day_sequence_full_domain_and_rollover(self):
        for day in range(365):
            self.assertEqual(self.one([("HSDS", str(day))])["HSDS"].value, day)
        stream = wire_block([("HSDS", "364")]) + wire_block([("HSDS", "0")])
        self.assertEqual(values(TextParser().feed(stream)), [{"HSDS": 364}, {"HSDS": 0}])


if __name__ == "__main__":
    unittest.main()
