# DIS — Security & Compliance

Decision Intelligence Suite is a **read-only analytics layer** over laboratory
data (LIMS, documents). It never mutates source systems. This document maps the
implemented controls to the regimes that matter for a regulated lab —
**ALCOA+**, **ISO/IEC 17025**, and **21 CFR Part 11** — and tells an operator
how to configure them.

> Scope note: DIS is a decision-support tool. It surfaces and explains existing
> records; the LIMS remains the system of record. DIS does not generate GxP
> "original data" — it reads it.

---

## 1. Data integrity — read-only by construction

| Layer | Control | Where |
|-------|---------|-------|
| Application | All generated/handwritten SQL is parsed and rejected unless it is a single read-only `SELECT`/`WITH`; `INSERT/UPDATE/DELETE/DDL/… INTO` are blocked. | [querybuilder/executor.py](querybuilder/executor.py) — `assert_read_only` |
| Application | Query builder compiler only ever emits `SELECT`. | [querybuilder/compiler.py](querybuilder/compiler.py) |
| Application | AI agent tools are read-only; the model never writes data and never computes figures itself — it narrates validated tool output. | [ai/tools.py](ai/tools.py), [ai/agent.py](ai/agent.py) |
| Database | Dedicated **read-only DB account** with `db_datareader` + explicit `DENY` on all writes. | [db/grant-readonly-mssql.sql](db/grant-readonly-mssql.sql), `DB_USER=dis_readonly` |
| Result bounds | Row cap (`DSE_QUERY_MAX_ROWS`) and statement timeout (`DSE_QUERY_TIMEOUT_SECONDS`). | [config/settings/base.py](config/settings/base.py) |

Use **both** the app guard and the read-only DB grant — the grant is the
authoritative backstop if the app is ever bypassed.

---

## 2. ALCOA+ mapping (for the DIS audit trail)

DIS records who asked what, when, and what it returned — an audit trail of
*analysis activity* (not a replacement for the LIMS' own GxP audit trail).

| ALCOA+ | How DIS satisfies it |
|--------|----------------------|
| **Attributable** | Every query/insight is tied to an authenticated user + client IP. [core/audit.py](core/audit.py), `AuditLog` model. SSO (below) gives enterprise identity. |
| **Legible** | Audit entries are structured JSON (`action`, `target`, `summary`, `detail`) exposed via a read-only admin/API. [core/models.py](core/models.py), [core/views.py](core/views.py) |
| **Contemporaneous** | `created_at` stamped at write time (`auto_now_add`). |
| **Original** | DIS reads the LIMS as source of truth; it never edits it. Tool outputs trace back to the exact SQL (returned in each payload). |
| **Accurate** | Figures come from deterministic, **unit-tested** engines (stats/anomaly/RCA/forecast/NL→SQL), not the LLM. Golden tests gate every change — see [§5](#5-accuracy-gate). |
| **+ Complete / Consistent / Enduring / Available** | Audit log is append-only (no delete in the API/admin), indexed, and stored in Postgres alongside the app. |

The AI conversation history (`ai/models.py`) additionally preserves the full
question → tool-call → evidence → answer trace for each agent run.

---

## 3. Access control & SSO (OIDC)

Enterprise identity via any OIDC provider (Keycloak / Okta / Azure AD / Auth0).
**Disabled by default** — turning it on requires no code change.

Enable:

1. `pip install mozilla-django-oidc` (left out of the default image; it is the
   only extra dependency).
2. In `.env.prod`:
   ```ini
   OIDC_ENABLED=True
   OIDC_RP_CLIENT_ID=dis
   OIDC_RP_CLIENT_SECRET=...
   OIDC_OP_BASE_URL=https://login.yourorg.com/realms/lab   # Keycloak-style
   # For Okta/Azure/Auth0 set explicit endpoints instead:
   # OIDC_OP_AUTHORIZATION_ENDPOINT=...  OIDC_OP_TOKEN_ENDPOINT=...
   # OIDC_OP_USER_ENDPOINT=...  OIDC_OP_JWKS_ENDPOINT=...
   OIDC_STAFF_GROUPS=lab-admins
   OIDC_SUPERUSER_GROUPS=dis-superadmins
   ```

What it does ([config/settings/sso.py](config/settings/sso.py),
[config/oidc_backend.py](config/oidc_backend.py)):

- Adds the OIDC auth backend (local Django admin remains as fallback).
- Mounts `/oidc/` login/callback/logout routes.
- **Maps the IdP `groups` claim → Django groups** on every login (IdP is the
  source of truth), and grants `is_staff`/`is_superuser` from configurable
  group lists.
- RP-initiated logout back to the IdP end-session endpoint.

When disabled, DIS uses its built-in JWT auth (`djangorestframework-simplejwt`).

---

## 4. Transport, session & secrets hardening

Already enforced in [config/settings/prod.py](config/settings/prod.py):
HSTS (1 yr, preload, subdomains), SSL redirect, secure + HTTP-only session/CSRF
cookies, `SECURE_PROXY_SSL_HEADER` (behind the nginx TLS terminator),
`X_FRAME_OPTIONS=DENY`, `SECURE_CONTENT_TYPE_NOSNIFF`.

Secrets:
- Stored DataSource passwords are encrypted at rest with **Fernet**
  (`DSE_FERNET_KEY`). **Generate once and never rotate in place** — rotating it
  invalidates every saved connection password.
- No secrets in the image: everything is injected via `.env.prod` (keep it out
  of version control — see `.env.prod.example`).
- **Air-gapped LLM**: production inference is local vLLM (`LLM_BASE_URL` →
  internal), so prompts/data never leave the network. No cloud LLM key required.

---

## 5. Accuracy gate

Correctness is a security property here — a wrong number presented confidently is
the main risk of an analytics assistant. DIS mitigates it structurally:

- Numbers are produced by deterministic engines with **known-answer tests**
  (`analytics/tests.py`, `ai/tests.py` — incl. NL→SQL golden cases).
- Retrieval quality is checked by `manage.py eval_astm`; served-model quality by
  `finetune/eval_domain.py`.
- CI (`.github/workflows/ci.yml`, `make ci`) runs the deterministic gate on every
  push — a regression in intent-parsing or computation fails the build.

---

## 6. Operator checklist (before go-live)

- [ ] `DSE_FERNET_KEY` generated and backed up (never rotate in place).
- [ ] `DJANGO_SECRET_KEY` set to a long random value; `DJANGO_DEBUG=False`.
- [ ] `DJANGO_ALLOWED_HOSTS` / `CORS_ALLOWED_ORIGINS` locked to real hostnames.
- [ ] LIMS reached via the **read-only** account (`db/grant-readonly-mssql.sql`).
- [ ] TLS terminated at the web tier; `DJANGO_SSL_REDIRECT=True`.
- [ ] OIDC enabled and group→role mapping verified (if using SSO).
- [ ] LLM points at the **internal** vLLM endpoint (no egress).
- [ ] `make ci` green; `eval_astm` meets the retrieval threshold.
- [ ] Audit log reviewed/exported per your retention policy.
