"""
make_plan_overlay.py

Warps the scanned GMDC key plan (BaseMap_0.png) into a north-up Web Mercator
image using the same georeferencing as the vector layers, so the web map can
show the original CAD plan on top of satellite imagery for visual QA.

Output (served by the frontend):
    frontend/data/plan_overlay.png    transparent where the plan is blank
    frontend/data/plan_overlay.json   {"bounds": [[south, west], [north, east]], ...}

Run it again whenever the georeferencing changes (e.g. after editing gcps.points):
    python data_migration/make_plan_overlay.py
"""

import json
from pathlib import Path

import cv2
import numpy as np
from pyproj import Transformer

from georeference import gcp_fit, pixel_to_wgs84

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "legacy" / "qgis2web_2024_06_21-05_06_59_990280" / "data" / "BaseMap_0.png"
OUT_DIR = ROOT / "frontend" / "data"
TARGET_RES_M = 4.0  # output pixel size in EPSG:3857 metres


def main():
    img = cv2.imread(str(SCAN), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    to_merc = Transformer.from_crs(4326, 3857, always_xy=True)

    # Sample the (very nearly affine) pixel -> 3857 mapping and fit an affine.
    px, py = np.meshgrid(np.linspace(0, w, 15), np.linspace(0, h, 12))
    px, py = px.ravel(), py.ravel()
    merc = np.array([to_merc.transform(*pixel_to_wgs84(x, y)) for x, y in zip(px, py, strict=True)])
    A = np.c_[px, py, np.ones_like(px)]
    coef, *_ = np.linalg.lstsq(A, merc, rcond=None)          # (3, 2)
    fit_err = np.abs(A @ coef - merc).max()

    # Output canvas in 3857
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]]) @ coef
    minx, miny = corners.min(0)
    maxx, maxy = corners.max(0)
    W, H = int((maxx - minx) / TARGET_RES_M), int((maxy - miny) / TARGET_RES_M)

    # canvas pixel (cx, cy) -> 3857 -> scan pixel ; build the forward 2x3 for warpAffine
    # scan->canvas: cx = (X - minx)/res ; cy = (maxy - Y)/res
    M = np.array([
        [coef[0, 0] / TARGET_RES_M, coef[1, 0] / TARGET_RES_M, (coef[2, 0] - minx) / TARGET_RES_M],
        [-coef[0, 1] / TARGET_RES_M, -coef[1, 1] / TARGET_RES_M, (maxy - coef[2, 1]) / TARGET_RES_M],
    ])
    warped = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_AREA,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    alpha = np.where(warped.min(axis=2) > 235, 0, 255).astype(np.uint8)
    rgba = np.dstack([warped, alpha])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT_DIR / "plan_overlay.png"), rgba, [cv2.IMWRITE_PNG_COMPRESSION, 9])

    to_ll = Transformer.from_crs(3857, 4326, always_xy=True)
    west, south = to_ll.transform(minx, miny)
    east, north = to_ll.transform(maxx, maxy)
    meta = {
        "bounds": [[south, west], [north, east]],
        "method": "gcp" if gcp_fit() is not None else "graticule",
        "affine_fit_max_error_m": round(float(fit_err), 3),
        "pixel_size_m": TARGET_RES_M,
    }
    (OUT_DIR / "plan_overlay.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {OUT_DIR / 'plan_overlay.png'} ({W}x{H}), method={meta['method']}, "
          f"affine approx error {fit_err:.3f} m")


if __name__ == "__main__":
    main()
