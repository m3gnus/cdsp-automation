#!/usr/bin/env python3
"""Small local web UI for the installation controller services."""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import struct
import urllib.parse
import zlib
from contextlib import contextmanager
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import yaml

import motu_volume
import source_volume
from settings import (
    AIRPLAY_VOLUME_STATUS_PATH,
    AUDIO_CONTROL_LOCK_PATH,
    AUDIO_EQ_BACKUP_DIR,
    AUDIO_EQ_PATH,
    AUDIO_EQ_STATUS_PATH,
    CAMILLA_BINARY,
    CDSP_HOST,
    CDSP_PORT,
    CONFIG_DIR,
    ENV_FILE,
    ISO226_CAPABILITY_PATH,
    SOURCE_BASE_DIR,
    SOURCE_OVERRIDE_PATH,
    SPEAKER_AUDIO_DIR,
    SPEAKER_GENERATED_DIR,
    SPEAKER_PROFILE_DIR,
    SPEAKER_SELECTION_PATH,
    SPEAKER_STATUS_PATH,
)
from audio_eq import (
    atomic_write_json,
    audio_state_lock,
    default_audio_state,
    normalize_audio_state,
    read_audio_state,
)
from speaker_config import (
    compile_profile_config,
    identify_managed_config,
    load_profile,
    load_yaml_mapping,
    profile_catalog,
    require_config_volume_limit,
)
from speaker_profiles import (
    BUILTIN_SPEAKERS,
    DEFAULT_SPEAKER_ID,
    audio_control_lock,
    operator_configs_for_speaker,
    read_profile_audio_state,
    read_speaker_selection,
    resolve_profile_audio_path,
    speaker_selection_lock,
    update_speaker_selection,
    volume_ceiling,
)


# The historical bind address.  Kept as the fallback so an existing install
# that has never heard of INSTALLATION_UI_HOST does not silently lose its UI
# on upgrade; operators who want loopback-only set the env value to 127.0.0.1
# (reachable over an SSH tunnel) and restart cdsp-control-ui.service.
DEFAULT_UI_HOST = "0.0.0.0"
DEFAULT_UI_PORT = 8088
# Every state-changing request body is read only after Content-Length has been
# checked against this, so a hostile or malformed request cannot make the
# handler buffer an arbitrary amount of memory.  The largest legitimate body is
# an EQ state document, which is a few kilobytes.
MAX_REQUEST_BODY_BYTES = 262_144
# A client that opens a socket and then stalls must not hold a handler thread
# forever; socketserver applies this to the connection in setup().
REQUEST_TIMEOUT_SECONDS = 20.0


def ui_bind_host() -> str:
    """The configured bind address, falling back to the historical 0.0.0.0."""
    return os.environ.get("INSTALLATION_UI_HOST", "").strip() or DEFAULT_UI_HOST


def ui_bind_port() -> int:
    raw = os.environ.get("INSTALLATION_UI_PORT", "").strip()
    if not raw:
        return DEFAULT_UI_PORT
    return int(raw)


def configured_ui_token() -> str:
    """The optional shared secret.  Empty means "behave exactly as before"."""
    return os.environ.get("INSTALLATION_UI_TOKEN", "").strip()


HOST = ui_bind_host()
PORT = ui_bind_port()
CDSP_ENV = ENV_FILE
CDSP_CONFIG_DIR = CONFIG_DIR
UI_SERVICE = "cdsp-control-ui.service"
# The page's fonts, served from the folder the installer puts next to this
# script.  A fixed list, so a request can never name any other file.
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILES = ("inter-tight.woff2", "ibm-plex-mono-400.woff2", "ibm-plex-mono-500.woff2", "syne.woff2")
# How long a MOTU volume change waits for the source switcher to apply it
# (normally within one of its ~1.2 s passes) before answering 202.
MOTU_VOLUME_REPLY_SECONDS = 3.0
BACKUP_KEEP = 15
DEFAULT_REMOTE_NAME = "HID Remote01 Keyboard"
CAMILLA_HOST = CDSP_HOST
CAMILLA_PORT = CDSP_PORT
SHAIRPORT_CONFIG_PATH = Path("/etc/shairport-sync.conf")
SPOTIFY_VOLUME_DROPIN_PATH = Path(
    "/etc/systemd/system/raspotify.service.d/cdsp-volume-sync.conf"
)
# Name shown in the page title and header.
SITE_NAME = os.environ.get("SITE_NAME", "CamillaDSP")
VOLUME_MIN_DB = -80.0
# There is deliberately no VOLUME_MAX_DB here. The UI has no ceiling of its
# own: it honours whichever ceiling the switcher verified into service, and
# falls back to the most restrictive one when that answer is unavailable.

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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{{site}} — audio control</title>
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/icon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <meta name="theme-color" content="#f3e7d8" media="(prefers-color-scheme: light)">
  <meta name="theme-color" content="#170807" media="(prefers-color-scheme: dark)">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="{{site}}">
  <style>
    /* Amber instrument: warm paper, ink rules, halftone meter (after the vist.audio amber theme).
       Fonts: the Pi's own copy first (/fonts/, installed next to this script), Google's copy if
       that is missing, and the system fonts in the stacks below if neither loads. */
    @font-face { font-family: "Inter Tight"; font-style: normal; font-weight: 300 600; font-display: swap;
      src: url("/fonts/inter-tight.woff2") format("woff2"), url("https://fonts.gstatic.com/s/intertight/v9/NGSwv5HMAFg6IuGlBNMjxLsH8ag.woff2") format("woff2"); }
    @font-face { font-family: "IBM Plex Mono"; font-style: normal; font-weight: 400; font-display: swap;
      src: url("/fonts/ibm-plex-mono-400.woff2") format("woff2"), url("https://fonts.gstatic.com/s/ibmplexmono/v23/-F63fjptAgt5VM-kVkqdyU8n1i8q1w.woff2") format("woff2"); }
    @font-face { font-family: "IBM Plex Mono"; font-style: normal; font-weight: 500; font-display: swap;
      src: url("/fonts/ibm-plex-mono-500.woff2") format("woff2"), url("https://fonts.gstatic.com/s/ibmplexmono/v23/-F6qfjptAgt5VM-kVkqdyU8n3twJwlBFgg.woff2") format("woff2"); }
    @font-face { font-family: "Syne"; font-style: normal; font-weight: 700 800; font-display: swap;
      src: url("/fonts/syne.woff2") format("woff2"), url("https://fonts.gstatic.com/s/syne/v24/8vIH7w4qzmVxm2BL9A.woff2") format("woff2"); }
    :root {
      color-scheme: light;
      --bg: #f3e7d8;
      --panel: #f7efe4;
      --raised: #efe1cf;
      --ink: #2a1a10;
      --muted: #6f5644;
      --faint: #7a5f4c;
      --line: rgba(42,26,16,.14);
      --line2: rgba(42,26,16,.30);
      --rule: #2a1a10;
      --accent: #b0361b;
      --tone: #d9531f;
      --on-accent: #fbf3ea;
      --ok: #4b5530;
      --warn: #8a5300;
      --bad: #b0361b;
      --dot-off: rgba(42,26,16,.18);
      --code-bg: #2a1a10;
      --code-ink: #f3e7d8;
      --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      --sans: "Inter Tight", "Helvetica Neue", Arial, system-ui, sans-serif;
      --display: "Syne", "Arial Black", "Helvetica Neue", Arial, sans-serif;
      --radius: 12px;
      --tabbar: 0px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --bg: #170807; --panel: #1f0d0b; --raised: #2a1310;
        --ink: #f6e7dd; --muted: #c4a59d; --faint: #b5918a;
        --line: rgba(246,231,221,.13); --line2: rgba(246,231,221,.28); --rule: #f6e7dd;
        --accent: #ff5b3d; --tone: #ff7a45; --on-accent: #170807;
        --ok: #b5c47a; --warn: #f2a640; --bad: #ff6b52;
        --dot-off: rgba(246,231,221,.16); --code-bg: #0e0403; --code-ink: #f6e7dd;
      }
    }
    * { box-sizing: border-box; }
    html, body { overflow-x: clip; }  /* clip, not hidden: hidden would stop the header sticking */
    body {
      margin: 0; background: var(--bg); color: var(--ink);
      font: 15px/1.5 var(--sans); -webkit-font-smoothing: antialiased;
      -webkit-tap-highlight-color: transparent;
    }
    .wrap { min-height: 100vh; min-height: 100dvh; }
    button { font-family: inherit; }

    /* ---------- header ---------- */
    header {
      position: sticky; top: 0; z-index: 20;
      display: flex; align-items: center; justify-content: space-between; gap: 16px;
      padding: max(14px, env(safe-area-inset-top)) max(clamp(16px, 4vw, 36px), env(safe-area-inset-right)) 12px max(clamp(16px, 4vw, 36px), env(safe-area-inset-left));
      background: var(--bg); border-bottom: 1.5px solid var(--rule);
    }
    .brand { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
    .word { min-width: 0; }
    .word h1 { margin: 0; font: 800 20px/1 var(--display); letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .word h1 b { font-weight: 800; }
    .health { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; justify-content: flex-end; }
    .chip { display: inline-flex; align-items: center; gap: 7px; font: 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); white-space: nowrap; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line2); flex: none; }
    .dot.ok { background: var(--ok); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); }

    /* ---------- nav: pills in the header row on wide screens, a tab bar at the bottom on phones ---------- */
    nav { display: flex; gap: 2px; padding: 10px clamp(16px, 4vw, 36px); border-bottom: 1px solid var(--line); overflow-x: auto; scrollbar-width: none; }
    nav::-webkit-scrollbar { display: none; }
    nav button {
      appearance: none; border: 0; background: transparent; color: var(--ink); cursor: pointer; white-space: nowrap;
      padding: 9px 16px; border-radius: 999px; font: 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase;
    }
    nav button:hover { background: var(--raised); }
    nav button.active { background: var(--ink); color: var(--bg); }
    @media (min-width: 721px) { nav { position: sticky; top: 47px; z-index: 15; background: var(--bg); } }
    @media (max-width: 720px) {
      :root { --tabbar: calc(64px + env(safe-area-inset-bottom)); }
      nav {
        position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; padding: 0 0 env(safe-area-inset-bottom);
        display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0;
        background: var(--bg); border-top: 1px solid var(--line2); border-bottom: 0;
      }
      nav button { height: 64px; border-radius: 0; color: var(--muted); padding: 0 4px; }
      nav button:hover { background: transparent; }
      nav button.active { background: transparent; color: var(--ink); font-weight: 600; }
      nav button.active::before { content: "● "; color: var(--accent); }
      .health .chip + .chip { display: none; }
    }

    main { max-width: 1240px; margin: 0 auto; padding: clamp(16px, 3vw, 32px) max(clamp(16px, 4vw, 36px), env(safe-area-inset-right)) calc(60px + var(--tabbar)) max(clamp(16px, 4vw, 36px), env(safe-area-inset-left)); }
    section { display: none; }
    section.active { display: block; animation: fade .25s ease; }
    @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    @media (prefers-reduced-motion: reduce) { section.active { animation: none; } }

    .shead { display: flex; align-items: baseline; gap: 12px; margin: 34px 0 14px; padding-bottom: 8px; border-bottom: 1.5px solid var(--rule); }
    .shead:first-child { margin-top: 4px; }
    .shead .ix { font: 11px/1 var(--mono); color: var(--accent); letter-spacing: .06em; }
    .shead h2 { margin: 0; font: 700 19px/1.1 var(--display); letter-spacing: .02em; text-transform: uppercase; }
    .shead .rule { display: none; }

    .card { background: transparent; border: 1px solid var(--line2); border-radius: var(--radius); padding: 18px; min-width: 0; }
    .grid { display: grid; gap: 14px; }
    .g-auto { grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
    .hero { grid-template-columns: 1.4fr 1fr 1fr; }

    .tile { border-top: 1.5px solid var(--rule); padding: 12px 2px 4px; min-width: 0; }
    .cap { font: 11px/1.2 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 10px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .val { font-size: 19px; font-weight: 500; overflow-wrap: anywhere; }
    .val.sm { font-size: 16px; font-weight: 500; }
    .val.big { font: 400 30px/1.1 var(--mono); letter-spacing: -.02em; }
    .unit { font: 12px var(--mono); color: var(--muted); font-weight: 400; }
    .sub2 { font: 12px/1.4 var(--mono); color: var(--faint); margin-top: 8px; overflow-wrap: anywhere; }
    .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); } .faint { color: var(--faint); }

    .badge { display: inline-flex; align-items: center; gap: 6px; font: 10.5px/1 var(--mono); letter-spacing: .06em; padding: 5px 9px; border-radius: 999px; border: 1px solid var(--line2); color: var(--muted); text-transform: uppercase; }
    .badge.ok { color: var(--ok); border-color: currentColor; }
    .badge.warn { color: var(--warn); border-color: currentColor; }
    .badge.bad { color: var(--bad); border-color: currentColor; }

    /* level bar (kept for panels other than the remote) */
    .meter { height: 8px; border-radius: 999px; background: var(--dot-off); overflow: hidden; margin-top: 4px; }
    .meter > i { display: block; height: 100%; width: 0%; background: var(--tone); transition: width 260ms cubic-bezier(.2,.8,.2,1); }
    .meter.silent > i { opacity: .3; }

    /* buttons */
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    button.btn, .btn {
      appearance: none; cursor: pointer; font: 500 13px/1 var(--sans); min-height: 40px;
      padding: 10px 16px; border-radius: 999px; border: 1.5px solid var(--ink);
      color: var(--ink); background: transparent; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      transition: background .15s, color .15s, border-color .15s, transform .05s;
    }
    .btn:hover { background: var(--raised); }
    .btn:active { transform: translateY(1px); }
    .btn.primary { background: var(--ink); color: var(--bg); }
    .btn.primary:hover { background: var(--ink); opacity: .88; }
    .btn.on { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
    .btn.danger { border-color: var(--accent); color: var(--accent); }
    .btn.danger:hover { background: var(--accent); color: var(--on-accent); }
    .btn.ghost { border-color: var(--line2); }
    .btn.sm { padding: 7px 12px; min-height: 34px; font-size: 12px; }
    .btn:disabled { opacity: .4; cursor: not-allowed; }

    /* source grid (Remote · more controls) */
    .sources { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
    .btn.src { flex-direction: column; align-items: flex-start; border-radius: 12px; padding: 12px 14px; min-height: 60px; border-width: 1px; border-color: var(--line2); }
    .src .k { font: 10.5px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
    .src .n { font: 700 14px/1.2 var(--display); margin-top: 6px; }
    .src.on .k { color: inherit; }

    /* range / inputs */
    input[type=range] { -webkit-appearance: none; appearance: none; width: 100%; height: 28px; background: transparent; outline: none; cursor: pointer; }
    input[type=range]::-webkit-slider-runnable-track { height: 2px; background: var(--line2); }
    input[type=range]::-moz-range-track { height: 2px; background: var(--line2); }
    input[type=range]::-moz-range-progress { height: 2px; background: var(--tone); }
    input[type=range]::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 24px; height: 24px; margin-top: -11px; border-radius: 50%; background: var(--ink); border: 0; box-shadow: 0 0 0 4px var(--bg); }
    input[type=range]::-moz-range-thumb { width: 24px; height: 24px; border-radius: 50%; background: var(--ink); border: 0; box-shadow: 0 0 0 4px var(--bg); }
    input[type=number], input[type=text], select {
      width: 100%; padding: 9px 10px; border: 1px solid var(--line2); border-radius: 8px;
      background: var(--panel); color: var(--ink); font: 13px/1.2 var(--mono);
    }
    input:focus-visible, select:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    select { cursor: pointer; }

    .volume { display: grid; grid-template-columns: 1fr auto; gap: 12px 16px; align-items: center; }
    .volume .num { width: 92px; }

    /* per-source volume */
    .sv-grid { display: grid; grid-template-columns: minmax(70px,1fr) 92px 78px auto; gap: 6px 8px; align-items: center; }
    .sv-grid .h { font: 10.5px/1.2 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
    .sv-grid .num { width: 100%; }
    .sv-grid .live { color: var(--ok); font: 11px/1 var(--mono); }

    /* ---------- Remote ---------- */
    .home-wrap { display: grid; gap: 40px; }
    @media (min-width: 1000px) { .home-wrap { grid-template-columns: minmax(340px, 420px) minmax(0, 1fr); gap: 56px; align-items: start; } }
    .home {
      display: flex; flex-direction: column; justify-content: space-between; gap: 18px; width: 100%; max-width: 460px; margin: 0 auto;
      min-height: calc(100vh - 140px - var(--tabbar)); min-height: calc(100dvh - 140px - var(--tabbar));
    }
    @media (min-width: 1000px) { .home { min-height: 0; gap: 30px; } }
    .ht { display: grid; grid-template-columns: repeat(18, minmax(0, 1fr)); grid-template-rows: repeat(7, minmax(0, 1fr)); height: clamp(64px, 13vh, 108px); align-items: center; justify-items: center; }
    .ht i { display: block; width: 11px; height: 11px; border-radius: 50%; background: var(--dot-off); transform: scale(.2); transition: transform 90ms linear; }
    .ht i.on { background: var(--tone); }
    .ht i.hot { background: var(--accent); }
    .ht-axis { display: flex; justify-content: space-between; font: 10.5px/1 var(--mono); letter-spacing: .06em; color: var(--muted); margin-top: 8px; }
    .home-now { text-align: center; }
    .home-now .cap { justify-content: center; margin-bottom: 0; }
    .home-source { font: 800 clamp(24px, 8vw, 32px)/1.1 var(--display); letter-spacing: .02em; text-transform: uppercase; margin: 8px 0 0; overflow-wrap: anywhere; }
    .home-level { display: flex; justify-content: center; align-items: baseline; gap: 8px; margin-top: 10px; }
    .home-db { font: 400 clamp(36px, 11vw, 46px)/1 var(--mono); letter-spacing: -.02em; }
    .home-db.muted { color: var(--accent); text-decoration: line-through; text-decoration-thickness: 2px; }
    .home-level .sub2 { margin: 0; font-size: 12px; }
    .home-fader { padding: 0 6px; }
    .home-vol { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
    .big-btn {
      appearance: none; height: 60px; border-radius: 999px; border: 1.5px solid var(--ink); background: transparent; color: var(--ink);
      font: 300 30px/1 var(--sans); cursor: pointer; touch-action: manipulation; user-select: none; -webkit-user-select: none;
    }
    .big-btn:active { background: var(--raised); }
    .btn.home-mute { height: 60px; min-height: 60px; background: var(--ink); color: var(--bg); font-size: 15px; padding: 0 8px; }
    .btn.home-mute.is-muted { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
    .home-sources { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border: 1.5px solid var(--rule); border-radius: 12px; overflow: hidden; }
    .home-sources .cell {
      appearance: none; border: 0; background: transparent; color: var(--ink); cursor: pointer; text-align: left;
      min-height: 58px; padding: 0 14px; display: flex; align-items: center; justify-content: space-between; gap: 8px;
      border-top: 1px solid var(--line2); border-left: 1px solid var(--line2);
    }
    .home-sources .cell:nth-child(-n+2) { border-top: 0; }
    .home-sources .cell:nth-child(odd) { border-left: 0; }
    .home-sources .cell:last-child:nth-child(odd) { grid-column: span 2; }
    .home-sources .cell .n { font: 700 15px/1.15 var(--display); overflow-wrap: anywhere; }
    .home-sources .cell .k { font: 10.5px/1 var(--mono); letter-spacing: .04em; color: var(--muted); white-space: nowrap; }
    .home-sources .cell.on { background: var(--accent); color: var(--on-accent); }
    .home-sources .cell.on .k { color: inherit; }
    .src-cap { justify-content: space-between; }
    .auto-pill { appearance: none; cursor: pointer; border: 1px solid var(--line2); border-radius: 999px; background: transparent; color: var(--muted); font: 10.5px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; padding: 0 14px; min-height: 32px; }
    .auto-pill.on { border-color: var(--accent); color: var(--accent); }
    .home-hint { margin-top: 28px; }
    .home-sources .cell:disabled { opacity: .45; cursor: not-allowed; }
    .home-foot { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
    .home-motu { display: flex; align-items: center; gap: 10px; }
    .home-motu .btn { width: 44px; height: 44px; min-height: 44px; padding: 0; font-size: 18px; }
    .home-motu .val { font: 15px/1 var(--mono); min-width: 64px; text-align: center; }
    .linkish { appearance: none; background: none; border: 0; border-bottom: 1px solid currentColor; padding: 12px 0 2px; color: var(--accent); cursor: pointer; font: 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; }
    @media (display-mode: standalone) { .home-hint { display: none; } }
    .home-more .shead:first-child { margin-top: 0; }

    .diag-row { display: grid; grid-template-columns: 18px minmax(120px, 220px) 1fr; gap: 8px; align-items: baseline; padding: 6px 0; border-bottom: 1px solid var(--line); }

    /* ---------- Sound ---------- */
    .audio-layout { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(280px, .8fr); gap: 18px; }
    .eq-plot { width: 100%; min-height: 340px; display: block; touch-action: none; user-select: none; background: transparent; border: 1px solid var(--line); border-radius: 10px; }
    .eq-grid-line { stroke: var(--line); stroke-width: 1; }
    .eq-zero { stroke: var(--line2); stroke-width: 1.4; }
    .eq-curve { fill: none; stroke: var(--accent); stroke-width: 2.6; }
    .eq-band-curve { fill: none; stroke-width: 1.3; opacity: .5; }
    .eq-band-curve.selected { stroke-width: 2.2; opacity: .95; }
    .eq-band.selected { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
    .eq-fill { fill: url(#eqFill); opacity: .16; }
    .eq-handle { stroke: var(--bg); stroke-width: 3; cursor: grab; }
    .eq-handle:active { cursor: grabbing; }
    .eq-axis { fill: var(--muted); font: 12px var(--mono); }
    .eq-band-list { display: grid; gap: 8px; margin-top: 14px; }
    .eq-band { display: grid; grid-template-columns: auto minmax(105px,1fr) repeat(3,minmax(74px,.65fr)) auto; gap: 8px; align-items: end; padding: 10px 12px; border: 1px solid var(--line2); border-radius: 10px; }
    .eq-band label { color: var(--muted); font: 10.5px var(--mono); letter-spacing: .06em; text-transform: uppercase; }
    .eq-band label input, .eq-band label select { margin-top: 4px; width: 100%; }
    .eq-power { align-self: center; }
    .eq-region { font: 10px var(--mono); fill: var(--faint); letter-spacing: .04em; }
    .eq-region-bar { fill: var(--raised); stroke: var(--line); stroke-width: 1; }
    .eq-region-bar.alt { fill: var(--panel); }
    .eq-shade { fill: var(--tone); opacity: .12; }
    .eq-shade-edge { stroke: var(--tone); stroke-dasharray: 3 5; opacity: .6; }
    .eq-guide { margin-top: 14px; padding: 12px 14px; border: 1px solid var(--line2); border-radius: 10px; }
    .eq-guide-rows { display: grid; gap: 8px; margin-top: 8px; }
    .eq-guide-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .eq-guide-row > span { color: var(--muted); font-size: 11px; letter-spacing: .05em; text-transform: uppercase; min-width: 74px; }
    .eq-chip { border: 1px solid var(--line2); background: transparent; color: inherit; border-radius: 999px; padding: 4px 11px; font: inherit; font-size: 13px; cursor: pointer; }
    .eq-chip:hover { border-color: var(--muted); }
    .eq-chip.on { border-color: var(--accent); color: var(--accent); }
    .eq-guide-advice { margin-top: 10px; display: grid; gap: 6px; }
    .eq-guide-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 2px; }
    .audio-kv { display: grid; gap: 14px; align-content: start; }
    .audio-state { font: 12px var(--mono); }
    .speaker-summary { display: grid; gap: 10px; }
    .speaker-summary .active-name { font: 700 20px/1.15 var(--display); }
    .speaker-cell { display: grid; gap: 6px; }
    .speaker-cell > .btn { width: 100%; }
    .audio-notice { display: none; margin-bottom: 14px; border-color: var(--bad); }
    .audio-notice.show { display: flex; align-items: center; gap: 14px; }
    dialog.confirm-dialog {
      width: min(520px, calc(100vw - 28px)); padding: 0; color: var(--ink);
      border: 1.5px solid var(--rule); border-radius: 14px; background: var(--bg);
      box-shadow: 0 24px 80px rgba(0,0,0,.35);
    }
    dialog.confirm-dialog::backdrop { background: rgba(23,8,7,.55); backdrop-filter: blur(3px); }
    .confirm-body { padding: 22px; }
    .confirm-warning { margin: 14px 0; padding: 12px 14px; border: 1.5px solid var(--accent); border-radius: 10px; color: var(--ink); line-height: 1.5; }

    /* toggle switch */
    .switch { position: relative; display: inline-block; width: 42px; height: 24px; flex: none; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .switch span { position: absolute; inset: 0; background: var(--line2); border-radius: 999px; transition: .2s; }
    .switch span::before { content: ""; position: absolute; height: 18px; width: 18px; left: 3px; top: 3px; background: var(--bg); border-radius: 50%; transition: .2s; }
    .switch input:checked + span { background: var(--accent); }
    .switch input:checked + span::before { transform: translateX(18px); }

    /* action bar */
    .actionbar {
      position: sticky; bottom: var(--tabbar); margin-top: 14px; padding: 12px; z-index: 5;
      display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
      background: var(--bg); border: 1.5px solid var(--rule); border-radius: 12px;
    }
    .actionbar .spacer { flex: 1; }
    .spacer { flex: 1; }
    .pending { font: 11px/1 var(--mono); color: var(--accent); letter-spacing: .04em; }

    /* ---------- System ---------- */
    .svc-group { margin-bottom: 18px; }
    .svc-row { display: grid; grid-template-columns: minmax(0,1.4fr) auto auto; gap: 12px; align-items: center; padding: 12px 0; border-top: 1px solid var(--line); }
    .svc-row:first-child { border-top: 0; }
    .svc-name { min-width: 0; }
    .svc-name .l { font-weight: 500; font-size: 16px; }
    .svc-name .u { font: 10.5px/1.3 var(--mono); color: var(--faint); overflow-wrap: anywhere; }

    /* ---------- Logs ---------- */
    pre {
      white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.7 var(--mono);
      background: var(--code-bg); color: var(--code-ink); border-radius: 12px; padding: 16px 18px;
      max-height: 62vh; overflow: auto; margin: 0;
    }

    /* toast */
    #toast {
      position: fixed; left: 50%; bottom: calc(20px + var(--tabbar)); transform: translateX(-50%) translateY(20px);
      background: var(--ink); color: var(--bg); padding: 11px 18px; border-radius: 999px;
      font: 12px/1.3 var(--mono); z-index: 50; opacity: 0; transition: .25s; pointer-events: none; max-width: 90vw;
    }
    #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

    @media (max-width: 860px) {
      .hero { grid-template-columns: 1fr; }
      .audio-layout { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .val.big { font-size: 26px; }
      .volume { grid-template-columns: 1fr; }
      .volume .num { width: 100%; }
      .eq-band { grid-template-columns: auto 1fr 1fr; }
      .eq-band .eq-type { grid-column: span 2; }
      .svc-row { grid-template-columns: minmax(0,1fr) auto; }
      .svc-row > :last-child { grid-column: 1 / -1; }
    }
    @media (max-width: 720px) {
      header { padding-bottom: 10px; }
      main { padding-top: 14px; }
      .home { gap: 12px; }
      .ht { height: clamp(44px, 9vh, 96px); }
      .home-source { font-size: clamp(22px, 7vw, 30px); margin-top: 4px; }
      .home-level { margin-top: 6px; }
      .home-db { font-size: clamp(32px, 10vw, 44px); }
      .home-vol { margin-top: 8px; }
      .big-btn, .btn.home-mute { height: 54px; min-height: 54px; }
      .home-sources .cell { min-height: 50px; }
      .home-sources + * { margin: 0; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <div class="word">
          <h1><b>{{site}}</b></h1>
        </div>
      </div>
      <div class="health" id="health"></div>
    </header>

    <nav id="nav">
      <button class="active" data-tab="home">Remote</button>
      <button data-tab="audio">Sound</button>
      <button data-tab="services">System</button>
      <button data-tab="logs">Logs</button>
    </nav>

    <main>
      <section id="home" class="active">
        <div class="home-wrap">
          <div class="home">
            <div>
              <div class="ht" id="homeMeter" aria-hidden="true"></div>
              <div class="ht-axis"><span>−65</span><span id="homeSig">no input</span><span>−8 dBFS</span></div>
            </div>
            <div class="home-now">
              <div class="cap">Now playing</div>
              <div class="home-source" id="homeSource">—</div>
              <div class="home-level">
                <span class="home-db" id="homeVol">—</span>
                <span class="sub2" id="homeVolSub">dB</span>
              </div>
            </div>
            <div>
              <div class="home-fader"><input id="homeRange" type="range" min="-50" max="0" step="0.5" value="-40" aria-label="Volume"></div>
              <div class="home-vol">
                <button class="big-btn" data-home-vol="-1" aria-label="Volume down">−</button>
                <button class="btn home-mute" id="homeMute">Mute</button>
                <button class="big-btn" data-home-vol="1" aria-label="Volume up">+</button>
              </div>
            </div>
            <div>
              <div class="cap src-cap">Source <button class="auto-pill" id="homeAuto" data-home-auto aria-pressed="true">Auto</button></div>
              <div class="home-sources" id="homeSources"></div>
            </div>
            <div class="home-foot">
              <div class="home-motu">
                <span class="cap" style="margin:0">MOTU</span>
                <button class="btn" data-home-motu="-1" aria-label="MOTU down">−</button>
                <div class="val" id="homeMotu">—</div>
                <button class="btn" data-home-motu="1" aria-label="MOTU up">+</button>
              </div>
              <button class="linkish" id="homeAmps">Amps off</button>
            </div>
          </div>

          <div class="home-more" id="dashboard">
            <div class="shead"><span class="ix">01</span><h2>Controls</h2><span class="rule"></span></div>
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
                <div class="cap">MOTU main output <span id="motuBadge" class="badge">—</span></div>
                <div id="motuPanel"></div>
                <div class="sub2" id="motuCaption"></div>
              </div>
              <div class="card">
                <div class="cap">Per-source volume <span id="svBadge" class="badge">—</span></div>
                <div id="sourceVolumePanel"></div>
                <div class="sub2" id="sourceVolumeCaption"></div>
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
            <div class="sub2 home-hint">Tip: add this page to your home screen (Share → Add to Home Screen on iPhone, ⋮ → Add to Home screen on Android) to use it like an app.</div>
          </div>
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
            <div id="eqGuide" class="eq-guide"></div>
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
        <div class="shead"><span class="ix">01</span><h2>Status</h2><span class="rule"></span></div>
        <div class="grid g-auto" id="systemGrid" style="margin-bottom:12px"></div>
        <div class="shead"><span class="ix">02</span><h2>Health check</h2><span class="rule"></span></div>
        <div class="card" style="margin-bottom:12px">
          <div class="row"><button class="btn primary" id="runDiagnose">Run health check</button><span class="sub2" id="diagnoseSummary">Checks services, configs, the audio lock, the MOTU link, the remote and power. Takes a few seconds.</span></div>
          <div id="diagnoseReport"></div>
        </div>
        <div class="shead"><span class="ix">03</span><h2>Systemd services</h2><span class="rule"></span></div>
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
    let lastStatus = null;
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

    /* Volume slider taper. A range input is linear, which crams the loud end --
       where fine adjustment actually matters -- into a sliver: across -100..0 dB
       the step from -6 to -3 was 3% of the slider. Like a mixing-desk fader,
       the top TAPER_FINE_DB below the maximum gets TAPER_FINE_FRAC of the
       travel and the rest of the range shares what is left. Positions run
       0..TAPER_STEPS for a smooth drag; levels are snapped to the control's own
       step, so a slider never offers a value the device cannot hold. Every
       level round-trips exactly (level -> position -> level), so a slider
       rendered from the device's current level never implies a change. */
    const TAPER_STEPS = 1000, TAPER_FINE_DB = 24, TAPER_FINE_FRAC = 0.7;
    function taper(min, max, step) {
      const snap = db => Math.min(max, Math.max(min, Math.round(db / step) * step));
      const knee = max - TAPER_FINE_DB;
      const linear = !(knee > min);
      const kneePos = TAPER_STEPS * (1 - TAPER_FINE_FRAC);
      return {
        toDb(pos) {
          if (!(max > min)) return min;
          pos = Math.min(TAPER_STEPS, Math.max(0, Number(pos)));
          let db;
          if (linear) db = min + (max - min) * pos / TAPER_STEPS;
          else if (pos <= kneePos) db = min + (knee - min) * pos / kneePos;
          else db = knee + (max - knee) * (pos - kneePos) / (TAPER_STEPS - kneePos);
          return snap(db);
        },
        toPos(db) {
          if (!(max > min)) return 0;
          db = Math.min(max, Math.max(min, Number(db)));
          let pos;
          if (linear) pos = (db - min) / (max - min) * TAPER_STEPS;
          else if (db <= knee) pos = (db - min) / (knee - min) * kneePos;
          else pos = kneePos + (db - knee) / (max - knee) * (TAPER_STEPS - kneePos);
          return Math.round(pos);
        },
      };
    }
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
    /* The server only demands a token when INSTALLATION_UI_TOKEN is set.  The
       page is handed one through #token=... (so it never lands in a URL the
       server logs), keeps it in localStorage for later visits, and asks for it
       once if a state change comes back 401. */
    const TOKEN_KEY = "cdspControlToken";
    let controlToken = "";
    function storeToken(value) {
      controlToken = value;
      try { window.localStorage.setItem(TOKEN_KEY, value); } catch (e) { /* private mode */ }
    }
    (function loadToken() {
      let fromHash = "";
      try {
        fromHash = (new URLSearchParams((location.hash || "").replace(/^#/, "")).get("token") || "").trim();
      } catch (e) { fromHash = ""; }
      if (fromHash) {
        storeToken(fromHash);
        history.replaceState(null, "", location.pathname + location.search);
        return;
      }
      try { controlToken = window.localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { controlToken = ""; }
    })();
    async function api(path, options = {}, retried = false) {
      const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
      if (controlToken) headers["Authorization"] = `Bearer ${controlToken}`;
      const res = await fetch(path, { ...options, headers });
      const text = await res.text();
      const data = text ? JSON.parse(text) : {};
      if (res.status === 401 && !retried) {
        const entered = (window.prompt("This control UI requires its shared secret (INSTALLATION_UI_TOKEN):") || "").trim();
        if (entered) {
          storeToken(entered);
          return api(path, options, true);
        }
      }
      if (!res.ok) {
        const err = new Error(data.error || text || res.statusText);
        err.status = res.status; err.data = data;
        throw err;
      }
      return data;
    }

    /* ---------------- rendering ---------------- */
    function tile(cap, val, cls = "", sub = "") {
      return `<div class="tile"><div class="cap">${cap}</div>
        <div class="val ${cls}">${val}</div>${sub ? `<div class="sub2">${sub}</div>` : ""}</div>`;
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

    function updateSignal(db) {
      const lo = -65, hi = -8;
      const has = db != null && db > -200;
      const norm = has ? Math.max(0, Math.min(1, (db - lo) / (hi - lo))) : 0;
      signalTarget = norm;
      const txt = qs("#homeSig");
      if (txt) txt.textContent = has ? `${db.toFixed(1)} dBFS` : "no input";
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
      const volMax = c.volume_max_db != null ? Number(c.volume_max_db) : 0;
      const focus = document.activeElement;
      if (!qs("#volRange")) {
        qs("#volumePanel").innerHTML =
          `<div class="volume">
            <input id="volRange" data-volume type="range" min="${Math.min(-50, volMax)}" max="${volMax}" step="0.5" value="${vol}">
            <input id="volNum" class="num" data-volume type="number" min="${Math.min(-80, volMax)}" max="${volMax}" step="0.5" value="${vol.toFixed(1)}">
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
        qs("#volRange").min = Math.min(-50, volMax); qs("#volRange").max = volMax;
        qs("#volNum").min = Math.min(-80, volMax); qs("#volNum").max = volMax;
        if (focus !== qs("#volRange") && focus !== qs("#volNum")) {
          qs("#volRange").value = vol; qs("#volNum").value = vol.toFixed(1);
        }
        if (focus !== qs("#muteToggle")) qs("#muteToggle").checked = !!c.muted;
      }
    }

    /* ---------------- audio EQ ---------------- */
    const EQ_COLORS = ["#b0361b", "#2f6e9e", "#4b7a2a", "#8a4fa0", "#b86d0e", "#1d7f80", "#b8476f", "#6b6a1f"];
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
      svg.innerHTML = `<defs><linearGradient id="eqFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#d9531f"/><stop offset="1" stop-color="#d9531f" stop-opacity="0"/></linearGradient></defs>
        ${freqTicks.map(f => `<line class="eq-grid-line" x1="${eqX(f)}" x2="${eqX(f)}" y1="${EQ_Y0}" y2="${EQ_Y1}"/><text class="eq-axis" x="${eqX(f)}" y="342" text-anchor="middle">${f>=1000 ? (f/1000)+"k" : f}</text>`).join("")}
        ${gainTicks.map(g => `<line class="${g===0 ? "eq-zero" : "eq-grid-line"}" x1="${EQ_X0}" x2="${EQ_X1}" y1="${eqY(g)}" y2="${eqY(g)}"/><text class="eq-axis" x="45" y="${eqY(g)+4}" text-anchor="end">${g>0?"+":""}${g}</text>`).join("")}
        ${eqGuideShade()}${eqRegionStrip()}
        ${bandCurves}${markers}
        <path class="eq-fill" d="${fill}"/><path class="eq-curve" d="${curve}"/>
        ${section.bands.map((b,i) => {const y=eqY(eqSupportsGain(b)?b.gain:0);return `<g opacity="${b.enabled && section.enabled && (eqSoloBand==null||eqSoloBand===i) ? 1 : .25}"><circle class="eq-handle" data-eq-handle="${i}" cx="${eqX(b.freq)}" cy="${y}" r="${selectedEqBand===i?14:12}" fill="${EQ_COLORS[i%EQ_COLORS.length]}"/><text x="${eqX(b.freq)}" y="${y+4}" text-anchor="middle" fill="#fbf3ea" font-size="11" font-weight="700">${i+1}</text></g>`;}).join("")}`;
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

    /* ---------------- listening guide ----------------
       Starting points, not diagnoses: each chip names a search range and one
       bell to try there, which the listener then moves by ear. Ranges and
       settings merge two independent reviews of common practice. */
    const EQ_REGIONS = [["Sub-bass",20,60],["Bass",60,250],["Low mids",250,500],["Mids",500,2000],["Presence",2000,6000],["Brilliance",6000,12000],["Air",12000,20000]];
    const EQ_GUIDE = [
      {id:"boomy", label:"Boomy", more:true, lo:60, hi:150, freq:100, gain:-3, q:4,
       tip:"Usually a room resonance: one or two notes ring on. Sweep the band slowly through the range, stop where the boom is worst, and keep it narrow."},
      {id:"muddy", label:"Muddy", more:true, lo:180, hi:400, freq:250, gain:-3, q:1.4,
       tip:"Bass and lower voices blur together. A gentle cut here often also cures “dull”."},
      {id:"boxy", label:"Boxy", more:true, lo:350, hi:800, freq:500, gain:-2.5, q:1.8,
       tip:"Sounds like it comes from inside a box or cabinet. Close to muddy; try both."},
      {id:"honky", label:"Honky / nasal", more:true, lo:700, hi:1600, freq:1000, gain:-2.5, q:2,
       tip:"Voices sound pinched, like through a cardboard tube or a telephone."},
      {id:"harsh", label:"Harsh", more:true, lo:2000, hi:5000, freq:3000, gain:-2, q:1.6,
       tip:"Glare and listening fatigue live here, lower than most people expect. Small cuts go a long way."},
      {id:"piercing", label:"Piercing", more:true, lo:3500, hi:7000, freq:5000, gain:-2.5, q:2.5,
       tip:"Sharp, metallic edge on cymbals and strings. Overlaps harsh: if this does little, try harsh."},
      {id:"sibilant", label:"Sibilant", more:true, lo:5000, hi:9000, freq:6500, gain:-3, q:3,
       tip:"Hissing “s” and “sh” sounds. Often in the recording itself; a fixed cut dulls everything else, so keep it narrow and modest."},
      {id:"thin", label:"Thin", more:false, lo:80, hi:250, freq:150, gain:2, q:0.9,
       tip:"Lacks body and warmth. Too much 3–5 kHz sounds thin too, so try a harsh cut first."},
      {id:"hollow", label:"Hollow", more:false, lo:300, hi:800, freq:500, gain:2, q:1.2,
       tip:"Often a dip from speaker or room cancellation, which boosting cannot fill. If the boost barely helps, take it out again."},
      {id:"dull", label:"Dull / laid back", more:false, lo:3000, hi:6000, freq:4000, gain:2, q:0.9,
       tip:"Lacks presence and detail. Can also be too much low mids: try a muddy cut first."},
      {id:"air", label:"No air", more:false, lo:10000, hi:16000, freq:12000, gain:2, q:0.7,
       tip:"Cymbals lack shimmer and space. Subtle by nature; only audible if the speakers and your ears reach up there."},
    ];
    const EQ_TRIAL_ID = "guide_trial";
    let eqGuideChip = null;
    const eqFmtHz = f => f >= 1000 ? `${+(f/1000).toFixed(1)} kHz` : `${Math.round(f)} Hz`;
    const eqGuideItem = () => EQ_GUIDE.find(g => g.id === eqGuideChip) || null;
    const eqTrialIndex = () => audioState ? audioState.bands.findIndex(b => b.id === EQ_TRIAL_ID) : -1;

    function eqRegionStrip() {
      return EQ_REGIONS.map(([name, lo, hi], i) => {
        const x0 = eqX(lo), x1 = eqX(hi), mid = (x0 + x1) / 2, wide = x1 - x0 > name.length * 6.4 + 6;
        return `<rect class="eq-region-bar ${i % 2 ? "alt" : ""}" x="${x0}" y="2" width="${x1 - x0}" height="15"/>` +
          (wide ? `<text class="eq-region" x="${mid}" y="13" text-anchor="middle">${name}</text>` : "");
      }).join("");
    }

    function eqGuideShade() {
      const g = eqGuideItem(); if (!g) return "";
      const x0 = eqX(g.lo), x1 = eqX(g.hi);
      return `<rect class="eq-shade" x="${x0}" y="${EQ_Y0}" width="${x1 - x0}" height="${EQ_Y1 - EQ_Y0}"/>` +
        `<line class="eq-shade-edge" x1="${x0}" x2="${x0}" y1="${EQ_Y0}" y2="${EQ_Y1}"/><line class="eq-shade-edge" x1="${x1}" x2="${x1}" y1="${EQ_Y0}" y2="${EQ_Y1}"/>`;
    }

    function renderEqGuide() {
      const host = qs("#eqGuide"); if (!host || !audioState) return;
      const chips = more => EQ_GUIDE.filter(g => g.more === more).map(g =>
        `<button class="eq-chip ${eqGuideChip === g.id ? "on" : ""}" data-eq-guide="${g.id}" aria-pressed="${eqGuideChip === g.id}">${g.label}</button>`).join("");
      const g = eqGuideItem(), trial = eqTrialIndex(), full = trial < 0 && audioState.bands.length >= 16;
      let advice = "";
      if (g) {
        const sign = g.gain > 0 ? "+" : "−";
        advice = `<div class="eq-guide-advice">
          <div><b>${g.label}</b> · ${eqFmtHz(g.lo)}–${eqFmtHz(g.hi)} · try ${g.gain > 0 ? "a boost" : "a cut"} of ${sign}${Math.abs(g.gain)} dB at ${eqFmtHz(g.freq)}, Q ${g.q}</div>
          <div class="sub2">${g.tip}</div>
          <div class="eq-guide-actions">
            <button class="btn sm" id="eqGuideTry" ${full ? "disabled" : ""}>${trial >= 0 ? "Move trial band here" : "Try it"}</button>
            ${full ? `<span class="sub2">All 16 bands are in use: delete one first.</span>` : ""}
          </div></div>`;
      }
      let trialRow = "";
      if (trial >= 0) {
        const b = audioState.bands[trial];
        trialRow = `<div class="eq-guide-advice"><div class="sub2">Trial band ${trial + 1}: ${b.type} ${eqFmtHz(b.freq)}${eqSupportsGain(b) ? ` ${b.gain > 0 ? "+" : ""}${b.gain} dB` : ""}, Q ${b.q}. Drag it on the plot until it sounds right, and switch it off and on to compare.</div>
          <div class="eq-guide-actions">
            <button class="btn sm ghost" id="eqGuideCompare">${b.enabled ? "Bypass to compare" : "Turn back on"}</button>
            <button class="btn sm" id="eqGuideKeep">Keep</button>
            <button class="btn sm ghost" id="eqGuideDiscard">Discard</button>
          </div></div>`;
      }
      host.innerHTML = `<div class="cap">Sounds… <span class="sub2" style="text-transform:none;letter-spacing:0">pick what you hear for a starting point</span></div>
        <div class="eq-guide-rows">
          <div class="eq-guide-row"><span>Too much</span>${chips(true)}</div>
          <div class="eq-guide-row"><span>Too little</span>${chips(false)}</div>
        </div>${advice}${trialRow}
        <div class="sub2" style="margin-top:10px">Change one thing at a time and keep moves small (2–3 dB). Boosting is safe: automatic headroom lowers the level to match.</div>`;
      qsa("[data-eq-guide]").forEach(el => el.addEventListener("click", () => {
        eqGuideChip = eqGuideChip === el.dataset.eqGuide ? null : el.dataset.eqGuide; renderEqPlot(); renderEqGuide();
      }));
      const tryBtn = qs("#eqGuideTry");
      if (tryBtn) tryBtn.addEventListener("click", () => {
        const item = eqGuideItem(); if (!item) return;
        let i = eqTrialIndex();
        if (i < 0) {
          if (audioState.bands.length >= 16) { toast("A maximum of 16 EQ bands is supported."); return; }
          audioState.bands.push({id: EQ_TRIAL_ID, enabled: true, type: "Peaking", freq: item.freq, gain: item.gain, q: item.q});
          i = audioState.bands.length - 1;
        } else {
          Object.assign(audioState.bands[i], {enabled: true, type: "Peaking", freq: item.freq, gain: item.gain, q: item.q});
        }
        selectedEqBand = i; renderAudioEditor(); scheduleAudioSave();
      });
      const cmp = qs("#eqGuideCompare");
      if (cmp) cmp.addEventListener("click", () => {
        const b = audioState.bands[eqTrialIndex()]; if (!b) return;
        b.enabled = !b.enabled; renderAudioEditor(); scheduleAudioSave();
      });
      const keep = qs("#eqGuideKeep");
      if (keep) keep.addEventListener("click", () => {
        const b = audioState.bands[eqTrialIndex()]; if (!b) return;
        b.id = `band_${Date.now().toString(36)}`; eqGuideChip = null; renderAudioEditor(); scheduleAudioSave();
      });
      const discard = qs("#eqGuideDiscard");
      if (discard) discard.addEventListener("click", () => {
        const i = eqTrialIndex(); if (i < 0) return;
        audioState.bands.splice(i, 1);
        if (eqSoloBand === i) eqSoloBand = null; else if (eqSoloBand > i) eqSoloBand--;
        if (selectedEqBand != null && selectedEqBand >= audioState.bands.length) selectedEqBand = audioState.bands.length - 1;
        renderAudioEditor(); scheduleAudioSave();
      });
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

    // Explains where the automatic preamp comes from: the highest point of the
    // whole chain (user EQ plus speaker-profile filters), not this plot alone.
    function eqHeadroomNote(h) {
      if (!audioState.auto_headroom) return " Automatic headroom is off: only the manual preamp applies.";
      if (!h || h.peak_db == null) return " Automatic headroom keeps the loudest point of the whole chain (this EQ plus the speaker profile's filters) below full scale.";
      const peak = Number(h.peak_db), where = h.peak_hz != null && h.output_channel != null
        ? ` at ${h.peak_hz >= 1000 ? (h.peak_hz/1000).toFixed(1)+" kHz" : Math.round(h.peak_hz)+" Hz"} on output ${Number(h.output_channel)+1}` : "";
      const missing = (h.not_counted || []).length
        ? ` <span class="warn">Not counted: ${(h.not_counted).map(esc).join("; ")}.</span>` : "";
      return ` Whole-chain peak ${peak >= 0 ? "+" : ""}${peak.toFixed(1)} dB${where}, kept ${Number(h.margin_db ?? 0).toFixed(1)} dB below full scale.${missing}`;
    }

    function renderAudioEditor(status = {}) {
      if (!audioState) return;
      renderEqPlot(); renderEqBands(); renderEqGuide();
      qs("#eqGlobal").innerHTML = `<div class="cap">Global EQ</div>
        <label class="row" style="margin-top:12px"><span class="switch"><input id="eqEnabled" type="checkbox" ${audioState.enabled?"checked":""}><span></span></span> Enabled</label>
        <label class="row" style="margin-top:12px"><span class="switch"><input id="eqHeadroom" type="checkbox" ${audioState.auto_headroom?"checked":""}><span></span></span> Automatic headroom</label>
        <label style="display:block;margin-top:14px" class="sub2">Headroom margin (dB)<input id="eqMargin" type="number" min="0" max="6" step="0.5" value="${audioState.headroom_margin_db ?? 1}" ${audioState.auto_headroom?"":"disabled"} style="margin-top:5px"></label>
        <label style="display:block;margin-top:14px" class="sub2">Manual preamp (dB)<input id="eqPreamp" type="number" min="-24" max="0" step="0.5" value="${audioState.preamp_db}" style="margin-top:5px"></label>
        <div class="sub2" style="margin-top:10px">Effective preamp: <b>${status.effective_preamp_db != null ? Number(status.effective_preamp_db).toFixed(1) : "pending"} dB</b>.${eqHeadroomNote(status.headroom)}</div>`;
      qs("#eqEnabled").addEventListener("change", e => { audioState.enabled=e.target.checked; renderEqPlot(); scheduleAudioSave(); });
      qs("#eqHeadroom").addEventListener("change", e => { audioState.auto_headroom=e.target.checked; qs("#eqMargin").disabled=!e.target.checked; scheduleAudioSave(); });
      qs("#eqMargin").addEventListener("input", e => { audioState.headroom_margin_db=Number(e.target.value); scheduleAudioSave(); });
      qs("#eqPreamp").addEventListener("input", e => { audioState.preamp_db=Number(e.target.value); scheduleAudioSave(); });
      const spotifyBridge=audioBridge.spotify||{};
      const airplayReady=audioBridge.airplay_configured&&audioBridge.airplay_service_active===true&&audioBridge.live;
      const spotifyReady=audioBridge.spotify_configured&&audioBridge.live&&spotifyBridge.command_socket===true;
      const spotifyIdle=audioBridge.spotify_configured&&audioBridge.live&&spotifyBridge.receiver_socket===true&&!spotifyReady;
      const spotifyLabel=!audioBridge.spotify_configured?"Spotify sync not configured":spotifyReady?"Spotify ↔ master live":spotifyIdle?"Spotify ready · waiting for sender":"Spotify receiver unavailable";
      qs("#volumeArchitecture").innerHTML = `<div class="cap">Volume architecture</div><div class="val sm" style="margin-top:8px">One shared CamillaDSP master</div>
        <div class="sub2">Web UI, HID remote and network players change the same fader. The MOTU main output scales everything after it (loudness compensation follows it too); amplifier gains remain calibration stages.</div>
        <div class="row" style="margin-top:12px"><span class="badge ${airplayReady?"ok":"warn"}">${audioBridge.airplay_configured?(airplayReady?"AirPlay → master live":"AirPlay receiver unavailable"):"AirPlay sync not configured"}</span><span class="badge ${spotifyReady?"ok":"warn"}">${spotifyLabel}</span></div>
        <div class="sub2" style="margin-top:8px">Both receivers pass audio at unity. Spotify Connect mirrors changes in both directions once a sender connects; until then its receiver remains ready but volume sync is idle. AirPlay source changes control the master, but AirPlay cannot update the sender’s exact slider value when volume is changed here.</div>`;
      const l=audioState.loudness;
      qs("#loudnessPlan").innerHTML = `<div class="cap">ISO 226 loudness</div><div class="val sm" style="margin-top:8px">Fader-linked calibration</div>
        <label class="row" style="margin-top:12px"><span class="switch"><input data-loudness-check="enabled" type="checkbox" ${l.enabled?"checked":""} ${audioCapability.available?"":"disabled"}><span></span></span> Enabled</label>
        <div class="sub2" style="margin-top:8px">Set the master where you normally listen, play a 1 kHz sine, and enter the SPL you measure at the listening position. Phon equals SPL only at 1 kHz — a reading taken on music or noise will not give the right number. Compensation then follows the Main fader inside CamillaDSP and, once its calibration level is set, the MOTU main output.</div>
        <label class="sub2" style="display:block;margin-top:12px">Reference listening level (phon)<input data-loudness="reference_phon" type="number" min="40" max="90" step="1" value="${l.reference_phon}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Reference master volume (dB)<input data-loudness="reference_volume_db" type="number" min="-60" max="0" step="0.5" value="${l.reference_volume_db}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">MOTU main output at calibration (dB)<input id="loudnessMotu" type="number" min="-100" max="0" step="1" placeholder="not included" value="${l.reference_motu_db ?? ""}" style="margin-top:4px"></label>
        <div class="row" style="margin-top:8px"><button class="btn sm" id="loudnessUseCurrent" type="button">Use current levels</button><span class="sub2" id="loudnessMotuNote">${loudnessMotuNote(l)}</span></div>
        <label class="sub2" style="display:block;margin-top:10px">Strength<input data-loudness="strength" type="range" min="0" max="1" step="0.05" value="${l.strength}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Maximum bass boost (dB)<input data-loudness="max_bass_boost_db" type="number" min="0" max="18" step="0.5" value="${l.max_bass_boost_db}" style="margin-top:4px"></label>
        <label class="sub2" style="display:block;margin-top:10px">Maximum treble boost (dB)<input data-loudness="max_treble_boost_db" type="number" min="0" max="12" step="0.5" value="${l.max_treble_boost_db}" style="margin-top:4px"></label>
        <div class="row" style="margin-top:12px"><span class="badge ${l.enabled?"ok":audioCapability.available?"":"warn"}">${l.enabled?"Custom DSP engine active":audioCapability.available?"Ready when enabled":"Install custom DSP engine first"}</span></div>`;
      qsa("[data-loudness]").forEach(el => el.addEventListener("input", e => { audioState.loudness[e.target.dataset.loudness]=Number(e.target.value); scheduleAudioSave(); }));
      qsa("[data-loudness-check]").forEach(el => el.addEventListener("change", e => { audioState.loudness[e.target.dataset.loudnessCheck]=e.target.checked; scheduleAudioSave(); }));
      // Empty means "leave the MOTU out", the behaviour before it was followed.
      qs("#loudnessMotu").addEventListener("change", e => {
        const raw = e.target.value.trim();
        audioState.loudness.reference_motu_db = raw === "" ? null : Number(raw);
        qs("#loudnessMotuNote").textContent = loudnessMotuNote(audioState.loudness);
        scheduleAudioSave();
      });
      qs("#loudnessUseCurrent").addEventListener("click", () => {
        const c = (live && live.camilla && live.camilla.ok) ? live.camilla : (lastStatus?.camilla || {});
        if (c.volume_db == null) { toast("The CamillaDSP volume is not known yet."); return; }
        audioState.loudness.reference_volume_db = Math.round(Number(c.volume_db) * 2) / 2;
        audioState.loudness.reference_motu_db = (motu && motu.known) ? Number(motu.volume_db) : null;
        if (!(motu && motu.known)) toast("MOTU level unknown: calibrating on the fader only.");
        renderAudioEditor(); scheduleAudioSave();
      });
      const stateEl=qs("#eqApplyState");
      stateEl.textContent = status.error ? `error · ${status.error}` : (status.converged ? `live · revision ${audioState.revision}` : `applying · revision ${audioState.revision}`);
      stateEl.style.color = status.error ? "var(--bad)" : (status.converged ? "var(--ok)" : "var(--warn)");
    }

    function loudnessMotuNote(l) {
      if (l.reference_motu_db == null) return "MOTU changes are not compensated.";
      if (!(motu && motu.known)) return "Follows the MOTU; its level is unknown right now.";
      const offset = Number(motu.volume_db) - Number(l.reference_motu_db);
      return offset === 0 ? "MOTU at its calibration level." : `MOTU ${offset > 0 ? "+" : ""}${offset.toFixed(0)} dB from calibration: compensation adjusted.`;
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
      const error=status.ok===false?`<div class="sub2" style="color:var(--bad);margin-top:10px">${esc(status.error||"Transition failed — output stays muted")}</div>`:"";
      const profilePanel=qs("#speakerProfiles");
      if (profilePanel) profilePanel.innerHTML=`<div class="cap">Complete DSP profile</div><div class="sources" style="margin-top:10px">${cards}</div>
        <div class="sub2" style="margin-top:10px">Selected: <b>${esc(selected.label||selection.selected)}</b> · revision ${selection.revision||0}. Profiles are installed definitions and may contain FIR crossovers or custom filter chains. Selecting a target only opens the confirmation dialog. Swap passive speakers only after it shows active. User EQ and loudness are stored separately for every profile.${selected.bypass_user_eq?" This profile deliberately bypasses user EQ.":""}</div>${error}`;
      const dashboard=qs("#dashboardSpeaker");
      if (dashboard) {
        const active=selected.available&&status.applied===selection.selected&&status.ok&&speakerState.ready;
        // The 5s status poll re-renders this panel; keep a target the operator
        // already picked instead of snapping the list back to the live profile.
        const pendingTarget=qs("#dashboardSpeakerTarget")?.value;
        const options=Object.values(catalog).map(p=>`<option value="${esc(p.id)}" ${p.id===selection.selected?"selected":""} ${p.available?"":"disabled"}>${esc(p.label||p.id)}${p.available?"":" — unavailable"}</option>`).join("");
        dashboard.innerHTML=`<div class="active-name">${esc(selected.label||selection.selected||"—")}</div><div><span class="badge ${active?"ok":"warn"}">${active?"active":"switching"}</span></div><select id="dashboardSpeakerTarget" aria-label="Target speaker profile">${options}</select><button class="btn danger" id="dashboardSpeakerChange" disabled>Review profile change…</button><div class="sub2">Changing this is intentionally a confirmed maintenance action.</div>${error}`;
        const target=qs("#dashboardSpeakerTarget"), change=qs("#dashboardSpeakerChange");
        if (pendingTarget && catalog[pendingTarget]?.available) target.value=pendingTarget;
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
      // Re-arm the button on failure; the dialog stays open for another try.
      finally { apply.textContent="Mute and switch profile"; apply.disabled=qs("#speakerConfirmText").value.trim()!=="SWITCH"; }
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

    /* ---------------- MOTU main volume ----------------
       The MOTU's own output level, after CamillaDSP.  The control starts only
       from a level the server read off the device (never a default), and each
       write names the level it expects to replace, so it cannot jump.  The
       MOTU serves one client at a time, the source switcher: the server hands
       it each request and reports the level it publishes.  Slider moves are
       debounced here, and only the latest value is sent. */
    let motu = null, motuTarget = null, motuTimer = null, motuBusy = false, motuPoll = null;
    const MOTU_DEBOUNCE_MS = 700;
    // The MOTU main trim is whole dB only (a device limit), so every MOTU control
    // steps by 1 dB and never offers a finer level it cannot hold.
    const MOTU_STEP_DB = 1;
    let motuTaper = null;
    const motuDb = v => v == null ? "unknown" : (v <= -100 ? "−∞" : `${v.toFixed(0)}`);
    function renderMotu() {
      const panel = qs("#motuPanel"), badge = qs("#motuBadge");
      if (!panel) return;
      const known = !!(motu && motu.known);
      const writable = known && motu.writable && motu.max_db != null;
      badge.textContent = !known ? "unknown" : (motu.confirmed ? "read" : "sent");
      badge.className = "badge " + (!known ? "bad" : (writable ? "ok" : "warn"));
      if (!known) {
        panel.innerHTML = `<div class="val sm bad">unknown</div>
          <div class="row" style="margin-top:10px"><button class="btn sm" id="motuRead">Read again</button></div>`;
        qs("#motuRead").addEventListener("click", loadMotu);
      } else if (!qs("#motuRange") || motuTarget == null) {
        // Rebuilt only while no drag is pending, so a reply never yanks the
        // slider out from under the operator's finger.
        const max = writable ? motu.max_db : motu.volume_db;
        const shown = Math.min(motu.volume_db, max);
        motuTaper = taper(motu.min_db, max, MOTU_STEP_DB);
        const dis = writable ? "" : "disabled";
        panel.innerHTML = `<div class="volume">
            <input id="motuRange" type="range" min="0" max="${TAPER_STEPS}" step="1" value="${motuTaper.toPos(shown)}" ${dis}>
            <div class="val sm" id="motuValue">${motuDb(shown)}<span class="unit"> dB</span></div>
          </div>
          <div class="row" style="margin-top:10px">
            <button class="btn sm" data-motu-nudge="-1" ${dis}>−1 dB</button>
            <input id="motuNum" class="num" type="number" min="${motu.min_db}" max="${max}" step="${MOTU_STEP_DB}" value="${shown.toFixed(0)}" ${dis}>
            <button class="btn sm" data-motu-nudge="1" ${dis}>+1 dB</button>
            <button class="btn sm" id="motuRead">Read again</button>
          </div>`;
        qs("#motuRange").addEventListener("input", e => motuSetTarget(motuTaper.toDb(e.target.value), "slider"));
        // change, not input: typing "-3" passes through "-", which is not a level.
        qs("#motuNum").addEventListener("change", e => motuSetTarget(Number(e.target.value), "number"));
        qsa("[data-motu-nudge]").forEach(b => b.addEventListener("click", () =>
          motuSetTarget((motuTarget ?? motu.volume_db) + MOTU_STEP_DB * Number(b.dataset.motuNudge), "nudge")));
        qs("#motuRead").addEventListener("click", loadMotu);
      }
      renderMotuCaption();
    }
    function renderMotuCaption() {
      const cap = qs("#motuCaption"); if (!cap) return;
      const known = !!(motu && motu.known);
      const notes = [];
      if (known) notes.push(`device at <b>${motuDb(motu.volume_db)} dB</b>${motu.confirmed ? "" : " (sent, not yet read back)"}`);
      if (known && motu.max_db != null) notes.push(`ceiling ${motu.max_db.toFixed(0)} dB (MOTU_MAIN_VOLUME_MAX_DB)`);
      if (known && motu.max_db != null && motu.volume_db > motu.max_db) notes.push(`<span class="warn">above the ceiling</span>`);
      if (motuTarget != null) {
        notes.push(`<span class="warn">${motuDb(motuTarget)} dB pending</span>`);
      }
      if (motu && motu.reason) notes.push(`<span class="bad">${esc(motu.reason)}</span>`);
      cap.innerHTML = notes.join(" · ");
    }
    async function loadMotu() {
      try {
        motu = (await api("/api/motu/volume")).motu;
      }
      catch (e) { motu = null; toast(e.message); }
      renderMotu();
    }
    /* The one way a MOTU level is chosen, from the slider, the number box or
       the -1/+1 buttons. Nudges step from the pending target when there is one,
       so three quick +1 taps from -6 dB become a single write of -3 dB. Each
       control is kept in step with the others, and the write goes through the
       same debounce as a drag. */
    function motuSetTarget(db, source) {
      if (!motu || !motu.known || !motu.writable || !motuTaper || !Number.isFinite(db)) return;
      db = Math.min(motu.max_db, Math.max(motu.min_db, Math.round(db / MOTU_STEP_DB) * MOTU_STEP_DB));
      motuTarget = db;
      if (source !== "slider") { const r = qs("#motuRange"); if (r) r.value = motuTaper.toPos(db); }
      if (source !== "number") { const n = qs("#motuNum"); if (n) n.value = db.toFixed(0); }
      const label = qs("#motuValue"); if (label) label.innerHTML = `${motuDb(db)}<span class="unit"> dB</span>`;
      renderMotuCaption();
      clearTimeout(motuTimer);
      motuTimer = setTimeout(flushMotu, MOTU_DEBOUNCE_MS);
    }
    async function flushMotu() {
      if (motuBusy || motuTarget == null) return;
      if (!motu || !motu.known || !motu.writable) { motuTarget = null; renderMotu(); return; }
      if (motuTarget === motu.volume_db) { motuTarget = null; renderMotu(); return; }
      motuBusy = true;
      const target = motuTarget;
      try {
        const d = await api("/api/motu/volume", { method: "POST", body: JSON.stringify({ volume_db: target, expected_db: motu.volume_db }) });
        motu = d.motu;
        if (motuTarget === target) motuTarget = null;
        if (d.pending) {
          // The switcher is busy (a source change, say) and applies it when
          // it gets there; read what it publishes shortly.
          clearTimeout(motuPoll);
          motuPoll = setTimeout(loadMotu, 2000);
        }
      } catch (e) {
        // Refused: re-sync to what the device reports, drop the target.
        motu = e.data?.motu || null; motuTarget = null;
        toast(e.message);
      } finally {
        motuBusy = false;
        // A value dragged to while this request was in flight still goes out.
        if (motuTarget != null && motuTarget !== target) {
          clearTimeout(motuTimer);
          motuTimer = setTimeout(flushMotu, MOTU_DEBOUNCE_MS);
        }
        renderMotu();
      }
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
    async function runDiagnose() {
      const b = qs("#runDiagnose"); b.disabled = true;
      qs("#diagnoseSummary").textContent = "running…";
      try {
        const d = await api("/api/diagnose", { method: "POST", body: "{}" });
        const marks = { ok: ["✔", "ok"], warn: ["!", "warn"], fail: ["✘", "bad"], info: ["·", "faint"] };
        let group = null; const rows = [];
        for (const c of d.checks) {
          if (c.group !== group) { group = c.group; rows.push(`<div class="cap" style="margin-top:12px">${esc(group)}</div>`); }
          const [mark, cls] = marks[c.status] || ["?", "warn"];
          rows.push(`<div class="diag-row"><span class="${cls}">${mark}</span><b>${esc(c.name)}</b><span class="sub2">${esc(c.detail)}</span></div>`);
        }
        qs("#diagnoseReport").innerHTML = rows.join("");
        const n = s => d.checks.filter(c => c.status === s).length;
        qs("#diagnoseSummary").innerHTML = `<span class="ok">${n("ok")} ok</span> · <span class="warn">${n("warn")} warning(s)</span> · <span class="bad">${n("fail")} failure(s)</span>`;
      } catch (e) { qs("#diagnoseSummary").textContent = e.message; }
      finally { b.disabled = false; }
    }
    async function refreshLogs() {
      const unit = qs("#logUnit").value || "camilladsp.service";
      try { const d = await api(`/api/logs?unit=${encodeURIComponent(unit)}`); qs("#logBox").textContent = d.logs || "—"; }
      catch (e) { qs("#logBox").textContent = e.message; }
    }

    /* ---------------- per-source volume ---------------- */
    const svDb = v => v == null ? "" : Number(v).toFixed(1);
    function renderSourceVolume(data) {
      const sv = data.source_volume || {};
      const panel = qs("#sourceVolumePanel"), badge = qs("#svBadge");
      if (!panel) return;
      if (sv.error) {
        badge.textContent = "error"; badge.className = "badge bad";
        panel.innerHTML = `<div class="sub2 bad">${esc(sv.error)}</div>`;
        return;
      }
      badge.textContent = sv.enabled ? "on" : "off";
      badge.className = "badge " + (sv.enabled ? "ok" : "warn");
      // Never rebuild under a field being typed into.
      if (panel.contains(document.activeElement)) return;
      const available = (data.source || {}).available || {};
      const rows = Object.entries(sv.sources || {})
        .filter(([key]) => available[key]?.exists || sv.live === key)
        .map(([key, item]) => {
          const live = sv.live === key;
          const dis = live ? "disabled" : "";
          const remembered = item.cdsp_db != null || item.motu_db != null;
          return `<div>${esc(item.label)}</div>
            <input class="num" type="number" step="0.5" min="-80" max="0" placeholder="—" value="${svDb(item.cdsp_db)}" data-sv-source="${esc(key)}" data-sv-key="cdsp_db" ${dis} aria-label="${esc(item.label)} CamillaDSP start level">
            <input class="num" type="number" step="1" min="-100" max="0" placeholder="—" value="${item.motu_db == null ? "" : Number(item.motu_db).toFixed(0)}" data-sv-source="${esc(key)}" data-sv-key="motu_db" ${dis} aria-label="${esc(item.label)} MOTU start level">
            ${live ? `<span class="live">● live</span>` : `<button class="btn sm ghost" data-sv-forget="${esc(key)}" ${remembered ? "" : "disabled"}>Forget</button>`}`;
        });
      panel.innerHTML = rows.length
        ? `<div class="sv-grid"><div class="h">source</div><div class="h">Camilla dB</div><div class="h">MOTU dB</div><div></div>${rows.join("")}</div>`
        : `<div class="sub2">No sources available.</div>`;
      qsa("[data-sv-key]").forEach(i => i.addEventListener("change", e => {
        const v = e.target.value.trim();
        if (v === "") return;
        editSourceVolume({ source: e.target.dataset.svSource, [e.target.dataset.svKey]: Number(v) });
      }));
      qsa("[data-sv-forget]").forEach(b => b.addEventListener("click", () => editSourceVolume({ source: b.dataset.svForget, forget: true })));
      qs("#sourceVolumeCaption").innerHTML = sv.enabled
        ? `Each source starts at the level it last played at (speaker <b>${esc(sv.speaker)}</b>). Set a level for a source that is not playing to choose where it starts; ceilings still apply.`
        : `Off: SOURCE_VOLUME_MEMORY is disabled in the env file, so the volume carries over between sources.`;
    }
    async function editSourceVolume(payload) {
      try {
        const d = await api("/api/source-volume", { method: "POST", body: JSON.stringify(payload) });
        document.activeElement?.blur?.();
        renderSourceVolume({ source_volume: d.source_volume, source: lastStatus?.source });
      } catch (e) { toast(e.message); }
    }

    /* ---------------- live updates (Server-Sent Events) ---------------- */
    let live = null, liveSource = null, liveConnected = false;
    function applyLive(d) {
      live = d;
      const c = d.camilla || {};
      updateSignal(c.ok ? c.signal_db : null);
      if (c.ok) {
        const focus = document.activeElement;
        if (qs("#volRange") && focus !== qs("#volRange") && focus !== qs("#volNum")) {
          qs("#volRange").value = c.volume_db; qs("#volNum").value = Number(c.volume_db).toFixed(1);
        }
        if (qs("#muteToggle") && focus !== qs("#muteToggle")) qs("#muteToggle").checked = !!c.muted;
      }
      if (d.motu && motuTarget == null && !motuBusy) {
        const changed = !motu || motu.known !== d.motu.known || motu.volume_db !== d.motu.volume_db || motu.confirmed !== d.motu.confirmed || motu.writable !== d.motu.writable;
        motu = d.motu;
        if (changed) renderMotu();
      }
      if (d.source && liveSource !== JSON.stringify(d.source)) {
        liveSource = JSON.stringify(d.source);
        if (lastStatus) { lastStatus.source = Object.assign({}, lastStatus.source, d.source); renderSource(lastStatus); }
      }
      renderHome();
    }
    let liveStream = null;
    function startLive() {
      if (!window.EventSource || liveStream) return;
      liveStream = new EventSource("/api/events");
      liveStream.onmessage = e => { liveConnected = true; try { applyLive(JSON.parse(e.data)); } catch (err) { /* keep last */ } };
      liveStream.onerror = () => { liveConnected = false; };
    }
    function stopLive() { if (liveStream) { liveStream.close(); liveStream = null; } liveConnected = false; }

    /* ---------------- home (phone remote) ---------------- */
    const titleCase = v => v ? v[0].toUpperCase() + v.slice(1) : "—";
    function renderHome() {
      const c = (live && live.camilla && live.camilla.ok) ? live.camilla : (lastStatus?.camilla || {});
      const src = Object.assign({}, lastStatus?.source || {}, live?.source || {});
      const available = src.available || {};
      qs("#homeSource").textContent = SOURCE_LABELS[src.current] || titleCase(src.current);
      const vol = c.volume_db != null ? Number(c.volume_db) : null;
      const homeVol = qs("#homeVol");
      homeVol.textContent = vol == null ? "—" : vol.toFixed(1);
      homeVol.classList.toggle("muted", !!c.muted);
      qs("#homeVolSub").textContent = c.volume_max_db != null ? `dB · max ${Number(c.volume_max_db).toFixed(0)}` : "dB";
      const range = qs("#homeRange");
      const volMax = c.volume_max_db != null ? Number(c.volume_max_db) : 0;
      range.min = Math.min(-50, volMax); range.max = volMax;
      range.disabled = vol == null;
      if (vol != null && document.activeElement !== range) range.value = vol;
      const mute = qs("#homeMute");
      mute.textContent = c.muted ? "Unmute" : "Mute";
      mute.classList.toggle("is-muted", !!c.muted);
      const auto = !src.mode || src.mode === "auto";
      const cells = [];
      for (const [key, item] of Object.entries(available)) {
        if (!item.exists) continue;
        const state = src.current === key ? (src.mode === key ? "● held" : "● live") : "";
        cells.push(`<button class="cell ${src.current === key ? "on" : ""}" data-home-source="${esc(key)}"><span class="n">${esc(item.label)}</span><span class="k">${state}</span></button>`);
      }
      const autoBtn = qs("#homeAuto");
      autoBtn.classList.toggle("on", auto); autoBtn.setAttribute("aria-pressed", String(auto));
      autoBtn.textContent = auto ? "● Auto" : "Auto · off";
      const html = cells.join("");
      if (qs("#homeSources").innerHTML !== html) {
        qs("#homeSources").innerHTML = html;
        qsa("[data-home-source]").forEach(b => b.addEventListener("click", homeSource));
      }
      const known = !!(motu && motu.known);
      qs("#homeMotu").innerHTML = known ? `${motuDb(motuTarget ?? motu.volume_db)}<span class="unit"> dB</span>` : '<span class="faint">unknown</span>';
      qsa("[data-home-motu]").forEach(b => { b.disabled = !(known && motu.writable); });
    }
    const SOURCE_LABELS = { streamer: "Streamer", gadget: "USB Gadget", toslink: "TOSLINK", analog: "Analog" };
    async function homeSource(e) {
      const btn = e.currentTarget; btn.disabled = true;
      try { await api("/api/source", { method: "POST", body: JSON.stringify({ source: btn.dataset.homeAuto != null ? "auto" : btn.dataset.homeSource }) }); await load(); }
      catch (err) { toast(err.message); } finally { btn.disabled = false; }
    }
    /* Press and hold repeats, like the remote's volume keys. */
    function holdRepeat(button, action) {
      let timer = null, first = null;
      const stop = () => { clearTimeout(first); clearInterval(timer); first = timer = null; };
      button.addEventListener("pointerdown", e => {
        e.preventDefault(); stop(); action();
        first = setTimeout(() => { timer = setInterval(action, 180); }, 450);
      });
      ["pointerup", "pointerleave", "pointercancel"].forEach(ev => button.addEventListener(ev, stop));
      button.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); action(); } });
    }
    let homeVolumeBusy = false, homeVolumePending = 0;
    async function homeVolume(step) {
      homeVolumePending += step;
      if (homeVolumeBusy) return;
      homeVolumeBusy = true;
      try {
        while (homeVolumePending !== 0) {
          const delta = homeVolumePending; homeVolumePending = 0;
          const d = await api("/api/camilla/volume", { method: "POST", body: JSON.stringify({ delta_db: delta }) });
          const cam = d.camilla || {};
          if (cam.volume_db != null) {
            if (live && live.camilla) Object.assign(live.camilla, { volume_db: cam.volume_db, muted: cam.muted });
            else if (lastStatus) lastStatus.camilla = Object.assign({}, lastStatus.camilla, cam);
            renderHome();
          }
        }
      } catch (err) { homeVolumePending = 0; toast(err.message); }
      finally { homeVolumeBusy = false; }
    }

    /* ---------------- loop ---------------- */
    async function load() {
      const data = await api("/api/status");
      // Keep service controls available even if a later, unrelated panel fails
      // to render on an older browser or with partial device status.
      renderServices(data.services || {});
      renderLogOptions(data.services || {});
      renderHealth(data);
      updateSignal(data.camilla?.signal_db);
      renderSource(data);
      renderVolume(data);
      lastStatus = data;
      renderSourceVolume(data);
      renderHome();
      if (Number(data.camilla?.sample_rate)>0) eqSampleRate=Number(data.camilla.sample_rate);
      if (data.speaker) { speakerState=data.speaker; renderSpeakerProfiles(); }
      renderSystem(data);
    }

    async function pollLevels() {
      if (document.hidden || liveConnected) return;
      try { const d = await api("/api/levels"); updateSignal(d.signal_db); }
      catch (e) { /* keep last */ }
    }

    // Remote's halftone meter: the lit columns follow the real signal level
    // (−65 to −8 dBFS, left to right); the column heights only ripple.
    const HT_COLS = 18, HT_ROWS = 7;
    const htCells = [];
    (() => {
      const box = qs("#homeMeter");
      for (let r = 0; r < HT_ROWS; r++) for (let c = 0; c < HT_COLS; c++) {
        const i = document.createElement("i"); box.appendChild(i);
        htCells.push({ el: i, c, up: HT_ROWS - 1 - r, key: "" });
      }
    })();
    const calmMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let phase = 0, shownLevel = 0, htFrame = 0;
    function animateEq() {
      requestAnimationFrame(animateEq);
      if (document.hidden || (++htFrame & 1)) return;
      phase += 0.12;
      shownLevel += (signalTarget - shownLevel) * 0.3;
      const lit = shownLevel * HT_COLS;
      const heights = [];
      for (let c = 0; c < HT_COLS; c++) {
        const wob = calmMotion ? 1 : 0.5 + 0.5 * Math.sin(phase * (0.9 + c * 0.11) + c * 1.7);
        heights.push(c < lit ? Math.max(1, Math.round(HT_ROWS * (0.35 + 0.65 * wob))) : 0);
      }
      for (const cell of htCells) {
        const h = heights[cell.c];
        const on = cell.up < h, edge = cell.up === h && h > 0;
        const scale = on ? 1 - cell.up * 0.075 : (edge ? 0.42 : 0.2);
        const cls = on ? (cell.c >= HT_COLS * 0.82 ? "on hot" : "on") : "";
        const key = cls + scale.toFixed(2);
        if (key === cell.key) continue;
        cell.key = key; cell.el.className = cls; cell.el.style.transform = `scale(${scale.toFixed(2)})`;
      }
    }

    function activateTab(name) {
      if (name === "dashboard") name = "home";  // the old Dashboard tab now lives under Remote
      const btn = qsa("nav button").find(b => b.dataset.tab === name);
      if (!btn) return;
      qsa("nav button").forEach(x => x.classList.remove("active"));
      qsa("section").forEach(s => s.classList.remove("active"));
      btn.classList.add("active"); qs("#" + name).classList.add("active");
      // The hash names the section, so the browser scrolls it under the header; undo that.
      window.scrollTo(0, 0); requestAnimationFrame(() => window.scrollTo(0, 0));
      if (name === "logs") refreshLogs();
      if (name === "audio" && !audioLoaded) loadAudio();
    }
    // Only the hash drives tab activation; calling activateTab here too would
    // duplicate the tab's initial fetch (two /api/logs or /api/audio requests).
    qsa("nav button").forEach(b => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));
    window.addEventListener("hashchange", () => activateTab(location.hash.slice(1)));
    qs("#refreshLogs").addEventListener("click", refreshLogs);
    qs("#runDiagnose").addEventListener("click", runDiagnose);
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
    if (location.hash.slice(1)) { activateTab(location.hash.slice(1)); window.addEventListener("load", () => window.scrollTo(0, 0)); }
    load().catch(e => toast(e.message));
    // Once per page load, not with the 5 s status sweep: each device read
    // briefly drops the source switcher's meters.
    loadMotu();
    setInterval(() => { if (motuTarget != null) renderMotuCaption(); }, 1000);
    // A backgrounded phone must not keep the Pi spawning a status sweep every
    // 5s; refresh immediately instead when the page becomes visible again.
    // With the live stream connected the full sweep only has to catch what the
    // stream does not carry (services, speaker, availability): every 20 s.
    let refreshTick = 0;
    const refresh = () => { if (!document.hidden) load().catch(() => {}); };
    setInterval(() => { refreshTick++; if (!liveConnected || refreshTick % 4 === 0) refresh(); }, 5000);
    document.addEventListener("visibilitychange", () => { if (document.hidden) stopLive(); else { startLive(); refresh(); } });
    setInterval(pollLevels, 800);
    startLive();
    qsa("[data-home-vol]").forEach(b => holdRepeat(b, () => homeVolume(Number(b.dataset.homeVol))));
    qsa("[data-home-motu]").forEach(b => holdRepeat(b, () => {
      if (motu && motu.known && motu.writable) motuSetTarget((motuTarget ?? motu.volume_db) + MOTU_STEP_DB * Number(b.dataset.homeMotu), "nudge");
      renderHome();
    }));
    qs("#homeMute").addEventListener("click", () => {
      const c = (live && live.camilla && live.camilla.ok) ? live.camilla : (lastStatus?.camilla || {});
      setVolume({ muted: !c.muted });
    });
    qs("#homeAmps").addEventListener("click", ampsOff);
    qs("#homeAuto").addEventListener("click", homeSource);
    qs("#homeRange").addEventListener("change", e => { setVolume({ volume_db: Number(e.target.value) }); e.target.blur(); });
  </script>
</body>
</html>
"""

# The page is a raw string full of JS template literals, so str.format cannot
# be used on it.  Escaped because the value reaches a <title> and an <h1>.
HTML = HTML.replace("{{site}}", html.escape(SITE_NAME))


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
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, 124, str(exc))
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
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


SHOW_PROPERTIES = (
    "Id",
    "Names",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Description",
)


def systemctl_show(service: str) -> dict[str, str]:
    """Read one unit.  Independent of the batch, so it is a true fallback."""
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


def parse_systemctl_show(output: str) -> list[dict[str, str]]:
    """Split `systemctl show`'s blank-line-separated stanzas into records.

    Property order inside a stanza is irrelevant, and `--all` keeps empty
    properties present so a suppressed one cannot merge two units.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.isidentifier():
            continue
        current[key] = value
    if current:
        records.append(current)
    return records


def systemctl_show_many(units: list[str]) -> dict[str, dict[str, str]]:
    """One subprocess for the whole catalog, keyed by Id and by every alias."""
    if not units:
        return {}
    output = run(
        [
            "systemctl",
            "show",
            *units,
            "--all",
            "--no-page",
            f"--property={','.join(SHOW_PROPERTIES)}",
        ],
        timeout=6,
    )
    by_name: dict[str, dict[str, str]] = {}
    ambiguous: set[str] = set()
    for record in parse_systemctl_show(output):
        # `Id` is only the primary name; a requested alias appears in `Names`.
        names = {record.get("Id", ""), *record.get("Names", "").split()}
        names.discard("")
        for name in names:
            if name in by_name and by_name[name] is not record:
                ambiguous.add(name)
            by_name[name] = record
    return {
        unit: by_name[unit]
        for unit in units
        if unit in by_name and unit not in ambiguous
    }


def service_is_active(unit: str) -> bool:
    return run(["systemctl", "is-active", unit], timeout=3) == "active"


def service_status() -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    batched = systemctl_show_many(list(SERVICE_CATALOG))
    for name, meta in SERVICE_CATALOG.items():
        # Per unit, not per poll: one alias or duplicate costs one subprocess
        # instead of discarding the other eight units' results.
        props = batched.get(name) or systemctl_show(name)
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


def _finite_volume(value: Any) -> float:
    numeric = float(value)
    if isinstance(value, bool) or not math.isfinite(numeric):
        raise ValueError("volume must be a finite number")
    return numeric


def current_volume_max() -> float:
    """Ceiling of the profile the switcher last verified into service."""
    return volume_ceiling(SPEAKER_STATUS_PATH)


def clamp_volume(value: Any) -> float:
    maximum = current_volume_max()
    # A ceiling stricter than the usual floor still wins, so the floor follows
    # it down instead of clamping the refused volume back up.
    minimum = min(VOLUME_MIN_DB, maximum)
    return max(minimum, min(maximum, _finite_volume(value)))


@contextmanager
def camilla_client() -> Iterator[Any]:
    """Yield a connected CamillaClient and always disconnect afterwards."""
    from camilladsp import CamillaClient

    client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
    client.connect()
    try:
        yield client
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


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
            finite = []
            for level in levels:
                if level is None:
                    continue
                numeric = float(level)
                if math.isfinite(numeric) and numeric > -999.0:
                    finite.append(numeric)
        except Exception:
            try:
                if _levels_client is not None:
                    _levels_client.disconnect()
            except Exception:
                pass
            _levels_client = None
            return {"ok": False, "signal_db": None}

    return {"ok": True, "signal_db": max(finite) if finite else None}


# ---- live state for the page's event stream (/api/events) ----
# Every open page reads one shared snapshot, refreshed at most this often, on
# the persistent read-only client above: cheap reads only (levels, volume,
# mute, state, file path), never the full status sweep.
LIVE_INTERVAL_SECONDS = 0.5
# Each stream holds a handler thread; a few phones and a laptop is the use.
LIVE_MAX_STREAMS = 8
LIVE_KEEPALIVE_SECONDS = 15.0
# A stream ends after this long; EventSource reconnects by itself, so a
# forgotten tab cannot hold a thread for ever.
LIVE_STREAM_SECONDS = 1800.0
_live_lock = threading.Lock()
_live_cache: tuple[float, dict[str, Any]] | None = None
_live_identity: tuple[str | None, str | None] = (None, None)
_live_streams = 0
_live_streams_lock = threading.Lock()


def _strongest_level(levels: Any) -> float | None:
    finite = []
    for level in levels:
        if level is None:
            continue
        numeric = float(level)
        if math.isfinite(numeric) and numeric > -999.0:
            finite.append(numeric)
    return max(finite) if finite else None


def _read_live_camilla() -> dict[str, Any]:
    global _levels_client
    with _levels_lock:
        try:
            if _levels_client is None:
                from camilladsp import CamillaClient

                client = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
                client.connect()
                _levels_client = client
            client = _levels_client
            return {
                "ok": True,
                "state": str(client.general.state()).replace("ProcessingState.", ""),
                "config_file": client.config.file_path(),
                "volume_db": client.volume.main_volume(),
                "muted": client.volume.main_mute(),
                "signal_db": _strongest_level(client.levels.playback_rms()),
            }
        except Exception:
            try:
                if _levels_client is not None:
                    _levels_client.disconnect()
            except Exception:
                pass
            _levels_client = None
            return {"ok": False}


def _live_source(config_file: Any) -> str | None:
    """The playing source, recomputed only when the config path changes."""
    global _live_identity
    path = config_file if isinstance(config_file, str) else None
    if _live_identity[0] != path:
        current = None
        if path:
            try:
                identity = managed_config_identity(path)
                current = identity[0] if identity is not None else Path(path).stem
            except Exception:
                current = Path(path).stem
        _live_identity = (path, current)
    return _live_identity[1]


def live_snapshot() -> dict[str, Any]:
    """What changes second to second: level, volume, mute, source, MOTU."""
    global _live_cache
    with _live_lock:
        now = time.monotonic()
        if _live_cache is not None and now - _live_cache[0] < LIVE_INTERVAL_SECONDS * 0.8:
            return _live_cache[1]
        camilla = _read_live_camilla()
        if camilla.get("ok"):
            try:
                camilla["volume_max_db"] = current_volume_max()
            except Exception:
                camilla["volume_max_db"] = None
        snapshot = {
            "camilla": camilla,
            "source": {
                "mode": read_source_override() or "auto",
                "current": _live_source(camilla.get("config_file")),
            },
            "motu": motu_volume.read_status(),
        }
        _live_cache = (now, snapshot)
        return snapshot


def _claim_live_stream() -> bool:
    global _live_streams
    with _live_streams_lock:
        if _live_streams >= LIVE_MAX_STREAMS:
            return False
        _live_streams += 1
        return True


def _release_live_stream() -> None:
    global _live_streams
    with _live_streams_lock:
        _live_streams = max(0, _live_streams - 1)


# ---- installable web app: manifest and icons ----
THEME_COLOR = "#f3e7d8"
ICON_BARS = (0.34, 0.62, 0.9, 0.7, 0.46)
ICON_COOL = (0x2A, 0x1A, 0x10)
ICON_WARM = (0xB0, 0x36, 0x1B)


def manifest() -> dict[str, Any]:
    return {
        "name": f"{SITE_NAME} audio control",
        "short_name": SITE_NAME,
        "start_url": "/#home",
        "scope": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }


def _icon_bars(size: int) -> list[tuple[int, int, int, int, tuple[int, int, int]]]:
    """(x0, x1, y0, y1, colour) per bar, inside the maskable safe zone."""
    safe = size * 0.6  # the maskable icon's guaranteed-visible circle, roughly
    left = (size - safe) / 2
    slot = safe / len(ICON_BARS)
    bars = []
    for index, height in enumerate(ICON_BARS):
        mix = index / (len(ICON_BARS) - 1)
        colour = tuple(round(c + (w - c) * mix) for c, w in zip(ICON_COOL, ICON_WARM))
        x0 = round(left + slot * index + slot * 0.18)
        x1 = round(left + slot * (index + 1) - slot * 0.18)
        bar = safe * height
        y1 = round(size / 2 + safe / 2)
        bars.append((x0, x1, round(y1 - bar), y1, colour))
    return bars


@lru_cache(maxsize=4)
def icon_png(size: int) -> bytes:
    """The app icon as a PNG, drawn here so the UI stays one stdlib file."""
    background = bytes(int(THEME_COLOR[i : i + 2], 16) for i in (1, 3, 5))
    bars = _icon_bars(size)
    rows = []
    for y in range(size):
        row = bytearray(background * size)
        for x0, x1, y0, y1, colour in bars:
            if y0 <= y < y1:
                row[x0 * 3 : x1 * 3] = bytes(colour) * (x1 - x0)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def icon_svg() -> str:
    size = 512
    rects = "".join(
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="10" '
        f'fill="rgb({c[0]},{c[1]},{c[2]})"/>'
        for x0, x1, y0, y1, c in _icon_bars(size)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="96" fill="{THEME_COLOR}"/>{rects}</svg>'
    )


def camilla_status() -> dict[str, Any]:
    try:
        with camilla_client() as client:
            config = client.config.active() or {}
            return {
                "state": str(client.general.state()).replace("ProcessingState.", ""),
                "config_title": config.get("title"),
                "config_file": client.config.file_path(),
                "sample_rate": config.get("devices", {}).get("samplerate"),
                "volume_db": client.volume.main_volume(),
                "volume_max_db": current_volume_max(),
                "muted": client.volume.main_mute(),
            }
    except Exception as exc:
        return {"error": str(exc)}


def set_camilla_volume(payload: dict[str, Any]) -> dict[str, Any]:
    with camilla_client() as client:
        with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
            target_volume = None
            if "delta_db" in payload:
                current = float(client.volume.main_volume())
                target_volume = clamp_volume(
                    current + _finite_volume(payload["delta_db"])
                )
            elif "volume_db" in payload:
                target_volume = clamp_volume(payload["volume_db"])

            if "muted" in payload:
                muted = payload["muted"]
                if not isinstance(muted, bool):
                    raise ValueError("muted must be true or false")

            if target_volume is not None:
                client.volume.set_main_volume(target_volume)
            if "muted" in payload:
                client.volume.set_main_mute(muted)

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
        SOURCE_OVERRIDE_PATH.unlink(missing_ok=True)
        return

    if source not in SOURCE_CHOICES:
        raise ValueError("source not allowed")
    availability = source_availability()
    entry = availability.get(source, {})
    if not entry.get("exists"):
        raise FileNotFoundError(str(entry.get("path") or source))

    SOURCE_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write_runtime_value(SOURCE_OVERRIDE_PATH, source + "\n")


def current_speaker_selection() -> dict[str, Any]:
    return read_speaker_selection(
        SPEAKER_SELECTION_PATH, allowed_ids=BUILTIN_SPEAKERS
    )


def installed_profile_catalog() -> dict[str, dict[str, Any]]:
    return profile_catalog(SPEAKER_PROFILE_DIR, SOURCE_BASE_DIR, CDSP_CONFIG_DIR)


def source_availability() -> dict[str, dict[str, Any]]:
    selected = current_speaker_selection()["selected"]
    if selected == DEFAULT_SPEAKER_ID:
        return {
            key: {
                "label": label,
                "path": str(CDSP_CONFIG_DIR / f"{key}.yml"),
                "exists": (CDSP_CONFIG_DIR / f"{key}.yml").is_file(),
            }
            for key, label in SOURCE_CHOICES.items()
        }
    profile = installed_profile_catalog().get(selected, {})
    supported = set(profile.get("supported_sources") or [])
    profile_ready = bool(profile.get("available"))
    operator_configs = operator_configs_for_speaker(selected)
    paths = {
        source: (
            CDSP_CONFIG_DIR / operator_configs[source]
            if source in operator_configs
            else SOURCE_BASE_DIR / f"{source}.yml"
        )
        for source in SOURCE_CHOICES
    }
    return {
        key: {
            "label": label,
            "path": str(paths[key]),
            "exists": bool(
                profile_ready
                and key in supported
                and paths[key].is_file()
            ),
        }
        for key, label in SOURCE_CHOICES.items()
    }


def source_status(camilla: dict[str, Any]) -> dict[str, Any]:
    override = read_source_override()
    config_file = camilla.get("config_file")
    current = None
    if isinstance(config_file, str):
        identity = managed_config_identity(config_file)
        current = identity[0] if identity is not None else Path(config_file).stem

    return {
        "mode": override or "auto",
        "current": current,
        "config_file": config_file,
        "available": source_availability(),
    }


def source_volume_payload() -> dict[str, Any]:
    """What the per-source volume memory holds for the selected speaker."""
    speaker = current_speaker_selection()["selected"]
    remembered = source_volume.read_state()["speakers"].get(speaker, {})
    status = _read_json_object(SPEAKER_STATUS_PATH)
    live = status.get("source") if status.get("applied") == speaker else None
    return {
        "enabled": source_volume.enabled(),
        "speaker": speaker,
        "live": live if isinstance(live, str) else None,
        "sources": {
            key: {"label": label, **remembered.get(key, {})}
            for key, label in SOURCE_CHOICES.items()
        },
    }


def _optional_level(payload: dict[str, Any], key: str) -> float | None:
    if payload.get(key) is None:
        return None
    return _finite_volume(payload[key])


def edit_source_volume(payload: dict[str, Any]) -> dict[str, Any]:
    """Set or forget the levels a source that is not playing will start at.

    The playing source's levels are recorded from the live controls by the
    switcher; editing them here would be overwritten seconds later, so it is
    refused with a pointer to the real controls.
    """
    source = payload.get("source")
    if source not in SOURCE_CHOICES:
        raise ValueError("source not allowed")
    current = source_volume_payload()
    if source == current["live"]:
        raise RequestRefused(
            HTTPStatus.CONFLICT,
            f"{SOURCE_CHOICES[source]} is playing: use the volume controls; "
            "its levels are remembered from them",
        )
    if payload.get("forget") is True:
        source_volume.forget(current["speaker"], source)
    else:
        cdsp_db = _optional_level(payload, "cdsp_db")
        motu_db = _optional_level(payload, "motu_db")
        if cdsp_db is None and motu_db is None:
            raise ValueError("cdsp_db or motu_db is required")
        source_volume.remember(
            current["speaker"], source, cdsp_db=cdsp_db, motu_db=motu_db
        )
    return source_volume_payload()


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
        # Filtered on the stamp's shape: a speaker id may itself contain
        # "-", so "audio-eq-sub-*" would also match "audio-eq-sub-2-...".
        stamped = re.compile(re.escape(prefix) + r"-\d{8}-\d{6}\.json")
        old = sorted(
            p for p in backup_dir.glob(f"{prefix}-*.json") if stamped.fullmatch(p.name)
        )
        for extra in old[:-keep]:
            extra.unlink(missing_ok=True)
    except OSError:
        pass


def iso226_capability() -> tuple[dict[str, Any], bool]:
    """The ISO 226 engine installer's receipt, and whether it names the engine."""
    capability = _read_json_object(ISO226_CAPABILITY_PATH)
    return capability, capability.get("engine") == "Iso226"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def speaker_payload() -> dict[str, Any]:
    selection = current_speaker_selection()
    status = _read_json_object(SPEAKER_STATUS_PATH)
    return {
        "selection": selection,
        "catalog": installed_profile_catalog(),
        "status": status,
        # The switcher has applied the selected revision.
        "ready": status.get("ok") is True
        and status.get("selection_revision") == selection["revision"],
    }


def _selected_audio_path(selection: dict[str, Any]) -> Path:
    return resolve_profile_audio_path(
        SPEAKER_AUDIO_DIR,
        selection["selected"],
        legacy_path=AUDIO_EQ_PATH,
    )


def preflight_speaker_profile(speaker_id: str) -> None:
    """Compile and offline-check every declared source before selection."""
    if speaker_id == DEFAULT_SPEAKER_ID:
        paths = [CDSP_CONFIG_DIR / f"{source}.yml" for source in ("streamer", "gadget", "toslink")]
        analog = CDSP_CONFIG_DIR / "analog.yml"
        if analog.is_file():
            paths.append(analog)
        configs = [(path.stem, path) for path in paths]
    elif operator_configs_for_speaker(speaker_id):
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
        # Report the missing cap here rather than letting the transition fail
        # later: the operator owns these files and has to edit them by hand.
        for _source, path in configs:
            if path.is_file():
                require_config_volume_limit(
                    load_yaml_mapping(path, f"operator config {path.name}"),
                    profile,
                    label=f"operator config {path.name}",
                )
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
                result = run_result([CAMILLA_BINARY, "-c", str(temporary)], timeout=10)
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
        result = run_result([CAMILLA_BINARY, "-c", str(path)], timeout=10)
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
    if speaker_id == DEFAULT_SPEAKER_ID:
        if not (CDSP_CONFIG_DIR / f"{source}.yml").is_file():
            label = BUILTIN_SPEAKERS.get(speaker_id, {}).get("label") or speaker_id
            raise ValueError(f"{label} has no installed {source} configuration")
        return source
    supported = set(catalog.get(speaker_id, {}).get("supported_sources") or [])
    if source not in supported:
        raise ValueError(
            f"{catalog[speaker_id].get('label') or speaker_id} does not support the active {source} source"
        )
    return source


def managed_config_identity(current: Any) -> tuple[str, str] | None:
    """Match exact legacy, operator-owned, or generated DSP configs."""
    return identify_managed_config(
        current,
        config_dir=CDSP_CONFIG_DIR,
        generated_dir=SPEAKER_GENERATED_DIR,
        default_speaker_id=DEFAULT_SPEAKER_ID,
    )


def select_speaker(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("speaker selection must be an object")
    if raw.get("confirm") != "SWITCH":
        raise ValueError("speaker selection requires explicit SWITCH confirmation")
    speaker_id = raw.get("selected")
    expected_revision = raw.get("revision")
    catalog = installed_profile_catalog()
    available_ids = {
        profile_id
        for profile_id, entry in catalog.items()
        if entry.get("available")
    }
    if speaker_id not in available_ids:
        reason = catalog.get(speaker_id, {}).get("reason") or "profile is unavailable"
        raise ValueError(f"cannot select {speaker_id!r}: {reason}")
    preflight_speaker_profile(speaker_id)

    # Only the selection is committed here.  The source switcher picks it up
    # on its next pass and does the switch itself: mute, reload, check, then
    # the listener's previous mute state -- all under the audio-control lock.
    with audio_control_lock(AUDIO_CONTROL_LOCK_PATH):
        require_active_source_supported(speaker_id, catalog)
        update_speaker_selection(
            SPEAKER_SELECTION_PATH,
            speaker_id,
            expected_revision=expected_revision,
            allowed_ids=available_ids,
        )
    # Ensure the new profile has an isolated state before the editor loads it.
    read_profile_audio_state(
        SPEAKER_AUDIO_DIR, speaker_id, legacy_path=AUDIO_EQ_PATH
    )
    return speaker_payload()


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
    status = _read_json_object(AUDIO_EQ_STATUS_PATH)
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
            "CDSP-AIRPLAY-BEGIN" in shairport
            and 'ignore_volume_control = "yes"' in shairport
            and "airplay_volume_bridge.py --notify" in shairport
        )
    except OSError:
        configured = False
    shairport_active = service_is_active("shairport-sync.service")
    bridge_active = service_is_active("airplay-volume-bridge.service")
    try:
        spotify_dropin = SPOTIFY_VOLUME_DROPIN_PATH.read_text(encoding="utf-8")
        spotify_configured = (
            "librespot-cdsp" in spotify_dropin
            and "LIBRESPOT_VOLUME_CTRL=fixed" in spotify_dropin
            and "--notify-spotify" in spotify_dropin
            and "CDSP_SPOTIFY_VOLUME_SOCKET" in spotify_dropin
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
    with speaker_selection_lock(SPEAKER_SELECTION_PATH):
        selection = current_speaker_selection()
        if expected_speaker is not None and expected_speaker != selection["selected"]:
            raise ValueError("speaker selection changed elsewhere; reload before saving")
        audio_path = _selected_audio_path(selection)
        with audio_state_lock(audio_path):
            current = read_audio_state(audio_path)
            incoming_revision = raw.get("revision", current["revision"])
            if isinstance(incoming_revision, bool) or not isinstance(
                incoming_revision, int
            ):
                raise ValueError("revision must be an integer")
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


class RequestRefused(Exception):
    """A request rejected by a transport guard, with the status to report."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


DEFAULT_SCHEME_PORTS = {"http": "80", "https": "443"}


def normalize_authority(authority: str, scheme: str = "http") -> str:
    """Lower-cased host[:port] with the scheme's default port dropped."""
    authority = (authority or "").strip().lower()
    if not authority:
        return ""
    host, _, port = authority.rpartition(":")
    if not host or "]" in port or not port.isdigit():
        # No port, or an unbracketed IPv6 literal: the whole value is the host.
        host, port = authority, ""
    if port and port == DEFAULT_SCHEME_PORTS.get(scheme.lower()):
        port = ""
    return f"{host}:{port}" if port else host


def origin_is_same_site(origin: str, host_header: str) -> bool:
    """True when Origin is absent or names this very server.

    A browser attaches Origin to every cross-site POST, so a foreign value is
    the confused-deputy case and is refused.  A missing Origin is a non-browser
    client (curl, the installer's own checks) and stays allowed, which keeps
    this guard free for existing scripted callers.
    """
    origin = (origin or "").strip()
    if not origin:
        return True
    if origin.lower() == "null":
        return False
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme.lower() not in DEFAULT_SCHEME_PORTS or not parsed.netloc:
        return False
    if not (host_header or "").strip() or not host_names_this_pi(host_header):
        return False
    return normalize_authority(parsed.netloc, parsed.scheme) == normalize_authority(
        host_header, "http"
    )


def host_names_this_pi(host_header: str) -> bool:
    """True for an IP literal, localhost, or this machine's own name.

    Origin == Host alone is beaten by DNS rebinding: a page on a name the
    attacker re-points at the Pi sends matching values for both.
    """
    host = normalize_authority(host_header, "http")
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    own = socket.gethostname().lower()
    return host in {"localhost", own, f"{own}.local"}


def presented_token(headers: Any) -> str:
    """The bearer token on a request, from Authorization or X-Control-Token."""
    raw = (headers.get("Authorization") or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return (headers.get("X-Control-Token") or "").strip()


def authorize_state_change(headers: Any) -> None:
    """Raise unless the request may change state.

    Origin is checked unconditionally; the shared secret only when one is
    configured, so an install that never sets INSTALLATION_UI_TOKEN behaves
    exactly as it did before.
    """
    if not origin_is_same_site(headers.get("Origin"), headers.get("Host")):
        raise RequestRefused(HTTPStatus.FORBIDDEN, "cross-origin request refused")
    expected = configured_ui_token()
    if not expected:
        return
    supplied = presented_token(headers)
    # compare_digest, not ==: a plain comparison leaks the matching prefix
    # length through its timing, which is enough to recover a secret.
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise RequestRefused(HTTPStatus.UNAUTHORIZED, "control token required")


def request_body_length(headers: Any) -> int:
    """Validate Content-Length before a single byte of the body is read."""
    if (headers.get("Transfer-Encoding") or "").strip():
        # Chunked bodies have no declared length, so they cannot be bounded
        # up front; nothing in this UI sends one.
        raise RequestRefused(
            HTTPStatus.LENGTH_REQUIRED, "a declared Content-Length is required"
        )
    raw = headers.get("Content-Length")
    if raw is None:
        return 0
    try:
        length = int(str(raw).strip())
    except ValueError:
        raise RequestRefused(
            HTTPStatus.BAD_REQUEST, "Content-Length is not a number"
        ) from None
    if length < 0:
        raise RequestRefused(HTTPStatus.BAD_REQUEST, "Content-Length is negative")
    if length > MAX_REQUEST_BODY_BYTES:
        raise RequestRefused(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
        )
    return length


class Handler(BaseHTTPRequestHandler):
    server_version = "InstallationControl/0.3"
    # socketserver hands this to connection.settimeout(), so a client that
    # opens a socket and never finishes its request cannot pin a thread.
    timeout = REQUEST_TIMEOUT_SECONDS

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_live_events(self) -> None:
        """Stream live_snapshot() as Server-Sent Events while the page is open.

        A message goes out only when something changed; a comment line keeps
        idle proxies from closing the stream.  The page falls back to polling
        when the stream is refused or unavailable.
        """
        if not _claim_live_stream():
            self.send_json(
                {"ok": False, "error": "too many live streams"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            started = last_write = time.monotonic()
            last_body = None
            while time.monotonic() - started < LIVE_STREAM_SECONDS:
                body = json.dumps(live_snapshot(), separators=(",", ":"))
                now = time.monotonic()
                if body != last_body:
                    self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_body, last_write = body, now
                elif now - last_write >= LIVE_KEEPALIVE_SECONDS:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = now
                time.sleep(LIVE_INTERVAL_SECONDS)
        except OSError:
            pass  # the page closed or went to sleep
        finally:
            _release_live_stream()

    def refuse(self, refusal: RequestRefused) -> None:
        """Answer a guard rejection without draining the unread body."""
        # The body was never read, so this connection cannot be reused: the
        # unread bytes would be parsed as the next request line.
        self.close_connection = True
        if refusal.status == HTTPStatus.UNAUTHORIZED:
            body = json.dumps({"ok": False, "error": str(refusal)}).encode("utf-8")
            self.send_response(refusal.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", 'Bearer realm="control-ui"')
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json({"ok": False, "error": str(refusal)}, refusal.status)

    def read_json(self) -> dict[str, Any]:
        length = request_body_length(self.headers)
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestRefused(HTTPStatus.BAD_REQUEST, "truncated request body")
        return json.loads(body.decode("utf-8"))

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

        if parsed.path == "/manifest.webmanifest":
            self.send_body(json.dumps(manifest()).encode("utf-8"), "application/manifest+json")
            return

        if parsed.path == "/icon.svg":
            self.send_body(icon_svg().encode("utf-8"), "image/svg+xml")
            return

        icon_sizes = {"/icon-192.png": 192, "/icon-512.png": 512, "/apple-touch-icon.png": 180}
        if parsed.path in icon_sizes:
            self.send_body(icon_png(icon_sizes[parsed.path]), "image/png")
            return
        if parsed.path.startswith("/fonts/") and parsed.path[len("/fonts/"):] in FONT_FILES:
            # A missing file is a plain 404, which sends the page on to its
            # Google copy of the font.
            try:
                font = (FONTS_DIR / parsed.path[len("/fonts/"):]).read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_body(font, "font/woff2")
            return

        if parsed.path == "/api/events":
            self.send_live_events()
            return

        if parsed.path == "/api/status":
            services = service_status()
            camilla = camilla_status()
            if "error" not in camilla:
                camilla["signal_db"] = camilla_levels().get("signal_db")
            # An unreadable speaker selection must not blank the whole page:
            # the services panel is how the operator recovers from it.
            try:
                source = source_status(camilla)
            except Exception as exc:
                source = {"error": str(exc)}
            try:
                speaker = speaker_payload()
            except Exception as exc:
                speaker = {"error": str(exc)}
            try:
                volumes = source_volume_payload()
            except Exception as exc:
                volumes = {"error": str(exc)}
            self.send_json(
                {
                    "services": services,
                    "camilla": camilla,
                    "source": source,
                    "speaker": speaker,
                    "source_volume": volumes,
                    "remote": remote_status(services),
                }
            )
            return

        if parsed.path == "/api/levels":
            self.send_json(camilla_levels())
            return

        if parsed.path == "/api/motu/volume":
            # A read, so ungated like every GET: what the source switcher, the
            # MOTU's one client, last published.
            self.send_json({"ok": True, "motu": motu_volume.read_status()})
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
            # Guarded before the body is touched: an unauthorized or oversized
            # request is refused without buffering anything it sent.
            authorize_state_change(self.headers)
            payload = self.read_json()
        except RequestRefused as refusal:
            self.refuse(refusal)
            return
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        try:
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

            if parsed.path == "/api/motu/volume":
                self.send_motu_volume_change(payload)
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

            if parsed.path == "/api/source-volume":
                try:
                    volumes = edit_source_volume(payload)
                except RequestRefused as refusal:
                    self.send_json({"ok": False, "error": str(refusal)}, refusal.status)
                    return
                self.send_json({"ok": True, "source_volume": volumes})
                return

            if parsed.path == "/api/diagnose":
                # A POST, though it only reads: it runs camilladsp -c on every
                # config, so the token guard applies to it.
                import dataclasses

                import diagnose

                checks = diagnose.diagnose()
                self.send_json(
                    {"ok": True, "checks": [dataclasses.asdict(c) for c in checks]}
                )
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

            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def send_motu_volume_change(self, payload: dict[str, Any]) -> None:
        """Hand a MOTU main-volume change to the switcher, mapping its outcome.

        Every reply carries the switcher's current reading, so the page
        re-syncs its control to the device instead of retrying from a stale
        value.  A switcher that has not answered yet still applies the
        request: 202, and the page reads the result later.
        """
        try:
            _request_id, result = motu_volume.submit_request(
                payload, MOTU_VOLUME_REPLY_SECONDS
            )
        except motu_volume.MotuVolumeRefused as exc:
            self.send_json(
                {"ok": False, "error": str(exc), "motu": motu_volume.read_status()},
                HTTPStatus.CONFLICT,
            )
            return
        status = motu_volume.read_status()
        if result is None:
            self.send_json({"ok": True, "pending": True, "motu": status}, HTTPStatus.ACCEPTED)
            return
        if result.get("ok"):
            self.send_json({"ok": True, "written": result.get("written"), "motu": status})
            return
        code = (
            HTTPStatus.CONFLICT
            if result.get("kind") in {"conflict", "refused"}
            else HTTPStatus.SERVICE_UNAVAILABLE
        )
        self.send_json({"ok": False, "error": result.get("error"), "motu": status}, code)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> int:
    host = ui_bind_host()
    port = ui_bind_port()
    server = ThreadingHTTPServer((host, port), Handler)
    guard = (
        "shared secret required"
        if configured_ui_token()
        else "no authentication (INSTALLATION_UI_TOKEN unset)"
    )
    print(
        f"installation UI listening on http://{host}:{port} - {guard}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
