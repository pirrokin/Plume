"""Runtime settings, overridable through environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("PLUME_DATA_DIR", ROOT / "data")).resolve()
DOCS_DIR = DATA_DIR / "documents"
CACHE_DIR = DATA_DIR / "cache"

FRONTEND_DIST = Path(os.environ.get("PLUME_FRONTEND_DIST", ROOT / "frontend" / "dist")).resolve()

# Largest accepted upload, in megabytes.
MAX_UPLOAD_MB = int(os.environ.get("PLUME_MAX_UPLOAD_MB", "200"))

# Number of versions kept on disk for undo/redo.
MAX_VERSIONS = int(os.environ.get("PLUME_MAX_VERSIONS", "60"))

# Documents untouched for this many hours are deleted at startup.
RETENTION_HOURS = float(os.environ.get("PLUME_RETENTION_HOURS", "72"))


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
