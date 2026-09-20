import asyncio
import struct
import sys
import unittest

sys.path.insert(0, "src")

from ve_direct.device import AsyncVEDirect
from ve_direct.transport import SimulatedTransport, make_text_block


def run(coro):
    return asyncio.run(coro)


class TestDevice(unittest.TestCase):
    def _dev(self, **kw):
        text = [make_text_block({"PID": "0xA04A", "V": "27690", "CS": "3", "ERR": "0"})]
        sim = SimulatedTransport(
            text_chunks=text,
            hex_replies={0xEDBB: struct.pack("<H", 3130)},
            **kw,
        )
        return AsyncVEDirect(sim.read_chunk, sim.write), sim

    def test_read_one(self):
        async def go():
            dev, _ = self._dev()
            f = await dev.read_one(timeout=2)
            self.assertEqual(f["CS"].human, "Bulk")
            self.assertAlmostEqual(f.to_si()["V"], 27.69)
        run(go())

    def test_hex_get(self):
        async def go():
            dev, sim = self._dev()
            v = await dev.hex_get("PANEL_VOLTAGE")
            self.assertAlmostEqual(v, 31.3)
            self.assertTrue(sim.written[0].startswith(b":7BBED"))
        run(go())

    def test_hex_set_and_readback(self):
        async def go():
            dev, _ = self._dev()
            await dev.hex_set("BAT_MAX_CURRENT", 35.0)
            v = await dev.hex_get("BAT_MAX_CURRENT")
            self.assertAlmostEqual(v, 35.0)
        run(go())

    def test_read_only_guard(self):
        async def go():
            dev, _ = self._dev()
            from ve_direct.exceptions import RegisterError
            with self.assertRaises(RegisterError):
                await dev.hex_set("PANEL_VOLTAGE", 1.0)
        run(go())

    def test_text_and_hex_share_wire(self):
        async def go():
            text = make_text_block({"PID": "0xA04A", "V": "100"})
            sim = SimulatedTransport(
                text_chunks=[text],
                hex_replies={0xEDBB: struct.pack("<H", 100)},
            )
            dev = AsyncVEDirect(sim.read_chunk, sim.write)
            f = await dev.read_one(timeout=2)
            self.assertEqual(f["V"].value, 100)
            v = await dev.hex_get("PANEL_VOLTAGE", timeout=2)
            self.assertAlmostEqual(v, 1.0)
        run(go())

    def test_split_hex_reply_reassembled(self):
        """HEX reply arriving 3 bytes at a time must still resolve."""
        async def go():
            sim = SimulatedTransport(
                text_chunks=[],
                hex_replies={0xEDBB: struct.pack("<H", 3130)},
                chunk_size=3,
            )
            dev = AsyncVEDirect(sim.read_chunk, sim.write)
            v = await dev.hex_get("PANEL_VOLTAGE", timeout=5)
            self.assertAlmostEqual(v, 31.3)
        run(go())

    def test_text_queued_during_hex(self):
        """Text arriving mid-transaction is queued, not dropped."""
        async def go():
            text = make_text_block({"PID": "0xA04A", "V": "27690", "CS": "3"})
            sim = SimulatedTransport(
                text_chunks=[text],
                hex_replies={0xEDBB: struct.pack("<H", 3130)},
                chunk_size=7,
            )
            dev = AsyncVEDirect(sim.read_chunk, sim.write)
            v = await dev.hex_get("PANEL_VOLTAGE", timeout=5)
            self.assertAlmostEqual(v, 31.3)
            f = await dev.read_one(timeout=2)
            self.assertEqual(f["V"].value, 27690)
        run(go())

    def test_hex_error_handling(self):
        """Get flags and Orion 0x2/AAAA checksum error raise without timeout."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import ChecksumError, RegisterError

        async def go():
            dev, _ = self._dev()
            loop = _aio.get_running_loop()

            # Flags error on Done reply
            fut = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut)
            bad = _H.encode_frame(_H.CMD_GET, bytes.fromhex("BBED010000"))
            dev._handle_hex(_H.decode_line(bad))
            with self.assertRaises(RegisterError):
                fut.result()

            # Orion checksum-error response (:2AAAAFF) immediately fails pending waiter
            fut2 = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut2)
            dev._handle_hex(_H.decode_line(bytes.fromhex("3A324141414146460A")))
            self.assertTrue(fut2.done())
            with self.assertRaises(ChecksumError):
                fut2.result()
        run(go())

    def test_eof_raises_transport_error(self):
        """Empty read chunk at EOF raises TransportError rather than busy-looping."""
        async def go():
            dev = AsyncVEDirect(lambda n: asyncio.sleep(0, result=b""), lambda d: None)
            from ve_direct.exceptions import TransportError
            with self.assertRaises(TransportError):
                await dev.read_one(timeout=0.1)
        run(go())

    def test_get_product_id(self):
        """Verify family-specific 2-byte and standardized 4-byte PID replies."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            dev = AsyncVEDirect(lambda n: _aio.sleep(10), lambda d: None)
            loop = _aio.get_running_loop()

            # 2-byte form rejected
            fut = loop.create_future()
            fut.set_result(_H.decode_line(_H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
            dev._command_transact = lambda cmd, expected, t: fut
            with self.assertRaises(RegisterError):
                await dev.get_product_id(timeout=0.1)

            # BMV/Phoenix officially use a two-byte command-4 payload.
            dev.flavor = "BMV"
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)

            # 4-byte form accepted
            dev.flavor = "MPPT"
            fut2 = loop.create_future()
            fut2.set_result(_H.decode_line(_H.encode_frame(_H.CMD_DONE, bytes.fromhex("0042A0FF"))))
            dev._command_transact = lambda cmd, expected, t: fut2
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)
        run(go())

    def test_simulated_product_id_shapes(self):
        async def go():
            for flavor in ("MPPT", "ORION", "BMV", "INV"):
                sim = SimulatedTransport(
                    text_chunks=[], product_id=0xA381, flavor=flavor)
                dev = AsyncVEDirect(
                    sim.read_chunk, sim.write, flavor=flavor)
                self.assertEqual(await dev.get_product_id(), 0xA381)
        run(go())

    def test_coalesced_text_checksum_and_hex_reply(self):
        from ve_direct import hex as _H

        class CoalescedTransport:
            def __init__(self):
                self.incoming = asyncio.Queue()

            async def read(self, n):
                return await self.incoming.get()

            async def write(self, data):
                request = _H.decode_line(data)
                reply = _H.encode_frame(
                    _H.CMD_GET,
                    request.data[:2] + b"\x00" + struct.pack("<H", 3130),
                )
                text = make_text_block({"PID": "0xA04A", "V": "27690"})
                self.incoming.put_nowait(text + reply)

        async def go():
            transport = CoalescedTransport()
            dev = AsyncVEDirect(
                transport.read, transport.write, flavor="MPPT")
            self.assertAlmostEqual(await dev.hex_get("PANEL_VOLTAGE"), 31.3)
            self.assertEqual((await dev.read_one())["V"].value, 27690)
        run(go())

    def test_concurrent_readers_recheck_queue(self):
        async def go():
            incoming = asyncio.Queue()
            incoming.put_nowait(
                make_text_block({"PID": "0x204", "V": "1"})
                + make_text_block({"PID": "0x204", "V": "2"})
            )

            async def read(n):
                return await incoming.get()

            dev = AsyncVEDirect(read, lambda data: None)
            one, two = await asyncio.wait_for(asyncio.gather(
                dev.read_one(), dev.read_one()), 0.5)
            self.assertEqual({one["V"].value, two["V"].value}, {1, 2})
        run(go())

    def test_cancelled_transaction_fails_closed(self):
        from ve_direct.exceptions import TransportError

        async def go():
            wrote = asyncio.Event()

            async def read(n):
                await asyncio.sleep(10)

            async def write(data):
                wrote.set()

            dev = AsyncVEDirect(read, write)
            task = asyncio.create_task(dev.hex_get_raw(0x0201))
            await wrote.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(TransportError):
                await dev.hex_get_raw(0x0201)
            dev.reset_hex_state()
        run(go())

    def test_done_cannot_resolve_register_waiter(self):
        from ve_direct import hex as _H

        async def go():
            dev, _ = self._dev()
            fut = asyncio.get_running_loop().create_future()
            dev._register_waiter = (0x4116, _H.CMD_GET, fut)
            dev._handle_hex(_H.decode_line(
                _H.encode_frame(_H.CMD_DONE, bytes.fromhex("1641"))))
            self.assertFalse(fut.done())
            fut.cancel()
        run(go())

    def test_typed_async_notification(self):
        from ve_direct import hex as _H

        dev, _ = self._dev()
        frame = _H.decode_line(_H.encode_frame(
            _H.CMD_ASYNC,
            bytes.fromhex("BBED00") + struct.pack("<H", 3130),
        ))
        decoded = dev.decode_async_frame(frame, flavor="MPPT")
        self.assertEqual(decoded["name"], "PANEL_VOLTAGE")
        self.assertAlmostEqual(decoded["value"], 31.3)

    def test_boot_and_serial_power(self):
        """Verify boot sends :0 frame and from_port accepts dtr/rts."""
        async def go():
            sent = []
            dev = AsyncVEDirect(lambda n: asyncio.sleep(10), lambda d: sent.append(d))
            await dev.boot()
            self.assertEqual(sent, [bytes.fromhex("3A30353146413531464135314641353146413531464144450A")])
        run(go())
        import inspect
        sig = inspect.signature(AsyncVEDirect.from_port)
        self.assertIn("dtr", sig.parameters)
        self.assertIn("rts", sig.parameters)

    def test_orion_app_version_info(self):
        """Verify get_app_version_info() queries 0x0102 for Orion without TypeError."""
        import asyncio as _aio
        from ve_direct import hex as _H
        async def go():
            dev = AsyncVEDirect(lambda n: _aio.sleep(10), lambda d: None, flavor="ORION")
            fut = _aio.get_running_loop().create_future()
            # Register 0x0102 reply: 4 bytes payload: 00 FF 02 01 (release v1.02)
            frame = _H.decode_line(_H.encode_frame(_H.CMD_GET, bytes.fromhex("020100FF0201")))
            fut.set_result(frame)
            dev.hex_get_raw = lambda reg_id, timeout=2.0: fut
            info = await dev.get_app_version_info()
            self.assertEqual(info["version"], "1.02")
            self.assertEqual(info["type"], "release")
        run(go())

    def test_close_flag(self):
        async def go():
            dev, _ = self._dev()
            await dev.close()
            from ve_direct.exceptions import TransportError
            with self.assertRaises(TransportError):
                await dev.read_one(timeout=1)
        run(go())


    def test_concurrent_frames_and_hex_transactions(self):
        """Verify hex_get/hex_set during continuous frames() reading with tight timeouts."""
        import asyncio
        from ve_direct.device import AsyncVEDirect
        from ve_direct import hex as _H

        class MockTransport:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.closed = False

            async def read(self, n=4096):
                if self.closed and self.incoming.empty():
                    return b""
                return await self.incoming.get()

            async def write(self, data):
                cmd = bytes(data)
                if b":7BBED00" in cmd:  # hex_get(0xEDBB) -> PANEL_VOLTAGE
                    reply = _H.encode_frame(_H.CMD_GET, b"\xBB\xED\x00\xE0\x07")
                    self.incoming.put_nowait(reply)
                elif b":8000200" in cmd:  # hex_set(0x0200, 1)
                    reply = _H.encode_frame(_H.CMD_SET, b"\x00\x02\x00\x01")
                    self.incoming.put_nowait(reply)

            def close(self):
                self.closed = True

        async def go():
            transport = MockTransport()
            def make_text_frame(v):
                body = f"PID\t0xA042\r\nV\t{v}\r\nChecksum\t"
                s = sum(body.encode("ascii"))
                cs = (256 - (s % 256)) % 256
                return body.encode("ascii") + bytes([cs])

            dev = AsyncVEDirect(transport.read, transport.write, flavor="MPPT")

            async def feeder():
                v = 12000
                while not transport.closed:
                    transport.incoming.put_nowait(make_text_frame(v))
                    v += 10
                    await asyncio.sleep(0.005)

            feeder_task = asyncio.create_task(feeder())

            frames_received = []
            async def frame_reader():
                async for f in dev.frames():
                    frames_received.append(f)
                    if len(frames_received) >= 15:
                        break

            reader_task = asyncio.create_task(frame_reader())

            for _ in range(4):
                val = await dev.hex_get(0xEDBB, timeout=0.2)
                self.assertAlmostEqual(val, 20.16, places=2)
                await dev.hex_set(0x0200, 1, timeout=0.2)
                await asyncio.sleep(0.01)

            await reader_task
            feeder_task.cancel()
            transport.close()
            await dev.close()
            self.assertGreaterEqual(len(frames_received), 15)

        run(go())


if __name__ == "__main__":
    unittest.main()
