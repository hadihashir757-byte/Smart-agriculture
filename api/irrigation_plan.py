"""Demo watering plans: short pulses first, then watering until the zone reaches its target.

Only used while the irrigation station is in DEMO mode (the dashboard switch). Normal mode keeps
the ordinary one-shot decision in watering.py.

  No rain expected : pump ON 3 s, wait 5 s, three times.  Still under 80 %?  keep watering in short
                     bursts (measuring the soil after each) until the zone reaches 80 %.
  Rain expected    : only water up to 40 %.  Pump ON 5 s, wait 8 s, twice.  Still under 40 %?
                     keep watering in short bursts until the zone reaches 40 %.

Safety that stays on in a plan: tank level, rain on the sensor, a hard cap on extra bursts, the
station's own checks (e-stop, tank, one pump at a time ...), and the plan stops if demo mode is
switched off. The plan never runs unless AUTO_WATER=true.
"""

import time
import uuid

DEMO_TARGET_PCT = 80
RAIN_TARGET_PCT = 40
DRY_PULSE = {"pulse_s": 3, "wait_s": 5, "pulses": 3}
RAIN_PULSE = {"pulse_s": 5, "wait_s": 8, "pulses": 2}
BURST_S = 5                 # length of each extra burst after the pulses (the station's demo limit)
MAX_EXTRA_BURSTS = 10       # hard cap: never more than this many extra bursts in one plan
TANK_MIN_PCT = 15
SOIL_WAIT_S = 60            # how long to wait for the rover to deliver a new soil reading


def make_plan(zone_id, pump, soil, rain_mm, rain_detected, tank_percent, rain_soon_mm=5.0):
    """Returns {'run': bool, 'text': str, ...}. 'text' is the explanation shown on the dashboard."""
    if rain_detected:
        return {"run": False, "zone_id": zone_id, "text": f"Zone {zone_id}: it is raining now, so the AI is not watering."}
    if tank_percent is None:
        return {"run": False, "zone_id": zone_id, "text": f"Zone {zone_id}: the tank level is unknown, so the AI is not watering."}
    if tank_percent < TANK_MIN_PCT:
        return {"run": False, "zone_id": zone_id,
                "text": f"Zone {zone_id}: the tank is at {tank_percent:.0f}% (below {TANK_MIN_PCT}%), so the AI is not watering."}
    if soil is None:
        return {"run": False, "zone_id": zone_id, "text": f"Zone {zone_id}: no soil reading yet, so the AI is not watering."}

    rain = rain_mm >= rain_soon_mm
    target = RAIN_TARGET_PCT if rain else DEMO_TARGET_PCT
    p = RAIN_PULSE if rain else DRY_PULSE
    if soil >= target:
        why = (f"Rain is expected ({rain_mm:.0f} mm), so zone {zone_id} only needs {target}% and it is already at {soil:.0f}%."
               if rain else f"Zone {zone_id} is already at {soil:.0f}% (target {target}%).")
        return {"run": False, "zone_id": zone_id, "text": why + " No watering needed."}

    head = (f"Rain is expected ({rain_mm:.0f} mm), so zone {zone_id} (now {soil:.0f}%) is only watered up to {target}%. "
            if rain else f"Zone {zone_id} is at {soil:.0f}% and the target is {target}%. ")
    text = (head + f"Plan: pump {pump} on for {p['pulse_s']} s, wait {p['wait_s']} s, {_times(p['pulses'])}. "
            f"If the soil is still under {target}% after that, the pump keeps watering in {BURST_S} s bursts "
            f"until the zone reaches {target}%.")
    return {"run": True, "zone_id": zone_id, "pump": pump, "target": target, "rain": rain, "soil": soil,
            "pulse_s": p["pulse_s"], "wait_s": p["wait_s"], "pulses": p["pulses"], "burst_s": BURST_S, "text": text}


def _times(n):
    return {1: "once", 2: "twice", 3: "three times"}.get(n, f"{n} times")


class Io:
    """What the runner needs from the outside world (replaced by fakes in tests)."""
    def water(self, pump, seconds):          # -> {'accepted': bool, 'reason': str}
        raise NotImplementedError

    def measure_soil(self):                  # -> float percent or None
        raise NotImplementedError

    def tank(self):                          # -> float percent or None
        raise NotImplementedError

    def demo_on(self):                       # -> bool
        raise NotImplementedError

    def say(self, text, **extra):            # publish progress to the dashboard
        pass

    def sleep(self, seconds):
        time.sleep(seconds)


def run_plan(plan, io):
    """Executes a plan from make_plan(). Returns {'outcome': ..., 'soil': ..., 'summary': ...}."""
    pump, zone, target = plan["pump"], plan["zone_id"], plan["target"]
    say = io.say
    say(plan["text"], ai_phase="planned", ai_target_percent=target)

    def start(seconds):
        reply = io.water(pump, seconds)
        if not reply.get("accepted"):
            return f"the station refused ({reply.get('reason', 'unknown')})"
        return None

    for i in range(1, plan["pulses"] + 1):
        if not io.demo_on():
            return _end(say, "stopped", None, f"Demo mode was switched off, so zone {zone} watering stopped.")
        say(f"Pulse {i} of {plan['pulses']}: pump {pump} on for {plan['pulse_s']} s.", ai_phase="pulse")
        err = start(plan["pulse_s"])
        if err:
            return _end(say, "refused", None, f"Zone {zone}: {err}, so the plan stopped.")
        io.sleep(plan["pulse_s"])
        say(f"Pulse {i} done. Waiting {plan['wait_s']} s for the water to soak in.", ai_phase="wait")
        io.sleep(plan["wait_s"])

    soil = io.measure_soil()
    for burst in range(0, MAX_EXTRA_BURSTS + 1):
        if soil is None:
            return _end(say, "no_reading", None,
                        f"Zone {zone}: no new soil reading arrived after watering, so the plan stopped. Check the rover soil sensor.")
        if soil >= target:
            return _end(say, "reached", soil, f"Zone {zone} reached {soil:.0f}% (target {target}%). Watering finished.")
        if burst == MAX_EXTRA_BURSTS:
            return _end(say, "gave_up", soil,
                        f"Zone {zone} is still at {soil:.0f}% after {MAX_EXTRA_BURSTS} extra bursts, so the AI stopped. Check the pump, pipe and soil sensor.")
        if not io.demo_on():
            return _end(say, "stopped", soil, f"Demo mode was switched off, so zone {zone} watering stopped at {soil:.0f}%.")
        tank = io.tank()
        if tank is None or tank < TANK_MIN_PCT:
            return _end(say, "tank_low", soil, f"The tank is too low to continue, so zone {zone} stopped at {soil:.0f}%.")
        say(f"Zone {zone} is at {soil:.0f}%, still under {target}%. Burst {burst + 1}: pump {pump} on for {plan['burst_s']} s.",
            ai_phase="continuous", ai_soil_percent=soil)
        err = start(plan["burst_s"])
        if err:
            return _end(say, "refused", soil, f"Zone {zone}: {err}, so the plan stopped at {soil:.0f}%.")
        io.sleep(plan["burst_s"])
        io.sleep(plan["wait_s"])
        soil = io.measure_soil()
    return _end(say, "gave_up", soil, "stopped")   # pragma: no cover


def _end(say, outcome, soil, text):
    say(text, ai_phase="done", **({"ai_soil_percent": soil} if soil is not None else {}))
    return {"outcome": outcome, "soil": soil, "summary": text}


def new_command_id():
    return "plan_" + uuid.uuid4().hex[:10]
