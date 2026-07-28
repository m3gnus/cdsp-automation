"""EQ overlay math, persisted audio-state validation and loudness engine pins."""

from __future__ import annotations

from pathlib import Path

import audio_eq


REPOSITORY = Path(__file__).resolve().parents[1]


def test_audio_eq_overlay_is_idempotent_and_precedes_crossover() -> None:
    config = {
        "devices": {"capture": {"channels": 2}},
        "filters": {"room": {"type": "Gain", "parameters": {"gain": -1}}},
        "pipeline": [
            {"type": "Filter", "channels": [0, 1], "names": ["room"]},
            {"type": "Mixer", "name": "stereo_to_active"},
        ],
    }
    state = audio_eq.default_audio_state()
    state["revision"] = 3
    state["bands"][0]["gain"] = 4
    state["bands"][1]["gain"] = 2
    updated, preamp = audio_eq.apply_audio_overlay(config, state)

    assert preamp == -6
    mixer_index = next(
        i for i, step in enumerate(updated["pipeline"]) if step["type"] == "Mixer"
    )
    eq_step = updated["pipeline"][mixer_index - 1]
    assert eq_step["description"] == audio_eq.PIPELINE_DESCRIPTION
    assert eq_step["channels"] == [0, 1]
    assert updated["filters"]["uglan_ui_eq_preamp"]["parameters"]["gain"] == -6

    reapplied, second_preamp = audio_eq.apply_audio_overlay(updated, state)
    assert reapplied == updated
    assert second_preamp == preamp
    source_switcher = (REPOSITORY / "scripts" / "source_switcher.py").read_text()
    assert "_audio_overlay_matches(accepted, updated)" in source_switcher
    assert 'for key in ("inverted", "mute")' in source_switcher


def test_audio_eq_bypass_and_validation_are_safe() -> None:
    state = audio_eq.default_audio_state()
    state["bands"][2]["enabled"] = False
    config = {"devices": {"capture": {"channels": 2}}, "filters": {}, "pipeline": []}
    updated, _ = audio_eq.apply_audio_overlay(config, state)
    names = updated["pipeline"][0]["names"]
    assert not any("_03_mid" in name for name in names)

    state["bands"][0]["gain"] = 30
    try:
        audio_eq.normalize_audio_state(state)
    except ValueError as exc:
        assert "gain" in str(exc)
    else:
        raise AssertionError("unsafe EQ gain was accepted")

    state = audio_eq.default_audio_state()
    state["loudness"]["enabled"] = True
    normalized = audio_eq.normalize_audio_state(state)
    updated, _ = audio_eq.apply_audio_overlay(config, normalized)
    iso = updated["filters"]["uglan_ui_eq_iso226"]
    assert iso["type"] == "Iso226"
    assert iso["parameters"]["fader"] == "Main"


def test_audio_overlay_removes_legacy_loudness_and_tone_filters() -> None:
    config = {
        "devices": {"capture": {"channels": 2}},
        "filters": {
            "loudness": {"type": "Loudness", "parameters": {}},
            "other_iso": {"type": "Iso226", "parameters": {}},
            "Bass": {"type": "Biquad", "parameters": {}},
            "Treble": {"type": "Biquad", "parameters": {}},
            "room": {"type": "Biquad", "parameters": {}},
        },
        "pipeline": [
            {
                "type": "Filter",
                "channels": [0, 1],
                "names": ["loudness", "other_iso", "Bass", "Treble", "room"],
            }
        ],
    }
    updated, _ = audio_eq.apply_audio_overlay(config, audio_eq.default_audio_state())
    assert set(updated["filters"]) == {
        "room",
        "uglan_ui_eq_01_low",
        "uglan_ui_eq_02_low_mid",
        "uglan_ui_eq_03_mid",
        "uglan_ui_eq_04_high_mid",
        "uglan_ui_eq_05_high",
    }
    room_step = next(step for step in updated["pipeline"] if "room" in step.get("names", []))
    assert room_step["names"] == ["room"]


def test_remote_tone_updates_persistent_overlay_atomically(tmp_path: Path) -> None:
    state_path = tmp_path / "audio-eq.json"
    audio_eq.atomic_write_json(state_path, audio_eq.default_audio_state())
    first = audio_eq.update_tone_band(state_path, "low", 0.5)
    second = audio_eq.update_tone_band(state_path, "high", -1.0)
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["bands"][0]["gain"] == 0.5
    assert second["bands"][-1]["gain"] == -1.0
    reset = audio_eq.reset_tone_bands(state_path)
    assert reset["bands"][0]["gain"] == 0.0


def test_tone_band_identity_limits_and_deployment_permissions() -> None:
    state = audio_eq.default_audio_state()
    state["bands"][0]["type"] = "Peaking"
    try:
        audio_eq.normalize_audio_state(state)
    except ValueError as exc:
        assert "reserved tone bands" in str(exc)
    else:
        raise AssertionError("reserved Bass identity could be changed")

    installer = (REPOSITORY / "install.sh").read_text()
    assert 'install -d -m 0750 -o "$INSTALL_USER" -g "$INSTALL_USER"' in installer
    update_body = installer.split("update_utilities()", 1)[1].split(
        "pair_bluetooth_remote()", 1
    )[0]
    assert "ensure_audio_state_storage" in update_body


def test_iso226_patch_is_pinned_tested_and_fader_linked() -> None:
    build = (REPOSITORY / "scripts" / "build_camilladsp_iso226.sh").read_text()
    patch_text = (
        REPOSITORY / "camilladsp-iso226" / "camilladsp-v4.1.3-iso226.patch"
    ).read_text()
    assert "05e9cfcdf43c0dfe078ed3feb8af4c8bd701fd74" in build
    assert (
        "cargo test" in build
        and 'git -C "$BUILD_DIR/camilladsp" apply --check' in build
    )
    assert "rollback" in build and 'readlink -f "/proc/$pid/exe"' in build
    assert '"$CANDIDATE" --check "$config"' in build
    assert "processing_params.current_volume" in patch_text
    assert "reference_is_flat_and_quiet_listening_boosts_bass" in patch_text
    assert "available_headroom" in patch_text
    assert "update_filters" in patch_text and "filter.update_parameters" in patch_text
    assert "binary_sha256" in (REPOSITORY / "scripts" / "web_ui.py").read_text()
    assert (
        "binary_sha256"
        in (REPOSITORY / "scripts" / "source_switcher.py").read_text()
    )


def test_expanded_eq_types_are_validated_and_gainless_filters_omit_gain() -> None:
    state = audio_eq.default_audio_state()
    custom_types = ["Lowpass", "Highpass", "Bandpass", "Notch"]
    for index, filter_type in enumerate(custom_types):
        state["bands"].insert(
            -1,
            {
                "id": f"custom_{index}",
                "enabled": True,
                "type": filter_type,
                "freq": 300.0 * (index + 1),
                "gain": 18.0,
                "q": 12.0,
            },
        )
    normalized = audio_eq.normalize_audio_state(state)
    assert len(normalized["bands"]) == 9
    assert all(
        band["gain"] == 0
        for band in normalized["bands"]
        if band["type"] in custom_types
    )
    config = {
        "devices": {"capture": {"channels": 2}},
        "filters": {},
        "pipeline": [],
    }
    applied, _preamp = audio_eq.apply_audio_overlay(config, normalized)
    custom = [
        spec["parameters"]
        for name, spec in applied["filters"].items()
        if name.startswith(audio_eq.FILTER_PREFIX)
        and spec.get("parameters", {}).get("type") in custom_types
    ]
    assert len(custom) == 4
    assert all("gain" not in parameters for parameters in custom)


def test_eq_band_limit_is_sixteen() -> None:
    state = audio_eq.default_audio_state()
    while len(state["bands"]) < 16:
        index = len(state["bands"])
        state["bands"].insert(
            -1,
            {
                "id": f"extra_{index}",
                "enabled": True,
                "type": "Peaking",
                "freq": 1000,
                "gain": 0,
                "q": 1,
            },
        )
    assert len(audio_eq.normalize_audio_state(state)["bands"]) == 16
    state["bands"].insert(-1, {**state["bands"][1], "id": "too_many"})
    try:
        audio_eq.normalize_audio_state(state)
    except ValueError as exc:
        assert "between 1 and 16" in str(exc)
    else:
        raise AssertionError("17 EQ bands were accepted")


def test_audio_state_strict_booleans_versions_and_headroom_range() -> None:
    for mutate in (
        lambda state: state.update(enabled="false"),
        lambda state: state["bands"][0].update(enabled="false"),
        lambda state: state["loudness"].update(enabled="false"),
    ):
        state = audio_eq.default_audio_state()
        mutate(state)
        try:
            audio_eq.normalize_audio_state(state)
        except ValueError as exc:
            assert "true or false" in str(exc)
        else:
            raise AssertionError("string boolean was accepted")

    for version in (1, 2):
        state = audio_eq.default_audio_state()
        state["version"] = version
        assert audio_eq.normalize_audio_state(state)["version"] == audio_eq.STATE_VERSION
    state["version"] = audio_eq.STATE_VERSION + 1
    try:
        audio_eq.normalize_audio_state(state)
    except ValueError as exc:
        assert "unsupported audio state version" in str(exc)
    else:
        raise AssertionError("future audio state schema was accepted")

    state = audio_eq.default_audio_state()
    while len(state["bands"]) < 16:
        index = len(state["bands"])
        state["bands"].insert(-1, {
            "id": f"boost_{index}", "enabled": True, "type": "Peaking",
            "freq": 1000, "gain": 24, "q": 1,
        })
    try:
        audio_eq.normalize_audio_state(state)
    except ValueError as exc:
        assert "150 dB gain range" in str(exc)
    else:
        raise AssertionError("undeployable automatic headroom was accepted")

    # State files persisted before the secondary stereo program was retired
    # still carry a "stereo" object; it must load and be dropped on rewrite.
    legacy = audio_eq.default_audio_state()
    legacy["stereo"] = {
        "enabled": True,
        "auto_headroom": True,
        "preamp_db": 0.0,
        "trim_db": -12.0,
        "muted": False,
        "bands": [{"id": "low"}],
    }
    normalized = audio_eq.normalize_audio_state(legacy)
    assert "stereo" not in normalized


def test_audio_state_reports_the_installed_unity_linear_airplay_path() -> None:
    state = audio_eq.default_audio_state()
    assert state["volume"] == {
        "master": "camilladsp",
        "airplay_unity_bridge": True,
        "airplay_mapping": "linear",
    }
    state["volume"] = {
        "master": "camilladsp",
        "airplay_unity_bridge": False,
        "airplay_mapping": "perceptual",
    }
    assert audio_eq.normalize_audio_state(state)["volume"] == {
        "master": "camilladsp",
        "airplay_unity_bridge": True,
        "airplay_mapping": "linear",
    }
