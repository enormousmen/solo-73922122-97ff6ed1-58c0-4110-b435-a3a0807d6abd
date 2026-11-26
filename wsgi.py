"""Gunicorn entry point (single process, multiple request threads).

Exactly one worker is intentional: round state lives in memory and test
worker threads run inside this process. `python wsgi.py` also works for a
quick local check without gunicorn.
"""
import os

from app.server import create_app

application = create_app()
app = application

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    application.run(host="0.0.0.0", port=port, threaded=True)
