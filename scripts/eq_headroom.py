"""Whole-chain EQ headroom: the highest level the frequency shaping can reach.

The automatic preamp has to cover every frequency-dependent stage between the
capture and the DAC, not only the user EQ: speaker-profile correction and
crossover filters run after the mixer, per output channel, and a cut there can
cancel a user boost at the same frequency while a resonant shelf can add one.

So the config is walked as signal *paths* (capture channel -> filters ->
mixer row -> filters -> output channel), and the magnitude of every path is
evaluated on a dense log grid and refined around its maxima.  The preamp is
then minus the highest point of any path.

What is deliberately not counted:

* Flat *boosts* -- positive Gain filters and mixer gains, polarity, the fader.
  Those are level choices and belong to the user, not to the EQ.  Flat
  *attenuation* on a path does count, as headroom already reserved: a profile
  that pairs a resonant shelf with a "-7 dB calibration headroom" Gain must
  not be cut a second time.  Crediting a cut can only lower the preamp.
* Delays and allpass filters, which change phase but not magnitude.
* The ISO 226 loudness filter.  The engine scales its boost so that fader +
  loudness(f) <= 0 dB at every frequency (camilladsp-iso226 patch,
  ``cascade_gains``), so with loudness active the chain can never peak higher
  than the shaping alone at fader 0 dB, where loudness is flat.

Each path is a series chain, and a mixer row that sums several paths can at
most reach sum(|gain|) times the loudest of them, so the per-path maximum is a
true bound relative to the row's flat level.  CamillaDSP processes in 64-bit
float, so only the final output can clip and only end-to-end paths matter.

Magnitudes are exact for the RBJ-cookbook biquads CamillaDSP builds: each is
the bilinear transform, prewarped at its own centre frequency, of an analog
prototype, so its response at digital frequency w equals the prototype at
p = j * tan(w / 2) / tan(w0 / 2).  Anything this cannot evaluate is reported
as "not counted" instead of being guessed.
"""

from __future__ import annotations

import cmath
import json
import math
from collections import OrderedDict
from typing import Any, Callable


GRID_POINTS = 1200
GRID_MIN_HZ = 2.0
# Refine every local maximum within this many dB of a path's grid maximum.
REFINE_WINDOW_DB = 1.0
REFINE_ITERATIONS = 48
REFINE_MAX_PEAKS = 8
# Pure level / phase: never raise the magnitude response.
FLAT_FILTER_TYPES = {"Volume", "Delay", "Dither", "Limiter"}
# Path elements that carry a flat level in dB rather than a filter name.
LEVEL_MARK = "\x00level:"
# Loudness is bounded by the fader it is tied to; see the module docstring.
FADER_BOUNDED_TYPES = {"Iso226", "Loudness"}
UNITY_BIQUADS = {"Allpass", "AllpassFO"}

Response = Callable[[float], complex]


class NotCounted(Exception):
    """A filter whose magnitude this module cannot evaluate."""


def _num(params: dict[str, Any], key: str) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NotCounted(f"missing {key}")
    value = float(value)
    if not math.isfinite(value):
        raise NotCounted(f"non-finite {key}")
    return value


def _warp(fs: float, f0: float) -> float:
    if not 0 < f0 < fs / 2:
        raise NotCounted(f"frequency {f0:g} Hz outside 0..Nyquist")
    return math.tan(math.pi * f0 / fs)


def _prototype(fs: float, f0: float, analog: Callable[[complex], complex]) -> Response:
    k0 = _warp(fs, f0)

    def response(freq: float) -> complex:
        # Nyquist maps to p = infinity; stay a hair below it.
        w = math.pi * min(freq / (fs / 2), 1 - 1e-9)
        return analog(1j * math.tan(w / 2) / k0)

    return response


def _q_from(params: dict[str, Any], fs: float, f0: float) -> float:
    """Q for a band given as q or as bandwidth in octaves (RBJ)."""
    if params.get("q") is not None:
        q = _num(params, "q")
    elif params.get("bandwidth") is not None:
        bw = _num(params, "bandwidth")
        if bw <= 0:
            raise NotCounted("bandwidth must be positive")
        _warp(fs, f0)  # 0 < f0 < Nyquist, so sin(w0) > 0
        w0 = 2 * math.pi * f0 / fs
        try:
            q = 1 / (2 * math.sinh(math.log(2) / 2 * bw * w0 / math.sin(w0)))
        except OverflowError as exc:
            raise NotCounted("bandwidth too large") from exc
    else:
        raise NotCounted("needs q or bandwidth")
    if q <= 0:
        raise NotCounted("Q must be positive")
    return q


def _shelf_q(params: dict[str, Any], gain: float) -> float:
    """Q for a shelf given as q or as slope in dB/octave (RBJ shelf slope S)."""
    if params.get("q") is not None:
        q = _num(params, "q")
        if q <= 0:
            raise NotCounted("Q must be positive")
        return q
    if params.get("slope") is None:
        raise NotCounted("needs q or slope")
    s = _num(params, "slope") / 12.0
    if s <= 0:
        raise NotCounted("slope must be positive")
    a = 10 ** (gain / 40)
    inv_q_squared = (a + 1 / a) * (1 / s - 1) + 2
    if inv_q_squared <= 0:
        raise NotCounted("slope too steep for this gain")
    return 1 / math.sqrt(inv_q_squared)


def _z_response(b: list[float], a: list[float], fs: float) -> Response:
    def response(freq: float) -> complex:
        z1 = cmath.exp(-2j * math.pi * freq / fs)
        num = sum(coef * z1**i for i, coef in enumerate(b))
        den = sum(coef * z1**i for i, coef in enumerate(a))
        return num / den if den != 0 else complex(math.inf)

    return response


def biquad_response(params: dict[str, Any], fs: float) -> Response | None:
    """Response of one CamillaDSP ``Biquad``; None when it is unity magnitude."""
    kind = params.get("type")
    if kind in UNITY_BIQUADS:
        return None
    if kind == "Free":
        b = [_num(params, k) for k in ("b0", "b1", "b2")]
        a = [1.0, _num(params, "a1"), _num(params, "a2")]
        return _z_response(b, a, fs)
    f0 = _num(params, "freq")
    if kind == "Lowpass":
        q = _q_from(params, fs, f0)
        return _prototype(fs, f0, lambda p: 1 / (p * p + p / q + 1))
    if kind == "Highpass":
        q = _q_from(params, fs, f0)
        return _prototype(fs, f0, lambda p: p * p / (p * p + p / q + 1))
    if kind == "LowpassFO":
        return _prototype(fs, f0, lambda p: 1 / (p + 1))
    if kind == "HighpassFO":
        return _prototype(fs, f0, lambda p: p / (p + 1))
    if kind == "Bandpass":
        q = _q_from(params, fs, f0)
        return _prototype(fs, f0, lambda p: (p / q) / (p * p + p / q + 1))
    if kind == "Notch":
        q = _q_from(params, fs, f0)
        return _prototype(fs, f0, lambda p: (p * p + 1) / (p * p + p / q + 1))
    if kind in {"Peaking", "Lowshelf", "Highshelf", "LowshelfFO", "HighshelfFO"} and params.get("gain") == 0:
        return None  # a 0 dB bell or shelf is unity at every frequency
    if kind == "Peaking":
        gain = _num(params, "gain")
        q = _q_from(params, fs, f0)
        a = 10 ** (gain / 40)
        return _prototype(
            fs, f0, lambda p: (p * p + p * a / q + 1) / (p * p + p / (a * q) + 1)
        )
    if kind in {"Lowshelf", "Highshelf"}:
        gain = _num(params, "gain")
        q = _shelf_q(params, gain)
        a = 10 ** (gain / 40)
        r = math.sqrt(a) / q
        if kind == "Lowshelf":
            return _prototype(
                fs, f0, lambda p: a * (p * p + r * p + a) / (a * p * p + r * p + 1)
            )
        return _prototype(
            fs, f0, lambda p: a * (a * p * p + r * p + 1) / (p * p + r * p + a)
        )
    if kind == "LowshelfFO":
        a = 10 ** (_num(params, "gain") / 40)
        return _prototype(fs, f0, lambda p: (p + a) / (p + 1 / a))
    if kind == "HighshelfFO":
        a = 10 ** (_num(params, "gain") / 40)
        return _prototype(fs, f0, lambda p: a * (a * p + 1) / (p + a))
    raise NotCounted(f"Biquad type {kind!r} is not evaluated")


def _butterworth_qs(order: int) -> list[float | None]:
    """Section Qs of a Butterworth filter; None marks the first-order section."""
    qs: list[float | None] = [
        1 / (2 * math.sin((2 * k + 1) * math.pi / (2 * order)))
        for k in range(order // 2)
    ]
    if order % 2:
        qs.append(None)
    return qs


def _chain(responses: list[Response]) -> Response:
    def response(freq: float) -> complex:
        total = 1 + 0j
        for item in responses:
            total *= item(freq)
        return total

    return response


def combo_response(params: dict[str, Any], fs: float) -> Response | None:
    """Response of one CamillaDSP ``BiquadCombo``."""
    kind = params.get("type")
    if kind in {
        "ButterworthLowpass",
        "ButterworthHighpass",
        "LinkwitzRileyLowpass",
        "LinkwitzRileyHighpass",
    }:
        f0 = _num(params, "freq")
        order = params.get("order")
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise NotCounted("order must be a positive integer")
        high = kind.endswith("Highpass")
        if kind.startswith("LinkwitzRiley"):
            if order % 2:
                raise NotCounted("Linkwitz-Riley order must be even")
            qs = _butterworth_qs(order // 2) * 2
        else:
            qs = _butterworth_qs(order)
        sections = []
        for q in qs:
            if q is None:
                kind_fo = "HighpassFO" if high else "LowpassFO"
                section = {"type": kind_fo, "freq": f0}
            else:
                section = {"type": "Highpass" if high else "Lowpass", "freq": f0, "q": q}
            sections.append(biquad_response(section, fs))
        return _chain([s for s in sections if s is not None])
    if kind == "FivePointPeq":
        parts = [
            {"type": "Lowshelf", "freq": params.get("fls"), "gain": params.get("gls"), "q": params.get("qls")},
            *(
                {
                    "type": "Peaking",
                    "freq": params.get(f"fp{i}"),
                    "gain": params.get(f"gp{i}"),
                    "q": params.get(f"qp{i}"),
                }
                for i in (1, 2, 3)
            ),
            {"type": "Highshelf", "freq": params.get("fhs"), "gain": params.get("ghs"), "q": params.get("qhs")},
        ]
        return _chain([r for r in (biquad_response(p, fs) for p in parts) if r is not None])
    raise NotCounted(f"BiquadCombo type {kind!r} is not evaluated")


def filter_response(spec: Any, fs: float) -> Response | None:
    """Magnitude-bearing response of one filter, None when it is flat for EQ.

    Raises NotCounted for anything that may shape the response but cannot be
    evaluated here (FIR files, unknown types, malformed parameters).
    """
    if not isinstance(spec, dict):
        raise NotCounted("malformed filter")
    kind = spec.get("type")
    params = spec.get("parameters") or {}
    if not isinstance(params, dict):
        raise NotCounted("malformed parameters")
    if kind in FLAT_FILTER_TYPES or kind in FADER_BOUNDED_TYPES or kind == "Gain":
        return None
    if kind == "Biquad":
        return biquad_response(params, fs)
    if kind == "BiquadCombo":
        return combo_response(params, fs)
    if kind == "DiffEq":
        a = params.get("a") or [1.0]
        b = params.get("b") or [1.0]
        if not isinstance(a, list) or not isinstance(b, list):
            raise NotCounted("coefficients must be lists")
        if not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in [*a, *b]
        ):
            raise NotCounted("coefficients must be finite numbers")
        return _z_response([float(v) for v in b], [float(v) for v in a], fs)
    raise NotCounted(f"{kind} filters are not evaluated")


def _level_db(value: Any, scale: Any) -> float:
    """A gain as dB; anything unreadable counts as no attenuation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    if scale == "linear":
        return 20 * math.log10(abs(value)) if value else -math.inf
    return float(value)


def _source_gain(source: dict[str, Any]) -> Any:
    """A mixer source's gain; an omitted one is unity on either scale."""
    if "gain" in source and source["gain"] is not None:
        return source["gain"]
    return 1.0 if source.get("scale") == "linear" else 0.0


def gain_credit_db(spec: Any) -> float:
    """Attenuation a Gain filter reserves (<= 0); boosts and mutes give none."""
    if not isinstance(spec, dict) or spec.get("type") != "Gain":
        return 0.0
    params = spec.get("parameters") or {}
    if params.get("mute"):
        return 0.0
    return min(0.0, _level_db(params.get("gain"), params.get("scale")))


def _signal_paths(config: dict[str, Any], not_counted: dict[str, str]) -> dict[int, set[tuple[str, ...]]]:
    """Filter-name chains reaching each output channel, mixers included."""
    devices = config.get("devices") or {}
    capture = devices.get("capture") or {}
    channel_count = capture.get("channels") or 2
    paths: dict[int, set[tuple[str, ...]]] = {
        channel: {()} for channel in range(int(channel_count))
    }
    mixers = config.get("mixers") or {}
    for step in config.get("pipeline") or []:
        if not isinstance(step, dict) or step.get("bypassed"):
            continue
        kind = step.get("type")
        if kind == "Filter":
            channels = step.get("channels")
            if channels is None and isinstance(step.get("channel"), int):
                channels = [step["channel"]]
            targets = list(paths) if channels is None else channels
            names = tuple(str(name) for name in step.get("names") or [])
            for channel in targets:
                if channel in paths:
                    paths[channel] = {path + names for path in paths[channel]}
        elif kind == "Mixer":
            mixer = mixers.get(step.get("name"))
            if not isinstance(mixer, dict):
                not_counted[f"mixer {step.get('name')}"] = "mixer not defined; routing assumed 1:1"
                continue
            out_count = int((mixer.get("channels") or {}).get("out") or 0)
            routed: dict[int, set[tuple[str, ...]]] = {c: set() for c in range(out_count)}
            for row in mixer.get("mapping") or []:
                if not isinstance(row, dict) or row.get("mute"):
                    continue
                dest = row.get("dest")
                if dest not in routed:
                    continue
                sources = [
                    source
                    for source in row.get("sources") or []
                    if isinstance(source, dict) and not source.get("mute")
                ]
                # A row sums its sources, so their attenuation only counts as
                # the row total: L+R at -6 dB each is 0 dB for correlated bass.
                total = sum(
                    10 ** (_level_db(_source_gain(source), source.get("scale")) / 20)
                    for source in sources
                )
                level = 20 * math.log10(total) if total > 0 else 0.0
                extra = (f"{LEVEL_MARK}{level!r}",) if level < 0 else ()
                for source in sources:
                    routed[dest] |= {
                        path + extra for path in paths.get(source.get("channel"), set())
                    }
            paths = routed
        elif kind == "Processor":
            not_counted[f"processor {step.get('name')}"] = "processors are not evaluated"
    return paths


def _grid(fs: float, extra: list[float]) -> list[float]:
    nyquist = fs / 2
    ratio = nyquist / GRID_MIN_HZ
    points = [GRID_MIN_HZ * ratio ** (i / (GRID_POINTS - 1)) for i in range(GRID_POINTS)]
    points += [0.0, nyquist]
    points += [f for f in extra if 0 < f < nyquist]
    return sorted(set(points))


def _db(value: complex) -> float:
    magnitude = abs(value)
    if not math.isfinite(magnitude):
        return math.inf
    return 20 * math.log10(max(magnitude, 1e-12))


def _refine(path_db: Callable[[float], float], lo: float, hi: float) -> tuple[float, float]:
    """Golden-section search for the maximum between two grid neighbours."""
    lo_l, hi_l = math.log(max(lo, 1e-3)), math.log(hi)
    g = (math.sqrt(5) - 1) / 2
    c, d = hi_l - g * (hi_l - lo_l), lo_l + g * (hi_l - lo_l)
    fc, fd = path_db(math.exp(c)), path_db(math.exp(d))
    for _ in range(REFINE_ITERATIONS):
        if fc > fd:
            hi_l, d, fd = d, c, fc
            c = hi_l - g * (hi_l - lo_l)
            fc = path_db(math.exp(c))
        else:
            lo_l, c, fc = c, d, fd
            d = lo_l + g * (hi_l - lo_l)
            fd = path_db(math.exp(d))
    best = math.exp(c if fc > fd else d)
    return path_db(best), best


_CACHE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_CACHE_SIZE = 32


def _cache_key(config: dict[str, Any]) -> str:
    """Everything chain_peak reads, as one string."""
    devices = config.get("devices") or {}
    return json.dumps(
        [
            devices.get("samplerate"),
            (devices.get("capture") or {}).get("channels"),
            config.get("filters"),
            config.get("mixers"),
            config.get("pipeline"),
        ],
        sort_keys=True,
        default=str,
    )


def chain_peak(config: dict[str, Any]) -> dict[str, Any]:
    """Cached ``_chain_peak``: the switcher reconciles every second, and the
    config only changes on an edit, a source switch or a profile switch."""
    key = _cache_key(config)
    if key in _CACHE:
        _CACHE.move_to_end(key)
    else:
        _CACHE[key] = _chain_peak(config)
        if len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    result = _CACHE[key]
    return {**result, "not_counted": list(result["not_counted"])}


def _chain_peak(config: dict[str, Any]) -> dict[str, Any]:
    """Highest point of the frequency shaping on any capture->output path.

    Returns ``peak_db`` (0.0 for a chain with no shaping or attenuation, below
    zero when every path is attenuated), the
    frequency and output channel where it occurs, and ``not_counted``: filters
    or steps that may shape the response but could not be evaluated.
    """
    fs = float((config.get("devices") or {}).get("samplerate") or 48000)
    not_counted: dict[str, str] = {}
    paths = _signal_paths(config, not_counted)
    filters = config.get("filters") or {}

    responses: dict[str, Response] = {}
    centres: list[float] = []
    credits: dict[str, float] = {}
    for name in sorted({name for chains in paths.values() for path in chains for name in path}):
        if name.startswith(LEVEL_MARK):
            credits[name] = float(name[len(LEVEL_MARK):])
            continue
        if name not in filters:
            not_counted[name] = "filter not defined"
            continue
        try:
            response = filter_response(filters[name], fs)
        except NotCounted as exc:
            not_counted[name] = str(exc)
            continue
        credit = gain_credit_db(filters[name])
        if credit < 0:
            credits[name] = credit
        if response is not None:
            responses[name] = response
            params = filters[name].get("parameters") or {}
            for key in ("freq", "fls", "fp1", "fp2", "fp3", "fhs"):
                value = params.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    centres.append(float(value))

    grid = _grid(fs, centres)
    cached: dict[str, list[float]] = {
        name: [_db(response(f)) for f in grid] for name, response in responses.items()
    }

    best: dict[str, Any] = {"peak_db": -math.inf, "peak_hz": None, "output_channel": None}
    seen: set[tuple[tuple[str, ...], float]] = set()
    for channel in sorted(paths):
        for path in sorted(paths[channel]):
            counted = tuple(name for name in path if name in responses)
            credit = sum(credits.get(name, 0.0) for name in path)
            if (counted, credit) in seen:
                continue
            seen.add((counted, credit))
            if not counted:
                peak_db, peak_hz = credit, None
            else:
                totals = [
                    credit + sum(cached[name][i] for name in counted)
                    for i in range(len(grid))
                ]
                top = max(totals)

                def path_db(freq: float, counted: tuple[str, ...] = counted, credit: float = credit) -> float:
                    return credit + sum(_db(responses[name](freq)) for name in counted)

                candidates = [(top, grid[totals.index(top)])]
                # Only true peaks (higher than a neighbour) close to the top are
                # refined: a flat stretch has nothing between grid points.
                peaks = sorted(
                    (
                        (totals[i], i)
                        for i in range(1, len(grid) - 1)
                        if totals[i] >= top - REFINE_WINDOW_DB
                        and totals[i] >= totals[i - 1]
                        and totals[i] >= totals[i + 1]
                        and (totals[i] > totals[i - 1] or totals[i] > totals[i + 1])
                    ),
                    reverse=True,
                )[:REFINE_MAX_PEAKS]
                for _value, i in peaks:
                    candidates.append(_refine(path_db, grid[i - 1], grid[i + 1]))
                # A maximum at DC or Nyquist may hide a peak just inside it.
                if totals[0] >= top - REFINE_WINDOW_DB:
                    candidates.append(_refine(path_db, grid[1] / 1000, grid[1]))
                if totals[-1] >= top - REFINE_WINDOW_DB:
                    candidates.append(_refine(path_db, grid[-2], grid[-1]))
                peak_db, peak_hz = max(candidates)
            if peak_db > best["peak_db"]:
                best = {"peak_db": peak_db, "peak_hz": peak_hz, "output_channel": channel}
    if best["peak_db"] == -math.inf:
        # Nothing reaches an output (everything muted): nothing can clip.
        best = {"peak_db": 0.0, "peak_hz": None, "output_channel": None}

    return {
        "peak_db": round(best["peak_db"], 3) if math.isfinite(best["peak_db"]) else math.inf,
        "peak_hz": round(best["peak_hz"], 1) if best["peak_hz"] is not None else None,
        "output_channel": best["output_channel"],
        "not_counted": [f"{name}: {why}" for name, why in sorted(not_counted.items())],
    }
