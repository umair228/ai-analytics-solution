# Installation Qualification (IQ) — DSE Release 0

**Status:** Draft · **Owner:** Umair Khan (execute on the prod host, sign each step) · **System:** `[SITE: host / URL]`
**Purpose:** document objective evidence that DSE R0 is installed correctly and completely in the target environment.

## 1. Pre‑requisites (verify & record)
| # | Item | Expected | Verified |
|---|---|---|---|
| IQ‑P1 | Host OS / Docker | `[SITE]` (e.g. Win Server 2022 + WSL2 + Docker Engine) running | `[SIGN‑OFF]` |
| IQ‑P2 | Compose file | `dis-deploy/docker-compose.prod.yml` present; `.env.prod` populated | |
| IQ‑P3 | Registry access | Can pull `ghcr.io/umair228/dis-api` and `dis-web` at the intended `TAG` | |
| IQ‑P4 | App database | `pgvector/pgvector:pg16` service healthy; volume `pgdata` mounted | |
| IQ‑P5 | LLM endpoint | On‑prem / Tailscale model endpoint reachable from the `api` container (or accepted offline → AI‑explain degrades) | |
| IQ‑P6 | SMTP | `EMAIL_*` configured (or console backend accepted for pilot) | |

## 2. Installation steps (record command + outcome)
| # | Step | Command (on host) | Expected | Verified |
|---|---|---|---|---|
| IQ‑1 | Pull images | `make pull` (or `docker compose -f docker-compose.prod.yml pull`) | `dis-api`/`dis-web` at target TAG pulled | |
| IQ‑2 | Start stack | `docker compose -f docker-compose.prod.yml up -d` | `db`, `api`, `scheduler`, `web` all **Up/healthy** | |
| IQ‑3 | Migrations | (auto on `api` start: `migrate --noinput`) — check logs | All migrations applied incl. `analytics.0001_initial`, `datasets.0008_*`; no errors | |
| IQ‑4 | Static/seed | per deploy guide | Static collected; seed/demo data present if required | |

## 3. Installed‑component verification
| # | Check | How | Expected | Verified |
|---|---|---|---|---|
| IQ‑5 | Backend up | `GET /api/ai/status/` | 200; model name or "not configured" (graceful) | |
| IQ‑6 | New endpoints registered | `GET /api/analytics/stats/catalog/`, `GET /api/analytics/runs/` (authed) | 200; catalog lists 15 tests; runs list returns (possibly empty) | |
| IQ‑7 | Dependencies in image | `docker compose exec api python -c "import statsmodels, sklearn, scipy, xgboost, numpy; print(numpy.__version__)"` | imports succeed; numpy is `1.26.x` (`< 2`) | |
| IQ‑8 | Optional libs lazy/skip | `docker compose exec api python -c "import analytics.forecasting_models as m; print('ok', list(m.REGISTRY))"` | imports without requiring torch/neuralprophet; 11 methods registered | |
| IQ‑9 | Frontend served | open `[SITE]/` → app loads; sidebar shows Analytics group (Anomaly Detection, Statistical Calculations, Forecast Workbench, Chart Studio, Saved Runs) | renders, no console errors | |
| IQ‑10 | Scheduler | `scheduler` container running; cron/loop invokes `run_scheduled_refreshes` + `send_scheduled_reports` per schedule | service healthy; logs show due‑checks | |
| IQ‑11 | Audit logging | perform one analysis → check `AuditLog` has a QUERY entry for `AnalysisRun` | entry present | |

## 4. Rollback (verify documented)
`docker compose down` + redeploy previous `TAG`; Postgres migrations for R0 are additive (no destructive column drops) so a prior image runs against the same DB. Record the previous good TAG: `[SITE]`.

## 5. Sign‑off
Installed per this IQ: **Name / Role / Date** `[SIGN‑OFF]` — Reviewed: **Name / Date** `[SIGN‑OFF]`.
