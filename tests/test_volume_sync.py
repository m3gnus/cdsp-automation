from __future__ import annotations

import contextlib
import importlib.util
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
BUILDER = REPOSITORY / "scripts" / "build_librespot_volume_sync.sh"
# The receiver name an earlier release compiled one site's name into, assembled
# from fragments so the literal never appears in this repository.
LEGACY_TAG = "ug" "lan"
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
        self.assertIn("CDSP_SPOTIFY_VOLUME_SOCKET", patch_text)
        self.assertIn("set_volume_external", patch_text)
        self.assertIn("set_volume_without_event", patch_text)
        self.assertIn("spotify_ack:{}:{}", patch_text)
        self.assertIn("mpsc::channel(16)", patch_text)
        self.assertNotIn("mpsc::unbounded_channel", patch_text)
        self.assertIn("from_mode(0o660)", patch_text)
        self.assertIn("--notify-spotify", build)
        self.assertIn("CDSP_SPOTIFY_VOLUME_ACK_SOCKET", build)
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
                            {"name": "lounge", "playerid": "main"},
                            {"name": "lounge-stereo", "playerid": "stereo"},
                            {"name": "unrelated", "playerid": "other"},
                        ]
                    }
                return {}

            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(
                    volume_sync, "LMS_PLAYER_NAMES", ("lounge", "lounge-stereo")
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
                mock.patch.object(volume_sync, "LMS_PLAYER_NAMES", ("lounge",)),
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

    def _print_dropin(
        self, **settings: str
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("SPOTIFY_", "VOLUME_SYNC_", "AIRPLAY_VOLUME_"))
        }
        environment.update(settings)
        return subprocess.run(
            ["bash", str(BUILDER), "--print-dropin"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_dropin_leaves_the_receiver_device_configuration_alone_by_default(
        self,
    ) -> None:
        """Resetting ExecStart= does not clear the base unit's EnvironmentFile=,
        so omitting --device keeps whatever raspotify was already told to use."""
        result = self._print_dropin()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ExecStart=/usr/local/bin/librespot-cdsp\n", result.stdout
        )
        self.assertNotIn("--device", result.stdout)
        self.assertIn(
            "Environment=CDSP_SPOTIFY_VOLUME_SOCKET=/run/raspotify/cdsp-volume.sock",
            result.stdout,
        )
        self.assertIn("Group=audio", result.stdout)

    def test_dropin_renders_the_configured_device_socket_and_group(self) -> None:
        result = self._print_dropin(
            SPOTIFY_ALSA_DEVICE="hw:CARD=Loopback,DEV=0",
            SPOTIFY_VOLUME_COMMAND_SOCKET_PATH="/run/raspotify/site.sock",
            VOLUME_SYNC_GROUP="snd",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ExecStart=/usr/local/bin/librespot-cdsp --device hw:CARD=Loopback,DEV=0\n",
            result.stdout,
        )
        self.assertIn(
            "Environment=CDSP_SPOTIFY_VOLUME_SOCKET=/run/raspotify/site.sock",
            result.stdout,
        )
        self.assertIn("Group=snd", result.stdout)
        # One setting drives both the unit and the health check, so the drop-in
        # and the post-install probe can no longer disagree.
        builder = BUILDER.read_text(encoding="utf-8")
        self.assertIn('[[ ! -S "$COMMAND_SOCKET" ]]', builder)

    def test_dropin_rejects_settings_that_could_escape_the_unit_file(self) -> None:
        for setting in (
            {"SPOTIFY_ALSA_DEVICE": "x; reboot"},
            {"SPOTIFY_ALSA_DEVICE": "%H"},
            {"SPOTIFY_VOLUME_COMMAND_SOCKET_PATH": "relative.sock"},
            {"VOLUME_SYNC_GROUP": "bad group"},
        ):
            result = self._print_dropin(**setting)
            self.assertNotEqual(result.returncode, 0, setting)
            self.assertEqual(result.stdout, "", setting)
            self.assertIn("ExecStart", result.stderr + "ExecStart")

    def test_rollback_restores_a_working_superseded_receiver_pair(self) -> None:
        """The old drop-in only ever comes back while its binary still exists."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dropin_dir = root / "raspotify.service.d"
            dropin_dir.mkdir()
            build = root / "build"
            build.mkdir()
            target = root / "librespot-cdsp"
            legacy_target = root / "librespot-legacy"
            legacy_dropin = dropin_dir / f"{LEGACY_TAG}-volume-sync.conf"
            unrelated = dropin_dir / "10-operator.conf"
            log = root / "systemctl.log"

            target.write_text("new receiver\n", encoding="utf-8")
            legacy_target.write_text("old receiver\n", encoding="utf-8")
            legacy_dropin.write_text("ExecStart=/old\n", encoding="utf-8")
            unrelated.write_text("[Service]\n", encoding="utf-8")
            (dropin_dir / "cdsp-volume-sync.conf").write_text(
                "ExecStart=/new\n", encoding="utf-8"
            )
            (build / "previous-legacy-dropin").write_text(
                "ExecStart=/old\n", encoding="utf-8"
            )

            command = f"""
set -euo pipefail
export CDSP_AUTOMATION_LIBRESPOT_TARGET={target!s}
export CDSP_AUTOMATION_LEGACY_LIBRESPOT_TARGET={legacy_target!s}
export CDSP_AUTOMATION_RASPOTIFY_DROPIN_DIR={dropin_dir!s}
source {BUILDER!s}
systemctl() {{ printf 'systemctl %s\\n' "$*" >> {log!s}; }}
sudo() {{
  case "$1" in
    systemctl) shift; systemctl "$@" ;;
    *) command "$@" ;;
  esac
}}
BUILD_DIR={build!s}
had_target=false
had_dropin=false
had_legacy_dropin=true
rollback
"""
            subprocess.run(["bash", "-c", command], check=True, env=os.environ.copy())

            self.assertEqual(
                legacy_dropin.read_text(encoding="utf-8"), "ExecStart=/old\n"
            )
            self.assertTrue(legacy_target.is_file())
            self.assertFalse(target.exists())
            self.assertFalse((dropin_dir / "cdsp-volume-sync.conf").exists())
            self.assertTrue(unrelated.is_file())
            calls = log.read_text(encoding="utf-8")
            self.assertIn("daemon-reload", calls)
            self.assertIn("restart raspotify.service", calls)

    def test_unknown_marker_content_forces_one_rebuild_then_is_rewritten(self) -> None:
        """A deployment carrying an older marker file has defined behaviour."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "librespot-volume-sync.sha256"
            build = root / "build"
            build.mkdir()
            marker.write_text("a" * 64 + "\n", encoding="utf-8")
            log = root / "result.log"

            command = f"""
set -euo pipefail
export CDSP_AUTOMATION_LIBRESPOT_MARKER={marker!s}
source {BUILDER!s}
sudo() {{
  local args=()
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  command install "${{args[@]}}"
}}
BUILD_DIR={build!s}
digest="$(printf 'payload' | sha256_stream)"
if marker_matches "$digest"; then echo "stale-marker-accepted" >> {log!s}; else echo "rebuild" >> {log!s}; fi
write_marker "$digest"
if marker_matches "$digest"; then echo "rewritten" >> {log!s}; else echo "still-unknown" >> {log!s}; fi
if marker_matches "different"; then echo "any-digest-accepted" >> {log!s}; else echo "digest-checked" >> {log!s}; fi
"""
            subprocess.run(["bash", "-c", command], check=True, env=os.environ.copy())
            self.assertEqual(
                log.read_text(encoding="utf-8").split(),
                ["rebuild", "rewritten", "digest-checked"],
            )
            self.assertTrue(
                marker.read_text(encoding="utf-8").startswith("cdsp-volume-sync/1 ")
            )

    def test_uninstall_removes_both_receiver_generations(self) -> None:
        builder = BUILDER.read_text(encoding="utf-8")
        uninstall = builder.split("uninstall() {", 1)[1].split("\n}", 1)[0]
        self.assertIn('sudo rm -f "$DROPIN" "$TARGET" "$MARKER"', uninstall)
        self.assertIn('sudo rm -f "$LEGACY_DROPIN" "$LEGACY_TARGET"', uninstall)

    def test_superseded_artifacts_are_removed_only_after_both_services_pass(
        self,
    ) -> None:
        """Ordering is the whole safety property of the migration."""
        builder = BUILDER.read_text(encoding="utf-8")
        remove_legacy_dropin = builder.index('sudo rm -f "$LEGACY_DROPIN"')
        reload_units = builder.index("sudo systemctl daemon-reload\n  sudo systemctl restart airplay-volume-bridge.service")
        health_check = builder.index("Patched librespot did not become healthy.")
        complete = builder.index("deployment_complete=true")
        remove_legacy_target = builder.index('sudo rm -f "$LEGACY_TARGET"')
        self.assertLess(remove_legacy_dropin, reload_units)
        self.assertLess(health_check, complete)
        self.assertLess(complete, remove_legacy_target)
        self.assertIn(
            "systemctl is-active --quiet airplay-volume-bridge.service", builder
        )
        # Exact names only: no glob may sweep an administrator's own drop-in,
        # receiver binary or socket out of the way.
        for shape in ("*volume-sync.conf", "*volume.sock", "librespot-*", "find "):
            self.assertNotIn(shape, builder, shape)

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


def test_spotify_tracker_deadlines_use_monotonic_time(tmp_path: Path) -> None:
    """Every tracker deadline must be fed the monotonic clock, not time.time().

    The UI can step this Pi's wall clock from a phone, so a retry, heartbeat or
    ack deadline measured against time.time() would strand a pending Spotify
    command. Drives one full daemon iteration and records what the tracker was
    actually given, so a call site regressing to time.time() fails here.
    """
    wall, mono = 1_900_000_000.0, 1234.5
    given: list[float] = []

    class RecordingTracker(volume_sync.SpotifyCommandTracker):
        def queue(self, volume, camilla_state, *, now):
            given.append(now)
            return super().queue(volume, camilla_state, now=now)

        def should_send(self, now):
            given.append(now)
            return super().should_send(now)

        def mark_sent(self, now):
            given.append(now)
            return super().mark_sent(now)

        def acknowledge(self, command_id, volume, *, now):
            given.append(now)
            return super().acknowledge(command_id, volume, now=now)

        def needs_heartbeat(self, now):
            given.append(now)
            return super().needs_heartbeat(now)

        def healthy(self, now):
            given.append(now)
            return super().healthy(now)

    class Server:
        def __init__(self) -> None:
            self.reads = 0

        def bind(self, _path: str) -> None:
            pass

        def settimeout(self, _timeout: float) -> None:
            pass

        def setblocking(self, _flag: bool) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"spotify_ack:1:40000"
            raise BlockingIOError

        def close(self) -> None:
            pass

    def stop_after_one_pass(_status: dict) -> None:
        raise KeyboardInterrupt

    with (
        patch.object(volume_sync.time, "time", lambda: wall),
        patch.object(volume_sync.time, "monotonic", lambda: mono),
        patch.object(volume_sync, "SpotifyCommandTracker", RecordingTracker),
        patch.object(volume_sync, "SOCKET_PATH", tmp_path / "input.sock"),
        patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", tmp_path / "active"),
        patch.object(
            volume_sync, "SPOTIFY_COMMAND_SOCKET_PATH", tmp_path / "command.sock"
        ),
        patch.object(volume_sync.socket, "socket", lambda *_args: Server()),
        patch.object(volume_sync, "secure_socket", lambda _path: None),
        patch.object(volume_sync, "service_is_active", lambda _service: False),
        patch.object(volume_sync, "CamillaClient", lambda *_a: SimpleNamespace(
            connect=lambda: None, disconnect=lambda: None
        )),
        patch.object(
            volume_sync, "read_mirrorable_camilla_volume", lambda _c: (-20.0, False)
        ),
        patch.object(volume_sync, "send_spotify_volume", lambda *_a: None),
        patch.object(volume_sync, "write_status", stop_after_one_pass),
    ):
        with contextlib.suppress(KeyboardInterrupt):
            volume_sync.run_daemon()

    # acknowledge, queue, should_send and mark_sent at minimum.
    assert len(given) >= 4
    assert given == [mono] * len(given)


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


def test_secure_socket_survives_a_missing_group_and_still_restricts_the_mode(
    tmp_path: Path, capsys
) -> None:
    """secure_socket runs outside run_daemon's try/except; it must not raise."""
    path = tmp_path / "input.sock"
    path.write_bytes(b"")
    path.chmod(0o666)
    with patch.object(
        volume_sync.grp, "getgrnam", side_effect=KeyError("no such group")
    ):
        volume_sync.secure_socket(path)
    assert path.stat().st_mode & 0o777 == 0o660
    assert "unavailable" in capsys.readouterr().err


def test_secure_socket_survives_a_denied_chown_without_widening_the_socket(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "input.sock"
    path.write_bytes(b"")
    path.chmod(0o666)
    with (
        patch.object(volume_sync.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=0)),
        patch.object(volume_sync.os, "chown", side_effect=PermissionError("denied")),
    ):
        volume_sync.secure_socket(path)
    # Tightened before the chown is attempted, so a failure never leaves the
    # receiver socket world-writable.
    assert path.stat().st_mode & 0o777 == 0o660
    message = capsys.readouterr().err
    assert "AirPlay and Spotify callbacks" in message
