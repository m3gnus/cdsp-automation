from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

from scripts import speaker_profiles


# conftest pins this at a nonexistent path so imports stay hermetic; restore
# that instead of unsetting, which would fall back to the real /etc lookup.
HERMETIC_CATALOG_PATH = os.environ.get(
    "SPEAKER_CATALOG_PATH", "/nonexistent/speaker-catalog.json"
)


def _reload_with_catalog(path: str) -> None:
    os.environ["SPEAKER_CATALOG_PATH"] = path
    importlib.reload(speaker_profiles)


class SpeakerCatalogNormalizationTests(unittest.TestCase):
    def test_minimal_catalog_defaults_label_and_first_speaker(self) -> None:
        catalog = speaker_profiles.normalize_speaker_catalog(
            {"speakers": {"mains": {}}}
        )
        self.assertEqual(catalog["default"], "mains")
        self.assertEqual(catalog["speakers"]["mains"]["label"], "mains")
        self.assertEqual(catalog["operator_configs"], {})

    def test_operator_configs_are_collected_per_speaker(self) -> None:
        catalog = speaker_profiles.normalize_speaker_catalog(
            {
                "default": "mains",
                "speakers": {
                    "mains": {},
                    "vintage": {
                        "operator_configs": {"streamer": "vintage-streamer.yml"}
                    },
                },
            }
        )
        self.assertEqual(
            catalog["operator_configs"],
            {"vintage": {"streamer": "vintage-streamer.yml"}},
        )

    def test_unknown_default_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            speaker_profiles.normalize_speaker_catalog(
                {"default": "missing", "speakers": {"mains": {}}}
            )

    def test_empty_or_malformed_documents_are_rejected(self) -> None:
        for raw in (None, [], {"speakers": {}}, {"speakers": {"x": []}}):
            with self.assertRaises(ValueError):
                speaker_profiles.normalize_speaker_catalog(raw)

    def test_operator_configs_reject_invalid_sources_and_paths(self) -> None:
        invalid_configs = (
            [],
            {"bluetooth": "vintage-bluetooth.yml"},
            {"streamer": "../vintage-streamer.yml"},
            {"streamer": "/tmp/vintage-streamer.yml"},
            {"streamer": "vintage-streamer.txt"},
            {"streamer": 42},
        )
        for operator_configs in invalid_configs:
            with self.subTest(operator_configs=operator_configs):
                with self.assertRaises(ValueError):
                    speaker_profiles.normalize_speaker_catalog(
                        {
                            "speakers": {
                                "mains": {},
                                "vintage": {
                                    "operator_configs": operator_configs
                                },
                            }
                        }
                    )

    def test_normalized_speaker_and_source_collisions_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            speaker_profiles.normalize_speaker_catalog(
                {"speakers": {"Mains": {}, "mains": {}}}
            )
        with self.assertRaises(ValueError):
            speaker_profiles.normalize_speaker_catalog(
                {
                    "speakers": {
                        "mains": {},
                        "vintage": {
                            "operator_configs": {
                                "Streamer": "first.yml",
                                "streamer": "second.yml",
                            }
                        },
                    }
                }
            )


class SpeakerCatalogLoadTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ["SPEAKER_CATALOG_PATH"] = HERMETIC_CATALOG_PATH
        importlib.reload(speaker_profiles)

    def test_missing_catalog_keeps_builtins(self) -> None:
        _reload_with_catalog("/does/not/exist/speaker-catalog.json")
        self.assertEqual(speaker_profiles.DEFAULT_SPEAKER_ID, "kantarellen")
        self.assertEqual(speaker_profiles.LEGACY_SPEAKER_ID, "kantarellen")
        self.assertIn("partymeh", speaker_profiles.BUILTIN_SPEAKERS)

    def test_catalog_default_becomes_the_legacy_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "speaker-catalog.json")
            path.write_text(
                json.dumps(
                    {
                        "default": "mains",
                        "speakers": {
                            "mains": {"label": "Mains"},
                            "vintage": {
                                "operator_configs": {
                                    "streamer": "vintage-streamer.yml"
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            _reload_with_catalog(str(path))
            self.assertEqual(speaker_profiles.DEFAULT_SPEAKER_ID, "mains")
            self.assertEqual(speaker_profiles.LEGACY_SPEAKER_ID, "mains")
            self.assertEqual(
                set(speaker_profiles.BUILTIN_SPEAKERS), {"mains", "vintage"}
            )
            self.assertEqual(
                speaker_profiles.operator_config_for_source("vintage", "streamer"),
                "vintage-streamer.yml",
            )
            selection = speaker_profiles.default_speaker_selection()
            self.assertEqual(selection["selected"], "mains")

    def test_invalid_catalog_warns_and_keeps_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "speaker-catalog.json")
            path.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                _reload_with_catalog(str(path))
            self.assertIn("using built-ins", output.getvalue())
            self.assertEqual(speaker_profiles.DEFAULT_SPEAKER_ID, "kantarellen")
            self.assertIn("kantarellen", speaker_profiles.BUILTIN_SPEAKERS)


if __name__ == "__main__":
    unittest.main()
