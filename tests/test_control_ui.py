"""Control UI behavior: audio state, speaker switching, HTML."""

from __future__ import annotations

import copy
import inspect
import json
import math
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import audio_eq
import speaker_config
import speaker_profiles
import web_ui


REPOSITORY = Path(__file__).resolve().parents[1]


def test_manual_amp_off_keeps_trigger_service_running() -> None:
    with patch.object(web_ui, "run_checked", return_value="") as run_checked:
        web_ui.turn_amps_off()

    run_checked.assert_called_once_with(
        ["systemctl", "kill", "-s", "USR1", "cdsp-trigger.service"],
        timeout=5,
    )
    assert 'id="ampOff"' in web_ui.HTML
    assert "next audio session" in web_ui.HTML


def test_volume_api_rejects_nonfinite_and_boolean_values() -> None:
    applied: list[float] = []
    volume = SimpleNamespace(
        main_volume=lambda: -20.0,
        set_main_volume=applied.append,
    )
    client = SimpleNamespace(volume=volume)
    invalid = (
        {"volume_db": math.nan},
        {"volume_db": math.inf},
        {"delta_db": -math.inf},
        {"volume_db": True},
    )
    with (
        patch.object(web_ui, "camilla_client", return_value=nullcontext(client)),
        patch.object(web_ui, "audio_control_lock", return_value=nullcontext()),
    ):
        for payload in invalid:
            try:
                web_ui.set_camilla_volume(payload)
            except ValueError as exc:
                assert "finite number" in str(exc)
            else:
                raise AssertionError(f"invalid volume was accepted: {payload}")
    assert applied == []


def test_blocked_unmute_does_not_partially_apply_volume() -> None:
    applied: list[float] = []
    client = SimpleNamespace(
        volume=SimpleNamespace(
            main_volume=lambda: -20.0,
            set_main_volume=applied.append,
        )
    )
    with (
        patch.object(web_ui, "camilla_client", return_value=nullcontext(client)),
        patch.object(web_ui, "audio_control_lock", return_value=nullcontext()),
        patch.object(
            web_ui,
            "require_audio_unmute_allowed",
            side_effect=RuntimeError("audio inhibited"),
        ),
    ):
        try:
            web_ui.set_camilla_volume({"volume_db": -10, "muted": False})
        except RuntimeError as exc:
            assert "inhibited" in str(exc)
        else:
            raise AssertionError("blocked unmute was accepted")
    assert applied == []


def test_live_meter_recovers_from_invalid_levels_and_ignores_nonfinite_values() -> None:
    class Client:
        def __init__(self, levels: object) -> None:
            self.levels = SimpleNamespace(playback_rms=lambda: levels)
            self.disconnected = False

        def disconnect(self) -> None:
            self.disconnected = True

    invalid = Client(None)
    with patch.object(web_ui, "_levels_client", invalid):
        assert web_ui.camilla_levels() == {"ok": False, "signal_db": None}
        assert invalid.disconnected is True
        assert web_ui._levels_client is None

    valid = Client([None, float("nan"), -1000.0, "-42.5"])
    with patch.object(web_ui, "_levels_client", valid):
        assert web_ui.camilla_levels() == {"ok": True, "signal_db": -42.5}


def test_media_folder_scan_tolerates_drive_disappearing(tmp_path: Path) -> None:
    with (
        patch.object(web_ui, "MEDIA_ROOT", tmp_path),
        patch.object(Path, "iterdir", side_effect=OSError("unmounted")),
    ):
        assert web_ui.list_media_folders() == []


def test_web_audio_state_follows_selected_speaker_profile(tmp_path: Path) -> None:
    selection_path = tmp_path / "selection.json"
    audio_dir = tmp_path / "audio"
    speaker_profiles.update_speaker_selection(
        selection_path, "partymeh", expected_revision=0
    )
    state = audio_eq.default_audio_state()
    with (
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", selection_path),
        patch.object(web_ui, "SPEAKER_AUDIO_DIR", audio_dir),
        patch.object(web_ui, "AUDIO_EQ_PATH", tmp_path / "legacy.json"),
    ):
        saved = web_ui.write_audio_eq_state(state, expected_speaker="partymeh")
    assert saved["revision"] == 1
    assert audio_eq.read_audio_state(audio_dir / "partymeh.json")["revision"] == 1
    assert not (audio_dir / "kantarellen.json").exists()


def test_web_audio_save_holds_speaker_selection_lock_through_commit(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "audio-eq.json"
    selection_path = tmp_path / "selection.json"
    audio_eq.atomic_write_json(state_path, audio_eq.default_audio_state())
    selection_locked = False

    @contextmanager
    def selection_lock(path: Path):
        nonlocal selection_locked
        assert path == selection_path
        selection_locked = True
        try:
            yield
        finally:
            selection_locked = False

    def guarded_write(path: Path, payload: dict, mode: int = 0o644) -> None:
        assert selection_locked
        audio_eq.atomic_write_json(path, payload, mode)

    with (
        patch.object(web_ui, "AUDIO_EQ_PATH", state_path),
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", selection_path),
        patch.object(web_ui, "speaker_selection_lock", side_effect=selection_lock),
        patch.object(web_ui, "atomic_write_json", side_effect=guarded_write),
    ):
        saved = web_ui.write_audio_eq_state(audio_eq.default_audio_state())

    assert saved["revision"] == 1
    assert selection_locked is False


def test_web_speaker_selection_mutes_and_removes_ready_token(tmp_path: Path) -> None:
    selection_path = tmp_path / "selection.json"
    ready_path = tmp_path / "ready.json"
    speaker_profiles.clear_audio_inhibit(ready_path)

    class Volume:
        muted = False

        def main_mute(self) -> bool:
            return self.muted

        def set_main_mute(self, value: bool) -> None:
            self.muted = value

    volume = Volume()

    class Client:
        def __init__(self, *_args: object) -> None:
            self.volume = volume

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    catalog = {
        "kantarellen": {"id": "kantarellen", "available": True},
        "partymeh": {"id": "partymeh", "available": True},
    }
    with (
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", selection_path),
        patch.object(web_ui, "SPEAKER_AUDIO_DIR", tmp_path / "audio"),
        patch.object(web_ui, "AUDIO_EQ_PATH", tmp_path / "legacy.json"),
        patch.object(web_ui, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(web_ui, "AUDIO_READY_PATH", ready_path),
        patch.object(
            web_ui, "SPEAKER_TRANSITION_PATH", tmp_path / "transition.json"
        ),
        patch.object(web_ui, "profile_catalog", return_value=catalog),
        patch.object(web_ui, "preflight_speaker_profile"),
        patch.object(web_ui, "require_active_source_supported", return_value="streamer"),
        patch.dict(sys.modules, {"camilladsp": SimpleNamespace(CamillaClient=Client)}),
        patch.object(web_ui, "speaker_payload", return_value={"ok": True}),
    ):
        result = web_ui.select_speaker(
            {"selected": "partymeh", "revision": 0, "confirm": "SWITCH"}
        )
    assert result == {"ok": True}
    assert volume.muted is True
    assert not ready_path.exists()
    assert speaker_profiles.read_speaker_selection(selection_path)["selected"] == "partymeh"
    transition = json.loads((tmp_path / "transition.json").read_text())
    assert transition == {
        "version": 1,
        "revision": 1,
        "selected": "partymeh",
        "restore_mute": False,
    }


def test_stale_web_speaker_selection_does_not_mute_or_remove_ready(
    tmp_path: Path,
) -> None:
    selection_path = tmp_path / "selection.json"
    ready_path = tmp_path / "ready.json"
    speaker_profiles.clear_audio_inhibit(ready_path)
    muted: list[bool] = []

    class Client:
        def __init__(self, *_args: object) -> None:
            self.volume = SimpleNamespace(
                set_main_mute=lambda value: muted.append(value)
            )

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

    catalog = {
        "kantarellen": {"id": "kantarellen", "available": True},
        "partymeh": {"id": "partymeh", "available": True},
    }
    with (
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", selection_path),
        patch.object(web_ui, "SPEAKER_AUDIO_DIR", tmp_path / "audio"),
        patch.object(web_ui, "AUDIO_EQ_PATH", tmp_path / "legacy.json"),
        patch.object(web_ui, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock"),
        patch.object(web_ui, "AUDIO_READY_PATH", ready_path),
        patch.object(web_ui, "profile_catalog", return_value=catalog),
        patch.object(web_ui, "preflight_speaker_profile"),
        patch.object(web_ui, "require_active_source_supported", return_value="streamer"),
        patch.dict(sys.modules, {"camilladsp": SimpleNamespace(CamillaClient=Client)}),
    ):
        try:
            web_ui.select_speaker(
                {"selected": "partymeh", "revision": 99, "confirm": "SWITCH"}
            )
        except ValueError as exc:
            assert "changed elsewhere" in str(exc)
        else:
            raise AssertionError("stale speaker selection was accepted")
    assert muted == []
    assert ready_path.is_file()
    assert speaker_profiles.read_speaker_selection(selection_path)["selected"] == "kantarellen"


def test_audio_eq_ui_write_rejects_stale_revision(tmp_path: Path) -> None:
    state_path = tmp_path / "audio-eq.json"
    status_path = tmp_path / "status.json"
    backup_path = tmp_path / "backups"
    initial = audio_eq.default_audio_state()
    audio_eq.atomic_write_json(state_path, initial)
    with (
        patch.object(web_ui, "AUDIO_EQ_PATH", state_path),
        patch.object(web_ui, "AUDIO_EQ_STATUS_PATH", status_path),
        patch.object(web_ui, "AUDIO_EQ_BACKUP_DIR", backup_path),
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", tmp_path / "selection.json"),
    ):
        saved = web_ui.write_audio_eq_state(initial)
        assert saved["revision"] == 1
        try:
            web_ui.write_audio_eq_state(initial)
        except ValueError as exc:
            assert "changed elsewhere" in str(exc)
        else:
            raise AssertionError("stale audio state overwrite was accepted")
    assert 'data-tab="audio"' in web_ui.HTML


def test_audio_eq_ui_write_rejects_non_integer_revision(tmp_path: Path) -> None:
    state_path = tmp_path / "audio-eq.json"
    initial = audio_eq.default_audio_state()
    audio_eq.atomic_write_json(state_path, initial)

    with (
        patch.object(web_ui, "AUDIO_EQ_PATH", state_path),
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", tmp_path / "selection.json"),
    ):
        for invalid in (True, 0.5, "0"):
            candidate = copy.deepcopy(initial)
            candidate["revision"] = invalid
            try:
                web_ui.write_audio_eq_state(candidate)
            except ValueError as exc:
                assert "revision must be an integer" in str(exc)
            else:
                raise AssertionError(f"non-integer revision was accepted: {invalid!r}")


def test_audio_ui_uses_installed_profiles_and_exposes_load_failures() -> None:
    assert 'id="speakerEditor"' not in web_ui.HTML
    assert "data-edit-speaker" not in web_ui.HTML
    assert "Profiles are installed definitions and may contain FIR crossovers" in web_ui.HTML
    assert 'id="audioRetry"' in web_ui.HTML
    assert "function setAudioAvailable" in web_ui.HTML
    assert "audio state unavailable" in inspect.getsource(web_ui.Handler.do_GET)
    assert 'id="eqPlot"' in web_ui.HTML


def test_volume_status_requires_live_acknowledged_spotify_receiver() -> None:
    assert "audioBridge.live&&spotifyBridge.command_socket===true" in web_ui.HTML
    assert "Spotify ↔ master live" in web_ui.HTML
    assert "spotifyBridge.receiver_socket===true&&!spotifyReady" in web_ui.HTML
    assert "Spotify ready · waiting for sender" in web_ui.HTML
    assert "audioBridge.airplay_service_active===true&&audioBridge.live" in web_ui.HTML


def test_volume_status_requires_current_running_bridge_heartbeat() -> None:
    source = inspect.getsource(web_ui.audio_eq_payload)
    assert '"airplay-volume-bridge.service"' in source
    assert "bridge_active" in source
    assert 'bridge_status.get("updated_at", 0)) < 5' in source
    assert '"bridge_service_active": bridge_active' in source


def test_services_render_before_optional_status_panels_with_legacy_js_support() -> None:
    load_source = web_ui.HTML.split("async function load()", 1)[1].split(
        "async function pollLevels()", 1
    )[0]
    assert "renderServices(data.services || {})" in load_source
    assert load_source.index("renderServices(") < load_source.index("renderSystem(")
    assert ".replaceAll(" not in web_ui.HTML


def test_iso226_ui_rejects_enable_without_verified_engine(tmp_path: Path) -> None:
    state_path = tmp_path / "audio-eq.json"
    audio_eq.atomic_write_json(state_path, audio_eq.default_audio_state())
    state = audio_eq.default_audio_state()
    state["loudness"]["enabled"] = True
    with (
        patch.object(web_ui, "AUDIO_EQ_PATH", state_path),
        patch.object(web_ui, "ISO226_CAPABILITY_PATH", tmp_path / "missing.json"),
    ):
        try:
            web_ui.write_audio_eq_state(state)
        except ValueError as exc:
            assert "custom ISO 226" in str(exc)
        else:
            raise AssertionError("ISO 226 enabled without verified engine")


def test_simultaneous_audio_posts_cannot_both_overwrite_revision(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "audio-eq.json"
    initial = audio_eq.default_audio_state()
    audio_eq.atomic_write_json(state_path, initial)

    def save(gain: float) -> str:
        candidate = audio_eq.default_audio_state()
        candidate["bands"][0]["gain"] = gain
        try:
            web_ui.write_audio_eq_state(candidate)
            return "saved"
        except ValueError:
            return "stale"

    with (
        patch.object(web_ui, "AUDIO_EQ_PATH", state_path),
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", tmp_path / "selection.json"),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, (1.0, 2.0)))
    assert sorted(results) == ["saved", "stale"]
    assert audio_eq.read_audio_state(state_path)["revision"] == 1


def test_audio_autosave_uses_snapshot_and_drains_newer_edits() -> None:
    assert "const snapshot=JSON.parse(JSON.stringify(audioState))" in web_ui.HTML
    assert "generation !== audioEditGeneration" in web_ui.HTML
    assert "audioSaveTimer=setTimeout(saveAudio,0)" in web_ui.HTML


def test_eq_band_switch_click_is_not_intercepted_by_row_selection() -> None:
    assert 'aria-label="Band ${i+1} enabled"' in web_ui.HTML
    assert 'e.target.closest("input,select,button,label")' in web_ui.HTML
    assert 'title="Bypass this band"' not in web_ui.HTML


def test_speaker_profile_deployment_and_gui_contract_are_present() -> None:
    example = speaker_config.normalize_profile(
        speaker_config.load_yaml_mapping(
            REPOSITORY / "speaker-profile.example.yml",
            "speaker profile example",
        )
    )
    assert example["enabled"] is False
    assert example["active_outputs"] == []
    assert len(example["muted_outputs"]) == example["output_channels"]

    installer = (REPOSITORY / "install.sh").read_text()
    for setting in (
        "SPEAKER_PROFILE_DIR",
        "SOURCE_BASE_DIR",
        "SPEAKER_GENERATED_DIR",
        "AUDIO_CONTROL_LOCK_PATH",
        "AUDIO_READY_PATH",
    ):
        assert setting in installer
    assert 'id="speakerProfiles"' in web_ui.HTML
    assert 'api("/api/speaker"' in web_ui.HTML
    assert "speakerState?.selection?.selected" in web_ui.HTML


def test_speaker_transition_rejects_unsupported_active_source_and_preflights_analog(
    tmp_path: Path,
) -> None:
    catalog = {
        "partymeh": {
            "label": "PartyMEH",
            "supported_sources": ["streamer"],
        }
    }
    generated = tmp_path / "generated"
    config = {"devices": {}}
    digest = speaker_config.config_digest(config)
    managed = generated / digest / "analog--partymeh.yml"
    managed.parent.mkdir(parents=True)
    managed.write_text(yaml.safe_dump(config))
    with (
        patch.object(web_ui, "SPEAKER_GENERATED_DIR", generated),
        patch.object(web_ui, "camilla_status", return_value={"config_file": str(managed)}),
    ):
        try:
            web_ui.require_active_source_supported("partymeh", catalog)
        except ValueError as exc:
            assert "does not support the active analog source" in str(exc)
        else:
            raise AssertionError("unsupported active source reached speaker commit")

    with (
        patch.object(web_ui, "CDSP_CONFIG_DIR", tmp_path / "legacy"),
        patch.object(web_ui, "SPEAKER_GENERATED_DIR", generated),
    ):
        spoof = tmp_path / "analog--partymeh.yml"
        spoof.write_text(yaml.safe_dump(config))
        assert web_ui.managed_config_identity(str(spoof)) is None
        bad_digest = generated / ("0" * 64) / "analog--partymeh.yml"
        bad_digest.parent.mkdir(parents=True)
        bad_digest.write_text(yaml.safe_dump(config))
        assert web_ui.managed_config_identity(str(bad_digest)) is None
        assert web_ui.managed_config_identity(str(managed)) == ("analog", "partymeh")

    for source in ("streamer", "gadget", "toslink", "analog"):
        (tmp_path / f"{source}.yml").write_text("devices: {}\n")
    checked: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        checked.append(Path(command[-1]).stem)
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(web_ui, "CDSP_CONFIG_DIR", tmp_path),
        patch.object(web_ui.subprocess, "run", side_effect=fake_run),
    ):
        web_ui.preflight_speaker_profile("kantarellen")
    assert checked == ["streamer", "gadget", "toslink", "analog"]


def test_site_default_speaker_uses_legacy_control_paths(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for source in ("streamer", "gadget", "toslink"):
        (config_dir / f"{source}.yml").write_text("devices: {}\n")

    checked: list[str] = []

    def fake_run_result(command: list[str], **_kwargs: object) -> SimpleNamespace:
        checked.append(Path(command[-1]).stem)
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(web_ui, "DEFAULT_SPEAKER_ID", "mains"),
        patch.object(web_ui, "CDSP_CONFIG_DIR", config_dir),
        patch.object(
            web_ui,
            "current_speaker_selection",
            return_value={"selected": "mains"},
        ),
        patch.object(web_ui, "run_result", side_effect=fake_run_result),
        patch.object(
            web_ui,
            "camilla_status",
            return_value={"config_file": str(config_dir / "streamer.yml")},
        ),
    ):
        availability = web_ui.source_availability()
        assert availability["streamer"]["exists"] is True
        assert availability["analog"]["exists"] is False
        assert web_ui.managed_config_identity(
            str(config_dir / "streamer.yml")
        ) == ("streamer", "mains")
        web_ui.preflight_speaker_profile("mains")
        assert web_ui.require_active_source_supported("mains", {}) == "streamer"

    assert checked == ["streamer", "gadget", "toslink"]


def test_operator_profile_availability_uses_operator_configs(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "configs"
    source_base_dir = tmp_path / "source-bases"
    config_dir.mkdir()
    source_base_dir.mkdir()
    operator_path = config_dir / "vintage-streamer.yml"
    operator_path.write_text("devices: {}\n")
    catalog = {
        "vintage": {
            "available": True,
            "supported_sources": ["streamer"],
        }
    }

    with (
        patch.dict(
            speaker_profiles.OPERATOR_CONFIG_SPEAKERS,
            {"vintage": {"streamer": operator_path.name}},
            clear=True,
        ),
        patch.object(web_ui, "CDSP_CONFIG_DIR", config_dir),
        patch.object(web_ui, "SOURCE_BASE_DIR", source_base_dir),
        patch.object(
            web_ui,
            "current_speaker_selection",
            return_value={"selected": "vintage"},
        ),
        patch.object(web_ui, "installed_profile_catalog", return_value=catalog),
    ):
        availability = web_ui.source_availability()
        assert availability["streamer"] == {
            "label": web_ui.SOURCE_CHOICES["streamer"],
            "path": str(operator_path),
            "exists": True,
        }
        assert web_ui.source_status(
            {"config_file": str(operator_path)}
        )["current"] == "streamer"


def test_source_status_does_not_trust_unmanaged_generated_filename(
    tmp_path: Path,
) -> None:
    spoof = tmp_path / "streamer--spoof.yml"
    spoof.write_text("devices: {}\n")
    with (
        patch.object(web_ui, "read_source_override", return_value=None),
        patch.object(web_ui, "source_availability", return_value={}),
        patch.object(web_ui, "CDSP_CONFIG_DIR", tmp_path / "configs"),
        patch.object(web_ui, "SPEAKER_GENERATED_DIR", tmp_path / "generated"),
    ):
        status = web_ui.source_status({"config_file": str(spoof)})
    assert status["current"] == "streamer--spoof"


def test_parse_env_tolerates_disappearing_or_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.env"
    with patch.object(Path, "read_text", side_effect=OSError("unavailable")):
        assert web_ui.parse_env(path) == {}


def test_run_result_converts_subprocess_timeout_to_failure() -> None:
    timeout = subprocess.TimeoutExpired(["slow-command"], 2)
    with patch.object(web_ui.subprocess, "run", side_effect=timeout):
        result = web_ui.run_result(["slow-command"], timeout=2)

    assert result.returncode == 124
    assert "timed out" in result.stdout


def test_speaker_switch_requires_server_confirmation_and_dashboard_dialog() -> None:
    try:
        web_ui.select_speaker({"selected": "kantarellen", "revision": 0})
    except ValueError as exc:
        assert "SWITCH confirmation" in str(exc)
    else:
        raise AssertionError("speaker switch API accepted an unconfirmed request")
    assert 'id="dashboardSpeaker"' in web_ui.HTML
    assert 'id="speakerConfirm"' in web_ui.HTML
    assert "Type <b>SWITCH</b>" in web_ui.HTML
    assert 'confirm:"SWITCH"' in web_ui.HTML
    assert "audioState && !(await saveAudio())" in web_ui.HTML
