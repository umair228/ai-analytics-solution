# Deployment Guide — DSE Release 0

**Status:** Draft · **Owner:** Umair Khan (validate by deploying to `[SITE]`) · **Reference:** `dis-deploy/` (`docker-compose.prod.yml`, `Makefile`, `scripts/`, `.env.prod.example`)

## 1. Topology
- **Build/registry:** GitHub Actions on each app repo builds and pushes images to **GHCR**: `ghcr.io/umair228/dis-api` (Django backend + scheduler) and `ghcr.io/umair228/dis-web` (built frontend, served by nginx).
- **Runtime (prod host `89.35.193.15`):** Docker Compose stack from `dis-deploy/docker-compose.prod.yml`:
  - `db` — `pgvector/pgvector:pg16` (Postgres, app database).
  - `api` — `dis-api:${TAG}`; on start runs `python manage.py migrate --noinput && …` then serves via gunicorn.
  - `scheduler` — `dis-api:${TAG}`; runs the periodic management commands (`run_scheduled_refreshes`, `send_scheduled_reports`).
  - `web` — `dis-web:${TAG}`; nginx serving the SPA and proxying `/api`.
- **LLM:** served on‑prem / via Tailscale (no GPU on the app host); AI features degrade gracefully if unreachable.

## 2. CI → image build
1. Merge the release branch (`release-0-workflows`) into **`main`** on both app repos.
2. GitHub Actions:
   - `ci.yml` runs `manage.py check` + the analytics/agent/NL→SQL test suites.
   - `build-push.yml` builds the image and pushes to GHCR (tags: `latest`, the commit SHA, and `v*` for version tags). Backend image installs `requirements.txt` **under `.docsearch-constraints.txt`** (keeps `numpy<2`; installs `xgboost`) and NeuralProphet from its pinned Git tag.
3. Choose the deploy **TAG** (the new SHA or a `vX.Y` tag).

## 3. Deploy / upgrade on the prod host
```
# in dis-deploy/ on the host
export TAG=<sha-or-version>          # set in .env.prod
make pull                            # docker compose -f docker-compose.prod.yml pull
make up                              # docker compose ... up -d   (api auto-runs migrations)
docker compose -f docker-compose.prod.yml ps     # all services Up/healthy
docker compose -f docker-compose.prod.yml logs -f api | head   # confirm migrations applied
```
(If a self‑hosted Actions runner on the host performs CD, this is triggered automatically on merge to `main`; otherwise run the commands above.)

## 4. Configuration (`.env.prod`)
Populate from `.env.prod.example`: Postgres creds, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS` (include `89.35.193.15` / the hostname), `LLM_PROVIDER=local` + endpoint, `EMAIL_*` (SMTP) for scheduled reports/email, `TAG`. `[SITE]` fill values.

## 5. Scheduled distribution
Ensure the `scheduler` service (or host cron) invokes:
- `python manage.py run_scheduled_refreshes` — every ~5 min.
- `python manage.py send_scheduled_reports` — daily (sends dataset CSVs, saved‑run exports, and chart reports per their cadence).

## 6. Smoke test (post‑deploy)
Open `[SITE]/` → run one Anomaly, one Statistics, one Forecast (Auto) → confirm charts + Saved Runs + an export. Run the IQ checklist (`05-installation-qualification.md`).

## 7. Rollback
R0 migrations are additive (no destructive drops), so reverting the image TAG is safe:
```
export TAG=<previous-good>; make pull && make up
```

## 8. Notes / caveats
- **HTTP‑only pilot:** the current pilot is served over HTTP. For production beyond the pilot, terminate **TLS** at nginx / a reverse proxy and set `SECURE_*` Django settings (see Cybersecurity doc).
- **XGBoost** needs the OpenMP runtime; it is present in the Linux container image. On a developer macOS host install `libomp` (`brew install libomp`) or the method is simply skipped.
