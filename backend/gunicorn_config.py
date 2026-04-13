"""
Gunicorn config — starts background threads after the first worker forks.

Two background threads are launched:
  1. Monitor loop  — polls watched URLs for availability changes
  2. Booking worker — processes checkout jobs from the queue (Playwright)
"""
import os
import logging

logger = logging.getLogger("ticketalert.gunicorn")

# ── Worker configuration ─────────────────────────────────────────────────────
# MUST be 1 worker — the monitor loop and booking worker use in-process state.
# Multiple workers would create duplicate monitors and race on the database.
workers = 1
threads = 4
timeout = 180              # Playwright operations can be slow
graceful_timeout = 30      # Time to finish in-flight requests on reload
keep_alive = 5
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "gthread"   # gthread handles concurrent requests better with threads

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# ── Lifecycle hooks ──────────────────────────────────────────────────────────

def post_fork(server, worker):
    """Called after gunicorn forks the worker process."""
    try:
        from backend.app import start_monitor
    except ImportError:
        from app import start_monitor

    # start_monitor() calls start_worker() internally, so a single call does both
    start_monitor()
    logger.info("Monitor loop + booking worker started in worker PID %s", worker.pid)


def worker_exit(server, worker):
    """Cleanup on worker shutdown."""
    logger.info("Worker PID %s exiting — background threads will be killed (daemon)", worker.pid)

