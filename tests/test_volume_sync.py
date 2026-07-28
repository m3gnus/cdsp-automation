from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

import web_ui


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "airplay_volume_bridge.py"
spec = importlib.util.spec_from_file_location("volume_sync", SCRIPT)
assert spec and spec.loader
volume_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume_sync)


class VolumeSyncTests(unittest.TestCase):
    def test_spotify_mapping_round_trips_shared_fader(self) -> None:
        self.assertEqual(volume_sync.map_spotify_volume(0), (-50.0, True))
        self.assertEqual(volume_sync.map_spotify_volume(65535), (0.0, False))
        middle_db, muted = volume_sync.map_spotify_volume(32768)
        self.assertFalse(muted)
        self.assertAlmostEqual(middle_db, -25.0, delta=0.001)
        self.assertEqual(volume_sync.map_camilla_to_spotify(0, False), 65535)
        self.assertEqual(volume_sync.map_camilla_to_spotify(-50, True), 0)
        self.assertEqual(volume_sync.map_camilla_to_spotify(-50, False), 1)

    def test_build_is_pinned_and_avoids_double_attenuation(self) -> None:
        build = (REPOSITORY / "scripts" / "build_librespot_volume_sync.sh").read_text()
        patch_text = (
            REPOSITORY
            / "librespot-volume-sync"
            / "librespot-v0.8.0-volume-sync.patch"
        ).read_text()
        self.assertIn("d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d", build)
        self.assertIn("LIBRESPOT_VOLUME_CTRL=fixed", build)
        self.assertIn("Box::new(NoOpVolume)", patch_text)
        self.assertIn("UGLAN_SPOTIFY_VOLUME_SOCKET", patch_text)
        self.assertIn("set_volume_external", patch_text)
        self.assertIn("set_volume_without_event", patch_text)
        self.assertIn("spotify_ack:{}:{}", patch_text)
        self.assertIn("mpsc::channel(16)", patch_text)
        self.assertNotIn("mpsc::unbounded_channel", patch_text)
        self.assertIn("from_mode(0o660)", patch_text)
        self.assertIn("ExecStart=$TARGET --device uglan_main", build)
        self.assertIn("--notify-spotify", build)
        self.assertIn("UGLAN_SPOTIFY_VOLUME_ACK_SOCKET", build)
        self.assertIn("Group=audio", build)
        self.assertIn("deployment_started=true", build)
        self.assertIn('git -C "$BUILD_DIR/librespot" apply --check', build)

    def test_outbound_command_stays_pending_until_exact_ack(self) -> None:
        tracker = volume_sync.SpotifyCommandTracker()
        command_id = tracker.queue(0, (-20.0, True), now=1.0)
        self.assertTrue(tracker.should_send(1.0))
        tracker.mark_sent(1.0)
        self.assertFalse(tracker.should_send(1.05))
        self.assertFalse(tracker.acknowledge(command_id, 1, now=1.1))
        self.assertIsNotNone(tracker.pending)
        self.assertTrue(tracker.acknowledge(command_id, 0, now=1.2))
        self.assertIsNone(tracker.pending)
        self.assertTrue(tracker.healthy(1.2))
        self.assertEqual(
            volume_sync.parse_bridge_message(f"spotify_ack:{command_id}:0".encode()),
            ("spotify_ack", (command_id, 0)),
        )

    def test_idle_receiver_is_distinct_from_acknowledged_sync(self) -> None:
        bridge = SCRIPT.read_text()
        self.assertIn('"receiver_socket": receiver_socket', bridge)
        self.assertIn('else "idle"', bridge)
        self.assertIn("waiting for an active Spotify Connect session", bridge)

    def test_airplay_handoff_stops_streamer_players_and_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            calls: list[tuple[str, list[object]]] = []

            def request(player: str, terms: list[object]) -> dict:
                calls.append((player, terms))
                if terms[0] == "players":
                    return {
                        "players_loop": [
                            {"name": "uglan", "playerid": "main"},
                            {"name": "uglan-stereo", "playerid": "stereo"},
                            {"name": "unrelated", "playerid": "other"},
                        ]
                    }
                return {}

            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(
                    volume_sync, "LMS_PLAYER_NAMES", ("uglan", "uglan-stereo")
                ),
                mock.patch.object(volume_sync, "_lms_request", side_effect=request),
                mock.patch.object(volume_sync.time, "sleep"),
            ):
                volume_sync.begin_network_playback()
                self.assertEqual(active.read_text().strip(), "airplay-active")
                volume_sync.finish_network_playback()
            self.assertFalse(active.exists())
            self.assertIn(("main", ["stop"]), calls)
            self.assertIn(("stereo", ["stop"]), calls)
            self.assertNotIn(("other", ["stop"]), calls)

    def test_unreachable_lms_never_blocks_network_playback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(volume_sync, "LMS_PLAYER_NAMES", ("uglan",)),
                mock.patch.object(
                    volume_sync,
                    "_lms_request",
                    side_effect=OSError("connection refused"),
                ),
                mock.patch.object(volume_sync.time, "sleep") as sleep,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                volume_sync.begin_network_playback()
                self.assertEqual(active.read_text().strip(), "airplay-active")
            sleep.assert_not_called()
            self.assertIn("LMS streamer stop skipped", output.getvalue())

    def test_no_configured_players_skips_lms_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(volume_sync, "LMS_PLAYER_NAMES", ()),
                mock.patch.object(volume_sync, "_lms_request") as lms,
                mock.patch.object(volume_sync.time, "sleep") as sleep,
            ):
                volume_sync.begin_network_playback("spotify")
                self.assertEqual(active.read_text().strip(), "spotify-active")
            lms.assert_not_called()
            sleep.assert_not_called()

    def test_first_active_receiver_keeps_playback_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            services: list[tuple[str, bool]] = []
            arbiter = volume_sync.PlaybackArbiter()
            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(volume_sync, "_lms_request", return_value={}),
                mock.patch.object(volume_sync.time, "sleep"),
                mock.patch.object(
                    volume_sync,
                    "set_receiver_service",
                    side_effect=lambda service, enabled: services.append(
                        (service, enabled)
                    ),
                ),
            ):
                self.assertTrue(arbiter.start("spotify"))
                self.assertFalse(arbiter.start("airplay"))
                self.assertEqual(arbiter.owner, "spotify")
                self.assertEqual(active.read_text().strip(), "spotify-active")
                self.assertTrue(arbiter.stop("spotify"))
            self.assertEqual(
                services,
                [
                    (volume_sync.AIRPLAY_SERVICE, False),
                    (volume_sync.AIRPLAY_SERVICE, False),
                    (volume_sync.AIRPLAY_SERVICE, True),
                ],
            )

    def test_receiver_stop_keeps_retryable_state_when_peer_enable_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active"
            active.write_text("airplay-active\n")
            arbiter = volume_sync.PlaybackArbiter()
            arbiter.owner = "airplay"
            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(
                    volume_sync,
                    "set_receiver_service",
                    side_effect=RuntimeError("systemctl failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "systemctl failed"),
            ):
                arbiter.stop("airplay")

            self.assertEqual(arbiter.owner, "airplay")
            self.assertEqual(active.read_text().strip(), "airplay-active")


def test_airplay_volume_mapping_endpoints_curve_and_mute() -> None:
    assert volume_sync.map_airplay_volume(-144) == (-50.0, True)
    assert volume_sync.map_airplay_volume(-30) == (-50.0, False)
    assert volume_sync.map_airplay_volume(0) == (0.0, False)
    midpoint, muted = volume_sync.map_airplay_volume(-15)
    assert not muted
    assert midpoint == -25.0
    # Both source and destination are halfway through their visual sliders.
    assert (midpoint + 50.0) / 50.0 == (-15.0 + 30.0) / 30.0
    assert 'id="volRange" data-volume type="range" min="-50" max="0"' in web_ui.HTML
    for unsafe in ((-70, 3, 1.5), (-10, 0, 1.5), (-70, 0, 8)):
        try:
            volume_sync.map_airplay_volume(-10, *unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe mapping accepted: {unsafe}")


def test_spotify_event_callback_forwards_volume_and_playback_lifecycle(monkeypatch) -> None:
    sent: list[tuple[bytes, str]] = []

    class Socket:
        def settimeout(self, _timeout: float) -> None:
            pass

        def sendto(self, payload: bytes, path: str) -> None:
            sent.append((payload, path))

        def close(self) -> None:
            pass

    monkeypatch.setattr(volume_sync.socket, "socket", lambda *_args: Socket())
    volume_sync.notify_spotify({"PLAYER_EVENT": "playing", "VOLUME": "7"})
    volume_sync.notify_spotify(
        {"PLAYER_EVENT": "volume_changed", "VOLUME": "32768"}
    )
    volume_sync.notify_spotify({"PLAYER_EVENT": "paused"})
    volume_sync.notify_spotify({"PLAYER_EVENT": "stopped"})
    assert sent == [
        (b"spotify_session:start", str(volume_sync.SOCKET_PATH)),
        (b"spotify:32768", str(volume_sync.SOCKET_PATH)),
        (b"spotify_session:stop", str(volume_sync.SOCKET_PATH)),
        (b"spotify_session:stop", str(volume_sync.SOCKET_PATH)),
    ]


def test_spotify_tracker_deadlines_use_monotonic_time() -> None:
    source = inspect.getsource(volume_sync.run_daemon)
    assert "tracker_now = time.monotonic()" in source
    assert "spotify_sync.acknowledge(\n" in source
    assert "now=tracker_now" in source
    assert "spotify_sync.healthy(tracker_now)" in source


def test_spotify_mirror_pauses_before_reading_a_transition_mute(
    tmp_path: Path,
) -> None:
    class Volume:
        def __init__(self) -> None:
            self.reads = 0

        def main_volume(self) -> float:
            self.reads += 1
            return -50.0

        def main_mute(self) -> bool:
            self.reads += 1
            return True

    volume = Volume()
    client = SimpleNamespace(volume=volume)
    with (
        patch.object(
            volume_sync, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"
        ),
        patch.object(volume_sync, "AUDIO_READY_PATH", tmp_path / "missing.json"),
    ):
        assert volume_sync.read_mirrorable_camilla_volume(client) is None
    assert volume.reads == 0


def test_airplay_cannot_unmute_while_config_transition_is_inhibited(
    tmp_path: Path,
) -> None:
    class Volume:
        def __init__(self) -> None:
            self.mute = True
            self.volume = -40.0

        def set_main_volume(self, value: float) -> None:
            self.volume = value

        def set_main_mute(self, value: bool) -> None:
            self.mute = value

    client = SimpleNamespace(volume=Volume())
    ready = tmp_path / "ready.json"
    with (
        patch.object(
            volume_sync, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"
        ),
        patch.object(volume_sync, "AUDIO_READY_PATH", ready),
    ):
        try:
            volume_sync.set_client_volume(client, -10)
        except RuntimeError as exc:
            assert "inhibited" in str(exc)
        else:
            raise AssertionError("AirPlay unmuted an inhibited output")
    assert client.volume.mute is True
    assert client.volume.volume == -40.0


def test_airplay_notify_callback_starts_without_deployment_helpers(
    tmp_path: Path,
) -> None:
    """The /usr/local callback copy must not import daemon-only modules."""
    callback = tmp_path / "airplay_volume_bridge.py"
    callback.write_bytes(SCRIPT.read_bytes())
    result = subprocess.run(
        [sys.executable, str(callback)],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage: airplay_volume_bridge.py" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_playback_arbiter_keeps_first_receiver_until_it_stops(tmp_path: Path) -> None:
    active = tmp_path / "playback-active"
    services: list[tuple[str, bool]] = []
    arbiter = volume_sync.PlaybackArbiter()
    with (
        patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
        patch.object(volume_sync, "_lms_request", return_value={}),
        patch.object(volume_sync.time, "sleep"),
        patch.object(
            volume_sync,
            "set_receiver_service",
            side_effect=lambda service, enabled: services.append((service, enabled)),
        ),
    ):
        assert arbiter.start("airplay")
        assert not arbiter.start("spotify")
        assert arbiter.owner == "airplay"
        assert active.read_text().strip() == "airplay-active"
        assert not arbiter.stop("spotify")
        assert arbiter.stop("airplay")
    assert services == [
        (volume_sync.SPOTIFY_SERVICE, False),
        (volume_sync.SPOTIFY_SERVICE, False),
        (volume_sync.SPOTIFY_SERVICE, True),
    ]
    assert not active.exists()


def test_playback_arbiter_recovers_active_airplay_from_dbus(tmp_path: Path) -> None:
    active = tmp_path / "playback-active"
    services: list[tuple[str, bool]] = []
    arbiter = volume_sync.PlaybackArbiter()
    with (
        patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
        patch.object(volume_sync, "shairport_playback_active", return_value=True),
        patch.object(
            volume_sync,
            "set_receiver_service",
            side_effect=lambda service, enabled: services.append((service, enabled)),
        ),
    ):
        assert arbiter.recover() == "airplay"
    assert active.read_text().strip() == "airplay-active"
    assert services == [(volume_sync.SPOTIFY_SERVICE, False)]


if __name__ == "__main__":
    unittest.main()
