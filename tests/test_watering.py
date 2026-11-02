import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import watering  # noqa: E402

NOW = 1_800_000_000
MAP = {1: 1, 2: 2}
DRY = {"soil_moisture_percent": 25, "temperature_c": 28, "humidity_percent": 60,
       "rain_detected": False, "reading_ts": NOW - 30}


def d(readings=None, **kw):
    return watering.decide(1, readings or DRY, now=NOW, zone_pump_map=MAP, **kw)


def test_dry_soil_waters_with_valid_command():
    r = d()
    assert r["decision"] == "water" and r["pump"] == 1
    p = r["command"]["params"]
    assert r["command"]["method"] == "water"
    assert watering.MIN_SECONDS <= p["seconds"] <= watering.MAX_SECONDS
    assert p["issued_at"] == NOW and p["command_id"].startswith("ai_")


def test_command_ids_are_unique():
    assert d()["command"]["params"]["command_id"] != d()["command"]["params"]["command_id"]


def test_unknown_zone_skips():
    assert watering.decide(9, DRY, now=NOW, zone_pump_map=MAP)["decision"] == "skip"


def test_missing_soil_skips():
    assert d({**DRY, "soil_moisture_percent": None})["decision"] == "skip"


def test_stale_reading_skips():
    assert d({**DRY, "reading_ts": NOW - 10_000})["decision"] == "skip"


def test_rain_skips():
    assert d({**DRY, "rain_detected": True})["decision"] == "skip"


def test_cooldown_skips_then_allows():
    assert d(last_water_ts=NOW - 60)["decision"] == "skip"
    assert d(last_water_ts=NOW - watering.COOLDOWN_S - 1)["decision"] == "water"


def test_low_tank_skips():
    assert d(tank_percent=10)["decision"] == "skip"


def test_wet_soil_skips():
    assert d({**DRY, "soil_moisture_percent": 85})["decision"] == "skip"


def test_seconds_are_clamped():
    r = d({**DRY, "soil_moisture_percent": 5})   # critically dry -> 45 s from the rules
    assert r["seconds"] == watering.MAX_SECONDS


# ---- rain-soon rule, tank unknown, plant-health rules ----
import pytest  # noqa: E402


def soil(pct, **extra):
    return {**DRY, "soil_moisture_percent": pct, **extra}


H = lambda label, conf=0.9: {"label": label, "confidence": conf}  # noqa: E731


@pytest.mark.parametrize("pct,rain,expect", [
    (12, 10, "water"),   # critical: always
    (30, 10, "water"),   # rain coming but < 40: short top-up
    (39, 10, "water"),
    (40, 10, "skip"),    # >= 40: wait for rain
    (55, 10, "skip"),
    (30, 2, "skip"),     # small rain ignored, tomato mature start=30 -> adequate
    (25, 2, "water"),
    (85, 0, "skip"),     # too wet
])
def test_rain_matrix(pct, rain, expect):
    assert d(soil(pct), rain_forecast_mm=rain)["decision"] == expect


def test_rain_topup_is_short():
    r = d(soil(30), rain_forecast_mm=10)
    assert r["seconds"] <= 15 and "top-up" in r["reason"]


def test_rain_now_beats_critical():
    assert d(soil(5, rain_detected=True), rain_forecast_mm=0)["decision"] == "skip"


def test_tank_unknown_skips():
    assert d(tank_percent=None)["decision"] == "skip"


def test_seedling_waters_earlier_than_mature():
    assert d(soil(35), plant_stage="seedling")["decision"] == "water"
    assert d(soil(35), plant_stage="mature")["decision"] == "skip"


def test_plantain_needs_more_water_than_tomato():
    assert d(soil(45), crop_type="plantain")["decision"] == "water"
    assert d(soil(45), crop_type="tomato")["decision"] == "skip"


def test_hot_dry_air_adds_time():
    from irrigation_rules import get_irrigation_recommendation as g
    cool = g(29, 25, 60, 0, "tomato", "mature")["duration_sec"]
    hot = g(29, 36, 25, 0, "tomato", "mature")["duration_sec"]
    assert hot == cool + 10


def test_never_exceeds_max():
    assert d(soil(1))["seconds"] <= watering.MAX_SECONDS


def test_disease_skips_when_not_critical():
    r = d(soil(25), plant_health=H("Early blight"))
    assert r["decision"] == "skip" and "disease" in r["reason"]


def test_disease_critical_dry_waters_gently():
    r = d(soil(10), plant_health=H("Late blight"))
    assert r["decision"] == "water" and r["seconds"] <= 15


def test_stress_adds_5s():
    base = d(soil(25))["seconds"]
    assert d(soil(25), plant_health=H("wilt"))["seconds"] == base + 5


def test_overwatered_skips_even_if_dry_reading():
    assert d(soil(25), plant_health=H("overwatered"))["decision"] == "skip"


def test_low_confidence_or_healthy_ignored():
    base = d(soil(25))["seconds"]
    assert d(soil(25), plant_health=H("Early blight", 0.3))["seconds"] == base
    assert d(soil(25), plant_health=H("healthy"))["seconds"] == base


def test_cooldown_blocks_second_watering():
    assert d(last_water_ts=NOW - 600)["decision"] == "skip"
    assert d(last_water_ts=NOW - 4000)["decision"] == "water"
