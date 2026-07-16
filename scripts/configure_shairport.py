#!/usr/bin/env python3
"""Idempotently configure Shairport Sync's AirPlay volume callback."""

from __future__ import annotations

import os
import base64
import re
import shutil
import sys
import tempfile
from pathlib import Path


GENERAL_KEYS = ("ignore_volume_control", "run_this_when_volume_is_set")
GENERAL_BEGIN = "// UGLAN-AIRPLAY-BEGIN"
GENERAL_END = "// UGLAN-AIRPLAY-END"
SESSION_KEYS = (
    "run_this_before_play_begins",
    "run_this_after_play_ends",
    "wait_for_completion",
)
SESSION_BEGIN = "// UGLAN-AIRPLAY-SESSION-BEGIN"
SESSION_END = "// UGLAN-AIRPLAY-SESSION-END"
SESSION_BLOCK_BEGIN = "// UGLAN-AIRPLAY-SESSION-BLOCK-BEGIN"
SESSION_BLOCK_END = "// UGLAN-AIRPLAY-SESSION-BLOCK-END"
DSP_KEYS = ("loudness", "loudness_reference_volume_db")
DSP_BEGIN = "// UGLAN-LOUDNESS-BEGIN"
DSP_END = "// UGLAN-LOUDNESS-END"
ALSA_KEYS = ("output_device",)
ALSA_BEGIN = "// UGLAN-OUTPUT-BEGIN"
ALSA_END = "// UGLAN-OUTPUT-END"


def _update_block(
    text: str,
    block: str,
    keys: tuple[str, ...],
    begin: str,
    end_marker: str,
    managed_settings: list[str] | None,
) -> str:
    lines = text.splitlines(keepends=True)
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^\s*{re.escape(block)}\s*=", line)
            and not line.lstrip().startswith("//")
        ),
        None,
    )
    if start is None:
        raise ValueError(f"active {block} block not found")
    depth = 0
    opened = False
    end = None
    for index in range(start, len(lines)):
        code = lines[index].split("//", 1)[0]
        depth += code.count("{")
        if "{" in code:
            opened = True
        depth -= code.count("}")
        if opened and depth == 0:
            end = index
            break
    if end is None:
        raise ValueError(f"{block} block is not balanced")

    key_pattern = re.compile(r"^\s*(" + "|".join(keys) + r")\s*=")
    original_values: list[str] = []
    body: list[str] = []
    in_managed = False
    encoded_original = None
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped == begin:
            in_managed = True
            continue
        if stripped == end_marker:
            in_managed = False
            continue
        if in_managed:
            if stripped.startswith("// original-base64: "):
                encoded_original = stripped.split(": ", 1)[1]
            continue
        if key_pattern.match(line):
            original_values.append(line)
        else:
            body.append(line)
    if encoded_original is not None:
        original_text = base64.b64decode(encoded_original).decode("utf-8")
        original_values = original_text.splitlines(keepends=True)
    if managed_settings is None:
        settings = original_values
    else:
        encoded = base64.b64encode("".join(original_values).encode("utf-8")).decode(
            "ascii"
        )
        settings = [
            f"    {begin}\n",
            f"    // original-base64: {encoded}\n",
            *managed_settings,
            f"    {end_marker}\n",
        ]
    return "".join(lines[: start + 1] + body + settings + lines[end:])


def update_general_block(text: str, callback: str | None) -> str:
    general = None
    dsp = None
    if callback is not None:
        general = [
            '    ignore_volume_control = "yes";\n',
            f'    run_this_when_volume_is_set = "{callback} ";\n',
        ]
        # Shairport's optional DSP loudness is separate from volume handling.
        # Make it explicitly off so it can never stack with CamillaDSP ISO226.
        dsp = ['    loudness = "no";\n']
    updated = _update_block(
        text,
        "general",
        GENERAL_KEYS,
        GENERAL_BEGIN,
        GENERAL_END,
        general,
    )
    try:
        updated = _update_block(updated, "dsp", DSP_KEYS, DSP_BEGIN, DSP_END, dsp)
    except ValueError as exc:
        # A missing (or commented-only) DSP block means Shairport DSP loudness
        # cannot be enabled by this config, so there is nothing to manage.
        if str(exc) == "active dsp block not found":
            pass
        else:
            raise

    created_session = re.compile(
        rf"\n?{re.escape(SESSION_BLOCK_BEGIN)}\n.*?{re.escape(SESSION_BLOCK_END)}\n?",
        re.DOTALL,
    )
    updated = created_session.sub("\n", updated)
    session_settings = None
    if callback is not None:
        suffix = " --notify"
        if not callback.endswith(suffix):
            raise ValueError("AirPlay callback must end with --notify")
        command = callback[: -len(suffix)]
        session_settings = [
            f'    run_this_before_play_begins = "{command} --airplay-start";\n',
            f'    run_this_after_play_ends = "{command} --airplay-stop";\n',
            '    wait_for_completion = "yes";\n',
        ]
    try:
        return _update_block(
            updated,
            "sessioncontrol",
            SESSION_KEYS,
            SESSION_BEGIN,
            SESSION_END,
            session_settings,
        )
    except ValueError as exc:
        if str(exc) != "active sessioncontrol block not found":
            raise
        if session_settings is None:
            return updated
        separator = "" if not updated or updated.endswith("\n") else "\n"
        return (
            updated
            + separator
            + f"{SESSION_BLOCK_BEGIN}\n"
            + "sessioncontrol =\n{\n"
            + f"    {SESSION_BEGIN}\n"
            + "".join(session_settings)
            + f"    {SESSION_END}\n"
            + "};\n"
            + f"{SESSION_BLOCK_END}\n"
        )


def configure(
    path: Path, callback: str | None, output_device: str | None = None
) -> Path:
    original = path.read_text(encoding="utf-8")
    updated = update_general_block(original, callback)
    if output_device is not None:
        managed = None if callback is None else [f'    output_device = "{output_device}";\n']
        updated = _update_block(
            updated, "alsa", ALSA_KEYS, ALSA_BEGIN, ALSA_END, managed
        )
    backup = path.with_suffix(path.suffix + ".pre-airplay-volume-bridge")
    if updated == original:
        return backup
    if not backup.exists():
        shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)
    return backup


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    remove = len(argv) == 2 and argv[0] == "--remove"
    if not remove and len(argv) not in (2, 3):
        print(
            "usage: configure_shairport.py [--remove] CONFIG [CALLBACK [OUTPUT_DEVICE]]",
            file=sys.stderr,
        )
        return 2
    try:
        if remove:
            path = Path(argv[1])
            configure(path, None, "uglan_main")
            print(f"Removed managed AirPlay settings from {path}")
            return 0
        backup = configure(Path(argv[0]), argv[1], argv[2] if len(argv) == 3 else None)
        print(f"Configured {argv[0]}; first-install backup: {backup}")
        return 0
    except Exception as exc:
        print(f"Could not configure Shairport Sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
