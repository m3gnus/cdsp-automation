"""Parametric crossover expansion, acoustic sum and geometry guards."""

from __future__ import annotations

import copy
import math
from pathlib import Path

import yaml

import audio_eq
import speaker_config
import speaker_xo
from profile_fixtures import capture_base, partymeh_document, seed_profile


def test_parametric_crossover_compiles_flat_and_widens_program() -> None:
    profile = seed_profile("partymeh")
    assert profile["active_outputs"] == [0, 1, 2, 3, 4, 5]
    state = audio_eq.default_audio_state()
    for source, channels in (("streamer", 4),):
        config = speaker_config.compile_profile_config(
            capture_base(channels), profile, state, source_id=source
        )
        assert config["mixers"]["spk_partymeh_ways"]["channels"]["in"] == channels
        assert config["pipeline"][-1] == {
            "type": "Mixer",
            "name": "spk_partymeh_output",
        }
        assert config["devices"]["volume_limit"] == -10.0

    # The acoustic sum of the LR24 ways must stay flat through both
    # crossover points; each way alone sits 6 dB down at its corner.
    response = speaker_xo.crossover_response(
        profile["crossover"], [100.0, 300.0, 1000.0, 3000.0, 10000.0]
    )
    assert all(abs(level) < 0.35 for level in response["sum"]["main"])
    by_name = {way["name"]: way["db"] for way in response["ways"]}
    assert math.isclose(by_name["low"][1], -6.02, abs_tol=0.1)
    assert math.isclose(by_name["high"][3], -6.02, abs_tol=0.35)


def test_parametric_crossover_places_trims_and_delay() -> None:
    document = {
        "version": 1,
        "id": "partymeh",
        "enabled": True,
        "supported_sources": ["streamer"],
        "max_volume_db": -20,
        "bypass_user_eq": False,
        "raw_measurement": False,
        "crossover": {
            "version": 1,
            "playback": {
                "type": "Alsa",
                "device": "hw:test",
                "channels": 8,
                "format": "S32_LE",
            },
            "ways": [
                {
                    "name": "low",
                    "lowpass": {"freq": 300, "slope": "LR24"},
                    "outputs": [4, 5],
                },
                {
                    "name": "high",
                    "highpass": {"freq": 300, "slope": "LR24"},
                    "gain_db": -2.5,
                    "delay_ms": 0.3,
                    "invert": True,
                    "outputs": [0, 1],
                },
            ],
        },
    }
    profile = speaker_config.normalize_profile(document)
    fragment = profile["camilladsp"]
    assert profile["capabilities"] == {}

    route = fragment["mixers"]["spk_partymeh_output"]["mapping"]
    high_left = next(row for row in route if row["dest"] == 0)
    assert high_left["sources"] == [{"channel": 2, "gain": -2.5, "inverted": True}]
    low_left = next(row for row in route if row["dest"] == 4)
    assert low_left["sources"] == [{"channel": 0}]

    # Every way is fed by the two-channel main program.
    expand_mixer = fragment["mixers"]["spk_partymeh_ways"]
    assert expand_mixer["channels"] == {"in": 2, "out": 4}
    high_left_bus = next(row for row in expand_mixer["mapping"] if row["dest"] == 2)
    assert high_left_bus["sources"] == [{"channel": 0}]

    delay = fragment["filters"]["spk_partymeh_high_delay"]
    assert delay["parameters"] == {"delay": 0.3, "unit": "ms", "subsample": False}
    filtered_buses = {
        step["channels"][0]
        for step in fragment["pipeline"]
        if step["type"] == "Filter"
    }
    assert filtered_buses == {0, 1, 2, 3}


def test_persisted_program_channels_key_still_normalizes_and_loads(
    tmp_path: Path,
) -> None:
    """Profiles saved before the stereo program retired carry the key."""
    document = partymeh_document()
    document["crossover"]["program_channels"] = 2
    normalized = speaker_xo.normalize_crossover(copy.deepcopy(document["crossover"]))
    assert "program_channels" not in normalized

    (tmp_path / "partymeh.yml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    profile = speaker_config.load_profile(tmp_path, "partymeh")
    assert profile["active_outputs"] == [0, 1, 2, 3, 4, 5]
    assert "program_channels" not in profile["crossover"]

    # The retired four-channel dual-program width is no longer valid.
    document["crossover"]["program_channels"] = 4
    try:
        speaker_xo.normalize_crossover(document["crossover"])
    except ValueError as exc:
        assert "program_channels" in str(exc)
    else:
        raise AssertionError("retired four-channel program width was accepted")


def test_parametric_crossover_rejects_unsafe_geometry() -> None:
    def spec(**overrides):
        base = {
            "version": 1,
            "playback": {"device": "hw:test", "channels": 8},
            "ways": [
                {"name": "low", "lowpass": {"freq": 300}, "outputs": [4, 5]},
                {"name": "high", "highpass": {"freq": 300}, "outputs": [0, 1]},
            ],
        }
        base.update(overrides)
        return base

    failures = [
        spec(ways=[{"name": "a", "outputs": [0, 1]}, {"name": "b", "outputs": [1, 2]}]),
        spec(ways=[{"name": "a", "outputs": [3, 3]}]),
        spec(ways=[{"name": "a", "source": "stereo", "outputs": [0, 1]}]),
        spec(ways=[{
            "name": "a",
            "highpass": {"freq": 3000},
            "lowpass": {"freq": 300},
            "outputs": [0, 1],
        }]),
        spec(ways=[{"name": "a", "lowpass": {"freq": 300, "slope": "LR36"}, "outputs": [0, 1]}]),
        # Wide enough that only the way-count limit can reject this one.
        spec(
            playback={"device": "hw:test", "channels": 10},
            ways=[{"name": f"w{i}", "outputs": [2 * i, 2 * i + 1]} for i in range(5)],
        ),
        spec(ways=[{"name": "a", "outputs": [0, 1], "buzz": 1}]),
    ]
    for broken in failures:
        try:
            speaker_xo.normalize_crossover(broken)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe crossover accepted: {broken['ways']}")

    for tweak in (
        {"lowpass": {"freq": 300}},
        {"gain_db": -3},
        {"delay_ms": 1},
        {"invert": True},
    ):
        way = {"name": "direct", "outputs": [6, 7], **tweak}
        raw = spec(ways=[way])
        try:
            speaker_xo.normalize_crossover(raw, raw_measurement=True)
        except ValueError as exc:
            assert "full-range and unity" in str(exc)
        else:
            raise AssertionError(f"raw measurement accepted {tweak}")

    document = {
        "version": 1,
        "id": "partymeh",
        "enabled": False,
        "supported_sources": ["toslink"],
        "max_volume_db": -20,
        "bypass_user_eq": False,
        "raw_measurement": False,
        "crossover": spec(),
    }
    conflicting = copy.deepcopy(document)
    conflicting["camilladsp"] = {"devices": {}}
    try:
        speaker_xo.expand_crossover_profile(conflicting)
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("crossover and camilladsp were both accepted")
    derived = copy.deepcopy(document)
    derived["active_outputs"] = [0]
    try:
        speaker_xo.expand_crossover_profile(derived)
    except ValueError as exc:
        assert "derive" in str(exc)
    else:
        raise AssertionError("hand-written derived field was accepted")
