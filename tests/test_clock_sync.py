from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
import contextlib
import io
import itertools
from pathlib import Path
from unittest import mock

import pytest


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


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def connect_dump() -> list[bytes]:
    """The frames a real UltraLite mk5 pushed to a fresh WebSocket client."""
    document = json.loads((FIXTURES / "motu_ultralite_mk5_connect_dump.json").read_text())
    return [bytes.fromhex(frame) for frame in document["frames_hex"]]


class ReplaySocket:
    """A WebSocket stand-in replaying captured device frames, recording sends."""

    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    def connect(self, *_args: object, **_kwargs: object) -> None:
        pass

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv_data(self, *_args: object, **_kwargs: object) -> tuple[int, bytes]:
        if not self.frames:
            raise TimeoutError("timed out")
        return clock_sync.websocket.ABNF.OPCODE_BINARY, self.frames.pop(0)

    def send(self, payload: bytes, *_args: object, **_kwargs: object) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


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

    def test_real_connect_dump_carries_exactly_one_clock_frame(self) -> None:
        """Parse what an UltraLite mk5 actually pushed, not a guessed format."""
        frames = connect_dump()
        values = [clock_sync.clock_source_value(frame) for frame in frames]
        found = [value for value in values if value is not None]
        # Captured while the rig ran the streamer source on the internal clock.
        self.assertEqual(found, [3])
        self.assertEqual(clock_sync.MOTU_CLOCK_SOURCE_VALUES[3], "internal")
        # Neighbouring parameters in the same dump are not the clock:
        # sample rate (10), clock lock flags (15, 16), meters (6000).
        self.assertIsNone(clock_sync.clock_source_value(bytes.fromhex("000a00000002ee00")))
        self.assertIsNone(clock_sync.clock_source_value(bytes.fromhex("000f000001")))
        self.assertIsNone(clock_sync.clock_source_value(frames[-1]))
        # A client-style write frame carries a length field and is not a report.
        self.assertIsNone(
            clock_sync.clock_source_value(bytes.fromhex(clock_sync.CLOCK_PAYLOADS["optical"]))
        )

    def test_write_payloads_are_the_encoding_of_the_values_read_back(self) -> None:
        """Writes and read-back must agree on one parameter and one value set."""
        by_name = {name: value for value, name in clock_sync.MOTU_CLOCK_SOURCE_VALUES.items()}
        self.assertEqual(set(by_name), set(clock_sync.CLOCK_PAYLOADS))
        for name, value in by_name.items():
            expected = bytes([0, clock_sync.MOTU_CLOCK_SOURCE_PARAM, 0, 0, 0, 1, value])
            self.assertEqual(bytes.fromhex(clock_sync.CLOCK_PAYLOADS[name]), expected)

    def test_read_back_takes_the_clock_from_the_state_pushed_on_connect(self) -> None:
        sock = ReplaySocket(connect_dump())
        with mock.patch.object(clock_sync.websocket, "WebSocket", return_value=sock):
            self.assertEqual(clock_sync.read_motu_clock(), "internal")
        # Read-only: nothing was sent, the socket was closed, and reading
        # stopped at the clock frame instead of draining the meter stream.
        self.assertEqual(sock.sent, [])
        self.assertTrue(sock.closed)
        frames = connect_dump()
        clock_at = frames.index(bytes.fromhex("000b000003"))
        self.assertEqual(sock.frames, frames[clock_at + 1 :])

    def test_read_back_maps_optical_and_leaves_other_sources_unknown(self) -> None:
        # Several read-backs in one test: lift the shared access window, which
        # has its own tests below.
        window = mock.patch.dict(os.environ, {"MOTU_ACCESS_WINDOW_SECONDS": "0"})
        window.start()
        self.addCleanup(window.stop)
        def dump_with_clock(value: int) -> list[bytes]:
            return [
                frame[:4] + bytes([value])
                if clock_sync.clock_source_value(frame) is not None
                else frame
                for frame in connect_dump()
            ]

        with mock.patch.object(
            clock_sync.websocket, "WebSocket", return_value=ReplaySocket(dump_with_clock(2))
        ):
            self.assertEqual(clock_sync.read_motu_clock(), "optical")

        # 0 is the device's S/PDIF input: a real source, but not one we drive.
        with (
            mock.patch.object(
                clock_sync.websocket, "WebSocket", return_value=ReplaySocket(dump_with_clock(0))
            ),
            mock.patch.object(clock_sync, "_next_motu_error_log", 0.0),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertIsNone(clock_sync.read_motu_clock())
        self.assertIn("neither internal nor optical", out.getvalue())

    def test_read_back_reports_unknown_when_the_device_cannot_be_read(self) -> None:
        # Several read-backs in one test: lift the shared access window, which
        # has its own tests below.
        window = mock.patch.dict(os.environ, {"MOTU_ACCESS_WINDOW_SECONDS": "0"})
        window.start()
        self.addCleanup(window.stop)
        offline = mock.Mock()
        offline.connect.side_effect = OSError("no route")
        with (
            mock.patch.object(clock_sync.websocket, "WebSocket", return_value=offline),
            mock.patch.object(clock_sync, "_next_motu_error_log", 0.0),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertIsNone(clock_sync.read_motu_clock())
        self.assertIn("read-back unavailable", out.getvalue())
        offline.close.assert_called_once()

        # Only meters, then silence: no clock frame is "unknown", not a guess.
        meters_only = ReplaySocket([connect_dump()[-1]])
        with (
            mock.patch.object(clock_sync.websocket, "WebSocket", return_value=meters_only),
            mock.patch.object(clock_sync, "_next_motu_error_log", 0.0),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertIsNone(clock_sync.read_motu_clock())
        self.assertIn("read-back unavailable", out.getvalue())
        self.assertEqual(meters_only.sent, [])

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
                # Passes far enough apart for the re-write back-off to expire.
                mock.patch.object(
                    clock_sync.time, "monotonic", side_effect=itertools.count(0, 100)
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        self.assertEqual(set_clock.call_args_list, [mock.call("optical"), mock.call("optical")])

    def test_main_does_not_rewrite_a_refused_clock_every_pass(self) -> None:
        """Each write re-locks the MOTU audibly; a refusal must not loop it."""
        client = mock.Mock()
        client.is_connected.return_value = True
        client.config.active.return_value = {"devices": {"samplerate": 48000}}
        client.config.file_path.return_value = "/tmp/toslink.yml"
        sleeps = 0

        def stop_after_fifth_iteration(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps == 5:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp, (
            mock.patch.object(clock_sync, "STATE_PATH", Path(tmp) / "motu-clock-source")
        ):
            with (
                mock.patch.object(clock_sync, "CamillaClient", return_value=client),
                mock.patch.object(clock_sync, "read_motu_clock", return_value="internal"),
                mock.patch.object(clock_sync, "set_motu_clock", return_value=True) as set_clock,
                mock.patch.object(
                    clock_sync.time, "sleep", side_effect=stop_after_fifth_iteration
                ),
                # One pass per second, well inside the re-write back-off.
                mock.patch.object(
                    clock_sync.time, "monotonic", side_effect=itertools.count(0, 1)
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                clock_sync.main()

        self.assertEqual(set_clock.call_args_list, [mock.call("optical")])

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


def _clock_sync_client(paths: list[str]) -> mock.Mock:
    client = mock.Mock()
    client.is_connected.return_value = True
    client.config.active.return_value = {"devices": {"samplerate": 48000}}
    client.config.file_path.side_effect = lambda: paths[0]
    return client


def test_main_adopts_a_clock_the_switcher_already_wrote(tmp_path: Path) -> None:
    """The switcher moved config and clock together; do not write it again."""
    state = tmp_path / "motu-clock-source"
    state.write_text("internal\n")
    paths = ["/tmp/streamer.yml"]
    client = _clock_sync_client(paths)
    passes = itertools.count()

    def next_pass(_seconds: float) -> None:
        if next(passes) == 0:
            # A muted switcher transition, complete by the next pass.
            paths[0] = "/tmp/toslink.yml"
            state.write_text("optical\n")
        else:
            raise KeyboardInterrupt

    with (
        mock.patch.object(clock_sync, "STATE_PATH", state),
        mock.patch.object(clock_sync, "CamillaClient", return_value=client),
        mock.patch.object(clock_sync, "read_motu_clock", return_value=None),
        mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
        mock.patch.object(clock_sync.time, "sleep", side_effect=next_pass),
        contextlib.redirect_stdout(io.StringIO()),
        pytest.raises(KeyboardInterrupt),
    ):
        clock_sync.main()

    set_clock.assert_not_called()


def test_main_redecides_under_the_lock_instead_of_undoing_a_transition(
    tmp_path: Path,
) -> None:
    """Half-way through a transition the clock is new but the path is old.

    Observed outside the lock that reads as "write the old clock back"; the
    decision is re-made once the switcher releases the lock, by which time
    the path has caught up and there is nothing to do.
    """
    state = tmp_path / "motu-clock-source"
    state.write_text("optical\n")
    paths = ["/tmp/streamer.yml"]
    client = _clock_sync_client(paths)
    guarded: list[bool] = []

    @contextlib.contextmanager
    def switcher_finishes_while_we_wait():
        paths[0] = "/tmp/toslink.yml"
        guarded.append(True)
        yield True

    with (
        mock.patch.object(clock_sync, "STATE_PATH", state),
        mock.patch.object(clock_sync, "CamillaClient", return_value=client),
        mock.patch.object(clock_sync, "read_motu_clock", return_value=None),
        mock.patch.object(clock_sync, "transition_guard", switcher_finishes_while_we_wait),
        mock.patch.object(clock_sync, "set_motu_clock") as set_clock,
        mock.patch.object(clock_sync.time, "sleep", side_effect=KeyboardInterrupt),
        contextlib.redirect_stdout(io.StringIO()),
        pytest.raises(KeyboardInterrupt),
    ):
        clock_sync.main()

    assert guarded == [True]
    set_clock.assert_not_called()


def test_clock_decisions_wait_for_the_audio_control_lock(tmp_path: Path) -> None:
    lock = Path(os.environ["AUDIO_CONTROL_LOCK_PATH"])
    entered: list[str] = []
    with speaker_profiles.audio_control_lock(lock):
        import threading

        def decide() -> None:
            with clock_sync.transition_guard():
                entered.append("clock_sync")

        worker = threading.Thread(target=decide)
        worker.start()
        worker.join(0.2)
        assert entered == []
        entered.append("switcher done")
    worker.join(2)
    assert entered == ["switcher done", "clock_sync"]


def _one_pass_wanting_optical(tmp_path: Path, **patches) -> mock.Mock:
    state = tmp_path / "motu-clock-source"
    state.write_text("internal\n")
    client = _clock_sync_client(["/tmp/toslink.yml"])
    with contextlib.ExitStack() as stack:
        for target, name, value in (
            (clock_sync, "STATE_PATH", state),
            (clock_sync, "CamillaClient", mock.Mock(return_value=client)),
            (clock_sync, "read_motu_clock", mock.Mock(return_value=None)),
            *patches.get("extra", ()),
        ):
            stack.enter_context(mock.patch.object(target, name, value))
        set_clock = stack.enter_context(mock.patch.object(clock_sync, "set_motu_clock"))
        stack.enter_context(
            mock.patch.object(clock_sync.time, "sleep", side_effect=KeyboardInterrupt)
        )
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        with pytest.raises(KeyboardInterrupt):
            clock_sync.main()
    return set_clock


def test_main_only_verifies_while_the_switcher_manages_the_clock(
    tmp_path: Path, monkeypatch
) -> None:
    unit = tmp_path / "cdsp-source-switcher.service"
    unit.touch()
    monkeypatch.setenv("SOURCE_SWITCHER_UNIT_PATH", str(unit))
    _one_pass_wanting_optical(tmp_path).assert_not_called()

    # Opting the switcher out hands the writes back to this daemon.
    monkeypatch.setenv("SOURCE_MOTU_CLOCK", "false")
    _one_pass_wanting_optical(tmp_path).assert_called_once_with("optical")


def test_main_skips_the_write_when_the_lock_cannot_be_taken(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SOURCE_SWITCHER_UNIT_PATH", str(tmp_path / "absent"))
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    monkeypatch.setenv("AUDIO_CONTROL_LOCK_PATH", str(blocked / "audio.lock"))
    _one_pass_wanting_optical(
        tmp_path, extra=((clock_sync, "_next_motu_error_log", 0.0),)
    ).assert_not_called()
