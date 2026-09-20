"""Speaker selection CAS semantics and per-profile audio-state isolation."""

from __future__ import annotations

import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
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


class FakeEngineConfig:
    """The config command group of one live CamillaDSP instance."""

    def __init__(
        self, description: str | None = None, *, accept_set_value: bool = True
    ) -> None:
        self.live: dict = {"devices": {}}
        if description is not None:
            self.live["description"] = description
        self.accept_set_value = accept_set_value
        self.set_active_calls = 0

    def description(self) -> str | None:
        return self.live.get("description")

    def active(self) -> dict:
        return self.live

    def set_active(self, value: dict) -> None:
        self.set_active_calls += 1
        self.live = value

    def set_value(self, pointer: str, value: object) -> None:
        if not self.accept_set_value:
            raise RuntimeError("SetConfigValue is not supported")
        assert pointer == "/description"
        self.live["description"] = value


def engine_client(config: object) -> SimpleNamespace:
    return SimpleNamespace(config=config)


def test_engine_generation_marker_round_trips_and_keeps_the_description() -> None:
    generation = speaker_profiles.new_engine_generation()
    assert len(generation) == 32
    base = "Generated source=streamer speaker=partymeh version=3"
    marked = speaker_profiles.description_with_marker(base, generation)
    assert base in marked
    assert speaker_profiles.engine_generation_from_description(marked) == generation
    assert speaker_profiles.description_without_marker(marked) == base

    # Re-stamping replaces the previous generation instead of accumulating.
    second = speaker_profiles.new_engine_generation()
    restamped = speaker_profiles.description_with_marker(marked, second)
    assert speaker_profiles.engine_generation_from_description(restamped) == second
    assert speaker_profiles.description_without_marker(restamped) == base

    for absent in (None, "", base, f"{speaker_profiles.ENGINE_MARKER_PREFIX}nothex", 7):
        assert speaker_profiles.engine_generation_from_description(absent) is None


def test_stamping_falls_back_to_set_active_and_fails_when_it_is_dropped() -> None:
    generation = speaker_profiles.new_engine_generation()
    config = FakeEngineConfig("Operator config")
    speaker_profiles.stamp_engine_generation(engine_client(config), generation)
    assert config.set_active_calls == 0
    assert speaker_profiles.live_engine_generation(engine_client(config)) == generation

    # An engine or client library without SetConfigValue, and one whose
    # SetConfigValue is silently ignored, both fall back to a whole-config write.
    legacy = FakeEngineConfig("Operator config", accept_set_value=False)
    speaker_profiles.stamp_engine_generation(engine_client(legacy), generation)
    assert legacy.set_active_calls == 1
    assert speaker_profiles.live_engine_generation(engine_client(legacy)) == generation

    class Ignoring(FakeEngineConfig):
        def set_value(self, pointer: str, value: object) -> None:
            pass

    ignoring = Ignoring("Operator config")
    speaker_profiles.stamp_engine_generation(engine_client(ignoring), generation)
    assert ignoring.set_active_calls == 1
    assert (
        speaker_profiles.live_engine_generation(engine_client(ignoring)) == generation
    )

    class Amnesiac(Ignoring):
        def set_active(self, value: dict) -> None:
            self.set_active_calls += 1

    try:
        speaker_profiles.stamp_engine_generation(
            engine_client(Amnesiac("Operator config")), generation
        )
    except RuntimeError as exc:
        assert "did not retain" in str(exc)
    else:
        raise AssertionError("an engine that dropped the marker was called ready")


def test_malformed_or_foreign_ready_tokens_never_authorize_unmute(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready.json"
    generation = speaker_profiles.new_engine_generation()
    client = engine_client(
        FakeEngineConfig(speaker_profiles.engine_generation_marker(generation))
    )

    rejected = (
        "",
        "   ",
        '{"version": 2, "engine_generation": "',
        '{"ready": true}',
        "[]",
        "null",
        json.dumps({"version": 1, "engine_generation": generation}),
        json.dumps({"version": 2}),
        json.dumps({"version": 2, "engine_generation": ""}),
        json.dumps({"version": 2, "engine_generation": "not-a-generation"}),
        json.dumps({"version": 2, "engine_generation": generation.upper()}),
        json.dumps({"version": 2, "engine_generation": True}),
        # Well formed, but minted for a different engine instance.
        json.dumps(
            {
                "version": 2,
                "engine_generation": speaker_profiles.new_engine_generation(),
            }
        ),
    )
    for content in rejected:
        ready.write_text(content, encoding="utf-8")
        assert speaker_profiles.audio_inhibit_active(ready, client)
        try:
            speaker_profiles.require_audio_unmute_allowed(ready, client)
        except RuntimeError as exc:
            assert "inhibited" in str(exc)
        else:
            raise AssertionError(f"a bad ready token authorized unmute: {content!r}")

    ready.unlink()
    assert speaker_profiles.audio_inhibit_active(ready, client)

    # The well-formed token for this very engine instance still works.
    speaker_profiles.clear_audio_inhibit(
        ready,
        generation=generation,
        applied={"speaker": "partymeh", "source": "streamer"},
    )
    speaker_profiles.require_audio_unmute_allowed(ready, client)
    token = json.loads(ready.read_text(encoding="utf-8"))
    assert token["version"] == speaker_profiles.AUDIO_READY_VERSION
    assert token["engine_generation"] == generation
    assert token["speaker"] == "partymeh"

    try:
        speaker_profiles.clear_audio_inhibit(ready, generation="")
    except ValueError as exc:
        assert "engine generation" in str(exc)
    else:
        raise AssertionError("a ready token was written without a generation")


def test_unreachable_engine_inhibits_rather_than_authorizing(tmp_path: Path) -> None:
    """A token cannot outlive the connection that is supposed to confirm it."""
    ready = tmp_path / "ready.json"
    speaker_profiles.clear_audio_inhibit(
        ready, generation=speaker_profiles.new_engine_generation()
    )

    class Broken:
        def description(self) -> str:
            raise OSError("websocket closed")

    assert speaker_profiles.live_engine_generation(engine_client(Broken())) is None
    assert speaker_profiles.audio_inhibit_active(ready, engine_client(Broken()))
