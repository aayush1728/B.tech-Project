"""Unit tests for the georeferencing transform (no database needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data_migration"))

from pyproj import Transformer  # noqa: E402

from georeference import _fit, legacy_to_mine_grid, legacy_to_wgs84  # noqa: E402

TO_LEGACY = Transformer.from_crs("EPSG:3857", "OGC:CRS84", always_xy=True)


def pixel_to_legacy(px, py):
    """What qgis2web did: pixel (px, py) -> 'CRS84' via inverse Web Mercator."""
    return TO_LEGACY.transform(px, -py)


def test_grid_fit_is_tight():
    f = _fit()
    assert f["rmse_px_to_grid_m"] < 2.0
    assert 3.3 < f["kx"] < 3.5  # metres per pixel


def test_grid_tick_maps_to_printed_value():
    # vertical line at px 1086 is the printed "7200 E"; row 1021 is "17600 N"
    E, N = legacy_to_mine_grid(*pixel_to_legacy(1086, 1021))
    assert abs(E - 7200) < 3 and abs(N - 17600) < 3


def test_control_point_maps_to_graticule_intersection():
    f = _fit()
    lon, lat = legacy_to_wgs84(*pixel_to_legacy(*f["cp_pixel"]))
    exp_lon, exp_lat = f["cp_lonlat"]
    assert abs(lon - exp_lon) < 1e-7 and abs(lat - exp_lat) < 1e-7


def test_rotation_preserves_distance():
    """Similarity transform at scale 1: 800 m on the mine grid stays ~800 m."""
    geod = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True)
    a = geod.transform(*legacy_to_wgs84(*pixel_to_legacy(377, 549)))   # 4800 E, 19200 N
    b = geod.transform(*legacy_to_wgs84(*pixel_to_legacy(614, 549)))   # 5600 E, 19200 N
    d = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    assert abs(d - 800) < 5
