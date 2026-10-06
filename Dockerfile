# =============================================================================
# SvodkaPress NextGen — production image
# Multi-stage: dependencies are compiled in `builder`, only the resulting
# virtualenv + runtime libraries are shipped in `runtime`.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — compile wheels into an isolated virtualenv
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libpq-dev \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV" && pip install --upgrade pip setuptools wheel

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim image with Tesseract OCR, OpenCV and Chromium
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    DEBIAN_FRONTEND=noninteractive

# System runtime dependencies:
#   tesseract-ocr / -rus  -> OCR watermark detection (rus+eng)
#   libgl1 / libglx-mesa0 -> OpenCV runtime (supersede libgl1-mesa-glx, which
#                            was removed from Debian bookworm)
#   libglib2.0-0          -> OpenCV / Chromium runtime
#   curl                  -> container healthcheck
#   git                   -> VCS operations for deployment tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        libgl1 \
        libglx-mesa0 \
        libglib2.0-0 \
        libpq5 \
        curl \
        git \
        ca-certificates \
        tini \
        openssh-client \
        fonts-liberation \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Chromium + its OS-level dependencies. Installed as root into a shared path so
# the non-root runtime user can execute the browser.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN groupadd --gid 1000 svodka \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash svodka

WORKDIR /app

COPY --chown=svodka:svodka . /app

# Created with uid/gid 1000 so named volumes mounted here inherit an ownership
# the non-root runtime user can write to. A mount point missing from this list is
# created by Docker as root, and the container then cannot write to it — which is
# exactly how the fastembed model cache failed on first deploy.
RUN mkdir -p /app/logs /app/storage/media /app/storage/fastembed /app/secrets \
    && chown -R svodka:svodka /app /ms-playwright

USER svodka

EXPOSE 8000

# Liveness only: /health fails when PostgreSQL is unreachable but tolerates a
# Redis outage, so a degraded queue does not mark a serving container unhealthy.
# Use /health/ready for dependency gating.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
