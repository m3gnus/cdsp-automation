#!/usr/bin/env python3
"""Automatic CamillaDSP config switching by active source."""

from __future__ import annotations

import glob
import os
import subprocess
import time

try:
    import websocket
except ImportError:
    websocket = None

from camilladsp import CamillaClient


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


CAMILLA_IP = os.environ.get("CDSP_HOST", "127.0.0.1")
CAMILLA_PORT = int(os.environ.get("CDSP_PORT", "1234"))
CHECK_INTERVAL = float(os.environ.get("SOURCE_CHECK_INTERVAL", "1.0"))
IDLE_TIMEOUT = float(os.environ.get("SOURCE_IDLE_TIMEOUT", "60"))
LOWER_PRIORITY_ACTIVE_TIMEOUT = float(
    os.environ.get("SOURCE_LOWER_PRIORITY_ACTIVE_TIMEOUT", "0")
)
SETTLE_TIME = float(os.environ.get("SOURCE_SETTLE_TIME", "2.0"))
AUDIO_THRESHOLD_DB = float(os.environ.get("SOURCE_AUDIO_THRESHOLD_DB", "-80"))
DEBUG_MODE = env_bool("SOURCE_DEBUG", False)
MOTU_WS_URL = os.environ.get("MOTU_WS_URL", "ws://169.254.51.193:1280")
TOSLINK_MOTU_METERS = env_bool("SOURCE_TOSLINK_MOTU_METERS", True)
ANALOG_MOTU_METERS = env_bool("SOURCE_ANALOG_MOTU_METERS", False)
MOTU_METER_ACTIVE_BELOW = int(os.environ.get("SOURCE_MOTU_METER_ACTIVE_BELOW", "250"))
MOTU_METER_MAX_AGE = float(os.environ.get("SOURCE_MOTU_METER_MAX_AGE", "2.0"))
MOTU_CONNECT_RETRY_SECONDS = float(os.environ.get("SOURCE_MOTU_CONNECT_RETRY_SECONDS", "10"))
MOTU_READ_WINDOW_SECONDS = float(os.environ.get("SOURCE_MOTU_READ_WINDOW_SECONDS", "0.2"))
TOSLINK_ACTIVE_SECONDS = float(os.environ.get("SOURCE_TOSLINK_ACTIVE_SECONDS", "0.5"))
TOSLINK_IDLE_SECONDS = float(os.environ.get("SOURCE_TOSLINK_IDLE_SECONDS", "5"))
ANALOG_ACTIVE_SECONDS = float(os.environ.get("SOURCE_ANALOG_ACTIVE_SECONDS", "5"))
ANALOG_IDLE_SECONDS = float(os.environ.get("SOURCE_ANALOG_IDLE_SECONDS", "30"))
SOURCE_IDLE_MODE = os.environ.get("SOURCE_IDLE_MODE", "keep-last").strip().lower()

TOSLINK_METER_PAIRS = tuple(
    int(value)
    for value in os.environ.get("SOURCE_TOSLINK_METER_PAIRS", "12,13").split(",")
    if value.strip()
)
ANALOG_METER_PAIRS = tuple(
    int(value)
    for value in os.environ.get("SOURCE_ANALOG_METER_PAIRS", "16,18").split(",")
    if value.strip()
)

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("CDSP_CONFIG_DIR", os.path.join(HOME, "camilladsp/configs"))

TOSLINK_CFG = os.path.join(CONFIG_DIR, "toslink.yml")
STREAMER_CFG = os.path.join(CONFIG_DIR, "streamer.yml")
GADGET_CFG = os.path.join(CONFIG_DIR, "gadget.yml")
ANALOG_CFG = os.path.join(CONFIG_DIR, "analog.yml")
SOURCE_OVERRIDE_PATH = os.environ.get("SOURCE_OVERRIDE_PATH", "/run/cdsp-source-switcher/manual_source")

CONFIGS = {
    "toslink": TOSLINK_CFG,
    "streamer": STREAMER_CFG,
    "gadget": GADGET_CFG,
    "analog": ANALOG_CFG,
}


class MotuMeterReader:
    """Read passive meter frames from MOTU UltraLite mk5 CueMix WebSocket."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.last_pairs: dict[int, tuple[int, int]] = {}
        self.last_seen = 0.0
        self.next_connect_attempt = 0.0
        self.next_error_log = 0.0

    def close(self) -> None:
        if self.ws is None:
            return
        try:
            self.ws.close()
        except Exception:
            pass
        self.ws = None

    def log_error(self, message: str) -> None:
        now = time.monotonic()
        if now >= self.next_error_log:
            print(message, flush=True)
            self.next_error_log = now + 30

    def connect(self) -> bool:
        now = time.monotonic()
        if self.ws is not None:
            return True
        if now < self.next_connect_attempt:
            return False

        self.next_connect_attempt = now + MOTU_CONNECT_RETRY_SECONDS
        if websocket is None:
            self.log_error("MOTU meter connection unavailable: install websocket-client")
            return False

        try:
            ws = websocket.WebSocket()
            ws.settimeout(1)
            ws.connect(self.url, timeout=1)
            ws.settimeout(0.05)
            self.ws = ws
            print(f"MOTU meters connected: {self.url}", flush=True)
            return True
        except Exception as exc:
            self.close()
            self.log_error(f"MOTU meter connection failed: {exc}")
            return False

    def read(self) -> dict[int, tuple[int, int]]:
        if not self.connect():
            return self.last_pairs if time.monotonic() - self.last_seen <= MOTU_METER_MAX_AGE else {}

        deadline = time.monotonic() + MOTU_READ_WINDOW_SECONDS
        while time.monotonic() < deadline and self.ws is not None:
            try:
                _opcode, data = self.ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                break
            except Exception as exc:
                self.close()
                self.log_error(f"MOTU meter read failed: {exc}")
                break

            # MOTU meter frames are binary. A text frame here is never a valid
            # meter payload; encoding it as UTF-8 would corrupt any byte >= 0x80
            # (multi-byte), so decode 1:1 via latin-1 to preserve raw bytes.
            payload = data.encode("latin-1") if isinstance(data, str) else bytes(data or b"")
            if len(payload) != 104 or payload[:4] != bytes.fromhex("17700000"):
                continue

            body = payload[4:]
            self.last_pairs = {
                pair: (body[pair * 2], body[pair * 2 + 1])
                for pair in range(len(body) // 2)
            }
            self.last_seen = time.monotonic()

        if time.monotonic() - self.last_seen > MOTU_METER_MAX_AGE:
            return {}
        return self.last_pairs


def meter_pairs_active(pairs: dict[int, tuple[int, int]], watched_pairs: tuple[int, ...]) -> bool:
    return any(
        pair in pairs and min(pairs[pair]) < MOTU_METER_ACTIVE_BELOW
        for pair in watched_pairs
    )


def update_meter_timers(
    is_active: bool,
    active_timer: float,
    idle_timer: float,
    idle_seconds: float,
) -> tuple[float, float]:
    if is_active:
        return active_timer + CHECK_INTERVAL, 0.0

    idle_timer += CHECK_INTERVAL
    if idle_timer >= idle_seconds:
        active_timer = 0.0
    return active_timer, idle_timer


def is_alsa_active(card_name: str) -> bool:
    """Check if any ALSA PCM for the card is in RUNNING state."""
    for path in glob.glob(f"/proc/asound/{card_name}/pcm*/sub*/status"):
        try:
            with open(path, "r", encoding="utf-8") as status_file:
                if "state: RUNNING" in status_file.read():
                    return True
        except OSError:
            continue
    return False


def is_gadget_available() -> bool:
    """Check if USB Gadget capture rate is non-zero."""
    try:
        res = subprocess.check_output(
            ["amixer", "-c", "UAC2Gadget", "contents"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        in_capture_rate = False
        for line in res.splitlines():
            if "name='Capture Rate'" in line:
                in_capture_rate = True
                continue
            if in_capture_rate and ": values=" in line:
                rate_text = line.split("values=", 1)[1].strip().split(",", 1)[0]
                return int(rate_text) > 0
    except Exception:
        pass
    return False


def same_config(current: str | None, target: str) -> bool:
    if not current:
        return False
    return os.path.abspath(current) == os.path.abspath(target)


def source_for_config(current: str | None) -> str | None:
    for source, target in CONFIGS.items():
        if same_config(current, target):
            return source
    return None


def audio_active(levels: object) -> bool:
    if not isinstance(levels, (list, tuple)):
        return False
    return any(
        isinstance(level, (int, float)) and level > AUDIO_THRESHOLD_DB
        for level in levels
    )


def validate_configs() -> None:
    missing = [path for path in (TOSLINK_CFG, STREAMER_CFG, GADGET_CFG) if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError("Missing CamillaDSP config(s): " + ", ".join(missing))


def read_manual_source() -> str | None:
    try:
        source = open(SOURCE_OVERRIDE_PATH, "r", encoding="utf-8").read().strip().lower()
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"Could not read source override: {exc}", flush=True)
        return None

    if not source or source == "auto":
        return None
    return source


def apply_config(cdsp: CamillaClient, file_path: str, settle_time: float = SETTLE_TIME) -> None:
    """Apply a CamillaDSP config file and wait for hardware to settle."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    config_name = os.path.basename(file_path)
    print(f">>> Switching to: {config_name}", flush=True)
    cdsp.config.set_file_path(file_path)
    cdsp.general.reload()
    time.sleep(settle_time)


def log_idle(source: str, seconds: float) -> None:
    if DEBUG_MODE and int(seconds) > 0 and int(seconds) % 5 == 0:
        print(f"-> {source}: idle {seconds:g}/{IDLE_TIMEOUT:g}s", flush=True)


def main() -> int:
    print(">>> CamillaDSP Source Switcher Started <<<", flush=True)
    print(
        "Priority: manual override -> active current source -> 1) Streamer (AirPlay) -> 2) USB Gadget "
        "-> 3) TOSLINK meters -> 4) Analog meters -> idle keep-last",
        flush=True,
    )
    validate_configs()

    cdsp = CamillaClient(CAMILLA_IP, CAMILLA_PORT)
    motu = MotuMeterReader(MOTU_WS_URL) if TOSLINK_MOTU_METERS or ANALOG_MOTU_METERS else None
    streamer_silence_timer = 0.0
    gadget_silence_timer = 0.0
    toslink_active_timer = 0.0
    toslink_idle_timer = TOSLINK_IDLE_SECONDS
    analog_active_timer = 0.0
    analog_idle_timer = ANALOG_IDLE_SECONDS
    last_active_source = None
    last_manual_error = None
    error_log_deadline = 0.0
    last_error_message = None

    while True:
        try:
            if not cdsp.is_connected():
                cdsp.connect()
                print("Connected to CamillaDSP", flush=True)

            current_config = cdsp.config.file_path()
            manual_source = read_manual_source()
            if manual_source:
                target = CONFIGS.get(manual_source)
                if target is None:
                    error = f"Unknown manual source override: {manual_source}"
                    if error != last_manual_error:
                        print(error, flush=True)
                        last_manual_error = error
                    time.sleep(CHECK_INTERVAL)
                    continue
                if not os.path.exists(target):
                    error = f"Manual source config missing: {target}"
                    if error != last_manual_error:
                        print(error, flush=True)
                        last_manual_error = error
                    time.sleep(CHECK_INTERVAL)
                    continue

                last_manual_error = None
                if not same_config(current_config, target):
                    apply_config(cdsp, target)
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    toslink_active_timer = 0.0
                    toslink_idle_timer = TOSLINK_IDLE_SECONDS
                    analog_active_timer = 0.0
                    analog_idle_timer = ANALOG_IDLE_SECONDS
                    last_active_source = f"manual:{manual_source}"
                time.sleep(CHECK_INTERVAL)
                continue

            meter_pairs = motu.read() if motu is not None else {}
            toslink_meter_active = TOSLINK_MOTU_METERS and meter_pairs_active(
                meter_pairs,
                TOSLINK_METER_PAIRS,
            )
            analog_meter_active = ANALOG_MOTU_METERS and meter_pairs_active(
                meter_pairs,
                ANALOG_METER_PAIRS,
            )
            toslink_active_timer, toslink_idle_timer = update_meter_timers(
                toslink_meter_active,
                toslink_active_timer,
                toslink_idle_timer,
                TOSLINK_IDLE_SECONDS,
            )
            analog_active_timer, analog_idle_timer = update_meter_timers(
                analog_meter_active,
                analog_active_timer,
                analog_idle_timer,
                ANALOG_IDLE_SECONDS,
            )
            toslink_available = (
                TOSLINK_MOTU_METERS
                and toslink_active_timer >= TOSLINK_ACTIVE_SECONDS
                and toslink_idle_timer < TOSLINK_IDLE_SECONDS
            )
            analog_available = (
                ANALOG_MOTU_METERS
                and os.path.exists(ANALOG_CFG)
                and analog_active_timer >= ANALOG_ACTIVE_SECONDS
                and analog_idle_timer < ANALOG_IDLE_SECONDS
            )
            lower_priority_meter_available = toslink_available or analog_available
            streamer_hw_active = is_alsa_active("Loopback")
            gadget_hw_available = is_gadget_available()
            current_source = source_for_config(current_config)

            if DEBUG_MODE:
                print(
                    "DEBUG: "
                    f"Streamer HW={streamer_hw_active}, "
                    f"Gadget HW={gadget_hw_available}, "
                    f"TOSLINK meter={toslink_meter_active}/{toslink_active_timer:g}/{toslink_idle_timer:g}, "
                    f"Analog meter={analog_meter_active}/{analog_active_timer:g}/{analog_idle_timer:g}, "
                    f"Last={last_active_source}, "
                    f"Current={current_source}, "
                    f"ST={streamer_silence_timer:g}, "
                    f"GT={gadget_silence_timer:g}, "
                    f"Config={os.path.basename(current_config or '')}",
                    flush=True,
                )

            # Keep the current source while it still has confirmed audio.
            if current_source == "streamer" and streamer_hw_active:
                last_active_source = "streamer"
                if audio_active(cdsp.levels.capture_rms()):
                    streamer_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Streamer: current source active", flush=True)
                    time.sleep(CHECK_INTERVAL)
                    continue
                else:
                    streamer_silence_timer += CHECK_INTERVAL
                    log_idle("Streamer", streamer_silence_timer)

                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources", flush=True)
                last_active_source = None

            elif current_source == "streamer" and last_active_source == "streamer":
                streamer_silence_timer += CHECK_INTERVAL
                log_idle("Streamer grace", streamer_silence_timer)
                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer grace ended early for lower-priority meter source",
                            flush=True,
                        )
                last_active_source = None

            if current_source == "gadget" and gadget_hw_available:
                last_active_source = "gadget"
                if audio_active(cdsp.levels.capture_rms()):
                    gadget_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Gadget: current source active", flush=True)
                    time.sleep(CHECK_INTERVAL)
                    continue
                else:
                    gadget_silence_timer += CHECK_INTERVAL
                    log_idle("Gadget", gadget_silence_timer)

                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Gadget idle timeout - checking other sources", flush=True)
                last_active_source = None

            elif current_source == "gadget" and last_active_source == "gadget":
                gadget_silence_timer += CHECK_INTERVAL
                log_idle("Gadget grace", gadget_silence_timer)
                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget grace ended early for lower-priority meter source",
                            flush=True,
                        )
                last_active_source = None

            if current_source == "toslink" and (toslink_available or audio_active(cdsp.levels.capture_rms())):
                last_active_source = "toslink"
                if DEBUG_MODE:
                    print("-> TOSLINK: current source active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            if current_source == "analog" and (analog_available or audio_active(cdsp.levels.capture_rms())):
                last_active_source = "analog"
                if DEBUG_MODE:
                    print("-> Analog: current source active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            # Priority 1: Streamer (AirPlay via ALSA Loopback), only when changing sources.
            if current_source != "streamer" and streamer_hw_active:
                last_active_source = "streamer"
                if not same_config(current_config, STREAMER_CFG):
                    apply_config(cdsp, STREAMER_CFG)
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    streamer_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Streamer: audio active", flush=True)
                else:
                    streamer_silence_timer += CHECK_INTERVAL
                    log_idle("Streamer", streamer_silence_timer)

                if streamer_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or streamer_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Streamer silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Streamer idle timeout - checking other sources", flush=True)
                last_active_source = None

            # Priority 2: USB Gadget, only when changing sources.
            if current_source != "gadget" and gadget_hw_available:
                last_active_source = "gadget"
                if not same_config(current_config, GADGET_CFG):
                    apply_config(cdsp, GADGET_CFG, settle_time=1.5)
                    gadget_silence_timer = 0.0
                    streamer_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue

                if audio_active(cdsp.levels.capture_rms()):
                    gadget_silence_timer = 0.0
                    if DEBUG_MODE:
                        print("-> Gadget: audio active", flush=True)
                else:
                    gadget_silence_timer += CHECK_INTERVAL
                    log_idle("Gadget", gadget_silence_timer)

                if gadget_silence_timer < IDLE_TIMEOUT:
                    if (
                        not lower_priority_meter_available
                        or gadget_silence_timer < LOWER_PRIORITY_ACTIVE_TIMEOUT
                    ):
                        time.sleep(CHECK_INTERVAL)
                        continue
                    if DEBUG_MODE:
                        print(
                            "Gadget silent while lower-priority meter source is active",
                            flush=True,
                        )

                if DEBUG_MODE:
                    print("Gadget idle timeout - checking other sources", flush=True)
                last_active_source = None

            # Priority 3: TOSLINK via MOTU input meters.
            if current_source != "toslink" and toslink_available:
                last_active_source = "toslink"
                if not same_config(current_config, TOSLINK_CFG):
                    apply_config(cdsp, TOSLINK_CFG)
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue
                if DEBUG_MODE:
                    print("-> TOSLINK: meter active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            # Priority 4: Analog via MOTU input meters, disabled by default.
            if current_source != "analog" and analog_available:
                last_active_source = "analog"
                if not same_config(current_config, ANALOG_CFG):
                    apply_config(cdsp, ANALOG_CFG)
                    streamer_silence_timer = 0.0
                    gadget_silence_timer = 0.0
                    time.sleep(CHECK_INTERVAL)
                    continue
                if DEBUG_MODE:
                    print("-> Analog: meter active", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            if SOURCE_IDLE_MODE == "toslink" and not same_config(current_config, TOSLINK_CFG):
                apply_config(cdsp, TOSLINK_CFG)
                streamer_silence_timer = 0.0
                gadget_silence_timer = 0.0
                last_active_source = None
            elif DEBUG_MODE:
                print(f"-> Idle: keeping {os.path.basename(current_config or '')}", flush=True)

        except Exception as exc:
            # Throttle identical errors to once per 30s. A CamillaDSP outage
            # otherwise floods the journal (~430 lines/incident observed) and
            # wears the SD card; a newly-changed error still logs immediately.
            message = str(exc)
            now = time.monotonic()
            if message != last_error_message or now >= error_log_deadline:
                print(f"Error: {message}", flush=True)
                last_error_message = message
                error_log_deadline = now + 30.0
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
