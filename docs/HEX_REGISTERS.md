# HEX registers — hex_get / hex_set reference

Source: Victron official docs — BlueSolar-HEX Rev 18, Orion-XS-HEX,
Phoenix-Inverter-HEX Rev 5 and BMV-7xx-HEX Rev 7, with Text 3.34 for
product hints. The database contains 429 names / 394 wire IDs; 14 Multi RS
entries are observational and separately exposed as
`EXPERIMENTAL_MULTI_RS_REGISTERS`. Unlisted IDs remain available through
`hex_get_raw()` / `hex_set_raw()`.
The tables below are a compact overview; range-generated history/tracker names
and some product aliases are omitted. `REGISTERS` is the authoritative list.

Same-ID aliases: For registers sharing wire IDs with device-specific names, scales, or data types (e.g. 0x0320/0x0321 INV alarm 0.01V vs Orion lockout 0.1V; 0xEDBB/0xEDBD/0xEDBC/0xED8D/0xED8F panel vs Orion in/out; 0xED8E BMV DC power int16 1W vs Orion output power uint32 0.01W; 0x2211/0x2212 INV range vs MPPT_RS adjustable; 0x0350-0x0353 MPPT relay battery 0.01V vs BMV relay 0.1V), lookup by name (lookup(name)) is exact. Calling lookup(int) on an aliased ID requires specifying flavor (e.g. lookup(0xED8E, flavor="ORION")), and raises AmbiguousRegisterError if flavor is omitted.

Per-flavor enum overrides share a wire ID with the generic entry but decode with family-documented value names: 0x0200/0x0201 (`ORION_DEVICE_MODE/STATE`, `INV_DEVICE_MODE/STATE` vs generic `DEVICE_MODE/STATE`), 0x031C/0x031E (`INV_WARNING/ALARM_REASON` vs generic, Phoenix Rev 5 §2.3.3 bits only), 0x034F (`BMV_RELAY_MODE` 0..2 DFLT/CHRG/REM, `INV_RELAY_MODE` without value 1; no generic entry — no official doc gives 0x034F to MPPT). A flavor-qualified lookup selects the override; no other family's decode changes.

Use `flavor="MPPT_SMALL"` for 10A/15A/20A MPPT chargers without a rotary switch. Their typed `BAT_TYPE` reads and writes use the small table, where raw 1 means `type_2`. Generic `MPPT` keeps the large-model table. Phoenix `INV` selects its own `APP_VER` decoder for register `0x0102`.

Flavor tags are deliberately liberal (a superset of strict doc provenance): e.g. pluggable-display 0x0401-0x0404 (BlueSolar-HEX: LCD models only), per-tracker history 0x10A0-0x10BE (BlueSolar-HEX Note 2: multi-tracker/MPPT RS only, also listed under MPPT), and generic ANY control/status IDs visible in the BMV view though absent from BMV-7xx-HEX. They are not narrowed because that would break `for_flavor()` consumers with no wire-behavior contradiction; strict provenance is tracked in `SOURCES.md`. Exceptions: 0x0400 collides (BlueSolar-HEX Display backlight mode ENUM vs BMV Rev 7 Backlight Always On BOOLEAN), so `DISPLAY_BACKLIGHT_MODE` is scoped to BlueSolar/SmartSolar MPPT + MPPT RS and only `BMV_BACKLIGHT_ALWAYS_ON` is visible to BMV/BMV71; 0x0350-0x0353 collide (BlueSolar-HEX relay battery 0.01V vs BMV Rev 7 relay 0.1V), so generic `RELAY_BAT_*` is scoped to BlueSolar/SmartSolar MPPT + MPPT RS and only `BMV_RELAY_*` is visible to BMV/BMV71.

STRUCT registers decode to dict (history, timer events, voltage range, load control, network status); encode accepts the same dict (histories are read-only — pass raw bytes to encode). Sentinels decode as None: BAT_TEMPERATURE/TIME_OF_DAY/BATTERY_VOLTAGE_SENSE 0xFFFF, BATTERY_TEMPERATURE_SENSE 0x7FFF, ORION_BATTERY_VOLTAGE 0x7FFFFFFF, LOW_TEMP_CHARGE_CURRENT 0xFFFF (=use max), SETTINGS_CHANGED_TIMESTAMP 0xEC41 0xFFFFFFFF (=Invalid/no settings ever modified, BMV Rev 7 p8), BMV alarm/relay temperature 0x032A-0x032D/0x035A-0x035D 0x0000 (=disabled, valid 174..372 K); history 0xFF/0xFFFF/0xFFFFFFFF per-field. BAT_VOLTAGE 0xEDEF raw 0 decodes as None (=Auto per the shared Battery-voltage-setting values table with 0xEDEA; readback normally returns the operational voltage). BAT_TYPE tables differ per charger (see decode_bat_type(charger="small"|"large"|"rs")); generic MPPT binds BAT_TYPE_LARGE and MPPT_SMALL selects BAT_TYPE_SMALL, while ENUM_MAPS["BAT_TYPE"] ({0xFF:user}) is only a back-compat stub bound to no register. YIELD_TODAY/YIELD_YESTERDAY are un32 on fw<=1.12, un16 above (BlueSolar-HEX Note 4); the DB accepts both reply widths.

Framing: ":<cmd><hex><check>\n", `(cmd+sum(data)+check)&0xFF==0x55`. Commands: ping `:154`, app-version `:352` (decode via decode_app_version), product-id `:451`, restart `:64F` (no reply), get `:7<id>00`, set `:8<id>00<val>` (e.g. `:8F0ED0064000C`), boot `:0<51FA*5>`. Get/Set register replies carry a flags byte (nonzero = `RegisterError`); command Done replies carry their command-specific payload; Async `:A` queued in `dev.async_frames`. `decode_line(..., strict=True)` raises `ChecksumError`; streaming defaults to `valid=False`.

`decode_line()` accepts uppercase or lowercase HEX digits and an optional LF
or CRLF terminator; a complete unterminated frame is also accepted. Signs,
spaces, tabs, extra terminators, and other characters raise `FrameError` in
both decode modes. Leading or trailing whitespace is not stripped.

## Device families (Flavors, hierarchy: MPPT->CHG, MPPT_SMALL/MPPT_RS->MPPT, MULTI_RS->INV+MPPT_RS, BMV71->BMV, ORION->CHG)

| Flavor | Devices | Text labels hint |
|---|---|---|
| ANY | All devices | PID, FW, SER# |
| MPPT | BlueSolar / SmartSolar MPPT solar chargers (incl. CHG battery/charger regs) | V, I, VPV, PPV, CS, MPPT, H19..H23, HSDS |
| MPPT_SMALL | 10A/15A/20A MPPT without rotary switch (incl. MPPT) | Small-model BAT_TYPE table |
| INV | Phoenix / Smart inverters | AC_OUT_V/I/S, MODE, WARN |
| CHG | Phoenix / Smart chargers, Smart BuckBoost | V/I per channel, MODE |
| ORION | Orion XS DC-DC (incl. CHG) | V, I, P, DC_IN_V/I/P, CS, OR, ERR, MODE |
| BMV | BMV-700/702/712, SmartShunt | V, VS, I, SOC, TTG, CE, H1..H18, MON |
| BMV71 | BMV-712/SmartShunt 2nd-gen | DC_MONITOR_MODE (also visible in BMV) |
| MPPT_RS | MPPT RS (incl. MPPT+CHG) | per-tracker PANEL_*_N |
| MULTI_RS | Multi RS / Inverter RS | U_AC_OUT_* |

In code: from ve_direct.registers import for_flavor; for_flavor("BMV").

`BMV_DESCRIPTION` is String20, including its terminating NUL. Typed writes accept up to 19 ASCII characters and reject longer inputs rather than exceeding the wire width.

Flavor names are case-insensitive. Enum labels are case-sensitive; unknown
load-control labels raise `ValueError` before a write. Typed scalar decoders
raise `RegisterError` when numeric, boolean, enum, or bitmask bytes are
incomplete. YIELD_TODAY/YIELD_YESTERDAY require either two or four bytes.
Empty VOID replies remain valid, and raw APIs retain malformed value bytes.
Flagged Async notifications expose their original bytes in `value` with the
device's `flags`, without manufacturing a typed reading.

Structured numeric encoders reject fractional numbers and values outside
their wire widths with `ValueError`. Dictionary modes occupy four bits;
timer anchors, dim levels, and voltage-range fields occupy eight bits;
timer offsets are signed 16-bit integers. Packed numeric words retain all
bits within their unsigned 8-, 16-, or 32-bit width. Structured one-bit flags
accept booleans or numeric 0/1 and reject strings. These bounds enforce the
wire representation without adding model-specific setting limits. Raw byte
APIs remain available for explicit encodings.

`dev.async_frames` holds 256 notifications by default. Set `async_queue_size`
on the constructor or either factory to change the positive integer limit.
Overflow discards the oldest queued notification and increments
`dev.dropped_async_frames`; below capacity notifications retain arrival order.
HEX reply routing continues even if notifications overflow. Firmware helper
`decode_app_version_24()` interprets an integer as a complete unsigned
four-byte register word, including the identifier byte and zero padding.

## hex_get — all readable registers (READ_ONLY + READ_WRITE + CONSTANT)

| Name | ID | Class | Access | Type | Unit | Scale | Devices | Enum |
|---|---|---|---|---|---|---|---|---|
| BLE_MODE | 0x0090 | ENUM | READ_WRITE | uint8_t | — | 1.0 | All devices | BLE_MODE |
| PRODUCT_ID | 0x0100 | STRUCT | CONSTANT | uint32_t | — | 1.0 | All devices | PRODUCT_ID |
| PRODUCT_REVISION | 0x0101 | STRUCT | READ_ONLY | uint24_t | — | 1.0 | All devices | PRODUCT_REVISION |
| APP_VER | 0x0102 | STRUCT | CONSTANT | uint32_t | — | 1.0 | Generic, Orion | APP_VERSION |
| INV_APP_VER | 0x0102 | STRUCT | CONSTANT | uint32_t | — | 1.0 | Phoenix inverter | APP_VERSION_INV |
| GROUP_ID | 0x0104 | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | All devices | — |
| SERIAL_NUMBER | 0x010A | STRING | CONSTANT | — | — | 1.0 | All devices | — |
| MODEL_NAME | 0x010B | STRING | CONSTANT | — | — | 1.0 | All devices | — |
| BMV_DESCRIPTION | 0x010C | STRING | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| CAPABILITIES | 0x0140 | BITMASK | CONSTANT | uint32_t | — | 1.0 | All devices | CAPABILITIES |
| CAPABILITIES_BLE | 0x0150 | BITMASK | READ_ONLY | uint32_t | — | 1.0 | All devices | CAPABILITIES_BLE |
| DEVICE_MODE | 0x0200 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DEVICE_MODE |
| DEVICE_STATE | 0x0201 | ENUM | READ_ONLY | — | — | 1.0 | All devices | DEVICE_STATE |
| REMOTE_CONTROL_USED | 0x0202 | BITMASK | READ_WRITE | uint32_t | — | 1.0 | All devices | REMOTE_CONTROL_USED |
| DEVICE_OFF_REASON | 0x0205 | BITMASK | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT | OFF_REASON_MPPT |
| DEVICE_FUNCTION | 0x0206 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | DEVICE_FUNCTION |
| DEVICE_OFF_REASON_2 | 0x0207 | BITMASK | READ_ONLY | uint32_t | — | 1.0 | BlueSolar/SmartSolar MPPT | OFF_REASON_MPPT |
| INV_DEVICE_OFF_REASON | 0x0207 | BITMASK | READ_ONLY | uint32_t | — | 1.0 | Phoenix/Smart inverters | OFF_REASON_INV |
| AC_OUT_VOLTAGE_SETPOINT_MIN | 0x0231 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| AC_OUT_VOLTAGE_SETPOINT_MAX | 0x0232 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| MPPT_TRACKERS | 0x0244 | NUMERIC | CONSTANT | uint8_t | — | 1.0 | MPPT RS | — |
| BMV_DEEPEST_DISCHARGE | 0x0300 | NUMERIC | READ_ONLY | int32_t | Ah | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_DEPTH_LAST_DISCHARGE | 0x0301 | NUMERIC | READ_ONLY | int32_t | Ah | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_DEPTH_AVERAGE_DISCHARGE | 0x0302 | NUMERIC | READ_ONLY | int32_t | Ah | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_NUMBER_CYCLES | 0x0303 | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_NUMBER_FULL_DISCHARGES | 0x0304 | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CUMULATIVE_AMP_HOURS | 0x0305 | NUMERIC | READ_ONLY | int32_t | Ah | 0.1 | BMV-700/702/712, SmartShunt | — |
| UNKNOWN_0305 | 0x0305 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| BMV_MINIMUM_VOLTAGE | 0x0306 | NUMERIC | READ_ONLY | int32_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_MAXIMUM_VOLTAGE | 0x0307 | NUMERIC | READ_ONLY | int32_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_SECONDS_SINCE_FULL_CHARGE | 0x0308 | NUMERIC | READ_ONLY | uint32_t | s | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_NUMBER_AUTOMATIC_SYNCS | 0x0309 | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_NUMBER_LOW_VOLTAGE_ALARMS | 0x030A | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_NUMBER_HIGH_VOLTAGE_ALARMS | 0x030B | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_MINIMUM_STARTER_VOLTAGE | 0x030E | NUMERIC | READ_ONLY | int32_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_MAXIMUM_STARTER_VOLTAGE | 0x030F | NUMERIC | READ_ONLY | int32_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_AMOUNT_DISCHARGED_ENERGY | 0x0310 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BMV-700/702/712, SmartShunt | — |
| U_OUTPUT_YIELD | 0x0310 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| BMV_AMOUNT_CHARGED_ENERGY | 0x0311 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BMV-700/702/712, SmartShunt | — |
| U_USER_YIELD | 0x0311 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| WARNING_REASON | 0x031C | BITMASK | READ_ONLY | uint16_t | — | 1.0 | All devices | WARNING_REASON |
| ALARM_REASON | 0x031E | BITMASK | READ_ONLY | uint16_t | — | 1.0 | All devices | WARNING_REASON |
| BMV_ALARM_ACKNOWLEDGE | 0x031F | VOID | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_VOLTAGE | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| INPUT_VOLTAGE_LOCKOUT | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Orion XS | — |
| BMV_ALARM_LOW_VOLTAGE_CLEAR | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| INPUT_VOLTAGE_RESTART | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Orion XS | — |
| BMV_ALARM_HIGH_VOLTAGE | 0x0322 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_VOLTAGE_CLEAR | 0x0323 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_STARTER | 0x0324 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_STARTER_CLEAR | 0x0325 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_STARTER | 0x0326 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_STARTER_CLEAR | 0x0327 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_SOC | 0x0328 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_SOC_CLEAR | 0x0329 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_TEMPERATURE | 0x032A | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_TEMPERATURE_CLEAR | 0x032B | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_TEMPERATURE | 0x032C | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_TEMPERATURE_CLEAR | 0x032D | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_MID_VOLTAGE | 0x0331 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_MID_VOLTAGE_CLEAR | 0x0332 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_INVERT | 0x034D | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| RELAY_CONTROL | 0x034E | BOOLEAN | READ_WRITE | — | — | 1.0 | All devices | — |
| BMV_RELAY_MODE | 0x034F | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | RELAY_MODE_BMV |
| BMV_RELAY_LOW_VOLTAGE | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_LOW_SET | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_LOW_VOLTAGE_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_LOW_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_HIGH_VOLTAGE | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_HIGH_SET | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_HIGH_VOLTAGE_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_HIGH_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_LOW_STARTER | 0x0354 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_STARTER_CLEAR | 0x0355 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_STARTER | 0x0356 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_STARTER_CLEAR | 0x0357 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_TEMPERATURE | 0x035A | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_TEMPERATURE_CLEAR | 0x035B | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_TEMPERATURE | 0x035C | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_TEMPERATURE_CLEAR | 0x035D | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_MID_VOLTAGE | 0x0361 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_MID_VOLTAGE_CLEAR | 0x0362 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_MIDPOINT_VOLTAGE | 0x0382 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_MIDPOINT_DEVIATION | 0x0383 | NUMERIC | READ_ONLY | int16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_BACKLIGHT_ALWAYS_ON | 0x0400 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| DISPLAY_BACKLIGHT_MODE | 0x0400 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | DISPLAY_BACKLIGHT_MODE |
| DISPLAY_BACKLIGHT_INTENSITY | 0x0401 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_BACKLIGHT_INTENSITY |
| DISPLAY_SCROLL_SPEED | 0x0402 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_SCROLL_SPEED |
| DISPLAY_SETUP_LOCK | 0x0403 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_SETUP_LOCK |
| DISPLAY_TEMPERATURE_UNIT | 0x0404 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_TEMPERATURE_UNIT |
| DISPLAY_CONTRAST | 0x0406 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | MPPT RS | — |
| DISPLAY_BACKLIGHT_MODE_RS | 0x0408 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | DISPLAY_BACKLIGHT_MODE_RS |
| UNKNOWN_0FFC | 0x0FFC | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | Multi RS / Inverter RS | — |
| BMV_START_SYNCHRONIZED | 0x0FFD | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| TTG | 0x0FFE | NUMERIC | READ_ONLY | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| SOC | 0x0FFF | NUMERIC | READ_ONLY | uint16_t | % | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_BATTERY_CAPACITY | 0x1000 | NUMERIC | READ_WRITE | uint16_t | Ah | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGED_VOLTAGE | 0x1001 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_TAIL_CURRENT | 0x1002 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGED_DETECTION_TIME | 0x1003 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGE_EFFICIENCY | 0x1004 | NUMERIC | READ_WRITE | uint16_t | % | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_PEUKERT_COEFFICIENT | 0x1005 | NUMERIC | READ_WRITE | uint16_t | — | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_CURRENT_THRESHOLD | 0x1006 | NUMERIC | READ_WRITE | uint16_t | A | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_TTG_DELTA_T | 0x1007 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_DISCHARGE_FLOOR | 0x1008 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_SOC_CLEAR | 0x1009 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_MIN_ENABLED_TIME | 0x100A | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | All devices | — |
| BMV_RELAY_DISABLE_TIME | 0x100B | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_USER_CURRENT_ZERO | 0x1034 | NUMERIC | READ_ONLY | int16_t | ADC count | 1.0 | BMV-700/702/712, SmartShunt | — |
| INV_HISTORY_TIME | 0x1040 | NUMERIC | READ_ONLY | uint32_t | s | 1.0 | Phoenix/Smart inverters | — |
| INV_HISTORY_ENERGY | 0x1041 | NUMERIC | READ_ONLY | uint32_t | kVAh | 0.01 | Phoenix/Smart inverters | — |
| CUMULATIVE_SERVICE_HISTORY | 0x1042 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CUMULATIVE_HISTORY |
| CUMULATIVE_USER_HISTORY | 0x1043 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CUMULATIVE_HISTORY |
| TOTAL_HISTORY | 0x104F | STRUCT | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | HISTORY_TOTAL |
| CYCLE_COUNT | 0x106F | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Orion XS | — |
| CYCLE_SEQUENCE_NUMBER | 0x1099 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CYCLE_SEQUENCE |
| CHARGE_ALGORITHM_VERSION | 0x2000 | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_VOLTAGE_SETPOINT | 0x2001 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_VOLTAGE_SENSE | 0x2002 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_TEMPERATURE_SENSE | 0x2003 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_STATE_ELAPSED | 0x2007 | NUMERIC | READ_WRITE | uint32_t | ms | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ABSORPTION_TIME | 0x2008 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| NETWORK_ERROR_CODE | 0x2009 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | CHR_ERROR_CODE |
| BATTERY_CHARGE_CURRENT | 0x200A | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_IDLE_VOLTAGE | 0x200B | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LINK_DEVICE_STATE | 0x200C | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | LINK_DEVICE_STATE |
| NETWORK_INFO | 0x200D | BITMASK | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | NETWORK_INFO |
| NETWORK_MODE | 0x200E | BITMASK | READ_WRITE | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | NETWORK_MODE |
| NETWORK_STATUS | 0x200F | STRUCT | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | NETWORK_STATUS |
| TOTAL_CHARGE_CURRENT | 0x2013 | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_CURRENT_PERCENT | 0x2014 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGE_CURRENT_LIMIT | 0x2015 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| MANUAL_EQUALISE_PENDING | 0x2018 | BOOLEAN | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TOTAL_DC_INPUT_POWER | 0x2027 | NUMERIC | READ_WRITE | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| SOLAR_ACTIVITY | 0x2030 | BOOLEAN | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | — |
| TIME_OF_DAY | 0x2031 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| AC_OUT_VOLTAGE | 0x2200 | NUMERIC | READ_ONLY | int16_t | V | 0.01 | Phoenix/Smart inverters | — |
| AC_OUT_CURRENT | 0x2201 | NUMERIC | READ_ONLY | int16_t | A | 0.1 | Phoenix/Smart inverters | — |
| AC_OUT_NOM_VOLTAGE | 0x2202 | NUMERIC | READ_ONLY | uint8_t | V | 1.0 | Phoenix/Smart inverters | — |
| AC_OUT_RATED_POWER | 0x2203 | NUMERIC | READ_ONLY | int16_t | VA | 1.0 | Phoenix/Smart inverters | — |
| AC_OUT_APPARENT_POWER | 0x2205 | NUMERIC | READ_ONLY | int32_t | VA | 1.0 | Phoenix/Smart inverters | — |
| ADJUSTABLE_VOLTAGE_MIN | 0x2211 | NUMERIC | CONSTANT | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| VOLTAGE_RANGE_MIN | 0x2211 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| ADJUSTABLE_VOLTAGE_MAX | 0x2212 | NUMERIC | CONSTANT | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| VOLTAGE_RANGE_MAX | 0x2212 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| U_AC_OUT_VOLTAGE | 0x2213 | NUMERIC | READ_ONLY | int16_t | V | 0.01 | Multi RS / Inverter RS | — |
| U_AC_OUT_CURRENT | 0x2214 | NUMERIC | READ_ONLY | int16_t | A | 0.01 | Multi RS / Inverter RS | — |
| U_AC_OUT_REAL_POWER | 0x2215 | NUMERIC | READ_ONLY | int32_t | W | 1.0 | Multi RS / Inverter RS | — |
| U_AC_OUT_APPARENT_POWER | 0x2216 | NUMERIC | READ_ONLY | int32_t | VA | 1.0 | Multi RS / Inverter RS | — |
| UNKNOWN_2250 | 0x2250 | NUMERIC | READ_ONLY | int16_t | — | 1.0 | Multi RS / Inverter RS | — |
| UNKNOWN_2251 | 0x2251 | NUMERIC | READ_ONLY | int16_t | — | 1.0 | Multi RS / Inverter RS | — |
| TWO_WIRE_BMS_INPUT_STATE | 0xD01F | BITMASK | READ_ONLY | uint8_t | — | 1.0 | MPPT RS | TWO_WIRE_BMS |
| REMOTE_INPUT_MODE_CONFIG | 0xD0C0 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | REMOTE_INPUT_MODE_CONFIG |
| ORION_BATTERY_VOLTAGE | 0xD18D | NUMERIC | READ_ONLY | int32_t | V | 0.001 | Orion XS | — |
| U_AC_OUT_CURRENT_MA | 0xD3A1 | NUMERIC | READ_ONLY | int32_t | A | 0.001 | Multi RS / Inverter RS | — |
| UNKNOWN_D5C8 | 0xD5C8 | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | Multi RS / Inverter RS | — |
| UNKNOWN_D5CA | 0xD5CA | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | Multi RS / Inverter RS | — |
| UNKNOWN_D5CB | 0xD5CB | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | Multi RS / Inverter RS | — |
| INV_LOOP_GET_IINV | 0xEB4E | NUMERIC | READ_ONLY | int16_t | A | 0.001 | Phoenix/Smart inverters | — |
| INV_DYN_CUTOFF_VOLTAGE | 0xEBB1 | NUMERIC | READ_ONLY | uint16_t | V | 0.001 | Phoenix/Smart inverters | — |
| SETTINGS_CHANGED_TIMESTAMP | 0xEC41 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | All devices | — |
| MPPT_TRACKER_MODE_1 | 0xECC3 | ENUM | READ_ONLY | — | — | 1.0 | MPPT RS | MPPT_TRACKER_MODE |
| PANEL_VOLTAGE_1 | 0xECCB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| PANEL_POWER_1 | 0xECCC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | MPPT RS | — |
| PANEL_CURRENT_1 | 0xECCD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | MPPT RS | — |
| MPPT_TRACKER_MODE_2 | 0xECD3 | ENUM | READ_ONLY | — | — | 1.0 | MPPT RS | MPPT_TRACKER_MODE |
| PANEL_VOLTAGE_2 | 0xECDB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| PANEL_POWER_2 | 0xECDC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | MPPT RS | — |
| PANEL_CURRENT_2 | 0xECDD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | MPPT RS | — |
| MPPT_TRACKER_MODE_3 | 0xECE3 | ENUM | READ_ONLY | — | — | 1.0 | MPPT RS | MPPT_TRACKER_MODE |
| PANEL_VOLTAGE_3 | 0xECEB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| PANEL_POWER_3 | 0xECEC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | MPPT RS | — |
| PANEL_CURRENT_3 | 0xECED | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | MPPT RS | — |
| MPPT_TRACKER_MODE_4 | 0xECF3 | ENUM | READ_ONLY | — | — | 1.0 | MPPT RS | MPPT_TRACKER_MODE |
| PANEL_VOLTAGE_4 | 0xECFB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| PANEL_POWER_4 | 0xECFC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | MPPT RS | — |
| PANEL_CURRENT_4 | 0xECFD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | MPPT RS | — |
| REBULK_VOLTAGE_OFFSET | 0xED2E | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_AUX_VOLTAGE | 0xED7D | NUMERIC | READ_ONLY | int16_t | V | 0.01 | BMV-700/702/712, SmartShunt | — |
| BATTERY_RIPPLE_VOLTAGE | 0xED8B | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| BMV_CURRENT_PRECISE | 0xED8C | NUMERIC | READ_ONLY | int32_t | A | 0.001 | BMV-700/702/712, SmartShunt | — |
| DC_CHANNEL1_VOLTAGE | 0xED8D | NUMERIC | READ_ONLY | int16_t | V | 0.01 | BMV-700/702/712, SmartShunt, Phoenix/Smart inverters, MPPT RS | — |
| OUTPUT_VOLTAGE | 0xED8D | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Orion XS | — |
| DC_CHANNEL1_POWER | 0xED8E | NUMERIC | READ_ONLY | int16_t | W | 1.0 | BMV-700/702/712, SmartShunt | — |
| OUTPUT_POWER | 0xED8E | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | Orion XS | — |
| DC_CHANNEL1_CURRENT | 0xED8F | NUMERIC | READ_ONLY | int16_t | A | 0.1 | BMV-700/702/712, SmartShunt, MPPT RS | — |
| OUTPUT_CURRENT | 0xED8F | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Orion XS | — |
| LOAD_AES_TIMER | 0xED90 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_OUTPUT_OFF_REASON | 0xED91 | BITMASK | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT | LOAD_OUTPUT_OFF_REASON |
| SUNSET_DELAY | 0xED96 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| SUNRISE_DELAY | 0xED97 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| RX_PORT_MODE | 0xED98 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | RX_PORT_MODE |
| PANEL_VOLTAGE_DAY | 0xED99 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| PANEL_VOLTAGE_NIGHT | 0xED9A | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| GRADUAL_DIM_SPEED | 0xED9B | NUMERIC | READ_WRITE | uint8_t | s | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_SWITCH_LOW | 0xED9C | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| LOAD_SWITCH_HIGH | 0xED9D | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| TX_PORT_MODE | 0xED9E | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | TX_PORT_MODE |
| MIDPOINT_SHIFT | 0xEDA7 | NUMERIC | READ_WRITE | int16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_OUTPUT_STATE | 0xEDA8 | BOOLEAN | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_OUTPUT_VOLTAGE | 0xEDA9 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| LOAD_OUTPUT_CONTROL | 0xEDAB | STRUCT | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | LOAD_CONTROL |
| LOAD_OFFSET_VOLTAGE | 0xEDAC | NUMERIC | READ_WRITE | uint8_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| LOAD_CURRENT | 0xEDAD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT | — |
| PANEL_INPUT_RESISTANCE | 0xEDB1 | NUMERIC | READ_ONLY | uint32_t | Ohm | 1.0 | MPPT RS | — |
| PANEL_STARTING_VOLTAGE | 0xEDB2 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| MPPT_TRACKER_MODE | 0xEDB3 | ENUM | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | MPPT_TRACKER_MODE |
| PANEL_MAXIMUM_VOLTAGE | 0xEDB8 | NUMERIC | CONSTANT | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| RELAY_PANEL_HIGH_CLEAR | 0xEDB9 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_PANEL_HIGH_SET | 0xEDBA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| INPUT_VOLTAGE | 0xEDBB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Orion XS | — |
| PANEL_VOLTAGE | 0xEDBB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| INPUT_POWER | 0xEDBC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | Orion XS | — |
| PANEL_POWER | 0xEDBC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT | — |
| INPUT_CURRENT | 0xEDBD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Orion XS | — |
| PANEL_CURRENT | 0xEDBD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT | — |
| PANEL_MAXIMUM_CURRENT | 0xEDBF | NUMERIC | CONSTANT | uint16_t | A | 0.1 | MPPT RS | — |
| EQUALISE_VOLTAGE_MAXIMUM | 0xEDC6 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| EQUALISE_CURRENT_MAXIMUM | 0xEDC7 | NUMERIC | READ_ONLY | uint8_t | % | 1.0 | MPPT RS | — |
| VOLTAGE_COMPENSATION | 0xEDCA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| STREETLIGHT_VERSION | 0xEDCC | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| HISTORY_VERSION | 0xEDCD | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| VOLTAGE_SETTINGS_RANGE | 0xEDCE | STRUCT | READ_ONLY | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | VOLTAGE_RANGE |
| MAXIMUM_POWER_YESTERDAY | 0xEDD0 | NUMERIC | READ_ONLY | uint16_t | W | 1.0 | BlueSolar/SmartSolar MPPT | — |
| YIELD_YESTERDAY | 0xEDD1 | NUMERIC | READ_ONLY | uint16_t/uint32_t (fw-dependent) | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| MAXIMUM_POWER_TODAY | 0xEDD2 | NUMERIC | READ_ONLY | uint16_t | W | 1.0 | BlueSolar/SmartSolar MPPT | — |
| YIELD_TODAY | 0xEDD3 | NUMERIC | READ_ONLY | uint16_t/uint32_t (fw-dependent) | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| ADDITIONAL_CHARGER_STATE | 0xEDD4 | BITMASK | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | ADDITIONAL_CHARGER_STATE |
| CHARGER_VOLTAGE | 0xEDD5 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGER_CURRENT | 0xEDD7 | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| RELAY_OPERATION_MODE | 0xEDD9 | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_OPERATION_MODE |
| CHR_ERROR_CODE | 0xEDDA | ENUM | READ_ONLY | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | CHR_ERROR_CODE |
| CHR_INTERNAL_TEMPERATURE | 0xEDDB | NUMERIC | READ_ONLY | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| USER_YIELD | 0xEDDC | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| SYSTEM_YIELD | 0xEDDD | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| CHARGER_MAXIMUM_CURRENT | 0xEDDF | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_LOW_TEMP_LEVEL | 0xEDE0 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| EQUALISATION_DURATION | 0xEDE3 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| EQUALISATION_CURRENT_LEVEL | 0xEDE4 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| AUTO_EQUALISE_STOP_ON_VOLTAGE | 0xEDE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LOW_TEMP_CHARGE_CURRENT | 0xEDE6 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TAIL_CURRENT | 0xEDE7 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMS_PRESENT | 0xEDE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_VOLTAGE_SETTING | 0xEDEA | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | BAT_VOLTAGE_SETTING |
| BAT_TEMPERATURE | 0xEDEC | NUMERIC | READ_ONLY | uint16_t | K | 0.01 | All devices | — |
| BAT_VOLTAGE | 0xEDEF | NUMERIC | READ_WRITE | uint8_t | V | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| INV_BAT_VOLTAGE | 0xEDEF | NUMERIC | READ_ONLY | uint8_t | V | 1.0 | Phoenix/Smart inverters | — |
| BAT_MAX_CURRENT | 0xEDF0 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_TYPE | 0xEDF1 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | BAT_TYPE_LARGE |
| BAT_TEMPERATURE_COMPENSATION | 0xEDF2 | NUMERIC | READ_WRITE | int16_t | mV/K | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_EQUALISATION_VOLTAGE | 0xEDF4 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_STORAGE_VOLTAGE | 0xEDF5 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_FLOAT_VOLTAGE | 0xEDF6 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_ABSORPTION_VOLTAGE | 0xEDF7 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_ABSORPTION_LIMIT | 0xEDFB | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_BULK_LIMIT | 0xEDFC | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| AUTOMATIC_EQUALISATION_MODE | 0xEDFD | NUMERIC | READ_WRITE | uint8_t | days | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ADAPTIVE_MODE | 0xEDFE | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERYSAFE_MODE | 0xEDFF | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ESTIMATED_BATTERY_TEMPERATURE | 0xEE12 | NUMERIC | READ_ONLY | uint16_t | K | 0.01 | Orion XS | — |
| SHUTDOWN_VOLTAGE | 0xEE36 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_VOLTAGE | 0xEE37 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| DELAYED_START_VOLTAGE | 0xEE38 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_DELAY | 0xEE39 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | Orion XS | — |
| BMV_SYNC_STATE | 0xEEB6 | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| DC_MONITOR_MODE | 0xEEB8 | ENUM | READ_WRITE | int16_t | — | 1.0 | BMV-700/702/712, SmartShunt (+BMV71) | MON |
| BMV_SHOW_VOLTAGE | 0xEEE0 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_AUXILIARY_VOLTAGE | 0xEEE1 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_MID_VOLTAGE | 0xEEE2 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_CURRENT | 0xEEE3 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_CONSUMED_AH | 0xEEE4 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_SOC | 0xEEE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_TTG | 0xEEE6 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_TEMPERATURE | 0xEEE7 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_POWER | 0xEEE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_TEMPERATURE_COEFFICIENT | 0xEEF4 | NUMERIC | READ_WRITE | uint16_t | %CAP/degC | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_SCROLL_SPEED | 0xEEF5 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SETUP_LOCK | 0xEEF6 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_TEMPERATURE_UNIT | 0xEEF7 | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | BMV_TEMPERATURE_UNIT |
| BMV_AUXILIARY_INPUT | 0xEEF8 | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | BMV_AUXILIARY_INPUT |
| BMV_SW_VERSION | 0xEEF9 | STRUCT | READ_ONLY | — | — | 1.0 | BMV-700/702/712, SmartShunt | BMV_SW_VERSION |
| BMV_SHUNT_VOLTS | 0xEEFA | NUMERIC | READ_WRITE | uint16_t | V | 0.001 | BMV-700/702/712, SmartShunt | — |
| BMV_SHUNT_AMPS | 0xEEFB | NUMERIC | READ_WRITE | uint16_t | A | 1.0 | BMV-700/702/712, SmartShunt | — |
| ALARM_BUZZER | 0xEEFC | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_BACKLIGHT_INTENSITY | 0xEEFE | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CONSUMED_AH | 0xEEFF | NUMERIC | READ_ONLY | int32_t | Ah | 0.1 | BMV-700/702/712, SmartShunt | — |
| TIMER_EVENT_0..5 | 0xEDA0..0xEDA5 | STRUCT | READ_WRITE | — | — | — | BlueSolar/SmartSolar MPPT | TIMER_EVENT |
| DAILY_HISTORY_0..30 | 0x1050..0x106E | STRUCT | READ_ONLY | — | — | — | BlueSolar/SmartSolar MPPT | HISTORY_DAY |
| MPPT_HISTORY_0..30 | 0x10A0..0x10BE | STRUCT | READ_ONLY | — | — | — | MPPT RS, BlueSolar/SmartSolar MPPT | HISTORY_MPPT_DAY |
| CYCLE_HISTORY_0..40 | 0x1070..0x1098 | STRUCT | READ_ONLY | — | — | — | Orion XS | CYCLE_HISTORY |
## hex_set — writable registers (READ_WRITE + WRITE_ONLY)

| Name | ID | Class | Access | Type | Unit | Scale | Devices | Enum |
|---|---|---|---|---|---|---|---|---|
| RESTORE_DEFAULTS | 0x0004 | VOID | WRITE_ONLY | — | — | 1.0 | All devices | — |
| BLE_MODE | 0x0090 | ENUM | READ_WRITE | uint8_t | — | 1.0 | All devices | BLE_MODE |
| BMV_DESCRIPTION | 0x010C | STRING | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| DEVICE_MODE | 0x0200 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DEVICE_MODE |
| REMOTE_CONTROL_USED | 0x0202 | BITMASK | READ_WRITE | uint32_t | — | 1.0 | All devices | REMOTE_CONTROL_USED |
| DEVICE_FUNCTION | 0x0206 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | DEVICE_FUNCTION |
| AC_OUT_VOLTAGE_SETPOINT | 0x0230 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| BMV_ALARM_ACKNOWLEDGE | 0x031F | VOID | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| ALARM_LOW_VOLTAGE_SET | 0x0320 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| BMV_ALARM_LOW_VOLTAGE | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| INPUT_VOLTAGE_LOCKOUT | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Orion XS | — |
| ALARM_LOW_VOLTAGE_CLEAR | 0x0321 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| BMV_ALARM_LOW_VOLTAGE_CLEAR | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| INPUT_VOLTAGE_RESTART | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Orion XS | — |
| BMV_ALARM_HIGH_VOLTAGE | 0x0322 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_VOLTAGE_CLEAR | 0x0323 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_STARTER | 0x0324 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_STARTER_CLEAR | 0x0325 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_STARTER | 0x0326 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_STARTER_CLEAR | 0x0327 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_SOC | 0x0328 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_SOC_CLEAR | 0x0329 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_TEMPERATURE | 0x032A | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_LOW_TEMPERATURE_CLEAR | 0x032B | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_TEMPERATURE | 0x032C | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_HIGH_TEMPERATURE_CLEAR | 0x032D | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_MID_VOLTAGE | 0x0331 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_ALARM_MID_VOLTAGE_CLEAR | 0x0332 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_INVERT | 0x034D | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| RELAY_CONTROL | 0x034E | BOOLEAN | READ_WRITE | — | — | 1.0 | All devices | — |
| BMV_RELAY_MODE | 0x034F | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | RELAY_MODE_BMV |
| INV_RELAY_MODE | 0x034F | ENUM | WRITE_ONLY | — | — | 1.0 | Phoenix/Smart inverters | RELAY_MODE_INV |
| BMV_RELAY_LOW_VOLTAGE | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_LOW_SET | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_LOW_VOLTAGE_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_LOW_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_HIGH_VOLTAGE | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_HIGH_SET | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_HIGH_VOLTAGE_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_BAT_HIGH_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMV_RELAY_LOW_STARTER | 0x0354 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_STARTER_CLEAR | 0x0355 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_STARTER | 0x0356 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_STARTER_CLEAR | 0x0357 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_TEMPERATURE | 0x035A | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_TEMPERATURE_CLEAR | 0x035B | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_TEMPERATURE | 0x035C | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_HIGH_TEMPERATURE_CLEAR | 0x035D | NUMERIC | READ_WRITE | uint16_t | K | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_MID_VOLTAGE | 0x0361 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_MID_VOLTAGE_CLEAR | 0x0362 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_BACKLIGHT_ALWAYS_ON | 0x0400 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| DISPLAY_BACKLIGHT_MODE | 0x0400 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | DISPLAY_BACKLIGHT_MODE |
| DISPLAY_BACKLIGHT_INTENSITY | 0x0401 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_BACKLIGHT_INTENSITY |
| DISPLAY_SCROLL_SPEED | 0x0402 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_SCROLL_SPEED |
| DISPLAY_SETUP_LOCK | 0x0403 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_SETUP_LOCK |
| DISPLAY_TEMPERATURE_UNIT | 0x0404 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_TEMPERATURE_UNIT |
| DISPLAY_CONTRAST | 0x0406 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | MPPT RS | — |
| DISPLAY_BACKLIGHT_MODE_RS | 0x0408 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | DISPLAY_BACKLIGHT_MODE_RS |
| BMV_START_SYNCHRONIZED | 0x0FFD | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_BATTERY_CAPACITY | 0x1000 | NUMERIC | READ_WRITE | uint16_t | Ah | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGED_VOLTAGE | 0x1001 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_TAIL_CURRENT | 0x1002 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGED_DETECTION_TIME | 0x1003 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CHARGE_EFFICIENCY | 0x1004 | NUMERIC | READ_WRITE | uint16_t | % | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_PEUKERT_COEFFICIENT | 0x1005 | NUMERIC | READ_WRITE | uint16_t | — | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_CURRENT_THRESHOLD | 0x1006 | NUMERIC | READ_WRITE | uint16_t | A | 0.01 | BMV-700/702/712, SmartShunt | — |
| BMV_TTG_DELTA_T | 0x1007 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_DISCHARGE_FLOOR | 0x1008 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_RELAY_LOW_SOC_CLEAR | 0x1009 | NUMERIC | READ_WRITE | uint16_t | % | 0.1 | BMV-700/702/712, SmartShunt | — |
| RELAY_MIN_ENABLED_TIME | 0x100A | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | All devices | — |
| BMV_RELAY_DISABLE_TIME | 0x100B | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_ZERO_CURRENT | 0x1029 | VOID | WRITE_ONLY | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SYNCHRONIZE | 0x102C | VOID | WRITE_ONLY | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_CLEAR_HISTORY | 0x1030 | VOID | WRITE_ONLY | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| CLEAR_HISTORY | 0x1030 | VOID | WRITE_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, Orion XS | — |
| CHARGE_VOLTAGE_SETPOINT | 0x2001 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_VOLTAGE_SENSE | 0x2002 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_TEMPERATURE_SENSE | 0x2003 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| REMOTE_COMMAND | 0x2004 | ENUM | WRITE_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | REMOTE_COMMAND |
| CHARGE_STATE_ELAPSED | 0x2007 | NUMERIC | READ_WRITE | uint32_t | ms | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ABSORPTION_TIME | 0x2008 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| NETWORK_ERROR_CODE | 0x2009 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | CHR_ERROR_CODE |
| BATTERY_CHARGE_CURRENT | 0x200A | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| BATTERY_IDLE_VOLTAGE | 0x200B | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LINK_DEVICE_STATE | 0x200C | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | LINK_DEVICE_STATE |
| NETWORK_MODE | 0x200E | BITMASK | READ_WRITE | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | NETWORK_MODE |
| TOTAL_CHARGE_CURRENT | 0x2013 | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_CURRENT_PERCENT | 0x2014 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGE_CURRENT_LIMIT | 0x2015 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| TOTAL_DC_INPUT_POWER | 0x2027 | NUMERIC | READ_WRITE | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TIME_OF_DAY | 0x2031 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| AC_LOAD_SENSE_POWER_THRESHOLD | 0x2206 | NUMERIC | WRITE_ONLY | uint16_t | VA | 1.0 | Phoenix/Smart inverters | — |
| AC_LOAD_SENSE_POWER_CLEAR | 0x2207 | NUMERIC | WRITE_ONLY | uint16_t | VA | 1.0 | Phoenix/Smart inverters | — |
| SHUTDOWN_LOW_VOLTAGE_SET | 0x2210 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| REMOTE_INPUT_MODE_CONFIG | 0xD0C0 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | REMOTE_INPUT_MODE_CONFIG |
| INV_WAVE_SET50HZ_NOT60HZ | 0xEB03 | ENUM | WRITE_ONLY | — | — | 1.0 | Phoenix/Smart inverters | INV_AC_FREQUENCY |
| INV_ECO_MODE_MIN_CURRENT | 0xEB04 | NUMERIC | WRITE_ONLY | int16_t | A | 0.001 | Phoenix/Smart inverters | — |
| INV_ECO_MODE_RETRY_TIME | 0xEB06 | NUMERIC | WRITE_ONLY | uint8_t | s | 0.25 | Phoenix/Smart inverters | — |
| INV_ECO_LOAD_DETECT_PERIODS | 0xEB10 | NUMERIC | WRITE_ONLY | uint8_t | — | 1.0 | Phoenix/Smart inverters | — |
| INV_NVM_COMMAND | 0xEB99 | ENUM | WRITE_ONLY | — | — | 1.0 | Phoenix/Smart inverters | INV_NVM_COMMAND |
| INV_DYN_CUTOFF_FACTOR5 | 0xEBB2 | NUMERIC | WRITE_ONLY | uint16_t | — | 1.0 | Phoenix/Smart inverters | — |
| INV_DYN_CUTOFF_FACTOR250 | 0xEBB3 | NUMERIC | WRITE_ONLY | uint16_t | — | 1.0 | Phoenix/Smart inverters | — |
| INV_DYN_CUTOFF_FACTOR2000 | 0xEBB5 | NUMERIC | WRITE_ONLY | uint16_t | — | 1.0 | Phoenix/Smart inverters | — |
| INV_DYN_CUTOFF_FACTOR | 0xEBB7 | NUMERIC | WRITE_ONLY | uint16_t | — | 1.0 | Phoenix/Smart inverters | — |
| INV_DYN_CUTOFF_ENABLE | 0xEBBA | BOOLEAN | WRITE_ONLY | — | — | 1.0 | Phoenix/Smart inverters | — |
| SETTINGS_CHANGED_TIMESTAMP | 0xEC41 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | All devices | — |
| REBULK_VOLTAGE_OFFSET | 0xED2E | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LOAD_AES_TIMER | 0xED90 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| SUNSET_DELAY | 0xED96 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| SUNRISE_DELAY | 0xED97 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| RX_PORT_MODE | 0xED98 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | RX_PORT_MODE |
| PANEL_VOLTAGE_DAY | 0xED99 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| PANEL_VOLTAGE_NIGHT | 0xED9A | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| GRADUAL_DIM_SPEED | 0xED9B | NUMERIC | READ_WRITE | uint8_t | s | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_SWITCH_LOW | 0xED9C | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| LOAD_SWITCH_HIGH | 0xED9D | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| TX_PORT_MODE | 0xED9E | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | TX_PORT_MODE |
| MIDPOINT_SHIFT | 0xEDA7 | NUMERIC | READ_WRITE | int16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| LOAD_OUTPUT_CONTROL | 0xEDAB | STRUCT | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT | LOAD_CONTROL |
| LOAD_OFFSET_VOLTAGE | 0xEDAC | NUMERIC | READ_WRITE | uint8_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| RELAY_PANEL_HIGH_CLEAR | 0xEDB9 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_PANEL_HIGH_SET | 0xEDBA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| VOLTAGE_COMPENSATION | 0xEDCA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| RELAY_OPERATION_MODE | 0xEDD9 | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_OPERATION_MODE |
| BAT_LOW_TEMP_LEVEL | 0xEDE0 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| EQUALISATION_DURATION | 0xEDE3 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| EQUALISATION_CURRENT_LEVEL | 0xEDE4 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| AUTO_EQUALISE_STOP_ON_VOLTAGE | 0xEDE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LOW_TEMP_CHARGE_CURRENT | 0xEDE6 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TAIL_CURRENT | 0xEDE7 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BMS_PRESENT | 0xEDE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_VOLTAGE_SETTING | 0xEDEA | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | BAT_VOLTAGE_SETTING |
| BAT_VOLTAGE | 0xEDEF | NUMERIC | READ_WRITE | uint8_t | V | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_MAX_CURRENT | 0xEDF0 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_TYPE | 0xEDF1 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | BAT_TYPE_LARGE |
| BAT_TEMPERATURE_COMPENSATION | 0xEDF2 | NUMERIC | READ_WRITE | int16_t | mV/K | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_EQUALISATION_VOLTAGE | 0xEDF4 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_STORAGE_VOLTAGE | 0xEDF5 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_FLOAT_VOLTAGE | 0xEDF6 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_ABSORPTION_VOLTAGE | 0xEDF7 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_ABSORPTION_LIMIT | 0xEDFB | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BAT_BULK_LIMIT | 0xEDFC | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| AUTOMATIC_EQUALISATION_MODE | 0xEDFD | NUMERIC | READ_WRITE | uint8_t | days | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ADAPTIVE_MODE | 0xEDFE | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERYSAFE_MODE | 0xEDFF | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost, BlueSolar/SmartSolar MPPT, MPPT RS | — |
| SHUTDOWN_VOLTAGE | 0xEE36 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_VOLTAGE | 0xEE37 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| DELAYED_START_VOLTAGE | 0xEE38 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_DELAY | 0xEE39 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | Orion XS | — |
| DC_MONITOR_MODE | 0xEEB8 | ENUM | READ_WRITE | int16_t | — | 1.0 | BMV-700/702/712, SmartShunt (+BMV71) | MON |
| BMV_SHOW_VOLTAGE | 0xEEE0 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_AUXILIARY_VOLTAGE | 0xEEE1 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_MID_VOLTAGE | 0xEEE2 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_CURRENT | 0xEEE3 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_CONSUMED_AH | 0xEEE4 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_SOC | 0xEEE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_TTG | 0xEEE6 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_TEMPERATURE | 0xEEE7 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SHOW_POWER | 0xEEE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_TEMPERATURE_COEFFICIENT | 0xEEF4 | NUMERIC | READ_WRITE | uint16_t | %CAP/degC | 0.1 | BMV-700/702/712, SmartShunt | — |
| BMV_SCROLL_SPEED | 0xEEF5 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_SETUP_LOCK | 0xEEF6 | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_TEMPERATURE_UNIT | 0xEEF7 | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | BMV_TEMPERATURE_UNIT |
| BMV_AUXILIARY_INPUT | 0xEEF8 | ENUM | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | BMV_AUXILIARY_INPUT |
| BMV_SHUNT_VOLTS | 0xEEFA | NUMERIC | READ_WRITE | uint16_t | V | 0.001 | BMV-700/702/712, SmartShunt | — |
| BMV_SHUNT_AMPS | 0xEEFB | NUMERIC | READ_WRITE | uint16_t | A | 1.0 | BMV-700/702/712, SmartShunt | — |
| ALARM_BUZZER | 0xEEFC | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| BMV_BACKLIGHT_INTENSITY | 0xEEFE | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| TIMER_EVENT_0..5 | 0xEDA0..0xEDA5 | STRUCT | READ_WRITE | — | — | — | BlueSolar/SmartSolar MPPT | TIMER_EVENT |
