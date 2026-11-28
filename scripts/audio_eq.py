"""Validated persistent audio-EQ state and CamillaDSP overlay composition."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from eq_headroom import chain_peak


STATE_VERSION = 3
FILTER_PREFIX = "cdsp_ui_eq_"
PIPELINE_DESCRIPTION = "CDSP user EQ (owned by source switcher)"
GAIN_FILTER_TYPES = {"Peaking", "Lowshelf", "Highshelf"}
ALLOWED_TYPES = GAIN_FILTER_TYPES | {"Lowpass", "Highpass", "Bandpass", "Notch"}
MAX_BANDS = 16
# ISO 226:2003 defines the contours up to 90 phon, and only to 80 above 4 kHz,
# so 81-90 already extrapolates the top of the curve. The engine rejects anything
# higher; see camilladsp-iso226/camilladsp-v4.1.3-iso226.patch.
ISO226_MAX_PHON = 90
# Kept below the computed whole-chain peak for transient overshoot of steep
# filters and inter-sample peaks rebuilt by the resampler, neither of which a
# magnitude response shows.
DEFAULT_HEADROOM_MARGIN_DB = 1.0
MAX_HEADROOM_MARGIN_DB = 6.0
# CamillaDSP's Gain filter range.
MIN_PREAMP_DB = -150.0

DEFAULT_BANDS = [
    {
        "id": "low",
        "enabled": True,
        "type": "Lowshelf",
        "freq": 80.0,
        "gain": 0.0,
        "q": 0.7,
    },
    {
        "id": "low_mid",
        "enabled": True,
        "type": "Peaking",
        "freq": 250.0,
        "gain": 0.0,
        "q": 1.0,
    },
    {
        "id": "mid",
        "enabled": True,
        "type": "Peaking",
        "freq": 1000.0,
        "gain": 0.0,
        "q": 1.0,
    },
    {
        "id": "high_mid",
        "enabled": True,
        "type": "Peaking",
        "freq": 4000.0,
        "gain": 0.0,
        "q": 1.0,
    },
    {
        "id": "high",
        "enabled": True,
        "type": "Highshelf",
        "freq": 9000.0,
        "gain": 0.0,
        "q": 0.7,
    },
]


def default_audio_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "revision": 0,
        "enabled": True,
        "auto_headroom": True,
        "headroom_margin_db": DEFAULT_HEADROOM_MARGIN_DB,
        "preamp_db": 0.0,
        "bands": copy.deepcopy(DEFAULT_BANDS),
        "volume": {
            "master": "camilladsp",
            "airplay_unity_bridge": True,
            "airplay_mapping": "linear",
        },
        "loudness": {
            "engine": "iso226",
            "implementation_status": "available",
            "enabled": False,
            "reference_phon": 80.0,
            "reference_volume_db": -10.0,
            # The MOTU main level the reference was measured at; None leaves
            # the MOTU out of the calibration (the behaviour before it existed).
            "reference_motu_db": None,
            "strength": 1.0,
            "max_bass_boost_db": 10.0,
            "max_treble_boost_db": 4.0,
        },
    }


def _number(value: Any, label: str, lo: float, hi: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(result) or result < lo or result > hi:
        raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
    return round(result, 4)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _state_version(raw: dict[str, Any]) -> int:
    """Accept the two compatible legacy layouts and reject unknown schemas."""
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("audio state version must be an integer")
    if version not in {1, 2, STATE_VERSION}:
        raise ValueError(f"unsupported audio state version: {version}")
    return version


def _normalize_bands(bands_in: Any) -> list[dict[str, Any]]:
    if not isinstance(bands_in, list) or not 1 <= len(bands_in) <= MAX_BANDS:
        raise ValueError(f"bands must contain between 1 and {MAX_BANDS} entries")

    bands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(bands_in):
        if not isinstance(item, dict):
            raise ValueError(f"bands band {index + 1} must be an object")
        band_id = str(item.get("id") or f"band_{index + 1}").strip().lower()
        if (
            not band_id
            or len(band_id) > 32
            or not all(c.isalnum() or c == "_" for c in band_id)
        ):
            raise ValueError(f"bands band {index + 1} has an invalid id")
        if band_id in seen:
            raise ValueError(f"duplicate bands band id: {band_id}")
        seen.add(band_id)
        filter_type = str(item.get("type", "Peaking"))
        if filter_type not in ALLOWED_TYPES:
            raise ValueError(
                f"bands band {index + 1} has unsupported type {filter_type!r}"
            )
        bands.append(
            {
                "id": band_id,
                "enabled": _boolean(
                    item.get("enabled", True), f"bands band {index + 1} enabled"
                ),
                "type": filter_type,
                "freq": _number(
                    item.get("freq", 1000),
                    f"bands band {index + 1} frequency",
                    20,
                    20000,
                ),
                "gain": (
                    _number(
                        item.get("gain", 0),
                        f"bands band {index + 1} gain",
                        -12 if band_id in {"low", "high"} else -24,
                        12 if band_id in {"low", "high"} else 24,
                    )
                    if filter_type in GAIN_FILTER_TYPES
                    else 0.0
                ),
                "q": _number(
                    item.get("q", 1), f"bands band {index + 1} Q", 0.1, 20
                ),
            }
        )

    reserved = {band["id"]: band for band in bands if band["id"] in {"low", "high"}}
    if set(reserved) != {"low", "high"}:
        raise ValueError("reserved tone bands 'low' and 'high' must be present")
    if reserved["low"]["type"] != "Lowshelf" or reserved["high"]["type"] != "Highshelf":
        raise ValueError(
            "reserved tone bands must remain low=Lowshelf and high=Highshelf"
        )
    return bands


def normalize_audio_state(raw: Any, *, revision: int | None = None) -> dict[str, Any]:
    """Return a strict, JSON-safe state object or raise ValueError.

    Only known sections are read, so documents persisted before the secondary
    "stereo" program was retired still load: their leftover ``stereo`` object
    is deliberately ignored and dropped on the next write.
    """
    if not isinstance(raw, dict):
        raise ValueError("audio state must be an object")
    _state_version(raw)

    defaults = default_audio_state()
    bands = _normalize_bands(raw.get("bands", defaults["bands"]))

    volume_in = raw.get("volume", {})
    loudness_in = raw.get("loudness", {})
    if not isinstance(volume_in, dict) or not isinstance(loudness_in, dict):
        raise ValueError("volume and loudness must be objects")

    current_revision = raw.get("revision", 0) if revision is None else revision
    if (
        isinstance(current_revision, bool)
        or not isinstance(current_revision, int)
        or current_revision < 0
    ):
        raise ValueError("revision must be a non-negative integer")

    state = {
        "version": STATE_VERSION,
        "revision": current_revision,
        "enabled": _boolean(raw.get("enabled", True), "enabled"),
        "auto_headroom": _boolean(
            raw.get("auto_headroom", True), "auto_headroom"
        ),
        "headroom_margin_db": _number(
            raw.get("headroom_margin_db", DEFAULT_HEADROOM_MARGIN_DB),
            "headroom margin",
            0,
            MAX_HEADROOM_MARGIN_DB,
        ),
        "preamp_db": _number(raw.get("preamp_db", 0), "preamp", -24, 0),
        "bands": bands,
        "volume": {
            "master": "camilladsp",
            # These describe the installed architecture, not user preferences.
            # Normalize legacy state written before the unity/linear bridge was
            # deployed so the API cannot report the obsolete double-fader path.
            "airplay_unity_bridge": True,
            "airplay_mapping": "linear",
        },
        "loudness": {
            "engine": "iso226",
            "implementation_status": "available",
            "enabled": _boolean(
                loudness_in.get("enabled", False), "loudness enabled"
            ),
            "reference_phon": _number(
                loudness_in.get("reference_phon", 80),
                "reference phon",
                40,
                ISO226_MAX_PHON,
            ),
            "reference_volume_db": _number(
                loudness_in.get("reference_volume_db", -10), "reference volume", -60, 0
            ),
            "reference_motu_db": (
                None
                if loudness_in.get("reference_motu_db") is None
                else _number(
                    loudness_in["reference_motu_db"], "reference MOTU level", -100, 0
                )
            ),
            "strength": _number(
                loudness_in.get("strength", 1), "loudness strength", 0, 1
            ),
            "max_bass_boost_db": _number(
                loudness_in.get("max_bass_boost_db", 10), "maximum bass boost", 0, 18
            ),
            "max_treble_boost_db": _number(
                loudness_in.get("max_treble_boost_db", 4), "maximum treble boost", 0, 12
            ),
        },
    }
    if state["auto_headroom"] and state["enabled"]:
        peak = user_eq_peak_db(state["bands"])
        if peak + state["headroom_margin_db"] > -MIN_PREAMP_DB:
            raise ValueError(
                f"the EQ peaks at +{peak:.0f} dB; automatic headroom needs more than "
                f"CamillaDSP's {-MIN_PREAMP_DB:g} dB gain range"
            )
    return state


def user_eq_peak_db(bands: list[dict[str, Any]]) -> float:
    """Peak of the enabled user bands alone, for rejecting undeployable states."""
    filters = {
        f"band{index}": {
            "type": "Biquad",
            "parameters": {
                "type": band["type"],
                "freq": band["freq"],
                "q": band["q"],
                **({"gain": band["gain"]} if band["type"] in GAIN_FILTER_TYPES else {}),
            },
        }
        for index, band in enumerate(bands)
        if band["enabled"]
    }
    if not filters:
        return 0.0
    config = {
        "devices": {"samplerate": 48000, "capture": {"channels": 1}},
        "filters": filters,
        "pipeline": [{"type": "Filter", "channels": [0], "names": list(filters)}],
    }
    return float(chain_peak(config)["peak_db"])


def read_audio_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_audio_state()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read audio state: {exc}") from exc
    return normalize_audio_state(raw)


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
            temporary = Path(handle.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Lock files are shared between the root control UI and the daemons running as
# the install user, so every participant must be able to open them O_RDWR.  The
# install group is common to both (the UI unit sets Group=), which leaves the
# mode: O_CREAT's mode argument is masked by the creator's umask, and systemd's
# default 022 would strip group write from a lazily created lock and lock the
# other uid out for good.
LOCK_FILE_MODE = 0o660


@contextmanager
def exclusive_file_lock(lock_path: Path, mode: int = LOCK_FILE_MODE):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, mode)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        try:
            # Applied to the descriptor, never to the path: replacing a live
            # lock file's inode would let two holders believe they own it.
            os.fchmod(handle.fileno(), mode)
        except OSError:
            # Someone else created it; only its owner may adjust the mode.
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def audio_state_lock(path: Path):
    """Serialize read-modify-write operations from the UI and HID remote."""
    return exclusive_file_lock(path.with_name(f"{path.name}.lock"))


def update_tone_band(
    path: Path,
    band_id: str,
    change_db: float | None,
    minimum_db: float = -12.0,
    maximum_db: float = 12.0,
) -> dict[str, Any]:
    """Atomically adjust, or reset, a reserved persistent tone band."""
    if band_id not in {"low", "high"}:
        raise ValueError("tone control must target reserved band 'low' or 'high'")
    if minimum_db > maximum_db or minimum_db < -12 or maximum_db > 12:
        raise ValueError("tone limits must be ordered within -12..12 dB")
    with audio_state_lock(path):
        state = read_audio_state(path)
        band = next((item for item in state["bands"] if item["id"] == band_id), None)
        if band is None:
            raise ValueError(f"reserved tone band {band_id!r} is missing")
        target = 0.0 if change_db is None else float(band["gain"]) + float(change_db)
        band["gain"] = round(max(minimum_db, min(maximum_db, target)), 4)
        state = normalize_audio_state(state, revision=state["revision"] + 1)
        atomic_write_json(path, state)
        return state


def reset_tone_bands(path: Path) -> dict[str, Any]:
    """Reset both reserved tone bands in one lock transaction and revision."""
    with audio_state_lock(path):
        state = read_audio_state(path)
        for band_id in ("low", "high"):
            band = next(
                (item for item in state["bands"] if item["id"] == band_id), None
            )
            if band is None:
                raise ValueError(f"reserved tone band {band_id!r} is missing")
            band["gain"] = 0.0
        state = normalize_audio_state(state, revision=state["revision"] + 1)
        atomic_write_json(path, state)
        return state


def effective_preamp_db(state: dict[str, Any], peak_db: float | None) -> float:
    """The preamp for a whole-chain shaping peak (see eq_headroom.chain_peak).

    The preamp is a headroom stage of its own, not part of the user EQ, so it
    stays in force while the EQ is switched off: the speaker profile's own
    correction can peak above 0 dB too.  Automatic headroom puts the highest
    point of the chain ``headroom_margin_db`` below full scale and never
    raises the level; a lower manual preamp still wins.  ``peak_db`` None
    means the chain was not evaluated, so only the manual value applies.
    """
    manual = float(state.get("preamp_db", 0))
    if not state.get("auto_headroom", True) or peak_db is None:
        return manual
    if not math.isfinite(float(peak_db)):
        return -math.inf  # an unstable filter: rejected by the caller
    margin = float(state.get("headroom_margin_db", DEFAULT_HEADROOM_MARGIN_DB))
    automatic = min(0.0, -(float(peak_db) + margin))
    # Round towards more attenuation so display rounding never costs headroom.
    return math.floor(min(manual, automatic) * 100) / 100


def is_owned_filter_name(name: object) -> bool:
    """True for a filter this tool owns."""
    return str(name).startswith(FILTER_PREFIX)


def _strip_overlay(config: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    filters = updated.setdefault("filters", {})
    # The UI overlay is the sole owner of level-dependent EQ and of the two
    # reserved tone shelves.  Older configs contain CamillaDSP Loudness, Bass,
    # and Treble filters; leaving them connected would stack them with the
    # overlay whenever a source is reloaded.
    legacy_names = {
        name
        for name, spec in filters.items()
        if (
            isinstance(spec, dict)
            and spec.get("type") in {"Loudness", "Iso226"}
            and not is_owned_filter_name(name)
        )
        or str(name).lower() in {"bass", "treble"}
    }
    for name in list(filters):
        if is_owned_filter_name(name) or name in legacy_names:
            del filters[name]

    pipeline: list[dict[str, Any]] = []
    for original in updated.get("pipeline", []):
        step = copy.deepcopy(original)
        if step.get("description") == PIPELINE_DESCRIPTION:
            continue
        names = step.get("names")
        if isinstance(names, list):
            names = [
                name
                for name in names
                if not is_owned_filter_name(name) and name not in legacy_names
            ]
            if not names and step.get("type") == "Filter":
                continue
            step["names"] = names
        pipeline.append(step)
    updated["pipeline"] = pipeline
    return updated


def _program_channels(
    config: dict[str, Any],
    pipeline: list[Any],
    insert_at: int,
    capture_channels: int,
) -> list[int]:
    """The capture channels the program actually arrives on.

    The overlay belongs on the channels the first mixer reads, not on 0..1.
    A multichannel interface gives every input its own channel -- on a MOTU
    UltraLite mk5 the analog pair lands on 2/3 and TOSLINK on 12/13 -- so a
    fixed 0..1 applies the whole user EQ, loudness included, to channels the
    pipeline never reads, and the correction silently does nothing.

    Anything this cannot read confidently falls back to the leading pair,
    which is what a plain stereo capture uses anyway.
    """
    fallback = list(range(min(2, max(1, capture_channels))))
    if insert_at >= len(pipeline):
        return fallback
    step = pipeline[insert_at]
    if not isinstance(step, dict) or step.get("type") != "Mixer":
        return fallback
    mixer = (config.get("mixers") or {}).get(step.get("name"))
    if not isinstance(mixer, dict):
        return fallback
    channels: set[int] = set()
    for mapping in mixer.get("mapping") or []:
        if not isinstance(mapping, dict):
            continue
        for source in mapping.get("sources") or []:
            if not isinstance(source, dict):
                continue
            channel = source.get("channel")
            if isinstance(channel, bool) or not isinstance(channel, int):
                return fallback
            channels.add(channel)
    if not channels or any(c < 0 or c >= capture_channels for c in channels):
        return fallback
    return sorted(channels)


# The engine accepts reference levels in this range (Iso226 validate_config).
ENGINE_REFERENCE_LEVEL_RANGE = (-100.0, 20.0)


def loudness_reference_level(
    loudness: dict[str, Any], motu_db: float | None
) -> float:
    """The fader level the engine should treat as the reference, in dB.

    The engine derives the listening level from its own fader alone:
    ``phon = reference_phon + fader - reference_level``.  The MOTU main output
    sits after CamillaDSP, so every dB it is above the level the reference was
    measured at is a dB louder in the room that the fader cannot see.  Moving
    the reference down by that offset folds the MOTU into the same sum:
    ``phon = reference_phon + (fader - reference_volume) + (motu - reference_motu)``.
    Without a calibrated MOTU level, or while the MOTU level is unknown, the
    reference is used as entered.
    """
    reference = float(loudness["reference_volume_db"])
    calibrated = loudness.get("reference_motu_db")
    if calibrated is None or motu_db is None:
        return reference
    low, high = ENGINE_REFERENCE_LEVEL_RANGE
    return round(max(low, min(high, reference - (float(motu_db) - float(calibrated)))), 2)


def apply_audio_overlay(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    motu_db: float | None = None,
    report: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    """Compose the reserved user-EQ step into an active CamillaDSP config.

    ``motu_db`` is the MOTU main output level now, which moves the loudness
    reference (loudness_reference_level); None uses it as calibrated.

    The preamp is sized from the whole chain -- this overlay plus every
    speaker-profile filter after the mixer -- and stays in place with the user
    EQ switched off.  ``report``, when given, is filled with the chain peak
    the preamp was sized from (see eq_headroom.chain_peak).
    """
    state = normalize_audio_state(state)
    updated = _strip_overlay(config)
    filters = updated.setdefault("filters", {})
    names: list[str] = []
    loudness = state["loudness"]
    if state["enabled"] and loudness["enabled"]:
        name = f"{FILTER_PREFIX}iso226"
        filters[name] = {
            "type": "Iso226",
            "parameters": {
                "reference_level": loudness_reference_level(loudness, motu_db),
                "reference_phon": loudness["reference_phon"],
                "strength": loudness["strength"],
                "max_bass_boost": loudness["max_bass_boost_db"],
                "max_treble_boost": loudness["max_treble_boost_db"],
                "fader": "Main",
            },
        }
        names.append(name)
    # The preamp goes straight after loudness, ahead of the bands.
    preamp_at = len(names)

    for index, band in enumerate(state["bands"], start=1):
        if not state["enabled"]:
            break
        if not band["enabled"]:
            continue
        name = f"{FILTER_PREFIX}{index:02d}_{band['id']}"
        parameters = {
            "type": band["type"],
            "freq": band["freq"],
            "q": band["q"],
        }
        if band["type"] in GAIN_FILTER_TYPES:
            parameters["gain"] = band["gain"]
        filters[name] = {
            "type": "Biquad",
            "parameters": parameters,
        }
        names.append(name)

    capture_channels = int(
        updated.get("devices", {}).get("capture", {}).get("channels") or 2
    )
    pipeline = updated.setdefault("pipeline", [])
    insert_at = next(
        (i for i, existing in enumerate(pipeline) if existing.get("type") == "Mixer"),
        len(pipeline),
    )
    step = {
        "type": "Filter",
        "channels": _program_channels(updated, pipeline, insert_at, capture_channels),
        "names": names,
        "description": PIPELINE_DESCRIPTION,
        "bypassed": False,
    }
    if names:
        pipeline.insert(insert_at, step)

    peak = chain_peak(updated) if state["auto_headroom"] else None
    preamp = effective_preamp_db(state, peak["peak_db"] if peak else None)
    if preamp < MIN_PREAMP_DB:
        raise ValueError(
            f"the EQ chain peaks at {peak['peak_db'] if peak else '?'} dB, beyond "
            f"CamillaDSP's {MIN_PREAMP_DB:g} dB gain range"
        )
    if abs(preamp) >= 0.0001:
        name = f"{FILTER_PREFIX}preamp"
        filters[name] = {"type": "Gain", "parameters": {"gain": preamp, "scale": "dB"}}
        names.insert(preamp_at, name)
        if len(names) == 1:
            pipeline.insert(insert_at, step)
    if report is not None:
        report.clear()
        report.update(
            peak
            or {"peak_db": None, "peak_hz": None, "output_channel": None, "not_counted": []}
        )
        report["margin_db"] = state["headroom_margin_db"]
    return updated, preamp


def status_payload(
    state: dict[str, Any],
    *,
    applied: bool,
    effective_preamp: float | None,
    error: str = "",
    headroom: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "revision": int(state.get("revision", 0)),
        "applied": bool(applied),
        "effective_preamp_db": effective_preamp,
        "headroom": headroom,
        "error": error,
        "updated_at": time.time(),
    }
