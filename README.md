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
- Optional `~/camilladsp/configs/analog.yml` for manual or meter-based analog input
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

To change the IP later, edit `~/camilladsp/cdsp-automation.env`:

```text
MOTU_WS_URL=ws://YOUR_MOTU_IP:1280
MOTU_OPTICAL_RATE=48000
```

### Important Note on Sample Rates

This script assumes:
- **TOSLINK input = 48kHz** (TVs, game consoles, streaming devices typically use 48kHz)
- **Other inputs = different rates** (44.1kHz for CD quality, 96kHz for high-res, etc.)

If your setup uses a different optical sample rate, change `MOTU_OPTICAL_RATE` in `~/camilladsp/cdsp-automation.env`.


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
   - **Must use 48kHz sample rate**

2. **`~/camilladsp/configs/streamer.yml`**
   - Configure for ALSA Loopback (from Squeezelite/AirPlay)
   - Must use a different sample rate (e.g., 44.1kHz or 96kHz)

3. **`~/camilladsp/configs/gadget.yml`**
   - Configure for USB Gadget
   - Must use a different sample rate (e.g., 44.1kHz or 96kHz)

Optional manual-only configs can also be selected through the override file. For example,
`~/camilladsp/configs/analog.yml` can be pinned manually even though analog is not
auto-detected by default.

**Why different sample rates?** The MOTU Clock Sync utility uses sample rate to determine which clock source to use. If all configs use the same rate, clock switching won't work correctly.

### How It Works

1. Checks if hardware is active (device connected and ready)
2. Switches to that source's config
3. Monitors actual audio playback via RMS levels
4. Keeps the current source while it is still playing
5. Waits 60 seconds of silence before abandoning a higher-priority hardware source unless another meter-confirmed source is already active
6. Uses passive MOTU meter frames to detect TOSLINK activity
7. Keeps the current config when all sources are idle unless `SOURCE_IDLE_MODE=toslink`

If a lower-priority MOTU meter source is already active, the switcher uses
`SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT` to leave a silent higher-priority source
faster than the normal track-gap timeout. The default is immediate handoff after
the lower source has passed its own activity debounce.

### Configuration

Edit `~/camilladsp/cdsp-automation.env`:

```text
SOURCE_IDLE_TIMEOUT=60
SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT=0
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
- `~/camilladsp/cdsp-automation.env` - User settings preserved across script updates

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

For TOSLINK meter detection, check that the MOTU WebSocket is reachable:

```bash
journalctl -u cdsp-source-switcher -n 100 | grep "MOTU meters"
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
