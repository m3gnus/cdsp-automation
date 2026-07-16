from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


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
        self.assertEqual(volume_sync.map_camilla_to_spotify(0, False), 65535)
        self.assertEqual(volume_sync.map_camilla_to_spotify(-50, True), 0)
        self.assertEqual(volume_sync.map_camilla_to_spotify(-50, False), 1)

    def test_build_is_pinned_and_avoids_double_attenuation(self) -> None:
        build = (REPOSITORY / "scripts" / "build_librespot_volume_sync.sh").read_text()
        patch = (
            REPOSITORY
            / "librespot-volume-sync"
            / "librespot-v0.8.0-volume-sync.patch"
        ).read_text()
        self.assertIn("d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d", build)
        self.assertIn("LIBRESPOT_VOLUME_CTRL=fixed", build)
        self.assertIn("Box::new(NoOpVolume)", patch)
        self.assertIn("set_volume_external", patch)
        self.assertIn("set_volume_without_event", patch)
        self.assertIn("spotify_ack:{}:{}", patch)
        self.assertIn("mpsc::channel(16)", patch)
        self.assertNotIn("mpsc::unbounded_channel", patch)
        self.assertIn("UGLAN_SPOTIFY_VOLUME_ACK_SOCKET", build)
        self.assertIn("Group=audio", build)

    def test_outbound_command_stays_pending_until_exact_ack(self) -> None:
        tracker = volume_sync.SpotifyCommandTracker()
        command_id = tracker.queue(0, (-20.0, True), now=1.0)
        self.assertTrue(tracker.should_send(1.0))
        tracker.mark_sent(1.0)
        self.assertFalse(tracker.acknowledge(command_id, 1, now=1.1))
        self.assertIsNotNone(tracker.pending)
        self.assertTrue(tracker.acknowledge(command_id, 0, now=1.2))
        self.assertTrue(tracker.healthy(1.2))

    def test_idle_receiver_is_distinct_from_acknowledged_sync(self) -> None:
        bridge = SCRIPT.read_text()
        self.assertIn('"receiver_socket": receiver_socket', bridge)
        self.assertIn('else "idle"', bridge)
        self.assertIn("waiting for an active Spotify Connect session", bridge)

    def test_airplay_handoff_stops_schedule_players_and_clears_inhibit(self) -> None:
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
                        ]
                    }
                return {}

            with (
                mock.patch.object(volume_sync, "AIRPLAY_ACTIVE_PATH", active),
                mock.patch.object(volume_sync, "_lms_request", side_effect=request),
                mock.patch.object(volume_sync.time, "sleep"),
            ):
                volume_sync.interrupt_scheduled_playback()
                self.assertEqual(active.read_text().strip(), "active")
                volume_sync.finish_airplay_playback()
            self.assertFalse(active.exists())
            self.assertIn(("main", ["stop"]), calls)
            self.assertIn(("stereo", ["stop"]), calls)


if __name__ == "__main__":
    unittest.main()
