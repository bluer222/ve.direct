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

    def test_split_hex_reply_reassembled(self):
        """HEX reply arriving 3 bytes at a time must still resolve."""
        # Includes test_hex_get: named Get decodes 31.3 V and sends :7BBED.
        async def go():
            sim = SimulatedTransport(
                text_chunks=[],
                hex_replies={0xEDBB: struct.pack("<H", 3130)},
                chunk_size=3,
            )
            dev = AsyncVEDirect(sim.read_chunk, sim.write)
            v = await dev.hex_get("PANEL_VOLTAGE", timeout=5)
            self.assertAlmostEqual(v, 31.3)
            self.assertTrue(sim.written[0].startswith(b":7BBED"))
        run(go())

    def test_text_queued_during_hex(self):
        """Text arriving mid-transaction is queued, not dropped."""
        # Includes test_read_one: CS=3 means Bulk and 27690 mV is 27.69 V.
        # Includes test_text_and_hex_share_wire: one transport delivers both
        # a parsed Text frame and a scaled HEX Get result in either order.
        async def go():
            text_first = make_text_block({"PID": "0xA04A", "V": "100"})
            sim_first = SimulatedTransport(
                text_chunks=[text_first],
                hex_replies={0xEDBB: struct.pack("<H", 100)},
            )
            dev_first = AsyncVEDirect(sim_first.read_chunk, sim_first.write)
            self.assertEqual((await dev_first.read_one(timeout=2))["V"].value, 100)
            self.assertAlmostEqual(
                await dev_first.hex_get("PANEL_VOLTAGE", timeout=2), 1.0)
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
            self.assertEqual(f["CS"].human, "Bulk")
            self.assertAlmostEqual(f.to_si()["V"], 27.69)
        run(go())

    def test_handle_hex_rejects_invalid(self):
        from ve_direct import hex as _H
        from ve_direct.exceptions import ChecksumError

        async def go():
            dev, _ = self._dev()
            bad = _H.decode_line(_H.build_get(0x0201))
            object.__setattr__(bad, "valid", False)
            with self.assertRaises(ChecksumError):
                dev._handle_hex(bad)
        run(go())

    def test_product_id_rejects_malformed_shape(self):
        # Command :4 requires exactly two bytes. Even a valid four-byte
        # register 0x0100 structure is not a command reply.
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            loop = _aio.get_running_loop()
            for badhex in ("", "42", "0142A0FF", "0042A000", "0042A0", "0042A0FF"):
                dev2 = AsyncVEDirect(
                    lambda n: _aio.sleep(10), lambda d: None, flavor="MPPT")
                f = loop.create_future()
                raw = bytes.fromhex(badhex)
                f.set_result(_H.decode_line(
                    _H.encode_frame(_H.CMD_DONE, raw)))
                dev2._command_transact = lambda cmd, expected, t, _f=f: _f
                with self.assertRaises(RegisterError, msg=badhex):
                    await dev2.get_product_id(timeout=0.1)
        run(go())

    def test_error_framing_vs_boot(self):
        # Includes test_hex_error_handling: a flagged Get fails its waiter,
        # and Orion :2AAAA raises ChecksumError without waiting for timeout.
        # Also retains test_sim_round_trips' :4AAAA error routing assertion.
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import ChecksumError, RegisterError

        async def go():
            loop = _aio.get_running_loop()
            dev, _ = self._dev()
            fut_flags = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut_flags)
            flagged_get = _H.encode_frame(
                _H.CMD_GET, bytes.fromhex("BBED010000"))
            dev._handle_hex(_H.decode_line(flagged_get))
            self.assertTrue(fut_flags.done())
            with self.assertRaises(RegisterError):
                fut_flags.result()
            # :4AAAA framing error -> ChecksumError
            fut = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut)
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_ERROR, bytes.fromhex("AAAA"))))
            with self.assertRaises(ChecksumError):
                fut.result()
            # Payload zero belongs to boot entry, not a register request.
            fut2 = loop.create_future()
            dev._register_waiter = None
            dev._boot_waiter = fut2
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_ERROR, b"\x00")))
            with self.assertRaises(RegisterError):
                fut2.result()
            dev._boot_waiter = None
            # :2AAAA (Orion checksum error) -> ChecksumError
            fut3 = loop.create_future()
            dev._register_waiter = (0xEDBB, _H.CMD_GET, fut3)
            dev._handle_hex(_H.decode_line(_H.encode_frame(
                _H.CMD_CHECKSUM_ERROR, bytes.fromhex("AAAA"))))
            with self.assertRaises(ChecksumError):
                fut3.result()
        run(go())

    def test_sim_round_trips(self):
        # Includes test_sim_framing_unknown_boot_width's default MPPT ping:
        # its version 1.16 is an application version.
        # Includes test_orion_app_version_info: Orion reads 0x0102 and
        # decodes its 4-byte value as release version 1.02.
        # Includes test_non_orion_app_version_unknown_propagates: MPPT and
        # unspecified flavor propagate Unknown without reading 0x0102.
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
            self.assertEqual(_H.decode_ping_version(f.data)["type"], "application")
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
            self.assertEqual(_H.encode_frame(_H.CMD_DONE, raw), b":11641FD\n")
            # The removed simulator RC ping still checks the wire path;
            # test_hex covers the same 0xD101 decoder result separately.
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
            self.assertEqual(info6["type"], "release")
            self.assertEqual(sim6.written[-1], _H.build_get(0x0102))
            # A non-Orion client must propagate Unknown instead of reading 0x0102.
            for flavor in ("MPPT", None):
                sim6b = SimulatedTransport(
                    text_chunks=[], flavor="ORION",
                    hex_replies={0x0102: bytes.fromhex("00FF0201")})
                dev6b = AsyncVEDirect(sim6b.read_chunk, sim6b.write,
                                       flavor=flavor)
                with self.assertRaises(RegisterError, msg=str(flavor)):
                    await dev6b.get_app_version_info(timeout=2)
                self.assertEqual(sim6b.written, [_H.build_app_version()])
            # async_notifications() pump with injected :A frame.
            sim7 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            dev7 = AsyncVEDirect(sim7.read_chunk, sim7.write, flavor="MPPT")
            sim7.inject_async(0x0201, b"\x05")
            gen = dev7.async_notifications(flavor="MPPT")
            note = await asyncio.wait_for(gen.__anext__(), 2)
            self.assertEqual(note["register_id"], 0x0201)
            await gen.aclose()
        run(go())

    def test_sim_framing_unknown_width(self):
        # Includes test_sim_framing_unknown_boot_width's bad checksums,
        # Unknown reply, and zero-payload register widths. Its boot and ping
        # cases live in test_sim_boot_failure_reply and test_sim_round_trips.
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
            # Width-correct zero payloads from register DB.
            sim3 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim3.write(_H.build_get(0xEDBC))  # PANEL_POWER uint32
            f = _H.decode_line(bytes(sim3._pending_out))
            self.assertEqual(len(f.value_bytes), 4)
            sim4 = SimulatedTransport(text_chunks=[], flavor="MPPT")
            await sim4.write(_H.build_get(0xEDBB))  # PANEL_VOLTAGE uint16
            f4 = _H.decode_line(bytes(sim4._pending_out))
            self.assertEqual(len(f4.value_bytes), 2)
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
        """Command :4 uses two bytes independently of the product family."""
        import asyncio as _aio
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            dev = AsyncVEDirect(lambda n: _aio.sleep(10), lambda d: None)
            loop = _aio.get_running_loop()

            # No family selection is needed to decode a command reply.
            fut = loop.create_future()
            fut.set_result(_H.decode_line(_H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
            dev._command_transact = lambda cmd, expected, t: fut
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)

            # BMV/Phoenix officially use a two-byte command-4 payload.
            dev.flavor = "BMV"
            self.assertEqual(await dev.get_product_id(timeout=0.1), 0xA042)

            # The four-byte form belongs to Get register 0x0100.
            dev.flavor = "MPPT"
            fut2 = loop.create_future()
            fut2.set_result(_H.decode_line(_H.encode_frame(_H.CMD_DONE, bytes.fromhex("0042A0FF"))))
            dev._command_transact = lambda cmd, expected, t: fut2
            with self.assertRaises(RegisterError):
                await dev.get_product_id(timeout=0.1)
        run(go())

    def test_simulated_product_id_shapes(self):
        async def go():
            for flavor in ("MPPT", "MPPT_SMALL", "MPPT_RS", "ORION", "BMV", "INV"):
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

        async def go():
            dev, _ = self._dev()
            frame = _H.decode_line(_H.encode_frame(
                _H.CMD_ASYNC,
                bytes.fromhex("BBED00") + struct.pack("<H", 3130),
            ))
            decoded = dev.decode_async_frame(frame, flavor="MPPT")
            self.assertEqual(decoded["name"], "PANEL_VOLTAGE")
            self.assertAlmostEqual(decoded["value"], 31.3)
        run(go())

    def test_boot_command(self):
        """Boot sends the documented :0 frame."""
        # Includes test_boot_and_serial_power: boot writes the magic frame,
        # and from_port accepts dtr and rts power-control arguments.
        async def go():
            sent = []
            dev = AsyncVEDirect(lambda n: asyncio.sleep(10), lambda d: sent.append(d))
            await dev.boot(timeout=0.01)
            self.assertEqual(sent, [bytes.fromhex("3A30353146413531464135314641353146413531464144450A")])
        run(go())
        import inspect
        signature = inspect.signature(AsyncVEDirect.from_port)
        self.assertIn("dtr", signature.parameters)
        self.assertIn("rts", signature.parameters)

    def test_sim_boot_failure_reply(self):
        """boot_fails=True emits documented :40051 (Error payload 0x00)."""
        # Includes test_sim_framing_unknown_boot_width's silent success case.
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
            frame = _H.decode_line(bytes(sim2._pending_out))
            self.assertEqual(frame.cmd, _H.CMD_ERROR)
            self.assertEqual(bytes(frame.data), b"\x00")
        run(go())

    def test_boot_failure_reaches_boot_call_and_late_failure_is_logged(self):
        from ve_direct.exceptions import RegisterError

        async def go():
            sim = SimulatedTransport(boot_fails=True)
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
            with self.assertRaisesRegex(RegisterError, "could not enter bootloader"):
                await dev.boot(timeout=0.1)
            self.assertEqual((await dev.ping(timeout=0.1)).data, b"\x16\x41")

            # Silence ends the observation window. A later boot error has a
            # specific meaning and must not be assigned to the next Ping.
            sim2 = SimulatedTransport()
            dev2 = AsyncVEDirect(sim2.read_chunk, sim2.write, flavor="MPPT")
            await dev2.boot(timeout=0.01)
            sim2.inject_raw(b":40051\n")
            with self.assertLogs("ve_direct.device", level="WARNING") as logged:
                self.assertEqual((await dev2.ping(timeout=0.1)).data, b"\x16\x41")
            self.assertIn("late or unsolicited boot failure", logged.output[0])
        run(go())

    def test_boot_cancellation_and_write_failure_leave_hex_closed(self):
        from ve_direct.exceptions import TransportError

        async def go():
            for phase in ("write", "read", "write_error"):
                with self.subTest(phase=phase):
                    started = asyncio.Event()

                    async def read(n):
                        started.set()
                        await asyncio.Event().wait()

                    async def write(data):
                        if phase == "write_error":
                            raise OSError("partial serial write")
                        if phase == "write":
                            started.set()
                            await asyncio.Event().wait()

                    dev = AsyncVEDirect(read, write, flavor="MPPT")
                    task = asyncio.create_task(dev.boot(timeout=1))
                    if phase == "write_error":
                        with self.assertRaises(OSError):
                            await task
                    else:
                        await started.wait()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    with self.assertRaises(TransportError):
                        await dev.ping(timeout=0.1)
        run(go())

    def test_restart_disables_hex_until_reset_and_preserves_text(self):
        from ve_direct.exceptions import TransportError

        async def go():
            sim = SimulatedTransport(text_chunks=[make_text_block({"V": "12000"})])
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
            await dev.restart()
            with self.assertRaises(TransportError):
                await dev.ping(timeout=0.1)
            self.assertEqual((await dev.read_one(timeout=0.1))["V"].value, 12000)
            # This fixture has no pre-restart reply or remaining input.
            self.assertFalse(sim._stream or sim._pending_out)
            dev.reset_hex_state()
            self.assertEqual((await dev.ping(timeout=0.1)).data, b"\x16\x41")
        run(go())

    def test_reset_discards_partial_reply_but_keeps_queued_frames(self):
        from ve_direct.exceptions import TimeoutError

        async def go():
            incoming = asyncio.Queue()

            async def read(n):
                return await incoming.get()

            dev = AsyncVEDirect(read, lambda data: None, flavor="MPPT")
            # The timed-out transaction has already consumed complete Text
            # and Async data, followed by a partial Get response.
            incoming.put_nowait(make_text_block({"V": "12000"})
                                + b":A0102000543\n:7F0ED0096")
            with self.assertRaises(TimeoutError):
                await dev.hex_get_raw(0xEDF0, timeout=0.01)
            await dev.restart()
            self.assertTrue(incoming.empty())
            dev.reset_hex_state()
            # A suffix after reset cannot complete the discarded old prefix.
            incoming.put_nowait(b"00DB\n:7F0ED0064000D\n")
            reply = await dev.hex_get_raw(0xEDF0, timeout=0.1)
            self.assertEqual(reply.value_bytes, b"\x64\x00")
            self.assertEqual((await dev.read_one(timeout=0.1))["V"].value, 12000)
            notification = dev.async_frames.get_nowait()
            self.assertEqual(notification.value_bytes, b"\x05")
        run(go())

    def test_sim_parameter_error_round_trip(self):
        """Simulated Parameter Error reaches Get and Set callers."""
        # The wire reply still carries flag 0x04, and global errors cover
        # Get as well as Set; these checks were removed during consolidation.
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            # Per-register trigger parallels boot_fails opt-in.
            sim = SimulatedTransport(
                text_chunks=[], flavor="MPPT",
                hex_replies={0xEDBB: struct.pack("<H", 3130)},
                parameter_error_registers={0xEDBB})
            dev = AsyncVEDirect(sim.read_chunk, sim.write, flavor="MPPT")
            with self.assertRaises(RegisterError) as cm:
                await dev.hex_get("PANEL_VOLTAGE", timeout=2)
            self.assertIn("parameter error", str(cm.exception))
            raw_sim = SimulatedTransport(
                text_chunks=[], flavor="MPPT",
                parameter_error_registers={0xEDBB})
            await raw_sim.write(_H.build_get(0xEDBB))
            raw_reply = _H.decode_line(bytes(raw_sim._pending_out))
            self.assertEqual(raw_reply.flags, _H.HEX_FLAG_PARAMETER_ERROR)
            # Global flag trigger, including Set path.
            sim3 = SimulatedTransport(
                text_chunks=[], flavor="MPPT", parameter_error=True)
            dev3 = AsyncVEDirect(sim3.read_chunk, sim3.write, flavor="MPPT")
            with self.assertRaises(RegisterError) as cm3:
                await dev3.hex_get("PANEL_VOLTAGE", timeout=2)
            self.assertIn("parameter error", str(cm3.exception))
            with self.assertRaises(RegisterError) as cm4:
                await dev3.hex_set("BAT_MAX_CURRENT", 35.0, timeout=2)
            self.assertIn("parameter error", str(cm4.exception))
        run(go())

    def test_truncated_async_rejected(self):
        """Truncated :A (id only, no flags/value) raises RegisterError."""
        from ve_direct import hex as _H
        from ve_direct.exceptions import RegisterError

        async def go():
            dev, _ = self._dev()
            for payload in (bytes.fromhex("0102"), bytes.fromhex("01")):
                frame = _H.decode_line(_H.encode_frame(_H.CMD_ASYNC, payload))
                with self.assertRaises(RegisterError, msg=payload.hex()):
                    dev.decode_async_frame(frame, flavor="MPPT")
        run(go())

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

    def test_product_id_rs_accepts_two_byte_command_reply(self):
        """BlueSolar/Orion's four-byte rule is specific to Get 0x0100."""
        import asyncio as _aio
        from ve_direct import hex as _H

        async def go():
            loop = _aio.get_running_loop()
            dev = AsyncVEDirect(
                lambda n: _aio.sleep(10), lambda d: None, flavor="MPPT_RS")
            fut = loop.create_future()
            fut.set_result(_H.decode_line(
                _H.encode_frame(_H.CMD_DONE, bytes.fromhex("42A0"))))
            dev._command_transact = lambda cmd, expected, t: fut
            for flavor in ("MPPT_RS", "MPPT_SMALL", "MULTI_RS", "ORION", "MPPT", None):
                dev.flavor = flavor
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
                return make_text_block({"PID": "0xA042", "V": str(v)})

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

            await asyncio.wait_for(reader_task, 1)
            feeder_task.cancel()
            transport.close()
            await dev.close()
            self.assertGreaterEqual(len(frames_received), 15)

        run(go())


if __name__ == "__main__":
    unittest.main()
