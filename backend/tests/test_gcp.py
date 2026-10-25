"""Unit tests for the GCP least-squares georeferencing (no database needed)."""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data_migration"))

from gcp import GCP, fit, leave_one_out, read_qgis_points  # noqa: E402

# A known "truth": 3.386 m/px, rotated 4.8°, offset into UTM 43N
SCALE, THETA, TX, TY = 3.386, math.radians(4.8), 220_000.0, 2_400_000.0


def truth(px, py):
    a, b = SCALE * math.cos(THETA), SCALE * math.sin(THETA)
    u, v = px, -py
    return a * u - b * v + TX, b * u + a * v + TY


def make_gcps(noise_m=0.0, seed=0):
    rng = np.random.default_rng(seed)
    pts = [(200, 150), (3300, 220), (1800, 1300), (400, 2400), (3200, 2500), (1000, 700)]
    out = []
    for i, (px, py) in enumerate(pts):
        E, N = truth(px, py)
        dE, dN = rng.normal(0, noise_m, 2) if noise_m else (0, 0)
        out.append(GCP(px, py, E + dE, N + dN, str(i + 1)))
    return out


@pytest.mark.parametrize("model", ["helmert", "affine"])
def test_exact_points_recover_transform(model):
    f = fit(make_gcps(), model)
    assert f.rmse < 1e-6
    assert abs(f.scale_m_per_px - SCALE) < 1e-6
    assert abs(f.rotation_deg - 4.8) < 1e-6
    E, N = f.apply(2500, 900)
    tE, tN = truth(2500, 900)
    assert abs(E - tE) < 1e-6 and abs(N - tN) < 1e-6


def test_noisy_points_give_sensible_rmse():
    f = fit(make_gcps(noise_m=5.0), "helmert")
    assert 1 < f.rmse < 15
    assert abs(f.scale_m_per_px - SCALE) < 0.01


def test_blunder_stands_out_in_leave_one_out():
    g = make_gcps(noise_m=2.0)
    g[2] = GCP(g[2].px, g[2].py, g[2].E + 150, g[2].N, g[2].label)  # a 150 m mis-click
    errs = leave_one_out(g, "helmert")
    assert int(np.argmax(errs)) == 2 and errs[2] > 100


def test_too_few_points():
    with pytest.raises(ValueError):
        fit(make_gcps()[:2], "affine")


def test_read_qgis_points(tmp_path):
    p = tmp_path / "gcps.points"
    p.write_text(
        '#CRS: GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],'
        'CS[ellipsoidal,2],AXIS["latitude",north],AXIS["longitude",east],ANGLEUNIT["degree",0.0174532925199433],'
        'ID["EPSG",4326]]\n'
        "mapX,mapY,sourceX,sourceY,enable,dX,dY,residual\n"
        "72.20,21.67,100.5,-200.25,1,0,0,0\n"
        "72.25,21.63,3000,-2400,0,0,0,0\n"
    )
    g = read_qgis_points(p)
    assert len(g) == 1                      # disabled point skipped
    assert g[0].px == 100.5 and g[0].py == 200.25
    assert 200_000 < g[0].E < 300_000 and 2_390_000 < g[0].N < 2_410_000
