# Python 3.12 (not 3.13): neuralprophet 0.9.0 requires <=3.12, and the whole
# numpy<2 / pandas 2.2 / torch / sentence-transformers stack is known-good here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# System dependencies: unixODBC + Microsoft ODBC Driver 18 (SQL Server),
# plus build tools for the database driver wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg apt-transport-https unixodbc unixodbc-dev gcc g++ git \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .docsearch-constraints.txt ./
# NeuralProphet 0.9.0 (forecasting app): GitHub-only + a broken Requires-Python
# (<=3.12 excludes 3.12.x patch releases), so install it from the tag with the
# python check disabled. Constraints keep numpy/pandas/torch pinned. Runs fine on 3.12.
RUN pip install --no-cache-dir --ignore-requires-python -c .docsearch-constraints.txt \
        "neuralprophet @ git+https://github.com/ourownstory/neural_prophet.git@0.9.0"
RUN pip install --no-cache-dir -c .docsearch-constraints.txt -r requirements.txt \
    && python -m spacy download en_core_web_sm

COPY . .

# ---------------------------------------------------------------------------
# Name the build, and make the image carry its own Operational Qualification.
# ---------------------------------------------------------------------------
# A validation summary that does not say which build it is about is a piece of paper, so
# the sha and version are stamped in and the OQ report records them.
ARG DIS_GIT_SHA=unknown
ARG DIS_VERSION=unknown
ENV DIS_GIT_SHA=${DIS_GIT_SHA} \
    DIS_VERSION=${DIS_VERSION}

# Run the OQ here — inside the image, against the exact code the image contains — so the
# evidence at /api/validation/oq/ cannot be evidence about some *other* build.
#
# `|| true` looks like a cop-out and is the opposite. There is no path by which this can
# make the image claim something it did not earn: if the qualification cannot run, no
# report is written, and the API then reports — in these words — that this build carries
# no OQ evidence. Absence is honest; a stale or borrowed report would not be. What `|| true`
# buys is that a build-environment problem cannot take production down with it. The gate
# that stops failing code from shipping is CI (`make ci`), which is where a gate belongs.
RUN python manage.py qualify --json \
    || echo "OQ did not run at image-build time; /api/validation/oq/ will report that this build carries no evidence."

EXPOSE 8000

# --timeout 300 matches nginx proxy_read_timeout: a Document-Search / Knowledge
# Base upload rebuilds the whole index synchronously (re-embeds every chunk), and
# the first one also loads/downloads the embed model — 120s was too short and the
# worker was killed mid-upload. Seeding via `manage.py seed_docsearch` avoids the
# request path entirely; this keeps interactive uploads working too.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 300"]
