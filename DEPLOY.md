# Deploying: GitHub Actions → Neon (PostGIS) + Render

Pipeline:

```
push to main ─► test (lint, PostGIS, pytest) ─► docker smoke test ─► deploy
                                                                     ├─ publish image to ghcr.io
                                                                     ├─ migrate Neon (load_data.py)
                                                                     ├─ trigger Render deploy hook
                                                                     └─ poll /health until this commit is live
6th of each month ─► Sentinel-2 monitor ─► new observation in Neon ─► shows on the live map
```

Everything is on free tiers. Setup takes about 20 minutes, done once.

## 1. Push the code

```powershell
cd D:\Project_resume\Btech\B.tech-Project
git add -A
git commit -m "FastAPI + PostGIS app, georeferencing, Sentinel-2 monitoring, CI/CD"
git push origin main
```

Open the repo's **Actions** tab. `CI/CD` should run. `test` and `docker` should go green. `deploy`
publishes the image and then warns that deployment is skipped, because the secrets don't exist yet.

## 2. Database: Neon (free PostGIS)

1. Go to <https://neon.tech> and sign in with GitHub.
2. Create a project named `surkha`. Use Postgres 16 and region **AWS Asia Pacific (Singapore)**.
3. On the dashboard, open **Connection string**. Untick *Pooled connection* and copy the URL. It
   looks like `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`.

PostGIS needs no extra step, because `load_data.py` runs `CREATE EXTENSION postgis`.

## 3. Web service: Render

1. Go to <https://render.com> and sign in with GitHub.
2. Choose **New → Blueprint** and pick this repository. Render reads `render.yaml`.
3. When it asks for `DATABASE_URL`, paste the Neon URL. Click **Apply**.
4. The first build takes about 5 minutes. Your URL will be something like
   `https://surkha-mine-monitor.onrender.com`.
5. Open the service's **Settings → Deploy Hook** and copy the hook URL.

## 4. Connect GitHub to both

In the repo, go to **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `NEON_DATABASE_URL` | the Neon URL from step 2 |
| Secret | `RENDER_DEPLOY_HOOK_URL` | the deploy hook from step 3 |
| Variable | `LIVE_URL` | `https://surkha-mine-monitor.onrender.com`, with no trailing slash |

Then go to **Actions → CI/CD → Run workflow** on `main`. The `deploy` job should end with
"Deployed <commit> to <url>".

## 5. Fill the monitoring history

Go to **Actions → Sentinel-2 monitor → Run workflow** and enter:
- `start` = `2019-01`
- `end` = last month
- `step` = `1`

Expect roughly 20–40 seconds per month, about 1 hour for monthly data since 2019. For a quick
first pass, use `step` = `3`; you can fill the gaps later, since stored months are skipped.
After that, the workflow adds one new month automatically on the 6th.

Run it on your own PC against the Docker database instead:

```powershell
pip install -r monitoring\requirements.txt
python -m monitoring.s2_monitor --start 2024-01 --end 2024-06
```

## Notes and limits

- **Render's free plan sleeps after 15 minutes idle.** The first request then takes about a minute.
  Open the URL a few minutes before a demo.
- **Neon's free plan has 0.5 GB of storage.** Monthly footprints since 2019 come to a few MB.
- **Each deploy re-runs `load_data.py`**, which rebuilds the 6 layer tables but never touches the
  `s2_*` monitoring tables.
- **To roll back,** re-run an older `CI/CD` run, or use **Manual Deploy → previous commit** in Render.
