#!/usr/bin/env python3
"""
CamillaDSP USB HID Remote Control

Controls CamillaDSP via a Bluetooth/USB HID remote:
- Volume control (up/down/mute)
- Tone adjustment (bass/treble via arrow keys)
- Power button: hold 1s to restart services, hold 10s to shutdown

Designed to work alongside other cdsp-automation utilities.

Requirements:
    pip install evdev pycamilladsp
"""

import asyncio
import evdev
import os
import signal
import subprocess
import sys
import time
from camilladsp import CamillaClient

# ====================== CONFIGURATION ======================

# Remote device name - change this to match your USB HID remote
# Run: python3 -c "import evdev; print([d.name for d in [evdev.InputDevice(p) for p in evdev.list_devices()]])"
# to find your remote's name
REMOTE_NAME = "HID Remote01 Keyboard"

# CamillaDSP connection settings
CDSP_HOST = "127.0.0.1"
CDSP_PORT = 1234

# Tone adjustment limits (in dB)
TONE_MIN = -6
TONE_MAX = 6
TONE_STEP = 0.5  # Increment per button press

# Volume limits (in dB)
VOLUME_MIN = -80
VOLUME_MAX = 0
VOLUME_STEP = 1  # Increment per button press

# Remote key mappings - adjust if your remote uses different key codes
KEY_BINDINGS = {
    'VOLUMEDOWN': 'KEY_VOLUMEDOWN',
    'VOLUMEUP': 'KEY_VOLUMEUP',
    'MUTE': 'KEY_MUTE',
    'UP': 'KEY_UP',          # Treble Up
    'DOWN': 'KEY_DOWN',      # Treble Down
    'LEFT': 'KEY_LEFT',      # Bass Down
    'RIGHT': 'KEY_RIGHT',    # Bass Up
    'ENTER': 'KEY_ENTER',    # Show status / Reset tone (hold)
    'POWER': 'KEY_POWER',    # Restart services (1s hold) / Shutdown (10s hold)
}

# ====================== GLOBAL STATE ======================

cdsp = None
KEYDOWN = 0

# ====================== HELPER FUNCTIONS ======================

def find_remote_device():
    """Search for the USB HID remote device by name."""
    print(f"Searching for remote '{REMOTE_NAME}'...")

    while True:
        devices = {evdev.InputDevice(path).name: path for path in evdev.list_devices()}

        if REMOTE_NAME in devices:
            print(f"Found '{REMOTE_NAME}' at {devices[REMOTE_NAME]}")
            return evdev.InputDevice(devices[REMOTE_NAME])

        print(f"Remote '{REMOTE_NAME}' not found. Retrying in 2 seconds...")
        time.sleep(2)


def connect_to_camilladsp():
    """Establish connection to CamillaDSP."""
    global cdsp

    print(f"Connecting to CamillaDSP at {CDSP_HOST}:{CDSP_PORT}...")
    cdsp = CamillaClient(CDSP_HOST, CDSP_PORT)

    while True:
        try:
            cdsp.connect()
            print("Connected to CamillaDSP successfully!")
            return
        except Exception as e:
            print(f"Failed to connect: {e}. Retrying in 2 seconds...")
            time.sleep(2)


def adjust_volume(change):
    """Adjust the main volume by the specified amount."""
    try:
        current_volume = cdsp.volume.main_volume()
        new_volume = max(VOLUME_MIN, min(VOLUME_MAX, current_volume + change))
        cdsp.volume.set_main_volume(new_volume)
        print(f"Volume: {new_volume:.1f} dB")
    except Exception as e:
        print(f"Error adjusting volume: {e}")


def toggle_mute():
    """Toggle the mute state."""
    try:
        is_muted = cdsp.volume.main_mute()
        cdsp.volume.set_main_mute(not is_muted)
        print(f"Mute: {'ON' if not is_muted else 'OFF'}")
    except Exception as e:
        print(f"Error toggling mute: {e}")


def adjust_tone(parameter, change):
    """
    Adjust bass or treble gain.

    Args:
        parameter: 'Bass' or 'Treble'
        change: Amount to change (positive or negative)
    """
    try:
        cdspconf = cdsp.config.active()
        if not cdspconf:
            print("No active configuration")
            return

        filters = cdspconf.get("filters", {})
        if parameter not in filters:
            print(f"Filter '{parameter}' not found in config")
            return

        current_gain = filters[parameter]["parameters"]["gain"]
        new_gain = max(TONE_MIN, min(TONE_MAX, current_gain + change))
        filters[parameter]["parameters"]["gain"] = new_gain
        cdsp.config.set_active(cdspconf)
        print(f"{parameter}: {new_gain:+.1f} dB")

    except Exception as e:
        print(f"Error adjusting {parameter}: {e}")


def get_current_tone():
    """Get current bass and treble values."""
    try:
        cdspconf = cdsp.config.active()
        if cdspconf:
            filters = cdspconf.get('filters', {})
            if 'Bass' in filters and 'Treble' in filters:
                bass = filters['Bass']['parameters']['gain']
                treble = filters['Treble']['parameters']['gain']
                return bass, treble
    except Exception as e:
        print(f"Error getting tone: {e}")
    return None, None


def reset_tone():
    """Reset both bass and treble to 0."""
    try:
        cdspconf = cdsp.config.active()
        if not cdspconf:
            return

        filters = cdspconf.get("filters", {})
        if 'Bass' in filters and 'Treble' in filters:
            filters['Bass']['parameters']['gain'] = 0
            filters['Treble']['parameters']['gain'] = 0
            cdsp.config.set_active(cdspconf)
            print("Tone reset: Bass=0 dB, Treble=0 dB")

    except Exception as e:
        print(f"Error resetting tone: {e}")


def restart_services():
    """
    Restart CamillaDSP and related services.
    Continues even if some services are not available.

    Note: cdsp-trigger is restarted via a background process with a delay
    to allow amplifiers to fully power down before the relay reopens.
    cdsp-remote is restarted last (which kills this script, but the
    background process survives to restart the trigger).
    """
    # Delay before restarting trigger (allows amplifiers to power down)
    trigger_delay = 5

    # Use systemd-run to create a transient timer that restarts trigger after delay
    # Use timestamp in unit name to avoid conflicts with previous runs
    unit_name = f'cdsp-trigger-restart-{int(time.time())}'
    try:
        result = subprocess.run(
            ['sudo', 'systemd-run', '--no-block', f'--on-active={trigger_delay}',
             f'--unit={unit_name}', 'systemctl', 'restart', 'cdsp-trigger.service'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print(f"  Scheduled: cdsp-trigger.service (in {trigger_delay}s)")
        else:
            print(f"  Warning: systemd-run failed: {result.stderr}")
    except Exception as e:
        print(f"  Warning: Could not schedule trigger restart: {e}")

    # Services to restart immediately (cdsp-remote last since it kills this script)
    services = [
        'camilladsp.service',
        'camillagui.service',
        'cdsp-motu-sync.service',
        'cdsp-source-switcher.service',
        'cdsp-remote.service',  # Must be last - restarts this script
    ]

    print("Restarting services...")
    for service in services:
        try:
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', service],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                print(f"  Restarted: {service}")
            else:
                print(f"  Skipped: {service} (not available or failed)")
        except subprocess.TimeoutExpired:
            print(f"  Timeout: {service}")
        except Exception as e:
            print(f"  Error restarting {service}: {e}")


# ====================== EVENT HANDLING ======================

async def handle_remote_events(device):
    """
    Process events from the remote control device.

    Args:
        device: evdev InputDevice
    """
    global KEYDOWN

    counter_volume = 0

    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                attrib = evdev.categorize(event)
                key = attrib.keycode

                # Key pressed (keystate == 1)
                if attrib.keystate == 1:
                    if key in (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP']):
                        change = -VOLUME_STEP if key == KEY_BINDINGS['VOLUMEDOWN'] else VOLUME_STEP
                        adjust_volume(change)

                    elif KEY_BINDINGS['MUTE'] in key:
                        toggle_mute()

                    elif key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']):
                        # Treble adjustment
                        change = TONE_STEP if key == KEY_BINDINGS['UP'] else -TONE_STEP
                        adjust_tone('Treble', change)

                    elif key in (KEY_BINDINGS['LEFT'], KEY_BINDINGS['RIGHT']):
                        # Bass adjustment
                        change = TONE_STEP if key == KEY_BINDINGS['RIGHT'] else -TONE_STEP
                        adjust_tone('Bass', change)

                # Key held (keystate == 2)
                elif attrib.keystate == 2:
                    if key in (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP']):
                        counter_volume += 1
                        if counter_volume >= 2:
                            change = -VOLUME_STEP if key == KEY_BINDINGS['VOLUMEDOWN'] else VOLUME_STEP
                            adjust_volume(change)
                            counter_volume = 0

                    elif key == KEY_BINDINGS['ENTER']:
                        KEYDOWN += 1
                        if KEYDOWN >= 10:
                            reset_tone()
                            KEYDOWN = 0

                    elif key == KEY_BINDINGS['POWER']:
                        KEYDOWN += 1
                        if KEYDOWN == 10:
                            # ~1 second hold - restart services
                            print("Power button held 1s - restarting services...")
                            restart_services()
                        elif KEYDOWN >= 100:
                            # ~10 second hold - shutdown
                            print("Power button held 10s - shutting down...")
                            subprocess.run(['sudo', 'shutdown', '-h', 'now'])

                # Key released (keystate == 0)
                elif attrib.keystate == 0:
                    if key == KEY_BINDINGS['ENTER'] and KEYDOWN < 10:
                        # Short press - print current status
                        volume = cdsp.volume.main_volume()
                        muted = cdsp.volume.main_mute()
                        bass, treble = get_current_tone()
                        print(f"Status: Volume={volume:.1f}dB, Mute={'ON' if muted else 'OFF'}, "
                              f"Bass={bass:+.1f}dB, Treble={treble:+.1f}dB")

                    # Reset counters on key release
                    KEYDOWN = 0
                    counter_volume = 0

        except OSError as e:
            print(f"Device error: {e}. Attempting to reconnect...")
            await asyncio.sleep(2)
            # Try to find the device again
            device = find_remote_device()


# ====================== MAIN ======================

def cleanup(signum=None, frame=None):
    """Clean up resources on exit."""
    print("\nShutting down...")
    if cdsp:
        try:
            cdsp.disconnect()
        except:
            pass
    sys.exit(0)


def main():
    """Main entry point."""
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 50)
    print("CamillaDSP USB HID Remote Control")
    print("=" * 50)

    # Find the remote device
    remote = find_remote_device()

    # Connect to CamillaDSP
    connect_to_camilladsp()

    # Print initial status
    try:
        volume = cdsp.volume.main_volume()
        muted = cdsp.volume.main_mute()
        config = os.path.basename(cdsp.config.file_path())
        print(f"Current: Volume={volume:.1f}dB, Mute={'ON' if muted else 'OFF'}, Config={config}")
    except Exception as e:
        print(f"Could not get initial status: {e}")

    print("=" * 50)
    print("Ready! Listening for remote events...")
    print("Press Ctrl+C to exit")
    print("=" * 50)

    # Run the async event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(handle_remote_events(remote))
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
