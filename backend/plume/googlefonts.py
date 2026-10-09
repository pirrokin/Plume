"""Automatic download of open fonts from Google Fonts.

Many PDFs (Canva, web pages, Google Docs) use fonts that are freely available
on Google Fonts but not installed locally. When a document uses one of them,
the static font files are downloaded once and added to the font index, so
edited text uses the real typeface instead of a look-alike.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .fontnames import family_key

log = logging.getLogger(__name__)

ENABLED = os.environ.get("PLUME_GOOGLE_FONTS", "1") not in ("0", "false", "no")
METADATA_URL = "https://fonts.google.com/metadata/fonts"
CSS_URL = "https://fonts.googleapis.com/css2"
MAX_AGE = 14 * 24 * 3600

# PDF family names that were renamed on Google Fonts.
RENAMED = {
    "sourcesanspro": "sourcesans3",
    "sourceserifpro": "sourceserif4",
    "latoregular": "lato",
}

_FACE_RE = re.compile(
    r"@font-face\s*{[^}]*?font-style:\s*(\w+);[^}]*?font-weight:\s*(\d+);[^}]*?src:\s*url\(([^)]+)\)\s*format\('truetype'\)",
    re.S,
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # fall back to the system store
        return ssl.create_default_context()


_SSL = _ssl_context()


def _fetch(url: str, timeout: float = 20) -> bytes:
    # Without a browser User-Agent, Google serves plain TrueType files.
    req = urllib.request.Request(url, headers={"User-Agent": "plume-pdf"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as res:
        return res.read()


class GoogleFonts:
    def __init__(self, root: Path | None = None):
        self.root = root or (config.DATA_DIR / "fonts" / "google")
        self.meta_path = config.CACHE_DIR / "google-fonts.json"
        self._catalog: dict[str, dict] | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Event] = {}
        self._failed: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gfonts")
        self._refreshing = threading.Event()

    # ---------------------------------------------------------------- catalog
    def catalog(self, block: bool = False) -> dict[str, dict]:
        """Family catalog; never waits for the network unless `block` is set."""
        if self._catalog is not None:
            return self._catalog
        with self._lock:
            if self._catalog is not None:
                return self._catalog
            cached = None
            fresh = False
            try:
                cached = json.loads(self.meta_path.read_text())
                fresh = time.time() - self.meta_path.stat().st_mtime < MAX_AGE
            except (OSError, ValueError):
                cached = None
            if cached is not None and (fresh or not block):
                self._catalog = cached
                if not fresh:
                    threading.Thread(target=self._refresh, daemon=True).start()
                return cached
        if not block:
            threading.Thread(target=self._refresh, daemon=True).start()
            return {}
        self._refresh()
        return self._catalog or {}

    def _refresh(self) -> None:
        if not ENABLED or self._refreshing.is_set():
            return
        self._refreshing.set()
        try:
            raw = _fetch(METADATA_URL, timeout=30).decode("utf-8")
            full = json.loads(raw[raw.index("{"):])
            data = {
                family_key(f["family"]): {
                    "family": f["family"],
                    "category": f.get("category", ""),
                    "styles": sorted(f.get("fonts", {}).keys()),
                }
                for f in full.get("familyMetadataList", [])
            }
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            self.meta_path.write_text(json.dumps(data))
            self._catalog = data
            from .fontindex import get_index

            get_index().generation += 1  # families may now resolve differently
        except Exception as exc:
            log.info("Google Fonts catalog unavailable: %s", exc)
        finally:
            self._refreshing.clear()

    def lookup(self, key: str) -> dict | None:
        if not ENABLED:
            return None
        key = RENAMED.get(key, key)
        return self.catalog().get(key)

    def family_dir(self, key: str) -> Path:
        return self.root / RENAMED.get(key, key)

    def is_downloaded(self, key: str) -> bool:
        d = self.family_dir(key)
        return d.is_dir() and any(d.glob("*.ttf"))

    def _index(self, key: str) -> None:
        """Make a family downloaded earlier (maybe by another process) known to the index."""
        from .fontindex import get_index

        index = get_index()
        if not index.faces_for_key(RENAMED.get(key, key)):
            index.add_directory(self.family_dir(key))

    # ---------------------------------------------------------------- download
    def ensure(self, key: str, wait: float = 0.0) -> bool:
        """Download a family in the background; optionally wait for it."""
        entry = self.lookup(key)
        if entry is None:
            return False
        if self.is_downloaded(key):
            self._index(key)
            return True
        if time.time() - self._failed.get(key, 0) < 600:
            return False
        with self._lock:
            event = self._pending.get(key)
            if event is None:
                event = threading.Event()
                self._pending[key] = event
                self._pool.submit(self._download, key, entry, event)
        if wait > 0:
            event.wait(wait)
        return self.is_downloaded(key)

    def _download(self, key: str, entry: dict, event: threading.Event) -> None:
        try:
            specs = []
            for style in entry["styles"]:
                m = re.match(r"^(\d+)(i?)$", style)
                if m:
                    specs.append((1 if m.group(2) else 0, int(m.group(1))))
            if not specs:
                raise ValueError("no static styles")
            specs.sort()
            query = "ital,wght@" + ";".join(f"{i},{w}" for i, w in specs)
            url = f"{CSS_URL}?family={urllib.parse.quote(entry['family'])}:{query}"
            css = _fetch(url).decode("utf-8")
            faces = _FACE_RE.findall(css)
            if not faces:
                raise ValueError("no TrueType faces in stylesheet")
            target = self.family_dir(key)
            tmp = target.with_suffix(".tmp")
            tmp.mkdir(parents=True, exist_ok=True)
            for style, weight, font_url in faces:
                name = f"{key}-{weight}{'i' if style == 'italic' else ''}.ttf"
                (tmp / name).write_bytes(_fetch(font_url.strip("'\"")))
            if target.exists():
                for f in target.glob("*"):
                    f.unlink()
                target.rmdir()
            tmp.rename(target)
            log.info("downloaded Google font %s (%d faces)", entry["family"], len(faces))
            from .fontindex import get_index

            get_index().add_directory(target)
        except Exception as exc:
            log.warning("Google font %s not downloaded: %s", key, exc)
            self._failed[key] = time.time()
        finally:
            with self._lock:
                self._pending.pop(key, None)
            event.set()


_GF: GoogleFonts | None = None


def google_fonts() -> GoogleFonts:
    global _GF
    if _GF is None:
        _GF = GoogleFonts()
    return _GF
