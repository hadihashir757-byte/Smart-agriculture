"""Open-Meteo wrapper for rain forecast lookup.

Uses Open-Meteo (open-meteo.com) instead of OpenWeatherMap - genuinely free,
no API key at all, no signup, no multi-hour key-activation wait, works the
instant you have coordinates. Up to 10,000 calls/day on the free tier, far
more than this project needs.

Degrades gracefully: if LOCATION_LAT / LOCATION_LON aren't set, or the
request fails for any reason (network, outage), this returns 0mm forecast
rather than raising - the irrigation decision then just falls back to
soil-moisture-only logic instead of crashing the endpoint. 0mm is the safe
default: worst case you water when rain was actually coming (wastes some
water), never the other way around (silently skipping a needed watering
because of a weather API outage).
"""

import os
import time

import requests

_CACHE_TTL_SEC = 900  # 15 min - no real need to call it more often than this
_cache = {"ts": 0, "rain_mm": 0.0}


def get_rain_forecast_mm():
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["rain_mm"]

    lat = os.environ.get("LOCATION_LAT")
    lon = os.environ.get("LOCATION_LON")

    if not (lat and lon):
        return 0.0

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "precipitation",
                "forecast_days": 1,
                "timezone": "auto",
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        # Sum the next 6 hourly precipitation values (mm) starting from now -
        # same "~6h ahead" window the irrigation logic was already designed
        # around. Open-Meteo returns a full day's hourly timestamps; find the
        # current hour's index and sum forward from there.
        times = data.get("hourly", {}).get("time", [])
        precip = data.get("hourly", {}).get("precipitation", [])

        now_hour_str = time.strftime("%Y-%m-%dT%H:00", time.localtime(now))
        start_idx = times.index(now_hour_str) if now_hour_str in times else 0

        rain_mm = sum(precip[start_idx:start_idx + 6])

        _cache["ts"] = now
        _cache["rain_mm"] = rain_mm
        return rain_mm
    except Exception as e:
        print(f"[weather] forecast lookup failed, defaulting to 0mm: {e}")
        return 0.0
