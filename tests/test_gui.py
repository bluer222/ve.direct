"""HTTP coverage for GUI snapshot downloads."""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from vedirect_backend import Shared, Snapshot
from vedirect_gui import GuiState, _Handler


class TestGuiDump(unittest.TestCase):
    def setUp(self):
        self.state = GuiState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.gui_state = self.state
        self.server.gui_shared = Shared()
        self.server.gui_shared.post = mock.Mock()
        self.server.gui_port_label = "simulator"
        self.server.gui_flavor_label = "MPPT"
        self.server.gui_regs = [
            {"name": "PANEL_VOLTAGE", "id": 0xEDBB, "unit": "V"},
            {"name": "UNREAD", "id": 1, "unit": ""},
        ]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_download_contains_latest_available_values(self):
        self.state.push_frame(Snapshot(1, "12:00:00", [("V", "12.3", "")]))
        self.state.push_frame(Snapshot(2, "12:00:01", [("V", "12.4", "")]))
        self.state.push_result(1, "PANEL_VOLTAGE", True, "31.2")
        self.state.push_result(2, "PANEL_VOLTAGE", False, "timeout")
        self.state.push_result(3, "UNREAD", False, "unsupported")
        status, headers, body = self.get("/api/dump")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertRegex(headers["Content-Disposition"],
                         r'^attachment; filename="ve-direct-values-\d{8}T\d{12}Z\.json"$')
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        dump = json.loads(body)
        self.assertEqual(dump["source"], "simulator")
        self.assertEqual(dump["flavor"], "MPPT")
        self.assertEqual(datetime.fromisoformat(dump["exported_at"]).tzinfo, timezone.utc)
        self.assertEqual(dump["frame"], {
            "n": 2, "ts": "12:00:01", "items": [["V", "12.4", ""]]})
        self.assertEqual(dump["registers"], [{
            "name": "PANEL_VOLTAGE", "id": 0xEDBB, "unit": "V", "value": "31.2"}])
        self.server.gui_shared.post.assert_not_called()

    def test_empty_and_stopped_worker_can_be_exported(self):
        self.server.gui_shared.stop.set()
        status, _, body = self.get("/api/dump")
        self.assertEqual(status, 200)
        dump = json.loads(body)
        self.assertEqual(dump["frame"]["items"], [])
        self.assertEqual(dump["registers"], [])
        self.server.gui_shared.post.assert_not_called()

    def test_page_offers_download(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b'href="api/dump" download', body)
