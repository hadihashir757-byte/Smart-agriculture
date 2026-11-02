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

from flask import Blueprint, jsonify, request

import db
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

    crop_type = str(payload.get("crop_type") or DEFAULT_CROP).lower()

    # Latest photo result (ignored if too old). Only the growth stage influences the
    # watering thresholds; disease/health is reported, never used to water more.
    health = db.latest_accepted_plant_health(max_age_s=watering.PLANT_HEALTH_MAX_AGE_S)
    plant_stage = (health["growth_stage"] if health and health["growth_stage"] else "mature")
    health_summary = ({"label": health["predicted_class"], "confidence": health["confidence"],
                       "growth_stage": health["growth_stage"]} if health else None)

    try:
        rain_mm = get_weather_forecast()["rain_mm"]
    except Exception as e:  # noqa: BLE001
        print(f"[zone-event] weather unavailable, assuming no rain forecast: {e}")
        rain_mm = 0.0

    # Tank level: ask the station itself when ThingsBoard is configured; else use what was sent.
    tank_percent, tank_note = None, ""
    if thingsboard.configured():
        try:
            tank_percent = thingsboard.latest_telemetry(thingsboard.STATION_DEVICE_NAME,
                                                         ["tank_level_percent"]).get("tank_level_percent")
        except Exception as e:  # noqa: BLE001
            tank_note = f"could not read tank level from ThingsBoard ({e})"
    if tank_percent is None:
        tank_percent = payload.get("tank_level_percent")
    if tank_percent is None:
        tank_percent, tank_note = 100, tank_note or "tank level unknown - station will enforce it"

    result = watering.decide(
        zone_id, readings, crop_type=crop_type, plant_stage=plant_stage, rain_forecast_mm=rain_mm,
        tank_percent=float(tank_percent), last_water_ts=db.last_watering_ts(zone_id),
        plant_health=health_summary,
    )

    command_id = result["command"]["params"]["command_id"] if result["command"] else None
    row_id = db.insert_watering_decision(
        zone_id, result["pump"], result["seconds"], result["decision"], result["reason"], command_id,
        "skipped" if result["decision"] == "skip" else ("recommended" if not AUTO_WATER else "pending"),
        result["inputs"],
    )

    status, station_reply = ("skipped" if result["decision"] == "skip" else "dry_run"), None
    if result["decision"] == "water" and AUTO_WATER:
        if not thingsboard.configured():
            status = "send_failed"
            result["reason"] += " | AUTO_WATER is on but TB_USERNAME/TB_PASSWORD are not set"
        else:
            try:
                station_reply = thingsboard.send_rpc(thingsboard.STATION_DEVICE_NAME,
                                                     result["command"]["method"], result["command"]["params"])
                status = "sent_accepted" if station_reply.get("accepted") else "sent_rejected"
            except Exception as e:  # noqa: BLE001
                status = "send_failed"
                result["reason"] += f" | could not reach the station: {e}"
    if result["decision"] == "water" or status != "skipped":
        db.update_watering_decision(row_id, status, station_reply)

    thingsboard.push_ai_telemetry({
        "ai_zone_id": zone_id, "ai_decision": result["decision"], "ai_seconds": result["seconds"],
        "ai_reason": result["reason"], "ai_status": status,
        "ai_plant_label": health_summary["label"] if health_summary else None,
    })

    return jsonify({
        "decision": result["decision"], "zone_id": zone_id, "pump": result["pump"],
        "seconds": result["seconds"], "reason": result["reason"], "status": status,
        "auto_water": AUTO_WATER, "station_reply": station_reply, "tank_note": tank_note or None,
        "plant_health": health_summary,
    })


@bp.route("/decisions", methods=["GET"])
@require_api_key
def decisions():
    limit = min(int(request.args.get("limit", 50)), 500)
    return jsonify(db.recent_watering_decisions(limit))
