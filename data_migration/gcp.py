"""
gcp.py -- ground-control-point georeferencing by least squares.

The graticule method in georeference.py rests on ONE control point (where the
printed meridian and parallel cross). Any error in how that meridian was
drafted, or in the datum the drafter used, shifts the whole mine. This module
replaces it with a proper multi-point solution:

    * Control points are picked in QGIS's Georeferencer on BaseMap_0.png
      against satellite imagery and saved as a standard `.points` file.
    * A 2D similarity (4-parameter Helmert) or affine (6-parameter) transform
      from scan pixels to UTM 43N metres is fitted by least squares.
    * Per-point residuals and RMSE are reported, so bad points can be found
      and the fit can be defended with numbers.

QGIS `.points` format (one header comment, then CSV):

    #CRS: GEOGCRS["WGS 84", ...]          (or EPSG:3857, or anything pyproj reads)
    mapX,mapY,sourceX,sourceY,enable,dX,dY,residual
    72.2011,21.6702,1043.2,-1120.5,1,0,0,0

`sourceY` is negative (QGIS counts raster rows downwards as -y), which matches
how the legacy qgis2web export placed the scan.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer

TARGET_EPSG = 32643  # WGS 84 / UTM zone 43N (72-78 °E) -- metres, conformal


@dataclass
class GCP:
    px: float        # scan column
    py: float        # scan row (positive, downwards)
    E: float         # UTM 43N easting (m)
    N: float         # UTM 43N northing (m)
    label: str = ""


@dataclass
class Fit:
    model: str                 # "helmert" or "affine"
    params: np.ndarray         # helmert: [a, b, tx, ty]; affine: [a1,a2,a3,b1,b2,b3]
    residuals: np.ndarray      # (n, 2) metres, observed - fitted
    gcps: list

    @property
    def rmse(self) -> float:
        return float(np.sqrt(np.mean(np.sum(self.residuals ** 2, axis=1))))

    @property
    def scale_m_per_px(self) -> float:
        if self.model == "helmert":
            return float(math.hypot(self.params[0], self.params[1]))
        a1, a2, _, b1, b2, _ = self.params
        return float(math.sqrt(abs(a1 * b2 - a2 * b1)))

    @property
    def rotation_deg(self) -> float:
        if self.model == "helmert":
            return math.degrees(math.atan2(self.params[1], self.params[0]))
        return math.degrees(math.atan2(self.params[3], self.params[0]))

    def apply(self, px, py):
        """Scan pixel (px, py-down) -> UTM 43N (E, N)."""
        u, v = np.asarray(px, float), -np.asarray(py, float)
        p = self.params
        if self.model == "helmert":
            a, b, tx, ty = p
            return a * u - b * v + tx, b * u + a * v + ty
        a1, a2, a3, b1, b2, b3 = p
        return a1 * u + a2 * v + a3, b1 * u + b2 * v + b3

    def report(self) -> str:
        lines = [
            f"model               : {self.model} ({len(self.gcps)} GCPs, "
            f"{2 * len(self.gcps) - len(self.params)} degrees of freedom)",
            f"pixel size          : {self.scale_m_per_px:.3f} m",
            f"rotation            : {self.rotation_deg:.3f}°",
            f"RMSE                : {self.rmse:.2f} m",
            "residuals (dE, dN, |r|) m:",
        ]
        for g, (de, dn) in zip(self.gcps, self.residuals, strict=True):
            flag = "  <-- check" if math.hypot(de, dn) > 2.5 * self.rmse and self.rmse > 0 else ""
            lines.append(f"  {g.label or '':>6} {de:8.2f} {dn:8.2f} {math.hypot(de, dn):8.2f}{flag}")
        return "\n".join(lines)


def read_qgis_points(path: str | Path) -> list[GCP]:
    """Parse a QGIS Georeferencer .points file into GCPs in UTM 43N."""
    text = Path(path).read_text(encoding="utf-8").splitlines()
    src_crs = CRS.from_epsg(4326)
    rows = []
    for line in text:
        if line.startswith("#CRS:"):
            wkt = line[len("#CRS:"):].strip()
            if wkt:
                src_crs = CRS.from_user_input(wkt)
        elif line.strip() and not line.startswith("#"):
            rows.append(line)
    to_utm = Transformer.from_crs(src_crs, TARGET_EPSG, always_xy=True)
    gcps = []
    for i, r in enumerate(csv.DictReader(rows)):
        if str(r.get("enable", "1")).strip() in ("0", "false", "False"):
            continue
        E, N = to_utm.transform(float(r["mapX"]), float(r["mapY"]))
        gcps.append(GCP(px=float(r["sourceX"]), py=-float(r["sourceY"]), E=E, N=N,
                        label=str(i + 1)))
    return gcps


def fit(gcps: list[GCP], model: str = "helmert") -> Fit:
    """Least-squares fit (parametric adjustment, equal weights)."""
    if model not in ("helmert", "affine"):
        raise ValueError("model must be 'helmert' or 'affine'")
    need = 2 if model == "helmert" else 3
    if len(gcps) < need:
        raise ValueError(f"{model} needs at least {need} GCPs, got {len(gcps)}")

    u = np.array([g.px for g in gcps])
    v = -np.array([g.py for g in gcps])
    obs = np.array([[g.E, g.N] for g in gcps])
    n = len(gcps)

    if model == "helmert":
        # E = a u - b v + tx ;  N = b u + a v + ty
        A = np.zeros((2 * n, 4))
        A[0::2] = np.c_[u, -v, np.ones(n), np.zeros(n)]
        A[1::2] = np.c_[v, u, np.zeros(n), np.ones(n)]
    else:
        A = np.zeros((2 * n, 6))
        A[0::2, :3] = np.c_[u, v, np.ones(n)]
        A[1::2, 3:] = np.c_[u, v, np.ones(n)]

    L = obs.reshape(-1)
    params, *_ = np.linalg.lstsq(A, L, rcond=None)
    residuals = (L - A @ params).reshape(-1, 2)
    return Fit(model=model, params=params, residuals=residuals, gcps=gcps)


def leave_one_out(gcps: list[GCP], model: str = "helmert") -> np.ndarray:
    """Independent check error of each GCP when it is left out of the fit."""
    errs = []
    for i in range(len(gcps)):
        rest = gcps[:i] + gcps[i + 1:]
        f = fit(rest, model)
        E, N = f.apply(gcps[i].px, gcps[i].py)
        errs.append(math.hypot(gcps[i].E - E, gcps[i].N - N))
    return np.array(errs)
