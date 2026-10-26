# Surkha Mine Monitor: Web GIS + Sentinel-2 change monitoring

A full-stack mine-site monitoring app.

- **Stack:** PostGIS + FastAPI + Leaflet.js, plus a monthly **Sentinel-2** pipeline that tracks
  excavation, water and vegetation across the lease since 2019.
- **Delivery:** containerised with **Docker** and delivered by **GitHub Actions CI/CD** to Render,
  with PostGIS on Neon.

**Site:** Surkha (North) Lignite Mine, Bhavnagar district, Gujarat (operated by GMDC)

![CI](https://github.com/aayush1728/B.tech-Project/actions/workflows/ci.yml/badge.svg)

## Background

This project began as my B.Tech dissertation deliverable. It was a static
[qgis2web](https://github.com/tomchadwin/qgis2web) export of survey data
digitised from GMDC's key plan of the mine. The export had six layers baked
into static HTML/JS: lease boundary, lignite boundary, ultimate pit limit,
contours, canals and lakes. It had no database and no backend.

This repo rebuilds it as a real spatial-data application:

- Geometries live in **PostGIS**, georeferenced to true WGS84.
- A **FastAPI** service serves them as GeoJSON and computes monitoring
  statistics in SQL.
- A **Leaflet** front end renders everything live over satellite imagery.
- A scheduled job adds a new **Sentinel-2** observation every month, so the
  app shows how the mine changes, not just the 2023 plan.

## Architecture

```
 legacy qgis2web .js ──► data_migration/ ──► PostGIS 16 + PostGIS 3.4
   (pixel coords)        georeference.py          │  GiST-indexed, EPSG:4326
                         load_data.py             ▼
                                            FastAPI (backend/app)
                                     GeoJSON + spatial analysis endpoints
                                                  │
                                                  ▼
                                     Leaflet.js (frontend/) — served by FastAPI
```

| Container | Purpose | Port |
|---|---|---|
| `postgis` | PostgreSQL 16 + PostGIS 3.4 | 5432 |
| `migrate` | One-shot job: georeference and load the 6 layers (78 features) | — |
| `api` | FastAPI and the Leaflet map | **8000** |
| `pgadmin` | DB browser | 8080 |

## Quick start

**Prerequisites:** Docker Desktop

```bash
docker compose up --build
```

Then open:

- **http://localhost:8000**: the map
- **http://localhost:8000/docs**: interactive API docs (Swagger)
- **http://localhost:8080**: pgAdmin (credentials are in `docker-compose.yml`)

The `migrate` container prints the georeferencing parameters and a sanity
check (lease area against the published figure) before the API starts.

**Add satellite data.** With the stack running, in a second terminal:

```bash
pip install -r monitoring/requirements.txt
python -m monitoring.s2_monitor --start 2024-03 --end 2024-03            # one month, ~1 min
python -m monitoring.s2_monitor --start 2019-01 --end 2026-09 --step 3   # quarterly history
```

Refresh the map: the *Satellite monitoring* panel then shows the time series.

> On Windows with Conda and Python 3.13, if `pip` tries to compile
> `psycopg2`, run `conda install -c conda-forge psycopg2` first.

### Without Docker (for development)

```bash
docker compose up -d postgis
pip install -r backend/requirements-dev.txt -r data_migration/requirements.txt -r monitoring/requirements.txt
python data_migration/load_data.py
cd backend && uvicorn app.main:app --reload     # → http://localhost:8000
pytest -v
```

## API

| Endpoint | What it returns |
|---|---|
| `GET /api/layers` | Layer catalogue: feature counts, geometry types, extents |
| `GET /api/layers/{name}?bbox=&simplify_m=` | A layer as GeoJSON, filtered by bounding box (uses the GiST index via `&&`) and optionally simplified |
| `GET /api/stats/summary` | Lease, deposit and pit areas; pit share of lease; deposit inside the pit or outside the lease; contour range |
| `GET /api/analysis/water-setback?buffer_m=` | Lakes and canals within *N* m of the ultimate pit limit (`ST_DWithin` on geography) |
| `GET /api/analysis/lignite-outside-lease` | Parts of the lignite deposit outside the mining lease (`ST_Difference`) |
| `GET /api/analysis/point?lon=&lat=` | "What's here?": zone membership, distance to the lease boundary, nearest water, nearest contour (KNN `<->`) |
| `GET /api/monitoring/timeseries` | One row per month: bare ground, water and vegetation (ha) in the lease, the pit, outside the pit, and a 1 km buffer |
| `GET /api/monitoring/footprint?date=` | Bare-ground and water polygons for the observation nearest a date |
| `GET /api/monitoring/change?from=&to=` | Newly bare and no-longer-bare ground between two observations, with the share outside the ultimate pit |
| `GET /api/monitoring/alerts?persist=6` | Ground bare in every one of the last *N* observations, inside the lease but outside the pit, or outside the lease |

**Spatial conventions:**

- Data is stored in EPSG:4326.
- Every area and distance is computed on `::geography`, i.e. true metres on
  the WGS84 ellipsoid. Nothing is measured in planar degrees or Web Mercator.
- Layer names are whitelisted before being interpolated into SQL, and every
  value is passed as a bound parameter.

## Sentinel-2 monitoring

`monitoring/s2_monitor.py` runs monthly in GitHub Actions (`.github/workflows/monitor.yml`).

1. **Find scenes.** It searches Microsoft Planetary Computer's STAC for Sentinel-2 L2A scenes on
   MGRS tile **43QBE**, which contains the whole lease, least cloudy first.
2. **Read only what's needed.** It reads only the lease plus a 1 km window from the
   cloud-optimised GeoTIFFs. All bands are warped onto one 10 m grid, and the 2022 processing
   offset (−1000) is corrected.
3. **Classify each pixel** (`monitoring/classify.py`):
   - Cloud and shadow are masked using SCL, the scene-classification band.
   - Water: NDWI > 0.05. Vegetation: NDVI > 0.30. Bare: NDVI < 0.20 and BSI > 0.
   - A scene is used only if at least 90 % of the lease is cloud-free.
4. **Store the results.** Areas per zone go in `s2_observations`, and bare and water footprints
   go in `s2_footprint` as polygons. Change and persistence are then plain PostGIS (`ST_Difference`,
   a recursive `ST_Intersection`).

**Notes on the method:**
- Bare ground is detected spectrally; alerts use persistence across several scenes to separate mining
  activity from seasonal fallow land.
- Sentinel-2 at 10 m tracks footprints and trends; volumetrics come from drone DSMs.

**First real run (7 March 2024):**

| Area inside the lease | Value |
|---|---|
| Lease cloud-free | 100 % |
| Bare | 1,838 ha |
| Vegetation | 1,180 ha |
| Water | 17 ha |

Bare ground outside the 1,028 ha ultimate pit includes dumps, haul roads and seasonally fallow
farmland after the rabi harvest.

## CI/CD

| Stage | Job | What it does |
|---|---|---|
| Every push or PR | `test` | Ruff lint → load the layers into a PostGIS service → 33 pytest tests (API, georeferencing, GCP maths, the monitoring pipeline on synthetic scenes) |
| | `docker` | Builds the stack with Compose and smoke-tests `/health`, the stats and the map |
| Push to `main` | `deploy` | Publishes the image to `ghcr.io` → migrates the production DB (Neon) → triggers the Render deploy hook → polls `/health` until this commit is live |
| 6th of each month | `Sentinel-2 monitor` | Adds last month's observation to production |

Setup, step by step: [DEPLOY.md](DEPLOY.md).

## The CRS postmortem 🔎

**Symptom:** the six layers loaded into PostGIS fine, but on a basemap they
showed up a few kilometres from **0°N 0°E**, in the Gulf of Guinea, instead
of in Gujarat.

**What I first suspected:** a mislabelled CRS (a `set_crs` vs `to_crs`
problem).

**Root cause:** there was no real CRS behind the numbers at all.

- In QGIS, the layers were digitised over a scanned key plan
  (`BaseMap_0.png`, 3544 × 2658 px) that had **never been georeferenced**.
- QGIS gave the scan its default identity geotransform: origin (0, 0),
  1 unit = 1 pixel, inside EPSG:3857. Its `.aux.xml` shows exactly this.
- qgis2web then exported each vertex to CRS84.
- So every "longitude/latitude" in the export is really a **pixel position**
  passed through the inverse Web Mercator formula. Relabelling or
  reprojecting can't fix that, because the data has to be *georeferenced*.

**Fix:** done in `data_migration/georeference.py`, in three steps. No
external control points were needed, because the plan carries its own.

1. **Undo the export.** Convert CRS84 → EPSG:3857 metres, which gives back
   pixel coordinates (px, −py) exactly.
2. **Pixels → GMDC mine grid.** The plan prints an 800 m grid
   (4000 E … 14400 E, 12800 N … 20800 N). I detected 24 grid lines in the
   scan and fitted them by least squares, giving 3.386 m/px with an
   **RMSE of 0.91 m**. The plan's 1:20 000 scale bar agrees.
3. **Mine grid → WGS84.** The plan also prints one meridian
   (**72° 11′ 38.4″ E**) and one parallel (**21° 40′ 56.8″ N**).
   - Where they cross is a true control point.
   - Their slope shows the mine grid is **rotated 4.80°** from true north.
   - Scale is 1, because the grid is in metres.
   - Together these fully define a 2D similarity (Helmert) transform into a
     local tangent plane, which pyproj converts back to lat/lon.

Two smaller issues surfaced along the way:

- The boundary layers had been digitised as **open polylines** whose ends
  missed by 0.2–3.8 m. That's why `ST_BuildArea()` was needed before.
- The loader now closes these gaps (refusing anything over ~20 m) and stores
  valid polygons, so area and overlap queries work.

**Validation:**

| Check | Result |
|---|---|
| Lease area after the fix | **3,623 ha** vs **~3,672 ha** published by GMDC ([GEM wiki](https://www.gem.wiki/Bhavnagar_Lignite_Mine)): **1.3 % difference**, from a transform that used no outside data |
| Ultimate pit inside the lease | 100 % (< 0.1 ha outside) |
| Regression tests | `backend/tests/` fails CI if the layers ever drift out of Bhavnagar or the lease area moves more than 3 % from the published figure |
| Visual check | `exports/surkha_georeferenced.kml` opens in Google Earth over the real pit |

### Checking against satellite imagery

Tick **Original CAD plan** in the map's side panel. It shows the scanned plan,
warped with the same transform as the vectors, over Esri imagery
(`data_migration/make_plan_overlay.py` generates it).

Differences you may notice between the plan and current imagery, and why:

| Difference | Why |
|---|---|
| Pit and dump edges don't match the imagery | The plan is dated **31 Mar 2023**. Benches and dumps have moved since then. |
| A large pit south of the plan's working area | It isn't drawn on the 2023 plan, so it was opened or expanded after it (or left off this sheet). It is a real difference, not an error. |
| The ultimate pit limit sits outside the current pit | It's a **design** boundary: the final extent, not today's edge. |
| The lignite boundary and lease lines can't be seen on the ground | They're a subsurface deposit limit and a legal line. |
| Contours cut through the pit | They're the **pre-mining** ground surface. |

### Refining with ground control points (GCPs)

The single control point can be replaced by a least-squares fit to many
points (`data_migration/gcp.py`):

1. In QGIS, open **Layer → Georeferencer** and load
   `legacy/…/data/BaseMap_0.png`.
2. In the main QGIS window, add the Esri World Imagery XYZ layer.
3. Add **8–12 GCPs**, spread across the whole sheet:
   - Click a feature on the scan, then pick the same spot with
     **From map canvas**.
   - Use features that don't change: road junctions, canal and nala bends,
     building and compound corners, bridge ends.
   - Avoid pit and dump edges, because they change.
4. Save the points as `data_migration/gcps.points`. Any CRS works; it's
   read from the file header.
5. Run `python data_migration/make_plan_overlay.py`, then
   `docker compose up --build`.

The `migrate` log then reports the fitted scale and rotation, the **RMSE**,
and **per-point residuals**, and flags any point worse than 2.5 × RMSE.
`gcp.leave_one_out()` gives independent check errors for each point.

- Default model: 4-parameter Helmert, because a CAD plan should be
  conformal.
- Set `GCP_MODEL=affine` to also absorb differential shrinkage in the scan.

## Repository structure

```
.
├── docker-compose.yml          # postgis + migrate + api + pgadmin
├── backend/
│   ├── Dockerfile
│   ├── app/main.py             # FastAPI endpoints
│   ├── app/db.py               # connection pool
│   └── tests/                  # 33 tests: API, georeferencing, GCP maths, monitoring pipeline
├── frontend/                   # Leaflet app (served by FastAPI)
├── data_migration/
│   ├── georeference.py         # pixel → mine grid → WGS84
│   ├── load_data.py            # parse legacy .js → georeference → PostGIS
│   └── source_layers/          # original qgis2web layer exports
├── exports/surkha_georeferenced.kml
├── monitoring/                 # Sentinel-2 pipeline: classify.py, s2_monitor.py
├── render.yaml                 # Render blueprint (free web service)
├── DEPLOY.md                   # one-time setup for Neon + Render + GitHub secrets
├── .github/workflows/ci.yml    # lint → tests → docker smoke test → deploy (main)
├── .github/workflows/monitor.yml  # monthly Sentinel-2 run
└── legacy/                     # original static qgis2web export (B.Tech deliverable)
```

## Author

Aayush Yadav, M.Tech Geoinformatics, IIT Kanpur · B.Tech Mining Engineering, IIEST Shibpur
