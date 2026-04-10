# ══════════════════════════════════════════════════════════════════════════════
# TicketAlert — Production Dockerfile for Railway
# ══════════════════════════════════════════════════════════════════════════════
#
# This builds a container with:
#   - Python 3.11 + all pip dependencies
#   - Chromium browser + all system libraries Playwright needs
#   - playwright-stealth for anti-detection
#   - Gunicorn WSGI server with background worker threads
#
# Railway auto-detects this Dockerfile and uses it instead of NIXPACKS.

FROM python:3.11-slim

# Prevent .pyc files and ensure logs appear immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── 1. Install system dependencies for Playwright Chromium ───────────────────
# These are the exact libraries Chromium needs on Debian/Ubuntu.
# Installing them explicitly prevents "missing .so" errors on Railway.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxshmfence1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Install Python dependencies ──────────────────────────────────────────
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# ── 3. Install Playwright Chromium with ALL dependencies ────────────────────
# The --with-deps flag ensures Playwright also installs any system libs
# it needs beyond what we installed above (belt and suspenders).
RUN playwright install --with-deps chromium

# ── 4. Copy application code ────────────────────────────────────────────────
COPY . .

# ── 5. Health check ─────────────────────────────────────────────────────────
# Use wget (already installed above) — avoids Python nested-quote escaping issues
# and lets the shell expand ${PORT} correctly.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --spider -q http://127.0.0.1:${PORT:-8000}/health || exit 1

# ── 6. Start Gunicorn ───────────────────────────────────────────────────────
# gunicorn_config.py starts both the monitor loop AND the booking worker thread
CMD ["gunicorn", "-c", "backend/gunicorn_config.py", "backend.app:app"]
