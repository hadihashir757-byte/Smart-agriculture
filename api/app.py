"""
Smart Agriculture API server.

Endpoints (all except /health require header:  X-API-Key: <API_KEY>):
  POST /sensor-data       <- Arduino posts current readings
  GET  /get-recommendation -> Arduino polls this for the irrigation decision
  POST /upload-image      <- Android posts a plant photo for health diagnosis
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

import os
import sys
from functools import wraps

from flask import Flask, jsonify, request
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa: E402
import plant_health  # noqa: E402
from image_quality import check_quality  # noqa: E402
from irrigation_rules import get_irrigation_recommendation  # noqa: E402
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

    recommendation = get_irrigation_recommendation(
        soil_pct=latest["soil_pct"],
        temperature_c=latest["temperature_c"],
        humidity_pct=latest["humidity_pct"],
        rain_forecast_mm=rain_forecast_mm,
        crop_type=latest["crop_type"] or "tomato",
        plant_stage=latest["plant_stage"] or "mature",
    )

    db.insert_irrigation_log(
        water=recommendation["water"],
        duration_sec=recommendation["duration_sec"],
        reason=recommendation["reason"],
    )

    plant_status = db.latest_accepted_plant_health()

    return jsonify({
        "irrigation": recommendation,
        "rain_forecast_mm": rain_forecast_mm,
        "plant_health": {
            "label": plant_health.prettify_label(plant_status["predicted_class"]),
            "confidence": plant_status["confidence"],
        } if plant_status else None,
    })


@app.route("/plant-health-status", methods=["GET"])
@require_api_key
def plant_health_status():
    """Minimal endpoint for the Arduino: just the latest plant-health result,
    independent of any sensor data. Irrigation stays fully local on the
    device - this is the only thing the device needs to pull from the server."""
    plant_status = db.latest_accepted_plant_health()
    if plant_status is None:
        return jsonify({"label": "unknown", "confidence": None})
    return jsonify({
        "label": plant_health.prettify_label(plant_status["predicted_class"]),
        "confidence": plant_status["confidence"],
    })


@app.route("/upload-image", methods=["POST"])
@require_api_key
def upload_image():
    if "image" not in request.files:
        return jsonify({"error": "expected multipart form field 'image'"}), 400

    file = request.files["image"]
    try:
        img = Image.open(file.stream).convert("RGB")
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

    result = plant_health.predict(img)

    is_confident = result["label"] != "uncertain"
    db.insert_plant_health_prediction(
        predicted_class=result["raw_class"] or "uncertain",
        confidence=result["confidence"],
        accepted=is_confident,
        reject_reason=None if is_confident else "low confidence",
    )

    return jsonify({
        "accepted": is_confident,
        "prediction": result,
        "metrics": metrics,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
