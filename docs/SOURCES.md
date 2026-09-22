# Sources — what came from where

This file tracks the provenance of every protocol table in this repo:
official Victron docs vs `krahabb/esphome-victron-vedirect` (upstream port
source) vs reverse-engineering. Code references are `file:line` (approx).

## Documents consulted

| Doc | Revision / date | URL | Used for |
|---|---|---|---|
| VE.Direct-Protocol (Text) | 3.34, 2025-04-14 | https://www.victronenergy.com/upload/documents/VE.Direct-Protocol-3.34.pdf | All Text `LABEL`s, units, `CS`/`ERR`/`AR`/`WARN`/`OR`/`CAP_BLE`/`MODE`/`MPPT`/`MON`, 153 PIDs, serial `19200 8N1`, checksum `sum%256==0` |
| BlueSolar-HEX | Rev 18, 2021-11-15 | https://www.victronenergy.com/upload/documents/BlueSolar-HEX-protocol.pdf | HEX framing §1, BlueSolar/SmartSolar MPPT Get/Set items §1.1, async §1.2, examples §1.3, history, networking, relay/display/load/lighting/ports, 3 extra PIDs (`0xA048`, `0xA110/0xA111`) |
| Orion-XS-HEX | 2025-06-24 (web index) | https://www.victronenergy.com/upload/documents/Orion-XS-HEX-protocol.pdf | HEX framing §1 (incl. 24-bit version, response `0x2` checksum-error), Orion product info, `0x0206`, battery settings, engine shutdown `0xEE36-0xEE39`, input lockout `0x0320/0x0321`, in/out aliases, `0xD18D`/`0xEE12`, Orion history `0x1042/0x1043/0x106F/0x1099/0x1070-0x1098`, error/warning table |
| Phoenix-Inverter-HEX | Rev 5, 2021-09-06 | https://www.victronenergy.com/upload/documents/VE.Direct-HEX-Protocol-Phoenix-Inverter.pdf | Independently parsed and checked for IDs, widths, scales, units and R/W access |
| BMV-7xx-HEX | Rev 7, 2021-07-01 | https://www.victronenergy.com/upload/documents/BMV-7xx-HEX-Protocol.pdf | Independently parsed and checked for IDs, widths, scales, units and sentinels |
| krahabb `ve_reg_register.h` | `main` branch | https://raw.githubusercontent.com/krahabb/esphome-victron-vedirect/main/components/m3_vedirect/ve_reg_register.h | Port baseline: 110 common regs; `INV`/`CHG`/`BMV`/`RS` assignments; `UNKNOWN_`/`U_` convention (see below) |
| krahabb `ve_reg_enum.h` | `main` branch | https://raw.githubusercontent.com/krahabb/esphome-victron-vedirect/main/components/m3_vedirect/ve_reg_enum.h | Enum/bitmask baseline (`DEVICE_MODE`, `DEVICE_STATE`, `CHR_ERROR_CODE`, `WARNING_REASON`, `OFF_REASON`, `CAPABILITIES`, …) |
| krahabb `ve_reg_flavor.h` | `main` branch | https://raw.githubusercontent.com/krahabb/esphome-victron-vedirect/main/components/m3_vedirect/ve_reg_flavor.h | Flavor hierarchy (`MPPT->CHG`, `MPPT_RS->MPPT`, `MULTI_RS->INV+MPPT_RS`, `BMV71->BMV`, `ORION->CHG` extension is ours) |

## Text mode (`src/ve_direct/tables.py`, `src/ve_direct/text.py`)

- All 65 `LABEL_INFO` entries, wire units, `to_si()` scales: Text 3.34 Table I
  (`src/ve_direct/tables.py:10-76`, `src/ve_direct/text.py:143-168`).
- `CS`/`ERR`/`AR`+`WARN`/`OR`/`CAP_BLE`/`MODE`/`MPPT`/`MON` maps + notes
  (`---`, `On/Off` case-insensitivity, `TTG -1`, `FW C208`, `HSDS 0..364`):
  Text 3.34 Detailed field description (`src/ve_direct/tables.py:78-192`).
- 171 PIDs = 153 from Text 3.34 PID list + `0xA048`, `0xA110`, `0xA111` from
  BlueSolar-HEX + 15 Phoenix-HEX Rev 5 IDs (`0xA2B1/A2B2/A2B4` 5000VA Smart,
  `0xA2E*` 800VA HS, `0xA2F*` 1200VA HS)
  (`src/ve_direct/tables.py:196-368`).
- Wire framing/checksum/multi-block/`:A` interleave: Text 3.34 and the official
  FAQ reference frame handler (checksum label compared case-insensitively). HEX start detection is byte-state based, not
  line-regex based, because a reply can immediately follow the raw Text
  checksum or interrupt a record.
  Introduction + Message format + Data integrity
  (`src/ve_direct/text.py:64-66,171-228`, `src/ve_direct/device.py:127-155`).

## HEX framing (`src/ve_direct/hex.py`, `src/ve_direct/device.py`)

- `:<cmd><LE-hex><check>\n`, `(cmd+sum+check)&0xFF==0x55`, uppercase hex,
  LE numbers, Get `:7<id>00`, Set `:8<id>00<val>` (e.g. `:8F0ED0064000C`),
  ping `:154`, app-version `:352`, product-id `:451`, restart `:64F` (no reply),
  boot `:0<51FA*5>`, async `:A`, flags `0x01/0x02/0x04`: BlueSolar-HEX §1 +
  Orion-XS-HEX §1 (`src/ve_direct/hex.py:54-68,132-166`).
- Done/Get/Set/Unknown/Error/Ping response codes + `0xAAAA` framing error:
  BlueSolar-HEX §1 (`src/ve_direct/hex.py:17-51`).
- Product-id command responses are family-specific: BlueSolar/Orion use the
  standardized 4-byte `instance+pid+reserved` form; BMV Rev 7 and Phoenix Rev 5
  document 2-byte little-endian command responses.
  (`src/ve_direct/device.py:342-354`).
  Orion doc-internal tension (no behavior change): Orion-XS-HEX §1.3 examples
  answer `:451` with a 2-byte reply (`:1F0A3C1`), but the normative §1.1
  product-id text says the 2-byte form "should be ignored" (incorrect
  endianness on some builds). The code follows the normative text and rejects
  2-byte replies for ORION; see the `get_product_id` docstring reasoning.
- App-version 2-byte (top 2 bits fw type, minor hex digits) vs Orion 24-bit:
  BlueSolar-HEX §1 + Orion-XS-HEX §1 Note 1
  (`src/ve_direct/hex.py:54-73`, `decode_app_version`).
- `BOOT_MAGIC 51FA*5`: BlueSolar-HEX §1 command 0
  (`src/ve_direct/hex.py:29-30`).

## Register DB (`src/ve_direct/registers/__init__.py`)

### Official — BlueSolar-HEX Rev 18 §1.1 (MPPT + shared)

- Product info: `0x0100`, `0x0104`, `0x010A`, `0x010B`, `0x0140`
- Generic control: `0x0200`, `0x0201`, `0x0202`, `0x0205`, `0x0207`
- Battery settings: `0xEDFF`, `0xEDFE`, `0xEDFD`, `0xEDFC`, `0xEDFB`, `0xEDF7`,
  `0xEDF6`, `0xEDF4`, `0xEDF2`, `0xEDF1`, `0xEDF0`, `0xEDEF`, `0xEDEC`,
  `0xEDEA`, `0xEDE8`, `0xEDE7`, `0xEDE6`, `0xEDE5`, `0xEDE4`, `0xEDE3`,
  `0xED2E`, `0xEDE0`, `0xEDCA`
- Charger data: `0xEDDF`, `0xEDDD`, `0xEDDC`, `0xEDDB`, `0xEDDA`, `0xEDD7`,
  `0xEDD5`, `0xEDD4`, `0xEDD3`, `0xEDD2`, `0xEDD1`, `0xEDD0`, `0xEDCE`,
  `0xEDCD`, `0xEDCC`, `0xEDC7`, `0xEDC6`, `0x2211`, `0x2212`
- DC channel (MPPT RS): `0xED8B`, `0xED8D`, `0xED8F`
- Solar: `0x0244`, `0xEDBF`, `0xEDBC`, `0xEDBB`, `0xEDBD`, `0xEDB8`, `0xEDB3`,
  `0xEDB2`, `0xEDB1`, per-tracker `0xECC3/0xECCB/0xECCC/0xECCD`,
  `0xECD3/0xECDB/0xECDC/0xECDD`, `0xECE3/0xECEB/0xECEC/0xECED`,
  `0xECF3/0xECFB/0xECFC/0xECFD`
- Load: `0xEDAD`, `0xEDAC`, `0xEDAB`, `0xEDA9`, `0xEDA8`, `0xED9D`, `0xED9C`,
  `0xED91`, `0xED90`
- Relay: `0xEDD9`, `0x0350`, `0x0351`, `0x0352`, `0x0353`, `0xEDBA`, `0xEDB9`,
  `0x100A`
- Lighting: `0xEDA0-0xEDA5`, `0xEDA7`, `0xED9B`, `0xED9A`, `0xED99`, `0xED96`,
  `0xED97` (+ shared `0xED90`, `0x2030`, `0x2031`)
- Ports: `0xED9E`, `0xED98`
- Restore: `0x0004` (WRITE_ONLY, data ignored)
- History (STRUCT→dict): `0x1030` (WRITE_ONLY), `0x104F`,
  `0x1050-0x106E`, `0x10A0-0x10BE`
  (Doc-side ambiguity, resolved for the overview side: the BlueSolar-HEX
  History-data overview lists MPPT history as `0x10A0...0x10BE` — 31 registers,
  symmetric with daily history `0x1050...0x106E` (31) — while the History-MPPT
  day-record detail heading says `0x10A0..0x10BD` (30). The DB implements
  `0x10A0..0x10BE` following the overview/symmetry; see
  `src/ve_direct/registers/__init__.py` `range(0x10A0, 0x10BF)`.)
- Display: `0x0400`, `0x0401`, `0x0402`, `0x0403`, `0x0404`, `0x0406`, `0x0408`
- Networking: `0x2000`, `0x2001`, `0x2002`, `0x2003`, `0x2004` (WRITE_ONLY),
  `0x2007`, `0x2008`, `0x2009`, `0x200A`, `0x200B`, `0x200C`, `0x200D`,
  `0x200E`, `0x200F` (STRUCT), `0x2013`, `0x2014`, `0x2015`, `0x2018`, `0x2027`

### Official — Orion-XS-HEX (`ORION` flavor, inherits `CHG`)

- Engine shutdown: `0xEE36`, `0xEE37`, `0xEE38`, `0xEE39`
- Device function: `0x0206` (`DEVICE_FUNCTION`: 0 charger / 1 PSU)
- Sensing: `0xD18D` (sentinel `0x7FFFFFFF`), `0xEE12`
- History (STRUCT→dict): `0x106F`, `0x1099`, `0x1043`, `0x1042`,
  `0x1070-0x1098`
- Same-ID aliases, Orion names (see Aliases below): `0x0320/0x0321`
  (0.1V vs INV 0.01V), `0xEDBB/0xEDBD/0xEDBC/0xED8D/0xED8F` (same scale),
  `0xED8E` (uint32 0.01W vs BMV int16 1W)
- Shared battery/charger/sense/error tables with BlueSolar (same IDs)

### Official — Phoenix-Inverter-HEX Rev 5 (`INV` flavor)

Fully and independently parsed and validated directly from official Victron specifications via `tools/spec_parser.py`:

- Product info: `0x0100`, `0x0101` (revision), `0x0102` (app ver), `0x010A` (serial), `0x010B` (model)
- Capabilities: `0x0140` (capabilities 1), `0x0150` (BLE capabilities)
- Inverter control & settings: `0x0200` (mode), `0x0201` (state), `0x0207` (off reason),
  `0x0230` (voltage setpoint), `0x0231` (min voltage setpoint), `0x0232` (max voltage setpoint)
  (Note: `0x0209`/`0x020A` appeared in an earlier draft of this DB but in
  neither the Phoenix Rev 5 snapshot nor any other snapshot; both were
  removed as phantom registers.)
- Generic device control (§2.4): `0x0090` (BLE mode), `0xEC41` (settings changed) — shared `ANY` entries
- Relay (§2.8): `0x034E` (relay control, shared `ANY`), `0x034F` (`INV_RELAY_MODE` override, values 0,2,3,4,5,6 — no 1).
  No generic MPPT `0x034F` exists: the BlueSolar-HEX relay section documents
  only `0xEDD9`/`0x0350-0x0353`/`0xEDBA`/`0xEDB9`/`0x100A` (verified by ID
  search — no `0x034F` in the BlueSolar, Orion-XS, or Text snapshots), so the
  former MPPT-scoped generic entry was removed following the `0x0209`/`0x020A`
  phantom-removal precedent; only the INV/BMV overrides remain.
- Product-info extra: `0xEDEF` (battery voltage, `INV`-read-only alias of the CHG `BAT_VOLTAGE`);
  operation (§2.5): `0xED8D` (DC channel 1 / battery voltage, shared `BMV`/`INV`/`MPPT_RS`)
- AC output: `0x2200` (AC voltage), `0x2201` (AC current), `0x2202` (AC nominal voltage), `0x2203` (rated power),
  `0x2205` (apparent power), `0x2206` (load sense threshold), `0x2207` (load sense clear),
  `0x2210` (shutdown low-voltage set), `0x2211` (voltage range min), `0x2212` (voltage range max)
- Non-volatile memory: `0xEB99` (`INV_NVM_COMMAND`: 1=Save, 2=Revert, 3=Backup, 4=Default), `0x0004` (restore defaults)
- Eco mode & dynamic cut-off: `0xEB03` (50Hz/60Hz), `0xEB04` (eco min current), `0xEB06` (eco retry time),
  `0xEB10` (load detect periods), `0xEBBA` (dyn cutoff enable), `0xEBB1`/`0xEBB2`/`0xEBB3`/`0xEBB5`/`0xEBB7` (dyn cutoff parameters; `0xEBB4`/`0xEBB6` do not exist)
- Internal & diagnostics: `0xEB4E` (loop current), `0x031C` (warning reason), `0x031E` (alarm reason)
- History: `0x1040` (history time), `0x1041` (history energy)

### Official — BMV-7xx-HEX Rev 7 (`BMV` flavor, inherits `ANY`)

Fully and independently parsed and validated directly from official Victron specifications via `tools/spec_parser.py`:

- Monitor data: `0xED8D` (main voltage), `0xED7D` (aux starter voltage), `0xED8F` (current 0.1A),
  `0xED8C` (current precise 0.001A), `0xED8E` (power 1W), `0xEEFF` (consumed Ah 0.1Ah),
  `0x0FFF` (SOC 0.01%), `0x0FFE` (TTG min), `0xEDEC` (temp 0.01K), `0x0382` (midpoint voltage),
  `0x0383` (midpoint deviation), `0xEEB6` (synchronization state), `0xEEB8` (DC monitor mode,
  Sn16, BMV-712 v4.07+; visible under both `BMV` and `BMV71`), `0xEEFC` (buzzer, Un8 0..1)
- Monitor settings: `0x1000` (battery capacity), `0x1001` (charged voltage), `0x1002` (tail current),
  `0x1003` (charged detection time), `0x1004` (charge efficiency), `0x1005` (peukert coefficient),
  `0x1006` (current threshold), `0x1007` (TTG delta T), `0x1008` (discharge floor), `0x1009` (relay low SOC clear),
  `0x100B` (relay disable time), `0x1034` (user current zero), `0x1029` (zero current cmd), `0x102C` (synchronize cmd),
  `0x1030` (clear history cmd), `0x0FFD` (start synchronized)
- Historical statistics: `0x0300` (deepest discharge), `0x0301` (last discharge), `0x0302` (average discharge),
  `0x0303` (cycles), `0x0304` (full discharges), `0x0305` (cumulative Ah), `0x0306` (min battery voltage),
  `0x0307` (max battery voltage), `0x0308` (seconds since full charge), `0x0309` (automatic syncs),
  `0x030A` (low voltage alarms), `0x030B` (high voltage alarms), `0x030E` (min starter voltage),
  `0x030F` (max starter voltage), `0x0310` (discharged energy kWh), `0x0311` (charged energy kWh)
- Alarms & Relays: `0x0320..0x032D` (BMV alarm thresholds), `0x0331/0x0332` (midpoint alarm),
  `0x031F` (alarm acknowledge — table row has no write-only marker, so get+set),
  `0x034D` (relay invert, Un8 0..1), `0x034E` (relay state), `0x034F` (relay mode Un8 0..2),
  `0x0350..0x0357` (relay thresholds), `0x035A..0x035D` (relay temp), `0x0361/0x0362` (relay midpoint)
- Display & Shunt settings: `0xEEFE` (intensity), `0x0400` (always on, Un8 0..1), `0xEEF5` (scroll speed),
  `0xEEE0..0xEEE8` (show toggles, Un8 0..1), `0xEEF9` (SW version, Un16 HEX AA.BB),
  `0xEEF6` (setup lock, Un8 0..1), `0xEEFB` (shunt amps),
  `0xEEFA` (shunt volts), `0xEEF7` (temp unit), `0xEEF4` (temp coeff, unit `%CAP/°C`), `0xEEF8` (aux input),
  `0xEC41` (settings changed timestamp), `0x0120` (device uptime), `0x010C` (description)

### Reverse-engineered — krahabb, NO official doc

Header quote (`ve_reg_register.h`): "`UNKNOWN_` ... based off reverse
engineering field observations" / "`U_` prefix ... data meaning is inferred
... but still there's no official docs". All `MULTI_RS` flavor
(`src/ve_direct/registers/__init__.py:263-276`):

| Name | ID |
|---|---|
| `UNKNOWN_0305` | `0x0305` |
| `U_OUTPUT_YIELD` | `0x0310` |
| `U_USER_YIELD` | `0x0311` |
| `U_AC_OUT_VOLTAGE` | `0x2213` |
| `U_AC_OUT_CURRENT` | `0x2214` |
| `U_AC_OUT_REAL_POWER` | `0x2215` |
| `U_AC_OUT_APPARENT_POWER` | `0x2216` |
| `U_AC_OUT_CURRENT_MA` | `0xD3A1` |
| `UNKNOWN_D5C8` | `0xD5C8` |
| `UNKNOWN_D5CA` | `0xD5CA` |
| `UNKNOWN_D5CB` | `0xD5CB` |
| `UNKNOWN_2250` | `0x2250` |
| `UNKNOWN_2251` | `0x2251` |
| `UNKNOWN_0FFC` | `0x0FFC` |

Absent from BlueSolar-HEX, Orion-XS-HEX, and Text 3.34 (checked by ID
search); no public Multi RS / Inverter RS HEX doc exists (Victron manuals
limit that port to GlobalLink/VictronConnect, GX via VE.Can). They remain
available only as best-effort metadata and are explicitly listed in
`EXPERIMENTAL_MULTI_RS_REGISTERS`.

### Same-ID aliases (one wire ID, device-specific name/scale)

`REGISTERS` keeps every name. Calling `lookup(int)` on an aliased register
without a flavor raises
`AmbiguousRegisterError`; specifying `flavor` (e.g. `lookup(0xED8E, flavor="ORION")`)
resolves the device-specific register safely. A named lookup with a flavor is
also re-resolved by wire ID so generic names cannot silently use another
product's scale, width, enum, or access mode:

- `0x0320/0x0321`: `ALARM_LOW_VOLTAGE_SET/CLEAR` (INV, 0.01V) vs
  `INPUT_VOLTAGE_LOCKOUT/RESTART` (Orion, 0.1V) vs
  `BMV_ALARM_LOW_VOLTAGE/CLEAR` (BMV, 0.1V)
- `0xEDBB/0xEDBD/0xEDBC/0xED8D/0xED8F`: `PANEL_*`/`DC_CHANNEL1_*` (MPPT/BMV,
  plus INV for 0xED8D per Phoenix Rev 5 section 2.5)
  vs `INPUT_*/OUTPUT_*` (Orion) — same scale, different names
- `0xED8E`: `DC_CHANNEL1_POWER` (BMV, int16, 1W) vs `OUTPUT_POWER`
  (Orion, uint32, 0.01W)
- `0x2211/0x2212`: `VOLTAGE_RANGE_MIN/MAX` (INV) vs `ADJUSTABLE_VOLTAGE_*`
  (MPPT/MPPT_RS) — same scale
- `0xEDEF`: `BAT_VOLTAGE` (CHG, READ_WRITE) vs `INV_BAT_VOLTAGE` (INV,
  READ_ONLY, Phoenix section 2.1 product-info table) — same scale
- `0x0350/0x0351/0x0352/0x0353`: `RELAY_BAT_LOW_SET/LOW_CLEAR/HIGH_SET/HIGH_CLEAR`
  (MPPT/MPPT_RS, Un16 0.01 V, BlueSolar-HEX Relay settings) vs
  `BMV_RELAY_LOW_VOLTAGE/LOW_CLEAR/HIGH_VOLTAGE/HIGH_CLEAR` (BMV, Un16 0.1 V,
  BMV Rev 7 Relay settings). Phoenix Rev 5 and Orion-XS snapshots define no
  `0x0350-0x0353` (verified by ID search), so the generic entries are scoped to
  (`MPPT`, `MPPT_RS`) following the `0x0400` precedent; `for_flavor("BMV")`
  excludes them and `lookup(0x0350, flavor="BMV")` resolves the BMV meaning.
  Unflavored `lookup(0x0350)` raises `AmbiguousRegisterError`.

### Liberal flavoring policy (by design, not an error)

Flavor tags are a superset of strict doc provenance and are intentionally
not narrowed, because narrowing would break `for_flavor()` consumers with
no wire-behavior contradiction:

- Display `0x0401-0x0404` are `ANY` although BlueSolar-HEX scopes them to
  pluggable-LCD models; `0x0406`/`0x0408` stay `MPPT_RS` (internal display).
  `0x0400` is the exception: BMV Rev 7 defines it as Backlight Always On
  (Un8 0..1 OFF/ON) while BlueSolar-HEX defines it as Display backlight
  mode (0=keypress/1=on/2=auto), so `DISPLAY_BACKLIGHT_MODE` is scoped to
  (`MPPT`, `MPPT_RS`) and only `BMV_BACKLIGHT_ALWAYS_ON` is visible to
  `BMV`/`BMV71`. `0x0350-0x0353` are the second exception (same precedent):
  BlueSolar-HEX relay battery set/clear (0.01 V) vs BMV Rev 7 relay low/high
  voltage(+clear) (0.1 V), so generic `RELAY_BAT_*` is scoped to
  (`MPPT`, `MPPT_RS`) and only `BMV_RELAY_*` is visible to `BMV`/`BMV71`.
- `MPPT_HISTORY_*` (`0x10A0-0x10BE`) carry `MPPT` as well as `MPPT_RS`
  although BlueSolar-HEX Note 2 restricts them to multi-tracker units.
- Generic `ANY` control/status IDs (`APP_VER 0x0102`, `GROUP_ID`,
  `CAPABILITIES`, `DEVICE_MODE`/`DEVICE_STATE`, `WARNING`/`ALARM_REASON`,
  `RELAY_OPERATION_MODE`, `RELAY_PANEL_HIGH_*`, `DISPLAY_0x0401-0x0404`) are visible in
  the BMV view although BMV-7xx-HEX Rev 7 defines no such registers (verified
  by ID search of the snapshot: `0x0102`/`0x0104`/`0x0140`/`0x031C`/`0x031E`/
  `0xEDD9`/`0xEDBA`/`0xEDB9`/`0x0401`-`0x0404` never occur there, and
  `0x0200`-`0x0202` occur only as BMV60x product-ID values, not registers);
  likewise `0x0401-0x0404` are absent from the BMV display
  table (BMV documents only `0xEEFE`/`0x0400`/`0xEEF5`/`0xEEE0-0xEEE8` there).
- Un8 0..1 (OFF,ON) settings (BMV buzzer/show-toggles/setup-lock and friends,
  `RELAY_CONTROL`, `INV_DYN_CUTOFF_ENABLE`) stay `BOOLEAN`: Python bool ==
  0/1, so wire values round-trip and truthiness is preserved.

### Enums / sentinels / flavors

- Enums baseline `ve_reg_enum.h`, extended from docs: `RELAY_OPERATION_MODE`
  (BlueSolar relay §), `LOAD_OUTPUT_CONTROL`/`LOAD_OUTPUT_OFF_REASON`
  (load §), `TX_PORT_MODE`/`RX_PORT_MODE` (ports §), `REMOTE_COMMAND`/
  `NETWORK_INFO`/`NETWORK_MODE`/`LINK_DEVICE_STATE` (networking §),
  `DEVICE_FUNCTION` (Orion), `TIMER_ANCHOR` (lighting §),
   `BAT_TYPE_SMALL/LARGE/ORION/RS` + `decode_bat_type()` (product-specific
   battery-type tables; limitation: no small-charger flavor exists, so the
   generic `BAT_TYPE` register binds the large table — value 1 on 10/15/20A
   models reads `type_1` via `hex_get` instead of the documented `type_2`,
   use `decode_bat_type(v, charger="small")` there),
  `CAPABILITIES` full bits 9-15/18-20/25-26 (BlueSolar capabilities §),
  Orion error/warning additions (120, 150/151, 160-162),
  Phoenix `0x0207` off-reason bits 0/2/3/4/5 (HEX Rev 5 §2.3.2).
  Phoenix BLE capabilities are `0x0150` (product-info table, same ID as BMV);
  the §2.2.5 heading `0x0160` is a Victron typo and is not a second register.
- Per-flavor enum overrides (same wire ID, family-documented names; generic
  entries unchanged): `RELAY_MODE_BMV` (BMV Rev 7 `0x034F` 0..2 DFLT/CHRG/REM),
  `RELAY_MODE_INV` (Phoenix Rev 5 §2.8.1 `0x034F` values 0,2,3,4,5,6 — no 1),
  `DEVICE_STATE_ORION`/`DEVICE_MODE_ORION` (Orion-XS-HEX `0x0201` 0 NOT_CHARGING /
  11 PSU / 245 WAKE-UP / 255 UNAVAILABLE and `0x0200` 0/4 off, 1/3 on),
  `DEVICE_STATE_INV`/`DEVICE_MODE_INV` (Phoenix Rev 5 §2.3.1 states 0,1,2,9 and
  §2.4.2 modes 2,3,4,5,0xFD), `WARNING_REASON_INV` (Phoenix Rev 5 §2.3.3 bits
  0,1,5,6,8,9,10,11). Selected by flavor-qualified lookup; see `HEX_REGISTERS.md`.
- Doc errata captured in the DB: BlueSolar-HEX `0x200D` is typed un8 but lists
  bits 0..8 — bit 8 STANDBY is undecodable on the wire, so the map keeps bits
  0..7 and the dtype stays uint8_t; BlueSolar-HEX off-reason bits 7..8 are
  Reserved (no "engine" bit — that name is Orion-only) with bit 9 battery-temp-low;
  `BAT_VOLTAGE_SETTING` keeps BlueSolar values 0 (auto) and 36 although the
  Orion-XS-HEX table lists 12/24/48 only; charger error 19 (polarity reversed,
  disabled since fw 1.15) decodes instead of `unknown(19)`; `0xEDF5`
  (storage voltage) is absent from the BlueSolar-HEX Rev 18 battery table and
  from every other snapshot — kept as a krahabb-sourced CHG setting with this
  provenance note rather than silently dropped.
- Sentinels→`None` (encode back): `0xEDEC`/`0x2031`/`0x2002` = `0xFFFF`,
  `0x2003` = `0x7FFF`, `0xD18D` = `0x7FFFFFFF`, `0xEDE6` = `0xFFFF` (=use max),
  `0xEC41` = `0xFFFFFFFF`, history/cycle
  `0xFF`/`0xFFFF`/`0xFFFFFFFF` per-field.
- Flavor hierarchy from `ve_reg_flavor.h`
  (`MPPT->CHG`, `MPPT_RS->MPPT`, `MULTI_RS->INV+MPPT_RS`, `BMV71->BMV`;
  `ORION->CHG` extension is ours): `for_flavor()` in
  `src/ve_direct/registers/__init__.py`.

## Resolved Specifications & Implemented Features

The following items from the independent audit and official specifications have been fully implemented and verified:

- Response `0x2` (`CMD_CHECKSUM_ERROR`) and framing error `0xAAAA` are handled in `device.py` and immediately raise `ChecksumError`.
- Orion capabilities bits 28-31 (`hibernate`, `ac_apparent_power`, `psu_function`, `needs_battery_to_shutdown`) are in `CAPABILITIES_MAP`.
- BLE capabilities bits 0-8 (`ble_mode`, `permanent_off`, `ble_off_recovery_time`, `recovery_time_zero_permanent`, `service_mode`, `trends`, `ad_key`, `soc_sync_via_trend`, `settings_lock`) are in `CAPABILITIES_BLE_MAP`.
- `DEVICE_STATE` 250 `BLOCKED` is mapped in `DEVICE_STATE_MAP` (and the Orion
  `0x0201` override). It is deliberately absent from `LINK_DEVICE_STATE_MAP`:
  both the BlueSolar-HEX and Orion-XS-HEX Link-device-state (`0x200C`)
  tables list 0,2,3,4,5,6,7,11,245,246,247,248,249,252,255 with no 250
  (250 `BLOCKED` exists only in the `0x0201` tables), so 250 decodes
  `unknown(250)` there.
- `CLEAR_HISTORY 0x1030` and VE.Smart networking registers `0x2000..0x2015` are accessible under the `ORION` flavor.
- Serial: `from_port()` configures `dtr=True, rts=False` for VE.Direct cable power.
- Device: `device.boot()` wrapper sends `:0<51FA*5>`.
- Product ID: 2-byte command replies are accepted for BMV/BMV71/INV and for
  MPPT (legacy pre-1.30 path: BlueSolar-HEX §1.1 Note 1 says product info is
  2-byte since fw 1.12 and 4-byte only since fw 1.30, and §1.3 shows 2-byte
  Done replies, so pre-1.30 MPPTs can never return the 4-byte form);
  ORION/MULTI_RS/MPPT_RS require the standardized 4-byte representation
  (Orion-XS §1.1 carries the same "should be ignored" note for the 2-byte
  form). Unflavored 2-byte replies are rejected as unsafe without a flavor
  (`src/ve_direct/device.py:get_product_id`).
- Firmware Version: 24-bit Orion firmware version is decoded via `decode_app_version_24` and register `0x0102`.
- Phoenix-Inverter-HEX Rev 5 and BMV-7xx-HEX Rev 7 are independently parsed and 100% verified via `tools/spec_parser.py` and `tests/test_spec_parsers.py`.
- Shared-wire parsing is byte-state based and tested for bytewise reads,
  coalesced Text+HEX, mid-record Async insertion, corrupt blocks and recovery.
- HEX transactions are serialized. Timeout/cancellation intentionally marks
  routing unsynchronized, because the protocol has no transaction identifier
  with which to distinguish a late response from a new one.
- Phoenix R/W access, the `0xED2E` re-bulk ID, product-specific off reasons,
  battery types, BLE mode, firmware-dependent MPPT yield width, and product /
  version structures are represented in the register metadata.

Automated conformance can verify framing and public tables, but timing and
firmware-specific behavior still require physical devices from each family.
The project has no claim of verification for private bootloader transfer
commands, VE.Can, or undocumented Multi RS values.
