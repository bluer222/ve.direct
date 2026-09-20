#!/usr/bin/env python3
"""
Parsers for official Victron Energy VE.Direct protocol specifications.

Reads extracted text files for:
  1. BlueSolar MPPT HEX protocol (Rev 18)
  2. Orion-XS DC-DC charger HEX protocol
  3. BMV-7xx & SmartShunt HEX protocol (Rev 7)
  4. Phoenix Inverter HEX protocol (Rev 5)
  5. VE.Direct Text Mode protocol (Rev 3.34)
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


@dataclass
class ParsedRegister:
    id: int
    name: str
    description: str
    scale: Optional[float] = None
    data_type: str = "unknown"
    unit: Optional[str] = None
    access: str = "R/W"
    source: str = ""
    notes: str = ""
    sentinels: Set[int] = field(default_factory=set)


@dataclass
class ParsedTextField:
    label: str
    units: str
    description: str
    devices: List[str] = field(default_factory=list)


@dataclass
class ParsedPID:
    pid: int
    model_name: str
    phased_out: bool = False


class BlueSolarHEXParser:
    """Parser for BlueSolar / SmartSolar MPPT HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> BlueSolarHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        start = self.text.find("1.1 Get and Set items")
        end = self.text.find("1.2 Asynchronous items")
        section = self.text[start:end] if end > start else self.text[start:]

        pattern_std = re.compile(
            r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#]+?)\s{2,}([0-9.]+|\-)\s+([a-z0-9*()]+)\s*(\S.*)?$"
        )
        pattern_display = re.compile(
            r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#=]+?)\s{2,}(un8|un16|un32|sn16|sn32|string)\s*(\S.*)?$"
        )

        for line in section.splitlines():
            if not re.match(r"^\s*0x[0-9A-Fa-f]{4}\b", line):
                continue
            if any(skip in line for skip in ["0xEDEF will always", "A VE.Direct TX", "firmware version"]):
                continue

            m = pattern_std.match(line)
            if m:
                rid = int(m.group(1), 16)
                desc = m.group(2).strip()
                scale_str = m.group(3).strip()
                dtype = m.group(4).strip()
                unit_str = m.group(5).strip() if m.group(5) else None

                scale = float(scale_str) if scale_str != "-" else None
                sentinels = set()
                if unit_str and "0xFFFF" in unit_str:
                    sentinels.add(0xFFFF)

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=re.sub(r"\s*\(\*.*?\)", "", desc).strip().upper().replace(" ", "_").replace("-", "_").replace(".", "_"),
                    description=desc,
                    scale=scale,
                    data_type=dtype,
                    unit=unit_str,
                    source="BlueSolar-HEX",
                    sentinels=sentinels,
                )
                continue

            m_disp = pattern_display.match(line)
            if m_disp:
                rid = int(m_disp.group(1), 16)
                desc = m_disp.group(2).strip()
                dtype = m_disp.group(3).strip()
                unit_str = m_disp.group(4).strip() if m_disp.group(4) else None

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=re.sub(r"\s*\(\*.*?\)", "", desc).split("(")[0].strip().upper().replace(" ", "_").replace("-", "_"),
                    description=desc,
                    scale=None,
                    data_type=dtype,
                    unit=unit_str,
                    source="BlueSolar-HEX",
                )
                continue

            if "0x0004" in line and "Restore" in line:
                regs[0x0004] = ParsedRegister(0x0004, "RESTORE_DEFAULT", "Restore default", None, "write_only", None, access="W", source="BlueSolar-HEX")
            elif "0x1030" in line and "Clear history" in line:
                regs[0x1030] = ParsedRegister(0x1030, "CLEAR_HISTORY", "Clear history", None, "write_only", None, access="W", source="BlueSolar-HEX")
            elif "0x104F" in line and "Total history" in line:
                regs[0x104F] = ParsedRegister(0x104F, "TOTAL_HISTORY", "Total history", None, "struct", None, access="R", source="BlueSolar-HEX")

        return regs

    def parse_capabilities(self) -> Dict[int, str]:
        caps: Dict[int, str] = {}
        pos = self.text.find("Capabilities (register 0x0140)")
        if pos < 0:
            return caps
        section = self.text[pos : pos + 1500]
        for line in section.splitlines():
            m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z0-9_()./,\-\s]+?)(?:\s*\(0=no.*)?$", line)
            if m:
                bit = int(m.group(1))
                name = m.group(2).strip()
                if "Bit" not in name and "register" not in name:
                    caps[bit] = name
        return caps


class OrionXSHEXParser:
    """Parser for Orion-XS HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> OrionXSHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        start = self.text.find("1.1 Get and Set items")
        end = self.text.find("1.2 Asynchronous items")
        section = self.text[start:end] if end > start else self.text[start:]

        pattern = re.compile(
            r"^\s*(0x[0-9A-Fa-f]{4})\s+([A-Za-z0-9_()./,\-\s\*\#]+?)\s{2,}([0-9.]+|\-)\s+([a-z0-9]+)\s*(\S.*)?$"
        )

        for line in section.splitlines():
            m = pattern.match(line)
            if m:
                rid = int(m.group(1), 16)
                desc = m.group(2).strip()
                scale_str = m.group(3).strip()
                dtype = m.group(4).strip()
                unit_str = m.group(5).strip() if m.group(5) else None

                scale = float(scale_str) if scale_str != "-" else None
                sentinels = set()
                if unit_str and "0xFFFF" in unit_str:
                    sentinels.add(0xFFFF)

                regs[rid] = ParsedRegister(
                    id=rid,
                    name=re.sub(r"\s*\(\*.*?\)", "", desc).strip().upper().replace(" ", "_").replace("-", "_").replace(".", "_"),
                    description=desc,
                    scale=scale,
                    data_type=dtype,
                    unit=unit_str,
                    source="Orion-XS-HEX",
                    sentinels=sentinels,
                )
        return regs

    def parse_capabilities(self) -> Dict[int, str]:
        caps: Dict[int, str] = {}
        pos = self.text.find("Capabilities1 (register 0x0140)")
        if pos < 0:
            return caps
        section = self.text[pos : pos + 1800]
        for line in section.splitlines():
            m = re.match(r"^\s*(\d{1,2})\s+([A-Za-z0-9_()./,\-\s]+?)(?:\s*\(0=no.*)?$", line)
            if m:
                bit = int(m.group(1))
                name = m.group(2).strip()
                if "Bit" not in name and "register" not in name:
                    caps[bit] = name
        return caps


class BMV7xxHEXParser:
    """Parser for BMV-7xx and SmartShunt HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> BMV7xxHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        for line in self.text.splitlines():
            if not re.match(r"^\s*0x[0-9A-Fa-f]{4}\b", line):
                continue
            if any(skip in line for skip in ["talking to", "BMV712 = 0xA381", "0xA381 = BMV712"]):
                continue

            parts = re.split(r"\s{2,}", line.strip())
            if not parts or not re.match(r"^0x[0-9A-Fa-f]{4}$", parts[0]):
                continue

            rid = int(parts[0], 16)
            desc = parts[1] if len(parts) > 1 else ""
            dtype = "unknown"
            scale = None
            unit = None

            is_write_only = "(write only)" in desc.lower()
            for p in parts[2:]:
                p_clean = p.strip()
                if re.match(r"^(un8|un16|un24|un32|sn8|sn16|sn32|string\d*)$", p_clean, re.IGNORECASE):
                    dtype = p_clean.lower()
                elif not is_write_only and re.match(r"^(0?\.\d+)\s*([A-Za-z%]+)?$", p_clean):
                    sm = re.match(r"^(0?\.\d+)\s*([A-Za-z%]+)?$", p_clean)
                    if sm:
                        scale = float(sm.group(1))
                        unit = sm.group(2)
                elif p_clean in ["V", "A", "W", "Ah", "seconds", "minutes", "K", "%"]:
                    unit = p_clean

            if is_write_only:
                access = "WRITE_ONLY"
                scale = None
            elif "(read-only)" in desc.lower() or "(read only)" in desc.lower():
                access = "READ_ONLY"
            else:
                access = "READ_WRITE"
            clean_desc = re.sub(r"\s*\(read-only\)|\s*\(read only\)|\s*\(\*\)|\s*\(\d+\)", "", desc).strip()
            name = "BMV_" + clean_desc.upper().replace(" ", "_").replace("-", "_").replace(".", "_")

            regs[rid] = ParsedRegister(
                id=rid,
                name=name,
                description=desc,
                scale=scale,
                data_type=dtype,
                unit=unit,
                access=access,
                source="BMV-7xx-HEX",
            )
        return regs


class PhoenixInverterHEXParser:
    """Parser for Phoenix Inverter HEX protocol documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> PhoenixInverterHEXParser:
        return cls(path.read_text(errors="ignore"))

    def parse_registers(self) -> Dict[int, ParsedRegister]:
        regs: Dict[int, ParsedRegister] = {}
        for line in self.text.splitlines():
            if "0x0004" in line and "VE_REG_RESTORE_DEFAULT" in line:
                regs[0x0004] = ParsedRegister(0x0004, "VE_REG_RESTORE_DEFAULT", "Restore user defaults", None, "write_only", None, access="W", source="Phoenix-HEX")
                continue
            if not re.match(r"^\s*0x[0-9A-Fa-f]{4}\b", line) or "VE_REG_" not in line:
                continue

            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 4:
                continue

            rid = int(parts[0], 16)
            name = parts[1].strip()
            access = parts[2].strip()
            dtype = parts[3].strip()

            scale = None
            unit = None
            if len(parts) >= 5:
                p4 = parts[4].strip()
                if not p4.endswith(")") and p4 != "-":
                    try:
                        scale = float(p4)
                    except ValueError:
                        scale = None
            if len(parts) >= 6:
                p5 = parts[5].strip()
                if not p5.endswith(")") and p5 != "-":
                    unit = re.sub(r"\s*\d+\)", "", p5).strip()
                    if unit in ("-", ""):
                        unit = None

            regs[rid] = ParsedRegister(
                id=rid,
                name=name,
                description=name,
                scale=scale,
                data_type=dtype,
                unit=unit,
                access=access,
                source="Phoenix-HEX",
            )
        return regs


class VEDirectTextParser:
    """Parser for VE.Direct Text Mode Protocol 3.34 documentation."""

    def __init__(self, text: str):
        self.text = text

    @classmethod
    def from_file(cls, path: Path) -> VEDirectTextParser:
        return cls(path.read_text(errors="ignore"))

    def parse_fields(self) -> Dict[str, ParsedTextField]:
        fields: Dict[str, ParsedTextField] = {}
        start = self.text.find("The units used by the serial interface are as follows:")
        end = self.text.find("Table I Supported Text-mode fields")
        section = self.text[start:end] if end > start else ""

        for line in section.splitlines():
            m = re.match(r"^\s{10,25}([A-Za-z0-9_#]+)\s{2,}([A-Za-z0-9/%#\-\.\s]+?)\s{2,}(.+)$", line)
            if m:
                label = m.group(1).strip()
                if label in ["Label", "Table", "The", "Note", "Victron", "General", "E-mail", "When", "Both", "Available", "60x"]:
                    continue
                units = m.group(2).strip()
                desc = m.group(3).strip()
                fields[label] = ParsedTextField(label=label, units=units, description=desc)
        return fields

    def parse_pids(self) -> Dict[int, ParsedPID]:
        pids: Dict[int, ParsedPID] = {}
        pos = self.text.find("PID\n                       The product id:")
        if pos < 0:
            pos = self.text.find("The product id:")
        section = self.text[pos:]

        for line in section.splitlines():
            if "Implementation guidelines" in line or "Connector types" in line:
                break
            m = re.search(r"^\s*([A-Za-z0-9\s|/\-()]+?)\s{2,}(0x[0-9A-Fa-f]{3,4})(\*)?", line)
            if m:
                name = m.group(1).strip()
                pid_hex = m.group(2).strip()
                phased = bool(m.group(3))
                if not any(kw in name for kw in ["Product", "register", "vreg", "Command", "Table"]):
                    pid = int(pid_hex, 16)
                    pids[pid] = ParsedPID(pid=pid, model_name=name, phased_out=phased)
        return pids

    def parse_cs_states(self) -> Dict[int, str]:
        states: Dict[int, str] = {}
        pos = self.text.find("The state of operation. See the table below")
        if pos < 0:
            return states
        section = self.text[pos : pos + 1000]
        for line in section.splitlines():
            m = re.match(r"^\s*([A-Za-z\s/()]+?)\s+(\d{1,3})\s+(?:•|\d)", line)
            if m:
                name = m.group(1).strip()
                code = int(m.group(2))
                states[code] = name
        return states

    def parse_errors(self) -> Dict[int, str]:
        errors: Dict[int, str] = {}
        pos = self.text.find("The error code of the device (relevant when the device is in the fault state)")
        if pos < 0:
            return errors
        section = self.text[pos : pos + 1500]
        for line in section.splitlines():
            m = re.match(r"^\s*([A-Za-z0-9\s/(),\-]+?)\s{2,}(\d{1,3})\s*$", line)
            if m:
                desc = m.group(1).strip()
                code = int(m.group(2))
                errors[code] = desc
        return errors


def load_all_specs(specs_dir: Path) -> Dict[str, Any]:
    return {
        "bluesolar": BlueSolarHEXParser.from_file(specs_dir / "BlueSolar-HEX-protocol.txt"),
        "orion": OrionXSHEXParser.from_file(specs_dir / "Orion-XS-HEX-protocol.txt"),
        "bmv": BMV7xxHEXParser.from_file(specs_dir / "BMV-7xx-HEX-Protocol.txt"),
        "phoenix": PhoenixInverterHEXParser.from_file(specs_dir / "VE.Direct-HEX-Protocol-Phoenix-Inverter.txt"),
        "text": VEDirectTextParser.from_file(specs_dir / "VE.Direct-Protocol-3.34.txt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse official Victron Energy specifications.")
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "specs",
        help="Directory containing extracted specification text files.",
    )
    args = parser.parse_args()

    specs = load_all_specs(args.specs_dir)
    bs_regs = specs["bluesolar"].parse_registers()
    or_regs = specs["orion"].parse_registers()
    bmv_regs = specs["bmv"].parse_registers()
    ph_regs = specs["phoenix"].parse_registers()
    text_fields = specs["text"].parse_fields()
    pids = specs["text"].parse_pids()
    caps_bs = specs["bluesolar"].parse_capabilities()
    caps_or = specs["orion"].parse_capabilities()

    print(f"Parsed Specifications Summary ({args.specs_dir}):")
    print(f"  BlueSolar MPPT HEX: {len(bs_regs)} registers, {len(caps_bs)} capability bits")
    print(f"  Orion-XS HEX:      {len(or_regs)} registers, {len(caps_or)} capability bits")
    print(f"  BMV-7xx HEX:        {len(bmv_regs)} registers")
    print(f"  Phoenix Inverter:   {len(ph_regs)} registers")
    print(f"  Text Mode Protocol: {len(text_fields)} fields, {len(pids)} product IDs")

    # Detect aliased IDs across specs
    all_spec_regs: Dict[int, List[ParsedRegister]] = {}
    for r in list(bs_regs.values()) + list(or_regs.values()) + list(bmv_regs.values()) + list(ph_regs.values()):
        all_spec_regs.setdefault(r.id, []).append(r)

    aliased = {rid: rlist for rid, rlist in all_spec_regs.items() if len(rlist) > 1}
    print(f"\nDiscovered {len(aliased)} shared/aliased register IDs across HEX specs:")
    for rid, rlist in sorted(aliased.items()):
        sources = ", ".join(r.source for r in rlist)
        scales = "/".join(str(r.scale) for r in rlist)
        print(f"  0x{rid:04X}: scales=[{scales}], sources=[{sources}]")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
