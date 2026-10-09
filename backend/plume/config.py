"""Runtime settings, overridable through environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("PLUME_DATA_DIR", ROOT / "data")).resolve()
DOCS_DIR = DATA_DIR / "sessions"
CACHE_DIR = DATA_DIR / "cache"

FRONTEND_DIST = Path(os.environ.get("PLUME_FRONTEND_DIST", ROOT / "frontend" / "dist")).resolve()

# Largest accepted upload, in megabytes.
MAX_UPLOAD_MB = int(os.environ.get("PLUME_MAX_UPLOAD_MB", "200"))

# Number of versions kept on disk for undo/redo.
MAX_VERSIONS = int(os.environ.get("PLUME_MAX_VERSIONS", "60"))

# Send the session cookie only over HTTPS (set to 1 behind a TLS proxy that
# does not forward X-Forwarded-Proto).
COOKIE_SECURE = os.environ.get("PLUME_COOKIE_SECURE", "0") in ("1", "true", "yes")

# Documents only live for the browser session that opened them.
# Deleted after this long without a heartbeat from the tab:
SESSION_IDLE_MINUTES = float(os.environ.get("PLUME_SESSION_IDLE_MINUTES", "30"))
# Deleted this long after the tab was closed (a reload cancels it):
SESSION_CLOSE_GRACE_SECONDS = float(os.environ.get("PLUME_SESSION_CLOSE_GRACE_SECONDS", "45"))
# Deleted after this long whatever happens:
SESSION_MAX_HOURS = float(os.environ.get("PLUME_SESSION_MAX_HOURS", "12"))
# Kept for compatibility with earlier configurations.
RETENTION_HOURS = SESSION_MAX_HOURS

# Largest number of pages accepted.
MAX_PAGES = int(os.environ.get("PLUME_MAX_PAGES", "500"))


def font_dirs() -> list[Path]:
    """Directories scanned for installed fonts, most specific first."""
    dirs: list[Path] = [ROOT / "backend" / "fonts", DATA_DIR / "fonts"]
    extra = os.environ.get("PLUME_FONT_DIRS")
    if extra:
        dirs += [Path(p) for p in extra.split(os.pathsep) if p]
    home = Path.home()
    if sys.platform == "darwin":
        dirs += [
            home / "Library" / "Fonts",
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            # Fonts shipped with Microsoft Office (Calibri, Cambria, ...).
            Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts"),
        ]
    elif sys.platform.startswith("win"):
        windir = Path(os.environ.get("WINDIR", "C:/Windows"))
        dirs += [windir / "Fonts", home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"]
    else:
        dirs += [home / ".fonts", home / ".local" / "share" / "fonts", Path("/usr/local/share/fonts"), Path("/usr/share/fonts")]
    return [d for d in dirs if d.is_dir()]


def ensure_dirs() -> None:
    for d in (DATA_DIR, DOCS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
