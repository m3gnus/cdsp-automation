"""Whole-chain EQ headroom: path walking, exact biquad magnitudes, preamp rule."""

from __future__ import annotations

import cmath
import math

import pytest

import audio_eq
import eq_headroom


FS = 48000.0


def _rbj(kind: str, f0: float, q: float, gain: float = 0.0):
    """Direct RBJ-cookbook coefficients, as CamillaDSP computes them."""
    a = 10 ** (gain / 40)
    w0 = 2 * math.pi * f0 / FS
    alpha = math.sin(w0) / (2 * q)
    c = math.cos(w0)
    s2 = 2 * math.sqrt(a) * alpha
    if kind == "Peaking":
        return [1 + alpha * a, -2 * c, 1 - alpha * a], [1 + alpha / a, -2 * c, 1 - alpha / a]
    if kind == "Lowshelf":
        return (
            [a * ((a + 1) - (a - 1) * c + s2), 2 * a * ((a - 1) - (a + 1) * c), a * ((a + 1) - (a - 1) * c - s2)],
            [(a + 1) + (a - 1) * c + s2, -2 * ((a - 1) + (a + 1) * c), (a + 1) + (a - 1) * c - s2],
        )
    if kind == "Highshelf":
        return (
            [a * ((a + 1) + (a - 1) * c + s2), -2 * a * ((a - 1) + (a + 1) * c), a * ((a + 1) + (a - 1) * c - s2)],
            [(a + 1) - (a - 1) * c + s2, 2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - s2],
        )
    if kind == "Lowpass":
        return [(1 - c) / 2, 1 - c, (1 - c) / 2], [1 + alpha, -2 * c, 1 - alpha]
    if kind == "Highpass":
        return [(1 + c) / 2, -(1 + c), (1 + c) / 2], [1 + alpha, -2 * c, 1 - alpha]
    if kind == "Notch":
        return [1, -2 * c, 1], [1 + alpha, -2 * c, 1 - alpha]
    if kind == "Bandpass":
        return [alpha, 0, -alpha], [1 + alpha, -2 * c, 1 - alpha]
    raise AssertionError(kind)


def _z_db(b, a, freq):
    z1 = cmath.exp(-2j * math.pi * freq / FS)
    h = (b[0] + b[1] * z1 + b[2] * z1 * z1) / (a[0] + a[1] * z1 + a[2] * z1 * z1)
    return 20 * math.log10(abs(h))


@pytest.mark.parametrize(
    "kind,f0,q,gain",
    [
        ("Peaking", 1000, 1.4, 6),
        ("Peaking", 12000, 20, -24),
        ("Lowshelf", 800, 2, -10),
        ("Highshelf", 9000, 0.7, 5),
        ("Lowpass", 100, 0.707, 0),
        ("Highpass", 15000, 3, 0),
        ("Notch", 2500, 5, 0),
        ("Bandpass", 400, 1, 0),
    ],
)
def test_prototype_magnitude_matches_the_engine_biquad(kind, f0, q, gain) -> None:
    params = {"type": kind, "freq": f0, "q": q, "gain": gain}
    response = eq_headroom.biquad_response(params, FS)
    b, a = _rbj(kind, f0, q, gain)
    for freq in (5, 63, 440, 999, 1000, 3150, 10000, 20000, 23900):
        assert abs(eq_headroom._db(response(freq)) - _z_db(b, a, freq)) < 1e-6


def _stereo_config(*, user_filters=(), speaker=None, extra_filters=None):
    """2 capture channels -> optional user step -> 2-out mixer -> speaker step."""
    filters = dict(extra_filters or {})
    pipeline = []
    if user_filters:
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": list(user_filters)})
    pipeline.append({"type": "Mixer", "name": "out"})
    if speaker:
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": list(speaker)})
    return {
        "devices": {"samplerate": FS, "capture": {"channels": 2}},
        "filters": filters,
        "mixers": {
            "out": {
                "channels": {"in": 2, "out": 2},
                "mapping": [
                    {"dest": 0, "sources": [{"channel": 0}]},
                    {"dest": 1, "sources": [{"channel": 1, "inverted": True}]},
                ],
            }
        },
        "pipeline": pipeline,
    }


def _bq(kind, freq, gain=0.0, q=1.0):
    return {"type": "Biquad", "parameters": {"type": kind, "freq": freq, "gain": gain, "q": q}}


def _state(**changes):
    state = audio_eq.default_audio_state()
    state["headroom_margin_db"] = 0.0
    state.update(changes)
    return state


def _band(state, index, **changes):
    state["bands"][index].update(changes)
    return state


def test_separate_boosts_cost_only_the_highest_peak_not_their_sum() -> None:
    state = _state()
    _band(state, 0, gain=4)  # 80 Hz low shelf
    _band(state, 4, gain=3)  # 9 kHz high shelf
    report = {}
    _updated, preamp = audio_eq.apply_audio_overlay(_stereo_config(), state, report=report)
    assert 4.0 <= report["peak_db"] < 4.05
    assert preamp == pytest.approx(-report["peak_db"], abs=0.011)


def test_a_speaker_cut_cancels_a_user_boost_at_the_same_frequency() -> None:
    state = _state()
    _band(state, 2, gain=6, q=2)  # +6 dB at 1 kHz
    config = _stereo_config(
        speaker=["spk_cut"], extra_filters={"spk_cut": _bq("Peaking", 1000, -6, 2)}
    )
    report = {}
    _updated, preamp = audio_eq.apply_audio_overlay(config, state, report=report)
    assert abs(report["peak_db"]) < 1e-6
    assert preamp == 0.0


def test_profile_shelf_overshoot_is_covered_with_the_user_eq_off() -> None:
    # A Q 3 cut shelf overshoots by about +6.9 dB above its corner.
    config = _stereo_config(
        speaker=["reflex"], extra_filters={"reflex": _bq("Lowshelf", 44.6, -10, 3)}
    )
    state = _state(enabled=False)
    report = {}
    updated, preamp = audio_eq.apply_audio_overlay(config, state, report=report)
    assert 6.8 < report["peak_db"] < 7.0
    assert 55 < report["peak_hz"] < 70
    assert preamp == pytest.approx(-report["peak_db"], abs=0.011)
    eq_step = next(s for s in updated["pipeline"] if s.get("description") == audio_eq.PIPELINE_DESCRIPTION)
    assert eq_step["names"] == ["cdsp_ui_eq_preamp"]


def test_attenuation_already_in_the_chain_is_credited_but_boosts_are_not() -> None:
    reflex = _bq("Lowshelf", 44.6, -10, 3)
    reserved = _stereo_config(
        user_filters=["calibration"],
        speaker=["reflex"],
        extra_filters={"calibration": {"type": "Gain", "parameters": {"gain": -7}}, "reflex": reflex},
    )
    boosted = _stereo_config(
        user_filters=["calibration"],
        speaker=["reflex"],
        extra_filters={"calibration": {"type": "Gain", "parameters": {"gain": 3}}, "reflex": reflex},
    )
    assert eq_headroom.chain_peak(reserved)["peak_db"] < 0
    assert 6.8 < eq_headroom.chain_peak(boosted)["peak_db"] < 7.0

    trimmed = _stereo_config(speaker=["reflex"], extra_filters={"reflex": reflex})
    trimmed["mixers"]["out"]["mapping"][0]["sources"][0]["gain"] = -10
    trimmed["mixers"]["out"]["mapping"][1]["sources"][0]["gain"] = -10
    assert eq_headroom.chain_peak(trimmed)["peak_db"] < -3


def test_loudness_is_bounded_by_its_fader_and_adds_nothing() -> None:
    state = _state()
    state["loudness"]["enabled"] = True
    report = {}
    _updated, preamp = audio_eq.apply_audio_overlay(_stereo_config(), state, report=report)
    assert report["peak_db"] == 0.0
    assert report["not_counted"] == []
    assert preamp == 0.0


def test_margin_is_kept_below_full_scale_and_manual_preamp_can_go_lower() -> None:
    state = _state(headroom_margin_db=1.0)
    _updated, preamp = audio_eq.apply_audio_overlay(_stereo_config(), state)
    assert preamp == -1.0
    state["preamp_db"] = -3.0
    _updated, preamp = audio_eq.apply_audio_overlay(_stereo_config(), state)
    assert preamp == -3.0


def test_automatic_headroom_off_applies_only_the_manual_preamp() -> None:
    state = _state(auto_headroom=False)
    _band(state, 0, gain=6)
    report = {}
    updated, preamp = audio_eq.apply_audio_overlay(_stereo_config(), state, report=report)
    assert preamp == 0.0
    assert "cdsp_ui_eq_preamp" not in updated["filters"]
    assert report["peak_db"] is None


def test_unevaluable_filters_are_reported_not_guessed() -> None:
    config = _stereo_config(
        speaker=["fir", "odd"],
        extra_filters={
            "fir": {"type": "Conv", "parameters": {"type": "Wav", "filename": "x.wav"}},
            "odd": {"type": "Biquad", "parameters": {"type": "LinkwitzTransform"}},
        },
    )
    report = eq_headroom.chain_peak(config)
    assert [line.split(":")[0] for line in report["not_counted"]] == ["fir", "odd"]


def test_muted_rows_and_bypassed_steps_do_not_count() -> None:
    config = _stereo_config(
        speaker=["boost"], extra_filters={"boost": _bq("Peaking", 1000, 9, 1)}
    )
    config["pipeline"][-1]["bypassed"] = True
    assert eq_headroom.chain_peak(config)["peak_db"] == 0.0

    config["pipeline"][-1]["bypassed"] = False
    for row in config["mixers"]["out"]["mapping"]:
        row["mute"] = True
    assert eq_headroom.chain_peak(config)["peak_db"] == 0.0


def test_a_narrow_peak_between_grid_points_is_found() -> None:
    config = _stereo_config(
        speaker=["needle"], extra_filters={"needle": _bq("Peaking", 10123.4, 24, 20)}
    )
    config["devices"]["samplerate"] = 192000
    report = eq_headroom.chain_peak(config)
    assert report["peak_db"] == pytest.approx(24.0, abs=0.01)


def test_crossover_edges_follow_their_output() -> None:
    # A bass boost reaches the woofer output but not the tweeter's highpass.
    config = _stereo_config(
        user_filters=["bass"],
        speaker=[],
        extra_filters={
            "bass": _bq("Lowshelf", 100, 6, 0.7),
            "hp": {"type": "BiquadCombo", "parameters": {"type": "LinkwitzRileyHighpass", "freq": 2000, "order": 4}},
            "lp": {"type": "BiquadCombo", "parameters": {"type": "LinkwitzRileyLowpass", "freq": 2000, "order": 4}},
        },
    )
    config["pipeline"].append({"type": "Filter", "channels": [0], "names": ["hp"]})
    config["pipeline"].append({"type": "Filter", "channels": [1], "names": ["lp"]})
    report = eq_headroom.chain_peak(config)
    assert report["output_channel"] == 1
    assert report["peak_db"] == pytest.approx(6.0, abs=0.01)

    config["pipeline"][-1]["names"] = ["hp"]
    assert eq_headroom.chain_peak(config)["peak_db"] < 0.5


def test_headroom_margin_is_validated() -> None:
    state = audio_eq.default_audio_state()
    assert state["headroom_margin_db"] == 1.0
    state["headroom_margin_db"] = 7
    with pytest.raises(ValueError, match="headroom margin"):
        audio_eq.normalize_audio_state(state)
    legacy = audio_eq.default_audio_state()
    del legacy["headroom_margin_db"]
    assert audio_eq.normalize_audio_state(legacy)["headroom_margin_db"] == 1.0


def test_a_summing_row_credits_only_its_total_attenuation() -> None:
    # Mono sub: L+R at -6 dB each sums back to 0 dB for correlated bass, so a
    # +6 dB shelf still reaches +6 dB on that output.
    config = _stereo_config(user_filters=["bass"], extra_filters={"bass": _bq("Lowshelf", 80, 6, 0.7)})
    config["mixers"]["out"] = {
        "channels": {"in": 2, "out": 1},
        "mapping": [{"dest": 0, "sources": [{"channel": 0, "gain": -6.0206}, {"channel": 1, "gain": -6.0206}]}],
    }
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(6.0, abs=0.01)

    # The same sources at -12 dB each leave 6 dB of reserved attenuation.
    for source in config["mixers"]["out"]["mapping"][0]["sources"]:
        source["gain"] = -12.0412
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(0.0, abs=0.05)


def test_linear_mixer_gains_default_to_unity() -> None:
    config = _stereo_config(speaker=["boost"], extra_filters={"boost": _bq("Peaking", 1000, 5, 1)})
    for row in config["mixers"]["out"]["mapping"]:
        row["sources"][0]["scale"] = "linear"
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(5.0, abs=0.01)
    config["mixers"]["out"]["mapping"][0]["sources"][0]["gain"] = 0.5
    config["mixers"]["out"]["mapping"][1]["sources"][0]["gain"] = 0.5
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(5.0 - 6.0206, abs=0.01)


def test_filter_steps_without_channels_apply_to_every_channel() -> None:
    config = _stereo_config(extra_filters={"boost": _bq("Peaking", 1000, 4, 1)})
    config["pipeline"].insert(0, {"type": "Filter", "names": ["boost"]})
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(4.0, abs=0.01)
    config["pipeline"][0] = {"type": "Filter", "channel": 1, "names": ["boost"]}
    report = eq_headroom.chain_peak(config)
    assert report["peak_db"] == pytest.approx(4.0, abs=0.01)
    assert report["output_channel"] == 1


def test_bandwidth_slope_and_first_order_forms() -> None:
    # Bandwidth in octaves maps to the RBJ Q; one octave is Q ~= 1.414.
    bw = eq_headroom.biquad_response({"type": "Peaking", "freq": 1000, "gain": 6, "bandwidth": 1}, FS)
    qd = eq_headroom.biquad_response({"type": "Peaking", "freq": 1000, "gain": 6, "q": 1.4142}, FS)
    for freq in (500, 707, 1414, 2000):
        assert abs(eq_headroom._db(bw(freq)) - eq_headroom._db(qd(freq))) < 0.05
    # A 12 dB/octave slope is S = 1, the steepest shelf without overshoot.
    shelf = eq_headroom.biquad_response({"type": "Lowshelf", "freq": 200, "gain": 6, "slope": 12}, FS)
    assert max(eq_headroom._db(shelf(f)) for f in range(1, 24000, 7)) <= 6.0001
    fo = eq_headroom.biquad_response({"type": "HighshelfFO", "freq": 3000, "gain": -8}, FS)
    assert eq_headroom._db(fo(3000)) == pytest.approx(-4.0, abs=0.01)
    assert eq_headroom._db(fo(23999)) == pytest.approx(-8.0, abs=0.1)
    assert eq_headroom.biquad_response({"type": "AllpassFO", "freq": 500}, FS) is None


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 1000, "gain": 3, "bandwidth": 0}},
        {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 0, "gain": 3, "bandwidth": 1}},
        {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 24000, "gain": 3, "q": 1}},
        {"type": "DiffEq", "parameters": {"a": "1, 0.5", "b": [1]}},
        {"type": "DiffEq", "parameters": {"a": [1, float("nan")], "b": [1]}},
    ],
)
def test_degenerate_filters_are_not_counted_instead_of_crashing(spec) -> None:
    with pytest.raises(eq_headroom.NotCounted):
        eq_headroom.filter_response(spec, FS)


def test_an_unstable_filter_is_rejected_not_deployed() -> None:
    # A pole on the unit circle at DC: unbounded gain.
    config = _stereo_config(
        speaker=["integrator"],
        extra_filters={"integrator": {"type": "DiffEq", "parameters": {"a": [1, -1], "b": [1]}}},
    )
    with pytest.raises(ValueError, match="gain range"):
        audio_eq.apply_audio_overlay(config, _state())


def test_repeated_reconciles_reuse_the_computed_peak() -> None:
    config = _stereo_config(speaker=["b"], extra_filters={"b": _bq("Peaking", 777, 3.3, 2)})
    first = eq_headroom.chain_peak(config)
    first["not_counted"].append("caller mutation")
    again = eq_headroom.chain_peak(config)
    assert again["peak_db"] == first["peak_db"]
    assert again["not_counted"] == []
    config["filters"]["b"]["parameters"]["gain"] = 4.4
    assert eq_headroom.chain_peak(config)["peak_db"] == pytest.approx(4.4, abs=0.01)
