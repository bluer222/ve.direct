# HEX registers — hex_get / hex_set reference

Source: Victron official docs — BlueSolar-HEX Rev 18, Orion-XS-HEX,
Phoenix-Inverter-HEX Rev 5 and BMV-7xx-HEX Rev 7, with Text 3.34 for
product hints. The database contains 428 names / 394 wire IDs; 14 Multi RS
entries are observational and separately exposed as
`EXPERIMENTAL_MULTI_RS_REGISTERS`. Unlisted IDs remain available through
`hex_get_raw()` / `hex_set_raw()`.
The tables below are a compact overview; range-generated history/tracker names
and some product aliases are omitted. `REGISTERS` is the authoritative list.

Same-ID aliases: For registers sharing wire IDs with device-specific names, scales, or data types (e.g. 0x0320/0x0321 INV alarm 0.01V vs Orion lockout 0.1V; 0xEDBB/0xEDBD/0xEDBC/0xED8D/0xED8F panel vs Orion in/out; 0xED8E BMV DC power int16 1W vs Orion output power uint32 0.01W; 0x2211/0x2212 INV range vs MPPT_RS adjustable), lookup by name (lookup(name)) is exact. Calling lookup(int) on an aliased ID requires specifying flavor (e.g. lookup(0xED8E, flavor="ORION")), and raises AmbiguousRegisterError if flavor is omitted.

Per-flavor enum overrides share a wire ID with the generic entry but decode with family-documented value names: 0x0200/0x0201 (`ORION_DEVICE_MODE/STATE`, `INV_DEVICE_MODE/STATE` vs generic `DEVICE_MODE/STATE`), 0x031C/0x031E (`INV_WARNING/ALARM_REASON` vs generic, Phoenix Rev 5 §2.3.3 bits only), 0x034F (`BMV_RELAY_MODE` 0..2 DFLT/CHRG/REM, `INV_RELAY_MODE` without value 1, vs the MPPT-shared superset). A flavor-qualified lookup selects the override; no other family's decode changes.

Flavor tags are deliberately liberal (a superset of strict doc provenance): e.g. pluggable-display 0x0400-0x0404 (BlueSolar-HEX: LCD models only), per-tracker history 0x10A0-0x10BE (BlueSolar-HEX Note 2: multi-tracker/MPPT RS only, also listed under MPPT), and generic ANY control/status IDs visible in the BMV view though absent from BMV-7xx-HEX. They are not narrowed because that would break `for_flavor()` consumers with no wire-behavior contradiction; strict provenance is tracked in `SOURCES.md`.

STRUCT registers decode to dict (history, timer events, voltage range, load control, network status); encode accepts the same dict (histories are read-only — pass raw bytes to encode). Sentinels decode as None: BAT_TEMPERATURE/TIME_OF_DAY/BATTERY_VOLTAGE_SENSE 0xFFFF, BATTERY_TEMPERATURE_SENSE 0x7FFF, ORION_BATTERY_VOLTAGE 0x7FFFFFFF, LOW_TEMP_CHARGE_CURRENT 0xFFFF (=use max); history 0xFF/0xFFFF/0xFFFFFFFF per-field. BAT_TYPE tables differ per charger (see decode_bat_type(charger="small"|"large"|"rs")); default BAT_TYPE map is {0xFF:user} for backward compat. YIELD_TODAY/YIELD_YESTERDAY are un32 on fw<=1.12, un16 above (BlueSolar-HEX Note 4); the DB accepts both reply widths.

Framing: ":<cmd><hex><check>\n", `(cmd+sum(data)+check)&0xFF==0x55`. Commands: ping `:154`, app-version `:352` (decode via decode_app_version), product-id `:451`, restart `:64F` (no reply), get `:7<id>00`, set `:8<id>00<val>` (e.g. `:8F0ED0064000C`), boot `:0<51FA*5>`. Replies Done/Get/Set carry flags byte (nonzero = `RegisterError`); Async `:A` queued in `dev.async_frames`. `decode_line(..., strict=True)` raises `ChecksumError`; streaming defaults to `valid=False`.

## Device families (Flavors, hierarchy: MPPT->CHG, MPPT_RS->MPPT, MULTI_RS->INV+MPPT_RS, BMV71->BMV, ORION->CHG)

| Flavor | Devices | Text labels hint |
|---|---|---|
| ANY | All devices | PID, FW, SER# |
| MPPT | BlueSolar / SmartSolar MPPT solar chargers (incl. CHG battery/charger regs) | V, I, VPV, PPV, CS, MPPT, H19..H23, HSDS |
| INV | Phoenix / Smart inverters | AC_OUT_V/I/S, MODE, WARN |
| CHG | Phoenix / Smart chargers, Smart BuckBoost | V/I per channel, MODE |
| ORION | Orion XS DC-DC (incl. CHG) | V, I, P, DC_IN_V/I/P, CS, OR, ERR, MODE |
| BMV | BMV-700/702/712, SmartShunt | V, VS, I, SOC, TTG, CE, H1..H18, MON |
| BMV71 | BMV-712/SmartShunt 2nd-gen | DC_MONITOR_MODE (also visible in BMV) |
| MPPT_RS | MPPT RS (incl. MPPT+CHG) | per-tracker PANEL_*_N |
| MULTI_RS | Multi RS / Inverter RS | U_AC_OUT_* |

In code: from ve_direct.registers import for_flavor; for_flavor("BMV").

## hex_get — all readable registers (READ_ONLY + READ_WRITE + CONSTANT)

| Name | ID | Class | Access | Type | Unit | Scale | Devices | Enum |
|---|---|---|---|---|---|---|---|---|
| BLE_MODE | 0x0090 | ENUM | READ_WRITE | uint8_t | — | 1.0 | All devices | BLE_MODE |
| PRODUCT_ID | 0x0100 | STRUCT | CONSTANT | uint32_t | — | 1.0 | All devices | PRODUCT_ID |
| APP_VER | 0x0102 | STRUCT | CONSTANT | uint32_t | — | 1.0 | All devices | APP_VERSION |
| GROUP_ID | 0x0104 | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | All devices | — |
| SERIAL_NUMBER | 0x010A | STRING | CONSTANT | — | — | 1.0 | All devices | — |
| MODEL_NAME | 0x010B | STRING | CONSTANT | — | — | 1.0 | All devices | — |
| CAPABILITIES | 0x0140 | BITMASK | CONSTANT | uint32_t | — | 1.0 | All devices | CAPABILITIES |
| CAPABILITIES_BLE | 0x0150 | BITMASK | READ_ONLY | uint32_t | — | 1.0 | All devices | CAPABILITIES_BLE |
| DEVICE_MODE | 0x0200 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DEVICE_MODE |
| DEVICE_STATE | 0x0201 | ENUM | READ_ONLY | — | — | 1.0 | All devices | DEVICE_STATE |
| REMOTE_CONTROL_USED | 0x0202 | BITMASK | READ_WRITE | uint32_t | — | 1.0 | All devices | REMOTE_CONTROL_USED |
| DEVICE_OFF_REASON | 0x0205 | BITMASK | READ_ONLY | uint8_t | — | 1.0 | All devices | DEVICE_OFF_REASON_2 |
| DEVICE_FUNCTION | 0x0206 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | DEVICE_FUNCTION |
| DEVICE_OFF_REASON_2 | 0x0207 | BITMASK | READ_ONLY | uint32_t | — | 1.0 | All devices | DEVICE_OFF_REASON_2 |
| AC_OUT_VOLTAGE_SETPOINT | 0x0230 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| MPPT_TRACKERS | 0x0244 | NUMERIC | CONSTANT | uint8_t | — | 1.0 | MPPT RS | — |
| UNKNOWN_0305 | 0x0305 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| U_OUTPUT_YIELD | 0x0310 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| U_USER_YIELD | 0x0311 | NUMERIC | READ_ONLY | uint32_t | kWh | 0.001 | Multi RS / Inverter RS | — |
| WARNING_REASON | 0x031C | BITMASK | READ_ONLY | uint16_t | — | 1.0 | All devices | WARNING_REASON |
| ALARM_REASON | 0x031E | BITMASK | READ_ONLY | uint16_t | — | 1.0 | All devices | WARNING_REASON |
| ALARM_LOW_VOLTAGE_SET | 0x0320 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| INPUT_VOLTAGE_LOCKOUT | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| ALARM_LOW_VOLTAGE_CLEAR | 0x0321 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| INPUT_VOLTAGE_RESTART | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| RELAY_CONTROL | 0x034E | BOOLEAN | READ_WRITE | — | — | 1.0 | All devices | — |
| RELAY_MODE | 0x034F | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_MODE |
| RELAY_BAT_LOW_SET | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_LOW_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_HIGH_SET | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_HIGH_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| DISPLAY_BACKLIGHT_MODE | 0x0400 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_BACKLIGHT_MODE |
| DISPLAY_BACKLIGHT_INTENSITY | 0x0401 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_SCROLL_SPEED | 0x0402 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_SETUP_LOCK | 0x0403 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_TEMPERATURE_UNIT | 0x0404 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_CONTRAST | 0x0406 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | MPPT RS | — |
| DISPLAY_BACKLIGHT_MODE_RS | 0x0408 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | DISPLAY_BACKLIGHT_MODE_RS |
| UNKNOWN_0FFC | 0x0FFC | NUMERIC | READ_ONLY | uint32_t | — | 1.0 | Multi RS / Inverter RS | — |
| TTG | 0x0FFE | NUMERIC | READ_ONLY | uint16_t | min | 1.0 | BMV-700/702/712, SmartShunt | — |
| SOC | 0x0FFF | NUMERIC | READ_ONLY | uint16_t | % | 0.01 | BMV-700/702/712, SmartShunt | — |
| RELAY_MIN_ENABLED_TIME | 0x100A | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | All devices | — |
| CUMULATIVE_SERVICE_HISTORY | 0x1042 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CUMULATIVE_HISTORY |
| CUMULATIVE_USER_HISTORY | 0x1043 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CUMULATIVE_HISTORY |
| TOTAL_HISTORY | 0x104F | STRUCT | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | HISTORY_TOTAL |
| CYCLE_COUNT | 0x106F | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Orion XS | — |
| CYCLE_SEQUENCE_NUMBER | 0x1099 | STRUCT | READ_ONLY | — | — | 1.0 | Orion XS | CYCLE_SEQUENCE |
| CHARGE_ALGORITHM_VERSION | 0x2000 | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGE_VOLTAGE_SETPOINT | 0x2001 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_VOLTAGE_SENSE | 0x2002 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_TEMPERATURE_SENSE | 0x2003 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | 0.01 °C |
| CHARGE_STATE_ELAPSED | 0x2007 | NUMERIC | READ_WRITE | uint32_t | ms | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ABSORPTION_TIME | 0x2008 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| NETWORK_ERROR_CODE | 0x2009 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | CHR_ERROR_CODE |
| BATTERY_CHARGE_CURRENT | 0x200A | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_IDLE_VOLTAGE | 0x200B | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LINK_DEVICE_STATE | 0x200C | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | LINK_DEVICE_STATE |
| NETWORK_INFO | 0x200D | BITMASK | READ_ONLY | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | NETWORK_INFO |
| NETWORK_MODE | 0x200E | BITMASK | READ_WRITE | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | NETWORK_MODE |
| NETWORK_STATUS | 0x200F | STRUCT | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | NETWORK_STATUS |
| TOTAL_CHARGE_CURRENT | 0x2013 | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_CURRENT_PERCENT | 0x2014 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGE_CURRENT_LIMIT | 0x2015 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| MANUAL_EQUALISE_PENDING | 0x2018 | BOOLEAN | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TOTAL_DC_INPUT_POWER | 0x2027 | NUMERIC | READ_WRITE | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| SOLAR_ACTIVITY | 0x2030 | BOOLEAN | READ_ONLY | — | — | 1.0 | BlueSolar/SmartSolar MPPT | — |
| TIME_OF_DAY | 0x2031 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| AC_OUT_VOLTAGE | 0x2200 | NUMERIC | READ_ONLY | int16_t | V | 0.01 | Phoenix/Smart inverters | — |
| AC_OUT_CURRENT | 0x2201 | NUMERIC | READ_ONLY | int16_t | A | 0.1 | Phoenix/Smart inverters | — |
| AC_OUT_APPARENT_POWER | 0x2205 | NUMERIC | READ_ONLY | int32_t | VA | 1.0 | Phoenix/Smart inverters | — |
| SHUTDOWN_LOW_VOLTAGE_SET | 0x2210 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| ADJUSTABLE_VOLTAGE_MIN | 0x2211 | NUMERIC | CONSTANT | uint16_t | V | 0.01 | MPPT RS | — |
| VOLTAGE_RANGE_MIN | 0x2211 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| ADJUSTABLE_VOLTAGE_MAX | 0x2212 | NUMERIC | CONSTANT | uint16_t | V | 0.01 | MPPT RS | — |
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
| BATTERY_RIPPLE_VOLTAGE | 0xED8B | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| DC_CHANNEL1_VOLTAGE | 0xED8D | NUMERIC | READ_ONLY | int16_t | V | 0.01 | All devices | — |
| OUTPUT_VOLTAGE | 0xED8D | NUMERIC | READ_ONLY | int16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| DC_CHANNEL1_POWER | 0xED8E | NUMERIC | READ_ONLY | int16_t | W | 1.0 | BMV-700/702/712, SmartShunt | — |
| OUTPUT_POWER | 0xED8E | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| DC_CHANNEL1_CURRENT | 0xED8F | NUMERIC | READ_ONLY | int16_t | A | 0.1 | All devices | — |
| OUTPUT_CURRENT | 0xED8F | NUMERIC | READ_ONLY | int16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
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
| INPUT_VOLTAGE | 0xEDBB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| PANEL_VOLTAGE | 0xEDBB | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT | — |
| INPUT_POWER | 0xEDBC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| PANEL_POWER | 0xEDBC | NUMERIC | READ_ONLY | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT | — |
| INPUT_CURRENT | 0xEDBD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| PANEL_CURRENT | 0xEDBD | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT | — |
| PANEL_MAXIMUM_CURRENT | 0xEDBF | NUMERIC | CONSTANT | uint16_t | A | 0.1 | MPPT RS | — |
| EQUALISE_VOLTAGE_MAXIMUM | 0xEDC6 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | MPPT RS | — |
| EQUALISE_CURRENT_MAXIMUM | 0xEDC7 | NUMERIC | READ_ONLY | uint8_t | % | 1.0 | MPPT RS | — |
| VOLTAGE_COMPENSATION | 0xEDCA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| STREETLIGHT_VERSION | 0xEDCC | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| HISTORY_VERSION | 0xEDCD | NUMERIC | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| VOLTAGE_SETTINGS_RANGE | 0xEDCE | STRUCT | READ_ONLY | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | VOLTAGE_RANGE |
| MAXIMUM_POWER_YESTERDAY | 0xEDD0 | NUMERIC | READ_ONLY | uint16_t | W | 1.0 | BlueSolar/SmartSolar MPPT | — |
| YIELD_YESTERDAY | 0xEDD1 | NUMERIC | READ_ONLY | uint16_t/uint32_t (fw-dependent) | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| MAXIMUM_POWER_TODAY | 0xEDD2 | NUMERIC | READ_ONLY | uint16_t | W | 1.0 | BlueSolar/SmartSolar MPPT | — |
| YIELD_TODAY | 0xEDD3 | NUMERIC | READ_ONLY | uint16_t/uint32_t (fw-dependent) | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| ADDITIONAL_CHARGER_STATE | 0xEDD4 | BITMASK | READ_ONLY | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | ADDITIONAL_CHARGER_STATE |
| CHARGER_VOLTAGE | 0xEDD5 | NUMERIC | READ_ONLY | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| CHARGER_CURRENT | 0xEDD7 | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| RELAY_OPERATION_MODE | 0xEDD9 | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_OPERATION_MODE |
| CHR_ERROR_CODE | 0xEDDA | ENUM | READ_ONLY | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | CHR_ERROR_CODE |
| CHR_INTERNAL_TEMPERATURE | 0xEDDB | NUMERIC | READ_ONLY | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| USER_YIELD | 0xEDDC | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| SYSTEM_YIELD | 0xEDDD | NUMERIC | READ_ONLY | uint32_t | kWh | 0.01 | BlueSolar/SmartSolar MPPT | — |
| CHARGER_MAXIMUM_CURRENT | 0xEDDF | NUMERIC | READ_ONLY | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_LOW_TEMP_LEVEL | 0xEDE0 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| REBULK_VOLTAGE_OFFSET | 0xED2E | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| EQUALISATION_DURATION | 0xEDE3 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| EQUALISATION_CURRENT_LEVEL | 0xEDE4 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| AUTO_EQUALISE_STOP_ON_VOLTAGE | 0xEDE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| LOW_TEMP_CHARGE_CURRENT | 0xEDE6 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| TAIL_CURRENT | 0xEDE7 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BMS_PRESENT | 0xEDE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_VOLTAGE_SETTING | 0xEDEA | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | BAT_VOLTAGE_SETTING |
| BAT_TEMPERATURE | 0xEDEC | NUMERIC | READ_ONLY | uint16_t | K | 0.01 | All devices | 0.01 Kelvin (0xFFFF = None) |
| BAT_VOLTAGE | 0xEDEF | NUMERIC | READ_WRITE | uint8_t | V | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | Force battery voltage |
| INV_BAT_VOLTAGE | 0xEDEF | NUMERIC | READ_ONLY | uint8_t | V | 1.0 | Phoenix/Smart inverters | — |
| BAT_MAX_CURRENT | 0xEDF0 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_TYPE | 0xEDF1 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | BAT_TYPE |
| BAT_TEMPERATURE_COMPENSATION | 0xEDF2 | NUMERIC | READ_WRITE | int16_t | mV/K | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_EQUALISATION_VOLTAGE | 0xEDF4 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_STORAGE_VOLTAGE | 0xEDF5 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_FLOAT_VOLTAGE | 0xEDF6 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_ABSORPTION_VOLTAGE | 0xEDF7 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_ABSORPTION_LIMIT | 0xEDFB | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_BULK_LIMIT | 0xEDFC | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| AUTOMATIC_EQUALISATION_MODE | 0xEDFD | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| ADAPTIVE_MODE | 0xEDFE | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BATTERYSAFE_MODE | 0xEDFF | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| ESTIMATED_BATTERY_TEMPERATURE | 0xEE12 | NUMERIC | READ_ONLY | uint16_t | K | 0.01 | Orion XS | 0.01 Kelvin |
| SHUTDOWN_VOLTAGE | 0xEE36 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_VOLTAGE | 0xEE37 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| DELAYED_START_VOLTAGE | 0xEE38 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_DELAY | 0xEE39 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | Orion XS | — |
| DC_MONITOR_MODE | 0xEEB8 | ENUM | READ_WRITE | int16_t | — | 1.0 | BMV-700/702/712, SmartShunt (+BMV71) | MON |
| ALARM_BUZZER | 0xEEFC | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| TIMER_EVENT_0..5 | 0xEDA0..0xEDA5 | STRUCT | READ_WRITE | — | — | — | BlueSolar/SmartSolar MPPT | TIMER_EVENT |
| DAILY_HISTORY_0..30 | 0x1050..0x106E | STRUCT | READ_ONLY | — | — | — | BlueSolar/SmartSolar MPPT | HISTORY_DAY |
| MPPT_HISTORY_0..30 | 0x10A0..0x10BE | STRUCT | READ_ONLY | — | — | — | MPPT RS, BlueSolar/SmartSolar MPPT | HISTORY_MPPT_DAY |
| CYCLE_HISTORY_0..40 | 0x1070..0x1098 | STRUCT | READ_ONLY | — | — | — | Orion XS | CYCLE_HISTORY |

## hex_set — writable registers (READ_WRITE + WRITE_ONLY)

| Name | ID | Class | Access | Type | Unit | Scale | Devices | Enum |
|---|---|---|---|---|---|---|---|---|
| BLE_MODE | 0x0090 | ENUM | READ_WRITE | uint8_t | — | 1.0 | All devices | BLE_MODE |
| DEVICE_MODE | 0x0200 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DEVICE_MODE |
| REMOTE_CONTROL_USED | 0x0202 | BITMASK | READ_WRITE | uint32_t | — | 1.0 | All devices | REMOTE_CONTROL_USED |
| DEVICE_FUNCTION | 0x0206 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | DEVICE_FUNCTION |
| AC_OUT_VOLTAGE_SETPOINT | 0x0230 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| ALARM_LOW_VOLTAGE_SET | 0x0320 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| INPUT_VOLTAGE_LOCKOUT | 0x0320 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| ALARM_LOW_VOLTAGE_CLEAR | 0x0321 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| INPUT_VOLTAGE_RESTART | 0x0321 | NUMERIC | READ_WRITE | uint16_t | V | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| RELAY_CONTROL | 0x034E | BOOLEAN | READ_WRITE | — | — | 1.0 | All devices | — |
| RELAY_MODE | 0x034F | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_MODE |
| RELAY_BAT_LOW_SET | 0x0350 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_LOW_CLEAR | 0x0351 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_HIGH_SET | 0x0352 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| RELAY_BAT_HIGH_CLEAR | 0x0353 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | All devices | — |
| DISPLAY_BACKLIGHT_MODE | 0x0400 | ENUM | READ_WRITE | — | — | 1.0 | All devices | DISPLAY_BACKLIGHT_MODE |
| DISPLAY_BACKLIGHT_INTENSITY | 0x0401 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_SCROLL_SPEED | 0x0402 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_SETUP_LOCK | 0x0403 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_TEMPERATURE_UNIT | 0x0404 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | All devices | — |
| DISPLAY_CONTRAST | 0x0406 | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | MPPT RS | — |
| DISPLAY_BACKLIGHT_MODE_RS | 0x0408 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | DISPLAY_BACKLIGHT_MODE_RS |
| RELAY_MIN_ENABLED_TIME | 0x100A | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | All devices | — |
| CHARGE_VOLTAGE_SETPOINT | 0x2001 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_VOLTAGE_SENSE | 0x2002 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_TEMPERATURE_SENSE | 0x2003 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | 0.01 °C |
| CHARGE_STATE_ELAPSED | 0x2007 | NUMERIC | READ_WRITE | uint32_t | ms | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| ABSORPTION_TIME | 0x2008 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| NETWORK_ERROR_CODE | 0x2009 | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | CHR_ERROR_CODE |
| BATTERY_CHARGE_CURRENT | 0x200A | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| BATTERY_IDLE_VOLTAGE | 0x200B | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| LINK_DEVICE_STATE | 0x200C | ENUM | READ_WRITE | — | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | LINK_DEVICE_STATE |
| NETWORK_MODE | 0x200E | BITMASK | READ_WRITE | uint8_t | — | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | NETWORK_MODE |
| TOTAL_CHARGE_CURRENT | 0x2013 | NUMERIC | READ_WRITE | int32_t | A | 0.001 | BlueSolar/SmartSolar MPPT, MPPT RS, Orion XS | — |
| CHARGE_CURRENT_PERCENT | 0x2014 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| CHARGE_CURRENT_LIMIT | 0x2015 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TOTAL_DC_INPUT_POWER | 0x2027 | NUMERIC | READ_WRITE | uint32_t | W | 0.01 | BlueSolar/SmartSolar MPPT, MPPT RS | — |
| TIME_OF_DAY | 0x2031 | NUMERIC | READ_WRITE | uint16_t | min | 1.0 | BlueSolar/SmartSolar MPPT | — |
| SHUTDOWN_LOW_VOLTAGE_SET | 0x2210 | NUMERIC | WRITE_ONLY | uint16_t | V | 0.01 | Phoenix/Smart inverters | — |
| REMOTE_INPUT_MODE_CONFIG | 0xD0C0 | ENUM | READ_WRITE | — | — | 1.0 | MPPT RS | REMOTE_INPUT_MODE_CONFIG |
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
| VOLTAGE_COMPENSATION | 0xEDCA | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| RELAY_OPERATION_MODE | 0xEDD9 | ENUM | READ_WRITE | — | — | 1.0 | All devices | RELAY_OPERATION_MODE |
| BAT_LOW_TEMP_LEVEL | 0xEDE0 | NUMERIC | READ_WRITE | int16_t | C | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| REBULK_VOLTAGE_OFFSET | 0xED2E | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| EQUALISATION_DURATION | 0xEDE3 | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| EQUALISATION_CURRENT_LEVEL | 0xEDE4 | NUMERIC | READ_WRITE | uint8_t | % | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| AUTO_EQUALISE_STOP_ON_VOLTAGE | 0xEDE5 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| LOW_TEMP_CHARGE_CURRENT | 0xEDE6 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| TAIL_CURRENT | 0xEDE7 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BMS_PRESENT | 0xEDE8 | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_VOLTAGE_SETTING | 0xEDEA | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | BAT_VOLTAGE_SETTING |
| BAT_MAX_CURRENT | 0xEDF0 | NUMERIC | READ_WRITE | uint16_t | A | 0.1 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_TYPE | 0xEDF1 | ENUM | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | BAT_TYPE |
| BAT_TEMPERATURE_COMPENSATION | 0xEDF2 | NUMERIC | READ_WRITE | int16_t | mV/K | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_EQUALISATION_VOLTAGE | 0xEDF4 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_STORAGE_VOLTAGE | 0xEDF5 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_FLOAT_VOLTAGE | 0xEDF6 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_ABSORPTION_VOLTAGE | 0xEDF7 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_ABSORPTION_LIMIT | 0xEDFB | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BAT_BULK_LIMIT | 0xEDFC | NUMERIC | READ_WRITE | uint16_t | h | 0.01 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| AUTOMATIC_EQUALISATION_MODE | 0xEDFD | NUMERIC | READ_WRITE | uint8_t | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| ADAPTIVE_MODE | 0xEDFE | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| BATTERYSAFE_MODE | 0xEDFF | BOOLEAN | READ_WRITE | — | — | 1.0 | Phoenix/Smart chargers, Orion XS, BuckBoost | — |
| SHUTDOWN_VOLTAGE | 0xEE36 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_VOLTAGE | 0xEE37 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| DELAYED_START_VOLTAGE | 0xEE38 | NUMERIC | READ_WRITE | uint16_t | V | 0.01 | Orion XS | — |
| START_DELAY | 0xEE39 | NUMERIC | READ_WRITE | uint32_t | s | 1.0 | Orion XS | — |
| ALARM_BUZZER | 0xEEFC | BOOLEAN | READ_WRITE | — | — | 1.0 | BMV-700/702/712, SmartShunt | — |
| TIMER_EVENT_0..5 | 0xEDA0..0xEDA5 | STRUCT | READ_WRITE | — | — | — | BlueSolar/SmartSolar MPPT | TIMER_EVENT |
