"""One place that turns a photo into a plant-health result.

Used by the /upload-image route (phone uploads) and by the background agent
(server fetches from the camera). Steps: quality gate -> growth stage ->
disease model -> save -> show on the ThingsBoard dashboard.
"""

import base64
import io
import os
import time

import numpy as np

import db
import crops
import plant_health
import thingsboard
from crop_growth_tips import get_growth_tip  # noqa: F401  (kept for parity with app.py imports)
from growth_stage import plant_pixel_ratio, estimate_growth_stage
from image_quality import check_quality
from plant_care_tips import get_care_tip

THUMB_PX = int(os.environ.get("PHOTO_THUMB_PX", "240"))          # longest side of the dashboard thumbnail
THUMB_QUALITY = int(os.environ.get("PHOTO_THUMB_QUALITY", "55"))
QUALITY_GATE_ENABLED = os.environ.get("QUALITY_GATE_ENABLED", "true").lower() == "true"


def make_thumbnail(img):
    """Small JPEG as a data URL (a few KB) so the photo can travel to ThingsBoard as telemetry."""
    t = img.convert("RGB").copy()
    t.thumbnail((THUMB_PX, THUMB_PX))
    buf = io.BytesIO()
    t.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def process_photo(img, zone_id=None, crop_type=None):
    """Runs the pipeline, then shows the photo on the ThingsBoard dashboard (best-effort, never raises).
    A photo without a zone_id (sent by hand) goes to the 'custom photo' card, zone photos to their zone's card."""
    result = _process(img, zone_id, crop_type)
    try:
        note = "" if result.get("accepted") else str(result.get("reason") or "")[:60]
        crop = crops.crop_for_zone(zone_id) if zone_id is not None else None
        thingsboard.push_ai_telemetry(thingsboard.photo_telemetry(make_thumbnail(img), zone_id, crop, time.time(), note))
    except Exception as e:  # noqa: BLE001
        print(f"[photo] could not show the photo on the dashboard: {e}")
    return result


def _process(img, zone_id=None, crop_type=None):
    """img: a PIL image. Returns the same JSON-able dict the /upload-image route always returned.
    zone_id: the zone the photo shows - its crop, growth baseline and health result are kept per zone.
    crop_type: overrides the crop (otherwise ZONE_CROP_MAP / the latest reading / DEFAULT_CROP)."""
    img = img.convert("RGB")
    rgb_array = np.array(img)

    ok, reason, metrics = check_quality(rgb_array)
    if QUALITY_GATE_ENABLED and not ok:
        db.insert_plant_health_prediction(None, None, accepted=False, reject_reason=reason, zone_id=zone_id)
        return {"accepted": False, "reason": reason, "metrics": metrics}

    current_ratio = plant_pixel_ratio(rgb_array)
    baseline = db.get_growth_baseline(zone_id)
    if baseline is None:
        db.set_growth_baseline(current_ratio, zone_id)
        stage = "seedling"      # first accepted photo defines the baseline
    else:
        stage = estimate_growth_stage(current_ratio, baseline["baseline_ratio"])

    if crop_type:
        crop_type = str(crop_type).lower()
    elif zone_id is not None:
        crop_type = crops.crop_for_zone(zone_id)
    else:
        latest_reading = db.latest_sensor_reading()
        crop_type = ((latest_reading["crop_type"] if latest_reading and latest_reading["crop_type"]
                      else os.environ.get("DEFAULT_CROP", "tomato")).lower())

    if not plant_health.supported_crop(crop_type):
        db.insert_plant_health_prediction(
            predicted_class=None, confidence=None, accepted=False,
            reject_reason=f"disease detection not available for crop_type={crop_type}", growth_stage=stage,
            zone_id=zone_id)
        return {"accepted": False,
                "reason": f"Disease detection isn't available for '{crop_type}' yet - "
                          f"supported crops: {', '.join(sorted(plant_health.MODEL_PATHS))}.",
                "growth_stage": stage, "metrics": metrics}

    result = plant_health.predict(img, crop_type=crop_type)
    is_confident = result["label"] != "uncertain"
    db.insert_plant_health_prediction(
        predicted_class=result["raw_class"] or "uncertain", confidence=result["confidence"],
        accepted=is_confident, reject_reason=None if is_confident else "low confidence",
        growth_stage=stage, top_predictions=result["top_predictions"], zone_id=zone_id)

    if is_confident:    # show on the ThingsBoard dashboard (best-effort, never raises)
        thingsboard.push_ai_telemetry(thingsboard.plant_telemetry(
            result["raw_class"], result["confidence"], stage,
            get_care_tip(result["raw_class"]), plant_health.prettify_label(result["raw_class"])))

    return {"accepted": is_confident, "prediction": result, "growth_stage": stage, "metrics": metrics}
