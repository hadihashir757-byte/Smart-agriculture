"""One place that turns a photo into a plant-health result.

Used by the /upload-image route (phone uploads) and by the background agent
(server fetches from the camera). Steps: quality gate -> growth stage ->
disease model -> save -> show on the ThingsBoard dashboard.
"""

import os

import numpy as np

import db
import plant_health
import thingsboard
from crop_growth_tips import get_growth_tip  # noqa: F401  (kept for parity with app.py imports)
from growth_stage import plant_pixel_ratio, estimate_growth_stage
from image_quality import check_quality
from plant_care_tips import get_care_tip

QUALITY_GATE_ENABLED = os.environ.get("QUALITY_GATE_ENABLED", "true").lower() == "true"


def process_photo(img):
    """img: a PIL image. Returns the same JSON-able dict the /upload-image route always returned."""
    img = img.convert("RGB")
    rgb_array = np.array(img)

    ok, reason, metrics = check_quality(rgb_array)
    if QUALITY_GATE_ENABLED and not ok:
        db.insert_plant_health_prediction(None, None, accepted=False, reject_reason=reason)
        return {"accepted": False, "reason": reason, "metrics": metrics}

    current_ratio = plant_pixel_ratio(rgb_array)
    baseline = db.get_growth_baseline()
    if baseline is None:
        db.set_growth_baseline(current_ratio)
        stage = "seedling"      # first accepted photo defines the baseline
    else:
        stage = estimate_growth_stage(current_ratio, baseline["baseline_ratio"])

    latest_reading = db.latest_sensor_reading()
    crop_type = ((latest_reading["crop_type"] if latest_reading and latest_reading["crop_type"]
                  else os.environ.get("DEFAULT_CROP", "tomato")).lower())

    if not plant_health.supported_crop(crop_type):
        db.insert_plant_health_prediction(
            predicted_class=None, confidence=None, accepted=False,
            reject_reason=f"disease detection not available for crop_type={crop_type}", growth_stage=stage)
        return {"accepted": False,
                "reason": f"Disease detection isn't available for '{crop_type}' yet - "
                          f"supported crops: {', '.join(sorted(plant_health.MODEL_PATHS))}.",
                "growth_stage": stage, "metrics": metrics}

    result = plant_health.predict(img, crop_type=crop_type)
    is_confident = result["label"] != "uncertain"
    db.insert_plant_health_prediction(
        predicted_class=result["raw_class"] or "uncertain", confidence=result["confidence"],
        accepted=is_confident, reject_reason=None if is_confident else "low confidence",
        growth_stage=stage, top_predictions=result["top_predictions"])

    if is_confident:    # show on the ThingsBoard dashboard (best-effort, never raises)
        thingsboard.push_ai_telemetry(thingsboard.plant_telemetry(
            result["raw_class"], result["confidence"], stage,
            get_care_tip(result["raw_class"]), plant_health.prettify_label(result["raw_class"])))

    return {"accepted": is_confident, "prediction": result, "growth_stage": stage, "metrics": metrics}
