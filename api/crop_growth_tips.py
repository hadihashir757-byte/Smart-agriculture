"""
General growth/care tips per crop type - independent of disease diagnosis.

Unlike plant_care_tips.py (which only applies once a specific disease has
been diagnosed, and only exists for crops with a trained model), this is
just practical growing advice for whichever crop is currently selected (see
the V11/V12 switches on the Arduino) - useful even when nothing is wrong
with the plant, and doesn't depend on any trained model at all.
"""

GROWTH_TIPS = {
    "tomato": (
        "Summer crop: less water, more sunlight. Needs 6-8 hours of direct sunlight a "
        "day. Water deeply but less often - letting the soil dry slightly between "
        "waterings encourages deeper, stronger roots. Stake or cage the plant as it "
        "grows to support the fruit clusters, and pinch off side shoots (suckers) for "
        "better fruit production. Keep watering consistent once flowering starts - "
        "irregular watering during fruiting is the most common cause of blossom end rot."
    ),
    "plantain": (
        "Winter crop: more water. One honest correction to the usual 'less sunlight' "
        "framing - plantain actually prefers full sun, same as tomato; what sets it "
        "apart is its heavy, constant water need, not shade tolerance. Keep soil "
        "consistently moist - never let it dry out, unlike tomato. Remove dried/dead "
        "leaves regularly, and keep suckers (side shoots at the base) thinned to one or "
        "two at a time so the main plant isn't competing with its own offshoots for "
        "water and nutrients."
    ),
}

DEFAULT_GROWTH_TIP = "No specific growth tips available for this crop type yet."


def get_growth_tip(crop_type):
    if not crop_type:
        return None
    return GROWTH_TIPS.get(crop_type.lower(), DEFAULT_GROWTH_TIP)
