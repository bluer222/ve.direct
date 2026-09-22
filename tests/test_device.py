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

    def test_handle_hex_rejects_invalid(self):
        from ve_direct import hex as _H
        from ve_direct.exceptions import ChecksumError
        dev, _ = self._dev()
        bad = _H.decode_line(_H.build_get(0x0201))
        object.__setattr__(bad, "valid", False)
        with self.assertRaises(ChecksumError):
            dev._handle_hex(bad)

    def test_product_id_legacy_and_shape(self):
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            # Legacy pre-1.30 MPPT 2-byte fallback (BlueSolar §1.1 Note 1).
            dev = AsyncVEDirect(lambda n: _aio.sleep(10), lambda d: None,
                                flavor="MPPT")
            fut = loop.create_future()
            fut.set_result(_H.decode_line(
                _H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
            dev._command_transact = lambda cmd, expected, t: fut
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)
            # Malformed 4-byte shape rejected (want 00 <PID> FF).
            for badhex in ("0142A0FF", "0042A000", "0042A0"):
                dev2 = AsyncVEDirect(
                    lambda n: _aio.sleep(10), lambda d: None, flavor="MPPT")
                f = loop.create_future()
                raw = bytes.fromhex(badhex)
                if len(raw) == 3:
                    # pad to exercise invalid-length path via direct frame
                    f.set_result(_H.decode_line(
                        _H.encode_frame(_H.CMD_DONE, raw)))
                else:
                    f.set_result(_H.decode_line(
                        _H.encode_frame(_H.CMD_DONE, raw)))
                dev2._command_transact = lambda cmd, expected, t, _f=f: _f
                with self.assertRaises(RegisterError, msg=badhex):
                    await dev2.get_product_id(timeout=0.1)
        run(go())

    def test_error_framing_vs_boot(self):
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import ChecksumError, RegisterError

        async def go():
            loop = _aio.get_running_loop()
            dev, _ = self._dev()
            # :4AAAA framing error -> ChecksumError
            fut = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut)
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_ERROR, bytes.fromhex("AAAA"))))
            with self.assertRaises(ChecksumError):
                fut.result()
            # :4 with payload 0 (unable to enter bootloader) -> RegisterError
            fut2 = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut2)
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_ERROR, b"\x00")))
            with self.assertRaises(RegisterError):
                fut2.result()
            # :2AAAA (Orion checksum error) -> ChecksumError
            fut3 = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut3)
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_CHECKSUM_ERROR, bytes.fromhex("AAAA"))))
            with self.assertRaises(ChecksumError):
                fut3.result()
        run(go())

    def test_sim_round_trips(self):
        from ve_direct import hex as _H

        async def go():
            # Restart :64F sends no reply.
            sim = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
            await dev.restart()
            self.assertEqual(sim.written[-1], b":64F\n")
            self.assertEqual(bytes(sim._pending_out), b"")
            # Ping :154 -> :5... (BlueSolar §1.3 :51641F9).
            sim2 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev2 = AsyncVEDirect(sim2.read_chunk, sim2.write, flavor="MPPT")
            f = await dev2.ping(timeout=2)
            self.assertEqual(f.cmd, _H.CMD_PING)
            self.assertEqual(_H.decode_ping_version(f.data)["version"], "1.16")
            # Orion ping 0xYFFF no-version form.
            sim3 = SimulatedTransport(text_chunks=[], flavor="ORION")
            dev3 = AsyncVEDirect(sim3.read_chunk, sim3.write, flavor="ORION")
            f3 = await dev3.ping(timeout=2)
            self.assertEqual(f3.data, b"\xFF\x7F")
            self.assertIsNone(_H.decode_ping_version(f3.data)["version"])
            # App-version :352 -> Done (BlueSolar §1.3 :11641FD).
            sim4 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev4 = AsyncVEDirect(sim4.read_chunk, sim4.write, flavor="MPPT")
            raw = await dev4.get_app_version(timeout=2)
            self.assertEqual(raw, bytes.fromhex("1641"))
            self.assertEqual(sim4.written[-1], b":352\n")
            line = _H.encode_frame(_H.CMD_DONE, bytes.fromhex("1641"))
            self.assertEqual(line, b":11641FD\n")
            # Release-candidate ping 0xD101 -> 1.01-RCD (also via sim).
            info = _H.decode_ping_version(bytes.fromhex("01D1"))
            self.assertEqual(info["version"], "1.01-RCD")
            sim_rc = SimulatedTransport(text_chunks=[], flavor="MPPT",
                                        app_version=bytes.fromhex("01D1"))
            dev_rc = AsyncVEDirect(sim_rc.read_chunk, sim_rc.write,
                                   flavor="MPPT")
            f_rc = await dev_rc.ping(timeout=2)
            self.assertEqual(
                _H.decode_ping_version(f_rc.data)["version"], "1.01-RCD")
            # Unknown-response routing :3020050.
            sim5 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev5 = AsyncVEDirect(sim5.read_chunk, sim5.write, flavor="MPPT")
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            dev5._register_waiter = (
                0x0201, _H.CMD_GET, fut)
            dev5._handle_hex(_H.decode_line(b":3020050\n"))
            from ve_direct.exceptions import RegisterError
            with self.assertRaises(RegisterError):
                fut.result()
            # Orion deprecated :3 -> Unknown incl. fallback to 0x0102.
            sim6 = SimulatedTransport(
                text_chunks=[], flavor="ORION",
                hex_replies={0x0102: bytes.fromhex("00FF0201")})
            dev6 = AsyncVEDirect(sim6.read_chunk, sim6.write, flavor="ORION")
            info6 = await dev6.get_app_version_info(timeout=2)
            self.assertEqual(info6["version"], "1.02")
            # Non-Orion has no 0x0102 (BlueSolar §1.1 lists only
            # 0x0100/0x0104/0x010A/0x010B/0x0140): :3 Unknown propagates
            # as RegisterError with no undocumented 0x0102 fallback.
            sim6b = SimulatedTransport(
                text_chunks=[], flavor="ORION",
                hex_replies={0x0102: bytes.fromhex("00FF0201")})
            dev6b = AsyncVEDirect(sim6b.read_chunk, sim6b.write,
                                   flavor="MPPT")
            with self.assertRaises(RegisterError):
                await dev6b.get_app_version_info(timeout=2)
            # Error :4AAAA framing-error routing into _handle_hex.
            from ve_direct.exceptions import ChecksumError
            fut2 = loop.create_future()
            dev5._register_waiter = (0x0201, _H.CMD_GET, fut2)
            dev5._handle_hex(_H.decode_line(b":4AAAAFD\n"))
            with self.assertRaises(ChecksumError):
                fut2.result()
            # async_notifications() pump with injected :A frame.
            import struct as _s
            sim7 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev7 = AsyncVEDirect(sim7.read_chunk, sim7.write, flavor="MPPT")
            sim7.inject_async(0x0201, b"\x05")
            gen = dev7.async_notifications(flavor="MPPT")
            note = await asyncio.wait_for(gen.__anext__(), 2)
            self.assertEqual(note["register_id"], 0x0201)
            await gen.aclose()
        run(go())

    def test_sim_framing_unknown_boot_width(self):
        from ve_direct import hex as _H

        async def go():
            # Corrupt-checksum Get -> documented error replies.
            for flavor, want in (("ORION", b":2AAAAFF\n"),
                                 ("MPPT", b":4AAAAFD\n")):
                sim = SimulatedTransport(text_chunks=[], flavor=flavor)
                good = _H.build_get(0x0201)
                bad = bytearray(good)
                bad[-3] = ord("0") if bad[-3] != ord("0") else ord("1")
                await sim.write(bytes(bad))
                self.assertEqual(bytes(sim._pending_out), want)
            # Reserved/unknown command -> Unknown reply (:2000152 -> :3020050).
            sim = SimulatedTransport(text_chunks=[], flavor="ORION")
            await sim.write(_H.encode_frame(0x2, bytes.fromhex("0001")))
            self.assertEqual(bytes(sim._pending_out), b":3020050\n")
            # Boot :0 -> no reply (never Unknown).
            sim2 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim2.write(_H.build_boot())
            self.assertEqual(bytes(sim2._pending_out), b"")
            # Width-correct zero payloads from register DB.
            sim3 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim3.write(_H.build_get(0xEDBC))  # PANEL_POWER uint32
            f = _H.decode_line(bytes(sim3._pending_out))
            self.assertEqual(len(f.value_bytes), 4)
            sim4 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim4.write(_H.build_get(0xEDBB))  # PANEL_VOLTAGE uint16
            f4 = _H.decode_line(bytes(sim4._pending_out))
            self.assertEqual(len(f4.value_bytes), 2)
            # Default non-Orion ping is application (BlueSolar §1.3 0x4116).
            sim5 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim5.write(_H.build_ping())
            fp = _H.decode_line(bytes(sim5._pending_out))
            self.assertEqual(_H.decode_ping_version(fp.data)["type"],
                             "application")
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
            frame = _H.decode_line(_H.encode_frame(_H.CMD_GET, bytes.fromhex("02010000FF0201")))
            fut.set_result(frame)
            dev.hex_get_raw = lambda reg_id, timeout=2.0: fut
            info = await dev.get_app_version_info()
            self.assertEqual(info["version"], "1.02")
            self.assertEqual(info["type"], "release")
        run(go())

    def test_non_orion_app_version_unknown_propagates(self):
        """Non-Orion :3 Unknown must not fall back to 0x0102 (BlueSolar §1.1)."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            for flavor in ("MPPT", None):
                dev = AsyncVEDirect(
                    lambda n: _aio.sleep(10), lambda d: None, flavor=flavor)
                loop = _aio.get_running_loop()
                fut = loop.create_future()
                fut.set_exception(RegisterError("device reports Unknown"))
                dev.get_app_version = lambda timeout=2.0, _f=fut: _f
                called = []
                orig = dev.hex_get_raw
                async def fail_get(rid, timeout=2.0):
                    called.append(rid)
                    raise AssertionError("0x0102 fallback must not run")
                dev.hex_get_raw = fail_get
                with self.assertRaises(RegisterError):
                    await dev.get_app_version_info(timeout=0.1)
                self.assertEqual(called, [])
                dev.hex_get_raw = orig
        run(go())

    def test_sim_boot_failure_reply(self):
        """boot_fails=True emits documented :40051 (Error payload 0x00)."""
        from ve_direct import hex as _H

        async def go():
            # Success default: silence.
            sim = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim.write(_H.build_boot())
            self.assertEqual(bytes(sim._pending_out), b"")
            # Failure opt-in: :4 payload 0x00.
            sim2 = SimulatedTransport(
                text_chunks=[], flavor="MPPT", boot_fails=True)
            await sim2.write(_H.build_boot())
            self.assertEqual(bytes(sim2._pending_out), b":40051\n")
            f = _H.decode_line(bytes(sim2._pending_out))
            self.assertEqual(f.cmd, _H.CMD_ERROR)
            self.assertEqual(bytes(f.data), b"\x00")
        run(go())

    def test_truncated_async_rejected(self):
        """Truncated :A (id only, no flags/value) raises RegisterError."""
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError
        dev, _ = self._dev()
        for payload in (bytes.fromhex("0102"), bytes.fromhex("01")):
            frame = _H.decode_line(_H.encode_frame(_H.CMD_ASYNC, payload))
            with self.assertRaises(RegisterError, msg=payload.hex()):
                dev.decode_async_frame(frame, flavor="MPPT")

    def test_truncated_get_set_does_not_resolve(self):
        """2-byte id-only Get/Set (e.g. id 0xEDBB) must not resolve waiter."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            for cmd in (_H.CMD_GET, _H.CMD_SET):
                dev, _ = self._dev()
                fut = loop.create_future()
                dev._register_waiter = (0xEDBB, cmd, fut)
                frame = _H.decode_line(_H.encode_frame(cmd, bytes.fromhex("BBED")))
                with self.assertRaises(RegisterError, msg=hex(cmd)):
                    dev._handle_hex(frame)
                self.assertFalse(fut.done(), msg=hex(cmd))
                fut.cancel()
        run(go())

    def test_empty_done_rejected(self):
        """Empty/short Done must fail waiter; get_app_version() raises."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            for payload in (b"", b"\x00"):
                dev, _ = self._dev()
                fut = loop.create_future()
                dev._command_waiter = (_H.CMD_DONE, fut)
                frame = _H.decode_line(_H.encode_frame(_H.CMD_DONE, payload))
                dev._handle_hex(frame)
                self.assertTrue(fut.done())
                with self.assertRaises(RegisterError, msg=payload.hex()):
                    fut.result()
            # Accessor layer also rejects (mocked transact bypassing _handle_hex).
            dev2, _ = self._dev()
            fut2 = loop.create_future()
            fut2.set_result(_H.decode_line(_H.encode_frame(_H.CMD_DONE, b"")))
            dev2._command_transact = lambda cmd, expected, t: fut2
            with self.assertRaises(RegisterError):
                await dev2.get_app_version(timeout=0.1)
        run(go())

    def test_empty_ping_rejected(self):
        """Empty/short PingResp must fail waiter; ping() raises."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            for payload in (b"", b"\x00"):
                dev, _ = self._dev()
                fut = loop.create_future()
                dev._command_waiter = (_H.CMD_PING, fut)
                frame = _H.decode_line(_H.encode_frame(_H.CMD_PING, payload))
                dev._handle_hex(frame)
                self.assertTrue(fut.done())
                with self.assertRaises(RegisterError, msg=payload.hex()):
                    fut.result()
            dev2, _ = self._dev()
            fut2 = loop.create_future()
            fut2.set_result(_H.decode_line(_H.encode_frame(_H.CMD_PING, b"")))
            dev2._command_transact = lambda cmd, expected, t: fut2
            with self.assertRaises(RegisterError):
                await dev2.ping(timeout=0.1)
        run(go())

    def test_product_id_rs_rejects_two_byte(self):
        """MPPT_RS/ORION 2-byte PID rejected (BlueSolar §1.1: ignore)."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            for flavor in ("MPPT_RS", "MULTI_RS", "ORION"):
                dev = AsyncVEDirect(
                    lambda n: _aio.sleep(10), lambda d: None, flavor=flavor)
                fut = loop.create_future()
                fut.set_result(_H.decode_line(
                    _H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
                dev._command_transact = lambda cmd, expected, t: fut
                with self.assertRaises(RegisterError, msg=flavor):
                    await dev.get_product_id(timeout=0.1)
            # MPPT 2-byte legacy still accepted.
            dev = AsyncVEDirect(
                lambda n: _aio.sleep(10), lambda d: None, flavor="MPPT")
            fut = loop.create_future()
            fut.set_result(_H.decode_line(
                _H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
            dev._command_transact = lambda cmd, expected, t: fut
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)
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
