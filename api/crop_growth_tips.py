"""
General growth/care tips per crop type - independent of disease diagnosis.

Unlike plant_care_tips.py (which only applies once a specific disease has
been diagnosed, and only exists for tomato since that's the only trained
model), this is just practical growing advice for whichever crop is
currently selected (see the V11/V12 switches on the Arduino) - useful even
when nothing is wrong with the plant, and works for amaranth too since it
doesn't depend on any trained model at all.
"""

GROWTH_TIPS = {
    "tomato": (
        "Needs 6-8 hours of direct sunlight a day. Water deeply but less often - "
        "letting the soil dry slightly between waterings encourages deeper, stronger "
        "roots. Stake or cage the plant as it grows to support the fruit clusters, and "
        "pinch off side shoots (suckers) for better fruit production. Keep watering "
        "consistent once flowering starts - irregular watering during fruiting is the "
        "most common cause of blossom end rot."
    ),
    "amaranth": (
        "Tolerates partial shade well - doesn't need full-day direct sun like tomato "
        "does. Prefers consistently moist soil, so avoid letting it dry out completely "
        "between waterings. Fast-growing leafy green - can often be harvested in cuts "
        "(cut-and-come-again) within about 25-30 days. If seedlings come up densely, "
        "thin them out so remaining plants have room for proper leaf growth."
    ),
}

DEFAULT_GROWTH_TIP = "No specific growth tips available for this crop type yet."


def get_growth_tip(crop_type):
    if not crop_type:
        return None
    return GROWTH_TIPS.get(crop_type.lower(), DEFAULT_GROWTH_TIP)
