# Build context is the repository root (see docker-compose.yml) so that the
# mva/ library can be installed into the image.

# Multi-stage build: Builder stage
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY homework/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Install the MVA Canvas library
COPY mva/pyproject.toml mva/README.md /build/mva/
COPY mva/src /build/mva/src
RUN pip install --no-cache-dir --user /build/mva

# Multi-stage build: Runtime stage
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /root/.local /usr/local

COPY homework/app.py homework/homework_data.py homework/gunicorn.conf.py ./
COPY homework/templates/ templates/

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5000/health', timeout=5); exit(0 if r.status_code == 200 else 1)" || exit 1

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
