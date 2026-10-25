"""
Integration tests: run against a real PostGIS loaded by
data_migration/load_data.py (CI does this in a service container).

The geographic assertions double as regression tests for the CRS fix --
if georeferencing breaks, the layers land back near 0°N 0°E and these fail.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Published lease area (GEM / GMDC): ~3,672 ha
PUBLISHED_LEASE_HA = 3672


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_layers_catalogue():
    layers = {lyr["name"]: lyr for lyr in client.get("/api/layers").json()}
    assert set(layers) == {
        "contours", "lease_boundary", "lignite_boundary",
        "ultimate_pit_limit", "lakes", "canals",
    }
    assert sum(lyr["feature_count"] for lyr in layers.values()) == 78


@pytest.mark.parametrize("name", ["lease_boundary", "contours", "lakes"])
def test_layers_are_georeferenced_to_bhavnagar(name):
    """Every vertex must sit in Bhavnagar district, Gujarat -- not off Africa."""
    layer = next(lyr for lyr in client.get("/api/layers").json() if lyr["name"] == name)
    minx, miny, maxx, maxy = layer["bbox"]
    assert 72.1 < minx < maxx < 72.35
    assert 21.55 < miny < maxy < 21.75


def test_lease_area_matches_published_value():
    s = client.get("/api/stats/summary").json()
    assert abs(float(s["lease_area_ha"]) - PUBLISHED_LEASE_HA) / PUBLISHED_LEASE_HA < 0.03


def test_pit_is_inside_lease():
    s = client.get("/api/stats/summary").json()
    assert float(s["pit_outside_lease_ha"]) < 0.1


def test_layer_geojson_and_bbox_filter():
    full = client.get("/api/layers/contours")
    assert full.headers["content-type"].startswith("application/geo+json")
    n_full = len(full.json()["features"])
    part = client.get("/api/layers/contours", params={"bbox": "72.19,21.64,72.21,21.66"})
    assert 0 < len(part.json()["features"]) < n_full


def test_unknown_layer_and_bad_bbox():
    assert client.get("/api/layers/users;DROP TABLE x").status_code == 404
    assert client.get("/api/layers/contours", params={"bbox": "1,2"}).status_code == 422
    assert client.get("/api/layers/contours", params={"bbox": "5,5,1,1"}).status_code == 422


def test_point_inside_pit():
    r = client.get("/api/analysis/point", params={"lon": 72.21, "lat": 21.66}).json()
    assert r["in_lease"] and r["in_pit"]
    assert r["nearest_contour"]["elevation_m"] is not None


def test_point_far_away_is_outside_lease():
    r = client.get("/api/analysis/point", params={"lon": 72.5, "lat": 21.9}).json()
    assert not r["in_lease"] and r["distance_to_lease_boundary_m"] > 10_000


def test_water_setback_grows_with_buffer():
    small = client.get("/api/analysis/water-setback", params={"buffer_m": 1}).json()
    big = client.get("/api/analysis/water-setback", params={"buffer_m": 2000}).json()
    assert small["violations"] <= big["violations"]


def test_lignite_outside_lease():
    fc = client.get("/api/analysis/lignite-outside-lease").json()
    total = sum(f["properties"]["area_ha"] for f in fc["features"])
    s = client.get("/api/stats/summary").json()
    assert abs(total - float(s["lignite_outside_lease_ha"])) < 1
