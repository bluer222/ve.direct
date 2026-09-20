"""Flavor shortcuts: MPPT / Inverter / Charger / BMV / OrionXS / RS."""
from . import for_flavor as _for

MPPT_REGISTERS = _for("MPPT")
INV_REGISTERS = _for("INV")
CHG_REGISTERS = _for("CHG")
ORION_REGISTERS = _for("ORION")
BMV_REGISTERS = _for("BMV")
RS_REGISTERS = {**_for("MPPT_RS"), **_for("MULTI_RS")}
