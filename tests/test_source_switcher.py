from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

from scripts import source_switcher


class WledDelaySettingsTests(unittest.TestCase):
    def test_missing_wled_env_keeps_delay_disabled(self) -> None:
        with mock.patch.object(source_switcher, "WLED_ENV_PATH", "/does/not/exist"):
            self.assertEqual(
                source_switcher.read_wled_delay_settings(),
                (False, 0.0, "wled_light_sync_delay"),
            )

    def test_valid_wled_env_enables_requested_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory, "wled.env")
            env_path.write_text(
                "CAMILLA_DELAY_ENABLED=true\n"
                "CAMILLA_DELAY_MS=17.5\n"
                "CAMILLA_DELAY_FILTER_NAME=custom_delay\n",
                encoding="utf-8",
            )
            with mock.patch.object(source_switcher, "WLED_ENV_PATH", str(env_path)):
                self.assertEqual(
                    source_switcher.read_wled_delay_settings(),
                    (True, 17.5, "custom_delay"),
                )

    def test_invalid_delay_is_safely_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory, "wled.env")
            env_path.write_text(
                "CAMILLA_DELAY_ENABLED=true\nCAMILLA_DELAY_MS=not-a-number\n",
                encoding="utf-8",
            )
            with mock.patch.object(source_switcher, "WLED_ENV_PATH", str(env_path)):
                enabled, delay_ms, _name = source_switcher.read_wled_delay_settings()
            self.assertFalse(enabled)
            self.assertEqual(delay_ms, 0.0)


class DelayFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "devices": {"capture": {"channels": 2}},
            "filters": {
                "sync": {
                    "type": "Delay",
                    "parameters": {"delay": 12.0, "unit": "ms", "subsample": False},
                }
            },
            "pipeline": [
                {"type": "Filter", "channels": [0, 1], "names": ["sync"]}
            ],
        }

    def test_exactly_one_matching_filter_is_current(self) -> None:
        self.assertTrue(source_switcher._has_requested_delay(self.config, "sync", 12.0))

    def test_duplicate_pipeline_filter_is_not_current_and_is_repaired(self) -> None:
        self.config["pipeline"].append(
            {"type": "Filter", "channels": [0, 1], "names": ["sync"]}
        )
        self.assertFalse(source_switcher._has_requested_delay(self.config, "sync", 12.0))

        repaired = source_switcher._add_delay(self.config, "sync", 12.0)
        matching_steps = [
            step
            for step in repaired["pipeline"]
            if "sync" in step.get("names", [])
        ]
        self.assertEqual(len(matching_steps), 1)
        self.assertTrue(source_switcher._has_requested_delay(repaired, "sync", 12.0))


class ConfigRecoveryTests(unittest.TestCase):
    def client(self, state_name: str, active_config: object, path: str = "") -> mock.Mock:
        client = mock.Mock()
        client.general.state.return_value = types.SimpleNamespace(name=state_name)
        client.config.active.return_value = active_config
        client.config.file_path.return_value = path
        return client

    def test_inactive_state_reloads_existing_remembered_config_on_retry_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "streamer.yml")
            config_path.touch()
            client = self.client("INACTIVE", None, str(config_path))
            recovery = source_switcher.ConfigRecoveryGuard(
                retry_seconds=10,
                log_seconds=30,
            )

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(recovery.ready(client, 0.0))
                self.assertFalse(recovery.ready(client, 5.0))
                self.assertFalse(recovery.ready(client, 10.0))

            self.assertEqual(client.general.reload.call_count, 2)
            client.config.active.assert_not_called()
            self.assertEqual(output.getvalue().count("CamillaDSP recovery:"), 1)

    def test_missing_active_config_recovers_even_when_state_says_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "streamer.yml")
            config_path.touch()
            client = self.client("PAUSED", {}, str(config_path))
            recovery = source_switcher.ConfigRecoveryGuard()

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(recovery.ready(client, 0.0))

            client.general.reload.assert_called_once_with()

    def test_healthy_paused_and_running_configs_are_never_reloaded(self) -> None:
        recovery = source_switcher.ConfigRecoveryGuard()
        for state_name in ("PAUSED", "RUNNING"):
            client = self.client(state_name, {"devices": {"samplerate": 48000}})
            self.assertTrue(recovery.ready(client, 0.0))
            client.general.reload.assert_not_called()
            client.config.file_path.assert_not_called()

    def test_invalid_remembered_path_is_not_reloaded(self) -> None:
        client = self.client("INACTIVE", None, "/does/not/exist.yml")
        recovery = source_switcher.ConfigRecoveryGuard()

        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(recovery.ready(client, 0.0))

        client.general.reload.assert_not_called()
        self.assertIn("remembered config is not a file", output.getvalue())


if __name__ == "__main__":
    unittest.main()
