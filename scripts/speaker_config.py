"""Compile source bases and speaker DSP fragments into full CamillaDSP configs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import string
import tempfile
from pathlib import Path
from typing import Any

import yaml

from audio_eq import apply_audio_overlay, audio_state_lock
from speaker_profiles import BUILTIN_SPEAKERS, DEFAULT_SPEAKER_ID, normalize_speaker_id
from speaker_xo import expand_crossover_profile


PROFILE_VERSION = 1
SOURCE_IDS = {"streamer", "gadget", "toslink", "analog"}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _finite_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}")
    return result


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return value


def _boolean(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _normalize_capabilities(
    value: Any, *, output_channels: int, active_outputs: list[int]
) -> dict[str, Any]:
    raw = _mapping(value, "capabilities")
    unknown = set(raw) - {"secondary_program", "meter_bands"}
    if unknown:
        raise ValueError("unsupported capabilities: " + ", ".join(sorted(unknown)))
    result: dict[str, Any] = {
        "secondary_program": _boolean(
            raw.get("secondary_program"), "capabilities.secondary_program", False
        )
    }
    meter_bands = raw.get("meter_bands")
    if meter_bands is None:
        result["meter_bands"] = None
        return result
    meter_bands = _mapping(meter_bands, "capabilities.meter_bands")
    if set(meter_bands) != {"low", "mid", "high"}:
        raise ValueError("capabilities.meter_bands must define low, mid and high")
    used: set[int] = set()
    normalized: dict[str, list[int]] = {}
    for name in ("low", "mid", "high"):
        channels = _normalize_outputs(
            meter_bands[name], f"capabilities.meter_bands.{name}", output_channels
        )
        if not channels:
            raise ValueError(f"capabilities.meter_bands.{name} cannot be empty")
        if not set(channels) <= set(active_outputs):
            raise ValueError(f"capabilities.meter_bands.{name} contains inactive outputs")
        if used & set(channels):
            raise ValueError("capabilities.meter_bands channel groups overlap")
        used.update(channels)
        normalized[name] = channels
    result["meter_bands"] = normalized
    return result


def load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    return _mapping(value, label)


def normalize_profile(raw: Any, *, expected_id: str | None = None) -> dict[str, Any]:
    profile = _mapping(raw, "speaker profile")
    revision = profile.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("speaker profile revision must be a non-negative integer")
    crossover: dict[str, Any] | None = None
    if profile.get("crossover") is not None:
        derived_keys = (
            "camilladsp", "output_channels", "active_outputs",
            "muted_outputs", "output_roles", "capabilities",
        )
        expanded = expand_crossover_profile(
            {k: v for k, v in profile.items() if k not in derived_keys}
        )
        # Round-tripping a normalized profile is allowed, but any supplied
        # derived field must match its generated value exactly.
        for key in derived_keys:
            if key in profile and profile[key] != expanded[key]:
                raise ValueError(
                    f"crossover profile field {key} does not match its generated value"
                )
        profile = expanded
        crossover = profile["crossover"]
    elif "crossover" in profile:
        profile = {k: v for k, v in profile.items() if k != "crossover"}
    required_fields = {
        "version", "id", "enabled", "supported_sources", "output_channels",
        "active_outputs", "muted_outputs", "output_roles", "max_volume_db",
        "bypass_user_eq", "raw_measurement", "capabilities", "camilladsp",
    }
    allowed_fields = required_fields | {"label", "description", "crossover", "revision"}
    unknown = set(profile) - allowed_fields
    missing = required_fields - set(profile)
    if unknown:
        raise ValueError("unsupported speaker profile fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("missing speaker profile fields: " + ", ".join(sorted(missing)))
    version = _integer(profile.get("version"), "speaker profile version", 0, 1000)
    if version != PROFILE_VERSION:
        raise ValueError(f"unsupported speaker profile version: {version}")

    profile_id = normalize_speaker_id(profile.get("id"))
    if expected_id is not None and profile_id != normalize_speaker_id(expected_id):
        raise ValueError(
            f"speaker profile id {profile_id!r} does not match {expected_id!r}"
        )
    if profile_id == DEFAULT_SPEAKER_ID:
        raise ValueError("Kantarellen uses the legacy compatibility profile")

    sources_in = profile.get("supported_sources", [])
    if not isinstance(sources_in, list) or not sources_in:
        raise ValueError("supported_sources must be a non-empty list")
    supported_sources = []
    for value in sources_in:
        if not isinstance(value, str):
            raise ValueError("supported source ids must be strings")
        source = value.strip().lower()
        if source not in SOURCE_IDS:
            raise ValueError(f"unsupported source id: {source}")
        if source not in supported_sources:
            supported_sources.append(source)

    output_channels = _integer(
        profile.get("output_channels"), "output_channels", 1, 64
    )
    active_outputs = _normalize_outputs(
        profile.get("active_outputs"), "active_outputs", output_channels
    )
    muted_outputs = _normalize_outputs(
        profile.get("muted_outputs"), "muted_outputs", output_channels
    )
    if set(active_outputs) & set(muted_outputs):
        raise ValueError("active_outputs and muted_outputs overlap")
    if set(active_outputs) | set(muted_outputs) != set(range(output_channels)):
        raise ValueError("every physical output must be declared active or muted")

    roles = profile.get("output_roles")
    if not isinstance(roles, list) or len(roles) != output_channels:
        raise ValueError("output_roles must label every physical output")
    if any(not isinstance(role, str) for role in roles):
        raise ValueError("output roles must be strings")
    output_roles = [role.strip() for role in roles]
    if any(not role for role in output_roles):
        raise ValueError("output roles cannot be empty")

    fragment = copy.deepcopy(_mapping(profile.get("camilladsp"), "camilladsp"))
    fragment_devices = _mapping(fragment.get("devices", {}), "camilladsp.devices")
    unexpected_device_keys = set(fragment_devices) - {"playback"}
    if unexpected_device_keys:
        raise ValueError(
            "speaker profile cannot override source device fields: "
            + ", ".join(sorted(unexpected_device_keys))
        )
    if "playback" not in fragment_devices:
        raise ValueError("speaker profile must define devices.playback")
    fragment["devices"] = fragment_devices
    prefix = f"spk_{profile_id}_"
    for section in ("filters", "mixers", "processors"):
        entries = _mapping(fragment.get(section, {}), f"camilladsp.{section}")
        for name in entries:
            if not str(name).startswith(prefix):
                raise ValueError(f"{section} name {name!r} must start with {prefix!r}")
        fragment[section] = entries
    if not isinstance(fragment.get("pipeline"), list):
        raise ValueError("camilladsp.pipeline must be a list")

    raw_measurement = _boolean(
        profile.get("raw_measurement"), "raw_measurement", False
    )
    if (profile_id == "measurement") != raw_measurement:
        raise ValueError("only the Measurement profile must set raw_measurement: true")
    bypass_user_eq = _boolean(
        profile.get("bypass_user_eq"), "bypass_user_eq", False
    )
    if raw_measurement:
        if not bypass_user_eq:
            raise ValueError("raw Measurement routing must bypass user EQ")
        if fragment["filters"] or fragment["processors"]:
            raise ValueError("raw Measurement profile cannot contain filters or processors")
        pipeline = fragment["pipeline"]
        if (
            len(fragment["mixers"]) != 1
            or len(pipeline) != 1
            or not isinstance(pipeline[0], dict)
            or pipeline[0].get("type") != "Mixer"
            or pipeline[0].get("name") != next(iter(fragment["mixers"]), None)
        ):
            raise ValueError("raw Measurement must contain exactly one output mixer")

    return {
        "version": PROFILE_VERSION,
        "id": profile_id,
        "revision": revision,
        "crossover": copy.deepcopy(crossover),
        "label": str(profile.get("label") or BUILTIN_SPEAKERS.get(profile_id, {}).get("label") or profile_id),
        "description": str(profile.get("description") or ""),
        "enabled": _boolean(profile.get("enabled"), "enabled", False),
        "supported_sources": supported_sources,
        "output_channels": output_channels,
        "active_outputs": active_outputs,
        "muted_outputs": muted_outputs,
        "output_roles": output_roles,
        "max_volume_db": _finite_number(
            profile["max_volume_db"], "max_volume_db", -100, 0
        ),
        "bypass_user_eq": bypass_user_eq,
        "raw_measurement": raw_measurement,
        "capabilities": _normalize_capabilities(
            profile.get("capabilities", {}),
            output_channels=output_channels,
            active_outputs=active_outputs,
        ),
        "camilladsp": fragment,
    }


def _normalize_program_map(value: Any, capture_channels: int) -> dict[str, list[int]]:
    """Where a source base carries the program in its capture stream.

    Defaults: main on channels 0/1, stereo on 2/3 when the capture is at
    least four channels wide. Multichannel interfaces override with e.g.
    ``program: {main: [12, 13]}``.
    """
    if value is None:
        value = {}
    spec = _mapping(value, "source program")
    unknown = set(spec) - {"main", "stereo"}
    if unknown:
        raise ValueError(
            "unsupported source program keys: " + ", ".join(sorted(unknown))
        )
    result: dict[str, list[int]] = {}
    # An explicitly declared map never gains a guessed stereo pair; the
    # implicit 2/3 default exists only for undeclared loopback-style bases.
    defaults: dict[str, list[int] | None] = {
        "main": [0, 1],
        "stereo": [2, 3] if capture_channels >= 4 and not spec else None,
    }
    used: set[int] = set()
    for name in ("main", "stereo"):
        channels = spec.get(name, defaults[name])
        if channels is None:
            continue
        if (
            not isinstance(channels, list)
            or len(channels) != 2
            or any(isinstance(c, bool) or not isinstance(c, int) for c in channels)
        ):
            raise ValueError(f"source program {name} must be [left, right] integers")
        left, right = channels
        if left == right:
            raise ValueError(f"source program {name} channels must differ")
        for channel in (left, right):
            if not 0 <= channel < capture_channels:
                raise ValueError(
                    f"source program {name} channel {channel} is outside the capture range"
                )
            if channel in used:
                raise ValueError("source program channels overlap")
            used.add(channel)
        result[name] = [left, right]
    return result


def _normalize_outputs(value: Any, label: str, channel_count: int) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    outputs: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{label} entries must be integers")
        output = item
        if output < 0 or output >= channel_count:
            raise ValueError(f"{label} entry {output} is outside the output range")
        if output in outputs:
            raise ValueError(f"{label} contains duplicate output {output}")
        outputs.append(output)
    return outputs


def load_profile(profile_dir: Path, profile_id: str) -> dict[str, Any]:
    selected = normalize_speaker_id(profile_id)
    path = profile_dir / f"{selected}.yml"
    return normalize_profile(
        load_yaml_mapping(path, f"speaker profile {selected}"), expected_id=selected
    )


def profile_catalog(profile_dir: Path, source_base_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for profile_id, metadata in BUILTIN_SPEAKERS.items():
        if profile_id == DEFAULT_SPEAKER_ID:
            result[profile_id] = {
                **metadata,
                "id": profile_id,
                "available": True,
                "legacy": True,
                "installed": True,
                "editable": False,
                "reason": "",
            }
            continue
        try:
            profile = load_profile(profile_dir, profile_id)
            missing = [
                source
                for source in profile["supported_sources"]
                if not (source_base_dir / f"{source}.yml").is_file()
            ]
            available = bool(profile["enabled"] and not missing)
            reason = "" if available else (
                f"missing source bases: {', '.join(missing)}"
                if missing
                else "profile is disabled"
            )
            result[profile_id] = {
                "id": profile_id,
                "label": profile["label"],
                "description": profile["description"],
                "available": available,
                "legacy": False,
                "installed": True,
                "editable": profile["crossover"] is not None,
                "reason": reason,
                "revision": profile["revision"],
                "enabled": profile["enabled"],
                "max_volume_db": profile["max_volume_db"],
                "supported_sources": profile["supported_sources"],
                "capabilities": profile["capabilities"],
                "bypass_user_eq": profile["bypass_user_eq"],
                "raw_measurement": profile["raw_measurement"],
                "output_roles": profile["output_roles"],
                "crossover": profile["crossover"],
            }
        except FileNotFoundError:
            result[profile_id] = {
                **metadata,
                "id": profile_id,
                "available": False,
                "legacy": False,
                "installed": False,
                "editable": True,
                "reason": "DSP definition has not been installed",
            }
        except ValueError as exc:
            result[profile_id] = {
                **metadata,
                "id": profile_id,
                "available": False,
                "legacy": False,
                "installed": True,
                "editable": False,
                "reason": str(exc),
            }
    return result


def compile_profile_config(
    source_base: dict[str, Any],
    profile: dict[str, Any],
    audio_state: dict[str, Any],
    *,
    source_id: str,
) -> dict[str, Any]:
    source = str(source_id).strip().lower()
    if source not in profile["supported_sources"]:
        raise ValueError(f"speaker profile {profile['id']} does not support {source}")
    if source_base.get("mixers") or source_base.get("processors"):
        raise ValueError("source base cannot own output mixers or processors")
    if profile["raw_measurement"]:
        if source_base.get("filters"):
            raise ValueError("raw Measurement source base cannot contain filters")
        if source_base.get("pipeline"):
            raise ValueError("raw Measurement source base pipeline must be empty")

    fragment = profile["camilladsp"]
    result = copy.deepcopy(source_base)
    # Base-owned program placement metadata; never part of the DSP config.
    program_spec = result.pop("program", None)
    source_devices = copy.deepcopy(
        _mapping(result.get("devices", {}), "source devices")
    )
    source_devices.pop("playback", None)
    result["devices"] = source_devices
    result["devices"]["playback"] = copy.deepcopy(fragment["devices"]["playback"])
    for section in ("filters", "mixers", "processors"):
        base_entries = _mapping(result.get(section, {}), f"source {section}")
        profile_entries = fragment[section]
        overlap = set(base_entries) & set(profile_entries)
        if overlap:
            raise ValueError(f"duplicate {section}: {', '.join(sorted(overlap))}")
        result[section] = {**copy.deepcopy(base_entries), **copy.deepcopy(profile_entries)}
    source_pipeline = result.get("pipeline", [])
    if not isinstance(source_pipeline, list):
        raise ValueError("source pipeline must be a list")
    result["pipeline"] = copy.deepcopy(source_pipeline) + copy.deepcopy(fragment["pipeline"])
    result["title"] = f"{profile['label']} · {source}"
    result["description"] = (
        f"Generated source={source} speaker={profile['id']} version={profile['version']}"
    )

    devices = result["devices"]
    playback = _mapping(devices.get("playback"), "devices.playback")
    if _integer(
        playback.get("channels"), "playback channels", 1, 64
    ) != profile["output_channels"]:
        raise ValueError("playback channel count does not match speaker profile")

    if profile.get("crossover"):
        # Parametric fragments declare the minimum program width and reference
        # logical program channels (main 0/1, stereo 2/3). Widen the entry
        # mixer to the source's real capture width and remap the logical
        # channels onto the physical ones the base declares (a multichannel
        # interface may deliver the program on any capture pair).
        capture = _mapping(devices.get("capture", {}), "source capture device")
        capture_channels = _integer(
            capture.get("channels"), "source capture channels", 1, 64
        )
        program_min = profile["crossover"]["program_channels"]
        if capture_channels < program_min:
            raise ValueError(
                f"speaker profile {profile['id']} needs a {program_min}-channel "
                f"program but source {source} provides {capture_channels}"
            )
        program_map = _normalize_program_map(program_spec, capture_channels)
        logical_to_physical: dict[int, int] = {}
        for name, base_index in (("main", 0), ("stereo", 2)):
            channels = program_map.get(name)
            if channels is not None:
                logical_to_physical[base_index] = channels[0]
                logical_to_physical[base_index + 1] = channels[1]
        entry_step = fragment["pipeline"][0]
        entry_mixer = result["mixers"][entry_step["name"]]
        entry_mixer["channels"]["in"] = capture_channels
        for row in entry_mixer["mapping"]:
            for row_source in row.get("sources") or []:
                logical = row_source.get("channel")
                if logical in logical_to_physical:
                    row_source["channel"] = logical_to_physical[logical]
                elif logical is not None and logical >= 2:
                    raise ValueError(
                        f"source {source} does not provide a stereo program "
                        f"for speaker profile {profile['id']}"
                    )
    current_limit = devices.get("volume_limit")
    if current_limit is None:
        devices["volume_limit"] = profile["max_volume_db"]
    else:
        devices["volume_limit"] = min(
            _finite_number(current_limit, "devices.volume_limit", -150, 50),
            profile["max_volume_db"],
        )

    _validate_pipeline_references(result)
    _validate_output_contract(result, profile)
    if not profile["bypass_user_eq"]:
        result, _preamp = apply_audio_overlay(result, audio_state)
    return result


def _validate_pipeline_references(config: dict[str, Any]) -> None:
    for index, step in enumerate(config.get("pipeline", []), start=1):
        if not isinstance(step, dict):
            raise ValueError(f"pipeline step {index} must be an object")
        kind = step.get("type")
        if kind == "Filter":
            names = step.get("names")
            if not isinstance(names, list) or not names:
                raise ValueError(f"pipeline filter step {index} has no names")
            missing = [name for name in names if name not in config["filters"]]
            if missing:
                raise ValueError(f"pipeline step {index} has missing filters: {missing}")
        elif kind == "Mixer":
            if step.get("name") not in config["mixers"]:
                raise ValueError(f"pipeline step {index} references a missing mixer")
        elif kind == "Processor":
            if step.get("name") not in config["processors"]:
                raise ValueError(f"pipeline step {index} references a missing processor")
        else:
            raise ValueError(f"pipeline step {index} has unsupported type {kind!r}")


def _validate_output_contract(config: dict[str, Any], profile: dict[str, Any]) -> None:
    mixer_steps = [
        step for step in config["pipeline"] if step.get("type") == "Mixer"
    ]
    if not mixer_steps:
        raise ValueError("speaker profile must contain an output mixer")
    if config["pipeline"][-1].get("type") != "Mixer":
        raise ValueError("speaker output mixer must be the final pipeline step")
    mixer = config["mixers"][mixer_steps[-1]["name"]]
    channels = _mapping(mixer.get("channels"), "output mixer channels")
    input_channels = _integer(
        channels.get("in"), "output mixer input channels", 1, 64
    )
    if _integer(channels.get("out"), "output mixer output channels", 1, 64) != profile["output_channels"]:
        raise ValueError("output mixer channel count does not match speaker profile")
    mapping = mixer.get("mapping")
    if not isinstance(mapping, list):
        raise ValueError("output mixer mapping must be a list")
    destinations: dict[int, dict[str, Any]] = {}
    for row in mapping:
        if not isinstance(row, dict) or "dest" not in row:
            raise ValueError("output mixer rows must have destinations")
        destination = _integer(
            row["dest"], "output mixer destination", 0, profile["output_channels"] - 1
        )
        if destination in destinations:
            raise ValueError(f"duplicate output mixer destination: {destination}")
        destinations[destination] = row
    for output in range(profile["output_channels"]):
        if output not in destinations:
            raise ValueError(f"output mixer does not define output {output}")
        row = destinations[output]
        muted = _boolean(row.get("mute"), f"output {output} mute", False)
        sources = row.get("sources")
        if not isinstance(sources, list):
            raise ValueError(f"output {output} sources must be a list")
        if output in profile["muted_outputs"]:
            if not muted:
                raise ValueError(f"output {output} must be muted")
            if sources:
                raise ValueError(f"muted output {output} cannot have sources")
            continue
        if muted:
            raise ValueError(f"active output {output} is muted")
        if not sources:
            raise ValueError(f"active output {output} must have a source")
        if profile["raw_measurement"]:
            if len(sources) != 1:
                raise ValueError(
                    f"raw Measurement output {output} must have exactly one source"
                )
            direct_source = _mapping(sources[0], f"output {output} source")
            if any(key in direct_source for key in ("gain", "inverted", "mute")):
                raise ValueError(
                    f"raw Measurement output {output} source must be direct and unity"
                )
        for source in sources:
            source = _mapping(source, f"output {output} source")
            _integer(
                source.get("channel"),
                f"output {output} source channel",
                0,
                input_channels - 1,
            )
            if "gain" in source:
                _finite_number(source["gain"], f"output {output} gain", -150, 150)
            for key in ("inverted", "mute"):
                if key in source:
                    _boolean(source[key], f"output {output} source {key}", False)


def config_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_generated_config(
    generated_dir: Path,
    config: dict[str, Any],
    *,
    source_id: str,
    profile_id: str,
) -> tuple[Path, str]:
    digest = config_digest(config)
    directory = generated_dir / digest
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source_id}--{profile_id}.yml"
    if path.exists():
        existing = load_yaml_mapping(path, "generated config")
        if config_digest(existing) != digest:
            raise ValueError(f"generated config integrity mismatch: {path}")
        return path, digest
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=f".{path.name}.", delete=False
        ) as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path, digest


def canonical_profile_document(profile: dict[str, Any]) -> dict[str, Any]:
    """Compact on-disk form for a parametric profile (derived fields omitted)."""
    if not profile.get("crossover"):
        raise ValueError("only parametric crossover profiles have a canonical form")
    return {
        "version": profile["version"],
        "id": profile["id"],
        "label": profile["label"],
        "description": profile["description"],
        "enabled": profile["enabled"],
        "revision": profile["revision"],
        "supported_sources": list(profile["supported_sources"]),
        "max_volume_db": profile["max_volume_db"],
        "bypass_user_eq": profile["bypass_user_eq"],
        "raw_measurement": profile["raw_measurement"],
        "crossover": copy.deepcopy(profile["crossover"]),
    }


def save_profile(
    profile_dir: Path,
    profile_id: str,
    data: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Validate and atomically persist a parametric profile with CAS protection."""
    selected = normalize_speaker_id(profile_id)
    if selected == DEFAULT_SPEAKER_ID:
        raise ValueError("Kantarellen uses the legacy compatibility profile")
    payload = dict(_mapping(data, "speaker profile"))
    if "crossover" not in payload:
        raise ValueError("only parametric crossover profiles can be saved")
    payload["id"] = selected
    path = profile_dir / f"{selected}.yml"
    with audio_state_lock(path):
        current_revision = 0
        try:
            existing = normalize_profile(
                load_yaml_mapping(path, f"speaker profile {selected}"),
                expected_id=selected,
            )
            current_revision = existing["revision"]
        except FileNotFoundError:
            pass
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise ValueError("expected revision must be an integer")
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError("speaker profile changed elsewhere; reload before saving")
        payload["revision"] = current_revision + 1
        normalized = normalize_profile(payload, expected_id=selected)
        document = canonical_profile_document(normalized)
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
                yaml.safe_dump(document, handle, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o644)
                temporary = Path(handle.name)
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return normalized


def prune_generated_configs(
    generated_dir: Path,
    *,
    protected_paths: tuple[Path, ...] = (),
    retain: int = 64,
) -> int:
    """Remove oldest immutable config generations, never the active target."""
    if retain < 1:
        raise ValueError("retain must be at least 1")
    try:
        children = list(generated_dir.iterdir())
    except OSError:
        return 0
    protected = {
        path.resolve(strict=False).parent for path in protected_paths
    }
    candidates: list[tuple[float, Path]] = []
    hex_digits = set(string.hexdigits)
    for child in children:
        if (
            len(child.name) != 64
            or any(character not in hex_digits for character in child.name)
            or child.is_symlink()
            or not child.is_dir()
            or child.resolve(strict=False) in protected
        ):
            continue
        try:
            candidates.append((child.stat().st_mtime, child))
        except OSError:
            continue
    candidates.sort(reverse=True)
    removed = 0
    for _mtime, directory in candidates[retain:]:
        try:
            shutil.rmtree(directory)
            removed += 1
        except OSError:
            continue
    return removed
