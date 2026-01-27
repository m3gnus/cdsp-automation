import asyncio
import time
import RPi.GPIO as GPIO
from camilladsp import CamillaClient

PowerGpio = 4

# GPIO setup
GPIO.setmode(GPIO.BCM)
GPIO.setup(PowerGpio, GPIO.OUT, initial=GPIO.LOW)  # Relay off initially

# CamillaDSP connection settings
CAMILLA_IP = "127.0.0.1"
CAMILLA_PORT = 1234

delay_time = 320          # seconds to wait before turning relay off after music stops
check_interval = 0.2     # check every 200 ms

async def relay_control():
    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    counter = 0

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP")

            rms_levels = cdsp.levels.capture_rms()
            music_playing = any(level > -999 for level in rms_levels)

            if music_playing:
                GPIO.output(PowerGpio, GPIO.HIGH)
                counter = 0
                print("Music playing - Relay ON")
            else:
                if GPIO.input(PowerGpio) == GPIO.HIGH:
                    counter += check_interval
                    if counter >= delay_time:
                        GPIO.output(PowerGpio, GPIO.LOW)
                        print("No music - Relay OFF")
                        counter = 0
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)
            continue

        await asyncio.sleep(check_interval)

if __name__ == "__main__":
    try:
        asyncio.run(relay_control())
    except KeyboardInterrupt:
        print("Interrupted, cleaning up GPIO")
    finally:
        GPIO.cleanup()
