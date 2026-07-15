from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import types
import unittest
from unittest import mock


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

if "RPi.GPIO" not in sys.modules:
    rpi = types.ModuleType("RPi")
    gpio = types.ModuleType("RPi.GPIO")
    rpi.GPIO = gpio
    sys.modules["RPi"] = rpi
    sys.modules["RPi.GPIO"] = gpio

from scripts import trigger


class TriggerRecoveryTests(unittest.TestCase):
    def test_stale_client_is_replaced_without_dropping_relay(self) -> None:
        first_client = mock.Mock()
        first_client.is_connected.side_effect = [False, True]
        first_client.levels.capture_rms.side_effect = [
            [-10.0, -20.0],
            ConnectionError("CamillaDSP restarted"),
        ]
        replacement_client = mock.Mock()
        clients = mock.Mock(side_effect=[first_client, replacement_client])

        gpio = mock.Mock()
        gpio.BCM = 11
        gpio.OUT = 1
        gpio.HIGH = 1
        gpio.LOW = 0
        stop = asyncio.Event()

        async def stop_after_recovery(_seconds: float) -> None:
            stop.set()

        async def exercise() -> None:
            with (
                mock.patch.object(trigger, "CamillaClient", clients),
                mock.patch.object(trigger, "GPIO", gpio),
                mock.patch.object(trigger, "CHECK_INTERVAL", 0.001),
                mock.patch.object(trigger.asyncio, "sleep", new=stop_after_recovery),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                await trigger.relay_control(stop, asyncio.Event())

        asyncio.run(exercise())

        self.assertEqual(clients.call_count, 2)
        first_client.disconnect.assert_called_once()
        self.assertEqual(
            gpio.output.call_args_list,
            [mock.call(trigger.POWER_GPIO, gpio.HIGH), mock.call(trigger.POWER_GPIO, gpio.LOW)],
        )


if __name__ == "__main__":
    unittest.main()
