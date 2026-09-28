"""
Rule-based irrigation decision engine.

This is v1 of the Irrigation AI mentioned in the project diagram: no training
data exists yet (the hardware just went live), so we start with an explicit,
explainable rule set instead of a model. Once a few weeks of real
sensor + weather + outcome data has been logged (see sensor_data table in
app.py), this function's inputs/outputs can be reused as the label-generation
step for training a real model (XGBoost) without changing the Flask route.

Decision priority (checked in this order - first match wins):
  1. Water tank too low          -> never irrigate, no exceptions
  2. Soil at/above the stop point -> skip, already wet enough
  3. Soil critically dry          -> irrigate regardless of forecast
  4. Rain incoming                -> skip, it's about to be watered for free
  5. Soil below the crop/stage's start point -> irrigate
  6. Otherwise                    -> skip, moisture is in a fine range already
"""

# Below this water tank %, never irrigate - matches the Arduino's own
# LOW_WATER_THRESHOLD_PCT safety cutoff, so the "AI" recommendation and the
# device's local safety check agree instead of contradicting each other.
LOW_WATER_TANK_PCT = 15

# At or above this soil moisture %, stop/skip watering - a single clear
# ceiling regardless of crop or growth stage.
STOP_SOIL_PCT = 80

# Below this soil %, water regardless of forecast - the plant can't wait.
CRITICAL_SOIL_PCT = 20

# Below this soil % (and above CRITICAL_SOIL_PCT), start watering if no rain
# is coming. Seedlings have shallow roots and dry out faster, so they get a
# higher (earlier) start point than mature plants.
CROP_START_THRESHOLDS = {
    "tomato":  {"seedling": 40, "mature": 30},
    "pepper":  {"seedling": 35, "mature": 28},
    "default": {"seedling": 35, "mature": 30},
}

# If forecast rain (mm) in the next few hours is at or above this, skip
# watering even if soil is dry - it's about to be watered for free.
RAIN_SKIP_THRESHOLD_MM = 5.0


def get_irrigation_recommendation(soil_pct, temperature_c, humidity_pct,
                                   rain_forecast_mm, crop_type, plant_stage,
                                   water_pct=100):
    """
    Returns a dict: {"water": bool, "duration_sec": int, "reason": str}

    soil_pct           - current soil moisture, 0-100
    temperature_c       - current temperature
    humidity_pct         - current humidity
    rain_forecast_mm    - forecast rainfall in the next ~6h, from weather API
    crop_type           - e.g. "tomato" (must match a key in CROP_START_THRESHOLDS,
                           falls back to "default")
    plant_stage         - "seedling" or "mature"
    water_pct           - current water tank level, 0-100 (default 100 =
                           assume full if not provided, so callers that don't
                           track tank level don't accidentally get blocked)
    """
    if water_pct < LOW_WATER_TANK_PCT:
        return {
            "water": False,
            "duration_sec": 0,
            "reason": f"Water tank too low ({water_pct}% < {LOW_WATER_TANK_PCT}%) - cannot irrigate",
        }

    if soil_pct >= STOP_SOIL_PCT:
        return {
            "water": False,
            "duration_sec": 0,
            "reason": f"Soil at or above stop point ({soil_pct}% >= {STOP_SOIL_PCT}%) - no watering needed",
        }

    if soil_pct < CRITICAL_SOIL_PCT:
        return {
            "water": True,
            "duration_sec": 45,
            "reason": f"Soil critically dry ({soil_pct}% < {CRITICAL_SOIL_PCT}%) - watering regardless of forecast",
        }

    if rain_forecast_mm >= RAIN_SKIP_THRESHOLD_MM:
        return {
            "water": False,
            "duration_sec": 0,
            "reason": f"Rain forecast ({rain_forecast_mm}mm) covers watering need - skipping",
        }

    thresholds = CROP_START_THRESHOLDS.get(crop_type.lower(), CROP_START_THRESHOLDS["default"])
    start_pct = thresholds.get(plant_stage.lower(), thresholds["mature"])

    if soil_pct >= start_pct:
        return {
            "water": False,
            "duration_sec": 0,
            "reason": f"Soil moisture adequate for {crop_type}/{plant_stage} ({soil_pct}% >= {start_pct}%)",
        }

    # Below the start threshold, not critical, no rain coming - water,
    # scaled by how far below start we are and how hot/dry the air is.
    deficit = start_pct - soil_pct
    duration = 20 + deficit  # seconds, simple linear scale
    if temperature_c >= 32 or humidity_pct <= 30:
        duration += 10  # hot/dry air dries soil faster - water a bit longer

    return {
        "water": True,
        "duration_sec": int(duration),
        "reason": f"Soil below start point for {crop_type}/{plant_stage} ({soil_pct}% < {start_pct}%), no rain forecast",
    }
