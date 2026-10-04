#!/usr/bin/env python3
"""Localhost web GUI: live streamed Text values on top, HEX register table below.

Serves a single page on 127.0.0.1 (no external dependencies, stdlib only).
The top section shows the latest Text frame, refreshed about once a second.
The table lists one row per register with a last-read box, an editable
value-to-set box, and per-row Get / Set buttons:

    python tools/vedirect_gui.py --port /dev/ttyUSB0 --flavor MPPT
    python tools/vedirect_gui.py --sim        # offline demo, no hardware
    python tools/vedirect_gui.py --sim --open # ... and open a browser tab

Connection flags mirror vedirect_cli.py; VE_DIRECT_PORT / VE_DIRECT_FLAVOR
env vars are honored. The server binds localhost only by default (--host
overrides, e.g. 0.0.0.0 for LAN access on a trusted network).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import queue
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
TOOLS_DIR = Path(__file__).resolve().parent
for _p in (str(SRC_DIR), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vedirect_backend import Shared, _worker_entry, build_rows  # noqa: E402
from vedirect_cli import parse_set_value  # noqa: E402

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ve.direct localhost GUI</title>
<style>
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  margin: 0 auto; max-width: 1100px; padding: 1rem 1.25rem 3rem; line-height: 1.4; }
header { position: sticky; top: 0; background: Canvas; padding: .5rem 0;
  border-bottom: 1px solid GrayText; z-index: 5; }
h1 { font-size: 1.25rem; margin: .1rem 0; }
h2 { font-size: 1.05rem; margin: 1.25rem 0 .5rem; }
.meta { font-size: .85rem; opacity: .8; }
#msg { min-height: 1.4em; font-size: .9rem; margin-top: .25rem; }
#cards { display: flex; flex-wrap: wrap; gap: .4rem; }
.card { border: 1px solid GrayText; border-radius: .4rem; padding: .3rem .55rem;
  font-size: .9rem; }
.card b { margin-right: .35rem; }
.card .human { opacity: .7; font-size: .8em; margin-left: .3rem; }
.controls { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
  margin: .5rem 0; }
.controls input[type=search], .controls select, .controls button { font-size: .9rem; }
#q { flex: 1; min-width: 12rem; padding: .3rem .5rem; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
thead th { position: sticky; top: 0; background: Canvas; text-align: left;
  border-bottom: 2px solid GrayText; padding: .3rem .4rem; }
tbody td { border-bottom: 1px solid GrayText; padding: .25rem .4rem; vertical-align: middle; }
tbody tr.pending td.last { opacity: .6; font-style: italic; }
.badge { font-size: .75em; border: 1px solid GrayText; border-radius: .3rem;
  padding: 0 .35rem; white-space: nowrap; }
.ro { color: #4aa3ff; } .rw { color: #4caf7d; } .wo { color: #e09a3c; } .co { color: #999; }
td.last { font-family: ui-monospace, monospace; max-width: 16rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
input.setval { width: 10rem; font-size: .85rem; padding: .15rem .3rem; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .4; }
a.download { border: 1px solid GrayText; border-radius: .3rem;
  padding: .2rem .5rem; font-size: .9rem; color: CanvasText; text-decoration: none; }
code { font-size: .85em; }
.counts { font-size: .8rem; opacity: .75; }
</style>
</head>
<body>
<header>
<h1>ve.direct localhost GUI</h1>
<div class="meta">source <b>{{PORT_LABEL}}</b> &middot; flavor <b>{{FLAVOR_LABEL}}</b>
&middot; frames <b id="fcount">0</b></div>
<div class="controls">
<a class="download" href="api/dump" download>Dump available values</a>
<span class="counts">JSON: latest text frame and cached register values</span>
</div>
<div id="msg" aria-live="polite">connecting&hellip;</div>
</header>

<h2>Live text stream</h2>
<div id="cards"></div>

<h2>HEX registers</h2>
<div class="controls">
<input type="search" id="q" placeholder="Filter registers (name, id, class)&hellip;" autocomplete="off">
<select id="fAccess">
<option value="">All access</option>
<option value="READ_ONLY">READ_ONLY</option>
<option value="READ_WRITE">READ_WRITE</option>
<option value="WRITE_ONLY">WRITE_ONLY</option>
<option value="CONSTANT">CONSTANT</option>
</select>
<button id="getAll" type="button">Get all visible</button>
<span class="counts" id="counts"></span>
</div>
<table>
<thead><tr><th>Name</th><th>ID</th><th>Access</th><th>Last read</th>
<th>Value to set</th><th>Get</th><th>Set</th></tr></thead>
<tbody id="rows"></tbody>
</table>

<script>
let REGS = [];
let since = 0;
let pending = {};  // req_id -> {name, op}
const rowsEl = document.getElementById('rows');
const cardsEl = document.getElementById('cards');
const msgEl = document.getElementById('msg');
const rowByName = {};

function note(t) {
  const ts = new Date().toLocaleTimeString();
  msgEl.textContent = ts + ' ' + t;
}

async function loadRegs() {
  REGS = await fetch('api/registers').then(r => r.json());
  rowsEl.textContent = '';
  for (const reg of REGS) {
    const tr = document.createElement('tr');
    tr.dataset.name = reg.name;
    tr.dataset.blob = (reg.name + ' 0x' + reg.id.toString(16).padStart(4, '0')
      + ' ' + reg.id + ' ' + reg.cls + ' ' + reg.access).toLowerCase();
    tr.dataset.access = reg.access;
    rowByName[reg.name] = tr;

    const tdName = document.createElement('td');
    const code = document.createElement('code');
    code.textContent = reg.name;
    tdName.appendChild(code);
    tr.appendChild(tdName);

    const tdId = document.createElement('td');
    const idCode = document.createElement('code');
    idCode.textContent = '0x' + reg.id.toString(16).toUpperCase().padStart(4, '0');
    tdId.appendChild(idCode);
    tr.appendChild(tdId);

    const tdAcc = document.createElement('td');
    const badge = document.createElement('span');
    badge.textContent = reg.access;
    badge.className = 'badge ' + {READ_ONLY: 'ro', READ_WRITE: 'rw',
      WRITE_ONLY: 'wo', CONSTANT: 'co'}[reg.access];
    tdAcc.appendChild(badge);
    tr.appendChild(tdAcc);

    const tdLast = document.createElement('td');
    tdLast.className = 'last';
    tdLast.textContent = '-';
    tr.appendChild(tdLast);

    const tdSet = document.createElement('td');
    const inp = document.createElement('input');
    inp.className = 'setval';
    inp.placeholder = reg.unit ? reg.unit : 'value';
    tdSet.appendChild(inp);
    tr.appendChild(tdSet);

    const tdGet = document.createElement('td');
    const bGet = document.createElement('button');
    bGet.textContent = 'Get';
    bGet.disabled = reg.access === 'WRITE_ONLY';
    bGet.addEventListener('click', () => doGet(reg.name));
    tdGet.appendChild(bGet);
    tr.appendChild(tdGet);

    const tdSetB = document.createElement('td');
    const bSet = document.createElement('button');
    bSet.textContent = 'Set';
    bSet.disabled = (reg.access === 'READ_ONLY' || reg.access === 'CONSTANT');
    bSet.addEventListener('click', () => doSet(reg.name, inp.value));
    tdSetB.appendChild(bSet);
    tr.appendChild(tdSetB);

    rowsEl.appendChild(tr);
  }
  applyFilter();
}

function applyFilter() {
  const needle = document.getElementById('q').value.trim().toLowerCase();
  const acc = document.getElementById('fAccess').value;
  let shown = 0;
  for (const tr of rowsEl.children) {
    const ok = (!needle || tr.dataset.blob.includes(needle))
      && (!acc || tr.dataset.access === acc);
    tr.style.display = ok ? '' : 'none';
    if (ok) shown++;
  }
  document.getElementById('counts').textContent =
    shown + ' / ' + rowsEl.children.length + ' registers';
}

function renderFrame(frame) {
  document.getElementById('fcount').textContent = frame.n;
  cardsEl.textContent = '';
  if (!frame.items.length) {
    cardsEl.textContent = 'waiting for frames\u2026';
    return;
  }
  for (const [label, shown, human] of frame.items) {
    const d = document.createElement('div');
    d.className = 'card';
    const b = document.createElement('b');
    b.textContent = label;
    d.appendChild(b);
    const v = document.createElement('span');
    v.textContent = shown;
    d.appendChild(v);
    if (human) {
      const s = document.createElement('span');
      s.className = 'human';
      s.textContent = human;
      d.appendChild(s);
    }
    cardsEl.appendChild(d);
  }
}

function handleEvent(ev) {
  // status events (stream/worker errors) carry name === null
  if (!ev.name) { note(ev.text); return; }
  const tr = rowByName[ev.name];
  const rec = pending[ev.req_id];
  if (rec) delete pending[ev.req_id];
  if (!tr) { note(ev.name + ': ' + ev.text); return; }
  tr.classList.remove('pending');
  const last = tr.querySelector('td.last');
  if (ev.ok) {
    last.textContent = ev.text;
    note(ev.name + ' = ' + ev.text);
  } else {
    note(ev.name + ': ERROR ' + ev.text);
  }
}

async function poll() {
  try {
    const s = await fetch('api/state?since=' + since).then(r => r.json());
    since = s.seq;
    renderFrame(s.frame);
    for (const [name, text] of Object.entries(s.values)) {
      const tr = rowByName[name];
      if (tr && !tr.classList.contains('pending'))
        tr.querySelector('td.last').textContent = text;
    }
    for (const ev of s.events) handleEvent(ev);
  } catch (e) {
    note('poll failed: ' + e);
  }
}

async function postOp(url, body, name, op) {
  let data;
  try {
    const resp = await fetch(url, {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
    data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.status);
  } catch (e) {
    note(name + ': ERROR ' + e.message);
    return;
  }
  pending[data.req_id] = {name, op};
  const tr = rowByName[name];
  if (tr) {
    tr.classList.add('pending');
    tr.querySelector('td.last').textContent = op === 'get' ? '\u2026' : 'writing\u2026';
  }
}

function doGet(name) { postOp('api/get', {name}, name, 'get'); }

function doSet(name, value) {
  if (!value.trim()) { note(name + ': type a value-to-set first'); return; }
  postOp('api/set', {name, value}, name, 'set');
}

async function getAllVisible() {
  let n = 0;
  for (const tr of rowsEl.children) {
    if (tr.style.display === 'none') continue;
    const btn = tr.querySelectorAll('button')[0];
    if (btn.disabled || tr.classList.contains('pending')) continue;
    doGet(tr.dataset.name);
    n++;
    await new Promise(r => setTimeout(r, 5));  // keep request order stable
  }
  note('polling ' + n + ' registers\u2026');
}

document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('fAccess').addEventListener('change', applyFilter);
document.getElementById('getAll').addEventListener('click', getAllVisible);

loadRegs().then(() => { poll(); setInterval(poll, 700); });
</script>
</body>
</html>
"""


class GuiState:
    """Latest frame + sequenced result events, shared by HTTP threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seq = 0
        self.events: deque = deque(maxlen=300)  # {seq, req_id, name, ok, text}
        self.frame = {"n": 0, "ts": "", "items": []}
        self.values: dict[str, str] = {}

    def push_frame(self, snap) -> None:
        with self.lock:
            self.frame = {"n": snap.n, "ts": snap.ts,
                          "items": [[l, s, h] for l, s, h in snap.items]}

    def push_result(self, req_id: int, name, ok: bool, text: str) -> None:
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "req_id": req_id,
                                "name": name, "ok": ok, "text": text})
            if ok and name:
                self.values[name] = text


def _pump(shared: Shared, state: GuiState, stop: threading.Event) -> None:
    """Move worker snapshots/results into GuiState (runs on its own thread)."""
    while not stop.is_set():
        try:
            snap = shared.snapshots.get(timeout=0.1)
            # keep only the latest frame
            try:
                while True:
                    snap = shared.snapshots.get_nowait()
            except queue.Empty:
                pass
            state.push_frame(snap)
        except queue.Empty:
            pass
        try:
            while True:
                req_id, name, ok, text = shared.results.get_nowait()
                state.push_result(req_id, name, ok, text)
        except queue.Empty:
            pass


def _send_json(handler: BaseHTTPRequestHandler, obj, status: int = 200,
               filename: str | None = None) -> None:
    body = json.dumps(obj, indent=2 if filename else None).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ve.direct-gui/1"

    def log_message(self, *args) -> None:  # keep console output quiet
        pass

    def _route(self):
        return urlparse(self.path).path

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server
        path = self._route()
        if path in ("/", "/index.html"):
            page = PAGE.replace("{{PORT_LABEL}}",
                                html.escape(srv.gui_port_label)).replace(
                "{{FLAVOR_LABEL}}", html.escape(srv.gui_flavor_label))
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/registers":
            _send_json(self, srv.gui_regs)
        elif path == "/api/dump":
            state: GuiState = srv.gui_state
            now = datetime.now(timezone.utc)
            with state.lock:
                dump = {
                    "exported_at": now.isoformat(),
                    "source": srv.gui_port_label,
                    "flavor": srv.gui_flavor_label,
                    "frame": state.frame,
                    "registers": [dict(reg, value=state.values[reg["name"]])
                                  for reg in srv.gui_regs if reg["name"] in state.values],
                }
            _send_json(self, dump,
                       filename=f"ve-direct-values-{now:%Y%m%dT%H%M%S%fZ}.json")
        elif path == "/api/state":
            try:
                since = int(parse_qs(urlparse(self.path).query)
                            .get("since", ["0"])[0])
            except ValueError:
                since = 0
            state: GuiState = srv.gui_state
            with state.lock:
                events = [e for e in state.events if e["seq"] > since]
                _send_json(self, {"seq": state.seq, "frame": state.frame,
                                  "events": events, "values": state.values})
        else:
            _send_json(self, {"error": "not found"}, 404)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if not 0 < length <= 65536:
            raise ValueError("JSON body must contain 1..65536 bytes")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("expected a JSON object") from None
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body

    def _post_operation(self, op, name, value=None):
        try:
            req_id = self.server.gui_shared.post(op, name, value)
        except RuntimeError as exc:
            _send_json(self, {"error": str(exc)}, 503)
        else:
            _send_json(self, {"req_id": req_id})

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        path = self._route()
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed_origin = urlparse(origin)
            if (parsed_origin.scheme not in ("http", "https")
                    or parsed_origin.netloc.lower() != self.headers.get("Host", "").lower()):
                _send_json(self, {"error": "cross-site requests are not accepted"}, 403)
                return
        if self.headers.get_content_type() != "application/json":
            _send_json(self, {"error": "expected application/json"}, 415)
            return
        try:
            body = self._read_json()
        except ValueError as exc:
            _send_json(self, {"error": str(exc)}, 400)
            return
        name = body.get("name")
        if not isinstance(name, str) or not name:
            _send_json(self, {"error": "missing register 'name'"}, 400)
            return
        if name not in srv.gui_known:
            _send_json(self, {"error": f"unknown register {name!r}"}, 400)
            return
        if srv.gui_shared.stop.is_set():
            _send_json(self, {"error": "device worker is stopped"}, 503)
            return
        if path == "/api/get":
            self._post_operation("get", name)
        elif path == "/api/set":
            value = body.get("value", "")
            if not isinstance(value, str):
                _send_json(self, {"error": "set 'value' must be a string"}, 400)
                return
            try:
                parsed = parse_set_value(value)
            except ValueError as exc:
                _send_json(self, {"error": f"bad value: {exc}"}, 400)
                return
            self._post_operation("set", name, parsed)
        else:
            _send_json(self, {"error": "not found"}, 404)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Localhost web GUI: live Text stream plus per-register Get/Set table.")
    p.add_argument("--port", default=os.environ.get("VE_DIRECT_PORT"),
                   help="Serial port (e.g. /dev/ttyUSB0). Omit for --sim.")
    p.add_argument("--flavor", default=os.environ.get("VE_DIRECT_FLAVOR"),
                   help="Device family: MPPT, MPPT_SMALL, MPPT_RS, ORION, BMV, BMV71, INV, CHG.")
    p.add_argument("--baudrate", type=int, default=19200)
    p.add_argument("--timeout", type=float, default=2.0,
                   help="HEX reply timeout in seconds (default 2.0).")
    p.add_argument("--sim", action="store_true",
                   help="Use offline simulator instead of hardware.")
    p.add_argument("--all", action="store_true",
                   help="Include range-expanded history/timer rows in the table.")
    p.add_argument("--host", default="127.0.0.1",
                   help="Interface to bind (default 127.0.0.1, localhost only).")
    p.add_argument("--listen-port", type=int, default=8080,
                   help="Local port to serve on (default 8080).")
    p.add_argument("--open", action="store_true",
                   help="Open the page in the default browser.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    shared = Shared()
    worker = threading.Thread(target=_worker_entry, args=(args, shared),
                              daemon=True)
    worker.start()
    if not shared.ready.wait(timeout=10):
        print("error: worker did not start", file=sys.stderr)
        return 1

    rows = build_rows(args.flavor, args.all)
    regs = [{"name": r.name, "id": r.reg_id, "access": r.access, "cls": r.cls,
             "unit": r.unit, "scale": r.scale, "flavors": list(r.flavors)}
            for r in rows]

    state = GuiState()
    stop = threading.Event()
    pump = threading.Thread(target=_pump, args=(shared, state, stop),
                            daemon=True)
    pump.start()

    server = ThreadingHTTPServer((args.host, args.listen_port), _Handler)
    server.gui_shared = shared
    server.gui_state = state
    server.gui_regs = regs
    server.gui_known = {r["name"] for r in regs}
    server.gui_port_label = args.port if (args.port and not args.sim) else "simulator"
    server.gui_flavor_label = args.flavor or "ALL"

    url = f"http://{args.host}:{args.listen_port}/"
    print(f"Serving ve.direct GUI on {url} "
          f"({len(regs)} registers, flavor {server.gui_flavor_label})")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", file=sys.stderr)
    finally:
        stop.set()
        shared.stop.set()
        server.shutdown()
        worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
