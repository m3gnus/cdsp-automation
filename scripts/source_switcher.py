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
from pathlib import Path

try:
    import websocket
except ImportError:
    websocket = None

from camilladsp import CamillaClient
from audio_eq import (
    FILTER_PREFIX,
    PIPELINE_DESCRIPTION,
    STEREO_FILTER_PREFIX,
    STEREO_PIPELINE_DESCRIPTION,
    apply_audio_overlay,
    atomic_write_json,
    effective_preamp_db,
    status_payload,
)
from speaker_config import (
    compile_profile_config,
    config_digest,
    load_profile,
    load_yaml_mapping,
    prune_generated_configs,
    profile_catalog,
    write_generated_config,
)
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    DEFAULT_SPEAKER_ID,
    audio_control_lock,
    audio_inhibit_active,
    clear_audio_inhibit,
    read_profile_audio_state,
    read_speaker_selection,
    set_audio_inhibit,
    speaker_selection_lock,
)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("SOURCE_CHECK_INTERVAL", "1.0"))
IDLE_TIMEOUT = float(os.environ.get("SOURCE_IDLE_TIMEOUT", "60"))
LOWER_PRIORITY_ACTIVE_TIMEOUT = float(
    os.environ.get("SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT", "0")
)
SETTLE_TIME = float(os.environ.get("SOURCE_SETTLE_TIME", "2.0"))
AUDIO_THRESHOLD_DB = float(os.environ.get("SOURCE_AUDIO_THRESHOLD_DB", "-80"))
DEBUG_MODE = env_bool("SOURCE_DEBUG", False)
MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
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
WLED_ENV_PATH = os.environ.get("WLED_REACTIVE_ENV", "/etc/default/wled-music-reactive")
DELAY_REAPPLY_SECONDS = float(os.environ.get("SOURCE_DELAY_REAPPLY_SECONDS", "5.0"))
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

    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.last_pairs: dict[int, tuple[int, int]] = {}
        self.last_seen = 0.0
        self.next_connect_attempt = 0.0
        self.next_error_log = 0.0

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
    """Identify only exact legacy or digest-addressed generated configs."""
    for source, target in CONFIGS.items():
        if same_config(current, target):
            return source, DEFAULT_SPEAKER_ID
    if not current:
        return None
    path = Path(current).resolve(strict=False)
    generated_root = SPEAKER_GENERATED_DIR.resolve(strict=False)
    if path.parent.parent != generated_root:
        return None
    digest = path.parent.name
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    parts = path.stem.split("--", 1)
    if (
        len(parts) != 2
        or parts[0] not in CONFIGS
        or parts[1] not in BUILTIN_SPEAKERS
        or parts[1] == DEFAULT_SPEAKER_ID
    ):
        return None
    try:
        config = load_yaml_mapping(path, "managed generated config")
    except (OSError, ValueError):
        return None
    if config_digest(config) != digest:
        return None
    return parts[0], parts[1]


def source_for_config(current: str | None) -> str | None:
    identity = managed_config_identity(current)
    if identity:
        return identity[0]
    return None


def speaker_for_config(current: str | None) -> str | None:
    identity = managed_config_identity(current)
    return identity[1] if identity else None


def current_speaker_selection() -> dict:
    return read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )


def speaker_catalog() -> dict:
    return profile_catalog(SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR)


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
            set_audio_inhibit(
                AUDIO_READY_PATH,
                {
                    "reason": "selected profile unavailable",
                    "target": selected_speaker,
                    "updated_at": time.time(),
                },
            )
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


def _write_speaker_status(payload: dict) -> None:
    try:
        current = json.loads(SPEAKER_STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        current = {}
    comparable = {key: value for key, value in payload.items() if key != "updated_at"}
    old = {key: value for key, value in current.items() if key != "updated_at"}
    if comparable != old:
        atomic_write_json(SPEAKER_STATUS_PATH, payload)


SPECTRUM_SERVICE = os.environ.get("SPECTRUM_SERVICE", "camilladsp-spectrum.service")
# Substring of a capture device that the spectrum analyzer's dsnoop tap
# contends with (e.g. "UltraLitemk5"). Empty disables all lifecycle handling.
SPECTRUM_CONTENDS_WITH = os.environ.get("SPECTRUM_CONTENDS_WITH", "")


def _spectrum_contends(config: dict | None) -> bool:
    if not SPECTRUM_CONTENDS_WITH or not isinstance(config, dict):
        return False
    capture = (config.get("devices") or {}).get("capture") or {}
    return SPECTRUM_CONTENDS_WITH in str(capture.get("device") or "")


def _set_spectrum_service(active: bool) -> None:
    """Best-effort analyzer start/stop; it must never block a source apply."""
    if not SPECTRUM_CONTENDS_WITH:
        return
    action = "start" if active else "stop"
    try:
        subprocess.run(
            ["systemctl", action, SPECTRUM_SERVICE],
            timeout=15,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"spectrum service {action} failed: {exc}", flush=True)


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

    def ready(self, cdsp: CamillaClient, now: float) -> bool:
        state = _processing_state(cdsp)
        active_config = None if state == "inactive" else cdsp.config.active()
        if state != "inactive" and active_config:
            self.next_attempt = 0.0
            self.next_log = 0.0
            self.last_message = None
            return True
        if now < self.next_attempt:
            return False
        self.next_attempt = now + self.retry_seconds

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
            "bypass_user_eq": False,
            "legacy": True,
            "capabilities": {
                "secondary_program": False,
                "meter_bands": {
                    "low": [4, 5],
                    "mid": [2, 3],
                    "high": [0, 1],
                },
            },
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
        "bypass_user_eq": profile["bypass_user_eq"],
        "legacy": False,
        "capabilities": profile["capabilities"],
        "selection_revision": selection_revision,
        "audio_state": audio_state,
        "expected_config": compiled,
    }


def audio_active(levels: object) -> bool:
    if not isinstance(levels, (list, tuple)):
        return False
    return any(
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


# --- WLED light-sync delay ownership -------------------------------------
# The WLED music-reactive controller used to inject this Delay filter into the
# live CamillaDSP config every few seconds, racing this switcher's reloads
# (every source switch dropped the filter for up to 5s, and the two writers
# could clobber each other). The switcher is the SOLE writer of the config, so
# it now owns the filter: it re-asserts it on every config apply and
# periodically, with no second writer to race. Delay parameters are read from
# the WLED env file so the control-UI "sync" sliders keep working unchanged.


def read_wled_delay_settings() -> tuple[bool, float, str]:
    enabled, delay_ms, name = False, 0.0, "wled_light_sync_delay"
    try:
        with open(WLED_ENV_PATH, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if key == "CAMILLA_DELAY_ENABLED":
                    enabled = value.lower() in {"1", "true", "yes", "on"}
                elif key == "CAMILLA_DELAY_MS":
                    try:
                        delay_ms = float(value)
                    except ValueError:
                        enabled, delay_ms = False, 0.0
                elif key == "CAMILLA_DELAY_FILTER_NAME" and value:
                    name = value
    except FileNotFoundError:
        pass
    return enabled, delay_ms, name


def _delay_channels(config: dict) -> list:
    capture = config.get("devices", {}).get("capture", {})
    channels = int(capture.get("channels") or 2)
    # The installation's second stereo program does not drive the WLED strip.
    # Keep light-sync latency on the main stereo input only.
    return list(range(min(2, max(1, channels))))


def _remove_delay(config: dict, name: str) -> tuple:
    changed = False
    new_config = copy.deepcopy(config)
    filters = new_config.get("filters", {})
    if name in filters:
        del filters[name]
        changed = True
    pipeline = []
    for step in new_config.get("pipeline", []):
        names = step.get("names")
        if isinstance(names, list) and name in names:
            remaining = [n for n in names if n != name]
            if remaining:
                next_step = copy.deepcopy(step)
                next_step["names"] = remaining
                pipeline.append(next_step)
            changed = True
        else:
            pipeline.append(step)
    new_config["pipeline"] = pipeline
    return new_config, changed


def _has_requested_delay(config: dict, name: str, delay_ms: float) -> bool:
    delay_filter = config.get("filters", {}).get(name)
    if not delay_filter or delay_filter.get("type") != "Delay":
        return False
    params = delay_filter.get("parameters", {})
    try:
        delay_matches = abs(float(params.get("delay")) - delay_ms) < 0.001
    except (TypeError, ValueError):
        delay_matches = False
    if not delay_matches or params.get("unit") != "ms":
        return False
    expected_channels = _delay_channels(config)
    matches = []
    for step in config.get("pipeline", []):
        names = step.get("names")
        if isinstance(names, list) and name in names:
            matches.append(step)
    return len(matches) == 1 and matches[0].get("channels") == expected_channels


def _add_delay(config: dict, name: str, delay_ms: float) -> dict:
    new_config, _ = _remove_delay(config, name)
    channels = _delay_channels(new_config)
    filters = new_config.setdefault("filters", {})
    filters[name] = {
        "type": "Delay",
        "parameters": {"delay": delay_ms, "unit": "ms", "subsample": False},
    }
    new_config.setdefault("pipeline", []).insert(
        0,
        {
            "type": "Filter",
            "channels": channels,
            "names": [name],
            "description": "WLED light sync delay (owned by source switcher)",
            "bypassed": False,
        },
    )
    return new_config


def ensure_delay_filter(cdsp: CamillaClient) -> None:
    """Make the live config's WLED sync-delay filter match the WLED env.

    Idempotent: only calls set_active when the filter is missing/wrong (add) or
    present-but-disabled (remove), so steady state performs no writes.
    """
    enabled, delay_ms, name = read_wled_delay_settings()
    config = cdsp.config.active()
    if not config:
        return
    if enabled:
        if _has_requested_delay(config, name, delay_ms):
            return
        cdsp.config.set_active(_add_delay(config, name, delay_ms))
        print(f"WLED light-sync delay applied: {delay_ms:.1f}ms", flush=True)
    else:
        new_config, changed = _remove_delay(config, name)
        if changed:
            cdsp.config.set_active(new_config)
            print("WLED light-sync delay removed (disabled)", flush=True)


def _write_audio_eq_status(payload: dict) -> None:
    """Publish apply convergence without rewriting an unchanged status file."""
    try:
        with open(AUDIO_EQ_STATUS_PATH, "r", encoding="utf-8") as handle:
            current = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        current = {}
    comparable = {key: value for key, value in payload.items() if key != "updated_at"}
    old_comparable = {
        key: value for key, value in current.items() if key != "updated_at"
    }
    if comparable != old_comparable:
        atomic_write_json(Path(AUDIO_EQ_STATUS_PATH), payload)


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


def _audio_overlay_matches(actual: dict, expected: dict) -> bool:

    actual_filters = actual.get("filters", {})
    expected_filters = expected.get("filters", {})
    expected_owned = {
        name: _comparable_filter(value)
        for name, value in expected_filters.items()
        if name.startswith(FILTER_PREFIX) or name.startswith(STEREO_FILTER_PREFIX)
    }
    actual_owned = {
        name: _comparable_filter(value)
        for name, value in actual_filters.items()
        if name.startswith(FILTER_PREFIX) or name.startswith(STEREO_FILTER_PREFIX)
    }
    if actual_owned != expected_owned:
        return False
    actual_steps = [
        step
        for step in actual.get("pipeline", [])
        if step.get("description")
        in {PIPELINE_DESCRIPTION, STEREO_PIPELINE_DESCRIPTION}
    ]
    expected_steps = [
        step
        for step in expected.get("pipeline", [])
        if step.get("description")
        in {PIPELINE_DESCRIPTION, STEREO_PIPELINE_DESCRIPTION}
    ]
    return actual_steps == expected_steps


def _configs_equivalent(actual: dict, expected: dict) -> bool:
    """Ignore CamillaDSP's materialization of omitted optional fields."""
    def comparable(config: dict) -> dict:
        result = copy.deepcopy(config)
        result["filters"] = {
            name: _comparable_filter(value)
            for name, value in result.get("filters", {}).items()
        }
        return result

    return comparable(actual) == comparable(expected)


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
            safe_state["stereo"]["enabled"] = False
            safe_state["stereo"]["muted"] = False
            safe_state["stereo"]["trim_db"] = 0.0
            safe_state["stereo"]["preamp_db"] = 0.0
            updated, _preamp = apply_audio_overlay(config, safe_state)
            if not _configs_equivalent(config, updated):
                cdsp.config.set_active(updated)
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
                cdsp.config.set_active(updated)
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
        cdsp.config.set_active(updated)
        print(
            f"Audio EQ revision {state['revision']} applied "
            f"({len(state['bands'])} bands, preamp {preamp:+.1f}dB)",
            flush=True,
        )
        accepted = cdsp.config.active() or {}
        if not _audio_overlay_matches(accepted, updated):
            raise RuntimeError("CamillaDSP did not confirm the requested audio overlay")
    _write_audio_eq_status(
        {
            **status_payload(state, applied=True, effective_preamp=preamp),
            "speaker": speaker_id,
        }
    )


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
        set_audio_inhibit(
            AUDIO_READY_PATH,
            {
                "reason": "config transition",
                "target": target.get("speaker") if target else None,
                "source": target.get("source") if target else None,
                "updated_at": time.time(),
            },
        )
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
    try:
        if target and target.get("selection_revision") is not None:
            selection_guard = speaker_selection_lock(SPEAKER_SELECTION_PATH)
            selection_guard.__enter__()
            selection_guard_entered = True
        if target and not target.get("legacy", False):
            on_disk = load_yaml_mapping(Path(file_path), "generated config")
            if config_digest(on_disk) != target["digest"]:
                raise RuntimeError("generated config integrity check failed")
        if target and target.get("selection_revision") is not None:
            current_selection = current_speaker_selection()
            if (
                current_selection["selected"] != target["speaker"]
                or current_selection["revision"] != target["selection_revision"]
            ):
                raise RuntimeError("speaker selection changed before config reload")
        # Release the analyzer's dsnoop hold before a config that captures
        # the same hardware; harmless no-op for loopback/gadget captures.
        target_config = target.get("expected_config") if target else None
        if _spectrum_contends(target_config):
            _set_spectrum_service(False)
        cdsp.config.set_file_path(file_path)
        cdsp.general.reload()
        time.sleep(settle_time)
        state = _processing_state(cdsp)
        if state not in {"running", "paused"}:
            raise RuntimeError(f"CamillaDSP did not reach a safe running state: {state}")
        if not same_config(cdsp.config.file_path(), file_path):
            raise RuntimeError("CamillaDSP did not retain the requested config path")
        if target:
            accepted = cdsp.config.active()
            expected = target.get("expected_config")
            if expected is not None and (
                not accepted or not _configs_equivalent(accepted, expected)
            ):
                raise RuntimeError("CamillaDSP active config differs from requested config")

        if target and target.get("selection_revision") is not None:
            current_selection = current_speaker_selection()
            if (
                current_selection["selected"] != target["speaker"]
                or current_selection["revision"] != target["selection_revision"]
            ):
                raise RuntimeError("speaker selection changed during config transition")

        # Re-assert owned overlays before sound returns.
        ensure_delay_filter(cdsp)
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
        maximum = float(target.get("max_volume_db", 0)) if target else 0.0
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
                    "ok": True,
                    "error": "",
                    "updated_at": time.time(),
                }
            )
        cdsp.volume.set_main_mute(previous_mute)
        clear_pending_transition(target)
        clear_audio_inhibit(AUDIO_READY_PATH)
        if not _spectrum_contends(target_config):
            _set_spectrum_service(True)
        if target and not target.get("legacy", False):
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
            set_audio_inhibit(
                AUDIO_READY_PATH,
                {"reason": "config transition failed", "updated_at": time.time()},
            )
            cdsp.volume.set_main_mute(True)
        except Exception:
            pass
        rollback_ok = False
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
                    and bool(rollback_active)
                    and _configs_equivalent(rollback_active, previous_expected)
                )
            except Exception as rollback_exc:
                print(f"Config rollback failed: {rollback_exc}", flush=True)
        # Never unmute automatically after an uncertain profile transition.
        try:
            cdsp.volume.set_main_mute(True)
        except Exception:
            pass
        # Re-align the analyzer with whatever config is actually loaded now.
        if rollback_ok:
            _set_spectrum_service(not _spectrum_contends(previous_expected))
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


def log_idle(source: str, seconds: float) -> None:
    if DEBUG_MODE and int(seconds) > 0 and int(seconds) % 5 == 0:
        print(f"-> {source}: idle {seconds:g}/{IDLE_TIMEOUT:g}s", flush=True)


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
        set_audio_inhibit(
            AUDIO_READY_PATH,
            {"reason": "source switcher startup", "updated_at": time.time()},
        )
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    motu = (
        MotuMeterReader(MOTU_WS_URL)
        if TOSLINK_MOTU_METERS or ANALOG_MOTU_METERS
        else None
    )
    streamer_silence_timer = 0.0
    gadget_silence_timer = 0.0
    toslink_active_timer = 0.0
    toslink_idle_timer = TOSLINK_IDLE_SECONDS
    analog_active_timer = 0.0
    analog_idle_timer = ANALOG_IDLE_SECONDS
    last_active_source = None
    last_manual_error = None
    error_log_deadline = 0.0
    last_error_message = None
    next_delay_check = 0.0
    next_audio_eq_check = 0.0
    startup_restore_mute: bool | None = None
    startup_configs_validated = False
    recovery = ConfigRecoveryGuard()

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            if not recovery.ready(cdsp, time.monotonic()):
                time.sleep(CHECK_INTERVAL)
                continue

            if startup_restore_mute is None and audio_inhibit_active(AUDIO_READY_PATH):
                startup_restore_mute = mute_for_startup_validation(cdsp)
            current_config = cdsp.config.file_path()
            selection = current_speaker_selection()
            selected_speaker = selection["selected"]
            if not startup_configs_validated:
                # Missing configs now fail only after the live engine is muted.
                validate_configs(selected_speaker)
                startup_configs_validated = True
            if audio_inhibit_active(AUDIO_READY_PATH):
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

            # A restart begins inhibited. Re-apply even a matching managed
            # config once so provenance, Camilla validation, overlays, and the
            # selected revision are all verified before controls may unmute.
            if (
                audio_inhibit_active(AUDIO_READY_PATH)
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
                    set_audio_inhibit(
                        AUDIO_READY_PATH,
                        {
                            "reason": "speaker transition",
                            "target": selected_speaker,
                            "source": current_source,
                            "updated_at": time.time(),
                        },
                    )
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

            # Own the WLED light-sync delay filter: re-assert periodically so it
            # self-heals and picks up UI changes to CAMILLA_DELAY_MS/ENABLED.
            now = time.monotonic()
            if now >= next_delay_check:
                next_delay_check = now + DELAY_REAPPLY_SECONDS
                try:
                    ensure_delay_filter(cdsp)
                except Exception as exc:
                    print(f"WLED delay ensure failed: {exc}", flush=True)

            if now >= next_audio_eq_check and current_speaker == selected_speaker:
                next_audio_eq_check = now + AUDIO_EQ_REAPPLY_SECONDS
                try:
                    ensure_audio_eq(cdsp)
                except Exception as exc:
                    try:
                        state = speaker_audio_state(
                            current_speaker_selection()["selected"]
                        )
                        _write_audio_eq_status(
                            {
                                **status_payload(
                                    state,
                                    applied=False,
                                    effective_preamp=effective_preamp_db(state),
                                    error=str(exc),
                                ),
                                "speaker": current_speaker_selection()["selected"],
                            }
                        )
                    except Exception:
                        pass
                    print(f"Audio EQ ensure failed: {exc}", flush=True)

            manual_source = read_manual_source()
            if manual_source:
                if manual_source not in CONFIGS:
                    error = f"Unknown manual source override: {manual_source}"
                    if error != last_manual_error:
                        print(error, flush=True)
                        last_manual_error = error
                    time.sleep(CHECK_INTERVAL)
                    continue
                try:
                    target = resolve_config_target(
                        manual_source,
                        selected_speaker,
                        selection_revision=selection["revision"],
                    )
                except Exception as exc:
                    error = f"Manual source/speaker config unavailable: {exc}"
                    if error != last_manual_error:
                        print(error, flush=True)
                        last_manual_error = error
                    time.sleep(CHECK_INTERVAL)
                    continue

                last_manual_error = None
                if not same_config(current_config, target["path"]):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute
                    )
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    toslink_active_timer = 0.0
                    toslink_idle_timer = TOSLINK_IDLE_SECONDS
                    analog_active_timer = 0.0
                    analog_idle_timer = ANALOG_IDLE_SECONDS
                    last_active_source = f"manual:{manual_source}"
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
            if DEBUG_MODE:
                print(
                    "DEBUG: "
                    f"Streamer HW={streamer_hw_active}, "
                    f"Gadget HW={gadget_hw_available}, "
                    f"TOSLINK meter={toslink_meter_active}/{toslink_active_timer:g}/{toslink_idle_timer:g}, "
                    f"Analog meter={analog_meter_active}/{analog_active_timer:g}/{analog_idle_timer:g}, "
                    f"Last={last_active_source}, "
                    f"Current={current_source}, "
                    f"ST={streamer_silence_timer:g}, "
                    f"GT={gadget_silence_timer:g}, "
                    f"Config={os.path.basename(current_config or '')}",
                    flush=True,
                )

            # Keep the current source while it still has confirmed audio.
            if current_source == "streamer" and streamer_hw_active:
                last_active_source = "streamer"
                if audio_active(cdsp.levels.capture_rms()):
                    streamer_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Streamer: current source active", flush=True)
                    time.sleep(CHECK_INTERVAL)
                    continue
                else:
                    streamer_silence_timer += CHECK_INTERVAL
                    log_idle("Streamer", streamer_silence_timer)

                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources", flush=True)
                last_active_source = None

            elif current_source == "streamer" and last_active_source == "streamer":
                streamer_silence_timer += CHECK_INTERVAL
                log_idle("Streamer grace", streamer_silence_timer)
                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer grace ended early for lower-priority meter source",
                            flush=True,
                        )
                last_active_source = None

            if current_source == "gadget" and gadget_hw_available:
                last_active_source = "gadget"
                if audio_active(cdsp.levels.capture_rms()):
                    gadget_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Gadget: current source active", flush=True)
                    time.sleep(CHECK_INTERVAL)
                    continue
                else:
                    gadget_silence_timer += CHECK_INTERVAL
                    log_idle("Gadget", gadget_silence_timer)

                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Gadget idle timeout - checking other sources", flush=True)
                last_active_source = None

            elif current_source == "gadget" and last_active_source == "gadget":
                gadget_silence_timer += CHECK_INTERVAL
                log_idle("Gadget grace", gadget_silence_timer)
                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget grace ended early for lower-priority meter source",
                            flush=True,
                        )
                last_active_source = None

            if current_source == "toslink" and (
                toslink_available or audio_active(cdsp.levels.capture_rms())
            ):
                last_active_source = "toslink"
                if DEBUG_MODE:
                    print("-> TOSLINK: current source active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            if current_source == "analog" and (
                analog_available or audio_active(cdsp.levels.capture_rms())
            ):
                last_active_source = "analog"
                if DEBUG_MODE:
                    print("-> Analog: current source active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            # Priority 1: Streamer (AirPlay via ALSA Loopback), only when changing sources.
            if current_source != "streamer" and streamer_hw_active:
                last_active_source = "streamer"
                target = resolve_config_target(
                    "streamer", selected_speaker,
                    selection_revision=selection["revision"],
                )
                if not same_config(current_config, target["path"]):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute
                    )
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    streamer_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Streamer: audio active", flush=True)
                else:
                    streamer_silence_timer += CHECK_INTERVAL
                    log_idle("Streamer", streamer_silence_timer)

                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources", flush=True)
                last_active_source = None

            # Priority 2: USB Gadget, only when changing sources.
            if current_source != "gadget" and gadget_hw_available:
                last_active_source = "gadget"
                target = resolve_config_target(
                    "gadget", selected_speaker,
                    selection_revision=selection["revision"],
                )
                if not same_config(current_config, target["path"]):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute, settle_time=1.5
                    )
                    gadget_silence_timer = 0.0
                    streamer_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    gadget_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Gadget: audio active", flush=True)
                else:
                    gadget_silence_timer += CHECK_INTERVAL
                    log_idle("Gadget", gadget_silence_timer)

                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Gadget idle timeout - checking other sources", flush=True)
                last_active_source = None

            # Priority 3: TOSLINK via MOTU input meters.
            if current_source != "toslink" and toslink_available:
                last_active_source = "toslink"
                target = resolve_config_target(
                    "toslink", selected_speaker,
                    selection_revision=selection["revision"],
                )
                if not same_config(current_config, target["path"]):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute
                    )
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue
                if DEBUG_MODE:
                    print("-> TOSLINK: meter active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            # Priority 4: Analog via MOTU input meters, disabled by default.
            if current_source != "analog" and analog_available:
                last_active_source = "analog"
                target = resolve_config_target(
                    "analog", selected_speaker,
                    selection_revision=selection["revision"],
                )
                if not same_config(current_config, target["path"]):
                    startup_restore_mute = apply_arbitrated_config(
                        cdsp, target, startup_restore_mute
                    )
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue
                if DEBUG_MODE:
                    print("-> Analog: meter active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            if SOURCE_IDLE_MODE == "toslink" and "toslink" in supported_sources:
                target = resolve_config_target(
                    "toslink", selected_speaker,
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
                streamer_silence_timer = 0.0
                gadget_silence_timer = 0.0
                last_active_source = None
            elif DEBUG_MODE:
                print(
                    f"-> Idle: keeping {os.path.basename(current_config or '')}",
                    flush=True,
                )

        except Exception as exc:
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
