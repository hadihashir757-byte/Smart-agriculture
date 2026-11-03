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


def test_photo_not_configured(monkeypatch):
    monkeypatch.setattr(camera, "BASE", "")
    assert "not configured" in agent.handle_photo(rover(), NOW)


def test_photo_taken_when_due_and_bright(cam, fake_state):
    seen = []
    ok = lambda img: seen.append(img) or {"accepted": True, "prediction": {"label": "Healthy", "confidence": 0.9}, "growth_stage": "mature"}  # noqa: E731
    out = agent.handle_photo(rover(), NOW, process_photo=ok, fetch=jpeg_bytes)
    assert out.startswith("photo ok") and len(seen) == 1
    assert float(fake_state["last_photo_ts"]) == NOW
    assert agent.handle_photo(rover(), NOW + 60, process_photo=ok, fetch=jpeg_bytes) == "photo not due"


def test_photo_skipped_when_dark(cam):
    out = agent.handle_photo(rover(light=3), NOW, process_photo=lambda i: {}, fetch=jpeg_bytes)
    assert "too dark" in out


def test_camera_failure_retries_soon(cam, fake_state):
    def boom():
        raise RuntimeError("could not reach the camera")
    out = agent.handle_photo(rover(), NOW, process_photo=lambda i: {}, fetch=boom)
    assert "photo failed" in out
    # due again after the short retry delay, not after the full interval
    assert agent.handle_photo(rover(), NOW + agent.PHOTO_RETRY_S + 1, process_photo=lambda i: {"accepted": True, "prediction": {}}, fetch=jpeg_bytes).startswith("photo ok")


def test_rejected_photo_retries_soon(cam, fake_state):
    rej = lambda img: {"accepted": False, "reason": "too blurry"}  # noqa: E731
    assert "rejected" in agent.handle_photo(rover(), NOW, process_photo=rej, fetch=jpeg_bytes)
    assert agent.handle_photo(rover(), NOW + 100, process_photo=rej, fetch=jpeg_bytes) == "photo not due"


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
    assert ran and "demo plan reached" in out


def test_demo_plan_is_not_executed_without_auto_water():
    ran = []
    def fake_eval(zone_id, readings, crop):
        return {"decision": "water", "seconds": 3, "status": "dry_run", "reason": "x", "auto_water": False,
                "plan": {"run": True, "zone_id": zone_id}}
    out = agent.handle_soil(rover(), NOW, fake_eval, run_plan=lambda p: ran.append(p))
    assert not ran and "dry_run" in out
