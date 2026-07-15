from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


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
        self.assertIn("spirc.set_volume(volume)", patch)


if __name__ == "__main__":
    unittest.main()
