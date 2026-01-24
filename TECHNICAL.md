# CamillaDSP Automation Utilities for Raspberry Pi

I've created three Python utilities that automate common tasks when using CamillaDSP on a Raspberry Pi. They're designed to work together or independently, depending on your needs.

## Installation

```bash
wget https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/install.sh -O install.sh
chmod +x install.sh && ./install.sh
```

The installer provides a menu to install utilities individually or all at once, and sets up systemd services for each one.

---

## 1. Trigger Control (GPIO Relay)

### What it does (simple):
Automatically turns on a GPIO pin when music is playing and turns it off after 5 minutes of silence. Perfect for controlling amplifier power via a relay.

### How it works (detailed):
The script continuously monitors CamillaDSP's capture RMS levels every 200ms. When any channel shows activity (RMS > -999dB), it immediately sets GPIO pin 4 HIGH. When silence is detected, it starts a 320-second countdown timer. Only if silence persists for the full duration does it set the pin LOW.

**Why this approach:**
- **200ms polling interval** - Fast enough to catch audio immediately, but not so frequent it wastes CPU
- **320-second timeout** - Long enough to handle natural gaps in music (between tracks, quiet passages) without constantly cycling your amplifier on/off, which could cause pops or reduce component life
- **RMS threshold of -999dB** - This is CamillaDSP's indicator for "no signal," making it reliable even with various audio levels
- **Uses lgpio** - The modern GPIO library that works with current Raspberry Pi OS versions (RPi.GPIO is deprecated)

**Practical use:** Connect a 5V relay module to GPIO 4 and ground. Use the relay's normally-open contacts to control your amplifier's 12V trigger input or power circuit.

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
Intelligently switches between three CamillaDSP configs based on which audio source is active. Priority: AirPlay Streamer → USB Gadget → TOSLINK (fallback).

### How it works (detailed):
The script checks multiple hardware indicators every second:
1. **AirPlay/Streamer**: Reads `/proc/asound/Loopback/pcm*/sub*/status` to see if ALSA Loopback is in RUNNING state
2. **USB Gadget**: Executes `amixer` to check if UAC2Gadget's capture rate is non-zero (indicating a connected USB host)
3. **RMS monitoring**: After switching configs, it monitors RMS levels to detect actual audio activity vs. hardware just being "ready"

When a higher-priority source becomes active, it immediately switches configs. When a source goes silent, it waits 60 seconds before considering lower-priority sources, preventing rapid switching during track changes or brief pauses.

**Why this approach:**
- **Hardware state checking** - Looking at `/proc/asound` and `amixer` output gives us reliable, kernel-level information about audio hardware state
- **Two-phase detection** - First checks if hardware is "ready" (device connected/active), then uses RMS levels to detect actual audio playback. This prevents switching to a connected-but-silent device
- **Grace periods** - The 60-second timeout and "last active source" tracking ensure the switcher doesn't jump away from your current source just because of a quiet passage or pause button
- **RMS delta threshold** - Checking if RMS *changes* (not just absolute level) reliably detects active audio even at low volumes, while ignoring noise floor
- **Settle time** - After switching configs, the script waits 2 seconds for hardware to reinitialize, preventing glitches

**Priority logic explained:**
- **Priority 1: Streamer** - Assumes AirPlay/network streaming is your primary source. When you start casting from your phone, it takes over immediately
- **Priority 2: USB Gadget** - Direct USB connection (phone, laptop) is secondary. Useful when you plug in directly but don't want to interrupt if streaming is active
- **Priority 3: TOSLINK** - Optical input is the fallback. Always available, so it's what you'll hear when nothing else is playing

**Config requirements:** You need to create three config files:
- `toslink.yml` - Configured for optical input
- `streamer.yml` - Configured for ALSA Loopback (from Squeezelite/AirPlay)
- `gadget.yml` - Configured for USB Gadget (Pi Zero as USB sound card)

---

## Why Systemd Services?

All three utilities run as systemd services with these benefits:
- **Auto-start on boot** - No need to manually launch them
- **Automatic restart** - If a script crashes, systemd brings it back up within 1 second
- **Dependency management** - They won't start until CamillaDSP is running
- **Logging** - View logs with `journalctl -u cdsp-trigger -f` (or `-motu-sync`, `-source-switcher`)
- **Easy control** - Standard `systemctl start/stop/restart` commands

---

## Debug Mode

The Source Switcher includes a `DEBUG_MODE` flag. Set it to `True` in the script to see detailed output:
```python
DEBUG_MODE = True  # In source_switcher.py
```

This shows real-time status of hardware detection, timers, and switching decisions - helpful for troubleshooting or understanding the logic.

---

## Can I Use Just One?

Absolutely! The utilities are independent:
- **Just Trigger** - For basic amp power control
- **Just MOTU Sync** - If you only need clock management
- **Just Source Switcher** - For automatic source selection
- **Any combination** - They don't interfere with each other

---

## Requirements

- Raspberry Pi (any model with GPIO for trigger control)
- CamillaDSP installed and running on port 1234
- Python 3 with venv support
- For MOTU sync: MOTU UltraLite (or similar) on the network
- For Source Switcher: Appropriate audio hardware and configs

---

## Questions?

Let me know if you have issues or need help adapting these for different hardware!
