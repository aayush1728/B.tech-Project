"""
classify.py -- pure-numpy land-cover classification of one Sentinel-2 L2A scene.

No I/O here, so every rule can be unit-tested on synthetic arrays.

Inputs are surface reflectance (0-1) on a common 10 m grid:
    B02 blue, B03 green, B04 red, B08 NIR, B11 SWIR-1 (resampled from 20 m),
    SCL scene classification (resampled nearest from 20 m).

Rules (deliberately simple and explainable -- thresholds live in THRESHOLDS):
    invalid    : SCL is no-data, saturated, cloud shadow, cloud (med/high) or cirrus
    water      : NDWI  = (G - NIR) / (G + NIR)                > water_ndwi
    vegetation : NDVI  = (NIR - R) / (NIR + R)                > veg_ndvi
    bare       : NDVI < bare_ndvi_max  and  BSI > bare_bsi_min, not water
                 BSI = ((SWIR + R) - (NIR + B)) / ((SWIR + R) + (NIR + B))
    other      : everything else that is valid (mixed / built-up / sparse)

Known limitation (say it out loud in reviews): "bare" catches benches, dumps and
haul roads -- but also fallow farmland in the dry season. Persistence over
several observations (see the API's alerts) is what separates a mine from a
field that gets sown next season.
"""

from __future__ import annotations

import numpy as np

# SCL classes (ESA L2A): 0 no data, 1 saturated, 2 dark area, 3 cloud shadow, 4 vegetation,
# 5 not vegetated, 6 water, 7 unclassified, 8 cloud medium, 9 cloud high, 10 cirrus, 11 snow
SCL_INVALID = (0, 1, 3, 8, 9, 10)

THRESHOLDS = {
    "water_ndwi": 0.05,
    "veg_ndvi": 0.30,
    "bare_ndvi_max": 0.20,
    "bare_bsi_min": 0.00,
}

INVALID, OTHER, BARE, WATER, VEGETATION = 0, 1, 2, 3, 4
CLASS_NAMES = {INVALID: "invalid", OTHER: "other", BARE: "bare", WATER: "water", VEGETATION: "vegetation"}


def harmonize(dn: np.ndarray, processing_baseline: str | None) -> np.ndarray:
    """L2A digital numbers -> reflectance. Baseline 04.00+ (from 25 Jan 2022) adds a
    -1000 offset (BOA_ADD_OFFSET); ignoring it biases every index after 2022."""
    offset = -1000.0 if processing_baseline and processing_baseline >= "04.00" else 0.0
    return np.clip((dn.astype("float32") + offset) / 10000.0, 0.0, 1.5)


def _ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = (a - b) / (a + b)
    return np.where(np.isfinite(r), r, 0.0).astype("float32")


def indices(b02, b03, b04, b08, b11) -> dict[str, np.ndarray]:
    return {
        "ndvi": _ratio(b08, b04),
        "ndwi": _ratio(b03, b08),
        "bsi": _ratio(b11 + b04, b08 + b02),
    }


def classify(b02, b03, b04, b08, b11, scl, thresholds: dict | None = None) -> np.ndarray:
    """Return a uint8 class raster (INVALID/OTHER/BARE/WATER/VEGETATION)."""
    t = {**THRESHOLDS, **(thresholds or {})}
    ix = indices(b02, b03, b04, b08, b11)
    out = np.full(scl.shape, OTHER, dtype="uint8")
    water = ix["ndwi"] > t["water_ndwi"]
    veg = (ix["ndvi"] > t["veg_ndvi"]) & ~water
    bare = (ix["ndvi"] < t["bare_ndvi_max"]) & (ix["bsi"] > t["bare_bsi_min"]) & ~water
    out[bare] = BARE
    out[veg] = VEGETATION
    out[water] = WATER
    out[np.isin(scl, SCL_INVALID)] = INVALID
    return out


def class_areas_ha(classes: np.ndarray, mask: np.ndarray, pixel_area_m2: float = 100.0) -> dict:
    """Area per class (ha) inside a boolean mask, plus the valid share of the mask."""
    inside = classes[mask]
    n = inside.size
    out = {name: float((inside == code).sum() * pixel_area_m2 / 1e4)
           for code, name in CLASS_NAMES.items() if code != INVALID}
    out["valid_pct"] = float(100.0 * (inside != INVALID).sum() / n) if n else 0.0
    return out
