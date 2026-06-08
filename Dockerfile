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

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120"]
