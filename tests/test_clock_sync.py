from __future__ import annotations

import sys
import tempfile
import types
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

try:
    import websocket  # noqa: F401
except ImportError:
    websocket = types.ModuleType("websocket")
    websocket.ABNF = types.SimpleNamespace(OPCODE_BINARY=2)
    websocket.WebSocket = object
    sys.modules["websocket"] = websocket

from scripts import clock_sync


REPOSITORY = Path(__file__).resolve().parents[1]


class ClockSyncTests(unittest.TestCase):
    def test_current_sample_rate_validates_shape_and_value(self) -> None:
        self.assertEqual(
            clock_sync.current_sample_rate({"devices": {"samplerate": "48000"}}),
            48000,
        )
        self.assertIsNone(clock_sync.current_sample_rate({"devices": {"samplerate": 0}}))
        self.assertIsNone(clock_sync.current_sample_rate(None))

    def test_motu_failure_is_reported_to_caller_for_retry(self) -> None:
        failing_socket = mock.Mock()
        failing_socket.connect.side_effect = OSError("offline")
        with mock.patch.object(clock_sync.websocket, "WebSocket", return_value=failing_socket):
            self.assertFalse(clock_sync.set_motu_clock("optical"))
        failing_socket.close.assert_called_once()

    def test_unknown_clock_source_is_rejected(self) -> None:
        self.assertFalse(clock_sync.set_motu_clock("word-clock"))

    def test_source_identity_supports_legacy_generated_and_operator_configs(self) -> None:
        self.assertEqual(clock_sync.source_for_config_path("/tmp/streamer.yml"), "streamer")
        self.assertEqual(
            clock_sync.source_for_config_path("/tmp/toslink--kantarellen.yml"),
            "toslink",
        )
        self.assertEqual(
            clock_sync.source_for_config_path("/tmp/partymeh-streamer.yml"),
            "streamer",
        )
        self.assertEqual(
            clock_sync.source_for_config_path("/tmp/partymeh-toslink.yml"),
            "toslink",
        )
        self.assertIsNone(clock_sync.source_for_config_path("/tmp/partymeh.yml"))

    def test_persisted_clock_round_trips_and_rejects_junk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state" / "motu-clock-source"
            with mock.patch.object(clock_sync, "STATE_PATH", state):
                self.assertIsNone(clock_sync.read_persisted_clock())
                with contextlib.redirect_stdout(io.StringIO()):
                    clock_sync.persist_clock("internal")
                self.assertEqual(clock_sync.read_persisted_clock(), "internal")
                state.write_text("word-clock\n")
                self.assertIsNone(clock_sync.read_persisted_clock())

    def test_persist_failure_is_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("")
            state = blocker / "motu-clock-source"
            with (
                mock.patch.object(clock_sync, "STATE_PATH", state),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                clock_sync.persist_clock("internal")
            self.assertIn("cannot persist clock state", out.getvalue())

    def test_main_skips_redundant_clock_write_after_restart(self) -> None:
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 192000}}
        client.config.file_path.return_value = "/tmp/partymeh-streamer.yml"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "motu-clock-source"
            state.write_text("internal\n")
            with (
                mock.patch.object(clock_sync, "STATE_PATH", state),
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=KeyboardInterrupt
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        set_clock.assert_not_called()

    def test_main_retries_a_failed_clock_command_without_a_rate_change(self) -> None:
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 48000}}
        client.config.file_path.return_value = "/tmp/toslink.yml"
        sleeps = 0

        def stop_after_second_iteration(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise KeyboardInterrupt

        with (
            mock.patch.object(clock_sync, "CamillaClient", return_value=client),
            mock.patch.object(
                clock_sync,
                "set_motu_clock",
                side_effect=[False, True],
            ) as set_clock,
            mock.patch.object(clock_sync.time, "sleep", side_effect=stop_after_second_iteration),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            clock_sync.main()

        self.assertEqual(set_clock.call_args_list, [mock.call("optical"), mock.call("optical")])


def test_clock_send_failure_is_not_latched_and_equal_rates_use_source_identity() -> None:
    class FailingSocket:
        def connect(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("MOTU offline")

        def close(self) -> None:
            pass

    with (
        mock.patch.object(clock_sync.websocket, "WebSocket", FailingSocket),
        mock.patch.object(clock_sync, "_next_motu_error_log", 0.0),
        mock.patch.object(clock_sync.time, "monotonic", side_effect=[0.0, 1.0]),
        mock.patch("builtins.print") as log,
    ):
        assert clock_sync.set_motu_clock("optical") is False
        assert clock_sync.set_motu_clock("optical") is False
    assert log.call_count == 1
    assert clock_sync.source_for_config_path("/generated/streamer--kantarellen.yml") == "streamer"
    assert clock_sync.source_for_config_path("/generated/toslink--kantarellen.yml") == "toslink"
    assert clock_sync.source_for_config_path("/configs/partymeh-streamer.yml") == "streamer"
    assert clock_sync.source_for_config_path("/configs/partymeh-toslink.yml") == "toslink"
    source = (REPOSITORY / "scripts" / "clock_sync.py").read_text()
    success_branch = source.split("if set_motu_clock(desired_clock):", 1)[1]
    assert success_branch.index("last_clock = desired_clock") < success_branch.index(
        "except Exception"
    )


if __name__ == "__main__":
    unittest.main()
