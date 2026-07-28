"""Speaker selection CAS semantics and per-profile audio-state isolation."""

from __future__ import annotations

import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

import audio_eq
import speaker_profiles
from scripts import source_switcher


def test_speaker_selection_is_revision_safe_and_profile_audio_isolated(
    tmp_path: Path,
) -> None:
    selection_path = tmp_path / "speaker-selection.json"
    first = speaker_profiles.update_speaker_selection(
        selection_path,
        "partymeh",
        expected_revision=0,
        allowed_ids=speaker_profiles.BUILTIN_SPEAKERS,
    )
    assert first == {"version": 1, "revision": 1, "selected": "partymeh"}
    try:
        speaker_profiles.update_speaker_selection(
            selection_path,
            "measurement",
            expected_revision=0,
            allowed_ids=speaker_profiles.BUILTIN_SPEAKERS,
        )
    except ValueError as exc:
        assert "changed elsewhere" in str(exc)
    else:
        raise AssertionError("stale speaker selection overwrite was accepted")

    legacy = tmp_path / "audio-eq.json"
    state = audio_eq.default_audio_state()
    state["bands"][0]["gain"] = 2.5
    audio_eq.atomic_write_json(legacy, state)
    audio_root = tmp_path / "speaker-audio"
    migrated = speaker_profiles.read_profile_audio_state(
        audio_root, "kantarellen", legacy_path=legacy
    )
    party = speaker_profiles.read_profile_audio_state(
        audio_root, "partymeh", legacy_path=legacy
    )
    assert migrated["bands"][0]["gain"] == 2.5
    assert party["bands"][0]["gain"] == 0.0
    assert legacy.exists()
    assert not (audio_root / "kantarellen.json").exists()


def test_speaker_selection_rejects_unknown_and_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "speaker-selection.json"
    for payload, message in (
        ({"version": 1, "revision": 0, "selected": "unknown"}, "unknown"),
        ({"version": 99, "revision": 0, "selected": "kantarellen"}, "version"),
    ):
        audio_eq.atomic_write_json(path, payload)
        try:
            speaker_profiles.read_speaker_selection(path)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"invalid selection accepted: {payload}")


def test_concurrent_speaker_selection_compare_and_swap(tmp_path: Path) -> None:
    path = tmp_path / "speaker-selection.json"

    def select(speaker_id: str) -> str:
        try:
            speaker_profiles.update_speaker_selection(
                path, speaker_id, expected_revision=0
            )
            return "saved"
        except ValueError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(select, ("partymeh", "measurement")))
    assert sorted(results) == ["saved", "stale"]
    assert speaker_profiles.read_speaker_selection(path)["revision"] == 1


def test_forced_selection_touch_and_status_revision_signal(tmp_path: Path) -> None:
    selection_path = tmp_path / "speaker-selection.json"
    first = speaker_profiles.update_speaker_selection(selection_path, "partymeh")
    assert first["revision"] == 1
    unchanged = speaker_profiles.update_speaker_selection(selection_path, "partymeh")
    assert unchanged["revision"] == 1

    commits: list[dict] = []
    forced = speaker_profiles.update_speaker_selection(
        selection_path,
        "partymeh",
        expected_revision=1,
        force=True,
        before_commit=commits.append,
    )
    assert forced["revision"] == 2
    assert commits and commits[0]["selected"] == "partymeh"

    switcher = source_switcher
    status_path = tmp_path / "speaker-profile-status.json"
    with patch.object(switcher, "SPEAKER_STATUS_PATH", status_path):
        assert switcher._speaker_status_revision() is None
        status_path.write_text(json.dumps({"ok": True, "selection_revision": 2}))
        assert switcher._speaker_status_revision() == 2
        status_path.write_text(json.dumps({"ok": False, "selection_revision": 3}))
        assert switcher._speaker_status_revision() is None
        status_path.write_text(json.dumps({"ok": True, "selection_revision": True}))
        assert switcher._speaker_status_revision() is None
