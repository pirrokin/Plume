"""Turn a PDF page into editable text blocks.

MuPDF gives us lines of characters. Lines sharing a baseline are joined into
rows, rows are cut at gaps that cannot be word spaces (table cells, bullets,
tab stops), and the resulting segments are regrouped into paragraphs the way a
word processor laid them out, so that a paragraph can be edited and re-wrapped
as a whole.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field

import pymupdf

from .docfonts import DocFonts

TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_CLIP
    | pymupdf.TEXT_COLLECT_STYLES
)

# MuPDF character flags.
CF_STRIKE = 1
CF_UNDERLINE = 2
CF_FILLED = 16
CF_STROKED = 32

_BULLET_CHARS = "•◦▪▫■□●○◆◇►▸▶➢➤✓✔❖-–—*·→⇒"
_BULLET_RE = re.compile(r"^(?:[" + re.escape(_BULLET_CHARS) + r"]|\d{1,3}[.)]|[a-zA-Z][.)]|[ivxIVX]{1,5}[.)])$")
_HYPHENS = ("-", "‐", "­")
_NUMERIC_RE = re.compile(r"^[\s\d.,:;/%+\-\u2013\u2014()€$£¥\u00a0\u202f]+(?:[A-Za-z]{0,3}\.?)?$")


@dataclass
class Char:
    c: str
    x0: float
    x1: float
    oy: float
    top: float
    bottom: float
    size: float
    style: tuple  # (family, weight, italic, size, color, alpha, underline, strike, render)
    font: str
    span_id: int
    synthetic: bool = False
    reliable: bool = True


@dataclass
class Segment:
    chars: list[Char]
    line_id: int
    baseline: float = 0.0
    size: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    text: str = ""
    first_word_w: float = 0.0
    word_gap: float = 0.0  # median distance between words
    space_w: float = 0.0  # median width of real space characters
    invisible: bool = False
    after_bullet: bool = False
    column_right: float = 0.0
    style_sig: tuple = ()

    def finish(self) -> "Segment":
        visible = [c for c in self.chars if not c.c.isspace()] or self.chars
        weights: dict[tuple[float, float], int] = {}
        for c in visible:
            k = (round(c.oy, 1), round(c.size, 2))
            weights[k] = weights.get(k, 0) + 1
        (self.baseline, self.size) = max(weights.items(), key=lambda kv: (kv[1], kv[0][1]))[0]
        self.x0 = min(c.x0 for c in visible)
        self.x1 = max(c.x1 for c in visible)
        self.top = min(c.top for c in visible)
        self.bottom = max(c.bottom for c in visible)
        self.text = "".join(c.c for c in self.chars)
        # Width of the first word.
        w = 0.0
        for c in self.chars:
            if c.c.isspace():
                if w:
                    break
                continue
            w += c.x1 - c.x0
        self.first_word_w = w
        spaces = [c.x1 - c.x0 for c in self.chars if c.c == " " and not c.synthetic]
        self.space_w = statistics.median(spaces) if spaces else 0.25 * self.size
        gaps = _word_gaps(self.chars)
        self.word_gap = statistics.median(gaps) if gaps else self.space_w
        self.invisible = all(c.style[8] == "invisible" for c in visible)
        counts: dict[tuple, int] = {}
        for c in visible:
            sig = (c.style[0], c.style[1] >= 600, c.style[2], c.style[6], c.style[4])
            counts[sig] = counts.get(sig, 0) + 1
        self.style_sig = max(counts.items(), key=lambda kv: kv[1])[0]
        return self

    @property
    def is_bullet(self) -> bool:
        return bool(_BULLET_RE.match(self.text.strip()))


def _word_gaps(chars: list[Char]) -> list[float]:
    """Distances between the last letter of a word and the first letter of the next one."""
    idx = [i for i, c in enumerate(chars) if not c.c.isspace()]
    gaps = []
    for a, b in zip(idx, idx[1:]):
        g = chars[b].x0 - chars[a].x1
        if b - a > 1 or g > 0.25 * chars[a].size:
            gaps.append(g)
    return gaps


@dataclass
class Para:
    lines: list[Segment] = field(default_factory=list)

    @property
    def last(self) -> Segment:
        return self.lines[-1]

    @property
    def spacing(self) -> float | None:
        if len(self.lines) < 2:
            return None
        return statistics.median(b.baseline - a.baseline for a, b in zip(self.lines, self.lines[1:]))

    @property
    def body_x0(self) -> float:
        if len(self.lines) >= 2:
            return min(l.x0 for l in self.lines[1:])
        return self.lines[0].x0

    @property
    def max_x1(self) -> float:
        return max(l.x1 for l in self.lines)

    @property
    def max_width(self) -> float:
        return max(l.x1 - l.x0 for l in self.lines)


def _hex(color: int) -> str:
    return "#%06x" % (color & 0xFFFFFF)


def _style_render(char_flags: int) -> str:
    filled = bool(char_flags & CF_FILLED)
    stroked = bool(char_flags & CF_STROKED)
    if filled and stroked:
        return "fillstroke"
    if stroked:
        return "stroke"
    if filled:
        return "fill"
    return "invisible"


# ---------------------------------------------------------------- segments
def _dominant_baseline(chars: list[Char]) -> tuple[float, float]:
    counts: dict[tuple[float, float], int] = {}
    for c in chars:
        if not c.c.isspace():
            k = (round(c.oy, 1), c.size)
            counts[k] = counts.get(k, 0) + 1
    if not counts:
        return chars[0].oy, chars[0].size
    return max(counts.items(), key=lambda kv: (kv[1], kv[0][1]))[0]


def _page_segments(page: pymupdf.Page, fonts: DocFonts) -> tuple[list[Segment], list[dict]]:
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    lines: list[tuple[float, float, list[Char]]] = []
    others: list[dict] = []
    span_id = 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            dx, dy = line["dir"]
            horizontal = abs(dy) < 1e-3 and dx > 0 and line.get("wmode", 0) == 0
            chars: list[Char] = []
            for span in line["spans"]:
                span_id += 1
                fam = fonts.family_for_span(span["font"], span["flags"])
                reliable = fonts.name_reliable(span["font"])
                cf = span.get("char_flags", CF_FILLED)
                size = round(span["size"], 2)
                style = (
                    fam.key, fam.weight, fam.italic, size, _hex(span["color"]),
                    round(span.get("alpha", 255) / 255.0, 3),
                    bool(cf & CF_UNDERLINE), bool(cf & CF_STRIKE), _style_render(cf),
                )
                asc = span.get("ascender", 0.9)
                desc = span.get("descender", -0.2)
                for ch in span["chars"]:
                    x0, _, x1, _ = ch["bbox"]
                    cy = ch["origin"][1]
                    chars.append(Char(
                        c=ch["c"], x0=x0, x1=x1, oy=cy,
                        top=cy - asc * size, bottom=cy - desc * size,
                        size=size, style=style, font=span["font"], span_id=span_id,
                        synthetic=bool(ch.get("synthetic")), reliable=reliable,
                    ))
            if not chars or all(c.c.isspace() for c in chars):
                continue
            if not horizontal:
                others.append({
                    "bbox": list(line["bbox"]),
                    "text": "".join(c.c for c in chars).strip(),
                    "reason": "Texte orienté : sa modification n'est pas encore prise en charge.",
                })
                continue
            base, size = _dominant_baseline(chars)
            lines.append((base, size, chars))

    # Join MuPDF lines lying on the same baseline (MuPDF splits widely spaced
    # justified lines into several lines).
    lines.sort(key=lambda t: (round(t[0], 1), min(c.x0 for c in t[2])))
    rows: list[list] = []  # [baseline, size, chars, x1]
    for base, size, chars in lines:
        x0 = min(c.x0 for c in chars if not c.c.isspace())
        x1 = max(c.x1 for c in chars if not c.c.isspace())
        target = None
        for row in reversed(rows[-12:]):
            if abs(row[0] - base) <= 0.15 * max(size, row[1]) and abs(row[1] - size) <= 0.25 * max(size, row[1]) and row[3] <= x0 + 0.5:
                target = row
                break
        if target is not None:
            target[2].extend(chars)
            target[3] = max(target[3], x1)
        else:
            rows.append([base, size, list(chars), x1])

    segments: list[Segment] = []
    for row_id, (_, _, chars, _) in enumerate(rows):
        parts = _split_row(chars)
        for i, part in enumerate(parts):
            seg = Segment(part, row_id).finish()
            if i > 0 and segments and segments[-1].is_bullet and segments[-1].line_id == row_id:
                seg.after_bullet = True
            segments.append(seg)
    _column_rights(segments)
    return segments, others


def _split_row(chars: list[Char]) -> list[list[Char]]:
    """Split a row of characters at gaps that cannot be ordinary word spaces."""
    idx = [i for i, c in enumerate(chars) if not c.c.isspace()]
    pairs = []
    for a, b in zip(idx, idx[1:]):
        gap = chars[b].x0 - chars[a].x1
        between = chars[a + 1:b]
        # Gaps holding a (real or synthesised) space are word gaps; a wide gap
        # without any space only occurs between separate text runs.
        is_word_gap = bool(between)
        pairs.append((a, b, gap, between, is_word_gap))
    word_gaps = [p[2] for p in pairs if p[4]]
    uniform = len(word_gaps) >= 3 and min(word_gaps) > 0 and max(word_gaps) <= 1.3 * min(word_gaps)
    smallest = min(word_gaps) if len(word_gaps) >= 2 else None

    parts: list[list[Char]] = []
    start = 0
    for a, b, gap, between, is_word_gap in pairs:
        ca, cb = chars[a], chars[b]
        size = max(ca.size, cb.size)
        split = False
        if gap < -0.5 * size:
            split = True  # text going backwards: separate run printed over
        elif not is_word_gap:
            split = gap > 0.5 * size
        elif any(c.c == "\t" for c in between) and gap > 1.0 * size:
            split = True
        else:
            if uniform:
                split = gap > 8 * size
            elif smallest is not None and gap > 2.5 * max(smallest, 0.15 * size) and gap > 0.8 * size:
                split = True
            elif gap > 3.0 * size:
                split = True
        if not split and gap > 0.4 * size:
            head = "".join(c.c for c in chars[start:a + 1]).strip()
            split = bool(_BULLET_RE.match(head))
        if split:
            parts.append(chars[start:a + 1])
            start = b
    parts.append(chars[start:])
    out = []
    for p in parts:
        while p and p[0].c.isspace():
            p = p[1:]
        while p and p[-1].c.isspace():
            p = p[:-1]
        if p:
            out.append(p)
    return out


def _column_rights(segments: list[Segment]) -> None:
    """Estimate, for each segment, the right edge of the text column it belongs to."""
    for seg in segments:
        tol = max(1.5, 0.5 * seg.size)
        right = seg.x1
        for other in segments:
            if abs(other.x0 - seg.x0) <= tol and abs(other.baseline - seg.baseline) <= 25 * seg.size:
                if abs(other.size - seg.size) <= 0.25 * seg.size:
                    right = max(right, other.x1)
        seg.column_right = right


# ---------------------------------------------------------------- paragraphs
def _compatible(p: Para, seg: Segment) -> float | None:
    """Score (lower is better) for appending `seg` to `p`, or None."""
    last = p.last
    s = max(last.size, seg.size)
    if abs(last.size - seg.size) > 0.2 * s:
        return None
    if last.invisible != seg.invisible:
        return None
    gap = seg.baseline - last.baseline
    if gap < 0.7 * s or gap > 1.9 * s:
        return None
    spacing = p.spacing
    if spacing is not None and abs(gap - spacing) > 0.2 * s:
        return None
    if seg.x0 >= last.x1 or seg.x1 <= last.x0:
        return None
    if seg.is_bullet or seg.after_bullet or (last.is_bullet and len(p.lines) == 1):
        return None
    if _NUMERIC_RE.match(seg.text) and _NUMERIC_RE.match(last.text):
        return None  # stacked amounts / dates are table cells, not wrapped prose
    if " " not in seg.text.strip() and " " not in last.text.strip() and max(seg.x1, last.x1) - min(seg.x0, last.x0) < 6 * s:
        return None  # stacked one-word labels (plans, diagrams) are separate texts
    first_token = seg.text.split(" ", 1)[0]
    if len(first_token) <= 4 and _BULLET_RE.match(first_token):
        return None
    if seg.style_sig != last.style_sig and (seg.x1 - seg.x0) < 0.6 * (last.x1 - last.x0):
        # A short line in another style (heading, label) right below a paragraph.
        return None
    tol = max(1.5, 0.5 * s)
    left = abs(seg.x0 - p.body_x0) <= tol
    indent = len(p.lines) == 1 and 0 < last.x0 - seg.x0 <= 4 * s and seg.x1 >= last.x1 - tol
    right = abs(seg.x1 - last.x1) <= tol
    center = abs((seg.x0 + seg.x1) / 2 - (last.x0 + last.x1) / 2) <= tol
    space = max(last.space_w, 0.2 * s)
    if left or indent:
        right_edge = max(p.max_x1, seg.x1, seg.column_right if left else 0)
        if last.x1 + space + seg.first_word_w <= right_edge - 0.5:
            return None  # the next word would have fitted: `last` ended a paragraph
        return gap
    if right or center:
        width = max(p.max_width, seg.x1 - seg.x0)
        if (last.x1 - last.x0) + space + seg.first_word_w <= width - 0.5:
            return None
        return gap + s  # prefer left-aligned matches
    return None


def _group(segments: list[Segment]) -> list[Para]:
    order = sorted(segments, key=lambda sg: (round(sg.baseline, 1), sg.x0))
    paras: list[Para] = []
    active: list[Para] = []
    for seg in order:
        active = [p for p in active if seg.baseline - p.last.baseline <= 2.0 * max(p.last.size, seg.size)]
        best = None
        best_score = None
        for p in active:
            if p.last.baseline >= seg.baseline - 0.1:
                continue
            score = _compatible(p, seg)
            if score is not None and (best_score is None or score < best_score):
                best, best_score = p, score
        if best is None:
            best = Para([seg])
            paras.append(best)
            active.append(best)
        else:
            best.lines.append(seg)
    return paras


# ---------------------------------------------------------------- block model
def _runs_of(chars: list[Char], baseline: float) -> list[dict]:
    runs: list[dict] = []
    current = None
    for c in chars:
        rise = round(baseline - c.oy, 1)
        if abs(rise) < 0.3:
            rise = 0.0
        key = (c.style, rise)
        if current is not None and current["_key"] == key:
            current["text"] += c.c
            current["_chars"].append(c)
            continue
        fam, weight, italic, size, color, alpha, underline, strike, render = c.style
        current = {
            "_key": key, "_chars": [c],
            "text": c.c, "family": fam, "weight": weight, "italic": italic, "size": size,
            "color": color, "alpha": alpha, "underline": underline, "strike": strike,
            "rise": rise, "spacing": 0.0, "render": render,
        }
        runs.append(current)
    for r in runs:
        r["spacing"] = _char_spacing(r["_chars"])
    return runs


def _char_spacing(chars: list[Char]) -> float:
    deltas = []
    for a, b in zip(chars, chars[1:]):
        if a.c.isspace() or b.c.isspace() or a.span_id != b.span_id:
            continue
        deltas.append(b.x0 - a.x1)
    if len(deltas) < 2:
        return 0.0
    m = statistics.median(deltas)
    return round(m, 3) if abs(m) > 0.02 * chars[0].size else 0.0


def _clean_runs(runs: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in runs if r["text"]]


_STYLE_KEYS = ("family", "weight", "italic", "size", "color", "alpha", "underline", "strike", "rise", "spacing", "render")


def _same_style(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in _STYLE_KEYS)


def _paragraph_runs(lines: list[dict], wrap: bool) -> list[dict]:
    """Content of a block as a single run list; soft line breaks become spaces."""
    out: list[dict] = []
    for i, line in enumerate(lines):
        runs = [dict(r) for r in line["runs"]]
        if i > 0 and out:
            prev = out[-1]
            if prev["text"].endswith("­"):
                prev["text"] = prev["text"][:-1]
            elif prev["text"].endswith(_HYPHENS) and runs and runs[0]["text"][:1].islower():
                pass
            else:
                prev["text"] += " " if wrap else "\n"
        for r in runs:
            if out and _same_style(out[-1], r):
                out[-1]["text"] += r["text"]
            else:
                out.append(r)
    return out


def _alignment(p: Para) -> tuple[str, float]:
    lines = p.lines
    s = max(l.size for l in lines)
    tol = max(1.5, 0.4 * s)
    if len(lines) == 1:
        return "left", 0.0
    left = min(l.x0 for l in lines)
    right = max(l.x1 for l in lines)
    indent = 0.0
    first, rest = lines[0], lines[1:]
    body_left = min(l.x0 for l in rest)
    if first.x0 - body_left > tol and all(abs(l.x0 - body_left) <= tol for l in rest):
        indent = first.x0 - body_left
        left = body_left
    lefts = all(abs(l.x0 - left) <= tol for l in rest) and (indent > 0 or abs(first.x0 - left) <= tol)
    body = lines[:-1]
    if lefts and all(abs(l.x1 - right) <= tol for l in body):
        if len(body) >= 2:
            return "justify", indent
        has_real_spaces = any(c.c == " " and not c.synthetic for c in body[0].chars)
        natural = statistics.median(l.space_w for l in lines)
        if has_real_spaces:
            stretched = body[0].word_gap > natural * 1.03 + 0.05
        else:
            stretched = body[0].word_gap > 0.3 * s
        last_full = abs(lines[-1].x1 - right) <= tol
        if stretched or not last_full:
            return ("justify" if stretched else "left"), indent
        return "left", indent
    if lefts:
        return "left", indent
    if all(abs(l.x1 - right) <= tol for l in lines):
        return "right", 0.0
    centers = [(l.x0 + l.x1) / 2 for l in lines]
    if max(centers) - min(centers) <= 2 * tol:
        return "center", 0.0
    return "left", indent


def _block_issues(lines: list[Segment], fonts: DocFonts) -> tuple[list[str], bool]:
    """Warnings about a block, and whether it can be edited safely."""
    issues = []
    editable = True
    chars = [c for l in lines for c in l.chars]
    if all(l.invisible for l in lines):
        issues.append("Texte numérisé (OCR) : le passage modifié sera réécrit dans une police standard sur un fond uni.")
        return issues, True
    if any(c.c == "\ufffd" for c in chars) or any(not c.reliable for c in chars if not c.c.isspace()):
        issues.append("Une partie du texte est encodée sans correspondance Unicode : il ne peut pas être modifié sans risque.")
        editable = False
    families = {c.style[0] for c in chars}
    substitutes = []
    for f in sorted(families):
        fam = fonts.family(f)
        if not fam.has_real_faces():
            substitutes.append(f"{fam.name} → police générique")
        elif fam.substitute:
            substitutes.append(f"{fam.name} → {fam.substitute} pour les lettres absentes")
    if substitutes:
        issues.append("Police d'origine incomplète ou indisponible : " + "; ".join(substitutes) + ".")
    return issues, editable


def _signature(lines: list[Segment]) -> str:
    h = hashlib.sha1()
    for l in lines:
        h.update(f"{l.text}|{l.x0:.1f}|{l.baseline:.1f};".encode())
    return h.hexdigest()[:12]


def extract_page(page: pymupdf.Page, fonts: DocFonts) -> dict:
    """Page model: text blocks in reading order, plus internal data for editing."""
    segments, others = _page_segments(page, fonts)
    paras = _group(segments)
    paras.sort(key=lambda p: (round(p.lines[0].top / 4), p.lines[0].x0))
    blocks = []
    internal = {}
    for i, p in enumerate(paras):
        bid = f"b{i}"
        lines_model = []
        for l in p.lines:
            runs = _runs_of(l.chars, l.baseline)
            lines_model.append({
                "bbox": [l.x0, l.top, l.x1, l.bottom],
                "baseline": l.baseline,
                "text": l.text,
                "runs": _clean_runs(runs),
            })
        align, indent = _alignment(p)
        wrap = len(p.lines) > 1
        x0 = min(l.x0 for l in p.lines) if indent == 0 else p.body_x0
        x1 = max(l.x1 for l in p.lines)
        line_height = p.spacing if p.spacing else round(1.2 * max(l.size for l in p.lines), 2)
        issues, editable = _block_issues(p.lines, fonts)
        scan = all(l.invisible for l in p.lines)
        block = {
            "id": bid,
            "sig": _signature(p.lines),
            "bbox": [x0, min(l.top for l in p.lines), x1, max(l.bottom for l in p.lines)],
            "x": x0,
            "width": max(x1 - x0, 1.0),
            "baseline": p.lines[0].baseline,
            "lineHeight": round(line_height, 3),
            "align": align,
            "indent": round(indent, 2),
            "wrap": wrap,
            "size": p.lines[0].size,
            "lines": lines_model,
            "runs": _clean_runs(_paragraph_runs(lines_model, wrap)),
            "editable": editable,
            "issues": issues,
            "scan": scan,
        }
        if scan:
            _scan_style(page, block, fonts)
        blocks.append(block)
        internal[bid] = p.lines
    _infer_single_line_alignment(blocks)
    return {"blocks": blocks, "others": others, "internal": internal}


SCAN_FAMILY = "helvetica"
# Fonts tried for rewriting scanned text; the one whose widths match the scan wins.
SCAN_CANDIDATES = ["helvetica", "arialnarrow", "timesnewroman", "georgia", "verdana", "couriernew"]


def _scan_family(block: dict, fonts: DocFonts) -> str:
    """Standard font whose letter widths best fit the scanned lines."""
    import math

    lines = [l for l in block["lines"] if l["text"].strip()]
    ocr_width = sum(l["bbox"][2] - l["bbox"][0] for l in lines)
    if ocr_width <= 0:
        return SCAN_FAMILY
    best, best_err = SCAN_FAMILY, None
    for key in SCAN_CANDIDATES:
        if fonts.index.resolve_family_key(key) is None and key != SCAN_FAMILY:
            continue
        chain = fonts.chain(key, 400, False)
        font = chain[0].font
        natural = sum(font.text_length(l["text"].strip(), fontsize=block["size"]) for l in lines)
        if natural <= 0:
            continue
        err = abs(math.log(ocr_width / natural))
        if key != SCAN_FAMILY:
            err += 0.03  # prefer the plain default unless another font fits clearly better
        if best_err is None or err < best_err:
            best, best_err = key, err
    return best


def _scan_style(page: pymupdf.Page, block: dict, fonts: DocFonts) -> None:
    """Scanned text: rewrite it in a standard font, in the ink colour, over the paper colour."""
    from .ocr import sample_colors

    bg, ink = sample_colors(page, pymupdf.Rect(block["bbox"]))
    block["bg"] = bg
    family = _scan_family(block, fonts)
    for runs in [block["runs"]] + [l["runs"] for l in block["lines"]]:
        for r in runs:
            r["color"] = ink
            r["render"] = "fill"
            r["alpha"] = 1.0
            r["spacing"] = 0.0
            r["family"] = family


def _infer_single_line_alignment(blocks: list[dict]) -> None:
    """Single lines take the alignment they share with their neighbours (table columns)."""
    for b in blocks:
        if b["wrap"] or not b["editable"]:
            continue
        text = "".join(r["text"] for r in b["runs"]).strip()
        if len(text) <= 2:
            continue
        x0, x1 = b["bbox"][0], b["bbox"][2]
        cx = (x0 + x1) / 2
        votes = {"left": 0, "right": 0, "center": 0}
        tol = max(1.0, 0.15 * b["size"])
        for o in blocks:
            if o is b or abs(o["baseline"] - b["baseline"]) > 12 * b["size"]:
                continue
            if abs(o["baseline"] - b["baseline"]) < 0.5 * b["size"]:
                continue  # same row: not a column neighbour
            if abs(o["size"] - b["size"]) > 0.25 * b["size"]:
                continue
            ox0, ox1 = o["bbox"][0], o["bbox"][2]
            same_left = abs(ox0 - x0) <= tol
            same_right = abs(ox1 - x1) <= tol
            same_center = abs((ox0 + ox1) / 2 - cx) <= tol
            if same_left and same_right:
                continue  # same width: says nothing about the alignment
            if same_left:
                votes["left"] += 1
            elif same_right:
                votes["right"] += 1
            elif same_center:
                votes["center"] += 1
        if votes["right"] >= 2 and votes["right"] > votes["left"]:
            b["align"] = "right"
        elif votes["center"] >= 2 and votes["center"] > votes["left"] + votes["right"]:
            b["align"] = "center"
        elif votes["right"] == 1 and votes["left"] == 0 and votes["center"] == 0:
            b["align"] = "right"
        elif votes["center"] == 1 and votes["left"] == 0 and votes["right"] == 0:
            b["align"] = "center"
