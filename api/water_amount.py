"""NORMAL mode: how many seconds to water, and in how many turns.

The go / no-go decision stays in watering.decide(). Once it says "water", this module works out the
amount from the crop, its growth stage, how far the soil is below its comfortable level, the weather
and the plant's health, then splits it into short turns (soak time between them).

  target moisture  : tomato 60 % (seedling 65), pepper 60 % (65), plantain 70 % (75), other 60 % (65)
  seconds          : soil gap (target - soil) x crop factor x stage factor x weather factor
  crop factor      : tomato 1.0 | pepper 0.85 | plantain 1.25 | other 1.0
  stage factor     : seedling 0.7 (small root zone) | mature 1.0
  weather factor   : 32 C or hotter x1.2 | 18 C or cooler x0.85 | air humidity <= 30 % x1.1 | >= 85 % x0.9
  plant health     : stress +5 s, fungal disease: gentle (<= 15 s), see watering._health_adjust
  limits           : 5 .. 60 s in total; one turn is at most 20 s; 60 s rest between turns (the station's rule)
  rain coming      : only the short top-up the rules allow (one turn)
"""
import math

import watering

TARGET_PCT = {
    "tomato": {"seedling": 65, "mature": 60},
    "plantain": {"seedling": 75, "mature": 70},
    "pepper": {"seedling": 65, "mature": 60},
    "default": {"seedling": 65, "mature": 60},
}
CROP_FACTOR = {"tomato": 1.0, "pepper": 0.85, "plantain": 1.25, "default": 1.0}
STAGE_FACTOR = {"seedling": 0.7, "mature": 1.0}
MIN_TOTAL_S, MAX_TOTAL_S = 5, 60
TURN_MAX_S = 20
REST_S = 60          # the station's normal-mode rest between runs of the same pump


def _key(crop, table):
    return crop if crop in table else "default"


def split_turns(total):
    n = max(1, math.ceil(total / TURN_MAX_S))
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def make_turn_plan(zone_id, pump, soil, crop, stage, temperature_c, humidity_pct, rain_soon, decided_seconds,
                   decide_reason, plant_health=None):
    crop = (crop or "default").lower()
    stage = stage if stage in STAGE_FACTOR else "mature"
    target = TARGET_PCT[_key(crop, TARGET_PCT)][stage]
    label = f"{crop}, {stage}"

    if rain_soon:
        total = max(MIN_TOTAL_S, int(decided_seconds))
        text = (f"Zone {zone_id} ({label}) is at {soil:.0f}% and rain is coming, so the AI only gives a short top-up: "
                f"{total} s in 1 turn (pump {pump}). The rain does the rest.")
        return {"kind": "turns", "run": True, "zone_id": zone_id, "pump": pump, "soil": soil, "target": target,
                "total_s": total, "turns": [total], "rest_s": REST_S, "text": text}

    gap = max(0.0, target - soil)
    cf, sf = CROP_FACTOR[_key(crop, CROP_FACTOR)], STAGE_FACTOR[stage]
    wf, wnotes = 1.0, []
    if temperature_c is not None and temperature_c >= 32:
        wf *= 1.2
        wnotes.append(f"hot day {temperature_c:.0f} C x1.2")
    elif temperature_c is not None and temperature_c <= 18:
        wf *= 0.85
        wnotes.append(f"cool day {temperature_c:.0f} C x0.85")
    if humidity_pct is not None and humidity_pct <= 30:
        wf *= 1.1
        wnotes.append(f"dry air {humidity_pct:.0f}% x1.1")
    elif humidity_pct is not None and humidity_pct >= 85:
        wf *= 0.9
        wnotes.append(f"humid air {humidity_pct:.0f}% x0.9")
    raw = gap * cf * sf * wf
    total = int(round(raw))
    total, hnote, _skip = watering._health_adjust(total, soil, plant_health)
    total = max(MIN_TOTAL_S, min(MAX_TOTAL_S, int(total)))
    turns = split_turns(total)

    how = (f"{gap:.0f}% gap to {target}% x {cf:g} ({crop}) x {sf:g} ({stage})"
           + ("".join(f" x [{n}]" for n in wnotes) if wnotes else "") + f" = about {raw:.0f} s")
    if hnote:
        how += f"; plant health: {hnote.lstrip('; ')}"
    if total != int(round(raw)) and not hnote:
        how += f"; limited to {total} s (allowed {MIN_TOTAL_S}-{MAX_TOTAL_S} s)"
    if len(turns) == 1:
        turn_text = f"{total} s in 1 turn"
    else:
        turn_text = (f"{total} s in {len(turns)} turns of " + ", ".join(f"{t} s" for t in turns) +
                     f" with {REST_S} s rest between turns (the soil is checked after each turn and watering stops early at {target}%)")
    text = f"Zone {zone_id} ({label}) is at {soil:.0f}%, comfortable level is {target}%. Water needed: {how}. Plan: pump {pump} waters {turn_text}."
    return {"kind": "turns", "run": True, "zone_id": zone_id, "pump": pump, "soil": soil, "target": target,
            "total_s": total, "turns": turns, "rest_s": REST_S, "text": text}


def run_turns(plan, io):
    """Executes a 'turns' plan. Same Io object as irrigation_plan.run_plan; returns {'outcome','soil','summary'}."""
    import irrigation_plan as ip
    pump, zone, target, turns = plan["pump"], plan["zone_id"], plan["target"], plan["turns"]
    say = io.say
    say(plan["text"], ai_phase="planned", ai_target_percent=target, ai_turns=len(turns))
    io.hold(True)
    try:
        soil = None
        for i, secs in enumerate(turns, 1):
            tank = io.tank()
            if tank is None or tank < ip.TANK_MIN_PCT:
                return ip._end(say, "tank_low", soil, f"The tank is too low to continue, so zone {zone} watering stopped before turn {i}.")
            io.hold(True)                      # refresh the "stay at the zone" request (the rover releases it after 5 min)
            say(f"Turn {i} of {len(turns)}: pump {pump} on for {secs} s.", ai_phase="turn")
            reply = io.water(pump, secs)
            if not reply.get("accepted"):
                why = reply.get("reason", "unknown")
                return ip._end(say, "refused", soil, f"Zone {zone}: the station refused ({ip.REFUSAL_TEXT.get(why, why)}), so watering stopped at turn {i}.")
            io.sleep(secs)
            if i == len(turns):
                break
            say(f"Turn {i} done. Resting {plan['rest_s']} s, then checking the soil.", ai_phase="wait")
            io.sleep(plan["rest_s"])
            soil = io.measure_soil()
            if soil is not None and soil >= target:
                return ip._end(say, "reached", soil, f"Zone {zone} reached {soil:.0f}% (comfortable level {target}%) after turn {i}, so the remaining turns are skipped.")
        return ip._end(say, "done", soil, f"Zone {zone}: all {len(turns)} turn(s) done ({plan['total_s']} s of water).")
    finally:
        io.hold(False)
