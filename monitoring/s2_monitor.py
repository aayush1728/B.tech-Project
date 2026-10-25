"""
s2_monitor.py -- monthly Sentinel-2 monitoring of the Surkha (North) lease.

For each month it:
  1. searches Microsoft Planetary Computer's STAC for Sentinel-2 L2A scenes on
     MGRS tile 43QBE (the lease sits wholly inside it), least cloudy first;
  2. reads only the window around the lease (+1 km) from the cloud-optimised
     GeoTIFFs -- a few MB per band instead of the 1 GB tile;
  3. classifies every 10 m pixel (classify.py) and rejects the scene if under
     MIN_VALID_PCT of the lease is cloud-free;
  4. stores areas per zone in `s2_observations` and the bare/water footprints as
     polygons in `s2_footprint`, so change and persistence are plain PostGIS SQL.

Usage:
    python -m monitoring.s2_monitor --start 2019-01 --end 2026-10            # backfill
    python -m monitoring.s2_monitor --start 2019-01 --end 2026-10 --step 3   # quarterly, faster
    python -m monitoring.s2_monitor --latest                                # last full month (cron)

Months already stored are skipped unless --force.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

from monitoring.classify import (
    BARE,
    VEGETATION,
    WATER,
    class_areas_ha,
    classify,
    harmonize,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
log = logging.getLogger("s2_monitor")

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
MGRS_TILE = "43QBE"
SCENE_EPSG = 32643
AOI_BUFFER_M = 1000.0
MAX_SCENE_CLOUD = 40.0      # scene-level filter before reading pixels
MIN_VALID_PCT = 90.0        # lease-level filter after cloud masking
MAX_CANDIDATES = 4          # scenes tried per month
MIN_POLYGON_PIXELS = 50     # 0.5 ha: drop speckle before vectorising
BANDS = ("B02", "B03", "B04", "B08", "B11", "SCL")

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://webgis:webgis_dev_password@localhost:5432/mine_monitoring")

DDL = """
CREATE TABLE IF NOT EXISTS s2_observations (
    obs_date             DATE PRIMARY KEY,
    scene_id             TEXT NOT NULL,
    mgrs_tile            TEXT NOT NULL,
    scene_cloud_pct      REAL,
    valid_pct            REAL,
    lease_bare_ha        REAL, lease_water_ha REAL, lease_veg_ha REAL, lease_other_ha REAL,
    pit_bare_ha          REAL, pit_water_ha   REAL,
    outside_pit_bare_ha  REAL,
    buffer_bare_ha       REAL,
    created_at           TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS s2_footprint (
    obs_date DATE NOT NULL REFERENCES s2_observations (obs_date) ON DELETE CASCADE,
    class    TEXT NOT NULL,
    area_ha  REAL,
    geom     GEOMETRY(MultiPolygon, 4326),
    PRIMARY KEY (obs_date, class)
);
CREATE INDEX IF NOT EXISTS s2_footprint_geom_idx ON s2_footprint USING GIST (geom);
"""


# ---------------------------------------------------------------- helpers
def month_range(start: str, end: str, step: int = 1):
    """'2024-01', '2024-04' -> [date(2024,1,1), date(2024,2,1), ...]."""
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield dt.date(y, m, 1)
        m += step
        while m > 12:
            m -= 12
            y += 1


def month_end(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def pick_candidates(items: list, tile: str = MGRS_TILE, limit: int = MAX_CANDIDATES) -> list:
    """Keep one tile, drop reprocessed duplicates (same acquisition, keep newest
    processing), sort least cloudy first."""
    best = {}
    for it in items:
        p = it.properties
        if p.get("s2:mgrs_tile") != tile:
            continue
        key = p["datetime"][:10]
        if key not in best or it.id > best[key].id:   # id ends with processing time
            best[key] = it
    return sorted(best.values(), key=lambda it: it.properties.get("eo:cloud_cover", 100))[:limit]


def load_zones(cur):
    """Lease and ultimate-pit polygons (WGS84) from PostGIS."""
    cur.execute("SELECT ST_AsBinary(ST_Union(geom)) FROM lease_boundary")
    lease = wkb.loads(bytes(cur.fetchone()[0]))
    cur.execute("SELECT ST_AsBinary(ST_Union(geom)) FROM ultimate_pit_limit")
    pit = wkb.loads(bytes(cur.fetchone()[0]))
    return lease, pit


# ---------------------------------------------------------------- raster work
def read_scene(item, aoi_utm):
    """Read the AOI window of each band onto one 10 m grid. Returns (arrays, transform)."""
    import planetary_computer
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.vrt import WarpedVRT

    item = planetary_computer.sign(item)
    # A 10 m grid snapped to whole 10 m (S2 tiles use 10 m-aligned origins), so
    # every band -- 10 m and 20 m alike -- is warped onto exactly the same pixels.
    minx, miny, maxx, maxy = aoi_utm.bounds
    x0, y1 = np.floor(minx / 10) * 10, np.ceil(maxy / 10) * 10
    w, h = int(np.ceil((maxx - x0) / 10)), int(np.ceil((y1 - miny) / 10))
    grid = from_origin(x0, y1, 10, 10)
    out = {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="3"):
        for band in BANDS:
            with rasterio.open(item.assets[band].href) as src:
                if src.crs.to_epsg() != SCENE_EPSG:
                    raise ValueError(f"{item.id}: expected EPSG:{SCENE_EPSG}, got {src.crs}")
                resampling = Resampling.nearest if band == "SCL" else Resampling.bilinear
                with WarpedVRT(src, crs=src.crs, transform=grid, width=w, height=h,
                               resampling=resampling, nodata=0) as vrt:
                    out[band] = vrt.read(1)
    return out, grid


def rasterize(geom_utm, shape_hw, transform):
    from rasterio.features import rasterize as rio_rasterize
    return rio_rasterize([(mapping(geom_utm), 1)], out_shape=shape_hw, transform=transform,
                         fill=0, dtype="uint8").astype(bool)


def vectorize(mask: np.ndarray, transform, to_wgs84) -> MultiPolygon | None:
    """Boolean mask -> cleaned MultiPolygon in WGS84 (speckle under 0.5 ha removed)."""
    from rasterio.features import shapes, sieve
    clean = sieve(mask.astype("uint8"), size=MIN_POLYGON_PIXELS)
    polys = [shape(g) for g, v in shapes(clean, mask=clean.astype(bool), transform=transform) if v == 1]
    if not polys:
        return None
    merged = unary_union(polys).simplify(5.0, preserve_topology=True)   # 5 m, half a pixel
    merged = shp_transform(to_wgs84, merged)
    if merged.geom_type == "Polygon":
        merged = MultiPolygon([merged])
    return merged if merged.geom_type == "MultiPolygon" else None


def process_scene(item, lease, pit):
    """Classify one scene and return (row dict, {class: MultiPolygon}) or None if too cloudy."""
    to_utm = Transformer.from_crs(4326, SCENE_EPSG, always_xy=True).transform
    to_wgs = Transformer.from_crs(SCENE_EPSG, 4326, always_xy=True).transform
    lease_u, pit_u = shp_transform(to_utm, lease), shp_transform(to_utm, pit)
    aoi_u = lease_u.buffer(AOI_BUFFER_M)

    bands, transform = read_scene(item, aoi_u)
    pb = item.properties.get("s2:processing_baseline")
    refl = {b: harmonize(bands[b], pb) for b in BANDS if b != "SCL"}
    classes = classify(refl["B02"], refl["B03"], refl["B04"], refl["B08"], refl["B11"], bands["SCL"])

    hw = classes.shape
    m_lease = rasterize(lease_u, hw, transform)
    m_pit = rasterize(pit_u, hw, transform) & m_lease
    m_buffer = rasterize(aoi_u, hw, transform) & ~m_lease

    a_lease = class_areas_ha(classes, m_lease)
    if a_lease["valid_pct"] < MIN_VALID_PCT:
        log.info("  %s rejected: %.1f%% of lease valid", item.id, a_lease["valid_pct"])
        return None
    a_pit = class_areas_ha(classes, m_pit)
    a_out = class_areas_ha(classes, m_lease & ~m_pit)
    a_buf = class_areas_ha(classes, m_buffer)

    row = {
        "obs_date": item.properties["datetime"][:10],
        "scene_id": item.id,
        "mgrs_tile": item.properties.get("s2:mgrs_tile"),
        "scene_cloud_pct": item.properties.get("eo:cloud_cover"),
        "valid_pct": round(a_lease["valid_pct"], 2),
        "lease_bare_ha": a_lease["bare"], "lease_water_ha": a_lease["water"],
        "lease_veg_ha": a_lease["vegetation"], "lease_other_ha": a_lease["other"],
        "pit_bare_ha": a_pit["bare"], "pit_water_ha": a_pit["water"],
        "outside_pit_bare_ha": a_out["bare"],
        "buffer_bare_ha": a_buf["bare"],
    }
    aoi_mask = m_lease | m_buffer
    footprints = {
        "bare": vectorize((classes == BARE) & aoi_mask, transform, to_wgs),
        "water": vectorize((classes == WATER) & aoi_mask, transform, to_wgs),
        "vegetation": vectorize((classes == VEGETATION) & aoi_mask, transform, to_wgs),
    }
    return row, footprints


# ---------------------------------------------------------------- database
def save(cur, row: dict, footprints: dict):
    cols = list(row)
    cur.execute(
        f"INSERT INTO s2_observations ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (obs_date) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "obs_date"),
        [row[c] for c in cols],
    )
    cur.execute("DELETE FROM s2_footprint WHERE obs_date = %s", (row["obs_date"],))
    for cls, geom in footprints.items():
        if geom is None:
            continue
        cur.execute(
            "INSERT INTO s2_footprint (obs_date, class, area_ha, geom) VALUES "
            "(%s, %s, ST_Area(ST_GeomFromGeoJSON(%s)::geography) / 1e4, "
            " ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))))",
            (row["obs_date"], cls, json.dumps(mapping(geom)), json.dumps(mapping(geom))),
        )


def months_done(cur) -> set:
    cur.execute("SELECT date_trunc('month', obs_date)::date FROM s2_observations")
    return {r[0] for r in cur.fetchall()}


def run(months, force=False):
    from pystac_client import Client

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()
    lease, pit = load_zones(cur)
    done = set() if force else months_done(cur)
    bbox = list(lease.buffer(0.01).bounds)
    catalog = Client.open(STAC_URL)

    stored = skipped = 0
    for m in months:
        if m in done:
            continue
        search = catalog.search(
            collections=[COLLECTION], bbox=bbox,
            datetime=f"{m.isoformat()}/{month_end(m).isoformat()}",
            query={"eo:cloud_cover": {"lt": MAX_SCENE_CLOUD}},
        )
        candidates = pick_candidates(list(search.items()))
        result = None
        for item in candidates:
            try:
                result = process_scene(item, lease, pit)
            except Exception as exc:  # one bad scene must not stop a backfill
                log.warning("  %s failed: %s", item.id, exc)
                result = None
            if result:
                break
        if result:
            save(cur, *result)
            conn.commit()
            stored += 1
            r = result[0]
            log.info("%s  %s  bare %.0f ha  water %.0f ha  veg %.0f ha  (valid %.0f%%)",
                     m.strftime("%Y-%m"), r["obs_date"], r["lease_bare_ha"],
                     r["lease_water_ha"], r["lease_veg_ha"], r["valid_pct"])
        else:
            skipped += 1
            log.info("%s  no usable scene (%d candidates)", m.strftime("%Y-%m"), len(candidates))
    cur.close()
    conn.close()
    log.info("done: %d months stored, %d without a usable scene", stored, skipped)
    return stored


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", help="first month, YYYY-MM")
    ap.add_argument("--end", help="last month, YYYY-MM")
    ap.add_argument("--step", type=int, default=1, help="months between observations")
    ap.add_argument("--latest", action="store_true", help="the last complete month only")
    ap.add_argument("--force", action="store_true", help="recompute months already stored")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.latest:
        first = dt.date.today().replace(day=1)
        last = (first - dt.timedelta(days=1)).replace(day=1)
        months = [last]
    elif a.start and a.end:
        months = list(month_range(a.start, a.end, a.step))
    else:
        ap.error("give --start and --end, or --latest")
    run(months, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
