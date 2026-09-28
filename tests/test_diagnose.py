"""The health check: each check's verdicts, and that it only ever reads."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import diagnose
import settings
from diagnose import FAIL, INFO, OK, WARN


REPOSITORY = Path(__file__).resolve().parents[1]


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_env_numbers_are_checked(tmp_path: Path) -> None:
    env = tmp_path / "cdsp-automation.env"
    env.write_text("# c\nCDSP_PORT=1234\nPOWER_GPIO=four\nREMOTE_NAME=HID Remote01 Keyboard\n")
    checks, values = diagnose.check_env(env)
    assert values["REMOTE_NAME"] == "HID Remote01 Keyboard"
    numeric = next(c for c in checks if c.name == "Numeric settings")
    assert numeric.status == FAIL and "POWER_GPIO='four'" in numeric.detail


def test_a_missing_env_file_fails(tmp_path: Path) -> None:
    checks, values = diagnose.check_env(tmp_path / "missing.env")
    assert checks[0].status == FAIL and values == {}


def test_services_report_required_optional_and_restart_loops() -> None:
    output = "\n\n".join(
        [
            "Id=camilladsp.service\nLoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0",
            "Id=cdsp-source-switcher.service\nLoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=7",
            "Id=cdsp-trigger.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=0",
            "Id=cdsp-remote.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nNRestarts=0",
        ]
    )
    with patch.object(diagnose, "run", return_value=completed(output)):
        checks = {c.name: c for c in diagnose.check_services()}
    assert checks["camilladsp.service"].status == OK
    assert checks["cdsp-source-switcher.service"].status == WARN
    assert "restarted 7x" in checks["cdsp-source-switcher.service"].detail
    assert checks["cdsp-trigger.service"].status == FAIL
    assert checks["cdsp-remote.service"].status == INFO


def test_throttling_now_fails_and_since_boot_warns() -> None:
    for value, expected in (("0x0", OK), ("0x50000", WARN), ("0x50005", FAIL)):
        with patch.object(
            diagnose, "run", return_value=completed(f"throttled={value}\n")
        ):
            power = next(
                c for c in diagnose.check_platform({}) if c.name == "Power / throttling"
            )
        assert power.status == expected, value


def test_the_lock_check_reports_free_held_and_unwritable(tmp_path: Path) -> None:
    lock = tmp_path / "audio-control.lock"
    with patch.object(settings, "AUDIO_CONTROL_LOCK_PATH", lock):
        assert diagnose.check_lock()[0].status == WARN  # missing
        lock.touch()
        assert diagnose.check_lock()[0].status == OK
        holder = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(diagnose, "LOCK_WAIT_SECONDS", 0.2):
                held = diagnose.check_lock()[0]
        finally:
            os.close(holder)
        assert held.status == FAIL and "stuck" in held.detail


def test_the_motu_is_never_connected_to(tmp_path: Path) -> None:
    status = tmp_path / "motu.json"
    status.write_text(json.dumps({"known": True, "writable": True, "volume_db": -6}))
    commands = []

    def fake_run(command, timeout=10.0):
        commands.append(command)
        return completed()

    with (
        patch.object(settings, "MOTU_VOLUME_STATUS_PATH", status),
        patch.object(diagnose, "run", side_effect=fake_run),
        patch("socket.socket", side_effect=AssertionError("no sockets")),
    ):
        checks = diagnose.check_motu({"MOTU_WS_URL": "ws://169.254.51.193:1280"})
    assert [c.status for c in checks] == [OK, OK]
    assert commands == [["ping", "-c", "1", "-W", "1", "169.254.51.193"]]


def test_an_unknown_motu_state_fails_with_the_switchers_reason(tmp_path: Path) -> None:
    status = tmp_path / "motu.json"
    status.write_text(json.dumps({"known": False, "reason": "the MOTU is not connected"}))
    with (
        patch.object(settings, "MOTU_VOLUME_STATUS_PATH", status),
        patch.object(diagnose, "run", return_value=completed(returncode=1)),
    ):
        checks = diagnose.check_motu({"MOTU_WS_URL": "ws://motu:1280"})
    assert [c.status for c in checks] == [FAIL, FAIL]
    assert checks[1].detail == "the MOTU is not connected"


def test_a_failed_transition_is_a_failure(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"ok": False, "error": "CamillaDSP rejected x.yml"}))
    selection = tmp_path / "selection.json"
    with (
        patch.object(settings, "SPEAKER_STATUS_PATH", status),
        patch.object(settings, "SPEAKER_SELECTION_PATH", selection),
        patch.object(diagnose, "validate_config", return_value=(True, "passes")),
    ):
        checks = {c.name: c for c in diagnose.check_configs({}, {"CDSP_CONFIG_DIR": str(tmp_path)})}
    assert checks["Last transition"].status == FAIL
    assert "rejected" in checks["Last transition"].detail
    assert checks["toslink.yml"].status == FAIL  # required, missing
    assert checks["analog.yml"].status == INFO  # optional


def test_configs_are_validated_with_camilladsp(tmp_path: Path) -> None:
    for source in ("streamer", "gadget", "toslink"):
        (tmp_path / f"{source}.yml").write_text("devices: {}\n")
    calls = []

    def fake_run(command, timeout=10.0):
        calls.append(command)
        bad = command[-1].endswith("gadget.yml")
        return completed("error: bad samplerate" if bad else "", 1 if bad else 0)

    with (
        patch.object(settings, "SPEAKER_SELECTION_PATH", tmp_path / "selection.json"),
        patch.object(settings, "SPEAKER_STATUS_PATH", tmp_path / "none.json"),
        patch.object(diagnose, "run", side_effect=fake_run),
    ):
        checks = {c.name: c for c in diagnose.check_configs({}, {"CDSP_CONFIG_DIR": str(tmp_path)})}
    assert checks["streamer.yml"].status == OK
    assert checks["gadget.yml"].status == FAIL and "bad samplerate" in checks["gadget.yml"].detail
    assert all(call[:2] == [settings.CAMILLA_BINARY, "-c"] for call in calls)


def test_one_crashing_check_does_not_hide_the_rest() -> None:
    with (
        patch.object(diagnose, "check_env", return_value=([], {})),
        patch.object(diagnose, "check_platform", return_value=[]),
        patch.object(diagnose, "check_services", return_value=[]),
        patch.object(diagnose, "check_camilla", return_value=([], {})),
        patch.object(diagnose, "check_configs", side_effect=RuntimeError("boom")),
        patch.object(diagnose, "check_lock", return_value=[diagnose.Check("Locks", "x", OK)]),
        patch.object(diagnose, "check_motu", return_value=[]),
        patch.object(diagnose, "check_remote", return_value=[]),
        patch.object(diagnose, "check_state_files", return_value=[]),
        patch.object(diagnose, "check_recent_errors", return_value=[]),
    ):
        checks = diagnose.diagnose()
    assert [c.status for c in checks] == [WARN, OK]
    assert "boom" in checks[0].detail


def test_exit_status_and_rendering(capsys) -> None:
    checks = [
        diagnose.Check("System", "Python", OK, "3.11"),
        diagnose.Check("Services", "camilladsp.service", FAIL, "failed"),
    ]
    with patch.object(diagnose, "diagnose", return_value=checks):
        assert diagnose.main([]) == 1
        text = capsys.readouterr().out
        assert "✘ camilladsp.service" in text and "1 ok, 0 warning(s), 1 failure(s)" in text
        assert diagnose.main(["--json"]) == 1
        assert json.loads(capsys.readouterr().out)[1]["status"] == FAIL
    with patch.object(diagnose, "diagnose", return_value=checks[:1]):
        assert diagnose.main([]) == 0


def test_installer_offers_the_health_check() -> None:
    installer = (REPOSITORY / "install.sh").read_text()
    assert "12) Run Health Check (diagnose)" in installer
    assert "12) run_diagnose ;;" in installer
    assert '"$VENV_DIR/bin/python3" "$SCRIPTS_DIR/diagnose.py" || true' in installer
