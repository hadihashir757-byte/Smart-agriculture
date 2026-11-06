"""Background agent: the part that decides WHEN to act. Run it next to the web server:

    python agent.py          # forever (checks every AGENT_POLL_S seconds)
    python agent.py --once   # one check, then exit (handy for testing)

Every check, using only OUTGOING connections (ThingsBoard, weather service, the phone camera):
  1. Ask ThingsBoard for the rover's latest readings.
  2. A NEW soil measurement (newer than the last one handled, and not too old) -> decide for that
     zone with rover_routes.evaluate_zone (same rules + safety layer as /zone-event; sends the water
     command only if AUTO_WATER=true).
  3. A photo: when the rover has just arrived at a zone (PHOTO_MODE=zone, the default) or every
     PHOTO_INTERVAL_S (PHOTO_MODE=timer, or both), and it is daylight -> fetch a snapshot from the
     phone camera and run the plant-health pipeline.

Settings (environment variables, all optional except the ThingsBoard login and the camera address):
  ROVER_DEVICE_NAME=rover 01   AGENT_POLL_S=60   PHOTO_INTERVAL_S=1800   PHOTO_RETRY_S=300
  PHOTO_MODE=zone              (zone | timer | both)  - use AGENT_POLL_S=10 so the photo is taken soon after arrival
  PHOTO_ARRIVAL_MAX_AGE_S=150  (an arrival older than this is too late for a photo: the rover has left)
  MIN_LIGHT_PERCENT=15         (no photos when the rover's light sensor reads below this)
  MAX_READING_AGE_S=900        (a soil reading older than this is never acted on)
  PHOTO_REQUEST_ZONE=1         (zone the "Take photo" dashboard button is for)
  PHOTO_REQUEST_MAX_AGE_S=300  (a button press older than this is ignored, e.g. one made while the agent was off)

The dashboard's "Take photo now" button sets the server attribute `photo_request` (the time of the click) on the
rover device. Every check the agent looks at it; a new value means: take one photo right now (even in the dark or
with the rover away from the zone), analyse it and show the result on the dashboard. Use AGENT_POLL_S=10 so it
answers within about 10 seconds.

The dashboard's "Zone 1 check" button sets `zone_check_request` the same way. The AI then acts as if the rover is
at the zone: it takes a photo and works out the irrigation advice (decision, seconds, reason, report) from the newest
soil measurement (not older than MAX_READING_AGE_S - press "Measure soil" first). It only ADVISES: nothing is sent
to the station from this button.
"""

import io
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envload  # noqa: E402,F401  (reads ../.env first, before the settings below are read)

import camera
import crops
import db
import plan_runner
import thingsboard
import watering

ROVER_DEVICE_NAME = os.environ.get("ROVER_DEVICE_NAME", "rover 01")
POLL_S = int(os.environ.get("AGENT_POLL_S", "60"))
PHOTO_INTERVAL_S = int(os.environ.get("PHOTO_INTERVAL_S", "1800"))
PHOTO_RETRY_S = int(os.environ.get("PHOTO_RETRY_S", "300"))
MIN_LIGHT_PERCENT = float(os.environ.get("MIN_LIGHT_PERCENT", "15"))
PHOTO_MODE = os.environ.get("PHOTO_MODE", "zone").strip().lower()      # zone | timer | both
PHOTO_ARRIVAL_MAX_AGE_S = int(os.environ.get("PHOTO_ARRIVAL_MAX_AGE_S", "150"))
PHOTO_REQUEST_ZONE = int(os.environ.get("PHOTO_REQUEST_ZONE", "1"))
PHOTO_REQUEST_MAX_AGE_S = int(os.environ.get("PHOTO_REQUEST_MAX_AGE_S", "300"))
WEATHER_PUSH_S = int(os.environ.get("WEATHER_PUSH_S", "900"))          # how often the forecast is sent to the dashboard

ROVER_KEYS = ["soil_moisture_percent", "temperature_c", "humidity_percent", "light_percent",
              "rain_detected", "zone_id", "event", "arrival_id", "at_zone"]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _bool(v):
    return str(v).strip().lower() in ("true", "1")


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _state_float(key):
    try:
        return float(db.get_state(key, 0))
    except (TypeError, ValueError):
        return 0.0


def handle_soil(rover, now, evaluate_zone, run_plan=None):
    """Returns a short text describing what happened (also used by tests)."""
    if "soil_moisture_percent" not in rover:
        return "no soil measurement yet"
    soil, ts_ms = rover["soil_moisture_percent"]
    if ts_ms <= _state_float("last_soil_ts"):
        return "no new soil measurement"
    db.set_state("last_soil_ts", ts_ms)          # remember first, so a crash can never double-water

    age_s = now - ts_ms / 1000.0
    if age_s > watering.MAX_READING_AGE_S:
        return f"new soil measurement is {int(age_s / 60)} min old - ignored"

    zone = thingsboard.value_at(ROVER_DEVICE_NAME, "zone_id", ts_ms)
    if zone is None and "zone_id" in rover:
        zone = rover["zone_id"][0]
    try:
        zone_id = int(float(zone))
    except (TypeError, ValueError):
        return "soil measured but the zone is unknown - skipped"

    readings = {
        "soil_moisture_percent": _float(soil),
        "temperature_c": _float(rover.get("temperature_c", (None,))[0]),
        "humidity_percent": _float(rover.get("humidity_percent", (None,))[0]),
        "light_percent": _float(rover.get("light_percent", (None,))[0]),
        "rain_detected": _bool(rover.get("rain_detected", (False,))[0]),
        "reading_ts": ts_ms / 1000.0,
    }
    result = evaluate_zone(zone_id, readings, crops.crop_for_zone(zone_id))
    if result.get("plan") and result.get("auto_water") and run_plan is not False:
        outcome = (run_plan or plan_runner.run)(result["plan"])
        return f"zone {zone_id}: plan {outcome['outcome']} - {outcome['summary']}"
    return f"zone {zone_id}: {result['decision']} {result['seconds']}s [{result['status']}] - {result['reason']}"


def _shoot(fetch=None, process_photo=None, zone_id=None):
    """Takes one photo and runs the plant-health pipeline. Returns (accepted, text)."""
    try:
        data = (fetch or camera.fetch_snapshot)()
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        return False, f"photo failed: {e}"
    if process_photo is None:
        from photo_pipeline import process_photo as _pp
        process_photo = _pp
    result = process_photo(img) if zone_id is None else process_photo(img, zone_id=zone_id)
    if result.get("accepted"):
        pred = result.get("prediction") or {}
        return True, f"photo ok: {pred.get('label')} ({pred.get('confidence', 0):.0%}), stage {result.get('growth_stage')}"
    if result.get("growth_stage") and "isn't available" in str(result.get("reason", "")):
        # a crop without a disease model (for example mango): the photo was still used for the growth stage, so it is done
        return True, f"photo ok: growth stage {result.get('growth_stage')} (no disease model for this crop)"
    return False, f"photo rejected: {result.get('reason') or 'the model was not confident enough (low confidence)'}"


def _too_dark(rover):
    light = _float(rover.get("light_percent", (None,))[0])
    if light is not None and light < MIN_LIGHT_PERCENT:
        return f"too dark for a photo (light {light:.0f}% < {MIN_LIGHT_PERCENT:.0f}%)"
    return None


def photo_on_arrival(rover, now, process_photo=None, fetch=None):
    """One photo each time the rover arrives at a zone. The rover sends a new random `arrival_id` with every
    report while it stands at a zone; we take the photo for each id once, as long as the rover is still there."""
    if "arrival_id" not in rover:
        return "photo not due"
    arrival_id, ts_ms = rover["arrival_id"]
    arrival_id = str(arrival_id).split(".")[0]
    if arrival_id in ("", "0") or arrival_id == str(db.get_state("last_arrival_id", "")):
        return "photo not due"
    if now - ts_ms / 1000.0 > PHOTO_ARRIVAL_MAX_AGE_S:
        db.set_state("last_arrival_id", arrival_id)         # too late: the rover has left this zone
        return "photo not due"
    dark = _too_dark(rover)
    if dark:
        db.set_state("last_arrival_id", arrival_id)         # dark: no point trying again at this zone
        return dark
    zone = rover.get("zone_id", (None,))[0]
    try:
        zone_id = int(float(zone))
    except (TypeError, ValueError):
        zone_id = None
    ok, text = _shoot(fetch, process_photo, zone_id)
    if ok:
        db.set_state("last_arrival_id", arrival_id)
        db.set_state("last_photo_ts", now)
        return f"zone {zone}: {text}"
    return f"zone {zone}: {text} - will try again while the rover is still there"      # not marked done: retried on the next check


def photo_on_timer(rover, now, process_photo=None, fetch=None):
    last_photo = _state_float("last_photo_ts")
    if (now - last_photo) < PHOTO_INTERVAL_S:
        return "photo not due"
    dark = _too_dark(rover)
    if dark:
        return dark
    retry_at = now - PHOTO_INTERVAL_S + PHOTO_RETRY_S     # makes the next try happen in PHOTO_RETRY_S
    ok, text = _shoot(fetch, process_photo)
    if ok:
        db.set_state("last_photo_ts", now)
        return text
    db.set_state("last_photo_ts", retry_at)
    if text.startswith("photo rejected"):
        return f"{text} - will retry in {PHOTO_RETRY_S // 60} min"
    return text


def handle_photo(rover, now, process_photo=None, fetch=None):
    """Takes a photo when due (on arrival at a zone and/or on a timer, see PHOTO_MODE). Returns a short text."""
    if not camera.configured():
        return "camera not configured (CAMERA_BASE_URL)"
    notes = []
    if PHOTO_MODE in ("zone", "both"):
        notes.append(photo_on_arrival(rover, now, process_photo, fetch))
    if PHOTO_MODE in ("timer", "both"):
        notes.append(photo_on_timer(rover, now, process_photo, fetch))
    real = [n for n in notes if n != "photo not due"]
    return "; ".join(real) if real else "photo not due"


def _new_click(read_request, state_key, now):
    """Looks at a dashboard button's attribute. Returns None when there is no NEW click, "old" when the click is
    older than PHOTO_REQUEST_MAX_AGE_S (ignored), or "new" for a fresh click. A click is remembered first, so one
    click can never be handled twice."""
    value = read_request()
    if value in (None, ""):
        return None
    value = str(value).split(".")[0]
    if value == str(db.get_state(state_key, "")):
        return None
    db.set_state(state_key, value)
    try:
        age_s = now - int(value) / 1000.0                # the button stores the click time in milliseconds
    except ValueError:
        age_s = 0
    return "old" if age_s > PHOTO_REQUEST_MAX_AGE_S else "new"


def _stamp(now):
    return time.strftime("%d %b %H:%M", time.localtime(now))


def handle_photo_request(now, process_photo=None, fetch=None, read_request=None, push=None):
    """The dashboard's "Take photo now" button: a NEW value of the rover attribute `photo_request` means one photo
    right now. The photo goes through the same pipeline (quality gate, growth stage, disease model) and the result
    appears on the dashboard cards; a one-line result is also sent as `ai_photo_status`."""
    read_request = read_request or (lambda: thingsboard.server_attribute(ROVER_DEVICE_NAME, "photo_request"))
    push = push or thingsboard.push_ai_telemetry
    click = _new_click(read_request, "last_photo_request", now)
    if click is None:
        return "photo request not due"
    if click == "old":
        return "photo request is old - ignored"
    if not camera.configured():
        text = "camera not configured (CAMERA_BASE_URL)"
    else:
        ok, text = _shoot(fetch, process_photo, PHOTO_REQUEST_ZONE)
        if ok:
            db.set_state("last_photo_ts", now)
    push({"ai_photo_status": f"{_stamp(now)} - {text}"})
    return f"button: {text}"


def handle_zone_check(now, evaluate_zone=None, process_photo=None, fetch=None, read_request=None, push=None, rover=None):
    """The dashboard's "Zone 1 check" button (rover attribute `zone_check_request`). The AI treats it as "the rover
    is at the zone now": it takes a photo for the zone, then works out the irrigation advice from the newest soil
    measurement, the plant result, the weather and the tank, and shows it on the dashboard decision cards.
    ADVICE ONLY: nothing is ever sent to the station from this button, whatever AUTO_WATER says."""
    read_request = read_request or (lambda: thingsboard.server_attribute(ROVER_DEVICE_NAME, "zone_check_request"))
    push = push or thingsboard.push_ai_telemetry
    click = _new_click(read_request, "last_zone_check", now)
    if click is None:
        return "zone check not due"
    if click == "old":
        return "zone check is old - ignored"
    zone_id = PHOTO_REQUEST_ZONE
    notes = []
    if camera.configured():
        ok, text = _shoot(fetch, process_photo, zone_id)
        notes.append(text)
        if ok:
            db.set_state("last_photo_ts", now)
    else:
        notes.append("no photo (camera not configured)")
    rover = rover if rover is not None else thingsboard.latest_with_ts(ROVER_DEVICE_NAME, ROVER_KEYS)
    soil = rover.get("soil_moisture_percent")
    if soil is None or now - soil[1] / 1000.0 > watering.MAX_READING_AGE_S:
        notes.append("no recent soil measurement - press Measure soil, then Zone 1 check again")
        push({"ai_photo_status": f"{_stamp(now)} - Zone {zone_id} check: " + "; ".join(notes)})
        return "zone check: " + "; ".join(notes)
    readings = {
        "soil_moisture_percent": _float(soil[0]),
        "temperature_c": _float(rover.get("temperature_c", (None,))[0]),
        "humidity_percent": _float(rover.get("humidity_percent", (None,))[0]),
        "light_percent": _float(rover.get("light_percent", (None,))[0]),
        "rain_detected": _bool(rover.get("rain_detected", (False,))[0]),
        "reading_ts": soil[1] / 1000.0,
    }
    if evaluate_zone is None:
        from rover_routes import evaluate_zone as _ez
        evaluate_zone = _ez
    result = evaluate_zone(zone_id, readings, crops.crop_for_zone(zone_id), advice_only=True)
    notes.append(f"advice: {result['decision']} {result['seconds']}s (nothing was started) - {result['reason']}")
    push({"ai_photo_status": f"{_stamp(now)} - Zone {zone_id} check: " + "; ".join(notes)})
    return f"zone {zone_id} check: " + "; ".join(notes)


def push_weather(now, forecast=None):
    """Sends the weather forecast to the ThingsBoard dashboard every WEATHER_PUSH_S seconds."""
    if (now - _state_float("last_weather_ts")) < WEATHER_PUSH_S:
        return "weather not due"
    db.set_state("last_weather_ts", now)               # also on failure: tried recently, try again next interval
    if not thingsboard.AI_DEVICE_TOKEN:
        return "weather: AI_DEVICE_TOKEN not set - not shown on the dashboard"
    if not (os.environ.get("LOCATION_LAT") and os.environ.get("LOCATION_LON")):
        return "weather: LOCATION_LAT / LOCATION_LON not set"
    if forecast is None:
        from weather import get_weather_forecast as forecast
    data = thingsboard.weather_telemetry(forecast(), now)
    if not data:
        return "weather: forecast unavailable"
    thingsboard.push_ai_telemetry(data)
    return f"weather sent to the dashboard: {data['ai_weather_summary']}"


def tick(now=None, evaluate_zone=None):
    now = time.time() if now is None else now
    if not thingsboard.configured():
        return ["ThingsBoard login is not set (TB_USERNAME / TB_PASSWORD) - nothing to do"]
    if evaluate_zone is None:
        from rover_routes import evaluate_zone as _ez
        evaluate_zone = _ez
    rover = thingsboard.latest_with_ts(ROVER_DEVICE_NAME, ROVER_KEYS)
    notes = []
    for step in (lambda: handle_zone_check(now, evaluate_zone, rover=rover), lambda: handle_photo_request(now), lambda: handle_photo(rover, now), lambda: handle_soil(rover, now, evaluate_zone), lambda: push_weather(now)):   # photo first: the decision then uses a fresh plant-health result
        try:
            notes.append(step())
        except Exception as e:  # noqa: BLE001 - one failing job must not stop the other
            notes.append(f"error: {e.__class__.__name__}: {e}")
    return notes


_stop = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def main():
    db.init_db()
    once = "--once" in sys.argv
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    log(f"agent started: checking '{ROVER_DEVICE_NAME}' every {POLL_S}s, photos: {PHOTO_MODE}, "
        f"AUTO_WATER={os.environ.get('AUTO_WATER', 'false')}")
    while not _stop:
        try:
            for n in tick():
                if n not in ("no new soil measurement", "photo not due", "photo request not due", "zone check not due", "weather not due"):
                    log(n)
        except Exception as e:  # noqa: BLE001
            log(f"check failed (will try again): {e.__class__.__name__}: {e}")
        if once:
            break
        for _ in range(POLL_S):
            if _stop:
                break
            time.sleep(1)
    log("agent stopped")


if __name__ == "__main__":
    main()
