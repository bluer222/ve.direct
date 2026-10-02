#!/usr/bin/env python3
"""Shared asyncio device worker for the ve.direct frontends.

Owns the AsyncVEDirect connection on a background thread: continuously
reads Text frames into ``shared.snapshots`` and serves get/set requests
posted via ``shared.post()``, reporting to ``shared.results``. Used by
both tools/vedirect_tui.py (curses) and tools/vedirect_gui.py (localhost
web). No UI code lives here.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from contextlib import suppress
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
TOOLS_DIR = Path(__file__).resolve().parent
for _p in (str(SRC_DIR), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ve_direct import AsyncVEDirect  # noqa: E402
from ve_direct.transport import SimulatedTransport, make_text_block  # noqa: E402

# Range-expanded history/timer rows flood the table; hidden unless requested.
RANGE_PREFIXES = ("DAILY_HISTORY_", "MPPT_HISTORY_", "CYCLE_HISTORY_", "TIMER_EVENT_")


@dataclass
class Row:
    name: str
    reg_id: int
    access: str
    last: str = "-"
    setbuf: str = ""
    pending: bool = False
    cls: str = ""
    dtype: str = ""
    unit: str = ""
    scale: float = 1.0
    flavors: tuple = ()


@dataclass
class Snapshot:
    n: int = 0
    ts: str = ""
    items: list = field(default_factory=list)  # (label, shown, human)


class Shared:
    """Thread-safe bridge between the asyncio worker and a frontend thread."""

    def __init__(self) -> None:
        self.snapshots: queue.Queue = queue.Queue()  # Snapshot, frontend keeps latest
        self.results: queue.Queue = queue.Queue()  # (req_id, name, ok, text)
        self.requests: asyncio.Queue | None = None  # created on worker loop
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self._req_id = 0
        self._lock = threading.Lock()

    def post(self, op: str, name, value=None) -> int:
        """Enqueue a get/set request from another thread; returns request id."""
        with self._lock:
            self._req_id += 1
            req_id = self._req_id
        if (self.stop.is_set() or self.loop is None or self.requests is None
                or self.loop.is_closed()):
            raise RuntimeError("device worker is stopped")
        try:
            self.loop.call_soon_threadsafe(
                self.requests.put_nowait, (req_id, op, name, value))
        except RuntimeError:
            raise RuntimeError("device worker is stopped") from None
        return req_id


def build_rows(flavor: str | None, include_all: bool, search: str = "") -> list[Row]:
    from ve_direct.registers import REGISTERS, for_flavor

    regs = for_flavor(flavor) if flavor else dict(REGISTERS)
    needle = search.lower()
    rows = []
    for name in sorted(regs):
        if not include_all and name.startswith(RANGE_PREFIXES):
            continue
        reg = regs[name]
        blob = f"{name} 0x{reg.id:04X} {reg.id} {reg.cls} {reg.access}".lower()
        if needle and needle not in blob:
            continue
        rows.append(Row(name=name, reg_id=reg.id, access=reg.access,
                        cls=reg.cls, dtype=reg.dtype, unit=reg.unit or "",
                        scale=reg.scale, flavors=tuple(sorted(reg.flavors))))
    return rows


def snapshot_from_frame(frame, n: int) -> Snapshot:
    si = frame.to_si()
    items = []
    for label, tv in frame.fields.items():
        shown = si.get(label, tv.value)
        items.append((label, str(shown), tv.human or ""))
    return Snapshot(n=n, ts=datetime.now().strftime("%H:%M:%S"), items=items)


def fmt_value(value) -> str:
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in value.items()) + "}"
    return repr(value)


def sim_blocks(count: int = 180) -> list[bytes]:
    """Varying demo blocks so the stream pane visibly updates in --sim mode."""
    states = ["3", "3", "4", "4", "5", "3"]
    blocks = []
    for i in range(count):
        blocks.append(make_text_block({
            "PID": "0xA04A",
            "V": str(26800 + (i * 37) % 1200),
            "I": str(3800 + (i * 53) % 2400),
            "VPV": str(31000 + (i * 29) % 900),
            "PPV": str(110 + (i * 7) % 40),
            "CS": states[i % len(states)],
            "MPPT": "2", "ERR": "0", "LOAD": "ON",
        }))
    return blocks


async def _read_loop(dev: AsyncVEDirect, shared: Shared, pace: float) -> None:
    n = 0
    try:
        async for frame in dev.frames():
            if shared.stop.is_set():
                return
            n += 1
            snap = snapshot_from_frame(frame, n)
            while shared.snapshots.qsize() > 1:  # keep frontend on latest frame
                try:
                    shared.snapshots.get_nowait()
                except queue.Empty:
                    break
            shared.snapshots.put(snap)
            if pace:
                await asyncio.sleep(pace)
    except Exception as exc:
        shared.results.put((0, None, False, f"stream: {type(exc).__name__}: {exc}"))
        shared.stop.set()


async def _handle_request(dev: AsyncVEDirect, shared: Shared, req, timeout: float) -> None:
    req_id, op, name, value = req
    try:
        if op == "get":
            result = await dev.hex_get(name, timeout=timeout)
        else:
            result = await dev.hex_set(name, value, timeout=timeout)
        shared.results.put((req_id, name if isinstance(name, str) else f"0x{name:04X}",
                            True, fmt_value(result)))
    except Exception as exc:  # noqa: BLE001 - surfaced in the frontend message line
        label = name if isinstance(name, str) else f"0x{name:04X}"
        shared.results.put((req_id, label, False, f"{type(exc).__name__}: {exc}"))


async def _worker(args, shared: Shared) -> None:
    """args needs sim/port/baudrate/flavor/timeout (argparse or equivalent)."""
    shared.requests = asyncio.Queue()
    shared.loop = asyncio.get_running_loop()
    sim = args.sim or not args.port
    if sim:
        transport = SimulatedTransport(text_chunks=sim_blocks(),
                                       flavor=(args.flavor or "MPPT"))
        dev = AsyncVEDirect(transport.read_chunk, transport.write,
                            flavor=args.flavor)
    else:
        dev = await AsyncVEDirect.from_port(args.port, baudrate=args.baudrate,
                                            flavor=args.flavor)
    async with dev:
        shared.ready.set()
        reader = asyncio.create_task(
            _read_loop(dev, shared, pace=1.0 if sim else 0.0))
        try:
            while not shared.stop.is_set():
                try:
                    req = await asyncio.wait_for(shared.requests.get(), 0.1)
                except asyncio.TimeoutError:
                    continue
                await _handle_request(dev, shared, req, args.timeout)
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader


def _worker_entry(args, shared: Shared) -> None:
    try:
        asyncio.run(_worker(args, shared))
    except Exception as exc:  # noqa: BLE001 - never silently die
        shared.results.put((0, None, False, f"worker: {type(exc).__name__}: {exc}"))
        shared.stop.set()
        shared.ready.set()
