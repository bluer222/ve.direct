"""Log Text frames. Usage: python3 async_log.py /dev/ttyUSB0 [--sim]"""
import asyncio
import sys

sys.path.insert(0, "src")

from ve_direct.device import AsyncVEDirect
from ve_direct.transport import SimulatedTransport, make_text_block


async def main() -> None:
    if "--sim" in sys.argv or len(sys.argv) < 2:
        sim = SimulatedTransport(text_chunks=[
            make_text_block({"PID": "0xA04A", "V": "27690", "I": "4400",
                             "VPV": "31300", "PPV": "125", "CS": "3",
                             "MPPT": "2", "ERR": "0"}),
        ])
        dev = AsyncVEDirect(sim.read_chunk, sim.write)
        print("--- SIMULATED (pass a port for hardware) ---")
        print((await dev.read_one(timeout=2)).to_si())
        return
    async with await AsyncVEDirect.from_port(sys.argv[1]) as dev:
        async for frame in dev.frames():
            print(frame.to_si())


asyncio.run(main())
