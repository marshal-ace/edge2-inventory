"""
pi_server.py  —  Single-process Pi service for Edge2 Smart Inventory System
Replaces: pi_api.py + temp_reporter.py

NOW INCLUDES: MCP23017 (DFRobot Gravity I2C GPIO Expander) support.
This gives 16 extra digital output pins (for LEDs) on top of the 6 native
Raspberry Pi BCM GPIO pins already in use, without touching the TCA9548A
multiplexer logic used for the MCP9808 temperature sensors.

PIN NUMBERING CONVENTION (important for the Railway/Flask side too):
  - gpio_pin  0-99   -> native Raspberry Pi BCM GPIO pin (unchanged, e.g. 17,27,22,23,24,25)
  - gpio_pin 100-115 -> MCP23017 extender pin (gpio_pin - 100 = MCP pin 0-15)

So MCP pin 0 is addressed as gpio_pin=100, MCP pin 1 as gpio_pin=101, ... MCP pin 15 as gpio_pin=115.
This keeps the /led API identical — callers just pass a bigger gpio_pin number and this
file transparently routes it to the extender instead of native GPIO.

Usage:
  PI_API_TOKEN=yourtoken FLASK_URL=https://yourapp.railway.app python pi_server.py

Environment variables:
  PI_API_TOKEN   — shared secret with Railway app
  FLASK_URL      — Railway app base URL
  MCP23017_ADDR  — (optional) override I2C address of the extender, default 0x20

Requires (in addition to existing requirements):
  pip install adafruit-circuitpython-mcp230xx adafruit-blinka
"""

from flask import Flask, request, jsonify
from gpiozero import LED
from threading import Timer, Thread
import os
import time
import csv
import requests as _requests
from datetime import datetime

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
API_TOKEN     = os.environ.get("PI_API_TOKEN", "changeme-secret")
FLASK_URL     = os.environ.get("FLASK_URL", "").rstrip("/")
AUTO_OFF_S    = 33
TEMP_INTERVAL = 30

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temperature_log.csv")

# sensor_id (matches Railway DB) → multiplexer channel
SENSOR_CHANNELS = {1: 0, 2: 1, 3: 2}
TCA9548A_ADDR   = 0x70

# ── MCP23017 GPIO extender config ─────────────────────────────────────────────
MCP23017_ADDR      = int(os.environ.get("MCP23017_ADDR", "0x20"), 16) \
                      if os.environ.get("MCP23017_ADDR") else 0x20
EXTENDER_PIN_OFFSET = 100          # gpio_pin >= 100 => extender pin (gpio_pin - 100)
EXTENDER_PIN_COUNT  = 16           # MCP23017 has 16 GPIO pins (0-15)

# ── I2C — initialised lazily inside the background thread only ────────────────
# Do NOT init busio/board at module level: if I2C is locked or unavailable
# the whole process crashes before Flask can even start.
_i2c = None

def _get_i2c():
    """Lazily initialise I2C bus. Returns bus or None on failure."""
    global _i2c
    if _i2c is not None:
        return _i2c
    try:
        import board, busio
        _i2c = busio.I2C(board.SCL, board.SDA)
        print("[pi_server] I2C bus initialised", flush=True)
        return _i2c
    except Exception as e:
        print(f"[pi_server] I2C init failed: {e}", flush=True)
        return None

def _select_channel(i2c, channel: int):
    i2c.writeto(TCA9548A_ADDR, bytes([1 << channel]))

def _deselect_all(i2c):
    try:
        i2c.writeto(TCA9548A_ADDR, bytes([0x00]))
    except Exception:
        pass

def _read_sensor(channel: int):
    """Read temperature from MCP9808 on given multiplexer channel. Returns float or None."""
    import adafruit_mcp9808
    i2c = _get_i2c()
    if i2c is None:
        return None
    try:
        _select_channel(i2c, channel)
        sensor = adafruit_mcp9808.MCP9808(i2c)
        temp = round(sensor.temperature, 2)
        _deselect_all(i2c)
        return temp
    except Exception as e:
        _deselect_all(i2c)
        print(f"[pi_server] Channel {channel} read failed: {e}", flush=True)
        return None

# ── MCP23017 extender — lazily initialised, independent of the TCA9548A mux ───
# The Gravity MCP23017 module sits directly on the main I2C bus (its own fixed
# address, default 0x20), NOT behind the temperature-sensor multiplexer, so it
# is unaffected by _select_channel()/_deselect_all() calls above.
_mcp = None

def _get_mcp():
    """Lazily initialise the MCP23017 GPIO extender. Returns device or None on failure."""
    global _mcp
    if _mcp is not None:
        return _mcp
    try:
        from adafruit_mcp230xx.mcp23017 import MCP23017
        i2c = _get_i2c()
        if i2c is None:
            return None
        _mcp = MCP23017(i2c, address=MCP23017_ADDR)
        print(f"[pi_server] MCP23017 extender initialised at 0x{MCP23017_ADDR:02X}", flush=True)
        return _mcp
    except Exception as e:
        print(f"[pi_server] MCP23017 init failed: {e}", flush=True)
        return None


class _ExtenderLED:
    """Wraps a single MCP23017 pin so it exposes the same .on()/.off() interface
    as gpiozero.LED — this lets _get_led() return either type interchangeably
    and every other part of this file (auto-off timers, /led, /led/off) works
    unmodified regardless of whether a pin is native or on the extender."""

    def __init__(self, mcp, pin_num: int):
        import digitalio
        self._pin = mcp.get_pin(pin_num)
        self._pin.direction = digitalio.Direction.OUTPUT
        self._pin.value = False

    def on(self):
        self._pin.value = True

    def off(self):
        self._pin.value = False


# ── GPIO/extender LED objects — also lazy ─────────────────────────────────────
_LED_OBJECTS: dict = {}

def _get_led(gpio_pin: int):
    """Return an LED-like object (native gpiozero.LED or _ExtenderLED) for gpio_pin.
    gpio_pin >= EXTENDER_PIN_OFFSET routes to the MCP23017 extender."""
    if gpio_pin not in _LED_OBJECTS:
        if gpio_pin >= EXTENDER_PIN_OFFSET:
            ext_pin = gpio_pin - EXTENDER_PIN_OFFSET
            if not (0 <= ext_pin < EXTENDER_PIN_COUNT):
                raise ValueError(
                    f"Invalid extender pin {ext_pin} (gpio_pin={gpio_pin}); "
                    f"must map to 0-{EXTENDER_PIN_COUNT - 1}"
                )
            mcp = _get_mcp()
            if mcp is None:
                raise RuntimeError("MCP23017 extender not available (check wiring/I2C)")
            _LED_OBJECTS[gpio_pin] = _ExtenderLED(mcp, ext_pin)
        else:
            _LED_OBJECTS[gpio_pin] = LED(gpio_pin)
    return _LED_OBJECTS[gpio_pin]

# ── Per-pin independent auto-off timers ───────────────────────────────────────
_timers: dict = {}

def _make_off_fn(gpio_pin: int):
    def _off():
        print(f"[pi_server] Auto-off: GPIO{gpio_pin}", flush=True)
        _get_led(gpio_pin).off()
        _timers.pop(gpio_pin, None)
    return _off

def _schedule_auto_off(gpio_pin: int):
    if gpio_pin in _timers and _timers[gpio_pin] is not None:
        _timers[gpio_pin].cancel()
    t = Timer(AUTO_OFF_S, _make_off_fn(gpio_pin))
    t.daemon = True
    t.start()
    _timers[gpio_pin] = t
    print(f"[pi_server] Auto-off scheduled for GPIO{gpio_pin} in {AUTO_OFF_S}s", flush=True)

def _all_off():
    for led in _LED_OBJECTS.values():
        led.off()
    for t in list(_timers.values()):
        if t:
            t.cancel()
    _timers.clear()

# ── Auth ──────────────────────────────────────────────────────────────────────
def _authorized(req) -> bool:
    return req.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"

# ── CSV logger ────────────────────────────────────────────────────────────────
def _ensure_log_header():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp", "sensor_id", "location", "temperature"])

def _append_log(sensor_id: int, location: str, temperature: float):
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            sensor_id, location, temperature,
        ])

# ── Upload to Railway ─────────────────────────────────────────────────────────
def _upload_reading(sensor_id: int, temperature: float):
    if not FLASK_URL:
        return
    try:
        resp = _requests.post(
            f"{FLASK_URL}/api/temperature",
            json={"temperature": temperature, "humidity": None, "sensor_id": sensor_id},
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=5,
        )
        print(f"[pi_server] Sensor {sensor_id}: {temperature}°C → Railway {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[pi_server] Sensor {sensor_id} POST failed: {e}", flush=True)

# ── Background temperature monitor ────────────────────────────────────────────
def _temp_monitor_loop():
    _ensure_log_header()
    print("[pi_server] Temperature monitor loop started", flush=True)
    while True:
        for sensor_id, channel in SENSOR_CHANNELS.items():
            temp = _read_sensor(channel)
            if temp is None:
                print(f"[pi_server] Sensor {sensor_id} (CH{channel}): no reading", flush=True)
                continue
            _append_log(sensor_id, f"sensor_{sensor_id}", temp)
            _upload_reading(sensor_id, temp)
        time.sleep(TEMP_INTERVAL)

def _start_temp_monitor():
    t = Thread(target=_temp_monitor_loop, daemon=True, name="temp-monitor")
    t.start()
    print(f"[pi_server] Temperature monitor started (interval={TEMP_INTERVAL}s)", flush=True)

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/led", methods=["POST"])
def led():
    if not _authorized(request):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # Accept gpio_pin (new, also used for extender pins >=100) OR column letter (legacy)
    gpio_pin = data.get("gpio_pin")
    if gpio_pin is None:
        _legacy = {"A": 17, "B": 27, "C": 22}
        letter   = str(data.get("column", "")).upper()
        gpio_pin = _legacy.get(letter)

    if gpio_pin is None:
        return jsonify({
            "status": "error",
            "message": "Provide 'gpio_pin' (int) or legacy 'column' (A/B/C)"
        }), 400

    try:
        gpio_pin = int(gpio_pin)
        _get_led(gpio_pin).on()
    except (ValueError, RuntimeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"GPIO error: {e}"}), 500

    _schedule_auto_off(gpio_pin)
    source = "extender" if gpio_pin >= EXTENDER_PIN_OFFSET else "native"
    print(f"[pi_server] GPIO{gpio_pin} ON ({source})", flush=True)
    return jsonify({"status": "success", "gpio_pin": gpio_pin, "source": source})


@app.route("/led/off", methods=["POST"])
def led_off():
    if not _authorized(request):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    _all_off()
    print("[pi_server] All LEDs OFF (manual)", flush=True)
    return jsonify({"status": "success", "message": "All LEDs off"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "pi_server",
        "mcp23017_configured": True,
        "mcp23017_addr": f"0x{MCP23017_ADDR:02X}",
        "mcp23017_online": _mcp is not None,
    })


@app.route("/gpio/pins", methods=["GET"])
def gpio_pins():
    """Diagnostic: lists which native/extender pins are currently in use and
    whether the extender is reachable. Useful when wiring up new columns."""
    if not _authorized(request):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    mcp = _get_mcp()
    return jsonify({
        "extender_online": mcp is not None,
        "extender_pin_range": [EXTENDER_PIN_OFFSET, EXTENDER_PIN_OFFSET + EXTENDER_PIN_COUNT - 1],
        "active_pins": sorted(_LED_OBJECTS.keys()),
    })


@app.route("/temperature", methods=["GET"])
def temperature():
    """On-demand: read all sensors now and return JSON."""
    if not _authorized(request):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    results = []
    for sensor_id, channel in SENSOR_CHANNELS.items():
        temp = _read_sensor(channel)
        results.append({"sensor_id": sensor_id, "channel": channel, "temperature": temp})
    return jsonify(results)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[pi_server] Starting — Token={'SET' if API_TOKEN != 'changeme-secret' else 'DEFAULT'}, FLASK_URL={FLASK_URL or '(not set)'}", flush=True)
    print(f"[pi_server] MCP23017 extender expected at 0x{MCP23017_ADDR:02X} (pins {EXTENDER_PIN_OFFSET}-{EXTENDER_PIN_OFFSET + EXTENDER_PIN_COUNT - 1})", flush=True)
    _start_temp_monitor()
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
