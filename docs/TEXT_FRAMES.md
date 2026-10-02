# Text frames — label reference

Wire: 19200 8N1, records `CR LF LABEL TAB VALUE`, approximately one
block/sec. The raw byte after `Checksum<TAB>` makes `sum(block)%256==0`.
HEX starts are detected byte-by-byte, including immediately after that raw
checksum and while interrupting a Text record.
Only an exact case-insensitive `Checksum` label after a CRLF record boundary
terminates a block. Unknown labels such as `XChecksum`, and values containing
`Checksum`, remain data within the validated block.
An initial capture fragment starting at `Checksum<TAB>` or `LF Checksum<TAB>`
is discarded so its raw checksum byte cannot hide the next complete block.
BMV/SmartShunt sends alternating PID-block + history-block. In code: frame[label].value/.unit/.human, frame.to_si(), frame.describe(label).

Nonnumeric measurement text stays in `.raw` and `.value`. `to_si()` preserves
that string and converts the other numeric fields independently. Missing
measurements retain `None`.

The client queues up to 256 Text frames by default. Set a positive integer
`text_queue_size` in its constructor, `from_streams()`, or `from_port()` to
change the limit. Overflow discards the oldest queued frame and increments
`dev.dropped_text_frames`. Frames below capacity retain arrival order.
`read_one()` timeouts raise the exported `ve_direct.TimeoutError`.
Closing the client wakes blocked Text readers with `TransportError`.

## Labels

| Label | Wire unit | SI in to_si() | Devices (typical) | Description |
|---|---|---|---|---|
| PID | — | — | BMV-70x, BMV-71x/SmartShunt, MPPT, Inverter, Charger, OrionXS/BuckBoost (no BMV-60x) | Product ID (hex, e.g. 0xA04A) |
| FW | — | — | BMV/SmartShunt (60x, 70x, 71x), MPPT, Inverter (Charger/BuckBoost use FWE) | Firmware version 16-bit (208 = v2.08, C208 = RC) |
| FWE | — | — | Charger, OrionXS/BuckBoost | Firmware version 24-bit, optional leading zeros (0208FF or 208FF = v2.08 release) |
| SER# | — | — | MPPT, Inverter, Charger, OrionXS/BuckBoost | Serial number (LLYYMMSSSSS) |
| BMV | — | — | BMV/SmartShunt | Model description (deprecated, use PID) |
| V | mV | V ÷1000 | MPPT, Inverter, Charger, BMV/SmartShunt, OrionXS/BuckBoost | Main / channel 1 (battery) voltage |
| V2 | mV | V ÷1000 | Charger | Channel 2 (battery) voltage |
| V3 | mV | V ÷1000 | Charger | Channel 3 (battery) voltage |
| VS | mV | V ÷1000 | BMV/SmartShunt | Auxiliary (starter) voltage |
| VM | mV | V ÷1000 | BMV-70x, BMV-71x/SmartShunt | Mid-point voltage of the battery bank |
| DM | ‰ | % ÷10 | BMV-70x, BMV-71x/SmartShunt | Mid-point deviation of the battery bank |
| VPV | mV | V ÷1000 | MPPT | Panel voltage |
| PPV | W | as-is | MPPT | Panel power |
| I | mA | A ÷1000 | MPPT, Charger, BMV/SmartShunt, OrionXS/BuckBoost | Main / channel 1 battery current (>0 = charging) |
| I2 | mA | A ÷1000 | Charger | Channel 2 battery current |
| I3 | mA | A ÷1000 | Charger | Channel 3 battery current |
| IL | mA | A ÷1000 | MPPT | Load current |
| LOAD | — | — | MPPT | Load output state (ON/OFF) |
| T | °C | as-is | see spec matrix | Battery temperature (--- = no sensor) |
| P | W | as-is | BMV-70x, BMV-71x/SmartShunt, OrionXS/BuckBoost | Instantaneous power |
| CE | mAh | Ah ÷1000 | BMV/SmartShunt | Consumed Amp Hours (--- if unsynced / DC-monitor) |
| SOC | ‰ | % ÷10 | BMV/SmartShunt | State of charge (1000 = 100.0%) |
| TTG | min | as-is | BMV/SmartShunt | Time-to-go (-1 = infinite, not discharging) |
| Alarm | — | — | BMV/SmartShunt | Alarm condition active (ON/OFF) |
| Relay | — | — | MPPT, Inverter, Charger, BMV/SmartShunt | Relay state (ON/OFF) |
| AR | — | — | BMV/SmartShunt, Inverter | Alarm reason (bitmask sum, decimal) |
| WARN | — | — | Inverter | Warning reason, live (inverters only, same bits as AR) |
| OR | — | — | MPPT, Inverter, OrionXS/BuckBoost | Off reason (hexadecimal bitmask, e.g. 0x00000010) |
| CS | — | — | MPPT, Inverter, Charger, OrionXS/BuckBoost | State of operation |
| ERR | — | — | MPPT, Charger, OrionXS/BuckBoost | Error code |
| MODE | — | — | Inverter, Charger, OrionXS/BuckBoost | Device mode |
| MPPT | — | — | MPPT | Tracker operation mode |
| MON | — | — | BMV-71x/SmartShunt | DC monitor mode |
| HSDS | — | — | MPPT | Day sequence number (0..364, new day = history rollover) |
| H1 | mAh | Ah ÷1000 | BMV/SmartShunt | Depth of the deepest discharge |
| H2 | mAh | Ah ÷1000 | BMV/SmartShunt | Depth of the last discharge |
| H3 | mAh | Ah ÷1000 | BMV/SmartShunt | Depth of the average discharge |
| H4 | — | — | BMV/SmartShunt | Number of charge cycles |
| H5 | — | — | BMV/SmartShunt | Number of full discharges |
| H6 | mAh | Ah ÷1000 | BMV/SmartShunt | Cumulative Amp Hours drawn |
| H7 | mV | V ÷1000 | BMV/SmartShunt | Minimum main (battery) voltage |
| H8 | mV | V ÷1000 | BMV/SmartShunt | Maximum main (battery) voltage |
| H9 | s | as-is | BMV/SmartShunt | Seconds since last full charge |
| H10 | — | — | BMV/SmartShunt | Number of automatic synchronizations |
| H11 | — | — | BMV/SmartShunt | Number of low main voltage alarms |
| H12 | — | — | BMV/SmartShunt | Number of high main voltage alarms |
| H13 | — | — | see spec matrix | Number of low auxiliary voltage alarms |
| H14 | — | — | see spec matrix | Number of high auxiliary voltage alarms |
| H15 | mV | V ÷1000 | BMV/SmartShunt | Minimum auxiliary (battery) voltage |
| H16 | mV | V ÷1000 | BMV/SmartShunt | Maximum auxiliary (battery) voltage |
| H17 | 0.01kWh | kWh ÷100 | BMV-70x, BMV-71x/SmartShunt | Discharged energy (BMV) / produced energy (DC monitor) |
| H18 | 0.01kWh | kWh ÷100 | BMV-70x, BMV-71x/SmartShunt | Charged energy (BMV) / consumed energy (DC monitor) |
| H19 | 0.01kWh | kWh ÷100 | MPPT | Yield total (user resettable) |
| H20 | 0.01kWh | kWh ÷100 | MPPT | Yield today |
| H21 | W | as-is | MPPT | Maximum power today |
| H22 | 0.01kWh | kWh ÷100 | MPPT | Yield yesterday |
| H23 | W | as-is | MPPT | Maximum power yesterday |
| AC_OUT_V | 0.01V | V ÷100 | Inverter | AC output voltage |
| AC_OUT_I | 0.1A | A ÷10 | Inverter | AC output current |
| AC_OUT_S | VA | as-is | Inverter | AC output apparent power |
| DC_IN_V | 0.01V | V ÷100 | OrionXS/BuckBoost | DC input voltage (BuckBoost/OrionXS) |
| DC_IN_I | 0.1A | A ÷10 | OrionXS/BuckBoost | DC input current (BuckBoost/OrionXS) |
| DC_IN_P | W | as-is | OrionXS/BuckBoost | DC input power, 1W units per spec (BuckBoost/OrionXS) |
| CAP_BLE | — | — | Inverter (Smart Phoenix; §CAP_BLE + history 3.26, no Fields-table row) | BLE off-capability bitmask |

## Decoded values

CS (state): 0=Off, 1=Low power, 2=Fault, 3=Bulk, 4=Absorption, 5=Float, 6=Storage, 7=Equalize (manual), 9=Inverting, 11=Power supply, 245=Starting-up, 246=Repeated absorption, 247=Auto equalize / Recondition, 248=BatterySafe, 252=External control

ERR: 0=No error, 2=Battery voltage too high, 17=Charger temperature too high, 18=Charger over current, 19=Charger current reversed (often transient on start/stop), 20=Bulk time limit exceeded, 21=Current sensor issue, 26=Terminals overheated, 28=Converter issue (dual converter models), 33=Input voltage too high (solar), 34=Input current too high (solar), 38=Input shutdown (excessive battery voltage), 39=Input shutdown (current flow during off mode), 65=Lost communication with one of devices, 66=Synchronised charging device configuration issue, 67=BMS connection lost, 68=Network misconfigured, 116=Factory calibration data lost, 117=Invalid/incompatible firmware, 119=User settings invalid

OR bits: 0x1=No input power, 0x2=Switched off (power switch), 0x4=Switched off (device mode register), 0x8=Remote input, 0x10=Protection active, 0x20=Paygo, 0x40=BMS, 0x80=Engine shutdown detection, 0x100=Analysing input voltage

AR/WARN bits: 1=Low voltage, 2=High voltage, 4=Low SOC, 8=Low starter voltage, 16=High starter voltage, 32=Low temperature, 64=High temperature, 128=Mid voltage, 256=Overload, 512=DC-ripple, 1024=Low V AC out, 2048=High V AC out, 4096=Short circuit, 8192=BMS lockout

MODE: 1=Charger (VE_REG_MODE_CHARGER), 2=Inverter (VE_REG_MODE_INVERTER), 4=Off, 5=Eco, 253=Hibernate (smart only)

MPPT: 0=Off, 1=Voltage or current limited, 2=MPP Tracker active

MON: -9=Solar charger, -8=Wind turbine, -7=Shaft generator, -6=Alternator, -5=Fuel cell, -4=Water generator, -3=DC/DC charger, -2=AC charger, -1=Generic source, 0=Battery monitor (BMV), 1=Generic load, 2=Electric drive, 3=Fridge, 4=Water pump, 5=Bilge pump, 6=DC system, 7=Inverter, 8=Water heater

CAP_BLE bits: 0x1=BLE supports switching off, 0x2=BLE switching off is permanent (0=No BLE off capability)

PID: e.g. 0x204=BMV-702, 0xA04A=BlueSolar 100|30 rev2, 0xA058=SmartSolar 150|35, 0xA3F0=Orion XS 12V/12V-50A, 0xA3F1=Orion XS 1400; full map in tables.py:PID_MAP (171 entries incl. 0xA048, 0xA110/0xA111 from BlueSolar-HEX and Phoenix-HEX Rev 5 0xA2B1/0xA2E1/0xA2F1).

Notes: SOC raw 0..1000 = 0..100.0%. TTG -1 = infinite (not discharging,
surfaced as `.human = "Infinite (not discharging)"`).
T/CE/SOC/TTG show `---` when unsynced or no sensor. FW/FWE expose decoded
version text through `.human`. LOAD/Alarm/Relay are normalized to uppercase
(old firmware sent On/Off).

Implementation guidelines (spec p.11): label 9B, value 33B, max 22
fields/block. The parser keeps liberal acceptance (arbitrary lengths) but
caps buffered input at 8 KiB (oversized HEX candidates and Text backlog are
truncated) so hostile/corrupt input cannot grow memory unbounded.
