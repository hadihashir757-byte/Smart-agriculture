"""
Hand-crafted image feature extraction for the Plant Health model.

No CNN is used here - this machine has no working TensorFlow/PyTorch install
(no wheels for this Python version on Intel Mac). Instead we extract classic
color + texture features that are well established in the plant-pathology
literature for leaf disease detection:

  - HSV color histograms (disease lesions shift hue/saturation vs. healthy green)
  - Green / brown-yellow / dark pixel ratios (simple, robust disease indicator)
  - GLCM texture stats (contrast, homogeneity, energy, correlation - lesions
    and spots change leaf surface texture)
  - Edge density (spotting/lesions increase high-frequency detail)

This module is imported by both the training script and the Flask inference
route, so the exact same feature vector shape is used for training and
prediction.
"""

import numpy as np
from PIL import Image
from skimage.color import rgb2gray, rgb2hsv
from skimage.feature import graycomatrix, graycoprops, canny

IMG_SIZE = (128, 128)


def load_image(path_or_file_or_image):
    img = path_or_file_or_image
    if not isinstance(img, Image.Image):
        img = Image.open(img)
    img = img.convert("RGB").resize(IMG_SIZE)
    return np.array(img)


def _color_histogram_features(hsv):
    feats = []
    for channel in range(3):  # H, S, V
        hist, _ = np.histogram(hsv[:, :, channel], bins=16, range=(0, 1))
        hist = hist / hist.sum()  # normalize so image size doesn't matter
        feats.extend(hist)
    return feats


def _color_ratio_features(hsv):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    total = h.size

    # Healthy leaf green: hue ~0.17-0.45 (roughly 60-160 degrees), decent saturation
    green_mask = (h >= 0.17) & (h <= 0.45) & (s >= 0.2)
    # Disease lesions/spots: brown/yellow hues, or very dark/necrotic patches
    brown_yellow_mask = (h >= 0.02) & (h < 0.17) & (s >= 0.15)
    dark_mask = v < 0.25

    return [
        green_mask.sum() / total,
        brown_yellow_mask.sum() / total,
        dark_mask.sum() / total,
        float(s.mean()),
        float(v.mean()),
    ]


def _texture_features(gray_uint8):
    glcm = graycomatrix(
        gray_uint8, distances=[1, 3], angles=[0, np.pi / 2],
        levels=256, symmetric=True, normed=True,
    )
    feats = []
    for prop in ("contrast", "homogeneity", "energy", "correlation"):
        feats.extend(graycoprops(glcm, prop).flatten())

    edges = canny(gray_uint8 / 255.0)
    feats.append(float(edges.mean()))  # edge density
    return feats


def extract_features(path_or_file):
    """Returns a 1D numpy feature vector for one image."""
    rgb = load_image(path_or_file)
    hsv = rgb2hsv(rgb)
    gray = (rgb2gray(rgb) * 255).astype(np.uint8)

    feats = []
    feats.extend(_color_histogram_features(hsv))   # 48
    feats.extend(_color_ratio_features(hsv))        # 5
    feats.extend(_texture_features(gray))           # 17
    return np.array(feats, dtype=np.float32)


FEATURE_VECTOR_LENGTH = len(extract_features(Image.new("RGB", IMG_SIZE, (100, 150, 80))))
