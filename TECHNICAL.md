# CamillaDSP Automation Utilities for Raspberry Pi

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
managed-path provenance are checked before use. The ready-token and shared
audio-control lock make reload a fail-closed transaction: inhibit → mute →
validate/reload/verify → overlay → stamp the engine generation → restore
requested mute → publish ready.
Rollback always re-inhibits and asserts mute before loading the previous graph.

The "requested mute" is the listener's state captured *before* the transition's
own safety mute, and recovery can carry it across several failed attempts. A
listener who mutes in between only sets a flag the switcher already set, so
every mute writer (control UI, remote, AirPlay/Spotify bridge) that mutes while
the ready token is absent also leaves `mute-request.json` beside the token.
The switcher drops that file whenever it captures the live mute state (the
capture already includes it) and takes it at restore time, so a mute requested
after the capture keeps the restored output muted. Unmuting while inhibited is
refused, as before.

After a reload, `SOURCE_SETTLE_TIME` (default 2 s; 1.5 s for the USB gadget) is only a grace period
before the first look. The engine is then polled until it reports Running or
Paused on the requested file, within the same `SOURCE_CONFIG_APPLY_TIMEOUT`
(measured from the reload) as the active-config read-back, so a slow Starting
phase is not mistaken for a failure and rolled back.

Readiness is scoped to one CamillaDSP *instance*, not to the boot. CamillaDSP
4.1.3 exposes no process id over its websocket, so the switcher synthesizes an
engine generation, rotates it on every (re)connection, and writes it into the
live config's `description` with `SetConfigValue` (falling back to a whole
config write). The ready token records the same generation next to the applied
config path, digest, source, speaker and selection revision; consumers compare
the two with one `GetConfigDescription` on the client they already hold. An
engine restart, a reload from file, a recovery reload, a websocket reconnect,
or any unhandled error in the switcher loop all invalidate readiness, and the
loop re-applies the selected config through the same muted, verified path
before audio can return.

The marker is a handshake, not proof of engine identity or config integrity:
consumers only check the description. A foreign `SetConfig`/`SetConfigValue`
that keeps the description (for example a routing change made through the
engine's websocket) keeps readiness, and a marked config saved to disk and
loaded into another instance carries the marker with it. The switcher assumes
it is the only writer of the live graph; nothing enforces that.

CamillaDSP answers `SetConfig` and `SetConfigValue` once the change is
*queued*, not applied. Every live-config write here - reload, EQ overlay
(including the measurement bypass), readiness stamp - therefore polls for its
own expected state against `SOURCE_CONFIG_APPLY_TIMEOUT` instead of reading back
once. The stamp falls back to a whole-config write only when `SetConfigValue`
was rejected or never took within that deadline. An EQ write is confirmed only
when the whole filter set and pipeline match, so a bypass that has not yet
removed a legacy Bass/Treble/Loudness stage, or an engine reporting no config,
never reads as done.

The reload read-back compares against the config *captured* at resolve time
(digest-checked for managed targets), never a fresh read of the file: an
operator file edited between the integrity check and the reload is what the
engine loads, and must not be able to verify itself. The engine normalizes the
captured mapping (`ReadConfig`), and the preprocessing a reload applies is
added on top (`$samplerate$`/`$channels$` tokens, relative Conv coefficient
paths resolved against the canonical config directory), because `ReadConfig`
skips that step. A swapped file therefore fails verification and rolls back
muted; the engine is not handed an immutable snapshot, so the edit is rejected
rather than prevented.

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

I've created four Python utilities that automate common tasks when using CamillaDSP on a Raspberry Pi. Trigger control and MOTU clock sync run on their own. The source switcher is the control core: it is the only writer of the audio-ready token that permits an unmute, and the only thing that applies a persisted tone/EQ edit, so the remote control (and the AirPlay/Spotify volume bridge and web UI described later) require it.

## Installation

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh && ./install.sh
```

The installer provides a menu to install utilities individually or all at once, and sets up systemd services for each one. The entries that install a component depending on the source switcher (menu options 6, 9 and 12) install the switcher too when it is missing, say so before they act, and list it in their closing summary.

Python 3.10 or newer is required.

---

## 1. Trigger Control (GPIO Relay)

### What it does (simple):

Automatically turns on a GPIO pin when music is playing and turns it off after 5 minutes of silence. Perfect for controlling amplifier power via a relay.

### How it works (detailed):

The script continuously monitors CamillaDSP's capture RMS levels every 200ms by default. When any channel is above the configured activity threshold (`TRIGGER_AUDIO_THRESHOLD_DB`, default `-80` dB), it immediately sets GPIO pin 4 HIGH. When silence is detected, it starts a 320-second countdown timer. Only if silence persists for the full duration does it set the pin LOW.

**Why this approach:**

- **200ms polling interval** - Fast enough to catch audio immediately, but not so frequent it wastes CPU
- **320-second timeout** - Long enough to handle natural gaps in music (between tracks, quiet passages) without constantly cycling your amplifier on/off, which could cause pops or reduce component life
- **Configurable RMS threshold** - Defaults to `-80` dB, avoiding a hard dependency on sentinel values and keeping quiet-but-real audio detectable
- **Uses lgpio** - The modern GPIO library that works with current Raspberry Pi OS versions (RPi.GPIO is deprecated)
- **Manual off without disabling automation** - `SIGUSR1` drops the relay
  immediately and suppresses the current continuous audio session; after
  silence, the next audio activity turns the relay on normally

**Practical use:** Connect a 5V relay module to GPIO 4 and ground. Use the relay's normally-open contacts to send the Pi's 5V output to an amplifier trigger input that accepts it.

---

## 2. MOTU Clock Sync

### What it does (simple):

Automatically switches your MOTU audio interface's clock source from the active
managed source identity. TOSLINK selects optical; streamer, gadget, and analog
select internal, regardless of sample rate.

### How it works (detailed):

The script polls CamillaDSP's active managed config path every second. A
`toslink` config selects optical clock; `streamer`, `gadget`, and `analog`
select internal clock. The sample rate is checked for a valid running config
but is not used to infer ownership. It sends binary WebSocket commands directly
to the MOTU's control WebSocket (port 1280). The hex payloads (`000b0000000103`
for internal, `000b0000000102` for optical) are CueMix 5's own encoding of
parameter 11 (`kClockSource`): id, index 0, length 1, value.

The config path is named by `speaker_config.identify_managed_config()`, the
same lookup the source switcher and the control UI use, so the speaker
catalog's source-to-filename mapping - not a filename convention - decides
which source owns the clock. Only the two names this project generates itself
(`<source>.yml` and `<source>--<speaker>.yml`) are still recognized by shape,
as a fallback for configs the catalog does not describe; anything else the
catalog cannot name leaves the clock untouched rather than being guessed at.

**Why this approach:**

- **WebSocket communication** - the UltraLite mk5 exposes a binary WebSocket API that MOTU's CueMix 5 app uses. By capturing and replaying these commands, we can control the device programmatically without any official API
- **Source identity** - The immutable managed config name records the active
  input, so equal-rate sources still select the correct owner
- **Binary payloads** - The MOTU protocol uses binary WebSocket frames, not JSON/text, which is why we need `binascii.unhexlify()`

- **Read-back over the same WebSocket** - a binary WebSocket send that does
  not raise proves only that the frame left this host. On every new
  connection the device pushes its whole parameter set unsolicited (one
  `id, index, value` frame per parameter) before the meter stream, so the
  daemon connects, sends nothing, and takes parameter 11 from that dump. The
  persisted `MOTU_CLOCK_STATE_PATH` value is treated as a cache of the last
  *request*, and the device's own answer overrides it. A confirmed value is
  re-checked every `MOTU_CLOCK_VERIFY_INTERVAL` seconds, which also notices a
  clock changed from CueMix 5. The device serves one client at a time, so a
  read-back briefly displaces the source switcher's meter connection, which
  reconnects by itself. Verification is skipped on any pass where a clock
  change is already due, so a stalled read can never delay the change itself,
  and a write the device contradicts is repeated at most once per
  `MOTU_CLOCK_REWRITE_INTERVAL`, never every pass. The UltraLite has no HTTP
  API (port 80 closes every request unanswered); the HTTP datastore belongs
  to MOTU's AVB interfaces.

**Practical use:** Switching between TOSLINK and USB changes the MOTU clock
owner even when both graphs run at 48 kHz. Failed WebSocket sends are retried
instead of being recorded as applied, and so is a send the device accepted but
never applied.

**Clock changes inside the mute window.** With the MOTU Clock Sync unit
installed (`SOURCE_MOTU_CLOCK=auto`; `true`/`false` override), every clock
write is made by the source switcher through one operation: under the
audio-control lock, with mute requested, it first waits for the output to
actually go silent, then writes, then waits `MOTU_CLOCK_SETTLE_SECONDS` for
the re-lock. "Silent" is not the mute flag: CamillaDSP ramps mute over
`volume_ramp_time` (400 ms by default), and behind the ramp sit up to
`queuelimit` processed chunks plus the `target_level` device buffer. The
switcher waits out the ramp, requires the playback peak meter to read at or
below `MOTU_CLOCK_SILENT_DB` over the queued-audio window, then lets that span
drain. The meter only reports chunks it played, so an idle engine returns an
empty history; that counts as silence only when the engine state was also
read successfully as `Paused` or `Inactive`. A failed or malformed meter
reading, `Starting`, `Stalled` or an unrecognized state is "unknown" and keeps
waiting. If silence is not confirmed within `MOTU_CLOCK_SILENCE_TIMEOUT` past the
ramp, the clock is not written.

In a source transition that operation runs after the integrity and selection
checks and *before* the reload, so the new graph opens the interface on a
clock that has already re-locked. A transition that rolls back restores the
previous clock the same way before reloading the previous graph. A clock the
shared `MOTU_CLOCK_STATE_PATH` cache already names is not written again.

A failed write does not fail the transition, but it is never retried on live
audio. The cache still disagrees with the source, and so does it when
clock_sync's read-back contradicts a write. The switcher's loop notices, and
- while readiness is held, at most once per `MOTU_CLOCK_RETRY_SECONDS` - runs a
small muted correction under the lock: mute, the same silent write and
settle, then the listener's previous mute state.

The daemon only verifies while the switcher manages the clock (it detects the
switcher's unit, `SOURCE_SWITCHER_UNIT_PATH`): it reads back, writes what the
device really reports into the shared cache, and never writes the clock
itself. Standalone, without a switcher, it writes as before, under the
audio-control lock; if the lock cannot be taken it skips the write rather
than making an uncoordinated one.

**Note:** The payloads and read-back values are for the MOTU UltraLite mk5, taken from CueMix 5's `dev.js`. Other MOTU models may use different parameters - check that model's `dev_*.js` in CueMix 5.

### MOTU main output volume (control UI)

The control UI's "MOTU main output" card replaces CueMix 5's main volume knob,
which cannot reach the MOTU while it hangs off the Pi's USB network
(`scripts/motu_volume.py`). It is CueMix's `kMainTrim`: parameter 5011, one
byte of attenuation in dB (6 = -6 dB, 100 = -inf). It scales every output
enabled in `kMainGroup` (parameter 5012, an int16 bit per output DAC). On this
unit that group is `0x03ff`, meaning all ten analog line outputs. So the high
(Main 1-2), mid (Line 3-4) and low (Line 5-6) crossover pairs always move
together. A write is refused if the group ever stops covering all of them.

- **Ceiling:** `MOTU_MAIN_VOLUME_MAX_DB` (default `0`, the top of the MOTU's
  main attenuator -- the same range as its front-panel knob). Set it lower to
  cap the control. It is enforced server-side, because the MOTU sits after
  CamillaDSP and the profile volume limits cannot bound it. An unparseable
  value disables writes.
- **No jumps:** the page starts the slider from the level the server read off
  the device, and shows `unknown` with no slider when the device cannot be
  read. Every write names the level it replaces and is refused (409) if the
  device reports anything else, for example after the front-panel knob moved.
- **Uncertain writes:** a send that raises may still have reached the
  device, so the cached level is forgotten (`unknown`, 503) rather than kept
  as confirmed, and nothing is resent; the next permitted read settles it.
- **One client:** the browser debounces the slider and sends only the latest
  value once the shared access window reopens. Inside the window the server
  answers 429 with `retry_after`. The next section covers the window.

### Shared MOTU access window

The MOTU serves one WebSocket client at a time. The source switcher's meter
reader holds that slot, and any other connection drops it. The reader then
refuses to reconnect sooner than `SOURCE_MOTU_CONNECT_RETRY_SECONDS` (10 s)
after its previous connect. So if two extra connections land within ~10 s, the
meters stay dark for ~10 s. That exceeds the TOSLINK tolerance (2 s of held
values, `SOURCE_MOTU_METER_MAX_AGE`, plus the 5 s `SOURCE_TOSLINK_IDLE_SECONDS`
debounce), and TOSLINK drops mid-song.

Every extra connection is therefore recorded in `MOTU_ACCESS_PATH` (default
`/var/lib/cdsp-automation/motu-access.lock`, `scripts/motu_access.py`). The
record is a `CLOCK_MONOTONIC` reading plus the kernel boot id, written under
`flock`. clock_sync and the switcher (install user) and the root control UI all
use it. The installer pre-creates it as `$INSTALL_USER:$INSTALL_GROUP` with
mode 0660. Accesses are ranked:

1. **Clock write** (a source change): never waits, only records itself. If the
   record is unusable it is logged and the write still goes out.
2. **Clock read-back:** deferrable. Within `MOTU_ACCESS_WINDOW_SECONDS`
   (default 15) of any recorded access it is postponed until the window
   opens, without connecting. This includes the read-back that used to confirm
   a clock write about 1 s after sending it. If the record is unusable, the
   read-back is skipped.
3. **UI volume** read or write: deferrable, 429 with `retry_after`. If the
   record is unusable, the UI refuses to touch the MOTU.

A clock write cannot be delayed and cannot be predicted, so a deferrable access
may still land shortly before one. That is the switch *to* TOSLINK, where the
meters are what confirm the source. The meter reader closes this gap. When its
connection drops and the record shows an extra access made after that
connection opened, the reader reconnects on its next pass instead of waiting
out the 10 s backoff. Each recorded access forgives one drop. A drop the
record does not explain (the device vanished, a foreign client, an unreadable
record) keeps the backoff.

**An access in progress holds the device.** Each claim also records how long
the access can keep the device at most (its own timeouts: 7 s for a clock
read-back, 4 s for a clock write, 7 s for a UI volume access, never more than
10 s), and the access releases it as soon as it closes its connection. The
meter reader checks that span and reconnects under the record's lock, so it
cannot reconnect in the middle of an access. It used to: after a clock write
inside a source transition the switcher only notices the dropped meters once
the transition is done, and on the rig that late reconnect landed 70 ms into a
read-back that had just been let through the reopened window, resetting it
before the device had sent its clock. A skipped pass costs no backoff; the
reader connects on the next pass after the access ends. Deferrable accesses
respect the same span: one is allowed only once both the window since the
last access has passed *and* no access still holds the device, so a UI volume
access cannot take the device from a read-back that is running past 5 s.
`retry_after` reports the same condition. Clock writes keep their priority.

**Worst-case meter gap.** A single extra access costs the time the reader
takes to notice the drop (up to one switcher pass, 1 s plus a 0.2 s read
window) plus one pass to reconnect, and the dump before the first meter frame
(0.12 s measured). That is about 2.5-3.5 s. It happened once on a live read:
the drop was logged and the reader reconnected 1.1 s later. Held values cover
the first 2 s, so the TOSLINK idle counter advances by at most about 1.5 s of
its 5 s. Deferrable accesses are at least `MOTU_ACCESS_WINDOW_SECONDS` (5 s)
from every other extra access, so their gaps never stack. The only possible stacking is a clock write right
after a deferrable access. If the write lands before the reader has
reconnected, both drops fall inside one gap. If it lands just after the
reconnect but before the first meter frame, the gaps merge to about 5-6 s of
no fresh meters. That is still under the ~7 s tolerance, with the idle
counter reaching about 4 s. Previously that case stayed dark for ~10 s.

**Choosing the window.** The window used to be 15 s, sized to exceed the
meter reader's 10 s reconnect backoff. A recorded access no longer waits out
that backoff, so the window only has to exceed one switcher pass: a 1 s sleep
plus a 0.2 s meter read. It was checked by driving the real reader, meter
parsing, TOSLINK timers and access record through minutes of simulated
playback, with a fake one-client MOTU that drops the reader mid-read on every
access and a clock write chasing each deferrable access. The window was swept
against switcher passes of 1-6 s and every landing moment across a pass:

- At normal passes (1-2 s), 3 s and wider never cost TOSLINK a single pass of
  meter data. 5 s keeps it at zero, with margin, even at a 4.5 s pass.
- TOSLINK drops only when accesses land about once per switcher pass, so the
  reader never gets a pass with a fresh frame -- 2 s accesses against 2 s
  passes, or 5 s against 5 s. Even then it takes a few specific landing
  moments to line up.

At 5 s that needs switcher passes stretched to about 5 s, one after another.
A pass only runs that long inside `apply_config`, during a source change, so
it takes several source changes in a row, each chased by a MOTU volume change
landing in step, while TOSLINK is the source. Normal playback runs 1.2 s
passes and is not exposed. `test_default_window_keeps_toslink_through_back_to_back_accesses`
runs that simulation at the default window, and
`test_toslink_simulation_detects_accesses_landing_once_per_pass` shows it can
fail. Raise `MOTU_ACCESS_WINDOW_SECONDS` if a site needs more margin; it trades
it for a slower response between MOTU changes.

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
- **Silent-probe backoff** - Selecting a source to find out whether it is playing costs a full config reload plus a mute/restore, so a probe that hears nothing is remembered. The source is not re-probed for `SOURCE_PROBE_BACKOFF_SECONDS`, growing by `SOURCE_PROBE_BACKOFF_FACTOR` up to `SOURCE_PROBE_BACKOFF_MAX`. Without this, two ready-but-silent inputs alternate forever, because readiness alone requalified each one as soon as the other timed out. Confirmed audio, a manual override, or the hardware genuinely going away and coming back all clear the backoff.
- **Probe window vs track gap** - `SOURCE_IDLE_TIMEOUT` is the grace a source gets *after its playback has been confirmed*, so a quiet passage or a pause does not lose it. A source that has only ever proved ready gets the much shorter `SOURCE_PROBE_SILENCE_TIMEOUT` instead: there was no music to leave a gap in.
- **Grace periods** - The 60-second timeout and "last active source" tracking ensure the switcher doesn't jump away from a source it has heard playing just because of a quiet passage or pause button
- **Fast lower-priority handoff** - If streamer or gadget is silent while TOSLINK/analog MOTU meters are active, `SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT` lets the switcher fall through sooner than the normal track-gap timeout.
- **Confirmed audio pre-empts a grace** - A rival that has been *confirmed playing* for `SOURCE_PREEMPT_DWELL_SECONDS` cuts a silent source's grace short instead of waiting it out; a higher-priority rival waits for nothing else, a lower-priority one is additionally gated by `SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT`. The dwell keeps a single noisy meter frame from yanking the config away mid-track. A rival that is only *ready* never pre-empts - protecting a track gap from a connected-but-paused source is the entire point of the grace.
- **Meter sources get no second grace** - `toslink_available` / `analog_available` only go false after `SOURCE_TOSLINK_IDLE_SECONDS` / `SOURCE_ANALOG_IDLE_SECONDS` of quiet meters, so a meter source has already served a track-gap grace by the time it reads silent. `SourceSnapshot.self_metering` marks that, and such a source is released as soon as its meter settles rather than holding the output for another `SOURCE_IDLE_TIMEOUT`. It is never probed either, so it is never backed off: its meter requalifies it the instant signal returns.
- **RMS level threshold** - Audio is treated as active when any capture channel is above `SOURCE_AUDIO_THRESHOLD_DB` (default `-80` dB). This keeps steady tones, quiet sustained passages, and compressed audio from being mistaken for silence.
- **Keep-last idle behavior** - When all sources are idle, the default is to leave the current config alone. Set `SOURCE_IDLE_MODE=toslink` to restore the older always-fallback behavior.
- **Settle time** - After switching configs, the script waits 2 seconds for hardware to reinitialize, preventing glitches
- **Boot-race recovery** - If CamillaDSP remembers a config path but started before its audio device existed, the switcher reloads that existing config while processing is `INACTIVE`; healthy `PAUSED`/`RUNNING` configs are left untouched. The reload only happens once the engine reads back as muted; if the lock, the token removal, the mute request or its read-back fails, that attempt is skipped

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

- `analog.yml` - Configured for analog inputs, selectable manually or by setting `SOURCE_ANALOG_MOTU_METERS=true`

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
- **Separate tone step** - Bass/treble use 0.5dB steps (configurable) for fine adjustment, while volume uses 1dB steps for faster changes
- **Tone limits** - Configurable ±6dB default range prevents accidental over-boosting
- **Persistent tone control** - Atomically updates reserved `low`/`high` shelf IDs in the shared audio overlay; the source switcher applies them and remains the sole live-config writer
- **Device reconnection** - If the Bluetooth remote disconnects, the script automatically searches for it again
- **Recovery controls stay available** - A failed CamillaDSP connection does not block the HID event loop, so the power-button restart and shutdown actions still work
- **Throttled idle logging** - The remote is checked every two seconds while asleep, but unchanged "not found" status is logged only every five minutes by default

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
- cdsp-motu-sync.service
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
Filters written under this tool's earlier owned prefix are stripped by the same
pass, so a config recomposed after an update carries only the current names.

---

## 5. Web Control UI (optional)

`scripts/web_ui.py` is a single-file stdlib `http.server` dashboard installed
by menu option 12 as `cdsp-control-ui.service`. It reuses the sibling modules
in `scripts/` (`audio_eq.py`, `speaker_profiles.py`, `speaker_config.py`,
`speaker_xo.py`) and the shared `cdsp-automation.env`, and it respects the
single-writer contract: every EQ or speaker edit goes through the persistent
state files and is composed into the live config by the source switcher.

The unit deliberately runs as root because the UI restarts services, mounts
USB storage, and sets the system clock. Users who do not want a root web
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
  (`ssh -N -L 8088:127.0.0.1:8088 <user>@<pi>`). Menu option 12 prints the
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
- **Logging** - View logs with `journalctl -u cdsp-trigger -f` (or `-motu-sync`, `-source-switcher`, `-remote`)
- **Easy control** - Standard `systemctl start/stop/restart` commands

Current units are enabled from `multi-user.target`. On update, the installer
removes the pre-2026-07 `/lib/systemd/system` fragments and rebuilds enablement
with `systemctl reenable`, removing stale `default.target.wants` links.
CamillaDSP's own unit must not specify `After=default.target` or
`After=graphical.target`; either creates a boot ordering cycle when the service
is enabled from that target.

---

## Debug Mode

The Source Switcher includes a `SOURCE_DEBUG` setting. Set it in `~/camilladsp/cdsp-automation.env` to see detailed output:

```text
SOURCE_DEBUG=true
```

This shows real-time status of hardware detection, timers, and switching decisions - helpful for troubleshooting or understanding the logic.

For Remote Control, all actions are logged to journalctl by default. Watch live:

```bash
journalctl -u cdsp-remote -f
```

---

## Can I Use Just One?

Trigger control and MOTU clock sync are independent. The source switcher is a
required dependency of every component that unmutes or edits tone: the remote,
the AirPlay/Spotify volume bridge and the web control UI all call
`speaker_profiles.require_audio_unmute_allowed()` before unmuting, and
`clear_audio_inhibit()` - the only thing that grants it - is called from
`scripts/source_switcher.py` alone. The switcher is likewise the only applier
of the persisted EQ overlay.

- **Just Trigger** - For basic amp power control
- **Just MOTU Sync** - If you only need clock management
- **Just Source Switcher** - For automatic source selection
- **Remote, volume bridge, web UI** - Each requires the Source Switcher; the
  installer pulls it in rather than producing a component that can never unmute
  and whose tone edits are never applied
- **Compatible combinations** - Shared volume writers serialize through the
  audio-control lock

The token is a JSON document naming the engine generation it was verified
against, so it cannot be forged by creating the file, and the check is never
weakened: it is what keeps audio muted until a verified config is live.

---

## Requirements

- Raspberry Pi (any model with GPIO for trigger control)
- CamillaDSP installed and running on port 1234
- Python 3 with venv support
- For MOTU sync: MOTU UltraLite (or similar) on the network
- For Source Switcher: Appropriate audio hardware and configs
- For Remote: Bluetooth or USB HID remote control

---

## Questions?

Let me know if you have issues or need help adapting these for different hardware!
