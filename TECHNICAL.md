# CamillaDSP Automation Utilities for Raspberry Pi

I've created four Python utilities that automate common tasks when using CamillaDSP on a Raspberry Pi. They're designed to work together or independently, depending on your needs.

## Installation

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh && ./install.sh
```

The installer provides a menu to install utilities individually or all at once, and sets up systemd services for each one.

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

**Practical use:** Connect a 5V relay module to GPIO 4 and ground. Use the relay's normally-open contacts to send the Pi's 5V output to an amplifier trigger input that accepts it.

---

## 2. MOTU Clock Sync

### What it does (simple):

Automatically switches your MOTU audio interface's clock source based on CamillaDSP's sample rate. Sets it to "optical" for 48kHz, "internal" for everything else.

### How it works (detailed):

The script polls CamillaDSP's active configuration every second to read the current sample rate. When a rate change is detected, it sends binary WebSocket commands directly to the MOTU's web interface to change the clock source. The hex payloads (`000b0000000103` for internal, `000b0000000102` for optical) are reverse-engineered commands from MOTU's web UI.

**Why this approach:**

- **WebSocket communication** - MOTU interfaces expose a WebSocket API that their web UI uses. By capturing and replaying these commands, we can control the device programmatically without any official API
- **Sample rate detection** - Reading from CamillaDSP's config ensures we're always synchronized with the actual DSP state, not making assumptions
- **48kHz = optical logic** - This assumes your TOSLINK/optical input runs at 48kHz (common for streaming devices, TVs, game consoles) while internal sources use different rates
- **Binary payloads** - The MOTU protocol uses binary WebSocket frames, not JSON/text, which is why we need `binascii.unhexlify()`

**Practical use:** If you switch between a 48kHz TOSLINK source and a 44.1kHz USB input, the MOTU needs to match its clock source. This script eliminates manual switching and prevents audio dropouts from clock mismatch.

**Note:** The hex payloads are for MOTU UltraLite. Other MOTU models may use different commands - you'd need to capture them from the web UI using browser developer tools.

---

## 3. Source Switcher

### What it does (simple):

Automatically switches between CamillaDSP configs based on which audio source is active. Priority: manual override → AirPlay Streamer → USB Gadget → TOSLINK meter detection → optional analog meter detection.

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
- **Hardware state checking** - Looking at `/proc/asound` and `amixer` output gives us reliable, kernel-level information about audio hardware state
- **MOTU meter detection** - TOSLINK is detected from live MOTU input meter frames instead of being treated as always active
- **Two-phase detection** - First checks if hardware is "ready" (device connected/active), then uses RMS levels to detect actual audio playback. This prevents switching to a connected-but-silent device
- **Grace periods** - The 60-second timeout and "last active source" tracking ensure the switcher doesn't jump away from your current source just because of a quiet passage or pause button
- **Fast lower-priority handoff** - If streamer or gadget is silent while TOSLINK/analog MOTU meters are active, `SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT` lets the switcher fall through sooner than the normal track-gap timeout.
- **RMS level threshold** - Audio is treated as active when any capture channel is above `SOURCE_AUDIO_THRESHOLD_DB` (default `-80` dB). This keeps steady tones, quiet sustained passages, and compressed audio from being mistaken for silence.
- **Keep-last idle behavior** - When all sources are idle, the default is to leave the current config alone. Set `SOURCE_IDLE_MODE=toslink` to restore the older always-fallback behavior.
- **Settle time** - After switching configs, the script waits 2 seconds for hardware to reinitialize, preventing glitches

**Priority logic explained:**

- **Manual override** - Used for sources that are hard to auto-detect, such as analog input. The override file is transient by default under `/run`.
- **Priority 1: Streamer** - Assumes AirPlay/network streaming is your primary source. When you start casting from your phone, it takes over immediately
- **Priority 2: USB Gadget** - Direct USB connection (phone, laptop) is secondary. Useful when you plug in directly but don't want to interrupt if streaming is active
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
- **Tone limits** - Hardcoded ±6dB range prevents accidentally over-boosting frequencies
- **Filter-based tone control** - Modifies the `gain` parameter in Bass/Treble Biquad filters, which must exist in your CamillaDSP config
- **Device reconnection** - If the Bluetooth remote disconnects, the script automatically searches for it again

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

Holding the power button for ~1 second restarts all CamillaDSP-related services:
- camilladsp.service
- camillagui.service
- cdsp-motu-sync.service
- cdsp-source-switcher.service
- cdsp-remote.service
- cdsp-trigger.service (delayed by 3 seconds by default to allow amplifiers to power cycle properly)

The trigger service restart uses `systemd-run` to create a transient timer that survives the cdsp-remote service restart. This ensures proper sequencing even though the restart command kills the script itself.

Holding for ~10 seconds triggers a system shutdown (`shutdown -h now`).

**Finding your remote:**

After pairing a Bluetooth remote, find its device name:

```bash
python3 -c "import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])"
```

The script searches for the configured `REMOTE_NAME` from `~/camilladsp/cdsp-automation.env` and waits if it's not found (useful for remotes that auto-sleep).

**CamillaDSP requirements:**

For tone controls, your config must have filters named exactly `Bass` and `Treble` with a `gain` parameter:

```yaml
filters:
  Bass:
    type: Biquad
    parameters:
      type: Lowshelf
      freq: 85
      gain: 0
      q: 0.9
  Treble:
    type: Biquad
    parameters:
      type: Highshelf
      freq: 6500
      gain: 0
      q: 0.7
```

---

## Why Systemd Services?

All four utilities run as systemd services with these benefits:

- **Auto-start on boot** - No need to manually launch them
- **Automatic restart** - If a script crashes, systemd brings it back up after `RestartSec=2`
- **Dependency management** - They won't start until CamillaDSP is running
- **Logging** - View logs with `journalctl -u cdsp-trigger -f` (or `-motu-sync`, `-source-switcher`, `-remote`)
- **Easy control** - Standard `systemctl start/stop/restart` commands

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
