"""Fonts of one document: families, glyph fallback chains and web fonts.

A *family* gathers every face we can use for a font family: subsets embedded
in the PDF (only for the characters they really contain) and the matching
installed font. Face selection mimics CSS font matching so that the browser
editor and the PDF writer pick the same glyphs.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import pymupdf

from . import lookalike
from .fontindex import ALIASES, Face, FontIndex, get_index
from .googlefonts import google_fonts
from .fontnames import css_weight_order, family_key, generic_class, parse_font_name, strip_subset
from .pdffonts import PreparedFont, add_case_aliases, is_text_reliable, prepare_font

_B14 = {
    ("sans", False, False): "helv", ("sans", True, False): "hebo",
    ("sans", False, True): "heit", ("sans", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("mono", False, False): "cour", ("mono", True, False): "cobo",
    ("mono", False, True): "coit", ("mono", True, True): "cobi",
}

SYNTH_ITALIC_SKEW = 0.25  # same slant as Chrome's synthetic italic
# How long writing waits for a missing family being downloaded, in seconds.
DOWNLOAD_WAIT = 6.0

log = logging.getLogger(__name__)


@dataclass
class FamRef:
    key: str
    weight: int
    italic: bool
    # Drawing of the letters when the font itself tells it (outlined Type 3 copies).
    render: str | None = None
    outline: float = 0.0  # stroke width, in em


@dataclass
class Type3Info:
    """What a Type 3 font (letters drawn as pictures) reproduces."""

    family: str | None = None  # key of the family whose letters it draws
    name: str | None = None
    weight: int = 400
    italic: bool = False
    render: str | None = None  # "stroke" for outlined ("hollow") letters
    outline: float = 0.0  # stroke width, in em


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
    # Family this glyph source stands in for, when it is a substitute (look-alike,
    # generic or universal fallback) rather than a face of the family itself.
    stands_for: str | None = None
    # Look-alike of a titling font (lowercase drawn as capitals): writes capitals.
    caps: bool = False

    def char_for(self, ch: str) -> str:
        """Character actually written for `ch` with this glyph source."""
        if self.caps and ch.islower() and len(ch.upper()) == 1:
            return ch.upper()
        return ch

    def covers(self, u: int) -> bool:
        if self.caps:
            u = ord(self.char_for(chr(u)))
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
        # Installed family used in place of this one (metric-compatible alias or look-alike).
        self.substitute: str | None = self._alias_name()
        self.similar: lookalike.Match | None = None
        self._similar_done = False
        self._slots: dict[tuple[int, bool], Slot] | None = None

    def _alias_name(self) -> str | None:
        if self.sys_key and self.sys_key != self.key:
            faces = self.owner.index.faces_for_key(self.sys_key)
            if faces:
                return faces[0].family
        return None

    @property
    def caps(self) -> bool:
        """The family draws lowercase letters as capitals (titling font)."""
        return bool(self.similar and self.similar.caps)

    def pending_download(self) -> str | None:
        """Google Fonts family being downloaded to stand in for this one."""
        m = self.similar
        if m is not None and m.google and self.sys_key is None:
            return m.key
        return None

    def slots(self) -> dict[tuple[int, bool], Slot]:
        if self._slots is None:
            slots: dict[tuple[int, bool], Slot] = {}
            for xref, weight, italic in self.owner.embedded_for_key(self.key):
                pf = self.owner.prepared(xref)
                if pf.usable:
                    # The font program knows its style better than names like "cmssbx10".
                    if pf.font is not None and pf.font.is_bold and weight < 600:
                        weight = 700
                    if pf.font is not None and pf.font.is_italic:
                        italic = True
                    slots.setdefault((weight, italic), Slot()).emb.append(xref)
            if self.sys_key is None and not self._similar_done:
                self._similar_done = True
                self._find_similar(slots)
            if self.sys_key:
                faces = self.owner.index.faces_for_key(self.sys_key)
                by_style = {(f.weight, f.italic): f for f in faces}
                if self.similar is not None and self.similar.key == self.sys_key:
                    # Each embedded face gets the look-alike face closest to it.
                    for slot, style in self.owner.lookalike_faces(self).items():
                        if style in by_style:
                            slots.setdefault(slot, Slot()).sys = by_style[style]
                for face in faces:
                    slot = slots.setdefault((face.weight, face.italic), Slot())
                    if slot.sys is None:
                        slot.sys = face
            self._slots = slots
        return self._slots

    def _find_similar(self, slots: dict[tuple[int, bool], Slot]) -> None:
        """Pick the font that looks the most like the embedded subsets, for their missing letters."""
        if google_fonts().lookup(self.key):
            return  # the real font is being downloaded
        queries = {}
        for slot, s in slots.items():
            for xref in s.emb:
                sig = self.owner.signature(xref)
                if sig is not None and len(sig.glyphs) >= 3:
                    queries[slot] = sig
                    break
        match = self.owner.lookalike(self.key, queries)
        if match is None:
            return
        self.similar = match
        if self.owner.index.faces_for_key(match.key):
            self.sys_key = match.key
            self.substitute = self.owner.index.faces_for_key(match.key)[0].family
        else:
            self.substitute = match.name
            google_fonts().ensure(match.key)  # in the background; the family refreshes once it is there

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
            "substitute": self.substitute,
            "bold": any(w >= 600 for w, _ in slots),
            "italic": any(it for _, it in slots),
            "caps": self.caps,
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
        self._type3_xrefs: dict[str, int] = {}
        self._type3_info: dict[int, Type3Info] = {}
        # Fonts written by Plume as substitutes, mapped to the family they stand for.
        self._stand_ins: dict[str, str] = {}
        self._prepared: dict[int, PreparedFont] = {}
        self._span_cache: dict[tuple[str, int], FamRef] = {}
        self._fitz_sys: dict[str, pymupdf.Font] = {}
        self._chains: dict[tuple[str, int, bool], list[ChainItem]] = {}
        self._signatures: dict[int, lookalike.Signature | None] = {}
        self._lookalikes: dict[str, lookalike.Match | None] = {}
        self._lookalike_faces: dict[str, dict[tuple[int, bool], tuple[int, bool]]] = {}
        self._generation = self.index.generation

    # ---------------------------------------------------------------- scanning
    def invalidate(self) -> None:
        """Call after the document changed (new fonts may have been embedded)."""
        with self._lock:
            self._emb_by_key = None
            self._reliable = {}
            self._type3_names = set()
            self._type3_xrefs = {}
            self._stand_ins = {}
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
                    typ, tag = self.doc.xref_get_key(xref, "PlumeFamily")
                    if typ == "string" and tag:
                        # Text extraction names spans after the font's PostScript name.
                        for alias in (name, _postscript_name(self.doc, xref)):
                            if alias:
                                self._stand_ins[alias] = tag
                    ok = is_text_reliable(self.doc, xref)
                    self._reliable[name] = self._reliable.get(name, True) and ok
                    if ftype == "Type3":
                        self._type3_names.add(name)
                        self._type3_xrefs[name] = xref
                        self._type3_xrefs[f"Type3 ({xref} 0 R)"] = xref
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
                sig = self.signature(xref)
                if sig is not None and sig.caps_only() and add_case_aliases(pf):
                    # Titling font: "a" and "A" are the same capital, whichever is typed.
                    self._signatures.pop(xref, None)
            return pf

    def signature(self, xref: int) -> lookalike.Signature | None:
        """Picture-based signature of an embedded font (see lookalike.py)."""
        with self._lock:
            if xref not in self._signatures:
                pf = self._prepared.get(xref) or self.prepared(xref)
                sig = None
                if pf.usable and pf.font is not None:
                    try:
                        sig = lookalike.font_signature(pf.font)
                    except Exception as exc:
                        log.debug("signature of font %s failed: %s", xref, exc)
                self._signatures[xref] = sig
            return self._signatures[xref]

    def lookalike_faces(self, fam: Family) -> dict[tuple[int, bool], tuple[int, bool]]:
        """Installed face of the look-alike family standing in for each embedded face."""
        m = fam.similar
        if m is None:
            return {}
        with self._lock:
            cached = self._lookalike_faces.get(fam.key)
            if cached is None:
                queries = {}
                for xref, weight, italic in self.embedded_for_key(fam.key):
                    sig = self.signature(xref)
                    pf = self.prepared(xref)
                    if pf.font is not None and pf.font.is_bold and weight < 600:
                        weight = 700
                    if pf.font is not None and pf.font.is_italic:
                        italic = True
                    if sig is not None and len(sig.glyphs) >= 3:
                        queries.setdefault((weight, italic), sig)
                try:
                    cached = lookalike.best_faces(self.index, m.key, queries, m.caps) or dict(m.faces)
                except Exception as exc:
                    log.debug("look-alike faces of %s: %s", fam.key, exc)
                    cached = dict(m.faces)
                self._lookalike_faces[fam.key] = cached
            return cached

    def lookalike(self, key: str, queries: dict[tuple[int, bool], lookalike.Signature]) -> lookalike.Match | None:
        """Font family looking the most like the given faces of family `key`."""
        with self._lock:
            if key not in self._lookalikes:
                match = None
                if queries:
                    try:
                        match = lookalike.find_lookalike(queries, lookalike.candidates(self.index), exclude={key})
                    except Exception as exc:
                        log.warning("look-alike search failed for %s: %s", key, exc)
                if match is not None:
                    log.info("look-alike for %s: %s (%.2f)", key, match.name, match.score)
                self._lookalikes[key] = match
            return self._lookalikes[key]

    # ---------------------------------------------------------------- families
    def _refresh_if_fonts_added(self) -> None:
        """New fonts (e.g. downloaded from Google Fonts) may change family resolution."""
        if self.index.generation != self._generation:
            self._generation = self.index.generation
            self._chains.clear()
            for fam in self.families.values():
                fam.sys_key = self.index.resolve_family_key(fam.key)
                fam.exact_sys = fam.sys_key == fam.key
                fam.substitute = fam._alias_name()
                fam.similar = None
                fam._similar_done = False
                fam._slots = None

    def missing_family_keys(self) -> list[str]:
        """Families used by the document that have no installed face of their own."""
        keys = set(self._scan())
        return sorted(k for k in keys if self.index.resolve_family_key(k) != k)

    def request_downloads(self, keys: list[str] | None = None) -> list[str]:
        """Start downloading the families available on Google Fonts; returns the ones started."""
        gf = google_fonts()
        started = []
        for key in keys if keys is not None else self.missing_family_keys():
            for candidate in ALIASES.get(key, [key]):
                if self.index.resolve_family_key(candidate) == candidate:
                    break  # already installed
                if gf.lookup(candidate):
                    gf.ensure(candidate)
                    started.append(candidate)
                    break
        return started

    def family(self, key: str, name: str | None = None, generic: str | None = None) -> Family:
        with self._lock:
            self._refresh_if_fonts_added()
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
            with self._lock:
                self._scan()
                stand_in = self._stand_ins.get(strip_subset(fontname))
            if stand_in:
                fam = self.family(stand_in)
                ref = FamRef(fam.key, info["weight"], info["italic"])
                self._span_cache[cache_key] = ref
                return ref
            if self.is_type3(fontname):
                t3 = self.type3_info(fontname)
                if t3.family:
                    fam = self.family(t3.family, t3.name, info["generic"])
                    ref = FamRef(fam.key, t3.weight, t3.italic or info["italic"], t3.render, t3.outline)
                    self._span_cache[cache_key] = ref
                    return ref
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

    def type3_info(self, fontname: str) -> Type3Info:
        """Family a Type 3 font copies, and whether it draws the letters as outlines.

        Canva exports text with an outline effect as Type 3 glyphs whose font
        descriptor still names the original font ("Intro"): the text is then
        rewritten with that font, stroked instead of filled.
        """
        with self._lock:
            self._scan()
            xref = self._type3_xrefs.get(strip_subset(fontname)) or self._type3_xrefs.get(fontname)
            if xref is None:
                return Type3Info()
            info = self._type3_info.get(xref)
            if info is not None:
                return info
            info = Type3Info()
            self._type3_info[xref] = info
            name = _type3_font_name(self.doc, xref)
            parsed = parse_font_name(name) if name else None
            if parsed is None or parsed["key"] in ("", "unknown") or parsed["key"].startswith(("unnamed", "type3")):
                return info
            info.family, info.name = parsed["key"], parsed["name"]
            info.weight, info.italic = parsed["weight"], parsed["italic"]
            # Compare with the same letters of the real font, when we have it.
            ref_font = None
            emb = sorted(self._scan().get(info.family, []), key=lambda t: (t[2], abs(t[1] - info.weight)))
            for exref, _w, _it in emb:
                pf = self.prepared(exref)
                if pf.usable and pf.font is not None:
                    ref_font = pf.font
                    break
            if ref_font is None and self.index.resolve_family_key(info.family) == info.family:
                faces = sorted(self.index.faces_for_key(info.family), key=lambda f: (f.italic, abs(f.weight - info.weight)))
                if faces:
                    ref_font = self.sys_font(faces[0])
            if ref_font is not None:
                try:
                    found = lookalike.compare_type3(self.doc, xref, ref_font)
                except Exception as exc:
                    log.debug("type3 comparison failed for %s: %s", xref, exc)
                    found = None
                if found is not None:
                    growth, ink = found
                    if growth > 0.006:
                        info.render = "stroke" if ink < 0.9 else "fillstroke"
                        info.outline = round(growth, 4)
            return info

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

    def _items_for(self, fam: Family, weight: int, italic: bool, stands_for: str | None = None) -> list[ChainItem]:
        m = fam.match(weight, italic)
        if not m:
            return []
        slot, sb, si = m
        items = []
        for xref in slot.emb:
            pf = self.prepared(xref)
            if pf.usable and pf.font is not None:
                items.append(ChainItem(pf.font, pf.codepoints, sb, si, f"emb:{xref}", stands_for))
        if slot.sys is not None:
            # A look-alike installed font stands for the family it replaces.
            sys_for = stands_for or (None if fam.exact_sys else fam.key)
            caps = fam.caps and not fam.exact_sys
            items.append(ChainItem(self.sys_font(slot.sys), None, sb, si, f"sys:{slot.sys.id}", sys_for, caps))
        return items

    def wait_for_downloads(self, keys: list[str]) -> None:
        """Give the downloads of fonts standing in for these families a moment to finish."""
        gf = google_fonts()
        for key in keys:
            if self.index.resolve_family_key(key) != key:
                for candidate in ALIASES.get(key, [key]):
                    if gf.lookup(candidate):
                        gf.ensure(candidate, wait=DOWNLOAD_WAIT)
                        break
            with self._lock:
                fam = self.family(key)
                fam.slots()  # may start downloading a look-alike
                pending = fam.pending_download()
            if pending:
                gf.ensure(pending, wait=DOWNLOAD_WAIT)

    def chain(self, key: str, weight: int, italic: bool) -> list[ChainItem]:
        ck = (key, int(weight), bool(italic))
        if ck not in self._chains:
            self.wait_for_downloads([key])
        with self._lock:
            self._refresh_if_fonts_added()
            cached = self._chains.get(ck)
            if cached is not None:
                return cached
            fam = self.family(key)
            items = self._items_for(fam, weight, italic)
            gfam = self.generic_family(fam.generic)
            if gfam is not None and gfam.key != fam.key:
                items += self._items_for(gfam, weight, italic, stands_for=key)
            b14 = _B14[(fam.generic, weight >= 600, bool(italic))]
            items.append(ChainItem(pymupdf.Font(b14), None, False, False, f"b14:{b14}", key))
            for ukey in self.index.universal_keys():
                items += self._items_for(self.family(ukey), 400, False, stands_for=key)
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

        self.wait_for_downloads(keys)
        with self._lock:
            self._refresh_if_fonts_added()
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


def _type3_font_name(doc: pymupdf.Document, xref: int) -> str | None:
    """Name of the font a Type 3 font was made from, from its descriptor."""
    typ, val = doc.xref_get_key(xref, "FontDescriptor")
    if typ == "xref":
        desc = int(val.split()[0])
        for key in ("FontName", "FontFamily"):
            t, v = doc.xref_get_key(desc, key)
            if t == "name" and v.strip("/"):
                return strip_subset(v.lstrip("/"))
            if t == "string" and v.strip():
                return v.strip()
    t, v = doc.xref_get_key(xref, "BaseFont")
    if t == "name" and v.strip("/"):
        return strip_subset(v.lstrip("/"))
    return None


def _postscript_name(doc: pymupdf.Document, xref: int) -> str | None:
    try:
        import io

        from fontTools.ttLib import TTFont

        buf = doc.extract_font(xref)[3]
        if not buf:
            return None
        name = TTFont(io.BytesIO(buf), lazy=True)["name"].getDebugName(6)
        return strip_subset(name) if name else None
    except Exception:
        return None


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
