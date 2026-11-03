"""Minimal ThingsBoard client (REST) - used to send the `water` RPC to the
irrigation station and to read its tank level.

Everything comes from environment variables, nothing secret lives in the code:
  TB_BASE_URL          default https://eu.thingsboard.cloud
  TB_USERNAME          tenant user e-mail (a dedicated low-privilege user is best)
  TB_PASSWORD
  STATION_DEVICE_NAME  default "irrigation station"
  AI_DEVICE_TOKEN      optional: access token of a ThingsBoard device (e.g. "ai_server")
                       that receives the AI decisions as telemetry for dashboards
"""

import os
import time

import requests

BASE = os.environ.get("TB_BASE_URL", "https://eu.thingsboard.cloud").rstrip("/")
USERNAME = os.environ.get("TB_USERNAME")
PASSWORD = os.environ.get("TB_PASSWORD")
STATION_DEVICE_NAME = os.environ.get("STATION_DEVICE_NAME", "irrigation station")
AI_DEVICE_TOKEN = os.environ.get("AI_DEVICE_TOKEN")
TIMEOUT = 10

_jwt = {"token": None, "exp": 0}
_device_ids = {}


def configured():
    return bool(USERNAME and PASSWORD)


def _login():
    r = requests.post(f"{BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, timeout=TIMEOUT)
    r.raise_for_status()
    _jwt["token"] = r.json()["token"]
    _jwt["exp"] = time.time() + 2 * 3600  # tokens last longer; refresh early
    return _jwt["token"]


def _headers():
    if not _jwt["token"] or time.time() > _jwt["exp"]:
        _login()
    return {"X-Authorization": f"Bearer {_jwt['token']}"}


def _device_id(name):
    if name not in _device_ids:
        r = requests.get(f"{BASE}/api/tenant/devices", params={"deviceName": name}, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        _device_ids[name] = r.json()["id"]["id"]
    return _device_ids[name]


def latest_telemetry(device_name, keys):
    """Latest value of each key, e.g. {'tank_level_percent': 72}. Missing keys are left out."""
    dev = _device_id(device_name)
    r = requests.get(f"{BASE}/api/plugins/telemetry/DEVICE/{dev}/values/timeseries",
                     params={"keys": ",".join(keys)}, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for k, v in r.json().items():
        if v:
            out[k] = v[0]["value"]
    return out


def latest_with_ts(device_name, keys):
    """Like latest_telemetry but keeps the time: {'soil_moisture_percent': (value, ts_ms)}."""
    dev = _device_id(device_name)
    r = requests.get(f"{BASE}/api/plugins/telemetry/DEVICE/{dev}/values/timeseries",
                     params={"keys": ",".join(keys)}, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return {k: (v[0]["value"], int(v[0]["ts"])) for k, v in r.json().items() if v}


def value_at(device_name, key, ts_ms, window_ms=120000):
    """The newest value of `key` recorded at or shortly before ts_ms (None if nothing)."""
    dev = _device_id(device_name)
    r = requests.get(f"{BASE}/api/plugins/telemetry/DEVICE/{dev}/values/timeseries",
                     params={"keys": key, "startTs": int(ts_ms - window_ms), "endTs": int(ts_ms + 5000),
                             "limit": 1, "orderBy": "DESC", "agg": "NONE"},
                     headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    vals = r.json().get(key) or []
    return vals[0]["value"] if vals else None


def send_rpc(device_name, method, params, timeout_ms=8000):
    """Two-way RPC: waits for the device's reply, so we learn if the station accepted or refused."""
    dev = _device_id(device_name)
    r = requests.post(f"{BASE}/api/rpc/twoway/{dev}",
                      json={"method": method, "params": params, "timeout": timeout_ms},
                      headers=_headers(), timeout=timeout_ms / 1000 + 5)
    r.raise_for_status()
    return r.json()


def push_ai_telemetry(data):
    """Best-effort: publish the AI decision so a dashboard can show it. Never raises."""
    if not AI_DEVICE_TOKEN:
        return
    data = {k: v for k, v in data.items() if v is not None}   # never overwrite good values with null
    if not data:
        return
    try:
        requests.post(f"{BASE}/api/v1/{AI_DEVICE_TOKEN}/telemetry", json=data, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"[thingsboard] could not push AI telemetry: {e}")


def plant_telemetry(raw_class, confidence, stage, care_tip, pretty_label):
    """Fields shown on the dashboard's 'Plant health / Plant state / Care tip' cards."""
    return {"ai_plant_label": pretty_label, "ai_plant_confidence": confidence,
            "ai_plant_stage": stage, "ai_care_tip": care_tip}
