"""Plant Health inference: loads the trained model once and exposes predict().

See train_plant_health.py for how plant_health_model.joblib was produced
(hand-crafted color/texture features -> HistGradientBoostingClassifier,
trained on the tomato subset of PlantVillage).
"""

import os

import joblib
import numpy as np

from feature_extraction import extract_features
from plant_care_tips import get_care_tip

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "plant_health_model.joblib")

CONFIDENCE_THRESHOLD = 0.55  # below this, report "uncertain" instead of a guess

_model_bundle = None


def _load():
    global _model_bundle
    if _model_bundle is None:
        _model_bundle = joblib.load(MODEL_PATH)
    return _model_bundle


def prettify_label(raw_label):
    # "Tomato___Early_blight" -> "Early blight"
    name = raw_label.split("___", 1)[-1]
    return name.replace("_", " ").strip()


def predict(rgb_image_array_or_path):
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
    bundle = _load()
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
