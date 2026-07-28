#!/usr/bin/env python3
"""Small local web UI for the installation controller services."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import fcntl
import yaml

from audio_eq import (
    atomic_write_json,
    audio_state_lock,
    default_audio_state,
    normalize_audio_state,
    read_audio_state,
)
from speaker_config import (
    PROFILE_VERSION,
    compile_profile_config,
    config_digest,
    load_profile,
    load_yaml_mapping,
    profile_catalog,
    save_profile,
)
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    DEFAULT_SPEAKER_ID,
    OPERATOR_CONFIG_SPEAKERS,
    audio_control_lock,
    normalize_speaker_id,
    operator_configs_for_speaker,
    read_profile_audio_state,
    read_speaker_selection,
    require_audio_unmute_allowed,
    resolve_profile_audio_path,
    set_audio_inhibit,
    update_speaker_selection,
)
from speaker_xo import crossover_response, normalize_crossover


HOST = os.environ.get("INSTALLATION_UI_HOST", "0.0.0.0")
PORT = int(os.environ.get("INSTALLATION_UI_PORT", "8088"))
CDSP_ENV = Path(
    os.environ.get("CDSP_AUTOMATION_ENV", "/home/magnus/camilladsp/cdsp-automation.env")
)
CDSP_CONFIG_DIR = Path(
    os.environ.get("CDSP_CONFIG_DIR", "/home/magnus/camilladsp/configs")
)
SPEAKER_GENERATED_DIR = Path(
    os.environ.get(
        "SPEAKER_GENERATED_DIR", "/var/lib/cdsp-automation/generated-configs"
    )
)
SOURCE_OVERRIDE_PATH = Path(
    os.environ.get("SOURCE_OVERRIDE_PATH", "/run/cdsp-source-switcher/manual_source")
)
SOURCE_OVERRIDE_OWNER_PATH = Path(
    os.environ.get("SOURCE_OVERRIDE_OWNER_PATH", f"{SOURCE_OVERRIDE_PATH}.owner")
)
SOURCE_OVERRIDE_LOCK_PATH = Path(
    os.environ.get("SOURCE_OVERRIDE_LOCK_PATH", f"{SOURCE_OVERRIDE_PATH}.lock")
)
UI_SERVICE = "cdsp-control-ui.service"
AUDIO_EQ_PATH = Path(
    os.environ.get("AUDIO_EQ_PATH", "/var/lib/cdsp-automation/audio-eq.json")
)
AUDIO_EQ_STATUS_PATH = Path(
    os.environ.get(
        "AUDIO_EQ_STATUS_PATH", "/run/cdsp-source-switcher/audio-eq-status.json"
    )
)
AUDIO_EQ_BACKUP_DIR = Path(
    os.environ.get("AUDIO_EQ_BACKUP_DIR", "/var/lib/installation/audio-eq-backups")
)
AUDIO_CONTROL_LOCK_PATH = Path(
    os.environ.get(
        "AUDIO_CONTROL_LOCK_PATH", "/var/lib/cdsp-automation/audio-control.lock"
    )
)
AUDIO_READY_PATH = Path(
    os.environ.get(
        "AUDIO_READY_PATH", "/run/cdsp-source-switcher/audio-ready.json"
    )
)
SPEAKER_SELECTION_PATH = Path(
    os.environ.get(
        "SPEAKER_SELECTION_PATH", "/var/lib/cdsp-automation/speaker-selection.json"
    )
)
SPEAKER_AUDIO_DIR = Path(
    os.environ.get("SPEAKER_AUDIO_DIR", "/var/lib/cdsp-automation/speaker-audio")
)
SPEAKER_PROFILE_DIR = Path(
    os.environ.get("SPEAKER_PROFILE_DIR", "/etc/cdsp-automation/speaker-profiles")
)
SOURCE_BASE_DIR = Path(
    os.environ.get("SOURCE_BASE_DIR", "/etc/cdsp-automation/source-bases")
)
SPEAKER_STATUS_PATH = Path(
    os.environ.get(
        "SPEAKER_STATUS_PATH",
        "/run/cdsp-source-switcher/speaker-profile-status.json",
    )
)
SPEAKER_TRANSITION_PATH = Path(
    os.environ.get(
        "SPEAKER_TRANSITION_PATH",
        "/var/lib/cdsp-automation/speaker-transition.json",
    )
)
BACKUP_KEEP = 15
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/mnt/whispers"))
AUDIO_EXTS = {
    ".wav",
    ".flac",
    ".mp3",
    ".aif",
    ".aiff",
    ".m4a",
    ".ogg",
    ".opus",
    ".wv",
    ".aac",
}
MIN_VALID_EPOCH = 1_704_067_200  # 2024-01-01
MAX_VALID_EPOCH = 4_102_444_800  # 2100-01-01
DEFAULT_REMOTE_NAME = "HID Remote01 Keyboard"
CAMILLA_HOST = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CAMILLA_BINARY = os.environ.get("CAMILLA_BINARY", "camilladsp")
AIRPLAY_VOLUME_STATUS_PATH = Path(
    os.environ.get(
        "AIRPLAY_VOLUME_STATUS_PATH", "/run/airplay-volume-bridge/status.json"
    )
)
SHAIRPORT_CONFIG_PATH = Path(
    os.environ.get("SHAIRPORT_CONFIG_PATH", "/etc/shairport-sync.conf")
)
SPOTIFY_VOLUME_DROPIN_PATH = Path(
    os.environ.get(
        "SPOTIFY_VOLUME_DROPIN_PATH",
        "/etc/systemd/system/raspotify.service.d/uglan-volume-sync.conf",
    )
)
ISO226_CAPABILITY_PATH = Path(
    os.environ.get(
        "ISO226_CAPABILITY_PATH", "/var/lib/cdsp-automation/iso226-engine.json"
    )
)
VOLUME_MIN_DB = -80.0
VOLUME_MAX_DB = 0.0

SOURCE_CHOICES = {
    "streamer": "Streamer",
    "gadget": "USB Gadget",
    "toslink": "TOSLINK",
    "analog": "Analog",
}

SERVICE_CATALOG: dict[str, dict[str, Any]] = {
    UI_SERVICE: {
        "label": "Control UI",
        "group": "Control",
        "required": True,
        "controls": ["restart"],
    },
    "camilladsp.service": {
        "label": "CamillaDSP",
        "group": "Audio",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "camillagui.service": {
        "label": "CamillaDSP GUI",
        "group": "Audio",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "cdsp-source-switcher.service": {
        "label": "Source switcher",
        "group": "Automation",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "cdsp-trigger.service": {
        "label": "Amp trigger",
        "group": "Automation",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "cdsp-motu-sync.service": {
        "label": "MOTU clock sync",
        "group": "Automation",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "cdsp-remote.service": {
        "label": "HID remote",
        "group": "Automation",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
    "lyrionmusicserver.service": {
        "label": "Lyrion server",
        "group": "Playback",
        "required": False,
        "controls": ["start", "restart", "stop"],
    },
    "squeezelite-main.service": {
        "label": "Main six-channel player",
        "group": "Playback",
        "required": True,
        "controls": ["start", "restart", "stop"],
    },
}


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UGLAN — audio control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0c0e;
      --bg2: #0f1114;
      --panel: #14161a;
      --panel2: #101216;
      --raised: #191c21;
      --line: #24272e;
      --line2: #2e323a;
      --ink: #eceae4;
      --muted: #8a8d95;
      --faint: #5f636b;
      --cool: #4d8dff;
      --warm: #ff9a45;
      --ok: #5fd08a;
      --warn: #f4c451;
      --bad: #ff6b6b;
      --accent-grad: linear-gradient(90deg, var(--cool), var(--warm));
      --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", "Menlo", "Consolas", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      --radius: 12px;
    }
    * { box-sizing: border-box; }
    html, body { overflow-x: hidden; }
    body {
      margin: 0;
      background:
        radial-gradient(1200px 600px at 78% -8%, rgba(255,154,69,0.06), transparent 60%),
        radial-gradient(1100px 620px at 10% -12%, rgba(77,141,255,0.07), transparent 55%),
        var(--bg);
      color: var(--ink);
      font: 14px/1.5 var(--sans);
      -webkit-font-smoothing: antialiased;
      letter-spacing: 0.1px;
    }
    /* subtle film grain */
    .grain {
      position: fixed; inset: 0; z-index: 0; pointer-events: none; opacity: 0.05;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
      mix-blend-mode: overlay;
    }
    .wrap { position: relative; z-index: 1; }

    /* ---------- header ---------- */
    header {
      position: sticky; top: 0; z-index: 20;
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: 14px clamp(14px, 4vw, 28px);
      background: rgba(11,12,14,0.78);
      backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--line);
    }
    .brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .eq { display: flex; align-items: flex-end; gap: 2.5px; height: 26px; width: 34px; flex: none; }
    .eq i {
      display: block; width: 4px; height: 20%;
      background: var(--accent-grad); border-radius: 2px;
      transition: height 90ms linear; opacity: 0.9;
    }
    .word { min-width: 0; }
    .word h1 {
      margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 3px;
      line-height: 1; white-space: nowrap;
    }
    .word h1 b {
      background: var(--accent-grad); -webkit-background-clip: text; background-clip: text;
      -webkit-text-fill-color: transparent; font-weight: 800;
    }
    .word .sub {
      font: 10px/1.2 var(--mono); letter-spacing: 2px; text-transform: uppercase;
      color: var(--faint); margin-top: 5px; white-space: nowrap;
    }
    .health { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .chip {
      display: inline-flex; align-items: center; gap: 7px;
      font: 11px/1 var(--mono); letter-spacing: 0.4px;
      padding: 7px 10px; border: 1px solid var(--line); border-radius: 999px;
      color: var(--muted); background: var(--panel2); white-space: nowrap;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); flex: none; }
    .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px rgba(95,208,138,0.15); }
    .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px rgba(244,196,81,0.15); }
    .dot.bad { background: var(--bad); box-shadow: 0 0 0 3px rgba(255,107,107,0.16); }
    .dot.live { animation: pulse 1.6s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

    /* ---------- nav ---------- */
    nav {
      position: sticky; top: 61px; z-index: 15;
      display: flex; gap: 4px; padding: 8px clamp(14px, 4vw, 28px);
      background: rgba(11,12,14,0.7); backdrop-filter: blur(10px);
      border-bottom: 1px solid var(--line); overflow-x: auto; scrollbar-width: none;
    }
    nav::-webkit-scrollbar { display: none; }
    nav button {
      appearance: none; border: 1px solid transparent; background: transparent;
      color: var(--muted); cursor: pointer; white-space: nowrap;
      padding: 8px 14px; border-radius: 8px;
      font: 12px/1 var(--mono); letter-spacing: 1.2px; text-transform: uppercase;
    }
    nav button:hover { color: var(--ink); }
    nav button.active { color: var(--ink); background: var(--raised); border-color: var(--line2); }

    main { max-width: 1180px; margin: 0 auto; padding: clamp(16px, 3vw, 30px) clamp(14px, 4vw, 28px) 60px; }
    section { display: none; }
    section.active { display: block; animation: fade 0.25s ease; }
    @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

    .shead {
      display: flex; align-items: baseline; gap: 10px; margin: 26px 0 12px;
    }
    .shead:first-child { margin-top: 4px; }
    .shead .ix { font: 11px/1 var(--mono); color: var(--warm); letter-spacing: 1px; }
    .shead h2 {
      margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 2px; text-transform: uppercase;
      color: var(--muted);
    }
    .shead .rule { flex: 1; height: 1px; background: linear-gradient(90deg, var(--line2), transparent); }

    .card {
      background: linear-gradient(180deg, var(--panel), var(--panel2));
      border: 1px solid var(--line); border-radius: var(--radius);
      padding: 16px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.02);
    }
    .grid { display: grid; gap: 12px; }
    .g-auto { grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); }
    .hero { grid-template-columns: 1.4fr 1fr 1fr; }
    .split { grid-template-columns: 1fr 1fr; align-items: start; }

    .tile { background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; padding: 13px 14px; min-width: 0; }
    .cap { font: 10px/1.2 var(--mono); letter-spacing: 1.4px; text-transform: uppercase; color: var(--faint); margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
    .val { font-size: 19px; font-weight: 650; overflow-wrap: anywhere; }
    .val.sm { font-size: 15px; font-weight: 600; }
    .val.big { font-size: 30px; font-weight: 700; font-family: var(--mono); letter-spacing: -0.5px; }
    .unit { font-size: 12px; color: var(--muted); font-weight: 500; }
    .sub2 { font: 11px/1.3 var(--mono); color: var(--faint); margin-top: 6px; overflow-wrap: anywhere; }
    .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); } .cool { color: var(--cool); }

    .badge {
      display: inline-flex; align-items: center; gap: 6px; font: 11px/1 var(--mono);
      letter-spacing: 0.6px; padding: 5px 9px; border-radius: 999px;
      border: 1px solid var(--line2); color: var(--muted); text-transform: uppercase;
    }
    .badge.ok { color: var(--ok); border-color: rgba(95,208,138,0.4); }
    .badge.warn { color: var(--warn); border-color: rgba(244,196,81,0.4); }
    .badge.bad { color: var(--bad); border-color: rgba(255,107,107,0.4); }

    /* signal meter */
    .meter { height: 12px; border-radius: 7px; background: #0a0b0d; border: 1px solid var(--line); overflow: hidden; position: relative; margin-top: 4px; }
    .meter > i { display: block; height: 100%; width: 0%; background: var(--accent-grad); border-radius: 7px 0 0 7px; transition: width 260ms cubic-bezier(.2,.8,.2,1); }
    .meter.silent > i { opacity: 0.25; }

    /* buttons */
    .row { display: flex; gap: 8px; flex-wrap: wrap; }
    button.btn, .btn {
      appearance: none; cursor: pointer; font: 12px/1 var(--sans); font-weight: 600;
      padding: 10px 13px; border-radius: 9px; border: 1px solid var(--line2);
      color: var(--ink); background: var(--raised); letter-spacing: 0.3px;
      transition: border-color 0.15s, background 0.15s, transform 0.05s;
    }
    .btn:hover { border-color: var(--faint); }
    .btn:active { transform: translateY(1px); }
    .btn.primary { border-color: transparent; background: var(--accent-grad); color: #0a0b0d; font-weight: 700; }
    .btn.on { border-color: var(--cool); color: var(--ink); background: rgba(77,141,255,0.14); box-shadow: inset 0 0 0 1px rgba(77,141,255,0.35); }
    .btn.danger { background: #2a1618; border-color: #5c2a2c; color: #ffb4b4; }
    .btn.ghost { background: transparent; }
    .btn.sm { padding: 7px 10px; font-size: 11px; }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    a.btn { text-decoration: none; display: inline-flex; align-items: center; justify-content: center; }

    /* source grid */
    .sources { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
    .src { text-align: left; position: relative; padding: 12px; }
    .src .k { font: 10px/1 var(--mono); letter-spacing: 1px; text-transform: uppercase; color: var(--faint); }
    .src .n { font-size: 14px; font-weight: 650; margin-top: 6px; }
    .src.on { border-color: var(--cool); background: rgba(77,141,255,0.12); }
    .src.on .k { color: var(--cool); }

    /* range / inputs */
    input[type=range] {
      -webkit-appearance: none; appearance: none; width: 100%; height: 4px; border-radius: 4px;
      background: var(--line2); outline: none;
    }
    input[type=range]::-webkit-slider-thumb {
      -webkit-appearance: none; appearance: none; width: 17px; height: 17px; border-radius: 50%;
      background: var(--ink); border: 3px solid var(--cool); cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,0.5);
    }
    input[type=range]::-moz-range-thumb { width: 15px; height: 15px; border-radius: 50%; background: var(--ink); border: 3px solid var(--cool); cursor: pointer; }
    input[type=number], input[type=text], select {
      width: 100%; padding: 8px 9px; border: 1px solid var(--line2); border-radius: 8px;
      background: #0b0c0e; color: var(--ink); font: 13px/1 var(--mono);
    }
    input:focus, select:focus, button:focus-visible { outline: 2px solid rgba(77,141,255,0.5); outline-offset: 1px; }
    select { cursor: pointer; }

    .volume { display: grid; grid-template-columns: 1fr auto; gap: 12px 16px; align-items: center; }
    .volume .num { width: 92px; }
    .audio-layout { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(280px, .8fr); gap: 14px; }
    .eq-plot { width: 100%; min-height: 380px; display: block; touch-action: none; user-select: none; background:#090b0e; border:1px solid var(--line); border-radius:10px; }
    .eq-grid-line { stroke: #252a31; stroke-width: 1; }
    .eq-zero { stroke: #59606b; stroke-width: 1.4; }
    .eq-curve { fill: none; stroke: var(--warm); stroke-width: 3; filter: drop-shadow(0 0 5px rgba(255,154,69,.28)); }
    .eq-band-curve { fill:none; stroke-width:1.3; opacity:.42; }
    .eq-band-curve.selected { stroke-width:2.2; opacity:.9; }
    .eq-band.selected { border-color:rgba(77,141,255,.7); box-shadow:inset 0 0 0 1px rgba(77,141,255,.18); }
    .eq-fill { fill: url(#eqFill); opacity: .2; }
    .eq-handle { stroke: #0b0c0e; stroke-width: 3; cursor: grab; }
    .eq-handle:active { cursor: grabbing; }
    .eq-axis { fill: var(--muted); font: 12px var(--mono); }
    .eq-band-list { display: grid; gap: 8px; margin-top: 12px; }
    .eq-band { display: grid; grid-template-columns: auto minmax(105px,1fr) repeat(3,minmax(74px,.65fr)) auto; gap: 8px; align-items: end; padding: 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel2); }
    .eq-band label { color: var(--muted); font-size: 11px; letter-spacing: .05em; text-transform: uppercase; }
    .eq-band label input, .eq-band label select { margin-top: 4px; width: 100%; }
    .eq-power { align-self: center; }
    .audio-kv { display: grid; gap: 10px; }
    .audio-state { font: 12px var(--mono); }
    .speaker-summary { display: grid; gap: 10px; }
    .speaker-summary .active-name { font-size: 20px; font-weight: 720; }
    .speaker-cell { display: grid; gap: 6px; }
    .speaker-cell > .btn { width: 100%; }
    .audio-notice { display: none; margin-bottom: 12px; border-color: rgba(255,107,107,.45); }
    .audio-notice.show { display: flex; align-items: center; gap: 14px; }
    dialog.confirm-dialog {
      width: min(520px, calc(100vw - 28px)); padding: 0; color: var(--ink);
      border: 1px solid var(--line2); border-radius: 14px; background: var(--panel);
      box-shadow: 0 24px 80px rgba(0,0,0,.7);
    }
    dialog.confirm-dialog::backdrop { background: rgba(3,5,8,.82); backdrop-filter: blur(4px); }
    .confirm-body { padding: 20px; }
    .confirm-warning { margin: 14px 0; padding: 12px; border: 1px solid #704326; border-radius: 9px; background: #25180f; color: #ffd2a8; line-height: 1.5; }

    /* toggle switch */
    .switch { position: relative; display: inline-block; width: 42px; height: 24px; flex: none; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .switch span { position: absolute; inset: 0; background: var(--line2); border-radius: 999px; transition: 0.2s; }
    .switch span::before { content: ""; position: absolute; height: 18px; width: 18px; left: 3px; top: 3px; background: var(--ink); border-radius: 50%; transition: 0.2s; }
    .switch input:checked + span { background: var(--cool); }
    .switch input:checked + span::before { transform: translateX(18px); }

    /* action bar */
    .actionbar {
      position: sticky; bottom: 0; margin-top: 14px; padding: 12px; z-index: 5;
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
      background: rgba(16,18,22,0.92); backdrop-filter: blur(8px);
      border: 1px solid var(--line); border-radius: 12px;
    }
    .actionbar .spacer { flex: 1; }
    .pending { font: 11px/1 var(--mono); color: var(--warm); letter-spacing: 0.5px; }

    /* services */
    table { width: 100%; border-collapse: collapse; }
    .svc-group { margin-bottom: 18px; }
    .svc-row { display: grid; grid-template-columns: minmax(0,1.4fr) auto auto; gap: 12px; align-items: center; padding: 11px 0; border-top: 1px solid var(--line); }
    .svc-row:first-child { border-top: 0; }
    .svc-name { min-width: 0; }
    .svc-name .l { font-weight: 600; }
    .svc-name .u { font: 10px/1.3 var(--mono); color: var(--faint); overflow-wrap: anywhere; }

    /* logs */
    pre {
      white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.55 var(--mono);
      background: #08090b; border: 1px solid var(--line); border-radius: 10px; padding: 14px;
      max-height: 60vh; overflow: auto; color: #c7cbd1;
    }

    /* toast */
    #toast {
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%) translateY(20px);
      background: #2a1618; border: 1px solid #5c2a2c; color: #ffd2d2; padding: 10px 16px; border-radius: 10px;
      font: 12px/1.3 var(--mono); z-index: 50; opacity: 0; transition: 0.25s; pointer-events: none; max-width: 90vw;
    }
    #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

    @media (max-width: 860px) {
      .hero, .split { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .word .sub { display: none; }
      .word h1 { font-size: 17px; letter-spacing: 2px; }
      .val.big { font-size: 26px; }
      .volume { grid-template-columns: 1fr; }
      .volume .num { width: 100%; }
      .audio-layout { grid-template-columns: 1fr; }
      .eq-band { grid-template-columns: auto 1fr 1fr; }
      .eq-band .eq-type { grid-column: span 2; }
    }
  </style>
</head>
<body>
  <div class="grain"></div>
  <div class="wrap">
    <header>
      <div class="brand">
        <div class="eq" id="eq" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <div class="word">
          <h1><b>UGLAN</b></h1>
          <div class="sub">sound · control</div>
        </div>
      </div>
      <div class="health" id="health"></div>
    </header>

    <nav id="nav">
      <button class="active" data-tab="dashboard">Dashboard</button>
      <button data-tab="audio">Audio</button>
      <button data-tab="services">Services</button>
      <button data-tab="logs">Logs</button>
    </nav>

    <main>
      <section id="dashboard" class="active">
        <div class="shead"><span class="ix">01</span><h2>Now</h2><span class="rule"></span></div>
        <div class="grid hero" id="heroGrid"></div>

        <div class="shead"><span class="ix">02</span><h2>Controls</h2><span class="rule"></span></div>
        <div class="grid g-auto">
          <div class="card">
            <div class="cap">Source <span id="sourceMode" class="badge">—</span></div>
            <div class="sources" id="sourcePanel"></div>
            <div class="sub2" id="sourceCaption"></div>
          </div>
          <div class="card">
            <div class="cap">CamillaDSP volume</div>
            <div id="volumePanel"></div>
          </div>
          <div class="card">
            <div class="cap">Speaker profile</div>
            <div id="dashboardSpeaker" class="speaker-summary"></div>
          </div>
          <div class="card">
            <div class="cap">Amplifier power</div>
            <button class="btn danger" id="ampOff">Turn amps off now</button>
            <div class="sub2">Turns off the trigger immediately. Automatic triggering stays enabled and will turn the amps on again with the next audio session.</div>
          </div>
        </div>

        <div class="shead"><span class="ix">03</span><h2>System</h2><span class="rule"></span></div>
        <div class="grid g-auto" id="systemGrid"></div>
        <div class="grid split" style="margin-top:12px">
          <div class="card" id="clockCard"></div>
          <div class="card" id="storageCard"></div>
        </div>
      </section>

      <section id="audio">
        <div class="shead"><span class="ix">00</span><h2>Speaker profile</h2><span class="rule"></span></div>
        <div class="card audio-notice" id="audioNotice"><div style="flex:1"><div class="val sm bad">Audio controls unavailable</div><div class="sub2" id="audioNoticeText"></div></div><button class="btn" id="audioRetry">Retry</button></div>
        <div class="card" id="speakerProfiles" style="margin-bottom:12px"></div>
        <div class="shead"><span class="ix">01</span><h2>Parametric EQ</h2><span class="rule"></span></div>
        <div class="audio-layout">
          <div class="card">
            <div class="cap">User EQ response</div>
            <svg id="eqPlot" class="eq-plot" viewBox="0 0 900 360" role="img" aria-label="User equalizer frequency response"></svg>
            <div id="eqBands" class="eq-band-list"></div>
            <div class="actionbar">
              <button class="btn" id="eqAdd" disabled>+ Add band</button>
              <button class="btn ghost" id="eqFlat" disabled>Reset gains</button>
              <label class="btn sm ghost"><input id="eqShowBands" type="checkbox" checked disabled> Individual curves</label>
              <span class="spacer"></span>
              <span class="pending audio-state" id="eqApplyState">—</span>
              <button class="btn primary" id="eqSave" disabled>Save now</button>
            </div>
          </div>
          <div class="audio-kv">
            <div class="card" id="eqGlobal"></div>
            <div class="card" id="volumeArchitecture"></div>
            <div class="card" id="loudnessPlan"></div>
          </div>
        </div>
      </section>

      <section id="services">
        <div class="shead"><span class="ix">01</span><h2>Systemd services</h2><span class="rule"></span></div>
        <div id="servicesTable"></div>
      </section>

      <section id="logs">
        <div class="shead"><span class="ix">01</span><h2>Journal</h2><span class="rule"></span></div>
        <div class="row" style="margin-bottom:12px">
          <select id="logUnit" style="max-width:280px"></select>
          <button class="btn" id="refreshLogs">Refresh</button>
        </div>
        <pre id="logBox">—</pre>
      </section>
    </main>
    <dialog id="speakerConfirm" class="confirm-dialog">
      <form method="dialog" class="confirm-body">
        <div class="cap">Confirm speaker profile change</div>
        <div class="val sm" id="speakerConfirmRoute" style="margin-top:9px"></div>
        <div class="confirm-warning">This mutes CamillaDSP and replaces the complete DSP profile, routing, EQ and output protection as one transaction. Confirm the passive speakers are physically connected for the target profile.</div>
        <label class="sub2">Type <b>SWITCH</b> to enable the change<input id="speakerConfirmText" type="text" autocomplete="off" style="margin-top:6px"></label>
        <div class="row" style="margin-top:16px"><button class="btn ghost" id="speakerConfirmCancel" type="button">Cancel</button><span class="spacer"></span><button class="btn danger" id="speakerConfirmApply" type="button" disabled>Mute and switch profile</button></div>
      </form>
    </dialog>
  </div>
  <div id="toast"></div>

  <script>
    let signalTarget = 0;
    let audioState = null;
    let audioLoaded = false;
    let audioSaveTimer = null;
    let audioSaving = false;
    let audioDirty = false;
    let audioEditGeneration = 0;
    let audioBridge = {};
    let audioCapability = {};
    let speakerState = null;
    let pendingSpeakerId = null;
    let draggedBand = null;
    let selectedEqBand = null;
    let eqSoloBand = null;
    let eqShowBandCurves = true;
    let eqSampleRate = 48000;

    const qs = s => document.querySelector(s);
    const qsa = s => Array.from(document.querySelectorAll(s));
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
    const CLOCK_LOCALE = "en-GB";
    const CLOCK_TIME = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
    const fmtClock = d => d.toLocaleTimeString(CLOCK_LOCALE, CLOCK_TIME);

    function toast(msg) {
      const t = qs("#toast"); t.textContent = msg; t.classList.add("show");
      clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 4200);
    }
    function statusClass(v) {
      if (v === true || v === "active" || v === "ok" || v === "installed" || v === "running") return "ok";
      if (v === false || v === "failed" || v === "inactive" || v === "dead" || v === "not-found" || v === "not found") return "bad";
      return "warn";
    }
    function serviceText(s) {
      if (!s) return "unknown";
      if (s.load === "not-found") return "not found";
      return s.active || "unknown";
    }
    async function api(path, options = {}) {
      const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (!res.ok) throw new Error(data.error || text || res.statusText);
      return data;
    }

    /* ---------------- rendering ---------------- */
    function tile(cap, val, cls = "", sub = "", valExtra = "") {
      return `<div class="tile"><div class="cap">${cap}</div>
        <div class="val ${cls}">${val}${valExtra}</div>${sub ? `<div class="sub2">${sub}</div>` : ""}</div>`;
    }

    function renderHealth(data) {
      const svcs = data.services || {};
      const required = Object.values(svcs).filter(s => s.required);
      const active = required.filter(s => s.active === "active").length;
      const allOk = active === required.length;
      const time = fmtClock(new Date());
      qs("#health").innerHTML =
        `<span class="chip"><span class="dot ${allOk ? "ok" : "bad"}"></span>${active}/${required.length} systems</span>` +
        `<span class="chip" title="local time">${time}</span>`;
    }

    function renderHero(data) {
      const c = data.camilla || {};
      const src = data.source || {};
      const state = c.error ? "error" : (c.state || serviceText(data.services["camilladsp.service"]));
      const stCls = c.error ? "bad" : (state === "RUNNING" ? "ok" : (state === "PAUSED" ? "warn" : "warn"));
      const vol = c.volume_db != null ? Number(c.volume_db).toFixed(1) : "—";
      const activeSrc = src.current ? (src.current[0].toUpperCase() + src.current.slice(1)) : (c.config_title || "—");
      qs("#heroGrid").innerHTML =
        `<div class="tile">
          <div class="cap">Input signal <span id="sigBadge" class="badge">—</span></div>
          <div class="meter silent" id="sigMeter"><i></i></div>
          <div class="sub2" id="sigText">—</div>
        </div>
        <div class="tile">
          <div class="cap">Output</div>
          <div class="val big">${vol}<span class="unit"> dB</span></div>
          <div class="sub2">${c.muted ? '<span class="warn">muted</span>' : "unmuted"} · CamillaDSP <span class="${stCls}">${esc(state)}</span></div>
        </div>
        <div class="tile">
          <div class="cap">Active source</div>
          <div class="val sm">${esc(activeSrc)}</div>
          <div class="sub2">${esc(c.config_title || "—")}</div>
        </div>`;
      updateSignal(c.signal_db);
    }

    function updateSignal(db) {
      const meter = qs("#sigMeter"); if (!meter) return;
      const lo = -65, hi = -8;
      const has = db != null && db > -200;
      const norm = has ? Math.max(0, Math.min(1, (db - lo) / (hi - lo))) : 0;
      signalTarget = norm;
      meter.querySelector("i").style.width = (norm * 100).toFixed(1) + "%";
      meter.classList.toggle("silent", !has || norm < 0.02);
      const badge = qs("#sigBadge");
      if (badge) { badge.textContent = has ? "signal" : "silent"; badge.className = "badge " + (has ? "ok" : ""); }
      const txt = qs("#sigText");
      if (txt) txt.innerHTML = has ? `${db.toFixed(1)} dBFS` : `<span class="faint">no input</span>`;
    }

    function renderSource(data) {
      const src = data.source || {};
      const available = src.available || {};
      const mode = src.mode || "auto";
      qs("#sourceMode").textContent = mode === "auto" ? "auto" : "manual";
      qs("#sourceMode").className = "badge " + (mode === "auto" ? "ok" : "warn");
      const btns = [`<button class="btn src ${mode === "auto" ? "on" : ""}" data-source-action="auto">
          <div class="k">mode</div><div class="n">Auto</div></button>`];
      for (const [key, item] of Object.entries(available)) {
        const on = mode === key;
        const active = src.current === key;
        btns.push(`<button class="btn src ${on ? "on" : ""}" data-source-action="${esc(key)}" ${item.exists ? "" : "disabled"}>
          <div class="k">${active ? "● live" : (item.exists ? "source" : "n/a")}</div>
          <div class="n">${esc(item.label)}</div></button>`);
      }
      qs("#sourcePanel").innerHTML = btns.join("");
      const cur = src.current ? (src.current[0].toUpperCase() + src.current.slice(1)) : "—";
      qs("#sourceCaption").innerHTML = `now playing: <b>${esc(cur)}</b> · ${mode === "auto" ? "priority auto-switch" : "manual override held"}`;
      qsa("[data-source-action]").forEach(b => b.addEventListener("click", sourceAction));
    }

    function renderVolume(data) {
      const c = data.camilla || {};
      const vol = c.volume_db != null ? Number(c.volume_db) : -40;
      const focus = document.activeElement;
      if (!qs("#volRange")) {
        qs("#volumePanel").innerHTML =
          `<div class="volume">
            <input id="volRange" data-volume type="range" min="-50" max="0" step="0.5" value="${vol}">
            <input id="volNum" class="num" data-volume type="number" min="-80" max="0" step="0.5" value="${vol.toFixed(1)}">
          </div>
          <div class="row" style="margin-top:14px">
            <button class="btn sm" data-volume-action="down">−1 dB</button>
            <button class="btn primary sm" data-volume-action="set">Set</button>
            <button class="btn sm" data-volume-action="up">+1 dB</button>
            <label class="btn sm" style="display:inline-flex;align-items:center;gap:9px;cursor:pointer">
              <span class="switch"><input id="muteToggle" type="checkbox" ${c.muted ? "checked" : ""}><span></span></span> Mute
            </label>
          </div>`;
        qsa("[data-volume]").forEach(i => i.addEventListener("input", e => {
          qsa("[data-volume]").forEach(o => { if (o !== e.target) o.value = e.target.value; });
        }));
        qsa("[data-volume-action]").forEach(b => b.addEventListener("click", volumeAction));
        qs("#muteToggle").addEventListener("change", e => setVolume({ muted: e.target.checked }));
      } else {
        if (focus !== qs("#volRange") && focus !== qs("#volNum")) {
          qs("#volRange").value = vol; qs("#volNum").value = vol.toFixed(1);
        }
        if (focus !== qs("#muteToggle")) qs("#muteToggle").checked = !!c.muted;
      }
    }

    /* ---------------- audio EQ ---------------- */
    const EQ_COLORS = ["#5aa0ff", "#ffd166", "#63df8c", "#d78cff", "#ff8d6b", "#68d8e8", "#f27fb1", "#a8e063"];
    const EQ_X0 = 55, EQ_X1 = 875, EQ_Y0 = 20, EQ_Y1 = 320;
    const EQ_TYPES = ["Peaking","Lowshelf","Highshelf","Lowpass","Highpass","Bandpass","Notch"];
    const EQ_GAIN_TYPES = new Set(["Peaking","Lowshelf","Highshelf"]);
    const eqX = freq => EQ_X0 + Math.log(Math.max(20, Math.min(20000, freq)) / 20) / Math.log(1000) * (EQ_X1 - EQ_X0);
    const eqFreq = x => 20 * Math.pow(1000, Math.max(0, Math.min(1, (x - EQ_X0) / (EQ_X1 - EQ_X0))));
    const eqY = gain => EQ_Y0 + (24 - Math.max(-24, Math.min(24, gain))) / 48 * (EQ_Y1 - EQ_Y0);
    const eqGain = y => 24 - Math.max(0, Math.min(1, (y - EQ_Y0) / (EQ_Y1 - EQ_Y0))) * 48;
    const eqSupportsGain = band => EQ_GAIN_TYPES.has(band.type);

    function bandResponseDb(freq, band) {
      if (!band.enabled) return 0;
      const sr = eqSampleRate, w0 = 2 * Math.PI * band.freq / sr, w = 2 * Math.PI * freq / sr;
      const alpha = Math.sin(w0) / (2 * band.q), A = Math.pow(10, band.gain / 40), c = Math.cos(w0), sa = Math.sqrt(A);
      let b0, b1, b2, a0, a1, a2;
      if (band.type === "Peaking") {
        b0=1+alpha*A; b1=-2*c; b2=1-alpha*A; a0=1+alpha/A; a1=-2*c; a2=1-alpha/A;
      } else if (band.type === "Lowshelf") {
        b0=A*((A+1)-(A-1)*c+2*sa*alpha); b1=2*A*((A-1)-(A+1)*c); b2=A*((A+1)-(A-1)*c-2*sa*alpha);
        a0=(A+1)+(A-1)*c+2*sa*alpha; a1=-2*((A-1)+(A+1)*c); a2=(A+1)+(A-1)*c-2*sa*alpha;
      } else if (band.type === "Highshelf") {
        b0=A*((A+1)+(A-1)*c+2*sa*alpha); b1=-2*A*((A-1)+(A+1)*c); b2=A*((A+1)+(A-1)*c-2*sa*alpha);
        a0=(A+1)-(A-1)*c+2*sa*alpha; a1=2*((A-1)-(A+1)*c); a2=(A+1)-(A-1)*c-2*sa*alpha;
      } else if (band.type === "Lowpass") {
        b0=(1-c)/2; b1=1-c; b2=(1-c)/2; a0=1+alpha; a1=-2*c; a2=1-alpha;
      } else if (band.type === "Highpass") {
        b0=(1+c)/2; b1=-(1+c); b2=(1+c)/2; a0=1+alpha; a1=-2*c; a2=1-alpha;
      } else if (band.type === "Bandpass") {
        b0=alpha; b1=0; b2=-alpha; a0=1+alpha; a1=-2*c; a2=1-alpha;
      } else if (band.type === "Notch") {
        b0=1; b1=-2*c; b2=1; a0=1+alpha; a1=-2*c; a2=1-alpha;
      } else {
        return 0;
      }
      const cr=Math.cos(w), si=Math.sin(w), c2=Math.cos(2*w), s2=Math.sin(2*w);
      const nr=b0+b1*cr+b2*c2, ni=-b1*si-b2*s2, dr=a0+a1*cr+a2*c2, di=-a1*si-a2*s2;
      return 10 * Math.log10((nr*nr+ni*ni) / Math.max(1e-18, dr*dr+di*di));
    }

    function combinedResponse(freq) {
      if (!audioState || !audioState.enabled) return 0;
      return audioState.bands.reduce((sum, band, index) => sum + (eqSoloBand==null||eqSoloBand===index?bandResponseDb(freq, band):0), 0);
    }

    function renderEqPlot() {
      const section = audioState;
      if (!section) return;
      const svg = qs("#eqPlot"), freqTicks = [20,50,100,200,500,1000,2000,5000,10000,20000];
      const gainTicks = [-24,-12,0,12,24];
      const points = [];
      for (let i=0; i<=220; i++) {
        const x = EQ_X0 + (EQ_X1-EQ_X0)*i/220, f = eqFreq(x), y = eqY(combinedResponse(f));
        points.push(`${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`);
      }
      const curve = points.join(" "), fill = `${curve} L${EQ_X1},${eqY(0)} L${EQ_X0},${eqY(0)} Z`;
      const bandCurves=eqShowBandCurves?section.bands.map((band,index)=>{ const p=[]; for(let i=0;i<=160;i++){const x=EQ_X0+(EQ_X1-EQ_X0)*i/160;p.push(`${i?"L":"M"}${x.toFixed(1)},${eqY(bandResponseDb(eqFreq(x),band)).toFixed(1)}`);} return `<path class="eq-band-curve ${selectedEqBand===index?"selected":""}" stroke="${EQ_COLORS[index%EQ_COLORS.length]}" d="${p.join(" ")}"/>`; }).join(""):"";
      let markers="";
      if(selectedEqBand!=null&&section.bands[selectedEqBand]){const b=section.bands[selectedEqBand],q=Math.max(.1,b.q),root=Math.sqrt(1+1/(4*q*q)),lo=b.freq*(root-1/(2*q)),hi=b.freq*(root+1/(2*q));markers=`<line x1="${eqX(lo)}" x2="${eqX(lo)}" y1="${EQ_Y0}" y2="${EQ_Y1}" stroke="${EQ_COLORS[selectedEqBand%EQ_COLORS.length]}" stroke-dasharray="4 5" opacity=".55"/><line x1="${eqX(hi)}" x2="${eqX(hi)}" y1="${EQ_Y0}" y2="${EQ_Y1}" stroke="${EQ_COLORS[selectedEqBand%EQ_COLORS.length]}" stroke-dasharray="4 5" opacity=".55"/>`;}
      svg.innerHTML = `<defs><linearGradient id="eqFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#ff9a45"/><stop offset="1" stop-color="#4d8dff"/></linearGradient></defs>
        ${freqTicks.map(f => `<line class="eq-grid-line" x1="${eqX(f)}" x2="${eqX(f)}" y1="${EQ_Y0}" y2="${EQ_Y1}"/><text class="eq-axis" x="${eqX(f)}" y="342" text-anchor="middle">${f>=1000 ? (f/1000)+"k" : f}</text>`).join("")}
        ${gainTicks.map(g => `<line class="${g===0 ? "eq-zero" : "eq-grid-line"}" x1="${EQ_X0}" x2="${EQ_X1}" y1="${eqY(g)}" y2="${eqY(g)}"/><text class="eq-axis" x="45" y="${eqY(g)+4}" text-anchor="end">${g>0?"+":""}${g}</text>`).join("")}
        ${bandCurves}${markers}
        <path class="eq-fill" d="${fill}"/><path class="eq-curve" d="${curve}"/>
        ${section.bands.map((b,i) => {const y=eqY(eqSupportsGain(b)?b.gain:0);return `<g opacity="${b.enabled && section.enabled && (eqSoloBand==null||eqSoloBand===i) ? 1 : .25}"><circle class="eq-handle" data-eq-handle="${i}" cx="${eqX(b.freq)}" cy="${y}" r="${selectedEqBand===i?14:12}" fill="${EQ_COLORS[i%EQ_COLORS.length]}"/><text x="${eqX(b.freq)}" y="${y+4}" text-anchor="middle" fill="#091014" font-size="11" font-weight="700">${i+1}</text></g>`;}).join("")}`;
      svg.onpointerdown = e => {
        const handle = e.target.closest && e.target.closest("[data-eq-handle]");
        if (!handle) return;
        draggedBand = Number(handle.dataset.eqHandle); selectedEqBand=draggedBand; renderEqBands(); svg.setPointerCapture(e.pointerId); e.preventDefault();
      };
      svg.onpointermove = e => {
        if (draggedBand == null) return;
        const rect = svg.getBoundingClientRect(), x=(e.clientX-rect.left)*900/rect.width, y=(e.clientY-rect.top)*360/rect.height;
        const band = audioState.bands[draggedBand];
        // Shift = fine adjustment: approach the pointer instead of jumping to it.
        const targetFreq = eqFreq(x);
        band.freq = e.shiftKey
          ? Math.round(Math.max(20, Math.min(20000, band.freq + (targetFreq - band.freq) * 0.15)) * 10) / 10
          : Math.round(targetFreq);
        if(eqSupportsGain(band)){
          const limit=["low","high"].includes(band.id)?12:24, targetGain=eqGain(y);
          const gain=e.shiftKey?band.gain+(targetGain-band.gain)*0.15:targetGain;
          band.gain=Math.max(-limit,Math.min(limit,Math.round(gain*10)/10));
        }
        renderEqPlot(); syncEqBandInputs(draggedBand); scheduleAudioSave();
      };
      svg.onpointerup = svg.onpointercancel = () => { draggedBand = null; };
      svg.ondblclick = e => { const handle=e.target.closest&&e.target.closest("[data-eq-handle]"); if(!handle)return; const b=audioState.bands[Number(handle.dataset.eqHandle)]; if(eqSupportsGain(b)){b.gain=0;renderAudioEditor();scheduleAudioSave();} };
      svg.onwheel = e => { const handle=e.target.closest&&e.target.closest("[data-eq-handle]"); if(!handle)return; e.preventDefault(); const i=Number(handle.dataset.eqHandle),b=audioState.bands[i],step=e.shiftKey?0.02:0.1; b.q=Math.max(.1,Math.min(20,Math.round((b.q+(e.deltaY<0?step:-step))*100)/100)); selectedEqBand=i;renderAudioEditor();scheduleAudioSave(); };
    }

    function syncEqBandInputs(index) {
      const band = audioState.bands[index];
      qsa(`[data-eq-index="${index}"]`).forEach(el => {
        const key=el.dataset.eqKey; if (document.activeElement !== el && key in band) el.value=band[key];
      });
    }

    function renderEqBands() {
      const section = audioState;
      qs("#eqBands").innerHTML = section.bands.map((b,i) => `<div class="eq-band ${selectedEqBand===i?"selected":""}" data-eq-row="${i}">
        <label class="eq-power" title="${b.enabled?"Disable":"Enable"} band ${i+1}"><span class="switch"><input aria-label="Band ${i+1} enabled" data-eq-index="${i}" data-eq-key="enabled" type="checkbox" ${b.enabled?"checked":""}><span></span></span></label>
        <label class="eq-type">Type<select data-eq-index="${i}" data-eq-key="type" ${["low","high"].includes(b.id)?"disabled":""}>${EQ_TYPES.map(t=>`<option ${t===b.type?"selected":""}>${t}</option>`).join("")}</select></label>
        <label>Frequency<input data-eq-index="${i}" data-eq-key="freq" type="number" min="20" max="20000" step="1" value="${b.freq}"></label>
        <label>Gain dB<input data-eq-index="${i}" data-eq-key="gain" type="number" min="${["low","high"].includes(b.id)?-12:-24}" max="${["low","high"].includes(b.id)?12:24}" step="0.1" value="${b.gain}" ${eqSupportsGain(b)?"":"disabled"}></label>
        <label>Q<input data-eq-index="${i}" data-eq-key="q" type="number" min="0.1" max="20" step="0.1" value="${b.q}"></label>
        <div class="row"><button class="btn sm ${eqSoloBand===i?"on":"ghost"}" data-eq-solo="${i}" title="Show only this band in the response plot; audio is unchanged">Plot solo</button><button class="btn sm ghost" data-eq-delete="${i}" ${section.bands.length<=1||["low","high"].includes(b.id)?"disabled":""}>×</button></div></div>`).join("");
      qsa("[data-eq-key]").forEach(el => el.addEventListener("input", e => {
        const bands=audioState.bands, i=Number(e.target.dataset.eqIndex), key=e.target.dataset.eqKey;
        bands[i][key] = e.target.type === "checkbox" ? e.target.checked : (key === "type" ? e.target.value : Number(e.target.value));
        if(key==="type"){if(!eqSupportsGain(bands[i]))bands[i].gain=0;renderEqBands();}
        renderEqPlot(); scheduleAudioSave();
      }));
      // Labels contain the visible switch and the number-field captions. If a
      // label click rebuilds the row, the browser never gets to perform its
      // default checkbox/focus action.
      qsa("[data-eq-row]").forEach(row=>row.addEventListener("click",e=>{if(e.target.closest("input,select,button,label"))return;selectedEqBand=Number(row.dataset.eqRow);renderAudioEditor();}));
      qsa("[data-eq-solo]").forEach(el=>el.addEventListener("click",e=>{const i=Number(e.currentTarget.dataset.eqSolo);eqSoloBand=eqSoloBand===i?null:i;selectedEqBand=i;renderAudioEditor();}));
      qsa("[data-eq-delete]").forEach(el => el.addEventListener("click", e => {
        const bands=audioState.bands, i=Number(e.currentTarget.dataset.eqDelete);bands.splice(i,1);if(eqSoloBand===i)eqSoloBand=null;else if(eqSoloBand>i)eqSoloBand--;if(selectedEqBand>=bands.length)selectedEqBand=bands.length-1;renderAudioEditor(); scheduleAudioSave();
      }));
    }

    function renderAudioEditor(status = {}) {
      if (!audioState) return;
      renderEqPlot(); renderEqBands();
      qs("#eqGlobal").innerHTML = `<div class="cap">Global EQ</div>
        <label class="row" style="margin-top:12px"><span class="switch"><input id="eqEnabled" type="checkbox" ${audioState.enabled?"checked":""}><span></span></span> Enabled</label>
        <label class="row" style="margin-top:12px"><span class="switch"><input id="eqHeadroom" type="checkbox" ${audioState.auto_headroom?"checked":""}><span></span></span> Automatic headroom</label>
        <label style="display:block;margin-top:14px" class="sub2">Manual preamp (dB)<input id="eqPreamp" type="number" min="-24" max="0" step="0.5" value="${audioState.preamp_db}" style="margin-top:5px"></label>
        <div class="sub2" style="margin-top:10px">Effective preamp: <b>${status.effective_preamp_db != null ? Number(status.effective_preamp_db).toFixed(1) : "pending"} dB</b>. Auto mode reserves the sum of positive boosts.</div>`;
      qs("#eqEnabled").addEventListener("change", e => { audioState.enabled=e.target.checked; renderEqPlot(); scheduleAudioSave(); });
      qs("#eqHeadroom").addEventListener("change", e => { audioState.auto_headroom=e.target.checked; scheduleAudioSave(); });
      qs("#eqPreamp").addEventListener("input", e => { audioState.preamp_db=Number(e.target.value); scheduleAudioSave(); });
      const spotifyBridge=audioBridge.spotify||{};
      const airplayReady=audioBridge.airplay_configured&&audioBridge.airplay_service_active===true&&audioBridge.live;
      const spotifyReady=audioBridge.spotify_configured&&audioBridge.live&&spotifyBridge.command_socket===true;
      const spotifyIdle=audioBridge.spotify_configured&&audioBridge.live&&spotifyBridge.receiver_socket===true&&!spotifyReady;
      const spotifyLabel=!audioBridge.spotify_configured?"Spotify sync not configured":spotifyReady?"Spotify ↔ master live":spotifyIdle?"Spotify ready · waiting for sender":"Spotify receiver unavailable";
      qs("#volumeArchitecture").innerHTML = `<div class="cap">Volume architecture</div><div class="val sm" style="margin-top:8px">One shared CamillaDSP master</div>
        <div class="sub2">Web UI, HID remote and network players change the same fader. MOTU trims and amplifier gains remain calibration stages.</div>
        <div class="row" style="margin-top:12px"><span class="badge ${airplayReady?"ok":"warn"}">${audioBridge.airplay_configured?(airplayReady?"AirPlay → master live":"AirPlay receiver unavailable"):"AirPlay sync not configured"}</span><span class="badge ${spotifyReady?"ok":"warn"}">${spotifyLabel}</span></div>
        <div class="sub2" style="margin-top:8px">Both receivers pass audio at unity. Spotify Connect mirrors changes in both directions once a sender connects; until then its receiver remains ready but volume sync is idle. AirPlay source changes control the master, but AirPlay cannot update the sender’s exact slider value when volume is changed here.</div>`;
      const l=audioState.loudness;
      qs("#loudnessPlan").innerHTML = `<div class="cap">ISO 226 loudness</div><div class="val sm" style="margin-top:8px">Fader-linked calibration</div>
        <label class="row" style="margin-top:12px"><span class="switch"><input data-loudness-check="enabled" type="checkbox" ${l.enabled?"checked":""} ${audioCapability.available?"":"disabled"}><span></span></span> Enabled</label>
        <div class="sub2" style="margin-top:8px">At the reference master setting, measure SPL at the listening position and enter that value as phon. Compensation then follows the Main fader inside CamillaDSP.</div>
        <label class="sub2" style="display:block;margin-top:12px">Reference listening level (phon)<input data-loudness="reference_phon" type="number" min="40" max="100" step="1" value="${l.reference_phon}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Reference master volume (dB)<input data-loudness="reference_volume_db" type="number" min="-60" max="0" step="0.5" value="${l.reference_volume_db}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Strength<input data-loudness="strength" type="range" min="0" max="1" step="0.05" value="${l.strength}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Maximum bass boost (dB)<input data-loudness="max_bass_boost_db" type="number" min="0" max="18" step="0.5" value="${l.max_bass_boost_db}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Maximum treble boost (dB)<input data-loudness="max_treble_boost_db" type="number" min="0" max="12" step="0.5" value="${l.max_treble_boost_db}" style="margin-top:4px"></label>
        <div class="row" style="margin-top:12px"><span class="badge ${l.enabled?"ok":audioCapability.available?"":"warn"}">${l.enabled?"Custom DSP engine active":audioCapability.available?"Ready when enabled":"Install custom DSP engine first"}</span></div>`;
      qsa("[data-loudness]").forEach(el => el.addEventListener("input", e => { audioState.loudness[e.target.dataset.loudness]=Number(e.target.value); scheduleAudioSave(); }));
      qsa("[data-loudness-check]").forEach(el => el.addEventListener("change", e => { audioState.loudness[e.target.dataset.loudnessCheck]=e.target.checked; scheduleAudioSave(); }));
      const stateEl=qs("#eqApplyState");
      stateEl.textContent = status.error ? `error · ${status.error}` : (status.converged ? `live · revision ${audioState.revision}` : `applying · revision ${audioState.revision}`);
      stateEl.style.color = status.error ? "var(--bad)" : (status.converged ? "var(--ok)" : "var(--warn)");
    }

    function renderSpeakerProfiles() {
      if (!speakerState) return;
      const selection=speakerState.selection||{}, catalog=speakerState.catalog||{}, status=speakerState.status||{};
      const cards=Object.values(catalog).map(p => {
        const selected=p.id===selection.selected, applied=p.id===status.applied;
        const state=selected?(p.available&&applied&&status.ok&&speakerState.ready?"active":"applying"):applied?"currently loaded":"";
        const detail=p.available?(state||p.description||""):(p.reason||"profile unavailable");
        return `<div class="speaker-cell"><button class="btn ${selected?"primary":""}" data-speaker="${esc(p.id)}" ${p.available&&!selected?"":"disabled"} title="${esc(p.reason||p.description||"")}" style="text-align:left;min-height:64px">
          <b>${esc(p.label||p.id)}</b><br><span class="sub2">${esc(detail)}</span></button></div>`;
      }).join("");
      const selected=catalog[selection.selected]||{};
      const error=status.ok===false?`<div class="sub2" style="color:var(--bad);margin-top:10px">${esc(status.error||"Transition failed — output remains inhibited")}</div>`:"";
      const profilePanel=qs("#speakerProfiles");
      if (profilePanel) profilePanel.innerHTML=`<div class="cap">Complete DSP profile</div><div class="sources" style="margin-top:10px">${cards}</div>
        <div class="sub2" style="margin-top:10px">Selected: <b>${esc(selected.label||selection.selected)}</b> · revision ${selection.revision||0}. Profiles are installed definitions and may contain FIR crossovers or custom filter chains. Selecting a target only opens the confirmation dialog. Swap passive speakers only after it shows active. User EQ and loudness are stored separately for every profile.${selected.bypass_user_eq?" This profile deliberately bypasses user EQ.":""}</div>${error}`;
      const dashboard=qs("#dashboardSpeaker");
      if (dashboard) {
        const active=selected.available&&status.applied===selection.selected&&status.ok&&speakerState.ready;
        const options=Object.values(catalog).map(p=>`<option value="${esc(p.id)}" ${p.id===selection.selected?"selected":""} ${p.available?"":"disabled"}>${esc(p.label||p.id)}${p.available?"":" — unavailable"}</option>`).join("");
        dashboard.innerHTML=`<div class="active-name">${esc(selected.label||selection.selected||"—")}</div><div><span class="badge ${active?"ok":"warn"}">${active?"active":"transition / inhibited"}</span></div><select id="dashboardSpeakerTarget" aria-label="Target speaker profile">${options}</select><button class="btn danger" id="dashboardSpeakerChange" disabled>Review profile change…</button><div class="sub2">Changing this is intentionally a confirmed maintenance action.</div>${error}`;
        const target=qs("#dashboardSpeakerTarget"), change=qs("#dashboardSpeakerChange");
        const sync=()=>{ change.disabled=!target.value||target.value===selection.selected; };
        target.addEventListener("change",sync); sync();
        change.addEventListener("click",()=>requestSpeakerChange(target.value));
      }
      qsa("[data-speaker]").forEach(button => button.addEventListener("click", () => requestSpeakerChange(button.dataset.speaker)));
    }

    function requestSpeakerChange(speakerId) {
      if (!speakerState || speakerId===speakerState.selection?.selected) return;
      const profile=speakerState.catalog?.[speakerId];
      if (!profile?.available) { toast(profile?.reason||"Speaker profile unavailable"); return; }
      pendingSpeakerId=speakerId;
      const current=speakerState.catalog?.[speakerState.selection?.selected]||{};
      qs("#speakerConfirmRoute").textContent=`${current.label||speakerState.selection.selected} → ${profile.label||speakerId}`;
      qs("#speakerConfirmText").value="";
      qs("#speakerConfirmApply").disabled=true;
      qs("#speakerConfirm").showModal();
      setTimeout(()=>qs("#speakerConfirmText").focus(),0);
    }

    async function confirmSpeakerChange() {
      if (qs("#speakerConfirmText").value.trim()!=="SWITCH") return;
      if (!pendingSpeakerId) return;
      if (audioSaving) { toast("Wait for the current EQ save to finish."); return; }
      if (audioState && !(await saveAudio())) {
        toast("Resolve the pending EQ save before changing speakers.");
        return;
      }
      const apply=qs("#speakerConfirmApply"); apply.disabled=true; apply.textContent="Muting and validating…";
      try {
        const result=await api("/api/speaker",{method:"POST",body:JSON.stringify({selected:pendingSpeakerId,revision:speakerState.selection.revision,confirm:"SWITCH"})});
        pendingSpeakerId=null;
        speakerState=result.speaker; qs("#speakerConfirm").close(); renderSpeakerProfiles(); audioLoaded=false; await loadAudio();
        [1000,2500,5000].forEach(delay => setTimeout(() => { if (!audioDirty && !audioSaving) loadAudio(); }, delay));
      } catch(e) { toast(e.message); await loadAudio(); }
      finally { apply.textContent="Mute and switch profile"; }
    }

    function setAudioAvailable(available, message="") {
      const notice=qs("#audioNotice"), noticeText=qs("#audioNoticeText");
      if (notice) notice.classList.toggle("show", !available);
      if (noticeText) noticeText.textContent=message;
      ["#eqAdd","#eqFlat","#eqSave","#eqShowBands"].forEach(selector => {
        const control=qs(selector); if (control) control.disabled=!available;
      });
    }

    async function loadAudio() {
      qs("#eqApplyState").textContent="loading…";
      try {
        const data=await api("/api/audio");
        audioState=data.state; audioBridge=data.volume_bridge||{}; audioCapability=data.iso226_capability||{}; speakerState=data.speaker||null;
        audioLoaded=true; audioDirty=false; setAudioAvailable(true); renderSpeakerProfiles(); renderAudioEditor(data.status||{});
        qs("#eqSave").disabled=true;
      } catch (e) {
        audioState=null; audioLoaded=false; audioDirty=false;
        const message=`Could not load audio state: ${e.message}`;
        setAudioAvailable(false, message);
        qs("#eqApplyState").textContent="unavailable"; qs("#eqApplyState").style.color="var(--bad)";
      }
    }
    function scheduleAudioSave() { audioDirty=true; audioEditGeneration++; clearTimeout(audioSaveTimer); qs("#eqApplyState").textContent="pending changes"; qs("#eqApplyState").style.color="var(--warn)"; qs("#eqSave").disabled=false; audioSaveTimer=setTimeout(saveAudio,900); }
    async function saveAudio() {
      if (!audioState) return false;
      if (!audioDirty) return true;
      if (audioSaving) return false;
      const generation=audioEditGeneration;
      const snapshot=JSON.parse(JSON.stringify(audioState));
      let conflictReloaded=false;
      audioSaving=true; clearTimeout(audioSaveTimer); qs("#eqApplyState").textContent="saving…"; qs("#eqSave").disabled=true;
      try {
        const data=await api("/api/audio",{method:"POST",body:JSON.stringify({state:snapshot,speaker:speakerState?.selection?.selected})});
        if (generation === audioEditGeneration) { audioState=data.state; audioDirty=false; renderAudioEditor(data.status||{}); }
        else { audioState.revision=data.state.revision; }
        setTimeout(() => { if (generation === audioEditGeneration && !audioSaving) loadAudio(); },1200);
        return true;
      } catch(e) {
        if (e.message.includes("changed elsewhere")) { await loadAudio(); audioEditGeneration=generation; conflictReloaded=true; toast("Audio settings changed from another control; live values reloaded. Please repeat your edit."); }
        else { toast(e.message); qs("#eqApplyState").textContent=e.message; qs("#eqApplyState").style.color="var(--bad)"; }
        return false;
      }
      finally {
        audioSaving=false;
        if (!conflictReloaded && generation !== audioEditGeneration) { clearTimeout(audioSaveTimer); audioSaveTimer=setTimeout(saveAudio,0); }
        else if (audioDirty) qs("#eqSave").disabled=false;
      }
    }

    function renderSystem(data) {
      const s = data.services, c = data.camilla || {}, r = data.remote || {};
      const st = n => serviceText(s[n]);
      const cl = n => statusClass(s[n] && s[n].active);
      qs("#systemGrid").innerHTML = [
        tile("CamillaDSP", esc(c.error ? "error" : (c.state || st("camilladsp.service"))), c.error ? "bad" : cl("camilladsp.service"), esc(c.config_title || "")),
        tile("Source switcher", esc(st("cdsp-source-switcher.service")), cl("cdsp-source-switcher.service")),
        tile("MOTU clock sync", esc(st("cdsp-motu-sync.service")), cl("cdsp-motu-sync.service")),
        tile("Amp trigger", esc(st("cdsp-trigger.service")), cl("cdsp-trigger.service")),
        tile("HID remote", r.connected ? "connected" : "not connected", r.connected ? "ok" : "warn", esc(r.expected_name || "")),
        tile("Config", esc(c.config_title || "—"), "", esc((c.config_file || "").split("/").pop() || "")),
      ].join("");
    }

    function renderServices(services = {}) {
      const table = qs("#servicesTable");
      const entries = Object.values(services || {});
      if (!entries.length) {
        table.innerHTML = `<div class="card"><div class="val warn">Service status unavailable</div><div class="sub2">Waiting for the next status refresh.</div></div>`;
        return;
      }
      const groups = {};
      entries.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
      table.innerHTML = Object.entries(groups).map(([group, items]) => {
        const rows = items.map(s => {
          const disabled = s.load === "not-found";
          const acts = (s.controls || []).map(a =>
            `<button class="btn sm ${a === "stop" ? "danger" : "ghost"}" data-service="${esc(s.name)}" data-action="${a}" ${disabled ? "disabled" : ""}>${a}</button>`).join("");
          return `<div class="svc-row">
            <div class="svc-name"><div class="l">${esc(s.label)}</div><div class="u">${esc(s.name)}</div></div>
            <span class="badge ${statusClass(s.active)}">${esc(disabled ? "not found" : s.active)}${s.sub && s.sub !== "running" && !disabled ? " · " + esc(s.sub) : ""}</span>
            <div class="row">${acts}</div></div>`;
        }).join("");
        return `<div class="card svc-group"><div class="cap">${esc(group)}</div>${rows}</div>`;
      }).join("");
      qsa("[data-service]").forEach(b => b.addEventListener("click", serviceAction));
    }

    function renderLogOptions(services) {
      const sel = qs("#logUnit"); const cur = sel.value || "camilladsp.service";
      sel.innerHTML = Object.values(services).filter(s => s.load !== "not-found")
        .map(s => `<option value="${esc(s.name)}">${esc(s.label)}</option>`).join("");
      sel.value = services[cur] && services[cur].load !== "not-found" ? cur : "camilladsp.service";
    }

    /* ---------------- actions ---------------- */
    async function setVolume(payload) { try { await api("/api/camilla/volume", { method: "POST", body: JSON.stringify(payload) }); await load(); } catch (e) { toast(e.message); } }
    function currentVolumeInput() { return Number(qs("#volNum").value); }
    async function volumeAction(e) {
      const a = e.currentTarget.dataset.volumeAction;
      if (a === "down") await setVolume({ delta_db: -1 });
      else if (a === "up") await setVolume({ delta_db: 1 });
      else await setVolume({ volume_db: currentVolumeInput() });
    }
    async function sourceAction(e) {
      const btn = e.currentTarget; btn.disabled = true;
      try { await api("/api/source", { method: "POST", body: JSON.stringify({ source: btn.dataset.sourceAction }) });
        await new Promise(r => setTimeout(r, 1200)); await load();
      } catch (err) { toast(err.message); } finally { btn.disabled = false; }
    }
    async function serviceAction(e) {
      const b = e.currentTarget; b.disabled = true;
      try { await api("/api/service", { method: "POST", body: JSON.stringify({ service: b.dataset.service, action: b.dataset.action }) });
        await new Promise(r => setTimeout(r, 700)); await load();
      } catch (err) { toast(err.message); } finally { b.disabled = false; }
    }
    async function ampsOff() {
      const b = qs("#ampOff"); b.disabled = true;
      try {
        await api("/api/amps/off", { method: "POST", body: "{}" });
        toast("Amps turned off · automatic trigger remains enabled");
      } catch (err) { toast(err.message); }
      finally { b.disabled = false; }
    }
    async function refreshLogs() {
      const unit = qs("#logUnit").value || "camilladsp.service";
      try { const d = await api(`/api/logs?unit=${encodeURIComponent(unit)}`); qs("#logBox").textContent = d.logs || "—"; }
      catch (e) { qs("#logBox").textContent = e.message; }
    }

    /* ---- USB storage: status + mount / unmount ---- */
    async function loadStorage() { try { renderStorage(await api("/api/storage")); } catch (e) { /* keep */ } }
    function renderStorage(s) {
      const box = qs("#storageCard"); if (!box) return;
      s = s || {};
      const free = s.free_bytes != null ? (s.free_bytes / 1e9).toFixed(1) + " GB free" : "";
      const size = s.size_bytes != null ? " / " + (s.size_bytes / 1e9).toFixed(0) + " GB" : "";
      box.style.borderColor = s.mounted ? "var(--line)" : "var(--warn)";
      box.innerHTML = `<div class="cap">USB storage
          <span class="badge ${s.mounted ? "ok" : "warn"}">${s.mounted ? "mounted" : "not mounted"}</span></div>
        <div class="val sm" style="margin-top:6px">${esc(s.root || "/mnt/whispers")}</div>
        <div class="sub2">${s.mounted
          ? `${esc(s.device || "")} · ${esc(s.fstype || "")} · ${free}${size} · ${(s.folders || []).length} folders`
          : "not mounted — Mount it, or it auto-mounts on next boot / on access"}</div>
        <div class="row" style="margin-top:10px">
          <button class="btn sm" id="stMount" ${s.mounted ? "disabled" : ""}>Mount</button>
          <button class="btn sm danger" id="stUnmount" ${s.mounted ? "" : "disabled"}>Unmount (safe remove)</button>
          <button class="btn sm ghost" id="stRefresh">Refresh</button>
        </div>`;
      const act = async action => {
        try { const r = await api("/api/storage", { method: "POST", body: JSON.stringify({ action }) });
          renderStorage(r.storage); toast(action === "mount" ? "Mounted" : "Unmounted — safe to remove the drive"); }
        catch (e) { toast(e.message); }
      };
      const m = qs("#stMount"), u = qs("#stUnmount"), r = qs("#stRefresh");
      if (m) m.addEventListener("click", () => act("mount"));
      if (u) u.addEventListener("click", () => act("unmount"));
      if (r) r.addEventListener("click", loadStorage);
    }

    /* ---- clock sync from the phone (site has no RTC battery / no Wi-Fi) ---- */
    let clockSyncing = false;
    async function syncClockFromPhone() {
      if (clockSyncing) return;
      clockSyncing = true;
      try {
        await api("/api/time", { method: "POST", body: JSON.stringify({ epoch: Date.now() / 1000 }) });
        toast("Pi clock set from this phone");
        setTimeout(load, 400);
      } catch (e) { toast(e.message); }
      finally { clockSyncing = false; }
    }
    function maybeSyncClock(piEpoch) {
      if (typeof piEpoch !== "number") return;
      const drift = Date.now() / 1000 - piEpoch;   // + = phone ahead of Pi
      renderClock(piEpoch, drift);
      if (Math.abs(drift) > 12 && !clockSyncing) syncClockFromPhone();
    }
    function renderClock(piEpoch, drift) {
      const box = qs("#clockCard"); if (!box) return;
      const piT = fmtClock(new Date(piEpoch * 1000));
      const ad = Math.abs(drift), ok = ad <= 12;
      box.style.borderColor = ok ? "var(--line)" : "var(--warn)";
      box.innerHTML = `<div class="cap">Pi clock ${ok ? `<span class="ok">in sync</span>` : `<span class="warn">off by ${Math.round(ad)}s</span>`}</div>
        <div class="row" style="align-items:center;gap:12px;margin-top:4px">
          <div class="val sm" style="flex:1">${piT}</div>
          <button class="btn sm" id="clockSyncBtn">Sync from this phone</button>
        </div>
        <div class="sub2" style="margin-top:6px">No RTC battery or Wi-Fi on site: open this page after a power cut and it sets the Pi's clock from your phone.</div>`;
      qs("#clockSyncBtn").addEventListener("click", syncClockFromPhone);
    }

    /* ---------------- loop ---------------- */
    async function load() {
      const data = await api("/api/status");
      // Keep service controls available even if a later, unrelated panel fails
      // to render on an older browser or with partial device status.
      renderServices(data.services || {});
      renderLogOptions(data.services || {});
      renderHealth(data);
      renderHero(data);
      renderSource(data);
      renderVolume(data);
      if (Number(data.camilla?.sample_rate)>0) eqSampleRate=Number(data.camilla.sample_rate);
      if (data.speaker) { speakerState=data.speaker; renderSpeakerProfiles(); }
      renderSystem(data);
      maybeSyncClock(data.time);
      loadStorage();
    }

    async function pollLevels() {
      if (document.hidden) return;
      try { const d = await api("/api/levels"); updateSignal(d.signal_db); }
      catch (e) { /* keep last */ }
    }

    // header equalizer, driven by real signal magnitude
    const bars = qsa("#eq i");
    let phase = 0;
    function animateEq() {
      phase += 0.08;
      bars.forEach((b, i) => {
        const wob = 0.5 + 0.5 * Math.sin(phase * (1.1 + i * 0.28) + i * 1.3);
        const h = 12 + (12 + signalTarget * 74) * wob;
        b.style.height = Math.min(100, h).toFixed(0) + "%";
      });
      requestAnimationFrame(animateEq);
    }

    function activateTab(name) {
      const btn = qsa("nav button").find(b => b.dataset.tab === name);
      if (!btn) return;
      qsa("nav button").forEach(x => x.classList.remove("active"));
      qsa("section").forEach(s => s.classList.remove("active"));
      btn.classList.add("active"); qs("#" + name).classList.add("active");
      if (name === "logs") refreshLogs();
      if (name === "audio" && !audioLoaded) loadAudio();
    }
    qsa("nav button").forEach(b => b.addEventListener("click", () => {
      location.hash = b.dataset.tab; activateTab(b.dataset.tab);
    }));
    window.addEventListener("hashchange", () => activateTab(location.hash.slice(1)));
    qs("#refreshLogs").addEventListener("click", refreshLogs);
    qs("#logUnit").addEventListener("change", refreshLogs);
    qs("#ampOff").addEventListener("click", ampsOff);
    qs("#eqAdd").addEventListener("click", () => {
      const section=audioState;
      if (!section || section.bands.length >= 16) { toast("A maximum of 16 EQ bands is supported."); return; }
      section.bands.push({id:`band_${Date.now().toString(36)}`,enabled:true,type:"Peaking",freq:1000,gain:0,q:1});
      selectedEqBand=section.bands.length-1; renderAudioEditor(); scheduleAudioSave();
    });
    qs("#eqFlat").addEventListener("click", () => {
      const section=audioState;
      if (!section) return; section.bands.forEach(b => b.gain=0); section.preamp_db=0; renderAudioEditor(); scheduleAudioSave();
    });
    qs("#eqSave").addEventListener("click", saveAudio);
    qs("#audioRetry").addEventListener("click", loadAudio);
    qs("#eqShowBands").addEventListener("change", e => { eqShowBandCurves=e.target.checked; renderEqPlot(); });
    qs("#speakerConfirmText").addEventListener("input", e => { qs("#speakerConfirmApply").disabled=e.target.value.trim()!=="SWITCH"; });
    qs("#speakerConfirmText").addEventListener("keydown", e => { if (e.key==="Enter" && e.target.value.trim()==="SWITCH") { e.preventDefault(); confirmSpeakerChange(); } });
    qs("#speakerConfirmApply").addEventListener("click", confirmSpeakerChange);
    qs("#speakerConfirmCancel").addEventListener("click", () => { pendingSpeakerId=null; qs("#speakerConfirm").close(); });
    qs("#speakerConfirm").addEventListener("cancel", () => { pendingSpeakerId=null; });

    animateEq();
    if (location.hash.slice(1)) activateTab(location.hash.slice(1));
    load().catch(e => toast(e.message));
    setInterval(() => load().catch(() => {}), 5000);
    setInterval(pollLevels, 800);
  </script>
</body>
</html>
"""


def run_result(
    command: list[str], timeout: float = 5.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, str(exc))


def run(command: list[str], timeout: float = 5.0) -> str:
    return run_result(command, timeout).stdout.strip()


def run_checked(command: list[str], timeout: float = 10.0) -> str:
    result = run_result(command, timeout)
    output = result.stdout.strip()
    if result.returncode != 0:
        raise RuntimeError(output or f"{command[0]} exited with {result.returncode}")
    return output


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def systemctl_show(service: str) -> dict[str, str]:
    output = run(
        [
            "systemctl",
            "show",
            service,
            "--no-page",
            "--property=LoadState,ActiveState,SubState,UnitFileState,Description",
        ],
        timeout=4,
    )
    props: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def service_status() -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for name, meta in SERVICE_CATALOG.items():
        props = systemctl_show(name)
        active = (
            props.get("ActiveState")
            or run(["systemctl", "is-active", name], timeout=3)
            or "unknown"
        )
        load = props.get("LoadState") or "unknown"
        entry: dict[str, Any] = {
            "name": name,
            "label": meta["label"],
            "group": meta["group"],
            "required": meta["required"],
            "controls": meta["controls"],
            "active": active,
            "sub": props.get("SubState", ""),
            "load": load,
            "enabled": props.get("UnitFileState", ""),
            "description": props.get("Description", ""),
        }
        payload[name] = entry
    return payload


def clamp_volume(value: float) -> float:
    return max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, value))


# ---- persistent read-only CamillaDSP client for the live signal meter ----
_levels_lock = threading.Lock()
_levels_client: Any = None


def camilla_levels() -> dict[str, Any]:
    """Return the strongest current playback RMS level in dBFS (or None).

    Uses one persistent, read-only CamillaDSP connection reused across polls so
    the live meter is cheap. Self-heals by reconnecting on any error.
    """
    global _levels_client
    with _levels_lock:
        try:
            if _levels_client is None:
                from camilladsp import CamillaClient

                client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
                client.connect()
                _levels_client = client
            levels = _levels_client.levels.playback_rms()
        except Exception:
            try:
                if _levels_client is not None:
                    _levels_client.disconnect()
            except Exception:
                pass
            _levels_client = None
            return {"ok": False, "signal_db": None}

    finite = [lvl for lvl in levels if lvl is not None and lvl > -999.0]
    return {"ok": True, "signal_db": max(finite) if finite else None}


def camilla_status() -> dict[str, Any]:
    client: Any = None
    try:
        from camilladsp import CamillaClient

        client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
        client.connect()
        config = client.config.active() or {}
        payload = {
            "state": str(client.general.state()).replace("ProcessingState.", ""),
            "config_title": config.get("title"),
            "config_file": client.config.file_path(),
            "sample_rate": config.get("devices", {}).get("samplerate"),
            "volume_db": client.volume.main_volume(),
            "muted": client.volume.main_mute(),
        }
        return payload
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass


def set_camilla_volume(payload: dict[str, Any]) -> dict[str, Any]:
    from camilladsp import CamillaClient

    client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
    client.connect()
    try:
        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            if "delta_db" in payload:
                current = float(client.volume.main_volume())
                client.volume.set_main_volume(
                    clamp_volume(current + float(payload["delta_db"]))
                )
            elif "volume_db" in payload:
                client.volume.set_main_volume(
                    clamp_volume(float(payload["volume_db"]))
                )

            if "muted" in payload:
                muted = payload["muted"]
                if not isinstance(muted, bool):
                    raise ValueError("muted must be true or false")
                if not muted:
                    require_audio_unmute_allowed(AUDIO_READY_PATH)
                client.volume.set_main_mute(muted)
    finally:
        client.disconnect()

    return camilla_status()


def input_devices() -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    path = Path("/proc/bus/input/devices")
    if not path.exists():
        return devices

    current: dict[str, Any] = {}
    for line in path.read_text(errors="replace").splitlines():
        if not line:
            if current:
                devices.append(current)
                current = {}
            continue
        if line.startswith("N: Name="):
            current["name"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            current["handlers"] = line.split("=", 1)[1].split()

    if current:
        devices.append(current)
    return devices


def remote_status(services: dict[str, dict[str, Any]]) -> dict[str, Any]:
    settings = parse_env(CDSP_ENV)
    expected_name = settings.get("REMOTE_NAME", DEFAULT_REMOTE_NAME)
    matches = [
        device for device in input_devices() if device.get("name") == expected_name
    ]
    return {
        "expected_name": expected_name,
        "connected": bool(matches),
        "service_active": services.get("cdsp-remote.service", {}).get("active")
        == "active",
        "devices": matches,
    }


def read_source_override() -> str | None:
    source = _read_runtime_value(SOURCE_OVERRIDE_PATH)
    return source if source and source != "auto" else None


@contextmanager
def source_override_lock() -> Iterator[None]:
    SOURCE_OVERRIDE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(SOURCE_OVERRIDE_LOCK_PATH, flags, 0o644)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_runtime_value(path: Path, value: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
            temporary = Path(handle.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_runtime_value(path: Path) -> str:
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return handle.read().strip().lower()
    except OSError:
        return ""


def write_source_override(source: str) -> None:
    if source == "auto":
        with source_override_lock():
            SOURCE_OVERRIDE_PATH.unlink(missing_ok=True)
            SOURCE_OVERRIDE_OWNER_PATH.unlink(missing_ok=True)
        return

    if source not in SOURCE_CHOICES:
        raise ValueError("source not allowed")
    availability = source_availability()
    entry = availability.get(source, {})
    if not entry.get("exists"):
        raise FileNotFoundError(str(entry.get("path") or source))

    with source_override_lock():
        SOURCE_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_runtime_value(SOURCE_OVERRIDE_OWNER_PATH, "ui\n")
        _write_runtime_value(SOURCE_OVERRIDE_PATH, source + "\n")


def source_availability() -> dict[str, dict[str, Any]]:
    selection = read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )
    selected = selection["selected"]
    if selected == "kantarellen":
        return {
            key: {
                "label": label,
                "path": str(CDSP_CONFIG_DIR / f"{key}.yml"),
                "exists": (CDSP_CONFIG_DIR / f"{key}.yml").is_file(),
            }
            for key, label in SOURCE_CHOICES.items()
        }
    profile = profile_catalog(
        SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, CDSP_CONFIG_DIR
    ).get(selected, {})
    supported = set(profile.get("supported_sources") or [])
    profile_ready = bool(profile.get("available"))
    return {
        key: {
            "label": label,
            "path": str(SOURCE_BASE_DIR / f"{key}.yml"),
            "exists": bool(
                profile_ready
                and key in supported
                and (SOURCE_BASE_DIR / f"{key}.yml").is_file()
            ),
        }
        for key, label in SOURCE_CHOICES.items()
    }


def source_status(camilla: dict[str, Any]) -> dict[str, Any]:
    override = read_source_override()
    config_file = camilla.get("config_file")
    current = None
    if isinstance(config_file, str):
        stem = Path(config_file).stem
        source = stem.split("--", 1)[0]
        current = source if source in SOURCE_CHOICES else stem

    return {
        "mode": override or "auto",
        "current": current,
        "config_file": config_file,
        "available": source_availability(),
    }


def list_media_folders() -> dict[str, Any]:
    """Session folders on the USB drive, each with a playable-audio count.

    Hidden entries and macOS metadata (._*, .Spotlight-V100, .Trashes, …) are
    skipped so the GUI shows only real session folders.
    """
    mounted = False
    try:
        mounted = MEDIA_ROOT.is_mount()
    except OSError:
        mounted = False
    folders: list[dict[str, Any]] = []
    if MEDIA_ROOT.is_dir():
        for p in sorted(MEDIA_ROOT.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            try:
                count = sum(
                    1
                    for f in p.iterdir()
                    if f.is_file()
                    and f.suffix.lower() in AUDIO_EXTS
                    and not f.name.startswith(".")
                )
            except OSError:
                count = 0
            folders.append({"name": p.name, "count": count})
    return {"root": str(MEDIA_ROOT), "mounted": mounted, "folders": folders}


def storage_status() -> dict[str, Any]:
    """USB media status read from /proc/mounts so it never triggers the autofs
    mount (which would defeat a manual unmount)."""
    target = str(MEDIA_ROOT)
    mounted = False
    device = fstype = None
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == target:
                    mounted, device, fstype = True, parts[0], parts[2]
                    break
    except OSError:
        pass
    info: dict[str, Any] = {
        "root": target,
        "mounted": mounted,
        "device": device,
        "fstype": fstype,
    }
    if mounted:
        try:
            st = os.statvfs(target)
            info["free_bytes"] = st.f_bavail * st.f_frsize
            info["size_bytes"] = st.f_blocks * st.f_frsize
        except OSError:
            pass
        info["folders"] = list_media_folders().get("folders", [])
    return info


def _backup_file(
    path: Path, backup_dir: Path, prefix: str, keep: int = BACKUP_KEEP
) -> None:
    """Snapshot `path` into backup_dir/<prefix>-<stamp>.json before it's overwritten."""
    if not path.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (backup_dir / f"{prefix}-{stamp}.json").write_bytes(path.read_bytes())
        old = sorted(backup_dir.glob(f"{prefix}-*.json"))
        for extra in old[:-keep]:
            extra.unlink(missing_ok=True)
    except OSError:
        pass


def set_system_clock(epoch: int) -> None:
    """Set the Pi's clock from a trusted epoch (e.g. the phone's browser time)."""
    if not (MIN_VALID_EPOCH <= epoch <= MAX_VALID_EPOCH):
        raise ValueError("epoch out of range")
    run_checked(["date", "-s", f"@{epoch}"], timeout=5)
    run(["hwclock", "-w"], timeout=5)  # best-effort; harmless without a cell


_iso_capability_lock = threading.Lock()
_iso_capability_checked_at = 0.0
_iso_capability_cache: tuple[dict[str, Any], bool] = ({}, False)


def iso226_capability() -> tuple[dict[str, Any], bool]:
    """Verify the marker hash against the running CamillaDSP executable."""
    global _iso_capability_checked_at, _iso_capability_cache
    with _iso_capability_lock:
        now = time.monotonic()
        if now - _iso_capability_checked_at < 30:
            return _iso_capability_cache
        try:
            capability = json.loads(ISO226_CAPABILITY_PATH.read_text(encoding="utf-8"))
            pid = int(
                run(
                    [
                        "systemctl",
                        "show",
                        "-p",
                        "MainPID",
                        "--value",
                        "camilladsp.service",
                    ]
                )
            )
            executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
            digest = hashlib.sha256()
            with executable.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            available = bool(
                capability.get("engine") == "Iso226"
                and capability.get("binary_sha256") == digest.hexdigest()
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            capability, available = {}, False
        _iso_capability_checked_at = now
        _iso_capability_cache = capability, available
        return _iso_capability_cache


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def speaker_payload() -> dict[str, Any]:
    selection = read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )
    status = _read_json_object(SPEAKER_STATUS_PATH)
    catalog = profile_catalog(SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, CDSP_CONFIG_DIR)
    return {
        "selection": selection,
        "catalog": catalog,
        "status": status,
        "ready": AUDIO_READY_PATH.is_file(),
    }


def _selected_audio_path(selection: dict[str, Any]) -> Path:
    return resolve_profile_audio_path(
        SPEAKER_AUDIO_DIR,
        selection["selected"],
        legacy_path=AUDIO_EQ_PATH,
    )


def preflight_speaker_profile(speaker_id: str) -> None:
    """Compile and offline-check every declared source before selection."""
    if speaker_id == "kantarellen":
        paths = [CDSP_CONFIG_DIR / f"{source}.yml" for source in ("streamer", "gadget", "toslink")]
        analog = CDSP_CONFIG_DIR / "analog.yml"
        if analog.is_file():
            paths.append(analog)
        configs = [(path.stem, path) for path in paths]
    elif speaker_id in OPERATOR_CONFIG_SPEAKERS:
        profile = load_profile(SPEAKER_PROFILE_DIR, speaker_id)
        operator_configs = operator_configs_for_speaker(speaker_id)
        missing_sources = [
            source
            for source in profile["supported_sources"]
            if source not in operator_configs
        ]
        if missing_sources:
            raise ValueError(
                f"{speaker_id} has no operator config for: {', '.join(missing_sources)}"
            )
        configs = [
            (source, CDSP_CONFIG_DIR / operator_configs[source])
            for source in profile["supported_sources"]
        ]
    else:
        profile = load_profile(SPEAKER_PROFILE_DIR, speaker_id)
        audio_state = read_profile_audio_state(
            SPEAKER_AUDIO_DIR, speaker_id, legacy_path=AUDIO_EQ_PATH
        )
        configs = []
        for source in profile["supported_sources"]:
            source_base = load_yaml_mapping(
                SOURCE_BASE_DIR / f"{source}.yml", f"source base {source}"
            )
            compiled = compile_profile_config(
                source_base, profile, audio_state, source_id=source
            )
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", suffix=".yml", delete=False
                ) as handle:
                    yaml.safe_dump(compiled, handle, sort_keys=False)
                    temporary = Path(handle.name)
                result = subprocess.run(
                    [CAMILLA_BINARY, "-c", str(temporary)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    check=False,
                )
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            if result.returncode != 0:
                raise ValueError(
                    f"CamillaDSP rejected {source}/{speaker_id}: "
                    f"{result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.returncode}"
                )
        return

    for source, path in configs:
        if not path.is_file():
            raise FileNotFoundError(path)
        result = subprocess.run(
            [CAMILLA_BINARY, "-c", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"CamillaDSP rejected {source}/{speaker_id}")


def require_active_source_supported(
    speaker_id: str, catalog: dict[str, dict[str, Any]]
) -> str:
    """Fail before selection commit if the target cannot preserve live source."""
    camilla = camilla_status()
    if camilla.get("error"):
        raise ValueError(f"cannot verify active audio source: {camilla['error']}")
    identity = managed_config_identity(camilla.get("config_file"))
    if identity is None:
        raise ValueError("cannot change speakers while the active DSP config is unmanaged")
    source, _active_speaker = identity
    if speaker_id == "kantarellen":
        if not (CDSP_CONFIG_DIR / f"{source}.yml").is_file():
            raise ValueError(f"Kantarellen has no installed {source} configuration")
        return source
    supported = set(catalog.get(speaker_id, {}).get("supported_sources") or [])
    if source not in supported:
        raise ValueError(
            f"{catalog[speaker_id].get('label') or speaker_id} does not support the active {source} source"
        )
    return source


def managed_config_identity(current: Any) -> tuple[str, str] | None:
    """Match exact legacy, operator-owned, or generated DSP configs."""
    if not isinstance(current, str) or not current:
        return None
    current_absolute = os.path.abspath(current)
    for source in SOURCE_CHOICES:
        target = CDSP_CONFIG_DIR / f"{source}.yml"
        if current_absolute == os.path.abspath(target):
            return source, "kantarellen"
    for speaker_id in OPERATOR_CONFIG_SPEAKERS:
        for source, filename in operator_configs_for_speaker(speaker_id).items():
            if current_absolute == os.path.abspath(CDSP_CONFIG_DIR / filename):
                return source, speaker_id
    path = Path(current).resolve(strict=False)
    generated_root = SPEAKER_GENERATED_DIR.resolve(strict=False)
    if path.parent.parent != generated_root:
        return None
    digest = path.parent.name
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    parts = path.stem.split("--", 1)
    if (
        len(parts) != 2
        or parts[0] not in SOURCE_CHOICES
        or parts[1] not in BUILTIN_SPEAKERS
        or parts[1] == "kantarellen"
    ):
        return None
    try:
        config = load_yaml_mapping(path, "managed generated config")
    except (OSError, ValueError):
        return None
    if config_digest(config) != digest:
        return None
    return parts[0], parts[1]


def select_speaker(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("speaker selection must be an object")
    if raw.get("confirm") != "SWITCH":
        raise ValueError("speaker selection requires explicit SWITCH confirmation")
    speaker_id = raw.get("selected")
    expected_revision = raw.get("revision")
    catalog = profile_catalog(SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, CDSP_CONFIG_DIR)
    available_ids = {
        profile_id
        for profile_id, entry in catalog.items()
        if entry.get("available")
    }
    if speaker_id not in available_ids:
        reason = catalog.get(speaker_id, {}).get("reason") or "profile is unavailable"
        raise ValueError(f"cannot select {speaker_id!r}: {reason}")
    preflight_speaker_profile(speaker_id)

    def inhibit_and_mute(updated: dict[str, Any]) -> None:
        # Called by update_speaker_selection only after its revision check has
        # succeeded, while both the audio and selection locks are held.
        set_audio_inhibit(
            AUDIO_READY_PATH,
            {"reason": "speaker selected", "target": speaker_id},
        )
        from camilladsp import CamillaClient

        client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
        try:
            client.connect()
            restore_mute = bool(client.volume.main_mute())
            atomic_write_json(
                SPEAKER_TRANSITION_PATH,
                {
                    "version": 1,
                    "revision": updated["revision"],
                    "selected": updated["selected"],
                    "restore_mute": restore_mute,
                },
            )
            client.volume.set_main_mute(True)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        require_active_source_supported(speaker_id, catalog)
        update_speaker_selection(
            SPEAKER_SELECTION_PATH,
            speaker_id,
            expected_revision=expected_revision,
            allowed_ids=available_ids,
            before_commit=inhibit_and_mute,
        )
    # Ensure the new profile has an isolated state before the editor loads it.
    read_profile_audio_state(
        SPEAKER_AUDIO_DIR, speaker_id, legacy_path=AUDIO_EQ_PATH
    )
    return speaker_payload()


def preview_speaker_crossover(raw: Any) -> dict[str, Any]:
    """Validate an unsaved crossover spec and return its response curves."""
    if not isinstance(raw, dict):
        raise ValueError("crossover preview must be an object")
    crossover = normalize_crossover(
        raw.get("crossover"), raw_measurement=bool(raw.get("raw_measurement"))
    )
    return {"crossover": crossover, "response": crossover_response(crossover)}


def save_speaker_profile(raw: Any) -> dict[str, Any]:
    """Persist a parametric profile; editing the live one re-applies muted."""
    if not isinstance(raw, dict):
        raise ValueError("speaker profile payload must be an object")
    speaker_id = normalize_speaker_id(raw.get("id"))
    if speaker_id not in BUILTIN_SPEAKERS:
        raise ValueError(f"unknown speaker profile: {speaker_id}")
    if speaker_id == DEFAULT_SPEAKER_ID:
        raise ValueError("Kantarellen uses the legacy configs and cannot be edited here")
    profile_in = raw.get("profile")
    if not isinstance(profile_in, dict):
        raise ValueError("profile must be an object")
    raw_measurement = speaker_id == "measurement"
    document = {
        "version": PROFILE_VERSION,
        "id": speaker_id,
        "label": str(profile_in.get("label") or BUILTIN_SPEAKERS[speaker_id]["label"]),
        "description": str(
            profile_in.get("description")
            or BUILTIN_SPEAKERS[speaker_id]["description"]
        ),
        "enabled": profile_in.get("enabled"),
        "supported_sources": profile_in.get("supported_sources"),
        "max_volume_db": profile_in.get("max_volume_db"),
        "bypass_user_eq": True if raw_measurement else profile_in.get(
            "bypass_user_eq", False
        ),
        "raw_measurement": raw_measurement,
        "crossover": profile_in.get("crossover"),
    }
    selection = read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )
    is_active = selection["selected"] == speaker_id
    if is_active and document["enabled"] is not True:
        raise ValueError(
            "the active speaker profile cannot be disabled; switch speakers first"
        )
    if is_active and raw.get("confirm") != "SWITCH":
        raise ValueError(
            "editing the active speaker profile requires explicit SWITCH confirmation"
        )

    profile_path = SPEAKER_PROFILE_DIR / f"{speaker_id}.yml"
    previous_bytes = (
        profile_path.read_bytes() if profile_path.is_file() else None
    )
    saved = save_profile(
        SPEAKER_PROFILE_DIR,
        speaker_id,
        document,
        expected_revision=raw.get("revision"),
    )
    if not is_active:
        return {"applied": False, "profile_revision": saved["revision"],
                "speaker": speaker_payload()}

    # The live crossover changed: verify it end to end, then bump the
    # selection revision so the switcher re-applies it muted and validated.
    # Any failure restores the previous definition on disk.
    def restore_previous() -> None:
        if previous_bytes is None:
            profile_path.unlink(missing_ok=True)
        else:
            temporary = profile_path.with_name(f".{profile_path.name}.rollback")
            temporary.write_bytes(previous_bytes)
            temporary.replace(profile_path)

    try:
        preflight_speaker_profile(speaker_id)
        catalog = profile_catalog(
            SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, CDSP_CONFIG_DIR
        )
        if not catalog.get(speaker_id, {}).get("available"):
            reason = catalog.get(speaker_id, {}).get("reason") or "profile unavailable"
            raise ValueError(f"edited profile failed validation: {reason}")

        def inhibit_and_mute(updated: dict[str, Any]) -> None:
            set_audio_inhibit(
                AUDIO_READY_PATH,
                {"reason": "active profile edited", "target": speaker_id},
            )
            from camilladsp import CamillaClient

            client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
            try:
                client.connect()
                restore_mute = bool(client.volume.main_mute())
                atomic_write_json(
                    SPEAKER_TRANSITION_PATH,
                    {
                        "version": 1,
                        "revision": updated["revision"],
                        "selected": updated["selected"],
                        "restore_mute": restore_mute,
                    },
                )
                client.volume.set_main_mute(True)
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass

        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            require_active_source_supported(speaker_id, catalog)
            update_speaker_selection(
                SPEAKER_SELECTION_PATH,
                speaker_id,
                expected_revision=raw.get("selection_revision"),
                allowed_ids={speaker_id},
                before_commit=inhibit_and_mute,
                force=True,
            )
    except Exception:
        restore_previous()
        raise
    return {"applied": True, "profile_revision": saved["revision"],
            "speaker": speaker_payload()}


def audio_eq_payload() -> dict[str, Any]:
    speakers = speaker_payload()
    selection = speakers["selection"]
    audio_path = _selected_audio_path(selection)
    try:
        state = read_profile_audio_state(
            SPEAKER_AUDIO_DIR,
            selection["selected"],
            legacy_path=AUDIO_EQ_PATH,
        )
        state_error = ""
    except ValueError as exc:
        state = default_audio_state()
        state_error = str(exc)
    try:
        status = json.loads(AUDIO_EQ_STATUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            status = {}
    except (OSError, json.JSONDecodeError):
        status = {}
    if state_error:
        status = {**status, "applied": False, "error": state_error}
    try:
        revision_matches = int(status.get("revision", -1)) == int(
            state.get("revision", 0)
        )
    except (TypeError, ValueError):
        revision_matches = False
        status = {**status, "applied": False, "error": "invalid audio status revision"}
    status["converged"] = bool(
        status.get("applied")
        and status.get("speaker") == selection["selected"]
        and revision_matches
    )
    try:
        shairport = SHAIRPORT_CONFIG_PATH.read_text(encoding="utf-8")
        configured = (
            "UGLAN-AIRPLAY-BEGIN" in shairport
            and 'ignore_volume_control = "yes"' in shairport
            and "airplay_volume_bridge.py --notify" in shairport
        )
    except OSError:
        configured = False
    shairport_active = (
        run(["systemctl", "is-active", "shairport-sync.service"], timeout=3)
        == "active"
    )
    bridge_active = (
        run(
            ["systemctl", "is-active", "airplay-volume-bridge.service"],
            timeout=3,
        )
        == "active"
    )
    try:
        spotify_dropin = SPOTIFY_VOLUME_DROPIN_PATH.read_text(encoding="utf-8")
        spotify_configured = (
            "librespot-uglan" in spotify_dropin
            and "LIBRESPOT_VOLUME_CTRL=fixed" in spotify_dropin
            and "--notify-spotify" in spotify_dropin
            and "UGLAN_SPOTIFY_VOLUME_SOCKET" in spotify_dropin
        )
    except OSError:
        spotify_configured = False
    try:
        bridge_status = json.loads(
            AIRPLAY_VOLUME_STATUS_PATH.read_text(encoding="utf-8")
        )
        live = bool(
            bridge_active
            and bridge_status.get("ok")
            and time.time() - float(bridge_status.get("updated_at", 0)) < 5
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        bridge_status, live = {}, False
    capability, available = iso226_capability()
    return {
        "state": state,
        "status": status,
        "volume_bridge": {
            "configured": configured or spotify_configured,
            "airplay_configured": configured,
            "airplay_service_active": shairport_active,
            "bridge_service_active": bridge_active,
            "spotify_configured": spotify_configured,
            "live": live,
            **bridge_status,
        },
        "iso226_capability": {"available": available, **capability},
        "speaker": speakers,
        "audio_path": str(audio_path),
    }


def write_audio_eq_state(raw: Any, *, expected_speaker: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("state must be an object")
    loudness = raw.get("loudness", {})
    if isinstance(loudness, dict) and loudness.get("enabled"):
        _capability, available = iso226_capability()
        if not available:
            raise ValueError(
                "install and verify the custom ISO 226 CamillaDSP engine first"
            )
    selection = read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )
    if expected_speaker is not None and expected_speaker != selection["selected"]:
        raise ValueError("speaker selection changed elsewhere; reload before saving")
    audio_path = _selected_audio_path(selection)
    with audio_state_lock(audio_path):
        latest = read_speaker_selection(
            SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
        )
        if latest != selection:
            raise ValueError("speaker selection changed elsewhere; reload before saving")
        current = read_audio_state(audio_path)
        incoming_revision = raw.get("revision", current["revision"])
        try:
            incoming_revision = int(incoming_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("revision must be an integer") from exc
        if incoming_revision != current["revision"]:
            raise ValueError("audio settings changed elsewhere; reload before saving")
        clean = normalize_audio_state(raw, revision=current["revision"] + 1)
        _backup_file(
            audio_path,
            AUDIO_EQ_BACKUP_DIR,
            f"audio-eq-{selection['selected']}",
        )
        atomic_write_json(audio_path, clean)
        return clean


def service_action(service: str, action: str) -> str:
    if service not in SERVICE_CATALOG:
        raise ValueError("service not allowed")
    controls = SERVICE_CATALOG[service]["controls"]
    if action not in controls:
        raise ValueError("action not allowed")

    if service == UI_SERVICE and action == "restart":
        unit = f"cdsp-control-ui-self-restart-{int(time.time())}"
        return run_checked(
            [
                "systemd-run",
                "--quiet",
                "--no-block",
                "--on-active=0.2",
                f"--unit={unit}",
                "systemctl",
                "restart",
                UI_SERVICE,
            ],
            timeout=5,
        )

    return run_checked(["systemctl", action, service], timeout=15)


def turn_amps_off() -> str:
    """Drop the trigger relay while leaving its automatic service running."""
    return run_checked(
        ["systemctl", "kill", "-s", "USR1", "cdsp-trigger.service"],
        timeout=5,
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "InstallationControl/0.3"

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/status":
            services = service_status()
            camilla = camilla_status()
            if "error" not in camilla:
                camilla["signal_db"] = camilla_levels().get("signal_db")
            self.send_json(
                {
                    "time": time.time(),
                    "services": services,
                    "camilla": camilla,
                    "source": source_status(camilla),
                    "speaker": speaker_payload(),
                    "remote": remote_status(services),
                }
            )
            return

        if parsed.path == "/api/storage":
            self.send_json(storage_status())
            return

        if parsed.path == "/api/levels":
            self.send_json(camilla_levels())
            return

        if parsed.path == "/api/audio":
            try:
                self.send_json(audio_eq_payload())
            except Exception as exc:
                self.send_json(
                    {"ok": False, "error": f"audio state unavailable: {exc}"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return

        if parsed.path == "/api/speaker":
            self.send_json(speaker_payload())
            return

        if parsed.path == "/api/logs":
            query = urllib.parse.parse_qs(parsed.query)
            unit = query.get("unit", ["camilladsp.service"])[0]
            if unit not in SERVICE_CATALOG:
                self.send_json(
                    {"ok": False, "error": "unit not allowed"}, HTTPStatus.BAD_REQUEST
                )
                return
            logs = run(["journalctl", "-u", unit, "--no-pager", "-n", "120"], timeout=8)
            self.send_json({"logs": logs})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/service":
                service = payload.get("service")
                action = payload.get("action")
                if not isinstance(service, str) or not isinstance(action, str):
                    raise ValueError("service and action are required")
                output = service_action(service, action)
                self.send_json({"ok": True, "output": output})
                return

            if parsed.path == "/api/camilla/volume":
                self.send_json({"ok": True, "camilla": set_camilla_volume(payload)})
                return

            if parsed.path == "/api/audio":
                state = payload.get("state")
                clean = write_audio_eq_state(
                    state, expected_speaker=payload.get("speaker")
                )
                self.send_json(
                    {"ok": True, "state": clean, "status": {"converged": False}}
                )
                return

            if parsed.path == "/api/speaker":
                self.send_json({"ok": True, "speaker": select_speaker(payload)})
                return

            if parsed.path == "/api/speaker/profile":
                self.send_json({"ok": True, **save_speaker_profile(payload)})
                return

            if parsed.path == "/api/speaker/profile/preview":
                self.send_json({"ok": True, **preview_speaker_crossover(payload)})
                return

            if parsed.path == "/api/amps/off":
                turn_amps_off()
                self.send_json({"ok": True})
                return

            if parsed.path == "/api/source":
                source = payload.get("source")
                if not isinstance(source, str):
                    raise ValueError("source is required")
                source = source.strip().lower()
                if source != "auto" and source not in SOURCE_CHOICES:
                    raise ValueError("source not allowed")
                write_source_override(source)
                run_checked(
                    ["systemctl", "start", "cdsp-source-switcher.service"], timeout=5
                )
                camilla = camilla_status()
                self.send_json(
                    {"ok": True, "source": source_status(camilla), "camilla": camilla}
                )
                return

            if parsed.path == "/api/storage":
                action = payload.get("action")
                if action == "mount":
                    run(["systemctl", "start", "mnt-whispers.automount"], timeout=8)
                    run_checked(
                        ["systemctl", "start", "mnt-whispers.mount"], timeout=20
                    )
                elif action == "unmount":
                    # Disable the autofs too so it stays unmounted for safe removal.
                    run(["systemctl", "stop", "mnt-whispers.automount"], timeout=10)
                    run(["systemctl", "stop", "mnt-whispers.mount"], timeout=15)
                    if os.path.ismount(str(MEDIA_ROOT)):
                        run_checked(["umount", str(MEDIA_ROOT)], timeout=15)
                else:
                    raise ValueError("action must be mount or unmount")
                self.send_json({"ok": True, "storage": storage_status()})
                return

            if parsed.path == "/api/time":
                epoch = payload.get("epoch")
                if not isinstance(epoch, (int, float)):
                    raise ValueError("epoch required")
                set_system_clock(int(float(epoch)))
                self.send_json({"ok": True, "now": time.time()})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"installation UI listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
