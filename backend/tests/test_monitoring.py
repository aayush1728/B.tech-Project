"""
Monitoring pipeline tests, end to end except for the network.

`read_scene` (the only function that touches Planetary Computer) is replaced
by a synthetic scene drawn over the real lease and pit polygons, so the rest of
the chain -- harmonise, classify, mask, area, vectorise, save, API -- runs for real.
Synthetic observations use 1999 dates and are removed afterwards.
"""

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import psycopg2
import pytest
from fastapi.testclient import TestClient
from shapely import affinity

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from monitoring import s2_monitor as s2  # noqa: E402
from monitoring.classify import (  # noqa: E402
    BARE,
    INVALID,
    VEGETATION,
    WATER,
    class_areas_ha,
    classify,
    harmonize,
)

client = TestClient(app)


# ---------------------------------------------------------------- pure functions
def test_harmonize_applies_2022_offset():
    dn = np.array([1500], dtype="uint16")
    assert harmonize(dn, "03.01")[0] == pytest.approx(0.15)
    assert harmonize(dn, "05.10")[0] == pytest.approx(0.05)


def spectra(kind, n=4):
    """Reflectance tuples (B02, B03, B04, B08, B11) typical of each cover."""
    v = {
        "veg":   (0.03, 0.06, 0.04, 0.40, 0.20),
        "bare":  (0.15, 0.20, 0.25, 0.28, 0.40),
        "water": (0.06, 0.08, 0.05, 0.03, 0.01),
    }[kind]
    return [np.full(n, x, dtype="float32") for x in v]


@pytest.mark.parametrize("kind,code", [("veg", VEGETATION), ("bare", BARE), ("water", WATER)])
def test_classify_typical_spectra(kind, code):
    scl = np.full(4, 4, dtype="uint8")
    assert (classify(*spectra(kind), scl) == code).all()


def test_clouds_are_invalid():
    scl = np.array([8, 9, 10, 3], dtype="uint8")
    assert (classify(*spectra("bare"), scl) == INVALID).all()


def test_class_areas_and_valid_share():
    classes = np.array([BARE, BARE, WATER, INVALID], dtype="uint8")
    a = class_areas_ha(classes, np.ones(4, bool))
    assert a["bare"] == pytest.approx(0.02) and a["water"] == pytest.approx(0.01)
    assert a["valid_pct"] == pytest.approx(75.0)


def test_month_range_and_step():
    ms = list(s2.month_range("2023-11", "2024-04", 2))
    assert ms == [dt.date(2023, 11, 1), dt.date(2024, 1, 1), dt.date(2024, 3, 1)]
    assert s2.month_end(dt.date(2024, 2, 1)) == dt.date(2024, 2, 29)


def test_pick_candidates_keeps_tile_dedupes_and_sorts():
    items = [
        SimpleNamespace(id="S2A_20240317_T43QBE_20240317", properties={"s2:mgrs_tile": "43QBE", "eo:cloud_cover": 0.5, "datetime": "2024-03-17T05:36:41Z"}),
        SimpleNamespace(id="S2A_20240317_T43QBE_20240319", properties={"s2:mgrs_tile": "43QBE", "eo:cloud_cover": 0.5, "datetime": "2024-03-17T05:36:41Z"}),
        SimpleNamespace(id="S2B_20240322_T43QBE_20240322", properties={"s2:mgrs_tile": "43QBE", "eo:cloud_cover": 0.1, "datetime": "2024-03-22T05:36:39Z"}),
        SimpleNamespace(id="S2B_20240322_T42QZK_20240322", properties={"s2:mgrs_tile": "42QZK", "eo:cloud_cover": 0.0, "datetime": "2024-03-22T05:36:39Z"}),
    ]
    got = s2.pick_candidates(items)
    assert [g.id for g in got] == ["S2B_20240322_T43QBE_20240322", "S2A_20240317_T43QBE_20240319"]


# ---------------------------------------------------------------- full pipeline, no network
def synthetic_reader(lease, pit, pit_scale=1.0, cloud=False):
    """A fake read_scene: vegetation everywhere, the pit bare, a lake, optional cloud."""
    from pyproj import Transformer
    from shapely.ops import transform as T

    to_utm = Transformer.from_crs(4326, s2.SCENE_EPSG, always_xy=True).transform
    pit_u = affinity.scale(T(to_utm, pit), pit_scale, pit_scale)
    lake_u = T(to_utm, lease).centroid.buffer(150)

    def read(item, aoi_u):
        from rasterio.transform import from_origin
        minx, miny, maxx, maxy = aoi_u.bounds
        x0, y1 = np.floor(minx / 10) * 10, np.ceil(maxy / 10) * 10
        w, h = int(np.ceil((maxx - x0) / 10)), int(np.ceil((y1 - miny) / 10))
        grid = from_origin(x0, y1, 10, 10)
        m_pit = s2.rasterize(pit_u, (h, w), grid)
        m_lake = s2.rasterize(lake_u, (h, w), grid)
        dn = lambda v: np.uint16(v * 10000)  # noqa: E731  (baseline 03.xx: no offset)
        cover = {"veg": (0.03, 0.06, 0.04, 0.40, 0.20), "bare": (0.15, 0.20, 0.25, 0.28, 0.40),
                 "water": (0.06, 0.08, 0.05, 0.03, 0.01)}
        bands = {b: np.full((h, w), dn(cover["veg"][i]), dtype="uint16")
                 for i, b in enumerate(("B02", "B03", "B04", "B08", "B11"))}
        for i, b in enumerate(("B02", "B03", "B04", "B08", "B11")):
            bands[b][m_pit] = dn(cover["bare"][i])
            bands[b][m_lake] = dn(cover["water"][i])
        scl = np.full((h, w), 4, dtype="uint8")
        if cloud:
            scl[:, : int(w * 0.5)] = 9
        bands["SCL"] = scl
        return bands, grid
    return read


def item(date):
    return SimpleNamespace(id=f"SYNTH_{date}", properties={
        "datetime": f"{date}T05:36:41Z", "s2:mgrs_tile": "43QBE",
        "eo:cloud_cover": 1.0, "s2:processing_baseline": "03.01"})


@pytest.fixture(scope="module")
def conn():
    c = psycopg2.connect(db.DATABASE_URL)
    cur = c.cursor()
    cur.execute(s2.DDL)
    c.commit()
    yield c
    cur.execute("DELETE FROM s2_observations WHERE obs_date < '2000-01-01'")
    c.commit()
    c.close()


def test_cloudy_scene_is_rejected(conn, monkeypatch):
    lease, pit = s2.load_zones(conn.cursor())
    monkeypatch.setattr(s2, "read_scene", synthetic_reader(lease, pit, cloud=True))
    assert s2.process_scene(item("1999-01-15"), lease, pit) is None


def test_pipeline_measures_pit_and_change(conn, monkeypatch):
    cur = conn.cursor()
    lease, pit = s2.load_zones(cur)
    cur.execute("SELECT ST_Area(ST_Intersection(p.geom, l.geom)::geography) / 1e4 "
                "FROM ultimate_pit_limit p, lease_boundary l")
    pit_ha = cur.fetchone()[0]

    # month 1: the pit at 80% of its linear size; month 2: the full pit
    for date, scale in (("1999-02-15", 0.8), ("1999-03-15", 1.0)):
        monkeypatch.setattr(s2, "read_scene", synthetic_reader(lease, pit, pit_scale=scale))
        row, fps = s2.process_scene(item(date), lease, pit)
        s2.save(cur, row, fps)
    conn.commit()

    ts = client.get("/api/monitoring/timeseries").json()
    synth = [r for r in ts["observations"] if r["obs_date"] < "2000-01-01"]
    assert len(synth) == 2
    full = synth[1]
    # bare inside the pit = the pit (minus the lake), to within rasterisation error
    assert abs(full["pit_bare_ha"] - pit_ha) / pit_ha < 0.05
    assert full["lease_water_ha"] == pytest.approx(7.07, rel=0.15)  # 150 m-radius lake
    assert synth[0]["pit_bare_ha"] < full["pit_bare_ha"]

    ch = client.get("/api/monitoring/change", params={"from": "1999-02-15", "to": "1999-03-15"}).json()
    assert ch["new_bare_ha"] > 0.2 * pit_ha        # 1 - 0.8^2 = 36% of the pit is new
    assert ch["features"]

    fp = client.get("/api/monitoring/footprint", params={"date": "1999-03-15"}).json()
    assert {f["properties"]["class"] for f in fp["features"]} == {"bare", "water", "vegetation"}

    al = client.get("/api/monitoring/alerts", params={"persist": 2}).json()
    assert al["persist"] == 2 and "features" in al
