"""
Quality gate for incoming plant photos: rejects blurry or badly-exposed
images before they ever reach the classifier, so a bad photo produces "no
new result" instead of a confident-looking wrong diagnosis.

Design note (why there's no explicit "5 photo batch" logic here): the
Android side posts one photo per minute to /upload-image. Rather than
buffering 5 photos and picking the best every 5 minutes, each upload is
judged independently right here - if it passes, it becomes the new
"latest accepted prediction"; if it fails, nothing is stored and the
previous good prediction just stays the latest one (see
db.latest_accepted_plant_health()). That gives the same practical outcome
("skip bad photos, keep last known good, don't go stale for long") with
less moving parts than windowed batching, and can update as often as every
minute instead of waiting a fixed 5.
"""

import numpy as np
from scipy.ndimage import laplace
from skimage.color import rgb2gray

BLUR_VARIANCE_THRESHOLD = 100.0   # below this = too blurry (classic "variance of Laplacian" test)
MIN_MEAN_BRIGHTNESS = 0.15        # below this (0-1 scale) = too dark
MAX_MEAN_BRIGHTNESS = 0.92        # above this = blown out / overexposed


def check_quality(rgb_image_array):
    """
    rgb_image_array: HxWx3 numpy array, values 0-255.
    Returns (ok: bool, reason: str, metrics: dict)
    """
    gray = rgb2gray(rgb_image_array)  # 0-1 float, used for brightness
    gray_255 = gray * 255.0           # Laplacian variance is calibrated for a 0-255 range

    blur_score = float(laplace(gray_255).var())
    brightness = float(gray.mean())

    metrics = {"blur_score": blur_score, "brightness": brightness}

    if blur_score < BLUR_VARIANCE_THRESHOLD:
        return False, f"too blurry (score={blur_score:.1f}, need >={BLUR_VARIANCE_THRESHOLD})", metrics

    if brightness < MIN_MEAN_BRIGHTNESS:
        return False, f"too dark (brightness={brightness:.2f})", metrics

    if brightness > MAX_MEAN_BRIGHTNESS:
        return False, f"overexposed (brightness={brightness:.2f})", metrics

    return True, "ok", metrics
