"""Visual font matching: the available typeface that looks the most like another.

Fonts embedded in PDFs are often commercial (Canva, Adobe or Microsoft fonts)
and only hold the letters the document uses. The letters they lack are
written with an installed or downloadable font chosen to look alike. Fonts
are compared as pictures: every letter is rendered and reduced to a small grid
of ink coverage over a fixed area around its origin, which captures its
width, weight, proportions and rough shape. Signatures of popular Google Fonts
families are computed in advance (tools/build_lookalikes.py), so that a family
that is not installed yet can be chosen, then downloaded.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import operator
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

SIG_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
GRID = 7  # cells per side
# Area sampled around a glyph origin, in em, y upwards: wide enough for a "W".
BOX_X0, BOX_X1, BOX_Y0, BOX_Y1 = -0.1, 1.3, -0.35, 1.05
PX = 50  # rendering resolution, pixels per em (a cell is 10 px)
LEVELS = 15  # ink coverage of a cell, stored as 0..LEVELS
ADV_UNIT = 200  # advances are stored in 1/200 em
_EM = 20.0  # font size used to render, in points
_COLS = 16
# Lowercase letters whose stems stand on the baseline: serifs widen their feet
# (capitals are left out: the "I" of many sans-serif fonts has bars).
_FOOT_CHARS = "ilnmhr"
# Letters made of a single vertical stem at mid-height: its width tells the weight.
_STEM_CHARS = "lIi"
_CELL = int(round((BOX_X1 - BOX_X0) * PX))
_STEP = _CELL // GRID
_RECORD = 1 + GRID * GRID  # bytes per letter in a packed signature: advance, then the grid
_CHAR_INDEX = {c: i for i, c in enumerate(SIG_CHARS)}

DATA_PATH = Path(__file__).with_name("data") / "lookalikes.bin"


@dataclass
class Signature:
    # char -> (advance in 1/ADV_UNIT em, GRID*GRID coverage values 0..LEVELS)
    glyphs: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    # Ink at the foot of the stems relative to mid-height: ~1 sans, 1.5-4 serif.
    serif: float | None = None
    stem: float | None = None  # width of a vertical stem, in em (weight)
    slant: float | None = None  # horizontal shift per unit of height (italics ~0.2)

    @property
    def mono(self) -> bool:
        return _is_mono([a for c, (a, _) in self.glyphs.items() if c.isalpha()])

    @property
    def count(self) -> int:
        return len(self.glyphs)

    def get(self, c: str) -> tuple[int, bytes] | None:
        return self.glyphs.get(c)

    def caps_only(self) -> bool:
        """Lowercase letters drawn as capitals (titling fonts such as Intro)."""
        pairs = [(c, c.upper()) for c in self.glyphs if c.islower() and c.upper() in self.glyphs]
        if len(pairs) < 3:
            return False
        same = sum(1 for lo, up in pairs if _glyph_distance(self.glyphs[lo], self.glyphs[up]) < 0.15)
        return same >= 0.8 * len(pairs)

    def as_capitals(self) -> "Signature":
        """Lowercase glyphs filed under their capital (to compare a titling font)."""
        glyphs = {c: g for c, g in self.glyphs.items() if not c.islower()}
        for c, g in self.glyphs.items():
            if c.islower():
                glyphs.setdefault(c.upper(), g)
        # The feet measured on "lowercase" capitals say nothing about serifs.
        return Signature(glyphs, None, self.stem, self.slant)

    def pack(self) -> bytes:
        out = bytearray()
        empty = bytes(GRID * GRID)
        for c in SIG_CHARS:
            adv, grid = self.glyphs.get(c, (0, empty))
            out.append(max(0, min(255, adv)) if c in self.glyphs else 0)
            out += grid
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes, features: list) -> "Signature":
        glyphs = {}
        for i, c in enumerate(SIG_CHARS):
            chunk = data[i * _RECORD:(i + 1) * _RECORD]
            if len(chunk) == _RECORD and chunk[0]:
                glyphs[c] = (chunk[0], bytes(chunk[1:]))
        serif, stem, slant = (list(features) + [None, None, None])[:3]
        return cls(glyphs, serif, stem, slant)

    def features(self) -> list:
        return [self.serif, self.stem, self.slant]


class PackedSignature:
    """Read-only signature kept as a single byte string (catalog entries, by the thousand)."""

    __slots__ = ("data", "mask", "serif", "stem", "slant", "mono", "count")

    def __init__(self, data: bytes, features: list):
        self.data = data
        self.mask = 0
        advs = []
        for i, c in enumerate(SIG_CHARS):
            adv = data[i * _RECORD] if (i + 1) * _RECORD <= len(data) else 0
            if adv:
                self.mask |= 1 << i
                if c.isalpha():
                    advs.append(adv)
        self.count = bin(self.mask).count("1")
        self.mono = _is_mono(advs)
        self.serif, self.stem, self.slant = (list(features) + [None, None, None])[:3]

    def get(self, c: str) -> tuple[int, bytes] | None:
        i = _CHAR_INDEX.get(c)
        if i is None or not (self.mask >> i) & 1:
            return None
        off = i * _RECORD
        return self.data[off], self.data[off + 1:off + _RECORD]


def _is_mono(advances: list[int]) -> bool:
    return len(advances) >= 5 and max(advances) - min(advances) <= max(1, 0.02 * max(advances))


# ---------------------------------------------------------------- rendering
def _origin(i: int) -> tuple[float, float]:
    """Origin of the i-th glyph on the rendering page, in points (y downwards)."""
    r, k = divmod(i, _COLS)
    cell = (BOX_X1 - BOX_X0) * _EM
    return k * cell - BOX_X0 * _EM, r * cell + BOX_Y1 * _EM


def _page_size(n: int) -> tuple[float, float]:
    cell = (BOX_X1 - BOX_X0) * _EM
    rows = (n + _COLS - 1) // _COLS
    return _COLS * cell, max(1, rows) * cell


def _ink_rows(pix: pymupdf.Pixmap, x0: int, x1: int, y0: int, y1: int) -> int:
    s, stride = pix.samples, pix.stride
    total = 0
    for y in range(y0, y1):
        row = s[y * stride + x0:y * stride + x1]
        total += 255 * len(row) - sum(row)
    return total


def _band_centre(pix: pymupdf.Pixmap, x0: int, x1: int, y0: int, y1: int) -> tuple[float, int]:
    """Horizontal centre of the ink in a band of rows, and the amount of ink."""
    s, stride = pix.samples, pix.stride
    total = moment = 0
    for y in range(y0, y1):
        off = y * stride
        for x in range(x0, x1):
            v = 255 - s[off + x]
            if v:
                total += v
                moment += v * x
    return (moment / total if total else 0.0), total


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[len(values) // 2]


def _analyse(pix: pymupdf.Pixmap, chars: list[str], advances: list[float]) -> Signature:
    sig = Signature()
    ratios: list[float] = []
    stems: list[float] = []
    slants: list[float] = []
    full = _STEP * _STEP * 255
    base = int(round(BOX_Y1 * PX))
    lo, hi = int(0.05 * PX), int(0.25 * PX)  # bands above the baseline, for the slant
    lo2, hi2 = int(0.40 * PX), int(0.60 * PX)
    for i, (c, adv) in enumerate(zip(chars, advances)):
        r, k = divmod(i, _COLS)
        x0, y0 = k * _CELL, r * _CELL
        grid = bytearray()
        for gy in range(GRID):
            for gx in range(GRID):
                ink = _ink_rows(pix, x0 + gx * _STEP, x0 + (gx + 1) * _STEP, y0 + gy * _STEP, y0 + (gy + 1) * _STEP)
                grid.append(min(LEVELS, int(round(ink / full * LEVELS))))
        if not any(grid) and not c.isspace():
            continue  # empty glyph (stripped by a subsetter)
        sig.glyphs[c] = (max(1, min(255, int(round(adv * ADV_UNIT)))), bytes(grid))
        yb = y0 + base
        if c in _FOOT_CHARS:
            foot = _ink_rows(pix, x0, x0 + _CELL, yb - 2, yb)
            mid = _ink_rows(pix, x0, x0 + _CELL, yb - 12, yb - 10)
            if mid > 0:
                ratios.append(foot / mid)
        if c in _STEM_CHARS:
            row = _ink_rows(pix, x0, x0 + _CELL, yb - int(0.22 * PX), yb - int(0.22 * PX) + 2)
            stems.append(row / 255 / 2 / PX)
        if c.isalnum() and c not in "acemnorsuvwxz":  # letters reaching above the x-height
            c_lo, ink_lo = _band_centre(pix, x0, x0 + _CELL, yb - hi, yb - lo)
            c_hi, ink_hi = _band_centre(pix, x0, x0 + _CELL, yb - hi2, yb - lo2)
            if ink_lo > 255 * 20 and ink_hi > 255 * 20:
                slants.append((c_hi - c_lo) / ((hi2 + lo2 - hi - lo) / 2))
    serif = _median(ratios)
    sig.serif = round(max(0.2, serif), 2) if serif is not None else None
    stem = _median(stems)
    sig.stem = round(stem, 4) if stem else None
    slant = _median(slants)
    sig.slant = round(slant, 3) if slant is not None else None
    return sig


def _pixmap(page: pymupdf.Page, px: int = PX) -> pymupdf.Pixmap:
    scale = px / _EM
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)


def _font_page(font: pymupdf.Font, chars: list[str]) -> pymupdf.Page:
    doc = pymupdf.open()
    w, h = _page_size(len(chars))
    page = doc.new_page(width=w, height=h)
    tw = pymupdf.TextWriter(page.rect)
    for i, c in enumerate(chars):
        tw.append(_origin(i), c, font=font, fontsize=_EM)
    tw.write_text(page)
    return page


def font_signature(font: pymupdf.Font, chars: str = SIG_CHARS) -> Signature:
    """Signature of the given letters of a font (those it has a glyph for)."""
    present = [c for c in chars if font.has_glyph(ord(c))]
    if not present:
        return Signature()
    page = _font_page(font, present)
    return _analyse(_pixmap(page), present, [font.glyph_advance(ord(c)) for c in present])


def _ink_box(pix: pymupdf.Pixmap, i: int, px: int) -> tuple[tuple[int, int, int, int], int] | None:
    """Bounding box (pixels, cell-relative) of the clearly inked area of cell i, and its ink."""
    cell = int(round((BOX_X1 - BOX_X0) * px))
    r, k = divmod(i, _COLS)
    x0, y0 = k * cell, r * cell
    s, stride = pix.samples, pix.stride
    xs, ys = [], []
    ink = 0
    for y in range(y0, min(y0 + cell, pix.height)):
        row = s[y * stride + x0:y * stride + min(x0 + cell, pix.width)]
        ink += 255 * len(row) - sum(row)
        dark = [x for x, v in enumerate(row) if v < 128]
        if dark:
            ys.append(y - y0)
            xs += (dark[0], dark[-1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1), ink


def compare_type3(doc: pymupdf.Document, xref: int, font: pymupdf.Font) -> tuple[float, float] | None:
    """How the letters of a Type 3 font differ from those of a real font.

    Returns (growth of the letters in em, ink ratio). Canva exports outlined
    ("hollow") text as Type 3 glyphs tracing the outline of the letters: they
    are larger by the stroke width and usually hold less ink.
    """
    glyphs = type3_glyphs(doc, xref)
    common = [c for c in SIG_CHARS if c in glyphs and font.has_glyph(ord(c))][:8]
    if not common:
        return None
    page = _type3_page(doc, xref, [(glyphs[c][0], _origin(i)) for i, c in enumerate(common)])
    if page is None:
        return None
    px = 200
    t3, real = _pixmap(page, px), _pixmap(_font_page(font, common), px)
    growth = []
    ink_t3 = ink_real = 0
    for i in range(len(common)):
        a, b = _ink_box(t3, i, px), _ink_box(real, i, px)
        if a is None or b is None:
            continue
        (ba, ia), (bb, ib) = a, b
        ink_t3 += ia
        ink_real += ib
        growth.append(((ba[2] - ba[0]) - (bb[2] - bb[0]) + (ba[3] - ba[1]) - (bb[3] - bb[1])) / 2 / px)
    if not growth or not ink_real:
        return None
    return _median(growth), ink_t3 / ink_real


def type3_signature(doc: pymupdf.Document, xref: int, chars: str = SIG_CHARS) -> Signature:
    """Signature of a Type 3 font, whose glyphs are drawings in the PDF itself."""
    glyphs = type3_glyphs(doc, xref)
    present = [c for c in chars if c in glyphs]
    if not present:
        return Signature()
    page = _type3_page(doc, xref, [(glyphs[c][0], _origin(i)) for i, c in enumerate(present)])
    if page is None:
        return Signature()
    return _analyse(_pixmap(page), present, [glyphs[c][1] for c in present])


def type3_glyphs(doc: pymupdf.Document, xref: int) -> dict[str, tuple[int, float]]:
    """Character -> (code, advance in em) of a Type 3 font, from its ToUnicode map."""
    from .pdffonts import _numbers, _resolve, parse_tounicode

    typ, val = _resolve(doc, xref, "ToUnicode")
    if typ != "stream":
        return {}
    try:
        to_unicode = parse_tounicode(doc.xref_stream(int(val)) or b"")
    except Exception:
        return {}
    matrix = _numbers(doc.xref_get_key(xref, "FontMatrix")[1]) or [0.001]
    typ, val = _resolve(doc, xref, "Widths")
    widths = _numbers(val) if typ == "array" else []
    first = doc.xref_get_key(xref, "FirstChar")[1]
    fc = int(first) if first.lstrip("-").isdigit() else 0
    out = {}
    for code, text in to_unicode.items():
        if len(text) == 1 and 0 <= code - fc < len(widths) and code < 256:
            out.setdefault(text, (code, abs(widths[code - fc] * matrix[0])))
    return out


def _type3_page(doc: pymupdf.Document, xref: int, items: list[tuple[int, tuple[float, float]]]):
    """A scratch page showing the given character codes of a Type 3 font.

    The font only exists inside `doc`, so a copy of the document is used to
    leave the original untouched.
    """
    try:
        tmp = pymupdf.open("pdf", doc.tobytes(garbage=0))
        w, h = _page_size(len(items))
        page = tmp.new_page(width=w, height=h)
        tmp.xref_set_key(page.xref, "Resources", f"<</Font<</PT3 {xref} 0 R>>>>")
        ops = ["BT"]
        for code, (x, y) in items:
            ops.append(f"/PT3 {_EM:g} Tf 1 0 0 1 {x:.3f} {h - y:.3f} Tm <{code:02X}> Tj")
        ops.append("ET")
        cx = tmp.get_new_xref()
        tmp.update_object(cx, "<<>>")
        tmp.update_stream(cx, "\n".join(ops).encode())
        tmp.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
        return tmp[page.number]
    except Exception as exc:
        log.debug("type3 rendering failed: %s", exc)
        return None


# ---------------------------------------------------------------- comparison
_SUB = operator.sub


def _glyph_distance(a: tuple[int, bytes], b: tuple[int, bytes]) -> float:
    img = sum(map(abs, map(_SUB, a[1], b[1]))) / (GRID * GRID * LEVELS)
    return img / 0.1 + abs(a[0] - b[0]) / ADV_UNIT / 0.05


def distance(q: Signature, c: Signature | PackedSignature, bound: float | None = None) -> float | None:
    """Dissimilarity of two signatures over their common letters (0 = identical).

    Returns None when they share too few letters, or as soon as the result is
    known to exceed `bound`.
    """
    pairs = []
    for g, mine in q.glyphs.items():
        other = c.get(g)
        if other is not None:
            pairs.append((mine, other))
    n = len(pairs)
    if n < max(3, 0.6 * len(q.glyphs)):
        return None
    style = style_distance(q, c)
    limit = None if bound is None else (bound - style) * n
    img = adv = 0
    k_img = GRID * GRID * LEVELS * 0.1
    k_adv = ADV_UNIT * 0.05
    for (aq, gq), (ac, gc) in pairs:
        img += sum(map(abs, map(_SUB, gq, gc)))
        adv += abs(aq - ac)
        if limit is not None and img / k_img + adv / k_adv > limit:
            return None
    return (img / k_img + adv / k_adv) / n + style


def style_distance(q: Signature, c: Signature | PackedSignature) -> float:
    """Differences of serifs, weight and slant, which a coarse grid sees poorly."""
    d = 0.0
    if q.serif and c.serif:
        d += 1.5 * max(0.0, abs(math.log(q.serif / c.serif)) - 0.1)
    if q.stem and c.stem:
        d += 1.5 * abs(math.log(q.stem / c.stem))
    if q.slant is not None and c.slant is not None:
        d += 4.0 * abs(q.slant - c.slant)
    return d


# ---------------------------------------------------------------- candidates
@dataclass
class Candidate:
    key: str  # family key
    name: str  # family name
    weight: int
    italic: bool
    sig: Signature | PackedSignature
    category: str = ""  # Google Fonts category ("Sans Serif", "Display"...)
    google: bool = False  # can be downloaded from Google Fonts


_catalog: list[Candidate] | None = None
_catalog_lock = threading.Lock()


def catalog() -> list[Candidate]:
    """Precomputed signatures of popular Google Fonts families."""
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = _load_catalog(DATA_PATH)
    return _catalog


def _load_catalog(path: Path) -> list[Candidate]:
    try:
        data = json.loads(zlib.decompress(path.read_bytes()))
    except (OSError, ValueError, zlib.error) as exc:
        log.warning("look-alike catalog unavailable: %s", exc)
        return []
    if data.get("grid") != GRID or data.get("chars") != SIG_CHARS:
        log.warning("look-alike catalog built with other settings: ignored")
        return []
    out = []
    for f in data["faces"]:
        sig = PackedSignature(base64.b64decode(f["g"]), f.get("s") or [])
        out.append(Candidate(f["k"], f["n"], f["w"], bool(f["i"]), sig, f.get("c", ""), True))
    return out


def save_catalog(path: Path, faces: list[Candidate]) -> None:
    data = {
        "grid": GRID,
        "chars": SIG_CHARS,
        "faces": [
            {"k": c.key, "n": c.name, "w": c.weight, "i": int(c.italic), "c": c.category,
             "s": [c.sig.serif, c.sig.stem, c.sig.slant], "g": base64.b64encode(_packed(c.sig)).decode()}
            for c in faces
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9))


def _packed(sig: Signature | PackedSignature) -> bytes:
    return sig.data if isinstance(sig, PackedSignature) else sig.pack()


@dataclass
class Match:
    key: str
    name: str
    score: float
    google: bool
    caps: bool  # compared as capitals: the original draws lowercase letters as capitals
    # Best face of the family for each queried (weight, italic) slot.
    faces: dict[tuple[int, bool], tuple[int, bool]] = field(default_factory=dict)


def find_lookalike(queries: dict[tuple[int, bool], Signature], candidates: list[Candidate],
                   exclude: set[str] | None = None) -> Match | None:
    """Family whose faces look the most like the queried faces of one family."""
    queries = {slot: q for slot, q in queries.items() if q.count >= 3}
    if not queries:
        return None
    caps = all(q.caps_only() for q in queries.values())
    if caps:
        queries = {slot: q.as_capitals() for slot, q in queries.items()}
    mono = any(q.mono for q in queries.values())
    families: dict[str, list[Candidate]] = {}
    for c in candidates:
        if exclude and c.key in exclude:
            continue
        if c.sig.mono != mono:
            continue
        families.setdefault(c.key, []).append(c)
    # Faces are weighed by the number of letters they show: a subset of three
    # letters says little about a typeface.
    weights = {slot: q.count for slot, q in queries.items()}
    total_weight = sum(weights.values())
    best: Match | None = None
    for key, faces in families.items():
        acc = 0.0
        chosen: dict[tuple[int, bool], tuple[int, bool]] = {}
        for slot, q in queries.items():
            bound = None
            if best is not None:
                bound = (best.score * total_weight - acc) / weights[slot]
                if bound <= 0:
                    acc = math.inf
                    break
            slot_best = None
            for face in faces:
                d = distance(q, face.sig, bound=bound)
                if d is None:
                    continue
                d += 0.1 * (face.italic != slot[1])
                if slot_best is None or d < slot_best[0]:
                    slot_best = (d, face)
            if slot_best is None:
                acc = math.inf
                break
            acc += slot_best[0] * weights[slot]
            chosen[slot] = (slot_best[1].weight, slot_best[1].italic)
        if not math.isfinite(acc):
            continue
        score = acc / total_weight
        if best is None or score < best.score:
            best = Match(key, faces[0].name, score, faces[0].google, caps, chosen)
    return best


# ---------------------------------------------------------------- installed fonts
_local: dict[str, tuple[float, int, Signature]] | None = None
_local_lock = threading.Lock()
_local_dirty = False


def _local_cache_path() -> Path:
    from . import config

    return config.CACHE_DIR / "lookalike-local.json"


def _local_signature(index, face) -> Signature | None:
    """Signature of an installed face, kept on disk between runs."""
    global _local, _local_dirty
    import os

    try:
        st = os.stat(face.path)
    except OSError:
        return None
    with _local_lock:
        if _local is None:
            _local = {}
            try:
                for fid, (mtime, size, packed, features) in json.loads(_local_cache_path().read_text()).items():
                    _local[fid] = (mtime, size, Signature.unpack(base64.b64decode(packed), features))
            except (OSError, ValueError, TypeError):
                pass
        hit = _local.get(face.id)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    try:
        sig = font_signature(pymupdf.Font(fontbuffer=index.face_bytes(face.id)))
    except Exception:
        return None
    with _local_lock:
        _local[face.id] = (st.st_mtime, st.st_size, sig)
        _local_dirty = True
    return sig


def _save_local() -> None:
    global _local_dirty
    with _local_lock:
        if not _local_dirty or _local is None:
            return
        data = {fid: [m, z, base64.b64encode(sig.pack()).decode(), sig.features()] for fid, (m, z, sig) in _local.items()}
        _local_dirty = False
    try:
        path = _local_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass


def candidates(index) -> list[Candidate]:
    """Look-alike candidates: the Google Fonts catalog and suitable installed families."""
    from .fontindex import LOCAL_LOOKALIKES
    from .fontnames import family_key

    out = list(catalog())
    known = {c.key for c in out}
    for name in LOCAL_LOOKALIKES:
        key = family_key(name)
        if key in known:
            continue
        known.add(key)
        for face in index.faces_for_key(key):
            if face.variable:
                continue
            sig = _local_signature(index, face)
            if sig is not None and sig.count >= 40:
                out.append(Candidate(key, face.family, face.weight, face.italic, sig))
    _save_local()
    return out


def best_faces(index, key: str, queries: dict[tuple[int, bool], Signature],
               caps: bool = False) -> dict[tuple[int, bool], tuple[int, bool]]:
    """For each queried face, the closest installed face of family `key`.

    The catalog holds a few weights per family; once the family is installed,
    all its weights can be compared.
    """
    faces = [f for f in index.faces_for_key(key) if not f.variable]
    sigs = [(f, _local_signature(index, f)) for f in faces]
    _save_local()
    out = {}
    for slot, q in queries.items():
        if caps:
            q = q.as_capitals()
        best = None
        for face, sig in sigs:
            if sig is None:
                continue
            d = distance(q, sig)
            if d is None:
                continue
            d += 0.1 * (face.italic != slot[1])
            if best is None or d < best[0]:
                best = (d, (face.weight, face.italic))
        if best is not None:
            out[slot] = best[1]
    return out
