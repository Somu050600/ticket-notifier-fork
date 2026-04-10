"""
Gunicorn config — starts background threads after the first worker forks.

Two background threads are launched:
  1. Monitor loop  — polls watched URLs for availability changes
  2. Booking worker — processes checkout jobs from the queue (Playwright)
"""
import os

workers = 1
threads = 4
timeout = 120
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "sync"


def post_fork(server, worker):
    """Called after gunicorn forks the worker process."""
    try:
        from backend.app import start_monitor
    except ImportError:
        from app import start_monitor

    # start_monitor() calls start_worker() internally, so a single call does both
    start_monitor()

