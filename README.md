# DSE — AI Analytics Solution (Backend)

Backend API for the **DSE Interactive Reporting & AI Analytics Solution** — a
Power BI-style platform optimised for LabWare LIMS and general data sources.

Frontend repo: [interactive-analytics-dse](https://github.com/umair228/interactive-analytics-dse)

## Stack

- **Django 5 + Django REST Framework** — API & metadata store
- **SQLAlchemy** — dynamic, runtime-defined connections to *external* data
  sources (SQL Server, Oracle, PostgreSQL, MySQL, SQLite, Excel/CSV)
- **SimpleJWT** — authentication; Django groups/roles — RBAC
- **cryptography (Fernet)** — connection credentials encrypted at rest
- **pandas** — Excel/CSV ingestion
- App metadata DB: SQLite (dev) / PostgreSQL (prod)

## Architecture

| App | Responsibility |
|-----|----------------|
| `core` | Audit log, encryption, RBAC permission classes, pagination |
| `accounts` | Custom user, Organization/Site/Lab, roles, JWT auth |
| `connections` | `DataSource` registry, SQLAlchemy engine factory, schema introspection |
| `querybuilder` | Query-spec → SQL compiler, safe read-only execution |

The app's own DB (Django ORM) only stores metadata. **External** data sources
are connected to dynamically at runtime via SQLAlchemy — that is what lets one
codebase talk to many database types and lets users add connections live.

## Local setup

```bash
# 1. create the virtualenv and install dependencies
uv venv --python 3.13
uv pip install -r requirements.txt
source .venv/bin/activate

# 2. configure environment
cp .env.example .env

# 3. migrate and create the admin user + default org/site/lab
python manage.py migrate
python manage.py bootstrap        # creates admin / admin12345

# 4. run
python manage.py runserver
```

API is served at `http://127.0.0.1:8000/api/`.

> SQL Server connectivity requires an ODBC driver on the host
> (`ODBC Driver 18 for SQL Server`). The Docker image installs it automatically.

## Key endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/health/` | Liveness probe |
| `POST` | `/api/auth/token/` | Login → JWT access + refresh |
| `POST` | `/api/auth/token/refresh/` | Refresh access token |
| `GET`  | `/api/auth/me/` | Current user profile |
| `GET`  | `/api/connections/source-types/` | Supported source types |
| `GET/POST` | `/api/datasources/` | Manage data-source connections |
| `POST` | `/api/datasources/{id}/test/` | Test a connection |
| `GET`  | `/api/datasources/{id}/tables/` | List tables/views |
| `GET`  | `/api/datasources/{id}/columns/?table=` | List columns |
| `POST` | `/api/datasources/{id}/upload/` | Upload an Excel/CSV file |
| `GET/POST` | `/api/queries/` | Manage saved queries |
| `POST` | `/api/queries/compile/` | Spec → generated SQL |
| `POST` | `/api/queries/execute/` | Run an ad-hoc query |
| `POST` | `/api/queries/{id}/run/` | Run a saved query |

## Roles (RBAC)

- **admin** — full access, manage users & all resources
- **analyst** — create connections, queries, dashboards
- **viewer** — read-only; view & run shared queries/dashboards

## Docker

```bash
docker compose up --build
```

## Roadmap

Phase 1 (this build): connectivity + visual query builder.
Next: datasets & interactive dashboards → data math → statistical analytics →
LLM assistant.
