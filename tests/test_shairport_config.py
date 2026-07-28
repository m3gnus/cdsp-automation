"""shairport-sync configuration editing: idempotence, restore and hooks."""

from __future__ import annotations

from pathlib import Path

import configure_shairport


REPOSITORY = Path(__file__).resolve().parents[1]


def test_airplay_config_keeps_audio_at_unity_and_serializes_callbacks() -> None:
    snippet = (REPOSITORY / "shairport-sync-volume.conf.example").read_text()
    assert 'ignore_volume_control = "yes"' in snippet
    assert "run_this_when_volume_is_set = " in snippet
    assert "/usr/local/libexec/airplay_volume_bridge.py --notify " in snippet


def test_shairport_configurator_is_idempotent_and_preserves_other_settings() -> None:
    initial = 'general =\n{\n    name = "UGLAN";\n    ignore_volume_control = "no";\n};\ndsp =\n{\n    loudness = "yes";\n    loudness_reference_volume_db = -20.0;\n};\ndiagnostics =\n{\n    statistics = "yes";\n    log_verbosity = 0;\n};\nalsa = { output_device = "hw:Loopback"; };\n'
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    first = configure_shairport.update_general_block(initial, callback)
    second = configure_shairport.update_general_block(first, callback)
    assert first == second
    assert 'name = "UGLAN"' in first
    assert 'output_device = "hw:Loopback"' in first
    assert first.count("run_this_when_volume_is_set") == 1
    assert first.count("run_this_before_play_begins") == 1
    assert first.count("run_this_after_play_ends") == 1
    assert first.count('wait_for_completion = "yes"') == 1
    assert "--airplay-start" in first
    assert "--airplay-stop" in first
    assert first.count('loudness = "no"') == 1
    assert "loudness_reference_volume_db" not in first
    assert first.count('statistics = "no"') == 1
    assert first.count("log_verbosity = 1") == 1
    restored = configure_shairport.update_general_block(second, None)
    assert restored == initial


def test_shairport_configurator_preserves_existing_session_hooks() -> None:
    initial = (
        'general =\n{\n'
        '    name = "UGLAN";\n'
        '};\n'
        'sessioncontrol =\n{\n'
        '    run_this_before_play_begins = "/opt/old-start";\n'
        '    run_this_after_play_ends = "/opt/old-stop";\n'
        '    wait_for_completion = "no";\n'
        '};\n'
    )
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    managed = configure_shairport.update_general_block(initial, callback)
    assert managed.count("sessioncontrol =") == 1
    assert "/opt/old-start" not in managed
    assert configure_shairport.update_general_block(managed, None) == initial


def test_shairport_configurator_accepts_missing_or_commented_dsp_block() -> None:
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    for suffix in (
        'alsa = { output_device = "hw:Loopback"; };\n',
        '// dsp =\n// {\n//     loudness = "yes";\n// };\n',
    ):
        initial = 'general =\n{\n    name = "UGLAN";\n};\n' + suffix
        first = configure_shairport.update_general_block(initial, callback)
        assert configure_shairport.update_general_block(first, callback) == first
        assert 'ignore_volume_control = "yes"' in first
        assert "run_this_when_volume_is_set" in first
        assert 'loudness = "no"' not in first
        assert configure_shairport.update_general_block(first, None) == initial


def test_cli_install_then_remove_round_trips_a_config_without_an_alsa_block(
    tmp_path: Path,
) -> None:
    """The installer's own two argument forms, on a stock shairport config."""
    initial = 'general =\n{\n    name = "Pi";\n};\n'
    config = tmp_path / "shairport-sync.conf"
    config.write_text(initial, encoding="utf-8")
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"

    assert configure_shairport.main([str(config), callback]) == 0
    managed = config.read_text(encoding="utf-8")
    assert 'ignore_volume_control = "yes"' in managed

    assert configure_shairport.main(["--remove", str(config)]) == 0
    assert config.read_text(encoding="utf-8") == initial
    assert "UGLAN" not in config.read_text(encoding="utf-8")


def test_shairport_configurator_adds_and_removes_missing_diagnostics_block() -> None:
    initial = 'general =\n{\n    name = "UGLAN";\n};\n'
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    managed = configure_shairport.update_general_block(initial, callback)
    assert managed.count("diagnostics =") == 1
    assert 'statistics = "no"' in managed
    assert "log_verbosity = 1" in managed
    assert configure_shairport.update_general_block(managed, callback) == managed
    assert configure_shairport.update_general_block(managed, None) == initial
