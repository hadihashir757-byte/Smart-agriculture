"""Which crop grows in which zone.

ZONE_CROP_MAP="1:tomato,2:mango" in .env. A zone that is not listed uses DEFAULT_CROP.
Read at call time so tests (and a restarted agent) always see the current setting.
"""

import os


def default_crop():
    return (os.environ.get("DEFAULT_CROP") or "tomato").strip().lower()


def parse_zone_crop_map(text=None):
    """'1:tomato,2:mango' -> {1: 'tomato', 2: 'mango'}. Bad entries are ignored."""
    text = text if text is not None else os.environ.get("ZONE_CROP_MAP", "")
    mapping = {}
    for part in text.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        zone, crop = part.split(":", 1)
        try:
            zone_id = int(zone.strip())
        except ValueError:
            continue
        crop = crop.strip().lower()
        if crop:
            mapping[zone_id] = crop
    return mapping


def crop_for_zone(zone_id):
    try:
        return parse_zone_crop_map().get(int(zone_id), default_crop())
    except (TypeError, ValueError):
        return default_crop()
