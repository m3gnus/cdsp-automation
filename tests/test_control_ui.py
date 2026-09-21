"""Control UI behavior: audio state, speaker switching, HTML."""

from __future__ import annotations

import copy
import email
import hmac
import inspect
import io
import json
import math
import os
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import audio_eq
import speaker_config
import speaker_profiles
import web_ui
from profile_fixtures import partymeh_document


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


def test_media_folder_scan_lists_sessions_and_tolerates_drive_disappearing(
    tmp_path: Path,
) -> None:
    """The UI renders only the folder count, so the scan must not stat files."""
    for name in ("Session B", "session a", ".Spotlight-V100"):
        (tmp_path / name).mkdir()
    (tmp_path / "track.wav").write_bytes(b"")
    (tmp_path / "._track.wav").write_bytes(b"")

    with patch.object(web_ui, "MEDIA_ROOT", tmp_path):
        assert web_ui.list_media_folders() == [
            {"name": "session a"},
            {"name": "Session B"},
        ]

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
    speaker_profiles.clear_audio_inhibit(ready_path, generation="c" * 32)

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
    speaker_profiles.clear_audio_inhibit(ready_path, generation="c" * 32)
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


def test_source_override_is_written_validated_and_cleared_by_auto(
    tmp_path: Path,
) -> None:
    """The switcher reads this file directly, so only vetted sources reach it."""
    override = tmp_path / "run" / "manual_source"
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "streamer.yml").write_text("---\n")

    with (
        patch.object(web_ui, "SOURCE_OVERRIDE_PATH", override),
        patch.object(web_ui, "CDSP_CONFIG_DIR", configs),
        patch.object(web_ui, "SPEAKER_SELECTION_PATH", tmp_path / "selection.json"),
    ):
        web_ui.write_source_override("streamer")
        assert override.read_text().strip() == "streamer"
        assert web_ui.read_source_override() == "streamer"

        # gadget.yml was never created, so pinning it would strand the switcher.
        for rejected, expected in (
            ("gadget", FileNotFoundError),
            ("../../etc/passwd", ValueError),
        ):
            try:
                web_ui.write_source_override(rejected)
            except expected:
                pass
            else:
                raise AssertionError(f"override accepted {rejected!r}")
        assert override.read_text().strip() == "streamer"

        web_ui.write_source_override("auto")
        assert not override.exists()
        assert web_ui.read_source_override() is None


def test_parse_env_reads_settings_and_tolerates_an_unreadable_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.env"
    path.write_text(
        "# managed by install.sh\n"
        "REMOTE_NAME=HID Remote01 Keyboard\n"
        "\n"
        "  MOTU_WS_URL = ws://169.254.51.193:1280  \n"
        "not-a-setting\n"
        "SOURCE_TOSLINK_METER_PAIRS=12,13\n"
    )
    assert web_ui.parse_env(path) == {
        "REMOTE_NAME": "HID Remote01 Keyboard",
        "MOTU_WS_URL": "ws://169.254.51.193:1280",
        "SOURCE_TOSLINK_METER_PAIRS": "12,13",
    }

    # remote_status must keep reporting while the env file is missing or the
    # root UI cannot read it; a missing file is the OSError that needs no patch.
    assert web_ui.parse_env(tmp_path / "absent.env") == {}
    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
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


def _stanza(**properties: str) -> str:
    return "".join(f"{key}={value}\n" for key, value in properties.items())


def test_batched_show_parses_stanzas_regardless_of_property_order() -> None:
    output = (
        _stanza(
            Id="a.service",
            Names="a.service",
            Description="one = two",
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            UnitFileState="enabled",
        )
        + "\n"
        + _stanza(
            SubState="dead",
            ActiveState="inactive",
            Names="b.service b-alias.service",
            UnitFileState="",
            Id="b.service",
            LoadState="not-found",
            Description="",
        )
        + "\n\n"
    )
    records = web_ui.parse_systemctl_show(output)
    assert len(records) == 2
    assert records[0]["Description"] == "one = two"
    assert records[1]["UnitFileState"] == ""
    assert records[1]["SubState"] == "dead"


def test_batched_show_maps_requested_aliases_through_names() -> None:
    output = (
        _stanza(Id="real.service", Names="real.service alias.service", ActiveState="active")
        + "\n"
    )
    with patch.object(web_ui, "run", return_value=output) as run:
        result = web_ui.systemctl_show_many(["alias.service"])
    assert result["alias.service"]["ActiveState"] == "active"
    assert run.call_count == 1
    # --all keeps empty properties present, so a suppressed one cannot merge
    # two units into a single corrupted record.
    assert "--all" in run.call_args[0][0]


def test_batched_show_drops_ambiguous_and_unrequested_records() -> None:
    output = (
        _stanza(Id="dup.service", Names="dup.service", ActiveState="active")
        + "\n"
        + _stanza(Id="other.service", Names="other.service dup.service", ActiveState="failed")
        + "\n"
        + _stanza(Id="extra.service", Names="extra.service", ActiveState="active")
        + "\n"
    )
    with patch.object(web_ui, "run", return_value=output):
        result = web_ui.systemctl_show_many(["dup.service", "other.service"])
    assert "dup.service" not in result
    assert result["other.service"]["ActiveState"] == "failed"
    assert "extra.service" not in result


def test_batched_show_tolerates_malformed_output() -> None:
    with patch.object(web_ui, "run", return_value="garbage\n\n= \n\nnot an identifier=1\n"):
        assert web_ui.systemctl_show_many(["a.service"]) == {}
    with patch.object(web_ui, "run") as run:
        assert web_ui.systemctl_show_many([]) == {}
    run.assert_not_called()


def test_service_status_uses_one_subprocess_and_falls_back_per_unit() -> None:
    units = list(web_ui.SERVICE_CATALOG)
    complete = "\n".join(
        _stanza(
            Id=unit,
            Names=unit,
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            UnitFileState="enabled",
            Description=unit,
        )
        for unit in units
    )
    with patch.object(web_ui, "run", return_value=complete) as run:
        payload = web_ui.service_status()
    assert set(payload) == set(units)
    assert run.call_count == 1

    partial = "\n".join(
        _stanza(
            Id=unit,
            Names=unit,
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            UnitFileState="enabled",
            Description=unit,
        )
        for unit in units[1:]
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], timeout: float = 5.0) -> str:
        calls.append(command)
        if len(calls) == 1:
            return partial
        return _stanza(
            LoadState="loaded", ActiveState="activating", SubState="start", Description="x"
        )

    with patch.object(web_ui, "run", side_effect=fake_run):
        payload = web_ui.service_status()
    # One batch plus exactly one single-unit fallback: the other eight units
    # are not re-queried because one of them was missing.
    assert len(calls) == 2
    assert calls[1][:3] == ["systemctl", "show", units[0]]
    assert payload[units[0]]["active"] == "activating"


def test_site_name_is_published_escaped_and_defaults_to_a_neutral_value() -> None:
    assert web_ui.SITE_NAME == "CamillaDSP"
    assert "{{site}}" not in web_ui.HTML
    assert "<title>CamillaDSP — audio control</title>" in web_ui.HTML
    assert "<h1><b>CamillaDSP</b></h1>" in web_ui.HTML

    source = (REPOSITORY / "scripts" / "web_ui.py").read_text(encoding="utf-8")
    assert 'HTML.replace("{{site}}", html.escape(SITE_NAME))' in source
    escaped = subprocess.run(
        [
            sys.executable,
            "-c",
            "import web_ui; print('<script>' in web_ui.HTML.split('</title>')[0])",
        ],
        cwd=str(REPOSITORY / "scripts"),
        env={"SITE_NAME": "<script>x</script>", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert escaped.stdout.strip() == "False"


def test_receiver_status_recognizes_both_marker_generations(tmp_path: Path) -> None:
    """The UI must not report "not configured" between update and reconfigure."""
    legacy_tag = "ug" "lan"
    dropin = tmp_path / "volume-sync.conf"
    shairport = tmp_path / "shairport-sync.conf"

    for marker, receiver, socket_env in (
        ("CDSP", "librespot-cdsp", "CDSP_SPOTIFY_VOLUME_SOCKET"),
        (
            legacy_tag.upper(),
            f"librespot-{legacy_tag}",
            f"{legacy_tag.upper()}_SPOTIFY_VOLUME_SOCKET",
        ),
    ):
        shairport.write_text(
            f"// {marker}-AIRPLAY-BEGIN\n"
            'ignore_volume_control = "yes";\n'
            "airplay_volume_bridge.py --notify\n",
            encoding="utf-8",
        )
        dropin.write_text(
            f"ExecStart=/usr/local/bin/{receiver}\n"
            "Environment=LIBRESPOT_VOLUME_CTRL=fixed\n"
            "Environment=LIBRESPOT_ONEVENT=x --notify-spotify\n"
            f"Environment={socket_env}=/run/raspotify/x.sock\n",
            encoding="utf-8",
        )
        with (
            patch.object(web_ui, "SHAIRPORT_CONFIG_PATH", shairport),
            patch.object(web_ui, "SPOTIFY_VOLUME_DROPIN_PATH", dropin),
            patch.object(web_ui, "service_is_active", return_value=True),
            patch.object(web_ui, "AUDIO_EQ_STATUS_PATH", tmp_path / "missing.json"),
            patch.object(web_ui, "AIRPLAY_VOLUME_STATUS_PATH", tmp_path / "gone.json"),
            patch.object(web_ui, "AUDIO_EQ_PATH", tmp_path / "audio-eq.json"),
            patch.object(web_ui, "SPEAKER_AUDIO_DIR", tmp_path / "speaker-audio"),
            patch.object(
                web_ui,
                "speaker_payload",
                return_value={"selection": {"selected": "default", "revision": 0}},
            ),
            patch.object(
                web_ui, "_selected_audio_path", return_value=tmp_path / "audio-eq.json"
            ),
            patch.object(web_ui, "iso226_capability", return_value=({}, False)),
        ):
            payload = web_ui.audio_eq_payload()
        assert payload["volume_bridge"]["airplay_configured"] is True, marker
        assert payload["volume_bridge"]["spotify_configured"] is True, receiver


def test_backup_directory_default_left_the_retired_state_tree() -> None:
    assert str(web_ui.AUDIO_EQ_BACKUP_DIR) == "/var/lib/cdsp-automation/audio-eq-backups"
    source = (REPOSITORY / "scripts" / "web_ui.py").read_text(encoding="utf-8")
    assert "/var/lib/installation" not in source


def _apply_web_volume(status_path: Path, payload: dict) -> tuple[list[float], float]:
    applied: list[float] = []
    client = SimpleNamespace(
        volume=SimpleNamespace(
            main_volume=lambda: -60.0,
            set_main_volume=applied.append,
            main_mute=lambda: False,
        )
    )
    with (
        patch.object(web_ui, "SPEAKER_STATUS_PATH", status_path),
        patch.object(web_ui, "camilla_client", return_value=nullcontext(client)),
        patch.object(web_ui, "audio_control_lock", return_value=nullcontext()),
        patch.object(web_ui, "camilla_status", return_value={}),
    ):
        web_ui.set_camilla_volume(payload)
        return applied, web_ui.current_volume_max()


def test_web_volume_cannot_exceed_the_applied_profile_ceiling(tmp_path: Path) -> None:
    """The UI has no ceiling of its own; the verified profile sets it."""
    status = tmp_path / "speaker-profile-status.json"
    status.write_text(
        json.dumps({"ok": True, "applied": "partymeh", "volume_limit_db": -20.0}),
        encoding="utf-8",
    )
    applied, ceiling = _apply_web_volume(status, {"volume_db": 0.0})
    assert ceiling == -20.0
    assert applied == [-20.0]
    # The same refusal on the relative path the +/- buttons use.
    applied, _ = _apply_web_volume(status, {"delta_db": 100.0})
    assert applied == [-20.0]
    # Below the cap the UI still passes the request through untouched.
    applied, _ = _apply_web_volume(status, {"volume_db": -35.0})
    assert applied == [-35.0]


def test_web_volume_fails_closed_without_a_verified_profile_status(
    tmp_path: Path,
) -> None:
    failsafe = speaker_profiles.FAILSAFE_VOLUME_LIMIT_DB
    assert failsafe < 0.0
    status = tmp_path / "speaker-profile-status.json"
    for payload in (
        None,
        {"ok": False, "volume_limit_db": 0.0},
        {"ok": True},
        {"ok": True, "volume_limit_db": None},
        {"ok": True, "volume_limit_db": "0"},
    ):
        if payload is None:
            status.unlink(missing_ok=True)
        else:
            status.write_text(json.dumps(payload), encoding="utf-8")
        applied, ceiling = _apply_web_volume(status, {"volume_db": 0.0})
        assert ceiling == failsafe, payload
        assert applied == [failsafe], payload


def test_web_volume_slider_bounds_follow_the_enforced_ceiling() -> None:
    """The control the operator sees must not offer what the server refuses."""
    assert "volume_max_db" in web_ui.HTML
    assert 'max="${volMax}"' in web_ui.HTML
    source = (REPOSITORY / "scripts" / "web_ui.py").read_text(encoding="utf-8")
    assert "VOLUME_MAX_DB = " not in source


def test_operator_profile_preflight_requires_the_profile_volume_cap(
    tmp_path: Path,
) -> None:
    """Selecting a capped profile reports an uncapped config before it goes live."""
    config_dir = tmp_path / "configs"
    profile_dir = tmp_path / "profiles"
    config_dir.mkdir()
    profile_dir.mkdir()
    document = partymeh_document()
    document["max_volume_db"] = -20
    (profile_dir / "partymeh.yml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    operator_configs = speaker_profiles.operator_configs_for_speaker("partymeh")
    for filename in operator_configs.values():
        (config_dir / filename).write_text(
            yaml.safe_dump({"devices": {"samplerate": 48000}}), encoding="utf-8"
        )

    checked: list[str] = []

    def fake_run_result(command: list[str], **_kwargs: object) -> SimpleNamespace:
        checked.append(Path(command[-1]).stem)
        return SimpleNamespace(returncode=0, stdout="")

    context = (
        patch.object(web_ui, "CDSP_CONFIG_DIR", config_dir),
        patch.object(web_ui, "SPEAKER_PROFILE_DIR", profile_dir),
        patch.object(web_ui, "run_result", side_effect=fake_run_result),
    )
    with context[0], context[1], context[2]:
        try:
            web_ui.preflight_speaker_profile("partymeh")
        except ValueError as exc:
            assert "no devices.volume_limit" in str(exc)
            assert "-20.0 dB" in str(exc)
        else:
            raise AssertionError("an uncapped operator config passed preflight")
    assert checked == []

    for filename in operator_configs.values():
        (config_dir / filename).write_text(
            yaml.safe_dump({"devices": {"samplerate": 48000, "volume_limit": -25}}),
            encoding="utf-8",
        )
    with context[0], context[1], context[2]:
        web_ui.preflight_speaker_profile("partymeh")
    assert sorted(checked) == sorted(
        Path(name).stem for name in operator_configs.values()
    )


# --------------------------------------------------------------------------
# Request-guard harness.  The handler's socket plumbing is replaced so do_POST
# can be driven directly, which is the only way to observe that an oversized or
# unauthorized body is refused *without being read*.
# --------------------------------------------------------------------------


class _CountingReader(io.BytesIO):
    """A request body that records every read, so "never buffered" is testable."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        self.reads.append(size)
        return super().read(size)


class _DrivableHandler(web_ui.Handler):
    def __init__(self, path: str, raw_headers: str, body: bytes) -> None:
        # Deliberately not calling BaseHTTPRequestHandler.__init__: it would
        # try to serve a real socket.
        self.path = path
        self.headers = email.message_from_string(raw_headers)
        self.rfile = _CountingReader(body)
        self.wfile = io.BytesIO()
        self.command = "POST"
        self.requestline = f"POST {path} HTTP/1.1"
        self.request_version = "HTTP/1.1"
        self.close_connection = False
        self.status: HTTPStatus | int | None = None
        self.sent_headers: dict[str, str] = {}

    def send_response(self, code, message=None):  # type: ignore[no-untyped-def]
        self.status = code

    def send_header(self, keyword, value):  # type: ignore[no-untyped-def]
        self.sent_headers[keyword] = value

    def end_headers(self) -> None:
        return None

    def send_error(self, code, message=None, explain=None):  # type: ignore[no-untyped-def]
        self.status = code

    def log_message(self, fmt, *args):  # type: ignore[no-untyped-def]
        return None

    def response_body(self) -> dict:
        raw = self.wfile.getvalue()
        return json.loads(raw.decode("utf-8")) if raw else {}


def _raw_headers(
    *,
    host: str = "pi.local:8088",
    origin: str | None = None,
    token: str | None = None,
    token_header: str = "Authorization",
    content_length: int | None = None,
    extra: str = "",
) -> str:
    lines = [f"Host: {host}"]
    if origin is not None:
        lines.append(f"Origin: {origin}")
    if token is not None:
        value = f"Bearer {token}" if token_header == "Authorization" else token
        lines.append(f"{token_header}: {value}")
    if content_length is not None:
        lines.append(f"Content-Length: {content_length}")
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n\n"


def _post(
    path: str = "/api/amps/off",
    *,
    body: bytes = b"{}",
    declared_length: int | None = None,
    **header_kwargs: object,
) -> _DrivableHandler:
    length = len(body) if declared_length is None else declared_length
    raw = _raw_headers(content_length=length, **header_kwargs)  # type: ignore[arg-type]
    return _DrivableHandler(path, raw, body)


def _environment_without(*names: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return env


def test_bind_host_reads_the_env_and_falls_back_to_every_interface() -> None:
    """0.0.0.0 stays the fallback so an upgrade never takes an install's UI away."""
    with patch.dict(
        os.environ, _environment_without("INSTALLATION_UI_HOST"), clear=True
    ):
        assert web_ui.ui_bind_host() == "0.0.0.0"
    with patch.dict(os.environ, {"INSTALLATION_UI_HOST": "   "}):
        assert web_ui.ui_bind_host() == "0.0.0.0"
    with patch.dict(os.environ, {"INSTALLATION_UI_HOST": "127.0.0.1"}):
        assert web_ui.ui_bind_host() == "127.0.0.1"
    with patch.dict(
        os.environ, _environment_without("INSTALLATION_UI_PORT"), clear=True
    ):
        assert web_ui.ui_bind_port() == 8088
    with patch.dict(os.environ, {"INSTALLATION_UI_PORT": "9000"}):
        assert web_ui.ui_bind_port() == 9000
    assert web_ui.DEFAULT_UI_HOST == "0.0.0.0"


def test_state_change_without_the_configured_token_is_refused() -> None:
    with patch.dict(os.environ, {"INSTALLATION_UI_TOKEN": "s3cret-value"}):
        handler = _post()
        with patch.object(web_ui, "turn_amps_off") as amps:
            handler.do_POST()
        assert handler.status == HTTPStatus.UNAUTHORIZED
        amps.assert_not_called()
        assert handler.sent_headers["WWW-Authenticate"].startswith("Bearer")
        # The body was never read, so the connection cannot be reused.
        assert handler.rfile.reads == []
        assert handler.close_connection is True

        wrong = _post(token="not-the-secret")
        with patch.object(web_ui, "turn_amps_off") as amps:
            wrong.do_POST()
        assert wrong.status == HTTPStatus.UNAUTHORIZED
        amps.assert_not_called()


def test_state_change_with_the_configured_token_is_accepted() -> None:
    with patch.dict(os.environ, {"INSTALLATION_UI_TOKEN": "s3cret-value"}):
        for header, value in (
            ("Authorization", "s3cret-value"),
            ("X-Control-Token", "s3cret-value"),
        ):
            handler = _post(token=value, token_header=header)
            with patch.object(web_ui, "turn_amps_off") as amps:
                handler.do_POST()
            assert handler.status == HTTPStatus.OK, header
            assert handler.response_body() == {"ok": True}
            amps.assert_called_once()


def test_token_comparison_uses_a_constant_time_primitive() -> None:
    """A plain == leaks the matching prefix length through its timing."""
    source = inspect.getsource(web_ui.authorize_state_change)
    assert "hmac.compare_digest(supplied, expected)" in source
    assert "supplied == expected" not in source
    assert "expected == supplied" not in source

    seen: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(left: str, right: str) -> bool:
        seen.append((left, right))
        return real(left, right)

    # A wrong first byte, a wrong length and a wrong last byte must all reach
    # the same primitive rather than being decided by a short-circuit.
    attempts = ("X3cret-value", "s", "s3cret-value-and-more", "s3cret-valuX")
    with (
        patch.dict(os.environ, {"INSTALLATION_UI_TOKEN": "s3cret-value"}),
        patch.object(web_ui.hmac, "compare_digest", spy),
    ):
        for attempt in attempts:
            try:
                web_ui.authorize_state_change(
                    email.message_from_string(_raw_headers(token=attempt))
                )
            except web_ui.RequestRefused as refusal:
                assert refusal.status == HTTPStatus.UNAUTHORIZED
            else:
                raise AssertionError(f"a wrong token was accepted: {attempt}")
    assert [left for left, _ in seen] == list(attempts)


def test_state_change_from_a_foreign_origin_is_refused() -> None:
    """Origin is checked with or without a token: a LAN browser is otherwise a
    confused deputy for anyone who can serve it a page."""
    with patch.dict(os.environ, _environment_without("INSTALLATION_UI_TOKEN"), clear=True):
        foreign = _post(origin="http://attacker.example")
        with patch.object(web_ui, "turn_amps_off") as amps:
            foreign.do_POST()
        assert foreign.status == HTTPStatus.FORBIDDEN
        assert "cross-origin" in foreign.response_body()["error"]
        amps.assert_not_called()
        assert foreign.rfile.reads == []

        same = _post(origin="http://pi.local:8088")
        with patch.object(web_ui, "turn_amps_off") as amps:
            same.do_POST()
        assert same.status == HTTPStatus.OK
        amps.assert_called_once()


def test_origin_matching_handles_absent_null_and_default_ports() -> None:
    same_site = web_ui.origin_is_same_site
    # No Origin at all is a non-browser client (curl, a shell script): allowed,
    # so scripted callers that worked before still work.
    assert same_site(None, "pi.local:8088") is True
    assert same_site("", "pi.local:8088") is True
    # A sandboxed or file:// page sends "null"; it is nobody's same site.
    assert same_site("null", "pi.local:8088") is False
    assert same_site("http://PI.local:8088", "pi.local:8088") is True
    assert same_site("http://pi.local", "pi.local:80") is True
    assert same_site("https://pi.local", "pi.local:443") is False
    assert same_site("http://pi.local:8089", "pi.local:8088") is False
    assert same_site("http://evil.example", "pi.local:8088") is False
    assert same_site("not-a-url", "pi.local:8088") is False
    assert same_site("http://pi.local:8088", "") is False


def test_oversized_body_is_refused_rather_than_buffered() -> None:
    oversized = web_ui.MAX_REQUEST_BODY_BYTES + 1
    handler = _post("/api/audio", body=b"{}", declared_length=oversized)
    with patch.object(web_ui, "write_audio_eq_state") as write_state:
        handler.do_POST()
    assert handler.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert str(web_ui.MAX_REQUEST_BODY_BYTES) in handler.response_body()["error"]
    # The refusal is decided from Content-Length alone: nothing was read.
    assert handler.rfile.reads == []
    write_state.assert_not_called()
    assert handler.close_connection is True


def test_undeclared_and_malformed_body_lengths_are_refused() -> None:
    chunked = _DrivableHandler(
        "/api/audio", _raw_headers(extra="Transfer-Encoding: chunked"), b"{}"
    )
    with patch.object(web_ui, "write_audio_eq_state") as write_state:
        chunked.do_POST()
    assert chunked.status == HTTPStatus.LENGTH_REQUIRED
    assert chunked.rfile.reads == []
    write_state.assert_not_called()

    for bad in ("not-a-number", "-1"):
        handler = _DrivableHandler(
            "/api/audio", _raw_headers(extra=f"Content-Length: {bad}"), b"{}"
        )
        handler.do_POST()
        assert handler.status == HTTPStatus.BAD_REQUEST, bad
        assert handler.rfile.reads == []


def test_a_body_at_the_limit_is_still_accepted() -> None:
    payload = json.dumps({"source": "toslink"}).encode("utf-8")
    assert len(payload) <= web_ui.MAX_REQUEST_BODY_BYTES
    handler = _post("/api/source", body=payload)
    with (
        patch.object(web_ui, "write_source_override") as override,
        patch.object(web_ui, "run_checked", return_value=""),
        patch.object(web_ui, "camilla_status", return_value={}),
        patch.object(web_ui, "source_status", return_value={"source": "toslink"}),
    ):
        handler.do_POST()
    assert handler.status == HTTPStatus.OK
    override.assert_called_once_with("toslink")
    assert handler.rfile.reads == [len(payload)]


def test_without_a_token_state_changes_behave_exactly_as_before() -> None:
    """No regression for the installs that never set INSTALLATION_UI_TOKEN."""
    for value in ({}, {"INSTALLATION_UI_TOKEN": "   "}):
        env = _environment_without("INSTALLATION_UI_TOKEN")
        env.update(value)  # type: ignore[arg-type]
        with patch.dict(os.environ, env, clear=True):
            assert web_ui.configured_ui_token() == ""
            handler = _post()
            with patch.object(web_ui, "turn_amps_off") as amps:
                handler.do_POST()
            assert handler.status == HTTPStatus.OK
            assert handler.response_body() == {"ok": True}
            amps.assert_called_once()
            assert "WWW-Authenticate" not in handler.sent_headers


def test_read_only_endpoints_stay_open_when_a_token_is_configured() -> None:
    """Only state changes are gated: the page itself must load so it can ask
    for the secret, and a GET cannot carry a header on a top-level navigation."""
    gated = inspect.getsource(web_ui.Handler.do_POST)
    assert "authorize_state_change(self.headers)" in gated
    assert "authorize_state_change" not in inspect.getsource(web_ui.Handler.do_GET)

    with patch.dict(os.environ, {"INSTALLATION_UI_TOKEN": "s3cret-value"}):
        handler = _DrivableHandler("/api/storage", _raw_headers(), b"")
        handler.command = "GET"
        with patch.object(web_ui, "storage_status", return_value={"mounted": False}):
            handler.do_GET()
    assert handler.status == HTTPStatus.OK


def test_handler_bounds_a_stalled_connection_with_a_socket_timeout() -> None:
    assert web_ui.Handler.timeout == web_ui.REQUEST_TIMEOUT_SECONDS
    assert 0 < web_ui.REQUEST_TIMEOUT_SECONDS <= 60


def test_frontend_sends_the_token_and_recovers_from_a_challenge() -> None:
    page = web_ui.HTML
    # Bearer header on every request the page makes.
    assert 'headers["Authorization"] = `Bearer ${controlToken}`' in page
    # Handed in out of band via the fragment, which servers and proxies do not
    # log, then remembered and scrubbed from the address bar.
    assert 'new URLSearchParams((location.hash || "").replace(/^#/, "")).get("token")' in page
    assert 'history.replaceState(null, "", location.pathname + location.search)' in page
    assert "window.localStorage.setItem(TOKEN_KEY, value)" in page
    # A 401 asks once and retries once; `retried` stops it looping.
    assert "if (res.status === 401 && !retried)" in page
    assert "return api(path, options, true);" in page


# ---------------------------------------------------------- MOTU main volume


@contextmanager
def _motu_device(**device_kwargs: object):
    """Point the UI's MOTU control at a replay of the real connect dump."""
    import motu_volume
    from test_motu_volume import Device, FakeClock

    device = Device(**device_kwargs)  # type: ignore[arg-type]
    clock = FakeClock()
    control = motu_volume.MotuMainVolume(connect=device.connect, clock=clock)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "MOTU_MAIN_VOLUME_MAX_DB",
            "MOTU_ACCESS_WINDOW_SECONDS",
            "MOTU_VOLUME_CACHE_SECONDS",
            "INSTALLATION_UI_TOKEN",
        )
    }
    with patch.dict(os.environ, env, clear=True):
        with patch.object(motu_volume, "MAIN_VOLUME", control):
            yield device, clock


def _get(path: str) -> _DrivableHandler:
    handler = _DrivableHandler(path, _raw_headers(), b"")
    handler.command = "GET"
    handler.do_GET()
    return handler


def test_motu_volume_change_is_token_gated_and_the_read_is_not() -> None:
    body = json.dumps({"volume_db": -20, "expected_db": -6}).encode()
    with _motu_device() as (device, clock):
        with patch.dict(os.environ, {"INSTALLATION_UI_TOKEN": "s3cret-value"}):
            refused = _post("/api/motu/volume", body=body)
            refused.do_POST()
            assert refused.status == HTTPStatus.UNAUTHORIZED
            cross_site = _post(
                "/api/motu/volume",
                body=body,
                origin="http://evil.example",
                token="s3cret-value",
            )
            cross_site.do_POST()
            assert cross_site.status == HTTPStatus.FORBIDDEN
            assert device.sockets == []

            read = _get("/api/motu/volume")
            assert read.status == HTTPStatus.OK
            assert read.response_body()["motu"]["volume_db"] == -6.0

            clock.now += 60
            allowed = _post("/api/motu/volume", body=body, token="s3cret-value")
            allowed.do_POST()
            assert allowed.status == HTTPStatus.OK
    assert device.sent == [bytes.fromhex("13930000000114")]


def test_motu_volume_ceiling_holds_for_a_request_straight_to_the_server() -> None:
    with _motu_device() as (device, _clock):
        with patch.dict(os.environ, {"MOTU_MAIN_VOLUME_MAX_DB": "-10"}):
            handler = _post(
                "/api/motu/volume",
                body=json.dumps({"volume_db": 0, "expected_db": -6}).encode(),
            )
            handler.do_POST()
    assert handler.status == HTTPStatus.OK
    assert handler.response_body()["motu"]["volume_db"] == -10.0
    assert device.sent == [bytes.fromhex("1393000000010a")]


def test_motu_volume_unknown_device_is_reported_and_writes_are_refused() -> None:
    with _motu_device(fail=True) as (device, clock):
        read = _get("/api/motu/volume")
        motu = read.response_body()["motu"]
        assert motu["known"] is False and motu["volume_db"] is None
        assert motu["writable"] is False

        clock.now += 60
        handler = _post(
            "/api/motu/volume",
            body=json.dumps({"volume_db": -20, "expected_db": -6}).encode(),
        )
        handler.do_POST()
    assert handler.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert handler.response_body()["motu"]["known"] is False
    assert device.sent == []


def test_motu_volume_burst_is_answered_429_with_retry_after() -> None:
    with _motu_device() as (device, clock):
        first = _post(
            "/api/motu/volume",
            body=json.dumps({"volume_db": -20, "expected_db": -6}).encode(),
        )
        first.do_POST()
        assert first.status == HTTPStatus.OK
        clock.now += 1
        second = _post(
            "/api/motu/volume",
            body=json.dumps({"volume_db": -21, "expected_db": -20}).encode(),
        )
        second.do_POST()
    assert second.status == HTTPStatus.TOO_MANY_REQUESTS
    assert second.response_body()["retry_after"] == 14.0
    assert len(device.sockets) == 1


def test_motu_volume_stale_expected_level_is_a_conflict_with_the_real_level() -> None:
    with _motu_device() as (device, _clock):
        handler = _post(
            "/api/motu/volume",
            body=json.dumps({"volume_db": -3, "expected_db": -30}).encode(),
        )
        handler.do_POST()
    assert handler.status == HTTPStatus.CONFLICT
    assert handler.response_body()["motu"]["volume_db"] == -6.0
    assert device.sent == []


def test_motu_volume_page_starts_from_the_device_and_debounces() -> None:
    page = web_ui.HTML
    # Read once at page load, never from the 5 s status sweep.
    load_body = page[
        page.index("async function load() {") : page.index("async function pollLevels()")
    ]
    assert "motu" not in load_body.lower()
    assert "\n    loadMotu();\n" in page
    # The slider exists only once a real reading is known; an unknown level
    # renders as text with no control to drag.
    render = page[
        page.index("function renderMotu() {") : page.index("function renderMotuCaption() {")
    ]
    assert render.index("if (!known)") < render.index('id="motuRange"')
    # Every write names the level it replaces, and moves are debounced and
    # retried after the server's window rather than sent per pixel.
    assert "expected_db: motu.volume_db" in page
    assert "setTimeout(flushMotu, Math.max(MOTU_DEBOUNCE_MS" in page
    assert "e.status === 429" in page
    # Nothing on this control touches the CamillaDSP mute or the ready token.
    motu_js = page[
        page.index("MOTU main volume ---") : page.index("/* ---------------- actions")
    ]
    assert "mute" not in motu_js.lower() and "/api/camilla" not in motu_js


def test_installer_ships_the_motu_module_and_its_ceiling() -> None:
    installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    assert "motu_access.py motu_volume.py web_ui.py" in installer
    assert "\nMOTU_MAIN_VOLUME_MAX_DB=0\n" in installer
    assert "\nMOTU_ACCESS_WINDOW_SECONDS=15\n" in installer
    assert "\nMOTU_ACCESS_PATH=/var/lib/cdsp-automation/motu-access.lock\n" in installer
