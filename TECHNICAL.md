# CamillaDSP Automation Utilities for Raspberry Pi

## Speaker-profile contract

Speaker selection is stored as a versioned, compare-and-swap document at
`SPEAKER_SELECTION_PATH`. Non-Kantarellen definitions use profile schema 1 and
must explicitly provide: `id`, `enabled`, `supported_sources`,
`output_channels`, complete `active_outputs`/`muted_outputs`, one role per
output, a non-positive `max_volume_db`, `bypass_user_eq`, `raw_measurement`,
capabilities, and a
CamillaDSP fragment. Profile-owned filter/mixer/processor names use the
`spk_<id>_` prefix. The profile owns playback and output routing; the source
base owns capture, samplerate, clock-related device settings, and (for normal
speaker profiles) pre-routing source filters. Source bases may not own mixers
or processors. A `raw_measurement: true` Measurement profile additionally
requires `bypass_user_eq: true`, empty source filters/pipeline, and empty
profile filters/processors; only its explicit output mixer remains.

`capabilities.meter_bands` is optional and, when present, strictly maps the
active playback outputs into non-overlapping low/mid/high groups. The applied
profile publishes this map in its transactional status. Consumers disable
frequency-dependent displays/effects when the map is absent or status is not
fully applied.

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
validate/reload/verify → overlay → restore requested mute → publish ready.
Rollback always re-inhibits and asserts mute before loading the previous graph.

I've created four Python utilities that automate common tasks when using CamillaDSP on a Raspberry Pi. They're designed to work together or independently, depending on your needs.

## Installation

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh && ./install.sh
```

The installer provides a menu to install utilities individually or all at once, and sets up systemd services for each one.

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
to the MOTU web interface. The hex payloads (`000b0000000103` for internal,
`000b0000000102` for optical) are reverse-engineered commands from MOTU's web UI.

**Why this approach:**

- **WebSocket communication** - MOTU interfaces expose a WebSocket API that their web UI uses. By capturing and replaying these commands, we can control the device programmatically without any official API
- **Source identity** - The immutable managed config name records the active
  input, so equal-rate sources still select the correct owner
- **Binary payloads** - The MOTU protocol uses binary WebSocket frames, not JSON/text, which is why we need `binascii.unhexlify()`

**Practical use:** Switching between TOSLINK and USB changes the MOTU clock
owner even when both graphs run at 48 kHz. Failed WebSocket sends are retried
instead of being recorded as applied.

**Note:** The hex payloads are for MOTU UltraLite. Other MOTU models may use different commands - you'd need to capture them from the web UI using browser developer tools.

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
- **Two-phase detection** - First checks if hardware is "ready" (device connected/active), then uses RMS levels to detect actual audio playback. This prevents switching to a connected-but-silent device
- **Grace periods** - The 60-second timeout and "last active source" tracking ensure the switcher doesn't jump away from your current source just because of a quiet passage or pause button
- **Fast lower-priority handoff** - If streamer or gadget is silent while TOSLINK/analog MOTU meters are active, `SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT` lets the switcher fall through sooner than the normal track-gap timeout.
- **RMS level threshold** - Audio is treated as active when any capture channel is above `SOURCE_AUDIO_THRESHOLD_DB` (default `-80` dB). This keeps steady tones, quiet sustained passages, and compressed audio from being mistaken for silence.
- **Keep-last idle behavior** - When all sources are idle, the default is to leave the current config alone. Set `SOURCE_IDLE_MODE=toslink` to restore the older always-fallback behavior.
- **Settle time** - After switching configs, the script waits 2 seconds for hardware to reinitialize, preventing glitches
- **Boot-race recovery** - If CamillaDSP remembers a config path but started before its audio device existed, the switcher reloads that existing config while processing is `INACTIVE`; healthy `PAUSED`/`RUNNING` configs are left untouched

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
through the source switcher's owned `uglan_ui_eq_*` overlay. The remote adjusts
the reserved low/high shelf bands. Source configs must not add separate
`Bass`, `Treble`, `Loudness`, or `Iso226` stages because those would stack with
the owned overlay; legacy stages are stripped when a config becomes active.

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

Absolutely! The utilities are independent:

- **Just Trigger** - For basic amp power control
- **Just MOTU Sync** - If you only need clock management
- **Just Source Switcher** - For automatic source selection
- **Just Remote** - For volume/tone control via remote
- **Any combination** - They don't interfere with each other

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
