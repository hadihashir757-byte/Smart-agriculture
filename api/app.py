"""
Smart Agriculture API server.

Endpoints (all except /health require header:  X-API-Key: <API_KEY>):
  POST /sensor-data       <- Arduino posts current readings
  GET  /get-recommendation -> Arduino polls this for the irrigation decision
  POST /upload-image      <- Android posts a plant photo for health diagnosis
  POST /demo-mode         <- Arduino posts {"enabled": bool} when the Demo Mode
                             switch changes; pushes an instant notification to
                             MacroDroid (best-effort) and updates stored state
  GET  /demo-mode-status  -> MacroDroid's fallback poll - always the source of
                             truth, in case the push above is ever missed
  GET  /health            <- no auth, for host uptime checks

Run locally:
    cd smart-agriculture
    source venv/bin/activate
    export API_KEY=dev-secret-change-me
    python3 api/app.py

Deploy: point your host's start command at `gunicorn app:app` (add gunicorn
to requirements.txt for production) from inside the api/ directory, or just
`python3 app.py` for a quick free-tier deploy - see README notes below.
"""

import json
import os
import sys
from functools import wraps

from flask import Flask, jsonify, request
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa: E402
import plant_health  # noqa: E402
from growth_stage import plant_pixel_ratio, estimate_growth_stage  # noqa: E402
from image_quality import check_quality  # noqa: E402
from crop_growth_tips import get_growth_tip  # noqa: E402
from irrigation_rules import get_irrigation_recommendation  # noqa: E402
from macrodroid import notify_macrodroid  # noqa: E402
from plant_care_tips import get_care_tip  # noqa: E402
from weather import get_rain_forecast_mm  # noqa: E402

app = Flask(__name__)
db.init_db()

API_KEY = os.environ.get("API_KEY", "dev-secret-change-me")
if API_KEY == "dev-secret-change-me":
    print("!! WARNING: API_KEY not set in environment - using an insecure default. "
          "Set API_KEY before deploying anywhere public. !!")


def require_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-API-Key")
        if provided != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sensor-data", methods=["POST"])
@require_api_key
def sensor_data():
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "expected JSON body"}), 400

    required = ["temperature_c", "humidity_pct", "soil_pct", "light_pct", "rain_pct", "water_pct"]
    missing = [k for k in required if k not in payload]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    db.insert_sensor_reading(payload)
    return jsonify({"status": "stored"})


@app.route("/get-recommendation", methods=["GET"])
@require_api_key
def get_recommendation():
    latest = db.latest_sensor_reading()
    if latest is None:
        return jsonify({"error": "no sensor data received yet"}), 404

    rain_forecast_mm = get_rain_forecast_mm()
    plant_status = db.latest_accepted_plant_health()
    # Prefer the image-derived growth stage (see growth_stage.py) over the
    # sensor payload's plant_stage field, which is just a static default -
    # this is the one place that's actually been observed from a photo.
    plant_stage = (plant_status["growth_stage"] if plant_status and plant_status["growth_stage"]
                   else latest["plant_stage"] or "mature")

    recommendation = get_irrigation_recommendation(
        soil_pct=latest["soil_pct"],
        temperature_c=latest["temperature_c"],
        humidity_pct=latest["humidity_pct"],
        rain_forecast_mm=rain_forecast_mm,
        crop_type=latest["crop_type"] or "tomato",
        plant_stage=plant_stage,
        water_pct=latest["water_pct"] if latest["water_pct"] is not None else 100,
    )

    db.insert_irrigation_log(
        water=recommendation["water"],
        duration_sec=recommendation["duration_sec"],
        reason=recommendation["reason"],
    )

    return jsonify({
        "irrigation": recommendation,
        "rain_forecast_mm": rain_forecast_mm,
        "growth_tip": get_growth_tip(latest["crop_type"] or "tomato"),
        "plant_health": {
            "label": plant_health.prettify_label(plant_status["predicted_class"]),
            "confidence": plant_status["confidence"],
            "growth_stage": plant_stage,
            "care_tip": get_care_tip(plant_status["predicted_class"]),
        } if plant_status else None,
    })


@app.route("/plant-health-status", methods=["GET"])
@require_api_key
def plant_health_status():
    """Minimal endpoint for the Arduino: just the latest plant-health result,
    independent of any sensor data. Irrigation stays fully local on the
    device - this is the only thing the device needs to pull from the server."""
    latest_reading = db.latest_sensor_reading()
    crop_type = (latest_reading["crop_type"] if latest_reading and latest_reading["crop_type"] else "tomato")
    growth_tip = get_growth_tip(crop_type)

    plant_status = db.latest_accepted_plant_health()
    if plant_status is None:
        return jsonify({"label": "unknown", "confidence": None, "growth_stage": "unknown",
                         "possible_diseases": [], "care_tip": None, "growth_tip": growth_tip})
    return jsonify({
        "label": plant_health.prettify_label(plant_status["predicted_class"]),
        "confidence": plant_status["confidence"],
        "growth_stage": plant_status["growth_stage"] or "unknown",
        "possible_diseases": json.loads(plant_status["top_predictions"]) if plant_status["top_predictions"] else [],
        "care_tip": get_care_tip(plant_status["predicted_class"]),
        "growth_tip": growth_tip,
    })


@app.route("/demo-mode", methods=["POST"])
@require_api_key
def demo_mode():
    payload = request.get_json(force=True, silent=True) or {}
    enabled = bool(payload.get("enabled", False))

    db.set_demo_mode(enabled)
    notify_macrodroid(enabled)  # best-effort push - see macrodroid.py

    return jsonify({"status": "stored", "demo_mode": enabled})


@app.route("/demo-mode-status", methods=["GET"])
@require_api_key
def demo_mode_status():
    """MacroDroid's fallback poll - always reflects the real stored state,
    regardless of whether the push notification above ever arrived."""
    return jsonify({"demo_mode": db.get_demo_mode()})


@app.route("/upload-image", methods=["POST"])
@require_api_key
def upload_image():
    # Accepts two ways of sending the photo, since not every HTTP client
    # (MacroDroid's HTTP Request action, for one) supports proper multipart
    # form uploads:
    #   1. multipart/form-data with the file under field name 'image'
    #      (what curl -F, Postman, etc. send)
    #   2. A raw image body - just the file's bytes as the entire POST body,
    #      no wrapping at all (what MacroDroid's "File" content source sends)
    import io

    if "image" in request.files:
        image_bytes = request.files["image"].stream.read()
    else:
        image_bytes = request.get_data()

    if not image_bytes:
        return jsonify({"error": "no image data received - send it as multipart field "
                                  "'image' or as the raw request body"}), 400

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return jsonify({"error": "could not decode image"}), 400

    import numpy as np
    rgb_array = np.array(img)

    ok, reason, metrics = check_quality(rgb_array)
    if not ok:
        db.insert_plant_health_prediction(None, None, accepted=False, reject_reason=reason)
        return jsonify({
            "accepted": False,
            "reason": reason,
            "metrics": metrics,
        }), 200  # not a server error - just a rejected photo, retake and resend

    # Growth stage: not a trained classifier (no labeled dataset exists for
    # this), but a CV heuristic - see growth_stage.py for why this works
    # specifically because the camera stays in a fixed position. Crop-agnostic,
    # so this runs regardless of which crop is currently selected.
    current_ratio = plant_pixel_ratio(rgb_array)
    baseline = db.get_growth_baseline()
    if baseline is None:
        db.set_growth_baseline(current_ratio)
        stage = "seedling"  # first-ever accepted photo defines the baseline
    else:
        stage = estimate_growth_stage(current_ratio, baseline["baseline_ratio"])

    # Disease detection, unlike growth stage, IS crop-specific - the model was
    # only ever trained on tomato (PlantVillage's tomato subset). Running it
    # against a different crop (e.g. amaranth, selected via V12) wouldn't fail
    # cleanly - it would force-fit the photo into one of the 10 tomato disease
    # classes and hand back a confident, completely wrong diagnosis and care
    # tip. Better to say "not supported" than to guess wrong.
    latest_reading = db.latest_sensor_reading()
    crop_type = ((latest_reading["crop_type"] if latest_reading and latest_reading["crop_type"] else "tomato")
                 .lower())

    if crop_type != "tomato":
        db.insert_plant_health_prediction(
            predicted_class=None,
            confidence=None,
            accepted=False,
            reject_reason=f"disease detection not available for crop_type={crop_type}",
            growth_stage=stage,
        )
        return jsonify({
            "accepted": False,
            "reason": f"Disease detection is currently only trained for tomato - "
                      f"'{crop_type}' isn't supported yet.",
            "growth_stage": stage,
            "metrics": metrics,
        })

    result = plant_health.predict(img)

    is_confident = result["label"] != "uncertain"
    db.insert_plant_health_prediction(
        predicted_class=result["raw_class"] or "uncertain",
        confidence=result["confidence"],
        accepted=is_confident,
        reject_reason=None if is_confident else "low confidence",
        growth_stage=stage,
        top_predictions=result["top_predictions"],
    )

    return jsonify({
        "accepted": is_confident,
        "prediction": result,
        "growth_stage": stage,
        "metrics": metrics,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
