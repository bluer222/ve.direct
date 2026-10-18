"""Log Text frames. Usage: python3 async_log.py /dev/ttyUSB0"""
import asyncio
import sys

sys.path.insert(0, "src")

from ve_direct.device import AsyncVEDirect


async def main() -> None:
    async with await AsyncVEDirect.from_port(sys.argv[1]) as dev:
        async for frame in dev.frames():
            print(frame.to_si())


asyncio.run(main())
