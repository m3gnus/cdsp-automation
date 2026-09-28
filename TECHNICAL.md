# CamillaDSP Automation Utilities for Raspberry Pi

## Settings

Shared paths (state under `/var/lib/cdsp-automation`, site files under
`/etc/cdsp-automation`, runtime files under `/run`) are constants in
`scripts/settings.py`; each script keeps its own timings as constants at its
top. Only what varies per site is read from `cdsp-automation.env`, which every
unit loads with `EnvironmentFile=`: `CDSP_HOST`, `CDSP_PORT`,
`CDSP_CONFIG_DIR` (the env file is its sibling), `SOURCE_OVERRIDE_PATH`,
`POWER_GPIO`, `MOTU_WS_URL`, `MOTU_MAIN_VOLUME_MAX_DB`,
`SOURCE_VOLUME_MEMORY`, `REMOTE_NAME`,
`REMOTE_VOLUME_MIN`/`MAX`, `AIRPLAY_VOLUME_MIN_DB`/`MAX_DB`,
`AIRPLAY_INTERRUPTED_LMS_PLAYERS`, `SPOTIFY_ALSA_DEVICE`, `SITE_NAME` and
`INSTALLATION_UI_HOST`/`PORT`/`TOKEN`. Other keys in the file are ignored.
The AirPlay callback copy in `/usr/local/libexec` gets `settings.py` beside it.

## Speaker-profile contract

Speaker selection is stored as a versioned, compare-and-swap document at
`SPEAKER_SELECTION_PATH`. Non-Kantarellen definitions use profile schema 1 and
must explicitly provide: `id`, `enabled`, `supported_sources`,
`output_channels`, complete `active_outputs`/`muted_outputs`, one role per
output, a non-positive `max_volume_db`, `bypass_user_eq`, `raw_measurement`,
and a
CamillaDSP fragment. Profile-owned filter/mixer/processor names use the
`spk_<id>_` prefix. The profile owns playback and output routing; the source
base owns capture, samplerate, clock-related device settings, and (for normal
speaker profiles) pre-routing source filters. Source bases may not own mixers
or processors. A `raw_measurement: true` Measurement profile additionally
requires `bypass_user_eq: true`, empty source filters/pipeline, and empty
profile filters/processors; only its explicit output mixer remains.

Every program is the two-channel main one; a source base places it in its
capture stream with `program: {main: [left, right]}` (default `[0, 1]`). The
retired secondary-"stereo"-program and `capabilities` features
(`secondary_program`, `meter_bands`) are accepted and ignored when older
profiles or crossover documents still carry them — including the persisted
`crossover.program_channels: 2` key.

The compiler rejects coercion in safety fields: YAML booleans must be real
booleans and channel/output indices must be real integers. The terminal mixer
must cover exactly every playback destination, muted destinations must have no
sources, and active destinations must have valid sources. CamillaDSP's offline
checker is an additional validation layer, not a replacement for this semantic
contract.

Generated files live under
`SPEAKER_GENERATED_DIR/<sha256>/<source>--<speaker>.yml`. Both the digest and
managed-path provenance are checked before use.

### Config transitions

Every config switch runs inside the shared audio-control lock
(`AUDIO_CONTROL_LOCK_PATH`): read the listener's mute state → mute →
integrity/selection checks → MOTU clock (if it changes) → reload → check →
EQ overlay → volume clamp → publish status → restore the listener's mute
state. The file is checked with `camilladsp -c` (`validate_config_file`)
before the reload. After the reload, `SETTLE_TIME` (2 s; 1.5 s
for the USB gadget) is only a grace period before the first look; the check
is simply that the engine reports Running or Paused on the requested config
path within `CONFIG_APPLY_TIMEOUT` (10 s, measured from the reload), so a
slow Starting phase is not mistaken for a failure.

Any failure reloads the previous config (checked the same way) and leaves the
engine muted: the switcher never unmutes after an uncertain transition. The
listener unmutes when they choose to, and until a later apply succeeds the
controls hold the fail-safe volume ceiling (below).

The remote, the AirPlay/Spotify bridge and the control UI take the same lock
around every volume or mute write, so a change made mid-switch simply waits
for the switch to finish. There is no other coordination: no ready token, no
mute-request file.

The switcher keeps the engine muted until it has applied a config on its
current CamillaDSP connection. When it starts, and whenever it reconnects, it
reads the listener's mute state, mutes, and remembers that state; boot
recovery (below) then runs muted, and the first successful apply - a re-apply
of the current managed config, a speaker change, or a source switch - restores
it. Any unhandled error in the loop starts this over. A speaker change from the
control UI only commits the new selection; the switcher's next pass (within
about a second) does the muted switch itself.

Accepted gap: CamillaDSP 4.1.3 exposes no process identity, so an engine that
crashes and restarts by itself is noticed only when the switcher's websocket
reconnects. Until then a control could unmute the restarted engine. An
explicit engine restart restarts the switcher too (`PartOf=`).

CamillaDSP answers `SetConfig` and `SetConfigValue` once the change is
*queued*, not applied, so the EQ overlay write (including the measurement
bypass) polls for its own expected state against `CONFIG_APPLY_TIMEOUT`
instead of reading back once. An EQ write is confirmed only when the whole
filter set and pipeline match, so a bypass that has not yet removed a legacy
Bass/Treble/Loudness stage, or an engine reporting no config, never reads as
done.

### Volume limits

A profile's `max_volume_db` is enforced through CamillaDSP's native
`devices.volume_limit`, never through a one-time clamp. For generated configs
the compiler writes it (keeping a stricter limit the source base already
declared). An operator-owned config is the operator's artifact, so it is
validated instead of rewritten: it must itself declare a `devices.volume_limit`
at least as restrictive as the profile's cap, or both selection preflight and
the transition refuse it by name.

A successful apply publishes the effective ceiling as `volume_limit_db` in the
speaker-profile status at `SPEAKER_STATUS_PATH`. Every volume writer — the HID
remote, the control UI, and the AirPlay/Spotify bridge — derives its maximum
from that verified value rather than a constant of its own. Anything short of
a successful apply that recorded a ceiling (no status, `ok` not true, a missing
or malformed value) means the most restrictive sane ceiling
(`speaker_profiles.FAILSAFE_VOLUME_LIMIT_DB`), never 0 dB. `REMOTE_VOLUME_MAX`
survives as a deployment's own preference but can only tighten that ceiling.

### Per-source volume memory

Each speaker/source pair remembers the CamillaDSP Main level and the MOTU main
output level it last played at, in `SOURCE_VOLUME_PATH`
(`scripts/source_volume.py`). Inside a source or speaker transition, while
muted and under the audio-control lock, the switcher records the outgoing
pair's levels and starts the incoming pair at the ones it remembers; the
CamillaDSP level is still clamped to the new profile's ceiling and the MOTU
level to `MOTU_MAIN_VOLUME_MAX_DB`, and the MOTU is written only on an open
connection whose level the device confirmed, with the usual main-group and
expected-level checks. A pair with nothing remembered keeps the level that
was playing. A transition that fails puts the previous levels back with the
previous config, so an unmute after a rollback is never louder than before.

While a source plays, its levels are recorded every
`SOURCE_VOLUME_RECORD_SECONDS` (10 s), and only when they changed. Nothing is
recorded until a config has been applied on the current CamillaDSP connection,
so a fail-safe ceiling is never remembered as a level. The memory is a
convenience: an unreadable or unwritable file is logged and never fails a
transition. The control UI shows the remembered levels and edits those of
sources that are not playing (`POST /api/source-volume`). Set
`SOURCE_VOLUME_MEMORY=0` to carry the volume over between sources instead.
A network receiver that sends its own volume when a session starts (AirPlay)
still sets the level after the switch, as it always has.

I've created Python utilities that automate common tasks when using CamillaDSP on a Raspberry Pi. Trigger control runs on its own. The source switcher is the control core, and the MOTU's only client (clock, meters, main volume): it is the only thing that applies a persisted tone/EQ or speaker change and the only publisher of the volume ceiling, so the remote control (and the AirPlay/Spotify volume bridge and web UI described later) require it.

## Installation

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh && ./install.sh
```

The installer provides a menu to install utilities individually or all at once, and sets up systemd services for each one. The entries that install a component depending on the source switcher (menu options 5, 8 and 11) install the switcher too when it is missing, say so before they act, and list it in their closing summary.

Python 3.10 or newer is required.

---

## 1. Trigger Control (GPIO Relay)

### What it does (simple):

Automatically turns on a GPIO pin when music is playing and turns it off after 5 minutes of silence. Perfect for controlling amplifier power via a relay.

### How it works (detailed):

The script continuously monitors CamillaDSP's capture RMS levels every 200ms by default. When any channel is above the configured activity threshold (`AUDIO_THRESHOLD_DB`, `-80` dB), it immediately sets GPIO pin 4 HIGH. When silence is detected, it starts a 320-second countdown timer. Only if silence persists for the full duration does it set the pin LOW.

**Why this approach:**

- **200ms polling interval** - Fast enough to catch audio immediately, but not so frequent it wastes CPU
- **320-second timeout** - Long enough to handle natural gaps in music (between tracks, quiet passages) without constantly cycling your amplifier on/off, which could cause pops or reduce component life
- **RMS threshold** - `-80` dB, avoiding a hard dependency on sentinel values and keeping quiet-but-real audio detectable
- **Uses lgpio** - The modern GPIO library that works with current Raspberry Pi OS versions (RPi.GPIO is deprecated)
- **Manual off without disabling automation** - `SIGUSR1` drops the relay
  immediately and suppresses the current continuous audio session; after
  silence, the next audio activity turns the relay on normally

**Practical use:** Connect a 5V relay module to GPIO 4 and ground. Use the relay's normally-open contacts to send the Pi's 5V output to an amplifier trigger input that accepts it.

---

## 2. MOTU Control (inside the Source Switcher)

### What it does (simple):

Keeps the MOTU UltraLite mk5's clock source in step with the active source
(TOSLINK selects optical; streamer, gadget and analog select internal,
regardless of sample rate), and lets the control UI set the MOTU's main
output volume. There is no separate service: the source switcher does both.

### How it works (detailed):

The UltraLite mk5 has no HTTP API (port 80 closes every request unanswered;
the HTTP datastore belongs to MOTU's AVB interfaces). Its whole control
surface is the binary WebSocket on port 1280 (`MOTU_WS_URL`) that MOTU's
CueMix 5 app uses, and it serves **one client at a time**: every new
connection drops the previous one. So the source switcher is the only client
on the Pi (`MotuConnection` in `scripts/source_switcher.py`). It holds one
connection and uses it for everything, as CueMix 5 itself does:

- **Meters** - the passive meter frames that detect TOSLINK and analog input
  (section 3).
- **State** - on every new connection the device pushes its whole parameter
  set unsolicited (one `id, index, value` frame per parameter), then the meter
  stream, and afterwards pushes any parameter that changes. The switcher reads
  that dump before using a fresh connection (up to `MOTU_STATE_TIMEOUT`,
  3 s), so the clock source (parameter 11, `kClockSource`: Internal=3,
  S/PDIF=0, Optical=2) and the main volume are known without asking. While
  the switcher is disconnected - for example while CueMix 5 holds the device -
  both are unknown, and nothing is written on a guess.
- **Writes** - CueMix 5's own layout, `id, index, length, value`, on the same
  socket: `000b0000000103` for internal, `000b0000000102` for optical.

**Clock changes inside the mute window.** While `SOURCE_MOTU_CLOCK` is true
(set `False` in `source_switcher.py` to leave the clock alone), every clock
write is one operation:
under the audio-control lock, with mute requested, the switcher first waits
for the output to actually go silent, then writes, then waits
`MOTU_CLOCK_SETTLE_SECONDS` (1 s) for the re-lock, then reconnects so the fresh
state dump - not its own belief - says whether the device took the value.
"Silent" is not the mute flag: CamillaDSP ramps mute over `volume_ramp_time`
(400 ms by default), and behind the ramp sit up to `queuelimit` processed
chunks plus the `target_level` device buffer. The switcher waits out the ramp,
requires the playback peak meter to read at or below `MOTU_CLOCK_SILENT_DB` (-100 dB)
over the queued-audio window, then lets that span drain. The meter only
reports chunks it played, so an idle engine returns an empty history; that
counts as silence only when the engine state was also read successfully as
`Paused` or `Inactive`. A failed or malformed meter reading, `Starting`,
`Stalled` or an unrecognized state is "unknown" and keeps waiting. If silence
is not confirmed within `MOTU_CLOCK_SILENCE_TIMEOUT` (1 s) past the ramp, the clock
is not written.

In a source transition that operation runs after the integrity and selection
checks and *before* the reload, so the new graph opens the interface on a
clock that has already re-locked. A transition that rolls back restores the
previous clock the same way before reloading the previous graph. A clock the
device already reports is not written again: every write re-locks and clicks.

A failed write does not fail the transition, but it is never retried on live
audio. When the device reports a clock other than the source's - a write that
failed or did not take, or a clock changed from CueMix 5 - the switcher's loop
notices and, once a config has been applied and at most once per
`MOTU_CLOCK_RETRY_SECONDS` (30 s), runs a small muted correction under the lock:
mute, the same silent write, settle and check, then the listener's previous
mute state.

**Note:** The parameter ids and values are for the MOTU UltraLite mk5, taken from CueMix 5's `dev.js`. Other MOTU models may use different parameters - check that model's `dev_*.js` in CueMix 5.

### MOTU main output volume (control UI)

The control UI's "MOTU main output" card replaces CueMix 5's main volume knob,
which cannot reach the MOTU while it hangs off the Pi's USB network. It is
CueMix's `kMainTrim`: parameter 5011, one byte of attenuation in dB (6 = -6 dB,
100 = -inf). It scales every output enabled in `kMainGroup` (parameter 5012,
an int16 bit per output DAC). On this unit that group is `0x03ff`, meaning all
ten analog line outputs. So the high (Main 1-2), mid (Line 3-4) and low
(Line 5-6) crossover pairs always move together. A write is refused if the
group ever stops covering all of them.

The UI never connects to the MOTU. A change is a small request file,
`MOTU_VOLUME_REQUEST_PATH` (`/run/cdsp-source-switcher/motu-volume-request.json`),
which the switcher takes on its next pass (about every 1.2 s) and applies on
its connection. It publishes the device's level and the outcome of the last
request in `MOTU_VOLUME_STATUS_PATH`
(`/run/cdsp-source-switcher/motu-volume.json`); the UI reads that for every
GET and waits up to 3 s for its request's outcome. A switcher busy inside a
source change answers later: the UI replies 202 and the page reads the
published level again shortly after. Only the newest request is kept, so a
slider drag never queues. Protocol helpers and the request/status plumbing
live in `scripts/motu_volume.py`.

- **Ceiling:** `MOTU_MAIN_VOLUME_MAX_DB` (default `0`, the top of the MOTU's
  main attenuator -- the same range as its front-panel knob). Set it lower to
  cap the control. The switcher enforces it when it applies a request, because
  the MOTU sits after CamillaDSP and the profile volume limits cannot bound
  it. An unparseable value disables writes.
- **No jumps:** the page starts the slider from the level the device reported,
  and shows `unknown` with no slider when it has not. Every write names the
  level it replaces and is refused (409) if the device reports anything else,
  for example after the front-panel knob moved.
- **Uncertain writes:** a send that raises may still have reached the device,
  so the switcher drops the connection and the level is `unknown` (503)
  rather than kept as confirmed, and nothing is resent; the next connection's
  state dump settles it. A level we wrote reads "sent" until the device
  pushes it back.

---

## 3. Source Switcher

### What it does (simple):

Automatically switches between CamillaDSP configs based on which audio source is active. Priority: manual override → current active source → AirPlay Streamer → USB Gadget → TOSLINK meter detection → optional analog meter detection.

### How it works (detailed):

The script checks multiple hardware indicators every second:

1. **AirPlay/Streamer**: Reads `/proc/asound/Loopback/pcm*/sub*/status` to see if ALSA Loopback is in RUNNING state
2. **USB Gadget**: Executes `amixer` to check if UAC2Gadget's capture rate is non-zero (indicating a connected USB host)
3. **TOSLINK**: Reads passive MOTU UltraLite mk5 meter frames over the CueMix WebSocket and watches the configured optical input meter pairs
4. **Analog**: Optionally reads MOTU meter pairs for analog inputs; disabled by default until the input mapping is verified
5. **RMS monitoring**: After switching streamer or gadget configs, it monitors RMS levels to detect actual audio activity vs. hardware just being "ready"

When a higher-priority source becomes active, it immediately switches configs. When a source goes silent, it waits 60 seconds before considering lower-priority sources, preventing rapid switching during track changes or brief pauses.

**Why this approach:**

- **Manual override first** - If `SOURCE_OVERRIDE_PATH` contains `toslink`, `streamer`, `gadget`, or `analog`, the switcher pins that config and skips automatic arbitration until the override is cleared or set to `auto`.
- **Current source hold** - If the current source still has confirmed audio, it keeps control even if another source also appears active. The ordered priorities are used only when choosing a new source.
- **Hardware state checking** - Looking at `/proc/asound` and `amixer` output gives us reliable, kernel-level information about audio hardware state
- **MOTU meter detection** - TOSLINK is detected from live MOTU input meter frames instead of being treated as always active
- **Three-state detection** - A source is *ready* (hardware says the stream is open), *playing* (capture RMS confirms audio), or *probed and found silent*. Capture levels only describe the selected config, so for the streamer and the USB gadget "playing" is simply unknown until the switcher selects them. The arbitration logic lives in one pure function, `source_switcher.arbitrate()`, which takes a snapshot of every source plus the elapsed pass time and returns the decision; `main()` only gathers the snapshot and applies the result. The elapsed time is the measured monotonic interval since the previous arbitration, capped at five check intervals so one stalled pass (a config apply, a reconnect) cannot satisfy a silence or dwell timeout by itself.
- **Silent-probe backoff** - Selecting a source to find out whether it is playing costs a full config reload plus a mute/restore, so a probe that hears nothing is remembered. The source is not re-probed for `PROBE_BACKOFF_SECONDS` (30 s), growing by `PROBE_BACKOFF_FACTOR` (4) up to `PROBE_BACKOFF_MAX` (900 s). Without this, two ready-but-silent inputs alternate forever, because readiness alone requalified each one as soon as the other timed out. Confirmed audio, a manual override, or the hardware genuinely going away and coming back all clear the backoff.
- **Probe window vs track gap** - `IDLE_TIMEOUT` (60 s) is the grace a source gets *after its playback has been confirmed*, so a quiet passage or a pause does not lose it. A source that has only ever proved ready gets the much shorter `PROBE_SILENCE_TIMEOUT` (5 s) instead: there was no music to leave a gap in.
- **Grace periods** - The 60-second timeout and "last active source" tracking ensure the switcher doesn't jump away from a source it has heard playing just because of a quiet passage or pause button
- **Fast lower-priority handoff** - If streamer or gadget is silent while TOSLINK/analog MOTU meters are active, `LOWER_PRIORITY_ACTIVE_TIMEOUT` (0 s) lets the switcher fall through sooner than the normal track-gap timeout.
- **Confirmed audio pre-empts a grace** - A rival that has been *confirmed playing* for `PREEMPT_DWELL_SECONDS` (2 s) cuts a silent source's grace short instead of waiting it out; a higher-priority rival waits for nothing else, a lower-priority one is additionally gated by `LOWER_PRIORITY_ACTIVE_TIMEOUT`. The dwell keeps a single noisy meter frame from yanking the config away mid-track. A rival that is only *ready* never pre-empts - protecting a track gap from a connected-but-paused source is the entire point of the grace.
- **Meter sources get no second grace** - `toslink_available` / `analog_available` only go false after `TOSLINK_IDLE_SECONDS` (5 s) / `ANALOG_IDLE_SECONDS` (30 s) of quiet meters, so a meter source has already served a track-gap grace by the time it reads silent. `SourceSnapshot.self_metering` marks that, and such a source is released as soon as its meter settles rather than holding the output for another `IDLE_TIMEOUT`. It is never probed either, so it is never backed off: its meter requalifies it the instant signal returns.
- **RMS level threshold** - Audio is treated as active when any capture channel is above `AUDIO_THRESHOLD_DB` (`-80` dB). This keeps steady tones, quiet sustained passages, and compressed audio from being mistaken for silence.
- **Keep-last idle behavior** - When all sources are idle, the current config is left alone (`SOURCE_IDLE_MODE = "keep-last"`; `"toslink"` falls back to TOSLINK instead).
- **Settle time** - After switching configs, the script waits 2 seconds for hardware to reinitialize, preventing glitches
- **Boot-race recovery** - If CamillaDSP remembers a config path but started before its audio device existed, the switcher reloads that existing config while processing is `INACTIVE`; healthy `PAUSED`/`RUNNING` configs are left untouched. The switcher has already muted (and remembered the listener's mute state) when it connected; recovery mutes again and reloads only once the engine reads back as muted. If the lock, the mute request or its read-back fails, that attempt is skipped. The next pass then re-applies the recovered config through the normal muted transition, which restores the remembered mute state

**Priority logic explained:**

- **Manual override** - Used for sources that are hard to auto-detect, such as analog input. The override file is transient by default under `/run`.
- **Current active source** - The currently selected config keeps priority while its audio is still active
- **Priority 1: Streamer** - First automatic choice when changing sources
- **Priority 2: USB Gadget** - Direct USB connection (phone, laptop) is secondary
- **Priority 3: TOSLINK** - Optical input becomes active when configured MOTU meter pairs show signal
- **Priority 4: Analog** - Optional, disabled by default. Enable only after confirming the correct MOTU meter pairs for the analog input channels

**Config requirements:** You need to create three config files:

- `toslink.yml` - Configured for optical input
- `streamer.yml` - Configured for ALSA Loopback (from Squeezelite/AirPlay)
- `gadget.yml` - Configured for USB Gadget (Pi Zero as USB sound card)

Optional configs:

- `analog.yml` - Configured for analog inputs, selectable manually or by setting `ANALOG_MOTU_METERS = True` in `source_switcher.py`

---

## 4. Remote Control (Bluetooth/USB HID)

### What it does (simple):

Lets you control CamillaDSP volume, mute, bass, and treble using a Bluetooth or USB remote control.

### How it works (detailed):

The script uses the `evdev` library to capture raw input events from the HID device. It runs an async event loop that processes key press, hold, and release events, translating them into CamillaDSP API calls via `pycamilladsp`.

**Event handling:**

- **keystate == 1** (pressed): Immediate action for volume/tone changes
- **keystate == 2** (held): Continuous volume adjustment when holding volume keys, tone reset trigger when holding ENTER
- **keystate == 0** (released): Status display on short ENTER press, counter resets

**Why this approach:**

- **evdev for input** - Direct kernel-level access to input events, works with any HID device that registers as a keyboard
- **Async event loop** - Non-blocking event processing allows the script to handle rapid button presses and long holds without lag
- **Separate tone step** - Bass/treble use 0.5dB steps for fine adjustment, while volume uses 1dB steps for faster changes
- **Tone limits** - A ±6dB range prevents accidental over-boosting
- **Persistent tone control** - Atomically updates reserved `low`/`high` shelf IDs in the shared audio overlay; the source switcher applies them and remains the sole live-config writer
- **Device reconnection** - If the Bluetooth remote disconnects, the script automatically searches for it again
- **Recovery controls stay available** - A failed CamillaDSP connection does not block the HID event loop, so the power-button restart and shutdown actions still work
- **Throttled idle logging** - The remote is checked every two seconds while asleep, but unchanged "not found" status is logged only every five minutes

**Button mapping:**

| Button | Press | Hold |
|--------|-------|------|
| Volume Up | +1dB | Continuous +1dB |
| Volume Down | -1dB | Continuous -1dB |
| Mute | Toggle | - |
| Up Arrow | Treble +0.5dB | - |
| Down Arrow | Treble -0.5dB | - |
| Right Arrow | Bass +0.5dB | - |
| Left Arrow | Bass -0.5dB | - |
| Enter | Show status | Reset tone to 0dB |
| Power | - | ~1s: Restart services, ~10s: Shutdown |

**Power button service restart:**

Holding the power button for ~1 second restarts the CamillaDSP control stack:
- camilladsp.service
- camillagui.service
- cdsp-source-switcher.service
- cdsp-remote.service

The trigger service deliberately stays running: it replaces its stale
CamillaDSP client and reconnects in place, keeping the GPIO relay latched while
the audio stack restarts. Remote self-restart uses one exact nonblocking
`systemctl` command after the other restarts. This avoids waiting on the service
issuing the command and keeps the sudo authorization narrow—there are no
wildcard arguments that could be expanded into another root command.

Holding for ~10 seconds triggers `systemctl poweroff`.

**Finding your remote:**

After pairing a Bluetooth remote, find its device name:

```bash
python3 -c "import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])"
```

The script searches for the configured `REMOTE_NAME` from `~/camilladsp/cdsp-automation.env` and waits if it's not found (useful for remotes that auto-sleep).

**CamillaDSP requirements:**

Tone control is stored in `/var/lib/cdsp-automation/audio-eq.json` and applied
through the source switcher's owned `cdsp_ui_eq_*` overlay. The remote adjusts
the reserved low/high shelf bands. Source configs must not add separate
`Bass`, `Treble`, `Loudness`, or `Iso226` stages because those would stack with
the owned overlay; legacy stages are stripped when a config becomes active.

---

## 5. Web Control UI (optional)

`scripts/web_ui.py` is a single-file stdlib `http.server` dashboard installed
by menu option 11 as `cdsp-control-ui.service`. It reuses the sibling modules
in `scripts/` (`audio_eq.py`, `speaker_profiles.py`, `speaker_config.py`,
`speaker_xo.py`) and the shared `cdsp-automation.env`, and it respects the
single-writer contract: every EQ or speaker edit goes through the persistent
state files and is composed into the live config by the source switcher.

The unit deliberately runs as root because the UI starts, stops and restarts
services. Users who do not want a root web
service simply skip this component — nothing else depends on it.

### Request guards

Two guards are unconditional, because they are safe whatever the
configuration:

- **Origin validation.** Every state-changing request (every `POST`) is
  refused with `403` when its `Origin` header names anything other than this
  server. A browser attaches `Origin` to every cross-site `POST`, so this is
  what stops a page on some other site using a LAN browser as a confused
  deputy for the whole audio stack. A request with *no* `Origin` is a
  non-browser client (`curl`, a shell script) and stays allowed, so existing
  scripted callers keep working.
- **Body bounds and timeouts.** `Content-Length` is validated before a single
  byte of a body is read: an undeclared length (`Transfer-Encoding: chunked`)
  is `411`, a malformed or negative one is `400`, and anything over
  `MAX_REQUEST_BODY_BYTES` (256 KiB) is `413`. The refusal never buffers the
  body, and the connection is closed rather than reused, because the unread
  bytes would otherwise be parsed as the next request. `Handler.timeout`
  (20 s) is applied to the connection by `socketserver`, so a client that
  opens a socket and stalls cannot pin a handler thread.

Two settings are opt-in, so that upgrading an existing install changes
nothing until the operator chooses otherwise:

- **`INSTALLATION_UI_HOST`** (default `0.0.0.0`) is the bind address. The
  historical value is kept as the fallback in both `install.sh` and
  `web_ui.py` so an upgrade cannot silently take a working UI away. Set it to
  `127.0.0.1` for loopback only and reach the UI over an SSH tunnel
  (`ssh -N -L 8088:127.0.0.1:8088 <user>@<pi>`). Menu option 11 prints the
  address it is about to bind to and offers loopback before it asks to
  install. `INSTALLATION_UI_PORT` (default `8088`) is read the same way. The
  unit deliberately carries no `Environment=INSTALLATION_UI_HOST` or
  `Environment=INSTALLATION_UI_PORT` line — either would shadow the operator's
  edit to `cdsp-automation.env`.
- **`INSTALLATION_UI_TOKEN`** (default empty) is an optional shared secret.
  While it is empty the UI is unauthenticated, exactly as it always was. When
  it is set, every `POST` must carry it as `Authorization: Bearer <token>`
  (or `X-Control-Token: <token>`) or it is refused with `401` and a
  `WWW-Authenticate: Bearer` challenge. The comparison uses
  `hmac.compare_digest`; a plain `==` would leak the matching prefix length
  through its timing. `GET` endpoints stay open either way — a top-level
  browser navigation cannot carry a header, so the page has to load before it
  can ask for the secret.

The page picks the token up from a URL fragment
(`http://<pi>:8088/#token=<secret>`), which neither the server nor a proxy
logs, keeps it in `localStorage`, scrubs it out of the address bar, and sends
it on every request. If a state change comes back `401` it prompts once and
retries once.

This is authentication for a single trusted operator, not a login system, and
it does not make the service less privileged. The remaining work is splitting
the UI into an unprivileged web process and a narrowly scoped privileged
helper, so that a flaw in the request handling is not automatically root; that
is a larger change and has not been done.

Because it shares the `flock` files with daemons that run as the install user,
its unit sets `Group=` to the install group and `UMask=0007`, and the locks
themselves are created `0660` — so whichever process reaches a lock first, the
other can still open it. The installer additionally pre-creates the three
static locks (`audio-eq.json.lock`, `audio-control.lock`,
`speaker-selection.json.lock`) owned by the install user.

---

## Why Systemd Services?

All four utilities run as systemd services with these benefits:

- **Auto-start on boot** - No need to manually launch them
- **Automatic restart** - If a script crashes, systemd brings it back up after `RestartSec=2`
- **Dependency management** - They are ordered after CamillaDSP and retry transient connection failures
- **Logging** - View logs with `journalctl -u cdsp-trigger -f` (or `-source-switcher`, `-remote`)
- **Easy control** - Standard `systemctl start/stop/restart` commands

Current units are enabled from `multi-user.target`. On update, the installer
rebuilds enablement with `systemctl reenable`, removing stale
`default.target.wants` links.
CamillaDSP's own unit must not specify `After=default.target` or
`After=graphical.target`; either creates a boot ordering cycle when the service
is enabled from that target.

---

## Debug Mode

The Source Switcher has a `DEBUG_MODE` constant. Set it to `True` near the top
of `~/camilladsp/scripts/source_switcher.py` and restart the switcher to see
detailed output. This shows real-time status of hardware detection, timers, and switching decisions - helpful for troubleshooting or understanding the logic.

For Remote Control, all actions are logged to journalctl by default. Watch live:

```bash
journalctl -u cdsp-remote -f
```

---

## Can I Use Just One?

Trigger control is independent. The source switcher is a
required dependency of the remote, the AirPlay/Spotify volume bridge and the
web control UI: it is the only applier of persisted EQ and speaker changes,
and the only publisher of the verified volume ceiling they read (without it
they hold `FAILSAFE_VOLUME_LIMIT_DB`).

- **Just Trigger** - For basic amp power control
- **Just Source Switcher** - For automatic source selection, with the MOTU
  clock following the source
- **Remote, volume bridge, web UI** - Each requires the Source Switcher; the
  installer pulls it in rather than producing a component whose edits are
  never applied
- **Compatible combinations** - Shared volume writers serialize through the
  audio-control lock

---

## Requirements

- Raspberry Pi (any model with GPIO for trigger control)
- CamillaDSP installed and running on port 1234
- Python 3 with venv support
- For MOTU clock/meters/volume: MOTU UltraLite mk5 on the network
- For Source Switcher: Appropriate audio hardware and configs
- For Remote: Bluetooth or USB HID remote control

---

## Questions?

Let me know if you have issues or need help adapting these for different hardware!
