"""Per-source volume memory: the store and the switcher's muted transition."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import source_volume
from scripts import source_switcher as switcher
from test_source_switcher import (
    FakeSwitcherConfig,
    FakeSwitcherGeneral,
    FakeSwitcherVolume,
)


# ------------------------------------------------------------------ store


def test_remember_and_recall_per_speaker_and_source() -> None:
    assert source_volume.remember("kantarellen", "toslink", cdsp_db=-30, motu_db=-6)
    assert source_volume.remember("kantarellen", "streamer", cdsp_db=-42.5)
    assert source_volume.remembered("kantarellen", "toslink") == {
        "cdsp_db": -30.0,
        "motu_db": -6.0,
    }
    assert source_volume.remembered("kantarellen", "streamer") == {"cdsp_db": -42.5}
    assert source_volume.remembered("partymeh", "toslink") == {}


def test_unchanged_levels_are_not_rewritten() -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-30)
    before = source_volume.STATE_PATH.stat().st_mtime_ns
    assert not source_volume.remember("kantarellen", "toslink", cdsp_db=-30)
    assert source_volume.STATE_PATH.stat().st_mtime_ns == before


def test_a_missing_level_leaves_the_stored_one_alone() -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-30, motu_db=-6)
    source_volume.remember("kantarellen", "toslink", cdsp_db=-20, motu_db=None)
    assert source_volume.remembered("kantarellen", "toslink") == {
        "cdsp_db": -20.0,
        "motu_db": -6.0,
    }


def test_levels_are_bounded_and_malformed_entries_dropped() -> None:
    source_volume.STATE_PATH.write_text(
        json.dumps(
            {
                "speakers": {
                    "kantarellen": {
                        "toslink": {"cdsp_db": 12, "motu_db": -500},
                        "streamer": {"cdsp_db": "loud"},
                        "../x": {"cdsp_db": -1},
                    },
                    "bad name": {"toslink": {"cdsp_db": -1}},
                }
            }
        )
    )
    assert source_volume.read_state()["speakers"] == {
        "kantarellen": {"toslink": {"cdsp_db": 0.0, "motu_db": -100.0}}
    }


def test_an_unreadable_file_is_an_empty_memory() -> None:
    source_volume.STATE_PATH.write_text("{not json")
    assert source_volume.remembered("kantarellen", "toslink") == {}


def test_forget() -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-30)
    assert source_volume.forget("kantarellen", "toslink")
    assert not source_volume.forget("kantarellen", "toslink")
    assert source_volume.read_state()["speakers"] == {}


def test_invalid_names_are_refused() -> None:
    with pytest.raises(ValueError):
        source_volume.remember("kantarellen", "../etc", cdsp_db=-1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", True), ("1", True), ("0", False), ("off", False), ("False", False)],
)
def test_enabled_from_env(monkeypatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("SOURCE_VOLUME_MEMORY", raw)
    assert source_volume.enabled() is expected


# ------------------------------------------------------------- transition


class FakeMotu:
    """The MotuConnection surface the transition uses."""

    def __init__(self, level: float | None) -> None:
        self.level = level
        self.writes: list[tuple[float, bool]] = []

    def level_db(self) -> float | None:
        return self.level

    def restore_level(self, volume_db: float, *, undo: bool = False) -> bool:
        self.writes.append((volume_db, undo))
        self.level = volume_db
        return True


def _client(tmp_path: Path, volume: float) -> SimpleNamespace:
    previous = tmp_path / "streamer.yml"
    previous.write_text("devices: {}\n")
    (tmp_path / "toslink.yml").write_text("devices: {}\n")
    return SimpleNamespace(
        config=FakeSwitcherConfig(str(previous)),
        volume=FakeSwitcherVolume(volume=volume),
        general=FakeSwitcherGeneral(["ProcessingState.RUNNING"] * 20),
    )


def _switch(tmp_path: Path, client, *, remember: bool, motu=None, status=None):
    previous = tmp_path / "streamer.yml"
    target_path = tmp_path / "toslink.yml"
    target = {
        "speaker": "kantarellen",
        "source": "toslink",
        "legacy": True,
        "digest": "",
        "volume_limit_db": -3,
    }
    identities = {
        str(previous): ("streamer", "kantarellen"),
        str(target_path): ("toslink", "kantarellen"),
    }
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(switcher, "AUDIO_CONTROL_LOCK_PATH", tmp_path / "audio.lock")
        )
        stack.enter_context(patch.object(switcher, "validate_config_file"))
        stack.enter_context(patch.object(switcher, "ensure_audio_eq"))
        stack.enter_context(
            patch.object(
                switcher, "_write_speaker_status", side_effect=status or (lambda _p: None)
            )
        )
        stack.enter_context(
            patch.object(switcher, "managed_config_identity", side_effect=identities.get)
        )
        stack.enter_context(patch.object(switcher, "_motu", motu))
        stack.enter_context(patch.object(switcher, "_owns_motu_clock", return_value=False))
        stack.enter_context(patch.object(switcher.time, "sleep"))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        switcher.apply_config(
            client, str(target_path), target=target, remember_volumes=remember
        )


def test_switch_records_outgoing_and_starts_incoming_at_its_remembered_levels(
    tmp_path: Path,
) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-25, motu_db=-12)
    motu = FakeMotu(level=-4)
    client = _client(tmp_path, -40)
    _switch(tmp_path, client, remember=True, motu=motu)
    assert client.volume.volume == -25
    assert client.volume.mute is False
    assert motu.writes == [(-12.0, False)]
    assert source_volume.remembered("kantarellen", "streamer") == {
        "cdsp_db": -40.0,
        "motu_db": -4.0,
    }


def test_a_remembered_level_is_still_clamped_to_the_profile_ceiling(
    tmp_path: Path,
) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=0)
    client = _client(tmp_path, -40)
    _switch(tmp_path, client, remember=True)
    assert client.volume.volume == -3


def test_a_pair_seen_first_keeps_the_playing_level(tmp_path: Path) -> None:
    motu = FakeMotu(level=-4)
    client = _client(tmp_path, -40)
    _switch(tmp_path, client, remember=True, motu=motu)
    assert client.volume.volume == -40
    assert motu.writes == []


def test_without_memory_the_volume_carries_over(tmp_path: Path) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-25, motu_db=-12)
    motu = FakeMotu(level=-4)
    client = _client(tmp_path, -40)
    _switch(tmp_path, client, remember=False, motu=motu)
    assert client.volume.volume == -40
    assert motu.writes == []
    assert source_volume.remembered("kantarellen", "streamer") == {}


def test_a_failed_switch_puts_the_previous_levels_back_and_stays_muted(
    tmp_path: Path,
) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-10, motu_db=0)
    motu = FakeMotu(level=-20)

    def status(payload: dict) -> None:
        if payload.get("ok"):
            raise OSError("status not written")

    client = _client(tmp_path, -40)
    with pytest.raises(OSError):
        _switch(tmp_path, client, remember=True, motu=motu, status=status)
    assert motu.writes == [(0.0, False), (-20.0, True)]
    assert client.volume.volume == -40
    assert client.volume.mute is True
    assert client.config.path == str(tmp_path / "streamer.yml")


def test_a_broken_memory_never_fails_the_switch(tmp_path: Path) -> None:
    client = _client(tmp_path, -40)
    with patch.object(source_volume, "remember", side_effect=OSError("read-only")):
        _switch(tmp_path, client, remember=True)
    assert client.volume.volume == -40
    assert client.volume.mute is False


def test_an_unidentifiable_previous_config_skips_the_memory(tmp_path: Path) -> None:
    source_volume.remember("kantarellen", "toslink", cdsp_db=-25)
    with patch.object(
        switcher, "managed_config_identity", side_effect=OSError("unreadable")
    ):
        assert switcher._pair_levels("/x.yml", ("toslink", "kantarellen"), -40) == {}
