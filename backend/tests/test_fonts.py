"""Font matching, outlined Type 3 titles, invisible text copies and line fitting."""

import copy
import io

import pymupdf
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from plume.docfonts import DocFonts
from plume.editor import edit_block
from plume.extract import extract_page
from plume.layout import layout
from plume.lookalike import Signature, catalog, find_lookalike, font_signature


def _text(block):
    return "".join(r["text"] for r in block["runs"])


def _find(model, start):
    return next(b for b in model["blocks"] if _text(b).startswith(start))


def _box(block):
    box = {k: block[k] for k in ("x", "width", "wrap", "align", "lineHeight", "indent", "size")}
    box["y"] = block["baseline"]
    return box


def test_lookalike_catalog_matches_standard_fonts():
    cat = catalog()
    assert len(cat) > 500
    categories = {c.key: c.category for c in cat}
    found = {}
    for name, slot in (("helv", (400, False)), ("hebo", (700, False)), ("tiro", (400, False)), ("cour", (400, False))):
        m = find_lookalike({slot: font_signature(pymupdf.Font(name))}, cat)
        found[name] = m
    assert found["helv"].key == "arimo"  # Arial's open twin looks like Helvetica
    assert found["hebo"].faces[(700, False)][0] >= 600
    assert categories[found["tiro"].key] == "Serif"
    assert categories[found["cour"].key] == "Monospace"


def test_titling_font_is_compared_as_capitals():
    sig = font_signature(pymupdf.Font("helv"), "ABCDE")
    titling = Signature(dict(sig.glyphs), sig.serif, sig.stem, sig.slant)
    for c in "ABCDE":
        titling.glyphs[c.lower()] = sig.glyphs[c]
    assert titling.caps_only()
    assert not font_signature(pymupdf.Font("helv")).caps_only()


# A tiny TrueType font whose letters are rectangles, and a Type 3 font drawing
# the same letters as outlines (Canva's "hollow" text effect: the outline of
# the stroke is filled, in shape-only glyphs).
_SHAPES = {
    "I": [(100, 0, 220, 700)],
    "L": [(100, 0, 520, 140)],
    "T": [(250, 0, 370, 580)],
    "E": [(100, 0, 520, 700)],
}
_ADV = 620


def _rect_font() -> bytes:
    order = [".notdef", "space"] + list(_SHAPES)
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({32: "space", **{ord(c): c for c in _SHAPES}})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        for x0, y0, x1, y1 in _SHAPES.get(name, []):
            pen.moveTo((x0, y0))
            pen.lineTo((x0, y1))
            pen.lineTo((x1, y1))
            pen.lineTo((x1, y0))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (_ADV if n in _SHAPES else 250, 0) for n in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Plume Rect", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    buf = io.BytesIO()
    fb.save(buf)
    return buf.getvalue()


def _add_hollow_type3(doc: pymupdf.Document, page: pymupdf.Page, stroke: float) -> None:
    """Type 3 font named after "PlumeRect" whose glyphs stroke the letters' outlines."""
    procs, codes = {}, {}
    h = stroke / 2
    for i, (ch, rects) in enumerate(_SHAPES.items()):
        ops = [f"{_ADV} 0 0 0 1000 1000 d1"]
        for x0, y0, x1, y1 in rects:
            ops.append(f"{x0 - h} {y0 - h} {x1 - x0 + stroke} {y1 - y0 + stroke} re")
            ops.append(f"{x0 + h} {y0 + h} {x1 - x0 - stroke} {y1 - y0 - stroke} re")
        ops.append("f*")
        x = doc.get_new_xref()
        doc.update_object(x, "<<>>")
        doc.update_stream(x, "\n".join(ops).encode())
        procs[ch] = x
        codes[ch] = 65 + i
    cmap = "\n".join(f"<{codes[c]:02X}> <{ord(c):04X}>" for c in _SHAPES)
    tu = doc.get_new_xref()
    doc.update_object(tu, "<<>>")
    doc.update_stream(tu, (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        f"{len(codes)} beginbfchar\n{cmap}\nendbfchar\nendcmap end end"
    ).encode())
    desc = doc.get_new_xref()
    doc.update_object(desc, "<</Type/FontDescriptor/FontName/ABCDEF+PlumeRect/Flags 32/ItalicAngle 0>>")
    diffs = " ".join(f"{codes[c]} /{c}" for c in _SHAPES)
    charprocs = " ".join(f"/{c} {procs[c]} 0 R" for c in _SHAPES)
    widths = " ".join(str(_ADV) for _ in _SHAPES)
    font = doc.get_new_xref()
    doc.update_object(font, (
        f"<</Type/Font/Subtype/Type3/FontBBox[0 0 1000 1000]/FontMatrix[.001 0 0 .001 0 0]"
        f"/CharProcs<<{charprocs}>>/Encoding<</Type/Encoding/Differences[{diffs}]>>"
        f"/FirstChar 65/LastChar {64 + len(codes)}/Widths[{widths}]/FontDescriptor {desc} 0 R/ToUnicode {tu} 0 R>>"
    ))
    holder, prefix = page.xref, "Resources/"
    typ, val = doc.xref_get_key(holder, "Resources")
    if typ == "xref":
        holder, prefix = int(val.split()[0]), ""
    typ, val = doc.xref_get_key(holder, prefix + "Font")
    if typ == "xref":
        doc.xref_set_key(int(val.split()[0]), "T3", f"{font} 0 R")
    else:
        doc.xref_set_key(holder, prefix + "Font/T3", f"{font} 0 R")
    text = "".join(f"{codes[c]:02X}" for c in "TILE")
    contents = page.get_contents()[0]
    data = (doc.xref_stream(contents) or b"") + f"\nBT /T3 40 Tf 1 0 0 1 300 600 Tm <{text}> Tj ET".encode()
    doc.update_stream(contents, data)


def test_outlined_type3_title_is_rewritten_with_its_font(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="rect", fontbuffer=_rect_font())
    page.insert_text((72, 242), "TITLE", fontsize=40, fontname="rect")
    _add_hollow_type3(doc, page, stroke=40)
    path = tmp_path / "hollow.pdf"
    doc.save(path)

    doc = pymupdf.open(path)
    fonts = DocFonts(doc)
    model = extract_page(doc[0], fonts)
    hollow = next(b for b in model["blocks"] if b["bbox"][0] > 250)
    run = hollow["runs"][0]
    assert run["family"] == "plumerect"  # named after the font descriptor, not "unknown"
    assert run["render"] == "stroke"
    assert 0.03 <= run["outline"] <= 0.05

    runs = copy.deepcopy(hollow["runs"])
    runs[0]["text"] = "LILT"
    edit_block(doc, fonts, 0, model["internal"][hollow["id"]], runs, _box(hollow))
    again = extract_page(doc[0], DocFonts(doc))
    edited = next(b for b in again["blocks"] if b["bbox"][0] > 250)
    assert _text(edited) == "LILT"
    assert edited["runs"][0]["render"] == "stroke"
    assert abs(edited["runs"][0]["outline"] - run["outline"]) < 0.005


def test_invisible_copy_is_edited_with_the_visible_text(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Bonjour le monde", fontsize=24, fontname="helv")
    page.insert_text((72, 100), "Bonjour le monde", fontsize=24, fontname="helv", render_mode=3)
    fonts = DocFonts(doc)
    model = extract_page(page, fonts)
    blocks = [b for b in model["blocks"] if "Bonjour" in _text(b)]
    assert len(blocks) == 1
    runs = copy.deepcopy(blocks[0]["runs"])
    runs[0]["text"] = "Au revoir"
    edit_block(doc, fonts, 0, model["internal"][blocks[0]["id"]], runs, _box(blocks[0]))
    text = doc[0].get_text()
    assert "Au revoir" in text and "Bonjour" not in text


def test_paragraph_keeps_its_line_breaks_when_positions_are_tighter_than_the_font(tmp_path):
    # Word positions letters with its own rounding: lines can come out a little
    # narrower than the font measures them.
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "x", fontsize=1, fontname="helv")
    lines = [
        "Afin de comparer plusieurs convertisseurs analogiques, on a réalisé un montage",
        "simple contenant le convertisseur et un potentiomètre alimenté par une source",
        "de tension continue.",
    ]
    ops = ["BT /helv 11 Tf -0.06 Tc 72 700 Td 14 TL"]
    for line in lines:
        ops.append("(" + line.encode("cp1252").decode("latin-1").replace("(", "\\(").replace(")", "\\)") + ") '")
    ops.append("ET")
    xref = page.get_contents()[0]
    doc.update_stream(xref, "\n".join(ops).encode("latin-1"))
    fonts = DocFonts(doc)
    model = extract_page(doc[0], fonts)
    block = _find(model, "Afin de comparer")
    assert block["wrap"] and len(block["lines"]) == 3
    result = layout(block["runs"], _box(block), fonts)
    assert [l.text.strip() for l in result.lines] == lines
