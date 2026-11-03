"""Background agent: the part that decides WHEN to act. Run it next to the web server:

    python agent.py          # forever (checks every AGENT_POLL_S seconds)
    python agent.py --once   # one check, then exit (handy for testing)

Every check, using only OUTGOING connections (ThingsBoard, weather service, the phone camera):
  1. Ask ThingsBoard for the rover's latest readings.
  2. A NEW soil measurement (newer than the last one handled, and not too old) -> decide for that
     zone with rover_routes.evaluate_zone (same rules + safety layer as /zone-event; sends the water
     command only if AUTO_WATER=true).
  3. Time for a photo (every PHOTO_INTERVAL_S), and it is
     daylight -> fetch a snapshot from the phone camera and run the plant-health pipeline.

Settings (environment variables, all optional except the ThingsBoard login and the camera address):
  ROVER_DEVICE_NAME=rover 01   AGENT_POLL_S=60   PHOTO_INTERVAL_S=1800   PHOTO_RETRY_S=300
  MIN_LIGHT_PERCENT=15         (no photos when the rover's light sensor reads below this)
  MAX_READING_AGE_S=900        (a soil reading older than this is never acted on)
"""

import io
import os
import signal
import sys
import time

import camera
import db
import irrigation_plan
import thingsboard
import watering

ROVER_DEVICE_NAME = os.environ.get("ROVER_DEVICE_NAME", "rover 01")
POLL_S = int(os.environ.get("AGENT_POLL_S", "60"))
PHOTO_INTERVAL_S = int(os.environ.get("PHOTO_INTERVAL_S", "1800"))
PHOTO_RETRY_S = int(os.environ.get("PHOTO_RETRY_S", "300"))
MIN_LIGHT_PERCENT = float(os.environ.get("MIN_LIGHT_PERCENT", "15"))
DEFAULT_CROP = os.environ.get("DEFAULT_CROP", "tomato")

ROVER_KEYS = ["soil_moisture_percent", "temperature_c", "humidity_percent", "light_percent",
              "rain_detected", "zone_id", "event"]


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
    result = evaluate_zone(zone_id, readings, DEFAULT_CROP)
    if result.get("plan") and result.get("auto_water") and run_plan is not False:
        outcome = (run_plan or _run_demo_plan)(result["plan"])
        return f"zone {zone_id}: demo plan {outcome['outcome']} - {outcome['summary']}"
    return f"zone {zone_id}: {result['decision']} {result['seconds']}s [{result['status']}] - {result['reason']}"


class _RealIo(irrigation_plan.Io):
    """Talks to the real station and rover through ThingsBoard."""

    def __init__(self, zone_id):
        self.zone_id = zone_id

    def water(self, pump, seconds):
        params = {"pump": pump, "seconds": seconds, "command_id": irrigation_plan.new_command_id(),
                  "issued_at": int(time.time())}
        try:
            reply = thingsboard.send_rpc(thingsboard.STATION_DEVICE_NAME, "water", params)
        except Exception as e:  # noqa: BLE001
            return {"accepted": False, "reason": f"could not reach the station: {e}"}
        db.insert_watering_decision(self.zone_id, pump, seconds, "water", "demo plan step", params["command_id"],
                                    "sent_accepted" if reply.get("accepted") else "sent_rejected",
                                    {"demo": True})
        return reply

    def measure_soil(self):
        t0 = int(time.time() * 1000)
        try:
            thingsboard.send_rpc(ROVER_DEVICE_NAME, "measureSoil", {})
        except Exception as e:  # noqa: BLE001
            log(f"could not ask the rover to measure soil: {e}")
        deadline = time.time() + irrigation_plan.SOIL_WAIT_S
        while time.time() < deadline:
            time.sleep(3)
            got = thingsboard.latest_with_ts(ROVER_DEVICE_NAME, ["soil_moisture_percent"]).get("soil_moisture_percent")
            if got and got[1] > t0:
                db.set_state("last_soil_ts", got[1])      # so the normal check does not handle this reading again
                return _float(got[0])
        return None

    def tank(self):
        return _float(thingsboard.latest_telemetry(thingsboard.STATION_DEVICE_NAME, ["tank_level_percent"]).get("tank_level_percent"))

    def demo_on(self):
        v = thingsboard.latest_telemetry(thingsboard.STATION_DEVICE_NAME, ["demo_mode"]).get("demo_mode")
        return str(v).strip().lower() in ("true", "1")

    def hold(self, on):
        try:
            thingsboard.send_rpc(ROVER_DEVICE_NAME, "holdZone", bool(on))
        except Exception as e:  # noqa: BLE001
            log(f"could not {'hold' if on else 'release'} the rover at the zone: {e}")

    def say(self, text, **extra):
        log(f"[plan] {text}")
        thingsboard.push_ai_telemetry({"ai_zone_id": self.zone_id, "ai_explanation": text, "ai_progress": text, **extra})


def _run_demo_plan(plan):
    return irrigation_plan.run_plan(plan, _RealIo(plan["zone_id"]))


def handle_photo(rover, now, process_photo=None, fetch=None):
    """Takes a photo when due. Returns a short text."""
    if not camera.configured():
        return "camera not configured (CAMERA_BASE_URL)"
    last_photo = _state_float("last_photo_ts")
    if (now - last_photo) < PHOTO_INTERVAL_S:
        return "photo not due"

    light = _float(rover.get("light_percent", (None,))[0])
    if light is not None and light < MIN_LIGHT_PERCENT:
        return f"too dark for a photo (light {light:.0f}% < {MIN_LIGHT_PERCENT:.0f}%)"

    retry_at = now - PHOTO_INTERVAL_S + PHOTO_RETRY_S     # makes the next try happen in PHOTO_RETRY_S
    try:
        data = (fetch or camera.fetch_snapshot)()
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        db.set_state("last_photo_ts", retry_at)
        return f"photo failed: {e}"
    if process_photo is None:
        from photo_pipeline import process_photo as _pp
        process_photo = _pp
    result = process_photo(img)
    if result.get("accepted"):
        db.set_state("last_photo_ts", now)
        pred = result.get("prediction") or {}
        return f"photo ok: {pred.get('label')} ({pred.get('confidence', 0):.0%}), stage {result.get('growth_stage')}"
    db.set_state("last_photo_ts", retry_at)
    return f"photo rejected: {result.get('reason')} - will retry in {PHOTO_RETRY_S // 60} min"


def tick(now=None, evaluate_zone=None):
    now = time.time() if now is None else now
    if not thingsboard.configured():
        return ["ThingsBoard login is not set (TB_USERNAME / TB_PASSWORD) - nothing to do"]
    if evaluate_zone is None:
        from rover_routes import evaluate_zone as _ez
        evaluate_zone = _ez
    rover = thingsboard.latest_with_ts(ROVER_DEVICE_NAME, ROVER_KEYS)
    notes = []
    for step in (lambda: handle_photo(rover, now), lambda: handle_soil(rover, now, evaluate_zone)):   # photo first: the decision then uses a fresh plant-health result
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
    log(f"agent started: checking '{ROVER_DEVICE_NAME}' every {POLL_S}s, photo every {PHOTO_INTERVAL_S // 60} min, "
        f"AUTO_WATER={os.environ.get('AUTO_WATER', 'false')}")
    while not _stop:
        try:
            for n in tick():
                if n not in ("no new soil measurement", "photo not due"):
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
