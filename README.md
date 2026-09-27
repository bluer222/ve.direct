# ve-direct — async Python for Victron VE.Direct (Text + HEX)

Async-only client covering the public Text spec (3.34 + BlueSolar-HEX PIDs:
every `LABEL` mapped to units/description/`CS`/`ERR`/`OR`/`CAP_BLE`/`AR`/
`MODE`/`MPPT`/`MON`/`PID` — 171 PIDs incl. 0xA048, 0xA110/0xA111, Phoenix
HEX Rev 5 0xA2B1/0xA2E1/0xA2F1 families) plus HEX
framing with an extensive 400+ register database validated against
Victron's official documentation (BlueSolar-HEX Rev 18, Orion-XS-HEX,
BMV-7xx-HEX Rev 7, Phoenix-Inverter-HEX Rev 5, and Text Protocol 3.34):
history (0x1030/0x104F/0x1050-0x106E/0x10A0-0x10BE, Orion 0x1042/0x1043/
0x106F/0x1099/0x1070-0x1098), VE.Smart networking 0x2000-0x2027, relay
0x0350-0x0353/0xEDD9/0xEDBA/0xEDB9/0x100A, display 0x0400-0x0408, load/
lighting/ports, charger/battery extras — STRUCT decoders return dicts,
sentinels (`0xFFFF`/`0x7FFF`/`0x7FFFFFFF`) decode as `None`.

Scope is the publicly documented host protocol: Text reception and documented
HEX commands/registers for MPPT, Phoenix Inverter, BMV-7xx/SmartShunt and Orion
XS. Firmware image transfer and VE.Can are separate protocols. `MULTI_RS`
entries are explicitly exported as `EXPERIMENTAL_MULTI_RS_REGISTERS` because
no public Victron HEX specification exists for them.

```python
async with await AsyncVEDirect.from_port("/dev/ttyUSB0") as dev:
    async for frame in dev.frames():          # checksum-validated Text
        print(frame.to_si())                  # SI floats + human enums
        # {'PID': 41034, 'V': 27.69, 'CS': 3, ...}
        print(frame["CS"].human)              # "Bulk"
    print(await dev.hex_get("PANEL_VOLTAGE")) # 31.3
    await dev.hex_set("BAT_MAX_CURRENT", 35.0)
```

## Validation & Tools

- `tools/fetch_specs.py` — Downloads all 5 official Victron Energy PDF specifications and extracts layout text for continuous verification.
- `tools/spec_parser.py` — Automated specification parsers for BlueSolar, Orion-XS, BMV-7xx, Phoenix Inverter, and Text Mode specs.
- `tests/test_spec_parsers.py` — Table coverage and metadata checks for register IDs, scale factors, data types, units, and aliased registers.
- `conformance/` — [Finite acceptance scope](conformance/SCOPE.md), [audit findings](conformance/FINDINGS.md), requirement-to-test ledgers, and independent behavioral tests. Run `python3 conformance/run.py` with the serial extra installed on a POSIX host; [the generated report](conformance/RESULTS.md) records failures and evidence gaps. Table coverage alone does not establish protocol correctness.

## Layout

- `src/ve_direct/tables.py` — Text labels, CS/ERR/OR/CAP_BLE/AR/MODE/MPPT/MON/PID maps
- `src/ve_direct/text.py` — byte-state `TextParser` (incremental, mid-record
  HEX-interleave safe, corruption resynchronization), `Frame`, `to_si()`
- `src/ve_direct/hex.py` — `:CMD+DATA+CHECK` framing (`sum == 0x55`), Get/Set/Ping/AppVersion/ProductId/Restart/Boot builders (`decode_line(strict=True)` raises `ChecksumError`, `decode_app_version()` parses fw type)
- `src/ve_direct/registers/` — HEX DB + `lookup/decode_value/encode_value/for_flavor/decode_bat_type`
  (`hex_get`/`hex_set` take documented names; `hex_get_raw`/`hex_set_raw` take raw ids; BITMASK encode accepts `["bit_name", ...]`; STRUCT (history/timer/load-control/network-status/voltage-range) decodes to dict; same-ID aliases require specifying `flavor` when looked up by ID via `lookup(id, flavor=...)`, otherwise raising `AmbiguousRegisterError` — prefer names or pass flavor)
- `src/ve_direct/device.py` — `AsyncVEDirect` (shared-wire Text/HEX demux,
  serialized request routing, typed `async_notifications()`)
- `src/ve_direct/transport.py` — `SimulatedTransport` + `make_text_block` (no hardware needed)

## Serial

`pip install 've-direct[serial]'` (pyserial + pyserial-asyncio). Core + tests are
stdlib-only: `python3 -m unittest discover -s tests`.

## Spec notes

- Text: `19200 8N1`, `\r\nLABEL\tVALUE`, block/sec, `Checksum` byte makes
  `sum(block) % 256 == 0`. BMV sends alternating PID/history blocks.
- HEX: `:<nibble><hex><check>\n`, LE values, Get `:7<id>00`, Set `:8<id>00<val>`
  (e.g. `:8F0ED0064000C`), ping `:154`, app-version `:352`, product-id `:451`,
  restart `:64F` (no reply), async `:A<id><flags><val>`.
  `dev.ping()/get_app_version()/get_product_id()/restart()` available.
- Unknown registers work by raw id via `hex_get_raw()`/`hex_set_raw()` (payload
  bytes undecoded); `hex_get()`/`hex_set()` require a documented register name.
  `WRITE_ONLY` (e.g. `RESTORE_DEFAULTS 0x0004`, `CLEAR_HISTORY 0x1030`,
  `REMOTE_COMMAND 0x2004`) raises on `hex_get`, allowed on `hex_set`.
  Flavors form a hierarchy (`MPPT->CHG`, `MPPT_SMALL/MPPT_RS->MPPT`, `MULTI_RS->INV+MPPT_RS`,
  `BMV71->BMV`, `ORION->CHG`; see `docs/HEX_REGISTERS.md`).
- Product ID command replies carry a two-byte little-endian ID for all
  documented families, with no flavor needed. Register `0x0100` has separate
  version-dependent layout rules, including its four-byte form.
- VE.Direct has no transaction IDs. Requests are serialized. After a request
  timeout or cancellation, the client fails closed to prevent a late response
  being attributed to a newer request; reset the link/device and call
  `reset_hex_state()` before issuing another HEX request.
- Command `0` boot entry is rejected for BMV devices, where it is reserved.
- `restart()` leaves HEX routing disabled until stale host input has been
  drained and `reset_hex_state()` is called. Text reception continues.
- `boot(timeout=0.2)` observes incoming traffic for boot failure during that
  interval. A failure raises `RegisterError`; silence does not confirm boot
  entry. Later boot failures are logged when read and cannot fail another command.
- Select `flavor="MPPT_SMALL"` for 10A/15A/20A MPPT models without a rotary
  switch so typed battery settings use the small-model table.
