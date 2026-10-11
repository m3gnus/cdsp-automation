# CamillaDSP Utilities for Raspberry Pi

A set of small background programs that make a Raspberry Pi running
[CamillaDSP](https://github.com/HEnquist/camilladsp) behave like a finished
hi-fi preamp: it picks the right input by itself, turns the amplifier on and
off, takes a remote control, and keeps the volume in step with AirPlay and
Spotify.

## What's included

| Part | What it does for you | Needs |
|------|----------------------|-------|
| [**Source Switcher**](#source-switcher) | Plays whichever source is active (streamer, USB, optical, analog) by loading the matching CamillaDSP config. Also sets the MOTU's clock and remembers a volume per source. **Most other parts depend on it.** | Your CamillaDSP configs; a MOTU UltraLite mk5 for optical/analog detection |
| [**Trigger Control**](#trigger-control) | Switches your amplifier on when music starts and off after about 5 minutes of silence. | A 5V relay and an amp with a trigger input |
| [**Remote Control**](#remote-control) | Volume, mute, bass, treble and more from a Bluetooth or USB remote. Buttons can be remapped. | Any remote that shows up as a keyboard |
| [**AirPlay + Spotify Volume Sync**](#airplay--spotify-volume-sync) | The volume slider on your phone moves the CamillaDSP volume, and changes made elsewhere move the phone slider. | `shairport-sync` and/or `raspotify` |
| [**ISO 226 Loudness**](#iso-226-loudness) | Adds bass and treble at low volume so music sounds balanced at every level. | Optional patched CamillaDSP build |
| [**Web Control UI**](#web-control-ui) | A dashboard and phone remote in your browser: volume, sources, EQ, speakers, services and logs. | Optional; runs as root |
| [**Health Check**](#health-check) | One command that checks everything and tells you what's wrong. | Nothing |

## Quick start

On the Raspberry Pi, run:

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh
./install.sh
```

Then pick from the menu:

1. **Install All Utilities** — everything except the web UI (recommended the first time)
2. **Update Utilities** — get the latest scripts; your settings are kept
3. **Install Trigger Control**
4. **Install Source Switcher**
5. **Install Remote Control**
6. **Pair Bluetooth Remote**
7. **Show Service Status**
8. **Install AirPlay + Spotify Volume Sync**
9. **Install ISO 226 Loudness Engine** — also rebuilds it
10. **Uninstall All Utilities** — asks first; your configs and settings are kept
11. **Install Web Control UI** — optional; asks first
12. **Run Health Check**

You don't need to run the installer as root; it uses `sudo` when it needs to.
Options 5, 8 and 11 also install the Source Switcher if it is missing,
because they don't work without it.

**After installing**, create your CamillaDSP configs (see
[Source Switcher](#source-switcher)) and put your own values in
`~/camilladsp/cdsp-automation.env` (see [Settings](#settings)).

## Before you start

- A Raspberry Pi running Raspberry Pi OS, with Python 3.10 or newer
- CamillaDSP installed and running
- Hardware for the parts you want: a relay for the trigger, a remote, a MOTU
  UltraLite mk5 — listed under each part below

## Can I install just one part?

- **Trigger Control** works entirely on its own.
- **Source Switcher** works on its own.
- **Remote Control**, **AirPlay + Spotify Volume Sync** and the **Web Control
  UI** each require **Source Switcher**. It is the only program that applies tone,
  EQ and speaker changes to CamillaDSP, and it tells the others how loud they
  are allowed to go. Without it they would be stuck at a safe low volume and
  their changes would never be heard. The installer adds it for you.

---

## Source Switcher

### What it does

Watches all your inputs and loads the CamillaDSP config for whichever one is
playing. It is also the only program that talks to the MOTU interface.

### What you need

Create these config files with **exactly** these names:

| File | Used for |
|------|----------|
| `~/camilladsp/configs/streamer.yml` | AirPlay / Spotify / Squeezelite (ALSA Loopback) |
| `~/camilladsp/configs/gadget.yml` | A computer connected over USB (USB Gadget mode) |
| `~/camilladsp/configs/toslink.yml` | The optical input on the MOTU |
| `~/camilladsp/configs/analog.yml` | *Optional* analog input |

The sources may all use the same sample rate. The switcher service won't start
until these configs exist.

### Which source wins

1. **Your manual choice**, if you have pinned one (see below)
2. **The source already playing** keeps playing
3. **Streamer**
4. **USB Gadget**
5. **TOSLINK** (detected from the MOTU's input meters)
6. **Analog** (only if you turn it on)

When nothing is playing, the current config stays loaded.

### Choosing a source by hand

Write `toslink`, `streamer`, `gadget` or `analog` to
`/run/cdsp-source-switcher/manual_source`. Write `auto`, empty the file or
delete it to go back to automatic. The remote and the web UI can do this too.

### Volume per source

Each source starts at the volume it last played at, separately for each
speaker. So the TV on TOSLINK comes back at TV volume and AirPlay comes back at
AirPlay volume. The "Per-source volume" card in the web UI shows these levels
and lets you change them. To turn this off, set `SOURCE_VOLUME_MEMORY=0` in the
settings file.

### MOTU clock

The MOTU must use the optical input as its clock for TOSLINK and its own
internal clock for everything else. The switcher changes this for you, only
while the output is muted, so you never hear the click. There is no separate
MOTU service any more; an install or update removes the old
`cdsp-motu-sync` service.

To set it up, find the MOTU's IP address: push the left front-panel knob on
the UltraLite mk5 and look for the IP in the info list (usually
`169.254.x.x`). Put it in the settings file:

```text
MOTU_WS_URL=ws://YOUR_MOTU_IP:1280
```

Other MOTU models may use different values; see
[Troubleshooting](#motu-clock-not-switching).

### How it decides (details)

- It checks whether each input is connected and ready, then listens to the
  actual audio level.
- A source that has gone quiet keeps control for **60 seconds**, so a pause
  between tracks doesn't make it switch away. A different source that is
  clearly playing (for 2 seconds) can take over sooner.
- TOSLINK and analog count as quiet after a few seconds of silent meters, so
  turning off the TV lets another source take over quickly.
- A source that looks connected but is silent (for example a paused AirPlay
  session) is tried for 5 seconds. If it stays silent, the switcher waits
  before trying it again: 30 seconds at first, growing to at most 15 minutes.
  Real audio, a manual choice, or reconnecting the source resets this.
- Every switch mutes first, loads and checks the new config, then unmutes. If
  the new config fails, the old one is put back and the output stays muted.

The timings are constants at the top of `scripts/source_switcher.py`.

---

## Trigger Control

### What it does

Turns your amplifier on when music starts and off after 320 seconds (about
5 minutes) of silence. The long delay means pauses between tracks and quiet
passages don't switch the amp off. You can also turn the amp off straight away
from the remote or the web UI; it then stays off until the music has stopped
and started again.

### What you need

- A 5V relay module ([example](https://www.aliexpress.com/item/1005007109343076.html))
- A mono 3.5mm jack cable ([example](https://www.aliexpress.com/item/32704200322.html))
- Jumper wires
- An amplifier with a trigger input that accepts 5V (most accept 3–12V)

### Wiring

| From | To |
|------|----|
| Pi 5V (pin 2 or 4) | Relay **DC+** |
| Pi ground (e.g. pin 6) | Relay **DC−** |
| Pi GPIO 4 (pin 7) | Relay **IN** |
| Pi 5V (pin 2 or 4) | Relay **COM** |
| Relay **NO** | Jack **tip** |
| Pi ground (any ground pin) | Jack **sleeve** |
| Relay **NC** | *nothing* |

Plug the jack into the amp's trigger input. When the script switches GPIO 4
on, the relay closes and sends 5V to the amp.

### Settings

To use a different GPIO pin, change `POWER_GPIO=4` in the settings file. The
320-second delay and the −80 dB "is music playing" threshold are constants in
`scripts/trigger.py`.

---

## Remote Control

### What it does

Lets you control the system from any Bluetooth or USB remote that shows up as
a keyboard (media remotes, USB IR receivers, presentation clickers;
[example](https://www.aliexpress.com/item/1005010182280772.html)).

### Default buttons

| Button | Action |
|--------|--------|
| **Volume up / down** | Volume ±1 dB (hold to repeat) |
| **Mute** | Mute on/off |
| **Up / Down** | Treble ±0.5 dB (range −6 to +6 dB) |
| **Right / Left** | Bass ±0.5 dB (range −6 to +6 dB) |
| **Enter** (press) | Write the current status to the log |
| **Enter** (hold 1 s) | Reset bass and treble to 0 |
| **Power** (hold 1 s) | Restart the CamillaDSP services |
| **Power** (hold 10 s) | Shut down the Pi |

### Setting it up

1. Pair the remote with installer option **6**, or by hand:

   ```bash
   bluetoothctl power on
   bluetoothctl scan on      # wait for the remote, note its MAC address
   bluetoothctl scan off
   bluetoothctl pair XX:XX:XX:XX:XX:XX
   bluetoothctl connect XX:XX:XX:XX:XX:XX
   bluetoothctl trust XX:XX:XX:XX:XX:XX
   ```

2. Find the remote's name:

   ```bash
   ~/camilladsp/.venv/bin/python3 -c "import evdev; print([evdev.InputDevice(p).name for p in evdev.list_devices()])"
   # e.g. ['HID Remote01 Keyboard', 'HID Remote01 Mouse', 'vc4-hdmi-0', 'pwr_button']
   ```

3. The installer asks for that name. You can change it later in the settings
   file, together with the volume limits:

   ```text
   REMOTE_NAME=HID Remote01 Keyboard
   REMOTE_VOLUME_MIN=-80
   REMOTE_VOLUME_MAX=0
   ```

   `REMOTE_VOLUME_MAX` can only lower the limit; the selected speaker's own
   maximum always applies.

### Changing the buttons

**1. Find out what your buttons are called.** Every remote is different:

```bash
sudo systemctl stop cdsp-remote
~/camilladsp/.venv/bin/python3 ~/camilladsp/scripts/cdsp_remote.py --learn
# press each button, then Ctrl+C
sudo systemctl start cdsp-remote
```

You'll see lines like `KEY_HOMEPAGE  pressed  [HID Remote01 Keyboard] -> not mapped`.
Only keys from the `REMOTE_NAME` device can be used; keys from a sibling
device (e.g. "… Consumer Control") are marked as unusable.

**2. Write your own key map**, starting from the current one (or from
`remote-keymap.example.json`):

```bash
~/camilladsp/.venv/bin/python3 ~/camilladsp/scripts/cdsp_remote.py --print-keymap > remote-keymap.json
# edit remote-keymap.json
sudo install -m 0644 remote-keymap.json /etc/cdsp-automation/remote-keymap.json
sudo systemctl restart cdsp-remote
```

Each key can have a `press` action (add `"repeat": true` to repeat while
held), a `hold` action (after `hold_seconds`, default 1 s) and a `long_hold`
action (after `long_hold_seconds`, default 10 s). If the file has a mistake,
it is logged and the default buttons stay in use.

| Action | Does |
|--------|------|
| `volume_up`, `volume_down`, `mute` | CamillaDSP volume (step: `volume_step_db`) |
| `bass_up`, `bass_down`, `treble_up`, `treble_down`, `tone_reset` | Tone (step: `tone_step_db`) |
| `motu_volume_up`, `motu_volume_down` | MOTU main output (step: `motu_step_db`) |
| `next_source` | Auto → each available source → Auto |
| `source_auto`, `source_streamer`, `source_gadget`, `source_toslink`, `source_analog` | Pick a source, or go back to automatic |
| `amps_off` | Turn the amp off now |
| `status` | Write the current state to the log |
| `restart_services`, `shutdown` | Only allowed on hold / long hold |

Changing speakers is deliberately not possible from the remote: the right
speakers have to be connected first, which the web UI asks you to confirm.

---

## AirPlay + Spotify Volume Sync

### What it does

Normally AirPlay and Spotify turn down the digital signal before CamillaDSP
sees it. With this installed, they send full-level audio and their volume
sliders control the CamillaDSP volume instead. It works both ways: turn it up
on the remote or web UI and the Spotify slider on your phone moves too.

### What you need

- **AirPlay:** `shairport-sync`. The installer backs up its config, changes
  only the volume settings, and puts the original back on uninstall.
- **Spotify:** a `raspotify` service. The installer builds a patched
  librespot 0.8.0 and switches back automatically if it doesn't start
  properly. Without `raspotify` this step is skipped. It uses raspotify's
  output device; set `SPOTIFY_ALSA_DEVICE` (see `aplay -L`) only to override
  it.

The AirPlay volume range is set with `AIRPLAY_VOLUME_MIN_DB` (default −50) and
`AIRPLAY_VOLUME_MAX_DB` (default 0).

### Stopping Squeezelite when AirPlay or Spotify starts

Optional: set `AIRPLAY_INTERRUPTED_LMS_PLAYERS` to a comma-separated list of
Squeezelite player names, and they are stopped when an AirPlay or Spotify
session starts. If the LMS server can't be reached, nothing breaks. Empty (the
default) turns this off.

---

## ISO 226 Loudness

### What it does

Our ears hear less bass and treble at low volume. This adds just the right
amount back as you turn the volume down, following the ISO 226 equal-loudness
curves, so music sounds balanced at any level.

It needs a patched CamillaDSP 4.1.3. Install All (option 1) builds it last;
option 9 builds it on its own, or rebuilds it. A normal update never touches
it. If the new build doesn't start, your old CamillaDSP is put back.

**Requirement:** `camilladsp.service` must start `/usr/local/bin/camilladsp`,
because that's the file it replaces. If your CamillaDSP is somewhere else,
Install All skips this step (and says so) and option 9 stops with the reason.

### Calibrating it

For the curves to be right, the system needs to know how loud "loud" is:

1. Set the volume to a comfortable level you'd normally listen at.
2. Play a **1 kHz sine tone** (not music or noise — the reading is only
   correct at 1 kHz).
3. Measure the sound level (SPL) at your listening position.
4. In the web UI, enter that reading as the **reference phon** (40–90), then
   turn the engine on.
5. Also enter the MOTU output level you measured at under **"MOTU main output
   at calibration"**, or press **"Use current levels"** while both are still
   where you measured.

After that, the correction follows both the CamillaDSP volume and the MOTU
output. So a source that plays with the MOTU at 0 dB instead of −6 dB gets
the right correction for how loud it really is. Leave the MOTU field empty to
calibrate on the CamillaDSP volume alone. If you built the engine before this
MOTU feature existed, rebuild it with option **9** to avoid a small click when
the MOTU level changes.

Technical background (accuracy, filter design) is in
[Under the hood](#under-the-hood).

---

## Web Control UI

### What it does

A web page (port 8088) for the whole system: volume and mute, source choice,
a parametric EQ with a response graph, loudness, speaker selection, service
status and restarts, live input meters and logs. Install it with option
**11**; it is not part of Install All.

### On your phone

The page opens on **Home**, a phone-sized remote with the playing source, a
level meter, big volume buttons, mute, source buttons, the MOTU output level
and an amp-off button. The Dashboard, Audio, Services and Logs tabs have
everything else. Everything updates live while the page is open.

To use it like an app, add it to your home screen: on iPhone, open
`http://<pi>:8088` in Safari, then Share → Add to Home Screen; on Android,
Chrome's ⋮ menu → Add to Home screen.

### Security — read this

The UI can start and stop system services, so it runs as **root** by design. By default
it is open to **anyone on your network, without a password**. Never expose it
to the internet. You can lock it down in the settings file:

| Setting | Default | Effect |
|---------|---------|--------|
| `INSTALLATION_UI_HOST` | `0.0.0.0` | `0.0.0.0` = whole network. `127.0.0.1` = this Pi only. |
| `INSTALLATION_UI_PORT` | `8088` | Port number |
| `INSTALLATION_UI_TOKEN` | *(empty)* | Set a secret to require it for any change |

Restart it after a change: `sudo systemctl restart cdsp-control-ui`. Option 11
offers to switch to `127.0.0.1` before it installs anything.

**This Pi only:** set `INSTALLATION_UI_HOST=127.0.0.1` and reach it from your
laptop through an SSH tunnel:

```bash
ssh -N -L 8088:127.0.0.1:8088 <user>@<pi>
# then open http://127.0.0.1:8088
```

**With a secret:** create one with `openssl rand -hex 32` and set
`INSTALLATION_UI_TOKEN=<that value>`. Open the page once as
`http://<pi>:8088/#token=<token>` and it remembers it; otherwise it asks the
first time you change something. Viewing still works without it. (API clients
send `Authorization: Bearer <token>`.)

Always on, whatever you configure: changes from other websites are refused,
and large or slow requests are cut off.

---

## Health Check

When something sounds wrong, after an OS upgrade, or before asking for help,
run the health check. Use installer option **12**, the **"Run health check"**
button on the web UI's Services tab, or:

```bash
~/camilladsp/.venv/bin/python3 ~/camilladsp/scripts/diagnose.py      # add --json for machine-readable output
```

It checks the Python version, disk space, Pi power problems (under-voltage),
the settings file, every service, the CamillaDSP connection, that every config
is valid, the speaker profiles, the MOTU connection, the remote and key map,
and recent errors in the logs. It only reads — it never changes anything.

---

## Settings

All your settings are in **`~/camilladsp/cdsp-automation.env`**. Updates
never overwrite it. After changing it, restart the affected service (or
re-run the installer's update).

| Setting | Default | Used by |
|---------|---------|---------|
| `CDSP_HOST`, `CDSP_PORT` | `127.0.0.1`, `1234` | Everything |
| `CDSP_CONFIG_DIR` | `~/camilladsp/configs` | Source Switcher |
| `SOURCE_OVERRIDE_PATH` | `/run/cdsp-source-switcher/manual_source` | Source Switcher |
| `SOURCE_VOLUME_MEMORY` | `1` | Source Switcher |
| `MOTU_WS_URL` | `ws://169.254.51.193:1280` | Source Switcher |
| `MOTU_MAIN_VOLUME_MAX_DB` | `0` | MOTU volume limit |
| `POWER_GPIO` | `4` | Trigger Control |
| `REMOTE_NAME` | `HID Remote01 Keyboard` | Remote Control |
| `REMOTE_VOLUME_MIN`, `REMOTE_VOLUME_MAX` | `-80`, `0` | Remote Control |
| `AIRPLAY_VOLUME_MIN_DB`, `AIRPLAY_VOLUME_MAX_DB` | `-50`, `0` | AirPlay sync |
| `AIRPLAY_INTERRUPTED_LMS_PLAYERS` | *(empty)* | AirPlay + Spotify sync |
| `SPOTIFY_ALSA_DEVICE` | *(empty)* | Spotify sync |
| `INSTALLATION_UI_HOST`, `_PORT`, `_TOKEN` | `0.0.0.0`, `8088`, *(empty)* | Web UI |
| `SITE_NAME` | `CamillaDSP` | Web UI |

Timings and thresholds are constants at the top of each script; shared paths
are in `scripts/settings.py`.

---

## Speakers and EQ

You can switch between several speaker setups (for example your main speakers
and a party or measurement setup) from the web UI. Each speaker has its own
EQ, bass/treble, loudness and volume limit.

- The **default** speaker uses your own config files from
  `~/camilladsp/configs/` as they are.
- Other speakers are **profiles**: a speaker part (crossover, delays, output
  routing) that is combined with a small per-source input part. They live in
  `/etc/cdsp-automation/speaker-profiles` and
  `/etc/cdsp-automation/source-bases`. See `speaker-profile.example.yml` and
  `source-base.example.yml`. A profile only shows up when it has
  `enabled: true` and all its source parts exist.
- To replace the list of speakers, write
  `/etc/cdsp-automation/speaker-catalog.json` (see
  `speaker-catalog.example.json`). If the file is broken, a warning is logged
  and the built-in list is used.

The built-in speakers (Kantarellen as the default, PartyMEH, Measurement) are
the maintainer's own; replace them with your own catalog. Only enable a
profile after you have measured its crossover, polarity, delay, gain and
channel routing. Changing speaker is always muted,
checked, and rolled back if it fails.

**Don't add your own filters named `Bass`, `Treble` or `loudness`** to your
configs. The Source Switcher adds its own EQ and tone filters and removes old
ones with those names, so they don't stack up.

The full rules for profiles are in [TECHNICAL.md](TECHNICAL.md).

---

## Managing services

| Service | Part |
|---------|------|
| `cdsp-source-switcher` | Source Switcher |
| `cdsp-trigger` | Trigger Control |
| `cdsp-remote` | Remote Control |
| `airplay-volume-bridge` | AirPlay + Spotify Volume Sync |
| `cdsp-control-ui` | Web Control UI |

Use the service name in these commands:

```bash
systemctl status cdsp-remote               # is it running?
journalctl -u cdsp-remote -f               # follow the log live
journalctl -u cdsp-remote -n 100           # last 100 log lines
sudo systemctl restart cdsp-remote         # restart (also: start, stop)
sudo systemctl enable cdsp-remote          # start at boot (disable: don't)
```

Restarting `camilladsp` also restarts the Source Switcher.

---

## Troubleshooting

Start with the [health check](#health-check) — it often names the problem.

### Remote not working

- Is the remote seen at all? Run the command from
  [step 2](#setting-it-up). If it's missing, check
  `bluetoothctl devices Connected` and re-pair with option 6.
- `REMOTE_NAME` in the settings file must match the name **exactly**.
- Test the buttons: `sudo systemctl stop cdsp-remote`, then
  `~/camilladsp/.venv/bin/python3 ~/camilladsp/scripts/cdsp_remote.py --learn`.
- Check the log: `journalctl -u cdsp-remote -n 100`.
- **Bass/treble do nothing?** Make sure `cdsp-source-switcher` is running —
  it is what applies tone changes.

### Amp not switching on

- Check the wiring: GPIO 4 → relay IN, and 5V/ground to the relay.
- Is the user allowed to use GPIO? `ls -l /dev/gpiochip0` should show the
  `gpio` group. If your user isn't in it: `sudo usermod -aG gpio $USER`, then
  log out and back in.
- Test the relay by hand: `sudo systemctl stop cdsp-trigger`, then
  `pinctrl set 4 op dh` (on) and `pinctrl set 4 op dl` (off). Start the
  service again afterwards.
- Check the log: `journalctl -u cdsp-trigger -n 100`.

### MOTU clock not switching

- Can the Pi reach the MOTU? `ping 169.254.51.193` (or your MOTU's IP).
- Is the control port open? (This briefly disconnects the Source Switcher.)

  ```bash
  timeout 2 bash -c '</dev/tcp/169.254.51.193/1280' && echo open
  ```

- Check the log: `journalctl -u cdsp-source-switcher -n 100 | grep MOTU`.
- **Other MOTU models:** the clock values come from MOTU's CueMix 5 app (for
  the UltraLite, `dev.js`: `kClockSource` is id 11 with Internal=3,
  S/PDIF=0, Optical=2). Other gen5 models may differ — check the matching
  `dev_*.js` and update `MOTU_CLOCK_WRITES` and `MOTU_CLOCK_NAMES` in
  `source_switcher.py`. MOTU's AVB interfaces are not supported.

### Source not switching

- Do the configs exist and are they valid?

  ```bash
  ls -l ~/camilladsp/configs/        # toslink.yml, streamer.yml, gadget.yml
  camilladsp -c ~/camilladsp/configs/toslink.yml
  ```

- Turn on debug output: set `DEBUG_MODE = True` near the top of
  `~/camilladsp/scripts/source_switcher.py`, run
  `sudo systemctl restart cdsp-source-switcher`, then
  `journalctl -u cdsp-source-switcher -f`. You'll see lines like:

  ```
  DEBUG: Streamer HW=True, Gadget HW=False, TOSLINK meter=False/0/5, Analog meter=False/0/30, Last=streamer, ST=0, GT=0
  -> Streamer: audio active
  -> Streamer: idle 5/60s
  ```

- Is the streamer input active? `cat /proc/asound/Loopback/pcm0p0/sub0/status`
  should say `state: RUNNING` while playing.
- Is a USB computer connected?
  `amixer -c UAC2Gadget contents | grep "Capture Rate" -A 1` should show a
  rate that isn't zero.
- Is TOSLINK detection working?
  `journalctl -u cdsp-source-switcher -n 100 | grep "MOTU connected"`.

---

## What gets installed

- `~/camilladsp/scripts/` — the Python scripts
- `~/camilladsp/configs/` — your CamillaDSP configs (you create these)
- `~/camilladsp/.venv/` — a Python environment for the scripts
- `~/camilladsp/cdsp-automation.env` — your settings
- `/var/lib/cdsp-automation/` — saved state (EQ, volumes, speaker choice)
- `/etc/cdsp-automation/` — optional site files (key map, speaker catalog,
  profiles)
- The systemd services listed under [Managing services](#managing-services)
- System packages: `python3-venv`, `python3-rpi-lgpio`, `alsa-utils`, `bluez`,
  `wget`, `curl`, `git`, plus build tools (`cargo`, `build-essential`,
  `pkg-config`, `libasound2-dev`, `libssl-dev`) for the patched CamillaDSP and
  librespot
- Python packages: `pycamilladsp`, `websocket-client`, `evdev`, `pyyaml`

Uninstall (option 10) removes the services, unit files and sudoers rules. It
keeps your configs, settings and saved state.

---

## Under the hood

Details for the curious; you don't need these to use the system. More is in
[TECHNICAL.md](TECHNICAL.md).

**One writer.** The Source Switcher is the only program that loads configs
into CamillaDSP. For every source it combines your config with the selected
speaker's saved EQ and loudness (from `/var/lib/cdsp-automation/speaker-audio`;
the default speaker keeps using `/var/lib/cdsp-automation/audio-eq.json`). The
web UI and remote only edit that saved state.

**Shared lock.** Everything that changes volume or mute takes the lock
`/var/lib/cdsp-automation/audio-control.lock`. The switcher holds it for a
whole switch (mute → load → check → apply EQ → restore mute), so a volume
change made mid-switch simply waits. At startup, and whenever it reconnects to
CamillaDSP, the switcher re-applies the current config the same way. If
CamillaDSP crashes and restarts by itself, there can be a short moment before
the switcher notices.

**MOTU connection.** The UltraLite accepts one control connection at a time,
so the switcher's single connection carries the meters, clock changes and the
MOTU volume. The MOTU reports its full state on connect and every change
after, so the switcher never rewrites a clock that is already right (each
clock change makes the MOTU re-lock audibly). Clock changes wait until the
output is confirmed silent (below −100 dB), then wait 1 s for the re-lock. A
clock change that failed, or one made in CueMix 5, is fixed later in its own
short muted step, at most every 30 s. Which clock to use comes from the
source's name in the speaker catalog, not from its sample rate.

**Speaker profile rules.** Every output must be declared active or muted, and
the last step in the pipeline must be an output mixer. A typical three-way
setup is: stereo source → expansion mixer → per-output crossover/delay/gain →
final 1:1 output mixer. A Measurement profile must set both
`raw_measurement: true` and `bypass_user_eq: true`; it then allows no filters
except its own output mixer, and its output level is up to you. Generated
configs are written to fixed, content-addressed files and checked with
`camilladsp -c` before loading.

**ISO 226 maths.** The curves use the ISO 226:2003 model as an approximation
of the 2023 revision (at most 0.6 dB different; the licensed 2023 data is not
included). The listening level is calculated as
`reference phon + (volume − reference volume) + (MOTU − reference MOTU)`.
Reference phon is limited to 40–90 because the standard stops there (and at
80 phon above 4 kHz, so 81–90 extrapolates the top). The filter is one gain
plus nine high shelves, solved per sample rate to stay within 0.6 dB of the
target from 20 Hz to 12.5 kHz, and it never boosts more than the volume
control has already turned down. Changes are crossfaded between audio chunks;
switching instantly caused about 30 dB more distortion during volume changes.
After installation, `/var/lib/cdsp-automation/iso226-engine.json` records the
engine, and the UI only offers ISO 226 loudness while that file exists.

---

## Contributing

Contributions are welcome:

1. Fork the repository
2. Create a feature branch
3. Test on your hardware
4. Open a pull request

## License

MIT License — see [LICENSE](LICENSE). Third-party notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Acknowledgments

- [CamillaDSP](https://github.com/HEnquist/camilladsp) by HEnquist
- [pycamilladsp](https://github.com/HEnquist/pycamilladsp) Python library
- [RPi-CamillaDSP](https://github.com/mdsimon2/RPi-CamillaDSP) — RPi-focused CamillaDSP setup by mdsimon2
- [Display, remote and trigger power for CamillaDSP streamer and preamp — Audio Science Review thread](https://www.audiosciencereview.com/forum/index.php?threads/display-remote-and-trigger-power-for-camilladsp-streamer-and-preamp-alternative-to-mdsimon2%E2%80%99s-implementation.52818/) — community discussion and alternative implementations

## Support

- **Bugs:** open a GitHub issue
- **Questions and ideas:** use GitHub Discussions
