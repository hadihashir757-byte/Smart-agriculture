"""Plant Health inference: loads the trained model(s) once and exposes predict().

Two independent models now, one per crop with a trained disease classifier:
  - tomato: see train_plant_health.py (PlantVillage tomato subset, 96.5% test acc)
  - plantain: see train_plant_health_plantain.py (BananaLSD dataset, 95.2% test acc)
Both use the same hand-crafted color/texture features (feature_extraction.py)
and the same HistGradientBoostingClassifier approach - only the training data
differs. A crop with no trained model (anything not in MODEL_PATHS) simply
isn't supported - see /upload-image's crop_type check in app.py, which
declines rather than guessing with the wrong model.
"""

import os

import joblib
import numpy as np

from feature_extraction import extract_features
from plant_care_tips import get_care_tip

MODEL_PATHS = {
    "tomato": os.path.join(os.path.dirname(__file__), "..", "models", "plant_health_model.joblib"),
    "plantain": os.path.join(os.path.dirname(__file__), "..", "models", "plant_health_model_plantain.joblib"),
}

CONFIDENCE_THRESHOLD = 0.55  # below this, report "uncertain" instead of a guess

_model_bundles = {}  # crop_type -> loaded bundle, cached after first use


def supported_crop(crop_type):
    return (crop_type or "").lower() in MODEL_PATHS


def _load(crop_type):
    crop_type = crop_type.lower()
    if crop_type not in _model_bundles:
        path = MODEL_PATHS[crop_type]  # raises KeyError for an unsupported crop - caller should check supported_crop() first
        _model_bundles[crop_type] = joblib.load(path)
    return _model_bundles[crop_type]


def prettify_label(raw_label):
    # "Tomato___Early_blight" -> "Early blight". Plantain's classes have no
    # "___" prefix at all (e.g. "sigatoka"), so this just passes them through
    # unchanged aside from the underscore->space cleanup.
    name = raw_label.split("___", 1)[-1]
    return name.replace("_", " ").strip()


def predict(rgb_image_array_or_path, crop_type="tomato"):
    """
    Returns dict:
      {
        "label": "Early blight" | "healthy" | "uncertain",
        "confidence": 0.0-1.0,
        "raw_class": "Tomato___Early_blight" or None,
        "top_predictions": [(label, prob), ...]  # top 3, for debugging/UI
        "care_tip": "..." or None  # practical advice for this diagnosis, see plant_care_tips.py
      }
    """
    bundle = _load(crop_type)
    clf = bundle["model"]

    features = extract_features(rgb_image_array_or_path).reshape(1, -1)
    probs = clf.predict_proba(features)[0]
    classes = clf.classes_

    order = np.argsort(probs)[::-1]
    top3 = [(prettify_label(classes[i]), float(probs[i])) for i in order[:3]]

    best_idx = order[0]
    best_prob = float(probs[best_idx])
    best_class = classes[best_idx]

    if best_prob < CONFIDENCE_THRESHOLD:
        return {
            "label": "uncertain",
            "confidence": best_prob,
            "raw_class": None,
            "top_predictions": top3,
            "care_tip": None,
        }

    return {
        "label": prettify_label(best_class),
        "confidence": best_prob,
        "raw_class": best_class,
        "top_predictions": top3,
        "care_tip": get_care_tip(best_class),
    }
