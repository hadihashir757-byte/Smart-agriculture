"""
Growth-stage estimation: seedling vs. mature.

There's no labeled dataset for this (PlantVillage only labels disease, not
plant age), so this isn't a trained classifier - it's a computer-vision
heuristic that works specifically because the camera stays in a fixed
position: as the plant grows, it physically covers more of each photo's
frame. We track the "plant pixel ratio" (green + healthy-leaf-colored
pixels / total pixels) against a stored baseline from the very first
accepted photo, and call it "mature" once that ratio has grown enough.

This assumes:
  - The camera doesn't move/zoom between shots (a moved camera changes
    frame coverage for reasons that have nothing to do with growth).
  - The first photo is taken close to planting time, so it's a genuine
    "seedling" baseline, not itself a mature plant.

If the camera gets bumped or repositioned, call reset_growth_baseline()
to start tracking over from the new framing.
"""

import numpy as np
from skimage.color import rgb2hsv

# Once the plant covers this many times more of the frame than the
# seedling baseline did, we call it "mature". 2x is a conservative,
# easily-observable jump in a home setup - tune based on what you actually
# see logged once you have a few weeks of real photos.
GROWTH_MATURE_RATIO_MULTIPLIER = 2.0


def plant_pixel_ratio(rgb_image_array):
    """Fraction (0-1) of the frame that looks like live plant material.

    Deliberately GREEN ONLY, not the wider green+brown/yellow range used for
    disease detection in feature_extraction.py - brown/yellow also matches
    soil, mulch, and pots, which would make a plain background register as
    'plant' and defeat the whole point of measuring frame coverage. Green is
    a much more specific signal for live leaf tissue."""
    hsv = rgb2hsv(rgb_image_array)
    h, s = hsv[:, :, 0], hsv[:, :, 1]

    green_mask = (h >= 0.17) & (h <= 0.45) & (s >= 0.2)

    return float(green_mask.sum() / green_mask.size)


def estimate_growth_stage(current_ratio, baseline_ratio):
    """Returns 'seedling' or 'mature'."""
    if baseline_ratio <= 0:
        return "seedling"
    return "mature" if current_ratio >= baseline_ratio * GROWTH_MATURE_RATIO_MULTIPLIER else "seedling"
