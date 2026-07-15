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


STATE_VERSION = 3
FILTER_PREFIX = "uglan_ui_eq_"
PIPELINE_DESCRIPTION = "UGLAN user EQ (owned by source switcher)"
STEREO_FILTER_PREFIX = "uglan_stereo_eq_"
STEREO_PIPELINE_DESCRIPTION = "UGLAN stereo system EQ (owned by source switcher)"
GAIN_FILTER_TYPES = {"Peaking", "Lowshelf", "Highshelf"}
ALLOWED_TYPES = GAIN_FILTER_TYPES | {"Lowpass", "Highpass", "Bandpass", "Notch"}
MAX_BANDS = 16

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
        "preamp_db": 0.0,
        "bands": copy.deepcopy(DEFAULT_BANDS),
        "stereo": {
            "enabled": True,
            "auto_headroom": True,
            "preamp_db": 0.0,
            "trim_db": -12.0,
            "muted": False,
            "bands": copy.deepcopy(DEFAULT_BANDS),
        },
        "volume": {
            "master": "camilladsp",
            "airplay_unity_bridge": False,
            "airplay_mapping": "perceptual",
        },
        "loudness": {
            "engine": "iso226",
            "implementation_status": "available",
            "enabled": False,
            "reference_phon": 80.0,
            "reference_volume_db": -10.0,
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


def _normalize_bands(bands_in: Any, label: str = "bands") -> list[dict[str, Any]]:
    if not isinstance(bands_in, list) or not 1 <= len(bands_in) <= MAX_BANDS:
        raise ValueError(f"{label} must contain between 1 and {MAX_BANDS} entries")

    bands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(bands_in):
        if not isinstance(item, dict):
            raise ValueError(f"{label} band {index + 1} must be an object")
        band_id = str(item.get("id") or f"band_{index + 1}").strip().lower()
        if (
            not band_id
            or len(band_id) > 32
            or not all(c.isalnum() or c == "_" for c in band_id)
        ):
            raise ValueError(f"{label} band {index + 1} has an invalid id")
        if band_id in seen:
            raise ValueError(f"duplicate {label} band id: {band_id}")
        seen.add(band_id)
        filter_type = str(item.get("type", "Peaking"))
        if filter_type not in ALLOWED_TYPES:
            raise ValueError(
                f"{label} band {index + 1} has unsupported type {filter_type!r}"
            )
        bands.append(
            {
                "id": band_id,
                "enabled": _boolean(
                    item.get("enabled", True), f"{label} band {index + 1} enabled"
                ),
                "type": filter_type,
                "freq": _number(
                    item.get("freq", 1000),
                    f"{label} band {index + 1} frequency",
                    20,
                    20000,
                ),
                "gain": (
                    _number(
                        item.get("gain", 0),
                        f"{label} band {index + 1} gain",
                        -12 if band_id in {"low", "high"} else -24,
                        12 if band_id in {"low", "high"} else 24,
                    )
                    if filter_type in GAIN_FILTER_TYPES
                    else 0.0
                ),
                "q": _number(
                    item.get("q", 1), f"{label} band {index + 1} Q", 0.1, 20
                ),
            }
        )

    reserved = {band["id"]: band for band in bands if band["id"] in {"low", "high"}}
    if set(reserved) != {"low", "high"}:
        prefix = "reserved tone bands" if label == "bands" else f"reserved {label} tone bands"
        raise ValueError(f"{prefix} 'low' and 'high' must be present")
    if reserved["low"]["type"] != "Lowshelf" or reserved["high"]["type"] != "Highshelf":
        prefix = "reserved tone bands" if label == "bands" else f"reserved {label} tone bands"
        raise ValueError(f"{prefix} must remain low=Lowshelf and high=Highshelf")
    return bands


def normalize_audio_state(raw: Any, *, revision: int | None = None) -> dict[str, Any]:
    """Return a strict, JSON-safe state object or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("audio state must be an object")
    _state_version(raw)

    defaults = default_audio_state()
    bands = _normalize_bands(raw.get("bands", defaults["bands"]))
    stereo_in = raw.get("stereo", defaults["stereo"])
    if not isinstance(stereo_in, dict):
        raise ValueError("stereo must be an object")
    stereo_bands = _normalize_bands(
        stereo_in.get("bands", defaults["stereo"]["bands"]), "stereo"
    )

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
        "preamp_db": _number(raw.get("preamp_db", 0), "preamp", -24, 0),
        "bands": bands,
        "stereo": {
            "enabled": _boolean(stereo_in.get("enabled", True), "stereo enabled"),
            "auto_headroom": _boolean(
                stereo_in.get("auto_headroom", True), "stereo auto_headroom"
            ),
            "preamp_db": _number(
                stereo_in.get("preamp_db", 0), "stereo preamp", -24, 0
            ),
            "trim_db": _number(
                stereo_in.get("trim_db", -12), "stereo trim", -60, 0
            ),
            "muted": _boolean(stereo_in.get("muted", False), "stereo muted"),
            "bands": stereo_bands,
        },
        "volume": {
            "master": "camilladsp",
            "airplay_unity_bridge": _boolean(
                volume_in.get("airplay_unity_bridge", False),
                "airplay_unity_bridge",
            ),
            "airplay_mapping": "perceptual",
        },
        "loudness": {
            "engine": "iso226",
            "implementation_status": "available",
            "enabled": _boolean(
                loudness_in.get("enabled", False), "loudness enabled"
            ),
            "reference_phon": _number(
                loudness_in.get("reference_phon", 80), "reference phon", 40, 100
            ),
            "reference_volume_db": _number(
                loudness_in.get("reference_volume_db", -10), "reference volume", -60, 0
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
    for label, section in (("main", state), ("stereo", state["stereo"])):
        if section["enabled"] and section["auto_headroom"]:
            positive_boost = sum(
                band["gain"]
                for band in section["bands"]
                if band["enabled"] and band["type"] in GAIN_FILTER_TYPES and band["gain"] > 0
            )
            if positive_boost > 150:
                raise ValueError(
                    f"{label} automatic headroom requires more than CamillaDSP's 150 dB gain range"
                )
    stereo = state["stereo"]
    stereo_positive = sum(
        band["gain"]
        for band in stereo["bands"]
        if band["enabled"] and band["type"] in GAIN_FILTER_TYPES and band["gain"] > 0
    )
    stereo_preamp = 0.0 if not stereo["enabled"] else stereo["preamp_db"]
    if stereo["enabled"] and stereo["auto_headroom"]:
        stereo_preamp = min(stereo_preamp, -stereo_positive)
    stereo_total_gain = -120.0 if stereo["muted"] else stereo["trim_db"] + stereo_preamp
    if stereo_total_gain < -150:
        raise ValueError(
            "stereo trim and headroom exceed CamillaDSP's 150 dB gain range"
        )
    return state


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


@contextmanager
def audio_state_lock(path: Path):
    """Serialize read-modify-write operations from the UI and HID remote."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, 0o644)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _effective_preamp_for(section: dict[str, Any]) -> float:
    if not section.get("enabled", True):
        return 0.0
    manual = float(section.get("preamp_db", 0))
    if not section.get("auto_headroom", True):
        return manual
    # The sum of all positive band gains is deliberately conservative, but it
    # provides a safe upper bound even when several filters overlap.
    positive = sum(
        max(0.0, float(band["gain"]))
        for band in section.get("bands", [])
        if band["enabled"]
    )
    return round(min(manual, -positive), 4)


def effective_preamp_db(state: dict[str, Any]) -> float:
    return _effective_preamp_for(state)


def effective_stereo_preamp_db(state: dict[str, Any]) -> float:
    return _effective_preamp_for(state.get("stereo", {}))


def _strip_overlay(config: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    filters = updated.setdefault("filters", {})
    # The UI overlay is the sole owner of level-dependent EQ and of the two
    # reserved tone shelves.  Older UGLAN configs contain CamillaDSP Loudness,
    # Bass, and Treble filters; leaving them connected would stack them with
    # the overlay whenever a source is reloaded.
    legacy_names = {
        name
        for name, spec in filters.items()
        if (
            isinstance(spec, dict)
            and spec.get("type") in {"Loudness", "Iso226"}
            and not name.startswith(FILTER_PREFIX)
        )
        or str(name).lower() in {"bass", "treble"}
    }
    for name in list(filters):
        if (
            name.startswith(FILTER_PREFIX)
            or name.startswith(STEREO_FILTER_PREFIX)
            or name in legacy_names
        ):
            del filters[name]

    pipeline: list[dict[str, Any]] = []
    for original in updated.get("pipeline", []):
        step = copy.deepcopy(original)
        if step.get("description") in {
            PIPELINE_DESCRIPTION,
            STEREO_PIPELINE_DESCRIPTION,
        }:
            continue
        names = step.get("names")
        if isinstance(names, list):
            names = [
                name
                for name in names
                if not str(name).startswith(FILTER_PREFIX)
                and not str(name).startswith(STEREO_FILTER_PREFIX)
                and name not in legacy_names
            ]
            if not names and step.get("type") == "Filter":
                continue
            step["names"] = names
        pipeline.append(step)
    updated["pipeline"] = pipeline
    return updated


def apply_audio_overlay(
    config: dict[str, Any], state: dict[str, Any]
) -> tuple[dict[str, Any], float]:
    """Compose the reserved stereo EQ step into an active CamillaDSP config."""
    state = normalize_audio_state(state)
    updated = _strip_overlay(config)
    preamp = effective_preamp_db(state)
    filters = updated.setdefault("filters", {})
    names: list[str] = []
    loudness = state["loudness"]
    if state["enabled"] and loudness["enabled"]:
        name = f"{FILTER_PREFIX}iso226"
        filters[name] = {
            "type": "Iso226",
            "parameters": {
                "reference_level": loudness["reference_volume_db"],
                "reference_phon": loudness["reference_phon"],
                "strength": loudness["strength"],
                "max_bass_boost": loudness["max_bass_boost_db"],
                "max_treble_boost": loudness["max_treble_boost_db"],
                "fader": "Main",
            },
        }
        names.append(name)
    if state["enabled"] and abs(preamp) >= 0.0001:
        name = f"{FILTER_PREFIX}preamp"
        filters[name] = {"type": "Gain", "parameters": {"gain": preamp, "scale": "dB"}}
        names.append(name)

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
    if names:
        pipeline.insert(
            insert_at,
            {
                "type": "Filter",
                "channels": list(range(min(2, max(1, capture_channels)))),
                "names": names,
                "description": PIPELINE_DESCRIPTION,
                "bypassed": False,
            },
        )
        insert_at += 1

    # The second installation program exists only in the four-channel streamer
    # configuration. Other sources stay stereo and therefore receive no
    # auxiliary filters or routing assumptions.
    if capture_channels >= 4:
        stereo = state["stereo"]
        stereo_names: list[str] = []
        stereo_preamp = effective_stereo_preamp_db(state)
        total_gain = -120.0 if stereo["muted"] else (
            float(stereo["trim_db"]) + stereo_preamp
        )
        if abs(total_gain) >= 0.0001:
            name = f"{STEREO_FILTER_PREFIX}gain"
            filters[name] = {
                "type": "Gain",
                "parameters": {"gain": round(total_gain, 4), "scale": "dB"},
            }
            stereo_names.append(name)
        if stereo["enabled"]:
            for index, band in enumerate(stereo["bands"], start=1):
                if not band["enabled"]:
                    continue
                name = f"{STEREO_FILTER_PREFIX}{index:02d}_{band['id']}"
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
                stereo_names.append(name)
        if stereo_names:
            pipeline.insert(
                insert_at,
                {
                    "type": "Filter",
                    "channels": [2, 3],
                    "names": stereo_names,
                    "description": STEREO_PIPELINE_DESCRIPTION,
                    "bypassed": False,
                },
            )
    return updated, preamp


def status_payload(
    state: dict[str, Any], *, applied: bool, effective_preamp: float, error: str = ""
) -> dict[str, Any]:
    return {
        "revision": int(state.get("revision", 0)),
        "applied": bool(applied),
        "effective_preamp_db": effective_preamp,
        "stereo_effective_preamp_db": effective_stereo_preamp_db(state),
        "error": error,
        "updated_at": time.time(),
    }
