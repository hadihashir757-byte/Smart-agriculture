"""OpenWeatherMap wrapper for rain forecast lookup.

Degrades gracefully: if OPENWEATHER_API_KEY / LOCATION_LAT / LOCATION_LON
aren't set, or the request fails for any reason (network, quota, bad key),
this returns 0mm forecast rather than raising - the irrigation decision then
just falls back to soil-moisture-only logic instead of crashing the endpoint.
0mm is the safe default: worst case you water when rain was actually coming
(wastes some water), never the other way around (silently skipping a needed
watering because of a weather API outage).
"""

import os
import time

import requests

_CACHE_TTL_SEC = 900  # 15 min - free OpenWeatherMap tier allows ~1000 calls/day,
                       # no need to call it on every /get-recommendation hit
_cache = {"ts": 0, "rain_mm": 0.0}


def get_rain_forecast_mm():
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["rain_mm"]

    api_key = os.environ.get("OPENWEATHER_API_KEY")
    lat = os.environ.get("LOCATION_LAT")
    lon = os.environ.get("LOCATION_LON")

    if not (api_key and lat and lon):
        return 0.0

    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        # Sum forecast rain volume over the next 2 x 3-hour blocks (~6h ahead)
        rain_mm = 0.0
        for block in data.get("list", [])[:2]:
            rain_mm += block.get("rain", {}).get("3h", 0.0)

        _cache["ts"] = now
        _cache["rain_mm"] = rain_mm
        return rain_mm
    except Exception as e:
        print(f"[weather] forecast lookup failed, defaulting to 0mm: {e}")
        return 0.0
