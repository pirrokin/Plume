"""Entry point for Passenger (o2switch « Setup Python App »).

Passenger speaks WSGI; Plume is an ASGI application, bridged by a2wsgi.
"""

import os
import sys

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(APP_ROOT, "backend"))
os.environ.setdefault("PLUME_DATA_DIR", os.path.join(os.path.expanduser("~"), "plume-data"))
os.environ.setdefault("PLUME_FRONTEND_DIST", os.path.join(APP_ROOT, "frontend", "dist"))

from a2wsgi import ASGIMiddleware  # noqa: E402

from plume.app import app as asgi_app  # noqa: E402

application = ASGIMiddleware(asgi_app)
