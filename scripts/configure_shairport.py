#!/usr/bin/env python3
"""Idempotently configure Shairport Sync's AirPlay bridge and diagnostics."""

from __future__ import annotations

import os
import base64
import re
import shutil
import sys
import tempfile
from pathlib import Path


MARKER_PREFIX = "CDSP"
# Releases before the installer was made site-neutral prefixed these markers
# with one deployment's name.  It is assembled from fragments so the literal
# never appears in this repository.  Recognition stays exact: a marker block
# this tool did not write is never claimed, and every write uses MARKER_PREFIX,
# so an existing config migrates the first time it is configured.
_LEGACY_MARKER_PREFIX = "UG" "LAN"


def _marker(tag: str) -> str:
    return f"// {MARKER_PREFIX}-{tag}"


def _legacy_form(marker: str) -> str:
    return marker.replace(f"// {MARKER_PREFIX}-", f"// {_LEGACY_MARKER_PREFIX}-", 1)


def _marker_forms(marker: str) -> tuple[str, str]:
    return marker, _legacy_form(marker)


def _block_pattern(begin: str, end: str) -> re.Pattern[str]:
    begins = "|".join(re.escape(form) for form in _marker_forms(begin))
    ends = "|".join(re.escape(form) for form in _marker_forms(end))
    return re.compile(rf"\n?(?:{begins})\n.*?(?:{ends})\n?", re.DOTALL)


GENERAL_KEYS = ("ignore_volume_control", "run_this_when_volume_is_set")
GENERAL_BEGIN = _marker("AIRPLAY-BEGIN")
GENERAL_END = _marker("AIRPLAY-END")
SESSION_KEYS = (
    "run_this_before_play_begins",
    "run_this_after_play_ends",
    "wait_for_completion",
)
SESSION_BEGIN = _marker("AIRPLAY-SESSION-BEGIN")
SESSION_END = _marker("AIRPLAY-SESSION-END")
SESSION_BLOCK_BEGIN = _marker("AIRPLAY-SESSION-BLOCK-BEGIN")
SESSION_BLOCK_END = _marker("AIRPLAY-SESSION-BLOCK-END")
DSP_KEYS = ("loudness", "loudness_reference_volume_db")
DSP_BEGIN = _marker("LOUDNESS-BEGIN")
DSP_END = _marker("LOUDNESS-END")
ALSA_KEYS = ("output_device",)
ALSA_BEGIN = _marker("OUTPUT-BEGIN")
ALSA_END = _marker("OUTPUT-END")
DIAGNOSTICS_KEYS = ("statistics", "log_verbosity")
DIAGNOSTICS_BEGIN = _marker("DIAGNOSTICS-BEGIN")
DIAGNOSTICS_END = _marker("DIAGNOSTICS-END")
DIAGNOSTICS_BLOCK_BEGIN = _marker("DIAGNOSTICS-BLOCK-BEGIN")
DIAGNOSTICS_BLOCK_END = _marker("DIAGNOSTICS-BLOCK-END")


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
    begin_forms = _marker_forms(begin)
    end_forms = _marker_forms(end_marker)
    original_values: list[str] = []
    body: list[str] = []
    in_managed = False
    encoded_original = None
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped in begin_forms:
            in_managed = True
            continue
        if stripped in end_forms:
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

    # Both spellings, so a block this tool created under the previous marker
    # name is replaced instead of joined by a second one that Shairport's own
    # config parser would then reject.
    updated = _block_pattern(SESSION_BLOCK_BEGIN, SESSION_BLOCK_END).sub("\n", updated)
    updated = _block_pattern(DIAGNOSTICS_BLOCK_BEGIN, DIAGNOSTICS_BLOCK_END).sub(
        "\n", updated
    )

    diagnostics_settings = None
    if callback is not None:
        diagnostics_settings = [
            '    statistics = "no";\n',
            "    log_verbosity = 1;\n",
        ]
    try:
        updated = _update_block(
            updated,
            "diagnostics",
            DIAGNOSTICS_KEYS,
            DIAGNOSTICS_BEGIN,
            DIAGNOSTICS_END,
            diagnostics_settings,
        )
    except ValueError as exc:
        if str(exc) != "active diagnostics block not found":
            raise
        if diagnostics_settings is not None:
            separator = "" if not updated or updated.endswith("\n") else "\n"
            updated = (
                updated
                + separator
                + f"{DIAGNOSTICS_BLOCK_BEGIN}\n"
                + "diagnostics =\n{\n"
                + f"    {DIAGNOSTICS_BEGIN}\n"
                + "".join(diagnostics_settings)
                + f"    {DIAGNOSTICS_END}\n"
                + "};\n"
                + f"{DIAGNOSTICS_BLOCK_END}\n"
            )

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
            # Strip exactly what install writes. Naming an output device here
            # would demand an alsa block that installation never creates, so
            # removal failed outright on stock configs that have none.
            configure(path, None, None)
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
