"""High-level text operations on a page: edit, move, delete and add text blocks."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

import pymupdf

from .docfonts import DocFonts
from .extract import TEXT_FLAGS, Char, Segment, _page_segments
from .layout import Glyph, LayoutResult, OutLine, Piece, layout, normalize_style
from .writer import remove_chars, write_pieces

log = logging.getLogger(__name__)


class EditError(Exception):
    pass


@dataclass
class EditReport:
    kept_lines: int
    rewritten_lines: int
    removed_chars: int
    restored_chars: int
    bbox: tuple[float, float, float, float] | None


def _char_key(c: Char) -> tuple:
    return (c.c, round(c.x0, 1), round(c.oy, 1))


def _page_chars(page: pymupdf.Page, fonts: DocFonts) -> list[Char]:
    segments, _ = _page_segments(page, fonts)
    return [c for s in segments for c in s.chars if not c.c.isspace()]


_COMPARE = ("family", "weight", "italic", "size", "color", "underline", "strike")


def _line_unchanged(seg: Segment, line: OutLine, align: str) -> bool:
    if seg.text.strip() != line.text.strip():
        return False
    if abs(seg.baseline - line.baseline) > 0.3:
        return False
    if abs(seg.x0 - line.x0) > 0.75:
        return False
    if align in ("justify", "right") and abs(seg.x1 - line.x1) > 1.0:
        return False
    old = [c for c in seg.chars if not c.c.isspace()]
    new = [g for g, _ in line.glyphs if not g.ch.isspace()]
    if len(old) != len(new):
        return False
    for c, g in zip(old, new):
        fam, weight, italic, size, color, alpha, underline, strike, render = c.style
        st = g.style
        if (fam, weight, italic, round(size, 1), color, underline, strike) != (
            st["family"], st["weight"], st["italic"], round(st["size"], 1), st["color"], st["underline"], st["strike"]
        ):
            return False
        if abs((seg.baseline - c.oy) - st["rise"]) > 0.5:
            return False
    return True


def _pieces_for_lines(result: LayoutResult, indices: set[int]):
    """Pieces and decorations belonging to the given output lines."""
    pieces = [p for p in result.pieces if p.line in indices]
    decos = [d for d in result.decos if d.line in indices]
    return pieces, decos


def _remove_verified(page: pymupdf.Page, fonts: DocFonts, targets: list[Char]) -> int:
    """Remove `targets`, check nothing else vanished and put back collateral losses."""
    if not targets:
        return 0
    before = _page_chars(page, fonts)
    target_keys = Counter(_char_key(c) for c in targets if not c.c.isspace())
    expected = Counter(_char_key(c) for c in before)
    expected.subtract(target_keys)
    remove_chars(page, targets)
    fonts.invalidate()
    after = _page_chars(page, fonts)
    actual = Counter(_char_key(c) for c in after)

    # Characters that should be gone but are still there: retry with taller strips.
    leftovers = [c for c in after if target_keys.get(_char_key(c), 0) > 0 and actual[_char_key(c)] > expected[_char_key(c)]]
    if leftovers:
        rects = []
        for c in leftovers:
            h = c.bottom - c.top
            rects.append(pymupdf.Rect((c.x0 + c.x1) / 2 - 0.1, c.top + 0.35 * h, (c.x0 + c.x1) / 2 + 0.1, c.bottom - 0.35 * h))
        for r in rects:
            page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        after = _page_chars(page, fonts)
        actual = Counter(_char_key(c) for c in after)
        still = [c for c in after if target_keys.get(_char_key(c), 0) > 0 and actual[_char_key(c)] > expected[_char_key(c)]]
        if still:
            raise EditError("Impossible d'effacer proprement le texte d'origine de ce bloc.")

    # Characters of other blocks removed by accident: rewrite them in place.
    missing = expected - actual
    lost = []
    pool = Counter(missing)
    for c in before:
        k = _char_key(c)
        if pool.get(k, 0) > 0 and target_keys.get(k, 0) == 0:
            lost.append(c)
            pool[k] -= 1
    if lost:
        log.warning("restoring %d characters removed as collateral", len(lost))
        _restore(page, fonts, lost)
    return len(lost)


def _restore(page: pymupdf.Page, fonts: DocFonts, chars: list[Char]) -> None:
    pieces = []
    for c in chars:
        fam, weight, italic, size, color, alpha, underline, strike, render = c.style
        style = normalize_style({
            "family": fam, "weight": weight, "italic": italic, "size": size, "color": color,
            "alpha": alpha, "render": render,
        })
        chain = fonts.chain(fam, weight, italic)
        item = next((i for i in chain if i.covers(ord(c.c))), chain[0])
        pieces.append(Piece(c.x0, c.oy, c.c, item, style, [c.x1 - c.x0]))
    write_pieces(page, pieces, [])


def edit_block(
    doc: pymupdf.Document,
    fonts: DocFonts,
    page_no: int,
    segments: list[Segment],
    runs: list[dict],
    box: dict,
) -> EditReport:
    """Replace the text of a block (given by its original segments) with `runs` laid out in `box`."""
    page = doc[page_no]
    result = layout(runs, box, fonts)
    align = box.get("align") or "left"
    keep: set[int] = set()
    for i, (seg, line) in enumerate(zip(segments, result.lines)):
        if _line_unchanged(seg, line, align):
            keep.add(i)
    if len(keep) == len(segments) == len(result.lines):
        return EditReport(len(keep), 0, 0, 0, result.bbox)
    targets = [c for i, seg in enumerate(segments) if i not in keep for c in seg.chars]
    restored = _remove_verified(page, fonts, targets)
    rewrite = set(range(len(result.lines))) - keep
    pieces, decos = _pieces_for_lines(result, rewrite)
    write_pieces(page, pieces, decos)
    fonts.invalidate()
    return EditReport(len(keep), len(rewrite), len(targets), restored, result.bbox)


def delete_block(doc: pymupdf.Document, fonts: DocFonts, page_no: int, segments: list[Segment]) -> EditReport:
    page = doc[page_no]
    targets = [c for seg in segments for c in seg.chars]
    restored = _remove_verified(page, fonts, targets)
    fonts.invalidate()
    return EditReport(0, 0, len(targets), restored, None)


def add_block(doc: pymupdf.Document, fonts: DocFonts, page_no: int, runs: list[dict], box: dict) -> EditReport:
    page = doc[page_no]
    result = layout(runs, box, fonts)
    write_pieces(page, result.pieces, result.decos)
    fonts.invalidate()
    return EditReport(0, len(result.lines), 0, 0, result.bbox)
