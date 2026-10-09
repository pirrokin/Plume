"""High-level text operations on a page: edit, move, delete and add text blocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pymupdf

from .docfonts import DocFonts
from .extract import TEXT_FLAGS, Char, Segment, _page_segments
from .layout import Glyph, LayoutResult, OutLine, Piece, layout, normalize_style
from .writer import cover_rect, remove_chars, write_pieces

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


def _match(wanted: list[Char], found: list[Char]) -> tuple[list[Char], list[Char]]:
    """Pair characters by value and position, tolerating tiny shifts.

    Rewriting a content stream can move glyphs by a hundredth of a point, so
    exact coordinates cannot be compared. Returns (unmatched wanted, matched found).
    """
    pool: dict[tuple[str, int], list[Char]] = {}
    for c in found:
        pool.setdefault((c.c, round(c.oy)), []).append(c)
    missing: list[Char] = []
    matched: list[Char] = []
    for c in wanted:
        best = None
        for dy in (0, -1, 1):
            for cand in pool.get((c.c, round(c.oy) + dy), []):
                if abs(cand.oy - c.oy) < 0.5 and abs(cand.x0 - c.x0) < 0.6:
                    if best is None or abs(cand.x0 - c.x0) < abs(best.x0 - c.x0):
                        best = cand
        if best is None:
            missing.append(c)
        else:
            pool[(best.c, round(best.oy))].remove(best)
            matched.append(best)
    return missing, matched


def _remove_verified(page: pymupdf.Page, fonts: DocFonts, targets: list[Char]) -> int:
    """Remove `targets`, check nothing else vanished and put back collateral losses."""
    targets = [c for c in targets if not c.c.isspace()]
    if not targets:
        return 0
    before = _page_chars(page, fonts)
    _, target_instances = _match(targets, before)
    taken = {id(c) for c in target_instances}
    others = [c for c in before if id(c) not in taken]
    remove_chars(page, targets)
    fonts.invalidate()
    after = _page_chars(page, fonts)

    # Characters that should be gone but are still there (thin glyphs such as
    # "-" or ".", unusual text matrices): retry with vertical hairlines through
    # their centre, first over the line box, then over the glyph box MuPDF
    # reports in its text trace.
    def leftovers_of(chars: list[Char]) -> list[Char]:
        _, kept_others = _match(others, chars)
        used = {id(c) for c in kept_others}
        rest = [c for c in chars if id(c) not in used]
        _, still = _match(target_instances, rest)
        return still

    for attempt in range(2):
        leftovers = leftovers_of(after)
        if not leftovers:
            break
        boxes = _trace_boxes(page) if attempt else {}
        for c in leftovers:
            cx = (c.x0 + c.x1) / 2
            y0, y1 = c.top, c.bottom
            box = boxes.get((round(c.x0, 1), round(c.oy, 1)))
            if box is not None:
                y0, y1 = min(y0, box.y0), max(y1, box.y1)
            h = y1 - y0
            page.add_redact_annot(pymupdf.Rect(cx - 0.1, y0 + 0.1 * h, cx + 0.1, y1 - 0.1 * h), fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        after = _page_chars(page, fonts)
    if leftovers_of(after):
        raise EditError("Impossible d'effacer proprement le texte d'origine de ce bloc.")

    # Characters of other blocks that were removed, or moved: MuPDF does not
    # compensate character spacing (Tc) when it drops glyphs, so the rest of a
    # letter-spaced line can shift. Moved copies are deleted (repeatedly, as
    # deleting them can shift what follows) and everything is rewritten at its
    # original position.
    lost, kept = _match(others, after)
    for _ in range(3):
        if not lost:
            break
        kept_ids = {id(c) for c in kept}
        orphans = [c for c in after if id(c) not in kept_ids]
        if not orphans:
            break
        remove_chars(page, orphans)
        after = _page_chars(page, fonts)
        lost, kept = _match(others, after)
    if lost:
        log.info("rewriting %d neighbouring characters at their original position", len(lost))
        _restore(page, fonts, lost)
    return len(lost)


def _trace_boxes(page: pymupdf.Page) -> dict[tuple[float, float], pymupdf.Rect]:
    """Glyph boxes from MuPDF's text trace, by character origin."""
    boxes = {}
    try:
        for span in page.get_texttrace():
            for _u, _g, origin, bbox in span["chars"]:
                boxes[(round(origin[0], 1), round(origin[1], 1))] = pymupdf.Rect(bbox)
    except Exception:
        pass
    return boxes


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


def scan_cover(segments: list[Segment]) -> list[pymupdf.Rect]:
    """Areas of a scanned text block to paint over: each line with a small margin.

    Line by line rather than the whole block, so that a bullet or a number
    beside an indented first line is not erased.
    """
    return [pymupdf.Rect(seg.x0, seg.top, seg.x1, seg.bottom) + (-1.5, -1.0, 1.5, 1.0) for seg in segments]


def edit_block(
    doc: pymupdf.Document,
    fonts: DocFonts,
    page_no: int,
    segments: list[Segment],
    runs: list[dict],
    box: dict,
    paper: str | None = None,
) -> EditReport:
    """Replace the text of a block (given by its original segments) with `runs` laid out in `box`.

    `paper` is the background colour of a scanned block: its area is painted
    over before the new text is written.
    """
    page = doc[page_no]
    result = layout(runs, box, fonts)
    align = box.get("align") or "left"
    keep: set[int] = set()
    if paper is None:
        for i, (seg, line) in enumerate(zip(segments, result.lines)):
            if _line_unchanged(seg, line, align):
                keep.add(i)
        if len(keep) == len(segments) == len(result.lines):
            return EditReport(len(keep), 0, 0, 0, result.bbox)
    targets = [c for i, seg in enumerate(segments) if i not in keep for c in seg.chars]
    restored = _remove_verified(page, fonts, targets)
    if paper is not None:
        for rect in scan_cover(segments):
            cover_rect(page, rect, paper)
    rewrite = set(range(len(result.lines))) - keep
    pieces, decos = _pieces_for_lines(result, rewrite)
    write_pieces(page, pieces, decos)
    fonts.invalidate()
    return EditReport(len(keep), len(rewrite), len(targets), restored, result.bbox)


def delete_block(
    doc: pymupdf.Document, fonts: DocFonts, page_no: int, segments: list[Segment], paper: str | None = None
) -> EditReport:
    page = doc[page_no]
    targets = [c for seg in segments for c in seg.chars]
    restored = _remove_verified(page, fonts, targets)
    if paper is not None:
        for rect in scan_cover(segments):
            cover_rect(page, rect, paper)
    fonts.invalidate()
    return EditReport(0, 0, len(targets), restored, None)


def add_block(doc: pymupdf.Document, fonts: DocFonts, page_no: int, runs: list[dict], box: dict) -> EditReport:
    page = doc[page_no]
    result = layout(runs, box, fonts)
    write_pieces(page, result.pieces, result.decos)
    fonts.invalidate()
    return EditReport(0, len(result.lines), 0, 0, result.bbox)
