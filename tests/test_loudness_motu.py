"""ISO 226 loudness follows the MOTU main output as well as the fader."""

from __future__ import annotations

import contextlib
import copy
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import audio_eq
import source_volume
from scripts import source_switcher as switcher
from test_source_volume import FakeMotu, _client


def loudness(**overrides) -> dict:
    state = audio_eq.default_audio_state()
    state["loudness"].update({"enabled": True, **overrides})
    return audio_eq.normalize_audio_state(state)


def reference_level(state: dict, motu_db: float | None) -> float:
    config = {"filters": {}, "pipeline": [], "devices": {"playback": {"channels": 2}}}
    updated, _ = audio_eq.apply_audio_overlay(config, state, motu_db=motu_db)
    return updated["filters"]["cdsp_ui_eq_iso226"]["parameters"]["reference_level"]


def test_a_louder_motu_moves_the_reference_down_by_the_same_amount() -> None:
    state = loudness(reference_volume_db=-10, reference_motu_db=-6)
    assert reference_level(state, -6) == -10  # as calibrated
    assert reference_level(state, 0) == -16  # 6 dB louder in the room
    assert reference_level(state, -20) == 4  # 14 dB quieter


def test_the_listening_level_the_engine_derives_includes_the_motu() -> None:
    """The engine's own formula: phon = reference_phon + fader - reference_level."""
    state = loudness(reference_phon=80, reference_volume_db=-10, reference_motu_db=-6)
    for fader, motu in ((-10, -6), (-30, -6), (-30, 0), (-24, -12)):
        phon = 80 + fader - reference_level(state, motu)
        assert phon == 80 + (fader - -10) + (motu - -6)


def test_without_a_calibrated_motu_level_nothing_changes() -> None:
    state = loudness(reference_volume_db=-10)
    assert state["loudness"]["reference_motu_db"] is None
    assert reference_level(state, 0) == -10
    calibrated = loudness(reference_volume_db=-10, reference_motu_db=-6)
    assert reference_level(calibrated, None) == -10  # MOTU level unknown


def test_the_reference_stays_inside_what_the_engine_accepts() -> None:
    state = loudness(reference_volume_db=-60, reference_motu_db=-100)
    assert reference_level(state, 0) == -100
    state = loudness(reference_volume_db=0, reference_motu_db=0)
    assert reference_level(state, -100) == 20


@pytest.mark.parametrize("bad", [5, -101, "loud", True, float("nan")])
def test_a_bad_calibrated_motu_level_is_refused(bad) -> None:
    state = audio_eq.default_audio_state()
    state["loudness"]["reference_motu_db"] = bad
    with pytest.raises(ValueError):
        audio_eq.normalize_audio_state(state)


def test_older_state_without_the_field_still_loads() -> None:
    state = audio_eq.default_audio_state()
    del state["loudness"]["reference_motu_db"]
    assert audio_eq.normalize_audio_state(state)["loudness"]["reference_motu_db"] is None


# --------------------------------------------------------------- switcher


def test_the_loudness_level_follows_the_connection_and_survives_a_disconnect() -> None:
    motu = SimpleNamespace(ws=object(), trim=6)
    with patch.object(switcher, "_motu", motu):
        assert switcher.motu_loudness_db() == -6.0
        motu.trim = 0  # our own write, not yet pushed back, still counts
        assert switcher.motu_loudness_db() == 0.0
        motu.ws, motu.trim = None, None  # disconnected: keep the last level
        assert switcher.motu_loudness_db() == 0.0
    with patch.object(switcher, "_motu", None):
        assert switcher.motu_loudness_db() == 0.0


def test_the_overlay_is_reapplied_when_the_motu_moves(tmp_path: Path) -> None:
    state = loudness(reference_volume_db=-10, reference_motu_db=-6)
    config = {"filters": {}, "pipeline": [], "devices": {"playback": {"channels": 2}}}
    submitted = []
    client = SimpleNamespace(config=SimpleNamespace(active=lambda: copy.deepcopy(config)))
    motu = SimpleNamespace(ws=object(), trim=0)
    with (
        patch.object(switcher, "_motu", motu),
        patch.object(switcher, "iso226_capability_available", return_value=True),
        patch.object(switcher, "_submit_audio_overlay", side_effect=lambda _c, u: submitted.append(u)),
        patch.object(switcher, "_write_audio_eq_status"),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        switcher.ensure_audio_eq(client, speaker_id=switcher.DEFAULT_SPEAKER_ID, state=state)
    params = submitted[-1]["filters"]["cdsp_ui_eq_iso226"]["parameters"]
    assert params["reference_level"] == -16


def test_a_switch_restores_the_motu_before_the_loudness_overlay(tmp_path: Path) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-25, motu_db=0)
    motu = FakeMotu(level=-6)
    order = []
    original = motu.restore_level

    def restore(volume_db, *, undo=False):
        order.append(("motu", volume_db))
        return original(volume_db, undo=undo)

    motu.restore_level = restore
    client = _client(tmp_path, -40)
    with patch.object(
        switcher, "ensure_audio_eq", side_effect=lambda *a, **k: order.append(("eq",))
    ):
        # _switch patches ensure_audio_eq itself; patch it again underneath.
        with patch.object(switcher, "validate_config_file"):
            _switch_keeping_eq(tmp_path, client, motu, order)
    assert order.index(("motu", 0.0)) < order.index(("eq",))


def _switch_keeping_eq(tmp_path, client, motu, order):
    target_path = tmp_path / "toslink.yml"
    target = {
        "speaker": "kantarellen",
        "source": "toslink",
        "legacy": True,
        "digest": "",
        "volume_limit_db": -3,
    }
    identities = {
        str(tmp_path / "streamer.yml"): ("streamer", "kantarellen"),
        str(target_path): ("toslink", "kantarellen"),
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock")
        )
        stack.enter_context(patch.object(switcher, "_write_speaker_status"))
        stack.enter_context(
            patch.object(switcher, "managed_config_identity", side_effect=identities.get)
        )
        stack.enter_context(patch.object(switcher, "_motu", motu))
        stack.enter_context(patch.object(switcher, "_owns_motu_clock", return_value=False))
        stack.enter_context(patch.object(switcher.time, "sleep"))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        switcher.apply_config(client, str(target_path), target=target, remember_volumes=True)


def test_the_calibration_card_offers_the_motu_level() -> None:
    import web_ui

    page = web_ui.HTML
    assert 'id="loudnessMotu"' in page and 'placeholder="not included"' in page
    assert 'id="loudnessUseCurrent"' in page
    assert 'audioState.loudness.reference_motu_db = raw === "" ? null : Number(raw);' in page


def test_the_engine_patch_crossfades_parameter_changes() -> None:
    patch_text = (
        Path(__file__).resolve().parents[1]
        / "camilladsp-iso226"
        / "camilladsp-v4.1.3-iso226.patch"
    ).read_text()
    assert "+            self.redesign_pending = true;" in patch_text
    assert "+    fn parameter_change_is_crossfaded() {" in patch_text
