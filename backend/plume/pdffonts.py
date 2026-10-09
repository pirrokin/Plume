"""Reuse of the fonts embedded in a PDF.

Embedded fonts are usually subsets, and often lack a Unicode cmap (macOS
Quartz, some Word exports). This module reads the PDF font dictionary
(ToUnicode, Widths, CIDToGIDMap), rebuilds a Unicode cmap when needed and
verifies that the result maps characters to the right glyphs by comparing the
glyph advances with the widths declared in the PDF.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

import pymupdf
from fontTools.fontBuilder import FontBuilder
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import cmap_format_4, cmap_format_12

from .fontnames import _style_info, pretty_family, split_font_name, strip_subset

log = logging.getLogger(__name__)
logging.getLogger("fontTools").setLevel(logging.ERROR)

_REF_RE = re.compile(r"^(\d+)\s+0\s+R$")


@dataclass
class PreparedFont:
    xref: int
    basefont: str
    usable: bool = False
    font: pymupdf.Font | None = None
    buffer: bytes = b""
    web_ok: bool = False  # buffer is a complete sfnt a browser can load
    web_mime: str = "font/ttf"
    codepoints: set[int] = field(default_factory=set)
    repaired: bool = False
    reason: str = ""


# ---------------------------------------------------------------- PDF objects
def _resolve(doc: pymupdf.Document, xref: int, key: str) -> tuple[str, str]:
    """Value of a dictionary key, following one level of indirection."""
    typ, val = doc.xref_get_key(xref, key)
    if typ == "xref":
        m = _REF_RE.match(val.strip())
        if m:
            ref = int(m.group(1))
            if doc.xref_is_stream(ref):
                return "stream", str(ref)
            obj = doc.xref_object(ref, compressed=True)
            return ("array" if obj.strip().startswith("[") else "dict"), obj
    return typ, val


_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)"


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(_NUMBER, text)]


def _hex_to_str(h: str) -> str:
    if not h:
        return ""
    if len(h) % 4:
        h = h.rjust(len(h) + (4 - len(h) % 4), "0")
    try:
        return bytes.fromhex(h).decode("utf-16-be", errors="replace")
    except ValueError:
        return ""


def parse_tounicode(data: bytes) -> dict[int, str]:
    text = data.decode("latin-1", errors="replace")
    out: dict[int, str] = {}
    for block in re.finditer(r"beginbfchar(.*?)endbfchar", text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block.group(1)):
            out[int(m.group(1), 16)] = _hex_to_str(m.group(2))
    for block in re.finditer(r"beginbfrange(.*?)endbfrange", text, re.S):
        body = block.group(1)
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", body):
            lo, hi = int(m.group(1), 16), int(m.group(2), 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            dst = m.group(3)
            if dst.startswith("["):
                for i, h in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    if lo + i <= hi:
                        out[lo + i] = _hex_to_str(h)
            else:
                h = dst[1:-1]
                if not h:
                    continue
                base = int(h, 16)
                width = len(h)
                for code in range(lo, hi + 1):
                    out[code] = _hex_to_str(format(base + code - lo, f"0{width}X"))
    return out


def _parse_w_array(text: str) -> dict[int, float]:
    """Parse a CID font /W array."""
    tokens = re.findall(r"\[|\]|" + _NUMBER, text)
    # Drop the outer brackets.
    if tokens and tokens[0] == "[":
        tokens = tokens[1:]
    if tokens and tokens[-1] == "]":
        tokens = tokens[:-1]
    widths: dict[int, float] = {}
    i = 0
    while i < len(tokens):
        if tokens[i] in "[]":
            i += 1
            continue
        first = int(float(tokens[i]))
        if i + 1 < len(tokens) and tokens[i + 1] == "[":
            j = i + 2
            cid = first
            while j < len(tokens) and tokens[j] != "]":
                widths[cid] = float(tokens[j])
                cid += 1
                j += 1
            i = j + 1
        elif i + 2 < len(tokens):
            last = int(float(tokens[i + 1]))
            w = float(tokens[i + 2])
            if 0 <= last - first <= 0xFFFF:
                for cid in range(first, last + 1):
                    widths[cid] = w
            i += 3
        else:
            break
    return widths


@dataclass
class FontDict:
    subtype: str = ""
    basefont: str = ""
    encoding: str = ""
    symbolic: bool = False
    to_unicode: dict[int, str] = field(default_factory=dict)
    widths: dict[int, float] = field(default_factory=dict)
    widths_declared: bool = False
    cid_to_gid: dict[int, int] | None = None  # None means identity


def read_font_dict(doc: pymupdf.Document, xref: int) -> FontDict:
    fd = FontDict()
    fd.subtype = doc.xref_get_key(xref, "Subtype")[1].lstrip("/")
    fd.basefont = doc.xref_get_key(xref, "BaseFont")[1].lstrip("/")
    enc_type, enc = doc.xref_get_key(xref, "Encoding")
    fd.encoding = enc.lstrip("/") if enc_type == "name" else ("dict" if enc_type in ("dict", "xref") else "")

    typ, val = _resolve(doc, xref, "ToUnicode")
    if typ == "stream":
        try:
            fd.to_unicode = parse_tounicode(doc.xref_stream(int(val)) or b"")
        except Exception:
            fd.to_unicode = {}

    desc_xref = xref
    if fd.subtype == "Type0":
        typ, val = doc.xref_get_key(xref, "DescendantFonts")
        refs = re.findall(r"(\d+)\s+0\s+R", val)
        if typ == "xref" and refs:  # indirect array
            refs = re.findall(r"(\d+)\s+0\s+R", doc.xref_object(int(refs[0])))
        if refs:
            desc_xref = int(refs[0])
            typ, val = _resolve(doc, desc_xref, "W")
            if typ == "array":
                fd.widths = _parse_w_array(val)
                fd.widths_declared = bool(fd.widths)
            typ, val = _resolve(doc, desc_xref, "CIDToGIDMap")
            if typ == "stream":
                raw = doc.xref_stream(int(val)) or b""
                fd.cid_to_gid = {i // 2: (raw[i] << 8) | raw[i + 1] for i in range(0, len(raw) - 1, 2)}
    else:
        typ, val = _resolve(doc, xref, "Widths")
        first = doc.xref_get_key(xref, "FirstChar")[1]
        if typ == "array" and first.lstrip("-").isdigit():
            fc = int(first)
            fd.widths = {fc + i: w for i, w in enumerate(_numbers(val))}
            fd.widths_declared = bool(fd.widths)

    typ, val = doc.xref_get_key(desc_xref, "FontDescriptor")
    m = _REF_RE.match(val.strip()) if typ == "xref" else None
    if m:
        flags = doc.xref_get_key(int(m.group(1)), "Flags")[1]
        if flags.lstrip("-").isdigit():
            fd.symbolic = bool(int(flags) & 4) and not bool(int(flags) & 32)

    if not fd.to_unicode and fd.subtype in ("TrueType", "Type1", "MMType1") and not fd.symbolic:
        fd.to_unicode = _encoding_to_unicode(doc, xref, fd.encoding)
    return fd


_CODECS = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman", "": "cp1252", "StandardEncoding": "latin-1"}


def _encoding_to_unicode(doc: pymupdf.Document, xref: int, encoding: str) -> dict[int, str]:
    """Code to Unicode mapping implied by a simple font's base encoding."""
    if encoding == "dict":
        typ, val = _resolve(doc, xref, "Encoding")
        m = re.search(r"/BaseEncoding\s*/(\w+)", val) if typ == "dict" else None
        encoding = m.group(1) if m else ""
        if typ == "dict" and "/Differences" in val:
            return {}  # glyph-name remapping: too unreliable to reconstruct
    codec = _CODECS.get(encoding)
    if codec is None:
        return {}
    out = {}
    for code in range(32, 256):
        try:
            ch = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            continue
        if ch.isprintable():
            out[code] = ch
    return out


# ---------------------------------------------------------------- repair
def _unicode_cmap(tt: TTFont) -> dict[int, str]:
    if "cmap" not in tt:
        return {}
    best: dict[int, str] = {}
    for table in tt["cmap"].tables:
        if table.isUnicode() and not (table.platformID == 3 and table.platEncID == 0):
            best.update(table.cmap)
    return best


def _code_to_glyph(tt: TTFont, fd: FontDict) -> dict[int, str]:
    """Map PDF character codes to glyph names of the embedded font program."""
    order = tt.getGlyphOrder()
    out: dict[int, str] = {}
    if fd.subtype == "Type0":
        if fd.encoding not in ("Identity-H", "Identity-V"):
            return {}
        for code in fd.to_unicode:
            gid = code if fd.cid_to_gid is None else fd.cid_to_gid.get(code)
            if gid is not None and 0 < gid < len(order):
                out[code] = order[gid]
        return out
    if fd.subtype != "TrueType" or "cmap" not in tt:
        return {}
    sym = tt["cmap"].getcmap(3, 0)
    mac = tt["cmap"].getcmap(1, 0)
    for code in fd.to_unicode:
        name = None
        if sym is not None:
            name = sym.cmap.get(0xF000 + code) or sym.cmap.get(code)
        if name is None and mac is not None and (fd.symbolic or not fd.encoding):
            name = mac.cmap.get(code)
        if name is not None:
            out[code] = name
    return out


def _add_unicode_cmap(tt: TTFont, mapping: dict[int, str]) -> None:
    if "cmap" not in tt:
        cmap = newTable("cmap")
        cmap.tableVersion = 0
        cmap.tables = []
        tt["cmap"] = cmap
    tables = [t for t in tt["cmap"].tables if not (t.platformID == 3 and t.platEncID in (1, 10)) and t.platformID != 0]
    bmp = {u: g for u, g in mapping.items() if u <= 0xFFFF}
    sub = cmap_format_4(4)
    sub.platformID, sub.platEncID, sub.language = 3, 1, 0
    sub.cmap = bmp
    tables.append(sub)
    if any(u > 0xFFFF for u in mapping):
        sub12 = cmap_format_12(12)
        sub12.platformID, sub12.platEncID, sub12.language = 3, 10, 0
        sub12.cmap = dict(mapping)
        tables.append(sub12)
    tt["cmap"].tables = tables


def _complete_tables(tt: TTFont, psname: str) -> None:
    """Add the tables browsers require that PDF subsets frequently omit.

    The PostScript name is set to the original BaseFont so that text written
    with this font is recognised as the same family when read back.
    """
    fb = FontBuilder(font=tt)
    upem = tt["head"].unitsPerEm
    # Always (re)write the names from the PDF BaseFont: subsets often carry no
    # family name at all, and MuPDF names the fonts it embeds after it.
    base, style = split_font_name(psname)
    weight, italic, _ = _style_info(style)
    bold = (weight or 400) >= 600
    style_name = ("Bold " if bold else "") + ("Italic" if italic else "")
    style_name = style_name.strip() or "Regular"
    family = pretty_family(base)
    fb.setupNameTable({
        "familyName": family,
        "styleName": style_name,
        "fullName": f"{family} {style_name}",
        "psName": re.sub(r"[^A-Za-z0-9-]", "", psname)[:63] or "PlumeFont",
        "uniqueFontIdentifier": f"Plume:{psname}",
    })
    if "post" not in tt:
        fb.setupPost()
    if "OS/2" not in tt:
        hhea = tt["hhea"]
        fb.setupOS2(
            sTypoAscender=hhea.ascent,
            sTypoDescender=hhea.descent,
            sTypoLineGap=hhea.lineGap,
            usWinAscent=max(0, hhea.ascent),
            usWinDescent=max(0, -hhea.descent),
            sxHeight=int(upem * 0.5),
            sCapHeight=int(upem * 0.7),
        )
        os2 = tt["OS/2"]
        cmap = _unicode_cmap(tt)
        if cmap:
            os2.usFirstCharIndex = min(min(cmap), 0xFFFF)
            os2.usLastCharIndex = min(max(cmap), 0xFFFF)
    if "OS/2" in tt:
        tt["OS/2"].fsType = 0  # allow use as a web font


def _sfnt_bytes(tt: TTFont) -> bytes:
    buf = io.BytesIO()
    tt.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- verification
def _verify(font: pymupdf.Font, fd: FontDict) -> tuple[bool, set[int], str]:
    """Check that Unicode characters reach glyphs with the widths declared in the PDF."""
    checked = matched = 0
    mismatched: list[str] = []
    codepoints: set[int] = set()
    for code, text in fd.to_unicode.items():
        if len(text) != 1:
            continue
        u = ord(text)
        if u < 32 or not font.has_glyph(u):
            continue
        if not text.isspace():
            try:
                if font.glyph_bbox(u).is_empty:
                    continue  # glyph stripped by the subsetter
            except Exception:
                continue
        codepoints.add(u)
        w = fd.widths.get(code)
        if not fd.widths_declared or w is None or w <= 0:
            continue
        checked += 1
        adv = font.glyph_advance(u) * 1000.0
        if abs(adv - w) <= max(12.0, 0.03 * w):
            matched += 1
        else:
            mismatched.append(text)
    if not codepoints:
        return False, set(), "aucun caractère Unicode exploitable"
    if checked and len(mismatched) > max(1, 0.05 * checked):
        return False, set(), f"largeurs incohérentes ({''.join(mismatched[:8])})"
    return True, codepoints, ""


def prepare_font(doc: pymupdf.Document, xref: int) -> PreparedFont:
    basefont = ""
    try:
        name, ext, ftype, buf = doc.extract_font(xref)
        basefont = name or ""
    except Exception as exc:
        return PreparedFont(xref, basefont, reason=f"extraction impossible: {exc}")
    pf = PreparedFont(xref, strip_subset(basefont))
    if ftype == "Type3" or not buf:
        pf.reason = "police non intégrée ou Type3"
        return pf
    fd = read_font_dict(doc, xref)

    tt = None
    if ext in ("ttf", "otf"):
        try:
            tt = TTFont(io.BytesIO(buf), lazy=False)
        except Exception:
            tt = None

    candidate = buf
    if tt is not None:
        try:
            existing = _unicode_cmap(tt)
            usable_codes = [c for c, t in fd.to_unicode.items() if len(t) == 1 and ord(t[0]) in existing]
            if fd.to_unicode and len(usable_codes) < 0.6 * len([t for t in fd.to_unicode.values() if len(t) == 1]):
                mapping = {}
                for code, glyph in _code_to_glyph(tt, fd).items():
                    text = fd.to_unicode.get(code, "")
                    if len(text) == 1 and ord(text) >= 32:
                        mapping.setdefault(ord(text), glyph)
                if mapping:
                    merged = dict(existing)
                    merged.update(mapping)
                    _add_unicode_cmap(tt, merged)
                    pf.repaired = True
            _complete_tables(tt, pf.basefont or f"PlumeEmb{xref}")
            candidate = _sfnt_bytes(tt)
            pf.web_ok = True
            pf.web_mime = "font/otf" if ("CFF " in tt or "CFF2" in tt) else "font/ttf"
        except Exception as exc:
            log.debug("font %s repair failed: %s", xref, exc)
            candidate = buf
            pf.web_ok = False

    try:
        font = pymupdf.Font(fontbuffer=candidate)
    except Exception as exc:
        pf.reason = f"police illisible: {exc}"
        return pf
    ok, codepoints, reason = _verify(font, fd)
    if not ok and candidate is not buf:
        # The repaired version failed; the original program may still be fine.
        try:
            font2 = pymupdf.Font(fontbuffer=buf)
            ok, codepoints, reason = _verify(font2, fd)
            if ok:
                font, candidate, pf.web_ok, pf.repaired = font2, buf, False, False
        except Exception:
            pass
    pf.usable = ok
    pf.reason = reason
    if ok:
        pf.font = font
        pf.buffer = candidate
        pf.codepoints = codepoints
    return pf


def add_case_aliases(pf: PreparedFont) -> bool:
    """Titling font: give each letter's missing case the glyph of the other case.

    Fonts like Canva's "Intro" draw lowercase letters as capitals, so the
    subset holding "k" can also write "K". Returns whether letters were added.
    """
    if not pf.usable or pf.font is None or not pf.buffer:
        return False
    try:
        tt = TTFont(io.BytesIO(pf.buffer), lazy=False)
        cmap = _unicode_cmap(tt)
        added: dict[int, str] = {}
        for u in sorted(pf.codepoints):
            if u not in cmap:
                continue
            for other in {chr(u).upper(), chr(u).lower()}:
                o = ord(other) if len(other) == 1 else 0
                if o and o != u and o not in pf.codepoints and o not in cmap:
                    added.setdefault(o, cmap[u])
        if not added:
            return False
        merged = dict(cmap)
        merged.update(added)
        _add_unicode_cmap(tt, merged)
        data = _sfnt_bytes(tt)
        font = pymupdf.Font(fontbuffer=data)
    except Exception as exc:
        log.debug("case aliases for font %s failed: %s", pf.xref, exc)
        return False
    pf.font, pf.buffer = font, data
    pf.codepoints |= set(added)
    return True


_STD_GLYPH_RE = re.compile(r"^(?:[A-Za-z]{1,16}|uni[0-9A-Fa-f]{4,6}|u[0-9A-Fa-f]{4,6}|[A-Za-z]+\.(?:sc|alt|ss\d\d))$")
_ODD_GLYPH_RE = re.compile(r"^(?:[a-zA-Z]{1,2}\d+|glyph\d+|g\d+|cid\d+|index\d+|c\d+)$")


def is_text_reliable(doc: pymupdf.Document, xref: int) -> bool:
    """Whether MuPDF can decode this font's characters to the right Unicode text."""
    try:
        subtype = doc.xref_get_key(xref, "Subtype")[1].lstrip("/")
        if doc.xref_get_key(xref, "ToUnicode")[0] == "xref":
            return True
        if subtype == "Type0":
            return False
        if subtype == "Type3":
            typ, val = _resolve(doc, xref, "Encoding")
            names = re.findall(r"/([^\s/\[\]<>()]+)", val) if typ in ("dict", "array") else []
            names = [n for n in names if n not in ("Encoding", "Differences", "Type")]
            return bool(names) and all(_STD_GLYPH_RE.match(n) and not _ODD_GLYPH_RE.match(n) for n in names)
        if subtype == "TrueType":
            fd = read_font_dict(doc, xref)
            if fd.symbolic and fd.encoding in ("", "dict"):
                return False
        return True
    except Exception:
        return True
