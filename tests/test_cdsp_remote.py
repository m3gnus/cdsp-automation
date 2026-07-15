from __future__ import annotations

import contextlib
import io
import stat
import subprocess
import sys
import types
import unittest
from unittest import mock


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

if "evdev" not in sys.modules:
    evdev = types.ModuleType("evdev")
    evdev.ecodes = types.SimpleNamespace(EV_KEY=1)
    evdev.categorize = lambda event: event
    evdev.list_devices = lambda: []
    evdev.InputDevice = object
    sys.modules["evdev"] = evdev

from scripts import cdsp_remote


class RemoteTests(unittest.TestCase):
    def tearDown(self) -> None:
        cdsp_remote.cdsp = None
        cdsp_remote.remote_device = None

    def test_connection_failure_does_not_enter_an_internal_retry_loop(self) -> None:
        client = mock.Mock()
        client.connect.side_effect = ConnectionError("not ready")
        with mock.patch.object(cdsp_remote, "CamillaClient", return_value=client):
            with self.assertRaises(ConnectionError):
                cdsp_remote.connect_to_camilladsp()
        self.assertEqual(client.connect.call_count, 1)
        self.assertIsNone(cdsp_remote.cdsp)

    def test_volume_action_returns_after_one_failed_connection_attempt(self) -> None:
        client = mock.Mock()
        client.connect.side_effect = ConnectionError("not ready")
        with (
            mock.patch.object(cdsp_remote, "CamillaClient", return_value=client),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            cdsp_remote.adjust_volume(1.0)

        self.assertEqual(client.connect.call_count, 1)
        self.assertIn("Error adjusting volume", output.getvalue())

    def test_missing_remote_status_is_throttled_while_polling_continues(self) -> None:
        sleeps = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise RuntimeError("stop test loop")

        output = io.StringIO()
        with (
            mock.patch.object(cdsp_remote.evdev, "list_devices", return_value=[]),
            mock.patch.object(cdsp_remote.time, "monotonic", side_effect=[0.0, 1.0, 2.0]),
            mock.patch.object(cdsp_remote.time, "sleep", side_effect=fake_sleep),
            mock.patch.object(cdsp_remote, "STATUS_LOG_SECONDS", 30.0),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(RuntimeError, "stop test loop"),
        ):
            cdsp_remote.find_remote_device()

        self.assertEqual(sleeps, 3)
        self.assertEqual(output.getvalue().count("not found"), 1)

    def test_self_restart_is_enqueued_without_blocking_its_own_service(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(cdsp_remote, "run_sudo", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cdsp_remote.restart_services()

        self.assertEqual(
            calls[-1],
            [
                cdsp_remote.SYSTEMCTL_BIN,
                "--no-block",
                "restart",
                "cdsp-remote.service",
            ],
        )
        self.assertFalse(any("cdsp-trigger.service" in command for command in calls))

    def test_shutdown_uses_exact_systemctl_poweroff(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(cdsp_remote, "run_sudo", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cdsp_remote.shutdown_system()

        self.assertEqual(calls, [[cdsp_remote.SYSTEMCTL_BIN, "poweroff"]])

    def test_writable_privileged_binary_is_rejected(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFREG | stat.S_IXUSR | stat.S_IWGRP,
            st_uid=0,
        )
        with (
            mock.patch.object(cdsp_remote.os.path, "realpath", return_value="/usr/bin/systemctl"),
            mock.patch.object(cdsp_remote.os, "stat", return_value=metadata),
            self.assertRaisesRegex(RuntimeError, "not trusted"),
        ):
            cdsp_remote.validate_trusted_executable("/usr/bin/systemctl")

    def test_privileged_command_paths_are_fixed_not_path_derived(self) -> None:
        self.assertEqual(cdsp_remote.SUDO_BIN, "/usr/bin/sudo")
        self.assertEqual(cdsp_remote.SYSTEMCTL_BIN, "/usr/bin/systemctl")


if __name__ == "__main__":
    unittest.main()
