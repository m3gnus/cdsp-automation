from __future__ import annotations

import asyncio
import contextlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
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

import speaker_profiles
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

    def test_device_reconnect_closes_old_handle_and_updates_cleanup_target(self) -> None:
        class Device:
            def __init__(self, error: Exception) -> None:
                self.error = error
                self.ungrabbed = False
                self.closed = False

            async def async_read_loop(self):
                if False:
                    yield None
                raise self.error

            def ungrab(self) -> None:
                self.ungrabbed = True

            def close(self) -> None:
                self.closed = True

        old = Device(OSError("disconnected"))
        replacement = Device(RuntimeError("stop test loop"))
        cdsp_remote.remote_device = old

        with (
            mock.patch.object(cdsp_remote.asyncio, "sleep", new=mock.AsyncMock()),
            mock.patch.object(
                cdsp_remote, "find_remote_device", return_value=replacement
            ),
            mock.patch.object(cdsp_remote, "grab_device") as grab,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "stop test loop"),
        ):
            asyncio.run(cdsp_remote.handle_remote_events(old))

        self.assertTrue(old.ungrabbed)
        self.assertTrue(old.closed)
        self.assertIs(cdsp_remote.remote_device, replacement)
        grab.assert_called_once_with(replacement)


class RemoteVolumeCeilingTests(unittest.TestCase):
    """The remote's ceiling is the applied profile's, never a fixed 0 dB."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.status = Path(self.directory.name, "speaker-profile-status.json")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(setattr, cdsp_remote, "cdsp", None)

    def write_status(self, payload: dict) -> None:
        self.status.write_text(json.dumps(payload), encoding="utf-8")

    def applied(self, limit: float) -> None:
        self.write_status(
            {"ok": True, "applied": "partymeh", "volume_limit_db": limit}
        )

    def raise_volume(self, start: float, *, override: float | None = None) -> float:
        volume = SimpleNamespace(
            main_volume=lambda: start,
            set_main_volume=lambda value: applied.append(value),
        )
        applied: list[float] = []
        client = SimpleNamespace(volume=volume)
        with (
            mock.patch.object(cdsp_remote, "SPEAKER_STATUS_PATH", self.status),
            mock.patch.object(cdsp_remote, "VOLUME_MAX_OVERRIDE", override),
            mock.patch.object(
                cdsp_remote, "ensure_cdsp_connected", return_value=client
            ),
            mock.patch.object(
                cdsp_remote, "audio_control_lock", return_value=nullcontext()
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            # Far more than any ceiling: the answer is always the ceiling.
            cdsp_remote.adjust_volume(500.0)
        self.assertEqual(len(applied), 1)
        return applied[0]

    def test_remote_volume_ceiling_follows_the_applied_profile(self) -> None:
        self.applied(-20.0)
        with mock.patch.object(cdsp_remote, "SPEAKER_STATUS_PATH", self.status):
            self.assertEqual(cdsp_remote.current_volume_max(), -20.0)
        self.assertEqual(self.raise_volume(-25.0), -20.0)
        # Already at the cap: a further volume-up must not move it.
        self.assertEqual(self.raise_volume(-20.0), -20.0)

    def test_remote_volume_ceiling_fails_closed_without_a_verified_apply(self) -> None:
        failsafe = speaker_profiles.FAILSAFE_VOLUME_LIMIT_DB
        unverified = (
            None,  # no status file at all
            {"ok": False, "volume_limit_db": 0.0},
            {"ok": True},  # applied, but published no ceiling
            {"ok": True, "volume_limit_db": "loud"},
            {"ok": True, "volume_limit_db": True},
            {"ok": True, "volume_limit_db": 900.0},
        )
        for payload in unverified:
            with self.subTest(payload=payload):
                if payload is None:
                    self.status.unlink(missing_ok=True)
                else:
                    self.write_status(payload)
                self.assertEqual(self.raise_volume(-40.0), failsafe)
                self.assertLess(failsafe, 0.0)

    def test_remote_env_override_can_only_tighten_the_profile_ceiling(self) -> None:
        self.applied(-20.0)
        # A stricter deployment preference wins.
        self.assertEqual(self.raise_volume(-40.0, override=-30.0), -30.0)
        # A looser one -- including the old 0 dB default -- does not.
        self.assertEqual(self.raise_volume(-40.0, override=0.0), -20.0)
        self.assertEqual(self.raise_volume(-40.0, override=10.0), -20.0)

    def test_remote_floor_follows_a_ceiling_stricter_than_the_floor(self) -> None:
        """A -90 dB cap must not be clamped back up to REMOTE_VOLUME_MIN."""
        self.applied(-90.0)
        self.assertEqual(self.raise_volume(-100.0), -90.0)


if __name__ == "__main__":
    unittest.main()
