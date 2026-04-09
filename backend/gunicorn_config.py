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
        from backend.autocheckout import start_worker
    except ImportError:
        from app import start_monitor
        from autocheckout import start_worker

    # Start the booking worker thread FIRST (so it's ready when tickets go live)
    start_worker()
    # Then start the availability monitor loop
    start_monitor()
