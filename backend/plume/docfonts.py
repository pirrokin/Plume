"""Fonts of one document: families, glyph fallback chains and web fonts.

A *family* gathers every face we can use for a font family: subsets embedded
in the PDF (only for the characters they really contain) and the matching
installed font. Face selection mimics CSS font matching so that the browser
editor and the PDF writer pick the same glyphs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pymupdf

from .fontindex import Face, FontIndex, get_index
from .fontnames import css_weight_order, family_key, generic_class, parse_font_name, strip_subset
from .pdffonts import PreparedFont, is_text_reliable, prepare_font

_B14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo",
    ("sans", False, True): "heit", ("sans", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("mono", False, False): "cour", ("mono", True, False): "cobo",
    ("mono", False, True): "coit", ("mono", True, True): "cobi",
}

SYNTH_ITALIC_SKEW = 0.25  # same slant as Chrome's synthetic italic


@dataclass
class FamRef:
    key: str
    weight: int
    italic: bool


@dataclass
class Slot:
    emb: list[int] = field(default_factory=list)
    sys: Face | None = None


@dataclass
class ChainItem:
    font: pymupdf.Font
    codepoints: set[int] | None  # None: everything the font has a glyph for
    synth_bold: bool
    synth_italic: bool
    source: str

    def covers(self, u: int) -> bool:
        if self.codepoints is not None:
            return u in self.codepoints
        return bool(self.font.has_glyph(u))


class Family:
    def __init__(self, owner: "DocFonts", key: str, name: str, generic: str):
        self.owner = owner
        self.key = key
        self.name = name
        self.generic = generic
        self.sys_key = owner.index.resolve_family_key(key)
        self.exact_sys = self.sys_key == key
        self._slots: dict[tuple[int, bool], Slot] | None = None

    def slots(self) -> dict[tuple[int, bool], Slot]:
        if self._slots is None:
            slots: dict[tuple[int, bool], Slot] = {}
            for xref, weight, italic in self.owner.embedded_for_key(self.key):
                pf = self.owner.prepared(xref)
                if pf.usable:
                    slots.setdefault((weight, italic), Slot()).emb.append(xref)
            if self.sys_key:
                for face in self.owner.index.faces_for_key(self.sys_key):
                    slots.setdefault((face.weight, face.italic), Slot()).sys = face
            self._slots = slots
        return self._slots

    def has_real_faces(self) -> bool:
        return bool(self.slots())

    def match(self, weight: int, italic: bool) -> tuple[Slot, bool, bool] | None:
        """CSS-like face selection: style first, then weight; returns synthesis flags."""
        slots = self.slots()
        if not slots:
            return None
        same_style = [k for k in slots if k[1] == italic]
        candidates = same_style or list(slots)
        chosen_italic = candidates[0][1]
        weights = css_weight_order(weight, [k[0] for k in candidates if k[1] == chosen_italic])
        w = weights[0]
        slot = slots[(w, chosen_italic)]
        synth_bold = weight >= 600 and w < 600
        synth_italic = italic and not chosen_italic
        return slot, synth_bold, synth_italic

    def describe(self) -> dict:
        slots = self.slots()
        return {
            "key": self.key,
            "name": self.name,
            "generic": self.generic,
            "available": bool(slots),
            "substituted": not slots or (not any(s.emb for s in slots.values()) and not self.exact_sys),
            "bold": any(w >= 600 for w, _ in slots),
            "italic": any(it for _, it in slots),
        }


class DocFonts:
    def __init__(self, doc: pymupdf.Document, index: FontIndex | None = None):
        self.doc = doc
        self.index = index or get_index()
        self.families: dict[str, Family] = {}
        self._lock = threading.RLock()
        self._emb_by_key: dict[str, list[tuple[int, int, bool]]] | None = None
        self._reliable: dict[str, bool] = {}
        self._type3_names: set[str] = set()
        self._prepared: dict[int, PreparedFont] = {}
        self._span_cache: dict[tuple[str, int], FamRef] = {}
        self._fitz_sys: dict[str, pymupdf.Font] = {}
        self._chains: dict[tuple[str, int, bool], list[ChainItem]] = {}

    # ---------------------------------------------------------------- scanning
    def invalidate(self) -> None:
        """Call after the document changed (new fonts may have been embedded)."""
        with self._lock:
            self._emb_by_key = None
            self._reliable = {}
            self._type3_names = set()
            self._span_cache.clear()
            self._chains.clear()
            for fam in self.families.values():
                fam._slots = None

    def _scan(self) -> dict[str, list[tuple[int, int, bool]]]:
        if self._emb_by_key is None:
            by_key: dict[str, list[tuple[int, int, bool]]] = {}
            seen: set[int] = set()
            for page in self.doc:
                for xref, ext, ftype, basefont, *_ in page.get_fonts(full=True):
                    if xref in seen or not xref:
                        continue
                    seen.add(xref)
                    name = strip_subset(basefont) if basefont else f"Type3 ({xref} 0 R)"
                    ok = is_text_reliable(self.doc, xref)
                    self._reliable[name] = self._reliable.get(name, True) and ok
                    if ftype == "Type3":
                        self._type3_names.add(name)
                        continue
                    if ext == "n/a":
                        continue
                    info = parse_font_name(basefont)
                    by_key.setdefault(info["key"], []).append((xref, info["weight"], info["italic"]))
            self._emb_by_key = by_key
        return self._emb_by_key

    def embedded_for_key(self, key: str) -> list[tuple[int, int, bool]]:
        with self._lock:
            return list(self._scan().get(key, []))

    def name_reliable(self, fontname: str) -> bool:
        """Whether text in the font MuPDF reports as `fontname` decodes to the right characters."""
        with self._lock:
            self._scan()
            return self._reliable.get(strip_subset(fontname), True)

    def is_type3(self, fontname: str) -> bool:
        with self._lock:
            self._scan()
            return strip_subset(fontname) in self._type3_names or fontname.startswith("Type3 (")

    def prepared(self, xref: int) -> PreparedFont:
        with self._lock:
            pf = self._prepared.get(xref)
            if pf is None:
                pf = prepare_font(self.doc, xref)
                self._prepared[xref] = pf
            return pf

    # ---------------------------------------------------------------- families
    def family(self, key: str, name: str | None = None, generic: str | None = None) -> Family:
        with self._lock:
            fam = self.families.get(key)
            if fam is None:
                sys_key = self.index.resolve_family_key(key)
                display = name
                if sys_key:
                    faces = self.index.faces_for_key(sys_key)
                    if faces and (sys_key == key or not display):
                        display = faces[0].family
                if not display:
                    display = key
                fam = Family(self, key, display, generic or generic_class(display))
                self.families[key] = fam
            return fam

    def family_for_span(self, fontname: str, flags: int) -> FamRef:
        cache_key = (fontname, flags)
        ref = self._span_cache.get(cache_key)
        if ref is None:
            info = parse_font_name(fontname, flags)
            key = info["key"]
            if not strip_subset(fontname) or key in ("unknown",) or key.startswith("unnamed") or self.is_type3(fontname):
                key = f"inconnue{info['generic']}"
                name = {"sans": "Police inconnue (sans empattement)", "serif": "Police inconnue (à empattement)",
                        "mono": "Police inconnue (chasse fixe)"}[info["generic"]]
            else:
                name = info["name"]
            fam = self.family(key, name, info["generic"])
            ref = FamRef(fam.key, info["weight"], info["italic"])
            self._span_cache[cache_key] = ref
        return ref

    def generic_family(self, generic: str) -> Family | None:
        key = self.index.generic_key(generic)
        return self.family(key) if key else None

    # ---------------------------------------------------------------- chains
    def sys_font(self, face: Face) -> pymupdf.Font:
        font = self._fitz_sys.get(face.id)
        if font is None:
            font = pymupdf.Font(fontbuffer=self.index.face_bytes(face.id))
            self._fitz_sys[face.id] = font
        return font

    def _items_for(self, fam: Family, weight: int, italic: bool) -> list[ChainItem]:
        m = fam.match(weight, italic)
        if not m:
            return []
        slot, sb, si = m
        items = []
        for xref in slot.emb:
            pf = self.prepared(xref)
            if pf.usable and pf.font is not None:
                items.append(ChainItem(pf.font, pf.codepoints, sb, si, f"emb:{xref}"))
        if slot.sys is not None:
            items.append(ChainItem(self.sys_font(slot.sys), None, sb, si, f"sys:{slot.sys.id}"))
        return items

    def chain(self, key: str, weight: int, italic: bool) -> list[ChainItem]:
        ck = (key, int(weight), bool(italic))
        with self._lock:
            cached = self._chains.get(ck)
            if cached is not None:
                return cached
            fam = self.family(key)
            items = self._items_for(fam, weight, italic)
            gfam = self.generic_family(fam.generic)
            if gfam is not None and gfam.key != fam.key:
                items += self._items_for(gfam, weight, italic)
            b14 = _B14[(fam.generic, weight >= 600, bool(italic))]
            items.append(ChainItem(pymupdf.Font(b14), None, False, False, f"b14:{b14}"))
            for ukey in self.index.universal_keys():
                items += self._items_for(self.family(ukey), 400, False)
            self._chains[ck] = items
            return items

    # ---------------------------------------------------------------- web fonts
    def css(self, doc_url: str, keys: list[str]) -> str:
        """@font-face rules for the given families plus fallbacks."""
        rules: list[str] = []
        done: set[str] = set()

        def add_family(css_name: str, fam: Family) -> None:
            for (w, it), slot in sorted(fam.slots().items()):
                if slot.sys is not None:
                    rules.append(_face_rule(css_name, w, it, f"/api/fonts/sys/{slot.sys.id}", slot.sys.ext, None))
                for xref in slot.emb:
                    pf = self.prepared(xref)
                    if pf.usable and pf.web_ok and pf.codepoints:
                        fmt = "opentype" if pf.web_mime == "font/otf" else "truetype"
                        rules.append(_face_rule(css_name, w, it, f"{doc_url}/fonts/emb/{xref}", fmt, pf.codepoints))

        with self._lock:
            for key in keys:
                if key in done:
                    continue
                done.add(key)
                add_family(f"PF {key}", self.family(key))
            for generic in ("sans", "serif", "mono"):
                gfam = self.generic_family(generic)
                if gfam is not None:
                    add_family(f"PF generic-{generic}", gfam)
            for ukey in self.index.universal_keys():
                add_family("PF universal", self.family(ukey))
        return "\n".join(rules)


def _unicode_range(cps: set[int]) -> str:
    ranges = []
    for u in sorted(cps):
        if ranges and u == ranges[-1][1] + 1:
            ranges[-1][1] = u
        else:
            ranges.append([u, u])
    return ", ".join(f"U+{a:X}" if a == b else f"U+{a:X}-{b:X}" for a, b in ranges)


def _face_rule(family: str, weight: int, italic: bool, url: str, fmt: str, cps: set[int] | None) -> str:
    fmt_name = {"ttf": "truetype", "otf": "opentype"}.get(fmt, fmt)
    parts = [
        f"font-family: \"{family}\"",
        f"src: url(\"{url}\") format(\"{fmt_name}\")",
        f"font-weight: {weight}",
        f"font-style: {'italic' if italic else 'normal'}",
        "font-display: block",
    ]
    if cps:
        parts.append(f"unicode-range: {_unicode_range(cps)}")
    return "@font-face { " + "; ".join(parts) + "; }"
