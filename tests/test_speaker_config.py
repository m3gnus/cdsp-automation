"""Speaker profile schema, compiler output contract and generated-config store."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

import audio_eq
import speaker_config
import speaker_profiles
from profile_fixtures import (
    capture_base,
    make_test_speaker_profile,
    partymeh_document,
    seed_profile,
)


def test_speaker_config_compiler_enforces_output_contract_and_eq_order(
    tmp_path: Path,
) -> None:
    source = {
        "devices": {
            "samplerate": 48000,
            "volume_limit": -20,
            "capture": {"type": "Alsa", "device": "hw:in", "channels": 2},
        },
        "filters": {},
        "pipeline": [],
    }
    profile = make_test_speaker_profile()
    state = audio_eq.default_audio_state()
    state["bands"][1]["gain"] = 2
    compiled = speaker_config.compile_profile_config(
        source, profile, state, source_id="streamer"
    )
    assert compiled["devices"]["volume_limit"] == -20
    mixer_index = next(
        index
        for index, step in enumerate(compiled["pipeline"])
        if step["type"] == "Mixer"
    )
    assert compiled["pipeline"][mixer_index - 1]["description"] == audio_eq.PIPELINE_DESCRIPTION
    assert compiled["mixers"]["spk_partymeh_output"]["mapping"][2]["mute"]
    path, digest = speaker_config.write_generated_config(
        tmp_path, compiled, source_id="streamer", profile_id="partymeh"
    )
    second_path, second_digest = speaker_config.write_generated_config(
        tmp_path, compiled, source_id="streamer", profile_id="partymeh"
    )
    assert path == second_path and digest == second_digest
    assert path.is_file()


def test_measurement_profile_bypasses_user_eq_and_invalid_routes_fail() -> None:
    source = {
        "devices": {"capture": {"channels": 2}},
        "filters": {},
        "pipeline": [],
    }
    profile = make_test_speaker_profile(bypass_user_eq=True)
    compiled = speaker_config.compile_profile_config(
        source, profile, audio_eq.default_audio_state(), source_id="streamer"
    )
    assert not any(name.startswith(audio_eq.FILTER_PREFIX) for name in compiled["filters"])

    broken = copy.deepcopy(profile)
    broken["camilladsp"]["mixers"]["spk_partymeh_output"]["mapping"][2]["mute"] = False
    try:
        speaker_config.compile_profile_config(
            source, broken, audio_eq.default_audio_state(), source_id="streamer"
        )
    except ValueError as exc:
        assert "must be muted" in str(exc)
    else:
        raise AssertionError("unsafe unmuted output was accepted")


def test_raw_measurement_contract_rejects_every_filter_layer() -> None:
    raw = {
        "version": 1,
        "id": "measurement",
        "label": "Measurement",
        "enabled": True,
        "supported_sources": ["streamer"],
        "output_channels": 2,
        "active_outputs": [0, 1],
        "muted_outputs": [],
        "output_roles": ["measurement-left", "measurement-right"],
        "max_volume_db": -20,
        "bypass_user_eq": True,
        "raw_measurement": True,
        "capabilities": {"secondary_program": False},
        "camilladsp": {
            "devices": {"playback": {"channels": 2}},
            "filters": {},
            "mixers": {
                "spk_measurement_output": {
                    "channels": {"in": 2, "out": 2},
                    "mapping": [
                        {"dest": 0, "sources": [{"channel": 0}]},
                        {"dest": 1, "sources": [{"channel": 1}]},
                    ],
                }
            },
            "processors": {},
            "pipeline": [{"type": "Mixer", "name": "spk_measurement_output"}],
        },
    }
    profile = speaker_config.normalize_profile(raw)
    # Compatibility contract: a hand-authored capabilities block is accepted
    # but its retired contents must never reach the catalog or the API.
    assert profile["capabilities"] == {}
    try:
        speaker_config.normalize_profile({**raw, "capabilities": "nope"})
    except ValueError as exc:
        assert "capabilities" in str(exc)
    else:
        raise AssertionError("non-object capabilities block accepted")
    source = {"devices": {"capture": {"channels": 2}}, "filters": {}, "pipeline": []}
    compiled = speaker_config.compile_profile_config(
        source, profile, audio_eq.default_audio_state(), source_id="streamer"
    )
    assert compiled["filters"] == {}
    assert [step["type"] for step in compiled["pipeline"]] == ["Mixer"]
    for section, value in (
        ("filters", {"source_filter": {}}),
        ("pipeline", [{"type": "Filter", "channels": [0], "names": ["source_filter"]}]),
    ):
        broken = copy.deepcopy(source)
        broken[section] = value
        try:
            speaker_config.compile_profile_config(
                broken, profile, audio_eq.default_audio_state(), source_id="streamer"
            )
        except ValueError as exc:
            assert "raw Measurement" in str(exc)
        else:
            raise AssertionError(f"raw Measurement accepted source {section}")

    extra_mixer = copy.deepcopy(raw)
    extra_mixer["camilladsp"]["mixers"]["spk_measurement_extra"] = copy.deepcopy(
        extra_mixer["camilladsp"]["mixers"]["spk_measurement_output"]
    )
    extra_mixer["camilladsp"]["pipeline"].insert(
        0, {"type": "Mixer", "name": "spk_measurement_extra"}
    )
    try:
        speaker_config.normalize_profile(extra_mixer)
    except ValueError as exc:
        assert "exactly one output mixer" in str(exc)
    else:
        raise AssertionError("raw Measurement accepted a preprocessing mixer")

    for mutation in ("multi-source", "gain", "inverted"):
        broken = copy.deepcopy(profile)
        sources = broken["camilladsp"]["mixers"]["spk_measurement_output"]["mapping"][0]["sources"]
        if mutation == "multi-source":
            sources.append({"channel": 1})
        else:
            sources[0][mutation] = 0 if mutation == "gain" else False
        try:
            speaker_config.compile_profile_config(
                source, broken, audio_eq.default_audio_state(), source_id="streamer"
            )
        except ValueError as exc:
            assert "raw Measurement" in str(exc)
        else:
            raise AssertionError(f"raw Measurement accepted {mutation}")


def test_speaker_profile_schema_rejects_coercion_and_source_device_override() -> None:
    base = make_test_speaker_profile()
    invalid_values = [
        ("version", 1.5),
        ("output_channels", 4.5),
        ("enabled", "false"),
        ("bypass_user_eq", "true"),
    ]
    for key, value in invalid_values:
        broken = copy.deepcopy(base)
        broken[key] = value
        try:
            speaker_config.normalize_profile(broken)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe coercion accepted for {key}")

    broken = copy.deepcopy(base)
    broken["active_outputs"][0] = 0.5
    try:
        speaker_config.normalize_profile(broken)
    except ValueError as exc:
        assert "integers" in str(exc)
    else:
        raise AssertionError("fractional output route was accepted")

    for missing in ("max_volume_db", "bypass_user_eq", "raw_measurement"):
        broken = copy.deepcopy(base)
        del broken[missing]
        try:
            speaker_config.normalize_profile(broken)
        except ValueError as exc:
            assert "missing speaker profile fields" in str(exc)
        else:
            raise AssertionError(f"missing safety field {missing} was accepted")
    broken = copy.deepcopy(base)
    broken["max_volum_db"] = broken.pop("max_volume_db")
    try:
        speaker_config.normalize_profile(broken)
    except ValueError as exc:
        assert "unsupported speaker profile fields" in str(exc)
    else:
        raise AssertionError("misspelled volume ceiling was accepted")

    broken = copy.deepcopy(base)
    broken["camilladsp"]["devices"]["samplerate"] = 96000
    try:
        speaker_config.normalize_profile(broken)
    except ValueError as exc:
        assert "cannot override source device fields" in str(exc)
    else:
        raise AssertionError("speaker profile overrode source samplerate")


def test_generated_speaker_config_detects_existing_artifact_corruption(
    tmp_path: Path,
) -> None:
    config = {"devices": {"samplerate": 48000}}
    digest = speaker_config.config_digest(config)
    path = tmp_path / digest / "streamer--partymeh.yml"
    path.parent.mkdir(parents=True)
    path.write_text("devices:\n  samplerate: 96000\n")
    try:
        speaker_config.write_generated_config(
            tmp_path, config, source_id="streamer", profile_id="partymeh"
        )
    except ValueError as exc:
        assert "integrity mismatch" in str(exc)
    else:
        raise AssertionError("corrupt immutable generated config was trusted")


def test_partymeh_catalog_requires_every_source_specific_config(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles"
    config_dir = tmp_path / "configs"
    profile_dir.mkdir()
    config_dir.mkdir()
    profile = yaml.safe_dump(partymeh_document(), sort_keys=False)
    (profile_dir / "partymeh.yml").write_text(profile, encoding="utf-8")
    configs = speaker_profiles.operator_configs_for_speaker("partymeh")
    for filename in configs.values():
        (config_dir / filename).write_text("devices: {}\n", encoding="utf-8")

    catalog = speaker_config.profile_catalog(profile_dir, tmp_path / "bases", config_dir)
    assert catalog["partymeh"]["available"] is True
    assert catalog["partymeh"]["operator_config"] == configs

    (config_dir / configs["analog"]).unlink()
    catalog = speaker_config.profile_catalog(profile_dir, tmp_path / "bases", config_dir)
    assert catalog["partymeh"]["available"] is False
    assert configs["analog"] in catalog["partymeh"]["reason"]


def test_generated_config_pruning_is_bounded_and_protects_active(tmp_path: Path) -> None:
    directories: list[Path] = []
    for index in range(4):
        directory = tmp_path / (f"{index:064x}")
        directory.mkdir()
        config = directory / "streamer--profile.yml"
        config.write_text("version: 1\n")
        directories.append(directory)
    ignored = tmp_path / "operator-files"
    ignored.mkdir()
    removed = speaker_config.prune_generated_configs(
        tmp_path,
        protected_paths=(directories[0] / "streamer--profile.yml",),
        retain=2,
    )
    assert removed == 1
    assert directories[0].exists()
    assert ignored.exists()


def test_profile_save_is_cas_guarded_and_round_trips(tmp_path: Path) -> None:
    document = speaker_config.canonical_profile_document(seed_profile("partymeh"))
    saved = speaker_config.save_profile(
        tmp_path, "partymeh", document, expected_revision=0
    )
    assert saved["revision"] == 1
    assert speaker_config.load_profile(tmp_path, "partymeh") == saved

    edited = speaker_config.canonical_profile_document(saved)
    edited["crossover"]["ways"][0]["lowpass"]["freq"] = 320.0
    edited["enabled"] = True
    updated = speaker_config.save_profile(
        tmp_path, "partymeh", edited, expected_revision=1
    )
    assert updated["revision"] == 2
    assert updated["enabled"] is True
    assert updated["crossover"]["ways"][0]["lowpass"]["freq"] == 320.0

    try:
        speaker_config.save_profile(tmp_path, "partymeh", edited, expected_revision=1)
    except ValueError as exc:
        assert "changed elsewhere" in str(exc)
    else:
        raise AssertionError("stale profile revision was accepted")

    try:
        speaker_config.save_profile(tmp_path, "kantarellen", document)
    except ValueError as exc:
        assert "legacy" in str(exc)
    else:
        raise AssertionError("kantarellen accepted a parametric save")

    try:
        speaker_config.save_profile(
            tmp_path, "partymeh", {"camilladsp": {"devices": {}}}
        )
    except ValueError as exc:
        assert "parametric" in str(exc)
    else:
        raise AssertionError("hand-authored fragment accepted through save")


def test_source_base_program_map_remaps_parametric_profiles() -> None:
    profile = seed_profile("partymeh")
    state = audio_eq.default_audio_state()
    base = capture_base(20)
    base["program"] = {"main": [12, 13]}
    config = speaker_config.compile_profile_config(
        base, profile, state, source_id="streamer"
    )
    assert "program" not in config, "program metadata must never reach CamillaDSP"
    expand = config["mixers"]["spk_partymeh_ways"]
    assert expand["channels"]["in"] == 20
    channels = {
        row["dest"]: row["sources"][0]["channel"] for row in expand["mapping"]
    }
    assert channels == {0: 12, 1: 13, 2: 12, 3: 13, 4: 12, 5: 13}

    # Duplicate or out-of-range main channels are rejected, and the retired
    # stereo program key is no longer a valid map entry.
    for bad in ({"main": [12, 12]}, {"main": [19, 25]}, {"main": [2, 3], "stereo": [3, 4]}):
        broken = capture_base(20)
        broken["program"] = bad
        try:
            speaker_config.compile_profile_config(
                broken, profile, state, source_id="toslink"
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad program map accepted: {bad}")

    # Hand-authored fragment profiles must also never leak the metadata key.
    classic = make_test_speaker_profile()
    base2 = capture_base(2)
    base2["program"] = {"main": [0, 1]}
    config = speaker_config.compile_profile_config(
        base2, classic, state, source_id="streamer"
    )
    assert "program" not in config
