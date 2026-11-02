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
