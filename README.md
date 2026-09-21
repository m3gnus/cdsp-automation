# CamillaDSP Utilities for Raspberry Pi

Automation utilities for CamillaDSP on Raspberry Pi: trigger control, MOTU clock sync, seamless source switching, and Bluetooth remote control.

## Prerequisites

Before installing, ensure you have:

**General Requirements:**
- Raspberry Pi (any model) running Raspberry Pi OS
- CamillaDSP installed and running
- Python 3.10 or newer

**For the optional ISO 226 Loudness Engine:**
- `camilladsp.service` must start `/usr/local/bin/camilladsp`, because the
  engine replaces that exact binary. A CamillaDSP installed elsewhere is
  detected before anything is compiled: Install All skips it with a note in the
  run summary and installs everything else, while menu option 10 fails and
  prints the reason.

**For the optional Spotify Volume Sync:**
- A `raspotify.service`. Without one the step is skipped with a note.
- The receiver inherits raspotify's own output device. Set
  `SPOTIFY_ALSA_DEVICE` in `~/camilladsp/cdsp-automation.env` (see `aplay -L`)
  only to override it.

**For Trigger Control:**
- 5V relay module ([like this](https://www.aliexpress.com/item/1005007109343076.html))
- Mono 3.5mm jack connector ([like this](https://www.aliexpress.com/item/32704200322.html))
- Your amplifier must support trigger input (typically 3-12V)

**For MOTU Clock Sync:**
- MOTU UltraLite mk5 (or compatible MOTU interface)
- MOTU accessible on your network

**For Source Switcher:**
- Three CamillaDSP config files with **specific naming**:
  - `~/camilladsp/configs/toslink.yml` - For the optical input
  - `~/camilladsp/configs/streamer.yml` - For AirPlay/network streaming
  - `~/camilladsp/configs/gadget.yml` - For USB Gadget mode
- Optional `~/camilladsp/configs/analog.yml` for manual or meter-based analog input
- Clock ownership is derived from the managed source name, not sample rate:
  TOSLINK selects optical; streamer, gadget, and analog select internal. Equal
  sample rates across sources are therefore supported.

**For Remote Control:**
- Bluetooth or USB HID remote control ([like this](https://www.aliexpress.com/item/1005010182280772.html))
- The installed persistent audio-EQ overlay (tone control uses its reserved low/high shelves)

## Quick Start

Download and run the installer:

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh
./install.sh
```

**Notes:**
- Run the installer on the Raspberry Pi where you want the utilities installed
- The installer uses `sudo` where required, so you do not need to run the whole script as root
- Choose option **1** to install all utilities at once, or install them
  individually - but note that options 6, 9 and 12 also install the source
  switcher, which they require in order to unmute or apply tone changes

## Audio control architecture

The source switcher is the only writer of the active CamillaDSP configuration.
It composes the selected speaker's persistent EQ/loudness overlay from
`/var/lib/cdsp-automation/speaker-audio` into every source before that
speaker's crossover. The default speaker keeps using the legacy
`/var/lib/cdsp-automation/audio-eq.json` state path. The browser and HID
remote resolve the selected profile for every edit, so Bass/Treble, user EQ,
and loudness remain independent per speaker.

The selectable speakers ship as the maintainer's defaults. A site replaces
the whole catalog without editing code by writing the JSON file at
`SPEAKER_CATALOG_PATH` (default `/etc/cdsp-automation/speaker-catalog.json`;
see `speaker-catalog.example.json`). The contract is one sentence: the
`default` speaker is the one that plays through your existing full CamillaDSP
configs (and keeps the legacy `audio-eq.json` state path); every other
speaker is a managed profile — parametric YAML in the profile directory, or
complete operator-owned CamillaDSP files mapped per source with
`operator_configs`. An unreadable or invalid catalog logs a warning and
keeps the built-ins.

Speaker selection and source arbitration are orthogonal. The catalog's default
speaker uses the existing full configs. Non-legacy profiles are strict YAML fragments in
`SPEAKER_PROFILE_DIR`; they are composed with capture-only YAML bases from
`SOURCE_BASE_DIR`, written to digest-addressed immutable files, checked with
`camilladsp -c`, and reloaded transactionally. A profile is unavailable until
all of its declared source bases exist and `enabled: true` is explicit.

The output contract requires every physical output to be declared active or
muted and the final pipeline step to be a safety/output mixer. A typical
three-way topology is: stereo source processing → expansion mixer → per-output
crossover/delay/gain filters → final 1:1 output mixer. Do not enable PartyMEH,
Bird, or Measurement definitions until measured crossover, polarity, delay,
gain, and channel routing values are known. Measurement must set both
`raw_measurement: true` and `bypass_user_eq: true`. That contract rejects all
source and profile filters/processors and every source pipeline step, leaving
only the profile's explicit direct output mixer; its output level remains the
operator's responsibility.
The repository's `speaker-profile.example.yml` is deliberately disabled and
fully muted; `source-base.example.yml` shows the capture-only boundary.

All master-volume writers share `AUDIO_CONTROL_LOCK_PATH`. `AUDIO_READY_PATH`
is absent by default and is written only as the final commit of a verified
transition. It is not a bare flag: it names the *engine generation* — a random
id the source switcher mints for every CamillaDSP connection it makes and
stamps into the live config's description — plus the applied config path,
digest, source, speaker, and speaker-selection revision. Every unmute path
re-reads that description from the CamillaDSP client it already holds and
refuses unless it still matches the token, so an engine that restarts or
reloads from file is inhibited immediately even though the token file is still
there. (It is a handshake, not a seal: a foreign live-config write that keeps
the description keeps readiness too - see TECHNICAL.md.) Anything missing, unparseable, or mismatched
inhibits. While inhibited, AirPlay, Spotify, the browser, and the HID remote
may mute but cannot unmute. `cdsp-source-switcher.service` is `PartOf=`
`camilladsp.service`, so restarting the engine restarts the switcher that
re-verifies it.

The all-utilities install also:

- builds the pinned CamillaDSP 4.1.3 ISO 226 patch, runs the full Rust library
  suite plus deployed-config checks, and rolls back automatically if anything
  fails between replacing the engine and publishing its receipt - restoring
  the engine and receipt that were in place just before the attempt;
- installs a persistent network-volume daemon and a non-blocking Shairport
  callback, backs up/validates its configuration, and restores the original
  volume settings on uninstall;
- builds a pinned librespot 0.8.0 receiver that keeps Spotify audio at unity,
  routes Spotify Connect volume into CamillaDSP, and publishes CamillaDSP/Web
  UI/HID changes back to the Spotify source slider. The installer verifies the
  patched receiver and rolls back automatically if its service or command
  socket is unhealthy.

When a network receiver session starts, the bridge can also stop local
LMS/Squeezelite streamer playback: set `AIRPLAY_INTERRUPTED_LMS_PLAYERS` to a
comma-separated list of player names. It is strictly best-effort — an
unreachable LMS never blocks AirPlay or Spotify — and unset (the default) the
hand-off is disabled.

ISO calibration: choose a comfortable reference master setting, play a 1 kHz
sine at a known digital level, measure SPL at the listening position, enter that
reading as the reference phon and then enable the engine. The tone matters —
phon equals SPL only at 1 kHz, so an SPL reading taken on music or broadband
noise will not give the right reference and a several-dB error here shifts the
whole compensation curve. Reference phon is limited to 40–90 because ISO
226:2003 defines the contours no higher (and only to 80 phon above 4 kHz, so
81–90 already extrapolates the top of the curve). Fixed MOTU and amplifier trims
remain calibration stages; day-to-day volume belongs to the CamillaDSP Main
fader.

The implementation uses the established ISO 226:2003 coefficient model as a
practical approximation to the 2023 revision. The published revision analysis
places the maximum difference at 0.6 dB; the licensed 2023 Annex B data is not
copied into this repository.

The filter realises the correction as one broadband gain plus nine high shelves.
The shelf gains are solved through a per-samplerate matrix rather than assigned
band-to-band, which holds the realised response within 0.6 dB of the intended
curve from 20 Hz to 12.5 kHz at every supported rate — the engine's Rust tests
assert this, along with the invariant that boost never exceeds the attenuation
the fader has already applied. Because coefficients can only change on chunk
boundaries, a chunk whose gains changed is crossfaded from the old cascade to
the new one; swapping outright measured about 30 dB more broadband splatter
during volume ramps.

---

## 🎮 Remote Control - Detailed Setup

### What It Does

Control CamillaDSP from a Bluetooth or USB HID remote. Volume and mute drive the
CamillaDSP Main fader; Bass and Treble update the shared persistent Audio overlay.

### Hardware Requirements

Any Bluetooth or USB remote that registers as an HID keyboard device. Common options:
- Bluetooth media remotes ([example](https://www.aliexpress.com/item/1005010182280772.html))
- USB IR remotes
- Bluetooth presentation clickers

### Button Mapping

| Button | Action |
|--------|--------|
| **VOLUME UP** | Increase volume by 1 dB (hold for continuous) |
| **VOLUME DOWN** | Decrease volume by 1 dB (hold for continuous) |
| **MUTE** | Toggle mute on/off |
| **UP arrow** | Increase treble by 0.5 dB (max +6 dB) |
| **DOWN arrow** | Decrease treble by 0.5 dB (min -6 dB) |
| **RIGHT arrow** | Increase bass by 0.5 dB (max +6 dB) |
| **LEFT arrow** | Decrease bass by 0.5 dB (min -6 dB) |
| **ENTER** (short press) | Print current status to log |
| **ENTER** (hold ~1 sec) | Reset bass and treble to 0 dB |
| **POWER** (hold ~1 sec) | Restart all CamillaDSP services |
| **POWER** (hold ~10 sec) | Shutdown the system |

### Pairing a Bluetooth Remote

Use the installer's built-in pairing option:

```bash
./install.sh
# Choose option 7: Pair Bluetooth Remote
```

Or pair manually:

```bash
bluetoothctl power on
bluetoothctl scan on
# Wait for your remote to appear, note the MAC address
bluetoothctl scan off
bluetoothctl pair XX:XX:XX:XX:XX:XX
bluetoothctl connect XX:XX:XX:XX:XX:XX
bluetoothctl trust XX:XX:XX:XX:XX:XX
```

### Finding Your Remote's Device Name

After pairing, find your remote's name:

```bash
python3 -c "import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])"
```

Example output:
```
['HID Remote01 Keyboard', 'HID Remote01 Mouse', 'vc4-hdmi-0', 'pwr_button']
```

The installer will prompt you to enter this name during installation.

### Configuration

User settings live in `~/camilladsp/cdsp-automation.env` and are preserved when you update the scripts. Common remote settings:

```text
REMOTE_NAME=HID Remote01 Keyboard
REMOTE_TONE_MIN=-6
REMOTE_TONE_MAX=6
REMOTE_TONE_STEP=0.5
REMOTE_VOLUME_MIN=-80
REMOTE_VOLUME_MAX=0
REMOTE_VOLUME_STEP=1
```

`REMOTE_VOLUME_MAX` can only lower the ceiling. The real maximum comes from the
speaker profile that is currently applied, so a profile capped at -20 dB stays
capped at -20 dB on the remote, in the control UI, and over AirPlay/Spotify.

### CamillaDSP Filter Ownership

Do not add legacy filters named `Bass`, `Treble`, or `loudness` to source
configs. The source switcher owns the persistent `cdsp_ui_eq_*` overlay and
uses its reserved low/high shelves for remote tone control. It also removes
legacy connected tone and loudness stages so they cannot stack with the GUI EQ
or the optional ISO226 filter.

---

## 🔌 Trigger Control - Detailed Setup

### What It Does

Automatically powers your amplifier on/off by sending the Pi's switched 5V signal into an amplifier trigger input.

### Hardware Requirements

1. **5V Relay Module** - [Example](https://www.aliexpress.com/item/1005007109343076.html)
2. **Mono 3.5mm Jack** - [Example](https://www.aliexpress.com/item/32704200322.html)
3. Jumper wires
4. Amplifier with 12V trigger input

### Wiring Instructions

**What You Need:**
- Raspberry Pi
- 5V relay module (6 pins total)
- Mono 3.5mm jack cable (to amplifier)

**Connections:**

**Step 1: Control Side of Relay** (powers the relay)
1. **Pi 5V** (Pin 2 or 4) → **Relay DC+**
2. **Pi Ground** (Pin 6, 9, 14, 20, 25, 30, 34, or 39) → **Relay DC-**
3. **Pi GPIO 4** (Pin 7) → **Relay IN**

**Step 2: Switch Side of Relay** (triggers the amp)
4. **Pi 5V** (Pin 2 or 4) → **Relay COM**
5. **Relay NO** → **Mono jack Tip**
6. **Pi Ground** (any ground pin) → **Mono jack Sleeve**

**Step 3: Leave Unused**
7. **Relay NC** → nothing (leave empty)

**Summary:**
- **Pi 5V** connects to 2 places: DC+ and COM
- **Pi Ground** connects to 2 places: DC- and jack sleeve
- **Pi GPIO 4** connects to 1 place: IN
- **Jack Tip** connects to 1 place: NO
- **Jack Sleeve** connects to 1 place: Pi Ground
- **NC terminal** stays empty

**Done!** When the script activates GPIO 4, the relay switches on and sends 5V to your amp's trigger input.

**3.5mm Jack to Amplifier:**
- Connect the mono jack to your amplifier's trigger input

### Configuration

Edit `~/camilladsp/cdsp-automation.env`:

```text
POWER_GPIO=4
TRIGGER_DELAY_SECONDS=320
TRIGGER_CHECK_INTERVAL=0.2
TRIGGER_AUDIO_THRESHOLD_DB=-80
```

### How It Works

- Detects music and turns relay ON (checks every 200ms)
- Starts 320-second countdown when music stops
- Only turns relay OFF if silence continues for full duration
- Resets countdown if music resumes
- Accepts `SIGUSR1` for an immediate manual OFF. If audio is still active it
  stays off until silence is observed, then automatic triggering is re-armed
  for the next audio session.

**Why 320 seconds?** Long enough to handle gaps between tracks and quiet passages without constantly cycling your amplifier on/off.

---

## 🎚️ MOTU Clock Sync

### What It Does

Automatically switches your MOTU audio interface's clock source from the active
managed source identity.

### How It Works

- Reads the source name from CamillaDSP's managed config path, through the
  same speaker-catalog lookup the source switcher uses
- Sends WebSocket commands to MOTU to change clock source
- **TOSLINK** → switches to **optical** clock
- **Streamer, USB gadget, or analog** → switches to **internal** clock
- Retries a failed MOTU command until it is confirmed sent
- Reads the clock source back from the MOTU and retries a command the device
  did not actually apply

### Requirements

- MOTU UltraLite mk5 (other MOTU models may need different hex payloads)
- MOTU must be accessible on your network
- The active config must use a managed source name

### Configuration

The installer will prompt for your MOTU's IP address. To find it, push the
UltraLite mk5's left front-panel knob to open the device info list; one entry
is the IP address (a self-assigned `169.254.x.x`, usually `169.254.51.193`
here). The UltraLite has no web interface: port 80 answers nothing, and all
control goes over the binary WebSocket on port 1280 that CueMix 5 uses.

To change the IP later, edit `~/camilladsp/cdsp-automation.env`:

```text
MOTU_WS_URL=ws://YOUR_MOTU_IP:1280
MOTU_CLOCK_STATE_PATH=/var/lib/cdsp-automation/motu-clock-source
```

Clock ownership is independent of sample rate. Sources may all run at 48 kHz;
the config identity still selects the correct clock. The config's identity
comes from the speaker catalog, so an operator config mapped to any filename
(not only `<speaker>-<source>.yml`) still selects the right clock.

The last requested clock choice is persisted at `MOTU_CLOCK_STATE_PATH` so
service restarts do not send a redundant command that makes the interface
re-lock and briefly mute. That file is a cache of what was asked for, not
proof of what the device did: the daemon reads the clock source back from the
state the MOTU pushes to every new WebSocket client (it sends nothing to read),
corrects the cache when the device disagrees (someone changed it in CueMix 5),
and re-sends a command the device never applied - at most once per
`MOTU_CLOCK_REWRITE_INTERVAL`, since every write re-locks the clock audibly.
If the device cannot be read, the daemon falls back to the cached value.

The UltraLite serves one WebSocket client at a time, so each read-back briefly
displaces the source switcher's meter connection (or an open CueMix 5), which
reconnects on its own; that is why a confirmed clock is re-checked only every
few minutes. Optional keys:

```text
MOTU_CLOCK_READBACK_TIMEOUT=3
MOTU_CLOCK_VERIFY_INTERVAL=300
MOTU_CLOCK_READBACK_RETRY_INTERVAL=300
MOTU_CLOCK_REWRITE_INTERVAL=30
```

When the Source Switcher is installed as well, it changes the clock itself,
inside its muted source transition and before the new config is loaded, so the
interface has re-locked before sound returns; a failed rollback puts the old
clock back while still muted. The daemon then only verifies, and steps in for
clock changes the switcher did not make. It decides under the same
audio-control lock, so it never undoes a transition that is half done, and it
reads the shared `MOTU_CLOCK_STATE_PATH` cache, so it never repeats the
switcher's write. Switcher keys:

```text
# auto: drive the clock whenever the MOTU Clock Sync unit is installed
SOURCE_MOTU_CLOCK=auto
# re-lock time allowed before the new graph is loaded on the interface
MOTU_CLOCK_SETTLE_SECONDS=1.0
```

`MOTU_DATASTORE_URL` is no longer read; an existing line for it can be removed.


---

## 🔄 Source Switcher

### What It Does

Automatically switches between CamillaDSP configs based on which audio source is playing.

**Priority order:**
1. **Manual override** - optional pinned source selected by writing to `SOURCE_OVERRIDE_PATH`
2. **Current active source** - if the current source is still playing, it keeps control
3. **Streamer** (AirPlay/network streaming) - first automatic choice when changing sources
4. **USB Gadget** (direct USB connection)
5. **TOSLINK** (optical input) - detected from MOTU input meters
6. **Analog** (optional) - disabled by default; can be enabled for MOTU input meters

When no automatic source is active, the default behavior is to keep the current
config instead of forcing TOSLINK. Set `SOURCE_IDLE_MODE=toslink` if you prefer
the older fallback behavior.

### Critical Configuration Requirements

**You MUST create three config files with exact names:**

1. **`~/camilladsp/configs/toslink.yml`**
   - Configure for optical input
   - Use the rate required by the source and DSP graph

2. **`~/camilladsp/configs/streamer.yml`**
   - Configure for ALSA Loopback (from Squeezelite/AirPlay)
   - May use the same sample rate as TOSLINK

3. **`~/camilladsp/configs/gadget.yml`**
   - Configure for USB Gadget
   - May use the same sample rate as the other sources

Optional manual-only configs can also be selected through the override file. For example,
`~/camilladsp/configs/analog.yml` can be pinned manually even though analog is not
auto-detected by default.

The source name, not a sample-rate heuristic, controls clock ownership.

### How It Works

1. Checks if hardware is active (device connected and ready)
2. Switches to that source's config
3. Monitors actual audio playback via RMS levels
4. Keeps the current source while it is still playing
5. Waits 60 seconds of silence before abandoning a source whose playback it has *confirmed*, unless another meter-confirmed source is already active
6. Remembers a source it selected and heard nothing from, and re-probes it on a growing backoff instead of every minute
7. Uses passive MOTU meter frames to detect TOSLINK activity
8. Keeps the current config when all sources are idle unless `SOURCE_IDLE_MODE=toslink`

A source with confirmed audio cuts a silent source's grace short rather than
waiting it out, once it has held that confirmation for
`SOURCE_PREEMPT_DWELL_SECONDS` (default 2, so a single noisy meter frame cannot
yank the config away mid-track). A *higher*-priority source waits for nothing
else. A *lower*-priority one is additionally gated by
`SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT`, which says how far into the current
source's silence it is allowed to act; the default is immediate handoff after
the lower source has passed its own activity debounce. A source that is merely
ready - a connected-but-paused AirPlay session, say - never cuts a grace short,
because that grace is exactly what protects a track gap from it.

The MOTU meter sources are a special case: `toslink_available` only goes false
after `SOURCE_TOSLINK_IDLE_SECONDS` of quiet meters, so by the time one of them
reads silent it has already served a track-gap grace of its own. It does not
get a second one on top - switch the TV off and the switcher starts looking
elsewhere as soon as the meters settle, not a minute later.

Hardware readiness only says a stream is open - a connected-but-paused AirPlay
session or an idle console looks exactly like a playing one until the switcher
selects it and reads the capture levels. A source selected this way is listened
to for `SOURCE_PROBE_SILENCE_TIMEOUT` seconds; if it stays silent the switcher
records that and will not select it again for `SOURCE_PROBE_BACKOFF_SECONDS`,
growing by `SOURCE_PROBE_BACKOFF_FACTOR` per consecutive silent probe up to
`SOURCE_PROBE_BACKOFF_MAX`. Confirmed audio, a manual override, and the source
disappearing and coming back all clear the backoff immediately. Setting
`SOURCE_PROBE_BACKOFF_SECONDS=0` disables the rate limit.

### Configuration

Edit `~/camilladsp/cdsp-automation.env`:

```text
SOURCE_IDLE_TIMEOUT=60
SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT=0
SOURCE_PREEMPT_DWELL_SECONDS=2
SOURCE_PROBE_SILENCE_TIMEOUT=5
SOURCE_PROBE_BACKOFF_SECONDS=30
SOURCE_PROBE_BACKOFF_FACTOR=4
SOURCE_PROBE_BACKOFF_MAX=900
SOURCE_AUDIO_THRESHOLD_DB=-80
SOURCE_OVERRIDE_PATH=/run/cdsp-source-switcher/manual_source
SOURCE_TOSLINK_MOTU_METERS=true
SOURCE_ANALOG_MOTU_METERS=false
SOURCE_IDLE_MODE=keep-last
SOURCE_TOSLINK_METER_PAIRS=12,13
SOURCE_ANALOG_METER_PAIRS=16,18
SOURCE_DEBUG=false
```

To pin a source manually, write one of `toslink`, `streamer`, `gadget`, or `analog`
to `SOURCE_OVERRIDE_PATH`. Remove the file, leave it empty, or write `auto` to return
to automatic switching.

### Debugging

Enable debug mode to see what the switcher is doing:

```text
SOURCE_DEBUG=true
```

Then watch the logs:

```bash
journalctl -u cdsp-source-switcher -f
```

You'll see output like:

```
DEBUG: Streamer HW=True, Gadget HW=False, TOSLINK meter=False/0/5, Analog meter=False/0/30, Last=streamer, ST=0, GT=0
-> Streamer: audio active
-> Streamer: idle 5/60s
```

---

## 🖥️ Web Control UI (optional)

### What It Does

A single-file, no-framework web dashboard (default port 8088) for the whole
audio stack: physical source switching, CamillaDSP master volume/mute, the
persistent parametric EQ with a computed response curve and loudness
controls, speaker-profile selection (muted, validated, rollback-protected
transitions), service health and restarts, live input levels, journal logs,
USB storage mounting, and system clock control.

The UI edits persistent state only; the source switcher remains the sole
writer of the live CamillaDSP configuration.

### Security Model

Because it manages services, storage mounts, and the system clock, the
`cdsp-control-ui.service` unit runs as **root** by design. Install it only if
you want that trade-off; every other utility works without it.

Out of the box it binds **every interface on port 8088 with no
authentication** — the historical behaviour, kept as the default so that
upgrading an existing install does not take its UI away. Two settings in
`~/camilladsp/cdsp-automation.env` narrow that, and menu option 12 offers the
first one before it installs anything:

| Setting | Default | Effect |
| --- | --- | --- |
| `INSTALLATION_UI_HOST` | `0.0.0.0` | Bind address. Set `127.0.0.1` for loopback only. |
| `INSTALLATION_UI_PORT` | `8088` | Listening port. |
| `INSTALLATION_UI_TOKEN` | *(empty)* | Empty means no authentication. Set a secret to require it. |

**Loopback only.** Set `INSTALLATION_UI_HOST=127.0.0.1` and
`sudo systemctl restart cdsp-control-ui`, then reach the UI through an SSH
tunnel from your laptop:

```bash
ssh -N -L 8088:127.0.0.1:8088 <user>@<pi>
# then open http://127.0.0.1:8088
```

**Shared secret.** Set a long random token and restart the service:

```bash
openssl rand -hex 32           # put the result in cdsp-automation.env
# INSTALLATION_UI_TOKEN=<that value>
sudo systemctl restart cdsp-control-ui
```

Every request that changes anything (volume, EQ, source, speaker profile,
services, storage, the clock) then needs
`Authorization: Bearer <token>`. Open the dashboard once as
`http://<pi>:8088/#token=<token>` and the page remembers it; otherwise it
prompts for the token the first time you change something. Status views stay
readable without it, so the page can load in order to ask.

Two protections are always on, whatever you configure: state-changing
requests from a foreign `Origin` are refused, and request bodies are bounded
(256 KiB) and timed out rather than buffered.

The service still runs as root. A token limits *who* can drive it; it does not
reduce what the process itself can do. Treat port 8088 as privileged either
way, and never expose it to the internet.

---

## Installation Details

The installer menu provides these options:

1. **Install All Utilities** - Recommended for first-time setup
2. **Update Utilities** - Downloads latest scripts and updates pycamilladsp
3. **Install Trigger Control** - GPIO relay control only
4. **Install MOTU Clock Sync** - MOTU clock management only
5. **Install Source Switcher** - Config switching only
6. **Install Remote Control** - Bluetooth/USB remote control (also installs
   option 5, which it requires)
7. **Pair Bluetooth Remote** - Interactive Bluetooth pairing
8. **Show Service Status** - Check if services are running
9. **Install AirPlay + Spotify Volume Sync** - Network receivers drive the
   CamillaDSP fader (also installs option 5, which it requires)
10. **Install ISO 226 Loudness Engine** - Pinned loudness-patched CamillaDSP build.
    Requires `camilladsp.service` to start `/usr/local/bin/camilladsp`.
11. **Uninstall All Utilities** - Remove the services, units and sudoers rules.
    Your configs, the env file and `/var/lib/cdsp-automation` state are kept.
12. **Install Web Control UI** - Optional root web dashboard (trusted LAN only;
    also installs option 5, which it requires)

Options 6, 9 and 12 install the source switcher when it is missing, because
the components they install cannot unmute without it. The switcher is the only
writer of the audio-ready token that permits an unmute, and the only thing that
applies persisted Bass/Treble/EQ edits to the running engine, so installing any
of them alone would produce a remote, bridge or UI that can never unmute and
whose tone edits go nowhere. Each run says so before it acts, and lists the
switcher under "Also installed, because the components you chose require it"
in its closing summary. Installing the switcher does not give it its source
configs - you still have to create them, or its service will not run.

Options 11 and 12 ask for a `y/N` confirmation before acting: one removes every
managed service, the other exposes a root web server. Option 12 first prints
the address it is about to bind to and whether a token is configured, and
offers to move the UI to `127.0.0.1` before you say yes.

Options 1, 2, 6, 9 and 12 end with a summary listing anything that was pulled
in as a dependency and any component that was skipped or failed, and no longer
abandon the rest of the run when one of them cannot be installed.

### What Gets Installed

**Directories created:**
- `~/camilladsp/scripts/` - Python scripts
- `~/camilladsp/configs/` - CamillaDSP config files (you must create these)
- `~/camilladsp/.venv/` - Python virtual environment
- `~/camilladsp/cdsp-automation.env` - User settings preserved across script updates

**System services:**
- `cdsp-trigger.service`
- `cdsp-motu-sync.service`
- `cdsp-source-switcher.service`
- `cdsp-remote.service`
- `airplay-volume-bridge.service`
- `cdsp-control-ui.service` (optional)

**Dependencies:**
- `websocket-client` (Python package)
- `pycamilladsp` (Python package)
- `evdev` (Python package)
- `python3-rpi-lgpio` (system package)
- `alsa-utils`, `bluez`, `wget`, `python3-venv` (system packages)

---

## Managing Services

### View Status

```bash
systemctl status cdsp-trigger
systemctl status cdsp-motu-sync
systemctl status cdsp-source-switcher
systemctl status cdsp-remote
```

### View Logs (Live)

```bash
journalctl -u cdsp-trigger -f
journalctl -u cdsp-motu-sync -f
journalctl -u cdsp-source-switcher -f
journalctl -u cdsp-remote -f
```

### View Last 100 Log Lines

```bash
journalctl -u cdsp-trigger -n 100
journalctl -u cdsp-motu-sync -n 100
journalctl -u cdsp-source-switcher -n 100
journalctl -u cdsp-remote -n 100
```

### Start/Stop/Restart

```bash
sudo systemctl start cdsp-remote
sudo systemctl stop cdsp-remote
sudo systemctl restart cdsp-remote
```

### Enable/Disable Auto-Start on Boot

```bash
sudo systemctl enable cdsp-remote   # Start on boot
sudo systemctl disable cdsp-remote  # Don't start on boot
```

---

## Troubleshooting

### Remote Control Not Working

**Check if remote is detected:**

```bash
python3 -c "import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])"
```

Your remote should appear in the list. If not:
- Check Bluetooth connection: `bluetoothctl devices Connected`
- Re-pair the remote using the installer (option 7)

**Check if the device name matches:**

The `REMOTE_NAME` value in `~/camilladsp/cdsp-automation.env` must exactly match what appears in the device list.

**Test remote input:**

```bash
python3 -m evdev.evtest
```

Select your remote and press buttons - you should see key events.

**Check logs:**

```bash
journalctl -u cdsp-remote -n 100
```

**Tone controls not working:**

Check that `cdsp-source-switcher.service` is active and that
`/var/lib/cdsp-automation/audio-eq.json` is writable by the service user. Do
not add separate `Bass` or `Treble` filters.

### Trigger Control Not Working

**Check wiring:**
- Verify GPIO 4 is connected to relay IN
- Verify 5V and GND are connected correctly
- Test relay manually: `gpio -g write 4 1` (requires wiringpi)

**Check permissions:**

```bash
ls -l /dev/gpiochip0
# Should show: crw-rw---- 1 root gpio
```

If not in gpio group:

```bash
sudo usermod -aG gpio $USER
# Then logout and login again
```

**Check logs:**

```bash
journalctl -u cdsp-trigger -n 100
```

Look for error messages about GPIO access or CamillaDSP connection.

### MOTU Clock Sync Not Working

**Verify MOTU IP address:**

```bash
ping 169.254.51.193  # Or your MOTU's IP
```

**Check the MOTU control WebSocket is reachable** (an UltraLite mk5 serves no
HTTP, so `curl` to port 80 always reports an empty reply; like any client,
this briefly displaces the source switcher's meter connection):

```bash
timeout 2 bash -c '</dev/tcp/169.254.51.193/1280' && echo open
```

**Check logs:**

```bash
journalctl -u cdsp-motu-sync -n 100
```

**For other MOTU models:**

The payloads and read-back values come from MOTU's CueMix 5 app, whose
unobfuscated JavaScript defines every device parameter (for the UltraLite,
`dev.js`: `kClockSource` is id 11 with Internal=3, S/PDIF=0, Optical=2). Other
MOTU gen5 models may differ; check the matching `dev_*.js`, then update
`CLOCK_PAYLOADS` and `MOTU_CLOCK_SOURCE_VALUES` in `clock_sync.py` together.
MOTU's AVB interfaces use a different (HTTP datastore) API and are not
supported.

### Source Switcher Not Switching

**Enable debug mode:**

Edit `~/camilladsp/cdsp-automation.env`:

```text
SOURCE_DEBUG=true
```

Restart service:

```bash
sudo systemctl restart cdsp-source-switcher
```

Watch logs:

```bash
journalctl -u cdsp-source-switcher -f
```

**Verify config files exist:**

```bash
ls -l ~/camilladsp/configs/
# Should show: toslink.yml, streamer.yml, gadget.yml
```

**Check if configs are valid:**

```bash
camilladsp -c ~/camilladsp/configs/toslink.yml
```

**Inspect configured sample rates:**

```bash
grep samplerate ~/camilladsp/configs/*.yml
# Equal rates are supported; each source should use the rate its graph expects.
```

**Test hardware detection manually:**

For Loopback (Streamer):

```bash
cat /proc/asound/Loopback/pcm0p0/sub0/status
# Look for "state: RUNNING" when streaming
```

For USB Gadget:

```bash
amixer -c UAC2Gadget contents | grep "Capture Rate" -A 1
# Should show non-zero rate when USB host connected
```

For TOSLINK meter detection, check that the MOTU WebSocket is reachable:

```bash
journalctl -u cdsp-source-switcher -n 100 | grep "MOTU meters"
```

---

## Can I Use Just One Utility?

**Some of them.** Trigger control and MOTU clock sync are genuinely standalone.
The remote, the AirPlay/Spotify volume bridge and the web control UI are not:
they all ask `require_audio_unmute_allowed()` for permission before unmuting,
and only the source switcher ever grants it by publishing the audio-ready
token. The switcher is also the only thing that applies a persisted
Bass/Treble/EQ edit to the running engine. So:

- Install only **Trigger Control** for amp power management
- Install only **MOTU Clock Sync** for clock source automation
- Install only **Source Switcher** for config switching
- **Remote Control**, **AirPlay + Spotify Volume Sync** and the **Web Control
  UI** each require **Source Switcher**. The installer adds it for you rather
  than leaving you with a component that can never unmute and whose tone edits
  are never applied
- Install any combination of the above; shared writers use the audio-control lock

The ready token is a JSON document carrying the generation of the engine it was
verified against, so writing one by hand is not a way around this - and the
check is what stops audio being unmuted against an unverified engine, so it is
never relaxed.

---

## Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Test thoroughly on your hardware
4. Submit a pull request

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [CamillaDSP](https://github.com/HEnquist/camilladsp) by HEnquist
- [pycamilladsp](https://github.com/HEnquist/pycamilladsp) Python library
- [RPi-CamillaDSP](https://github.com/mdsimon2/RPi-CamillaDSP) — RPi-focused CamillaDSP setup by mdsimon2
- [Display, remote and trigger power for CamillaDSP streamer and preamp — Audio Science Review thread](https://www.audiosciencereview.com/forum/index.php?threads/display-remote-and-trigger-power-for-camilladsp-streamer-and-preamp-alternative-to-mdsimon2%E2%80%99s-implementation.52818/) — community discussion and alternative implementations

---

## Support

- **Issues:** Open a GitHub issue for bugs
- **Questions:** Use GitHub Discussions
- **Ideas:** Start a discussion or open a feature request
