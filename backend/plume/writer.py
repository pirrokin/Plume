"""Low-level page writing: text placement, decorations and text removal."""

from __future__ import annotations

import logging
from collections import defaultdict

import pymupdf

from .docfonts import SYNTH_ITALIC_SKEW
from .layout import Deco, Piece

log = logging.getLogger(__name__)

_CORRECTIONS: dict[tuple, pymupdf.Matrix] = {}


def _rgb(color: str) -> tuple[float, float, float]:
    c = color.lstrip("#")
    return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0)


def _geometry_key(page: pymupdf.Page) -> tuple:
    return (tuple(page.mediabox), tuple(page.cropbox), page.rotation)


def textwriter_correction(page: pymupdf.Page) -> pymupdf.Matrix:
    """Translation that makes TextWriter land on the coordinates `get_text` reports.

    PyMuPDF's TextWriter misplaces text on pages whose CropBox does not start at
    the MediaBox origin. We measure the offset once per page geometry on a blank
    page with the same boxes.
    """
    key = _geometry_key(page)
    m = _CORRECTIONS.get(key)
    if m is not None:
        return m
    probe_doc = pymupdf.open()
    probe_doc.insert_pdf(page.parent, from_page=page.number, to_page=page.number, annots=False, links=False)
    probe = probe_doc[0]
    probe_doc.xref_set_key(probe.xref, "Contents", "null")
    probe_doc.xref_set_key(probe.xref, "Resources", "<<>>")
    probe = probe_doc.reload_page(probe)
    target = pymupdf.Point(probe.rect.width / 3, probe.rect.height / 3)
    if probe.rotation:
        target = target * probe.derotation_matrix
    tw = pymupdf.TextWriter(probe.rect)
    tw.append(target, "H", font=pymupdf.Font("helv"), fontsize=10)
    tw.write_text(probe)
    got = None
    for b in probe.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for span in line["spans"]:
                got = span["origin"]
    if got is None:
        m = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
    else:
        # A translation applied in PDF space moves page y in the opposite direction.
        m = pymupdf.Matrix(1, 0, 0, 1, target.x - got[0], got[1] - target.y)
    _CORRECTIONS[key] = m
    return m


def write_pieces(page: pymupdf.Page, pieces: list[Piece], decos: list[Deco]) -> None:
    if not pieces and not decos:
        return
    corr = textwriter_correction(page)
    groups: dict[tuple, pymupdf.TextWriter] = defaultdict(lambda: pymupdf.TextWriter(page.rect))
    italic_pieces: list[Piece] = []
    for piece in pieces:
        if piece.item.synth_italic:
            italic_pieces.append(piece)
            continue
        tw = groups[_group_key(piece)]
        _append(tw, piece)
    for key, tw in groups.items():
        color, alpha, mode = key
        tw.write_text(page, color=_rgb(color), opacity=alpha, render_mode=mode, matrix=corr)
    for piece in italic_pieces:
        color, alpha, mode = _group_key(piece)
        tw = pymupdf.TextWriter(page.rect)
        _append(tw, piece)
        # Shear about the piece's origin, expressed in TextWriter's PDF space.
        p = pymupdf.Point(piece.x, piece.y) * tw.ictm
        delta = pymupdf.Matrix(1, 1).pretranslate(p.x, p.y)
        shear = ~delta * pymupdf.Matrix(1, 0, SYNTH_ITALIC_SKEW, 1, 0, 0) * delta
        tw.write_text(page, color=_rgb(color), opacity=alpha, render_mode=mode, matrix=shear * corr)
    for d in decos:
        _draw_deco(page, d)


def _group_key(piece: Piece) -> tuple:
    render = piece.style.get("render", "fill")
    mode = {"fill": 0, "stroke": 1, "fillstroke": 2}.get(render, 0)
    if piece.item.synth_bold and mode == 0:
        mode = 2
    return (piece.style["color"], float(piece.style["alpha"]), mode)


def _append(tw: pymupdf.TextWriter, piece: Piece) -> None:
    font = piece.item.font
    size = piece.style["size"]
    natural = [font.glyph_advance(ord(ch)) * size for ch in piece.text]
    if all(abs(a - n) < 1e-3 for a, n in zip(piece.advances, natural)):
        tw.append((piece.x, piece.y), piece.text, font=font, fontsize=size)
        return
    x = piece.x
    for ch, adv in zip(piece.text, piece.advances):
        if not ch.isspace():
            tw.append((x, piece.y), ch, font=font, fontsize=size)
        x += adv


def _draw_deco(page: pymupdf.Page, d: Deco) -> None:
    rect = pymupdf.Rect(d.x0, d.y - d.thickness / 2, d.x1, d.y + d.thickness / 2)
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(color=None, fill=_rgb(d.color), fill_opacity=d.alpha, width=0)
    shape.commit(overlay=True)


# ---------------------------------------------------------------- removal
def strip_rects(chars) -> list[pymupdf.Rect]:
    """Thin rectangles touching exactly the given characters.

    MuPDF removes every character whose box intersects a redaction, so we use
    one hairline per span, at mid x-height, from the centre of its first
    character to the centre of its last one.
    """
    by_span: dict[int, list] = defaultdict(list)
    for c in chars:
        if c.c.isspace():
            continue
        by_span[c.span_id].append(c)
    rects = []
    for span_chars in by_span.values():
        # A span can wrap visually only within one line, but keep runs of
        # characters on the same baseline together.
        rows: dict[float, list] = defaultdict(list)
        for c in span_chars:
            rows[round(c.oy, 1)].append(c)
        for oy, row in rows.items():
            xs = [(c.x0 + c.x1) / 2 for c in row]
            y = oy - 0.3 * row[0].size
            x0, x1 = min(xs), max(xs)
            if x1 - x0 < 0.2:
                x0, x1 = x0 - 0.1, x0 + 0.1
            rects.append(pymupdf.Rect(x0, y - 0.05, x1, y + 0.05))
    return rects


def decoration_rects(chars) -> list[pymupdf.Rect]:
    """Areas holding the underline / strike-through strokes of the given characters."""
    rects = []
    by_line: dict[tuple, list] = defaultdict(list)
    for c in chars:
        fam, weight, italic, size, color, alpha, underline, strike, render = c.style
        if underline or strike:
            by_line[(round(c.oy, 1), underline, strike)].append(c)
    for (oy, underline, strike), cs in by_line.items():
        x0 = min(c.x0 for c in cs) - 0.5
        x1 = max(c.x1 for c in cs) + 0.5
        size = max(c.size for c in cs)
        if underline:
            rects.append(pymupdf.Rect(x0, oy - 0.02 * size, x1, oy + 0.3 * size))
        if strike:
            rects.append(pymupdf.Rect(x0, oy - 0.5 * size, x1, oy - 0.15 * size))
    return rects


def remove_chars(page: pymupdf.Page, chars) -> None:
    """Delete the given characters from the page content, leaving everything else."""
    rects = strip_rects(chars)
    if rects:
        for r in rects:
            page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    deco = decoration_rects(chars)
    if deco:
        for r in deco:
            page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )
