# CamillaDSP Utilities for Raspberry Pi

Automation utilities for CamillaDSP on Raspberry Pi: trigger control, MOTU clock sync, seamless source switching, and Bluetooth remote control.

## Prerequisites

Before installing, ensure you have:

**General Requirements:**
- Raspberry Pi (any model) running Raspberry Pi OS
- CamillaDSP installed and running
- Python 3.7 or newer

**For Trigger Control:**
- 5V relay module ([like this](https://www.aliexpress.com/item/1005007109343076.html))
- Mono 3.5mm jack connector ([like this](https://www.aliexpress.com/item/32704200322.html))
- Your amplifier must support trigger input (typically 3-12V)

**For MOTU Clock Sync:**
- MOTU UltraLite mk5 (or compatible MOTU interface)
- MOTU accessible on your network

**For Source Switcher:**
- Three CamillaDSP config files with **specific naming**:
  - `~/camilladsp/configs/toslink.yml` - Must be configured for 48kHz
  - `~/camilladsp/configs/streamer.yml` - For AirPlay/network streaming
  - `~/camilladsp/configs/gadget.yml` - For USB Gadget mode
- **IMPORTANT:** Only the TOSLINK config should use 48kHz sample rate. Other configs must use different rates (e.g., 44.1kHz, 96kHz)

**For Remote Control:**
- Bluetooth or USB HID remote control ([like this](https://www.aliexpress.com/item/1005010182280772.html))
- CamillaDSP config with `Bass` and `Treble` filters (for tone control)

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
- Choose option **1** to install all utilities at once, or install them individually

---

## 🎮 Remote Control - Detailed Setup

### What It Does

Control CamillaDSP from a Bluetooth or USB HID remote. Adjust volume, mute, bass, and treble without touching your computer.

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

Edit `~/camilladsp/scripts/cdsp_remote.py`:

```python
# Remote device name
REMOTE_NAME = "HID Remote01 Keyboard"

# Tone limits (in dB)
TONE_MIN = -6
TONE_MAX = 6
TONE_STEP = 0.5  # Increment per button press

# Volume limits (in dB)
VOLUME_MIN = -80
VOLUME_MAX = 0
VOLUME_STEP = 1  # Increment per button press
```

### CamillaDSP Filter Requirements

For tone controls to work, your CamillaDSP config must include `Bass` and `Treble` filters:

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

## 🔌 Trigger Control - Detailed Setup

### What It Does

Automatically powers your amplifier on/off via a 12V trigger signal based on audio activity.

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

Edit `~/camilladsp/scripts/trigger.py`:

```python
PowerGpio = 4              # GPIO pin number (change if using different pin)
delay_time = 320           # Seconds of silence before turning off (320 = 5min 20sec)
check_interval = 0.2       # How often to check audio (0.2 = 200ms)
```

### How It Works

- Detects music and turns relay ON (checks every 200ms)
- Starts 320-second countdown when music stops
- Only turns relay OFF if silence continues for full duration
- Resets countdown if music resumes

**Why 320 seconds?** Long enough to handle gaps between tracks and quiet passages without constantly cycling your amplifier on/off.

---

## 🎚️ MOTU Clock Sync

### What It Does

Automatically switches your MOTU audio interface's clock source when CamillaDSP changes sample rates.

### How It Works

- Detects sample rate changes in CamillaDSP
- Sends WebSocket commands to MOTU to change clock source
- **48kHz** → switches to **optical** clock
- **Other rates** → switches to **internal** clock

### Requirements

- MOTU UltraLite mk5 (other MOTU models may need different hex payloads)
- MOTU must be accessible on your network
- Your TOSLINK source must run at 48kHz

### Configuration

The installer will prompt for your MOTU's IP address. To find it:
1. Open MOTU web interface (usually `http://169.254.51.193`)
2. Go to **Settings → About**
3. Note the IP address

To change the IP later, edit `~/camilladsp/scripts/clock_sync.py`:

```python
MOTU_WS_URL = "ws://YOUR_MOTU_IP:1280"
```

### Important Note on Sample Rates

This script assumes:
- **TOSLINK input = 48kHz** (TVs, game consoles, streaming devices typically use 48kHz)
- **Other inputs = different rates** (44.1kHz for CD quality, 96kHz for high-res, etc.)

If your setup is different, modify the logic in `clock_sync.py`:

```python
if current_rate == 48000:
    set_motu_clock("optical")
else:
    set_motu_clock("internal")
```

---

## 🔄 Source Switcher

### What It Does

Automatically switches between CamillaDSP configs based on which audio source is playing.

**Priority order:**
1. **Streamer** (AirPlay/network streaming) - highest priority
2. **USB Gadget** (direct USB connection) - middle priority
3. **TOSLINK** (optical input) - fallback/default

### Critical Configuration Requirements

**You MUST create three config files with exact names:**

1. **`~/camilladsp/configs/toslink.yml`**
   - Configure for optical input
   - **Must use 48kHz sample rate**

2. **`~/camilladsp/configs/streamer.yml`**
   - Configure for ALSA Loopback (from Squeezelite/AirPlay)
   - Must use a different sample rate (e.g., 44.1kHz or 96kHz)

3. **`~/camilladsp/configs/gadget.yml`**
   - Configure for USB Gadget
   - Must use a different sample rate (e.g., 44.1kHz or 96kHz)

**Why different sample rates?** The MOTU Clock Sync utility uses sample rate to determine which clock source to use. If all configs use the same rate, clock switching won't work correctly.

### How It Works

1. Checks if hardware is active (device connected and ready)
2. Switches to that source's config
3. Monitors actual audio playback via RMS levels
4. Waits 60 seconds of silence before switching to lower priority source
5. Falls back to TOSLINK when no other source is active

### Configuration

Edit `~/camilladsp/scripts/source_switcher.py`:

```python
IDLE_TIMEOUT = 60          # Seconds of silence before switching sources
RMS_DELTA_EPS = 0.1        # How sensitive to audio changes (lower = more sensitive)
DEBUG_MODE = False         # Set to True to see detailed logging
```

### Debugging

Enable debug mode to see what the switcher is doing:

```python
DEBUG_MODE = True
```

Then watch the logs:

```bash
journalctl -u cdsp-source-switcher -f
```

You'll see output like:

```
DEBUG: Streamer HW=True, Gadget HW=False, Last=streamer, ST=0, GT=0
→ Streamer: Audio active
→ Streamer: Idle 5/60s
```

---

## Installation Details

The installer menu provides these options:

1. **Install All Utilities** - Recommended for first-time setup
2. **Update Utilities** - Downloads latest scripts and updates pycamilladsp
3. **Install Trigger Control** - GPIO relay control only
4. **Install MOTU Clock Sync** - MOTU clock management only
5. **Install Source Switcher** - Config switching only
6. **Install Remote Control** - Bluetooth/USB remote control only
7. **Pair Bluetooth Remote** - Interactive Bluetooth pairing
8. **Show Service Status** - Check if services are running
9. **Uninstall All Utilities** - Remove everything

### What Gets Installed

**Directories created:**
- `~/camilladsp/scripts/` - Python scripts
- `~/camilladsp/configs/` - CamillaDSP config files (you must create these)
- `~/camilladsp/.venv/` - Python virtual environment

**System services:**
- `cdsp-trigger.service`
- `cdsp-motu-sync.service`
- `cdsp-source-switcher.service`
- `cdsp-remote.service`

**Dependencies:**
- `websocket-client` (Python package)
- `pycamilladsp` (Python package)
- `evdev` (Python package)
- `python3-rpi-lgpio` (system package)

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

The name in the script must exactly match what appears in the device list. Edit `~/camilladsp/scripts/cdsp_remote.py` if needed.

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

Ensure your CamillaDSP config has `Bass` and `Treble` filters with a `gain` parameter.

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

**Check if MOTU web interface is accessible:**

```bash
curl http://169.254.51.193
# Should return HTML from MOTU
```

**Check logs:**

```bash
journalctl -u cdsp-motu-sync -n 100
```

**For other MOTU models:**

The hex payloads may be different. You'll need to capture them from your MOTU's web UI:
1. Open Chrome/Firefox Developer Tools (F12)
2. Go to Network tab, filter by "WS" (WebSocket)
3. Change clock source in MOTU web UI
4. Inspect the binary WebSocket message
5. Update `CLOCK_PAYLOADS` in `clock_sync.py`

### Source Switcher Not Switching

**Enable debug mode:**

Edit `~/camilladsp/scripts/source_switcher.py`:

```python
DEBUG_MODE = True
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

**Verify sample rates are different:**

```bash
grep samplerate ~/camilladsp/configs/*.yml
# toslink.yml should show 48000
# Others should show different rates
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

---

## Can I Use Just One Utility?

**Yes!** The utilities are completely independent:

- Install only **Trigger Control** for amp power management
- Install only **MOTU Clock Sync** for clock source automation
- Install only **Source Switcher** for config switching
- Install only **Remote Control** for volume/tone adjustment
- Install any combination

They don't interfere with each other and can run simultaneously.

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
