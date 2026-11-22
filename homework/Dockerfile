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

COPY homework/app.py homework/homework_data.py homework/gunicorn.conf.py \
     homework/tutor.py homework/mcp_server.py homework/web_tools.py homework/claude_broker_client.py ./
COPY homework/templates/ templates/
COPY homework/tutor_profile/ tutor_profile/

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# uid 1000 = the claude-broker user on the host: its socket folder is private to that user.
RUN adduser --disabled-password --gecos '' --uid 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5000/health', timeout=5); exit(0 if r.status_code == 200 else 1)" || exit 1

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
