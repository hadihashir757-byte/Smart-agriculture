"""
Practical care tips for each disease class the Plant Health model can detect.

This is a curated lookup, not something the classifier generates - it's a
color/texture classifier, not a text model, so it can only ever tell you
WHICH class an image matches, never WHAT to do about it. A static, reviewed
knowledge base is also more trustworthy for something like "should I destroy
this plant" than a generative guess would be.

Keyed by the model's raw class name (see feature_extraction.py / the
PlantVillage folder names), not the prettified label - so this stays in sync
with plant_health.py automatically without needing to match display strings.
"""

CARE_TIPS = {
    "Tomato___Bacterial_spot": (
        "Bacterial, spreads via water splash in humid conditions. Water at the soil "
        "line instead of overhead, remove and destroy infected leaves, avoid handling "
        "plants while wet, and consider a copper-based bactericide if it keeps spreading."
    ),
    "Tomato___Early_blight": (
        "Fungal, usually starts on older/lower leaves as target-like rings. Remove "
        "infected lower leaves, mulch to stop soil splashing onto foliage, water at the "
        "base, and rotate where you plant tomatoes next season."
    ),
    "Tomato___Late_blight": (
        "Serious and fast-spreading - can destroy a plant within days in humid weather. "
        "Remove and destroy infected plants immediately (don't compost them), avoid "
        "overhead watering, and improve air circulation. Act quickly on this one."
    ),
    "Tomato___Leaf_Mold": (
        "Common in humid, poorly-ventilated conditions (greenhouses especially). "
        "Improve airflow around the plant, reduce humidity, avoid wetting the leaves "
        "when watering, and remove infected leaves."
    ),
    "Tomato___Septoria_leaf_spot": (
        "Fungal, small spots with dark borders and gray centers, usually starting low "
        "on the plant. Remove infected lower leaves promptly, mulch the base, avoid "
        "overhead watering, and ensure good spacing/airflow between plants."
    ),
    "Tomato___Spider_mites Two-spotted_spider_mite": (
        "A pest, not a disease - tiny mites that thrive in hot, dry conditions (fine "
        "webbing may be visible). Increase humidity, rinse leaves with water regularly, "
        "and use insecticidal soap or neem oil if it's spreading."
    ),
    "Tomato___Target_Spot": (
        "Fungal, brown lesions with concentric rings similar to early blight. Remove "
        "infected leaves, improve air circulation, avoid overhead watering, and rotate "
        "crops next season."
    ),
    "Tomato___Tomato_Yellow_Leaf_Curl_Virus": (
        "Viral, spread by whiteflies - no cure once infected. Remove and destroy "
        "infected plants to stop it spreading further, control whiteflies (yellow "
        "sticky traps, insecticidal soap), and consider resistant varieties next time."
    ),
    "Tomato___Tomato_mosaic_virus": (
        "Viral, spread by contact (hands, tools) - no cure once infected. Remove and "
        "destroy infected plants, disinfect tools and hands between plants, and "
        "consider resistant varieties for your next planting."
    ),
    "Tomato___healthy": (
        "No problem detected - keep up the current watering and care routine, and "
        "keep an eye out for early signs of pests or disease."
    ),

    # Plantain/banana - from the BananaLSD dataset, raw class names are plain
    # (no "Tomato___"-style prefix since this dataset only covers one crop).
    "cordana": (
        "Fungal leaf spot (Cordana musae) - usually a secondary issue that shows up on "
        "already-stressed or physically damaged leaves rather than attacking healthy "
        "tissue directly. Remove affected leaves, avoid injuring the leaves when working "
        "around the plant, and improve overall plant nutrition/vigor - healthy plants "
        "rarely get a serious infection from this one."
    ),
    "sigatoka": (
        "One of the most serious banana diseases worldwide - causes streaking and "
        "spotting that spreads and can significantly reduce fruit yield if left "
        "unchecked. Remove and destroy severely affected leaves to cut down on spore "
        "spread, avoid overhead watering, and make sure plants have good spacing and "
        "airflow between them. Consider a fungicide if it keeps progressing - this is "
        "not a 'wait and see' disease."
    ),
    "pestalotiopsis": (
        "Fungal leaf spot/blight - grey-brown spots with dark borders, often linked to "
        "physical damage to the leaf or poor drainage around the roots. Remove affected "
        "leaves, avoid mechanical damage when handling the plant, and check that the "
        "soil isn't staying waterlogged."
    ),
    "healthy": (
        "No problem detected - keep up the current watering and care routine, and keep "
        "an eye out for early leaf spotting, which is the first sign of most banana leaf "
        "diseases."
    ),
}

DEFAULT_TIP = "No specific guidance available for this result - monitor the plant and consider consulting a local expert if symptoms worsen."


def get_care_tip(raw_class):
    if raw_class is None:
        return None
    return CARE_TIPS.get(raw_class, DEFAULT_TIP)
