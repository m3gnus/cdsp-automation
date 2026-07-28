"""Synthetic speaker-profile documents shared by the test suite.

Real installations keep their profile YAMLs outside this repository, so the
tests build minimal documents that exercise the same schema paths: a
three-way LR24 parametric crossover ("partymeh") and a hand-authored
CamillaDSP fragment profile.
"""

from __future__ import annotations

import copy
from typing import Any

import speaker_config


def _way(
    name: str,
    outputs: list[int],
    *,
    highpass: dict[str, Any] | None = None,
    lowpass: dict[str, Any] | None = None,
    source: str = "main",
) -> dict[str, Any]:
    return {
        "name": name,
        "source": source,
        "highpass": highpass,
        "lowpass": lowpass,
        "gain_db": 0,
        "delay_ms": 0,
        "invert": False,
        "outputs": outputs,
    }


def partymeh_document() -> dict[str, Any]:
    """Enabled three-way stereo parametric profile on outputs 1-6."""
    return {
        "version": 1,
        "id": "partymeh",
        "label": "PartyMEH",
        "description": "Three-way stereo test profile on outputs 1-6",
        "enabled": True,
        "revision": 0,
        "supported_sources": ["streamer", "gadget", "toslink", "analog"],
        "max_volume_db": 0,
        "bypass_user_eq": False,
        "raw_measurement": False,
        "crossover": {
            "version": 1,
            "playback": {
                "type": "Alsa",
                "device": "hw:test",
                "channels": 8,
                "format": "S32_LE",
            },
            "ways": [
                _way("low", [4, 5], lowpass={"freq": 300, "slope": "LR24"}),
                _way(
                    "mid",
                    [2, 3],
                    highpass={"freq": 300, "slope": "LR24"},
                    lowpass={"freq": 3000, "slope": "LR24"},
                ),
                _way("high", [0, 1], highpass={"freq": 3000, "slope": "LR24"}),
            ],
        },
    }


_DOCUMENTS = {
    "partymeh": partymeh_document,
}


def seed_profile(profile_id: str) -> dict[str, Any]:
    """Normalized (crossover-expanded) form of a synthetic profile."""
    return speaker_config.normalize_profile(copy.deepcopy(_DOCUMENTS[profile_id]()))


def make_test_speaker_profile(*, bypass_user_eq: bool = False) -> dict[str, Any]:
    """Hand-authored CamillaDSP fragment profile with two live outputs."""
    return speaker_config.normalize_profile(
        {
            "version": 1,
            "id": "partymeh",
            "label": "PartyMEH",
            "enabled": True,
            "supported_sources": ["streamer"],
            "output_channels": 4,
            "active_outputs": [0, 1],
            "muted_outputs": [2, 3],
            "output_roles": ["left", "right", "unused", "unused"],
            "max_volume_db": -6,
            "bypass_user_eq": bypass_user_eq,
            "raw_measurement": False,
            "capabilities": {"secondary_program": False},
            "camilladsp": {
                "devices": {
                    "playback": {
                        "type": "Alsa",
                        "device": "hw:test",
                        "channels": 4,
                        "format": "S32_LE",
                    }
                },
                "filters": {},
                "mixers": {
                    "spk_partymeh_output": {
                        "channels": {"in": 2, "out": 4},
                        "mapping": [
                            {"dest": 0, "mute": False, "sources": [{"channel": 0}]},
                            {"dest": 1, "mute": False, "sources": [{"channel": 1}]},
                            {"dest": 2, "mute": True, "sources": []},
                            {"dest": 3, "mute": True, "sources": []},
                        ],
                    }
                },
                "processors": {},
                "pipeline": [
                    {"type": "Mixer", "name": "spk_partymeh_output"}
                ],
            },
        }
    )


def capture_base(channels: int) -> dict[str, Any]:
    """Minimal source base config with an n-channel loopback capture."""
    return {
        "devices": {
            "samplerate": 48000,
            "chunksize": 1024,
            "capture": {
                "type": "Alsa",
                "device": "hw:Loopback,1",
                "channels": channels,
                "format": "S32_LE",
            },
            "volume_limit": -10,
        },
        "filters": {},
        "pipeline": [],
    }
