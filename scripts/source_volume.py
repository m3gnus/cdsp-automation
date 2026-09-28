#!/usr/bin/env python3
"""Per-source volume memory: the level each source was last played at.

Every speaker/source pair remembers two levels: the CamillaDSP Main fader
and the MOTU main output.  The source switcher records the outgoing pair's
levels inside a muted transition and starts the incoming pair at the levels it
remembers, so the TV on TOSLINK comes back at the TV's level and AirPlay at
AirPlay's.  While a source plays, the switcher also records any change every
few seconds, so a restart or power cut loses at most that much.

The memory never raises anything past a ceiling: the switcher still clamps
the CamillaDSP level to the applied profile's ``volume_limit`` and the MOTU
level to MOTU_MAIN_VOLUME_MAX_DB, exactly as it does for any other writer.
A pair seen for the first time starts at whatever was playing before.

The control UI edits the remembered levels of sources that are not playing,
which is how a site sets "the TV always starts at -30 dB".  Both writers take
the file's lock.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from audio_eq import atomic_write_json, audio_state_lock
import settings


STATE_VERSION = 1
# Written by the switcher (install user) and the control UI (root, with the
# install group); group-writable like the other shared state.
STATE_FILE_MODE = 0o660
# The widest band either level can hold.  The ceilings are applied on use.
CDSP_MIN_DB = -150.0
CDSP_MAX_DB = 0.0
MOTU_MIN_DB = -100.0
MOTU_MAX_DB = 0.0
# Read on each call, so a test (or a caller) can point it elsewhere.
STATE_PATH = settings.SOURCE_VOLUME_PATH
LEVEL_KEYS = {"cdsp_db": (CDSP_MIN_DB, CDSP_MAX_DB), "motu_db": (MOTU_MIN_DB, MOTU_MAX_DB)}


def enabled() -> bool:
    """SOURCE_VOLUME_MEMORY in the env file; on unless set to 0/false/off."""
    raw = os.environ.get("SOURCE_VOLUME_MEMORY", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _level(value: Any, key: str) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    low, high = LEVEL_KEYS[key]
    return round(max(low, min(high, numeric)), 2)


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 64 or not all(c.isalnum() or c in "-_" for c in value):
        return None
    return value


def normalize_state(raw: Any) -> dict[str, Any]:
    """A clean state document; anything malformed inside it is dropped."""
    speakers: dict[str, dict[str, dict[str, float]]] = {}
    if isinstance(raw, dict) and isinstance(raw.get("speakers"), dict):
        for speaker, sources in raw["speakers"].items():
            speaker = _name(speaker)
            if speaker is None or not isinstance(sources, dict):
                continue
            for source, levels in sources.items():
                source = _name(source)
                if source is None or not isinstance(levels, dict):
                    continue
                clean = {
                    key: level
                    for key in LEVEL_KEYS
                    if (level := _level(levels.get(key), key)) is not None
                }
                if clean:
                    speakers.setdefault(speaker, {})[source] = clean
    return {"version": STATE_VERSION, "speakers": speakers}


def read_state(path: Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    return normalize_state(raw)


def remembered(
    speaker: str, source: str, path: Path | None = None
) -> dict[str, float]:
    """The levels remembered for ``speaker``/``source`` (possibly empty)."""
    return dict(read_state(path)["speakers"].get(speaker, {}).get(source, {}))


def remember(
    speaker: str,
    source: str,
    *,
    cdsp_db: float | None = None,
    motu_db: float | None = None,
    path: Path | None = None,
) -> bool:
    """Record the levels given (None leaves one alone); True if anything changed.

    Unchanged levels are not rewritten, so recording every few seconds costs
    the SD card nothing while nobody touches a control.
    """
    speaker_name, source_name = _name(speaker), _name(source)
    if speaker_name is None or source_name is None:
        raise ValueError(f"invalid speaker/source: {speaker!r}/{source!r}")
    updates = {
        key: level
        for key, value in (("cdsp_db", cdsp_db), ("motu_db", motu_db))
        if value is not None and (level := _level(value, key)) is not None
    }
    if not updates:
        return False
    path = Path(path or STATE_PATH)
    with audio_state_lock(path):
        state = read_state(path)
        entry = state["speakers"].setdefault(speaker_name, {}).setdefault(source_name, {})
        if all(entry.get(key) == value for key, value in updates.items()):
            return False
        entry.update(updates)
        atomic_write_json(path, state, mode=STATE_FILE_MODE)
    return True


def forget(speaker: str, source: str, path: Path | None = None) -> bool:
    """Drop what ``speaker``/``source`` remembers; True if there was anything."""
    path = Path(path or STATE_PATH)
    with audio_state_lock(path):
        state = read_state(path)
        sources = state["speakers"].get(speaker, {})
        if source not in sources:
            return False
        del sources[source]
        if not sources:
            del state["speakers"][speaker]
        atomic_write_json(path, state, mode=STATE_FILE_MODE)
    return True
