"""Open-Meteo wrapper for weather forecast lookup.

Uses Open-Meteo (open-meteo.com) instead of OpenWeatherMap - genuinely free,
no API key at all, no signup, no multi-hour key-activation wait, works the
instant you have coordinates. Up to 10,000 calls/day on the free tier, far
more than this project needs.

Degrades gracefully: if LOCATION_LAT / LOCATION_LON aren't set, or the
request fails for any reason (network, outage), this returns a 0mm/"Unknown"
forecast rather than raising - the irrigation decision then just falls back
to soil-moisture-only logic instead of crashing the endpoint. 0mm is the safe
default: worst case you water when rain was actually coming (wastes some
water), never the other way around (silently skipping a needed watering
because of a weather API outage).
"""

import os
import time

import requests

_CACHE_TTL_SEC = 900  # 15 min - no real need to call it more often than this
_cache = {"ts": 0, "rain_mm": 0.0, "condition": "Unknown", "probability_pct": 0}

# Open-Meteo's weathercode is the standard WMO code table - collapsing it
# down to the three words a tiny Blynk label can actually show.
_RAINY_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
_CLOUDY_CODES = {1, 2, 3, 45, 48}
_WINDY_SPEED_KMH = 25  # above this, call it "Windy" regardless of weathercode


def _classify_condition(weather_code, wind_speed_kmh):
    if wind_speed_kmh is not None and wind_speed_kmh >= _WINDY_SPEED_KMH:
        return "Windy"
    if weather_code in _RAINY_CODES:
        return "Rainy"
    if weather_code in _CLOUDY_CODES:
        return "Cloudy"
    return "Sunny"


def get_weather_forecast():
    """Returns {"rain_mm": float, "condition": str, "probability_pct": int}.

    rain_mm is the sum of the next ~6 hours - what the irrigation logic's
    RAIN_SKIP_THRESHOLD_MM check is based on. condition and probability_pct
    are for display only (e.g. the Blynk dashboard), read from the current
    hour so they reflect "what's happening soon", not a 6-hour summary.
    """
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL_SEC:
        return {"rain_mm": _cache["rain_mm"], "condition": _cache["condition"],
                "probability_pct": _cache["probability_pct"]}

    lat = os.environ.get("LOCATION_LAT")
    lon = os.environ.get("LOCATION_LON")

    if not (lat and lon):
        return {"rain_mm": 0.0, "condition": "Unknown", "probability_pct": 0}

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "precipitation,precipitation_probability,weathercode,windspeed_10m",
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
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        precip = hourly.get("precipitation", [])
        probability = hourly.get("precipitation_probability", [])
        weathercode = hourly.get("weathercode", [])
        windspeed = hourly.get("windspeed_10m", [])

        now_hour_str = time.strftime("%Y-%m-%dT%H:00", time.localtime(now))
        start_idx = times.index(now_hour_str) if now_hour_str in times else 0

        rain_mm = sum(precip[start_idx:start_idx + 6])
        window_probability = probability[start_idx:start_idx + 6]
        probability_pct = max(window_probability) if window_probability else 0
        current_code = weathercode[start_idx] if start_idx < len(weathercode) else 0
        current_wind = windspeed[start_idx] if start_idx < len(windspeed) else None
        condition = _classify_condition(current_code, current_wind)

        _cache["ts"] = now
        _cache["rain_mm"] = rain_mm
        _cache["condition"] = condition
        _cache["probability_pct"] = probability_pct
        return {"rain_mm": rain_mm, "condition": condition, "probability_pct": probability_pct}
    except Exception as e:
        print(f"[weather] forecast lookup failed, defaulting: {e}")
        return {"rain_mm": 0.0, "condition": "Unknown", "probability_pct": 0}
