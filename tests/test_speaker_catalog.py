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


def _reload_with_catalog(path: str) -> None:
    os.environ["SPEAKER_CATALOG_PATH"] = path
    importlib.reload(speaker_profiles)


class SpeakerCatalogNormalizationTests(unittest.TestCase):
    def test_minimal_catalog_defaults_label_and_first_speaker(self) -> None:
        catalog = speaker_profiles.normalize_speaker_catalog(
            {"speakers": {"mains": {}}}
        )
        self.assertEqual(catalog["default"], "mains")
        self.assertIsNone(catalog["legacy"])
        self.assertEqual(catalog["speakers"]["mains"]["label"], "mains")
        self.assertEqual(catalog["operator_configs"], {})

    def test_operator_configs_accept_string_shorthand(self) -> None:
        catalog = speaker_profiles.normalize_speaker_catalog(
            {
                "default": "mains",
                "speakers": {
                    "mains": {
                        "label": "Mains",
                        "operator_configs": "mains-streamer.yml",
                    }
                },
            }
        )
        self.assertEqual(
            catalog["operator_configs"]["mains"],
            {"streamer": "mains-streamer.yml"},
        )

    def test_unknown_default_and_legacy_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            speaker_profiles.normalize_speaker_catalog(
                {"default": "missing", "speakers": {"mains": {}}}
            )
        with self.assertRaises(ValueError):
            speaker_profiles.normalize_speaker_catalog(
                {"legacy": "missing", "speakers": {"mains": {}}}
            )

    def test_empty_or_malformed_documents_are_rejected(self) -> None:
        for raw in (None, [], {"speakers": {}}, {"speakers": {"x": []}}):
            with self.assertRaises(ValueError):
                speaker_profiles.normalize_speaker_catalog(raw)


class SpeakerCatalogLoadTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("SPEAKER_CATALOG_PATH", None)
        importlib.reload(speaker_profiles)

    def test_missing_catalog_keeps_builtins(self) -> None:
        _reload_with_catalog("/does/not/exist/speaker-catalog.json")
        self.assertEqual(speaker_profiles.DEFAULT_SPEAKER_ID, "kantarellen")
        self.assertEqual(speaker_profiles.LEGACY_SPEAKER_ID, "kantarellen")
        self.assertIn("partymeh", speaker_profiles.BUILTIN_SPEAKERS)

    def test_catalog_file_replaces_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "speaker-catalog.json")
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "default": "mains",
                        "speakers": {
                            "mains": {
                                "label": "Mains",
                                "description": "Two-way monitors",
                                "operator_configs": {
                                    "streamer": "mains-streamer.yml"
                                },
                            },
                            "sub": {"label": "Sub"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            _reload_with_catalog(str(path))
            self.assertEqual(speaker_profiles.DEFAULT_SPEAKER_ID, "mains")
            self.assertIsNone(speaker_profiles.LEGACY_SPEAKER_ID)
            self.assertEqual(
                set(speaker_profiles.BUILTIN_SPEAKERS), {"mains", "sub"}
            )
            self.assertEqual(
                speaker_profiles.operator_config_for_source("mains", "streamer"),
                "mains-streamer.yml",
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
