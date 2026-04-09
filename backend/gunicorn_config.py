"""Gunicorn config — starts the background monitor after the first worker forks."""
import os

workers = 1
threads = 4
timeout = 120
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "sync"

def post_fork(server, worker):
    try:
        from backend.app import start_monitor
        from backend.autocheckout import start_checkout_workers
    except ImportError:
        from app import start_monitor
        from autocheckout import start_checkout_workers
    start_checkout_workers()
    start_monitor()
