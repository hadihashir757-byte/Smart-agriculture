import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

import agent  # noqa: E402
import camera  # noqa: E402
import db  # noqa: E402
import thingsboard  # noqa: E402

NOW = 1_800_000_000.0
NOW_MS = int(NOW * 1000)


@pytest.fixture(autouse=True)
def fake_state(monkeypatch):
    store = {}
    monkeypatch.setattr(db, "get_state", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(db, "set_state", lambda k, v: store.__setitem__(k, str(v)))
    monkeypatch.setattr(thingsboard, "USERNAME", "u")
    monkeypatch.setattr(thingsboard, "PASSWORD", "p")
    monkeypatch.setattr(thingsboard, "value_at", lambda name, key, ts: 2)
    return store


def rover(soil=22.0, soil_ts=NOW_MS - 20_000, light=60):
    d = {"zone_id": (3, NOW_MS), "temperature_c": (29.0, NOW_MS), "humidity_percent": (55.0, NOW_MS),
         "light_percent": (light, NOW_MS), "rain_detected": (False, NOW_MS), "event": ("periodic", NOW_MS)}
    if soil is not None:
        d["soil_moisture_percent"] = (soil, soil_ts)
    return d


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, zone_id, readings, crop):
        self.calls.append((zone_id, readings, crop))
        return {"decision": "water", "seconds": 20, "status": "dry_run", "reason": "test"}


def test_new_soil_reading_triggers_one_decision_for_the_right_zone():
    rec = Recorder()
    out = agent.handle_soil(rover(), NOW, rec)
    assert len(rec.calls) == 1 and rec.calls[0][0] == 2          # zone from value_at, not the latest zone
    assert rec.calls[0][1]["soil_moisture_percent"] == 22.0 and rec.calls[0][1]["rain_detected"] is False
    assert "zone 2" in out


def test_same_reading_is_never_handled_twice():
    rec = Recorder()
    agent.handle_soil(rover(), NOW, rec)
    assert agent.handle_soil(rover(), NOW + 60, rec) == "no new soil measurement"
    assert len(rec.calls) == 1


def test_newer_reading_is_handled():
    rec = Recorder()
    agent.handle_soil(rover(), NOW, rec)
    agent.handle_soil(rover(soil_ts=NOW_MS + 30 * 60_000), NOW + 31 * 60, rec)
    assert len(rec.calls) == 2


def test_old_reading_is_ignored_but_remembered():
    rec = Recorder()
    out = agent.handle_soil(rover(soil_ts=NOW_MS - 3_600_000), NOW, rec)
    assert "ignored" in out and not rec.calls
    assert agent.handle_soil(rover(soil_ts=NOW_MS - 3_600_000), NOW, rec) == "no new soil measurement"


def test_no_soil_value_means_nothing_to_do():
    rec = Recorder()
    assert agent.handle_soil(rover(soil=None), NOW, rec) == "no soil measurement yet" and not rec.calls


def test_unknown_zone_is_skipped(monkeypatch):
    monkeypatch.setattr(thingsboard, "value_at", lambda *a: None)
    r = rover(); del r["zone_id"]
    rec = Recorder()
    assert "zone is unknown" in agent.handle_soil(r, NOW, rec) and not rec.calls


# ---- photos ----
def jpeg_bytes():
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (32, 32), (10, 120, 10)).save(b, "JPEG"); return b.getvalue()


@pytest.fixture
def cam(monkeypatch):
    monkeypatch.setattr(camera, "BASE", "http://phone:8080")
    monkeypatch.setattr(agent, "PHOTO_MODE", "timer")          # the older tests below use the timer


@pytest.fixture
def zonecam(monkeypatch):
    monkeypatch.setattr(camera, "BASE", "http://phone:8080")
    monkeypatch.setattr(agent, "PHOTO_MODE", "zone")


def test_photo_not_configured(monkeypatch):
    monkeypatch.setattr(camera, "BASE", "")
    assert "not configured" in agent.handle_photo(rover(), NOW)


def test_photo_taken_when_due_and_bright(cam, fake_state):
    seen = []
    ok = lambda img, **kw: seen.append(img) or {"accepted": True, "prediction": {"label": "Healthy", "confidence": 0.9}, "growth_stage": "mature"}  # noqa: E731
    out = agent.handle_photo(rover(), NOW, process_photo=ok, fetch=jpeg_bytes)
    assert out.startswith("photo ok") and len(seen) == 1
    assert float(fake_state["last_photo_ts"]) == NOW
    assert agent.handle_photo(rover(), NOW + 60, process_photo=ok, fetch=jpeg_bytes) == "photo not due"


def test_photo_skipped_when_dark(cam):
    out = agent.handle_photo(rover(light=3), NOW, process_photo=lambda i, **kw: {}, fetch=jpeg_bytes)
    assert "too dark" in out


def test_camera_failure_retries_soon(cam, fake_state):
    def boom():
        raise RuntimeError("could not reach the camera")
    out = agent.handle_photo(rover(), NOW, process_photo=lambda i, **kw: {}, fetch=boom)
    assert "photo failed" in out
    # due again after the short retry delay, not after the full interval
    assert agent.handle_photo(rover(), NOW + agent.PHOTO_RETRY_S + 1, process_photo=lambda i, **kw: {"accepted": True, "prediction": {}}, fetch=jpeg_bytes).startswith("photo ok")


def test_rejected_photo_retries_soon(cam, fake_state):
    rej = lambda img, **kw: {"accepted": False, "reason": "too blurry"}  # noqa: E731
    assert "rejected" in agent.handle_photo(rover(), NOW, process_photo=rej, fetch=jpeg_bytes)
    assert agent.handle_photo(rover(), NOW + 100, process_photo=rej, fetch=jpeg_bytes) == "photo not due"


# ---- photo when the rover arrives at a zone ----
def at_zone_rover(arrival_id=123456, age_s=5, light=60):
    d = rover(light=light)
    d["arrival_id"] = (arrival_id, NOW_MS - age_s * 1000)
    return d


GOOD = lambda img, **kw: {"accepted": True, "prediction": {"label": "Healthy", "confidence": 0.9}, "growth_stage": "mature"}  # noqa: E731


def test_zone_photo_taken_once_per_arrival(zonecam, fake_state):
    out = agent.handle_photo(at_zone_rover(), NOW, process_photo=GOOD, fetch=jpeg_bytes)
    assert out.startswith("zone 3: photo ok")
    assert agent.handle_photo(at_zone_rover(), NOW + 10, process_photo=GOOD, fetch=jpeg_bytes) == "photo not due"      # same arrival: no second photo
    assert agent.handle_photo(at_zone_rover(arrival_id=777), NOW + 100, process_photo=GOOD, fetch=jpeg_bytes).startswith("zone 3: photo ok")   # next arrival


def test_zone_photo_no_arrival_means_no_photo(zonecam):
    assert agent.handle_photo(rover(), NOW, process_photo=GOOD, fetch=jpeg_bytes) == "photo not due"
    assert agent.handle_photo(at_zone_rover(arrival_id=0), NOW, process_photo=GOOD, fetch=jpeg_bytes) == "photo not due"


def test_zone_photo_too_late_is_skipped(zonecam, fake_state):
    out = agent.handle_photo(at_zone_rover(age_s=agent.PHOTO_ARRIVAL_MAX_AGE_S + 60), NOW, process_photo=GOOD, fetch=jpeg_bytes)
    assert out == "photo not due" and fake_state["last_arrival_id"] == "123456"


def test_zone_photo_failure_is_retried_while_rover_is_there(zonecam, fake_state):
    def boom():
        raise RuntimeError("camera offline")
    assert "will try again" in agent.handle_photo(at_zone_rover(), NOW, process_photo=GOOD, fetch=boom)
    assert agent.handle_photo(at_zone_rover(), NOW + 10, process_photo=GOOD, fetch=jpeg_bytes).startswith("zone 3: photo ok")


def test_zone_photo_dark_is_not_retried(zonecam, fake_state):
    assert "too dark" in agent.handle_photo(at_zone_rover(light=2), NOW, process_photo=GOOD, fetch=jpeg_bytes)
    assert agent.handle_photo(at_zone_rover(light=60), NOW + 10, process_photo=GOOD, fetch=jpeg_bytes) == "photo not due"


def test_zone_photo_for_a_crop_without_a_disease_model_still_counts(zonecam, fake_state):
    no_model = lambda img, **kw: {"accepted": False, "reason": "Disease detection isn't available for 'mango' yet", "growth_stage": "mature"}  # noqa: E731
    out = agent.handle_photo(at_zone_rover(), NOW, process_photo=no_model, fetch=jpeg_bytes)
    assert out.startswith("zone 3: photo ok") and "no disease model" in out
    assert agent.handle_photo(at_zone_rover(), NOW + 10, process_photo=no_model, fetch=jpeg_bytes) == "photo not due"      # not retried


def test_zone_mode_ignores_the_timer(zonecam):
    assert agent.handle_photo(rover(), NOW + 99999, process_photo=GOOD, fetch=jpeg_bytes) == "photo not due"


# ---- tick: one failing job must not stop the other ----
def test_tick_isolates_failures(monkeypatch, cam):
    monkeypatch.setattr(thingsboard, "latest_with_ts", lambda *a, **k: rover())
    def bad_eval(*a):
        raise ValueError("boom")
    monkeypatch.setattr(agent, "handle_photo", lambda *a, **k: "photo not due")
    notes = agent.tick(now=NOW, evaluate_zone=bad_eval)
    assert notes[1].startswith("error: ValueError") and notes[0] == "photo not due"


def test_tick_without_login(monkeypatch):
    monkeypatch.setattr(thingsboard, "USERNAME", None)
    assert "not set" in agent.tick(now=NOW)[0]


def test_camera_messages(monkeypatch):
    import requests
    monkeypatch.setattr(camera, "BASE", "http://phone:8080")
    class R:  # noqa: D401
        def __init__(self, code, body=b"", ctype="image/jpeg"):
            self.status_code, self.content, self.headers = code, body, {"Content-Type": ctype}
    monkeypatch.setattr(camera.requests, "get", lambda url, **k: R(401))
    with pytest.raises(RuntimeError, match="refused the login"):
        camera.fetch_snapshot()
    monkeypatch.setattr(camera.requests, "get", lambda url, **k: R(200, b"<html>", "text/html"))
    with pytest.raises(RuntimeError, match="did not return a picture"):
        camera.fetch_snapshot()
    monkeypatch.setattr(camera.requests, "get", lambda url, **k: R(200, jpeg_bytes()))
    assert camera.fetch_snapshot()[:2] == b"\xff\xd8"
    def down(url, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(camera.requests, "get", down)
    with pytest.raises(RuntimeError, match="same Wi-Fi"):
        camera.fetch_snapshot()


def test_demo_plan_is_executed_by_the_agent():
    ran = []
    def fake_eval(zone_id, readings, crop):
        return {"decision": "water", "seconds": 3, "status": "plan_ready", "reason": "x", "auto_water": True,
                "plan": {"run": True, "zone_id": zone_id}}
    def fake_run(plan):
        ran.append(plan)
        return {"outcome": "reached", "summary": "done"}
    out = agent.handle_soil(rover(), NOW, fake_eval, run_plan=fake_run)
    assert ran and "plan reached" in out


def test_demo_plan_is_not_executed_without_auto_water():
    ran = []
    def fake_eval(zone_id, readings, crop):
        return {"decision": "water", "seconds": 3, "status": "dry_run", "reason": "x", "auto_water": False,
                "plan": {"run": True, "zone_id": zone_id}}
    out = agent.handle_soil(rover(), NOW, fake_eval, run_plan=lambda p: ran.append(p))
    assert not ran and "dry_run" in out


# ---- per-zone crops ----
def test_decision_uses_the_crop_of_the_zone(monkeypatch):
    monkeypatch.setenv("ZONE_CROP_MAP", "2:mango,3:tomato")
    rec = Recorder()
    agent.handle_soil(rover(), NOW, rec)                         # value_at says zone 2
    assert rec.calls[0][2] == "mango"


def test_zone_photo_is_processed_for_the_rovers_zone(zonecam, fake_state):
    seen = []
    def pp(img, zone_id=None):
        seen.append(zone_id)
        return {"accepted": True, "prediction": {"label": "Healthy", "confidence": 0.9}, "growth_stage": "mature"}
    agent.handle_photo(at_zone_rover(), NOW, process_photo=pp, fetch=jpeg_bytes)
    assert seen == [3]


# ---- weather forecast on the dashboard ----
FORECAST = {"rain_mm": 2.34, "condition": "Rainy", "probability_pct": 70, "temperature_c": 27.46, "wind_kmh": 12.4}


def test_weather_is_pushed_then_not_again_until_due(monkeypatch, fake_state):
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", "x")
    monkeypatch.setenv("LOCATION_LAT", "11.6")
    monkeypatch.setenv("LOCATION_LON", "75.6")
    sent = []
    monkeypatch.setattr(thingsboard, "push_ai_telemetry", lambda d: sent.append(d))
    out = agent.push_weather(NOW, forecast=lambda: FORECAST)
    assert out.startswith("weather sent") and sent[0]["ai_weather_rain_mm_6h"] == 2.3
    assert sent[0]["ai_weather_temp_c"] == 27.5 and sent[0]["ai_weather_rain_chance_pct"] == 70
    assert agent.push_weather(NOW + 60, forecast=lambda: FORECAST) == "weather not due"
    assert agent.push_weather(NOW + agent.WEATHER_PUSH_S + 1, forecast=lambda: FORECAST).startswith("weather sent")


def test_weather_needs_location_and_token(monkeypatch, fake_state):
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", None)
    assert "AI_DEVICE_TOKEN" in agent.push_weather(NOW, forecast=lambda: FORECAST)
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", "x")
    monkeypatch.delenv("LOCATION_LAT", raising=False)
    assert "LOCATION" in agent.push_weather(NOW + 10_000, forecast=lambda: FORECAST)


def test_unknown_forecast_is_not_sent(monkeypatch, fake_state):
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", "x")
    monkeypatch.setenv("LOCATION_LAT", "11.6")
    monkeypatch.setenv("LOCATION_LON", "75.6")
    sent = []
    monkeypatch.setattr(thingsboard, "push_ai_telemetry", lambda d: sent.append(d))
    out = agent.push_weather(NOW, forecast=lambda: {"rain_mm": 0.0, "condition": "Unknown", "probability_pct": 0})
    assert "unavailable" in out and sent == []


def test_a_missing_setting_does_not_use_up_the_weather_slot(monkeypatch, fake_state):
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", None)
    monkeypatch.setenv("LOCATION_LAT", "11.6")
    monkeypatch.setenv("LOCATION_LON", "75.6")
    assert "AI_DEVICE_TOKEN" in agent.push_weather(NOW, forecast=lambda: FORECAST)
    monkeypatch.setattr(thingsboard, "AI_DEVICE_TOKEN", "x")
    sent = []
    monkeypatch.setattr(thingsboard, "push_ai_telemetry", lambda d: sent.append(d))
    assert agent.push_weather(NOW + 5, forecast=lambda: FORECAST).startswith("weather sent")     # right after the fix, not 15 min later
