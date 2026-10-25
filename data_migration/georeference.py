"""
georeference.py

Recovers true WGS84 coordinates for the legacy qgis2web layers.

THE BUG
-------
The B.Tech QGIS project digitised layers on top of a scanned GMDC key plan
(BaseMap_0.png) that was never georeferenced. QGIS placed the raster at the
default identity geotransform -- origin (0, 0), 1 unit per pixel -- in
EPSG:3857. qgis2web then exported every vertex to CRS84, so each "lon/lat"
in the .js files is really a *pixel position* run through the inverse Web
Mercator formula. That's why everything sits a few km from 0°N 0°E, in the
Gulf of Guinea.

The CRS label ("CRS84") wasn't wrong in the usual sense -- there simply was no
real-world CRS behind the numbers. A set_crs()/to_crs() call can't fix that;
it needs a georeferencing transform.

THE FIX (three exact, documented steps)
--------------------------------------
1. Undo the export:     CRS84 -> EPSG:3857 metres  == (px, -py) pixel coords
2. Pixel -> mine grid:  affine fitted by least squares to the plan's printed
                        800 m grid ticks (4000 E ... 14400 E, 12800 N ... 20800 N)
3. Mine grid -> WGS84:  the plan also prints one meridian (72°11'38.4" E) and
                        one parallel (21°40'56.8" N). Their intersection is a
                        true control point; their slope gives the rotation
                        between mine-grid north and true north. With scale
                        fixed at 1 (the grid is in metres -- confirmed by the
                        plan's 1:20 000 scale bar), that fully defines a 2D
                        similarity (Helmert) transform into a local tangent
                        plane centred on the control point, which pyproj
                        projects back to lat/lon.

All measured inputs live in CONTROL below so they can be audited / refined
(e.g. with extra Google Earth checkpoints) without touching the maths.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from pyproj import Transformer

# --------------------------------------------------------------------------
# Measured from BaseMap_0.png (3544 x 2658 px) -- see README "CRS postmortem"
# --------------------------------------------------------------------------
CONTROL = {
    # pixel column of each vertical grid line -> mine-grid Easting (m)
    "grid_cols": {
        141: 4000, 377: 4800, 614: 5600, 850: 6400, 1086: 7200, 1322: 8000,
        1559: 8800, 1795: 9600, 2031: 10400, 2267: 11200, 2504: 12000,
        2740: 12800, 2976: 13600, 3212: 14400,
    },
    # pixel row of each horizontal grid line -> mine-grid Northing (m)
    # (16000 N is hidden under map detail, so it is not used)
    "grid_rows": {
        76: 20800, 312: 20000, 549: 19200, 785: 18400, 1021: 17600,
        1257: 16800, 1730: 15200, 1966: 14400, 2202: 13600, 2438: 12800,
    },
    # graticule lines printed on the plan, fitted in pixel space:
    #   meridian  x = mx0 + ms * y      (72°11'38.4" E)
    #   parallel  y = py0 + ps * x      (21°40'56.8" N)
    "meridian": {"x0": 649.0, "slope": 0.0837, "lon_dms": (72, 11, 38.4)},
    "parallel": {"y0": 320.0, "slope": -0.0842, "lat_dms": (21, 40, 56.8)},
}


def dms_to_deg(d: float, m: float, s: float) -> float:
    return d + m / 60.0 + s / 3600.0


@lru_cache(maxsize=1)
def _fit():
    """Fit pixel->grid scale/offset and derive the control point + rotation."""
    cols = np.array(list(CONTROL["grid_cols"].items()), dtype=float)
    rows = np.array(list(CONTROL["grid_rows"].items()), dtype=float)

    # E = e0 + kx * px ;  N = n0 + ky * py   (least squares, 1D each)
    kx, e0 = np.polyfit(cols[:, 0], cols[:, 1], 1)
    ky, n0 = np.polyfit(rows[:, 0], rows[:, 1], 1)
    res_e = cols[:, 1] - (e0 + kx * cols[:, 0])
    res_n = rows[:, 1] - (n0 + ky * rows[:, 0])

    # intersection of meridian and parallel, in pixels
    m, p = CONTROL["meridian"], CONTROL["parallel"]
    cx = (m["x0"] + m["slope"] * p["y0"]) / (1 - m["slope"] * p["slope"])
    cy = p["y0"] + p["slope"] * cx
    cp_E, cp_N = e0 + kx * cx, n0 + ky * cy

    # rotation: true north is rotated theta toward grid-west.
    # Average the two independent estimates (meridian & parallel).
    theta = 0.5 * (math.atan(m["slope"]) + math.atan(-p["slope"]))

    return {
        "kx": kx, "e0": e0, "ky": ky, "n0": n0,
        "rmse_px_to_grid_m": float(np.sqrt(np.mean(np.r_[res_e, res_n] ** 2))),
        "cp_pixel": (cx, cy), "cp_grid": (cp_E, cp_N),
        "cp_lonlat": (dms_to_deg(*m["lon_dms"]), dms_to_deg(*p["lat_dms"])),
        "theta_deg": math.degrees(theta),
    }


@lru_cache(maxsize=1)
def _transformers():
    f = _fit()
    lon0, lat0 = f["cp_lonlat"]
    # Undo qgis2web's export (CRS84 -> 3857 metres == pixels)
    undo = Transformer.from_crs("OGC:CRS84", "EPSG:3857", always_xy=True)
    # Local tangent plane (true east/north metres) centred on the control point
    local = Transformer.from_crs(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m",
        "EPSG:4326", always_xy=True,
    )
    return undo, local


# --------------------------------------------------------------------------
# Optional: least-squares GCP solution (see gcp.py). If GCP_FILE exists it
# replaces the single-point graticule method for steps 2-3.
# --------------------------------------------------------------------------
GCP_FILE = Path(__file__).resolve().parent / "gcps.points"
GCP_MODEL = os.getenv("GCP_MODEL", "helmert")  # or "affine"


@lru_cache(maxsize=1)
def gcp_fit():
    """The GCP least-squares fit, or None when no .points file is present."""
    if not GCP_FILE.exists():
        return None
    from gcp import fit, read_qgis_points
    return fit(read_qgis_points(GCP_FILE), GCP_MODEL)


@lru_cache(maxsize=1)
def _utm_to_wgs84():
    from gcp import TARGET_EPSG
    return Transformer.from_crs(TARGET_EPSG, 4326, always_xy=True)


def pixel_to_wgs84(px: float, py: float) -> tuple[float, float]:
    """Scan pixel (column, row-down) -> true (lon, lat) WGS84."""
    g = gcp_fit()
    if g is not None:
        E, N = g.apply(px, py)
        return _utm_to_wgs84().transform(float(E), float(N))

    f = _fit()
    _, local = _transformers()
    E = f["e0"] + f["kx"] * px
    N = f["n0"] + f["ky"] * py
    dE, dN = E - f["cp_grid"][0], N - f["cp_grid"][1]
    t = math.radians(f["theta_deg"])
    east = dE * math.cos(t) + dN * math.sin(t)
    north = -dE * math.sin(t) + dN * math.cos(t)
    return local.transform(east, north)


def legacy_to_pixel(lon: float, lat: float) -> tuple[float, float]:
    """Undo qgis2web's export: fake CRS84 degrees -> scan pixel (col, row-down)."""
    undo, _ = _transformers()
    mx, my = undo.transform(lon, lat)
    return mx, -my


def legacy_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """Convert one legacy (fake CRS84) vertex to true (lon, lat) WGS84."""
    return pixel_to_wgs84(*legacy_to_pixel(lon, lat))


def _graticule_legacy_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """The original single-control-point method (kept for comparison)."""
    f = _fit()
    undo, local = _transformers()

    # 1. legacy degrees -> pixel coordinates
    mx, my = undo.transform(lon, lat)
    px, py = mx, -my

    # 2. pixel -> mine grid (m)
    E = f["e0"] + f["kx"] * px
    N = f["n0"] + f["ky"] * py

    # 3. mine grid -> true local east/north about the control point
    dE, dN = E - f["cp_grid"][0], N - f["cp_grid"][1]
    t = math.radians(f["theta_deg"])
    east = dE * math.cos(t) + dN * math.sin(t)
    north = -dE * math.sin(t) + dN * math.cos(t)

    return local.transform(east, north)


def legacy_to_mine_grid(lon: float, lat: float) -> tuple[float, float]:
    """Legacy vertex -> GMDC mine-grid (E, N) in metres (handy for checks)."""
    f = _fit()
    undo, _ = _transformers()
    mx, my = undo.transform(lon, lat)
    return f["e0"] + f["kx"] * mx, f["n0"] + f["ky"] * (-my)


def _map_coords(coords, fn):
    if isinstance(coords[0], (int, float)):
        return list(fn(coords[0], coords[1]))
    return [_map_coords(c, fn) for c in coords]


def georeference_geojson(fc: dict) -> dict:
    """Return a copy of a FeatureCollection with every vertex georeferenced."""
    out = {k: v for k, v in fc.items() if k != "features"}
    out["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}
    out["features"] = []
    for feat in fc["features"]:
        g = feat.get("geometry")
        new = dict(feat)
        if g is not None:
            new["geometry"] = {"type": g["type"],
                               "coordinates": _map_coords(g["coordinates"], legacy_to_wgs84)}
        out["features"].append(new)
    return out


def report() -> str:
    g = gcp_fit()
    if g is not None:
        return f"method              : GCP least squares ({GCP_FILE.name})\n" + g.report()
    f = _fit()
    lon, lat = f["cp_lonlat"]
    return (
        f"pixel size          : {f['kx']:.4f} m (E) / {-f['ky']:.4f} m (N)\n"
        f"grid-fit RMSE       : {f['rmse_px_to_grid_m']:.2f} m\n"
        f"control point pixel : ({f['cp_pixel'][0]:.1f}, {f['cp_pixel'][1]:.1f})\n"
        f"control point grid  : E {f['cp_grid'][0]:.1f}, N {f['cp_grid'][1]:.1f}\n"
        f"control point WGS84 : {lat:.6f} N, {lon:.6f} E\n"
        f"grid rotation       : true north is {f['theta_deg']:.2f}° west of grid north\n"
        f"method              : single graticule control point (add gcps.points to refine)"
    )


if __name__ == "__main__":
    print(report())
