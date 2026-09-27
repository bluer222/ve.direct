"""Acceptance checks against pinned public HEX specifications.

Run with PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tools:tests:conformance
python3 -m unittest test_hex_acceptance. Regressions fail normally.

Wire examples below are literal specification examples, not simulator output.
Derived fixtures use a local checksum oracle and never a production encoder.
The asynchronous cases cover the finite lifecycle transitions documented in
hex_requirements.json. They do not claim to exhaust all event schedules.
"""
import asyncio
import contextlib
import inspect
from pathlib import Path
import re
import types
import unittest
from unittest.mock import patch

from ve_direct.device import AsyncVEDirect
from ve_direct import hex as H
from ve_direct.exceptions import (
    ChecksumError, FrameError, RegisterError, TimeoutError as VETimeoutError,
    TransportError,
)
from ve_direct.tables import PID_MAP
from ve_direct.transport import SimulatedTransport


ROOT = Path(__file__).resolve().parents[1]


def wire_frame(command, payload=b""):
    """Independent arithmetic oracle for fixtures absent from example tables."""
    check = (85 - command - sum(payload)) % 256
    return b":" + format(command, "X").encode() + payload.hex().upper().encode() + format(check, "02X").encode() + b"\n"


def text_block(value=b"12000"):
    prefix = b"\r\nV\t" + value + b"\r\nChecksum\t"
    return prefix + bytes([(-sum(prefix)) % 256])


async def close_stream(stream):
    # unittest only awaits cleanup functions recognized as coroutine functions;
    # an async generator's builtin aclose method is not one of those.
    await stream.aclose()


# BlueSolar section 1.3, Phoenix Message examples, BMV Examples,
# Orion section 1.3. Duplicates shared between documents are listed once.
# Orion's printed :30300F4 is treated separately because its checksum is wrong.
EXAMPLES = (
    (b":154\n", 1, b""),
    (b":352\n", 3, b""),
    (b":451\n", 4, b""),
    (b":64F\n", 6, b""),
    (b":253\n", 2, b""),
    (b":2000152\n", 2, bytes.fromhex("0001")),
    (b":3020050\n", 3, bytes.fromhex("0200")),
    (b":4AAAAFD\n", 4, bytes.fromhex("AAAA")),
    (b":2AAAAFF\n", 2, bytes.fromhex("AAAA")),
    (b":51641F9\n", 5, bytes.fromhex("1641")),
    (b":11641FD\n", 1, bytes.fromhex("1641")),
    (b":1000351\n", 1, bytes.fromhex("0003")),
    (b":A0102000543\n", 10, bytes.fromhex("01020005")),
    (b":7F0ED0071\n", 7, bytes.fromhex("F0ED00")),
    (b":7F0ED009600DB\n", 7, bytes.fromhex("F0ED009600")),
    (b":8F0ED0064000C\n", 8, bytes.fromhex("F0ED006400")),
    (b":501440B\n", 5, bytes.fromhex("0144")),
    (b":101440F\n", 1, bytes.fromhex("0144")),
    (b":181A330\n", 1, bytes.fromhex("81A3")),
    (b":70010003E\n", 7, bytes.fromhex("001000")),
    (b":7001000C80076\n", 7, bytes.fromhex("001000C800")),
    (b":8001000F40148\n", 8, bytes.fromhex("001000F401")),
    (b":800100000003D\n", 8, bytes.fromhex("0010000000")),
    (b":8001004010038\n", 8, bytes.fromhex("0010040100")),
    (b":7001000F40149\n", 7, bytes.fromhex("001000F401")),
    (b":5FF7FD2\n", 5, bytes.fromhex("FF7F")),
    (b":8F0ED00F4017B\n", 8, bytes.fromhex("F0ED00F401")),
    (b":1F0A3C1\n", 1, bytes.fromhex("F0A3")),
    (b":70201004B\n", 7, bytes.fromhex("020100")),
    (b":702010000FF120139\n", 7, bytes.fromhex("02010000FF1201")),
    (b":7F0ED00F4017C\n", 7, bytes.fromhex("F0ED00F401")),
)


class HexWireAcceptance(unittest.TestCase):
    def test_every_literal_example_encodes_and_decodes(self):
        for literal, command, payload in EXAMPLES:
            with self.subTest(literal=literal):
                self.assertEqual(H.encode_frame(command, payload), literal)
                decoded = H.decode_line(literal, strict=True)
                self.assertEqual((decoded.cmd, decoded.data, decoded.valid), (command, payload, True))

    def test_all_byte_values_use_uppercase_hex_and_modulo_checksum(self):
        # Covers every octet, carries, zero/maximum nibble, and empty payload.
        for command in range(16):
            for payload in (b"", bytes(range(256)), b"\xff" * 513):
                with self.subTest(command=command, size=len(payload)):
                    frame = H.encode_frame(command, payload)
                    self.assertRegex(frame, rb"^:[0-9A-F](?:[0-9A-F]{2})+\n$")
                    self.assertEqual(frame, wire_frame(command, payload))
                    checksum = int(frame[-3:-1], 16)
                    self.assertEqual((command + sum(payload) + checksum) % 256, 85)

    def test_get_and_set_envelopes_use_little_endian_and_zero_flags(self):
        for register in (0, 0xFF, 0x100, 0x0102, 0x1000, 0xEDF0, 0xFFFF):
            with self.subTest(register=register):
                prefix = bytes([register % 256, register // 256, 0])
                self.assertEqual(H.build_get(register), wire_frame(7, prefix))
                for value in (b"", b"\x00", b"\x64\x00", b"\xff" * 4, b"serial\0"):
                    self.assertEqual(H.build_set(register, value), wire_frame(8, prefix + value))

    def test_fixed_command_builders(self):
        for builder, expected in (
            (H.build_ping, b":154\n"), (H.build_app_version, b":352\n"),
            (H.build_product_id, b":451\n"), (H.build_restart, b":64F\n"),
        ):
            with self.subTest(builder=builder.__name__):
                self.assertEqual(builder(), expected)

    def test_bad_checksums_and_unparseable_lines(self):
        # :452 is the deliberately invalid request in three source documents.
        self.assertFalse(H.decode_line(b":452\n").valid)
        with self.assertRaises(ChecksumError):
            H.decode_line(b":452\n", strict=True)
        for bad in (b"", b"154\n", b":\n", b":7\n", b":700\xff\n", b":G54\n", b":1234\n"):
            with self.subTest(bad=bad), self.assertRaises(FrameError):
                H.decode_line(bad, strict=True)

    def test_orion_unknown_example_conflicts_with_checksum_rule(self):
        # Do not bless the source typo as a valid frame.
        with self.assertRaises(ChecksumError):
            H.decode_line(b":30300F4\n", strict=True)
        self.assertEqual(H.encode_frame(3, b"\x03\x00"), b":303004F\n")
        self.assertEqual(H.decode_line(b":303004F\n", strict=True).data, b"\x03\x00")

    def test_direction_specific_names_and_register_envelopes(self):
        self.assertEqual(H.command_name(3, is_request=True), "AppVersion")
        self.assertEqual(H.command_name(3), "Unknown")
        self.assertEqual(H.command_name(4, is_request=True), "ProductId")
        self.assertEqual(H.command_name(4), "Error")
        for command in (7, 8, 10):
            frame = H.decode_line(wire_frame(command, b"\xf0\xed\x04\x01\x00"), strict=True)
            self.assertEqual((frame.register_id, frame.flags, frame.value_bytes), (0xEDF0, 4, b"\x01\x00"))
        for command in (1, 2, 3, 4, 5, 9, 15):
            frame = H.decode_line(wire_frame(command, b"\xf0\xed\x04"), strict=True)
            self.assertIsNone(frame.register_id)
            self.assertIsNone(frame.flags)
            self.assertEqual(frame.value_bytes, b"\xf0\xed\x04")

    def test_version_types_and_release_candidate_letters(self):
        for raw, version, kind in (
            (b"\x01\x01", "1.01", "bootloader"),
            (b"\x16\x41", "1.16", "application"),
            (b"\x01\x44", "4.01", "application"),
            (b"\x01\x81", "1.01", "tester"),
            (b"\x01\xc1", "1.01-RCC", "release_candidate"),
            (b"\x01\xd1", "1.01-RCD", "release_candidate"),
            (b"\x01\xe1", "1.01-RCE", "release_candidate"),
            (b"\x01\xf1", "1.01-RCF", "release_candidate"),
        ):
            with self.subTest(raw=raw):
                for decode in (H.decode_app_version, H.decode_ping_version):
                    result = decode(raw)
                    self.assertEqual((result["version"], result["type"]), (version, kind))

    def test_orion_ping_types_have_no_version(self):
        for raw, kind in (
            (b"\xff\x3f", "bootloader"), (b"\xff\x7f", "application"),
            (b"\xff\xbf", "tester"), (b"\xff\xff", "release_candidate"),
        ):
            with self.subTest(raw=raw):
                decoded = H.decode_ping_version(raw)
                self.assertIsNone(decoded["version"])
                self.assertEqual(decoded["type"], kind)

    def test_orion_24_bit_release_example(self):
        decoded = H.decode_app_version_24(bytes.fromhex("00FF1201"))
        self.assertEqual((decoded["version"], decoded["type"]), ("1.12", "release"))

    def test_catalogued_product_ids_are_available(self):
        # Direct extraction from bounded product tables, independent of the
        # repository's specification parsers. Electrical/model notes are not
        # host wire requirements and are recorded separately in the ledger.
        spans = (
            ("BlueSolar-HEX-protocol.txt", 96, 193, r"^\s*0x([0-9A-Fa-f]{4})\b", 80),
            ("Orion-XS-HEX-protocol.txt", 83, 87, r"^\s*0x([0-9A-Fa-f]{4})\b", 2),
            ("VE.Direct-HEX-Protocol-Phoenix-Inverter.txt", 119, 191, r"^\s*([0-9A-Fa-f]{4})\b", 50),
            ("BMV-7xx-HEX-Protocol.txt", 61, 64, r"=\s*0x([0-9A-Fa-f]{4})\b", 4),
        )
        for filename, start, end, pattern, minimum in spans:
            with self.subTest(document=filename):
                lines = (ROOT / "tests/specs" / filename).read_text().split("\n")[start - 1:end]
                ids = [int(match.group(1), 16) for line in lines if (match := re.search(pattern, line))]
                self.assertGreaterEqual(len(ids), minimum, "bounded source extraction unexpectedly empty or incomplete")
                self.assertEqual([f"0x{x:04X}" for x in ids if x not in PID_MAP], [])


class QueueWire:
    """A deterministic transport with independently supplied incoming bytes."""
    def __init__(self, on_write=None):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.reading = asyncio.Event()
        self.writes = []
        self.on_write = on_write

    async def read(self, n):
        self.reading.set()
        return await self.incoming.get()

    async def write(self, data):
        self.writes.append(bytes(data))
        self.sent.put_nowait(bytes(data))
        if self.on_write is not None:
            result = self.on_write(bytes(data))
            if inspect.isawaitable(result):
                await result


class HexClientAcceptance(unittest.IsolatedAsyncioTestCase):
    def device(self, wire, flavor="MPPT"):
        dev = AsyncVEDirect(wire.read, wire.write, flavor=flavor)
        self.addAsyncCleanup(dev.close)
        return dev

    def task(self, awaitable):
        task = asyncio.create_task(awaitable)
        async def finish():
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.addAsyncCleanup(finish)
        return task

    async def sent(self, wire, expected):
        self.assertEqual(await asyncio.wait_for(wire.sent.get(), .5), expected)

    async def test_request_response_examples(self):
        cases = (
            ("MPPT", "ping", (), b":154\n", b":51641F9\n", bytes.fromhex("1641")),
            ("BMV", "ping", (), b":154\n", b":501440B\n", bytes.fromhex("0144")),
            ("ORION", "ping", (), b":154\n", b":5FF7FD2\n", bytes.fromhex("FF7F")),
            ("INV", "get_app_version", (), b":352\n", b":11641FD\n", bytes.fromhex("1641")),
            ("BMV", "get_app_version", (), b":352\n", b":101440F\n", bytes.fromhex("0144")),
            ("MPPT", "hex_get_raw", (0xEDF0,), b":7F0ED0071\n", b":7F0ED009600DB\n", bytes.fromhex("9600")),
            ("ORION", "hex_get_raw", (0xEDF0,), b":7F0ED0071\n", b":7F0ED00F4017C\n", bytes.fromhex("F401")),
            ("BMV", "hex_get_raw", (0x1000,), b":70010003E\n", b":7001000C80076\n", bytes.fromhex("C800")),
            ("MPPT", "hex_set_raw", (0xEDF0, b"\x64\0"), b":8F0ED0064000C\n", b":8F0ED0064000C\n", b"\x64\0"),
            ("BMV", "hex_set_raw", (0x1000, b"\xf4\x01"), b":8001000F40148\n", b":8001000F40148\n", b"\xf4\x01"),
        )
        for flavor, method, args, request, reply, expected in cases:
            with self.subTest(flavor=flavor, method=method):
                wire = QueueWire()
                dev = self.device(wire, flavor)
                pending = self.task(getattr(dev, method)(*args, timeout=.5))
                await self.sent(wire, request)
                wire.incoming.put_nowait(reply)
                result = await pending
                if isinstance(result, H.HexFrame):
                    result = result.value_bytes
                self.assertEqual(result, expected)

    async def test_product_id_command_accepts_documented_two_byte_reply(self):
        for flavor, reply, pid in (
            ("BMV", b":181A330\n", 0xA381),
            ("BMV71", b":181A330\n", 0xA381),
            ("BMV", wire_frame(1, b"\xff\xff"), 0xFFFF),
            ("INV", b":1000351\n", 0x0300),
            ("MPPT", b":1000351\n", 0x0300),
            ("ORION", b":1F0A3C1\n", 0xA3F0),
            # MPPT_RS is in the BlueSolar product table; command :4 is
            # explicitly recommended on all models and software versions.
            ("MPPT_RS", wire_frame(1, b"\x10\xa1"), 0xA110),
            (None, b":181A330\n", 0xA381),
        ):
            with self.subTest(flavor=flavor):
                wire = QueueWire()
                dev = self.device(wire, flavor)
                pending = self.task(dev.get_product_id(timeout=.5))
                await self.sent(wire, b":451\n")
                wire.incoming.put_nowait(reply)
                self.assertEqual(await pending, pid)

    async def test_orion_version_uses_register_not_deprecated_command(self):
        wire = QueueWire()
        dev = self.device(wire, "ORION")
        pending = self.task(dev.get_app_version_info(timeout=.5))
        await self.sent(wire, b":70201004B\n")
        wire.incoming.put_nowait(b":702010000FF120139\n")
        result = await pending
        self.assertEqual((result["version"], result["type"]), ("1.12", "release"))
        self.assertEqual(wire.writes, [b":70201004B\n"])

    async def test_unknown_and_framing_errors_fail_current_request(self):
        for response, expected in (
            (b":303004F\n", RegisterError),
            (b":4AAAAFD\n", ChecksumError),
            (b":2AAAAFF\n", ChecksumError),
        ):
            for operation in ("command", "register"):
                with self.subTest(response=response, operation=operation):
                    wire = QueueWire()
                    dev = self.device(wire)
                    call = dev.get_app_version if operation == "command" else lambda **kw: dev.hex_get_raw(0xEDF0, **kw)
                    pending = self.task(call(timeout=.5))
                    await asyncio.wait_for(wire.sent.get(), .5)
                    wire.incoming.put_nowait(response)
                    with self.assertRaises(expected):
                        await pending
                    # An explicit rejection completes the transaction. A
                    # following successful request remains possible.
                    next_request = self.task(dev.ping(timeout=.5))
                    await self.sent(wire, b":154\n")
                    wire.incoming.put_nowait(b":51641F9\n")
                    self.assertEqual((await next_request).data, b"\x16\x41")

    async def test_each_get_set_error_flag_is_an_error_not_a_value(self):
        for command in (7, 8):
            for flag, reason in ((1, "unknown register"), (2, "not supported"), (4, "parameter error"), (7, "parameter error")):
                with self.subTest(command=command, flag=flag):
                    wire = QueueWire()
                    dev = self.device(wire)
                    call = dev.hex_get_raw(0xEDF0, timeout=.5) if command == 7 else dev.hex_set_raw(0xEDF0, b"\0\0", timeout=.5)
                    pending = self.task(call)
                    await asyncio.wait_for(wire.sent.get(), .5)
                    wire.incoming.put_nowait(wire_frame(command, bytes([0xF0, 0xED, flag, 1, 0])))
                    with self.assertRaisesRegex(RegisterError, reason):
                        await pending

    async def test_bmv_parameter_error_example_never_returns_nearest_value_as_success(self):
        wire = QueueWire()
        dev = self.device(wire, "BMV")
        pending = self.task(dev.hex_set_raw(0x1000, b"\0\0", timeout=.5))
        await self.sent(wire, b":800100000003D\n")
        wire.incoming.put_nowait(b":8001004010038\n")
        with self.assertRaisesRegex(RegisterError, "parameter error"):
            await pending

    async def test_reply_routing_requires_operation_and_register(self):
        wire = QueueWire()
        dev = self.device(wire)
        pending = self.task(dev.hex_get_raw(0xEDF0, timeout=.5))
        await self.sent(wire, b":7F0ED0071\n")
        # Wrong response code, wrong id, unrelated Done, then correct reply.
        wire.incoming.put_nowait(
            b":8F0ED0064000C\n"
            + wire_frame(7, b"\xf1\xed\0\x01")
            + b":11641FD\n:7F0ED009600DB\n"
        )
        self.assertEqual((await pending).value_bytes, b"\x96\0")
        # A Done reply must not satisfy Ping; Ping has response code 5.
        pending = self.task(dev.ping(timeout=.5))
        await self.sent(wire, b":154\n")
        wire.incoming.put_nowait(b":11641FD\n:51641F9\n")
        self.assertEqual((await pending).cmd, 5)

    async def test_raw_unknown_register_payload_remains_available(self):
        wire = QueueWire()
        dev = self.device(wire)
        pending = self.task(dev.hex_get_raw(0xDEAD, timeout=.5))
        await self.sent(wire, wire_frame(7, b"\xad\xde\0"))
        value = bytes(range(256))
        wire.incoming.put_nowait(wire_frame(7, b"\xad\xde\0" + value))
        self.assertEqual((await pending).value_bytes, value)

    async def test_corrupt_or_truncated_response_does_not_publish_a_value(self):
        for bad in (b":7F0ED009600DC\n", b":Q00\n", wire_frame(7, b"\xf0\xed")):
            with self.subTest(bad=bad):
                wire = QueueWire()
                dev = self.device(wire)
                pending = self.task(dev.hex_get_raw(0xEDF0, timeout=.5))
                await self.sent(wire, b":7F0ED0071\n")
                with self.assertLogs("ve_direct.device", level="WARNING"):
                    wire.incoming.put_nowait(bad + b":7F0ED009600DB\n")
                    self.assertEqual((await pending).value_bytes, b"\x96\0")

    async def test_empty_done_and_ping_cannot_report_success(self):
        for command, method in ((1, "get_app_version"), (5, "ping")):
            for payload in (b"", b"\x01"):
                with self.subTest(command=command, payload=payload):
                    wire = QueueWire()
                    dev = self.device(wire)
                    pending = self.task(getattr(dev, method)(timeout=.5))
                    await asyncio.wait_for(wire.sent.get(), .5)
                    wire.incoming.put_nowait(wire_frame(command, payload))
                    with self.assertRaises(RegisterError):
                        await pending

    async def test_restart_is_transmitted_without_waiting_for_a_reply(self):
        wire = QueueWire()
        dev = self.device(wire)
        await asyncio.wait_for(dev.restart(), .2)
        self.assertEqual(wire.writes, [b":64F\n"])
        self.assertFalse(wire.reading.is_set())

    async def test_bmv_reserved_boot_command_is_not_transmitted(self):
        for flavor in ("BMV", "BMV71"):
            with self.subTest(flavor=flavor):
                wire = QueueWire()
                dev = self.device(wire, flavor)
                with self.assertRaises(RegisterError):
                    await dev.boot()
                self.assertEqual(wire.writes, [])

    async def test_boot_command_contains_documented_magic_without_assuming_byte_order(self):
        for flavor in ("MPPT", "INV", "ORION"):
            with self.subTest(flavor=flavor):
                wire = QueueWire()
                dev = self.device(wire, flavor)
                await asyncio.wait_for(dev.boot(), .5)
                self.assertEqual(len(wire.writes), 1)
                sent = wire.writes[0]
                self.assertRegex(sent, rb"^:0(?:[0-9A-F]{2}){11}\n$")
                payload = bytes.fromhex(sent[2:-3].decode("ascii"))
                # Docs specify magic 0x51FA... and also say numbers are LE;
                # no boot example settles whether the magic is a number or
                # a byte sequence. Do not pick an interpretation silently.
                self.assertIn(payload, (bytes.fromhex("51FA" * 5), bytes.fromhex("FA51" * 5)))
                self.assertEqual((sum(payload) + int(sent[-3:-1], 16)) % 256, 85)

    async def test_boot_failure_is_not_assigned_to_next_command(self):
        wire = QueueWire()
        def answer(data):
            if data.startswith(b":0"):
                wire.incoming.put_nowait(b":40051\n")
            elif data == b":154\n":
                wire.incoming.put_nowait(b":51641F9\n")
        wire.on_write = answer
        dev = self.device(wire)
        # Both an explicit boot error and an API that consumes/reports that
        # error separately are acceptable; misattributing it to Ping is not.
        with contextlib.suppress(RegisterError):
            await asyncio.wait_for(dev.boot(), .5)
        self.assertEqual((await dev.ping(timeout=.5)).data, b"\x16\x41")

    async def test_serial_constructor_uses_19200_and_configurable_control_lines(self):
        calls = []
        serial = types.SimpleNamespace(dtr=None, rts=None)
        writer = types.SimpleNamespace(transport=types.SimpleNamespace(serial=serial), close=lambda: None)
        async def open_connection(**kwargs):
            calls.append(kwargs)
            return object(), writer
        fake = types.SimpleNamespace(open_serial_connection=open_connection)
        with patch.dict("sys.modules", {"serial_asyncio": fake}):
            dev = await AsyncVEDirect.from_port("/dev/acceptance-test")
            self.addAsyncCleanup(dev.close)
            self.assertEqual(calls, [{"url": "/dev/acceptance-test", "baudrate": 19200}])
            self.assertEqual((serial.dtr, serial.rts), (True, False))

    async def test_simulator_product_id_responses_follow_command_examples(self):
        for flavor, product_id, expected in (
            ("BMV", 0xA381, b":181A330\n"),
            ("INV", 0x0300, b":1000351\n"),
            ("MPPT", 0x0300, b":1000351\n"),
            ("ORION", 0xA3F0, b":1F0A3C1\n"),
        ):
            with self.subTest(flavor=flavor):
                simulator = SimulatedTransport(flavor=flavor, product_id=product_id)
                await simulator.write(b":451\n")
                self.assertEqual(await simulator.read_chunk(1024), expected)


# Keep lifecycle cases separate in the requirement ledger without duplicate
# discovery of the client checks.
class HexLifecycleAcceptance(unittest.IsolatedAsyncioTestCase):
    device = HexClientAcceptance.device
    task = HexClientAcceptance.task
    sent = HexClientAcceptance.sent

    async def test_transactions_are_serialized_across_command_and_register_calls(self):
        for first_kind, second_kind in (("get", "get"), ("get", "ping"), ("ping", "get"), ("ping", "ping")):
            with self.subTest(first=first_kind, second=second_kind):
                wire = QueueWire()
                dev = self.device(wire)
                def call(kind):
                    return dev.ping(timeout=.5) if kind == "ping" else dev.hex_get_raw(0xEDF0, timeout=.5)
                first = self.task(call(first_kind))
                await asyncio.wait_for(wire.sent.get(), .5)
                second = self.task(call(second_kind))
                await asyncio.sleep(0)
                self.assertEqual(len(wire.writes), 1)
                wire.incoming.put_nowait(b":51641F9\n" if first_kind == "ping" else b":7F0ED009600DB\n")
                await first
                await asyncio.wait_for(wire.sent.get(), .5)
                wire.incoming.put_nowait(b":51641F9\n" if second_kind == "ping" else b":7F0ED009600DB\n")
                await second
                self.assertEqual(len(wire.writes), 2)

    async def test_timeout_and_cancellation_after_write_fail_closed(self):
        for kind in ("register", "command"):
            for end in ("timeout", "cancel"):
                with self.subTest(kind=kind, end=end):
                    wire = QueueWire()
                    dev = self.device(wire)
                    call = dev.get_app_version(timeout=.02) if kind == "command" else dev.hex_get_raw(0xEDF0, timeout=.02)
                    pending = self.task(call)
                    await asyncio.wait_for(wire.reading.wait(), .5)
                    if end == "cancel":
                        pending.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await pending
                    else:
                        with self.assertRaises(VETimeoutError):
                            await pending
                    with self.assertRaises(TransportError):
                        await dev.ping(timeout=.1)
                    self.assertEqual(len(wire.writes), 1)

    async def test_cancellation_during_write_must_also_fail_closed(self):
        for kind in ("register", "command"):
            with self.subTest(kind=kind):
                sent = asyncio.Event()
                wire = QueueWire()
                async def write(data):
                    if len(wire.writes) == 1:
                        wire.incoming.put_nowait(b":11641FD\n" if kind == "command" else b":7F0ED009600DB\n")
                        sent.set()
                        # StreamWriter.write() already transmitted bytes;
                        # cancellation happens while drain() is suspended.
                        await asyncio.Event().wait()
                    else:
                        wire.incoming.put_nowait(wire_frame(1, b"\x17\x41") if kind == "command" else b":7F0ED0064000D\n")
                wire.on_write = write
                dev = self.device(wire)
                def call():
                    return dev.get_app_version(timeout=.5) if kind == "command" else dev.hex_get_raw(0xEDF0, timeout=.5)
                first = self.task(call())
                await asyncio.wait_for(sent.wait(), .5)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                with self.assertRaises(TransportError, msg="sent request can still have a delayed response"):
                    await call()
                self.assertEqual(len(wire.writes), 1)

    async def test_cancelling_a_queued_transaction_preserves_active_transaction(self):
        wire = QueueWire()
        dev = self.device(wire)
        first = self.task(dev.hex_get_raw(0xEDF0, timeout=.5))
        await self.sent(wire, b":7F0ED0071\n")
        queued = self.task(dev.get_app_version(timeout=.5))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        wire.incoming.put_nowait(b":7F0ED009600DB\n")
        self.assertEqual((await first).value_bytes, b"\x96\0")
        next_request = self.task(dev.ping(timeout=.5))
        await self.sent(wire, b":154\n")
        wire.incoming.put_nowait(b":51641F9\n")
        await next_request
        self.assertEqual(len(wire.writes), 2)

    async def test_reset_after_external_input_drain_reenables_transactions(self):
        wire = QueueWire()
        dev = self.device(wire)
        with self.assertRaises(VETimeoutError):
            await dev.hex_get_raw(0xEDF0, timeout=.02)
        wire.incoming.put_nowait(b":7F0ED009600DB\n")
        # The caller really drains the old transport before invoking reset.
        self.assertEqual(wire.incoming.get_nowait(), b":7F0ED009600DB\n")
        dev.reset_hex_state()
        wire.on_write = lambda data: wire.incoming.put_nowait(b":7F0ED0064000D\n")
        self.assertEqual((await dev.hex_get_raw(0xEDF0, timeout=.5)).value_bytes, b"\x64\0")

    async def test_restart_cannot_make_buffered_stale_replies_safe(self):
        for stale_location in ("transport", "partial_parser"):
            with self.subTest(stale_location=stale_location):
                wire = QueueWire()
                dev = self.device(wire)
                if stale_location == "partial_parser":
                    # The previous request consumes a partial response and
                    # then times out; its remaining bytes arrive later.
                    wire.incoming.put_nowait(b":7F0ED0096")
                with self.assertRaises(VETimeoutError):
                    await dev.hex_get_raw(0xEDF0, timeout=.02)
                wire.incoming.put_nowait(b"00DB\n" if stale_location == "partial_parser" else b":7F0ED009600DB\n")
                def answer(data):
                    if data == b":7F0ED0071\n":
                        wire.incoming.put_nowait(b":7F0ED0064000D\n")
                wire.on_write = answer
                await dev.restart()
                try:
                    result = await dev.hex_get_raw(0xEDF0, timeout=.5)
                except TransportError:
                    # Remaining closed until an external drain is safe.
                    continue
                self.assertEqual(result.value_bytes, b"\x64\0", "restart admitted pre-restart response")

    async def test_text_async_and_reply_survive_every_chunk_boundary(self):
        block = text_block()
        notification = b":A0102000543\n"
        reply = b":7F0ED009600DB\n"
        # Async can interrupt a Text record; its bytes are outside the Text
        # checksum. Also cover a response adjacent to a binary checksum.
        wire_data = block[:7] + notification + block[7:] + reply
        for cut in range(len(wire_data) + 1):
            with self.subTest(cut=cut):
                wire = QueueWire()
                dev = self.device(wire)
                pending = self.task(dev.hex_get_raw(0xEDF0, timeout=.5))
                await self.sent(wire, b":7F0ED0071\n")
                for part in (wire_data[:cut], wire_data[cut:]):
                    if part:
                        wire.incoming.put_nowait(part)
                self.assertEqual((await pending).value_bytes, b"\x96\0")
                self.assertEqual((await dev.read_one(timeout=.2))["V"].value, 12000)
                notes = dev.async_notifications()
                try:
                    note = await asyncio.wait_for(notes.__anext__(), .2)
                    self.assertEqual(note["register_id"], 0x0201)
                    self.assertIn(note["value"], ("float", "floating"))
                finally:
                    await notes.aclose()
                self.assertEqual(wire.writes, [b":7F0ED0071\n"], "Async must not be acknowledged")

    async def test_async_alone_is_typed_and_never_acknowledged(self):
        wire = QueueWire()
        dev = self.device(wire)
        wire.incoming.put_nowait(b":A0102000543\n")
        notes = dev.async_notifications()
        try:
            note = await asyncio.wait_for(notes.__anext__(), .5)
            self.assertEqual((note["register_id"], note["flags"]), (0x0201, 0))
            self.assertIn(note["value"], ("float", "floating"))
            self.assertEqual(wire.writes, [])
        finally:
            await notes.aclose()

    async def test_async_waiting_behind_text_reader_uses_already_queued_notification(self):
        wire = QueueWire()
        dev = self.device(wire)
        text_read = self.task(dev.read_one(timeout=.5))
        await asyncio.wait_for(wire.reading.wait(), .5)
        notes = dev.async_notifications()
        self.addAsyncCleanup(close_stream, notes)
        notification = self.task(notes.__anext__())
        await asyncio.sleep(0)
        wire.incoming.put_nowait(b":A0102000543\n" + text_block())
        self.assertEqual((await text_read)["V"].value, 12000)
        # No further wire bytes should be necessary to deliver the event
        # already read and enqueued by the competing Text consumer.
        note = await asyncio.wait_for(notification, .2)
        self.assertIn(note["value"], ("float", "floating"))

    async def test_competing_text_readers_recheck_already_queued_frames(self):
        wire = QueueWire()
        dev = self.device(wire)
        first = self.task(dev.read_one(timeout=.5))
        await asyncio.wait_for(wire.reading.wait(), .5)
        second = self.task(dev.read_one(timeout=.5))
        await asyncio.sleep(0)
        wire.incoming.put_nowait(text_block(b"12000") + text_block(b"13000"))
        results = await asyncio.wait_for(asyncio.gather(first, second), .5)
        self.assertEqual(sorted(frame["V"].value for frame in results), [12000, 13000])

    async def test_text_consumer_can_pump_a_pending_hex_reply(self):
        wire = QueueWire()
        dev = self.device(wire)
        stream = dev.frames()
        self.addAsyncCleanup(close_stream, stream)
        text_read = self.task(stream.__anext__())
        await asyncio.wait_for(wire.reading.wait(), .5)
        register_read = self.task(dev.hex_get_raw(0xEDF0, timeout=.5))
        await self.sent(wire, b":7F0ED0071\n")
        wire.incoming.put_nowait(b":7F0ED009600DB\n" + text_block())
        self.assertEqual((await register_read).value_bytes, b"\x96\0")
        self.assertEqual((await text_read)["V"].value, 12000)

    async def test_eof_and_closed_connection_are_transport_errors(self):
        for method in ("read_one", "ping"):
            with self.subTest(method=method):
                wire = QueueWire()
                dev = self.device(wire)
                wire.incoming.put_nowait(b"")
                with self.assertRaises(TransportError):
                    await getattr(dev, method)(timeout=.5)
                await dev.close()
                with self.assertRaises(TransportError):
                    await dev.hex_get_raw(0xEDF0, timeout=.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
