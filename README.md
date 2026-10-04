# ve.direct

An async Python client for Victron VE.Direct. Read checksum-validated Text
telemetry, query and write HEX registers, and receive unsolicited HEX
notifications over the same connection.

The library includes register definitions and decoders for BlueSolar and
SmartSolar MPPT, MPPT RS, Orion XS, BMV-7xx and compatible SmartShunt devices,
and Phoenix inverters. It requires Python 3.9 or newer.

## Install

Install from a checkout of this repository:

```sh
git clone https://github.com/bluer222/ve.direct.git
cd ve.direct
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ".[serial]"
```

The `serial` extra installs `pyserial` and `pyserial-asyncio`. For parsers,
register codecs, or a custom transport, `python -m pip install .` installs
the core library without runtime dependencies.

## Read a device

This example queries an MPPT charger, reads one Text block, then streams
subsequent blocks. Change the serial port and `flavor` for your device.

```python
import asyncio

from ve_direct import AsyncVEDirect


async def main():
    async with await AsyncVEDirect.from_port(
        "/dev/ttyUSB0", flavor="MPPT"
    ) as dev:
        print("Product ID:", hex(await dev.get_product_id()))
        print("Panel voltage:", await dev.hex_get("PANEL_VOLTAGE"))

        frame = await dev.read_one(timeout=5.0)
        print(frame.to_si())
        if "CS" in frame:
            print("Charge state:", frame["CS"].human)

        async for frame in dev.frames():
            print(frame.to_si())


asyncio.run(main())
```

Serial defaults are 19200 baud, 8 data bits, no parity, and one stop bit.
`from_port()` requests `dtr=True` and `rts=False`; both are configurable.

Each field retains its original text in `.raw`, its decoded value in
`.value`, its wire unit in `.unit`, and any readable interpretation in
`.human`. `frame.to_si()` converts measurements to units such as volts,
amps, amp-hours, kilowatt-hours, and percentages. State codes remain numeric;
use `.human` for their labels. Missing measurements decode as `None`.
Nonnumeric measurements retain their raw strings through `to_si()`, so a
bad voltage field does not prevent conversion of a valid current field.

Fields vary by device, firmware, and block. BMV devices can alternate
between blocks with different fields, so check for a label before using it.
See the [Text field reference](docs/TEXT_FRAMES.md) for labels and units.

## Local web GUI

Run the GUI against your device, or use `--sim --open` for an offline demo:

```sh
python tools/vedirect_gui.py --port /dev/ttyUSB0 --flavor MPPT --open
```

Click **Dump available values** to download a JSON snapshot of the latest
text frame and cached successful HEX register results, with register metadata,
source, device flavor, and a UTC export timestamp. Values use the same string
representations shown in the GUI. The dump includes cached registers regardless
of the current table filter. To collect more register values first, click
**Get all visible** and wait for the reads to finish. Unread registers are omitted.

## Import descriptions

Two dictionaries map protocol keys to readable descriptions:

```python
from ve_direct.descriptions import STREAM_DESCRIPTIONS, HEX_DESCRIPTIONS

print(STREAM_DESCRIPTIONS["V"])                  # Main / channel 1 (battery) voltage
print(HEX_DESCRIPTIONS["MPPT"]["PANEL_VOLTAGE"])  # Panel voltage
print(HEX_DESCRIPTIONS["BMV"]["SOC"])             # State of charge

async for frame in dev.frames():
    for key, value in frame.to_si().items():
        print(STREAM_DESCRIPTIONS.get(key, key), value)
```

`STREAM_DESCRIPTIONS` covers all known text labels across device families;
each frame contains only the labels sent by that device. `HEX_DESCRIPTIONS`
contains separate dictionaries for `ANY`, `BMV`, `BMV71`, `CHG`, `INV`, `MPPT`,
`MPPT_SMALL`, `MPPT_RS`, `MULTI_RS`, and `ORION`. Each flavor includes all
registers returned by `for_flavor()`, including shared and inherited registers,
aliases, and all history/timer entries. Shared names appear in every applicable
flavor dictionary. `ANY` contains the common registers shared by all flavors.
HEX descriptions expand symbolic names into readable labels. Unknown and
experimental Multi RS registers are marked. Names keep device-specific meanings
separate when numeric register IDs overlap.

Regenerate the bundled dictionaries after changing the protocol definitions:

```sh
python tools/generate_descriptions.py
python tools/generate_descriptions.py --check
```

To use them in a project without installing this library, generate a standalone
Python module and import its dictionaries:

```sh
python tools/generate_descriptions.py -o /path/to/project/vedirect_descriptions.py
```

```python
from vedirect_descriptions import STREAM_DESCRIPTIONS, HEX_DESCRIPTIONS
```

## Select a device family

Set `flavor` when opening a connection. Some register IDs have different
meanings, scales, or layouts across families, and the client uses this
setting to select the correct decoder and encoder.
Flavor names are case-insensitive, including later assignments to `dev.flavor`.

| Flavor | Device or register family |
| --- | --- |
| `MPPT` | BlueSolar and SmartSolar MPPT; uses the large-model battery-type table |
| `MPPT_SMALL` | 10A, 15A, and 20A MPPT models without a rotary switch |
| `MPPT_RS` | MPPT RS |
| `ORION` | Orion XS DC-DC chargers |
| `BMV` | BMV-700/702/712 and compatible SmartShunt devices |
| `BMV71` | BMV-712 and SmartShunt variant, inheriting BMV definitions |
| `INV` | Phoenix inverters |
| `CHG` | Shared charger register definitions |

For example, battery-type value `1` means `type_2` on `MPPT_SMALL` and
`type_1` on `MPPT`. The client does not infer this distinction from a
ProductId command reply. That command returns a two-byte little-endian ID
and needs no flavor; Get register `0x0100` has separate layout rules.

## Read and write registers

Within an open async connection, use documented register names:

```python
current_limit = await dev.hex_get("BAT_MAX_CURRENT")  # Amps
await dev.hex_set("BAT_MAX_CURRENT", 35.0)           # Set 35 A
```

Typed access applies the register's scale and representation. Enums accept
named values, bitmasks accept lists of bit names, and structured records
such as history and timers decode to dictionaries. Documented missing-value
sentinels decode as `None`. BMV description writes allow 19 ASCII characters
plus the terminating NUL within the String20 limit.
Enum labels are case-sensitive. Unknown labels, including load-control
labels such as `"autp"` or `"OFF"`, raise `ValueError` before writing.
Incomplete numeric, boolean, enum, or bitmask values raise `RegisterError`;
the raw APIs retain the original bytes for diagnosis. Empty VOID responses
remain valid. Flagged async error notifications retain raw bytes in `value`.
Enum, bitmask, and structured integer writes reject fractional values and
integers that do not fit their wire fields with `ValueError` before transmission. One-bit flags
and boolean registers accept booleans or numeric 0/1, and reject strings.
Use `False` to switch a boolean setting off. These checks enforce wire
representation; the device still validates its model-specific setting ranges.

`hex_get()` and `hex_set()` also accept numeric IDs. An ID shared by different
register definitions requires a flavor; otherwise lookup raises
`AmbiguousRegisterError`. Named lookup with a flavor selects that family's
definition. Reading a write-only register or writing a read-only or constant
register raises `RegisterError`.

For registers outside the catalog, use the raw API. It returns a `HexFrame`
whose `.value_bytes` contains the unconverted payload:

```python
reply = await dev.hex_get_raw(0xEDF0)
print(reply.value_bytes)
# hex_set_raw(register_id, payload_bytes) sends an explicitly encoded value.
```

The [HEX register reference](docs/HEX_REGISTERS.md) lists names, access rules,
units, and family differences. A catalog entry does not guarantee that every
model or firmware supports that register; the device's error flags are
reported as `RegisterError`.

## Streaming and connection recovery

`frames()` yields Text frames and `async_notifications()` yields decoded
unsolicited HEX notifications. They can run alongside HEX requests on the
same connection. A shared read lock routes incoming data to its consumer;
Text and notifications received during a command are queued for later
consumption. HEX request/response transactions are serialized.
Text read timeouts and cancelled stream consumers preserve a transport read
serving a pending HEX request, so that request can still receive its reply.

Both event queues hold up to 256 events by default. Configure positive integer
`text_queue_size` and `async_queue_size` on `AsyncVEDirect`, `from_streams()`,
or `from_port()`. When a queue is full, the client discards its oldest queued
event and retains the newest. `dropped_text_frames` and `dropped_async_frames`
count those discarded events. Below capacity, each queue preserves every
event in arrival order. Overflow never blocks reading or HEX reply routing.

VE.Direct has no transaction IDs. After a reply timeout, a failed read or write, or
cancellation after transmission may have started, the client blocks further
HEX requests with `TransportError` so a late reply cannot satisfy a newer
request. Reset the device/link and drain stale host input before calling
`reset_hex_state()`. That method clears partial parser state while preserving
already queued frames. `restart()` also leaves HEX routing blocked until
this recovery is complete. Text reception remains available.

`read_one()` and HEX reply timeouts raise `ve_direct.TimeoutError`, which
inherits from `VeDirectError`. EOF and calls interrupted by `close()` raise
`TransportError`. Other transport errors retain their original exception
types. Correlated device rejections raise `RegisterError` and permit later
requests without resetting the link.

`close()` cancels and awaits the client's active transport operations, wakes
blocked Text/notification consumers and pending requests with `TransportError`,
and closes a stored StreamWriter. It does not cancel the caller's tasks.
For custom transport cleanup, pass an optional `close_transport` callback
to the constructor. The callback runs once and can be synchronous or async.
Custom read/write callables must allow cancellation to finish.

`boot(timeout=0.2)` sends the boot-entry command and observes incoming traffic
for a failure during that interval. A failure raises `RegisterError`; silence
does not confirm boot entry. Later boot failures are logged when read and
cannot reject an unrelated command. Command `0` is reserved on BMV devices,
so `boot()` rejects it for `BMV` and `BMV71`.

## Try it without hardware

Run the included Text simulator from the repository root:

```sh
python examples/async_log.py --sim
```

Applications can use `SimulatedTransport` and `make_text_block` directly, or
provide their own async `read_chunk(n)` and `write(data)` callables to
`AsyncVEDirect`. `AsyncVEDirect.from_streams(reader, writer)` accepts an
existing asyncio stream pair. The [examples](examples/) also include Text
logging from a serial port and HEX register queries.

## Validation and scope

The frozen conformance scope covers public Text reception, documented HEX
commands and register codecs, and the library's async delivery contracts.
It pins five Victron specifications: Text Protocol 3.34, BlueSolar HEX Rev 18,
Orion XS HEX dated 16 June 2025, BMV-7xx HEX Rev 7, and Phoenix Inverter HEX
Rev 5.

The [acceptance report](conformance/RESULTS.md) records 191 requirement
groups, with all 125 executable host requirements passing across 122 tests
and 2,114 subtests. Specification ambiguities and device obligations remain
explicit. The [scope and stopping rule](conformance/SCOPE.md) define the
claim; the [audit findings](conformance/FINDINGS.md) record the resolved
defects and their regression criteria.

Firmware image transfer, private commands, VE.Can framing, and physical
device behavior are outside this host-library acceptance scope. Undocumented
Multi RS definitions are separate experimental entries in
`EXPERIMENTAL_MULTI_RS_REGISTERS`.

With the serial extra installed, run these from the repository root:

```sh
python conformance/run.py
python -m unittest discover -s tests
```

The conformance gate runs offline and requires a POSIX host for its serial
pseudo-terminal tests. It fails on missing evidence, skipped required tests,
or changed pinned sources. The ordinary suite includes live specification
freshness checks; install Poppler's `pdftotext` and provide network access
for a run without skipped freshness checks. Nix setup is documented in the
[conformance scope](conformance/SCOPE.md).

[CI](https://github.com/bluer222/ve.direct/actions/workflows/ci.yml) runs both
suites on Python 3.9, 3.12, and 3.14, including a weekly specification check.
For source citations and documented conflicts, see
[Sources](docs/SOURCES.md) and [Specification exceptions](docs/SPEC_EXCEPTIONS.md).
