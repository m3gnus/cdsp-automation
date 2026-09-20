from __future__ import annotations

import contextlib
import copy
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
    def setUp(self) -> None:
        self._runtime = tempfile.TemporaryDirectory()
        runtime = Path(self._runtime.name)
        self.ready_path = runtime / "ready.json"
        patches = (
            patch.object(source_switcher, "AUDIO_READY_PATH", self.ready_path),
            patch.object(
                source_switcher, "AUDIO_CONTROL_LOCK_PATH", runtime / "audio.lock"
            ),
        )
        for guard in patches:
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self._runtime.cleanup)

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

    def test_recovery_reload_is_latched_muted_and_never_stays_ready(self) -> None:
        """Recovery is an unverified config change, so it must inhibit first."""
        speaker_profiles.clear_audio_inhibit(
            self.ready_path, generation="a" * 32
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "streamer.yml")
            config_path.touch()
            client = self.client("INACTIVE", None, str(config_path))
            recovery = source_switcher.ConfigRecoveryGuard()

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(recovery.ready(client, 0.0))

        client.volume.set_main_mute.assert_called_once_with(True)
        self.assertFalse(self.ready_path.exists())

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
        speaker_profiles.clear_audio_inhibit(
            self.ready_path, generation="b" * 32
        )
        recovery = source_switcher.ConfigRecoveryGuard()
        for state_name in ("PAUSED", "RUNNING"):
            client = self.client(state_name, {"devices": {"samplerate": 48000}})
            self.assertTrue(recovery.ready(client, 0.0))
            client.general.reload.assert_not_called()
            client.config.file_path.assert_not_called()
            client.volume.set_main_mute.assert_not_called()
        self.assertTrue(self.ready_path.is_file())

    def test_invalid_remembered_path_is_not_reloaded(self) -> None:
        client = self.client("INACTIVE", None, "/does/not/exist.yml")
        recovery = source_switcher.ConfigRecoveryGuard()

        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(recovery.ready(client, 0.0))

        client.general.reload.assert_not_called()
        self.assertIn("remembered config is not a file", output.getvalue())


# CamillaDSP serializes its *parsed* configuration for GetConfig, so optional
# fields the submitted YAML omitted read back as null. These are the ALSA
# device options that bit the read-back comparison in practice.
ENGINE_OPTIONAL_DEVICE_FIELDS = ("stop_on_inactive", "link_volume_control", "labels")


def engine_materialized(config: dict) -> dict:
    """Mirror how the engine materializes omitted optional fields on read-back."""
    result = copy.deepcopy(config)
    devices = result.get("devices")
    if isinstance(devices, dict):
        for side in ("capture", "playback"):
            section = devices.get(side)
            if isinstance(section, dict):
                for field in ENGINE_OPTIONAL_DEVICE_FIELDS:
                    section.setdefault(field, None)
    filters = result.get("filters")
    if isinstance(filters, dict):
        for value in filters.values():
            if isinstance(value, dict):
                value.setdefault("description", None)
                parameters = value.get("parameters")
                if isinstance(parameters, dict):
                    for field in ("inverted", "mute"):
                        parameters.setdefault(field, None)
    return result


class FakeSwitcherConfig:
    """A live CamillaDSP config with the engine's read-back behaviour.

    ``active()`` serves the config the engine has actually applied, always in
    the materialized form the real GetConfig returns. ``apply_after`` models an
    asynchronously applied reload: the first N reads still report the previous
    config, and ``active_config`` pins one that never converges.

    Readiness is stamped into the *live* graph (``set_value``/``set_active``),
    never into the file, so ``restart_engine`` drops the stamp exactly the way
    a real engine restart does.
    """

    def __init__(
        self,
        path: str,
        description: str | None = None,
        *,
        apply_after: int = 0,
        active_config: dict | None = None,
        parses_files: bool = True,
    ) -> None:
        self.path = path
        self.applied_path = path
        self.apply_after = apply_after
        self.active_config = active_config
        self.active_calls = 0
        self.file_description = description
        self.live_description = description
        self.pinned: dict | None = None
        if parses_files:
            # Newer pycamilladsp clients expose the engine's own parser.
            self.read_and_parse_file = self._read_and_parse_file

    @staticmethod
    def _load(path: str) -> dict:
        try:
            return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError:
            return {}

    def _read_and_parse_file(self, path: str) -> dict:
        return engine_materialized(self._load(path))

    def file_path(self) -> str:
        return self.path

    def set_file_path(self, path: str) -> None:
        self.path = path

    def _applied(self) -> dict:
        """The graph the engine holds, before the live description stamp."""
        if self.pinned is not None:
            return copy.deepcopy(self.pinned)
        if self.active_config is not None:
            return engine_materialized(self.active_config)
        if self.active_calls > self.apply_after:
            self.applied_path = self.path
        loaded = self._load(self.applied_path)
        return engine_materialized(loaded) if loaded else {"devices": {}}

    def active(self) -> dict:
        self.active_calls += 1
        config = self._applied()
        if self.live_description is not None:
            config["description"] = self.live_description
        return config

    def set_active(self, value: dict) -> None:
        self.pinned = copy.deepcopy(value)
        self.live_description = value.get("description")

    def description(self) -> str | None:
        return self.live_description

    def set_value(self, pointer: str, value: object) -> None:
        assert pointer == "/description"
        self.live_description = value  # type: ignore[assignment]

    def restart_engine(self) -> None:
        """Model a CamillaDSP restart: the live graph reverts to the file."""
        self.pinned = None
        self.applied_path = self.path
        self.live_description = self.file_description


class FakeClock:
    """Deterministic stand-in for the time module used by apply_config."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


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


# A valid config that omits every optional ALSA device field. The engine
# materializes those as null on read-back, which is what the verification has
# to tolerate without loosening anything that affects routing or protection.
MINIMAL_ALSA_CONFIG = {
    "devices": {
        "samplerate": 48000,
        "chunksize": 1024,
        "capture": {
            "type": "Alsa",
            "channels": 2,
            "device": "hw:Loopback,1",
            "format": "S32LE",
        },
        "playback": {
            "type": "Alsa",
            "channels": 2,
            "device": "hw:MOTU,0",
            "format": "S32LE",
        },
    },
}


def run_verified_switch(
    tmp_path: Path,
    expected: dict,
    *,
    statuses: list[dict] | None = None,
    clock: FakeClock | None = None,
    timeout: float = 1.0,
    poll_interval: float = 0.25,
    **config_kwargs: object,
) -> tuple[SimpleNamespace, Exception | None]:
    """Apply a config whose target carries expected_config, capturing failure."""
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text(
        yaml.safe_dump(expected, sort_keys=False), encoding="utf-8"
    )
    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous), **config_kwargs),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["running", "running"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest(expected),
        "max_volume_db": -6,
        "expected_config": expected,
    }
    error: Exception | None = None
    if statuses is None:
        statuses = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "CONFIG_DIR", str(tmp_path)),
        patch.object(
            switcher, "SPEAKER_TRANSITION_PATH", tmp_path / "transition.json"
        ),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher, "time", clock or FakeClock()),
        patch.object(switcher, "CONFIG_APPLY_TIMEOUT", timeout),
        patch.object(switcher, "CONFIG_APPLY_POLL_INTERVAL", poll_interval),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except Exception as exc:  # noqa: BLE001 - the assertion is the message
            error = exc
    return client, error


def test_omitted_optional_device_fields_round_trip_through_verification(
    tmp_path: Path,
) -> None:
    """Nulls the engine materializes are not a difference from the request."""
    statuses: list[dict] = []
    client, error = run_verified_switch(
        tmp_path, MINIMAL_ALSA_CONFIG, statuses=statuses
    )
    assert error is None
    assert client.volume.mute is False
    assert statuses[-1]["ok"] is True
    # The read-back genuinely carried the materialized nulls.
    assert client.config.active()["devices"]["playback"]["stop_on_inactive"] is None


def test_verification_tolerates_materialized_nulls_without_engine_parsing(
    tmp_path: Path,
) -> None:
    """Older clients without ReadConfigFile fall back to null-stripping."""
    statuses: list[dict] = []
    client, error = run_verified_switch(
        tmp_path, MINIMAL_ALSA_CONFIG, statuses=statuses, parses_files=False
    )
    assert not hasattr(client.config, "read_and_parse_file")
    assert error is None
    assert client.volume.mute is False
    assert statuses[-1]["ok"] is True


def test_verification_still_rejects_a_genuinely_different_active_config(
    tmp_path: Path,
) -> None:
    """Routing and protection differences must survive the normalization."""
    rerouted = copy.deepcopy(MINIMAL_ALSA_CONFIG)
    rerouted["devices"]["playback"]["channels"] = 4
    limited = copy.deepcopy(MINIMAL_ALSA_CONFIG)
    limited["devices"]["playback"]["volume_limit"] = 0.0
    for divergent in (rerouted, limited):
        statuses: list[dict] = []
        client, error = run_verified_switch(
            tmp_path,
            MINIMAL_ALSA_CONFIG,
            statuses=statuses,
            active_config=divergent,
        )
        assert isinstance(error, RuntimeError)
        assert "differs from requested config" in str(error)
        assert client.volume.mute is True
        assert statuses[-1]["ok"] is False


def test_queued_config_is_accepted_once_it_becomes_active_before_the_deadline(
    tmp_path: Path,
) -> None:
    """Reload only queues the change; polling waits it out instead of racing."""
    clock = FakeClock()
    statuses: list[dict] = []
    client, error = run_verified_switch(
        tmp_path,
        MINIMAL_ALSA_CONFIG,
        statuses=statuses,
        clock=clock,
        apply_after=2,
    )
    assert error is None
    assert client.config.active_calls > 2
    assert 0.25 in clock.slept
    assert client.volume.mute is False
    assert statuses[-1]["ok"] is True


def test_config_that_never_becomes_active_fails_closed_at_the_deadline(
    tmp_path: Path,
) -> None:
    """A bounded deadline still rolls back and latches mute on timeout."""
    clock = FakeClock()
    statuses: list[dict] = []
    stale = {"devices": {"samplerate": 44100}}
    client, error = run_verified_switch(
        tmp_path,
        MINIMAL_ALSA_CONFIG,
        statuses=statuses,
        clock=clock,
        timeout=1.0,
        poll_interval=0.25,
        active_config=stale,
    )
    assert isinstance(error, RuntimeError)
    assert "differs from requested config" in str(error)
    # Polling stopped at the deadline rather than spinning forever.
    assert clock.slept.count(0.25) == 4
    assert client.volume.mute is True
    assert statuses[-1]["ok"] is False
    assert not (tmp_path / "ready.json").exists()


def test_legacy_target_does_not_let_a_swapped_file_verify_itself(
    tmp_path: Path,
) -> None:
    """A config swapped after resolve time must not become its own reference.

    Legacy targets carry a raw-byte ``_file_digest`` while the integrity gate
    compares a structural ``config_digest``, so that gate skips them.  Asking
    the engine to parse the file would then compare on-disk content against
    itself and verify a config nobody vetted.
    """
    resolved = copy.deepcopy(MINIMAL_ALSA_CONFIG)
    swapped = copy.deepcopy(MINIMAL_ALSA_CONFIG)
    swapped["devices"]["playback"]["channels"] = 8

    previous = tmp_path / "prev.yml"
    previous.write_text("devices: {}\n")
    target_path = tmp_path / "streamer.yml"
    # What is on disk at apply time is not what the target was resolved from.
    target_path.write_text(
        yaml.safe_dump(swapped, sort_keys=False), encoding="utf-8"
    )

    client = SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(),
        general=FakeSwitcherGeneral(["running", "running"]),
    )
    target = {
        "speaker": switcher.DEFAULT_SPEAKER_ID,
        "source": "streamer",
        "digest": "raw-byte-digest-the-gate-never-compares",
        "max_volume_db": 0.0,
        "legacy": True,
        "expected_config": resolved,
    }
    statuses: list[dict] = []
    error: Exception | None = None
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", tmp_path / "ready.json"),
        patch.object(switcher, "CONFIG_DIR", str(tmp_path)),
        patch.object(
            switcher, "SPEAKER_TRANSITION_PATH", tmp_path / "transition.json"
        ),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status", side_effect=statuses.append),
        patch.object(switcher, "time", FakeClock()),
        patch.object(switcher, "CONFIG_APPLY_TIMEOUT", 1.0),
        patch.object(switcher, "CONFIG_APPLY_POLL_INTERVAL", 0.25),
    ):
        try:
            switcher.apply_config(client, str(target_path), target=target)
        except Exception as exc:  # noqa: BLE001 - the assertion is the message
            error = exc
    assert isinstance(error, RuntimeError)
    assert "differs from requested config" in str(error)
    assert client.volume.mute is True
    assert statuses[-1]["ok"] is False


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


class LoopStop(BaseException):
    """Leave the switcher's endless loop without tripping its error handler."""


def run_switcher_iterations(client: object, sleeps: int) -> None:
    """Drive source_switcher.main() for a bounded number of sleeps."""
    remaining = [sleeps]

    def sleep(_seconds: float) -> None:
        remaining[0] -= 1
        if remaining[0] <= 0:
            raise LoopStop
    with (
        patch.object(switcher, "CamillaClient", lambda *_args: client),
        patch.object(switcher, "TOSLINK_MOTU_METERS", False),
        patch.object(switcher, "ANALOG_MOTU_METERS", False),
        patch.object(switcher.time, "sleep", side_effect=sleep),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        try:
            switcher.main()
        except LoopStop:
            pass


def switcher_client(
    config_path: str, *, connected: bool = False, description: str | None = None
) -> SimpleNamespace:
    config = FakeSwitcherConfig(config_path, description=description)
    client = SimpleNamespace(
        config=config,
        volume=FakeSwitcherVolume(mute=False),
        general=FakeSwitcherGeneral(["running"] * 64),
        connected=connected,
    )
    client.is_connected = lambda: client.connected
    def connect() -> None:
        client.connected = True
    client.connect = connect
    return client


def test_engine_restart_revokes_unmute_until_the_new_instance_is_verified(
    tmp_path: Path,
) -> None:
    """The switcher, UI and remote all keep running; only CamillaDSP restarts."""
    ready = tmp_path / "ready.json"
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "streamer--partymeh.yml"
    previous.write_text("devices: {}\n")
    target_path.write_text("devices: {}\n")
    client = SimpleNamespace(
        config=FakeSwitcherConfig(
            str(previous), description="Generated source=streamer speaker=partymeh"
        ),
        volume=FakeSwitcherVolume(mute=False),
        general=FakeSwitcherGeneral(["running", "running"]),
    )
    target = {
        "speaker": "partymeh",
        "source": "streamer",
        "digest": switcher.config_digest({"devices": {}}),
        "max_volume_db": 0.0,
        "selection_revision": 4,
    }
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", ready),
        patch.object(switcher, "SPEAKER_TRANSITION_PATH", tmp_path / "transition.json"),
        patch.object(switcher, "SPEAKER_GENERATED_DIR", tmp_path / "generated"),
        patch.object(switcher, "validate_config_file"),
        patch.object(switcher, "speaker_selection_lock", return_value=nullcontext()),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": "partymeh", "revision": 4},
        ),
        patch.object(switcher, "ensure_audio_eq"),
        patch.object(switcher, "_write_speaker_status"),
        patch.object(switcher.time, "sleep"),
    ):
        switcher.rotate_engine_generation()
        switcher.apply_config(client, str(target_path), target=target)

        # A verified transition authorizes every control.
        speaker_profiles.require_audio_unmute_allowed(ready, client)
        first_token = json.loads(ready.read_text())
        assert first_token["version"] == speaker_profiles.AUDIO_READY_VERSION
        assert first_token["speaker"] == "partymeh"
        assert first_token["selection_revision"] == 4
        assert first_token["config_digest"] == target["digest"]

        # CamillaDSP restarts underneath everyone.  /run is tmpfs but the boot
        # did not change, so the token file is still sitting there.
        client.config.restart_engine()
        assert ready.is_file()
        for consumer in ("remote", "web UI", "AirPlay bridge"):
            try:
                speaker_profiles.require_audio_unmute_allowed(ready, client)
            except RuntimeError as exc:
                assert "inhibited" in str(exc)
            else:
                raise AssertionError(f"{consumer} unmuted a restarted engine")
        assert switcher.audio_inhibit_active(
            ready, client, generation=switcher.engine_generation()
        )

        # Only a fresh verified transition restores it, under a new generation.
        second_generation = switcher.rotate_engine_generation()
        switcher.apply_config(client, str(target_path), target=target)
        speaker_profiles.require_audio_unmute_allowed(ready, client)
        second_token = json.loads(ready.read_text())
        assert second_token["engine_generation"] == second_generation
        assert second_token["engine_generation"] != first_token["engine_generation"]

        # The previous instance's token is worthless against the new engine.
        audio_eq.atomic_write_json(ready, first_token)
        try:
            speaker_profiles.require_audio_unmute_allowed(ready, client)
        except RuntimeError as exc:
            assert "inhibited" in str(exc)
        else:
            raise AssertionError("a previous engine instance's token authorized unmute")


def test_reconnect_drops_a_token_minted_for_the_previous_connection(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready.json"
    config_path = tmp_path / "streamer.yml"
    config_path.write_text("devices: {}\n")
    stale_generation = speaker_profiles.new_engine_generation()
    speaker_profiles.clear_audio_inhibit(ready, generation=stale_generation)
    client = switcher_client(
        str(config_path),
        description=speaker_profiles.engine_generation_marker(stale_generation),
    )
    applied: list[dict] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", ready),
        # This switcher run had already verified that generation, and the live
        # engine still carries it: only the websocket went away.  Even then the
        # reconnection may be a different engine process, so nothing survives it.
        patch.object(switcher, "_engine_generation", stale_generation),
        patch.dict(switcher.CONFIGS, {"streamer": str(config_path)}, clear=True),
        patch.object(switcher, "validate_configs"),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": switcher.DEFAULT_SPEAKER_ID, "revision": 0},
        ),
        patch.object(
            switcher,
            "managed_config_identity",
            return_value=("streamer", switcher.DEFAULT_SPEAKER_ID),
        ),
        patch.object(
            switcher, "resolve_config_target", return_value={"path": str(config_path)}
        ),
        patch.object(switcher, "apply_config", side_effect=lambda *a, **k: applied.append(k)),
    ):
        run_switcher_iterations(client, sleeps=1)
        # Connecting rotates the generation, so the token that matched the
        # engine a moment ago no longer belongs to this switcher run.
        assert switcher._engine_generation != stale_generation

    # The stale token was dropped and the config re-applied through the
    # verified path rather than trusted.
    assert not ready.exists()
    assert applied and applied[0]["audio_lock_held"] is True
    assert client.volume.mute is True


def test_loop_errors_drop_readiness_and_rerun_startup_validation(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready.json"
    config_path = tmp_path / "streamer.yml"
    config_path.write_text("devices: {}\n")
    generation = speaker_profiles.new_engine_generation()
    speaker_profiles.clear_audio_inhibit(ready, generation=generation)
    client = switcher_client(
        str(config_path),
        connected=True,
        description=speaker_profiles.engine_generation_marker(generation),
    )
    validations: list[str] = []
    with (
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", ready),
        patch.object(switcher, "_engine_generation", generation),
        patch.dict(switcher.CONFIGS, {"streamer": str(config_path)}, clear=True),
        patch.object(switcher, "validate_configs", side_effect=validations.append),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": switcher.DEFAULT_SPEAKER_ID, "revision": 0},
        ),
        patch.object(
            switcher,
            "require_selected_profile_available",
            side_effect=RuntimeError("engine went away mid-pass"),
        ),
    ):
        # The first pass starts ready: an already-connected client and a token
        # this run minted for the generation the live engine still carries.
        assert not switcher.audio_inhibit_active(
            ready, client, generation=generation
        )
        run_switcher_iterations(client, sleeps=4)

    assert not ready.exists()
    # Startup validation state was reset, so it ran again on the next pass.
    assert len(validations) >= 2


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


# ---------------------------------------------------------------------------
# Source arbitration
#
# The switcher used to treat hardware readiness as reason enough to select a
# source again, and reset the silence timers on every switch.  Two ready but
# silent inputs - the normal idle state of a rig whose TOSLINK and analog
# readiness both come off the same MOTU meter socket - therefore alternated
# forever, one full config reload plus a mute/restore per cycle.
# ---------------------------------------------------------------------------


def stream_source(ready: bool, playing: bool | None = None) -> switcher.SourceSnapshot:
    """A source whose readiness only means 'the stream is open'."""
    return switcher.SourceSnapshot(ready=ready, playing=playing)


def meter_source(active: bool) -> switcher.SourceSnapshot:
    """A meter source, whose readiness is itself a signal-presence measure."""
    return switcher.SourceSnapshot(ready=active, playing=active)


class ArbitrationRig:
    """Replay wall-clock against arbitrate(), tracking selections.

    ``playing`` names the source (if any) that would be heard once selected,
    so a source's confirmed-playback state is only observable while it is the
    selected one - exactly the constraint the real capture meters impose.
    """

    def __init__(
        self,
        ready: set[str],
        *,
        playing: str | None = None,
        interval: float = 1.0,
        start: float = 1000.0,
    ) -> None:
        self.ready = set(ready)
        self.playing = playing
        self.interval = interval
        self.now = start
        self.probes: dict[str, switcher.ProbeRecord] = {}
        self.current: str | None = None
        self.last_active: str | None = None
        self.selections: list[tuple[float, str]] = []
        self.manual: str | None = None

    def snapshot(self) -> dict[str, switcher.SourceSnapshot]:
        sources = {}
        for name in switcher.SOURCE_PRIORITY:
            ready = name in self.ready
            if name == self.current:
                playing = self.playing == name
            else:
                playing = None
            sources[name] = stream_source(ready, playing)
        return sources

    def step(self) -> switcher.ArbitrationDecision:
        decision = switcher.arbitrate(
            now=self.now,
            elapsed=self.interval,
            current_source=self.current,
            last_active=self.last_active,
            manual_source=self.manual,
            sources=self.snapshot(),
            probes=self.probes,
        )
        self.probes = decision.probes
        self.last_active = decision.last_active
        if decision.source is not None and decision.source != self.current:
            self.selections.append((self.now, decision.source))
            self.current = decision.source
        self.now += self.interval
        return decision

    def run(self, seconds: float) -> None:
        for _ in range(int(seconds / self.interval)):
            self.step()

    def reloads_after(self, timestamp: float) -> list[tuple[float, str]]:
        return [entry for entry in self.selections if entry[0] >= timestamp]


def test_arbitration_probes_a_ready_source_then_records_it_as_silent() -> None:
    """Readiness buys one probe; the silence it finds is remembered."""
    rig = ArbitrationRig({"streamer"})
    assert rig.step().source == "streamer"
    assert rig.probes["streamer"].confirmed is False

    # Listened to for the probe window, then written off with a backoff.
    rig.run(switcher.PROBE_SILENCE_TIMEOUT)
    record = rig.probes["streamer"]
    assert record.backoff_level == 1
    assert record.backoff_until > rig.now
    assert rig.selections == [(1000.0, "streamer")]


def test_ready_but_silent_source_is_not_requalified_by_readiness_alone() -> None:
    """The headline bug: hardware readiness must not re-arm a silent source."""
    rig = ArbitrationRig({"streamer", "gadget"})
    rig.run(switcher.PROBE_SILENCE_TIMEOUT + 1)
    assert [name for _, name in rig.selections] == ["streamer", "gadget"]

    # Both are still ready, and both have now been heard out.  Nothing may be
    # selected again until a backoff expires.
    rig.run(switcher.PROBE_SILENCE_TIMEOUT + 1)
    decision = rig.step()
    assert decision.source is None
    assert decision.reason == "no source qualified"
    assert [name for _, name in rig.selections] == ["streamer", "gadget"]


def test_two_ready_but_silent_sources_settle_instead_of_alternating() -> None:
    """Ten simulated minutes with two ready, silent inputs.

    Before the fix this alternated once per SOURCE_IDLE_TIMEOUT forever - ten
    reloads plus ten mute/restore cycles over this window, and sixty an hour
    after that.  The bound below is absolute, not 'fewer than before', and the
    quiet tail is what 'settles' means.
    """
    rig = ArbitrationRig({"streamer", "gadget"})
    rig.run(600.0)

    assert len(rig.selections) <= 8, rig.selections
    # The cadence decays: nothing at all in the last three minutes.
    assert rig.reloads_after(rig.now - 180.0) == []
    # And it really did try both, rather than settling by going deaf to one.
    assert {name for _, name in rig.selections} == {"streamer", "gadget"}


def test_silent_probe_backoff_keeps_decaying_over_an_hour() -> None:
    """The bound holds on the long horizon, well under the old 60/hour."""
    rig = ArbitrationRig({"streamer", "gadget"})
    rig.run(3600.0)
    assert len(rig.selections) <= 16, rig.selections
    first_half = [entry for entry in rig.selections if entry[0] < 1000.0 + 1800.0]
    second_half = rig.reloads_after(1000.0 + 1800.0)
    assert len(second_half) < len(first_half)


def test_a_source_that_starts_playing_after_a_silent_probe_is_picked_up() -> None:
    """The backoff must rate-limit probing without making the rig deaf."""
    rig = ArbitrationRig({"streamer", "gadget"})
    rig.run(120.0)
    assert rig.current == "gadget"
    silent_selections = list(rig.selections)

    # Music starts on the streamer while the gadget holds the config.  Nothing
    # can see it from here: capture levels describe the selected source only,
    # so the next probe is what finds it.
    rig.playing = "streamer"
    rig.run(switcher.PROBE_BACKOFF_MAX + switcher.PROBE_SILENCE_TIMEOUT + 2)
    assert rig.current == "streamer"
    assert len(rig.selections) > len(silent_selections)

    # Once confirmed it is held, and its silent history is forgotten.
    before = list(rig.selections)
    rig.run(600.0)
    assert rig.selections == before
    assert rig.probes["streamer"].backoff_level == 0
    assert rig.probes["streamer"].confirmed is True


def test_a_source_that_becomes_ready_again_is_probed_without_waiting() -> None:
    """A genuinely new session is not the one we gave up on."""
    rig = ArbitrationRig({"streamer"})
    rig.run(switcher.PROBE_SILENCE_TIMEOUT + 2)
    assert rig.probes["streamer"].backoff_level == 1
    assert rig.step().source is None

    # The AirPlay session ends and a new one opens a moment later.
    rig.ready = set()
    rig.run(switcher.PROBE_SILENCE_TIMEOUT + 1)
    rig.ready = {"streamer"}
    rig.playing = "streamer"
    assert rig.step().source == "streamer"


def test_flapping_readiness_does_not_forgive_the_probe_backoff() -> None:
    """A one-pass readiness blip is not a new session."""
    probes = {
        "streamer": switcher.ProbeRecord(
            was_ready=True, backoff_level=1, backoff_until=2000.0
        )
    }
    now = 1000.0

    def pass_with(streamer_ready: bool) -> switcher.ArbitrationDecision:
        nonlocal probes, now
        decision = switcher.arbitrate(
            now=now,
            elapsed=1.0,
            current_source="gadget",
            last_active=None,
            sources={
                "streamer": stream_source(streamer_ready),
                "gadget": stream_source(True, playing=False),
            },
            probes=probes,
        )
        probes = decision.probes
        now += 1.0
        return decision

    for step in range(40):
        assert pass_with(step % 2 == 0).source != "streamer", now
    assert probes["streamer"].backoff_until == 2000.0

    # Really gone for longer than a probe window, then back: a new session,
    # and the switcher owes it a look straight away.
    for _ in range(int(switcher.PROBE_SILENCE_TIMEOUT) + 1):
        pass_with(False)
    assert pass_with(True).source == "streamer"


def test_genuinely_playing_source_wins_immediately_over_a_backed_off_one() -> None:
    """Confirmed audio is never rate-limited."""
    probes = {
        "streamer": switcher.ProbeRecord(
            was_ready=True, backoff_level=3, backoff_until=5000.0
        )
    }
    decision = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source=None,
        sources={
            "streamer": stream_source(True),
            "gadget": stream_source(False),
            "toslink": meter_source(True),
            "analog": meter_source(False),
        },
        probes=probes,
    )
    # Streamer outranks TOSLINK but has been written off; TOSLINK's meter is
    # direct evidence of signal, so it is taken at once.
    assert decision.source == "toslink"
    assert decision.reason == "confirmed playing"
    assert decision.probes["toslink"].backoff_until == 0.0


def test_playing_current_source_is_not_preempted_by_a_higher_priority_one() -> None:
    decision = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source="toslink",
        last_active="toslink",
        sources={
            "streamer": stream_source(True),
            "gadget": stream_source(False),
            "toslink": meter_source(True),
            "analog": meter_source(False),
        },
        probes={},
    )
    assert decision.source == "toslink"
    assert decision.reason == "current source playing"


def test_confirmed_playback_gets_the_track_gap_grace_not_the_probe_window() -> None:
    """IDLE_TIMEOUT keeps its meaning; the probe window is a new, shorter one."""
    playing = switcher.ProbeRecord(was_ready=True, confirmed=True)
    probing = switcher.ProbeRecord(was_ready=True, confirmed=False)
    silent = {
        "streamer": stream_source(True, playing=False),
        "gadget": stream_source(True),
    }

    def hold_for(record: switcher.ProbeRecord) -> float:
        probes = {"streamer": record, "gadget": switcher.ProbeRecord(was_ready=True)}
        now = 1000.0
        while True:
            decision = switcher.arbitrate(
                now=now,
                elapsed=1.0,
                current_source="streamer",
                last_active="streamer",
                sources=silent,
                probes=probes,
            )
            probes = decision.probes
            if decision.source != "streamer":
                return now - 1000.0
            now += 1.0

    assert hold_for(probing) == switcher.PROBE_SILENCE_TIMEOUT - 1
    assert hold_for(playing) == switcher.IDLE_TIMEOUT - 1


def test_lower_priority_meter_source_cuts_a_silent_hold_short() -> None:
    """SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT keeps working for a played source."""
    probes = {
        "streamer": switcher.ProbeRecord(was_ready=True, confirmed=True, silence=1.0)
    }
    decision = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source="streamer",
        last_active="streamer",
        sources={
            "streamer": stream_source(True, playing=False),
            "gadget": stream_source(False),
            "toslink": meter_source(True),
            "analog": meter_source(False),
        },
        probes=probes,
        lower_priority_timeout=0.0,
    )
    assert decision.source == "toslink"

    # Raise the timeout and the streamer keeps its grace instead.
    held = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source="streamer",
        last_active="streamer",
        sources={
            "streamer": stream_source(True, playing=False),
            "gadget": stream_source(False),
            "toslink": meter_source(True),
            "analog": meter_source(False),
        },
        probes=probes,
        lower_priority_timeout=30.0,
    )
    assert held.source == "streamer"


def test_manual_override_overrides_a_backed_off_source() -> None:
    """An operator can always reach a source the switcher has written off."""
    probes = {
        "gadget": switcher.ProbeRecord(
            was_ready=True, backoff_level=4, backoff_until=9000.0
        )
    }
    decision = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source="streamer",
        last_active="streamer",
        manual_source="gadget",
        sources={
            "streamer": stream_source(True, playing=True),
            "gadget": stream_source(True),
        },
        probes=probes,
    )
    assert decision.source == "gadget"
    assert decision.manual is True
    assert decision.reason == "manual override"
    assert decision.probes["gadget"].backoff_until == 0.0
    assert decision.probes["gadget"].backoff_level == 0


def test_no_qualifying_source_leaves_the_idle_decision_to_the_caller() -> None:
    """Idle keep-last semantics: arbitration names no source, it does not idle."""
    decision = switcher.arbitrate(
        now=1000.0,
        elapsed=1.0,
        current_source="toslink",
        last_active=None,
        sources={
            "streamer": stream_source(False),
            "gadget": stream_source(False),
            "toslink": meter_source(False),
            "analog": meter_source(False),
        },
        probes={},
    )
    assert decision.source is None
    assert decision.last_active is None


def test_probe_backoff_delay_escalates_and_is_capped() -> None:
    delays = [
        switcher.probe_backoff_delay(level, base=30.0, factor=4.0, maximum=900.0)
        for level in range(6)
    ]
    assert delays == [30.0, 120.0, 480.0, 900.0, 900.0, 900.0]
    assert switcher.probe_backoff_delay(3, base=0.0) == 0.0


class SwitcherLoopClock:
    """A monotonic clock the switcher loop drives forward by sleeping."""

    def __init__(self, budget: float, start: float = 1000.0) -> None:
        self.now = start
        self.deadline = start + budget

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= self.deadline:
            raise LoopStop

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


def test_switcher_loop_stops_reloading_two_ready_but_silent_sources(
    tmp_path: Path,
) -> None:
    """The same settling, end to end through main()'s own apply path."""
    ready = tmp_path / "ready.json"
    configs = {}
    for name in ("streamer", "gadget"):
        path = tmp_path / f"{name}.yml"
        path.write_text("devices: {}\n")
        configs[name] = str(path)
    generation = speaker_profiles.new_engine_generation()
    speaker_profiles.clear_audio_inhibit(ready, generation=generation)

    config = FakeSwitcherConfig(
        configs["streamer"],
        description=speaker_profiles.engine_generation_marker(generation),
    )
    client = SimpleNamespace(
        config=config,
        volume=FakeSwitcherVolume(mute=False),
        general=SimpleNamespace(reload=lambda: None, state=lambda: "running"),
        # Everything is silent, always.
        levels=SimpleNamespace(capture_rms=lambda: [-120.0, -120.0]),
        is_connected=lambda: True,
        connect=lambda: None,
    )
    applied: list[str] = []
    clock = SwitcherLoopClock(budget=600.0)

    def fake_apply(_cdsp, path, **_kwargs) -> None:
        applied.append(Path(path).stem)
        config.path = path
        config.applied_path = path
        # A real apply verifies the graph and re-publishes readiness; without
        # that the loop would re-apply the same config forever and never reach
        # arbitration at all.
        speaker_profiles.clear_audio_inhibit(ready, generation=generation)

    def identity(current: str | None) -> tuple[str, str] | None:
        if not current:
            return None
        return Path(current).stem, switcher.DEFAULT_SPEAKER_ID

    with (
        patch.object(switcher, "CamillaClient", lambda *_args: client),
        patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(switcher, "AUDIO_READY_PATH", ready),
        patch.object(switcher, "_engine_generation", generation),
        patch.object(switcher, "TOSLINK_MOTU_METERS", False),
        patch.object(switcher, "ANALOG_MOTU_METERS", False),
        patch.dict(switcher.CONFIGS, configs, clear=True),
        patch.object(switcher, "validate_configs"),
        patch.object(switcher, "require_selected_profile_available"),
        patch.object(switcher, "ensure_current_speaker_audio_eq"),
        patch.object(switcher, "read_manual_source", return_value=None),
        patch.object(
            switcher,
            "current_speaker_selection",
            return_value={"selected": switcher.DEFAULT_SPEAKER_ID, "revision": 0},
        ),
        patch.object(switcher, "managed_config_identity", side_effect=identity),
        # Both inputs are wired up and both are dead quiet.
        patch.object(switcher, "is_alsa_active", return_value=True),
        patch.object(switcher, "is_gadget_available", return_value=True),
        patch.object(
            switcher,
            "resolve_config_target",
            side_effect=lambda source, *_a, **_k: {"path": configs[source]},
        ),
        patch.object(switcher, "apply_config", side_effect=fake_apply),
        patch.object(switcher, "time", clock),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        try:
            switcher.main()
        except LoopStop:
            pass

    # Ten simulated minutes.  The old loop reloaded once per SOURCE_IDLE_TIMEOUT
    # for as long as both stayed ready; this one runs out of reasons.
    assert len(applied) <= 8, applied
    assert set(applied) == {"streamer", "gadget"}


if __name__ == "__main__":
    unittest.main()
