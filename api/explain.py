"""Plain-English reasoning for the dashboard: what the AI checked, what it decided, and why."""


def as_bool(v):
    return str(v).strip().lower() in ("true", "1")


def rover_state(station_telemetry):
    """The rover's position as the STATION sees it (None when unknown)."""
    if not station_telemetry or "rover_link_ok" not in station_telemetry:
        return None
    try:
        zone = int(float(station_telemetry.get("rover_zone")))
    except (TypeError, ValueError):
        zone = None
    return {"link_ok": as_bool(station_telemetry.get("rover_link_ok")),
            "at_zone": as_bool(station_telemetry.get("rover_at_zone")), "zone": zone}


def rover_text(rover, zone_id):
    if rover is None:
        return "rover position unknown (the station checks it)"
    if not rover["link_ok"]:
        return "rover link LOST"
    if rover["at_zone"] and rover["zone"] == zone_id:
        return f"rover is AT zone {zone_id}"
    where = f"zone {rover['zone']}" if rover["zone"] else "no zone"
    return f"rover is NOT at zone {zone_id} (at {where}, moving)" if not rover["at_zone"] else f"rover is at zone {rover['zone']}, not zone {zone_id}"


def checks_line(zone_id, soil, tank, rain_mm, rain_detected, rover, reading_age_s=None, target=None, demo=False):
    parts = [f"zone {zone_id}",
             "soil: no reading" if soil is None else f"soil {soil:.0f}%" + (f" (target {target}%)" if target else ""),
             "tank: unknown" if tank is None else f"tank {tank:.0f}% (needs 15%+)",
             f"rain forecast {rain_mm:.0f} mm", "raining now: " + ("YES" if rain_detected else "no"),
             rover_text(rover, zone_id)]
    if reading_age_s is not None:
        parts.append(f"reading {reading_age_s:.0f} s old")
    parts.append("mode: DEMO" if demo else "mode: normal")
    return " | ".join(parts)


def report(checks, decision, why):
    return f"CHECKED: {checks}\nDECISION: {decision.upper()}\nWHY: {why}"
