from __future__ import annotations

import json
import os
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

import speaker_profiles
from scripts import clock_sync


REPOSITORY = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def config_dir(path: Path):
    """Point the identity lookup at a test config directory."""
    with mock.patch.dict(os.environ, {"CDSP_CONFIG_DIR": str(path)}):
        yield path


def datastore_response(payload: object):
    """A urlopen stand-in serving one MOTU datastore document."""
    body = json.dumps(payload).encode("utf-8")

    @contextlib.contextmanager
    def urlopen(_request: object, timeout: float | None = None):
        yield types.SimpleNamespace(read=lambda _limit=None: body)

    return urlopen


class ClockSyncTests(unittest.TestCase):
    def test_current_sample_rate_validates_shape_and_value(self) -> None:
        self.assertEqual(
            clock_sync.current_sample_rate({"devices": {"samplerate": "48000"}}),
            48000,
        )
        self.assertIsNone(clock_sync.current_sample_rate({"devices": {"samplerate": 0}}))
        self.assertIsNone(clock_sync.current_sample_rate({"devices": None}))
        self.assertIsNone(clock_sync.current_sample_rate(None))

    def test_motu_failure_is_reported_to_caller_for_retry(self) -> None:
        failing_socket = mock.Mock()
        failing_socket.connect.side_effect = OSError("offline")
        with mock.patch.object(clock_sync.websocket, "WebSocket", return_value=failing_socket):
            self.assertFalse(clock_sync.set_motu_clock("optical"))
        failing_socket.close.assert_called_once()

    def test_unknown_clock_source_is_rejected(self) -> None:
        self.assertFalse(clock_sync.set_motu_clock("word-clock"))

    def test_catalog_filename_mapping_decides_the_source_identity(self) -> None:
        """An operator config named by the catalog selects the clock it maps."""
        with tempfile.TemporaryDirectory() as tmp, config_dir(Path(tmp)) as configs:
            with mock.patch.dict(
                speaker_profiles.OPERATOR_CONFIG_SPEAKERS,
                {
                    "partymeh": {
                        "toslink": "optical-input.yml",
                        "streamer": "house-stream.yaml",
                    }
                },
                clear=True,
            ):
                self.assertEqual(
                    clock_sync.source_for_config_path(str(configs / "optical-input.yml")),
                    "toslink",
                )
                self.assertEqual(
                    clock_sync.source_for_config_path(str(configs / "house-stream.yaml")),
                    "streamer",
                )
                # The conventional operator name is only a name: with the
                # catalog pointing elsewhere it identifies nothing.
                self.assertIsNone(
                    clock_sync.source_for_config_path(str(configs / "partymeh-toslink.yml"))
                )

    def test_conventional_operator_filename_resolves_through_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, config_dir(Path(tmp)) as configs:
            self.assertEqual(
                clock_sync.source_for_config_path(str(configs / "partymeh-toslink.yml")),
                "toslink",
            )
            self.assertEqual(
                clock_sync.source_for_config_path(str(configs / "partymeh-streamer.yml")),
                "streamer",
            )

    def test_plain_and_generated_config_names_still_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, config_dir(Path(tmp)) as configs:
            self.assertEqual(
                clock_sync.source_for_config_path(str(configs / "streamer.yml")),
                "streamer",
            )
            # Generated configs live under a digest directory, and the two
            # names this project generates itself stay identifiable wherever
            # they are: they are the fallback the catalog does not describe.
            self.assertEqual(clock_sync.source_for_config_path("/tmp/streamer.yml"), "streamer")
            self.assertEqual(
                clock_sync.source_for_config_path("/tmp/toslink--kantarellen.yml"),
                "toslink",
            )

    def test_unknown_config_is_not_guessed_from_its_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, config_dir(Path(tmp)) as configs:
            self.assertIsNone(clock_sync.source_for_config_path(str(configs / "partymeh.yml")))
            # A speaker the catalog does not know, named by convention.
            self.assertIsNone(
                clock_sync.source_for_config_path(str(configs / "vintage-toslink.yml"))
            )
            self.assertIsNone(clock_sync.source_for_config_path(""))
            self.assertIsNone(clock_sync.source_for_config_path(None))

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

    def test_datastore_document_names_the_device_clock_source(self) -> None:
        self.assertEqual(
            clock_sync.clock_from_datastore({"ext/clockSource": "Internal"}), "internal"
        )
        self.assertEqual(
            clock_sync.clock_from_datastore({"ext/clockSource": "Optical In A"}), "optical"
        )
        self.assertEqual(
            clock_sync.clock_from_datastore(
                {
                    "ext/clockSource": 2,
                    "ext/clockSourceStrings": "Internal:Word Clock In:ADAT",
                }
            ),
            "optical",
        )
        # Anything the daemon cannot name is unknown, never a clock choice.
        self.assertIsNone(clock_sync.clock_from_datastore({"ext/clockSource": "Word Clock In"}))
        self.assertIsNone(
            clock_sync.clock_from_datastore(
                {"ext/clockSource": 9, "ext/clockSourceStrings": "Internal"}
            )
        )
        self.assertIsNone(clock_sync.clock_from_datastore({"ext/samplerate": 48000}))
        self.assertIsNone(clock_sync.clock_from_datastore("not a document"))

    def test_read_back_reports_unknown_when_the_device_cannot_be_read(self) -> None:
        with (
            mock.patch.object(
                clock_sync.urllib.request, "urlopen", side_effect=OSError("no route")
            ),
            mock.patch.object(clock_sync, "_next_motu_error_log", 0.0),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertIsNone(clock_sync.read_motu_clock())
        self.assertIn("read-back unavailable", out.getvalue())

        with mock.patch.object(
            clock_sync.urllib.request,
            "urlopen",
            datastore_response({"ext/clockSource": "Optical"}),
        ):
            self.assertEqual(clock_sync.read_motu_clock(), "optical")

    def test_read_back_url_is_derived_from_the_websocket_host(self) -> None:
        self.assertEqual(
            clock_sync._datastore_url("ws://169.254.51.193:1280"),
            "http://169.254.51.193/datastore",
        )
        self.assertEqual(
            clock_sync._datastore_url("wss://motu.local:1280"), "https://motu.local/datastore"
        )
        self.assertEqual(clock_sync._datastore_url("not a url"), "")

    def test_main_skips_redundant_clock_write_after_restart(self) -> None:
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 192000}}
        client.config.file_path.return_value = "/tmp/streamer.yml"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "motu-clock-source"
            state.write_text("internal\n")
            with (
                mock.patch.object(clock_sync, "STATE_PATH", state),
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                # An interface that cannot be read back leaves the cache as
                # the only hint, and it must still suppress the re-lock.
                mock.patch.object(clock_sync, "read_motu_clock", return_value=None),
                mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=KeyboardInterrupt
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        set_clock.assert_not_called()

    def test_main_leaves_a_confirmed_clock_alone(self) -> None:
        """A read-back that agrees must not become a reason to re-lock."""
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 48000}}
        client.config.file_path.return_value = "/tmp/toslink.yml"
        sleeps = 0

        def stop_after_third_iteration(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "motu-clock-source"
            state.write_text("optical\n")
            with (
                mock.patch.object(clock_sync, "STATE_PATH", state),
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                mock.patch.object(clock_sync, "read_motu_clock", return_value="optical"),
                mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=stop_after_third_iteration
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        set_clock.assert_not_called()

    def test_main_does_not_trust_the_persisted_clock_as_hardware_state(self) -> None:
        """The cache says internal; the device says optical, and it wins."""
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 48000}}
        client.config.file_path.return_value = "/tmp/streamer.yml"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "motu-clock-source"
            state.write_text("internal\n")
            with (
                mock.patch.object(clock_sync, "STATE_PATH", state),
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                mock.patch.object(clock_sync, "read_motu_clock", return_value="optical"),
                mock.patch.object(clock_sync, "set_motu_clock", return_value=True) as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=KeyboardInterrupt
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

            self.assertEqual(set_clock.call_args_list, [mock.call("internal")])
            self.assertEqual(state.read_text().strip(), "internal")

    def test_main_retries_a_send_the_device_never_applied(self) -> None:
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

        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(clock_sync, "STATE_PATH", Path(tmp) / "motu-clock-source")
        ):
            with (
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                # The send leaves the host, the MOTU stays on internal: a
                # successful send is not proof that the clock changed.
                mock.patch.object(clock_sync, "read_motu_clock", return_value="internal"),
                mock.patch.object(clock_sync, "set_motu_clock", return_value=True) as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=stop_after_second_iteration
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        self.assertEqual(set_clock.call_args_list, [mock.call("optical"), mock.call("optical")])

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

        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(clock_sync, "STATE_PATH", Path(tmp) / "motu-clock-source")
        ):
            with (
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                mock.patch.object(clock_sync, "read_motu_clock", return_value=None),
                mock.patch.object(
                    clock_sync,
                    "set_motu_clock",
                    side_effect=[False, True],
                ) as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=stop_after_second_iteration
                ),
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
    with config_dir(Path("/configs")):
        assert clock_sync.source_for_config_path("/configs/partymeh-streamer.yml") == "streamer"
        assert clock_sync.source_for_config_path("/configs/partymeh-toslink.yml") == "toslink"
    source = (REPOSITORY / "scripts" / "clock_sync.py").read_text()
    success_branch = source.split("if set_motu_clock(desired_clock):", 1)[1]
    assert success_branch.index("last_clock = desired_clock") < success_branch.index(
        "except Exception"
    )


if __name__ == "__main__":
    unittest.main()
