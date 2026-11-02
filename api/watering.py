"""Turns the AI/rule recommendation into a SAFE, station-ready watering command.

The station (irrigation_station_r4.ino) is always the final authority and will
refuse unsafe commands. This module is the *first* safety layer, so most bad
requests never even leave the server:

  * only known zones, mapped to a pump number (ZONE_PUMP_MAP)
  * stale / missing soil reading  -> no watering
  * rain detected                  -> no watering
  * per-zone cooldown              -> no watering
  * seconds clamped to [MIN_SECONDS, MAX_SECONDS]
  * every command gets a unique command_id and an issued_at (epoch seconds)

decide() is a pure function (no network, no database) so it is easy to test.
"""

import os
import time
import uuid

from irrigation_rules import get_irrigation_recommendation

MIN_SECONDS = int(os.environ.get("WATER_MIN_SECONDS", "5"))
MAX_SECONDS = int(os.environ.get("WATER_MAX_SECONDS", "30"))          # station has its own, lower or equal, limit
COOLDOWN_S = int(os.environ.get("WATER_COOLDOWN_S", "1800"))          # 30 min between waterings of one zone
MAX_READING_AGE_S = int(os.environ.get("MAX_READING_AGE_S", "900"))   # ignore readings older than 15 min
PLANT_HEALTH_MAX_AGE_S = int(os.environ.get("PLANT_HEALTH_MAX_AGE_S", "7200"))  # photo result goes stale after 2 h


def parse_zone_pump_map(text=None):
    """'1:1,2:2' -> {1: 1, 2: 2}  (zone_id : pump number)."""
    text = text if text is not None else os.environ.get("ZONE_PUMP_MAP", "1:1,2:2")
    mapping = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        zone, pump = part.split(":")
        mapping[int(zone)] = int(pump)
    return mapping


def _skip(zone_id, reason, inputs):
    return {"decision": "skip", "zone_id": zone_id, "pump": None, "seconds": 0,
            "reason": reason, "command": None, "inputs": inputs}


FUNGAL_WORDS = ("blight", "spot", "mold", "mould", "mildew", "rust", "sigatoka", "scab", "rot", "fungus", "fungal", "cordana", "pestalotiopsis")
NEUTRAL_WORDS = ("mite", "virus", "mosaic", "curl")   # not water-related: no change
OVERWATER_WORDS = ("overwater", "waterlog", "over water")
STRESS_WORDS = ("wilt", "drought", "water stress", "dry", "underwater")
HEALTH_MIN_CONFIDENCE = float(os.environ.get("HEALTH_MIN_CONFIDENCE", "0.6"))


def _health_adjust(seconds, soil, plant_health):
    """Returns (seconds, note, skip_reason). Only confident, fresh photo results act."""
    if not plant_health:
        return seconds, "", None
    label = str(plant_health.get("label") or "").split("___")[-1].replace("_", " ").strip().lower()
    conf = plant_health.get("confidence") or 0
    if not label or label in ("healthy", "uncertain") or conf < HEALTH_MIN_CONFIDENCE:
        return seconds, "", None
    if any(w in label for w in NEUTRAL_WORDS):
        return seconds, "", None
    if any(w in label for w in OVERWATER_WORDS):
        return 0, "", f"plant looks overwatered ({label}) - not watering"
    if any(w in label for w in FUNGAL_WORDS):
        if soil < 20:   # truly dry: still water, but gently and never more than 15 s
            return min(seconds, 15), f"; disease ({label}) so kept gentle", None
        return 0, "", f"plant disease ({label}) - wet soil/leaves make it worse, not watering"
    if any(w in label for w in STRESS_WORDS):
        return seconds + 5, f"; +5 s for water stress ({label})", None
    return seconds, "", None


def decide(zone_id, readings, crop_type="tomato", plant_stage="mature", rain_forecast_mm=0.0,
           tank_percent=100, last_water_ts=None, plant_health=None, now=None, zone_pump_map=None):
    """
    readings: dict with soil_moisture_percent, temperature_c, humidity_percent,
              rain_detected (bool), reading_ts (epoch seconds, optional)
    Returns a dict: decision ('water'|'skip'), pump, seconds, reason, command, inputs
    """
    now = time.time() if now is None else now
    zone_pump_map = zone_pump_map if zone_pump_map is not None else parse_zone_pump_map()
    inputs = {"readings": readings, "crop_type": crop_type, "plant_stage": plant_stage,
              "rain_forecast_mm": rain_forecast_mm, "tank_percent": tank_percent,
              "plant_health": plant_health}

    if tank_percent is None:
        return _skip(zone_id, "tank level unknown - not watering", inputs)

    if zone_id not in zone_pump_map:
        return _skip(zone_id, f"zone {zone_id} is not mapped to a pump", inputs)
    pump = zone_pump_map[zone_id]

    soil = readings.get("soil_moisture_percent")
    if soil is None:
        return _skip(zone_id, "no soil moisture reading for this zone - not watering", inputs)

    ts = readings.get("reading_ts")
    if ts is not None and (now - ts) > MAX_READING_AGE_S:
        return _skip(zone_id, "soil reading is too old - not watering", inputs)

    if readings.get("rain_detected"):
        return _skip(zone_id, "rain detected - not watering", inputs)

    if last_water_ts is not None and (now - last_water_ts) < COOLDOWN_S:
        wait_min = int((COOLDOWN_S - (now - last_water_ts)) / 60) + 1
        return _skip(zone_id, f"zone was watered recently - cooldown, about {wait_min} min left", inputs)

    rec = get_irrigation_recommendation(
        soil_pct=soil,
        temperature_c=readings.get("temperature_c") if readings.get("temperature_c") is not None else 25,
        humidity_pct=readings.get("humidity_percent") if readings.get("humidity_percent") is not None else 60,
        rain_forecast_mm=rain_forecast_mm,
        crop_type=crop_type,
        plant_stage=plant_stage,
        water_pct=tank_percent,
    )
    if not rec["water"]:
        return _skip(zone_id, rec["reason"], inputs)

    seconds, note, skip_reason = _health_adjust(int(rec["duration_sec"]), soil, plant_health)
    if skip_reason:
        return _skip(zone_id, skip_reason, inputs)
    rec["reason"] += note
    seconds = max(MIN_SECONDS, min(MAX_SECONDS, seconds))
    command_id = "ai_" + uuid.uuid4().hex[:12]
    command = {"method": "water",
               "params": {"pump": pump, "seconds": seconds,
                          "command_id": command_id, "issued_at": int(now)}}
    return {"decision": "water", "zone_id": zone_id, "pump": pump, "seconds": seconds,
            "reason": rec["reason"], "command": command, "inputs": inputs}
