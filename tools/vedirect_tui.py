#!/usr/bin/env python3
"""Curses TUI: live streamed Text values on top, HEX register table below.

Top pane shows the latest Text frame, refreshed as frames arrive (~1/sec).
The table lists one row per register with a last-read box and an editable
value-to-set box; each row supports Get and Set:

    python tools/vedirect_tui.py --port /dev/ttyUSB0 --flavor MPPT
    python tools/vedirect_tui.py --sim        # offline demo, no hardware

Keys: Up/Down (or j/k) move, PgUp/PgDn scroll, g = GET selected,
e = edit value-to-set, s = SET selected, G = GET all filtered rows,
/ = filter registers, q = quit. Empty input cancels an edit prompt.

Connection flags mirror vedirect_cli.py; VE_DIRECT_PORT / VE_DIRECT_FLAVOR
env vars are honored. Only stdlib (curses) is used for the interface.
"""
from __future__ import annotations

import argparse
import curses
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
TOOLS_DIR = Path(__file__).resolve().parent
for _p in (str(SRC_DIR), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vedirect_backend import Row, Shared, Snapshot, _worker_entry, build_rows  # noqa: E402
from vedirect_cli import parse_set_value  # noqa: E402


def _safe_add(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _prompt(stdscr, y: int, prompt_text: str, w: int) -> str | None:
    """Blocking one-line input on row y; empty input cancels (returns None)."""
    curses.echo()
    curses.curs_set(1)
    stdscr.nodelay(False)
    try:
        _safe_add(stdscr, y, 0, " " * (w - 1))
        _safe_add(stdscr, y, 0, prompt_text[: w - 1])
        stdscr.clrtoeol()
        stdscr.refresh()
        raw = stdscr.getstr(y, min(len(prompt_text), w - 2), 200)
        text = raw.decode("utf-8", "replace").strip()
        return text if text else None
    except (curses.error, ValueError):
        return None
    finally:
        curses.noecho()
        curses.curs_set(0)
        stdscr.nodelay(True)


def _draw(stdscr, args, rows: list[Row], sel: int, top: int, snap: Snapshot,
          message: str, filt: str) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    if h < 14 or w < 60:
        _safe_add(stdscr, 0, 0, "Terminal too small (need >= 60x14). Resize or press q.")
        stdscr.refresh()
        return
    port_label = args.port if (args.port and not args.sim) else "SIM"
    title = (f" ve.direct TUI | {port_label} | flavor {args.flavor or '-'} "
             f"| frames {snap.n} | {len(rows)} regs ")
    _safe_add(stdscr, 0, 0, title[: w - 1].ljust(w - 1), curses.A_REVERSE)
    _safe_add(stdscr, 1, 0, message[: w - 1])

    # --- live stream pane ---
    y = 2
    _safe_add(stdscr, y, 0, f"LIVE TEXT #{snap.n} @ {snap.ts}"[: w - 1], curses.A_BOLD)
    y += 1
    stream_bottom = min(h - 1, y + 4)
    x = 0
    if not snap.items:
        _safe_add(stdscr, y, 0, "waiting for frames..."[: w - 1])
        y += 1
    for label, shown, human in snap.items:
        cell = f"{label} {shown}" + (f" [{human}]" if human else "")
        if x + len(cell) + 2 > w:
            y += 1
            x = 0
            if y >= stream_bottom:
                break
        _safe_add(stdscr, y, x, cell)
        x += len(cell) + 3

    # --- register table ---
    y = stream_bottom + 1
    if y >= h - 3:
        stdscr.refresh()
        return
    hdr = f"HEX REGISTERS  filter: {filt or '-'}  ({len(rows)} shown)"
    _safe_add(stdscr, y, 0, hdr[: w - 1], curses.A_BOLD)
    y += 1
    name_w, id_w, acc_w, last_w = 28, 8, 10, 26
    set_w = max(8, w - (name_w + id_w + acc_w + last_w + 6))
    _safe_add(stdscr, y, 0,
              f"{'Name':<{name_w}} {'ID':<{id_w}} {'Access':<{acc_w}} "
              f"{'Last read':<{last_w}} {'Value to set':<{set_w}}"[: w - 1],
              curses.A_UNDERLINE)
    y += 1
    first_row_y = y
    visible = h - 1 - first_row_y - 1  # keep last line for help
    for i in range(max(0, visible)):
        idx = top + i
        if idx >= len(rows):
            break
        r = rows[idx]
        last = "..." if r.pending else r.last
        line = (f"{r.name:<{name_w}} 0x{r.reg_id:04X}  {r.access:<{acc_w}} "
                f"{last[:last_w]:<{last_w}} {r.setbuf[:set_w]:<{set_w}}")
        attr = curses.A_REVERSE if idx == sel else 0
        _safe_add(stdscr, first_row_y + i, 0, line[: w - 1], attr)
    help_line = "Up/Dn j/k move  PgUp/PgDn  g get  e edit value  s set  G get-all  / filter  q quit"
    _safe_add(stdscr, h - 1, 0, help_line[: w - 1], curses.A_REVERSE)
    stdscr.refresh()


def _run_ui(stdscr, args, shared: Shared) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(True)
    curses.noecho()
    stdscr.timeout(120)

    rows = build_rows(args.flavor, args.all)
    pending: dict[int, str] = {}  # req_id -> register name
    sel = 0
    top = 0
    snap = Snapshot()
    filt = ""
    message = "g:get  e:edit value  s:set  /=filter  q:quit"

    def visible_height() -> int:
        h, _ = stdscr.getmaxyx()
        return max(1, h - 9)

    def clamp() -> None:
        nonlocal sel, top
        sel = max(0, min(sel, len(rows) - 1)) if rows else 0
        vis = visible_height()
        top = max(0, min(top, max(0, len(rows) - vis)))
        if sel < top:
            top = sel
        elif sel >= top + vis:
            top = sel - vis + 1

    def note(text: str) -> None:
        nonlocal message
        message = f"{datetime.now().strftime('%H:%M:%S')} {text}"

    def post_request(op: str, r: Row, value=None) -> bool:
        try:
            req_id = shared.post(op, r.name, value)
        except RuntimeError as exc:
            note(str(exc))
            return False
        r.pending = True
        pending[req_id] = r.name
        return True

    def do_get(idx: int) -> bool:
        r = rows[idx]
        if r.access == "WRITE_ONLY":
            note(f"{r.name}: not readable ({r.access})")
            return False
        return post_request("get", r)

    while True:
        # drain worker updates
        try:
            while True:
                latest = shared.snapshots.get_nowait()
                snap = latest
        except queue.Empty:
            pass
        try:
            while True:
                req_id, name, ok, text = shared.results.get_nowait()
                if req_id == 0 or name is None:
                    note(text)
                    continue
                target = pending.pop(req_id, name)
                row = next((r for r in rows if r.name == target), None)
                if row is None:  # filtered out of view; report only
                    note(f"{target}: {text}")
                    continue
                row.pending = False
                if ok:
                    row.last = text
                    note(f"{target} = {text}")
                else:
                    note(f"{target}: ERROR {text}")
        except queue.Empty:
            pass

        clamp()
        h, w = stdscr.getmaxyx()
        _draw(stdscr, args, rows, sel, top, snap, message, filt)

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("g"), ord("G"), ord("s")) and shared.stop.is_set():
            note("device worker is stopped")
            continue
        if key in (ord("q"), ord("Q")):
            break
        elif key in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif key == curses.KEY_PPAGE:
            sel -= visible_height()
        elif key == curses.KEY_NPAGE:
            sel += visible_height()
        elif key == curses.KEY_HOME:
            sel = 0
        elif key == curses.KEY_END:
            sel = len(rows) - 1
        elif key == curses.KEY_RESIZE:
            pass  # redrawn next loop with new dimensions
        elif key == ord("g"):
            if rows:
                do_get(sel)
        elif key == ord("G"):
            count = 0
            for r in rows:
                if r.access != "WRITE_ONLY" and not r.pending:
                    if post_request("get", r):
                        count += 1
            note(f"polling {count} registers...")
        elif key in (ord("e"), ord("\n"), curses.KEY_ENTER):
            if rows:
                r = rows[sel]
                val = _prompt(stdscr, h - 2, f"Set {r.name} [{r.setbuf or '-'}] = ", w)
                if val is not None:
                    r.setbuf = val
                    note(f"{r.name} value-to-set: {val!r} (press s to write)")
        elif key == ord("s"):
            if rows:
                r = rows[sel]
                if r.access in ("READ_ONLY", "CONSTANT"):
                    note(f"{r.name}: not writable ({r.access})")
                elif not r.setbuf:
                    note(f"{r.name}: empty value-to-set (press e first)")
                else:
                    try:
                        value = parse_set_value(r.setbuf)
                    except ValueError as exc:
                        note(f"{r.name}: bad value: {exc}")
                    else:
                        if post_request("set", r, value):
                            note(f"{r.name} <- {r.setbuf} ...")
        elif key == ord("/"):
            val = _prompt(stdscr, h - 2, "Filter registers: ", w)
            filt = "" if val is None else val
            still_pending = set(pending.values())
            rows = build_rows(args.flavor, args.all, filt)
            for r in rows:
                r.pending = r.name in still_pending
            sel, top = 0, 0
            note(f"filter '{filt or '-'}': {len(rows)} registers")

    shared.stop.set()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live TUI: streamed Text values plus per-register Get/Set table.")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    shared = Shared()
    thread = threading.Thread(target=_worker_entry, args=(args, shared),
                              daemon=True)
    thread.start()
    if not shared.ready.wait(timeout=10):
        print("error: worker did not start", file=sys.stderr)
        return 1
    try:
        curses.wrapper(_run_ui, args, shared)
    finally:
        shared.stop.set()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
