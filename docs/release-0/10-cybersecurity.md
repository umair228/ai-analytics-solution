# Cybersecurity Document — DSE Release 0

**Status:** Draft · **Owner:** Zia (format to house template) · **Extends:** `../../SECURITY.md`

## 1. Security objective
DSE processes laboratory/QC data on‑premise. The security posture for R0 centres on **data locality** (no third‑party processing), **least‑privilege read access**, **auditability**, and **graceful failure**. This document covers what the R0 features add to the existing platform posture in `SECURITY.md`.

## 2. Data flow & classification
- **Inputs:** cached dataset snapshots (already materialized, access‑controlled) → analytics run **read‑only** over a pandas DataFrame. No analytics path writes to source LIMS/DBs.
- **Outputs:** result JSON persisted in `AnalysisRun` (owner‑scoped); chart images / exports generated on demand; emails sent via configured SMTP.
- **AI:** result *summaries* (compact, numbers‑grounded context) are sent to the **local** LLM endpoint only (on‑prem / Tailscale). **No customer data leaves the environment**; no public LLM/API is called at runtime.

## 3. Controls
| Domain | Control (R0) |
|---|---|
| **Authentication** | JWT (DRF SimpleJWT); all analytics/runs/explain/report endpoints require an authenticated user. |
| **Authorization** | `AnalysisRun.accessible_by` and report querysets are owner/shared‑scoped, mirroring `Dataset` ACLs; admins see all. Anomaly/stat/forecast runs only on datasets the caller can access. |
| **Audit** | Every run and deletion writes an immutable `AuditLog` entry (user, action, target, summary, IP); explanation/export/email actions audited. |
| **Local‑only inference** | `LLM_PROVIDER=local`; AI‑explain degrades gracefully (no error, no data retained externally) if the endpoint is down. |
| **Input safety** | Engines validate columns/params and raise typed `AnalyticsError`; numeric coercion is defensive; bounded model grids/epochs cap resource use. Calculated‑field expressions use a safe AST evaluator (no `eval`). |
| **Email/distribution** | Recipients are explicit; one‑off `email-now` and scheduled reports send only the chosen artifact; SMTP creds via env, not code. |
| **Secrets** | `DJANGO_SECRET_KEY`, DB creds, SMTP, LLM endpoint via `.env.prod` / environment — never committed. |
| **Dependencies / supply chain** | Pinned stack (`numpy<2`, `pandas 2.2`, pinned `torch`) via `.docsearch-constraints.txt`; new dep `xgboost` is lazy‑imported and constraint‑pinned; images built from versioned base; CI runs on each push. |
| **Determinism** | Fixed seeds → reproducible results support QC audit and tamper‑evidence. |

## 4. Network
- Backend behind nginx; only `/api` exposed to the SPA.
- **Pilot caveat:** R0 pilot is served over **HTTP** on `89.35.193.15`. **Recommendation before production:** terminate **TLS** at nginx/reverse proxy, enable Django `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HSTS; restrict `ALLOWED_HOSTS`/CORS to known origins. `[SITE]` record decision.
- LLM traffic confined to the on‑prem/Tailscale endpoint.

## 5. Threats considered (R0 surface)
- **Cross‑user data exposure** → mitigated by owner‑scoped runs/reports + per‑request dataset authorization (test TC‑D1; verify explain‑by‑run_id authorizes).
- **Resource exhaustion** (large series, deep models) → bounded grids/epochs, row/point caps, synchronous timeouts; async seam reserved.
- **Injection** → no raw SQL from analytics inputs; text‑to‑SQL path (separate feature) remains read‑only/parameterized per `SECURITY.md`.
- **Data egress via AI** → only local endpoint; compact numeric context, not raw record dumps.
- **Email misdirection** → explicit recipient entry; audited; consider an allowed‑domain policy `[SITE]`.

## 6. Compliance mapping
Map the above to the customer's framework (e.g. ISO 27001 A.9 access control, A.12 logging, A.14 secure development; 21 CFR Part 11 audit‑trail/electronic‑records where applicable). Reuse the mapping table in `SECURITY.md`. `[SIGN‑OFF]` Security reviewer / date.
