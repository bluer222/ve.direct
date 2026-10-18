# Spec exceptions — deliberate deviations and disambiguations

This log consolidates places where the code intentionally does not mirror a doc table cell, where two doc locations conflict and one side was chosen, where the doc is self-contradictory, and where behavior was kept for compatibility despite doc wording. Provenance for the underlying tables lives in docs/SOURCES.md. Ground truth for doc citations is tests/specs/*.txt. Code references are file:line against the current tree.

## Decision principles

The cases below do not all follow one rule. The rules actually applied are listed here, with one illustrating case each, followed by an honest note on where they diverge.

### Normative text outranks examples

When a normative rule and a printed example disagree, the rule wins. Orion section 1.3 prints Unknown reply `:30300F4`, which fails the documented checksum rule. The encoder produces `:303004F`, and strict decoding rejects the printed form. See the Orion Unknown checksum typo entry below. ProductId commands and Get register `0x0100` have separate layouts, as recorded in their catalog entry.

### Annotating notes qualify the cells they attach to

A footnote attached to a row redefines that row. Illustration: Phoenix 0xEB10 prints 0.02 in the scale cell, but Note 4 defines the wire value as a count of AC-out periods, so the database stores raw counts. See the 0xEB10 entry.

### Internal consistency decides unqualified conflicts

When two doc locations state different values with no qualifying note, the side that keeps the document self-consistent wins. Illustration: BlueSolar history overview lists MPPT history as 0x10A0 through 0x10BE while the detail heading prints 0x10A0 through 0x10BD, and the overview side is kept because it is symmetric with daily history 0x1050 through 0x106E and both sides agree there. See the 0x10BE entry.

### Never invent wire values

Undocumented shapes decode as unknown rather than as a guessed name. Illustration: Orion 24-bit version values with LL other than 0xFF decode as version None with an unknown build tag, and 3-byte inputs decode as unknown, instead of inventing a release or beta label. See src/ve_direct/hex.py:80-114.

### Physical plausibility breaks remaining ties

When the doc gives a special value plus a normal range, the non-physical reading confirms the special-value handling. Illustration: BAT_VOLTAGE raw 0 decodes as None because 0 V is non-physical and the shared battery-voltage-setting table defines 0 as auto, and BMV temperature raw 0 decodes as disabled because 0 K is non-physical while the valid range is 174 through 372 K. See the BAT_VOLTAGE and temperature entries.

### Liberal visibility unless there is a wire contradiction or a semantic collision, with quarantine for observations

Registers documented for one family stay visible to other families unless keeping them would misdecode the wire or conflate two meanings. Observed-but-undocumented ids are kept only when marked and quarantined. Illustration: display 0x0401 through 0x0404 stay ANY although BlueSolar scopes them to pluggable-LCD models, while 0x0400 and 0x0350 through 0x0353 are scoped narrowly because each collides semantically, and the 14 Multi RS ids live only in EXPERIMENTAL_MULTI_RS_REGISTERS. See the liberal flavor and 0xEDF5 entries.

### Evidence-pinned allowlists instead of silent skips

Parser-to-database comparisons allowlist only exact, justified ids, and each allowlist entry carries the evidence that would invalidate it. Illustration: SCALE_VALUE_EXCEPTIONS holds exactly one entry for Phoenix 0xEB10, and NON_REGISTER_IDS holds 0x0160 and 0xEFF0 only with header-versus-row evidence tests. See the SCALE_VALUE_EXCEPTIONS and typo entries.

### Where the scheme diverges

Superset and strictness are applied case by case, not uniformly. BAT_VOLTAGE keeps the BlueSolar superset of 0 and 36 even though the Orion table lists only 12, 24, and 48, because Orion hardware simply never reports the extra values and no misdecode follows. CAPABILITIES_BLE keeps Orion bit names 2 through 8 on the shared ANY register even though BMV and Phoenix reserve those bits, because reserved bits read 0 there and the names are harmless. By contrast 0x0400 and 0x0350 through 0x0353 are scoped strictly because each id carries two live meanings with different scales or classes, where a superset would misdecode. ProductId command replies use the documented two-byte shape for every family; the product-information register has separate width and endianness rules. The common thread is wire safety, not maximal strictness or maximal leniency.

## Catalog

### SmartSolar unavailable network inputs and intermediate display speeds

The two firmware 1.64 snapshots reviewed in [MPPT dump audit](MPPT_DUMP_AUDIT.md)
contain maximum wire values for five unused network inputs while both chargers
report standalone operation. The inferred markers are `0xFFFF` for `0x2001`,
`0x200B`, and `0x2015`, and `0x7FFFFFFF` for `0x200A` and `0x2013`. They decode
as `None` and encode back to the same marker. BlueSolar Rev 18 describes these
registers but does not document these special values. This is an inference
from the captures, pinned separately from documented sentinels in the spec
comparison test. A raw capture, a controlled network test, or a later official
spec assigning different semantics would require revisiting it. The shared
register-ID sentinel table applies this interpretation to other families
exposing these IDs too; those families were not tested on hardware.

BlueSolar defines display scroll speed `0x0402` with endpoints 1 slow and 5
fast. The 250/60 snapshot reports 3. Intermediate values now retain their
numbers as `speed_2`, `speed_3`, and `speed_4`. No timing or descriptive speed
is assumed, and values outside 1 through 5 still decode as unknown.

### Text firmware release candidate suffix

The Text snapshot says the value C208 means release candidate C for version 2.08 (tests/specs/VE.Direct-Protocol-3.34.txt:361). The code renders FW C208 as v2.08-RCC, which reads as RC acronym plus revision C (src/ve_direct/text.py:57-71). The HEX AppVersion decoder uses the same convention with -RC plus letter (src/ve_direct/hex.py:117-142). This side was trusted because the doubled C is intentional alignment between Text and HEX, not a duplicated character. This would change if Victron publishes a different Text rendering for release candidates.

### Text On and Off case handling

The Text snapshot notes that BMV-600 units with firmware 2.09 or lower sent On and Off instead of ON and OFF and recommends case-insensitive comparison (tests/specs/VE.Direct-Protocol-3.34.txt:355-357). The code normalizes LOAD, Alarm, and Relay with upper case comparison (src/ve_direct/text.py:180-183). This side was trusted because it follows the explicit implementation recommendation. This would change if a future label uses mixed case distinctively, in which case normalization would need per-label scoping.

### Text missing-value scope

The Text snapshot defines --- only for T and for CE, SOC, and TTG footnotes, while the code accepts --- liberally for any label except LOAD, Alarm, and Relay, and treats empty string as missing only for T, CE, SOC, and TTG (src/ve_direct/text.py:34-38, src/ve_direct/text.py:132-139). This side was trusted because strict rejection would drop plausible sensor-absent frames and the excluded labels are the only ones where --- would collide with a real state. This would change if hardware shows --- as a valid non-missing value on some other label.

### Text time to go infinite

The Text snapshot says time-to-go is infinite when the battery is not discharging and this is represented as -1 (tests/specs/VE.Direct-Protocol-3.34.txt:257, field row at :226). The code keeps numeric -1 and surfaces human Infinite (not discharging) (src/ve_direct/text.py:174-176). This side was trusted because it preserves the documented sentinel while keeping the wire value. This would change if a future revision redefines -1 or adds a second infinite sentinel.

### Text alarm, off reason, and ble unknown bits

The Text snapshot defines AR as a decimal sum of alarm bits, OR as hex off-reason bits, and CAP_BLE as two ble bits, with no unknown-bit wording (tests/specs/VE.Direct-Protocol-3.34.txt:377-380 for AR, :406-415 for OR, :418-420 for CAP_BLE). The code decodes known bits and appends Unknown bits 0x plus hex for the remainder instead of dropping them or returning no alarm (src/ve_direct/tables.py:374-404, src/ve_direct/text.py:169-173). This side was trusted because dropping unknown bits would silently lose device state and returning no alarm for a nonzero unknown value would be false. This would change if Victron documents the missing bit assignments, in which case the maps gain names and the fallback fires less often.

### Text day sequence range kept as documented

The Text snapshot lists HSDS as day sequence number 0..364 with a note that it is available since MPPT v1.16 (tests/specs/VE.Direct-Protocol-3.34.txt:290 and :643-646). The code documents the same 0..364 range (src/ve_direct/tables.py:44, docs/TEXT_FRAMES.md:46). There is no deviation here beyond keeping the exact bounds rather than assuming 0..365. This would change if a future revision extends the rollover range.

### Boolean for Un8 0 and 1 settings

BMV snapshot rows for buzzer, relay invert, backlight always on, show toggles, setup lock, and start synchronized use Un8 0..1 with OFF and ON, and Phoenix relay control uses 0..1 with OPEN and CLSD alongside Un8 (tests/specs/BMV-7xx-HEX-Protocol.txt:205 for buzzer, :226 for relay invert, :247 for backlight, :251-258 for show toggles, plus relay control in Phoenix section 2.8.2). The code models these as BOOLEAN (src/ve_direct/registers/__init__.py:148-150, src/ve_direct/registers/__init__.py:420-423). The helper defaults BOOLEAN dtype to uint8_t (src/ve_direct/registers/__init__.py:90-95). This side was trusted because Python bool equals 0 and 1, so wire values round-trip exactly and callers keep boolean truthiness, while an off and on string enum would make off truthy. This would change if a device reports a third value on one of these ids, in which case that register would need an enum.

### Liberal flavor policy

BlueSolar scopes display 0x0401 through 0x0404 to pluggable-LCD models (tests/specs/BlueSolar-HEX-protocol.txt:861-869), restricts MPPT history to multi-tracker units in Note 2 (tests/specs/BlueSolar-HEX-protocol.txt:783), and several generic ANY control and status ids are absent from the BMV snapshot by id search. The code keeps 0x0401 through 0x0404 as ANY, keeps MPPT history under both MPPT_RS and MPPT, and keeps generic ANY control and status ids visible in the BMV view (src/ve_direct/registers/__init__.py:40-56, src/ve_direct/registers/__init__.py:289-305, src/ve_direct/registers/__init__.py:379-382, docs/SOURCES.md:231-260). This side was trusted because narrowing would break for_flavor consumers with no wire-behavior contradiction. This would change if hardware shows that querying one of these ids on the excluded family errors or misdecodes, in which case that id would be scoped like 0x0400.

### Display 0x0400 scoping exception

BlueSolar defines 0x0400 as display backlight mode with 0 keypress, 1 on, 2 auto (tests/specs/BlueSolar-HEX-protocol.txt:885) while BMV defines 0x0400 as backlight always on with Un8 0..1 OFF and ON (tests/specs/BMV-7xx-HEX-Protocol.txt:247). The code scopes DISPLAY_BACKLIGHT_MODE to MPPT and MPPT_RS and exposes only BMV_BACKLIGHT_ALWAYS_ON to BMV and BMV71 (src/ve_direct/registers/__init__.py:48-52, src/ve_direct/registers/__init__.py:291-295, src/ve_direct/registers/__init__.py:314, src/ve_direct/registers/__init__.py:491). Unflavored lookup by 0x0400 raises AmbiguousRegisterError. This side was trusted because the two meanings differ in class and values and a superset would misdecode. This would change if Victron unifies the meaning across families.

### Relay 0x0350 through 0x0353 scoping exception

BlueSolar defines 0x0350 through 0x0353 as relay battery low and high set and clear with 0.01 V (tests/specs/BlueSolar-HEX-protocol.txt:643-654, with 0x0350 at :663 in the relay table), while BMV defines the same ids as relay low and high voltage plus clear with 0.1 V (tests/specs/BMV-7xx-HEX-Protocol.txt:230-233). Phoenix and Orion snapshots define no 0x0350 through 0x0353, verified by id search. The code scopes generic RELAY_BAT entries to MPPT and MPPT_RS and exposes only BMV_RELAY entries to BMV and BMV71 (src/ve_direct/registers/__init__.py:52-56, src/ve_direct/registers/__init__.py:296-310, src/ve_direct/registers/__init__.py:307-310, src/ve_direct/registers/__init__.py:476-479). Name lookup with a flavor re-resolves by wire id so a generic name under BMV flavor resolves to the BMV register. This side was trusted for the same collision reason as 0x0400, following that precedent. This would change if a future doc gives Phoenix or Orion a third meaning for these ids.

### Storage voltage 0xEDF5 provenance

The BlueSolar battery settings table lists 0xEDFF through 0xEDCA with no 0xEDF5 row (tests/specs/BlueSolar-HEX-protocol.txt:335-360), and 0xEDF5 occurs in none of the local snapshots by id search. The code keeps BAT_STORAGE_VOLTAGE as a CHG setting with an explicit krahabb provenance note (src/ve_direct/registers/__init__.py:211-215, docs/SOURCES.md:291-296). This side was trusted because dropping it would lose a plausible charger-family wire id carried over from the upstream baseline, while silent inclusion without provenance would overclaim doc support. This would change if Victron publishes 0xEDF5 with a different scale or access, or confirms it does not exist, in which case it would be corrected or removed.

### Battery voltage 0 and 36 superset

The BlueSolar battery-voltage-setting values table for 0xEDEF and 0xEDEA lists 0 auto, 12, 24, 36, and 48 (tests/specs/BlueSolar-HEX-protocol.txt:426-433), while the Orion table for the same registers lists only 12, 24, and 48 (tests/specs/Orion-XS-HEX-protocol.txt:267-272). The code keeps the shared BAT_VOLTAGE_SETTING map as a superset with 0, 12, 24, 36, and 48 (src/ve_direct/registers/__init__.py:613-616). Raw 0 on 0xEDEF decodes as None for auto following the sentinel convention (src/ve_direct/registers/__init__.py:825-834). This side was trusted because Orion devices simply never report 0 or 36, so no misdecode follows, while narrowing would lose BlueSolar behavior. This would change if Orion hardware is observed reporting 36 or 0 with a different meaning.

### Small charger battery type selection

The BlueSolar battery-type values section gives 10A, 15A, and 20A chargers without a rotary switch value 1 as TYPE_2, gives larger rotary-switch chargers value 1 as TYPE_1, and duplicates value 2 for both TYPE_2 and TYPE_3 in the large table. Select `MPPT_SMALL` for the small table through normal typed reads and writes; `MPPT` retains the large table. The large-table duplicate remains a source ambiguity, with `type_2` retained until Victron clarifies it. The former workaround requiring a standalone `decode_bat_type` call has been replaced by the explicit model selector.

### Battery type duplicate value 2 handling

The BlueSolar 30A and larger table prints value 2 twice, once for TYPE_2 GEL deep discharge 14.3V and once for TYPE_3 GEL deep discharge 14.4V (tests/specs/BlueSolar-HEX-protocol.txt:391-393). The code keeps BAT_TYPE_LARGE_MAP with value 2 as type_2 only (src/ve_direct/registers/__init__.py:757-758). This side was trusted because a single wire value cannot decode to two names and the first-listed TYPE_2 matches the small-charger TYPE_2 naming. This would change if Victron corrects the table to distinct values or clarifies which label value 2 should carry.

### Eco load detect periods raw count scale

The Phoenix table types 0xEB10 as un8 with scale cell 0.02 and unit s, with 0.016 on the next layout line (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:521), while Note 4 defines the value as expressed in number of periods of the AC-out frequency at either 50 or 60 Hz (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:531). The code stores INV_ECO_LOAD_DETECT_PERIODS as scale 1.0 with empty unit (src/ve_direct/registers/__init__.py:403-406). The spec parser allowlists this id from none-scale checks and records the value exception explicitly (tests/test_spec_parsers.py:346-349, tests/test_spec_parsers.py:360-366). This side was trusted because the annotating note qualifies the cell and hardcoding 0.02 would misdecode 60 Hz units by 25 percent. This would change if Victron publishes a single true scale or splits the register by frequency.

### Capabilities ble reserved bit naming

BMV documents only bits 0 HAS_SUPPORT and 1 PERMANENT for 0x0150 and reserves 2 through 31 (tests/specs/BMV-7xx-HEX-Protocol.txt:133-139), Phoenix documents only bits 0 and 1 and marks write as internal only (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:303 for the row and :366-375 for the section), while Orion names bits 0 through 8 and leaves 9 through 31 not defined (tests/specs/Orion-XS-HEX-protocol.txt:568-584). The code keeps one shared ANY CAPABILITIES_BLE register with Orion names for bits 2 through 8 (src/ve_direct/registers/__init__.py:703-716). This side was trusted because reserved bits read 0 on BMV and Phoenix hardware so the extra names never misdecode there, while a BMV-only override would force lookup ambiguity with no wire gain. This would change if BMV or Phoenix firmware assigns bits 2 through 8 a conflicting meaning.

### ProductId command and register layouts

The Orion section 1.1 endianness warning and four-byte instance/PID/reserved form describe Get register `0x0100`. Section 1.3 independently documents command `:451` with the two-byte reply `:1F0A3C1`. These are separate protocol operations, not a conflict between prose and an example. `get_product_id()` accepts two-byte little-endian command replies for every family without requiring a flavor. The earlier rejection policy was an implementation defect and has been removed.

### Link device state 250 removal

Both the BlueSolar and Orion link-device-state 0x200C tables list 0, 2, 3, 4, 5, 6, 7, 11, 245, 246, 247, 248, 249, 252, and 255 with no 250 (tests/specs/BlueSolar-HEX-protocol.txt:1104-1125, tests/specs/Orion-XS-HEX-protocol.txt:514-533), while the 0x0201 device-state tables list 250 BLOCKED (tests/specs/BlueSolar-HEX-protocol.txt:296, tests/specs/Orion-XS-HEX-protocol.txt:186). The code keeps 250 blocked in DEVICE_STATE_MAP and the Orion override but omits it from LINK_DEVICE_STATE_MAP so it decodes as unknown 250 there (src/ve_direct/registers/__init__.py:587-595, docs/SOURCES.md:313-318). This side was trusted because each table is authoritative for its own register. This would change if a future 0x200C table adds 250.

### Network info bit 8 drop

The BlueSolar networking table types 0x200D as un8 (tests/specs/BlueSolar-HEX-protocol.txt:906) while the network info detail lists bits 0 through 8 with bit 8 as unit held in STANDBY (tests/specs/BlueSolar-HEX-protocol.txt:1066-1080, with bit 8 at :1080). Orion repeats the same un8 typing with bits 0 through 8 (tests/specs/Orion-XS-HEX-protocol.txt:402 for the row and :480-494 for the detail). The code keeps dtype uint8_t and keeps bits 0 through 7 in NETWORK_INFO_MAP, leaving bit 8 to the generic unknown-bit fallback (src/ve_direct/registers/__init__.py:744-751). This side was trusted because bit 8 is undecodable in a one-byte Get reply and widening would break reply width. This would change if Victron retypes the register as un16 or clarifies bit 8 as a different byte.

### Shared enum per-flavor overrides

Several wire ids carry family-documented value names that differ from the generic entry. Orion documents device mode 0 or 4 off and 1 or 3 on (tests/specs/Orion-XS-HEX-protocol.txt:157-162) and its own 0x0201 state names including 11 PSU (tests/specs/Orion-XS-HEX-protocol.txt:164-184). Phoenix documents 0x0201 states 0, 1, 2, and 9 only (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:397-407), modes 2, 3, 4, 5, and 0xFD only (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:460-470), warning and alarm bits 0, 1, 5, 6, 8, 9, 10, and 11 only (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:419-431), and relay mode values 0, 2, 3, 4, 5, and 6 with no 1 (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:619-632). BMV documents relay mode 0..2 DFLT, CHRG, and REM (tests/specs/BMV-7xx-HEX-Protocol.txt:225). The code keeps the generic entry unchanged and adds flavor-qualified overrides selected by lookup with flavor (src/ve_direct/registers/__init__.py:539-555, module note at :31-38). Unflavored lookup by these ids raises AmbiguousRegisterError. This side was trusted because it preserves every family's documented names without changing any other family's decode. This would change if Victron unifies the value names across families.

### Phantom 0x0209 and 0x020A removal

No local snapshot contains 0x0209 or 0x020A by id search across BlueSolar, Orion, BMV, Phoenix, and Text snapshots. The code carries no 0x0209 register and documents the removal at both the generic and charger sites (src/ve_direct/registers/__init__.py:162, src/ve_direct/registers/__init__.py:176-178). Tests pin the absence (tests/test_registers.py:110-122). This side was trusted on internal-consistency grounds: an earlier draft carried these names but no doc evidence supports them. These would be restored if any official snapshot publishes them as real rows.

### Relay mode MPPT removal

The BlueSolar relay section documents only 0xEDD9, 0x0350 through 0x0353, 0xEDBA, 0xEDB9, and 0x100A, verified by id search with no 0x034F in the BlueSolar, Orion, or Text snapshots, while 0x034F appears only in Phoenix section 2.8.1 and BMV relay settings (tests/specs/BlueSolar-HEX-protocol.txt:643-654, tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:613 and :619-632, tests/specs/BMV-7xx-HEX-Protocol.txt:225). The code keeps no generic MPPT 0x034F and retains only the INV and BMV overrides, keeping the back-compat RELAY_MODE map unbound to any register (src/ve_direct/registers/__init__.py:151-156, src/ve_direct/registers/__init__.py:597-600). Tests pin that MPPT flavor lookup raises KeyError (tests/test_registers.py:142-151). This side was trusted following the phantom-removal precedent. This would change if BlueSolar or Orion publishes an MPPT 0x034F row.

### MPPT history 0x10BE table wins

The BlueSolar history overview lists MPPT history as 0x10A0 dotted through 0x10BE (tests/specs/BlueSolar-HEX-protocol.txt:775-783, endpoint at :798 and :780), while the History MPPT day record detail heading prints registers 0x10A0 through 0x10BD (tests/specs/BlueSolar-HEX-protocol.txt:858). The code implements 0x10A0 through 0x10BE (src/ve_direct/registers/__init__.py:378-382, docs/SOURCES.md:90-95). The parser resolution is pinned with both conflicting endpoints asserted (tests/test_spec_parsers.py:493-539). This side was trusted on symmetry: daily history 0x1050 through 0x106E agrees on both sides at 31 registers, so the overview endpoint keeps both blocks at 31. This would change if Victron corrects either side, in which case the parser test is written to fail for re-triage.

### Phoenix 0x0160 heading typo

The Phoenix product info table lists capabilities ble as 0x0150 (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:303) while section 2.2.5 heads the same material as Capabilities BLE register 0x0160 (tests/specs/VE.Direct-HEX-Protocol-Phoenix-Inverter.txt:366). The code creates no 0x0160 register and the test suite pins that 0x0160 occurs only as the header typo and never as a VE_REG table row (tests/test_spec_parsers.py:277-280, tests/test_spec_parsers.py:1161-1203, docs/SOURCES.md:277-278). This side was trusted because the table plus section body outrank a heading. This would change if a future revision adds a real 0x0160 table row.

### Orion unknown example checksum typo

The Orion section 1.3 prints :30300F4 for Unknown with payload 03 00 (tests/specs/Orion-XS-HEX-protocol.txt:609), but that line fails the checksum rule documented in both HEX section 1 framings. The code encodes the correct :303004F and documents that the printed form must not be copied (src/ve_direct/hex.py:180-183, src/ve_direct/transport.py:201-203). Tests pin the correct encoding (tests/test_hex.py:137). This side was trusted because the checksum rule is normative and mechanical. This would change only if Victron redefines the checksum rule itself.

### Orion and BlueSolar 0xEFF0 prose typo

Both the BlueSolar and Orion charge-current-limit prose cite register 0xEFF0 as the maximum battery current setting (tests/specs/BlueSolar-HEX-protocol.txt:1177, tests/specs/Orion-XS-HEX-protocol.txt:564), while the battery tables define that setting as 0xEDF0 (tests/specs/BlueSolar-HEX-protocol.txt:346, Orion battery table near :233-240). The test suite treats 0xEFF0 as a non-register typo with preconditions that it never appears as a table row (tests/test_spec_parsers.py:256-285, tests/test_spec_parsers.py:1170-1203). This side was trusted because the table row outranks prose. This would change if 0xEFF0 appears as a real table row.

### Orion missing zero rescue for 0xEDE0

The Orion battery table prints xEDE0 without the leading zero for battery low temperature level (tests/specs/Orion-XS-HEX-protocol.txt:233), against BlueSolar 0xEDE0 (tests/specs/BlueSolar-HEX-protocol.txt:357). The parser rescues this one id and the test suite allowlists it as scraper-blind (tests/test_spec_parsers.py:285). This side was trusted because the surrounding row context plus the BlueSolar spelling makes the intent unambiguous. This would change if Victron corrects the print, in which case the rescue becomes a normal parse.

### BMV alarm acknowledge access relaxation

The BMV alarm settings row for 0x031F Alarm Acknowledge carries dashes in every column (tests/specs/BMV-7xx-HEX-Protocol.txt:222) under the Get and Set items heading (tests/specs/BMV-7xx-HEX-Protocol.txt:110), unlike 0x1029, 0x102C, 0x0004, and 0x1030 which are explicitly marked write only (tests/specs/BMV-7xx-HEX-Protocol.txt:262-265). The code models 0x031F as VOID READ_WRITE for get plus set (src/ve_direct/registers/__init__.py:471-474). This side was trusted because the absence of a write-only marker under a Get and Set heading means get plus set. This would change if a revision marks the row write only or documents a read payload.

### Simulator boot failure flag

Both HEX section 1 response tables define Error payload 0 as unable to enter bootloader alongside payload 0xAAAA as framing error (tests/specs/BlueSolar-HEX-protocol.txt:71, tests/specs/Orion-XS-HEX-protocol.txt:58). The simulator adds a boot_fails switch that emits :40051 with payload 0x00 instead of rebooting silently (src/ve_direct/transport.py:30, src/ve_direct/transport.py:43, src/ve_direct/transport.py:219-227). The device layer treats Error 0xAAAA as ChecksumError and other Error payloads as RegisterError (src/ve_direct/device.py:252-268). This behavior goes beyond the docs in that no doc defines a test switch; the wire encoding itself is documented. This would change if Victron documents a different boot-failure payload.

### Simulator parameter error flag

Both HEX section 1 flag tables define 0x04 as parameter error for out-of-range or inconsistent values (tests/specs/BlueSolar-HEX-protocol.txt near the Get and Set flags table, tests/specs/Orion-XS-HEX-protocol.txt:79-81). The simulator adds a parameter_error switch that fails every Get and Set, or a selected register set, with flags 0x04 (src/ve_direct/transport.py:44-64, src/ve_direct/transport.py:140-141, src/ve_direct/transport.py:170-171, flag definitions at src/ve_direct/hex.py:34-41). This goes beyond the docs as a test control; the flag value itself is documented. This would change if the flag semantics are revised.

### Simulator width-correct zeros

No doc defines what a simulator should return for an unlisted register. The simulator answers known registers with dtype-width zero payloads when no canned reply exists (src/ve_direct/transport.py:76-88, src/ve_direct/transport.py:157-162). This behavior goes beyond the docs and exists so tests exercise decode paths with the correct reply width. This would change if a test needs a different default, in which case canned hex_replies override it.

### Fetch specs offline skip policy

The freshness design compares each snapshot's own revision markers against online Last-Modified headers without downloading, and the content gate compares full re-extracted text when network and pdftotext are available (tools/fetch_specs.py:9-22, tools/fetch_specs.py:283-344, tools/fetch_specs.py:347-373). The test suite skips, never fails, when offline or otherwise unverifiable, with a loud warning naming the unverifiable specs plus the last-verified manifest (tests/test_spec_parsers.py:720-752). This policy was chosen because pinned counts alone stay green against a new online revision, while failing offline would make the suite unusable without network. This would change if the project decides offline runs must fail on staleness, or if Victron provides versioned snapshot hashes.

### Scale value exceptions mechanism

The comparison between parsed doc scales and database scales asserts equality except for entries in SCALE_VALUE_EXCEPTIONS, which currently holds exactly Phoenix 0xEB10 at 1.0 (tests/test_spec_parsers.py:357-367 for the mechanism, tests/test_spec_parsers.py:646-651 for the assertion). SCALE_NONE_ALLOW and DTYPE_UNKNOWN_ALLOW cover ids where the doc genuinely prints no factor or no single width, including history structs, write-only commands, and firmware-dependent widths (tests/test_spec_parsers.py:290-355, tests/test_spec_parsers.py:369-385). This mechanism was trusted because it fails loudly on drift from either side instead of silently skipping. It would change by adding a new entry only with per-entry justification, never by widening an allowlist silently.

### Shared charger setting units omitted by table cells

`TAIL_CURRENT`, register `0xEDE7`, uses A with scale 0.1. BlueSolar and Orion
omit the unit from their table rows. The official [SmartSolar Control display
manual, setup setting 21](https://www.victronenergy.com/media/pg/SmartSolar_Control_Display/en/operation.html)
identifies the setting in A. This is separate from `BMV_TAIL_CURRENT`, which remains %.

`AUTOMATIC_EQUALISATION_MODE`, register `0xEDFD`, uses days with scale 1.
BlueSolar Note 2 defines 1 as daily and 2 as every other day, up to 250
in `tests/specs/BlueSolar-HEX-protocol.txt:360-361`. Zero remains off.
Both shared CHG definitions retain their original wire widths and numeric codecs.
The schema comparison keeps explicit per-register unit exceptions for the
BlueSolar and Orion equalisation table cells, which list encoded values instead of days.

## Open questions

These are genuine ambiguities where the current choice is a guess. Each states what evidence would settle it.

### Whether reserved ble bits stay zero on BMV and Phoenix hardware

The code assumes BMV and Phoenix reserved bits 2 through 8 read 0, so Orion names there are harmless. If future firmware assigns those bits a conflicting meaning, BMV and Phoenix decodes would mislabel them. Capture from BMV-712 and Phoenix hardware with ble features toggled and inspect raw 0x0150 words would settle this.

### Whether 0xEDF5 exists on the wire

The code keeps a krahabb-sourced storage voltage with BlueSolar-style scale and access but no doc row. If no charger answers 0xEDF5, the entry is dead metadata; if some charger answers with a different width or scale, the current definition misdecodes. A Get against BlueSolar and SmartSolar chargers across firmware versions would settle this.

### Whether single-tracker MPPT units return per-tracker history

The code exposes 0x10A0 through 0x10BE to MPPT although Note 2 scopes them to multi-tracker units. If single-tracker units answer with Unknown Id flags, the liberal tag is harmless but overstated; if they answer with combined data, the tag is accurate. Get probes on single-tracker versus MPPT RS hardware would settle this.

### Whether generic ANY display and network ids are safe on BMV and INV

The code leaves 0x0401 through 0x0404 and generic ANY control and status ids visible to families whose snapshots omit them. If BMV or INV hardware rejects these ids, for_flavor overpromises. Family-specific Get probes would settle this per id.

### Which label the duplicated battery type value 2 should carry

The doc prints value 2 for both TYPE_2 and TYPE_3 in the large-charger table and the code keeps type_2. If TYPE_3 is the intended label, decodes of value 2 misname the chemistry. A Victron correction or a readback from a rotary-set large charger would settle this.

### What BMV 0x031F get returns on hardware

The code allows get plus set because the row lacks a write-only marker, but the doc defines no read payload. If hardware answers Get with flags Unknown Id or Not supported, the READ_WRITE tag overpromises. A Get probe on BMV hardware would settle this.

### Whether eco load detect periods need frequency-aware scaling

The code returns raw period counts and leaves Hertz conversion to the caller. If installers expect seconds unconditionally, the raw count surprises; if the code hardcoded 0.02, 60 Hz sites would misread by 25 percent. A documented scale split or a frequency-aware helper would settle the ergonomics, but only hardware comparison at both frequencies confirms the counts.

### Whether link state 250 and network info bit 8 will ever appear

The code decodes both as unknown fallbacks. If future firmware sends them, current decodes surface bit8 or unknown 250 instead of a name, which is safe but unlabeled. A new snapshot adding either would settle this and the allowlists are written to fail for re-triage in that case.

### Firmware-specific timing and private commands

Automated conformance covers framing and public tables, but timing, firmware-specific behavior, private bootloader transfer commands, VE.Can, and undocumented Multi RS values still require physical devices from each family (docs/SOURCES.md:341-344). Device captures remain the only settlement for those.
