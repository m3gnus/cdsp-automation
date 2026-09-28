"""The remote key map: validation, and the press/hold rules it drives."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import signal
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

if "camilladsp" not in sys.modules:
    camilladsp = types.ModuleType("camilladsp")
    camilladsp.CamillaClient = object
    sys.modules["camilladsp"] = camilladsp
if "evdev" not in sys.modules:
    evdev = types.ModuleType("evdev")
    evdev.ecodes = types.SimpleNamespace(EV_KEY=1)
    evdev.categorize = lambda event: event
    evdev.list_devices = lambda: []
    evdev.InputDevice = object
    sys.modules["evdev"] = evdev

import remote_keymap
from remote_keymap import KeyDispatcher, KeymapError, parse_keymap
from scripts import cdsp_remote


def dispatch(events, keymap=None):
    """Feed (key, state, time) events; the actions, in order."""
    dispatcher = KeyDispatcher(keymap or remote_keymap.default_keymap())
    actions = []
    for key, state, now in events:
        actions.extend(dispatcher.event(key, state, now))
    return actions


# ------------------------------------------------ the long-standing layout


def test_volume_steps_on_press_and_every_second_repeat() -> None:
    events = [("KEY_VOLUMEUP", 1, 0.0)] + [("KEY_VOLUMEUP", 2, 0.5 + i * 0.03) for i in range(4)]
    events.append(("KEY_VOLUMEUP", 0, 0.7))
    assert dispatch(events) == ["volume_up"] * 3


def test_arrows_are_tone_and_mute_toggles() -> None:
    events = [
        (key, state, 0.0)
        for key in ("KEY_UP", "KEY_DOWN", "KEY_RIGHT", "KEY_LEFT", "KEY_MUTE")
        for state in (1, 0)
    ]
    assert dispatch(events) == ["treble_up", "treble_down", "bass_up", "bass_down", "mute"]


def test_enter_short_press_is_status_and_a_hold_resets_the_tone_once() -> None:
    assert dispatch([("KEY_ENTER", 1, 0.0), ("KEY_ENTER", 0, 0.2)]) == ["status"]
    held = [("KEY_ENTER", 1, 0.0)] + [("KEY_ENTER", 2, 0.5 + i * 0.25) for i in range(6)]
    held.append(("KEY_ENTER", 0, 2.0))
    assert dispatch(held) == ["tone_reset"]
    # A hold with no auto-repeat events still counts on release.
    assert dispatch([("KEY_ENTER", 1, 0.0), ("KEY_ENTER", 0, 1.5)]) == ["tone_reset"]


def test_power_restarts_on_release_after_a_hold_and_shuts_down_at_ten_seconds() -> None:
    assert dispatch([("KEY_POWER", 1, 0.0), ("KEY_POWER", 0, 0.4)]) == []
    assert dispatch([("KEY_POWER", 1, 0.0), ("KEY_POWER", 2, 1.5), ("KEY_POWER", 0, 3.0)]) == [
        "restart_services"
    ]
    long = [("KEY_POWER", 1, 0.0)] + [("KEY_POWER", 2, t) for t in (5.0, 10.1, 11.0)]
    long.append(("KEY_POWER", 0, 12.0))
    assert dispatch(long) == ["shutdown"]


def test_a_keycode_list_matches_any_mapped_name() -> None:
    assert dispatch([(["KEY_MUTE", "KEY_MIN_INTERESTING"], 1, 0.0)]) == ["mute"]
    assert dispatch([("KEY_PLAYPAUSE", 1, 0.0)]) == []


# -------------------------------------------------------------- site maps


def site(keys: dict, **settings) -> dict:
    return {"keys": keys, **settings}


def test_a_site_map_adds_source_and_amp_buttons() -> None:
    keymap = parse_keymap(
        site(
            {
                "KEY_HOMEPAGE": {"press": "next_source", "hold": "source_auto"},
                "KEY_BACK": {"press": "amps_off"},
                "KEY_MENU": {"press": "source_toslink"},
            }
        )
    )
    assert dispatch([("KEY_HOMEPAGE", 1, 0), ("KEY_HOMEPAGE", 0, 0.2)], keymap) == ["next_source"]
    assert dispatch([("KEY_HOMEPAGE", 1, 0), ("KEY_HOMEPAGE", 2, 1.2)], keymap) == ["source_auto"]
    assert dispatch([("KEY_BACK", 1, 0)], keymap) == ["amps_off"]
    assert dispatch([("KEY_MENU", 1, 0)], keymap) == ["source_toslink"]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (site({"KEY_A": {"press": "launch_missiles"}}), "unknown action"),
        (site({"KEY_A": {"press": "shutdown"}}), "hold or long_hold"),
        (site({"KEY_A": {"press": "restart_services"}}), "hold or long_hold"),
        (site({"A": {"press": "mute"}}), "evdev key name"),
        (site({"KEY_A": {}}), "at least one action"),
        (site({"KEY_A": {"press": "mute", "colour": "red"}}), "unknown field"),
        (site({"KEY_A": {"press": "volume_up", "repeat": True, "hold": "mute"}}), "repeating"),
        (site({"KEY_A": {"press": "volume_up", "repeat": "yes"}}), "true or false"),
        (site({"KEY_A": {"press": "mute"}}, volume_step_db=20), "between"),
        (site({"KEY_A": {"press": "mute"}}, volume_step_db=True), "number"),
        (site({"KEY_A": {"press": "mute"}}, hold_seconds=0.01), "between"),
        (site({"KEY_A": {"hold": "mute", "long_hold": "shutdown", "long_hold_seconds": 0.5}}), "exceed"),
        (site({}), "non-empty"),
        ({"keys": {"KEY_A": {"press": "mute"}}, "extra": 1}, "unknown setting"),
        ([], "JSON object"),
    ],
)
def test_invalid_maps_are_refused_with_the_reason(document, message: str) -> None:
    with pytest.raises(KeymapError, match=message):
        parse_keymap(document)


def test_speaker_changes_are_not_an_action() -> None:
    assert not any("speaker" in action for action in remote_keymap.ACTIONS)


def test_missing_invalid_and_valid_files(tmp_path: Path) -> None:
    path = tmp_path / "remote-keymap.json"
    keymap, problem = remote_keymap.load_keymap(path)
    assert problem is None and keymap.source == "built-in defaults"
    path.write_text("{broken")
    keymap, problem = remote_keymap.load_keymap(path)
    assert "invalid" in problem and keymap.keys == remote_keymap.default_keymap().keys
    path.write_text(json.dumps(site({"KEY_A": {"press": "mute"}}, volume_step_db=0.5)))
    keymap, problem = remote_keymap.load_keymap(path)
    assert problem is None and keymap.volume_step_db == 0.5 and list(keymap.keys) == ["KEY_A"]


def test_the_printed_map_loads_back_unchanged() -> None:
    default = remote_keymap.default_keymap()
    document = remote_keymap.keymap_document(default)
    assert parse_keymap(copy.deepcopy(document)).keys == default.keys
    assert document["keys"] == remote_keymap.DEFAULT_KEYMAP["keys"]


def test_the_example_file_is_a_valid_map() -> None:
    example = Path(__file__).resolve().parents[1] / "remote-keymap.example.json"
    parse_keymap(json.loads(example.read_text()))


# ------------------------------------------------------- remote actions


def test_run_action_uses_the_map_steps() -> None:
    keymap = parse_keymap(site({"KEY_A": {"press": "mute"}}, volume_step_db=2.5, tone_step_db=1))
    with (
        mock.patch.object(cdsp_remote, "keymap", keymap),
        mock.patch.object(cdsp_remote, "adjust_volume") as volume,
        mock.patch.object(cdsp_remote, "adjust_tone") as tone,
        mock.patch.object(cdsp_remote, "select_source") as source,
    ):
        cdsp_remote.run_action("volume_down")
        cdsp_remote.run_action("bass_up")
        cdsp_remote.run_action("source_gadget")
    volume.assert_called_once_with(-2.5)
    tone.assert_called_once_with("Bass", 1.0)
    source.assert_called_once_with("gadget")


def test_next_source_cycles_auto_then_each_available_source() -> None:
    import web_ui

    availability = {
        "streamer": {"exists": True},
        "gadget": {"exists": False},
        "toslink": {"exists": True},
        "analog": {"exists": False},
    }
    chosen = []
    for current, expected in (
        (None, "streamer"),
        ("streamer", "toslink"),
        ("toslink", "auto"),
        ("gadget", "streamer"),  # an unavailable pin starts the cycle over
    ):
        with (
            mock.patch.object(web_ui, "source_availability", return_value=availability),
            mock.patch.object(web_ui, "read_source_override", return_value=current),
            mock.patch.object(web_ui, "write_source_override", side_effect=chosen.append),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cdsp_remote.next_source()
        assert chosen[-1] == expected


def test_amps_off_signals_the_trigger_without_sudo() -> None:
    completed = SimpleNamespace(stdout="4242\n", returncode=0)
    with (
        mock.patch.object(cdsp_remote, "validate_trusted_executable"),
        mock.patch.object(cdsp_remote.subprocess, "run", return_value=completed) as run,
        mock.patch.object(cdsp_remote.os, "kill") as kill,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        cdsp_remote.amps_off()
    assert run.call_args.args[0][0] == cdsp_remote.SYSTEMCTL_BIN
    assert "sudo" not in " ".join(run.call_args.args[0])
    kill.assert_called_once_with(4242, signal.SIGUSR1)


def test_amps_off_does_nothing_when_the_trigger_is_not_running() -> None:
    completed = SimpleNamespace(stdout="0\n", returncode=0)
    with (
        mock.patch.object(cdsp_remote, "validate_trusted_executable"),
        mock.patch.object(cdsp_remote.subprocess, "run", return_value=completed),
        mock.patch.object(cdsp_remote.os, "kill") as kill,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        cdsp_remote.amps_off()
    kill.assert_not_called()


def test_motu_volume_step_names_the_level_it_replaces() -> None:
    import motu_volume

    status = {"known": True, "writable": True, "volume_db": -12.0}
    with (
        mock.patch.object(motu_volume, "read_status", return_value=status),
        mock.patch.object(
            motu_volume, "submit_request", return_value=("id", {"ok": True})
        ) as submit,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        cdsp_remote.adjust_motu_volume(1.0)
    submit.assert_called_once_with(
        {"volume_db": -11.0, "expected_db": -12.0}, cdsp_remote.MOTU_REPLY_SECONDS
    )


def test_motu_volume_step_is_skipped_while_the_level_is_unknown() -> None:
    import motu_volume

    with (
        mock.patch.object(motu_volume, "read_status", return_value={"known": False, "reason": "x"}),
        mock.patch.object(motu_volume, "submit_request") as submit,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        cdsp_remote.adjust_motu_volume(1.0)
    submit.assert_not_called()


def test_print_keymap_writes_clean_json(tmp_path: Path, capsys) -> None:
    with mock.patch.object(cdsp_remote, "REMOTE_KEYMAP_PATH", tmp_path / "none.json"):
        assert cdsp_remote.main(["--print-keymap"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["keys"]["KEY_POWER"] == {
        "hold": "restart_services",
        "long_hold": "shutdown",
    }
