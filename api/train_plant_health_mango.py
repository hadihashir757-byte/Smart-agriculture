"""
Trains the Plant Health model for MANGO: same hand-crafted image features
(see feature_extraction.py) -> scikit-learn HistGradientBoostingClassifier,
same approach as the tomato and plantain models.

Dataset: a folder of mango leaf photos with ONE SUB-FOLDER PER CLASS, for example the public
MangoLeafBD dataset (about 4,000 photos, 8 classes: Anthracnose, Bacterial Canker, Cutting Weevil,
Die Back, Gall Midge, Healthy, Powdery Mildew, Sooty Mould). Check the dataset page for its licence.
Put it here, so that each class is a folder directly inside:
    data/mango_leaf_disease/Anthracnose/*.jpg
    data/mango_leaf_disease/Healthy/*.jpg
    ...
(or point MANGO_DATA_DIR at the folder). If the dataset comes with an augmented copy, use only the
ORIGINAL photos: augmented copies of one photo landing in both the train and the test split make the
test accuracy look better than it is.

Class names become "Mango___<Folder_name>" (spaces turned into underscores), so they match the care tips
in plant_care_tips.py. The model is saved to models/plant_health_model_mango.joblib, and from then on
the server uses it for the mango zone (see MODEL_PATHS in plant_health.py).

Run from the Smart-agriculture folder with the venv active:
    python3 api/train_plant_health_mango.py
"""

import glob
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import classification_report, accuracy_score
from sklearn.model_selection import train_test_split

import sys
sys.path.insert(0, os.path.dirname(__file__))
from feature_extraction import extract_features  # noqa: E402

DATA_DIR = os.environ.get("MANGO_DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data", "mango_leaf_disease")
MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
RANDOM_SEED = 42


def gather_image_paths():
    class_dirs = sorted(glob.glob(os.path.join(DATA_DIR, "*")))
    paths, labels = [], []
    for class_dir in class_dirs:
        if not os.path.isdir(class_dir):
            continue
        label = "Mango___" + os.path.basename(class_dir).strip().replace(" ", "_")
        files = [f for f in glob.glob(os.path.join(class_dir, "*")) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        random.Random(RANDOM_SEED).shuffle(files)
        paths.extend(files)
        labels.extend([label] * len(files))
        print(f"  {label}: {len(files)} images")
    return paths, labels


def _extract_one(path):
    try:
        return extract_features(path)
    except Exception as e:
        print(f"  [skip] failed on {path}: {e}")
        return None


def main():
    print(f"Scanning dataset in {os.path.abspath(DATA_DIR)} ...")
    paths, labels = gather_image_paths()
    if not paths:
        raise SystemExit("No photos found. Put the dataset in data/mango_leaf_disease/<class name>/*.jpg (or set MANGO_DATA_DIR).")
    print(f"Total images: {len(paths)}")

    print("Extracting features (parallel across CPU cores)...")
    t0 = time.time()
    features, kept_labels = [], []
    with ProcessPoolExecutor() as pool:
        for path, label, vec in zip(paths, labels, pool.map(_extract_one, paths, chunksize=16)):
            if vec is not None:
                features.append(vec)
                kept_labels.append(label)
    print(f"Done in {time.time() - t0:.1f}s ({len(features)} usable images)")

    X = np.stack(features)
    y = np.array(kept_labels)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=RANDOM_SEED, stratify=y
    )

    print("Training HistGradientBoostingClassifier...")
    clf = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.1,
        random_state=RANDOM_SEED,
    )
    clf.fit(X_train, y_train)

    y_pred = clf.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    print(f"\nTest accuracy: {acc:.3f}\n")
    print(classification_report(y_test, y_pred))

    os.makedirs(MODEL_DIR, exist_ok=True)
    model_path = os.path.join(MODEL_DIR, "plant_health_model_mango.joblib")
    joblib.dump({"model": clf, "classes": sorted(set(y))}, model_path)
    print(f"Saved model to {model_path}")


if __name__ == "__main__":
    main()
