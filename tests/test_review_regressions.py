"""Regression coverage for shared reads, setting validation and device tools."""
from __future__ import annotations

import asyncio
import http.client
import json
import struct
import sys
import threading
import unittest
from contextlib import suppress
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from ve_direct import AsyncVEDirect, TimeoutError
from ve_direct import hex as H
from ve_direct.exceptions import TransportError
from ve_direct.transport import SimulatedTransport
from vedirect_backend import Shared, _read_loop, _worker_entry
from vedirect_cli import build_parser
from vedirect_gui import _Handler


class TestSharedReads(unittest.IsolatedAsyncioTestCase):
    async def test_text_deadline_preserves_pending_register_and_command_requests(self):
        for command in (False, True):
            with self.subTest(command=command):
                incoming = asyncio.Queue()
                reading = asyncio.Event()
                written = asyncio.Event()
                read_calls = 0

                async def read(n):
                    nonlocal read_calls
                    read_calls += 1
                    reading.set()
                    return await incoming.get()

                async def write(data):
                    written.set()

                dev = AsyncVEDirect(read, write, flavor="MPPT")
                consumer = asyncio.create_task(dev.read_one(timeout=.05))
                await reading.wait()
                request = asyncio.create_task(
                    dev.get_product_id(timeout=1) if command
                    else dev.hex_get("PANEL_VOLTAGE", timeout=1))
                await written.wait()
                with self.assertRaises(TimeoutError):
                    await consumer
                self.assertFalse(request.done())
                incoming.put_nowait(
                    H.encode_frame(H.CMD_DONE, struct.pack("<H", 0xA04A)) if command
                    else H.encode_frame(H.CMD_GET, bytes.fromhex("BBED003A0C")))
                self.assertAlmostEqual(await request, 0xA04A if command else 31.3)
                self.assertEqual(read_calls, 1, "the original read must be reused")
                incoming.put_nowait(H.encode_frame(H.CMD_PING, b"\x16\x41"))
                self.assertEqual((await dev.ping(timeout=1)).data, b"\x16\x41")
                await dev.close()

    async def test_cancelling_notification_consumer_preserves_hex_reply(self):
        incoming = asyncio.Queue()
        reading = asyncio.Event()
        written = asyncio.Event()

        async def read(n):
            reading.set()
            return await incoming.get()

        async def write(data):
            written.set()

        dev = AsyncVEDirect(read, write)
        stream = dev.async_notifications()
        consumer = asyncio.create_task(stream.__anext__())
        await reading.wait()
        request = asyncio.create_task(dev.ping(timeout=1))
        await written.wait()
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer
        incoming.put_nowait(H.encode_frame(H.CMD_PING, b"\x16\x41"))
        self.assertEqual((await request).data, b"\x16\x41")
        await dev.close()
        await stream.aclose()

    async def test_hex_timeout_cancels_its_read_and_blocks_late_replies(self):
        stopped = asyncio.Event()

        async def read(n):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        dev = AsyncVEDirect(read, lambda data: None)
        with self.assertRaises(TimeoutError):
            await dev.ping(timeout=.01)
        self.assertTrue(stopped.is_set())
        with self.assertRaises(TransportError):
            await dev.ping(timeout=.01)
        await dev.close()
        self.assertTrue(stopped.is_set())
        self.assertIsNone(dev._read_task)


class TestSettingValidation(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_scalar_settings_never_reach_transport(self):
        sim = SimulatedTransport()
        dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
        for name, values in (
            ("RELAY_CONTROL", ("off", "false", "0", [], {}, 2, -1, .5)),
            ("DEVICE_MODE", (4.9, -.5, -1, 256, float("nan"))),
            ("NETWORK_MODE", (.5, -.5, -1, 256, float("nan"))),
        ):
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    await dev.hex_set(name, value)
        self.assertEqual(sim.written, [])
        for value in (False, 0, 0.0, True, 1, 1.0):
            self.assertEqual(await dev.hex_set("RELAY_CONTROL", value), bool(value))
        self.assertEqual(await dev.hex_set("DEVICE_MODE", 4.0), "device_off")
        self.assertEqual(await dev.hex_set("NETWORK_MODE", ["networked"]), ["networked"])
        await dev.close()


class TestTools(unittest.IsolatedAsyncioTestCase):
    async def test_stream_transport_errors_stop_worker(self):
        async def read(n):
            raise OSError("disconnected")

        dev = AsyncVEDirect(read, lambda data: None)
        shared = Shared()
        await _read_loop(dev, shared, pace=0)
        self.assertTrue(shared.stop.is_set())
        self.assertIn("disconnected", shared.results.get_nowait()[3])
        await dev.close()

    def test_connection_failure_marks_worker_stopped(self):
        shared = Shared()
        args = SimpleNamespace(sim=False, port="missing", baudrate=19200, flavor="MPPT")
        with mock.patch("vedirect_backend.AsyncVEDirect.from_port", new=mock.AsyncMock(
                side_effect=OSError("missing port"))):
            # Worker entry owns its loop, as it does on the frontend thread.
            thread = threading.Thread(target=_worker_entry, args=(args, shared))
            thread.start()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(shared.ready.is_set())
        self.assertTrue(shared.stop.is_set())
        self.assertIn("missing port", shared.results.get_nowait()[3])

    def test_list_preserves_global_flavor(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["--flavor", "BMV", "list"]).flavor, "BMV")
        self.assertEqual(parser.parse_args(["list", "--flavor", "INV"]).flavor, "INV")


class TestGuiRequests(unittest.TestCase):
    def setUp(self):
        self.shared = Shared()
        self.shared.post = mock.Mock(return_value=1)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.gui_shared = self.shared
        self.server.gui_known = {"RELAY_CONTROL"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def post(self, body, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request("POST", "/api/set", body, headers or {
                "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_local_json_write_still_works(self):
        host = "%s:%s" % self.server.server_address
        status, body = self.post(json.dumps({"name": "RELAY_CONTROL", "value": "false"}), {
            "Origin": "http://" + host, "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(body["req_id"], 1)
        self.shared.post.assert_called_once_with("set", "RELAY_CONTROL", False)

    def test_cross_site_and_non_json_writes_are_rejected(self):
        body = json.dumps({"name": "RELAY_CONTROL", "value": "true"})
        for headers, expected in (({"Origin": "https://untrusted.example",
                                    "Content-Type": "application/json"}, 403),
                                  ({"Content-Type": "text/plain"}, 415)):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(body, headers)[0], expected)
        self.shared.post.assert_not_called()

    def test_malformed_requests_return_json_errors(self):
        for body in ("[]", "1", "true", "null", "{", "{}"):
            with self.subTest(body=body):
                status, result = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn("error", result)
        self.assertEqual(self.post("{}", {"Content-Type": "application/json",
                                           "Content-Length": "-1"})[0], 400)
        self.shared.post.assert_not_called()

    def test_stopped_worker_rejects_operations(self):
        self.shared.stop.set()
        self.assertEqual(self.post(json.dumps({"name": "RELAY_CONTROL", "value": "true"}))[0], 503)
        self.shared.post.assert_not_called()

    def test_worker_shutdown_race_returns_service_unavailable(self):
        self.shared.post.side_effect = RuntimeError("device worker is stopped")
        self.assertEqual(self.post(json.dumps({"name": "RELAY_CONTROL", "value": "true"}))[0], 503)
