#!/usr/bin/env python3
"""One-shot health report for the CamillaDSP automation stack.

Run it after an OS upgrade, when something sounds wrong, or before asking for
help: it checks the pieces the services depend on and prints one report.

    ~/camilladsp/.venv/bin/python3 ~/camilladsp/scripts/diagnose.py [--json]

Everything here only reads.  In particular the MOTU is never connected to:
it serves one WebSocket client at a time, so a probe would drop the source
switcher's meters.  Its state comes from what the switcher publishes, plus a
ping.  The exit status is 1 when any check failed, else 0.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import settings

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MARKS = {OK: "✔", WARN: "!", FAIL: "✘", INFO: "·"}

SERVICES = {
    "camilladsp.service": True,
    "cdsp-source-switcher.service": True,
    "cdsp-trigger.service": False,
    "cdsp-remote.service": False,
    "cdsp-control-ui.service": False,
    "airplay-volume-bridge.service": False,
    "shairport-sync.service": False,
    "raspotify.service": False,
}
SOURCES = ("streamer", "gadget", "toslink", "analog")
REQUIRED_SOURCES = ("streamer", "gadget", "toslink")
NUMERIC_ENV_KEYS = (
    "CDSP_PORT",
    "POWER_GPIO",
    "MOTU_MAIN_VOLUME_MAX_DB",
    "REMOTE_VOLUME_MIN",
    "REMOTE_VOLUME_MAX",
    "AIRPLAY_VOLUME_MIN_DB",
    "AIRPLAY_VOLUME_MAX_DB",
    "INSTALLATION_UI_PORT",
)
LOCK_WAIT_SECONDS = 5.0
DISK_WARN_FREE_BYTES = 200 * 1024 * 1024
# vcgencmd get_throttled bits: now (0-3) and since boot (16-19).
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "ARM frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage since boot",
    17: "frequency capped since boot",
    18: "throttled since boot",
    19: "soft temperature limit since boot",
}


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""


def run(command: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, "", f"{command[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", f"timed out after {timeout:g}s")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ------------------------------------------------------------------- checks


def check_platform(env: dict[str, str]) -> list[Check]:
    checks = []
    version = ".".join(map(str, sys.version_info[:3]))
    checks.append(
        Check(
            "System",
            "Python",
            OK if sys.version_info >= (3, 10) else FAIL,
            f"{version} (3.10 or newer required)",
        )
    )
    for label, path in (("root filesystem", Path("/")), ("state directory", settings.STATE_DIR)):
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            checks.append(Check("System", f"Free space ({label})", WARN, str(exc)))
            continue
        free_mb = usage.free / 1024 / 1024
        checks.append(
            Check(
                "System",
                f"Free space ({label})",
                OK if usage.free >= DISK_WARN_FREE_BYTES else WARN,
                f"{free_mb:.0f} MB free",
            )
        )
    throttled = run(["vcgencmd", "get_throttled"], timeout=5)
    if throttled.returncode == 0 and "=" in throttled.stdout:
        try:
            value = int(throttled.stdout.split("=", 1)[1].strip(), 16)
        except ValueError:
            value = None
        if value is None:
            checks.append(Check("System", "Power / throttling", INFO, throttled.stdout.strip()))
        else:
            flags = [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]
            now = any(value & (1 << bit) for bit in range(4))
            checks.append(
                Check(
                    "System",
                    "Power / throttling",
                    FAIL if now else (WARN if flags else OK),
                    ", ".join(flags) if flags else "no under-voltage or throttling since boot",
                )
            )
    else:
        checks.append(Check("System", "Power / throttling", INFO, "vcgencmd not available"))
    return checks


def check_env(env_path: Path) -> tuple[list[Check], dict[str, str]]:
    try:
        env = read_env_file(env_path)
    except OSError as exc:
        return [Check("Settings", "Env file", FAIL, f"{env_path}: {exc}")], {}
    checks = [Check("Settings", "Env file", OK, str(env_path))]
    bad = []
    for key in NUMERIC_ENV_KEYS:
        value = env.get(key, "")
        if not value:
            continue
        try:
            float(value)
        except ValueError:
            bad.append(f"{key}={value!r}")
    checks.append(
        Check(
            "Settings",
            "Numeric settings",
            FAIL if bad else OK,
            ("not numbers: " + ", ".join(bad)) if bad else "all parse",
        )
    )
    token = env.get("INSTALLATION_UI_TOKEN", "")
    host = env.get("INSTALLATION_UI_HOST", "0.0.0.0")
    if not token and host not in {"127.0.0.1", "localhost", "::1"}:
        checks.append(
            Check(
                "Settings",
                "Control UI exposure",
                INFO,
                f"listening on {host} without INSTALLATION_UI_TOKEN: trusted LAN only",
            )
        )
    return checks, env


def systemctl_states(units: list[str]) -> dict[str, dict[str, str]]:
    result = run(
        ["systemctl", "show", "--property=Id,LoadState,ActiveState,SubState,NRestarts", *units],
        timeout=10,
    )
    states: dict[str, dict[str, str]] = {}
    for stanza in result.stdout.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in stanza.splitlines() if "=" in line)
        if fields.get("Id"):
            states[fields["Id"]] = fields
    return states


def check_services() -> list[Check]:
    states = systemctl_states(list(SERVICES))
    if not states:
        return [Check("Services", "systemd", WARN, "systemctl gave no answer")]
    checks = []
    for unit, required in SERVICES.items():
        state = states.get(unit, {})
        load = state.get("LoadState", "unknown")
        active = state.get("ActiveState", "unknown")
        restarts = state.get("NRestarts", "0")
        if load == "not-found":
            checks.append(
                Check("Services", unit, FAIL if required else INFO, "not installed")
            )
            continue
        detail = f"{active} ({state.get('SubState', '?')})"
        if restarts not in {"", "0"}:
            detail += f", restarted {restarts}x since it was started"
        if active == "active":
            status = WARN if restarts not in {"", "0"} else OK
        elif active == "failed":
            status = FAIL
        else:
            status = FAIL if required else WARN
        checks.append(Check("Services", unit, status, detail))
    return checks


def check_camilla(env: dict[str, str]) -> tuple[list[Check], dict[str, Any]]:
    host = env.get("CDSP_HOST", settings.CDSP_HOST)
    try:
        port = int(env.get("CDSP_PORT", settings.CDSP_PORT))
    except ValueError:
        return [Check("CamillaDSP", "Websocket", FAIL, "CDSP_PORT is not a number")], {}
    try:
        from camilladsp import CamillaClient
    except ImportError as exc:
        return [Check("CamillaDSP", "Websocket", FAIL, f"pycamilladsp missing: {exc}")], {}
    client = CamillaClient(host, port)
    try:
        client.connect()
        live = {
            "state": str(client.general.state()).replace("ProcessingState.", ""),
            "config_file": client.config.file_path(),
            "volume_db": client.volume.main_volume(),
            "muted": client.volume.main_mute(),
        }
    except Exception as exc:
        return [Check("CamillaDSP", "Websocket", FAIL, f"{host}:{port}: {exc}")], {}
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    state = live["state"].upper()
    checks = [
        Check("CamillaDSP", "Websocket", OK, f"{host}:{port}"),
        Check(
            "CamillaDSP",
            "Processing state",
            OK if state in {"RUNNING", "PAUSED"} else (WARN if state == "INACTIVE" else FAIL),
            live["state"],
        ),
        Check("CamillaDSP", "Active config", INFO, str(live["config_file"])),
        Check(
            "CamillaDSP",
            "Volume",
            INFO,
            f"{float(live['volume_db']):+.1f} dB, {'muted' if live['muted'] else 'unmuted'}",
        ),
    ]
    return checks, live


def validate_config(path: Path) -> tuple[bool, str]:
    result = run([settings.CAMILLA_BINARY, "-c", str(path)], timeout=15)
    if result.returncode == 0:
        return True, "passes camilladsp -c"
    lines = (result.stdout + result.stderr).strip().splitlines()
    return False, lines[-1] if lines else f"exit {result.returncode}"


def check_configs(live: dict[str, Any], env: dict[str, str]) -> list[Check]:
    from speaker_config import profile_catalog
    from speaker_profiles import (
        BUILTIN_SPEAKERS,
        DEFAULT_SPEAKER_ID,
        read_speaker_selection,
    )

    # A shell run has not loaded the env file the units load.
    config_dir = Path(env.get("CDSP_CONFIG_DIR") or settings.CONFIG_DIR)
    checks = []
    try:
        selection = read_speaker_selection(
            settings.SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
        )
    except Exception as exc:
        return [Check("Configs", "Speaker selection", FAIL, str(exc))]
    selected = selection["selected"]
    checks.append(
        Check("Configs", "Speaker selection", OK, f"{selected} (revision {selection['revision']})")
    )
    if selected == DEFAULT_SPEAKER_ID:
        for source in SOURCES:
            path = config_dir / f"{source}.yml"
            if not path.is_file():
                checks.append(
                    Check(
                        "Configs",
                        f"{source}.yml",
                        FAIL if source in REQUIRED_SOURCES else INFO,
                        f"missing: {path}" if source in REQUIRED_SOURCES else "not configured (optional)",
                    )
                )
                continue
            valid, detail = validate_config(path)
            checks.append(Check("Configs", f"{source}.yml", OK if valid else FAIL, detail))
    else:
        try:
            catalog = profile_catalog(
                settings.SPEAKER_PROFILE_DIR, settings.SOURCE_BASE_DIR, config_dir
            )
        except Exception as exc:
            return checks + [Check("Configs", f"Profile {selected}", FAIL, str(exc))]
        entry = catalog.get(selected, {})
        checks.append(
            Check(
                "Configs",
                f"Profile {selected}",
                OK if entry.get("available") else FAIL,
                "available" if entry.get("available") else entry.get("reason") or "not installed",
            )
        )
        for source in entry.get("supported_sources") or []:
            base = settings.SOURCE_BASE_DIR / f"{source}.yml"
            checks.append(
                Check(
                    "Configs",
                    f"Source base {source}",
                    OK if base.is_file() or entry.get("operator_config") else FAIL,
                    str(base) if base.is_file() else "operator config" if entry.get("operator_config") else f"missing: {base}",
                )
            )
    config_file = live.get("config_file")
    if config_file and Path(config_file).is_file():
        valid, detail = validate_config(Path(config_file))
        checks.append(
            Check("Configs", "Running config", OK if valid else FAIL, f"{Path(config_file).name}: {detail}")
        )
    status = read_json(settings.SPEAKER_STATUS_PATH)
    if status is None:
        checks.append(
            Check("Configs", "Last transition", WARN, "the switcher has not published a status")
        )
    elif status.get("ok") is True:
        limit = status.get("volume_limit_db")
        checks.append(
            Check(
                "Configs",
                "Last transition",
                OK,
                f"{status.get('source')}/{status.get('applied')} applied, ceiling {limit} dB",
            )
        )
    else:
        checks.append(
            Check(
                "Configs",
                "Last transition",
                FAIL,
                f"failed: {status.get('error') or 'unknown error'} (engine left muted)",
            )
        )
    return checks


def check_lock() -> list[Check]:
    path = settings.AUDIO_CONTROL_LOCK_PATH
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return [Check("Locks", "Audio-control lock", WARN, f"missing: {path}")]
    except PermissionError:
        return [
            Check(
                "Locks",
                "Audio-control lock",
                FAIL,
                f"{path} is not writable by this user: every volume writer needs it",
            )
        ]
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return [
                        Check(
                            "Locks",
                            "Audio-control lock",
                            FAIL,
                            f"held for more than {LOCK_WAIT_SECONDS:g}s: a volume writer or "
                            "the switcher is stuck holding it",
                        )
                    ]
                time.sleep(0.1)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    return [Check("Locks", "Audio-control lock", OK, "free and writable")]


def check_motu(env: dict[str, str]) -> list[Check]:
    url = env.get("MOTU_WS_URL", "")
    host = urllib.parse.urlparse(url).hostname if url else None
    checks = []
    if host:
        ping = run(["ping", "-c", "1", "-W", "1", host], timeout=5)
        checks.append(
            Check(
                "MOTU",
                "Network",
                OK if ping.returncode == 0 else FAIL,
                f"{host} {'answers ping' if ping.returncode == 0 else 'does not answer ping'}",
            )
        )
    else:
        checks.append(Check("MOTU", "Network", WARN, "MOTU_WS_URL is not set"))
    status = read_json(settings.MOTU_VOLUME_STATUS_PATH)
    if status is None:
        checks.append(
            Check("MOTU", "Switcher connection", WARN, "the switcher has not published MOTU status")
        )
    elif status.get("known"):
        checks.append(
            Check(
                "MOTU",
                "Switcher connection",
                OK if status.get("writable") else WARN,
                f"main volume {status.get('volume_db')} dB"
                + ("" if status.get("writable") else f"; {status.get('reason')}"),
            )
        )
    else:
        checks.append(
            Check("MOTU", "Switcher connection", FAIL, status.get("reason") or "unknown")
        )
    return checks


def check_remote(env: dict[str, str]) -> list[Check]:
    import remote_keymap

    checks = []
    name = env.get("REMOTE_NAME", "")
    try:
        devices = Path("/proc/bus/input/devices").read_text(errors="replace")
    except OSError:
        devices = ""
    names = re.findall(r'^N: Name="(.*)"$', devices, flags=re.M)
    if name:
        checks.append(
            Check(
                "Remote",
                "Input device",
                OK if name in names else WARN,
                f"'{name}' {'connected' if name in names else 'not connected (asleep or unpaired?)'}",
            )
        )
    _keymap, problem = remote_keymap.load_keymap(settings.REMOTE_KEYMAP_PATH)
    checks.append(
        Check(
            "Remote",
            "Key map",
            FAIL if problem else OK,
            problem
            or (
                str(settings.REMOTE_KEYMAP_PATH)
                if settings.REMOTE_KEYMAP_PATH.exists()
                else "built-in defaults"
            ),
        )
    )
    return checks


def check_state_files() -> list[Check]:
    checks = []
    receipt = read_json(settings.ISO226_CAPABILITY_PATH)
    checks.append(
        Check(
            "State",
            "ISO 226 engine",
            OK if receipt and receipt.get("engine") == "Iso226" else INFO,
            "installed" if receipt and receipt.get("engine") == "Iso226" else "not installed (optional)",
        )
    )
    import source_volume

    try:
        raw = settings.SOURCE_VOLUME_PATH.read_text(encoding="utf-8")
        json.loads(raw)
        pairs = sum(len(v) for v in source_volume.read_state()["speakers"].values())
        checks.append(Check("State", "Per-source volume memory", OK, f"{pairs} speaker/source level(s)"))
    except FileNotFoundError:
        checks.append(Check("State", "Per-source volume memory", INFO, "nothing remembered yet"))
    except (OSError, ValueError) as exc:
        checks.append(Check("State", "Per-source volume memory", WARN, f"unreadable: {exc}"))
    return checks


def check_recent_errors() -> list[Check]:
    checks = []
    for unit in ("cdsp-source-switcher.service", "camilladsp.service"):
        result = run(
            ["journalctl", "-u", unit, "--since", "-1h", "--no-pager", "-q", "-o", "cat"],
            timeout=10,
        )
        if result.returncode != 0:
            checks.append(Check("Journal", unit, INFO, "journal not readable by this user"))
            continue
        errors = [
            line
            for line in result.stdout.splitlines()
            if re.search(r"\b(error|failed|rejected)\b", line, flags=re.I)
        ]
        checks.append(
            Check(
                "Journal",
                unit,
                WARN if errors else OK,
                f"{len(errors)} error line(s) in the last hour; latest: {errors[-1][:160]}"
                if errors
                else "no errors in the last hour",
            )
        )
    return checks


def diagnose() -> list[Check]:
    checks: list[Check] = []
    env_checks, env = check_env(settings.ENV_FILE)
    checks += check_platform(env)
    checks += env_checks
    checks += check_services()
    camilla_checks, live = check_camilla(env)
    checks += camilla_checks
    steps: list[Callable[[], list[Check]]] = [
        lambda: check_configs(live, env),
        check_lock,
        lambda: check_motu(env),
        lambda: check_remote(env),
        check_state_files,
        check_recent_errors,
    ]
    for step in steps:
        try:
            checks += step()
        except Exception as exc:  # one broken check must not hide the rest
            checks.append(Check("Diagnose", getattr(step, "__name__", "check"), WARN, f"check crashed: {exc}"))
    return checks


def render(checks: list[Check]) -> str:
    lines = []
    group = None
    for check in checks:
        if check.group != group:
            group = check.group
            lines.append(f"\n{group}")
        detail = f"  {check.detail}" if check.detail else ""
        lines.append(f"  {MARKS[check.status]} {check.name:<30}{detail}")
    counts = {status: sum(c.status == status for c in checks) for status in (OK, WARN, FAIL)}
    lines.append(
        f"\n{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)"
    )
    return "\n".join(lines).lstrip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    checks = diagnose()
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        print(render(checks))
    return 1 if any(check.status == FAIL for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
