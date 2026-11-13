# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - builder
#
# Wheels are built here so compilers and headers (argon2-cffi needs them) never
# reach the runtime image. A dependency-only layer is installed first so that
# editing application code does not invalidate the pip cache.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only the metadata first - this layer is cached until dependencies change.
COPY pyproject.toml README.md ./
COPY src/api_framework/__init__.py src/api_framework/__init__.py
RUN pip install --upgrade pip && pip install .

# Now the real source, and install again to pick it up.
COPY src ./src
RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2 - runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    APP_LOG_JSON=true

# Run as an unprivileged user. A container process running as root that is
# compromised has a far larger blast radius, and nothing here needs root.
RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Uses the readiness probe, so the container is only "healthy" once its
# dependencies are actually reachable - not merely once the port is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status == 200 else 1)"

ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "api_framework.app:app", "--host", "0.0.0.0", "--port", "8000"]
