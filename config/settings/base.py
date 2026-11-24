"""
Base Django settings shared by all environments for the DSE backend
(Interactive Reporting & AI Analytics Solution).
"""
import base64
import hashlib
from datetime import timedelta
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-insecure-secret-change-me")
DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="127.0.0.1,localhost", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    # local apps
    "core",
    "accounts",
    "connections",
    "querybuilder",
    "analytics",
    "datasets",
    "dashboards",
    "ai",
    "forecasting",
    "docsearch",
    "replication",
    "semantics",
    "coa",
    "validation",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # A role obliged to carry a second factor cannot use the API without one. Middleware,
    # not a permission class: see the note under DEFAULT_PERMISSION_CLASSES.
    "accounts.middleware.MfaEnforcementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database (app metadata only — external data sources are dynamic, see
# the `connections` app). dev = SQLite, prod = PostgreSQL.
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# I18N / static / media
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "accounts.authentication.APITokenAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # NB: MFA enforcement is deliberately NOT here. A permission class in this tuple applies
    # only to views that have no opinion of their own — and nearly every viewset in this
    # codebase overrides `permission_classes` or `get_permissions()`, which REPLACES this
    # list rather than adding to it. Put the MFA policy here and it protects exactly the
    # views that never thought about permissions. It lives in
    # accounts.middleware.MfaEnforcementMiddleware, where no view can drop it.
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    # Part 11 §11.300(d) asks for safeguards that *prevent* unauthorised use, not only
    # detect it. The account lockout detects; this prevents — it caps how fast anyone can
    # try, per address, before they ever reach a username.
    #
    # It also bounds a second problem: audit writes fail CLOSED and the trail is
    # append-only, so an unthrottled attacker hammering /api/auth/login/ writes an audit
    # row per attempt and can fill the disk. Detection that can be turned into a
    # denial-of-service is not much of a control.
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.ScopedRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {
        "login": config("DSE_LOGIN_RATE", default="10/min"),
        "register": config("DSE_REGISTER_RATE", default="5/hour"),
        # A six-digit code is a million guesses. A second factor you can query a million
        # times is not a second factor. (Failures also feed the account lockout.)
        "mfa": config("DSE_MFA_RATE", default="10/min"),
    },
}

def _optional_int(value):
    """decouple cast for an optional integer: unset / blank → None."""
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def jwt_refresh_lifetime(hours, days) -> timedelta:
    """Refresh-token lifetime: JWT_REFRESH_HOURS (when set) overrides
    JWT_REFRESH_DAYS, so a deployment can enforce a shorter, sub-day session
    ceiling (e.g. a working day) without touching the days default."""
    if hours:
        return timedelta(hours=hours)
    return timedelta(days=days)


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=config("JWT_ACCESS_MINUTES", default=60, cast=int)
    ),
    "REFRESH_TOKEN_LIFETIME": jwt_refresh_lifetime(
        config("JWT_REFRESH_HOURS", default=None, cast=_optional_int),
        config("JWT_REFRESH_DAYS", default=7, cast=int),
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:5173,http://127.0.0.1:5173",
    cast=Csv(),
)
CORS_ALLOW_CREDENTIALS = True
# Let a cross-origin SPA read the server-chosen export file name.
CORS_EXPOSE_HEADERS = ["Content-Disposition"]


# --------------------------------------------------------------------------
# DSE platform settings
# --------------------------------------------------------------------------
def _derive_fernet_key() -> str:
    """Use an explicit Fernet key if provided, else derive a stable one
    from SECRET_KEY (dev convenience). Production MUST set DSE_FERNET_KEY."""
    explicit = config("DSE_FERNET_KEY", default="")
    if explicit:
        return explicit
    digest = hashlib.sha256(SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


DSE_FERNET_KEY = _derive_fernet_key()

# Query-execution safety rails (see querybuilder.executor)
DSE_QUERY_MAX_ROWS = config("DSE_QUERY_MAX_ROWS", default=5000, cast=int)
DSE_QUERY_TIMEOUT_SECONDS = config("DSE_QUERY_TIMEOUT_SECONDS", default=30, cast=int)
DSE_CONNECTION_TEST_TIMEOUT = config("DSE_CONNECTION_TEST_TIMEOUT", default=10, cast=int)

# Where file-based data sources (Excel/CSV) are materialized to SQLite
DSE_MATERIALIZED_DIR = BASE_DIR / "media" / "materialized"

# Audit trail (core.audit). Writes fail CLOSED: an action that cannot be recorded
# does not happen. Set True only to ride out an incident — it re-opens the gap
# where an action succeeds while its audit entry silently vanishes, which voids
# the attributability claim in SECURITY.md.
AUDIT_FAIL_OPEN = config("AUDIT_FAIL_OPEN", default=False, cast=bool)

# --------------------------------------------------------------------------
# Access to the system itself.
# --------------------------------------------------------------------------
# Self-service registration. OFF, and it must stay off for any regulated deployment:
# 21 CFR Part 11 §11.10(d) requires "limiting system access to authorized individuals",
# and an endpoint that mints an account for anyone who asks is the plain opposite.
#
# It used to be open, and that was survivable only by accident: a new account could see
# nothing, because visibility was decided solely by owner/shared_with and both were empty
# for a stranger. The moment `Visibility.ORG` started meaning something (core.access), an
# open POST /api/auth/register/ became "read every org-wide dataset in the laboratory" —
# for anyone who could reach the box, over the plaintext HTTP the pilot still serves.
#
# Users are created by an administrator. In a laboratory with 27 named analysts, that is
# not a burden, it is the requirement.
DSE_ALLOW_SELF_REGISTRATION = config("DSE_ALLOW_SELF_REGISTRATION", default=False,
                                     cast=bool)

# Failed-login lockout — Part 11 §11.300(d): detect and report attempts at unauthorised
# use. After this many consecutive failures the account is locked for the cooldown, every
# attempt is written to the audit trail, and the lockout itself raises an urgent notice.
#
# Locking by username is a denial-of-service trade: someone who knows a name can lock it
# out. For an on-prem laboratory of named users behind a firewall that trade is worth
# making, and the lock EXPIRES rather than requiring an administrator at 3am.
DSE_LOGIN_MAX_FAILURES = config("DSE_LOGIN_MAX_FAILURES", default=5, cast=int)
DSE_LOGIN_LOCKOUT_MINUTES = config("DSE_LOGIN_LOCKOUT_MINUTES", default=15, cast=int)

# Two-factor authentication (TOTP, RFC 6238 — accounts/totp.py, proved against the RFC's
# own published test vectors).
#
# Any user may turn it on. DSE_REQUIRE_MFA_ROLES names the roles that MUST have it — and
# switching that on cannot lock anybody out: a non-compliant user still signs in, and is
# then refused every endpoint EXCEPT the ones that let them enrol
# (accounts.middleware.MfaEnforcementMiddleware). Refusing the login instead would leave people outside the
# building with the instructions locked inside it.
#
#   DSE_REQUIRE_MFA_ROLES=admin,qa_manager,lab_manager
DSE_REQUIRE_MFA_ROLES = config(
    "DSE_REQUIRE_MFA_ROLES", default="",
    cast=lambda v: [s.strip() for s in str(v).split(",") if s.strip()])
DSE_MFA_ISSUER = config("DSE_MFA_ISSUER", default="DIS")

# Record types whose modification requires a stated reason (Part 11 §11.10(e) change
# control). Empty by default — turning it on rejects any update that arrives without a
# reason, so it is a deliberate act, not a surprise. `preflight` reports that it is off.
DSE_REQUIRE_CHANGE_REASON = config(
    "DSE_REQUIRE_CHANGE_REASON", default="",
    cast=lambda v: [s.strip() for s in str(v).split(",") if s.strip()])

# The Operational Qualification report for THIS build — written by `manage.py qualify
# --json` in CI and served, read-only, at /api/validation/oq/. It is deliberately an
# artefact rather than something production recomputes: the OQ executes the test suite
# against a test database, and in a validated system it is evidence about the build that
# was qualified, not a thing you re-run against live data. If the file is absent, the API
# says so — an unqualified build must never look like a qualified one.
DSE_OQ_REPORT_PATH = config("DSE_OQ_REPORT_PATH",
                            default=str(BASE_DIR / "validation" / "oq_report.json"))

# --------------------------------------------------------------------------
# Cache. Declared explicitly rather than leaning on Django's implicit default,
# because something now depends on it: the Lab Health tiles (semantics.health)
# would otherwise issue six LIMS queries on every home-page load, for numbers that
# only move on a replication cadence measured in hours.
#
# LocMem is per-process, so N gunicorn workers hold N copies — fine for a 60s TTL
# on a home-page tile. Point this at Redis if that ever stops being true.
# --------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "dis-default",
    }
}

# --------------------------------------------------------------------------
# AI assistant — provider selection
#
# LLM_PROVIDER selects where generation runs:
#   "anthropic" -> the Claude API (cloud)
#   "local"     -> an on-prem OpenAI-compatible server (Ollama now, vLLM on the
#                  GPU box later). When "local", the app makes NO outbound calls
#                  to Anthropic — point LLM_BASE_URL at an internal host to keep
#                  everything air-gapped. Swapping Ollama -> vLLM is just a
#                  change of LLM_BASE_URL + LLM_MODEL (no code change).
# --------------------------------------------------------------------------
# NOTE: the default is "local", NOT "anthropic". The product's core promise is
# that lab data stays on-premise, and a default of "anthropic" made that promise
# one environment variable deep: a missing .env, a container started without
# env_file, or a typo in LLM_PROVIDER would have silently routed prompts — which
# include sample LIMS rows (see ai.context) — to api.anthropic.com. Failing to
# "local, not configured" is loud and safe; failing to "cloud" is quiet and not.
LLM_PROVIDER = config("LLM_PROVIDER", default="local")

# Egress kill-switch. Outbound calls to a third-party LLM require a DELIBERATE
# opt-in, so that reaching the cloud takes an explicit act rather than an
# omission. Enforced in ai.providers.egress at provider construction:
#   * the Anthropic provider refuses to build without it;
#   * the local provider refuses an LLM_BASE_URL that is not loopback/RFC1918/
#     CGNAT (Tailscale), so LLM_PROVIDER=local can't be pointed at OpenAI either.
DSE_ALLOW_EXTERNAL_LLM = config("DSE_ALLOW_EXTERNAL_LLM", default=False, cast=bool)

# Cloud provider (Claude) — also requires DSE_ALLOW_EXTERNAL_LLM=True.
CLAUDE_API_KEY = config("CLAUDE_API_KEY", default="")
CLAUDE_MODEL = config("CLAUDE_MODEL", default="claude-sonnet-4-6")

# Local provider (OpenAI-compatible). Defaults target a local Ollama daemon;
# for vLLM set LLM_BASE_URL=http://<gpu-host>:8000/v1 and
# LLM_MODEL=Qwen/Qwen2.5-7B-Instruct-AWQ (or similar).
LLM_BASE_URL = config("LLM_BASE_URL", default="http://127.0.0.1:11434/v1")
LLM_MODEL = config("LLM_MODEL", default="qwen2.5:7b-instruct")

# The digest of the model the system was QUALIFIED against. Written by `make pin`.
#
# LLM_MODEL is a mutable tag, and Ollama has no digest-pinned reference — so "pin the
# model" is advice nobody can follow. This is the answerable question instead: has it
# MOVED? If someone re-pulls the tag, the weights can change silently, and every accuracy
# number, every golden-set pass and the whole /validation page go on describing a model
# that is no longer the one answering questions. `manage.py preflight` compares this
# against what the host is actually serving and FAILS on a mismatch, because at that point
# the qualification is not stale — it is void.
DSE_LLM_MODEL_DIGEST = config("DSE_LLM_MODEL_DIGEST", default="")
LLM_API_KEY = config("LLM_API_KEY", default="EMPTY")
LLM_TIMEOUT = config("LLM_TIMEOUT", default=300, cast=int)
# Determinism. A regulated lab has to be able to re-run a question and get the
# same answer; "we asked again and got something else" ends a CSV conversation.
# temperature=0 + a fixed seed is the closest a hosted model gets to that, and
# it also makes tool-calling more reliable on smaller local models. Both are
# applied on EVERY provider — note the Anthropic path previously passed no
# temperature at all, so it ran at the API default of 1.0.
# LLM_NUM_CTX raises Ollama's context window (default ~4k is too small once tool
# schemas + results are in play); vLLM ignores it (set at serve time).
LLM_TEMPERATURE = config("LLM_TEMPERATURE", default=0.0, cast=float)
LLM_SEED = config("LLM_SEED", default=42, cast=int)
LLM_NUM_CTX = config("LLM_NUM_CTX", default=16384, cast=int)
# Curbs degenerate looping / language-drift that quantized models can fall into
# during long tool-use turns (Ollama reads it from options; vLLM ignores it).
LLM_REPEAT_PENALTY = config("LLM_REPEAT_PENALTY", default=1.1, cast=float)

# Agentic assistant (ai.agent) — the tool-using investigation loop.
#   AGENT_MAX_STEPS      — max reason→act→observe turns before a forced answer.
#   AGENT_MAX_TOOL_CALLS — hard backstop on total tool calls per run.
# Use a tool-capable model for LLM_PROVIDER=local (e.g. qwen2.5; the 14B variant
# is markedly more reliable at multi-step tool use than 7B).
AGENT_MAX_STEPS = config("AGENT_MAX_STEPS", default=6, cast=int)
AGENT_MAX_TOOL_CALLS = config("AGENT_MAX_TOOL_CALLS", default=24, cast=int)

# --------------------------------------------------------------------------
# Live text-to-SQL (ai.tools.ask_database / texttosql) — how much live schema
# to show the model per question. Larger schemas rely on the core-table
# allow-list in texttosql.schema until schema-RAG (Plan §4) lands.
# --------------------------------------------------------------------------
DSE_TEXTTOSQL_MAX_TABLES = config("DSE_TEXTTOSQL_MAX_TABLES", default=14, cast=int)
DSE_TEXTTOSQL_MAX_COLS = config("DSE_TEXTTOSQL_MAX_COLS", default=50, cast=int)
# When several DB connections exist and the agent didn't name one, ask_database
# falls back to this datasource (id or name) instead of stopping to ask the user.
DSE_TEXTTOSQL_DEFAULT_DATASOURCE = config("DSE_TEXTTOSQL_DEFAULT_DATASOURCE", default="")
# Validate → repair budget: generate, then up to (N-1) corrective retries when a
# gate (read-only / name allow-list / group-by / DB error / result sanity) fails.
DSE_TEXTTOSQL_MAX_ATTEMPTS = config("DSE_TEXTTOSQL_MAX_ATTEMPTS", default=3, cast=int)

# --------------------------------------------------------------------------
# LIMS → analytics-replica replication (replication app).
#   SOURCE/TARGET are connections.DataSource ids: source = live LIMS (read-only),
#   target = the replica (a Postgres schema in prod, SQLite in dev). The
#   text-to-SQL tool should point at the TARGET, never the live source.
#   FULL_HOUR is the local hour for the nightly full reconcile (catches deletes).
# --------------------------------------------------------------------------
DSE_REPLICATION_SOURCE_ID = config("DSE_REPLICATION_SOURCE_ID", default=None, cast=lambda v: int(v) if v else None)
DSE_REPLICATION_TARGET_ID = config("DSE_REPLICATION_TARGET_ID", default=None, cast=lambda v: int(v) if v else None)
DSE_REPLICATION_SCHEMA = config("DSE_REPLICATION_SCHEMA", default="")
DSE_REPLICATION_FULL_HOUR = config("DSE_REPLICATION_FULL_HOUR", default=2, cast=int)

# Who to email when the replica goes stale (replication.freshness / the scheduler's
# stale alert). A silently-failing replication otherwise serves stale data with full
# confidence and nobody finds out. Console/stderr always warns; this adds email.
DSE_ALERT_EMAILS = config("DSE_ALERT_EMAILS", default="", cast=Csv())

# --------------------------------------------------------------------------
# Forecasting source database (LIMS — external SQL Server, read-only)
#
# The `forecasting` app reads operational LIMS tables (INSTRUMENTS1_LOG,
# SAMPLE, INVENTORY_TRANS/ITEM) directly over pyodbc to train / serve the
# NeuralProphet time-series models.  It does NOT use the Django ORM, so this
# DB is kept separate from the app-metadata DB (DATABASES["default"]).
# --------------------------------------------------------------------------
# Committed, in-image oil-&-gas demo warehouse (forecasting/warehouse/) — not
# under media/, so it is never shadowed by the media Docker volume. Prefer it;
# fall back to the legacy media/ location for older checkouts.
_COMMITTED_WAREHOUSE = BASE_DIR / "forecasting" / "warehouse" / "refinery_lims.sqlite3"
_LEGACY_WAREHOUSE = BASE_DIR / "media" / "refinery_lims.sqlite3"
DEFAULT_LIMS_WAREHOUSE = str(
    _COMMITTED_WAREHOUSE if _COMMITTED_WAREHOUSE.exists() else _LEGACY_WAREHOUSE
)

FORECAST_DB = {
    # ENGINE: "mssql" (production LabWare LIMS over pyodbc) or "sqlite"
    # (self-contained LIMS warehouse file — the refinery demo).
    #
    # Each key first honours a FORECAST_DB_* override, then falls back to the
    # shared DB_* var. This lets forecasting stay on the sqlite refinery
    # warehouse (which the pre-trained models were built from) while the rest
    # of the app (datasets / doc-search NL->SQL) points DB_* at a live MSSQL
    # LIMS — the exact split needed to run forecasting on the server the way it
    # runs locally. Backward compatible: with no FORECAST_DB_* set, behaviour
    # is unchanged.
    "ENGINE": config("FORECAST_DB_ENGINE", default=config("DB_ENGINE", default="mssql")),
    "DRIVER": config("FORECAST_DB_DRIVER",
                     default=config("DB_DRIVER", default="ODBC Driver 18 for SQL Server")),
    "HOST": config("FORECAST_DB_HOST", default=config("DB_HOST", default="127.0.0.1")),
    "PORT": config("FORECAST_DB_PORT", default=config("DB_PORT", default="1433")),
    "NAME": config("FORECAST_DB_NAME", default=config("DB_NAME", default="SMJMUN_DEV")),
    "USER": config("FORECAST_DB_USER", default=config("DB_USER", default="SA")),
    "PASSWORD": config("FORECAST_DB_PASSWORD", default=config("DB_PASSWORD", default="")),
    # Path used only when ENGINE == "sqlite" (committed warehouse by default).
    "PATH": config("FORECAST_DB_SQLITE_PATH",
                   default=config("DB_SQLITE_PATH", default=DEFAULT_LIMS_WAREHOUSE)),
}

# Inventory transactions may live in their own LIMS database. Default to the
# same server/database as FORECAST_DB unless explicitly overridden.
FORECAST_INVENTORY_DB = {
    **FORECAST_DB,
    "HOST": config("DB_HOST_INVENTORY", default=FORECAST_DB["HOST"]),
    "NAME": config("DB_INVENTORY_NAME", default=FORECAST_DB["NAME"]),
}

# When True, the sample forecaster back-fills the dbo.SAMPLE.Labs column on
# every request (Portal-BE behaviour). Off by default: the lab classification
# is computed in-memory and the column write is a side-effect not needed for
# the forecast result, so we avoid mutating the source DB on a GET.
FORECAST_SYNC_LABS_COLUMN = config(
    "FORECAST_SYNC_LABS_COLUMN", default=False, cast=bool
)
# Connection timeout (seconds) for the LIMS forecast DB.
FORECAST_DB_TIMEOUT = config("FORECAST_DB_TIMEOUT", default=15, cast=int)

# --------------------------------------------------------------------------
# AI Document Search (`docsearch` app)
#
# RAG over LIMS knowledge documents (BM25 + optional MiniLM semantic rerank,
# answers synthesised by the shared Claude client) plus natural-language -> SQL
# over the live LabWare LIMS. Files live under MEDIA_ROOT; the corpus is indexed
# into a CSV fast-path. The NL->SQL source DB is configured separately from
# FORECAST_DB so forecasting can stay on the sqlite demo warehouse while document
# search queries MSSQL.
# --------------------------------------------------------------------------
DOCSEARCH_CORPUS_DIR = config(
    "DOCSEARCH_CORPUS_DIR", default=str(BASE_DIR / "media" / "docsearch" / "corpus"))
DOCSEARCH_INDEX_PATH = config(
    "DOCSEARCH_INDEX_PATH", default=str(BASE_DIR / "media" / "docsearch" / "chunks_index.csv"))
# Knowledge-base staging area: uploaded files awaiting review land here (NOT the
# corpus dir, so they are not auto-indexed until approved).
DOCSEARCH_STAGING_DIR = config(
    "DOCSEARCH_STAGING_DIR", default=str(BASE_DIR / "media" / "docsearch" / "staging"))
DOCSEARCH_FAQ_PATH = config(
    "DOCSEARCH_FAQ_PATH", default=str(BASE_DIR / "media" / "docsearch" / "standard_responses.xlsx"))
DOCSEARCH_EMBED_MODEL = config("DOCSEARCH_EMBED_MODEL", default="all-MiniLM-L12-v2")
DOCSEARCH_ENABLE_SEMANTIC = config("DOCSEARCH_ENABLE_SEMANTIC", default=True, cast=bool)
DOCSEARCH_ENABLE_OCR = config("DOCSEARCH_ENABLE_OCR", default=False, cast=bool)
DOCSEARCH_ENABLE_SQL = config("DOCSEARCH_ENABLE_SQL", default=True, cast=bool)
# Upload guards (per request): reject oversized files / too many files.
DOCSEARCH_MAX_UPLOAD_BYTES = config("DOCSEARCH_MAX_UPLOAD_BYTES", default=50 * 1024 * 1024, cast=int)
DOCSEARCH_MAX_UPLOAD_FILES = config("DOCSEARCH_MAX_UPLOAD_FILES", default=20, cast=int)
# Chunking / knowledge-base ingestion knobs.
# Passages are packed to ~CHUNK_TOKENS (hard-capped at CHUNK_MAX_TOKENS) with a
# small sentence overlap — replacing the old one-sentence-per-chunk indexing.
DOCSEARCH_CHUNK_TOKENS = config("DOCSEARCH_CHUNK_TOKENS", default=384, cast=int)
DOCSEARCH_CHUNK_MAX_TOKENS = config("DOCSEARCH_CHUNK_MAX_TOKENS", default=512, cast=int)
DOCSEARCH_CHUNK_OVERLAP = config("DOCSEARCH_CHUNK_OVERLAP", default=0.15, cast=float)
DOCSEARCH_KB_MAX_ROWS = config("DOCSEARCH_KB_MAX_ROWS", default=10000, cast=int)
# Hybrid retrieval (BM25 + dense) + cross-encoder rerank.
#   DOCSEARCH_HYBRID         — fuse BM25 with dense (semantic) retrieval (RRF)
#   DOCSEARCH_CANDIDATE_POOL — how many to pull from each leg before fusion
#   DOCSEARCH_PER_DOC_CAP    — how many distinct documents may enter the answer
#                              (was hard-coded to 1 = single doc; relaxed for
#                              cross-document questions, reranker re-tightens it)
#   DOCSEARCH_ENABLE_RERANK / DOCSEARCH_RERANK_MODEL — cross-encoder reranker.
#     Default is a small, fast CrossEncoder. For higher accuracy in production
#     set DOCSEARCH_RERANK_MODEL=BAAI/bge-reranker-base (pre-stage the weights for
#     air-gapped installs).
#   DOCSEARCH_EMBED_*_PREFIX — instruction prefixes some embedders need (bge/e5);
#     empty for MiniLM. Applied at BOTH index time (passage) and query time —
#     keep consistent or recall silently degrades.
#   DOCSEARCH_VECTOR_BACKEND — "numpy" (persisted .npy, in-memory cosine; fine to
#     ~hundreds of thousands of chunks) or "pgvector" (Postgres, for large scale).
DOCSEARCH_HYBRID = config("DOCSEARCH_HYBRID", default=True, cast=bool)
DOCSEARCH_CANDIDATE_POOL = config("DOCSEARCH_CANDIDATE_POOL", default=50, cast=int)
DOCSEARCH_RRF_K = config("DOCSEARCH_RRF_K", default=60, cast=int)
DOCSEARCH_PER_DOC_CAP = config("DOCSEARCH_PER_DOC_CAP", default=3, cast=int)
DOCSEARCH_MAX_CHUNKS = config("DOCSEARCH_MAX_CHUNKS", default=15, cast=int)
DOCSEARCH_RERANK_KEEP = config("DOCSEARCH_RERANK_KEEP", default=10, cast=int)
DOCSEARCH_ENABLE_RERANK = config("DOCSEARCH_ENABLE_RERANK", default=True, cast=bool)
DOCSEARCH_RERANK_MODEL = config(
    "DOCSEARCH_RERANK_MODEL", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
DOCSEARCH_EMBED_QUERY_PREFIX = config("DOCSEARCH_EMBED_QUERY_PREFIX", default="")
DOCSEARCH_EMBED_PASSAGE_PREFIX = config("DOCSEARCH_EMBED_PASSAGE_PREFIX", default="")
DOCSEARCH_VECTORS_PATH = config(
    "DOCSEARCH_VECTORS_PATH",
    default=str(BASE_DIR / "media" / "docsearch" / "chunk_vectors.npy"))
DOCSEARCH_VECTOR_BACKEND = config("DOCSEARCH_VECTOR_BACKEND", default="numpy")  # numpy | pgvector
# pgvector backend (used only when DOCSEARCH_VECTOR_BACKEND="pgvector"). DSN is a
# libpq connection string to a Postgres with the `vector` extension available.
DOCSEARCH_PGVECTOR_DSN = config("DOCSEARCH_PGVECTOR_DSN", default="")
DOCSEARCH_PGVECTOR_TABLE = config("DOCSEARCH_PGVECTOR_TABLE", default="docsearch_chunk_vectors")

# NL->SQL source DB (live LabWare LIMS over SQL Server by default). Reuses the
# DB_* credentials but has its own ENGINE switch so it stays on MSSQL even when
# DB_ENGINE=sqlite (forecasting demo mode).
DOCSEARCH_DB = {
    "ENGINE": config("DOCSEARCH_DB_ENGINE", default="mssql"),
    "DRIVER": config("DB_DRIVER", default="ODBC Driver 18 for SQL Server"),
    "HOST": config("DB_HOST", default="127.0.0.1"),
    "PORT": config("DB_PORT", default="1433"),
    "NAME": config("DB_NAME", default="SMJMUN_DEV"),
    "USER": config("DB_USER", default="SA"),
    "PASSWORD": config("DB_PASSWORD", default=""),
    "PATH": config("DB_SQLITE_PATH", default=DEFAULT_LIMS_WAREHOUSE),
}
DOCSEARCH_DB_TIMEOUT = config("DOCSEARCH_DB_TIMEOUT", default=20, cast=int)

# --------------------------------------------------------------------------
# Email (alert notifications).  Dev default = console backend (no SMTP needed).
# --------------------------------------------------------------------------
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = config("EMAIL_HOST", default="localhost")
EMAIL_PORT = config("EMAIL_PORT", default=25, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=False, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="dse-alerts@localhost")

# --------------------------------------------------------------------------
# Enterprise reporting: branding, audited exports, scheduled reports.
# Every default keeps the pre-existing behaviour, so a deployment that sets
# none of these (e.g. EGPC) is unaffected.
# --------------------------------------------------------------------------
# Branding — served as-is by GET /api/branding/ (blank = not set: the SPA then
# uses its build-time VITE_* values, then its own defaults). Server-side files
# and emails fall back to the platform name ("DIS", see
# core.branding) when the product name is blank, so an existing deployment is
# not renamed on upgrade.
DIS_BRAND_PRODUCT_NAME = config("DIS_BRAND_PRODUCT_NAME", default="")
DIS_BRAND_SHORT_NAME = config("DIS_BRAND_SHORT_NAME", default="")
DIS_BRAND_TAGLINE = config("DIS_BRAND_TAGLINE", default="")
DIS_BRAND_FOOTER = config("DIS_BRAND_FOOTER", default="")
# "full" = the complete platform; "reporting" = a reporting-only deployment
# (the SPA hides AI, forecasting and lab-specific modules).
DIS_PROFILES = ("full", "reporting")
DIS_PROFILE = config("DIS_PROFILE", default="full").strip().lower()
if DIS_PROFILE not in DIS_PROFILES:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        f"DIS_PROFILE={DIS_PROFILE!r} is not one of {', '.join(DIS_PROFILES)}.")

# Time zone for report stamps ("Data as of", "Generated at"), file-name dates
# and the scheduled-report send hour / calendar months.
DIS_REPORT_TZ = config("DIS_REPORT_TZ", default="Asia/Riyadh")
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ZoneInfo(DIS_REPORT_TZ)
except Exception as _exc:  # noqa: BLE001 — fail at startup, not mid-export
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(f"DIS_REPORT_TZ={DIS_REPORT_TZ!r} is not a valid time zone.") from _exc
# Local hour (0-23, in DIS_REPORT_TZ) from which scheduled reports go out.
# Unset = send whenever send_scheduled_reports runs and a report is due
# (the original behaviour).
DIS_REPORT_SEND_HOUR = config("DIS_REPORT_SEND_HOUR", default=None, cast=_optional_int)
if DIS_REPORT_SEND_HOUR is not None and not 0 <= DIS_REPORT_SEND_HOUR <= 23:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("DIS_REPORT_SEND_HOUR must be an hour between 0 and 23.")
# True = "monthly" reports go once per calendar month (from the 1st);
# False = every 30 days (the original behaviour).
DIS_REPORT_CALENDAR_MONTHS = config("DIS_REPORT_CALENDAR_MONTHS", default=False, cast=bool)
# Email subject prefixes (scheduled reports / threshold alerts).
DIS_EMAIL_SUBJECT_PREFIX = config("DIS_EMAIL_SUBJECT_PREFIX", default="[DIS Report]")
DIS_ALERT_SUBJECT_PREFIX = config("DIS_ALERT_SUBJECT_PREFIX", default="[DIS Alert]")
# Server-side export ceilings (GET /api/datasets/<id>/export/ and scheduled
# reports): a larger result is refused with HTTP 413 rather than truncated.
DIS_EXPORT_MAX_ROWS = config("DIS_EXPORT_MAX_ROWS", default=100000, cast=int)
DIS_EXPORT_PDF_MAX_ROWS = config("DIS_EXPORT_PDF_MAX_ROWS", default=2000, cast=int)


def _pairs(value) -> dict:
    """"a=b;c=d" -> {"a": "b", "c": "d"}; blank -> {}."""
    out = {}
    for part in str(value or "").split(";"):
        key, sep, val = part.partition("=")
        if sep and key.strip() and val.strip():
            out[key.strip()] = val.strip()
    return out


# Clearance for classified certified numbers (semantics.access): class -> the Django
# group whose members may see it, e.g. "cost=Cost cleared;bid_confidential=Bid
# confidential". A class with no group is visible to administrators only.
DIS_CLASS_CLEARANCE = _pairs(config("DIS_CLASS_CLEARANCE", default=""))
