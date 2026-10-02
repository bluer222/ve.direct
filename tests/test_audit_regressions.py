"""Regression checks for transport lifecycle, typed values and bounded telemetry."""
from __future__ import annotations

import asyncio
import struct
import sys
import unittest
from contextlib import suppress
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "src")

from ve_direct import AsyncVEDirect, TextParser, TimeoutError, VeDirectError
from ve_direct import hex as H
from ve_direct import registers as R
from ve_direct.exceptions import FrameError, RegisterError, TransportError
from ve_direct.transport import SimulatedTransport, make_text_block


def register_reply(command, register_id, value=b"", flags=0):
    return H.encode_frame(command, struct.pack("<HB", register_id, flags) + value)


class TestTransportFailures(unittest.IsolatedAsyncioTestCase):
    async def test_read_errors_and_eof_block_stale_get_set_and_command_replies(self):
        for kind in ("get", "set", "command"):
            for failure in (OSError("read failed"), b""):
                with self.subTest(kind=kind, failure=failure):
                    incoming = asyncio.Queue()
                    writes = []

                    async def write(data):
                        writes.append(data)
                        if kind == "command":
                            incoming.put_nowait(H.encode_frame(H.CMD_DONE, b"\x96\x00"))
                        else:
                            command = H.CMD_GET if kind == "get" else H.CMD_SET
                            incoming.put_nowait(register_reply(command, 0xEDF0, b"\x96\x00"))

                    async def read(n):
                        if isinstance(failure, Exception):
                            raise failure
                        return failure

                    dev = AsyncVEDirect(read, write, flavor="MPPT")

                    async def request():
                        if kind == "command":
                            return await dev.get_app_version(timeout=.1)
                        if kind == "set":
                            return await dev.hex_set_raw(0xEDF0, b"\x64\x00", timeout=.1)
                        return await dev.hex_get_raw(0xEDF0, timeout=.1)

                    expected = OSError if isinstance(failure, Exception) else TransportError
                    with self.assertRaises(expected):
                        await request()
                    with self.assertRaisesRegex(TransportError, "desynchronized"):
                        await request()
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(incoming.qsize(), 1)
                    await dev.close()

    async def test_competing_stream_read_error_fails_pending_requests_immediately(self):
        for stream_kind in ("text", "async"):
            for kind in ("get", "set", "command"):
                with self.subTest(stream=stream_kind, kind=kind):
                    started = asyncio.Event()
                    written = asyncio.Event()
                    writes = []

                    async def read(n):
                        started.set()
                        await written.wait()
                        raise OSError("competing reader failed")

                    async def write(data):
                        writes.append(data)
                        written.set()

                    dev = AsyncVEDirect(read, write, flavor="MPPT")
                    stream = dev.frames() if stream_kind == "text" else dev.async_notifications()
                    consumer = asyncio.create_task(stream.__anext__())
                    await started.wait()
                    if kind == "get":
                        request = dev.hex_get_raw(0xEDF0, timeout=10)
                    elif kind == "set":
                        request = dev.hex_set_raw(0xEDF0, b"\x64\x00", timeout=10)
                    else:
                        request = dev.get_app_version(timeout=10)
                    with self.assertRaisesRegex(OSError, "competing reader"):
                        await asyncio.wait_for(request, .5)
                    with self.assertRaises(OSError):
                        await consumer
                    with self.assertRaises(TransportError):
                        await dev.ping(timeout=.1)
                    self.assertEqual(len(writes), 1)
                    await dev.close()
                    await stream.aclose()

    async def test_correlated_device_rejections_leave_routing_usable(self):
        sim = SimulatedTransport(parameter_error_registers={0xEDF0})
        dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
        with self.assertRaises(RegisterError):
            await dev.hex_get("BAT_MAX_CURRENT")
        with self.assertRaises(RegisterError):
            await dev.hex_set("BAT_MAX_CURRENT", 10)
        self.assertEqual((await dev.ping()).data, b"\x16\x41")
        self.assertEqual(await dev.hex_get("PANEL_VOLTAGE"), 0)
        await dev.close()

    async def test_close_wakes_all_consumers_and_pending_command(self):
        started = asyncio.Event()
        wrote = asyncio.Event()
        transport_read_stopped = asyncio.Event()
        cleanup = mock.AsyncMock()

        async def read(n):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                transport_read_stopped.set()

        async def write(data):
            wrote.set()

        dev = AsyncVEDirect(read, write, close_transport=cleanup)
        text = dev.frames()
        notifications = dev.async_notifications()
        unrelated = asyncio.create_task(asyncio.Event().wait())
        consumers = [asyncio.create_task(text.__anext__()),
                     asyncio.create_task(notifications.__anext__()),
                     asyncio.create_task(dev.read_one(timeout=10))]
        await started.wait()
        consumers.append(asyncio.create_task(dev.get_app_version(timeout=10)))
        await wrote.wait()
        await asyncio.wait_for(asyncio.gather(dev.close(), dev.close()), .5)
        results = await asyncio.wait_for(asyncio.gather(*consumers, return_exceptions=True), .5)
        self.assertTrue(all(isinstance(result, TransportError) for result in results), results)
        self.assertTrue(transport_read_stopped.is_set())
        cleanup.assert_awaited_once_with()
        self.assertFalse(unrelated.done())
        self.assertIsNone(dev._read_task)
        self.assertIsNone(dev._write_task)
        self.assertTrue(dev._close_task.done())
        await text.aclose()
        await notifications.aclose()
        unrelated.cancel()
        with suppress(asyncio.CancelledError):
            await unrelated

    async def test_close_wakes_pending_transaction_during_write(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def write(data):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def read(n):
            self.fail("read must not start before the blocked write completes")

        dev = AsyncVEDirect(read, write)
        request = asyncio.create_task(dev.ping(timeout=10))
        await started.wait()
        await asyncio.wait_for(dev.close(), .5)
        with self.assertRaises(TransportError):
            await asyncio.wait_for(request, .5)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(dev._write_task)

    async def test_text_timeout_uses_library_error_and_cleans_up_read(self):
        stopped = asyncio.Event()

        async def read(n):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        dev = AsyncVEDirect(read, lambda data: None)
        with self.assertRaises(TimeoutError) as caught:
            await dev.read_one(timeout=.01)
        self.assertIsInstance(caught.exception, VeDirectError)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(dev._read_task)
        await dev.close()


class TestTypedAndFamilyBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_load_labels_fail_before_transmission(self):
        sim = SimulatedTransport()
        dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
        for label in ("autp", "OFF"):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "unknown enum label"):
                await dev.hex_set("LOAD_OUTPUT_CONTROL", label)
        self.assertEqual(sim.written, [])
        self.assertEqual((await dev.hex_set("LOAD_OUTPUT_CONTROL", "off"))["mode_name"], "off")
        await dev.close()

    async def test_lowercase_flavors_and_assignments_preserve_command_rules(self):
        for flavor in ("orion", "OrIoN"):
            sim = SimulatedTransport(flavor="ORION", hex_replies={0x0102: bytes.fromhex("00FF0201")})
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor=flavor)
            self.assertEqual(dev.flavor, "ORION")
            self.assertEqual((await dev.get_app_version_info())["version"], "1.02")
            self.assertEqual(H.decode_line(sim.written[-1]).cmd, H.CMD_GET)
            dev.flavor = "orion"
            self.assertEqual((await dev.get_app_version_info())["version"], "1.02")
            await dev.close()
        for flavor in ("bmv", "bmv71"):
            sim = SimulatedTransport(flavor=flavor)
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor=flavor)
            with self.assertRaisesRegex(RegisterError, "reserved"):
                await dev.boot(timeout=0)
            dev.flavor = flavor
            with self.assertRaises(RegisterError):
                await dev.boot(timeout=0)
            self.assertEqual(sim.written, [])
            await dev.close()

    async def test_short_typed_get_set_and_notifications_do_not_fabricate_values(self):
        for name, payload in (("BMS_PRESENT", b""), ("CAPABILITIES", b""),
                              ("WARNING_REASON", b"\x01"), ("PANEL_VOLTAGE", b"\x01"),
                              ("DEVICE_STATE", b"")):
            with self.subTest(name=name):
                incoming = asyncio.Queue()
                reg = R.lookup(name, flavor="MPPT")

                async def read(n):
                    return await incoming.get()

                async def write(data):
                    incoming.put_nowait(register_reply(H.CMD_GET, reg.id, payload))

                dev = AsyncVEDirect(read, write, flavor="MPPT")
                with self.assertRaisesRegex(RegisterError, "truncated"):
                    await dev.hex_get(name)
                self.assertEqual((await dev.hex_get_raw(reg.id)).value_bytes, payload)
                notification = H.decode_line(register_reply(H.CMD_ASYNC, reg.id, payload))
                with self.assertRaises(RegisterError):
                    dev.decode_async_frame(notification)
                flagged = H.decode_line(register_reply(H.CMD_ASYNC, reg.id, payload, flags=4))
                error = dev.decode_async_frame(flagged)
                self.assertEqual(error["flags"], 4)
                self.assertEqual(error["value"], payload)
                await dev.close()

        incoming = asyncio.Queue()

        async def read(n):
            return await incoming.get()

        async def write(data):
            frame = H.decode_line(data)
            incoming.put_nowait(register_reply(H.CMD_SET, frame.register_id))

        dev = AsyncVEDirect(read, write, flavor="MPPT")
        with self.assertRaisesRegex(RegisterError, "truncated"):
            await dev.hex_set("BAT_MAX_CURRENT", 10)
        self.assertIsNone(await dev.hex_set("CLEAR_HISTORY", b""))
        self.assertEqual((await dev.hex_set_raw(0xEDF0, b"\x64\x00")).value_bytes, b"")
        await dev.close()


class TestTelemetryLimits(unittest.IsolatedAsyncioTestCase):
    async def test_polling_bounds_both_queues_and_preserves_request_routing(self):
        for capacity, count in ((None, 1000), (3, 12), (5, 4)):
            with self.subTest(capacity=capacity, count=count):
                incoming = asyncio.Queue()
                counter = 0

                async def read(n):
                    return await incoming.get()

                async def write(data):
                    nonlocal counter
                    counter += 1
                    incoming.put_nowait(
                        make_text_block({"V": str(counter)})
                        + register_reply(H.CMD_ASYNC, 0xEDBB, struct.pack("<H", counter))
                        + register_reply(H.CMD_GET, 0xEDF0, b"\x64\x00"))

                options = {} if capacity is None else {"text_queue_size": capacity,
                                                       "async_queue_size": capacity}
                dev = AsyncVEDirect(read, write, flavor="MPPT", **options)
                for _ in range(count):
                    self.assertEqual(await dev.hex_get("BAT_MAX_CURRENT"), 10)
                limit = 256 if capacity is None else capacity
                retained = min(limit, count)
                self.assertEqual(dev._text_queue.qsize(), retained)
                self.assertEqual(dev.async_frames.qsize(), retained)
                self.assertEqual(dev.dropped_text_frames, max(0, count - limit))
                self.assertEqual(dev.dropped_async_frames, max(0, count - limit))
                text_values = [(await dev.read_one())["V"].value for _ in range(retained)]
                async_values = [int.from_bytes(dev.async_frames.get_nowait().value_bytes, "little")
                                for _ in range(retained)]
                expected = list(range(count - retained + 1, count + 1))
                self.assertEqual(text_values, expected)
                self.assertEqual(async_values, expected)
                await dev.close()

    async def test_factories_propagate_queue_limits_and_validate_before_open(self):
        writer = SimpleNamespace(write=mock.Mock(), drain=mock.AsyncMock(), close=mock.Mock())
        reader = asyncio.StreamReader()
        dev = AsyncVEDirect.from_streams(reader, writer, text_queue_size=2, async_queue_size=4)
        self.assertEqual(dev._text_queue.maxsize, 2)
        self.assertEqual(dev.async_frames.maxsize, 4)
        await dev.close()
        opener = mock.AsyncMock(return_value=(reader, writer))
        with mock.patch.dict(sys.modules, {"serial_asyncio": SimpleNamespace(open_serial_connection=opener)}):
            for bad in (0, -1, True, 1.5, None):
                for name in ("text_queue_size", "async_queue_size"):
                    with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                        await AsyncVEDirect.from_port("fake", **{name: bad})
            opener.assert_not_awaited()
            dev = await AsyncVEDirect.from_port("fake", text_queue_size=7, async_queue_size=9)
            self.assertEqual(dev._text_queue.maxsize, 7)
            self.assertEqual(dev.async_frames.maxsize, 9)
            await dev.close()


class TestCodecBoundaries(unittest.TestCase):
    def test_integer_orion_versions_keep_register_padding(self):
        for raw in (bytes.fromhex("00FF0800"), bytes.fromhex("00FF0000"),
                    bytes.fromhex("00FF1201"), bytes.fromhex("00120800")):
            with self.subTest(raw=raw):
                self.assertEqual(H.decode_app_version_24(int.from_bytes(raw, "little")),
                                 H.decode_app_version_24(raw))
        self.assertEqual(H.decode_app_version_24(0x0008FF00)["version"], "0.08")

    def test_mixed_valid_and_invalid_text_values_convert_independently(self):
        frame = TextParser().feed(make_text_block({"V": "invalid", "I": "1000", "SOC": "bad",
                                                  "H20": "250", "CE": "---"}))[0]
        converted = frame.to_si()
        self.assertEqual(converted["V"], "invalid")
        self.assertEqual(converted["SOC"], "bad")
        self.assertEqual(converted["I"], 1)
        self.assertEqual(converted["H20"], 2.5)
        self.assertIsNone(converted["CE"])

    def test_every_typed_scalar_rejects_short_payloads(self):
        widths = {"uint8_t": 1, "int8_t": 1, "uint16_t": 2, "int16_t": 2,
                  "uint24_t": 3, "uint32_t": 4, "int32_t": 4, "uint16_or_uint32": 2}
        for reg in R.REGISTERS.values():
            if reg.cls not in ("NUMERIC", "ENUM", "BOOLEAN", "BITMASK"):
                continue
            width = widths.get(reg.dtype, 1)
            with self.subTest(name=reg.name):
                with self.assertRaises(RegisterError):
                    R.decode_value(reg, b"\x00" * (width - 1))
        yield_reg = R.lookup("YIELD_TODAY")
        self.assertEqual(R.decode_value(yield_reg, b"\x01\x00"), .01)
        self.assertEqual(R.decode_value(yield_reg, b"\x01\x00\x00\x00"), .01)
        with self.assertRaises(RegisterError):
            R.decode_value(yield_reg, b"\x01\x00\x00")


class TestChecksumFieldBoundaries(unittest.TestCase):
    def test_checksum_is_an_exact_field_at_every_chunk_split(self):
        block = make_text_block({"V": "12345", "XChecksum": "42",
                                 "ChecksumSuffix": "123", "X": "prefixChecksum\t42"})
        for split in range(len(block) + 1):
            with self.subTest(split=split):
                parser = TextParser()
                frames = parser.feed(block[:split]) + parser.feed(block[split:])
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0]["V"].value, 12345)
                self.assertEqual(frames[0]["XChecksum"].raw, "42")
                self.assertEqual(frames[0]["X"].raw, "prefixChecksum\t42")
                self.assertEqual(parser.pending_bytes, 0)
        parser = TextParser()
        frames = [frame for byte in block for frame in parser.feed(bytes([byte]))]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["XChecksum"].raw, "42")

    def test_checksum_prefix_collision_never_publishes_invalid_voltage(self):
        bad = b"\r\nV\t499\r\nXChecksum\t92\r\nChecksum\t|"
        seed = make_text_block({"V": "999"})
        recovery = make_text_block({"V": "123"})
        self.assertEqual(sum(bad) % 256, 1)
        schedules = [[bad[:split], bad[split:]] for split in range(len(bad) + 1)]
        schedules.append([bytes([byte]) for byte in bad])
        for index, chunks in enumerate(schedules):
            with self.subTest(schedule=index):
                parser = TextParser()
                self.assertEqual(parser.feed(seed)[0]["V"].value, 999)
                for chunk in chunks:
                    self.assertEqual(parser.feed(chunk), [])
                self.assertEqual(parser.feed(recovery)[0]["V"].value, 123)
                self.assertEqual(parser.pending_bytes, 0)

    def test_exact_checksum_boundary_survives_hex_interruptions(self):
        block = make_text_block({"V": "100", "XChecksum": "42", "X": "Checksum\t1"})
        notification = register_reply(H.CMD_ASYNC, 0xEDBB, b"\x01\x00")
        points = (block.index(b"XChecksum\t") + len(b"XChecksum\t"),
                  block.index(b"X\tChecksum\t") + len(b"X\tChecksum\t"),
                  block.index(b"\r\nChecksum\t") + 5)
        for point in points:
            mixed = block[:point] + notification + block[point:]
            for split in range(len(mixed) + 1):
                with self.subTest(point=point, split=split):
                    parser = TextParser()
                    first, first_hex = parser.feed_events(mixed[:split])
                    last, last_hex = parser.feed_events(mixed[split:])
                    self.assertEqual(first_hex + last_hex, [notification])
                    self.assertEqual(len(first + last), 1)
                    self.assertEqual((first + last)[0]["XChecksum"].raw, "42")

    def test_all_checksum_byte_values_with_unknown_checksum_label(self):
        alphabet = set(range(33, 127)) - {ord(":")}
        residues = {}
        for residue in range(256):
            for first in sorted(alphabet):
                for second in sorted(alphabet):
                    third = (residue - first - second) % 256
                    if third in alphabet:
                        residues[residue] = bytes([first, second, third])
                        break
                if residue in residues:
                    break
        prefix = b"\r\nV\t100\r\nXChecksum\t"
        for label in (b"Checksum", b"CHECKSUM", b"cHeCkSuM"):
            suffix = b"\r\n" + label + b"\t"
            for checksum in range(256):
                value = residues[(-checksum - sum(prefix + suffix)) % 256]
                block = prefix + value + suffix + bytes([checksum])
                self.assertEqual(sum(block) % 256, 0)
                for bytewise in (False, True):
                    with self.subTest(label=label, checksum=checksum, bytewise=bytewise):
                        parser = TextParser()
                        chunks = [bytes([byte]) for byte in block] if bytewise else [block]
                        frames = [frame for chunk in chunks for frame in parser.feed(chunk)]
                        self.assertEqual(len(frames), 1)
                        self.assertEqual(frames[0]["XChecksum"].raw, value.decode("ascii"))
                        self.assertEqual(frames[0]["V"].value, 100)
                        self.assertEqual(parser.pending_bytes, 0)


    def test_initial_partial_checksum_field_recovers_without_publishing_fragment(self):
        block = make_text_block({"V": "12345"})
        for label in (b"Checksum", b"CHECKSUM", b"cHeCkSuM"):
            for prefix in (label + b"\t", b"\n" + label + b"\t"):
                for checksum in range(256):
                    stream = prefix + bytes([checksum]) + block
                    for bytewise in (False, True):
                        with self.subTest(prefix=prefix, checksum=checksum, bytewise=bytewise):
                            parser = TextParser()
                            chunks = [bytes([byte]) for byte in stream] if bytewise else [stream]
                            frames = [frame for chunk in chunks for frame in parser.feed(chunk)]
                            self.assertEqual(len(frames), 1)
                            self.assertEqual(frames[0]["V"].value, 12345)
                            self.assertEqual(parser.pending_bytes, 0)
                stream = prefix + b":" + block
                for split in range(len(stream) + 1):
                    with self.subTest(prefix=prefix, split=split):
                        parser = TextParser()
                        frames = parser.feed(stream[:split]) + parser.feed(stream[split:])
                        self.assertEqual(len(frames), 1)
                        self.assertEqual(frames[0]["V"].value, 12345)

    def test_partial_checksum_exception_ends_after_known_boundary(self):
        block = make_text_block({"V": "12345"})
        for prefix in (b"Checksum\t", b"\nChecksum\t"):
            for checksum in range(256):
                with self.subTest(prefix=prefix, checksum=checksum):
                    parser = TextParser()
                    self.assertEqual(parser.feed(block)[0]["V"].value, 12345)
                    self.assertEqual(parser.feed(prefix + bytes([checksum]) + block), [])
                    self.assertEqual(parser.feed(block)[0]["V"].value, 12345)


class TestHexStartRecovery(unittest.TestCase):
    def test_new_colon_recovers_complete_hex_frame_at_every_chunk_split(self):
        reply = register_reply(H.CMD_GET, 0xEDBB, b"\x2e\x16")
        damaged = register_reply(H.CMD_ASYNC, 0xEDBB, b"\xd2\x04")[:-1]
        for prefix in (damaged, b":", b"::7bad", b":nothex"):
            stream = prefix + reply
            schedules = [[stream[:split], stream[split:]] for split in range(len(stream) + 1)]
            schedules.append([bytes([byte]) for byte in stream])
            for index, chunks in enumerate(schedules):
                with self.subTest(prefix=prefix, schedule=index):
                    parser = TextParser()
                    text, lines = [], []
                    for chunk in chunks:
                        frames, found = parser.feed_events(chunk)
                        text.extend(frames)
                        lines.extend(found)
                    self.assertEqual(text, [])
                    self.assertEqual(lines, [reply])
                    self.assertEqual(parser.pending_bytes, 0)

    def test_hex_restart_preserves_interrupted_text_and_raw_colon_checksum(self):
        prefix = b"\r\nV\t12345\r\nX\t"
        printable = [byte for byte in range(33, 127) if byte != ord(":")]
        value = next(bytes([a, b, c]) for a in printable for b in printable for c in printable
                     if (sum(prefix) + a + b + c + sum(b"\r\nChecksum\t") + ord(":")) % 256 == 0)
        block = prefix + value + b"\r\nChecksum\t:"
        reply = register_reply(H.CMD_GET, 0xEDBB, b"\x2e\x16")
        damaged = register_reply(H.CMD_ASYNC, 0xEDBB, b"\xd2\x04")[:-1]
        point = block.index(b"12345") + 2
        stream = block[:point] + damaged + reply + block[point:] + reply
        schedules = [[stream[:split], stream[split:]] for split in range(len(stream) + 1)]
        schedules.append([bytes([byte]) for byte in stream])
        for index, chunks in enumerate(schedules):
            with self.subTest(schedule=index):
                parser = TextParser()
                text, lines = [], []
                for chunk in chunks:
                    frames, found = parser.feed_events(chunk)
                    text.extend(frames)
                    lines.extend(found)
                self.assertEqual(lines, [reply, reply])
                self.assertEqual(len(text), 1)
                self.assertEqual(text[0]["V"].value, 12345)
                self.assertEqual(text[0]["X"].raw, value.decode("ascii"))
                self.assertEqual(parser.pending_bytes, 0)


class TestStructuredWireIntegers(unittest.TestCase):
    def test_unsigned_and_signed_structured_fields_reject_lossy_inputs(self):
        cases = (
            (R.encode_timer_event, {"anchor": -1}),
            (R.encode_timer_event, {"anchor": 257}),
            (R.encode_timer_event, {"anchor": 1.5}),
            (R.encode_timer_event, {"dim_pct": 256}),
            (R.encode_timer_event, {"dim": -1}),
            (R.encode_timer_event, {"offset_min": -32769}),
            (R.encode_timer_event, {"offset_min": 32768}),
            (R.encode_timer_event, {"offset_min": .5}),
            (R.encode_voltage_range, {"min_V": -1}),
            (R.encode_voltage_range, {"max": 256}),
            (R.encode_voltage_range, {"min": 1.5}),
            (R.encode_load_control, {"mode": -12}),
            (R.encode_load_control, {"mode": 16}),
            (R.encode_load_control, {"mode": -.5}),
            (R.encode_load_control, {"lighting_timer": 256}),
            (R.encode_load_control, {"lighting_timer": .5}),
            (R.encode_load_control, {"lighting_timer": "false"}),
            (R.encode_load_control, {"lighting_timer": "0"}),
            (R.encode_network_status, {"mode": -1}),
            (R.encode_network_status, {"mode": 16}),
            (R.encode_network_status, {"using_isense": -1}),
            (R.encode_network_status, {"using_icharge": .5}),
            (R.encode_network_status, {"using_tsense": "true"}),
            (R.encode_bmv_sw_version, {"raw": -1}),
            (R.encode_bmv_sw_version, {"raw": 65536}),
            (R.encode_bmv_sw_version, {"raw": 1.5}),
            (R.encode_bmv_sw_version, "256.00"),
            (R.encode_bmv_sw_version, "1.100"),
            (R.encode_bmv_sw_version, "-1.00"),
            (R.encode_bmv_sw_version, "1.-1"),
            (R.encode_bmv_sw_version, "0x10000"),
        )
        for encoder, value in cases:
            with self.subTest(encoder=encoder.__name__, value=value), self.assertRaises(ValueError):
                encoder(value)

    def test_packed_words_preserve_every_representable_bit(self):
        for encoder, width in ((R.encode_load_control, 1), (R.encode_network_status, 1),
                               (R.encode_voltage_range, 2), (R.encode_bmv_sw_version, 2),
                               (R.encode_timer_event, 4)):
            maximum = (1 << (8 * width)) - 1
            for value in (0, 1, maximum // 2, maximum):
                with self.subTest(encoder=encoder.__name__, value=value):
                    self.assertEqual(encoder(value), value.to_bytes(width, "little"))
            for value in (-1, maximum + 1):
                with self.subTest(encoder=encoder.__name__, invalid=value), self.assertRaises(ValueError):
                    encoder(value)
            raw = maximum.to_bytes(width, "little")
            self.assertEqual(encoder(raw), raw)

    def test_representable_fields_keep_their_encoding_without_model_range_checks(self):
        self.assertEqual(R.encode_timer_event({"offset_min": -32768, "anchor": 255, "dim_pct": 255}),
                         b"\x00\x80\xff\xff")
        self.assertEqual(R.encode_timer_event({"offset_min": 32767, "anchor": 0, "dim": 0}),
                         b"\xff\x7f\x00\x00")
        self.assertEqual(R.encode_timer_event({"offset_min": "-10", "anchor": 1.0, "dim_pct": "100"}),
                         b"\xf6\xff\x01\x64")
        self.assertEqual(R.encode_voltage_range({"min": 0, "max": 255}), b"\x00\xff")
        self.assertEqual(R.encode_load_control({"mode": 15, "lighting_timer": True}), b"\x8f")
        self.assertEqual(R.encode_network_status({"mode": 15, "using_icharge": True,
                                                "using_isense": True, "using_tsense": True,
                                                "using_vsense": True}), b"\xff")
        self.assertEqual(R.encode_bmv_sw_version("255.FF"), b"\xff\xff")
        self.assertEqual(R.encode_bmv_sw_version({"raw": "65535"}), b"\xff\xff")

    def test_structured_flag_fields_accept_exact_zero_and_one(self):
        for value in (False, 0, 0.0):
            self.assertEqual(R.encode_load_control({"mode": 4, "lighting_timer": value}), b"\x04")
            self.assertEqual(R.encode_network_status({"mode": 4, "using_isense": value}), b"\x04")
        for value in (True, 1, 1.0):
            self.assertEqual(R.encode_load_control({"mode": 4, "lighting_timer": value}), b"\x84")
            self.assertEqual(R.encode_network_status({"mode": 4, "using_isense": value}), b"\x24")


class TestHexAlphabet(unittest.TestCase):
    def test_non_hex_characters_raise_in_both_decode_modes(self):
        malformed = (b":7F0ED 064000D\n", b":7F0ED+064000D\n", b":7F0ED-064000D\n",
                     b":1+0+054\n", b":5+0+050\n", b":7F0ED\t064000D\n",
                     b":7F0ED0 64000D\n", b": 154\n", b":154 \n", b" :154\n",
                     b":154\t", b":154\r", b":154\n\n", b":154\v")
        for raw in malformed:
            for strict in (False, True):
                with self.subTest(raw=raw, strict=strict), self.assertRaises(FrameError):
                    H.decode_line(raw, strict=strict)
        valid = b":7F0ED0064000D"
        # Substitute every non-hex ASCII character for each digit, including
        # the first and last, so whitespace trimming cannot hide a lost digit.
        excluded = set(b"0123456789abcdefABCDEF")
        for position in range(1, len(valid)):
            for byte in range(128):
                if byte in excluded:
                    continue
                bad = valid[:position] + bytes([byte]) + valid[position + 1:] + b"\n"
                for strict in (False, True):
                    with self.subTest(position=position, byte=byte, strict=strict):
                        with self.assertRaises(FrameError):
                            H.decode_line(bad, strict=strict)

    def test_lowercase_and_supported_terminators_remain_valid(self):
        frame = b":7F0ED0064000D"
        for source in (frame, frame.lower()):
            for terminator in (b"", b"\n", b"\r\n"):
                for text_input in (False, True):
                    raw = source + terminator
                    value = raw.decode("ascii") if text_input else raw
                    with self.subTest(raw=raw, text=text_input):
                        parsed = H.decode_line(value, strict=True)
                        self.assertTrue(parsed.valid)
                        self.assertEqual(parsed.value_bytes, b"\x64\x00")
                        self.assertEqual(parsed.raw, raw)


class TestPublicParsingAndWrites(unittest.IsolatedAsyncioTestCase):
    async def test_get_recovers_after_missing_async_newline_and_allows_followup(self):
        incoming = asyncio.Queue()
        writes = []
        damaged = register_reply(H.CMD_ASYNC, 0xEDBB, b"\xd2\x04")[:-1]
        reply = register_reply(H.CMD_GET, 0xEDBB, b"\x2e\x16")

        async def read(n):
            return await incoming.get()

        async def write(data):
            writes.append(data)
            if len(writes) == 1:
                for byte in damaged + reply:
                    incoming.put_nowait(bytes([byte]))
            else:
                incoming.put_nowait(register_reply(H.CMD_GET, 0xEDBB, b"\x64\x00"))

        dev = AsyncVEDirect(read, write, flavor="MPPT")
        try:
            self.assertEqual(await dev.hex_get("PANEL_VOLTAGE", timeout=.5), 56.78)
            self.assertEqual(await dev.hex_get("PANEL_VOLTAGE", timeout=.5), 1)
            self.assertEqual(len(writes), 2)
            self.assertTrue(dev.async_frames.empty())
        finally:
            await dev.close()

    async def test_invalid_checksum_prefix_is_never_returned_by_read_one(self):
        bad = b"\r\nV\t499\r\nXChecksum\t92\r\nChecksum\t|"
        good = make_text_block({"V": "12345", "XChecksum": "42"})
        schedules = [[bad[:split], bad[split:]] for split in range(len(bad) + 1)]
        schedules.append([bytes([byte]) for byte in bad])
        for index, chunks in enumerate(schedules):
            with self.subTest(schedule=index):
                incoming = asyncio.Queue()
                incoming.put_nowait(make_text_block({"V": "999"}))
                for chunk in chunks:
                    if chunk:
                        incoming.put_nowait(chunk)
                incoming.put_nowait(good)

                async def read(n):
                    return await incoming.get()

                dev = AsyncVEDirect(read, lambda data: None)
                self.assertEqual((await dev.read_one())["V"].value, 999)
                result = await dev.read_one()
                self.assertEqual(result["V"].value, 12345)
                self.assertEqual(result["XChecksum"].raw, "42")
                await dev.close()

    async def test_structured_out_of_range_writes_are_not_transmitted(self):
        sim = SimulatedTransport(flavor="MPPT")
        dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
        for name, value in (("LOAD_OUTPUT_CONTROL", {"mode": -12}),
                            ("LOAD_OUTPUT_CONTROL", {"mode": 16}),
                            ("LOAD_OUTPUT_CONTROL", {"mode": -.5}),
                            ("TIMER_EVENT_0", {"anchor": 257, "dim_pct": 100}),
                            ("TIMER_EVENT_0", {"anchor": 1.5}),
                            ("TIMER_EVENT_0", {"offset_min": 0, "anchor": 1, "dim_pct": 256}),
                            ("TIMER_EVENT_0", {"offset_min": -32769}),
                            ("LOAD_OUTPUT_CONTROL", {"lighting_timer": "false"})):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                await dev.hex_set(name, value)
        self.assertEqual(sim.written, [])
        result = await dev.hex_set("TIMER_EVENT_0", {"offset_min": -10, "anchor": 1, "dim_pct": 100})
        self.assertEqual(result["offset_min"], -10)
        self.assertEqual(H.decode_line(sim.written[-1]).value_bytes, b"\xf6\xff\x01\x64")
        explicit = b"\xff\xff\xff\xff"
        self.assertEqual((await dev.hex_set_raw(R.lookup("TIMER_EVENT_0").id, explicit)).value_bytes,
                         explicit)
        await dev.close()

    async def test_malformed_get_reply_cannot_satisfy_transaction(self):
        for marker in (b" ", b"+", b"-", b"\t"):
            with self.subTest(marker=marker):
                incoming = asyncio.Queue()
                writes = []

                async def read(n):
                    return await incoming.get()

                async def write(data):
                    writes.append(data)
                    incoming.put_nowait(b":7F0ED" + marker + b"064000D\n")
                    incoming.put_nowait(register_reply(H.CMD_GET, 0xEDF0, b"\x96\x00"))

                dev = AsyncVEDirect(read, write, flavor="MPPT")
                with self.assertLogs("ve_direct.device", level="WARNING") as logged:
                    self.assertEqual(await dev.hex_get("BAT_MAX_CURRENT", timeout=.1), 15)
                self.assertIn("invalid VE.Direct HEX line", logged.output[0])
                self.assertEqual(len(writes), 1)
                await dev.close()


if __name__ == "__main__":
    unittest.main()
