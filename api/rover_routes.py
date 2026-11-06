"""Routes for the ESP32 rover + ThingsBoard + irrigation-station setup.

  POST /zone-event   rover (via a ThingsBoard rule chain, or any HTTP client) reports that it
                     reached a zone and measured it. The server decides whether to water,
                     applies the safety layer in watering.py, logs the decision, and - only if
                     AUTO_WATER=true - sends the `water` RPC to the station and records what
                     the station really answered.
  GET  /decisions    recent decisions, newest first (for checking / dashboards).

AUTO_WATER is OFF by default: the server only *recommends* (dry run) until you turn it on.
"""

import os
import time

from flask import Blueprint, jsonify, request

import crops
import db
import explain
import irrigation_plan
import irrigation_rules
import plan_runner
import water_amount
import plant_health as plant_health_mod
import thingsboard
import watering
from auth import require_api_key
from weather import get_weather_forecast

bp = Blueprint("rover", __name__)

AUTO_WATER = os.environ.get("AUTO_WATER", "false").lower() == "true"
DEFAULT_CROP = os.environ.get("DEFAULT_CROP", "tomato")


def _num(payload, key):
    v = payload.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")


def evaluate_zone(zone_id, readings, crop_type, tank_hint=None, advice_only=False, caveat=None):
    """Decide (and, if AUTO_WATER, act) for one zone. Used by /zone-event and by the background agent.
    tank_hint: a tank level supplied by the caller, used only if the station cannot be asked.
    advice_only: for the dashboard's "Zone 1 check" button. The decision is worked out as if the rover stands at the zone
    and shown on the dashboard, but nothing is ever sent to the station (AUTO_WATER is ignored). Because it is only advice,
    an old soil reading is still used (the caveat text says so), so the button always ends in a decision.
    caveat: a short warning put in front of the reason (for example "soil value is 3 h old")."""
    auto = AUTO_WATER and not advice_only
    # Latest photo result (ignored if too old). Only the growth stage influences the
    # watering thresholds; disease/health is reported, never used to water more.
    health = db.latest_accepted_plant_health(max_age_s=watering.PLANT_HEALTH_MAX_AGE_S, zone_id=zone_id)
    plant_stage = (health["growth_stage"] if health and health["growth_stage"] else "mature")
    health_summary = ({"label": health["predicted_class"], "confidence": health["confidence"],
                       "growth_stage": health["growth_stage"]} if health else None)

    try:
        rain_mm = get_weather_forecast()["rain_mm"]
    except Exception as e:  # noqa: BLE001
        print(f"[decide] weather unavailable, assuming no rain forecast: {e}")
        rain_mm = 0.0

    # Tank level: ask the station itself when ThingsBoard is configured; else use what was sent.
    tank_percent, tank_note, demo, rover = None, "", False, None
    if thingsboard.configured():
        try:
            st = thingsboard.latest_telemetry(thingsboard.STATION_DEVICE_NAME, ["tank_level_percent", "demo_mode", "rover_link_ok", "rover_at_zone", "rover_zone"])
            tank_percent = st.get("tank_level_percent")
            rover = explain.rover_state(st)
            demo = str(st.get("demo_mode")).strip().lower() in ("true", "1")
        except Exception as e:  # noqa: BLE001
            tank_note = f"could not read tank level from ThingsBoard ({e})"
    if tank_percent is None:
        tank_percent = tank_hint
    if tank_percent is None:
        if thingsboard.configured():
            tank_note = tank_note or "tank level not available"   # decide() will skip
        else:
            tank_percent, tank_note = 100, "tank level unknown (no ThingsBoard) - station will enforce it"

    if demo:
        return _demo_plan(zone_id, readings, rain_mm, tank_percent, tank_note, health_summary, plant_stage, rover, advice_only)

    decide_readings = {**readings, "reading_ts": None} if advice_only else readings    # advice: an old soil value is better than no decision
    result = watering.decide(
        zone_id, decide_readings, crop_type=crop_type, plant_stage=plant_stage, rain_forecast_mm=rain_mm,
        tank_percent=None if tank_percent is None else float(tank_percent), last_water_ts=db.last_watering_ts(zone_id),
        plant_health=health_summary,
    )

    if caveat:
        extra = {"base_reason": f"{caveat}. {result['base_reason']}"} if "base_reason" in result else {}
        result = {**result, "reason": f"{caveat}. {result['reason']}", **extra}

    # The station only waters while the rover is stopped AT this zone - say so here too (same rule as demo mode).
    block = None if advice_only else irrigation_plan.rover_block_text(zone_id, rover)
    if result["decision"] == "water" and block:
        result = {**result, "decision": "skip", "seconds": 0, "command": None, "reason": block}

    plan = None
    if result["decision"] == "water":
        soil = readings.get("soil_moisture_percent")
        rain_soon = rain_mm >= irrigation_rules.RAIN_SKIP_THRESHOLD_MM and soil >= irrigation_rules.CRITICAL_SOIL_PCT
        plan = water_amount.make_turn_plan(
            zone_id, result["pump"], soil, crop_type, plant_stage, readings.get("temperature_c"),
            readings.get("humidity_percent"), rain_soon, result["seconds"], result.get("base_reason", ""), health_summary)
        result = {**result, "seconds": plan["total_s"],
                  "reason": f"{result.get('base_reason', result['reason'])}. {plan['text']}"}

    row_id = db.insert_watering_decision(
        zone_id, result["pump"], result["seconds"], result["decision"], result["reason"], None,
        "skipped" if result["decision"] == "skip" else ("recommended" if not auto else "pending"),
        {**result["inputs"], "plan": {k: v for k, v in plan.items() if k != "text"} if plan else None},
    )

    status = "skipped" if result["decision"] == "skip" else "dry_run"
    if plan and auto:
        if not thingsboard.configured():
            status = "send_failed"
            result["reason"] += " | AUTO_WATER is on but TB_USERNAME/TB_PASSWORD are not set"
        else:
            status = "plan_ready"       # the agent (or the background runner below) executes the turns
    if result["decision"] == "water" or status != "skipped":
        db.update_watering_decision(row_id, status, None)

    checks = explain.checks_line(zone_id, readings.get("soil_moisture_percent"),
                                 None if tank_percent is None else float(tank_percent), rain_mm,
                                 bool(readings.get("rain_detected")), rover, _age(readings),
                                 plan["target"] if plan else None, demo=False)
    why = plan["text"] if plan else result["reason"]
    thingsboard.push_ai_telemetry({
        "ai_zone_id": zone_id, "ai_decision": result["decision"], "ai_seconds": result["seconds"],
        "ai_turns": len(plan["turns"]) if plan else 0,
        "ai_reason": result["reason"], "ai_explanation": why, "ai_status": status,
        "ai_target_percent": plan["target"] if plan else None,
        "ai_report": explain.report(checks, "water" if result["decision"] == "water" else "do not water", why),
        "ai_plant_label": plant_health_mod.prettify_label(health_summary["label"]) if health_summary else None,
        "ai_plant_stage": plant_stage,
    })

    return {
        "decision": result["decision"], "zone_id": zone_id, "pump": result["pump"],
        "seconds": result["seconds"], "turns": plan["turns"] if plan else [], "reason": result["reason"],
        "status": status, "auto_water": auto, "station_reply": None, "tank_note": tank_note or None,
        "plant_health": health_summary, "plan": plan,
    }




def _age(readings):
    ts = readings.get("reading_ts")
    return None if ts is None else max(0.0, time.time() - ts)


def _demo_plan(zone_id, readings, rain_mm, tank_percent, tank_note, health_summary, plant_stage, rover=None, advice_only=False):
    """Demo mode: build the pulse plan (see irrigation_plan.py). The background agent runs it."""
    pump = watering.parse_zone_pump_map().get(zone_id)
    if pump is None:
        plan = {"run": False, "zone_id": zone_id, "text": f"Zone {zone_id} is not mapped to a pump, so the AI is not watering."}
    else:
        plan = irrigation_plan.make_plan(zone_id, pump, readings.get("soil_moisture_percent"), rain_mm,
                                         bool(readings.get("rain_detected")),
                                         None if tank_percent is None else float(tank_percent), rover=rover)
    decision = "water" if plan["run"] else "skip"
    status = ("plan_ready" if AUTO_WATER and not advice_only else "dry_run") if plan["run"] else "skipped"
    db.insert_watering_decision(zone_id, pump, plan.get("pulse_s", 0), decision, plan["text"], None, status,
                                {"readings": readings, "rain_forecast_mm": rain_mm, "tank_percent": tank_percent,
                                 "demo": True, "plan": {k: v for k, v in plan.items() if k != "text"}})
    checks = explain.checks_line(zone_id, readings.get("soil_moisture_percent"),
                                 None if tank_percent is None else float(tank_percent), rain_mm,
                                 bool(readings.get("rain_detected")), rover, _age(readings), plan.get("target"), demo=True)
    thingsboard.push_ai_telemetry({
        "ai_zone_id": zone_id, "ai_decision": decision, "ai_seconds": plan.get("pulse_s", 0),
        "ai_reason": plan["text"], "ai_explanation": plan["text"], "ai_status": status,
        "ai_report": explain.report(checks, "water" if plan["run"] else "do not water", plan["text"]),
        "ai_target_percent": plan.get("target"),
        "ai_plant_label": plant_health_mod.prettify_label(health_summary["label"]) if health_summary else None,
        "ai_plant_stage": plant_stage,
    })
    return {"decision": decision, "zone_id": zone_id, "pump": pump, "seconds": plan.get("pulse_s", 0),
            "reason": plan["text"], "status": status, "auto_water": AUTO_WATER and not advice_only, "station_reply": None,
            "tank_note": tank_note or None, "plant_health": health_summary, "demo": True,
            "plan": plan if plan["run"] else None}


@bp.route("/zone-event", methods=["POST"])
@require_api_key
def zone_event():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object"}), 400

    try:
        zone_id = int(payload["zone_id"])
        readings = {
            "soil_moisture_percent": _num(payload, "soil_moisture_percent"),
            "temperature_c": _num(payload, "temperature_c"),
            "humidity_percent": _num(payload, "humidity_percent"),
            "light_percent": _num(payload, "light_percent"),
            "rain_detected": bool(payload.get("rain_detected", False)),
            "reading_ts": _num(payload, "reading_ts"),
        }
        for pct_key in ("soil_moisture_percent", "humidity_percent", "light_percent"):
            v = readings[pct_key]
            if v is not None and not 0 <= v <= 100:
                raise ValueError(f"{pct_key} must be between 0 and 100")
    except KeyError:
        return jsonify({"error": "zone_id is required"}), 400
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    crop_type = str(payload.get("crop_type") or crops.crop_for_zone(zone_id)).lower()
    result = evaluate_zone(zone_id, readings, crop_type, payload.get("tank_level_percent"))
    if result.get("plan") and result["auto_water"] and result["status"] == "plan_ready":
        result["status"] = "plan_started" if plan_runner.start_in_background(result["plan"]) else "plan_busy"
    return jsonify(result)


@bp.route("/decisions", methods=["GET"])
@require_api_key
def decisions():
    limit = min(int(request.args.get("limit", 50)), 500)
    return jsonify(db.recent_watering_decisions(limit))
