"""
Rule-based irrigation decision engine.

This is v1 of the Irrigation AI mentioned in the project diagram: no training
data exists yet (the hardware just went live), so we start with an explicit,
explainable rule set instead of a model. Once a few weeks of real
sensor + weather + outcome data has been logged (see sensor_data table in
app.py), this function's inputs/outputs can be reused as the label-generation
step for training a real model (XGBoost) without changing the Flask route.
"""

# Target soil moisture % per crop, at the "seedling" and "mature" growth
# stages. Extend this table as you add crops.
CROP_TARGETS = {
    "tomato":  {"seedling": 65, "mature": 55},
    "pepper":  {"seedling": 60, "mature": 50},
    "default": {"seedling": 60, "mature": 55},
}

# If forecast rain (mm) in the next few hours is at or above this, skip
# watering even if soil is dry - it's about to be watered for free.
RAIN_SKIP_THRESHOLD_MM = 5.0

# Below this soil %, water regardless of forecast - the plant can't wait.
CRITICAL_SOIL_PCT = 20


def get_irrigation_recommendation(soil_pct, temperature_c, humidity_pct,
                                   rain_forecast_mm, crop_type, plant_stage):
    """
    Returns a dict: {"water": bool, "duration_sec": int, "reason": str}

    soil_pct           - current soil moisture, 0-100
    temperature_c       - current temperature
    humidity_pct         - current humidity
    rain_forecast_mm    - forecast rainfall in the next ~6h, from weather API
    crop_type           - e.g. "tomato" (must match a key in CROP_TARGETS,
                           falls back to "default")
    plant_stage         - "seedling" or "mature"
    """
    targets = CROP_TARGETS.get(crop_type.lower(), CROP_TARGETS["default"])
    target_pct = targets.get(plant_stage.lower(), targets["mature"])

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

    if soil_pct >= target_pct:
        return {
            "water": False,
            "duration_sec": 0,
            "reason": f"Soil at or above target for {crop_type}/{plant_stage} ({soil_pct}% >= {target_pct}%)",
        }

    # Dry, but not critical, and no rain coming - water, scaled a bit by
    # how far below target we are and how hot/dry the air is.
    deficit = target_pct - soil_pct
    duration = 20 + deficit  # seconds, simple linear scale
    if temperature_c >= 32 or humidity_pct <= 30:
        duration += 10  # hot/dry air dries soil faster - water a bit longer

    return {
        "water": True,
        "duration_sec": int(duration),
        "reason": f"Soil below target for {crop_type}/{plant_stage} ({soil_pct}% < {target_pct}%), no rain forecast",
    }
