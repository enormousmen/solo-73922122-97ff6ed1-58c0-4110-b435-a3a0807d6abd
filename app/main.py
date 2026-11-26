"""Local development entry point: ``python -m app.main``."""
from __future__ import annotations

import os

from .server import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # threaded=True keeps polling responsive while test worker threads run.
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
