# MPPT dump audit

Reviewed `../VeDirWiz/tty0.json` and `../VeDirWiz/tty1.json`, exported on
2026-10-16. Identity, telemetry, register statuses, and completed read jobs
are internally consistent. The audit found five missing unavailable-value
markers and one missing display-speed interpretation in the library.

## Device results

| Field | tty0.json | tty1.json |
| --- | --- | --- |
| Port | /dev/ttyUSB0 | /dev/ttyUSB1 |
| Model | SmartSolar MPPT 150/35 | SmartSolar MPPT 250/60 rev2 |
| Product ID, Text and HEX | 0xA058 | 0xA068 |
| Firmware, Text | 1.64 | 1.64 |
| Export time, UTC | 20:59:29 | 20:59:02 |
| Successful cached registers | 86 | 100 |
| Rejected registers | 28 | 14 |
| Telemetry samples | 15 | 22 |
| Sample battery voltage | 53.14 to 53.16 V | 53.13 to 53.19 V |
| Sample panel voltage | 53.05 V | 53.00 to 53.06 V |
| Charging current / panel power | 0 A / 0 W | 0 A / 0 W |
| Charge state / tracker state | Not charging / off | Not charging / off |
| Off reason / charger error | No input power / no error | No input power / no error |
| Maximum charge current | 35 A | 60 A |
| Total yield, Text | 47.68 kWh | 70.16 kWh |

Text and HEX serial numbers match within each device, and the two devices
have different identities. The selected `MPPT` flavor matches both models.
Battery voltage and panel voltage agree between the latest Text and HEX
values within 0.03 V. Notifications and reads occur at different times.

Both latest measurement jobs completed all 31 requested registers, with
six explicit unknown-register rejections and no timeout. Cached values are
0.34 to 14.52 seconds old on tty0 and 0.04 to 22.11 seconds old on tty1.
Every successful cached value was checked using a reconstructed payload
through the current codec, including structured load, timer, and network
values. Product ID retained actual payload bytes in its `raw` field.
All 37 telemetry samples have increasing timestamps, zero power/current,
no error, and the same no-input-power state. Their final numeric fields
match the exported telemetry.
Charge-state elapsed time is in milliseconds. After converting it to
seconds and accounting for read timestamps, it agrees with uptime within
one second on both devices.

Both devices report a 48 V nominal battery, a user preset, absorption and
float targets of 58.4 V, zero temperature compensation, and automatic
equalisation disabled. These readbacks agree. Battery chemistry and the
battery manufacturer's limits were not supplied, so suitability of the
charge settings was not assessed.

## Decoder corrections

Both devices are standalone, with no active external current, voltage, or
temperature input. These exported network values map exactly to maximum
wire integers after undoing the documented scale:

| Register | Exported value | Inferred marker | Corrected value |
| --- | --- | --- | --- |
| CHARGE_VOLTAGE_SETPOINT, 0x2001 | 655.35 V | 0xFFFF | None |
| BATTERY_IDLE_VOLTAGE, 0x200B | 655.35 V | 0xFFFF | None |
| BATTERY_CHARGE_CURRENT, 0x200A | 2147483.647 A | 0x7FFFFFFF | None |
| TOTAL_CHARGE_CURRENT, 0x2013 | 2147483.647 A | 0x7FFFFFFF | None |
| CHARGE_CURRENT_LIMIT, 0x2015 | 6553.5 A | 0xFFFF | None |

These are inferred unavailable or unset network inputs. They are not actual
battery measurements or the configured maximum charge current. The
[official BlueSolar HEX reference](https://www.victronenergy.com/upload/documents/BlueSolar-HEX-protocol.pdf)
defines their widths, scales, and roles in synchronized or external control,
but does not document these markers. The library now handles them through
its existing `None` sentinel convention. Tests pin the five exact markers,
normal values, negative battery current, zero, and an unrelated register's
maximum value. Shared register IDs use this interpretation in other device
families too; those families were not tested on hardware. Marker writes
were tested only through encoding, without a physical device write.

The 250/60 also reports `DISPLAY_SCROLL_SPEED` as `unknown(3)`. The official
reference specifies a range with endpoints 1 slow and 5 fast. The library
now preserves intermediate speeds as `speed_2`, `speed_3`, and `speed_4`.
The captured value becomes `speed_3`. Zero and values above 5 remain unknown.

VeDirWiz's `NULL_LABELS` also gained entries for the five network inputs so
its numeric editors show "Unavailable". This companion change lives in
`../VeDirWiz/vedirwiz/catalog.py`, outside this Git repository. Existing
JSON exports remain unchanged; a fresh app process and read use the fixes.

## Expected omissions and remaining limits

All rejected reads contain an explicit device response of "unknown register
id" with the correct catalog address. The 14 common rejections are
`APP_VER`, `PRODUCT_REVISION`, `DEVICE_OFF_REASON`, `WARNING_REASON`,
`ALARM_REASON`, `DEVICE_FUNCTION`, `BATTERYSAFE_MODE`, `VOLTAGE_COMPENSATION`,
`BAT_STORAGE_VOLTAGE`, `BAT_BULK_LIMIT`, `LOAD_CURRENT`, `LOAD_OUTPUT_VOLTAGE`,
`LOAD_AES_TIMER`, and deprecated `CHARGE_CURRENT_PERCENT`.
Firmware is still available in Text, and `DEVICE_OFF_REASON_2` supplies
the off reason. Unsupported load measurements coexist with virtual load
state and control, consistent with the advertised virtual-load capability.
`LOAD=ON` describes that virtual output.

The 150/35 additionally rejects nine relay and five display registers.
The 250/60 advertises relay and pluggable-display capabilities and accepts
those registers. Its relay is off in both Text and HEX. Successful reads
with `None` for absent battery temperature, remote sensors, unknown time,
and settings timestamp are distinct from rejected register reads.

The six all-zero timer records retain `anchor_name="unknown(0)"`. The
reference names only anchors 1 through 3; the library preserves zero
without inventing an event. Lighting timer control is disabled in both dumps.
`SOLAR_ACTIVITY=true` describes day detection and can coexist with no
usable charging input power.

Each dump contains 114 of the 188 catalog entries. The remaining 74 are
71 history entries and three write-only commands. VeDirWiz excludes history
from its automatic connection read, so detailed daily and lifetime HEX
history was not validated by these captures.

The shared activity log retains two HEX reply timeouts at 20:37 UTC and a
disconnect or competing-port-access error at 20:57 UTC. Later connections
and reads succeeded. The snapshots cannot establish the earlier failures'
cause or prove long-term link stability.

The exports contain decoded snapshots, not original Text blocks or HEX
traffic. Reconstructed payload checks cannot independently verify original
checksums, framing, or the inferred sentinel bytes. No live serial access,
device writes, or charging-under-load test was performed.

## Evidence and checks

Original file SHA-256 hashes:

- tty0.json: `fd0085dcabb2be4ea95df4d63b41762e29de38437f22ea3be916e88b882960a0`
- tty1.json: `320a0786bb59047871c46c46ce4ce467220011ac0cc71ed277863ce08199e31b`

The original dumps were left in VeDirWiz. The library regression tests retain
only the values needed to cover the corrections.

- Library suite: 323 tests passed, including live checks against all five
  official specifications, with no skips.
- Frozen conformance gate: 191 requirement groups, 122 tests, no failing
  requirements, evidence gaps, nonpassing checks, or ledger issues.
- Focused offscreen VeDirWiz check: all five unavailable editors and speed 3
  render correctly without staging writes.
- VeDirWiz suite: 20 of 21 tests passed. The help-panel visibility test,
  `test_question_mark_opens_help_in_its_own_window`, failed identically with
  the pre-change decoder and labels restored in memory. That existing UI
  issue was left outside this data-readout fix.

The conformance gate verifies the pinned host-protocol scope. The new
hardware-derived sentinel interpretation is recorded separately in
[spec exceptions](SPEC_EXCEPTIONS.md).
