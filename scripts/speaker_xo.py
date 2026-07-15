"""Parametric crossover specs compiled into validated CamillaDSP fragments.

A speaker profile may declare a ``crossover`` section instead of a hand-written
``camilladsp`` fragment.  The spec describes ways (low/mid/high…) with slopes,
trims, delays, polarity and physical outputs; this module deterministically
generates the fragment plus every derived routing field, so the GUI can edit
crossovers without anyone hand-authoring DSP YAML.  Generated fragments still
pass through the full strict profile validation in speaker_config.
"""

from __future__ import annotations

import cmath
import copy
import math
from typing import Any

from speaker_profiles import normalize_speaker_id


CROSSOVER_VERSION = 1
MAX_WAYS = 4
WAY_SOURCES = {"main", "stereo"}
# slope name -> (camilladsp filter family, order)
SLOPES: dict[str, tuple[str, int]] = {
    "BW6": ("ButterworthFO", 1),
    "BW12": ("Butterworth", 2),
    "BW18": ("Butterworth", 3),
    "BW24": ("Butterworth", 4),
    "LR12": ("LinkwitzRiley", 2),
    "LR24": ("LinkwitzRiley", 4),
    "LR48": ("LinkwitzRiley", 8),
}
DEFAULT_SLOPE = "LR24"
PREVIEW_POINTS = 200
PREVIEW_MIN_HZ = 10.0
PREVIEW_MAX_HZ = 22000.0


def _number(value: Any, label: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or not lo <= result <= hi:
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
    return round(result, 4)


def _boolean(value: Any, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _edge(value: Any, label: str) -> dict[str, Any] | None:
    """Normalize one crossover edge: null or {freq, slope}."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be null or an object with freq and slope")
    unknown = set(value) - {"freq", "slope"}
    if unknown:
        raise ValueError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
    slope = value.get("slope", DEFAULT_SLOPE)
    if slope not in SLOPES:
        raise ValueError(
            f"{label} slope must be one of {', '.join(sorted(SLOPES))}"
        )
    return {
        "freq": _number(value.get("freq"), f"{label} frequency", 20, 20000),
        "slope": slope,
    }


def _way_name(value: Any, index: int) -> str:
    name = str(value or "").strip().lower()
    if (
        not name
        or len(name) > 16
        or not all(c.isalnum() or c == "_" for c in name)
    ):
        raise ValueError(
            f"way {index + 1} name must be 1-16 lowercase letters, digits or '_'"
        )
    return name


def normalize_crossover(raw: Any, *, raw_measurement: bool = False) -> dict[str, Any]:
    """Return a strict crossover spec or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("crossover must be an object")
    allowed = {"version", "program_channels", "playback", "ways"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            "unsupported crossover fields: " + ", ".join(sorted(unknown))
        )
    version = raw.get("version", CROSSOVER_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != CROSSOVER_VERSION:
        raise ValueError(f"unsupported crossover version: {version!r}")

    program_channels = raw.get("program_channels", 2)
    if program_channels not in (2, 4):
        raise ValueError("crossover program_channels must be 2 or 4")

    playback = raw.get("playback")
    if not isinstance(playback, dict):
        raise ValueError("crossover playback must be an object")
    playback_channels = playback.get("channels")
    if (
        isinstance(playback_channels, bool)
        or not isinstance(playback_channels, int)
        or not 1 <= playback_channels <= 64
    ):
        raise ValueError("crossover playback channels must be between 1 and 64")
    device = str(playback.get("device") or "").strip()
    if not device:
        raise ValueError("crossover playback device is required")
    playback_out = {
        "type": str(playback.get("type") or "Alsa"),
        "device": device,
        "channels": playback_channels,
        "format": str(playback.get("format") or "S32_LE"),
    }

    ways_in = raw.get("ways")
    if not isinstance(ways_in, list) or not 1 <= len(ways_in) <= MAX_WAYS:
        raise ValueError(f"crossover must define between 1 and {MAX_WAYS} ways")
    ways: list[dict[str, Any]] = []
    names: set[str] = set()
    used_outputs: set[int] = set()
    for index, item in enumerate(ways_in):
        if not isinstance(item, dict):
            raise ValueError(f"way {index + 1} must be an object")
        unknown = set(item) - {
            "name", "source", "highpass", "lowpass",
            "gain_db", "delay_ms", "invert", "outputs",
        }
        if unknown:
            raise ValueError(
                f"way {index + 1} has unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        name = _way_name(item.get("name"), index)
        if name in names:
            raise ValueError(f"duplicate way name: {name}")
        names.add(name)
        source = str(item.get("source") or "main").strip().lower()
        if source not in WAY_SOURCES:
            raise ValueError(f"way {name!r} source must be 'main' or 'stereo'")
        if source == "stereo" and program_channels < 4:
            raise ValueError(
                f"way {name!r} uses the stereo program which needs program_channels: 4"
            )
        highpass = _edge(item.get("highpass"), f"way {name!r} highpass")
        lowpass = _edge(item.get("lowpass"), f"way {name!r} lowpass")
        if highpass and lowpass and highpass["freq"] >= lowpass["freq"]:
            raise ValueError(
                f"way {name!r} highpass frequency must be below its lowpass frequency"
            )
        outputs_in = item.get("outputs")
        if (
            not isinstance(outputs_in, list)
            or len(outputs_in) != 2
            or any(isinstance(o, bool) or not isinstance(o, int) for o in outputs_in)
        ):
            raise ValueError(f"way {name!r} outputs must be [left, right] integers")
        left, right = outputs_in
        if left == right:
            raise ValueError(f"way {name!r} left and right outputs must differ")
        for output in (left, right):
            if not 0 <= output < playback_channels:
                raise ValueError(
                    f"way {name!r} output {output} is outside the playback range"
                )
            if output in used_outputs:
                raise ValueError(f"output {output} is used by more than one way")
            used_outputs.add(output)
        way = {
            "name": name,
            "source": source,
            "highpass": highpass,
            "lowpass": lowpass,
            "gain_db": _number(item.get("gain_db", 0), f"way {name!r} gain", -24, 24),
            "delay_ms": _number(item.get("delay_ms", 0), f"way {name!r} delay", 0, 50),
            "invert": _boolean(item.get("invert"), f"way {name!r} invert"),
            "outputs": [left, right],
        }
        if raw_measurement and (
            way["highpass"] is not None
            or way["lowpass"] is not None
            or way["gain_db"] != 0
            or way["delay_ms"] != 0
            or way["invert"]
            or way["source"] != "main"
        ):
            raise ValueError(
                "raw Measurement ways must be full-range, unity, main-program only"
            )
        ways.append(way)
    if raw_measurement and len(ways) != 1:
        raise ValueError("raw Measurement must define exactly one way")
    return {
        "version": CROSSOVER_VERSION,
        "program_channels": program_channels,
        "playback": playback_out,
        "ways": ways,
    }


def _band_position(way: dict[str, Any]) -> float:
    return float(way["highpass"]["freq"]) if way["highpass"] else 0.0


def _edge_filter(edge: dict[str, Any], *, highpass: bool) -> dict[str, Any]:
    family, order = SLOPES[edge["slope"]]
    direction = "Highpass" if highpass else "Lowpass"
    if family == "ButterworthFO":
        return {
            "type": "Biquad",
            "parameters": {"type": f"{direction}FO", "freq": edge["freq"]},
        }
    return {
        "type": "BiquadCombo",
        "parameters": {
            "type": f"{family}{direction}",
            "order": order,
            "freq": edge["freq"],
        },
    }


def crossover_fragment(
    profile_id: str, xo: dict[str, Any], *, raw_measurement: bool = False
) -> dict[str, Any]:
    """Generate the camilladsp fragment for a normalized crossover spec."""
    prefix = f"spk_{normalize_speaker_id(profile_id)}_"
    ways = xo["ways"]
    playback = copy.deepcopy(xo["playback"])
    out_channels = playback["channels"]
    active = {o for way in ways for o in way["outputs"]}

    if raw_measurement:
        way = ways[0]
        mapping: list[dict[str, Any]] = []
        for output in range(out_channels):
            if output in active:
                side = way["outputs"].index(output)
                mapping.append({"dest": output, "sources": [{"channel": side}]})
            else:
                mapping.append({"dest": output, "mute": True, "sources": []})
        name = f"{prefix}output"
        return {
            "devices": {"playback": playback},
            "filters": {},
            "mixers": {
                name: {
                    "channels": {"in": xo["program_channels"], "out": out_channels},
                    "mapping": mapping,
                }
            },
            "processors": {},
            "pipeline": [{"type": "Mixer", "name": name}],
        }

    bus_count = 2 * len(ways)
    expand_mapping = []
    filters: dict[str, Any] = {}
    filter_steps: list[dict[str, Any]] = []
    for index, way in enumerate(ways):
        source_base = 0 if way["source"] == "main" else 2
        names: list[str] = []
        if way["highpass"]:
            name = f"{prefix}{way['name']}_hp"
            filters[name] = _edge_filter(way["highpass"], highpass=True)
            names.append(name)
        if way["lowpass"]:
            name = f"{prefix}{way['name']}_lp"
            filters[name] = _edge_filter(way["lowpass"], highpass=False)
            names.append(name)
        if way["delay_ms"] > 0:
            name = f"{prefix}{way['name']}_delay"
            filters[name] = {
                "type": "Delay",
                "parameters": {
                    "delay": way["delay_ms"],
                    "unit": "ms",
                    "subsample": False,
                },
            }
            names.append(name)
        for side in (0, 1):
            bus = 2 * index + side
            expand_mapping.append(
                {"dest": bus, "sources": [{"channel": source_base + side}]}
            )
            if names:
                filter_steps.append(
                    {"type": "Filter", "channels": [bus], "names": list(names)}
                )

    route_mapping = []
    for output in range(out_channels):
        row: dict[str, Any] | None = None
        for index, way in enumerate(ways):
            if output in way["outputs"]:
                side = way["outputs"].index(output)
                source: dict[str, Any] = {"channel": 2 * index + side}
                if way["gain_db"]:
                    source["gain"] = way["gain_db"]
                if way["invert"]:
                    source["inverted"] = True
                row = {"dest": output, "sources": [source]}
                break
        route_mapping.append(row or {"dest": output, "mute": True, "sources": []})

    ways_mixer = f"{prefix}ways"
    output_mixer = f"{prefix}output"
    return {
        "devices": {"playback": playback},
        "filters": filters,
        "mixers": {
            ways_mixer: {
                "channels": {"in": xo["program_channels"], "out": bus_count},
                "mapping": expand_mapping,
            },
            output_mixer: {
                "channels": {"in": bus_count, "out": out_channels},
                "mapping": route_mapping,
            },
        },
        "processors": {},
        "pipeline": [
            {"type": "Mixer", "name": ways_mixer},
            *filter_steps,
            {"type": "Mixer", "name": output_mixer},
        ],
    }


def derive_profile_fields(xo: dict[str, Any]) -> dict[str, Any]:
    """Routing/metadata fields implied by the crossover spec."""
    out_channels = xo["playback"]["channels"]
    roles = ["muted"] * out_channels
    active: list[int] = []
    for way in xo["ways"]:
        for side, output in enumerate(way["outputs"]):
            roles[output] = f"{way['name']} {'left' if side == 0 else 'right'}"
            active.append(output)
    active_outputs = sorted(active)
    muted_outputs = [o for o in range(out_channels) if o not in set(active)]

    meter_bands = None
    main_ways = [w for w in xo["ways"] if w["source"] == "main"]
    if len(main_ways) == 3:
        ordered = sorted(main_ways, key=_band_position)
        positions = [_band_position(w) for w in ordered]
        if len(set(positions)) == 3:
            meter_bands = {
                "low": sorted(ordered[0]["outputs"]),
                "mid": sorted(ordered[1]["outputs"]),
                "high": sorted(ordered[2]["outputs"]),
            }
    return {
        "output_channels": out_channels,
        "active_outputs": active_outputs,
        "muted_outputs": muted_outputs,
        "output_roles": roles,
        "capabilities": {
            "secondary_program": any(w["source"] == "stereo" for w in xo["ways"]),
            "meter_bands": meter_bands,
        },
    }


def expand_crossover_profile(raw: dict[str, Any]) -> dict[str, Any]:
    """Expand a crossover-based profile into the classic strict shape.

    The result still carries the ``crossover`` spec (and optional ``revision``)
    for editor round-trips; speaker_config validates the generated fragment
    exactly like a hand-authored one.
    """
    if not isinstance(raw, dict):
        raise ValueError("speaker profile must be an object")
    if "camilladsp" in raw:
        raise ValueError(
            "a speaker profile defines either crossover or camilladsp, not both"
        )
    derived_keys = (
        "output_channels", "active_outputs", "muted_outputs",
        "output_roles", "capabilities",
    )
    overlap = [key for key in derived_keys if key in raw]
    if overlap:
        raise ValueError(
            "crossover profiles derive these fields automatically: "
            + ", ".join(overlap)
        )
    raw_measurement = raw.get("raw_measurement") is True
    xo = normalize_crossover(raw.get("crossover"), raw_measurement=raw_measurement)
    profile_id = normalize_speaker_id(raw.get("id"))
    expanded = {k: v for k, v in raw.items() if k != "crossover"}
    expanded.update(derive_profile_fields(xo))
    expanded["crossover"] = xo
    expanded["camilladsp"] = crossover_fragment(
        profile_id, xo, raw_measurement=raw_measurement
    )
    return expanded


def _butterworth_poles(order: int) -> list[complex]:
    return [
        cmath.exp(1j * math.pi * (2 * k + order - 1) / (2 * order))
        for k in range(1, order + 1)
    ]


def _edge_response(edge: dict[str, Any] | None, freq: float, *, highpass: bool) -> complex:
    if edge is None:
        return 1.0 + 0j
    family, order = SLOPES[edge["slope"]]
    sections = [order // 2, order // 2] if family == "LinkwitzRiley" else [order]
    if family == "ButterworthFO":
        sections = [1]
    response = 1.0 + 0j
    ratio = freq / float(edge["freq"])
    for section_order in sections:
        s = 1j * ratio
        denominator = 1.0 + 0j
        for pole in _butterworth_poles(section_order):
            denominator *= s - pole
        numerator = s ** section_order if highpass else 1.0 + 0j
        response *= numerator / denominator
    return response


def way_response(way: dict[str, Any], freq: float) -> complex:
    """Complex response of one way at freq, incl. trim, delay and polarity."""
    response = _edge_response(way["highpass"], freq, highpass=True)
    response *= _edge_response(way["lowpass"], freq, highpass=False)
    response *= 10.0 ** (way["gain_db"] / 20.0)
    if way["invert"]:
        response = -response
    if way["delay_ms"] > 0:
        response *= cmath.exp(-2j * math.pi * freq * way["delay_ms"] / 1000.0)
    return response


def _to_db(magnitude: float) -> float:
    return round(20.0 * math.log10(max(magnitude, 1e-9)), 2)


def crossover_response(
    xo: dict[str, Any], frequencies: list[float] | None = None
) -> dict[str, Any]:
    """Per-way and per-program summed magnitude curves for the editor preview."""
    if frequencies is None:
        ratio = PREVIEW_MAX_HZ / PREVIEW_MIN_HZ
        frequencies = [
            PREVIEW_MIN_HZ * ratio ** (i / (PREVIEW_POINTS - 1))
            for i in range(PREVIEW_POINTS)
        ]
    ways_out = []
    sums: dict[str, list[complex]] = {}
    for way in xo["ways"]:
        responses = [way_response(way, f) for f in frequencies]
        ways_out.append(
            {
                "name": way["name"],
                "source": way["source"],
                "db": [_to_db(abs(r)) for r in responses],
            }
        )
        totals = sums.setdefault(way["source"], [0j] * len(frequencies))
        for i, r in enumerate(responses):
            totals[i] += r
    return {
        "frequencies": [round(f, 2) for f in frequencies],
        "ways": ways_out,
        "sum": {
            source: [_to_db(abs(r)) for r in totals]
            for source, totals in sums.items()
        },
    }
