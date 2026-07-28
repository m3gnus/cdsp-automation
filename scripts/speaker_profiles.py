"""Persistent speaker selection and per-speaker audio-state helpers.

The physical source and speaker are independent choices.  This module owns the
small persistent part of the speaker-profile system; CamillaDSP composition is
kept in the source switcher so it remains the only live-config writer.
"""

from __future__ import annotations

import json
import os
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
    return exclusive_file_lock(path.with_name(f"{path.name}.lock"), 0o644)


def audio_control_lock(path: Path):
    """Serialize config transitions with every master volume/mute writer."""
    return exclusive_file_lock(Path(path), 0o660)


def audio_inhibit_active(path: Path) -> bool:
    """A missing boot-scoped ready token is the fail-closed default."""
    return not Path(path).is_file()


def set_audio_inhibit(path: Path, payload: dict[str, Any]) -> None:
    del payload
    Path(path).unlink(missing_ok=True)


def clear_audio_inhibit(path: Path) -> None:
    atomic_write_json(Path(path), {"ready": True})


def require_audio_unmute_allowed(path: Path) -> None:
    if audio_inhibit_active(path):
        raise RuntimeError("audio output is inhibited until a verified config is active")


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

    Kantarellen deliberately keeps using the legacy file until deployment has
    restarted every legacy writer and performs an explicit quiesced migration.
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
    """Read one speaker's EQ state, retaining the legacy Kantarellen path."""
    target = resolve_profile_audio_path(root, speaker_id, legacy_path=legacy_path)
    if target.exists():
        return read_audio_state(target)

    with audio_state_lock(target):
        if target.exists():
            return read_audio_state(target)
        state = default_audio_state()
        atomic_write_json(target, state)
        return state
