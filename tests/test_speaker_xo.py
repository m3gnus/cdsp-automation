"""Parametric crossover expansion, acoustic sum and geometry guards."""

from __future__ import annotations

import copy
import math

import audio_eq
import speaker_config
import speaker_xo
from profile_fixtures import capture_base, seed_profile


def test_parametric_crossover_compiles_flat_and_widens_program() -> None:
    profile = seed_profile("partymeh")
    assert profile["active_outputs"] == [0, 1, 2, 3, 4, 5]
    assert profile["capabilities"]["meter_bands"] == {
        "low": [4, 5],
        "mid": [2, 3],
        "high": [0, 1],
    }
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

    # The four-channel dual profile refuses a two-channel program.
    dual = seed_profile("partymeh_bird")
    try:
        speaker_config.compile_profile_config(
            capture_base(2), dual, state, source_id="streamer"
        )
    except ValueError as exc:
        assert "4-channel" in str(exc)
    else:
        raise AssertionError("dual profile accepted a two-channel program")


def test_parametric_crossover_places_trims_delay_and_stereo_program() -> None:
    document = {
        "version": 1,
        "id": "partymeh_bird",
        "enabled": True,
        "supported_sources": ["streamer"],
        "max_volume_db": -20,
        "bypass_user_eq": False,
        "raw_measurement": False,
        "crossover": {
            "version": 1,
            "program_channels": 4,
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
                {"name": "bird", "source": "stereo", "outputs": [6, 7]},
            ],
        },
    }
    profile = speaker_config.normalize_profile(document)
    fragment = profile["camilladsp"]
    assert profile["capabilities"]["secondary_program"] is True

    route = fragment["mixers"]["spk_partymeh_bird_output"]["mapping"]
    high_left = next(row for row in route if row["dest"] == 0)
    assert high_left["sources"] == [{"channel": 2, "gain": -2.5, "inverted": True}]
    low_left = next(row for row in route if row["dest"] == 4)
    assert low_left["sources"] == [{"channel": 0}]

    expand = fragment["mixers"]["spk_partymeh_bird_ways"]["mapping"]
    bird_left = next(row for row in expand if row["dest"] == 4)
    assert bird_left["sources"] == [{"channel": 2}]

    delay = fragment["filters"]["spk_partymeh_bird_high_delay"]
    assert delay["parameters"] == {"delay": 0.3, "unit": "ms", "subsample": False}
    filtered_buses = {
        step["channels"][0]
        for step in fragment["pipeline"]
        if step["type"] == "Filter"
    }
    assert filtered_buses == {0, 1, 2, 3}, "full-range bird buses must stay direct"


def test_parametric_crossover_rejects_unsafe_geometry() -> None:
    def spec(**overrides):
        base = {
            "version": 1,
            "program_channels": 2,
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
        spec(ways=[{"name": f"w{i}", "outputs": [2 * i, 2 * i + 1]} for i in range(5)]),
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
        {"source": "stereo"},
    ):
        way = {"name": "direct", "outputs": [6, 7], **tweak}
        raw = spec(ways=[way], program_channels=4)
        try:
            speaker_xo.normalize_crossover(raw, raw_measurement=True)
        except ValueError as exc:
            assert "full-range, unity" in str(exc)
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
