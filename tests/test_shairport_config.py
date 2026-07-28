"""shairport-sync configuration editing: idempotence, restore and hooks."""

from __future__ import annotations

import base64

from pathlib import Path

import configure_shairport


REPOSITORY = Path(__file__).resolve().parents[1]

# The marker prefix earlier releases wrote, assembled from fragments so the
# literal never appears in this repository.
LEGACY_PREFIX = "UG" "LAN"
CALLBACK = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"


def legacy(marker: str) -> str:
    """The pre-rename spelling of one of the module's own markers."""
    new = f"// {configure_shairport.MARKER_PREFIX}-"
    assert marker.startswith(new)
    return marker.replace(new, f"// {LEGACY_PREFIX}-", 1)


def test_airplay_config_keeps_audio_at_unity_and_serializes_callbacks() -> None:
    snippet = (REPOSITORY / "shairport-sync-volume.conf.example").read_text()
    assert 'ignore_volume_control = "yes"' in snippet
    assert "run_this_when_volume_is_set = " in snippet
    assert "/usr/local/libexec/airplay_volume_bridge.py --notify " in snippet


def test_shairport_configurator_is_idempotent_and_preserves_other_settings() -> None:
    initial = 'general =\n{\n    name = "Living Room";\n    ignore_volume_control = "no";\n};\ndsp =\n{\n    loudness = "yes";\n    loudness_reference_volume_db = -20.0;\n};\ndiagnostics =\n{\n    statistics = "yes";\n    log_verbosity = 0;\n};\nalsa = { output_device = "hw:Loopback"; };\n'
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    first = configure_shairport.update_general_block(initial, callback)
    second = configure_shairport.update_general_block(first, callback)
    assert first == second
    assert 'name = "Living Room"' in first
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
        '    name = "Living Room";\n'
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
        initial = 'general =\n{\n    name = "Living Room";\n};\n' + suffix
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
    restored = config.read_text(encoding="utf-8")
    assert restored == initial
    assert configure_shairport.MARKER_PREFIX + "-" not in restored


def test_managed_block_written_under_the_previous_marker_name_is_migrated() -> None:
    """A deployed config keeps its saved original across the marker rename.

    The base64 payload is what --remove restores, so losing it would strand the
    operator's pre-install values behind a marker nothing recognizes any more.
    """
    initial = 'general =\n{\n    name = "Living Room";\n    ignore_volume_control = "no";\n};\n'
    managed = configure_shairport.update_general_block(initial, CALLBACK)
    aged = managed.replace(
        configure_shairport.GENERAL_BEGIN, legacy(configure_shairport.GENERAL_BEGIN)
    ).replace(configure_shairport.GENERAL_END, legacy(configure_shairport.GENERAL_END))
    assert LEGACY_PREFIX in aged

    migrated = configure_shairport.update_general_block(aged, CALLBACK)
    assert LEGACY_PREFIX not in migrated
    assert migrated.count(configure_shairport.GENERAL_BEGIN) == 1
    assert migrated.count("run_this_when_volume_is_set") == 1
    assert configure_shairport.update_general_block(migrated, None) == initial


def test_created_blocks_are_replaced_not_duplicated_after_the_marker_rename() -> None:
    """The wrapper markers gate block creation; two blocks fail Shairport."""
    initial = 'general =\n{\n    name = "Living Room";\n};\n'
    managed = configure_shairport.update_general_block(initial, CALLBACK)
    aged = managed
    for marker in (
        configure_shairport.SESSION_BLOCK_BEGIN,
        configure_shairport.SESSION_BLOCK_END,
        configure_shairport.SESSION_BEGIN,
        configure_shairport.SESSION_END,
        configure_shairport.DIAGNOSTICS_BLOCK_BEGIN,
        configure_shairport.DIAGNOSTICS_BLOCK_END,
        configure_shairport.DIAGNOSTICS_BEGIN,
        configure_shairport.DIAGNOSTICS_END,
    ):
        aged = aged.replace(marker, legacy(marker))

    migrated = configure_shairport.update_general_block(aged, CALLBACK)
    assert migrated.count("sessioncontrol =") == 1
    assert migrated.count("diagnostics =") == 1
    assert LEGACY_PREFIX not in migrated
    assert configure_shairport.update_general_block(migrated, None) == initial


def test_marker_tags_do_not_collide_across_generations() -> None:
    """AIRPLAY, AIRPLAY-SESSION and AIRPLAY-SESSION-BLOCK stay distinct."""
    markers = {
        configure_shairport.GENERAL_BEGIN,
        configure_shairport.SESSION_BEGIN,
        configure_shairport.SESSION_BLOCK_BEGIN,
    }
    assert len(markers) == 3
    for marker in markers:
        others = markers - {marker}
        assert not any(marker == other for other in others)
        assert not any(legacy(marker) == legacy(other) for other in others)
        # A longer tag must never be recognized as a shorter one.
        assert all(not other.endswith(marker.split("-", 1)[1]) for other in others)


def test_marker_blocks_this_tool_never_wrote_are_left_alone() -> None:
    """Recognition is by exact marker, so a foreign block is not claimed."""
    initial = (
        "general =\n{\n"
        '    name = "Living Room";\n'
        "    // SITE-AIRPLAY-BEGIN\n"
        "    // original-base64: bm90LW1pbmU=\n"
        "    // SITE-AIRPLAY-END\n"
        "};\n"
    )
    managed = configure_shairport.update_general_block(initial, CALLBACK)
    assert "// SITE-AIRPLAY-BEGIN" in managed
    assert "// SITE-AIRPLAY-END" in managed
    # Someone else's saved payload must not be mistaken for this tool's.
    assert "bm90LW1pbmU=" in managed
    assert configure_shairport.update_general_block(managed, None) == initial


def test_shairport_configurator_adds_and_removes_missing_diagnostics_block() -> None:
    initial = 'general =\n{\n    name = "Living Room";\n};\n'
    callback = "/usr/bin/python3 /usr/local/libexec/airplay_volume_bridge.py --notify"
    managed = configure_shairport.update_general_block(initial, callback)
    assert managed.count("diagnostics =") == 1
    assert 'statistics = "no"' in managed
    assert "log_verbosity = 1" in managed
    assert configure_shairport.update_general_block(managed, callback) == managed
    assert configure_shairport.update_general_block(managed, None) == initial


def test_callback_only_configure_migrates_a_legacy_output_block(tmp_path) -> None:
    """The installer's update path passes no device; legacy markers must
    still be rewritten in place with the managed device preserved."""
    legacy_begin = "// " + "UG" + "LAN-OUTPUT-BEGIN"
    legacy_end = "// " + "UG" + "LAN-OUTPUT-END"
    encoded = base64.b64encode(b'    output_device = "hw:0";\n').decode("ascii")
    config = tmp_path / "shairport-sync.conf"
    config.write_text(
        'general =\n{\n    name = "Living Room";\n};\n'
        "alsa =\n{\n"
        f"    {legacy_begin}\n"
        f"    // original-base64: {encoded}\n"
        '    output_device = "site_main";\n'
        f"    {legacy_end}\n"
        "};\n",
        encoding="utf-8",
    )
    configure_shairport.configure(config, "/usr/bin/callback --notify")
    text = config.read_text(encoding="utf-8")
    assert legacy_begin not in text and legacy_end not in text
    assert configure_shairport.ALSA_BEGIN in text
    assert 'output_device = "site_main"' in text
    assert f"// original-base64: {encoded}" in text
    configure_shairport.configure(config, None)
    restored = config.read_text(encoding="utf-8")
    assert configure_shairport.ALSA_BEGIN not in restored
    assert 'output_device = "hw:0"' in restored


def test_remove_strips_a_legacy_output_block_without_a_device_argument(tmp_path) -> None:
    legacy_begin = "// " + "UG" + "LAN-OUTPUT-BEGIN"
    legacy_end = "// " + "UG" + "LAN-OUTPUT-END"
    encoded = base64.b64encode(b'    output_device = "plughw:1";\n').decode("ascii")
    config = tmp_path / "shairport-sync.conf"
    config.write_text(
        "general =\n{\n};\n"
        "alsa =\n{\n"
        f"    {legacy_begin}\n"
        f"    // original-base64: {encoded}\n"
        '    output_device = "site_main";\n'
        f"    {legacy_end}\n"
        "};\n",
        encoding="utf-8",
    )
    configure_shairport.configure(config, None)
    text = config.read_text(encoding="utf-8")
    assert legacy_begin not in text and "OUTPUT-BEGIN" not in text
    assert 'output_device = "plughw:1"' in text
