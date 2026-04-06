"""Convenience entry point for running the app from the repo root."""
import os

from backend.app import app, start_monitor


if __name__ == "__main__":
    start_monitor()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
