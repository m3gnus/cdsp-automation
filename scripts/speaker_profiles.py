"""Persistent speaker selection and per-speaker audio-state helpers.

The physical source and speaker are independent choices.  This module owns the
small persistent part of the speaker-profile system; CamillaDSP composition is
kept in the source switcher so it remains the only live-config writer.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

from audio_eq import (
    atomic_write_json,
    audio_state_lock,
    default_audio_state,
    read_audio_state,
)


SELECTION_VERSION = 1
DEFAULT_SPEAKER_ID = "kantarellen"
# These profiles are complete operator-owned CamillaDSP files in the normal
# CamillaGUI config directory. They are intentionally editable in CamillaGUI;
# the source switcher validates and loads the file directly instead of
# generating an immutable config from the compact crossover schema.
OPERATOR_CONFIG_SPEAKERS: dict[str, str] = {
    "partymeh": "partymeh.yml",
}
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
    "partymeh_bird": {
        "label": "PartyMEH + Bird",
        "description": "PartyMEH on outputs 1–6 and Bird on outputs 7–8",
    },
}


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
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_speaker_selection()
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read speaker selection: {exc}") from exc
    return normalize_speaker_selection(raw, allowed_ids=allowed_ids)


@contextmanager
def speaker_selection_lock(path: Path):
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


@contextmanager
def audio_control_lock(path: Path):
    """Serialize config transitions with every master volume/mute writer."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, 0o660)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
        selected == DEFAULT_SPEAKER_ID
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
    selected = normalize_speaker_id(speaker_id)
    target = resolve_profile_audio_path(
        root, selected, legacy_path=legacy_path
    )
    if target.exists():
        return read_audio_state(target)

    with audio_state_lock(target):
        if target.exists():
            return read_audio_state(target)
        state = default_audio_state()
        atomic_write_json(target, state)
        return state


def migrate_legacy_profile_audio_state(
    root: Path,
    legacy_path: Path,
    *,
    legacy_writers_quiesced: bool = False,
) -> Path:
    """Copy legacy Kantarellen state after all old-path writers are stopped.

    A lock alone cannot prevent an old process that is waiting on the lock from
    writing immediately after migration.  Requiring an explicit quiesced
    cutover makes that deployment precondition visible and testable.
    """
    if not legacy_writers_quiesced:
        raise RuntimeError("legacy audio writers must be stopped before migration")
    target = profile_audio_path(root, DEFAULT_SPEAKER_ID)
    with audio_state_lock(legacy_path):
        with audio_state_lock(target):
            if target.exists():
                return target
            state = read_audio_state(legacy_path)
            atomic_write_json(target, state)
    return target
