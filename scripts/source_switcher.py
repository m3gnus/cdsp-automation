#!/usr/bin/env python3
"""Automatic CamillaDSP config switching by active source."""

from __future__ import annotations

import copy
import glob
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

try:
    import websocket
except ImportError:
    websocket = None

import yaml
from camilladsp import CamillaClient
from audio_eq import (
    FILTER_PREFIX,
    PIPELINE_DESCRIPTION,
    apply_audio_overlay,
    atomic_write_json,
    effective_preamp_db,
    status_payload,
)
from motu_access import AccessUnavailable, MotuAccess

try:
    import clock_sync
except ImportError:  # websocket-client missing: no MOTU control at all
    clock_sync = None
from speaker_config import (
    compile_profile_config,
    config_digest,
    config_volume_limit,
    identify_managed_config,
    load_profile,
    load_yaml_mapping,
    prune_generated_configs,
    profile_catalog,
    require_config_volume_limit,
    write_generated_config,
)
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    DEFAULT_SPEAKER_ID,
    audio_control_lock,
    audio_inhibit_active,
    clear_audio_inhibit,
    new_engine_generation,
    normalize_volume_limit,
    read_profile_audio_state,
    read_speaker_selection,
    set_audio_inhibit,
    speaker_selection_lock,
    stamp_engine_generation,
    operator_config_for_source,
)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("SOURCE_CHECK_INTERVAL", "1.0"))
# Longest span one arbitration pass may account for (see main()).
MAX_ARBITRATION_STEP = 5 * CHECK_INTERVAL
# How long a source whose playback has been *confirmed* is held through
# silence before the switcher looks elsewhere.  This is the track-gap grace
# period and its meaning is unchanged.
IDLE_TIMEOUT = float(os.environ.get("SOURCE_IDLE_TIMEOUT", "60"))
# How far into that grace period a *lower*-priority source with confirmed
# audio may cut the hold short.  Meaning unchanged, and it governs only the
# lower-priority direction: an operator who raises it is saying "do not let
# the TV steal my AirPlay track gap", which must not be read as "do not let
# AirPlay interrupt a TV that has stopped".
LOWER_PRIORITY_ACTIVE_TIMEOUT = float(
    os.environ.get("SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT", "0")
)
# How long a rival must have been *continuously confirmed playing* before it
# may cut a silent source's grace short.  Real audio should not wait, but one
# noisy meter frame should not yank the config away mid-track either, so a
# rival has to hold the observation for a couple of passes first.
PREEMPT_DWELL_SECONDS = max(
    float(os.environ.get("SOURCE_PREEMPT_DWELL_SECONDS", "2")), 0.0
)
# How long an *unconfirmed* source is listened to after the switcher probes it
# by selecting it.  Hardware readiness (an open ALSA Loopback stream, a USB
# gadget with a non-zero capture rate, a meter above its floor) says a stream
# exists, not that anything is playing through it, and the only way to find
# out is to select the source and read the capture levels.  A probe that hears
# nothing must give up quickly - IDLE_TIMEOUT is the wrong yardstick here,
# because nothing was ever playing to leave a gap in.
PROBE_SILENCE_TIMEOUT = float(os.environ.get("SOURCE_PROBE_SILENCE_TIMEOUT", "5"))
# A source probed and found silent is not re-probed until this backoff expires,
# growing by PROBE_BACKOFF_FACTOR per consecutive silent probe up to
# PROBE_BACKOFF_MAX.  Without it, hardware readiness alone makes a silent
# source eligible again on the very next pass, and two ready-but-silent inputs
# alternate forever - one config reload plus a mute/restore per cycle.
PROBE_BACKOFF_SECONDS = max(
    float(os.environ.get("SOURCE_PROBE_BACKOFF_SECONDS", "30")), 0.0
)
PROBE_BACKOFF_FACTOR = max(
    float(os.environ.get("SOURCE_PROBE_BACKOFF_FACTOR", "4")), 1.0
)
PROBE_BACKOFF_MAX = max(
    float(os.environ.get("SOURCE_PROBE_BACKOFF_MAX", "900")),
    PROBE_BACKOFF_SECONDS,
)
SETTLE_TIME = float(os.environ.get("SOURCE_SETTLE_TIME", "2.0"))
# SetConfig/Reload acknowledge that a change was queued, not that the
# processing controller finished applying it. SETTLE_TIME remains the grace
# period before the first state read; the read-back of the applied config is
# then polled until it converges or this bounded deadline expires.
CONFIG_APPLY_TIMEOUT = float(os.environ.get("SOURCE_CONFIG_APPLY_TIMEOUT", "10.0"))
CONFIG_APPLY_POLL_INTERVAL = float(
    os.environ.get("SOURCE_CONFIG_APPLY_POLL_INTERVAL", "0.25")
)
AUDIO_THRESHOLD_DB = float(os.environ.get("SOURCE_AUDIO_THRESHOLD_DB", "-80"))
DEBUG_MODE = env_bool("SOURCE_DEBUG", False)
MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
# Whether the switcher changes the MOTU clock itself, inside the muted
# transition.  "auto" follows the MOTU Clock Sync install: that unit's
# presence is what says this rig's clock is ours to drive.
SOURCE_MOTU_CLOCK = os.environ.get("SOURCE_MOTU_CLOCK", "auto").strip().lower()
MOTU_CLOCK_UNIT_PATH = Path(
    os.environ.get(
        "MOTU_CLOCK_UNIT_PATH", "/etc/systemd/system/cdsp-motu-sync.service"
    )
)
# How long the MOTU gets to re-lock before the new graph is loaded on it.
MOTU_CLOCK_SETTLE_SECONDS = float(os.environ.get("MOTU_CLOCK_SETTLE_SECONDS", "1.0"))
TOSLINK_MOTU_METERS = env_bool("SOURCE_TOSLINK_MOTU_METERS", True)
ANALOG_MOTU_METERS = env_bool("SOURCE_ANALOG_MOTU_METERS", False)
MOTU_METER_ACTIVE_BELOW = int(os.environ.get("SOURCE_MOTU_METER_ACTIVE_BELOW", "250"))
MOTU_METER_MAX_AGE = float(os.environ.get("SOURCE_MOTU_METER_MAX_AGE", "2.0"))
MOTU_CONNECT_RETRY_SECONDS = float(
    os.environ.get("SOURCE_MOTU_CONNECT_RETRY_SECONDS", "10")
)
MOTU_READ_WINDOW_SECONDS = float(
    os.environ.get("SOURCE_MOTU_READ_WINDOW_SECONDS", "0.2")
)
TOSLINK_ACTIVE_SECONDS = float(os.environ.get("SOURCE_TOSLINK_ACTIVE_SECONDS", "0.5"))
TOSLINK_IDLE_SECONDS = float(os.environ.get("SOURCE_TOSLINK_IDLE_SECONDS", "5"))
ANALOG_ACTIVE_SECONDS = float(os.environ.get("SOURCE_ANALOG_ACTIVE_SECONDS", "5"))
ANALOG_IDLE_SECONDS = float(os.environ.get("SOURCE_ANALOG_IDLE_SECONDS", "30"))
SOURCE_IDLE_MODE = os.environ.get("SOURCE_IDLE_MODE", "keep-last").strip().lower()
RECOVERY_RETRY_SECONDS = max(
    float(os.environ.get("SOURCE_RECOVERY_RETRY_SECONDS", "10")), 1.0
)
RECOVERY_LOG_SECONDS = max(
    float(os.environ.get("SOURCE_RECOVERY_LOG_SECONDS", "30")),
    RECOVERY_RETRY_SECONDS,
)

TOSLINK_METER_PAIRS = tuple(
    int(value)
    for value in os.environ.get("SOURCE_TOSLINK_METER_PAIRS", "12,13").split(",")
    if value.strip()
)
ANALOG_METER_PAIRS = tuple(
    int(value)
    for value in os.environ.get("SOURCE_ANALOG_METER_PAIRS", "16,18").split(",")
    if value.strip()
)

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CDSP_CONFIG_DIR", os.path.join(HOME, "camilladsp/configs"))

TOSLINK_CFG = os.path.join(CONFIG_DIR, "toslink.yml")
STREAMER_CFG = os.path.join(CONFIG_DIR, "streamer.yml")
GADGET_CFG = os.path.join(CONFIG_DIR, "gadget.yml")
ANALOG_CFG = os.path.join(CONFIG_DIR, "analog.yml")
SOURCE_OVERRIDE_PATH = os.environ.get(
    "SOURCE_OVERRIDE_PATH", "/run/cdsp-source-switcher/manual_source"
)
AUDIO_EQ_PATH = os.environ.get(
    "AUDIO_EQ_PATH", "/var/lib/cdsp-automation/audio-eq.json"
)
AUDIO_EQ_STATUS_PATH = os.environ.get(
    "AUDIO_EQ_STATUS_PATH", "/run/cdsp-source-switcher/audio-eq-status.json"
)
ISO226_CAPABILITY_PATH = os.environ.get(
    "ISO226_CAPABILITY_PATH", "/var/lib/cdsp-automation/iso226-engine.json"
)
AUDIO_EQ_REAPPLY_SECONDS = float(os.environ.get("AUDIO_EQ_REAPPLY_SECONDS", "1.0"))
SPEAKER_SELECTION_PATH = Path(
    os.environ.get(
        "SPEAKER_SELECTION_PATH",
        "/var/lib/cdsp-automation/speaker-selection.json",
    )
)
SPEAKER_AUDIO_DIR = Path(
    os.environ.get("SPEAKER_AUDIO_DIR", "/var/lib/cdsp-automation/speaker-audio")
)
SPEAKER_PROFILE_DIR = Path(
    os.environ.get("SPEAKER_PROFILE_DIR", "/etc/cdsp-automation/speaker-profiles")
)
SOURCE_BASE_DIR = Path(
    os.environ.get("SOURCE_BASE_DIR", os.path.join(CONFIG_DIR, "source-bases"))
)
SPEAKER_GENERATED_DIR = Path(
    os.environ.get(
        "SPEAKER_GENERATED_DIR",
        "/var/lib/cdsp-automation/generated-configs",
    )
)
SPEAKER_STATUS_PATH = Path(
    os.environ.get(
        "SPEAKER_STATUS_PATH",
        "/run/cdsp-source-switcher/speaker-profile-status.json",
    )
)
SPEAKER_TRANSITION_PATH = Path(
    os.environ.get(
        "SPEAKER_TRANSITION_PATH",
        "/var/lib/cdsp-automation/speaker-transition.json",
    )
)
CAMILLA_BINARY = os.environ.get("CAMILLA_BINARY", "camilladsp")
CONFIG_VALIDATE_TIMEOUT = float(os.environ.get("CONFIG_VALIDATE_TIMEOUT", "10"))
# CamillaDSP's own ceiling, and therefore the ceiling for anything that
# declares no cap of its own (the default speaker's full configs).
DEFAULT_VOLUME_LIMIT_DB = 0.0
AUDIO_CONTROL_LOCK_PATH = Path(
    os.environ.get(
        "AUDIO_CONTROL_LOCK_PATH", "/var/lib/cdsp-automation/audio-control.lock"
    )
)
AUDIO_READY_PATH = Path(
    os.environ.get(
        "AUDIO_READY_PATH", "/run/cdsp-source-switcher/audio-ready.json"
    )
)

CONFIGS = {
    "toslink": TOSLINK_CFG,
    "streamer": STREAMER_CFG,
    "gadget": GADGET_CFG,
    "analog": ANALOG_CFG,
}

_iso226_capability_result = False
_iso226_capability_next_check = 0.0

# Readiness generation for the CamillaDSP connection this process currently
# holds.  Rotated on every (re)connection, so a token minted for an earlier
# engine instance — or by an earlier switcher run — never authorizes unmuting.
_engine_generation = ""


def rotate_engine_generation() -> str:
    """Start a new readiness generation; every older token stops counting."""
    global _engine_generation
    _engine_generation = new_engine_generation()
    return _engine_generation


def engine_generation() -> str:
    return _engine_generation or rotate_engine_generation()


def iso226_capability_available() -> bool:
    """Verify the marker hash against the executable of the live service."""
    global _iso226_capability_result, _iso226_capability_next_check
    now = time.monotonic()
    if now < _iso226_capability_next_check:
        return _iso226_capability_result
    _iso226_capability_next_check = now + 30
    try:
        capability = json.loads(
            Path(ISO226_CAPABILITY_PATH).read_text(encoding="utf-8")
        )
        pid = int(
            subprocess.check_output(
                ["systemctl", "show", "-p", "MainPID", "--value", "camilladsp.service"],
                text=True,
                timeout=2,
            ).strip()
        )
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        digest = hashlib.sha256()
        with executable.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        _iso226_capability_result = bool(
            capability.get("engine") == "Iso226"
            and capability.get("binary_sha256") == digest.hexdigest()
        )
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        _iso226_capability_result = False
    return _iso226_capability_result


class MotuMeterReader:
    """Read passive meter frames from MOTU UltraLite mk5 CueMix WebSocket."""

    def __init__(self, url: str, access: MotuAccess | None = None) -> None:
        self.url = url
        self.ws = None
        self.last_pairs: dict[int, tuple[int, int]] = {}
        self.last_seen = 0.0
        self.next_connect_attempt = 0.0
        self.next_error_log = 0.0
        self.connected_at = 0.0
        # The extra MOTU accesses (clock writes and read-backs, control-UI
        # volume) are recorded in a shared file; see motu_access.py.
        self.access = access or MotuAccess()
        self.forgiven_access: float | None = None

    def close(self) -> None:
        if self.ws is None:
            return
        try:
            self.ws.close()
        except Exception:
            pass
        self.ws = None

    def log_error(self, message: str) -> None:
        now = time.monotonic()
        if now >= self.next_error_log:
            print(message, flush=True)
            self.next_error_log = now + 30

    def connect(self) -> bool:
        now = time.monotonic()
        if self.ws is not None:
            return True
        if now < self.next_connect_attempt:
            return False

        self.next_connect_attempt = now + MOTU_CONNECT_RETRY_SECONDS
        if websocket is None:
            self.log_error(
                "MOTU meter connection unavailable: install websocket-client"
            )
            return False

        try:
            ws = websocket.WebSocket()
            ws.settimeout(1)
            ws.connect(self.url, timeout=1)
            ws.settimeout(0.05)
            self.ws = ws
            self.connected_at = now
            print(f"MOTU meters connected: {self.url}", flush=True)
            return True
        except Exception as exc:
            self.close()
            self.log_error(f"MOTU meter connection failed: {exc}")
            return False

    def read(self) -> dict[int, tuple[int, int]]:
        if not self.connect():
            return (
                self.last_pairs
                if time.monotonic() - self.last_seen <= MOTU_METER_MAX_AGE
                else {}
            )

        deadline = time.monotonic() + MOTU_READ_WINDOW_SECONDS
        while time.monotonic() < deadline and self.ws is not None:
            try:
                _opcode, data = self.ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                break
            except Exception as exc:
                self.close()
                self.log_error(f"MOTU meter read failed: {exc}")
                self.forgive_coordinated_kick()
                break

            # MOTU meter frames are binary. A text frame here is never a valid
            # meter payload; encoding it as UTF-8 would corrupt any byte >= 0x80
            # (multi-byte), so decode 1:1 via latin-1 to preserve raw bytes.
            payload = (
                data.encode("latin-1") if isinstance(data, str) else bytes(data or b"")
            )
            if len(payload) != 104 or payload[:4] != bytes.fromhex("17700000"):
                continue

            body = payload[4:]
            self.last_pairs = {
                pair: (body[pair * 2], body[pair * 2 + 1])
                for pair in range(len(body) // 2)
            }
            self.last_seen = time.monotonic()

        if time.monotonic() - self.last_seen > MOTU_METER_MAX_AGE:
            return {}
        return self.last_pairs


    def forgive_coordinated_kick(self) -> None:
        """Reconnect on the next pass when a recorded MOTU access explains
        the drop, instead of after the SOURCE_MOTU_CONNECT_RETRY_SECONDS
        backoff.

        The device serves one client at a time, so every extra access drops
        this connection. The backoff exists for a device that is absent; a
        drop caused by an access recorded after this connection was made says
        the device is right there. Without this, an access followed within
        ~10 s by another (a UI volume change, then a clock write for a switch
        to TOSLINK) keeps the meters dark ~10 s and TOSLINK drops. Each
        recorded access forgives at most one drop, and an unexplained drop (or
        an unreadable record) keeps the backoff.
        """
        try:
            at, kind = self.access.last_access()
        except AccessUnavailable:
            return
        if at is None or at < self.connected_at or at == self.forgiven_access:
            return
        self.forgiven_access = at
        self.next_connect_attempt = time.monotonic()
        print(f"MOTU meters displaced by a {kind} access; reconnecting", flush=True)


def meter_pairs_active(
    pairs: dict[int, tuple[int, int]], watched_pairs: tuple[int, ...]
) -> bool:
    return any(
        pair in pairs and min(pairs[pair]) < MOTU_METER_ACTIVE_BELOW
        for pair in watched_pairs
    )


def update_meter_timers(
    is_active: bool,
    active_timer: float,
    idle_timer: float,
    idle_seconds: float,
) -> tuple[float, float]:
    if is_active:
        return active_timer + CHECK_INTERVAL, 0.0

    idle_timer += CHECK_INTERVAL
    if idle_timer >= idle_seconds:
        active_timer = 0.0
    return active_timer, idle_timer


def is_alsa_active(card_name: str) -> bool:
    """Check if any ALSA PCM for the card is in RUNNING state."""
    for path in glob.glob(f"/proc/asound/{card_name}/pcm*/sub*/status"):
        try:
            with open(path, "r", encoding="utf-8") as status_file:
                if "state: RUNNING" in status_file.read():
                    return True
        except OSError:
            continue
    return False


def is_gadget_available() -> bool:
    """Check if USB Gadget capture rate is non-zero."""
    try:
        res = subprocess.check_output(
            ["amixer", "-c", "UAC2Gadget", "contents"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        in_capture_rate = False
        for line in res.splitlines():
            if "name='Capture Rate'" in line:
                in_capture_rate = True
                continue
            if in_capture_rate and ": values=" in line:
                rate_text = line.split("values=", 1)[1].strip().split(",", 1)[0]
                return int(rate_text) > 0
    except Exception:
        pass
    return False


def same_config(current: str | None, target: str) -> bool:
    if not current:
        return False
    return os.path.abspath(current) == os.path.abspath(target)


def managed_config_identity(current: str | None) -> tuple[str, str] | None:
    """Identify exact legacy, operator-owned, or generated configs."""
    return identify_managed_config(
        current,
        config_dir=Path(CONFIG_DIR),
        generated_dir=SPEAKER_GENERATED_DIR,
        default_speaker_id=DEFAULT_SPEAKER_ID,
    )


def source_for_config(current: str | None) -> str | None:
    identity = managed_config_identity(current)
    return identity[0] if identity else None


def speaker_for_config(current: str | None) -> str | None:
    identity = managed_config_identity(current)
    return identity[1] if identity else None


def current_speaker_selection() -> dict:
    return read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )


def speaker_catalog() -> dict:
    return profile_catalog(SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, Path(CONFIG_DIR))


def speaker_audio_state(speaker_id: str) -> dict:
    return read_profile_audio_state(
        SPEAKER_AUDIO_DIR,
        speaker_id,
        legacy_path=Path(AUDIO_EQ_PATH),
    )


def pending_transition_mute(selection: dict) -> bool | None:
    try:
        value = json.loads(SPEAKER_TRANSITION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    restore_mute = value.get("restore_mute")
    if (
        value.get("revision") != selection.get("revision")
        or value.get("selected") != selection.get("selected")
        or not isinstance(restore_mute, bool)
    ):
        return None
    return restore_mute


def clear_pending_transition(target: dict | None) -> None:
    if not target:
        return
    try:
        value = json.loads(SPEAKER_TRANSITION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if (
        isinstance(value, dict)
        and value.get("revision") == target.get("selection_revision")
        and value.get("selected") == target.get("speaker")
    ):
        SPEAKER_TRANSITION_PATH.unlink(missing_ok=True)


def require_selected_profile_available(
    cdsp: CamillaClient,
    selected_speaker: str,
    *,
    selected_revision: int,
    current_speaker: str | None,
    current_source: str | None,
    current_config: str | None,
) -> None:
    if selected_speaker == DEFAULT_SPEAKER_ID:
        return
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        with speaker_selection_lock(SPEAKER_SELECTION_PATH):
            latest = current_speaker_selection()
            if (
                latest["selected"] != selected_speaker
                or latest["revision"] != selected_revision
            ):
                return
            selected_entry = speaker_catalog().get(selected_speaker, {})
            if selected_entry.get("available"):
                return
            error = (
                f"selected speaker profile {selected_speaker!r} became unavailable: "
                f"{selected_entry.get('reason') or 'unknown reason'}"
            )
            set_audio_inhibit(AUDIO_READY_PATH)
            cdsp.volume.set_main_mute(True)
            _write_speaker_status(
                {
                    "selected": selected_speaker,
                    "applied": current_speaker,
                    "source": current_source,
                    "config_path": current_config,
                    "config_digest": "",
                    "ok": False,
                    "rollback_ok": False,
                    "error": error,
                    "updated_at": time.time(),
                }
            )
    raise RuntimeError(error)


def _write_status_if_changed(path: Path, payload: dict) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.pop("updated_at", None)
    comparable = payload.copy()
    comparable.pop("updated_at", None)
    if comparable != current:
        atomic_write_json(path, payload)


def _write_speaker_status(payload: dict) -> None:
    _write_status_if_changed(SPEAKER_STATUS_PATH, payload)


def _speaker_status_revision() -> int | None:
    """Selection revision of the last successful apply, if recorded."""
    try:
        value = json.loads(SPEAKER_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    revision = value.get("selection_revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        return None
    return revision


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_config_file(path: Path) -> None:
    result = subprocess.run(
        [CAMILLA_BINARY, "-c", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=CONFIG_VALIDATE_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stdout.strip().splitlines()
        message = detail[-1] if detail else f"exit {result.returncode}"
        raise ValueError(f"CamillaDSP rejected {path.name}: {message}")


def _processing_state(cdsp: CamillaClient) -> str:
    """Normalize pycamilladsp enum and string representations."""
    state = cdsp.general.state()
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name.strip().lower()
    return str(state).rsplit(".", 1)[-1].strip().lower()


class ConfigRecoveryGuard:
    """Recover a remembered config that failed to activate during boot."""

    def __init__(self, retry_seconds: float = RECOVERY_RETRY_SECONDS,
                 log_seconds: float = RECOVERY_LOG_SECONDS) -> None:
        self.retry_seconds = retry_seconds
        self.log_seconds = log_seconds
        self.next_attempt = 0.0
        self.next_log = 0.0
        self.last_message: str | None = None

    def _log(self, message: str, now: float) -> None:
        if message != self.last_message or now >= self.next_log:
            print(message, flush=True)
            self.last_message = message
            self.next_log = now + self.log_seconds

    def _inhibit(self, cdsp: CamillaClient, now: float) -> bool:
        """Latch muted before recovery so the reload cannot become audible.

        A remembered config that failed to activate has never been through the
        verified apply path.  Dropping readiness here makes the main loop treat
        the recovered graph exactly like an intentional transition: muted,
        re-resolved, re-validated and re-stamped before anything may unmute.

        Dropping the token only stops cooperating controls from unmuting; it
        does not mute the engine.  So the mute is read back, and any failure
        -- lock, token, request or read-back -- returns False, which the caller
        must treat as "do not reload".
        """
        try:
            with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
                set_audio_inhibit(AUDIO_READY_PATH)
                cdsp.volume.set_main_mute(True)
                if not bool(cdsp.volume.main_mute()):
                    raise RuntimeError("engine did not report mute after the request")
        except Exception as exc:
            self._log(f"CamillaDSP recovery could not latch mute: {exc}", now)
            return False
        return True

    def ready(self, cdsp: CamillaClient, now: float) -> bool:
        state = _processing_state(cdsp)
        active_config = None if state == "inactive" else cdsp.config.active()
        if state != "inactive" and active_config:
            self.next_attempt = 0.0
            self.next_log = 0.0
            self.last_message = None
            return True
        muted = self._inhibit(cdsp, now)
        if now < self.next_attempt:
            return False
        self.next_attempt = now + self.retry_seconds
        if not muted:
            # Reloading unmuted could make an unverified config audible; wait
            # for the next attempt instead.
            return False

        remembered = cdsp.config.file_path()
        try:
            config_path = os.fspath(remembered) if remembered else ""
        except TypeError:
            config_path = ""
        reason = "processing state is inactive" if state == "inactive" else "active config is missing"
        if not config_path or not os.path.isfile(config_path):
            self._log(
                f"CamillaDSP recovery waiting: {reason}; remembered config is not a file: "
                f"{config_path or '<none>'}",
                now,
            )
            return False
        try:
            cdsp.general.reload()
        except Exception as exc:
            self._log(f"CamillaDSP recovery reload failed for {config_path}: {exc}", now)
        else:
            self._log(f"CamillaDSP recovery: {reason}; reloading {config_path}", now)
        return False


def _legacy_volume_limit(config: dict) -> float:
    """Ceiling for the default speaker's hand-maintained full configs.

    The default speaker declares no profile cap, so 0 dB is the ceiling unless
    the config itself asks for something stricter. A limit this module cannot
    parse is not allowed to read as permissive.
    """
    try:
        declared = config_volume_limit(config, label="legacy config")
    except ValueError:
        return DEFAULT_VOLUME_LIMIT_DB
    if declared is None:
        return DEFAULT_VOLUME_LIMIT_DB
    return min(DEFAULT_VOLUME_LIMIT_DB, declared)


def target_volume_limit(target: dict | None) -> float:
    """The ceiling a resolved target says its control surfaces must honour."""
    if not target:
        return DEFAULT_VOLUME_LIMIT_DB
    limit = normalize_volume_limit(target.get("volume_limit_db"))
    if limit is None:
        limit = normalize_volume_limit(target.get("max_volume_db"))
    return DEFAULT_VOLUME_LIMIT_DB if limit is None else limit


def resolve_config_target(
    source: str, speaker_id: str, *, selection_revision: int | None = None
) -> dict:
    """Return one prevalidated immutable config target for source + speaker."""
    if source not in CONFIGS:
        raise ValueError(f"unknown source: {source}")
    if speaker_id == DEFAULT_SPEAKER_ID:
        path = Path(CONFIGS[source])
        if not path.is_file():
            raise FileNotFoundError(path)
        audio_state = speaker_audio_state(speaker_id)
        expected_config = load_yaml_mapping(path, f"legacy {source} config")
        return {
            "path": str(path),
            "digest": _file_digest(path),
            "source": source,
            "speaker": speaker_id,
            "max_volume_db": 0.0,
            "volume_limit_db": _legacy_volume_limit(expected_config),
            "bypass_user_eq": False,
            "legacy": True,
            "capabilities": {},
            "selection_revision": selection_revision,
            "audio_state": audio_state,
            "expected_config": expected_config,
        }

    catalog = speaker_catalog()
    entry = catalog.get(speaker_id, {})
    if not entry.get("available"):
        raise ValueError(
            f"speaker profile {speaker_id!r} is unavailable: "
            f"{entry.get('reason') or 'not installed'}"
        )
    profile = load_profile(SPEAKER_PROFILE_DIR, speaker_id)
    if source not in profile["supported_sources"]:
        raise ValueError(f"speaker profile {speaker_id!r} does not support {source}")
    operator_filename = operator_config_for_source(speaker_id, source)
    if operator_filename:
        path = Path(CONFIG_DIR) / operator_filename
        expected_config = load_yaml_mapping(path, f"operator config {speaker_id}")
        # An operator config is the operator's artifact, so the profile cap is
        # verified here instead of being compiled in. Rejecting the transition
        # is the fail-closed answer: the alternative is going live on a config
        # nothing downstream can hold below the profile's limit.
        effective_limit = require_config_volume_limit(
            expected_config, profile, label=f"operator config {path.name}"
        )
        validate_config_file(path)
        return {
            "path": str(path),
            "digest": config_digest(expected_config),
            "source": source,
            "speaker": speaker_id,
            "max_volume_db": profile["max_volume_db"],
            "volume_limit_db": effective_limit,
            "bypass_user_eq": profile["bypass_user_eq"],
            "legacy": False,
            "operator_config": True,
            "capabilities": profile["capabilities"],
            "selection_revision": selection_revision,
            "audio_state": speaker_audio_state(speaker_id),
            "expected_config": expected_config,
        }
    source_base = load_yaml_mapping(
        SOURCE_BASE_DIR / f"{source}.yml", f"source base {source}"
    )
    audio_state = speaker_audio_state(speaker_id)
    compiled = compile_profile_config(
        source_base,
        profile,
        audio_state,
        source_id=source,
    )
    path, digest = write_generated_config(
        SPEAKER_GENERATED_DIR,
        compiled,
        source_id=source,
        profile_id=speaker_id,
    )
    validate_config_file(path)
    return {
        "path": str(path),
        "digest": digest,
        "source": source,
        "speaker": speaker_id,
        "max_volume_db": profile["max_volume_db"],
        # compile_profile_config() already wrote the native ceiling, keeping a
        # stricter pre-existing one; publish exactly what went into the file.
        "volume_limit_db": config_volume_limit(compiled, label="generated config"),
        "bypass_user_eq": profile["bypass_user_eq"],
        "legacy": False,
        "capabilities": profile["capabilities"],
        "selection_revision": selection_revision,
        "audio_state": audio_state,
        "expected_config": compiled,
    }


def audio_active(levels: object) -> bool:
    return isinstance(levels, (list, tuple)) and any(
        isinstance(level, (int, float)) and level > AUDIO_THRESHOLD_DB
        for level in levels
    )


def validate_configs(selected_speaker: str = DEFAULT_SPEAKER_ID) -> None:
    if selected_speaker != DEFAULT_SPEAKER_ID:
        entry = speaker_catalog().get(selected_speaker, {})
        if not entry.get("available"):
            raise FileNotFoundError(
                f"speaker profile {selected_speaker!r} unavailable: "
                f"{entry.get('reason') or 'unknown reason'}"
            )
        return
    missing = [path for path in (TOSLINK_CFG, STREAMER_CFG, GADGET_CFG) if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError("Missing CamillaDSP config(s): " + ", ".join(missing))


def read_manual_source() -> str | None:
    try:
        source = (
            open(SOURCE_OVERRIDE_PATH, "r", encoding="utf-8").read().strip().lower()
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"Could not read source override: {exc}", flush=True)
        return None

    if not source or source == "auto":
        return None
    return source


def _write_audio_eq_status(payload: dict) -> None:
    """Publish apply convergence without rewriting an unchanged status file."""
    _write_status_if_changed(Path(AUDIO_EQ_STATUS_PATH), payload)


def _comparable_filter(value: object) -> object:
    """Normalize optional fields CamillaDSP materializes on read-back."""
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    if result.get("description") is None:
        result.pop("description", None)
    parameters = result.get("parameters")
    if isinstance(parameters, dict):
        # Gain filters read back with these optional fields even when they
        # were omitted from the submitted config.
        for key in ("inverted", "mute"):
            if parameters.get(key) is None:
                parameters.pop(key, None)
    return result


def _audio_overlay_matches(actual: object, expected: dict) -> bool:
    """True once the engine holds everything an overlay write changed.

    The overlay does more than add owned filters: it also removes legacy
    Bass/Treble/Loudness stages and older owned names, and rewrites the
    pipeline around them.  So the whole filter *set* and the whole pipeline
    must match, not just the owned parts -- otherwise a bypass that never
    applied reads as done because both sides have no owned overlay.  Owned
    filter bodies compare through ``_comparable_filter``; other filters were
    copied untouched from the live graph and need only be present.  An absent
    or empty read-back never matches.
    """
    if not isinstance(actual, dict) or not actual:
        return False
    actual_filters = actual.get("filters") or {}
    expected_filters = expected.get("filters") or {}
    if set(actual_filters) != set(expected_filters):
        return False
    expected_owned = {
        name: _comparable_filter(value)
        for name, value in expected_filters.items()
        if name.startswith(FILTER_PREFIX)
    }
    actual_owned = {
        name: _comparable_filter(value)
        for name, value in actual_filters.items()
        if name.startswith(FILTER_PREFIX)
    }
    if actual_owned != expected_owned:
        return False
    return _without_null_fields(actual.get("pipeline") or []) == _without_null_fields(
        expected.get("pipeline") or []
    )


def _without_null_fields(value: object) -> object:
    """Drop null-valued mapping keys anywhere in a config tree.

    CamillaDSP serializes its *parsed* configuration for GetConfig, so every
    optional field the submitted YAML omitted reads back as null -- not only
    inside ``filters`` (where ``_comparable_filter`` patched this narrowly)
    but also for ALSA device options such as ``stop_on_inactive``,
    ``link_volume_control`` and ``labels``.

    Only the absent/null pairing collapses. A field the submitted config
    actually specifies is still compared strictly against the engine's value,
    and a concrete value the engine reports where the request said nothing is
    still a difference. Routing, channel counts, ``volume_limit`` and ``mute``
    therefore keep their exact comparison.
    """
    if isinstance(value, dict):
        return {
            key: _without_null_fields(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_without_null_fields(item) for item in value]
    return value


def _configs_equivalent(actual: dict, expected: dict) -> bool:
    """Ignore CamillaDSP's materialization of omitted optional fields."""
    return _without_null_fields(actual) == _without_null_fields(expected)


def _engine_parsed_config(cdsp: CamillaClient, captured: dict) -> dict | None:
    """Return the engine's own parse of the captured config, or None.

    Normalization approach: let the engine define it.  Handing the engine the
    *captured* mapping (ReadConfig) yields its deserialization with defaults
    filled in, so the read-back comparison needs no allowlist of optional
    fields that would drift with every upstream release.

    It must be the captured mapping, never a fresh read of the file: an
    operator file edited between the integrity check and the reload would
    otherwise be parsed, loaded and then verified against itself.  ReadConfig
    does not run the filename-aware validation a reload does (token
    substitution, relative coefficient paths); ``_engine_load_view`` adds that.

    Older pycamilladsp clients (and engines that reject the request) have no
    such call; the caller then falls back to structural null-stripping.
    """
    parser = getattr(getattr(cdsp, "config", None), "parse_yaml", None)
    if parser is None:
        return None
    try:
        parsed = parser(yaml.safe_dump(captured, sort_keys=False))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _engine_load_view(config: dict, config_path: str) -> dict:
    """Apply the preprocessing CamillaDSP performs when it loads ``config_path``.

    Neither the engine's ReadConfigFile nor our own YAML read does this, but a
    reload validates the file *with its filename*, which (pinned upstream
    ``validate_config``) first substitutes ``$samplerate$``/``$channels$`` in
    Conv filenames and pipeline step names, then rewrites a relative Conv
    filename to ``<canonical config dir>/<filename>`` when that file exists.
    The active config therefore names ``/.../configs/coeffs/hf.txt`` where the
    file says ``coeffs/hf.txt``.  Mirror exactly that, so the comparison stays
    strict about which coefficient file is loaded without tripping over how it
    was spelled.
    """
    result = copy.deepcopy(config)
    devices = result.get("devices")
    devices = devices if isinstance(devices, dict) else {}
    capture = devices.get("capture")
    tokens: dict[str, str] = {}
    if isinstance(devices.get("samplerate"), int):
        tokens["$samplerate$"] = str(devices["samplerate"])
    if isinstance(capture, dict) and isinstance(capture.get("channels"), int):
        tokens["$channels$"] = str(capture["channels"])

    def substitute(value: str) -> str:
        for token, replacement in tokens.items():
            value = value.replace(token, replacement)
        return value

    config_dir = (
        os.path.dirname(os.path.realpath(config_path))
        if os.path.exists(config_path)
        else None
    )
    filters = result.get("filters")
    if isinstance(filters, dict):
        for definition in filters.values():
            if not isinstance(definition, dict) or definition.get("type") != "Conv":
                continue
            parameters = definition.get("parameters")
            if not isinstance(parameters, dict) or parameters.get("type") not in (
                "Raw",
                "Wav",
            ):
                continue
            filename = parameters.get("filename")
            if not isinstance(filename, str):
                continue
            filename = substitute(filename)
            if config_dir and not os.path.isabs(filename):
                candidate = os.path.join(config_dir, filename)
                if os.path.exists(candidate):
                    filename = candidate
            parameters["filename"] = filename
    pipeline = result.get("pipeline")
    if isinstance(pipeline, list):
        for step in pipeline:
            if not isinstance(step, dict):
                continue
            if step.get("type") == "Filter" and isinstance(step.get("names"), list):
                step["names"] = [
                    substitute(name) if isinstance(name, str) else name
                    for name in step["names"]
                ]
            elif step.get("type") in ("Mixer", "Processor") and isinstance(
                step.get("name"), str
            ):
                step["name"] = substitute(step["name"])
    return result


def _reference_config(cdsp: CamillaClient, file_path: str, expected: dict) -> dict:
    """What the engine should report after loading ``file_path``.

    Built only from ``expected`` -- the mapping captured (and, for managed
    targets, digest-checked) at resolve time -- so whatever is on disk now
    has no say in what counts as correct.  ``file_path`` only supplies the
    directory relative coefficient paths resolve against.
    """
    reference = _engine_parsed_config(cdsp, expected)
    if reference is None:
        reference = expected
    return _engine_load_view(reference, file_path)


def _matches_reference(accepted: object, reference: dict) -> bool:
    if not isinstance(accepted, dict) or not accepted:
        return False
    return _configs_equivalent(accepted, reference)


def _accepted_config_matches(
    cdsp: CamillaClient,
    file_path: str,
    accepted: object,
    expected: dict,
) -> bool:
    """Compare an accepted read-back against the captured configuration."""
    return _matches_reference(accepted, _reference_config(cdsp, file_path, expected))


def _await_active_config(
    cdsp: CamillaClient,
    file_path: str,
    expected: dict,
    *,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> None:
    """Poll until the engine reports the requested config, or fail closed.

    A single read after a fixed sleep races the asynchronous application of a
    queued config. Poll against a bounded deadline instead; on timeout raise,
    which leaves the caller's rollback and latched mute intact.
    """
    if timeout is None:
        timeout = CONFIG_APPLY_TIMEOUT
    if poll_interval is None:
        poll_interval = CONFIG_APPLY_POLL_INTERVAL
    poll_interval = max(poll_interval, 0.01)
    reference = _reference_config(cdsp, file_path, expected)
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if _matches_reference(cdsp.config.active(), reference):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "CamillaDSP active config differs from requested config"
            )
        time.sleep(poll_interval)


def _submit_audio_overlay(cdsp: CamillaClient, updated: dict) -> None:
    """Queue ``updated`` and wait until the engine reports its owned overlay.

    SetConfig only queues the change, so an immediate read can still show the
    previous graph.  Poll against the same bounded deadline as a reload; a
    measurement bypass in particular must not report "flat" on the strength of
    an accepted request alone.
    """
    cdsp.config.set_active(updated)
    poll_interval = max(CONFIG_APPLY_POLL_INTERVAL, 0.01)
    deadline = time.monotonic() + max(CONFIG_APPLY_TIMEOUT, 0.0)
    while True:
        if _audio_overlay_matches(cdsp.config.active(), updated):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("CamillaDSP did not confirm the requested audio overlay")
        time.sleep(poll_interval)


def ensure_audio_eq(
    cdsp: CamillaClient,
    *,
    speaker_id: str | None = None,
    state: dict | None = None,
) -> None:
    """Merge the persistent user-EQ overlay into the active config.

    The source switcher remains the only writer. This function is idempotent,
    so UI edits converge quickly while steady-state polling performs no DSP or
    filesystem writes.
    """
    if speaker_id is None:
        speaker_id = current_speaker_selection()["selected"]
    if state is None:
        state = speaker_audio_state(speaker_id)
    config = cdsp.config.active()
    if not config:
        return
    if speaker_id != DEFAULT_SPEAKER_ID:
        profile = load_profile(SPEAKER_PROFILE_DIR, speaker_id)
        if profile["bypass_user_eq"]:
            safe_state = copy.deepcopy(state)
            safe_state["enabled"] = False
            safe_state["loudness"]["enabled"] = False
            safe_state["preamp_db"] = 0.0
            updated, _preamp = apply_audio_overlay(config, safe_state)
            if not _configs_equivalent(config, updated):
                _submit_audio_overlay(cdsp, updated)
            _write_audio_eq_status(
                {
                    **status_payload(state, applied=True, effective_preamp=0.0),
                    "speaker": speaker_id,
                }
            )
            return
    if state["loudness"]["enabled"]:
        if not iso226_capability_available():
            safe_state = copy.deepcopy(state)
            safe_state["loudness"]["enabled"] = False
            updated, preamp = apply_audio_overlay(config, safe_state)
            if not _configs_equivalent(config, updated):
                _submit_audio_overlay(cdsp, updated)
            _write_audio_eq_status(
                {
                    **status_payload(
                        state,
                        applied=False,
                        effective_preamp=preamp,
                        error="ISO 226 engine capability is missing; loudness bypassed",
                    ),
                    "speaker": speaker_id,
                }
            )
            return
    updated, preamp = apply_audio_overlay(config, state)
    changed = not _configs_equivalent(config, updated)
    if changed:
        _submit_audio_overlay(cdsp, updated)
        print(
            f"Audio EQ revision {state['revision']} applied "
            f"({len(state['bands'])} bands, preamp {preamp:+.1f}dB)",
            flush=True,
        )
    _write_audio_eq_status(
        {
            **status_payload(state, applied=True, effective_preamp=preamp),
            "speaker": speaker_id,
        }
    )


def ensure_current_speaker_audio_eq(cdsp: CamillaClient, current_speaker: str) -> None:
    """Reconcile EQ only while the selection still matches the live crossover."""
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH), speaker_selection_lock(
        SPEAKER_SELECTION_PATH
    ):
        if current_speaker_selection()["selected"] != current_speaker:
            return
        ensure_audio_eq(cdsp, speaker_id=current_speaker)


def _owns_motu_clock() -> bool:
    if clock_sync is None or SOURCE_MOTU_CLOCK in {"0", "false", "no", "off"}:
        return False
    if SOURCE_MOTU_CLOCK in {"1", "true", "yes", "on"}:
        return True
    return MOTU_CLOCK_UNIT_PATH.exists()


def _set_motu_clock_muted(clock: str) -> bool:
    """Write one clock source and let it re-lock; the caller holds mute."""
    assert clock_sync is not None
    if not clock_sync.set_motu_clock(clock):
        return False
    # The shared cache is how clock_sync learns this write was made: it
    # re-reads it under the same lock and only verifies from here on.
    clock_sync.persist_clock(clock)
    time.sleep(MOTU_CLOCK_SETTLE_SECONDS)
    return True


def _switch_motu_clock(target: dict | None) -> str | None:
    """Move the MOTU clock to the target's source inside the mute window.

    Runs muted, under the audio-control lock, *before* the reload, so the new
    graph opens the interface on a clock that has already re-locked and the
    re-lock can never land after unmute.  Returns the clock to restore if the
    transition rolls back, or None when nothing was changed.

    A failed write does not fail the transition: the audio path itself is
    fine, and clock_sync still owns retry and read-back, so the clock is only
    late, as it always was before.  A clock the shared cache already names is
    not written again: every write re-locks and clicks.
    """
    if not target or not _owns_motu_clock():
        return None
    source = target.get("source")
    if source not in SOURCE_PRIORITY:
        return None
    assert clock_sync is not None
    desired = "optical" if source == "toslink" else "internal"
    believed = clock_sync.read_persisted_clock()
    if believed == desired:
        return None
    if not _set_motu_clock_muted(desired):
        print(
            f"MOTU clock not switched to {desired} inside the transition; "
            "clock sync will retry",
            flush=True,
        )
        return None
    return believed


def _restore_motu_clock(previous_clock: str | None) -> None:
    """Best-effort return of the clock during a muted rollback."""
    if previous_clock is None or clock_sync is None:
        return
    try:
        if not _set_motu_clock_muted(previous_clock):
            print(f"MOTU clock not restored to {previous_clock}", flush=True)
    except Exception as exc:
        print(f"MOTU clock restore failed: {exc}", flush=True)


def apply_config(
    cdsp: CamillaClient,
    file_path: str,
    settle_time: float = SETTLE_TIME,
    *,
    target: dict | None = None,
    restore_mute: bool | None = None,
    audio_lock_held: bool = False,
) -> None:
    """Apply a prevalidated config while muted; latch mute on uncertainty."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    validate_config_file(Path(file_path))
    config_name = os.path.basename(file_path)
    print(f">>> Switching to: {config_name}", flush=True)
    previous_path = cdsp.config.file_path()
    try:
        previous_expected = load_yaml_mapping(
            Path(previous_path), "previous config"
        ) if previous_path else None
    except (OSError, ValueError):
        previous_expected = None
    audio_guard = None
    audio_guard_entered = False
    try:
        if not audio_lock_held:
            audio_guard = audio_control_lock(AUDIO_CONTROL_LOCK_PATH)
            audio_guard.__enter__()
            audio_guard_entered = True
        set_audio_inhibit(AUDIO_READY_PATH)
        previous_mute = (
            bool(cdsp.volume.main_mute())
            if restore_mute is None
            else bool(restore_mute)
        )
        previous_volume = float(cdsp.volume.main_volume())
        cdsp.volume.set_main_mute(True)
    except Exception:
        if audio_guard_entered:
            assert audio_guard is not None
            audio_guard.__exit__(None, None, None)
        raise
    selection_guard = None
    selection_guard_entered = False
    previous_clock: str | None = None
    try:
        if target and target.get("selection_revision") is not None:
            selection_guard = speaker_selection_lock(SPEAKER_SELECTION_PATH)
            selection_guard.__enter__()
            selection_guard_entered = True
        if target and not target.get("legacy", False):
            label = "operator config" if target.get("operator_config") else "generated config"
            on_disk = load_yaml_mapping(Path(file_path), label)
            if config_digest(on_disk) != target["digest"]:
                raise RuntimeError(f"{label} integrity check failed")
        if target and target.get("selection_revision") is not None:
            current_selection = current_speaker_selection()
            if (
                current_selection["selected"] != target["speaker"]
                or current_selection["revision"] != target["selection_revision"]
            ):
                raise RuntimeError("speaker selection changed before config reload")
        previous_clock = _switch_motu_clock(target)
        cdsp.config.set_file_path(file_path)
        cdsp.general.reload()
        time.sleep(settle_time)
        state = _processing_state(cdsp)
        if state not in {"running", "paused"}:
            raise RuntimeError(f"CamillaDSP did not reach a safe running state: {state}")
        if not same_config(cdsp.config.file_path(), file_path):
            raise RuntimeError("CamillaDSP did not retain the requested config path")
        if target:
            expected = target.get("expected_config")
            if expected is not None:
                _await_active_config(cdsp, file_path, expected)

        if target and target.get("selection_revision") is not None:
            current_selection = current_speaker_selection()
            if (
                current_selection["selected"] != target["speaker"]
                or current_selection["revision"] != target["selection_revision"]
            ):
                raise RuntimeError("speaker selection changed during config transition")

        # Re-assert the owned EQ overlay before sound returns.
        if target:
            ensure_audio_eq(
                cdsp,
                speaker_id=target["speaker"],
                state=target.get("audio_state"),
            )
        else:
            ensure_audio_eq(cdsp)
        if target and target.get("selection_revision") is not None:
            current_selection = current_speaker_selection()
            if (
                current_selection["selected"] != target["speaker"]
                or current_selection["revision"] != target["selection_revision"]
            ):
                raise RuntimeError("speaker selection changed before unmute")
        maximum = target_volume_limit(target)
        restored_volume = min(previous_volume, maximum)
        if restored_volume != previous_volume:
            print(
                f"Volume clamped for speaker profile: {previous_volume:+.1f} "
                f"-> {restored_volume:+.1f}dB",
                flush=True,
            )
        cdsp.volume.set_main_volume(restored_volume)
        if target:
            _write_speaker_status(
                {
                    "selected": target["speaker"],
                    "applied": target["speaker"],
                    "source": target["source"],
                    "config_path": file_path,
                    "config_digest": target["digest"],
                    "capabilities": target.get("capabilities", {}),
                    "selection_revision": target.get("selection_revision"),
                    # The ceiling every control surface reads back. Only a
                    # successful apply publishes one; the failure payloads
                    # below deliberately omit it so readers fail closed.
                    "volume_limit_db": maximum,
                    "ok": True,
                    "error": "",
                    "updated_at": time.time(),
                }
            )
        # Bind readiness to this engine instance before sound may return: the
        # marker lives in the live config only, so an engine that restarts or
        # reloads comes back without it and every consumer inhibits on its own.
        generation = engine_generation()
        stamp_engine_generation(
            cdsp,
            generation,
            timeout=CONFIG_APPLY_TIMEOUT,
            poll_interval=CONFIG_APPLY_POLL_INTERVAL,
        )
        cdsp.volume.set_main_mute(previous_mute)
        clear_pending_transition(target)
        clear_audio_inhibit(
            AUDIO_READY_PATH,
            generation=generation,
            applied={
                "config_path": file_path,
                "config_digest": target.get("digest", "") if target else "",
                "source": target.get("source") if target else None,
                "speaker": target.get("speaker") if target else None,
                "selection_revision": (
                    target.get("selection_revision") if target else None
                ),
            },
        )
        if (
            target
            and not target.get("legacy", False)
            and not target.get("operator_config", False)
        ):
            removed = prune_generated_configs(
                SPEAKER_GENERATED_DIR,
                protected_paths=(Path(file_path),),
            )
            if removed:
                print(f"Pruned {removed} old generated config(s)", flush=True)
    except Exception as exc:
        # Failure handling is fail-closed before any rollback I/O. The ready
        # token stays absent even if the mute RPC response is ambiguous.
        try:
            set_audio_inhibit(AUDIO_READY_PATH)
            cdsp.volume.set_main_mute(True)
        except Exception:
            pass
        rollback_ok = False
        _restore_motu_clock(previous_clock)
        if previous_path and os.path.exists(previous_path):
            try:
                cdsp.config.set_file_path(previous_path)
                cdsp.general.reload()
                time.sleep(settle_time)
                rollback_state = _processing_state(cdsp)
                rollback_active = cdsp.config.active()
                rollback_ok = (
                    rollback_state in {"running", "paused"}
                    and same_config(cdsp.config.file_path(), previous_path)
                    and previous_expected is not None
                    and _accepted_config_matches(
                        cdsp, previous_path, rollback_active, previous_expected
                    )
                )
            except Exception as rollback_exc:
                print(f"Config rollback failed: {rollback_exc}", flush=True)
        # Never unmute automatically after an uncertain profile transition.
        try:
            cdsp.volume.set_main_mute(True)
        except Exception:
            pass
        if target:
            _write_speaker_status(
                {
                    "selected": target["speaker"],
                    "applied": speaker_for_config(previous_path),
                    "source": source_for_config(previous_path),
                    "config_path": previous_path,
                    "config_digest": "",
                    "ok": False,
                    "rollback_ok": rollback_ok,
                    "error": str(exc),
                    "updated_at": time.time(),
                }
            )
        raise
    finally:
        if selection_guard_entered:
            selection_guard.__exit__(None, None, None)
        if audio_guard_entered:
            assert audio_guard is not None
            audio_guard.__exit__(None, None, None)


def log_idle(source: str, seconds: float, limit: float = IDLE_TIMEOUT) -> None:
    if DEBUG_MODE and int(seconds) > 0 and int(seconds) % 5 == 0:
        print(f"-> {source}: idle {seconds:g}/{limit:g}s", flush=True)


# ---------------------------------------------------------------------------
# Source arbitration
#
# Three states, not two.  Readiness and playback are different facts and the
# switcher used to conflate them:
#
#   ready    - the input exists: an ALSA Loopback PCM is RUNNING, the USB
#              gadget reports a capture rate, a MOTU meter pair is above its
#              floor.  An AirPlay session that is connected and paused is
#              ready.  So is a console left switched on.
#   playing  - capture levels confirm audio is actually flowing.  For the
#              streamer and the gadget this is only observable while that
#              source is the selected one, so off-source it is *unknown*
#              (None), not False.
#   silent   - the source was selected, listened to, and heard nothing.  This
#              is a durable fact about a probe, and it is the state the old
#              code had no room for: it reset the silence timer on every
#              switch and then re-selected the source purely because the
#              hardware was still ready.
# ---------------------------------------------------------------------------

SOURCE_PRIORITY = ("streamer", "gadget", "toslink", "analog")


@dataclass(frozen=True)
class SourceSnapshot:
    """What a single pass of the loop can observe about one source.

    ``playing`` is deliberately tri-state: ``None`` means "cannot be known
    without selecting this source", which is exactly the case that needs a
    rate-limited probe rather than an immediate switch.
    """

    ready: bool = False
    playing: bool | None = None
    #: Readiness is itself a debounced signal-presence measurement, as it is
    #: for the MOTU meter sources: ``ready`` only went false after the meter
    #: had been quiet for SOURCE_TOSLINK_IDLE_SECONDS / _ANALOG_IDLE_SECONDS.
    #: Such a source has already served its own track-gap grace by the time it
    #: reads silent, so it is not given a second one on top.
    self_metering: bool = False


@dataclass(frozen=True)
class ProbeRecord:
    """Persistent memory of what selecting a source actually produced."""

    #: Seconds of continuous silence while this source was selected.
    silence: float = 0.0
    #: Audio was confirmed at least once since this source was selected, so
    #: its silence is a track gap (IDLE_TIMEOUT) rather than a failed probe.
    confirmed: bool = False
    #: Readiness on the previous pass, for rising-edge detection.
    was_ready: bool = False
    #: Seconds the source has been continuously not ready.
    unready_for: float = 0.0
    #: Seconds this source has been continuously *confirmed playing*.  Only a
    #: rival that has sustained it may cut another source's grace short.
    playing_for: float = 0.0
    #: Consecutive silent probes; drives the backoff delay.
    backoff_level: int = 0
    #: Monotonic deadline before which this source must not be re-probed.
    backoff_until: float = 0.0


@dataclass(frozen=True)
class ArbitrationDecision:
    """The source the switcher should be on, and why."""

    #: Source to select, or ``None`` for the idle path (keep-last / idle mode).
    source: str | None
    reason: str
    probes: dict[str, ProbeRecord]
    last_active: str | None = None
    #: The decision came from a manual override, whose config failures are
    #: reported to the operator rather than raised.
    manual: bool = False


def probe_backoff_delay(
    level: int,
    *,
    base: float = PROBE_BACKOFF_SECONDS,
    factor: float = PROBE_BACKOFF_FACTOR,
    maximum: float = PROBE_BACKOFF_MAX,
) -> float:
    """Delay before the ``level``-th consecutive silent probe may repeat."""
    if base <= 0:
        return 0.0
    return min(base * (factor ** max(level, 0)), maximum)


def _selected(record: ProbeRecord) -> ProbeRecord:
    """Reset the listening state of a source the switcher is about to select.

    The backoff *level* survives, so a source that keeps disappointing keeps
    escalating; the deadline does not, because the probe is starting now.
    """
    return replace(record, silence=0.0, confirmed=False, backoff_until=0.0)


def _cleared(record: ProbeRecord) -> ProbeRecord:
    """Forget a source's silent history: it has proved itself, or was forced."""
    return replace(
        record, silence=0.0, confirmed=True, backoff_level=0, backoff_until=0.0
    )


def arbitrate(
    *,
    now: float,
    elapsed: float,
    current_source: str | None,
    last_active: str | None = None,
    manual_source: str | None = None,
    sources: dict[str, SourceSnapshot],
    probes: dict[str, ProbeRecord] | None = None,
    priority: tuple[str, ...] = SOURCE_PRIORITY,
    idle_timeout: float = IDLE_TIMEOUT,
    probe_silence_timeout: float = PROBE_SILENCE_TIMEOUT,
    lower_priority_timeout: float = LOWER_PRIORITY_ACTIVE_TIMEOUT,
    backoff_base: float = PROBE_BACKOFF_SECONDS,
    backoff_factor: float = PROBE_BACKOFF_FACTOR,
    backoff_max: float = PROBE_BACKOFF_MAX,
    preempt_dwell: float = PREEMPT_DWELL_SECONDS,
    log: bool = False,
) -> ArbitrationDecision:
    """Decide which source to be on from one snapshot of observable state.

    Pure: ``probes`` is never mutated, the updated memory is returned on the
    decision.  ``now`` is a monotonic timestamp used only for backoff
    deadlines; ``elapsed`` is the wall time this pass covers and is what the
    silence counters accumulate, so a slow pass is not mistaken for silence
    that never happened.
    """
    records = dict(probes or {})
    for name in sources:
        records.setdefault(name, ProbeRecord())

    # A source that just came back after really being gone is a new session,
    # not the one we already gave up on: forgive its backoff.  The
    # not-ready dwell requirement keeps flapping hardware from doing the same.
    # This loop is the only writer of the purely observational counters, so
    # every later branch reads them already including this pass.
    for name, state in sources.items():
        record = records[name]
        if state.ready:
            if not record.was_ready and record.unready_for >= probe_silence_timeout:
                record = replace(record, backoff_level=0, backoff_until=0.0)
            record = replace(record, was_ready=True, unready_for=0.0)
        else:
            record = replace(
                record, was_ready=False, unready_for=record.unready_for + elapsed
            )
        record = replace(
            record,
            playing_for=(
                record.playing_for + elapsed if state.playing is True else 0.0
            ),
        )
        records[name] = record

    # Priority 0: a manual override wins outright and clears any backoff, so
    # an operator can always reach a source the switcher has written off.
    if manual_source:
        records[manual_source] = _cleared(
            records.get(manual_source, ProbeRecord())
        )
        return ArbitrationDecision(
            source=manual_source,
            reason="manual override",
            probes=records,
            last_active=f"manual:{manual_source}",
            manual=True,
        )

    def rank(name: str | None) -> int:
        return priority.index(name) if name in priority else len(priority)

    def preempted_by(name: str, silence: float) -> str | None:
        """A rival with *confirmed* audio that may cut ``name``'s grace short.

        Only confirmed playback qualifies.  A rival that is merely ready - or
        whose playback is unknown because it is not the selected source - is
        exactly what the grace exists to protect against, and never counts.

        Priority decides how patient the grace is allowed to be.  A rival
        *above* the current source is the reason the priority order exists at
        all, so it waits for nothing but the dwell: real audio on a
        higher-priority input must not sit in silence for the length of
        somebody else's track gap.  A rival *below* keeps its documented
        LOWER_PRIORITY_ACTIVE_TIMEOUT gate, which defaults to immediate.
        """
        threshold = rank(name)
        for other in priority:
            state = sources.get(other)
            if other == name or state is None or state.playing is not True:
                continue
            if records[other].playing_for < preempt_dwell:
                continue
            if rank(other) < threshold or silence >= lower_priority_timeout:
                return other
        return None

    # Priority 1: keep the current source while it still deserves it.  An
    # actually-playing source is never pre-empted by a higher-priority one.
    current = sources.get(current_source) if current_source else None
    if current_source and current is not None:
        record = records[current_source]
        if current.playing:
            records[current_source] = _cleared(record)
            return ArbitrationDecision(
                source=current_source,
                reason="current source playing",
                probes=records,
                last_active=current_source,
            )
        # Silent.  The grace that follows a source whose hardware went away is
        # only extended to the source that was last actually selected.  A
        # source already written off keeps its config loaded when nothing else
        # qualifies (idle keep-last); listening to it again before its backoff
        # expires would re-run the give-up every probe window and escalate the
        # backoff without a single new observation.
        if now < record.backoff_until:
            last_active = None
        elif current.ready or last_active == current_source:
            record = replace(record, silence=record.silence + elapsed)
            records[current_source] = record
            # This is the distinction the old code was missing: a source that
            # had confirmed audio gets the full track-gap grace, a source that
            # only ever proved ready gets the much shorter probe window.  A
            # self-metering source gets neither: its readiness dropping is
            # already a debounced "the signal stopped", so a further grace on
            # top would just be the meter's idle timer counted twice.
            if current.self_metering:
                limit = 0.0
            elif record.confirmed:
                limit = idle_timeout
            else:
                limit = probe_silence_timeout
            if log:
                label = current_source.capitalize()
                log_idle(
                    label if current.ready else f"{label} grace", record.silence, limit
                )
            rival = preempted_by(current_source, record.silence)
            if record.silence < limit and rival is None:
                return ArbitrationDecision(
                    source=current_source,
                    reason="current source silent, within grace",
                    probes=records,
                    last_active=current_source if current.ready else last_active,
                )
            if log and DEBUG_MODE and rival is not None:
                print(
                    f"{current_source.capitalize()} silent and {rival} is playing"
                    " - handing over",
                    flush=True,
                )
            # Probed and found silent.  Remember that, and back off.  A
            # self-metering source is never probed, so it has nothing to back
            # off from: its meter requalifies it the instant signal returns.
            delay = probe_backoff_delay(
                record.backoff_level,
                base=backoff_base,
                factor=backoff_factor,
                maximum=backoff_max,
            )
            records[current_source] = replace(
                record,
                silence=0.0,
                confirmed=False,
                backoff_level=(
                    record.backoff_level
                    if current.self_metering
                    else record.backoff_level + 1
                ),
                backoff_until=0.0 if current.self_metering else now + delay,
            )
            if log and DEBUG_MODE and not current.self_metering:
                print(
                    f"{current_source.capitalize()} silent - not re-probing for "
                    f"{delay:g}s",
                    flush=True,
                )
        last_active = None

    # Priority 2..n: walk the priority order for something better.
    for name in priority:
        state = sources.get(name)
        if state is None or not state.ready or name == current_source:
            continue
        if state.playing:
            # Confirmed audio is never rate-limited.
            records[name] = _cleared(records[name])
            return ArbitrationDecision(
                source=name,
                reason="confirmed playing",
                probes=records,
                last_active=name,
            )
        if state.playing is False:
            # Observed and heard nothing; readiness alone does not requalify it.
            continue
        if now < records[name].backoff_until:
            continue
        records[name] = _selected(records[name])
        return ArbitrationDecision(
            source=name,
            reason="probing a ready source",
            probes=records,
            last_active=name,
        )

    return ArbitrationDecision(
        source=None, reason="no source qualified", probes=records, last_active=None
    )


def mute_for_startup_validation(cdsp: CamillaClient) -> bool:
    """Capture desired mute, then fail closed before any startup validation."""
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        restore_mute = bool(cdsp.volume.main_mute())
        cdsp.volume.set_main_mute(True)
    return restore_mute


def apply_arbitrated_config(
    cdsp: CamillaClient,
    target: dict,
    startup_restore_mute: bool | None,
    *,
    settle_time: float = SETTLE_TIME,
) -> None:
    """Apply the first selected source with the pre-start mute preference."""
    apply_config(
        cdsp,
        target["path"],
        settle_time=settle_time,
        target=target,
        restore_mute=startup_restore_mute,
    )
    return None


def main() -> int:
    print(">>> CamillaDSP Source Switcher Started <<<", flush=True)
    print(
        "Priority: manual override -> active current source -> 1) Streamer (AirPlay) -> 2) USB Gadget "
        "-> 3) TOSLINK meters -> 4) Analog meters -> idle keep-last",
        flush=True,
    )
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        set_audio_inhibit(AUDIO_READY_PATH)
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    motu = (
        MotuMeterReader(MOTU_WS_URL)
        if TOSLINK_MOTU_METERS or ANALOG_MOTU_METERS
        else None
    )
    probes: dict[str, ProbeRecord] = {}
    last_arbitration: float | None = None
    toslink_active_timer = 0.0
    toslink_idle_timer = TOSLINK_IDLE_SECONDS
    analog_active_timer = 0.0
    analog_idle_timer = ANALOG_IDLE_SECONDS
    last_active_source = None
    last_manual_error = None
    error_log_deadline = 0.0
    last_error_message = None
    next_audio_eq_check = 0.0
    startup_restore_mute: bool | None = None
    startup_configs_validated = False
    recovery = ConfigRecoveryGuard()

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                # A new websocket may be a new engine process.  Rotate the
                # generation and drop the token: whatever was verified belonged
                # to the connection that just ended, and startup validation has
                # to run again before audio may return.
                rotate_engine_generation()
                with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
                    set_audio_inhibit(AUDIO_READY_PATH)
                startup_restore_mute = None
                startup_configs_validated = False
                print("Connected to CamillaDSP", flush=True)

            if not recovery.ready(cdsp, time.monotonic()):
                time.sleep(CHECK_INTERVAL)
                continue

            # One engine round-trip per pass decides readiness for the whole
            # iteration: the token must exist, belong to this switcher run, and
            # name the generation the live engine instance still carries.
            inhibited = audio_inhibit_active(
                AUDIO_READY_PATH, cdsp, generation=engine_generation()
            )
            if inhibited:
                # An engine that dropped the marker without dropping the
                # connection (restart, external reload, foreign set_active) is
                # unverified; make that visible to every other control now.
                with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
                    set_audio_inhibit(AUDIO_READY_PATH)

            if startup_restore_mute is None and inhibited:
                startup_restore_mute = mute_for_startup_validation(cdsp)
            current_config = cdsp.config.file_path()
            selection = current_speaker_selection()
            selected_speaker = selection["selected"]
            if not startup_configs_validated:
                # Missing configs now fail only after the live engine is muted.
                validate_configs(selected_speaker)
                startup_configs_validated = True
            if inhibited:
                requested_mute = pending_transition_mute(selection)
                if requested_mute is not None:
                    startup_restore_mute = requested_mute
            current_identity = managed_config_identity(current_config)
            current_source = current_identity[0] if current_identity else None
            current_speaker = current_identity[1] if current_identity else None

            require_selected_profile_available(
                cdsp,
                selected_speaker,
                selected_revision=selection["revision"],
                current_speaker=current_speaker,
                current_source=current_source,
                current_config=current_config,
            )

            # A restart — of this switcher or of the engine — begins inhibited.
            # Re-apply even a matching managed config once so provenance,
            # Camilla validation, overlays, and the selected revision are all
            # verified before controls may unmute.
            if (
                inhibited
                and current_source
                and current_speaker == selected_speaker
            ):
                transition_guard = audio_control_lock(AUDIO_CONTROL_LOCK_PATH)
                transition_guard.__enter__()
                try:
                    restore_mute = (
                        startup_restore_mute
                        if startup_restore_mute is not None
                        else bool(cdsp.volume.main_mute())
                    )
                    cdsp.volume.set_main_mute(True)
                    target = resolve_config_target(
                        current_source,
                        selected_speaker,
                        selection_revision=selection["revision"],
                    )
                    apply_config(
                        cdsp,
                        target["path"],
                        target=target,
                        restore_mute=restore_mute,
                        audio_lock_held=True,
                    )
                    startup_restore_mute = None
                finally:
                    transition_guard.__exit__(None, None, None)
                time.sleep(CHECK_INTERVAL)
                continue

            # Handle a speaker change before applying that speaker's EQ to the
            # live graph. This keeps one profile's tonal correction from ever
            # being overlaid on another profile's crossover. A same-speaker
            # revision bump means the profile definition itself was edited and
            # must be recompiled and re-applied through the same transaction.
            profile_edit_pending = (
                current_speaker == selected_speaker
                and _speaker_status_revision() not in (None, selection["revision"])
            )
            if current_config and (
                current_speaker != selected_speaker or profile_edit_pending
            ):
                transition_guard = audio_control_lock(AUDIO_CONTROL_LOCK_PATH)
                transition_guard.__enter__()
                try:
                    restore_mute = (
                        startup_restore_mute
                        if startup_restore_mute is not None
                        else bool(cdsp.volume.main_mute())
                    )
                    set_audio_inhibit(AUDIO_READY_PATH)
                    cdsp.volume.set_main_mute(True)
                    if not current_source:
                        error = (
                            "active CamillaDSP config is not managed; "
                            "speaker transition is latched muted"
                        )
                        raise RuntimeError(error)
                    target = resolve_config_target(
                        current_source,
                        selected_speaker,
                        selection_revision=selection["revision"],
                    )
                except Exception as exc:
                    try:
                        _write_speaker_status(
                            {
                                "selected": selected_speaker,
                                "applied": current_speaker,
                                "source": current_source,
                                "config_path": current_config,
                                "config_digest": "",
                                "ok": False,
                                "rollback_ok": False,
                                "error": str(exc),
                                "updated_at": time.time(),
                            }
                        )
                    finally:
                        transition_guard.__exit__(None, None, None)
                    raise
                try:
                    apply_config(
                        cdsp,
                        target["path"],
                        target=target,
                        restore_mute=restore_mute,
                        audio_lock_held=True,
                    )
                    startup_restore_mute = None
                finally:
                    transition_guard.__exit__(None, None, None)
                time.sleep(CHECK_INTERVAL)
                continue

            now = time.monotonic()
            if now >= next_audio_eq_check and current_speaker == selected_speaker:
                next_audio_eq_check = now + AUDIO_EQ_REAPPLY_SECONDS
                try:
                    ensure_current_speaker_audio_eq(cdsp, current_speaker)
                except Exception as exc:
                    try:
                        failed_speaker = current_speaker_selection()["selected"]
                        state = speaker_audio_state(failed_speaker)
                        _write_audio_eq_status(
                            {
                                **status_payload(
                                    state,
                                    applied=False,
                                    effective_preamp=effective_preamp_db(state),
                                    error=str(exc),
                                ),
                                "speaker": failed_speaker,
                            }
                        )
                    except Exception:
                        pass
                    print(f"Audio EQ ensure failed: {exc}", flush=True)

            manual_source = read_manual_source()
            if manual_source and manual_source not in CONFIGS:
                error = f"Unknown manual source override: {manual_source}"
                if error != last_manual_error:
                    print(error, flush=True)
                    last_manual_error = error
                time.sleep(CHECK_INTERVAL)
                continue

            if selected_speaker == DEFAULT_SPEAKER_ID:
                supported_sources = set(CONFIGS)
            else:
                supported_sources = set(
                    load_profile(SPEAKER_PROFILE_DIR, selected_speaker)[
                        "supported_sources"
                    ]
                )
            meter_pairs = motu.read() if motu is not None else {}
            toslink_meter_active = TOSLINK_MOTU_METERS and "toslink" in supported_sources and meter_pairs_active(
                meter_pairs,
                TOSLINK_METER_PAIRS,
            )
            analog_meter_active = ANALOG_MOTU_METERS and "analog" in supported_sources and meter_pairs_active(
                meter_pairs,
                ANALOG_METER_PAIRS,
            )
            toslink_active_timer, toslink_idle_timer = update_meter_timers(
                toslink_meter_active,
                toslink_active_timer,
                toslink_idle_timer,
                TOSLINK_IDLE_SECONDS,
            )
            analog_active_timer, analog_idle_timer = update_meter_timers(
                analog_meter_active,
                analog_active_timer,
                analog_idle_timer,
                ANALOG_IDLE_SECONDS,
            )
            toslink_available = (
                TOSLINK_MOTU_METERS
                and toslink_active_timer >= TOSLINK_ACTIVE_SECONDS
                and toslink_idle_timer < TOSLINK_IDLE_SECONDS
            )
            analog_available = (
                ANALOG_MOTU_METERS
                and "analog" in supported_sources
                and os.path.exists(
                    ANALOG_CFG
                    if selected_speaker == DEFAULT_SPEAKER_ID
                    else SOURCE_BASE_DIR / "analog.yml"
                )
                and analog_active_timer >= ANALOG_ACTIVE_SECONDS
                and analog_idle_timer < ANALOG_IDLE_SECONDS
            )
            lower_priority_meter_available = toslink_available or analog_available
            streamer_hw_active = (
                "streamer" in supported_sources and is_alsa_active("Loopback")
            )
            gadget_hw_available = (
                "gadget" in supported_sources and is_gadget_available()
            )

            # Capture levels belong to whichever config is loaded, so a single
            # read per pass is all the confirmed-playback evidence there is -
            # and it says nothing at all about the sources that are not
            # selected.  A manual override is decided before any of it is
            # consulted, so do not spend the round-trip in that case.
            observing = None if manual_source else current_source
            level_probe: list[bool] = []

            def selected_source_playing() -> bool:
                if not level_probe:
                    level_probe.append(audio_active(cdsp.levels.capture_rms()))
                return level_probe[0]

            def stream_snapshot(name: str, ready: bool) -> SourceSnapshot:
                """A source whose readiness is only 'the stream is open'."""
                return SourceSnapshot(
                    ready=ready,
                    playing=selected_source_playing() if observing == name else None,
                )

            def meter_snapshot(name: str, available: bool) -> SourceSnapshot:
                """A meter source: readiness *is* a signal-presence measure."""
                active = available or (
                    observing == name and selected_source_playing()
                )
                return SourceSnapshot(
                    ready=active, playing=active, self_metering=True
                )

            snapshot = {
                "streamer": stream_snapshot("streamer", streamer_hw_active),
                "gadget": stream_snapshot("gadget", gadget_hw_available),
                "toslink": meter_snapshot("toslink", toslink_available),
                "analog": meter_snapshot("analog", analog_available),
            }

            if DEBUG_MODE:
                probe_debug = " ".join(
                    f"{name}={record.silence:g}s/L{record.backoff_level}"
                    for name, record in sorted(probes.items())
                )
                print(
                    "DEBUG: "
                    f"Streamer HW={streamer_hw_active}, "
                    f"Gadget HW={gadget_hw_available}, "
                    f"TOSLINK meter={toslink_meter_active}/{toslink_active_timer:g}/{toslink_idle_timer:g}, "
                    f"Analog meter={analog_meter_active}/{analog_active_timer:g}/{analog_idle_timer:g}, "
                    f"Lower-priority meter={lower_priority_meter_available}, "
                    f"Last={last_active_source}, "
                    f"Current={current_source}, "
                    f"Probes=[{probe_debug}], "
                    f"Config={os.path.basename(current_config or '')}",
                    flush=True,
                )

            # Each observation stands for the time since the previous one, so
            # a slow pass stretches nothing.  The cap keeps a single stalled
            # pass (a config apply, a reconnect) from satisfying a silence or
            # dwell timeout on its own.
            now = time.monotonic()
            elapsed = (
                CHECK_INTERVAL
                if last_arbitration is None
                else min(max(now - last_arbitration, 0.0), MAX_ARBITRATION_STEP)
            )
            last_arbitration = now
            decision = arbitrate(
                now=now,
                elapsed=elapsed,
                current_source=current_source,
                last_active=last_active_source,
                manual_source=manual_source,
                sources=snapshot,
                probes=probes,
                log=True,
            )
            probes = decision.probes
            last_active_source = decision.last_active

            if decision.source is None:
                # Nothing qualified.  Keep the last config unless the operator
                # asked for the older always-fall-back-to-TOSLINK behaviour.
                if SOURCE_IDLE_MODE == "toslink" and "toslink" in supported_sources:
                    target = resolve_config_target(
                        "toslink",
                        selected_speaker,
                        selection_revision=selection["revision"],
                    )
                else:
                    target = None
                if target is not None and not same_config(
                    current_config, target["path"]
                ):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute
                    )
                elif DEBUG_MODE:
                    print(
                        f"-> Idle: keeping {os.path.basename(current_config or '')}",
                        flush=True,
                    )
                time.sleep(CHECK_INTERVAL)
                continue

            if decision.source == current_source and not decision.manual:
                if DEBUG_MODE:
                    print(f"-> {decision.source}: {decision.reason}", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            try:
                target = resolve_config_target(
                    decision.source,
                    selected_speaker,
                    selection_revision=selection["revision"],
                )
            except Exception as exc:
                if not decision.manual:
                    raise
                # A manual override naming a config this speaker cannot serve
                # is an operator mistake, not a fault: report it and keep going.
                error = f"Manual source/speaker config unavailable: {exc}"
                if error != last_manual_error:
                    print(error, flush=True)
                    last_manual_error = error
                time.sleep(CHECK_INTERVAL)
                continue

            if decision.manual:
                last_manual_error = None

            if not same_config(current_config, target["path"]):
                startup_restore_mute = apply_arbitrated_config(
                    cdsp,
                    target,
                    startup_restore_mute,
                    settle_time=1.5 if decision.source == "gadget" else SETTLE_TIME,
                )
                if decision.manual:
                    # The meters describe an input the operator has taken out
                    # of the running; do not carry their history forward.
                    toslink_active_timer = 0.0
                    toslink_idle_timer = TOSLINK_IDLE_SECONDS
                    analog_active_timer = 0.0
                    analog_idle_timer = ANALOG_IDLE_SECONDS
            elif DEBUG_MODE:
                print(f"-> {decision.source}: {decision.reason}", flush=True)

        except Exception as exc:
            # An unhandled fault leaves the engine unverified from here on:
            # drop the token and re-run startup validation before audio may
            # return, whatever the fault was.
            try:
                set_audio_inhibit(AUDIO_READY_PATH)
            except Exception:
                pass
            startup_configs_validated = False
            # Throttle identical errors to once per 30s. A CamillaDSP outage
            # otherwise floods the journal (~430 lines/incident observed) and
            # wears the SD card; a newly-changed error still logs immediately.
            message = str(exc)
            now = time.monotonic()
            if message != last_error_message or now >= error_log_deadline:
                print(f"Error: {message}", flush=True)
                last_error_message = message
                error_log_deadline = now + 30.0
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
