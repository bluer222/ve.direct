"""HEX control. Usage: python3 async_control.py /dev/ttyUSB0"""
import asyncio
import sys

sys.path.insert(0, "src")

from ve_direct.device import AsyncVEDirect


async def main() -> None:
    port = sys.argv[1]
    async with await AsyncVEDirect.from_port(port) as dev:
        print("panel V:", await dev.hex_get("PANEL_VOLTAGE"))
        print("state:", await dev.hex_get("DEVICE_STATE"))
        print("charger V/A:", await dev.hex_get("CHARGER_VOLTAGE"),
              await dev.hex_get("CHARGER_CURRENT"))
        # Writes (uncomment carefully — check battery limits first!):
        # await dev.hex_set("BAT_MAX_CURRENT", 35.0)
        # await dev.hex_set("RELAY_CONTROL", True)
        # await dev.hex_set("LOAD_OUTPUT_CONTROL", {"mode": 1})  # AUTO


asyncio.run(main())
