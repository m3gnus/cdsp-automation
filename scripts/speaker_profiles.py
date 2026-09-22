"""Persistent speaker selection and per-speaker audio-state helpers.

The physical source and speaker are independent choices.  This module owns the
small persistent part of the speaker-profile system; CamillaDSP composition is
kept in the source switcher so it remains the only live-config writer.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from audio_eq import (
    atomic_write_json,
    audio_state_lock,
    default_audio_state,
    exclusive_file_lock,
    read_audio_state,
)


SELECTION_VERSION = 1
# The shipped catalog below describes the maintainer's own speakers so an
# unconfigured deployment keeps working. A site replaces the whole catalog
# with the JSON file at SPEAKER_CATALOG_PATH instead of editing this module.
# Contract: the DEFAULT speaker is the one that plays through the existing
# full CamillaDSP configs (and, for it alone, the legacy audio-eq.json state
# path); every other speaker is a managed profile. LEGACY_SPEAKER_ID is a
# readable alias for that same identity at the call sites that care about
# the legacy state path rather than the boot selection.
SPEAKER_CATALOG_PATH = os.environ.get(
    "SPEAKER_CATALOG_PATH", "/etc/cdsp-automation/speaker-catalog.json"
)
DEFAULT_SPEAKER_ID = "kantarellen"
LEGACY_SPEAKER_ID = DEFAULT_SPEAKER_ID
# These profiles are complete operator-owned CamillaDSP files in the normal
# CamillaGUI config directory. They are intentionally editable in CamillaGUI;
# the source switcher validates and loads the file directly instead of
# generating an immutable config from the compact crossover schema.
OPERATOR_CONFIG_SPEAKERS: dict[str, dict[str, str]] = {
    "partymeh": {
        "streamer": "partymeh-streamer.yml",
        "gadget": "partymeh-gadget.yml",
        "toslink": "partymeh-toslink.yml",
        "analog": "partymeh-analog.yml",
    },
}
OPERATOR_CONFIG_SOURCES = {"streamer", "gadget", "toslink", "analog"}
BUILTIN_SPEAKERS: dict[str, dict[str, str]] = {
    "kantarellen": {
        "label": "Kantarellen",
        "description": "Three-way stereo system on outputs 1–6",
    },
    "partymeh": {
        "label": "PartyMEH",
        "description": "Three-way stereo system on outputs 1–6",
    },
    "measurement": {
        "label": "Measurement",
        "description": "Direct measurement routing without speaker EQ or crossover",
    },
}


def operator_configs_for_speaker(speaker_id: str) -> dict[str, str]:
    """Return source -> filename for an operator-owned speaker profile."""
    return dict(OPERATOR_CONFIG_SPEAKERS.get(speaker_id) or {})


def operator_config_for_source(speaker_id: str, source: str) -> str | None:
    return OPERATOR_CONFIG_SPEAKERS.get(speaker_id, {}).get(source)


def normalize_speaker_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("speaker id must be a string")
    speaker_id = value.strip().lower()
    if (
        not speaker_id
        or len(speaker_id) > 48
        or not all(char.isalnum() or char in {"_", "-"} for char in speaker_id)
    ):
        raise ValueError("speaker id must use lowercase letters, numbers, '-' or '_'")
    return speaker_id


def normalize_speaker_catalog(raw: Any) -> dict[str, Any]:
    """Validate a site speaker-catalog document.

    Shape: {"default": "<id>", "speakers": {"<id>": {"label": "...",
    "description": "...", "operator_configs": {"<source>": "<file>.yml"}}}}.
    The default speaker plays through the existing full CamillaDSP configs;
    every other speaker must be an installed managed profile.
    """
    if not isinstance(raw, dict):
        raise ValueError("speaker catalog must be an object")
    speakers_raw = raw.get("speakers")
    if not isinstance(speakers_raw, dict) or not speakers_raw:
        raise ValueError("speaker catalog must define at least one speaker")
    speakers: dict[str, dict[str, str]] = {}
    operator_configs: dict[str, dict[str, str]] = {}
    for raw_id, meta in speakers_raw.items():
        speaker_id = normalize_speaker_id(raw_id)
        if speaker_id in speakers:
            raise ValueError(f"duplicate normalized speaker id: {speaker_id}")
        if not isinstance(meta, dict):
            raise ValueError(f"speaker {speaker_id!r} definition must be an object")
        speakers[speaker_id] = {
            "label": str(meta.get("label") or speaker_id),
            "description": str(meta.get("description") or ""),
        }
        configs = meta.get("operator_configs")
        if configs is None:
            continue
        if not isinstance(configs, dict):
            raise ValueError(
                f"speaker {speaker_id!r} operator_configs must be an object"
            )
        normalized_configs: dict[str, str] = {}
        for raw_source, raw_filename in configs.items():
            if not isinstance(raw_source, str):
                raise ValueError(
                    f"speaker {speaker_id!r} operator config source must be a string"
                )
            source = raw_source.strip().lower()
            if source not in OPERATOR_CONFIG_SOURCES:
                raise ValueError(
                    f"speaker {speaker_id!r} has unsupported operator config "
                    f"source: {source}"
                )
            if source in normalized_configs:
                raise ValueError(
                    f"speaker {speaker_id!r} has duplicate operator config "
                    f"source: {source}"
                )
            if not isinstance(raw_filename, str):
                raise ValueError(
                    f"speaker {speaker_id!r} operator config filename must be a string"
                )
            filename = raw_filename.strip()
            filename_path = Path(filename)
            if (
                not filename
                or filename_path.name != filename
                or filename_path.suffix.lower() not in {".yml", ".yaml"}
            ):
                raise ValueError(
                    f"speaker {speaker_id!r} operator config must be a .yml or "
                    ".yaml filename in the CamillaDSP config directory"
                )
            normalized_configs[source] = filename
        if normalized_configs:
            operator_configs[speaker_id] = normalized_configs
    default = normalize_speaker_id(raw.get("default", next(iter(speakers))))
    if default not in speakers:
        raise ValueError(f"default speaker {default!r} is not in the catalog")
    return {
        "default": default,
        "speakers": speakers,
        "operator_configs": operator_configs,
    }


def _apply_speaker_catalog() -> None:
    """Replace the built-in catalog from SPEAKER_CATALOG_PATH when present.

    Failures keep the built-ins and only warn: an unreadable catalog must not
    crash-loop every daemon importing this module, and speaker switching is
    still guarded by per-selection validation.
    """
    global DEFAULT_SPEAKER_ID, LEGACY_SPEAKER_ID
    try:
        with open(SPEAKER_CATALOG_PATH, "r", encoding="utf-8") as handle:
            catalog = normalize_speaker_catalog(json.load(handle))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        print(
            f"speaker catalog {SPEAKER_CATALOG_PATH} ignored, "
            f"using built-ins: {exc}",
            flush=True,
        )
        return
    DEFAULT_SPEAKER_ID = LEGACY_SPEAKER_ID = catalog["default"]
    # Mutate in place: function defaults and from-imports keep referencing
    # these dict objects.
    BUILTIN_SPEAKERS.clear()
    BUILTIN_SPEAKERS.update(catalog["speakers"])
    OPERATOR_CONFIG_SPEAKERS.clear()
    OPERATOR_CONFIG_SPEAKERS.update(catalog["operator_configs"])


_apply_speaker_catalog()


def default_speaker_selection() -> dict[str, Any]:
    return {
        "version": SELECTION_VERSION,
        "revision": 0,
        "selected": DEFAULT_SPEAKER_ID,
    }


def normalize_speaker_selection(
    raw: Any, *, allowed_ids: Iterable[str] = BUILTIN_SPEAKERS
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("speaker selection must be an object")
    version = raw.get("version", SELECTION_VERSION)
    revision = raw.get("revision", 0)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("speaker selection version and revision must be integers")
    if version != SELECTION_VERSION:
        raise ValueError(f"unsupported speaker selection version: {version}")
    selected = normalize_speaker_id(raw.get("selected", DEFAULT_SPEAKER_ID))
    if selected not in set(allowed_ids):
        raise ValueError(f"unknown speaker profile: {selected}")
    return {
        "version": SELECTION_VERSION,
        "revision": revision,
        "selected": selected,
    }


def read_speaker_selection(
    path: Path, *, allowed_ids: Iterable[str] = BUILTIN_SPEAKERS
) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_speaker_selection()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read speaker selection: {exc}") from exc
    return normalize_speaker_selection(raw, allowed_ids=allowed_ids)


def speaker_selection_lock(path: Path):
    return exclusive_file_lock(path.with_name(f"{path.name}.lock"))


def audio_control_lock(path: Path):
    """Serialize config transitions with every master volume/mute writer."""
    return exclusive_file_lock(Path(path))


# ---------------------------------------------------------------------------
# Audio readiness
#
# Readiness is a property of one CamillaDSP *instance*, not of the boot.  A
# token that only proves "some verified transition happened at some point since
# boot" still authorizes unmuting an engine that restarted underneath it and is
# now running an unverified graph.  So readiness is bound to an engine
# generation: a random id the source switcher mints for every CamillaDSP
# connection it makes, stamped into the live engine's own config description and
# recorded in the token.
#
# CamillaDSP 4.1.3 exposes no process/instance identifier over its websocket
# (GetVersion is the build, not the run), so the generation is synthesized here
# and rotated on every (re)connection.  Stamping it into the *active* config
# rather than the config file is what makes it instance-scoped: an engine that
# restarts, reloads from disk, or has its config replaced by anyone else comes
# back without the marker, so every consumer sees the mismatch on its own live
# client without needing to observe the restart.
# ---------------------------------------------------------------------------

AUDIO_READY_VERSION = 2
ENGINE_MARKER_PREFIX = "cdsp-audio-ready:"
_ENGINE_GENERATION_RE = re.compile(r"[0-9a-f]{32}")
_ENGINE_MARKER_RE = re.compile(
    rf"^{re.escape(ENGINE_MARKER_PREFIX)}([0-9a-f]{{32}})$", re.MULTILINE
)


def new_engine_generation() -> str:
    """Mint the readiness generation for one CamillaDSP connection."""
    return secrets.token_hex(16)


def engine_generation_marker(generation: str) -> str:
    return f"{ENGINE_MARKER_PREFIX}{generation}"


def description_without_marker(description: Any) -> str:
    """Return an engine config description with any marker line removed."""
    if not isinstance(description, str):
        return ""
    return "\n".join(
        line
        for line in description.splitlines()
        if not line.startswith(ENGINE_MARKER_PREFIX)
    ).strip("\n")


def description_with_marker(description: Any, generation: str) -> str:
    base = description_without_marker(description)
    marker = engine_generation_marker(generation)
    return f"{base}\n{marker}" if base else marker


def engine_generation_from_description(description: Any) -> str | None:
    if not isinstance(description, str):
        return None
    match = _ENGINE_MARKER_RE.search(description)
    return match.group(1) if match else None


def live_engine_generation(client: Any) -> str | None:
    """Read the generation the live engine instance carries, if any.

    One cheap ``GetConfigDescription`` round-trip on the client the caller
    already holds.  Any failure reads as "no generation", which inhibits.
    """
    try:
        return engine_generation_from_description(client.config.description())
    except Exception:
        return None


def _await_engine_generation(
    client: Any, generation: str, *, timeout: float, poll_interval: float
) -> bool:
    """Poll until the live engine carries ``generation``, or time runs out."""
    poll_interval = max(poll_interval, 0.01)
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if live_engine_generation(client) == generation:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def stamp_engine_generation(
    client: Any,
    generation: str,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.25,
) -> None:
    """Mark the live engine instance as verified for this generation.

    SetConfigValue and SetConfig only queue the change, so each write is
    followed by a bounded wait for the marker rather than one immediate read;
    a merely slow engine must not provoke a second, whole-config write.
    """
    if not generation:
        raise RuntimeError("cannot stamp an empty engine generation")
    marked = description_with_marker(client.config.description(), generation)
    try:
        client.config.set_value("/description", marked)
    except Exception:
        pass
    else:
        if _await_engine_generation(
            client, generation, timeout=timeout, poll_interval=poll_interval
        ):
            return
    # SetConfigValue was rejected, or accepted but never took.  Fall back to a
    # whole-config write; the switcher is the only live-config writer, so this
    # is safe.
    config = client.config.active()
    if not config:
        raise RuntimeError("CamillaDSP has no active config to mark ready")
    marked_config = dict(config)
    marked_config["description"] = marked
    client.config.set_active(marked_config)
    if not _await_engine_generation(
        client, generation, timeout=timeout, poll_interval=poll_interval
    ):
        raise RuntimeError("CamillaDSP did not retain the audio-ready marker")


def read_audio_ready_token(path: Path) -> dict[str, Any] | None:
    """Parse the ready token; anything unknown or malformed reads as absent."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != AUDIO_READY_VERSION:
        return None
    generation = raw.get("engine_generation")
    if not isinstance(generation, str) or not _ENGINE_GENERATION_RE.fullmatch(
        generation
    ):
        return None
    return raw


def audio_ready_generation(path: Path) -> str | None:
    token = read_audio_ready_token(path)
    return token["engine_generation"] if token else None


def audio_inhibit_active(
    path: Path, client: Any, *, generation: str | None = None
) -> bool:
    """Fail closed unless the token names the generation the engine carries.

    ``generation`` is the caller's own current generation; only the source
    switcher has one, and passing it additionally rejects a token minted by a
    previous switcher run against a still-running engine.
    """
    stored = audio_ready_generation(path)
    if stored is None:
        return True
    if generation is not None and stored != generation:
        return True
    return live_engine_generation(client) != stored


def set_audio_inhibit(path: Path) -> None:
    """Drop the ready token; absence alone is what inhibits unmuting."""
    Path(path).unlink(missing_ok=True)


def clear_audio_inhibit(
    path: Path, *, generation: str, applied: dict[str, Any] | None = None
) -> None:
    """Publish readiness for one engine generation and applied config."""
    if not isinstance(generation, str) or not _ENGINE_GENERATION_RE.fullmatch(
        generation
    ):
        raise ValueError("a ready token needs the current engine generation")
    payload: dict[str, Any] = {
        "version": AUDIO_READY_VERSION,
        "engine_generation": generation,
        "updated_at": time.time(),
    }
    if applied:
        payload.update(applied)
    atomic_write_json(Path(path), payload)


def require_audio_unmute_allowed(path: Path, client: Any) -> None:
    if audio_inhibit_active(path, client):
        raise RuntimeError("audio output is inhibited until a verified config is active")


# ---------------------------------------------------------------------------
# Listener mute requests during a transition
#
# While readiness is dropped the switcher holds the listener's mute state from
# before its own safety mute, and restores it once a config is verified -- which
# may be several retries later.  A control that mutes in between only changes the
# engine's flag, which the switcher's own mute already set, so the request would
# be lost and the restore would unmute over it.  The engine flag cannot carry the
# distinction; this file does.
#
# Every write and read happens under the audio-control lock.  The switcher
# discards the file whenever it captures the live mute state (the capture already
# includes any earlier request) and takes it at the moment it restores, so only a
# request made after the capture can survive to the restore.
# ---------------------------------------------------------------------------


def mute_request_path(ready_path: Path) -> Path:
    return Path(ready_path).with_name("mute-request.json")


def note_mute_request(ready_path: Path) -> None:
    """Record an explicit listener mute accepted while readiness is dropped.

    Only needed while the ready token is absent: that is the only time the
    switcher holds a mute state to restore.  A missing runtime directory means
    no switcher is running, so nothing holds one either.
    """
    if audio_ready_generation(ready_path) is not None:
        return
    path = mute_request_path(ready_path)
    if not path.parent.is_dir():
        return
    atomic_write_json(path, {"version": 1, "muted": True, "requested_at": time.time()})


def discard_mute_request(ready_path: Path) -> None:
    mute_request_path(ready_path).unlink(missing_ok=True)


def take_mute_request(ready_path: Path) -> bool:
    """Consume a pending listener mute request; True if there was one."""
    try:
        mute_request_path(ready_path).unlink()
    except FileNotFoundError:
        return False
    return True


def update_speaker_selection(
    path: Path,
    speaker_id: str,
    *,
    expected_revision: int | None = None,
    allowed_ids: Iterable[str] = BUILTIN_SPEAKERS,
    before_commit: Callable[[dict[str, Any]], None] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Commit a selection change; ``force`` re-commits the same speaker.

    A forced same-speaker commit bumps the revision so the switcher daemon
    re-resolves and re-applies the profile — the transactional path for
    "the active profile's crossover definition was edited".
    """
    selected = normalize_speaker_id(speaker_id)
    allowed = set(allowed_ids)
    if selected not in allowed:
        raise ValueError(f"speaker profile {selected!r} is not available")
    with speaker_selection_lock(path):
        current = read_speaker_selection(path, allowed_ids=allowed)
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
        ):
            raise ValueError("expected revision must be an integer")
        if expected_revision is not None and expected_revision != current["revision"]:
            raise ValueError("speaker selection changed elsewhere; reload before saving")
        if selected == current["selected"] and not force:
            return current
        updated = {
            "version": SELECTION_VERSION,
            "revision": current["revision"] + 1,
            "selected": selected,
        }
        if before_commit is not None:
            before_commit(updated)
        atomic_write_json(path, updated)
        return updated


def profile_audio_path(root: Path, speaker_id: str) -> Path:
    selected = normalize_speaker_id(speaker_id)
    return root / f"{selected}.json"


def resolve_profile_audio_path(
    root: Path,
    speaker_id: str,
    *,
    legacy_path: Path | None = None,
) -> Path:
    """Return the authoritative path without performing a racy lazy copy.

    The default speaker keeps reading ``legacy_path`` for as long as it has no
    per-speaker file of its own; there is no migration step, because a lock
    cannot stop an old writer that is already waiting on it from writing again.
    """
    selected = normalize_speaker_id(speaker_id)
    target = profile_audio_path(root, selected)
    if target.exists():
        return target
    if (
        selected == LEGACY_SPEAKER_ID
        and legacy_path is not None
        and legacy_path.exists()
    ):
        return legacy_path
    return target


def read_profile_audio_state(
    root: Path,
    speaker_id: str,
    *,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """Read one speaker's EQ state, retaining the default speaker's legacy path."""
    target = resolve_profile_audio_path(root, speaker_id, legacy_path=legacy_path)
    if target.exists():
        return read_audio_state(target)

    with audio_state_lock(target):
        if target.exists():
            return read_audio_state(target)
        state = default_audio_state()
        atomic_write_json(target, state)
        return state


# ====================== VERIFIED VOLUME CEILING ======================

# Every control surface derives its ceiling from the speaker-profile status
# the switcher publishes after a verified apply. When that answer is missing,
# stale or unparseable the surfaces must not fall back to 0 dB: an unknown
# profile may be a capped one, so the fail-closed default is the most
# restrictive ceiling that still leaves the system usable.
FAILSAFE_VOLUME_LIMIT_DB = -20.0
# Same bounds CamillaDSP accepts for devices.volume_limit.
VOLUME_LIMIT_MIN_DB = -150.0
VOLUME_LIMIT_MAX_DB = 50.0


def normalize_volume_limit(value: Any) -> float | None:
    """Coerce a stored ceiling, or ``None`` when it is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if not VOLUME_LIMIT_MIN_DB <= numeric <= VOLUME_LIMIT_MAX_DB:
        return None
    return numeric


def read_effective_volume_limit(
    path: Path, *, fallback: float = FAILSAFE_VOLUME_LIMIT_DB
) -> float:
    """Ceiling of the profile that is verified-applied right now.

    Anything short of a successful apply that recorded its ceiling -- no
    status file, unreadable JSON, ``ok`` not true, a missing or malformed
    ``volume_limit_db`` -- returns ``fallback`` rather than a permissive 0 dB.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        return fallback
    limit = normalize_volume_limit(raw.get("volume_limit_db"))
    return fallback if limit is None else limit


def volume_ceiling(
    path: Path,
    *,
    override: float | None = None,
    fallback: float = FAILSAFE_VOLUME_LIMIT_DB,
) -> float:
    """The applied profile's ceiling, optionally tightened by a local override.

    ``override`` is a deployment's own preference (an env var on one control
    surface). It can only ever further restrict: it must not be able to lift a
    speaker profile's cap.
    """
    limit = read_effective_volume_limit(path, fallback=fallback)
    if override is None:
        return limit
    return min(limit, float(override))
