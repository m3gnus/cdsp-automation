# CamillaDSP Utilities for Raspberry Pi

Automation utilities for CamillaDSP on Raspberry Pi: trigger control, MOTU clock sync, and intelligent source switching.

## Quick start

Download and run the installer from this repository:

```bash
wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/install.sh -O install.sh
chmod +x install.sh
./install.sh
```

Notes:
- Run the installer on the Raspberry Pi where you want the utilities installed.
- The installer uses `sudo` where required, so you do not need to run the whole script as root.

## Utilities

### 🔌 Trigger Control
Automatically controls a GPIO relay based on audio activity. Turns on immediately when music plays, and turns off after a configurable period of silence.

Use case: power control for amplifiers via a trigger relay.

### 🎚️ MOTU Clock Sync
Automatically switches a MOTU interface clock source based on CamillaDSP's sample rate (48 kHz → optical, otherwise → internal).

Use case: avoid audio dropouts caused by clock-source mismatches when switching sources.

### 🔄 Source Switcher
Intelligently switches CamillaDSP configurations based on the active audio source.

Priority: AirPlay/Streamer → USB Gadget → TOSLINK (fallback)

Use case: seamless switching between multiple audio sources.

## Requirements

- Raspberry Pi (any model with GPIO for trigger control)
- CamillaDSP installed and accessible (default port used by scripts: 1234)
- Python 3.7 or newer
- For MOTU sync: a MOTU interface accessible on your network (e.g. UltraLite)
- For Source Switcher: three configuration files (toslink.yml, streamer.yml, gadget.yml) — see Configuration below

## Installation (what the installer does)

The installer in this repo provides a simple menu-driven interface to:
- create a Python virtualenv under `~/camilladsp/.venv`
- install Python dependencies (websocket-client and pycamilladsp)
- install required system packages (e.g. `python3-rpi-lgpio`)
- download the utility scripts into `~/camilladsp/scripts`
- create systemd service unit files for each utility
- enable and optionally start those services
- provide an uninstall option

## Manual installation

If you prefer to install manually:

1. Create directories:
   ```bash
   mkdir -p ~/camilladsp/scripts
   mkdir -p ~/camilladsp/configs
   ```

2. Create a virtualenv and install Python deps:
   ```bash
   python3 -m venv --system-site-packages ~/camilladsp/.venv
   source ~/camilladsp/.venv/bin/activate
   pip install --upgrade pip
   pip install websocket-client
   pip install git+https://github.com/HEnquist/pycamilladsp.git
   deactivate
   ```

3. Install system package for GPIO (Raspberry Pi OS):
   ```bash
   sudo apt update
   sudo apt install -y python3-rpi-lgpio
   ```

4. Copy the scripts from this repo into `~/camilladsp/scripts` (or clone the repo) and make them executable:
   ```bash
   wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts/trigger.py -O ~/camilladsp/scripts/trigger.py
   wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts/clock_sync.py -O ~/camilladsp/scripts/clock_sync.py
   wget https://raw.githubusercontent.com/m3gnus/cdsp-automation/main/scripts/source_switcher.py -O ~/camilladsp/scripts/source_switcher.py
   chmod +x ~/camilladsp/scripts/*.py
   ```

5. Create systemd service unit files (examples are in the installer). After adding them to `/lib/systemd/system/` run:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable cdsp-trigger
   sudo systemctl enable cdsp-motu-sync
   sudo systemctl enable cdsp-source-switcher
   ```

## Configuration

- Scripts live at: `~/camilladsp/scripts/`
- Virtualenv: `~/camilladsp/.venv/`

Trigger Control (`trigger.py`):
- `PowerGpio`: GPIO pin number (default: 4)
- `delay_time`: seconds before turning off (default: 320)
- `check_interval`: polling frequency (default: 0.2)

MOTU Clock Sync (`clock_sync.py`):
- The installer prompts for your MOTU device IP address and writes it into the script.
- You can also edit `~/camilladsp/scripts/clock_sync.py` to change the IP later.

Source Switcher (`source_switcher.py`):
- `IDLE_TIMEOUT`: seconds of silence before switching (default: 60)
- `RMS_DELTA_EPS`: RMS change threshold (default: 0.1)
- `DEBUG_MODE`: enable verbose logging (default: False)

Required config files for Source Switcher (examples are documented with the scripts):
- `~/camilladsp/configs/toslink.yml`
- `~/camilladsp/configs/streamer.yml`
- `~/camilladsp/configs/gadget.yml`

## Managing services

View status:
```bash
systemctl status cdsp-trigger
systemctl status cdsp-motu-sync
systemctl status cdsp-source-switcher
```

View logs (follow):
```bash
journalctl -u cdsp-trigger -f
journalctl -u cdsp-motu-sync -f
journalctl -u cdsp-source-switcher -f
```

Start / stop / restart:
```bash
sudo systemctl start cdsp-trigger
sudo systemctl stop cdsp-trigger
sudo systemctl restart cdsp-trigger

sudo systemctl start cdsp-motu-sync
sudo systemctl stop cdsp-motu-sync
sudo systemctl restart cdsp-motu-sync

sudo systemctl start cdsp-source-switcher
sudo systemctl stop cdsp-source-switcher
sudo systemctl restart cdsp-source-switcher
```

Enable / disable auto-start:
```bash
sudo systemctl enable cdsp-trigger
sudo systemctl disable cdsp-trigger

sudo systemctl enable cdsp-motu-sync
sudo systemctl disable cdsp-motu-sync

sudo systemctl enable cdsp-source-switcher
sudo systemctl disable cdsp-source-switcher
```

## Troubleshooting

Trigger not working:
- Check GPIO wiring and relay connection.
- Verify permissions: `ls -l /dev/gpiochip0`
- Check logs: `journalctl -u cdsp-trigger -n 100`

MOTU sync not working:
- Verify the MOTU IP address is correct.
- Test basic connectivity: `ping <MOTU_IP>`
- Check that the MOTU's web UI is reachable from the Pi.
- Some MOTU models may use different binary payloads for control.

Source switcher not switching:
- Enable `DEBUG_MODE` in `source_switcher.py` to see decision logic in logs.
- Verify config files exist and are valid YAML.
- Check hardware detection lines in the script (e.g. Loopback/pcm status or amixer outputs).

## Technical details

For design rationale and deeper technical explanations, see [TECHNICAL.md](TECHNICAL.md).

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Test your changes
4. Submit a pull request

## License

MIT License — see [LICENSE](LICENSE) for details.

## Acknowledgments

- [CamillaDSP](https://github.com/HEnquist/camilladsp)
- [pycamilladsp](https://github.com/HEnquist/pycamilladsp) Python library
- [RPi-CamillaDSP](https://github.com/mdsimon2/RPi-CamillaDSP) — RPi-focused CamillaDSP setup by mdsimon2
- [Display, remote and trigger power for CamillaDSP streamer and preamp — Audio Science Review thread](https://www.audiosciencereview.com/forum/index.php?threads/display-remote-and-trigger-power-for-camilladsp-streamer-and-preamp-alternative-to-mdsimon2%E2%80%99s-implementation.52818/) — community discussion and alternative implementations

## Support

- Open an issue for bugs or feature requests
- Use Discussions for questions and ideas
