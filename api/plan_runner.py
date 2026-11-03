"""Runs watering plans (demo pulses or normal turns) against the real station and rover via ThingsBoard.

Used by the background agent (blocks while the plan runs) and by /zone-event (background thread).
Only one plan runs at a time.
"""
import os
import threading
import time

import db
import irrigation_plan
import thingsboard
import water_amount

ROVER_DEVICE_NAME = os.environ.get("ROVER_DEVICE_NAME", "rover 01")
_lock = threading.Lock()


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
        db.insert_watering_decision(self.zone_id, pump, seconds, "water", "plan step", params["command_id"],
                                    "sent_accepted" if reply.get("accepted") else "sent_rejected",
                                    {"plan": True})
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


def run(plan):
    """Runs one plan to the end. Returns {'outcome', 'soil', 'summary'}. Refuses to start if another plan is running."""
    if not _lock.acquire(blocking=False):
        return {"outcome": "busy", "soil": None, "summary": "another watering plan is still running"}
    try:
        io = _RealIo(plan["zone_id"])
        if plan.get("kind") == "turns":
            return water_amount.run_turns(plan, io)
        return irrigation_plan.run_plan(plan, io)
    finally:
        _lock.release()


def start_in_background(plan):
    if _lock.locked():
        return False
    threading.Thread(target=run, args=(plan,), daemon=True).start()
    return True
