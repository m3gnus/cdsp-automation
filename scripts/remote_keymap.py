#!/usr/bin/env python3
"""The HID remote's key map: which button does what, and when.

The map lives in REMOTE_KEYMAP_PATH (/etc/cdsp-automation/remote-keymap.json).
Without that file the remote behaves exactly as it always has
(``DEFAULT_KEYMAP``).  An unreadable or invalid file is reported and the
defaults stay in force, so a typo never leaves the remote dead.

Each key (an evdev name such as ``KEY_VOLUMEUP``; ``cdsp_remote.py --learn``
prints the names a remote sends) can carry up to three actions:

* ``press``     - on a short press.  With ``repeat`` it also repeats while the
                  key is held (volume keys).
* ``hold``      - once the key has been held ``hold_seconds``.
* ``long_hold`` - once the key has been held ``long_hold_seconds``.  A key
                  with a long hold fires its ``hold`` action on release, so
                  holding on towards the long hold never triggers both.

A key with a hold fires its ``press`` action on release, and only when it
was released before the hold.  The power actions (``restart_services``,
``shutdown``) may only sit on a hold or long hold, never a press: a
bumped remote must not take the system down.

Speaker-profile changes are deliberately not an action.  A profile change
replaces the routing and crossover and needs the matching passive speakers
connected; the control UI asks for an explicit confirmation for that reason,
and a button cannot.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCES = ("streamer", "gadget", "toslink", "analog")

ACTIONS = frozenset(
    {
        "volume_up",
        "volume_down",
        "mute",
        "bass_up",
        "bass_down",
        "treble_up",
        "treble_down",
        "tone_reset",
        "status",
        "restart_services",
        "shutdown",
        "next_source",
        "source_auto",
        "amps_off",
        "motu_volume_up",
        "motu_volume_down",
    }
    | {f"source_{name}" for name in SOURCES}
)
HOLD_ONLY_ACTIONS = frozenset({"restart_services", "shutdown"})
SLOTS = ("press", "hold", "long_hold")

DEFAULT_KEYMAP: dict[str, Any] = {
    "volume_step_db": 1.0,
    "tone_step_db": 0.5,
    "motu_step_db": 1.0,
    "hold_seconds": 1.0,
    "long_hold_seconds": 10.0,
    "keys": {
        "KEY_VOLUMEUP": {"press": "volume_up", "repeat": True},
        "KEY_VOLUMEDOWN": {"press": "volume_down", "repeat": True},
        "KEY_MUTE": {"press": "mute"},
        "KEY_UP": {"press": "treble_up"},
        "KEY_DOWN": {"press": "treble_down"},
        "KEY_RIGHT": {"press": "bass_up"},
        "KEY_LEFT": {"press": "bass_down"},
        "KEY_ENTER": {"press": "status", "hold": "tone_reset"},
        "KEY_POWER": {"hold": "restart_services", "long_hold": "shutdown"},
    },
}

# Bounds for the numeric settings: generous, but no step that could jump the
# volume by more than a few dB per press, and no hold timing that makes a
# hold indistinguishable from a press or impossible to reach.
STEP_BOUNDS = {
    "volume_step_db": (0.1, 6.0),
    "tone_step_db": (0.1, 3.0),
    "motu_step_db": (1.0, 6.0),
}
HOLD_BOUNDS = (0.3, 30.0)


class KeymapError(ValueError):
    """A key map that cannot be used, with the reason."""


@dataclass(frozen=True)
class KeyBinding:
    press: str | None = None
    hold: str | None = None
    long_hold: str | None = None
    repeat: bool = False
    hold_seconds: float = 1.0
    long_hold_seconds: float = 10.0


@dataclass(frozen=True)
class Keymap:
    keys: dict[str, KeyBinding]
    volume_step_db: float = 1.0
    tone_step_db: float = 0.5
    motu_step_db: float = 1.0
    source: str = "built-in defaults"


def _number(raw: Any, name: str, bounds: tuple[float, float]) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise KeymapError(f"{name} must be a number")
    value = float(raw)
    if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
        raise KeymapError(f"{name} must be between {bounds[0]:g} and {bounds[1]:g}")
    return value


def parse_keymap(document: Any, source: str = "built-in defaults") -> Keymap:
    """Validate a key map document; raises KeymapError naming the problem."""
    if not isinstance(document, dict):
        raise KeymapError("the key map must be a JSON object")
    unknown = set(document) - set(DEFAULT_KEYMAP)
    if unknown:
        raise KeymapError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    settings = {
        name: _number(document.get(name, DEFAULT_KEYMAP[name]), name, bounds)
        for name, bounds in STEP_BOUNDS.items()
    }
    hold = _number(
        document.get("hold_seconds", DEFAULT_KEYMAP["hold_seconds"]),
        "hold_seconds",
        HOLD_BOUNDS,
    )
    long_hold = _number(
        document.get("long_hold_seconds", DEFAULT_KEYMAP["long_hold_seconds"]),
        "long_hold_seconds",
        HOLD_BOUNDS,
    )
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise KeymapError("keys must be a non-empty object")
    keys: dict[str, KeyBinding] = {}
    for key, entry in raw_keys.items():
        if not isinstance(key, str) or not key.startswith(("KEY_", "BTN_")):
            raise KeymapError(f"{key!r} is not an evdev key name (KEY_... or BTN_...)")
        if not isinstance(entry, dict):
            raise KeymapError(f"{key}: must be an object")
        extra = set(entry) - {*SLOTS, "repeat", "hold_seconds", "long_hold_seconds"}
        if extra:
            raise KeymapError(f"{key}: unknown field(s): {', '.join(sorted(extra))}")
        actions: dict[str, str | None] = {}
        for slot in SLOTS:
            action = entry.get(slot)
            if action is None:
                actions[slot] = None
                continue
            if action not in ACTIONS:
                raise KeymapError(f"{key}: unknown action {action!r}")
            if slot == "press" and action in HOLD_ONLY_ACTIONS:
                raise KeymapError(f"{key}: {action} may only be a hold or long_hold")
            actions[slot] = action
        if not any(actions.values()):
            raise KeymapError(f"{key}: needs at least one action")
        repeat = entry.get("repeat", False)
        if not isinstance(repeat, bool):
            raise KeymapError(f"{key}: repeat must be true or false")
        if repeat and (actions["hold"] or actions["long_hold"]):
            raise KeymapError(f"{key}: a repeating key cannot also have a hold")
        key_hold = _number(entry.get("hold_seconds", hold), f"{key}.hold_seconds", HOLD_BOUNDS)
        key_long = _number(
            entry.get("long_hold_seconds", long_hold), f"{key}.long_hold_seconds", HOLD_BOUNDS
        )
        if actions["long_hold"] and actions["hold"] and key_long <= key_hold:
            raise KeymapError(f"{key}: long_hold_seconds must exceed hold_seconds")
        keys[key] = KeyBinding(
            press=actions["press"],
            hold=actions["hold"],
            long_hold=actions["long_hold"],
            repeat=repeat,
            hold_seconds=key_hold,
            long_hold_seconds=key_long,
        )
    return Keymap(keys=keys, source=source, **settings)


def default_keymap() -> Keymap:
    return parse_keymap(copy.deepcopy(DEFAULT_KEYMAP))


def load_keymap(path: Path) -> tuple[Keymap, str | None]:
    """The key map at ``path``, or the defaults and the reason they are used.

    A missing file is the normal case and has no reason.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_keymap(), None
    except OSError as exc:
        return default_keymap(), f"cannot read {path}: {exc}"
    try:
        return parse_keymap(json.loads(text), source=str(path)), None
    except (ValueError, KeymapError) as exc:
        return default_keymap(), f"{path} is invalid ({exc}); using the defaults"


# ------------------------------------------------------------------ dispatch


@dataclass
class KeyDispatcher:
    """Turn key events into actions, following a Keymap's press/hold rules.

    ``event(key, state, now)`` takes an evdev key name, its state (1 press,
    2 auto-repeat, 0 release) and a monotonic time, and returns the actions
    to run now, in order.  Pure, so it is tested without a remote.
    """

    keymap: Keymap
    _down: dict[str, float] = field(default_factory=dict)
    _fired: dict[str, set[str]] = field(default_factory=dict)
    _repeats: dict[str, int] = field(default_factory=dict)

    def event(self, key: str | list[str], state: int, now: float) -> list[str]:
        names = key if isinstance(key, list) else [key]
        name = next((n for n in names if n in self.keymap.keys), None)
        if name is None:
            return []
        binding = self.keymap.keys[name]
        if state == 1:
            return self._pressed(name, binding, now)
        if state == 2:
            return self._held(name, binding, now)
        if state == 0:
            return self._released(name, binding, now)
        return []

    def _pressed(self, name: str, binding: KeyBinding, now: float) -> list[str]:
        self._down[name] = now
        self._fired[name] = set()
        self._repeats[name] = 0
        if binding.press and not (binding.hold or binding.long_hold):
            return [binding.press]
        return []

    def _held(self, name: str, binding: KeyBinding, now: float) -> list[str]:
        started = self._down.setdefault(name, now)
        fired = self._fired.setdefault(name, set())
        if binding.repeat and binding.press:
            # Every second auto-repeat, as the remote always stepped volume.
            self._repeats[name] = self._repeats.get(name, 0) + 1
            if self._repeats[name] >= 2:
                self._repeats[name] = 0
                return [binding.press]
            return []
        held = now - started
        if binding.long_hold and held >= binding.long_hold_seconds and "long_hold" not in fired:
            fired.add("long_hold")
            return [binding.long_hold]
        if (
            binding.hold
            and not binding.long_hold
            and held >= binding.hold_seconds
            and "hold" not in fired
        ):
            fired.add("hold")
            return [binding.hold]
        return []

    def _released(self, name: str, binding: KeyBinding, now: float) -> list[str]:
        started = self._down.pop(name, now)
        fired = self._fired.pop(name, set())
        self._repeats.pop(name, None)
        if not (binding.hold or binding.long_hold) or fired:
            return []
        held = now - started
        if binding.long_hold and held >= binding.long_hold_seconds:
            return [binding.long_hold]
        if binding.hold and held >= binding.hold_seconds:
            return [binding.hold]
        if binding.press and held < binding.hold_seconds:
            return [binding.press]
        return []


def keymap_document(keymap: Keymap) -> dict[str, Any]:
    """A key map as the JSON document that would load it (for --print-keymap)."""
    keys: dict[str, Any] = {}
    for name, binding in keymap.keys.items():
        entry: dict[str, Any] = {
            slot: getattr(binding, slot) for slot in SLOTS if getattr(binding, slot)
        }
        if binding.repeat:
            entry["repeat"] = True
        if binding.hold and binding.hold_seconds != DEFAULT_KEYMAP["hold_seconds"]:
            entry["hold_seconds"] = binding.hold_seconds
        if (
            binding.long_hold
            and binding.long_hold_seconds != DEFAULT_KEYMAP["long_hold_seconds"]
        ):
            entry["long_hold_seconds"] = binding.long_hold_seconds
        keys[name] = entry
    return {
        "volume_step_db": keymap.volume_step_db,
        "tone_step_db": keymap.tone_step_db,
        "motu_step_db": keymap.motu_step_db,
        "hold_seconds": DEFAULT_KEYMAP["hold_seconds"],
        "long_hold_seconds": DEFAULT_KEYMAP["long_hold_seconds"],
        "keys": keys,
    }
