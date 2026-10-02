#!/usr/bin/env python3
"""Basic VE.Direct utility: view streamed Text values, read/write HEX registers.

Usage:
    python tools/vedirect_cli.py --port /dev/ttyUSB0 [--flavor MPPT] stream
    python tools/vedirect_cli.py --port /dev/ttyUSB0 get PANEL_VOLTAGE DEVICE_STATE
    python tools/vedirect_cli.py --port /dev/ttyUSB0 set BAT_MAX_CURRENT 35.0
    python tools/vedirect_cli.py --sim stream --count 2
    python tools/vedirect_cli.py list --flavor BMV --search relay

Connection options can also come from VE_DIRECT_PORT / VE_DIRECT_FLAVOR env vars.
Requires pyserial-asyncio for real hardware (pip install ".[serial]");
--sim runs fully offline with SimulatedTransport.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ve_direct import AsyncVEDirect  # noqa: E402
from ve_direct.transport import SimulatedTransport, make_text_block  # noqa: E402


def parse_set_value(text: str):
    """Parse a CLI value for hex_set: JSON object/array, bool, int, float, else str.

    Bitmask registers accept comma-separated names ("a,b") -> ["a", "b"].
    Plain enum labels stay strings; use JSON '{"mode": 1}' for STRUCT dicts.
    """
    s = text.strip()
    if s.startswith("{") or s.startswith("["):
        return json.loads(s)
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s, 0)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if "," in s and not s.startswith("0x"):
        return [part.strip() for part in s.split(",") if part.strip()]
    return s


def format_frame(frame, *, si: bool) -> str:
    lines = []
    values = frame.to_si() if si else None
    for label, tv in frame.fields.items():
        shown = values[label] if si else tv.value
        unit = f" {tv.unit}" if tv.unit else ""
        human = f" [{tv.human}]" if tv.human else ""
        lines.append(f"  {label:<8} {tv.raw:<12}{unit}  -> {shown}{human}  ({tv.desc})")
    return "\n".join(lines)


def make_sim_device(args) -> AsyncVEDirect:
    sim = SimulatedTransport(
        text_chunks=[
            make_text_block({
                "PID": "0xA04A", "V": "27690", "I": "4400",
                "VPV": "31300", "PPV": "125", "CS": "3",
                "MPPT": "2", "ERR": "0", "LOAD": "ON",
            }),
        ],
        flavor=args.flavor or "MPPT",
    )
    return AsyncVEDirect(sim.read_chunk, sim.write, flavor=args.flavor)


async def open_device(args) -> AsyncVEDirect:
    if args.sim or not args.port:
        if not args.sim and not args.port:
            print("No --port given, using --sim demo mode.", file=sys.stderr)
        return make_sim_device(args)
    return await AsyncVEDirect.from_port(
        args.port, baudrate=args.baudrate, flavor=args.flavor)


async def cmd_stream(args) -> int:
    dev = await open_device(args)
    async with dev:
        wanted = {l.strip() for l in args.labels.split(",") if l.strip()} if args.labels else None
        shown = 0
        async for frame in dev.frames():
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"--- frame #{shown + 1} @ {ts} ---")
            if wanted:
                sub = {k: v for k, v in frame.fields.items() if k in wanted}
                if not sub:
                    print("  (no requested labels in this frame)")
                    continue
                proxy = type(frame)(sub)
                print(format_frame(proxy, si=not args.raw))
            else:
                print(format_frame(frame, si=not args.raw))
            shown += 1
            if args.count and shown >= args.count:
                break
    return 0


async def cmd_get(args) -> int:
    dev = await open_device(args)
    async with dev:
        rc = 0
        for name in args.registers:
            try:
                value = await dev.hex_get(name, timeout=args.timeout)
                print(f"{name} = {value!r}")
            except Exception as exc:  # noqa: BLE001 - report per-register, keep going
                print(f"{name}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
                rc = 1
        return rc


async def cmd_set(args) -> int:
    dev = await open_device(args)
    async with dev:
        value = parse_set_value(args.value)
        try:
            result = await dev.hex_set(args.register, value, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001 - single op, report cleanly
            print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"{args.register} <- {value!r}  (readback: {result!r})")
        return 0


def cmd_list(args) -> int:
    from ve_direct.registers import REGISTERS, for_flavor

    regs = for_flavor(args.flavor) if args.flavor else dict(REGISTERS)
    needle = args.search.lower() if args.search else ""
    rows = []
    for name in sorted(regs):
        reg = regs[name]
        blob = f"{name} 0x{reg.id:04X} {reg.cls} {reg.access} {reg.dtype} {reg.unit}".lower()
        if needle and needle not in blob:
            continue
        rows.append(
            f"{name:<28} 0x{reg.id:04X}  {reg.cls:<8} {reg.access:<10} "
            f"{reg.dtype:<16} {reg.unit or '-':<8} x{reg.scale:g}  [{'|'.join(sorted(reg.flavors))}]"
        )
    print("\n".join(rows))
    print(f"({len(rows)} registers)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="View VE.Direct Text stream and read/write HEX registers.")
    p.add_argument("--port", default=os.environ.get("VE_DIRECT_PORT"),
                   help="Serial port (e.g. /dev/ttyUSB0). Omit for --sim.")
    p.add_argument("--flavor", default=os.environ.get("VE_DIRECT_FLAVOR"),
                   help="Device family: MPPT, MPPT_SMALL, MPPT_RS, ORION, BMV, BMV71, INV, CHG.")
    p.add_argument("--baudrate", type=int, default=19200)
    p.add_argument("--timeout", type=float, default=2.0,
                   help="HEX reply timeout in seconds (default 2.0).")
    p.add_argument("--sim", action="store_true",
                   help="Use offline simulator instead of hardware.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stream", help="Print streamed Text frames.")
    s.add_argument("--count", type=int, default=0,
                   help="Stop after N frames (0 = forever).")
    s.add_argument("--raw", action="store_true",
                   help="Show raw wire values instead of SI conversion.")
    s.add_argument("--labels", default="",
                   help="Comma-separated labels to show (default: all).")
    s.set_defaults(func=cmd_stream)

    g = sub.add_parser("get", help="Read one or more HEX registers.")
    g.add_argument("registers", nargs="+",
                   help="Register names or numeric IDs (e.g. PANEL_VOLTAGE 0xEDBB).")
    g.set_defaults(func=cmd_get)

    w = sub.add_parser("set", help="Write a HEX register (careful: real hardware!).")
    w.add_argument("register", help="Register name or numeric ID.")
    w.add_argument("value",
                   help="Value: number, true/false, enum label, 'bit1,bit2', or JSON dict.")
    w.set_defaults(func=cmd_set)

    l = sub.add_parser("list", help="List known registers (no hardware needed).")
    l.add_argument("--flavor", default=argparse.SUPPRESS,
                   help="Device family filter (also a global flag before the subcommand).")
    l.add_argument("--search", default="",
                   help="Filter substring (name, ID, class, access...).")
    l.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Numeric IDs arrive as strings; int(x, 0) accepts "0xEDBB" and "60347".
    if args.cmd in ("get",):
        parsed = []
        for item in args.registers:
            try:
                parsed.append(int(item, 0))
            except ValueError:
                parsed.append(item)
        args.registers = parsed
    if args.cmd == "set":
        try:
            args.register = int(args.register, 0)
        except ValueError:
            pass
    try:
        result = args.func(args)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
