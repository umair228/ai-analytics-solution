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
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=config("JWT_ACCESS_MINUTES", default=60, cast=int)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=config("JWT_REFRESH_DAYS", default=7, cast=int)
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
LLM_PROVIDER = config("LLM_PROVIDER", default="anthropic")

# Cloud provider (Claude)
CLAUDE_API_KEY = config("CLAUDE_API_KEY", default="")
CLAUDE_MODEL = config("CLAUDE_MODEL", default="claude-sonnet-4-6")

# Local provider (OpenAI-compatible). Defaults target a local Ollama daemon;
# for vLLM set LLM_BASE_URL=http://<gpu-host>:8000/v1 and
# LLM_MODEL=Qwen/Qwen2.5-7B-Instruct-AWQ (or similar).
LLM_BASE_URL = config("LLM_BASE_URL", default="http://127.0.0.1:11434/v1")
LLM_MODEL = config("LLM_MODEL", default="qwen2.5:7b-instruct")
LLM_API_KEY = config("LLM_API_KEY", default="EMPTY")
LLM_TIMEOUT = config("LLM_TIMEOUT", default=300, cast=int)
# Low temperature → reliable, repeatable tool-calling (esp. for smaller local
# models). LLM_NUM_CTX raises Ollama's context window (default ~4k is too small
# once tool schemas + results are in play); vLLM ignores it (set at serve time).
LLM_TEMPERATURE = config("LLM_TEMPERATURE", default=0.2, cast=float)
LLM_NUM_CTX = config("LLM_NUM_CTX", default=16384, cast=int)

# Agentic assistant (ai.agent) — the tool-using investigation loop.
#   AGENT_MAX_STEPS      — max reason→act→observe turns before a forced answer.
#   AGENT_MAX_TOOL_CALLS — hard backstop on total tool calls per run.
# Use a tool-capable model for LLM_PROVIDER=local (e.g. qwen2.5; the 14B variant
# is markedly more reliable at multi-step tool use than 7B).
AGENT_MAX_STEPS = config("AGENT_MAX_STEPS", default=6, cast=int)
AGENT_MAX_TOOL_CALLS = config("AGENT_MAX_TOOL_CALLS", default=24, cast=int)

# --------------------------------------------------------------------------
# Forecasting source database (LIMS — external SQL Server, read-only)
#
# The `forecasting` app reads operational LIMS tables (INSTRUMENTS1_LOG,
# SAMPLE, INVENTORY_TRANS/ITEM) directly over pyodbc to train / serve the
# NeuralProphet time-series models.  It does NOT use the Django ORM, so this
# DB is kept separate from the app-metadata DB (DATABASES["default"]).
# --------------------------------------------------------------------------
FORECAST_DB = {
    # ENGINE: "mssql" (production LabWare LIMS over pyodbc) or "sqlite"
    # (self-contained LIMS warehouse file — the refinery demo).
    "ENGINE": config("DB_ENGINE", default="mssql"),
    "DRIVER": config("DB_DRIVER", default="ODBC Driver 18 for SQL Server"),
    "HOST": config("DB_HOST", default="127.0.0.1"),
    "PORT": config("DB_PORT", default="1433"),
    "NAME": config("DB_NAME", default="SMJMUN_DEV"),
    "USER": config("DB_USER", default="SA"),
    "PASSWORD": config("DB_PASSWORD", default=""),
    # Path used only when ENGINE == "sqlite".
    "PATH": config("DB_SQLITE_PATH",
                   default=str(BASE_DIR / "media" / "refinery_lims.sqlite3")),
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
    "PATH": config("DB_SQLITE_PATH",
                   default=str(BASE_DIR / "media" / "refinery_lims.sqlite3")),
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
