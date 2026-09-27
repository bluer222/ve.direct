"""Host serial settings and a real OS serial path, without Victron hardware."""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ve_direct import AsyncVEDirect


class TransportAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_serial_defaults(self):
        """Text spec serial settings applied through pyserial to a pseudo-terminal."""
        master, slave = os.openpty()
        dev = None
        try:
            dev = await AsyncVEDirect.from_port(os.ttyname(slave))
            serial = dev._writer.transport.serial
            self.assertEqual(serial.baudrate, 19200)
            self.assertEqual(serial.bytesize, 8)
            self.assertEqual(serial.parity, 'N')
            self.assertEqual(serial.stopbits, 1)
            self.assertFalse(serial.xonxoff)
            self.assertFalse(serial.rtscts)
            self.assertFalse(serial.dsrdtr)
        finally:
            if dev is not None:
                await dev.close()
                await dev._writer.wait_closed()
            os.close(master)
            os.close(slave)

    async def test_control_lines(self):
        """Host drives requested DTR/RTS settings; no electrical-level claim."""
        import serial_asyncio
        for options, expected in (({}, (True, False)),
                                  ({'dtr': False, 'rts': True}, (False, True))):
            serial = SimpleNamespace(dtr=None, rts=None)
            writer = SimpleNamespace(transport=SimpleNamespace(serial=serial), close=Mock())
            reader = SimpleNamespace(read=AsyncMock())
            with patch.object(serial_asyncio, 'open_serial_connection',
                              AsyncMock(return_value=(reader, writer))) as opening:
                dev = await AsyncVEDirect.from_port('/fake-port', **options)
                opening.assert_awaited_once_with(url='/fake-port', baudrate=19200)
                self.assertEqual((serial.dtr, serial.rts), expected)
                await dev.close()

    async def test_serial_roundtrip(self):
        """OS serial path carries a literal HEX example and independent Text bytes."""
        master, slave = os.openpty()
        dev = None
        try:
            dev = await AsyncVEDirect.from_port(os.ttyname(slave), flavor='MPPT')
            request = asyncio.create_task(dev.hex_get('BAT_MAX_CURRENT', timeout=1))
            sent = await asyncio.wait_for(asyncio.to_thread(os.read, master, 1024), 1)
            self.assertEqual(sent, b':7F0ED0071\n')
            os.write(master, b':7F0ED009600DB\n')
            self.assertEqual(await request, 15.0)
            block = b'\r\nV\t12000\r\nChecksum\t'
            os.write(master, block + bytes([(-sum(block)) % 256]))
            self.assertEqual((await dev.read_one(timeout=1)).to_si()['V'], 12.0)
        finally:
            if dev is not None:
                await dev.close()
                await dev._writer.wait_closed()
            os.close(master)
            os.close(slave)
