"""
Trains the Plant Health model for plantain/banana: same hand-crafted image
features (see feature_extraction.py) -> scikit-learn HistGradientBoostingClassifier,
same approach as train_plant_health.py used for tomato.

Dataset: BananaLSD (Mendeley Data, CC BY 4.0) - 937 real photos across 4
classes: cordana, healthy, pestalotiopsis, sigatoka. Deliberately using only
the OriginalSet (not the AugmentedSet that ships alongside it) - the
augmented images are generated FROM the originals, so mixing them in risks
an augmented copy of a photo landing in the test split while its original
sits in the training split (or vice versa), which would inflate the test
accuracy without the model actually having learned anything extra.

Much smaller dataset than tomato's (937 vs ~18,000 real photos), so expect
meaningfully lower accuracy than tomato's 96.5% - that's an honest
consequence of less training data, not a bug.

Run from the smart-agriculture/ directory with the venv active:
    python3 api/train_plant_health_plantain.py
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

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "banana_leaf_disease", "OriginalSet")
MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
RANDOM_SEED = 42


def gather_image_paths():
    class_dirs = sorted(glob.glob(os.path.join(DATA_DIR, "*")))
    paths, labels = [], []
    for class_dir in class_dirs:
        if not os.path.isdir(class_dir):
            continue
        label = os.path.basename(class_dir)
        files = glob.glob(os.path.join(class_dir, "*"))
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
    print("Scanning dataset...")
    paths, labels = gather_image_paths()
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
    model_path = os.path.join(MODEL_DIR, "plant_health_model_plantain.joblib")
    joblib.dump({"model": clf, "classes": sorted(set(y))}, model_path)
    print(f"Saved model to {model_path}")


if __name__ == "__main__":
    main()
