"""Index of the fonts installed on the machine (or bundled with the app)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from . import config
from .fontnames import family_key, _style_info

log = logging.getLogger(__name__)

_EXTS = (".ttf", ".otf", ".ttc", ".otc")
_CACHE_VERSION = 3

# Alternatives tried, in order, when a family is not installed.
# Metric-compatible open fonts come right after the original.
ALIASES: dict[str, list[str]] = {
    "helvetica": ["helvetica", "arial", "liberationsans", "arimo", "nimbussans", "texgyreheros"],
    "helveticaneue": ["helveticaneue", "helvetica", "arial", "liberationsans"],
    "arial": ["arial", "liberationsans", "arimo", "helvetica", "nimbussans"],
    "timesnewroman": ["timesnewroman", "liberationserif", "tinos", "times", "nimbusroman"],
    "times": ["times", "timesnewroman", "liberationserif", "tinos", "nimbusroman"],
    "courier": ["courier", "couriernew", "liberationmono", "cousine", "nimbusmono"],
    "couriernew": ["couriernew", "liberationmono", "cousine", "courier"],
    "calibri": ["calibri", "carlito"],
    "cambria": ["cambria", "caladea"],
    "georgia": ["georgia", "gelasio"],
    "garamond": ["garamond", "ebgaramond", "adobegaramondpro"],
    "verdana": ["verdana", "dejavusans"],
    "tahoma": ["tahoma", "dejavusans", "verdana"],
    "segoeui": ["segoeui", "opensans", "notosans", "helveticaneue"],
    "centurygothic": ["centurygothic", "texgyreadventor", "urwgothic"],
    "bookantiqua": ["bookantiqua", "palatino", "palatinolinotype", "texgyrepagella"],
    "palatinolinotype": ["palatinolinotype", "palatino", "bookantiqua", "texgyrepagella"],
    "palatino": ["palatino", "palatinolinotype", "bookantiqua", "texgyrepagella"],
    "liberationsans": ["liberationsans", "arial", "arimo"],
    "liberationserif": ["liberationserif", "timesnewroman", "tinos"],
    "liberationmono": ["liberationmono", "couriernew", "cousine"],
    "arimo": ["arimo", "arial", "liberationsans"],
    "tinos": ["tinos", "timesnewroman", "liberationserif"],
    "cousine": ["cousine", "couriernew", "liberationmono"],
    "carlito": ["carlito", "calibri"],
    "caladea": ["caladea", "cambria"],
    "symbol": ["symbol"],
    "zapfdingbats": ["zapfdingbats"],
    "dejavusans": ["dejavusans", "verdana"],
    "trebuchetms": ["trebuchetms", "trebuchet"],
    "lucidagrande": ["lucidagrande", "lucidasansunicode"],
    "sfns": ["sfpro", "sfns", "helveticaneue"],
    "sfprotext": ["sfprotext", "sfpro", "sfns", "helveticaneue"],
    "sfprodisplay": ["sfprodisplay", "sfpro", "sfns", "helveticaneue"],
}

# Fallback families per generic class, in order of preference.
GENERIC: dict[str, list[str]] = {
    "sans": ["arial", "helvetica", "liberationsans", "arimo", "dejavusans", "notosans"],
    "serif": ["timesnewroman", "times", "liberationserif", "tinos", "dejavuserif", "notoserif"],
    "mono": ["couriernew", "courier", "liberationmono", "cousine", "dejavusansmono", "menlo"],
}

# Families with a very large coverage, used for characters missing everywhere else.
UNIVERSAL = ["arialunicodems", "notosans", "dejavusans", "lucidagrande"]

# Families shown first in the font menu.
POPULAR = [
    "Arial", "Helvetica", "Times New Roman", "Calibri", "Cambria", "Georgia", "Verdana",
    "Courier New", "Garamond", "Century Gothic", "Aptos", "Trebuchet MS", "Tahoma",
    "Palatino", "Book Antiqua", "Avenir Next", "Futura", "Gill Sans", "Optima",
    "Liberation Sans", "Liberation Serif", "DejaVu Sans", "Noto Sans", "Roboto", "Open Sans", "Lato",
]


@dataclass
class Face:
    id: str
    path: str
    index: int
    family: str
    style: str
    psname: str
    fullname: str
    weight: int
    italic: bool
    mono: bool
    cff: bool
    variable: bool
    keys: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return "otf" if self.cff else "ttf"


def _face_id(path: str, index: int) -> str:
    return hashlib.sha1(f"{path}#{index}".encode()).hexdigest()[:16]


def _best_name(name_table, name_id: int) -> str | None:
    best = None
    for rec in name_table.names:
        if rec.nameID != name_id:
            continue
        try:
            text = rec.toUnicode()
        except Exception:
            continue
        if not text:
            continue
        score = 0
        if rec.platformID == 3 and rec.langID == 0x409:
            score = 3
        elif rec.platformID == 1 and rec.langID == 0:
            score = 2
        elif rec.platformID == 3:
            score = 1
        if best is None or score > best[0]:
            best = (score, text)
    return best[1] if best else None


def _describe(font: TTFont, path: str, index: int) -> Face | None:
    if "name" not in font:
        return None
    if not any(t in font for t in ("glyf", "CFF ", "CFF2")):
        return None  # bitmap-only fonts (emoji) cannot be used to write text
    name = font["name"]
    family = _best_name(name, 16) or _best_name(name, 1)
    style = _best_name(name, 17) or _best_name(name, 2) or "Regular"
    psname = _best_name(name, 6) or ""
    fullname = _best_name(name, 4) or family or ""
    if not family or psname.startswith(".") or family.startswith("."):
        return None
    if family.lower() in ("lastresort", "last resort"):
        return None
    weight = None
    italic = False
    if "OS/2" in font:
        os2 = font["OS/2"]
        weight = int(os2.usWeightClass or 0)
        if 0 < weight < 10:
            weight *= 100
        italic = bool(os2.fsSelection & 1)
    style_weight, style_italic, _ = _style_info(style)
    if not weight:
        weight = style_weight or 400
    italic = italic or style_italic
    if "head" in font and font["head"].macStyle & 2:
        italic = True
    mono = bool("post" in font and font["post"].isFixedPitch)
    keys = []
    for n in (family, _best_name(name, 1), psname.split("-")[0] if psname else None):
        if n:
            k = family_key(n)
            if k and k not in keys:
                keys.append(k)
    return Face(
        id=_face_id(path, index),
        path=path,
        index=index,
        family=family,
        style=style,
        psname=psname,
        fullname=fullname,
        weight=max(100, min(900, int(round(weight / 100.0)) * 100)),
        italic=italic,
        mono=mono,
        cff="CFF " in font or "CFF2" in font,
        variable="fvar" in font,
        keys=keys,
    )


def _scan_file(path: str) -> list[Face]:
    faces: list[Face] = []
    try:
        if path.lower().endswith((".ttc", ".otc")):
            coll = TTCollection(path, lazy=True)
            for i, font in enumerate(coll.fonts):
                face = _describe(font, path, i)
                if face:
                    faces.append(face)
        else:
            font = TTFont(path, lazy=True)
            face = _describe(font, path, 0)
            if face:
                faces.append(face)
    except Exception as exc:  # broken or unsupported font files are skipped
        log.debug("font skipped %s: %s", path, exc)
    return faces


class FontIndex:
    """Lookup of installed font faces by family, PostScript name and id."""

    def __init__(self, dirs: list[Path] | None = None, cache_path: Path | None = None):
        self.dirs = dirs if dirs is not None else config.font_dirs()
        self.cache_path = cache_path or (config.CACHE_DIR / "font-index.json")
        self.faces: dict[str, Face] = {}
        self.by_key: dict[str, list[Face]] = {}
        self.by_ps: dict[str, Face] = {}
        self._lock = threading.Lock()
        self._loaded = False
        self._bytes_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------------ loading
    def ensure_loaded(self) -> "FontIndex":
        if self._loaded:
            return self
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True
        return self

    def _load(self) -> None:
        cached: dict = {}
        try:
            data = json.loads(self.cache_path.read_text())
            if data.get("version") == _CACHE_VERSION:
                cached = data.get("files", {})
        except Exception:
            pass
        files: dict[str, dict] = {}
        seen_real: set[str] = set()
        for d in self.dirs:
            for root, _dirs, names in os.walk(d):
                for fn in names:
                    if not fn.lower().endswith(_EXTS):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        real = os.path.realpath(path)
                        if real in seen_real:
                            continue
                        seen_real.add(real)
                        st = os.stat(path)
                    except OSError:
                        continue
                    entry = cached.get(path)
                    if entry and entry["mtime"] == st.st_mtime and entry["size"] == st.st_size:
                        files[path] = entry
                        continue
                    faces = _scan_file(path)
                    files[path] = {"mtime": st.st_mtime, "size": st.st_size, "faces": [asdict(f) for f in faces]}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"version": _CACHE_VERSION, "files": files}))
        except OSError:
            pass
        for entry in files.values():
            for fd in entry["faces"]:
                self._add(Face(**fd))
        log.info("font index: %d faces in %d families", len(self.faces), len(self.by_key))

    def _add(self, face: Face) -> None:
        if face.id in self.faces:
            return
        self.faces[face.id] = face
        for k in face.keys:
            self.by_key.setdefault(k, []).append(face)
        if face.psname:
            self.by_ps.setdefault(face.psname.lower(), face)

    # ------------------------------------------------------------------ lookup
    def face(self, face_id: str) -> Face | None:
        self.ensure_loaded()
        return self.faces.get(face_id)

    def faces_for_key(self, key: str) -> list[Face]:
        self.ensure_loaded()
        faces = self.by_key.get(key, [])
        # A family may be installed twice (e.g. system + Office copy): keep one face per style.
        unique: dict[tuple[int, bool], Face] = {}
        for f in faces:
            slot = (f.weight, f.italic)
            current = unique.get(slot)
            if current is None or _face_rank(f) < _face_rank(current):
                unique[slot] = f
        return list(unique.values())

    def face_for_psname(self, psname: str) -> Face | None:
        self.ensure_loaded()
        return self.by_ps.get(psname.lower())

    def resolve_family_key(self, key: str) -> str | None:
        """First installed family among `key` and its aliases."""
        self.ensure_loaded()
        for k in ALIASES.get(key, [key]):
            if self.by_key.get(k):
                return k
        return None

    def generic_key(self, generic: str) -> str | None:
        self.ensure_loaded()
        for k in GENERIC.get(generic, GENERIC["sans"]):
            if self.by_key.get(k):
                return k
        return None

    def universal_keys(self) -> list[str]:
        self.ensure_loaded()
        return [k for k in UNIVERSAL if self.by_key.get(k)]

    def families(self) -> list[dict]:
        """Installed families for the font menu (popular ones first)."""
        self.ensure_loaded()
        fams: dict[str, dict] = {}
        for face in self.faces.values():
            if face.variable and not face.family:
                continue
            k = family_key(face.family)
            item = fams.setdefault(k, {"key": k, "name": face.family, "styles": set()})
            item["styles"].add((face.weight, face.italic))
        popular_rank = {family_key(n): i for i, n in enumerate(POPULAR)}
        out = []
        for k, item in fams.items():
            out.append({
                "key": k,
                "name": item["name"],
                "bold": any(w >= 600 for w, _ in item["styles"]),
                "italic": any(it for _, it in item["styles"]),
                "popular": k in popular_rank,
            })
        out.sort(key=lambda f: (popular_rank.get(f["key"], 10_000), f["name"].lower()))
        return out

    # ------------------------------------------------------------------ files
    def face_bytes(self, face_id: str) -> bytes:
        """Standalone font file for a face (faces of collections are extracted)."""
        face = self.face(face_id)
        if face is None:
            raise KeyError(face_id)
        cached = self._bytes_cache.get(face_id)
        if cached is not None:
            return cached
        if not face.path.lower().endswith((".ttc", ".otc")):
            data = Path(face.path).read_bytes()
        else:
            out = config.CACHE_DIR / "faces" / f"{face_id}.{face.ext}"
            if out.exists():
                data = out.read_bytes()
            else:
                coll = TTCollection(face.path)
                buf = io.BytesIO()
                coll.fonts[face.index].save(buf)
                data = buf.getvalue()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
        if len(self._bytes_cache) > 64:
            self._bytes_cache.clear()
        self._bytes_cache[face_id] = data
        return data


def _face_rank(face: Face) -> tuple:
    # Prefer static fonts, then files from the first configured directories.
    return (face.variable, face.path.lower().endswith((".ttc", ".otc")), len(face.path))


_INDEX: FontIndex | None = None


def get_index() -> FontIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = FontIndex()
    return _INDEX.ensure_loaded()
