"""HEX register database, from Victron's official HEX protocol docs.

Sources (checked online):
- BlueSolar-HEX Rev 18 (BlueSolar/SmartSolar MPPT chargers) — full §1.1
  Get/Set items, §1.2 Async, history, VE.Smart networking, relay/display,
  load/lighting, TX/RX ports.
- Orion-XS-HEX (Orion XS DC-DC battery chargers) — product info, device
  control/state, battery settings, engine shutdown, input lockout, charger
  data, history (cumulative/cycle), remote control.
- VE.Direct-HEX-Protocol-Phoenix-Inverter Rev 5 + BMV-7xx-HEX (via
  krahabb/esphome-victron-vedirect ``ve_reg_register.h`` / ``ve_reg_enum.h``,
  itself extracted from those docs) — INV/CHG/BMV generic + RS extensions.
- VE.Direct-Protocol-3.34 (Text) for flavor hints only.

Each entry: name -> HexRegister(id, cls, access, dtype, unit, scale, flavors, enum).

Access: READ_ONLY | READ_WRITE | CONSTANT | WRITE_ONLY (write-only:
hex_get() raises RegisterError, hex_set()/hex_set_raw() allowed).
Cls: NUMERIC | ENUM | BOOLEAN | BITMASK | STRING | VOID | STRUCT.
STRUCT payloads decode to dict (history, timer events, voltage range,
load control, network status); encode accepts the same dict (or raw
bytes/int where documented).

Same-ID aliases: 0x0320/0x0321 (INV alarm vs Orion input lockout),
0xEDBB/0xEDBD/0xEDBC/0xED8D/0xED8F (MPPT panel vs Orion in/out),
0xED8E (BMV DC power vs Orion output power) share the wire id with
device-specific scale/name. lookup(name) is exact. Calling lookup(int) on aliased IDs without flavor
raises AmbiguousRegisterError; specify flavor (e.g. flavor="ORION") to disambiguate.
hex_get_raw()/hex_set_raw() are always unambiguous (raw bytes).

Flavor-specific enum overrides share the wire ID with the generic entry
but carry the family-documented value names: 0x0200/0x0201 (generic vs
Orion vs Phoenix DEVICE_MODE/DEVICE_STATE), 0x031C/0x031E (generic vs
Phoenix WARNING_REASON), 0x034F (MPPT vs BMV vs Phoenix RELAY_MODE).
A flavor-qualified lookup (or hex_get/hex_set on a flavored device)
selects the override; unflavored lookup(int) on these IDs raises
AmbiguousRegisterError.

Flavor policy (deliberately liberal): flavor tags are a superset of the
strict doc provenance. Registers documented for one family stay visible
under ANY (or a wider flavor) where another family plausibly shares the
wire ID — e.g. pluggable-display 0x0401-0x0404 (BlueSolar-HEX: LCD models
only), per-tracker history 0x10A0-0x10BE (BlueSolar-HEX Note 2: MPPT RS
only, also listed under MPPT), and generic ANY control/status IDs visible
in the BMV view although absent from BMV-7xx-HEX. Narrowing them would
break for_flavor() consumers with no wire-behavior contradiction, so the
stricter provenance lives in docs/SOURCES.md instead. 0x0400 is the
exception: BMV Rev 7 defines it as Backlight Always On (BOOLEAN) while
BlueSolar-HEX defines it as Display backlight mode (ENUM), so
DISPLAY_BACKLIGHT_MODE is scoped to (MPPT, MPPT_RS) and only
BMV_BACKLIGHT_ALWAYS_ON is visible to BMV/BMV71. 0x0350-0x0353 are the
second exception (same precedent): BlueSolar-HEX defines them as relay
battery set/clear (0.01 V) while BMV Rev 7 defines the same IDs as relay
low/high voltage(+clear) (0.1 V), so the generic RELAY_BAT_* entries are
scoped to (MPPT, MPPT_RS) and only BMV_RELAY_* is visible to BMV/BMV71.

Sentinels decoded as None (encode None back to sentinel):
- BAT_TEMPERATURE 0xEDEC 0xFFFF, TIME_OF_DAY 0x2031 0xFFFF,
  BATTERY_VOLTAGE_SENSE 0x2002 0xFFFF, BATTERY_TEMPERATURE_SENSE 0x2003
  0x7FFF, ORION_BATTERY_VOLTAGE 0xD18D 0x7FFFFFFF,
  LOW_TEMP_CHARGE_CURRENT 0xEDE6 0xFFFF (=use max),
  SETTINGS_CHANGED_TIMESTAMP 0xEC41 0xFFFFFFFF (=Invalid/no settings ever
  modified, BMV Rev 7 p8),
  BMV alarm/relay temperature 0x032A-0x032D/0x035A-0x035D 0x0000
  (=disabled, valid 174..372 K), history/cycle 0xFF/0xFFFF/
  0xFFFFFFFF per-field.
"""
from __future__ import annotations
from ..tables import MON_MAP

from dataclasses import dataclass
from ..exceptions import AmbiguousRegisterError, RegisterError


@dataclass(frozen=True)
class HexRegister:
    name: str
    id: int
    cls: str  # NUMERIC | ENUM | BOOLEAN | BITMASK | STRING | VOID | STRUCT
    access: str  # READ_ONLY | READ_WRITE | CONSTANT | WRITE_ONLY
    dtype: str = ""  # uint8_t | int8_t | uint16_t | int16_t | uint32_t | int32_t
    unit: str = ""
    scale: float = 1.0
    flavors: frozenset[str] = frozenset({"ANY"})
    enum: str | None = None


def _r(name, id, cls, access, dtype="", unit="", scale=1.0, flavors=("ANY",), enum=None):
    if not dtype:
        if cls == "ENUM":
            dtype = "int16_t" if id == 0xEEB8 else "uint8_t"
        elif cls == "BOOLEAN":
            dtype = "uint8_t"
        elif cls == "STRING":
            dtype = "string"
        elif id in (0x0100, 0x0102):
            dtype = "uint32_t"
        elif id == 0x0101:
            dtype = "uint24_t"
        elif id == 0xEDCE:
            dtype = "uint16_t"
        elif id in (0xEDAB, 0x200F):
            dtype = "uint8_t"
    return HexRegister(name, id, cls, access, dtype, unit, scale, frozenset(flavors), enum)


REGISTERS: dict[str, HexRegister] = {}
_BY_ID: dict[int, HexRegister] = {}
_REGISTERS_BY_ID: dict[int, list[HexRegister]] = {}


def _add(reg: HexRegister) -> None:
    REGISTERS[reg.name] = reg
    _REGISTERS_BY_ID.setdefault(reg.id, []).append(reg)
    if reg.id not in _BY_ID:
        _BY_ID[reg.id] = reg


def _add_alias(reg: HexRegister) -> None:
    REGISTERS[reg.name] = reg
    _REGISTERS_BY_ID.setdefault(reg.id, []).append(reg)
    if reg.id not in _BY_ID:
        _BY_ID[reg.id] = reg


# --- common (ANY) ---
for _reg in [
    _r("BLE_MODE", 0x0090, "ENUM", "READ_WRITE", "uint8_t", flavors=("ANY",), enum="BLE_MODE"),
    _r("PRODUCT_ID", 0x0100, "STRUCT", "CONSTANT", "uint32_t", enum="PRODUCT_ID"),
    _r("APP_VER", 0x0102, "STRUCT", "CONSTANT", "uint32_t", enum="APP_VERSION"),
    _r("SERIAL_NUMBER", 0x010A, "STRING", "CONSTANT"),
    _r("MODEL_NAME", 0x010B, "STRING", "CONSTANT"),
    _r("GROUP_ID", 0x0104, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("ANY",)),
    _r("CAPABILITIES", 0x0140, "BITMASK", "CONSTANT", "uint32_t", flavors=("ANY",), enum="CAPABILITIES"),
    # Doc access is R (Phoenix Rev 5 §2.1 product-info table; BLE row reads
    # R/(W) "write enabled for internal purposes only"): READ_ONLY, not
    # CONSTANT. No behavior change — device.py blocks writes to both.
    _r("CAPABILITIES_BLE", 0x0150, "BITMASK", "READ_ONLY", "uint32_t", flavors=("ANY",), enum="CAPABILITIES_BLE"),
    _r("DEVICE_MODE", 0x0200, "ENUM", "READ_WRITE", flavors=("ANY",), enum="DEVICE_MODE"),
    _r("DEVICE_STATE", 0x0201, "ENUM", "READ_ONLY", flavors=("ANY",), enum="DEVICE_STATE"),
    _r("REMOTE_CONTROL_USED", 0x0202, "BITMASK", "READ_WRITE", "uint32_t", flavors=("ANY",), enum="REMOTE_CONTROL_USED"),
    _r("DEVICE_OFF_REASON", 0x0205, "BITMASK", "READ_ONLY", "uint8_t", flavors=("MPPT",), enum="OFF_REASON_MPPT"),
    _r("DEVICE_OFF_REASON_2", 0x0207, "BITMASK", "READ_ONLY", "uint32_t", flavors=("MPPT",), enum="OFF_REASON_MPPT"),
    _r("WARNING_REASON", 0x031C, "BITMASK", "READ_ONLY", "uint16_t", flavors=("ANY",), enum="WARNING_REASON"),
    _r("ALARM_REASON", 0x031E, "BITMASK", "READ_ONLY", "uint16_t", flavors=("ANY",), enum="WARNING_REASON"),
    # Un8 0..1 (OFF,ON)/(OPEN,CLSD) in the docs; BOOLEAN is intentional:
    # Python bool == 0/1, so wire values round-trip and truthiness is kept.
    _r("RELAY_CONTROL", 0x034E, "BOOLEAN", "READ_WRITE"),
    # MPPT shares the Phoenix-family RELAY_MODE superset map (BlueSolar-HEX
    # documents no 0x034F of its own); BMV and INV use flavor overrides below.
    _r("RELAY_MODE", 0x034F, "ENUM", "READ_WRITE", flavors=("MPPT",), enum="RELAY_MODE"),
    _r("RESTORE_DEFAULTS", 0x0004, "VOID", "WRITE_ONLY", flavors=("ANY",)),
    _r("DC_CHANNEL1_VOLTAGE", 0xED8D, "NUMERIC", "READ_ONLY", "int16_t", "V", 0.01, ("BMV", "INV", "MPPT_RS")),
    _r("DC_CHANNEL1_CURRENT", 0xED8F, "NUMERIC", "READ_ONLY", "int16_t", "A", 0.1, ("BMV", "MPPT_RS")),
    _r("BAT_TEMPERATURE", 0xEDEC, "NUMERIC", "READ_ONLY", "uint16_t", "K", 0.01),
    # --- inverter ---
    # NOTE: no 0x0209 here (phantom register; see charger-section note).
    _r("INV_DEVICE_OFF_REASON", 0x0207, "BITMASK", "READ_ONLY", "uint32_t", flavors=("INV",), enum="OFF_REASON_INV"),
    _r("AC_OUT_VOLTAGE_SETPOINT", 0x0230, "NUMERIC", "WRITE_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("ALARM_LOW_VOLTAGE_SET", 0x0320, "NUMERIC", "WRITE_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("ALARM_LOW_VOLTAGE_CLEAR", 0x0321, "NUMERIC", "WRITE_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("AC_OUT_VOLTAGE", 0x2200, "NUMERIC", "READ_ONLY", "int16_t", "V", 0.01, ("INV",)),
    _r("AC_OUT_CURRENT", 0x2201, "NUMERIC", "READ_ONLY", "int16_t", "A", 0.1, ("INV",)),
    _r("AC_OUT_APPARENT_POWER", 0x2205, "NUMERIC", "READ_ONLY", "int32_t", "VA", 1.0, ("INV",)),
    _r("SHUTDOWN_LOW_VOLTAGE_SET", 0x2210, "NUMERIC", "WRITE_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    # Doc access is R (Phoenix Rev 5 §2.7 user battery control table):
    # READ_ONLY, not CONSTANT. No behavior change (device.py blocks both).
    _r("VOLTAGE_RANGE_MIN", 0x2211, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("VOLTAGE_RANGE_MAX", 0x2212, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    # --- charger (battery settings + charger data; MPPT inherits CHG, see for_flavor) ---
    # NOTE: no 0x020A here. 0x0209/0x020A appear in neither the Phoenix Rev 5
    # snapshot nor any other local snapshot (BlueSolar/BMV/Orion/Text); both
    # were removed as phantom registers.
    _r("DEVICE_FUNCTION", 0x0206, "ENUM", "READ_WRITE", flavors=("CHG",), enum="DEVICE_FUNCTION"),
    _r("BATTERYSAFE_MODE", 0xEDFF, "BOOLEAN", "READ_WRITE", flavors=("CHG",)),
    _r("VOLTAGE_COMPENSATION", 0xEDCA, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    _r("CHARGER_VOLTAGE", 0xEDD5, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("CHG",)),
    _r("CHARGER_CURRENT", 0xEDD7, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("CHG",)),
    _r("CHARGER_MAXIMUM_CURRENT", 0xEDDF, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("CHG",)),
    _r("ADDITIONAL_CHARGER_STATE", 0xEDD4, "BITMASK", "READ_ONLY", "uint8_t", flavors=("CHG",), enum="ADDITIONAL_CHARGER_STATE"),
    _r("VOLTAGE_SETTINGS_RANGE", 0xEDCE, "STRUCT", "READ_ONLY", flavors=("CHG",), enum="VOLTAGE_RANGE"),
    _r("HISTORY_VERSION", 0xEDCD, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("CHG",)),
    _r("STREETLIGHT_VERSION", 0xEDCC, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("CHG",)),
    _r("CHR_ERROR_CODE", 0xEDDA, "ENUM", "READ_ONLY", flavors=("CHG",), enum="CHR_ERROR_CODE"),
    _r("CHR_INTERNAL_TEMPERATURE", 0xEDDB, "NUMERIC", "READ_ONLY", "int16_t", "C", 0.01, ("CHG",)),
    _r("BAT_LOW_TEMP_LEVEL", 0xEDE0, "NUMERIC", "READ_WRITE", "int16_t", "C", 0.01, ("CHG",)),
    _r("REBULK_VOLTAGE_OFFSET", 0xED2E, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    _r("EQUALISATION_DURATION", 0xEDE3, "NUMERIC", "READ_WRITE", "uint16_t", "h", 0.01, ("CHG",)),
    _r("EQUALISATION_CURRENT_LEVEL", 0xEDE4, "NUMERIC", "READ_WRITE", "uint8_t", "%", 1.0, ("CHG",)),
    _r("AUTO_EQUALISE_STOP_ON_VOLTAGE", 0xEDE5, "BOOLEAN", "READ_WRITE", flavors=("CHG",)),
    _r("LOW_TEMP_CHARGE_CURRENT", 0xEDE6, "NUMERIC", "READ_WRITE", "uint16_t", "A", 0.1, ("CHG",)),
    _r("TAIL_CURRENT", 0xEDE7, "NUMERIC", "READ_WRITE", "uint16_t", "", 0.1, ("CHG",)),
    _r("BMS_PRESENT", 0xEDE8, "BOOLEAN", "READ_WRITE", flavors=("CHG",)),
    _r("BAT_VOLTAGE_SETTING", 0xEDEA, "ENUM", "READ_WRITE", flavors=("CHG",), enum="BAT_VOLTAGE_SETTING"),
    _r("BAT_VOLTAGE", 0xEDEF, "NUMERIC", "READ_WRITE", "uint8_t", "V", 1.0, ("CHG",)),
    _r("BAT_MAX_CURRENT", 0xEDF0, "NUMERIC", "READ_WRITE", "uint16_t", "A", 0.1, ("CHG",)),
    _r("BAT_TYPE", 0xEDF1, "ENUM", "READ_WRITE", flavors=("CHG",), enum="BAT_TYPE_LARGE"),
    # Small-charger limitation (BlueSolar-HEX Battery-type-values table):
    # 10A/15A/20A models (no rotary) define value 1 as TYPE_2, but there is
    # no small-charger flavor, so this register binds the large (30A+ rotary)
    # table where 1 is TYPE_1. Value 1 read via hex_get() on a 10/15/20A
    # model is therefore misread as type_1 — use
    # decode_bat_type(value, charger="small") for those models.
    _r("BAT_TEMPERATURE_COMPENSATION", 0xEDF2, "NUMERIC", "READ_WRITE", "int16_t", "mV/K", 0.01, ("CHG",)),
    _r("BAT_EQUALISATION_VOLTAGE", 0xEDF4, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    # Provenance: ABSENT from the BlueSolar-HEX Rev 18 §1.1 battery-settings
    # table and from every other local snapshot; carried over from the
    # krahabb upstream baseline as a charger-family setting. Kept (CHG) so
    # charger consumers do not lose a plausible wire ID — see SOURCES.md.
    _r("BAT_STORAGE_VOLTAGE", 0xEDF5, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    _r("BAT_FLOAT_VOLTAGE", 0xEDF6, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    _r("BAT_ABSORPTION_VOLTAGE", 0xEDF7, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("CHG",)),
    _r("BAT_ABSORPTION_LIMIT", 0xEDFB, "NUMERIC", "READ_WRITE", "uint16_t", "h", 0.01, ("CHG",)),
    _r("BAT_BULK_LIMIT", 0xEDFC, "NUMERIC", "READ_WRITE", "uint16_t", "h", 0.01, ("CHG",)),
    _r("AUTOMATIC_EQUALISATION_MODE", 0xEDFD, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("CHG",)),
    _r("ADAPTIVE_MODE", 0xEDFE, "BOOLEAN", "READ_WRITE", flavors=("CHG",)),
    # --- MPPT (BlueSolar/SmartSolar; inherits CHG via for_flavor) ---
    _r("SOLAR_ACTIVITY", 0x2030, "BOOLEAN", "READ_ONLY", flavors=("MPPT",)),
    _r("TIME_OF_DAY", 0x2031, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("MPPT",)),
    _r("LOAD_OUTPUT_STATE", 0xEDA8, "BOOLEAN", "READ_ONLY", flavors=("MPPT",)),
    _r("LOAD_CURRENT", 0xEDAD, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("MPPT",)),
    _r("LOAD_OFFSET_VOLTAGE", 0xEDAC, "NUMERIC", "READ_WRITE", "uint8_t", "V", 0.01, ("MPPT",)),
    _r("LOAD_OUTPUT_CONTROL", 0xEDAB, "STRUCT", "READ_WRITE", flavors=("MPPT",), enum="LOAD_CONTROL"),
    _r("LOAD_OUTPUT_VOLTAGE", 0xEDA9, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("LOAD_SWITCH_HIGH", 0xED9D, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("LOAD_SWITCH_LOW", 0xED9C, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("LOAD_OUTPUT_OFF_REASON", 0xED91, "BITMASK", "READ_ONLY", "uint8_t", flavors=("MPPT",), enum="LOAD_OUTPUT_OFF_REASON"),
    _r("LOAD_AES_TIMER", 0xED90, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("MPPT",)),
    _r("MPPT_TRACKER_MODE", 0xEDB3, "ENUM", "READ_ONLY", flavors=("MPPT",), enum="MPPT_TRACKER_MODE"),
    _r("PANEL_STARTING_VOLTAGE", 0xEDB2, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT_RS",)),
    _r("PANEL_INPUT_RESISTANCE", 0xEDB1, "NUMERIC", "READ_ONLY", "uint32_t", "Ohm", 1.0, ("MPPT_RS",)),
    _r("PANEL_MAXIMUM_VOLTAGE", 0xEDB8, "NUMERIC", "CONSTANT", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("PANEL_VOLTAGE", 0xEDBB, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("PANEL_POWER", 0xEDBC, "NUMERIC", "READ_ONLY", "uint32_t", "W", 0.01, ("MPPT",)),
    _r("PANEL_CURRENT", 0xEDBD, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("MPPT",)),
    _r("MAXIMUM_POWER_YESTERDAY", 0xEDD0, "NUMERIC", "READ_ONLY", "uint16_t", "W", 1.0, ("MPPT",)),
    _r("YIELD_YESTERDAY", 0xEDD1, "NUMERIC", "READ_ONLY", "uint16_or_uint32", "kWh", 0.01, ("MPPT",)),
    _r("MAXIMUM_POWER_TODAY", 0xEDD2, "NUMERIC", "READ_ONLY", "uint16_t", "W", 1.0, ("MPPT",)),
    _r("YIELD_TODAY", 0xEDD3, "NUMERIC", "READ_ONLY", "uint16_or_uint32", "kWh", 0.01, ("MPPT",)),
    _r("USER_YIELD", 0xEDDC, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.01, ("MPPT",)),
    _r("SYSTEM_YIELD", 0xEDDD, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.01, ("MPPT",)),
    _r("TX_PORT_MODE", 0xED9E, "ENUM", "READ_WRITE", flavors=("MPPT",), enum="TX_PORT_MODE"),
    _r("RX_PORT_MODE", 0xED98, "ENUM", "READ_WRITE", flavors=("MPPT",), enum="RX_PORT_MODE"),
    _r("MIDPOINT_SHIFT", 0xEDA7, "NUMERIC", "READ_WRITE", "int16_t", "min", 1.0, ("MPPT",)),
    _r("GRADUAL_DIM_SPEED", 0xED9B, "NUMERIC", "READ_WRITE", "uint8_t", "s", 1.0, ("MPPT",)),
    _r("PANEL_VOLTAGE_NIGHT", 0xED9A, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("PANEL_VOLTAGE_DAY", 0xED99, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT",)),
    _r("SUNSET_DELAY", 0xED96, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("MPPT",)),
    _r("SUNRISE_DELAY", 0xED97, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("MPPT",)),
    # --- BMV ---
    _r("TTG", 0x0FFE, "NUMERIC", "READ_ONLY", "uint16_t", "min", 1.0, ("BMV",)),
    _r("SOC", 0x0FFF, "NUMERIC", "READ_ONLY", "uint16_t", "%", 0.01, ("BMV",)),
    _r("DC_CHANNEL1_POWER", 0xED8E, "NUMERIC", "READ_ONLY", "int16_t", "W", 1.0, ("BMV",)),
    _r("DC_MONITOR_MODE", 0xEEB8, "ENUM", "READ_WRITE", flavors=("BMV", "BMV71"), enum="MON"),
    _r("ALARM_BUZZER", 0xEEFC, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    # --- MPPT_RS / MULTI_RS ---
    _r("MPPT_TRACKERS", 0x0244, "NUMERIC", "CONSTANT", "uint8_t", "", 1.0, ("MPPT_RS",)),
    _r("UNKNOWN_0305", 0x0305, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.001, ("MULTI_RS",)),
    _r("U_OUTPUT_YIELD", 0x0310, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.001, ("MULTI_RS",)),
    _r("U_USER_YIELD", 0x0311, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.001, ("MULTI_RS",)),
    _r("U_AC_OUT_VOLTAGE", 0x2213, "NUMERIC", "READ_ONLY", "int16_t", "V", 0.01, ("MULTI_RS",)),
    _r("U_AC_OUT_CURRENT", 0x2214, "NUMERIC", "READ_ONLY", "int16_t", "A", 0.01, ("MULTI_RS",)),
    _r("U_AC_OUT_REAL_POWER", 0x2215, "NUMERIC", "READ_ONLY", "int32_t", "W", 1.0, ("MULTI_RS",)),
    _r("U_AC_OUT_APPARENT_POWER", 0x2216, "NUMERIC", "READ_ONLY", "int32_t", "VA", 1.0, ("MULTI_RS",)),
    _r("U_AC_OUT_CURRENT_MA", 0xD3A1, "NUMERIC", "READ_ONLY", "int32_t", "A", 0.001, ("MULTI_RS",)),
    _r("UNKNOWN_D5C8", 0xD5C8, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("MULTI_RS",)),
    _r("UNKNOWN_D5CA", 0xD5CA, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("MULTI_RS",)),
    _r("UNKNOWN_D5CB", 0xD5CB, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("MULTI_RS",)),
    _r("UNKNOWN_2250", 0x2250, "NUMERIC", "READ_ONLY", "int16_t", "", 1.0, ("MULTI_RS",)),
    _r("UNKNOWN_2251", 0x2251, "NUMERIC", "READ_ONLY", "int16_t", "", 1.0, ("MULTI_RS",)),
    _r("UNKNOWN_0FFC", 0x0FFC, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("MULTI_RS",)),
    _r("TWO_WIRE_BMS_INPUT_STATE", 0xD01F, "BITMASK", "READ_ONLY", "uint8_t", flavors=("MPPT_RS",), enum="TWO_WIRE_BMS"),
    _r("REMOTE_INPUT_MODE_CONFIG", 0xD0C0, "ENUM", "READ_WRITE", flavors=("MPPT_RS",), enum="REMOTE_INPUT_MODE_CONFIG"),
    _r("BATTERY_RIPPLE_VOLTAGE", 0xED8B, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT_RS",)),
    _r("PANEL_MAXIMUM_CURRENT", 0xEDBF, "NUMERIC", "CONSTANT", "uint16_t", "A", 0.1, ("MPPT_RS",)),
    _r("EQUALISE_CURRENT_MAXIMUM", 0xEDC7, "NUMERIC", "READ_ONLY", "uint8_t", "%", 1.0, ("MPPT_RS",)),
    _r("EQUALISE_VOLTAGE_MAXIMUM", 0xEDC6, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT_RS",)),
    # BlueSolar-HEX §1.1 Charger-data table lists these with Note 6 (fw 1.30)
    # and no RS-only marker — unlike 0xEDC6/0xEDC7 (Note 8 "MPPT RS models
    # only") — so they stay visible under MPPT as well as MPPT_RS.
    _r("ADJUSTABLE_VOLTAGE_MIN", 0x2211, "NUMERIC", "CONSTANT", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("ADJUSTABLE_VOLTAGE_MAX", 0x2212, "NUMERIC", "CONSTANT", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    # --- relay / display (ANY: relay/display option per model) ---
    # Liberal ANY flavoring by design (see module docstring): BlueSolar-HEX
    # scopes 0x0401-0x0404 to pluggable-LCD models, but narrowing would break
    # for_flavor() consumers with no wire contradiction. 0x0400 is the
    # exception: BMV Rev 7 defines it as Backlight Always On (Un8 0..1
    # OFF/ON) while BlueSolar-HEX defines it as Display backlight mode
    # (0=keypress/1=on/2=auto), so it is scoped to (MPPT, MPPT_RS) so each
    # flavor resolves to exactly one 0x0400 meaning.
    # 0x0350-0x0353 are the second exception (same 0x0400 precedent):
    # BlueSolar-HEX Relay settings defines them as battery low/high set/clear
    # (Un16 0.01 V); BMV Rev 7 Relay settings defines the same wire IDs as
    # Relay Low/High Voltage(+Clear) (Un16 0.1 V, BMV_RELAY_*). Phoenix Rev 5
    # and Orion-XS snapshots define no 0x0350-0x0353 at all (verified by ID
    # search), so the generic entries are scoped to (MPPT, MPPT_RS) — the only
    # family documenting the 0.01 V meaning — so for_flavor("BMV") excludes
    # them and lookup(0x0350, flavor="BMV") resolves the 0.1 V BMV register.
    # Name-based lookup with a flavor re-resolves by wire ID (see lookup()),
    # so hex_get("RELAY_BAT_LOW_SET", flavor="BMV") decodes via the BMV entry.
    _r("RELAY_OPERATION_MODE", 0xEDD9, "ENUM", "READ_WRITE", flavors=("ANY",), enum="RELAY_OPERATION_MODE"),
    _r("RELAY_BAT_LOW_SET", 0x0350, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("RELAY_BAT_LOW_CLEAR", 0x0351, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("RELAY_BAT_HIGH_SET", 0x0352, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("RELAY_BAT_HIGH_CLEAR", 0x0353, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("RELAY_PANEL_HIGH_SET", 0xEDBA, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("ANY",)),
    _r("RELAY_PANEL_HIGH_CLEAR", 0xEDB9, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("ANY",)),
    _r("RELAY_MIN_ENABLED_TIME", 0x100A, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("ANY",)),
    _r("DISPLAY_BACKLIGHT_MODE", 0x0400, "ENUM", "READ_WRITE", flavors=("MPPT", "MPPT_RS"), enum="DISPLAY_BACKLIGHT_MODE"),
    _r("DISPLAY_BACKLIGHT_INTENSITY", 0x0401, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("ANY",)),
    _r("DISPLAY_SCROLL_SPEED", 0x0402, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("ANY",)),
    _r("DISPLAY_SETUP_LOCK", 0x0403, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("ANY",)),
    _r("DISPLAY_TEMPERATURE_UNIT", 0x0404, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("ANY",)),
    _r("DISPLAY_CONTRAST", 0x0406, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("MPPT_RS",)),
    _r("DISPLAY_BACKLIGHT_MODE_RS", 0x0408, "ENUM", "READ_WRITE", flavors=("MPPT_RS",), enum="DISPLAY_BACKLIGHT_MODE_RS"),
    # --- history (BlueSolar MPPT; STRUCT payloads, see decode helpers) ---
    _r("CLEAR_HISTORY", 0x1030, "VOID", "WRITE_ONLY", flavors=("MPPT", "ORION")),
    _r("TOTAL_HISTORY", 0x104F, "STRUCT", "READ_ONLY", flavors=("MPPT",), enum="HISTORY_TOTAL"),
    # --- VE.Smart networking / synchronized charging (MPPT + MPPT_RS) ---
    _r("CHARGE_ALGORITHM_VERSION", 0x2000, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("MPPT", "MPPT_RS", "ORION")),
    _r("CHARGE_VOLTAGE_SETPOINT", 0x2001, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS", "ORION")),
    _r("BATTERY_VOLTAGE_SENSE", 0x2002, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS", "ORION")),
    _r("BATTERY_TEMPERATURE_SENSE", 0x2003, "NUMERIC", "READ_WRITE", "int16_t", "C", 0.01, ("MPPT", "MPPT_RS", "ORION")),
    _r("REMOTE_COMMAND", 0x2004, "ENUM", "WRITE_ONLY", flavors=("MPPT", "MPPT_RS"), enum="REMOTE_COMMAND"),
    _r("CHARGE_STATE_ELAPSED", 0x2007, "NUMERIC", "READ_WRITE", "uint32_t", "ms", 1.0, ("MPPT", "MPPT_RS")),
    _r("ABSORPTION_TIME", 0x2008, "NUMERIC", "READ_WRITE", "uint16_t", "h", 0.01, ("MPPT", "MPPT_RS")),
    _r("NETWORK_ERROR_CODE", 0x2009, "ENUM", "READ_WRITE", flavors=("MPPT", "MPPT_RS"), enum="CHR_ERROR_CODE"),
    _r("BATTERY_CHARGE_CURRENT", 0x200A, "NUMERIC", "READ_WRITE", "int32_t", "A", 0.001, ("MPPT", "MPPT_RS", "ORION")),
    _r("BATTERY_IDLE_VOLTAGE", 0x200B, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("MPPT", "MPPT_RS")),
    _r("LINK_DEVICE_STATE", 0x200C, "ENUM", "READ_WRITE", flavors=("MPPT", "MPPT_RS", "ORION"), enum="LINK_DEVICE_STATE"),
    _r("NETWORK_INFO", 0x200D, "BITMASK", "READ_ONLY", "uint8_t", flavors=("MPPT", "MPPT_RS", "ORION"), enum="NETWORK_INFO"),
    _r("NETWORK_MODE", 0x200E, "BITMASK", "READ_WRITE", "uint8_t", flavors=("MPPT", "MPPT_RS", "ORION"), enum="NETWORK_MODE"),
    _r("NETWORK_STATUS", 0x200F, "STRUCT", "READ_ONLY", flavors=("MPPT", "MPPT_RS", "ORION"), enum="NETWORK_STATUS"),
    _r("TOTAL_CHARGE_CURRENT", 0x2013, "NUMERIC", "READ_WRITE", "int32_t", "A", 0.001, ("MPPT", "MPPT_RS", "ORION")),
    _r("CHARGE_CURRENT_PERCENT", 0x2014, "NUMERIC", "READ_WRITE", "uint8_t", "%", 1.0, ("MPPT", "MPPT_RS")),
    _r("CHARGE_CURRENT_LIMIT", 0x2015, "NUMERIC", "READ_WRITE", "uint16_t", "A", 0.1, ("MPPT", "MPPT_RS", "ORION")),
    _r("MANUAL_EQUALISE_PENDING", 0x2018, "BOOLEAN", "READ_ONLY", flavors=("MPPT", "MPPT_RS")),
    _r("TOTAL_DC_INPUT_POWER", 0x2027, "NUMERIC", "READ_WRITE", "uint32_t", "W", 0.01, ("MPPT", "MPPT_RS")),
    # --- Orion XS engine shutdown + sensing + cycle history (ORION; inherits CHG) ---
    _r("SHUTDOWN_VOLTAGE", 0xEE36, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("ORION",)),
    _r("START_VOLTAGE", 0xEE37, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("ORION",)),
    _r("DELAYED_START_VOLTAGE", 0xEE38, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.01, ("ORION",)),
    _r("START_DELAY", 0xEE39, "NUMERIC", "READ_WRITE", "uint32_t", "s", 1.0, ("ORION",)),
    _r("ORION_BATTERY_VOLTAGE", 0xD18D, "NUMERIC", "READ_ONLY", "int32_t", "V", 0.001, ("ORION",)),
    _r("ESTIMATED_BATTERY_TEMPERATURE", 0xEE12, "NUMERIC", "READ_ONLY", "uint16_t", "K", 0.01, ("ORION",)),
    _r("CYCLE_COUNT", 0x106F, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("ORION",)),
    _r("CYCLE_SEQUENCE_NUMBER", 0x1099, "STRUCT", "READ_ONLY", flavors=("ORION",), enum="CYCLE_SEQUENCE"),
    _r("CUMULATIVE_USER_HISTORY", 0x1043, "STRUCT", "READ_ONLY", flavors=("ORION",), enum="CUMULATIVE_HISTORY"),
    _r("CUMULATIVE_SERVICE_HISTORY", 0x1042, "STRUCT", "READ_ONLY", flavors=("ORION",), enum="CUMULATIVE_HISTORY"),
]:
    _add(_reg)

# Multi-tracker RS registers (1..4)
for _i, _base in ((1, 0xECC3), (2, 0xECD3), (3, 0xECE3), (4, 0xECF3)):
    _add(_r(f"MPPT_TRACKER_MODE_{_i}", _base, "ENUM", "READ_ONLY", flavors=("MPPT_RS",), enum="MPPT_TRACKER_MODE"))
    _add(_r(f"PANEL_VOLTAGE_{_i}", _base + 0x08, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("MPPT_RS",)))
    _add(_r(f"PANEL_POWER_{_i}", _base + 0x09, "NUMERIC", "READ_ONLY", "uint32_t", "W", 0.01, ("MPPT_RS",)))
    _add(_r(f"PANEL_CURRENT_{_i}", _base + 0x0A, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("MPPT_RS",)))

# Timer events 0..5 (0xEDA0..0xEDA5, un32 bitfields)
for _i in range(6):
    _add(_r(f"TIMER_EVENT_{_i}", 0xEDA0 + _i, "STRUCT", "READ_WRITE", flavors=("MPPT",), enum="TIMER_EVENT"))

# Daily history 0x1050..0x106E (today + 30 days) + MPPT per-tracker 0x10A0..0x10BE
for _rid in range(0x1050, 0x106F):
    _add(_r(f"DAILY_HISTORY_{_rid - 0x1050}", _rid, "STRUCT", "READ_ONLY", flavors=("MPPT",), enum="HISTORY_DAY"))
for _rid in range(0x10A0, 0x10BF):
    # Liberal flavoring by design: BlueSolar-HEX Note 2 restricts these to
    # multi-tracker units (MPPT RS), but MPPT is included so for_flavor("MPPT")
    # consumers keep working; see module docstring + SOURCES.md.
    _add(_r(f"MPPT_HISTORY_{_rid - 0x10A0}", _rid, "STRUCT", "READ_ONLY", flavors=("MPPT_RS", "MPPT"), enum="HISTORY_MPPT_DAY"))

# Orion cycle history 0x1070..0x1098 (newest..oldest)
for _rid in range(0x1070, 0x1099):
    _add(_r(f"CYCLE_HISTORY_{_rid - 0x1070}", _rid, "STRUCT", "READ_ONLY", flavors=("ORION",), enum="CYCLE_HISTORY"))

# --- Phoenix Inverter (Official VE.Direct-HEX-Protocol-Phoenix-Inverter) ---
for _reg in [
    _r("INV_NVM_COMMAND", 0xEB99, "ENUM", "WRITE_ONLY", flavors=("INV",), enum="INV_NVM_COMMAND"),
    _r("PRODUCT_REVISION", 0x0101, "STRUCT", "READ_ONLY", "uint24_t", "", 1.0, ("ANY",), "PRODUCT_REVISION"),
    _r("INV_BAT_VOLTAGE", 0xEDEF, "NUMERIC", "READ_ONLY", "uint8_t", "V", 1.0, ("INV",)),
    _r("INV_RELAY_MODE", 0x034F, "ENUM", "WRITE_ONLY", flavors=("INV",), enum="RELAY_MODE_INV"),
    _r("AC_OUT_NOM_VOLTAGE", 0x2202, "NUMERIC", "READ_ONLY", "uint8_t", "V", 1.0, ("INV",)),
    _r("AC_OUT_RATED_POWER", 0x2203, "NUMERIC", "READ_ONLY", "int16_t", "VA", 1.0, ("INV",)),
    _r("AC_LOAD_SENSE_POWER_THRESHOLD", 0x2206, "NUMERIC", "WRITE_ONLY", "uint16_t", "VA", 1.0, ("INV",)),
    _r("AC_LOAD_SENSE_POWER_CLEAR", 0x2207, "NUMERIC", "WRITE_ONLY", "uint16_t", "VA", 1.0, ("INV",)),
    _r("AC_OUT_VOLTAGE_SETPOINT_MIN", 0x0231, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("AC_OUT_VOLTAGE_SETPOINT_MAX", 0x0232, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("INV",)),
    _r("INV_WAVE_SET50HZ_NOT60HZ", 0xEB03, "ENUM", "WRITE_ONLY", flavors=("INV",), enum="INV_AC_FREQUENCY"),
    _r("INV_ECO_MODE_MIN_CURRENT", 0xEB04, "NUMERIC", "WRITE_ONLY", "int16_t", "A", 0.001, ("INV",)),
    _r("INV_ECO_MODE_RETRY_TIME", 0xEB06, "NUMERIC", "WRITE_ONLY", "uint8_t", "s", 0.25, ("INV",)),
    # Stored as a raw period count (Phoenix Rev 5 §2.6 Note 4: "expressed in
    # number of periods of the AC-out frequency"); the table's 0.02/0.016
    # print is the per-period duration in seconds, not a scale factor.
    _r("INV_ECO_LOAD_DETECT_PERIODS", 0xEB10, "NUMERIC", "WRITE_ONLY", "uint8_t", "", 1.0, ("INV",)),
    _r("INV_DYN_CUTOFF_ENABLE", 0xEBBA, "BOOLEAN", "WRITE_ONLY", flavors=("INV",)),
    _r("INV_DYN_CUTOFF_FACTOR", 0xEBB7, "NUMERIC", "WRITE_ONLY", "uint16_t", "", 1.0, ("INV",)),
    _r("INV_DYN_CUTOFF_FACTOR2000", 0xEBB5, "NUMERIC", "WRITE_ONLY", "uint16_t", "", 1.0, ("INV",)),
    _r("INV_DYN_CUTOFF_FACTOR250", 0xEBB3, "NUMERIC", "WRITE_ONLY", "uint16_t", "", 1.0, ("INV",)),
    _r("INV_DYN_CUTOFF_FACTOR5", 0xEBB2, "NUMERIC", "WRITE_ONLY", "uint16_t", "", 1.0, ("INV",)),
    _r("INV_DYN_CUTOFF_VOLTAGE", 0xEBB1, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.001, ("INV",)),
    _r("INV_LOOP_GET_IINV", 0xEB4E, "NUMERIC", "READ_ONLY", "int16_t", "A", 0.001, ("INV",)),
    _r("INV_HISTORY_TIME", 0x1040, "NUMERIC", "READ_ONLY", "uint32_t", "s", 1.0, ("INV",)),
    _r("INV_HISTORY_ENERGY", 0x1041, "NUMERIC", "READ_ONLY", "uint32_t", "kVAh", 0.01, ("INV",)),
]:
    _add(_reg)

# --- BMV (Official BMV-7xx-HEX-Protocol) ---
# NOTE on Un8 0..1 (OFF,ON) settings below (buzzer, relay invert, backlight,
# show-toggles, setup lock, start-synchronized): modeled as BOOLEAN on
# purpose. Python bool == 0/1, so wire values round-trip exactly and callers
# keep boolean truthiness; an off/on string enum would make "off" truthy.
for _reg in [
    _r("BMV_BATTERY_CAPACITY", 0x1000, "NUMERIC", "READ_WRITE", "uint16_t", "Ah", 1.0, ("BMV",)),
    _r("BMV_CHARGED_VOLTAGE", 0x1001, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_TAIL_CURRENT", 0x1002, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_CHARGED_DETECTION_TIME", 0x1003, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("BMV",)),
    _r("BMV_CHARGE_EFFICIENCY", 0x1004, "NUMERIC", "READ_WRITE", "uint16_t", "%", 1.0, ("BMV",)),
    _r("BMV_PEUKERT_COEFFICIENT", 0x1005, "NUMERIC", "READ_WRITE", "uint16_t", "", 0.01, ("BMV",)),
    _r("BMV_CURRENT_THRESHOLD", 0x1006, "NUMERIC", "READ_WRITE", "uint16_t", "A", 0.01, ("BMV",)),
    _r("BMV_TTG_DELTA_T", 0x1007, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("BMV",)),
    _r("BMV_DISCHARGE_FLOOR", 0x1008, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_RELAY_LOW_SOC_CLEAR", 0x1009, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_RELAY_DISABLE_TIME", 0x100B, "NUMERIC", "READ_WRITE", "uint16_t", "min", 1.0, ("BMV",)),
    _r("BMV_USER_CURRENT_ZERO", 0x1034, "NUMERIC", "READ_ONLY", "int16_t", "ADC count", 1.0, ("BMV",)),
    _r("BMV_ZERO_CURRENT", 0x1029, "VOID", "WRITE_ONLY", flavors=("BMV",)),
    _r("BMV_SYNCHRONIZE", 0x102C, "VOID", "WRITE_ONLY", flavors=("BMV",)),
    _r("BMV_CLEAR_HISTORY", 0x1030, "VOID", "WRITE_ONLY", flavors=("BMV",)),
    _r("BMV_DEPTH_LAST_DISCHARGE", 0x0301, "NUMERIC", "READ_ONLY", "int32_t", "Ah", 0.1, ("BMV",)),
    _r("BMV_DEPTH_AVERAGE_DISCHARGE", 0x0302, "NUMERIC", "READ_ONLY", "int32_t", "Ah", 0.1, ("BMV",)),
    _r("BMV_NUMBER_CYCLES", 0x0303, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("BMV",)),
    _r("BMV_NUMBER_FULL_DISCHARGES", 0x0304, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("BMV",)),
    _r("BMV_CUMULATIVE_AMP_HOURS", 0x0305, "NUMERIC", "READ_ONLY", "int32_t", "Ah", 0.1, ("BMV",)),
    _r("BMV_MINIMUM_VOLTAGE", 0x0306, "NUMERIC", "READ_ONLY", "int32_t", "V", 0.01, ("BMV",)),
    _r("BMV_MAXIMUM_VOLTAGE", 0x0307, "NUMERIC", "READ_ONLY", "int32_t", "V", 0.01, ("BMV",)),
    _r("BMV_SECONDS_SINCE_FULL_CHARGE", 0x0308, "NUMERIC", "READ_ONLY", "uint32_t", "s", 1.0, ("BMV",)),
    _r("BMV_NUMBER_AUTOMATIC_SYNCS", 0x0309, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("BMV",)),
    _r("BMV_NUMBER_LOW_VOLTAGE_ALARMS", 0x030A, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("BMV",)),
    _r("BMV_NUMBER_HIGH_VOLTAGE_ALARMS", 0x030B, "NUMERIC", "READ_ONLY", "uint32_t", "", 1.0, ("BMV",)),
    _r("BMV_MINIMUM_STARTER_VOLTAGE", 0x030E, "NUMERIC", "READ_ONLY", "int32_t", "V", 0.01, ("BMV",)),
    _r("BMV_MAXIMUM_STARTER_VOLTAGE", 0x030F, "NUMERIC", "READ_ONLY", "int32_t", "V", 0.01, ("BMV",)),
    _r("BMV_AMOUNT_DISCHARGED_ENERGY", 0x0310, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.01, ("BMV",)),
    _r("BMV_AMOUNT_CHARGED_ENERGY", 0x0311, "NUMERIC", "READ_ONLY", "uint32_t", "kWh", 0.01, ("BMV",)),
    _r("BMV_ALARM_LOW_VOLTAGE", 0x0320, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_VOLTAGE_CLEAR", 0x0321, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_HIGH_VOLTAGE", 0x0322, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_HIGH_VOLTAGE_CLEAR", 0x0323, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_STARTER", 0x0324, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_STARTER_CLEAR", 0x0325, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_HIGH_STARTER", 0x0326, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_HIGH_STARTER_CLEAR", 0x0327, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_SOC", 0x0328, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_SOC_CLEAR", 0x0329, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_ALARM_LOW_TEMPERATURE", 0x032A, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_ALARM_LOW_TEMPERATURE_CLEAR", 0x032B, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_ALARM_HIGH_TEMPERATURE", 0x032C, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_ALARM_HIGH_TEMPERATURE_CLEAR", 0x032D, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_ALARM_MID_VOLTAGE", 0x0331, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_ALARM_MID_VOLTAGE_CLEAR", 0x0332, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    # BMV Rev 7 Alarm settings row carries no "(write only)" marker — unlike
    # 0x1029/0x102C/0x0004/0x1030 — under the "Get and Set items" heading, so
    # get+set (READ_WRITE), not WRITE_ONLY.
    _r("BMV_ALARM_ACKNOWLEDGE", 0x031F, "VOID", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_RELAY_INVERT", 0x034D, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_RELAY_LOW_VOLTAGE", 0x0350, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_LOW_VOLTAGE_CLEAR", 0x0351, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_HIGH_VOLTAGE", 0x0352, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_HIGH_VOLTAGE_CLEAR", 0x0353, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_LOW_STARTER", 0x0354, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_LOW_STARTER_CLEAR", 0x0355, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_HIGH_STARTER", 0x0356, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_HIGH_STARTER_CLEAR", 0x0357, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("BMV",)),
    _r("BMV_RELAY_LOW_TEMPERATURE", 0x035A, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_RELAY_LOW_TEMPERATURE_CLEAR", 0x035B, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_RELAY_HIGH_TEMPERATURE", 0x035C, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_RELAY_HIGH_TEMPERATURE_CLEAR", 0x035D, "NUMERIC", "READ_WRITE", "uint16_t", "K", 0.01, ("BMV",)),
    _r("BMV_RELAY_MID_VOLTAGE", 0x0361, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_RELAY_MID_VOLTAGE_CLEAR", 0x0362, "NUMERIC", "READ_WRITE", "uint16_t", "%", 0.1, ("BMV",)),
    _r("BMV_BACKLIGHT_INTENSITY", 0xEEFE, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("BMV",)),
    _r("BMV_BACKLIGHT_ALWAYS_ON", 0x0400, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SCROLL_SPEED", 0xEEF5, "NUMERIC", "READ_WRITE", "uint8_t", "", 1.0, ("BMV",)),
    _r("BMV_SHOW_VOLTAGE", 0xEEE0, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_AUXILIARY_VOLTAGE", 0xEEE1, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_MID_VOLTAGE", 0xEEE2, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_CURRENT", 0xEEE3, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_CONSUMED_AH", 0xEEE4, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_SOC", 0xEEE5, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_TTG", 0xEEE6, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_TEMPERATURE", 0xEEE7, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHOW_POWER", 0xEEE8, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    # BMV Rev 7 Miscellaneous row: Un16 HEX (AA.BB). STRUCT so decode yields
    # {"raw": int, "version": "AA.BB"} with the raw word still accessible.
    _r("BMV_SW_VERSION", 0xEEF9, "STRUCT", "READ_ONLY", "uint16_t", "", 1.0, ("BMV",), "BMV_SW_VERSION"),
    _r("BMV_SETUP_LOCK", 0xEEF6, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("BMV_SHUNT_AMPS", 0xEEFB, "NUMERIC", "READ_WRITE", "uint16_t", "A", 1.0, ("BMV",)),
    _r("BMV_SHUNT_VOLTS", 0xEEFA, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.001, ("BMV",)),
    _r("BMV_TEMPERATURE_UNIT", 0xEEF7, "ENUM", "READ_WRITE", flavors=("BMV",), enum="BMV_TEMPERATURE_UNIT"),
    _r("BMV_TEMPERATURE_COEFFICIENT", 0xEEF4, "NUMERIC", "READ_WRITE", "uint16_t", "%CAP/degC", 0.1, ("BMV",)),
    _r("BMV_AUXILIARY_INPUT", 0xEEF8, "ENUM", "READ_WRITE", flavors=("BMV",), enum="BMV_AUXILIARY_INPUT"),
    _r("BMV_START_SYNCHRONIZED", 0x0FFD, "BOOLEAN", "READ_WRITE", flavors=("BMV",)),
    _r("SETTINGS_CHANGED_TIMESTAMP", 0xEC41, "NUMERIC", "READ_WRITE", "uint32_t", "s", 1.0, ("ANY",)),
    _r("BMV_DESCRIPTION", 0x010C, "STRING", "READ_WRITE", "string", "", 1.0, ("BMV",)),
    _r("DEVICE_UPTIME", 0x0120, "NUMERIC", "READ_ONLY", "uint32_t", "s", 1.0, ("ANY",)),
    _r("BMV_DEEPEST_DISCHARGE", 0x0300, "NUMERIC", "READ_ONLY", "int32_t", "Ah", 0.1, ("BMV",)),
    _r("BMV_MIDPOINT_VOLTAGE", 0x0382, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("BMV",)),
    _r("BMV_MIDPOINT_DEVIATION", 0x0383, "NUMERIC", "READ_ONLY", "int16_t", "%", 0.1, ("BMV",)),
    _r("BMV_AUX_VOLTAGE", 0xED7D, "NUMERIC", "READ_ONLY", "int16_t", "V", 0.01, ("BMV",)),
    _r("BMV_CURRENT_PRECISE", 0xED8C, "NUMERIC", "READ_ONLY", "int32_t", "A", 0.001, ("BMV",)),
    _r("BMV_SYNC_STATE", 0xEEB6, "NUMERIC", "READ_ONLY", "uint8_t", "", 1.0, ("BMV",)),
    _r("BMV_CONSUMED_AH", 0xEEFF, "NUMERIC", "READ_ONLY", "int32_t", "Ah", 0.1, ("BMV",)),
]:
    _add(_reg)


# Same-ID device aliases (lookup by name exact; lookup by int returns first — documented).
_add_alias(_r("INPUT_VOLTAGE_LOCKOUT", 0x0320, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("ORION",)))
_add_alias(_r("INPUT_VOLTAGE_RESTART", 0x0321, "NUMERIC", "READ_WRITE", "uint16_t", "V", 0.1, ("ORION",)))
_add_alias(_r("INPUT_VOLTAGE", 0xEDBB, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("ORION",)))
_add_alias(_r("INPUT_CURRENT", 0xEDBD, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("ORION",)))
_add_alias(_r("INPUT_POWER", 0xEDBC, "NUMERIC", "READ_ONLY", "uint32_t", "W", 0.01, ("ORION",)))
_add_alias(_r("OUTPUT_VOLTAGE", 0xED8D, "NUMERIC", "READ_ONLY", "uint16_t", "V", 0.01, ("ORION",)))
_add_alias(_r("OUTPUT_CURRENT", 0xED8F, "NUMERIC", "READ_ONLY", "uint16_t", "A", 0.1, ("ORION",)))
_add_alias(_r("OUTPUT_POWER", 0xED8E, "NUMERIC", "READ_ONLY", "uint32_t", "W", 0.01, ("ORION",)))
_add_alias(_r("ORION_DEVICE_OFF_REASON", 0x0207, "BITMASK", "READ_ONLY", "uint32_t", flavors=("ORION",), enum="OFF_REASON_ORION"))
_add_alias(_r("ORION_BAT_TYPE", 0xEDF1, "ENUM", "READ_WRITE", flavors=("ORION",), enum="BAT_TYPE_ORION"))
_add_alias(_r("MPPT_RS_BAT_TYPE", 0xEDF1, "ENUM", "READ_WRITE", flavors=("MPPT_RS",), enum="BAT_TYPE_RS"))

# --- Flavor-specific enum overrides (same wire ID, family-documented names).
# A flavor-qualified lookup resolves to these instead of the generic entry,
# so no other family's decode changes. Unflavored lookup(int) on these IDs
# raises AmbiguousRegisterError (documented alias pattern).
# - 0x034F BMV Rev 7 Relay settings: Un8 0..2 (DFLT, CHRG, REM).
_add_alias(_r("BMV_RELAY_MODE", 0x034F, "ENUM", "READ_WRITE", flavors=("BMV",), enum="RELAY_MODE_BMV"))
# - 0x0200/0x0201 Orion-XS-HEX: mode 0/4 off, 1/3 on; state NOT_CHARGING,
#   PSU(11), WAKE-UP(245), UNAVAILABLE(255), ...
_add_alias(_r("ORION_DEVICE_MODE", 0x0200, "ENUM", "READ_WRITE", flavors=("ORION",), enum="DEVICE_MODE_ORION"))
_add_alias(_r("ORION_DEVICE_STATE", 0x0201, "ENUM", "READ_ONLY", flavors=("ORION",), enum="DEVICE_STATE_ORION"))
# - 0x0200/0x0201 Phoenix Rev 5 §2.3.1/§2.4.2: states 0,1,2,9 and modes
#   2,3,4,5,0xFD only — the generic map's extra values never occur here.
_add_alias(_r("INV_DEVICE_MODE", 0x0200, "ENUM", "READ_WRITE", flavors=("INV",), enum="DEVICE_MODE_INV"))
_add_alias(_r("INV_DEVICE_STATE", 0x0201, "ENUM", "READ_ONLY", flavors=("INV",), enum="DEVICE_STATE_INV"))
# - 0x031C/0x031E Phoenix Rev 5 §2.3.3: bits 0,1,5,6,8,9,10,11 only.
_add_alias(_r("INV_WARNING_REASON", 0x031C, "BITMASK", "READ_ONLY", "uint16_t", flavors=("INV",), enum="WARNING_REASON_INV"))
_add_alias(_r("INV_ALARM_REASON", 0x031E, "BITMASK", "READ_ONLY", "uint16_t", flavors=("INV",), enum="WARNING_REASON_INV"))

# --- HEX enums (from ve_reg_enum.h + official docs) ---
DEVICE_MODE_MAP = {0: "off", 1: "charger_on", 2: "inverter_on", 3: "device_on",
                   4: "device_off", 5: "eco", 0xFD: "hibernate"}
# Orion-XS-HEX device mode values (register 0x0200): 0/4 Charger off,
# 1/3 Charger on. Separate map so Orion 3/4 decode as documented instead of
# the generic "device_on"/"device_off".
DEVICE_MODE_ORION_MAP = {0: "off", 1: "on", 3: "on", 4: "off"}
# Phoenix-Inverter-HEX Rev 5 §2.4.2 device mode values: 2,3,4,5,0xFD only
# (generic values 0/1 never occur on Phoenix and decode unknown here).
DEVICE_MODE_INV_MAP = {2: "inverter_on", 3: "device_on", 4: "device_off",
                       5: "eco", 0xFD: "hibernate"}
DEVICE_FUNCTION_MAP = {0: "charger", 1: "psu"}
# Generic HEX 0x0201 map. State 0 is NOT_CHARGING per BlueSolar-HEX §1.1
# Device-state-values and Orion-XS-HEX Device-state-values (both list 0 as
# NOT_CHARGING; LINK_DEVICE_STATE and the ORION override agree). Phoenix Rev 5
# §2.3.1 defines 0 as Off, but that family resolves via the INV override
# (DEVICE_STATE_INV_MAP[0]="off"), so the generic rename is safe there.
# 245 is WAKE-UP per BlueSolar-HEX §1.1 device-state
# values and Orion-XS-HEX device-state values (the LINK_DEVICE_STATE and
# ORION override maps agree); Text-3.34 CS "Starting-up" is a separate
# Text-mode map (tables.py) and does not apply here.
DEVICE_STATE_MAP = {0: "not_charging", 1: "low_power", 2: "fault", 3: "bulk", 4: "absorption",
                    5: "floating", 6: "storage", 7: "equalize_manual", 8: "passthru",
                    9: "inverting", 0x0A: "assisting", 0x0B: "power_supply",
                    0xF4: "sustain", 0xF5: "wake_up", 0xF6: "repeated_absorption",
                    0xF7: "auto_equalize", 0xF8: "battery_safe", 250: "blocked", 0xFC: "external_control",
                    0xFF: "unknown"}
# 0x200C LINK map: 250 BLOCKED exists only in the 0x0201 tables — both the
# BlueSolar-HEX and Orion-XS-HEX Link-device-state (0x200C) tables list
# 0,2,3,4,5,6,7,11,245,246,247,248,249,252,255 with no 250, so 250 decodes
# unknown(250) here.
LINK_DEVICE_STATE_MAP = {0: "not_charging", 2: "fault", 3: "bulk", 4: "absorption",
                         5: "float", 6: "storage", 7: "manual_equalise",
                         11: "power_supply", 245: "wake_up", 246: "repeated_absorption",
                         247: "auto_equalize", 248: "battery_safe", 249: "load_detect",
                         252: "external_control", 255: "unavailable"}
MPPT_TRACKER_MODE_MAP = {0: "off", 1: "v_i_limited", 2: "mppt"}
RELAY_MODE_MAP = {0: "warnings", 1: "charger", 2: "remote", 3: "disabled", 4: "normal", 5: "battery_low", 6: "fan"}
# BMV-7xx-HEX Rev 7 Relay settings: Un8 0..2 (DFLT, CHRG, REM). Separate map
# so BMV value 0 decodes as documented ("default") instead of the
# Phoenix-family "warnings", and values 3..6 are rejected on encode.
RELAY_MODE_BMV_MAP = {0: "default", 1: "charger", 2: "remote"}
# Phoenix-Inverter-HEX Rev 5 §2.8.1 documents relay values 0,2,3,4,5,6;
# value 1 has no Phoenix meaning and decodes unknown here.
RELAY_MODE_INV_MAP = {0: "warnings", 2: "remote", 3: "disabled", 4: "normal",
                      5: "battery_low", 6: "fan"}
RELAY_OPERATION_MODE_MAP = {0: "always_off", 1: "panel_high", 2: "temp_high",
                            3: "battery_low", 4: "equalisation_active", 5: "error",
                            6: "temp_low", 7: "battery_high", 8: "float_or_storage",
                            9: "day_detection", 10: "load_control"}
# Values 0 (auto detect) and 36 defined by BlueSolar-HEX battery-voltage
# setting table; the Orion-XS-HEX table lists 12/24/48 only, so the shared
# map stays a superset and Orion devices simply never report 0/36.
BAT_VOLTAGE_SETTING_MAP = {0: "auto_detect", 12: "v12", 24: "v24", 36: "v36", 48: "v48"}
CHR_ERROR_CODE_MAP = {0: "no_error", 1: "battery_temperature_high", 2: "battery_voltage_high",
                      3: "remote_temperature_a", 4: "remote_temperature_b", 5: "remote_temperature_c",
                      6: "remote_voltage_a", 7: "remote_voltage_b", 8: "remote_voltage_c",
                      11: "high_ripple", 14: "battery_temperature_low", 17: "charger_temperature_high",
                      18: "over_current",
                      # BlueSolar-HEX charger error table: Error 19 Charger
                      # current polarity reversed — disabled since fw 1.15
                      # ("can be safely ignored"), kept so raw 19 decodes.
                      19: "charger_current_polarity_reversed",
                      20: "bulk_time", 21: "current_sensor",
                      22: "internal_temperature_a", 23: "internal_temperature_b", 24: "fan",
                      26: "overheated", 27: "short_circuit", 28: "converter_issue", 29: "over_charge",
                      33: "input_voltage", 34: "input_current",
                      35: "input_power", 38: "input_shutdown_voltage", 39: "input_shutdown_current",
                      40: "input_shutdown_failure", 41: "inverter_shutdown_41", 42: "inverter_shutdown_42",
                      43: "inverter_shutdown_43", 50: "inverter_overload", 51: "inverter_temperature",
                      52: "inverter_peak_current", 53: "inverter_ouput_voltage_a", 54: "inverter_ouput_voltage_b",
                      55: "inverter_self_test_a", 56: "inverter_self_test_b", 57: "inverter_ac",
                      58: "inverter_self_test_c",
                      65: "communication", 66: "synchronisation", 67: "bms", 68: "network_a",
                      69: "network_b", 70: "network_c", 71: "network_d",
                      80: "pv_input_shutdown_80", 81: "pv_input_shutdown_81", 82: "pv_input_shutdown_82",
                      83: "pv_input_shutdown_83", 84: "pv_input_shutdown_84", 85: "pv_input_shutdown_85",
                      86: "pv_input_shutdown_86", 87: "pv_input_shutdown_87",
                      114: "cpu_temperature_high",
                      116: "calibration_lost", 117: "invalid_firmware", 119: "settings_lost",
                      120: "reference_voltage_failure", 121: "tester_fail",
                      150: "battery_temperature_high_warn", 151: "battery_temperature_low_warn",
                      160: "charger_overtemperature_derating", 161: "short_circuit_detected",
                      162: "converter_issue_warn",
                      200: "internal_dc_voltage_a", 201: "internal_dc_voltage_b", 202: "self_test",
                      203: "internal_supply_voltage_a", 205: "internal_supply_voltage_b",
                      212: "internal_supply_voltage_c", 215: "internal_supply_voltage_d",
                      0xFF: "unknown"}
# Orion-XS-HEX device state values (register 0x0201): 0 NOT_CHARGING,
# 11 PSU, 245 WAKE-UP, 255 UNAVAILABLE, ... Names follow LINK_DEVICE_STATE.
DEVICE_STATE_ORION_MAP = {0: "not_charging", 2: "fault", 3: "bulk",
                          4: "absorption", 5: "float", 6: "storage",
                          7: "manual_equalise", 11: "power_supply",
                          245: "wake_up", 246: "repeated_absorption",
                          247: "auto_equalize", 248: "battery_safe",
                          250: "blocked", 252: "external_control",
                          255: "unavailable"}
# Phoenix-Inverter-HEX Rev 5 §2.3.1 device state values: 0,1,2,9 only
# (generic charger states like bulk/absorption never occur on Phoenix).
DEVICE_STATE_INV_MAP = {0: "off", 1: "low_power", 2: "fault", 9: "inverting"}
WARNING_REASON_BITS = {0: "low_battery_voltage", 1: "high_battery_voltage", 2: "low_soc",
                       3: "low_starter_voltage", 4: "high_starter_voltage", 5: "low_temperature",
                       6: "high_temperature", 7: "mid_battery_voltage", 8: "overload",
                       9: "dc_ripple", 10: "low_ac_out_voltage", 11: "high_ac_out_voltage",
                       12: "short_circuit", 13: "bms_lockout"}
# Phoenix-Inverter-HEX Rev 5 §2.3.3 warning/alarm bits: 0,1,5,6,8,9,10,11
# only. Other positions are undocumented for Phoenix and decode as bitN here
# instead of inheriting MPPT/BMV-family names.
WARNING_REASON_INV_BITS = {0: "low_battery_voltage", 1: "high_battery_voltage",
                           5: "low_temperature", 6: "high_temperature",
                           8: "overload", 9: "dc_ripple",
                           10: "low_ac_out_voltage", 11: "high_ac_out_voltage"}
OFF_REASON_MPPT_BITS = {
    0: "no_input_power", 1: "physical_power_switch", 2: "soft_power_switch",
    3: "remote_input", 4: "internal_alarm", 5: "paygo", 6: "bms_shutdown",
    # BlueSolar-HEX §1.1: bits 7..8 Reserved, bit 9 Battery temperature too
    # low. (No bit 7 "engine" — that name belongs to the Orion map below.)
    9: "battery_temperature",
}
OFF_REASON_ORION_BITS = {
    0: "no_input_power", 2: "soft_power_switch", 3: "remote_input",
    4: "internal_reason", 5: "paygo", 6: "bms_shutdown",
    7: "engine_shutdown", 8: "error",
}
OFF_REASON_INV_BITS = {
    0: "no_input_power", 2: "soft_power_switch", 3: "remote_input",
    4: "internal_reason", 5: "paygo",
}
BLE_MODE_MAP = {0: "off", 1: "on"}
CAPABILITIES_MAP = {
    0: "load_output", 1: "rotary_encoder", 2: "history", 3: "batterysafe",
    4: "adaptive", 5: "manual_equalise", 6: "automatic_equalise", 7: "storage",
    8: "remote_on_off", 9: "solar_timer", 10: "alt_tx_pin", 11: "user_load_switch",
    12: "load_current_text", 13: "panel_current", 14: "bms", 15: "external_control",
    16: "synchronized_charging", 17: "relay", 18: "alt_rx_pin",
    19: "virtual_load_output", 20: "virtual_relay",
    21: "plugin_display", 25: "aes", 26: "battery_test", 27: "paygo",
    28: "hibernate", 29: "ac_apparent_power", 30: "psu_function",
    31: "needs_battery_to_shutdown",
}
CAPABILITIES_BLE_MAP = {
    # Decision: keep Orion-family bit names 2-8 on the shared ANY register
    # instead of scoping them out of the BMV path. BMV Rev 7 §Bluetooth
    # capabilities reserves bits 2..31 (only 0 HAS_SUPPORT, 1 PERMANENT) and
    # Phoenix Rev 5 §2.2.5 likewise documents only bits 0-1, while
    # Orion-XS-HEX §BLE capabilities names bits 0-8 (9..31 not defined).
    # Reserved bits read 0 on BMV/Phoenix hardware, so the extra names never
    # misdecode there — they are best-effort for Orion and harmless elsewhere.
    # A BMV-only override would force lookup(0x0150) ambiguous (like 0x0201)
    # with no wire-behavior gain, so the single ANY entry is kept.
    0: "ble_mode", 1: "permanent_off", 2: "ble_off_recovery_time",
    3: "recovery_time_zero_permanent", 4: "service_mode", 5: "trends",
    6: "ad_key", 7: "soc_sync_via_trend", 8: "settings_lock",
}
INV_NVM_COMMAND_MAP = {0: "no_command", 1: "save", 2: "revert", 3: "backup", 4: "default"}
INV_AC_FREQUENCY_MAP = {0: "60Hz", 1: "50Hz"}
BMV_TEMPERATURE_UNIT_MAP = {0: "celsius", 1: "fahrenheit"}
BMV_AUXILIARY_INPUT_MAP = {0: "starter_battery", 1: "mid_voltage", 2: "temperature"}
TWO_WIRE_BMS_MAP = {0: "two_wire_bms_enabled", 1: "allow_to_discharge", 2: "allow_to_charge"}
REMOTE_CONTROL_USED_MAP = {1: "remote_on_off"}
ADDITIONAL_CHARGER_STATE_MAP = {0: "safe_mode", 1: "auto_equalise", 4: "temp_dimming",
                                6: "input_current_dimming"}
LOAD_OUTPUT_CONTROL_MAP = {0: "off", 1: "auto", 2: "alt1", 3: "alt2", 4: "on",
                           5: "user1", 6: "user2", 7: "aes"}
LOAD_OUTPUT_OFF_REASON_MAP = {0: "battery_low", 1: "short_circuit", 2: "timer",
                              3: "remote_input", 4: "paygo", 7: "starting_up"}
TX_PORT_MODE_MAP = {0: "normal", 1: "pulse_001kwh", 2: "pwm_normal", 3: "pwm_inverted",
                    4: "virtual_load"}
RX_PORT_MODE_MAP = {0: "remote_on_off", 1: "load_config", 2: "load_on_off_inverted",
                    3: "load_on_off"}
DISPLAY_BACKLIGHT_MODE_MAP = {0: "keypress", 1: "on", 2: "auto"}
DISPLAY_BACKLIGHT_MODE_RS_MAP = {0: "off", 1: "on", 2: "auto"}
REMOTE_COMMAND_MAP = {1: "start_equalise", 2: "stop_equalise", 3: "sync_ui", 4: "sync_day"}
NETWORK_INFO_MAP = {0: "bms_controlled", 1: "vset_remote", 2: "charge_slave",
                    3: "charge_master", 4: "using_icharge", 5: "using_isense",
                    6: "using_tsense", 7: "using_vsense"}
# NOTE: BlueSolar-HEX lists a bit 8 STANDBY for 0x200D while typing the
# register un8 — self-inconsistent, bit 8 is undecodable on the wire (a Get
# reply carries 1 byte and decode masks to the dtype width). Dtype stays
# uint8_t per the doc table (widening would also break the sim's reply
# width); bit 8 decodes as bit8 via the generic unknown-bit fallback.
NETWORK_MODE_MAP = {0: "networked", 1: "slave", 2: "external_control", 3: "bms",
                    4: "charge_group_master", 5: "charge_instance_master", 6: "standby"}
TIMER_ANCHOR_MAP = {1: "sunset", 2: "midnight", 3: "sunrise"}
# BAT_TYPE depends on charger class (doc typo: 30A+ table duplicates value 2).
BAT_TYPE_SMALL_MAP = {1: "type_2", 0xFF: "user"}
BAT_TYPE_LARGE_MAP = {1: "type_1", 2: "type_2", 3: "type_4", 5: "type_5",
                      6: "type_6", 7: "type_7", 8: "type_8", 0xFF: "user"}
BAT_TYPE_RS_MAP = {1: "agm_normal", 2: "agm_high", 3: "lithium", 0xFF: "user"}
BAT_TYPE_ORION_MAP = {1: "lifepo4", 0xFF: "user"}

ENUM_MAPS: dict[str, dict[int, str]] = {
    "DEVICE_MODE": DEVICE_MODE_MAP,
    "DEVICE_MODE_ORION": DEVICE_MODE_ORION_MAP,
    "DEVICE_MODE_INV": DEVICE_MODE_INV_MAP,
    "DEVICE_FUNCTION": DEVICE_FUNCTION_MAP,
    "DEVICE_STATE": DEVICE_STATE_MAP,
    "DEVICE_STATE_ORION": DEVICE_STATE_ORION_MAP,
    "DEVICE_STATE_INV": DEVICE_STATE_INV_MAP,
    "LINK_DEVICE_STATE": LINK_DEVICE_STATE_MAP,
    "MPPT_TRACKER_MODE": MPPT_TRACKER_MODE_MAP,
    "RELAY_MODE": RELAY_MODE_MAP,
    "RELAY_MODE_BMV": RELAY_MODE_BMV_MAP,
    "RELAY_MODE_INV": RELAY_MODE_INV_MAP,
    "RELAY_OPERATION_MODE": RELAY_OPERATION_MODE_MAP,
    "BAT_VOLTAGE_SETTING": BAT_VOLTAGE_SETTING_MAP,
    "CHR_ERROR_CODE": CHR_ERROR_CODE_MAP,
    "BAT_TYPE": {0xFF: "user"},
    "BAT_TYPE_SMALL": BAT_TYPE_SMALL_MAP,
    "BAT_TYPE_LARGE": BAT_TYPE_LARGE_MAP,
    "BAT_TYPE_RS": BAT_TYPE_RS_MAP,
    "BAT_TYPE_ORION": BAT_TYPE_ORION_MAP,
    "MON": MON_MAP,
    "REMOTE_INPUT_MODE_CONFIG": {0: "remote_on_off", 1: "two_wire_bms"},
    "BLE_MODE": BLE_MODE_MAP,
    "CAPABILITIES": CAPABILITIES_MAP,
    "CAPABILITIES_BLE": CAPABILITIES_BLE_MAP,
    "INV_NVM_COMMAND": INV_NVM_COMMAND_MAP,
    "INV_AC_FREQUENCY": INV_AC_FREQUENCY_MAP,
    "BMV_TEMPERATURE_UNIT": BMV_TEMPERATURE_UNIT_MAP,
    "BMV_AUXILIARY_INPUT": BMV_AUXILIARY_INPUT_MAP,
    "OFF_REASON_MPPT": OFF_REASON_MPPT_BITS,
    "OFF_REASON_ORION": OFF_REASON_ORION_BITS,
    "OFF_REASON_INV": OFF_REASON_INV_BITS,
    "WARNING_REASON": WARNING_REASON_BITS,
    "WARNING_REASON_INV": WARNING_REASON_INV_BITS,
    "TWO_WIRE_BMS": TWO_WIRE_BMS_MAP,
    "REMOTE_CONTROL_USED": REMOTE_CONTROL_USED_MAP,
    "ADDITIONAL_CHARGER_STATE": ADDITIONAL_CHARGER_STATE_MAP,
    "LOAD_OUTPUT_CONTROL": LOAD_OUTPUT_CONTROL_MAP,
    "LOAD_OUTPUT_OFF_REASON": LOAD_OUTPUT_OFF_REASON_MAP,
    "TX_PORT_MODE": TX_PORT_MODE_MAP,
    "RX_PORT_MODE": RX_PORT_MODE_MAP,
    "DISPLAY_BACKLIGHT_MODE": DISPLAY_BACKLIGHT_MODE_MAP,
    "DISPLAY_BACKLIGHT_MODE_RS": DISPLAY_BACKLIGHT_MODE_RS_MAP,
    "REMOTE_COMMAND": REMOTE_COMMAND_MAP,
    "NETWORK_INFO": NETWORK_INFO_MAP,
    "NETWORK_MODE": NETWORK_MODE_MAP,
    "TIMER_ANCHOR": TIMER_ANCHOR_MAP,
}

_DTYPE_STRUCT: dict[str, str] = {
    "uint8_t": "B", "int8_t": "b", "uint16_t": "<H", "int16_t": "<h",
    "uint32_t": "<I", "int32_t": "<i",
}

# Raw sentinels meaning "no data" (decode -> None, encode None -> sentinel).
_NONE_SENTINELS: dict[int, set[int]] = {
    0xEDEC: {0xFFFF},
    0xEDE6: {0xFFFF},  # LOW_TEMP_CHARGE_CURRENT: 0xFFFF = use maximum charge current
    0x2031: {0xFFFF},
    0x2002: {0xFFFF},
    0x2003: {0x7FFF},
    0xD18D: {0x7FFFFFFF},
    0xEC41: {0xFFFFFFFF},
    # BMV Rev 7 Monitor/Relay tables: 0=disabled (valid 174..372 K).
    0x032A: {0},
    0x032B: {0},
    0x032C: {0},
    0x032D: {0},
    0x035A: {0},
    0x035B: {0},
    0x035C: {0},
    0x035D: {0},
}


def decode_bat_type(value: int, charger: str = "large") -> str:
    """Device-specific BAT_TYPE decode (doc tables differ per charger class).

    charger: "small" (10/15/20A no rotary), "large" (30A+ rotary, default),
    "rs" (MPPT RS), "orion" (Orion XS). Unknown values -> "unknown(n)".
    """
    m = {"small": BAT_TYPE_SMALL_MAP, "large": BAT_TYPE_LARGE_MAP,
         "rs": BAT_TYPE_RS_MAP, "orion": BAT_TYPE_ORION_MAP}.get(charger.lower(), BAT_TYPE_LARGE_MAP)
    return m.get(value, f"unknown({value})")


def lookup(name_or_id: str | int, flavor: str | None = None) -> HexRegister:
    """Look up a register by uppercase name or numeric ID.

    For aliased register IDs (IDs shared by multiple device flavors with different
    names, scales, or data types, e.g. 0xED8E), ``flavor`` is required to safely
    select the intended register definition. If ambiguous and no flavor is passed,
    raises :exc:`AmbiguousRegisterError`.
    """
    if isinstance(name_or_id, str):
        reg = REGISTERS.get(name_or_id.upper())
        if reg is None:
            raise KeyError(f"unknown HEX register {name_or_id!r}")
        if flavor is None:
            return reg
        # A generic name may share its wire ID with a more specific product
        # definition (different scale, access, class, or enum).  Resolve by ID
        # so the selected flavor always wins over an overly broad alias.
        name_or_id = reg.id

    if isinstance(name_or_id, int):
        candidates = _REGISTERS_BY_ID.get(name_or_id)
        if not candidates:
            raise KeyError(f"unknown HEX register 0x{name_or_id:04X}")

        if flavor is not None:
            f = flavor.upper()
            allowed = _FLAVOR_EXPAND.get(f, {f, "ANY"})
            matching = [r for r in candidates if allowed & set(r.flavors)]
            if matching:
                # Prefer exact, narrowly-scoped product metadata, then inherited
                # metadata, and use ANY only as a final fallback.
                matching.sort(key=lambda r: (
                    0 if f in r.flavors else 1,
                    1 if "ANY" in r.flavors else 0,
                    len(r.flavors),
                ))
                return matching[0]
            raise KeyError(f"register 0x{name_or_id:04X} not available for flavor {flavor!r}")

        if len(candidates) == 1:
            return candidates[0]

        distinct = {(r.dtype, r.scale, r.unit, r.access, r.cls, r.enum) for r in candidates}
        if len(distinct) > 1 or len(candidates) > 1:
            names = ", ".join(f"{r.name} ({'/'.join(r.flavors)})" for r in candidates)
            raise AmbiguousRegisterError(
                f"register ID 0x{name_or_id:04X} is ambiguous across flavors: [{names}]; specify flavor or register name"
            )
        return candidates[0]

    raise TypeError(f"expected str or int for name_or_id, got {type(name_or_id).__name__}")


def _u16(b: bytes, off: int) -> int | None:
    if off + 2 > len(b):
        return None
    import struct as _s
    return _s.unpack_from("<H", b, off)[0]


def _u32(b: bytes, off: int) -> int | None:
    if off + 4 > len(b):
        return None
    import struct as _s
    return _s.unpack_from("<I", b, off)[0]


def _none_if(raw: int | None, bad: set[int]) -> int | None:
    return None if raw is None or raw in bad else raw


def decode_timer_event(raw: bytes) -> dict:
    """0xEDA0..0xEDA5 un32: sn16 offset(min) + u8 anchor + u8 dim(0..100)."""
    import struct as _s
    if len(raw) < 4:
        return {"raw": raw.hex().upper()}
    off = _s.unpack_from("<h", raw, 0)[0]
    anchor = raw[2]
    dim = raw[3]
    amap = ENUM_MAPS.get("TIMER_ANCHOR", {})
    return {"offset_min": off, "anchor": anchor,
            "anchor_name": amap.get(anchor, f"unknown({anchor})"), "dim_pct": dim}


def encode_timer_event(value: dict | bytes | int) -> bytes:
    import struct as _s
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:4]).ljust(4, b"\x00")
    if isinstance(value, int):
        return _s.pack("<I", value & 0xFFFFFFFF)
    off = int(value.get("offset_min", 0))
    anchor = int(value.get("anchor", 1))
    dim = int(value.get("dim_pct", value.get("dim", 0)))
    return _s.pack("<hBB", off, anchor & 0xFF, dim & 0xFF)


def decode_voltage_range(raw: bytes) -> dict:
    """0xEDCE: low byte min system V, high byte max system V (1V units)."""
    if len(raw) < 2:
        return {"raw": raw.hex().upper()}
    return {"min_V": raw[0], "max_V": raw[1]}


def encode_voltage_range(value: dict | bytes | int) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:2]).ljust(2, b"\x00")
    if isinstance(value, int):
        return bytes([value & 0xFF, (value >> 8) & 0xFF])
    return bytes([int(value.get("min_V", value.get("min", 0))) & 0xFF,
                  int(value.get("max_V", value.get("max", 0))) & 0xFF])


def decode_load_control(raw: bytes) -> dict:
    """0xEDAB: low nibble mode 0..7 + bit7 lighting timer."""
    if not raw:
        return {"raw": ""}
    v = raw[0]
    mmap = ENUM_MAPS.get("LOAD_OUTPUT_CONTROL", {})
    return {"mode": v & 0x0F, "mode_name": mmap.get(v & 0x0F, f"unknown({v & 0x0F})"),
            "lighting_timer": bool(v & 0x80), "raw": v}


def encode_load_control(value: dict | bytes | int | str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:1])
    if isinstance(value, int):
        return bytes([value & 0xFF])
    if isinstance(value, str):
        rev = {v: k for k, v in ENUM_MAPS.get("LOAD_OUTPUT_CONTROL", {}).items()}
        return bytes([rev.get(value, 0) & 0xFF])
    mode = int(value.get("mode", 0)) & 0x0F
    if value.get("lighting_timer"):
        mode |= 0x80
    return bytes([mode])


def decode_network_status(raw: bytes) -> dict:
    """0x200F: low nibble charge-algorithm feedback + bits 4..7 sense flags."""
    if not raw:
        return {"raw": ""}
    v = raw[0]
    mode = v & 0x0F
    mmap = {0: "slave", 1: "group_master", 2: "instance_master",
            3: "group_and_instance_master", 4: "standalone"}
    return {"mode": mode, "mode_name": mmap.get(mode, f"unknown({mode})"),
            "using_icharge": bool(v & 0x10), "using_isense": bool(v & 0x20),
            "using_tsense": bool(v & 0x40), "using_vsense": bool(v & 0x80),
            "raw": v}


def encode_network_status(value: dict | bytes | int) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:1])
    if isinstance(value, int):
        return bytes([value & 0xFF])
    v = int(value.get("mode", 4)) & 0x0F
    if value.get("using_icharge"):
        v |= 0x10
    if value.get("using_isense"):
        v |= 0x20
    if value.get("using_tsense"):
        v |= 0x40
    if value.get("using_vsense"):
        v |= 0x80
    return bytes([v])


def _scale_or_none(raw: int | None, scale: float, bad: set[int]) -> float | int | None:
    if raw is None or raw in bad:
        return None
    v = raw * scale
    return v


def decode_history_total(raw: bytes) -> dict:
    """0x104F: 19B (fw<=1.16) or 34B (fw>=1.17). Unavailable -> None."""
    import struct as _s
    out: dict[str, float | int | None] = {}
    if len(raw) < 19:
        return {"raw": raw.hex().upper()}
    out["reserved"] = raw[0]
    out["error_db"] = raw[1]
    out["error_0"] = raw[2]
    out["error_1"] = raw[3]
    out["error_2"] = raw[4]
    out["error_3"] = raw[5]
    out["yield_user_kwh"] = _s.unpack_from("<I", raw, 6)[0] * 0.01
    out["yield_system_kwh"] = _s.unpack_from("<I", raw, 10)[0] * 0.01
    out["panel_max_V"] = _s.unpack_from("<H", raw, 14)[0] * 0.01
    out["battery_max_V"] = _s.unpack_from("<H", raw, 16)[0] * 0.01
    if len(raw) >= 19:
        out["days_available"] = raw[18]
    if len(raw) >= 34:
        out["battery_min_V"] = _s.unpack_from("<H", raw, 19)[0] * 0.01
    return out


def decode_history_day(raw: bytes) -> dict:
    """0x1050..0x106E: 34B day record. 0xFFFFFFFF consumed -> None (no load)."""
    import struct as _s
    if len(raw) < 34:
        return {"raw": raw.hex().upper()}
    yld = _s.unpack_from("<I", raw, 1)[0]
    con = _s.unpack_from("<I", raw, 5)[0]
    return {
        "yield_kwh": yld * 0.01,
        "consumed_kwh": None if con == 0xFFFFFFFF else con * 0.01,
        "battery_max_V": _s.unpack_from("<H", raw, 9)[0] * 0.01,
        "battery_min_V": _s.unpack_from("<H", raw, 11)[0] * 0.01,
        "error_0": raw[14], "error_1": raw[15], "error_2": raw[16], "error_3": raw[17],
        "time_bulk_min": _s.unpack_from("<H", raw, 18)[0],
        "time_absorption_min": _s.unpack_from("<H", raw, 20)[0],
        "time_float_min": _s.unpack_from("<H", raw, 22)[0],
        "power_max_W": _s.unpack_from("<I", raw, 24)[0],
        "battery_max_A": _s.unpack_from("<H", raw, 28)[0] * 0.1,
        "panel_max_V": _s.unpack_from("<H", raw, 30)[0] * 0.01,
        "day_seq": _s.unpack_from("<H", raw, 32)[0],
    }


def decode_history_mppt_day(raw: bytes) -> dict:
    """0x10A0..0x10BE: 36B per-tracker day record. 0xFFFF -> None."""
    import struct as _s
    if len(raw) < 36:
        return {"raw": raw.hex().upper()}
    def _e(off: int) -> float | None:
        v = _s.unpack_from("<H", raw, off)[0]
        return None if v == 0xFFFF else v * 0.01
    def _p(off: int) -> int | None:
        v = _s.unpack_from("<H", raw, off)[0]
        return None if v == 0xFFFF else v
    def _v(off: int) -> float | None:
        v = _s.unpack_from("<H", raw, off)[0]
        return None if v == 0xFFFF else v * 0.01
    return {
        "day_seq": _s.unpack_from("<H", raw, 1)[0],
        "energy_1_kwh": _e(3), "energy_2_kwh": _e(5),
        "energy_3_kwh": _e(7), "energy_4_kwh": _e(9),
        "peak_1_W": _p(11), "peak_2_W": _p(13),
        "peak_3_W": _p(15), "peak_4_W": _p(17),
        "voc_max_1_V": _v(19), "voc_max_2_V": _v(21),
        "voc_max_3_V": _v(23), "voc_max_4_V": _v(25),
    }


def decode_cumulative_history(raw: bytes) -> dict:
    """Orion 0x1042/0x1043: version + operation time + charged + counters."""
    import struct as _s
    if len(raw) < 25:
        return {"raw": raw.hex().upper()}
    return {
        "version": raw[0],
        "operation_time_s": _s.unpack_from("<I", raw, 1)[0],
        "charged_Ah": _s.unpack_from("<I", raw, 5)[0] * 0.1,
        "cycles_started": _s.unpack_from("<I", raw, 9)[0],
        "cycles_completed": _s.unpack_from("<I", raw, 13)[0],
        "power_ups": _s.unpack_from("<I", raw, 17)[0],
        "deep_discharges": _s.unpack_from("<I", raw, 21)[0],
    }


def decode_cycle_history(raw: bytes) -> dict:
    """Orion 0x1070..0x1098: per-cycle record. 0xFF.. -> None."""
    import struct as _s
    if len(raw) < 51:
        return {"raw": raw.hex().upper()}
    def _u32(off: int) -> int | None:
        v = _s.unpack_from("<I", raw, off)[0]
        return None if v == 0xFFFFFFFF else v
    def _u16(off: int) -> int | None:
        v = _s.unpack_from("<H", raw, off)[0]
        return None if v == 0xFFFF else v
    def _u8(off: int) -> int | None:
        v = raw[off]
        return None if v == 0xFF else v
    return {
        "version": raw[0],
        "start_time_s": _u32(1),
        "time_bulk_s": _u32(5), "time_absorption_s": _u32(9),
        "time_recondition_s": _u32(13), "time_float_s": _u32(17),
        "time_storage_s": _u32(21),
        "ah_bulk": None if _u32(25) is None else _u32(25) * 0.1,  # type: ignore[operator]
        "ah_absorption": None if _u32(29) is None else _u32(29) * 0.1,  # type: ignore[operator]
        "ah_recondition": None if _u32(33) is None else _u32(33) * 0.1,  # type: ignore[operator]
        "ah_float": None if _u32(37) is None else _u32(37) * 0.1,  # type: ignore[operator]
        "ah_storage": None if _u32(41) is None else _u32(41) * 0.1,  # type: ignore[operator]
        "voltage_start_V": None if _u16(45) is None else _u16(45) * 0.01,  # type: ignore[operator]
        "voltage_end_V": None if _u16(47) is None else _u16(47) * 0.01,  # type: ignore[operator]
        "battery_type": _u8(49), "error": _u8(50),
    }


def decode_cycle_sequence(raw: bytes) -> dict:
    """Orion 0x1099: start time of current charge cycle (un32 s)."""
    import struct as _s
    if len(raw) < 4:
        return {"raw": raw.hex().upper()}
    return {"start_time_s": _s.unpack_from("<I", raw, 0)[0]}


def decode_product_id(raw: bytes) -> dict:
    """Decode product-id register layouts used by documented families."""
    b = bytes(raw)
    if len(b) >= 4 and b[0] == 0 and b[3] in (0xFE, 0xFF):
        return {
            "product_id": int.from_bytes(b[1:3], "little"),
            "instance": b[0],
            "reserved": b[3],
            "raw": b.hex().upper(),
        }
    if len(b) >= 2:
        return {
            "product_id": int.from_bytes(b[:4], "little"),
            "raw": b.hex().upper(),
        }
    return {"product_id": None, "raw": b.hex().upper()}


def decode_app_version_register(raw: bytes) -> dict:
    """Decode 16-bit or identifier+24-bit firmware-version registers."""
    from ..hex import decode_app_version, decode_app_version_24
    b = bytes(raw)
    if len(b) >= 4 and b[0] == 0:
        return decode_app_version_24(b[:4])
    return decode_app_version(b)


def decode_product_revision(raw: bytes) -> dict:
    """Phoenix product revision: zero identifier followed by LE un16."""
    b = bytes(raw)
    if len(b) < 3:
        return {"revision": None, "raw": b.hex().upper()}
    return {
        "identifier": b[0],
        "revision": int.from_bytes(b[1:3], "little"),
        "raw": b.hex().upper(),
    }


def decode_bmv_sw_version(raw: bytes) -> dict:
    """BMV 0xEEF9: Un16 HEX (AA.BB) per BMV-7xx-HEX Rev 7 Miscellaneous table.

    High byte AA, low byte BB (minor as hex digits, same convention as
    decode_app_version): raw 0x0407 -> version "4.07". Raw word kept in
    "raw" for exact round-trips.
    """
    b = bytes(raw)
    if len(b) < 2:
        return {"raw": b.hex().upper(), "version": None}
    v = int.from_bytes(b[:2], "little")
    return {"raw": v, "version": f"{v >> 8}.{(v & 0xFF):02X}"}


def encode_bmv_sw_version(value: dict | bytes | int | str) -> bytes:
    """Encode BMV SW version: raw int, {"raw": int}, "AA.BB", or raw bytes."""
    import struct as _s
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:2]).ljust(2, b"\x00")
    if isinstance(value, int):
        return _s.pack("<H", value & 0xFFFF)
    if isinstance(value, str):
        s = value.strip()
        if "." in s:
            hi_s, lo_s = s.split(".", 1)
            return _s.pack("<H", ((int(hi_s, 0) & 0xFF) << 8) | (int(lo_s, 16) & 0xFF))
        return _s.pack("<H", int(s, 0) & 0xFFFF)
    if isinstance(value, dict):
        if "raw" in value:
            return _s.pack("<H", int(value["raw"]) & 0xFFFF)
        if "version" in value:
            return encode_bmv_sw_version(value["version"])
    raise ValueError(f"cannot encode BMV_SW_VERSION from {value!r}")


_STRUCT_DECODERS = {
    "PRODUCT_ID": decode_product_id,
    "BMV_SW_VERSION": decode_bmv_sw_version,
    "APP_VERSION": decode_app_version_register,
    "PRODUCT_REVISION": decode_product_revision,
    "HISTORY_TOTAL": decode_history_total,
    "HISTORY_DAY": decode_history_day,
    "HISTORY_MPPT_DAY": decode_history_mppt_day,
    "CUMULATIVE_HISTORY": decode_cumulative_history,
    "CYCLE_HISTORY": decode_cycle_history,
    "CYCLE_SEQUENCE": decode_cycle_sequence,
    "TIMER_EVENT": decode_timer_event,
    "VOLTAGE_RANGE": decode_voltage_range,
    "LOAD_CONTROL": decode_load_control,
    "NETWORK_STATUS": decode_network_status,
}


def decode_value(reg: HexRegister, raw: bytes) -> float | int | str | bool | list[str] | dict | None:
    """Decode LE payload bytes to scaled value / enum name / bool / bit names / dict."""
    if reg.cls == "STRING":
        return raw.split(b"\x00")[0].decode("ascii", errors="replace")
    if reg.cls == "BOOLEAN":
        if not raw:
            return False
        return bool(int.from_bytes(raw[:1], "little"))
    if reg.cls == "VOID":
        return raw.hex().upper()
    if reg.cls == "STRUCT":
        fn = _STRUCT_DECODERS.get(reg.enum or "")
        if fn is not None:
            try:
                return fn(bytes(raw))
            except Exception:
                return {"raw": bytes(raw).hex().upper()}
        return bytes(raw).hex().upper()
    if reg.cls == "ENUM":
        if not raw:
            return "unknown(empty)"
        signed = reg.dtype.startswith("int")
        width = {"int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2, "int32_t": 4, "uint32_t": 4}.get(reg.dtype, 1)
        if len(raw) < width:
            return "unknown(empty)"
        v = int.from_bytes(raw[:width], "little", signed=signed)
        return ENUM_MAPS.get(reg.enum or "", {}).get(v, f"unknown({v})")
    if reg.cls == "BITMASK":
        width = {"uint8_t": 1, "uint16_t": 2, "uint32_t": 4}.get(reg.dtype, len(raw))
        value = int.from_bytes(raw[:width], "little") if raw else 0
        names = ENUM_MAPS.get(reg.enum or "", {})
        if not names:
            return value
        # Bit-position semantics: bit N set -> names[N]. Value 0 -> [] (no bits).
        out = [names[bit] for bit in sorted(names) if value & (1 << bit)]
        unknown = value & ~sum(1 << bit for bit in names)
        bit = 0
        while unknown:
            if unknown & 1:
                out.append(f"bit{bit}")
            unknown >>= 1
            bit += 1
        return out
    if reg.dtype == "uint24_t":
        if len(raw) < 3:
            return raw.hex().upper()
        ival = int.from_bytes(raw[:3], "little")
        bad = _NONE_SENTINELS.get(reg.id, set())
        if ival in bad:
            return None
        return int(ival * reg.scale) if reg.scale == 1.0 else ival * reg.scale
    if reg.dtype == "uint16_or_uint32":
        if len(raw) >= 4:
            ival = int.from_bytes(raw[:4], "little")
        elif len(raw) >= 2:
            ival = int.from_bytes(raw[:2], "little")
        else:
            return raw.hex().upper()
        return ival * reg.scale
    fmt = _DTYPE_STRUCT.get(reg.dtype, "")
    if not fmt:
        return raw.hex().upper()
    import struct as _s
    size = _s.calcsize(fmt)
    if len(raw) < size:
        return raw.hex().upper()
    ival = _s.unpack(fmt, raw[:size])[0]
    bad = _NONE_SENTINELS.get(reg.id, set())
    if ival in bad:
        return None
    return ival * reg.scale


def encode_value(reg: HexRegister, value: float | int | str | bool | list[str] | tuple | dict | bytes | None) -> bytes:
    """Encode a human value to LE payload bytes (inverse of decode_value)."""
    import struct as _s
    if value is None:
        bad = _NONE_SENTINELS.get(reg.id, set())
        if bad:
            s = next(iter(bad))
            if reg.dtype == "uint24_t":
                return s.to_bytes(3, "little")
            fmt0 = _DTYPE_STRUCT.get(reg.dtype, "")
            if fmt0:
                return _s.pack(fmt0, s)
        raise ValueError(f"None not allowed for {reg.name}")
    if reg.cls == "BOOLEAN":
        return bytes([1 if value else 0])
    if reg.cls == "STRING":
        return str(value).encode("ascii") + b"\x00"
    if reg.cls == "STRUCT":
        kind = reg.enum or ""
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if kind == "TIMER_EVENT":
            assert isinstance(value, dict)
            return encode_timer_event(value)
        if kind == "VOLTAGE_RANGE":
            assert isinstance(value, (dict, int))
            return encode_voltage_range(value)  # type: ignore[arg-type]
        if kind == "LOAD_CONTROL":
            assert isinstance(value, (dict, int, str, bytes))
            return encode_load_control(value)  # type: ignore[arg-type]
        if kind == "NETWORK_STATUS":
            assert isinstance(value, (dict, int, bytes))
            return encode_network_status(value)  # type: ignore[arg-type]
        if kind == "BMV_SW_VERSION":
            assert isinstance(value, (dict, int, str, bytes))
            return encode_bmv_sw_version(value)  # type: ignore[arg-type]
        raise ValueError(f"STRUCT {reg.name}/{kind} is read-only (history); pass raw bytes")
    if reg.cls == "ENUM":
        if isinstance(value, str):
            rev = {v: k for k, v in ENUM_MAPS.get(reg.enum or "", {}).items()}
            if value not in rev:
                raise ValueError(f"unknown enum label {value!r} for {reg.name}")
            value = rev[value]
        signed = reg.dtype.startswith("int")
        width = {"int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2, "int32_t": 4, "uint32_t": 4}.get(reg.dtype, 1)
        return int(value).to_bytes(width, "little", signed=signed)
    if reg.cls == "BITMASK":
        width = {"uint8_t": 1, "uint16_t": 2, "uint32_t": 4}.get(reg.dtype, 4)
        if isinstance(value, (list, tuple)):
            # List of bit names -> integer mask (inverse of decode_value).
            rev = {v: k for k, v in ENUM_MAPS.get(reg.enum or "", {}).items()}
            mask = 0
            for name in value:
                if name not in rev:
                    raise ValueError(f"unknown bit name {name!r} for {reg.name}")
                mask |= 1 << rev[name]
            value = mask
        return int(value).to_bytes(width, "little")
    if reg.cls == "VOID":
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            s = value.strip().removeprefix("0x").removeprefix("0X").replace(" ", "")
            if len(s) % 2 == 1:
                s = "0" + s
            try:
                return bytes.fromhex(s)
            except ValueError as e:
                raise ValueError(f"cannot encode VOID {reg.name} from {value!r}") from e
        width = {"uint8_t": 1, "uint16_t": 2, "uint32_t": 4}.get(reg.dtype, 4)
        return int(value).to_bytes(width, "little")
    if reg.dtype == "uint24_t":
        return int(round(float(value) / reg.scale)).to_bytes(3, "little")
    fmt = _DTYPE_STRUCT.get(reg.dtype)
    if not fmt:
        raise ValueError(f"cannot encode {reg.name}")
    return _s.pack(fmt, int(round(float(value) / reg.scale)))


# Flavor hierarchy (upstream ve_reg_flavor.h): MPPT->CHG, MPPT_RS->MPPT,
# MULTI_RS->INV+MPPT_RS, BMV71->BMV, ORION->CHG, any CHG/INV/BMV->ANY.
_FLAVOR_EXPAND: dict[str, set[str]] = {
    "ANY": {"ANY"},
    "MPPT": {"MPPT", "CHG", "ANY"},
    "CHG": {"CHG", "ANY"},
    "ORION": {"ORION", "CHG", "ANY"},
    "INV": {"INV", "ANY"},
    "BMV": {"BMV", "ANY"},
    "BMV71": {"BMV71", "BMV", "ANY"},
    "MPPT_RS": {"MPPT_RS", "MPPT", "CHG", "ANY"},
    "MULTI_RS": {"MULTI_RS", "INV", "MPPT_RS", "MPPT", "CHG", "ANY"},
}


def for_flavor(flavor: str) -> dict[str, HexRegister]:
    """Registers visible to a device flavor (hierarchy included)."""
    f = flavor.upper()
    want = _FLAVOR_EXPAND.get(f, {f, "ANY"})
    return {n: r for n, r in REGISTERS.items() if want & set(r.flavors)}


# These entries come from field observations rather than a public Victron HEX
# specification.  Keeping an explicit view prevents callers from confusing
# best-effort Multi RS metadata with the officially documented families.
EXPERIMENTAL_MULTI_RS_REGISTERS: dict[str, HexRegister] = {
    name: reg for name, reg in REGISTERS.items()
    if "MULTI_RS" in reg.flavors
}
