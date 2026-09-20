from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import yaml


if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp

import audio_eq
import speaker_profiles
import web_ui
from scripts import source_switcher
from profile_fixtures import capture_base, partymeh_document


REPOSITORY = Path(__file__).resolve().parents[1]

# The reliability tests adopted from the installation archive address the
# daemon as "switcher"; both names refer to the same flat scripts module.
switcher = source_switcher


class ConfigRecoveryTests(unittest.TestCase):
    def client(self, state_name: str, active_config: object, path: str = "") -> mock.Mock:
        client = mock.Mock()
        client.general.state.return_value = types.SimpleNamespace(name=state_name)
        client.config.active.return_value = active_config
        client.config.file_path.return_value = path
        return client

    def test_inactive_state_reloads_existing_remembered_config_on_retry_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "streamer.yml")
            config_path.touch()
            client = self.client("INACTIVE", None, str(config_path))
            recovery = source_switcher.ConfigRecoveryGuard(
                retry_seconds=10,
                log_seconds=30,
            )

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertFalse(recovery.ready(client, 0.0))
                self.assertFalse(recovery.ready(client, 5.0))
                self.assertFalse(recovery.ready(client, 10.0))

            self.assertEqual(client.general.reload.call_count, 2)
            client.config.active.assert_not_called()
            self.assertEqual(output.getvalue().count("CamillaDSP recovery:"), 1)

    def test_missing_active_config_recovers_even_when_state_says_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "streamer.yml")
            config_path.touch()
            client = self.client("PAUSED", {}, str(config_path))
            recovery = source_switcher.ConfigRecoveryGuard()

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(recovery.ready(client, 0.0))

            client.general.reload.assert_called_once_with()

    def test_healthy_paused_and_running_configs_are_never_reloaded(self) -> None:
        recovery = source_switcher.ConfigRecoveryGuard()
        for state_name in ("PAUSED", "RUNNING"):
            client = self.client(state_name, {"devices": {"samplerate": 48000}})
            self.assertTrue(recovery.ready(client, 0.0))
            client.general.reload.assert_not_called()
            client.config.file_path.assert_not_called()

    def test_invalid_remembered_path_is_not_reloaded(self) -> None:
        client = self.client("INACTIVE", None, "/does/not/exist.yml")
        recovery = source_switcher.ConfigRecoveryGuard()

        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(recovery.ready(client, 0.0))

        client.general.reload.assert_not_called()
        self.assertIn("remembered config is not a file", output.getvalue())


class FakeSwitcherConfig:
    def __init__(self, path: str) -> None:
        self.path = path

    def file_path(self) -> str:
        return self.path

    def set_file_path(self, path: str) -> None:
        self.path = path

    def active(self) -> dict:
        return {"devices": {}}


class FakeSwitcherVolume:
    def __init__(self, volume: float = -2.0, mute: bool = False) -> None:
        self.volume = volume
        self.mute = mute

    def main_volume(self) -> float:
        return self.volume

    def set_main_volume(self, value: float) -> None:
        self.volume = value

    def main_mute(self) -> bool:
        return self.mute

    def set_main_mute(self, value: bool) -> None:
        self.mute = value


class FakeSwitcherGeneral:
    def __init__(self, states: list[str]) -> None:
        self.states = iter(states)
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1

    def state(self) -> str:
        return next(self.states)


def test_managed_config_identity_rejects_filename_spoofing(tmp_path: Path) -> None:
    spoof = tmp_path / "streamer--partymeh.yml"
    spoof.write_text("devices: {}\n")
    with patch.object(switcher, "SPEAKER_GENERATED_DIR", tmp_path / "generated"):
        assert switcher.managed_config_identity(str(spoof)) is None

        config = {"devices": {}}
        digest = switcher.config_digest(config)
        managed = tmp_path / "generated" / digest / "streamer--partymeh.yml"
        managed.parent.mkdir(parents=True)
        managed.write_text("devices: {}\n")
        assert switcher.managed_config_identity(str(managed)) == (
            "streamer",
            "partymeh",
        )

        managed.write_text("devices:\n  samplerate: 96000\n")
        assert switcher.managed_config_identity(str(managed)) is None


def test_partymeh_uses_source_specific_operator_owned_configs(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "partymeh.yml").write_text(
        yaml.safe_dump(partymeh_document(), sort_keys=False), encoding="utf-8"
    )
    expected_by_source = {}
    for index, (source, filename) in enumerate(
        speaker_profiles.operator_configs_for_speaker("partymeh").items()
    ):
        config_path = config_dir / filename
        # Operator configs must state the native ceiling themselves; partymeh
        # declares max_volume_db: 0, so 0 is the loosest one accepted.
        expected = {"devices": {"samplerate": 48000 + index, "volume_limit": 0}}
        config_path.write_text(yaml.safe_dump(expected), encoding="utf-8")
        expected_by_source[source] = (config_path, expected)
    with patch.object(web_ui, "CDSP_CONFIG_DIR", config_dir):
        for source, (config_path, _expected) in expected_by_source.items():
            assert web_ui.managed_config_identity(str(config_path)) == (
                source,
                "partymeh",
            )
    with (
        patch.object(switcher, "CONFIG_DIR", str(config_dir)),
        patch.object(switcher, "SPEAKER_PROFILE_DIR", profile_dir),
        patch.object(
            switcher,
            "speaker_catalog",
            return_value={"partymeh": {"available": True}},
        ),
        patch.object(switcher, "speaker_audio_state", return_value={}),
        patch.object(switcher, "validate_config_file"),
    ):
        for source, (config_path, expected) in expected_by_source.items():
            assert switcher.managed_config_identity(str(config_path)) == (
                source,
                "partymeh",
            )
            target = switcher.resolve_config_target(source, "partymeh")
            assert target["path"] == str(config_path)
            assert target["operator_config"] is True
            assert target["expected_config"] == expected
            assert target["max_volume_db"] == 0.0


def test_validate_config_file_reports_the_rejection_reason(tmp_path: Path) -> None:
    """The camilladsp -c gate before a config goes live, actually executed.

    Every other test patches this out, so without this the interlock could be
    a no-op and the suite would stay green.
    """
    config = tmp_path / "streamer.yml"
    config.write_text("devices: {}\n")
    rejecting = tmp_path / "camilladsp-reject"
    rejecting.write_text("#!/bin/sh\necho 'Error: bad samplerate' >&2\nexit 1\n")
    rejecting.chmod(0o755)
    accepting = tmp_path / "camilladsp-accept"
    accepting.write_text("#!/bin/sh\nexit 0\n")
    accepting.chmod(0o755)

    with patch.object(switcher, "CAMILLA_BINARY", str(rejecting)):
        try:
            switcher.validate_config_file(config)
        except ValueError as exc:
            assert "streamer.yml" in str(exc)
            assert "bad samplerate" in str(exc)
        else:
            raise AssertionError("a rejected config passed validation")

    with patch.object(switcher, "CAMILLA_BINARY", str(accepting)):
        switcher.validate_config_file(config)


def test_active_profile_becoming_unavailable_fails_closed(tmp_path: Path) -> None:
    client = SimpleNamespace(volume=FakeSwitcherVolume(mute=False))
    statuses: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(
            switcher,
            "speaker_catalog",
            return_value={
                "partymeh": {"available": False, "reason": "profile is disabled"}
            },
        ),
        patch.object(switcher, "speaker_selection_lock", return_value=nullcontext()),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": "partymeh", "revision": 3},
        ),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
    ):
        try:
            switcher.require_selected_profile_available(
                client,
                "partymeh",
                selected_revision=3,
                current_speaker="partymeh",
                current_source="streamer",
                current_config="/managed/config.yml",
            )
        except RuntimeError as exc:
            assert "became unavailable" in str(exc)
        else:
            raise AssertionError("disabled active profile kept playing")
    assert client.volume.mute is True
    assert not (tmp_path / "ready.json").exists()
    assert statuses[-1]["ok"] is False


def test_status_publishers_replace_valid_non_object_json(tmp_path: Path) -> None:
    speaker_status = tmp_path / "speaker-status.json"
    eq_status = tmp_path / "eq-status.json"
    speaker_status.write_text("[]")
    eq_status.write_text("null")
    speaker_payload = {"ok": True, "updated_at": 10}
    eq_payload = {"applied": True, "updated_at": 20}

    with (
        patch.object(switcher, "SPEAKER_STATUS_PATH", speaker_status),
        patch.object(switcher, "AUDIO_EQ_STATUS_PATH", str(eq_status)),
    ):
        switcher._write_speaker_status(speaker_payload)
        switcher._write_audio_eq_status(eq_payload)

    assert json.loads(speaker_status.read_text()) == speaker_payload
    assert json.loads(eq_status.read_text()) == eq_payload

    with patch.object(switcher, "atomic_write_json") as write:
        switcher._write_status_if_changed(
            speaker_status, {**speaker_payload, "updated_at": 30}
        )
    write.assert_not_called()


def test_measurement_bypass_strips_user_eq_overlay() -> None:
    state = audio_eq.default_audio_state()
    state["bands"][0]["gain"] = 4
    base = {
        "devices": {"capture": {"channels": 2}},
        "filters": {},
        "pipeline": [],
    }
    overlaid, _ = audio_eq.apply_audio_overlay(base, state)

    class LiveConfig:
        def __init__(self, value: dict) -> None:
            self.value = value

        def active(self) -> dict:
            return self.value

        def set_active(self, value: dict) -> None:
            self.value = value

    client = SimpleNamespace(config=LiveConfig(overlaid))
    with (
        patch.object(switcher, "load_profile", return_value={"bypass_user_eq": True}),
        patch.object(switcher, "_write_audio_eq_status"),
    ):
        switcher.ensure_audio_eq(client, speaker_id="measurement", state=state)
    assert not any(
        name.startswith(audio_eq.FILTER_PREFIX)
        for name in client.config.value["filters"]
    )
    assert not any(
        step.get("description") == audio_eq.PIPELINE_DESCRIPTION
        for step in client.config.value["pipeline"]
    )


def test_missing_iso226_capability_reports_bypass_when_config_is_already_safe() -> None:
    state = audio_eq.default_audio_state()
    state["loudness"]["enabled"] = True
    safe_state = audio_eq.default_audio_state()
    base = {
        "devices": {"capture": {"channels": 2}},
        "filters": {},
        "pipeline": [],
    }
    safe_config, _preamp = audio_eq.apply_audio_overlay(base, safe_state)
    config = mock.Mock()
    config.active.return_value = safe_config
    client = SimpleNamespace(config=config)
    statuses: list[dict] = []

    with (
        patch.object(switcher, "iso226_capability_available", return_value=False),
        patch.object(switcher, "_write_audio_eq_status", side_effect=statuses.append),
    ):
        switcher.ensure_audio_eq(
            client,
            speaker_id=speaker_profiles.DEFAULT_SPEAKER_ID,
            state=state,
        )

    config.set_active.assert_not_called()
    assert statuses[-1]["applied"] is False
    assert "capability is missing" in statuses[-1]["error"]


def test_periodic_eq_reconcile_locks_and_rechecks_speaker_selection(
    tmp_path: Path,
) -> None:
    held: list[str] = []

    @contextlib.contextmanager
    def lock(name: str):
        held.append(name)
        try:
            yield
        finally:
            held.pop()

    def current_selection() -> dict:
        assert held == ["audio", "selection"]
        return {"selected": "partymeh", "revision": 4}

    def ensure(_client: object, *, speaker_id: str) -> None:
        assert held == ["audio", "selection"]
        assert speaker_id == "partymeh"

    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "audio_control_lock", side_effect=lambda _path: lock("audio")),
        patch.object(
            switcher,
            "speaker_selection_lock",
            side_effect=lambda _path: lock("selection"),
        ),
        patch.object(
            switcher, "current_speaker_selection", side_effect=current_selection
        ),
        patch.object(switcher, "ensure_audio_eq", side_effect=ensure) as apply_eq,
    ):
        switcher.ensure_current_speaker_audio_eq(object(), "partymeh")

    apply_eq.assert_called_once()
    assert held == []


def test_periodic_eq_reconcile_skips_a_newly_selected_speaker() -> None:
    with (
        patch.object(switcher, "audio_control_lock", return_value=nullcontext()),
        patch.object(switcher, "speaker_selection_lock", return_value=nullcontext()),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": "measurement", "revision": 5},
        ),
        patch.object(switcher, "ensure_audio_eq") as apply_eq,
    ):
        switcher.ensure_current_speaker_audio_eq(object(), "partymeh")

    apply_eq.assert_not_called()


def test_speaker_config_switch_is_muted_validated_and_volume_clamped(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "toslink.yml"
    target_path = tmp_path / "toslink--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["ProcessingState.RUNNING"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "toslink",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
    }
    statuses: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "validate_config_file") as validate,
        patch.object(switcher, "ensure_audio_eq") as eq,
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher.time, "sleep"),
    ):
        switcher.apply_config(client, str(target_path), target=target)
    validate.assert_called_once_with(target_path)
    eq.assert_called_once_with(client, speaker_id="partymeh", state=None)
    assert client.config.path == str(target_path)
    assert client.volume.volume == -6
    assert client.volume.mute is False
    assert statuses[-1]["ok"] is True
    assert statuses[-1]["applied"] == "partymeh"
    assert (tmp_path / "ready.json").is_file()


def test_web_transition_preference_restores_unmuted_after_verified_switch(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    transition_path = tmp_path / "transition.json"
    audio_eq.atomic_write_json(
        transition_path,
        {
            "version": 1,
            "revision": 1,
            "selected": "partymeh",
            "restore_mute": False,
        },
    )
    selection = {"selected": "partymeh", "revision": 1}
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(mute=True),
        general=FakeSwitcherGeneral(["running"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
        "selection_revision": 1,
    }
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "SPEAKER_TRANSITION_PATH", transition_path),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "current_speaker_selection", return_value=selection),
        patch.object(switcher, "speaker_selection_lock", return_value=nullcontext()),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status"),
        patch.object(switcher.time, "sleep"),
    ):
        restore_mute = switcher.pending_transition_mute(selection)
        switcher.apply_config(
            client, str(target_path), target=target, restore_mute=restore_mute
        )
    assert restore_mute is False
    assert client.volume.mute is False
    assert not transition_path.exists()
    assert (tmp_path / "ready.json").is_file()


def test_speaker_config_switch_rolls_back_and_latches_mute_on_failure(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["stalled", "ProcessingState.Paused"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
    }
    statuses: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "CONFIG_DIR", str(tmp_path)),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher.time, "sleep"),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except RuntimeError as exc:
            assert "safe running state" in str(exc)
        else:
            raise AssertionError("failed config switch did not raise")
    assert client.config.path == str(previous)
    assert client.general.reloads == 2
    assert client.volume.mute is True
    assert statuses[-1]["ok"] is False
    assert statuses[-1]["rollback_ok"] is True
    assert statuses[-1]["applied"] == speaker_profiles.DEFAULT_SPEAKER_ID
    assert not (tmp_path / "ready.json").exists()


def test_speaker_selection_change_during_reload_prevents_unmute(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["running", "running"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
        "selection_revision": 4,
    }
    selections = [
        {"selected": "partymeh", "revision": 4},
        {"selected": "measurement", "revision": 5},
    ]
    statuses: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.dict(switcher.CONFIGS, {"streamer": str(previous)}, clear=True),
        patch.object(switcher, "validate_config_file"),
        patch.object(
            switcher, "current_speaker_selection", side_effect=selections
        ),
        patch.object(
            switcher, "speaker_selection_lock", return_value=nullcontext()
        ),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher.time, "sleep"),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except RuntimeError as exc:
            assert "selection changed" in str(exc)
        else:
            raise AssertionError("selection race reached unmute")
    assert client.config.path == str(previous)
    assert client.volume.mute is True
    assert statuses[-1]["rollback_ok"] is True


def test_ambiguous_final_unmute_failure_reinhibits_before_rollback(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")

    class AmbiguousVolume(FakeSwitcherVolume):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def set_main_mute(self, value: bool) -> None:
            self.mute = value
            if value is False and not self.failed:
                self.failed = True
                raise OSError("mute RPC response lost")

    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=AmbiguousVolume(),
        general=FakeSwitcherGeneral(["running", "running"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
    }
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.dict(switcher.CONFIGS, {"streamer": str(previous)}, clear=True),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status"),
        patch.object(switcher.time, "sleep"),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except OSError as exc:
            assert "response lost" in str(exc)
        else:
            raise AssertionError("ambiguous unmute did not fail closed")
    assert client.volume.mute is True
    assert client.config.path == str(previous)
    assert not (tmp_path / "ready.json").exists()


def test_startup_captures_prior_mute_then_mutes_before_validation(
    tmp_path: Path,
) -> None:
    client = SimpleNamespace(volume=FakeSwitcherVolume(mute=False))
    with patch.object(
        switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"
    ):
        restore_mute = switcher.mute_for_startup_validation(client)
    assert restore_mute is False
    assert client.volume.mute is True

    source = (REPOSITORY / "scripts" / "source_switcher.py").read_text()
    loop = source.split("while True:", 1)[1]
    assert loop.index("mute_for_startup_validation(cdsp)") < loop.index(
        "validate_configs(selected_speaker)"
    )


def test_empty_startup_first_source_restores_prestart_mute() -> None:
    target = {"path": "/managed/streamer.yml"}
    client = object()
    with patch.object(switcher, "apply_config") as apply:
        consumed = switcher.apply_arbitrated_config(
            client, target, False, settle_time=1.5
        )
    assert consumed is None
    apply.assert_called_once_with(
        client,
        target["path"],
        settle_time=1.5,
        target=target,
        restore_mute=False,
    )


def test_apply_config_publishes_selection_revision(tmp_path: Path) -> None:
    previous = tmp_path / "toslink.yml"
    target_path = tmp_path / "toslink--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    selection_path = tmp_path / "speaker-selection.json"
    selection_path.write_text(
        json.dumps({"version": 1, "revision": 7, "selected": "partymeh"})
    )
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["ProcessingState.RUNNING"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "toslink",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -6,
        "selection_revision": 7,
    }
    statuses: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "SPEAKER_SELECTION_PATH", selection_path),
        patch.object(switcher, "SPEAKER_TRANSITION_PATH", tmp_path / "transition.json"),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher.time, "sleep"),
    ):
        switcher.apply_config(client, str(target_path), target=target)
    assert statuses[-1]["ok"] is True
    assert statuses[-1]["selection_revision"] == 7


def test_catalog_default_speaker_uses_the_plain_source_configs(tmp_path: Path) -> None:
    """A site catalog's default speaker plays through the existing configs."""
    plain = tmp_path / "streamer.yml"
    plain.write_text("devices: {}\n")
    with (
        patch.object(switcher, "DEFAULT_SPEAKER_ID", "mains"),
        patch.dict(switcher.CONFIGS, {"streamer": str(plain)}, clear=True),
        patch.object(switcher, "speaker_audio_state", return_value={}),
    ):
        target = switcher.resolve_config_target("streamer", "mains")
    assert target["legacy"] is True
    assert target["path"] == str(plain)
    assert target["max_volume_db"] == 0.0
    assert target["volume_limit_db"] == 0.0


def _capped_operator_environment(
    tmp_path: Path, config_body: dict, *, max_volume_db: float = -20
):
    """One operator-owned streamer config for a profile capped at -20 dB."""
    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir(exist_ok=True)
    document = partymeh_document()
    document["max_volume_db"] = max_volume_db
    (profile_dir / "partymeh.yml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    filename = speaker_profiles.operator_configs_for_speaker("partymeh")["streamer"]
    (config_dir / filename).write_text(
        yaml.safe_dump(config_body), encoding="utf-8"
    )
    return (
        patch.object(switcher, "CONFIG_DIR", str(config_dir)),
        patch.object(switcher, "SPEAKER_PROFILE_DIR", profile_dir),
        patch.object(
            switcher,
            "speaker_catalog",
            return_value={"partymeh": {"available": True}},
        ),
        patch.object(switcher, "speaker_audio_state", return_value={}),
        patch.object(switcher, "validate_config_file"),
    )


def test_operator_config_without_the_profile_volume_cap_is_rejected(
    tmp_path: Path,
) -> None:
    """A -20 dB profile paired with an uncapped config must not go live."""
    patches = _capped_operator_environment(
        tmp_path, {"devices": {"samplerate": 48000}}
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4] as validate:
        try:
            switcher.resolve_config_target("streamer", "partymeh")
        except ValueError as exc:
            message = str(exc)
            assert "partymeh-streamer.yml" in message
            assert "no devices.volume_limit" in message
            assert "-20.0 dB" in message
        else:
            raise AssertionError("an unprotected operator config was resolved")
    # Fail closed before the transition: nothing was even offered to CamillaDSP.
    validate.assert_not_called()


def test_operator_config_with_a_looser_volume_cap_is_rejected(
    tmp_path: Path,
) -> None:
    patches = _capped_operator_environment(
        tmp_path, {"devices": {"samplerate": 48000, "volume_limit": -5}}
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        try:
            switcher.resolve_config_target("streamer", "partymeh")
        except ValueError as exc:
            assert "looser" in str(exc)
            assert "-5.0 dB" in str(exc)
        else:
            raise AssertionError("a looser operator ceiling was resolved")


def test_operator_config_with_a_stricter_volume_cap_is_accepted_unchanged(
    tmp_path: Path,
) -> None:
    body = {"devices": {"samplerate": 48000, "volume_limit": -30}}
    patches = _capped_operator_environment(tmp_path, body)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        target = switcher.resolve_config_target("streamer", "partymeh")
    assert target["operator_config"] is True
    assert target["max_volume_db"] == -20
    # The operator's stricter ceiling is what the controls must honour, and
    # the file itself is passed through byte-for-byte.
    assert target["volume_limit_db"] == -30
    assert target["expected_config"] == body
    assert yaml.safe_load(Path(target["path"]).read_text(encoding="utf-8")) == body


def test_generated_config_target_publishes_its_compiled_volume_ceiling(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    base_dir = tmp_path / "source-bases"
    base_dir.mkdir()
    document = partymeh_document()
    document["max_volume_db"] = -20
    (profile_dir / "capped.yml").write_text(
        yaml.safe_dump({**document, "id": "capped"}, sort_keys=False),
        encoding="utf-8",
    )
    base = capture_base(2)
    base["devices"].pop("volume_limit")
    (base_dir / "streamer.yml").write_text(yaml.safe_dump(base), encoding="utf-8")

    with (
        patch.object(switcher, "SPEAKER_PROFILE_DIR", profile_dir),
        patch.object(switcher, "SOURCE_BASE_DIR", base_dir),
        patch.object(switcher, "SPEAKER_GENERATED_DIR", tmp_path / "generated"),
        patch.object(
            switcher,
            "speaker_catalog",
            return_value={"capped": {"available": True}},
        ),
        patch.object(
            switcher, "speaker_audio_state", return_value=audio_eq.default_audio_state()
        ),
        patch.object(switcher, "validate_config_file"),
    ):
        target = switcher.resolve_config_target("streamer", "capped")

    assert target.get("operator_config") is None
    assert target["volume_limit_db"] == -20
    assert target["expected_config"]["devices"]["volume_limit"] == -20


def test_applied_speaker_status_publishes_the_verified_volume_ceiling(
    tmp_path: Path,
) -> None:
    """The controls read their ceiling back from this status payload."""
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "partymeh-streamer.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["ProcessingState.RUNNING"]),
    )
    client.volume.volume = 0.0
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -20,
        "volume_limit_db": -30,
        "operator_config": True,
    }
    status_path = tmp_path / "speaker-status.json"
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "SPEAKER_STATUS_PATH", status_path),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher.time, "sleep"),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        switcher.apply_config(client, str(target_path), target=target)

    # The stricter config ceiling, not the profile's -20, clamps the fader.
    assert client.volume.volume == -30
    published = json.loads(status_path.read_text(encoding="utf-8"))
    assert published["ok"] is True
    assert published["volume_limit_db"] == -30
    assert speaker_profiles.read_effective_volume_limit(status_path) == -30


def test_failed_transition_status_leaves_the_controls_failing_closed(
    tmp_path: Path,
) -> None:
    """A rollback publishes no ceiling, so readers take the failsafe one."""
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "partymeh-streamer.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["stalled", "ProcessingState.Paused"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": -20,
        "volume_limit_db": -20,
    }
    status_path = tmp_path / "speaker-status.json"
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "SPEAKER_STATUS_PATH", status_path),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher.time, "sleep"),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a stalled transition reported success")

    published = json.loads(status_path.read_text(encoding="utf-8"))
    assert published["ok"] is False
    assert "volume_limit_db" not in published
    assert (
        speaker_profiles.read_effective_volume_limit(status_path)
        == speaker_profiles.FAILSAFE_VOLUME_LIMIT_DB
    )


if __name__ == "__main__":
    unittest.main()
