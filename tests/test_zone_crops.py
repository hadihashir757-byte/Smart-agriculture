import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import crops  # noqa: E402
import db  # noqa: E402
import photo_pipeline  # noqa: E402
import plant_health  # noqa: E402


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()


def test_parse_zone_crop_map():
    assert crops.parse_zone_crop_map("1:tomato, 2:Mango") == {1: "tomato", 2: "mango"}
    assert crops.parse_zone_crop_map("junk,x:y,3:") == {}
    assert crops.parse_zone_crop_map("") == {}


def test_crop_for_zone_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ZONE_CROP_MAP", "1:tomato,2:mango")
    monkeypatch.setenv("DEFAULT_CROP", "plantain")
    assert crops.crop_for_zone(1) == "tomato"
    assert crops.crop_for_zone(2) == "mango"
    assert crops.crop_for_zone(5) == "plantain"
    assert crops.crop_for_zone(None) == "plantain"
    monkeypatch.delenv("ZONE_CROP_MAP")
    assert crops.crop_for_zone(2) == "plantain"


def test_plant_health_is_kept_per_zone(tmpdb):
    db.insert_plant_health_prediction("Tomato___Early_blight", 0.9, True, growth_stage="mature", zone_id=1)
    db.insert_plant_health_prediction(None, None, True, growth_stage="seedling", zone_id=2)
    assert db.latest_accepted_plant_health(zone_id=1)["predicted_class"] == "Tomato___Early_blight"
    assert db.latest_accepted_plant_health(zone_id=2)["growth_stage"] == "seedling"
    assert db.latest_accepted_plant_health(zone_id=3) is None
    # a photo without a zone (uploaded by hand) still counts for every zone
    db.insert_plant_health_prediction("Tomato___healthy", 0.8, True, growth_stage="mature")
    assert db.latest_accepted_plant_health(zone_id=3)["predicted_class"] == "Tomato___healthy"


def test_growth_baseline_is_kept_per_zone(tmpdb):
    assert db.get_growth_baseline(1) is None
    db.set_growth_baseline(0.2, 1)
    db.set_growth_baseline(0.5, 2)
    db.set_growth_baseline(0.9)
    assert db.get_growth_baseline(1)["baseline_ratio"] == 0.2
    assert db.get_growth_baseline(2)["baseline_ratio"] == 0.5
    assert db.get_growth_baseline()["baseline_ratio"] == 0.9


def test_old_database_gets_the_zone_column(tmp_path, monkeypatch):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE plant_health_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
              "predicted_class TEXT, confidence REAL, accepted INTEGER, reject_reason TEXT, growth_stage TEXT, top_predictions TEXT)")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.insert_plant_health_prediction("x", 0.5, True, zone_id=4)
    assert db.latest_accepted_plant_health(zone_id=4)["zone_id"] == 4


def photo():
    arr = np.zeros((64, 64, 3), dtype="uint8")
    arr[:, :32] = (40, 160, 40)
    return Image.fromarray(arr)


def test_mango_zone_photo_uses_the_mango_rules_and_its_own_baseline(tmpdb, monkeypatch):
    monkeypatch.setenv("ZONE_CROP_MAP", "1:tomato,2:mango")
    monkeypatch.setattr(photo_pipeline, "check_quality", lambda a: (True, "", {}))
    monkeypatch.setattr(photo_pipeline.thingsboard, "push_ai_telemetry", lambda d: None)
    seen = []
    monkeypatch.setattr(plant_health, "predict", lambda img, crop_type: seen.append(crop_type) or
                        {"label": "Healthy", "raw_class": "Tomato___healthy", "confidence": 0.9, "top_predictions": []})
    r2 = photo_pipeline.process_photo(photo(), zone_id=2)          # mango: no disease model, stage still stored
    assert r2["accepted"] is False and "mango" in r2["reason"] and r2["growth_stage"] == "seedling"
    r1 = photo_pipeline.process_photo(photo(), zone_id=1)          # tomato: full pipeline, own first-photo baseline
    assert r1["accepted"] is True and r1["growth_stage"] == "seedling" and seen == ["tomato"]
    assert db.get_growth_baseline(1) and db.get_growth_baseline(2)
    assert db.latest_accepted_plant_health(zone_id=2) is None      # mango photo gave no health result
    assert db.latest_accepted_plant_health(zone_id=1)["zone_id"] == 1


def test_photo_is_shown_on_the_dashboard_per_zone_and_as_custom(tmpdb, monkeypatch):
    monkeypatch.setenv("ZONE_CROP_MAP", "1:tomato,2:mango")
    monkeypatch.setattr(photo_pipeline, "check_quality", lambda a: (True, "", {}))
    monkeypatch.setattr(plant_health, "predict", lambda img, crop_type: {"label": "Healthy", "raw_class": "x", "confidence": 0.9, "top_predictions": []})
    pushed = []
    monkeypatch.setattr(photo_pipeline.thingsboard, "push_ai_telemetry", lambda d: pushed.append(d))
    photo_pipeline.process_photo(photo(), zone_id=2)
    photo_pipeline.process_photo(photo())
    keys = [k for d in pushed for k in d if k.startswith("ai_photo")]
    assert "ai_photo_zone2" in keys and "ai_photo_custom" in keys
    zone2 = next(d for d in pushed if "ai_photo_zone2" in d)
    assert zone2["ai_photo_zone2"].startswith("data:image/jpeg;base64,") and "mango" in zone2["ai_photo_zone2_label"]
    assert len(zone2["ai_photo_zone2"]) < 20000


def test_a_dashboard_failure_never_breaks_the_photo_result(tmpdb, monkeypatch):
    monkeypatch.setattr(photo_pipeline, "check_quality", lambda a: (False, "too blurry", {}))
    def boom(d): raise RuntimeError("thingsboard down")
    monkeypatch.setattr(photo_pipeline.thingsboard, "push_ai_telemetry", boom)
    assert photo_pipeline.process_photo(photo(), zone_id=1)["accepted"] is False


def test_mango_has_no_disease_model_until_one_is_trained(tmp_path, monkeypatch):
    monkeypatch.setitem(plant_health.MODEL_PATHS, "mango", str(tmp_path / "missing.joblib"))
    assert plant_health.supported_crop("mango") is False
    f = tmp_path / "plant_health_model_mango.joblib"
    f.write_bytes(b"x")
    monkeypatch.setitem(plant_health.MODEL_PATHS, "mango", str(f))
    assert plant_health.supported_crop("mango") is True
    assert plant_health.supported_crop("tomato") is True


def test_mango_care_tips_cover_every_class():
    import plant_care_tips
    for c in ("Anthracnose", "Bacterial_Canker", "Cutting_Weevil", "Die_Back", "Gall_Midge", "Healthy",
              "Powdery_Mildew", "Sooty_Mould"):
        assert plant_care_tips.get_care_tip("Mango___" + c) != plant_care_tips.DEFAULT_TIP


def test_mango_training_labels_match_the_care_tips(tmp_path, monkeypatch):
    import train_plant_health_mango as t
    for name in ("Powdery Mildew", "Healthy"):
        d = tmp_path / name
        d.mkdir()
        (d / "a.jpg").write_bytes(b"x")
        (d / "notes.txt").write_text("ignore me")
    monkeypatch.setattr(t, "DATA_DIR", str(tmp_path))
    paths, labels = t.gather_image_paths()
    assert sorted(set(labels)) == ["Mango___Healthy", "Mango___Powdery_Mildew"] and len(paths) == 2
