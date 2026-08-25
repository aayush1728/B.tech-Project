# Web GIS Industrial Site Monitoring App

A live, full-stack rebuild of a static mine-site GIS export into a real
monitoring application — built on **FastAPI + PostGIS + Leaflet.js**,
containerized with **Docker**, and deployed via **GitHub Actions CI/CD**.

## Background

This project began as a B.Tech dissertation deliverable: a static
[QGIS2Web](https://github.com/tomchadwin/qgis2web) export of survey data
for the **Surkha (North) Lignite Mine**, Bhavnagar district, Gujarat
(operated by GMDC) — six map layers (lease boundary, lignite deposit
boundary, ultimate pit limit, elevation contours, canals, lakes) baked
into static HTML/JS with no backend, no database, and no interactivity
beyond layer toggling.

This repo is a ground-up rebuild of that project as a real spatial data
application: geometries live in a PostGIS database, a FastAPI backend
serves them (and computes real monitoring statistics like boundary
overlap and encroachment distance), and a Leaflet.js frontend renders
everything live instead of from a static export.

## Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│  Leaflet.js │ <--> │   FastAPI    │ <--> │   PostGIS   │
│  frontend   │      │   backend    │      │  database   │
└─────────────┘      └──────────────┘      └─────────────┘
```

All services run in Docker containers, orchestrated via
`docker-compose.yml`.

## Status

- [x] **Data migration** — parse the legacy qgis2web `.js` layer exports
      and load them into 6 PostGIS tables (`data_migration/load_data.py`)
- [x] **Local dev environment** — PostGIS + pgAdmin via Docker Compose
- [ ] **Georeferencing fix** — legacy export coordinates are in an
      unknown local survey grid, not true WGS84; being corrected via
      manual control-point georeferencing before frontend work begins
- [ ] **FastAPI backend** — REST endpoints serving each layer as GeoJSON,
      plus monitoring endpoints (boundary overlap, encroachment distance)
- [ ] **Leaflet.js frontend** — live map rendering from the API
- [ ] **Docker Compose (full stack)** — one-command startup for db + api + web
- [ ] **GitHub Actions CI/CD** — automated build/test/deploy pipeline

## Tech Stack

| Layer          | Technology                          |
|----------------|--------------------------------------|
| Database       | PostgreSQL 16 + PostGIS 3.4          |
| Backend        | FastAPI (Python)                     |
| Frontend       | Leaflet.js                           |
| DB Admin       | pgAdmin 4                            |
| Containerization | Docker, Docker Compose             |
| CI/CD          | GitHub Actions                       |

## Local Setup

**Prerequisites:** Docker Desktop, Python 3.10+

1. Clone the repo and `cd` into it.
2. Copy the environment template:
   ```bash
   cp .env.example .env
   ```
3. Start the database (and pgAdmin):
   ```bash
   docker compose up -d
   ```
4. Install migration script dependencies:
   ```bash
   pip install -r data_migration/requirements.txt
   ```
   > On Windows/Conda, if `psycopg2-binary` fails to build, install it via
   > `conda install -c conda-forge psycopg2` instead, then `pip install
   > python-dotenv`.
5. Load the legacy layer data into PostGIS:
   ```bash
   python data_migration/load_data.py
   ```
   You should see 6 `[ok]` lines and a final count of 78 features loaded.
6. (Optional) Browse the database visually at **http://localhost:8080**
   (pgAdmin — see credentials in `docker-compose.yml`).

## Repository Structure

```
.
├── docker-compose.yml          # PostGIS + pgAdmin services
├── .env.example                 # DB connection config template
├── data_migration/
│   ├── load_data.py             # Parses legacy .js exports into PostGIS
│   ├── requirements.txt
│   └── source_layers/           # Original qgis2web layer exports (source data)
└── legacy/                      # Original static QGIS2Web export (B.Tech deliverable)
```

## Author

Aayush Yadav — M.Tech Geoinformatics, IIT Kanpur
B.Tech Mining Engineering, IIEST Shibpur
