"""Paragraph layout: line breaking, alignment and glyph font selection."""

from __future__ import annotations

from dataclasses import dataclass, field

from .docfonts import ChainItem, DocFonts

STYLE_KEYS = (
    "family", "weight", "italic", "size", "color", "alpha", "underline", "strike", "rise", "spacing", "render", "outline",
)
DEFAULT_STYLE = {
    "family": "arial", "weight": 400, "italic": False, "size": 12.0, "color": "#000000", "alpha": 1.0,
    "underline": False, "strike": False, "rise": 0.0, "spacing": 0.0, "render": "fill", "outline": 0.0,
}
EPS = 0.05

UNDERLINE_OFFSET = 0.11
UNDERLINE_THICKNESS = 0.06
STRIKE_OFFSET = 0.3


def normalize_style(run: dict) -> dict:
    style = {k: run.get(k, DEFAULT_STYLE[k]) for k in STYLE_KEYS}
    style["weight"] = int(style["weight"] or 400)
    style["italic"] = bool(style["italic"])
    style["size"] = max(1.0, min(float(style["size"]), 500.0))
    style["alpha"] = max(0.0, min(float(style["alpha"] if style["alpha"] is not None else 1.0), 1.0))
    style["rise"] = float(style["rise"] or 0.0)
    style["spacing"] = float(style["spacing"] or 0.0)
    style["outline"] = max(0.0, min(float(style["outline"] or 0.0), 0.5))  # stroke width, in em
    if style["render"] in (None, "invisible"):
        style["render"] = "fill"
    color = str(style["color"] or "#000000")
    if not color.startswith("#") or len(color) != 7:
        color = "#000000"
    style["color"] = color.lower()
    return style


def style_key(style: dict) -> tuple:
    return tuple(style[k] for k in STYLE_KEYS)


@dataclass
class Glyph:
    ch: str
    style: dict
    item: ChainItem
    adv: float  # advance including letter spacing


@dataclass
class Token:
    kind: str  # "word" | "space"
    glyphs: list[Glyph]

    @property
    def width(self) -> float:
        return sum(g.adv for g in self.glyphs)


@dataclass
class Piece:
    x: float
    y: float
    text: str
    item: ChainItem
    style: dict
    advances: list[float]
    line: int = 0


@dataclass
class Deco:
    x0: float
    x1: float
    y: float
    thickness: float
    color: str
    alpha: float
    line: int = 0


@dataclass
class OutLine:
    baseline: float
    x0: float
    x1: float
    glyphs: list[tuple[Glyph, float]] = field(default_factory=list)  # glyph and its x

    @property
    def text(self) -> str:
        return "".join(g.ch for g, _ in self.glyphs)


@dataclass
class LayoutResult:
    pieces: list[Piece]
    decos: list[Deco]
    lines: list[OutLine]
    bbox: tuple[float, float, float, float]


class _Shaper:
    def __init__(self, fonts: DocFonts):
        self.fonts = fonts
        self._chains: dict[tuple, list[ChainItem]] = {}

    def glyph(self, ch: str, style: dict) -> Glyph:
        key = (style["family"], style["weight"], style["italic"])
        chain = self._chains.get(key)
        if chain is None:
            chain = self.fonts.chain(*key)
            self._chains[key] = chain
        if ch == "\t" or (ch in "   " and not any(i.covers(ord(ch)) for i in chain)):
            ch = " "  # written as a plain space; tabs get a wider advance in _tokenize
        u = ord(ch)
        item = next((i for i in chain if i.covers(u)), chain[0])
        ch = item.char_for(ch)
        adv = item.font.glyph_advance(ord(ch)) * style["size"] + style["spacing"]
        return Glyph(ch, style, item, adv)


_NO_BREAK_BEFORE = set("»:;!?%€)]}")
_NO_BREAK_AFTER = set("«([{")


def _tokenize(chars: list[tuple[str, dict]], shaper: _Shaper) -> list[Token]:
    tokens: list[Token] = []
    for i, (ch, style) in enumerate(chars):
        g = shaper.glyph(ch, style)
        kind = "space" if ch in (" ", "\t") else "word"
        if ch == " ":
            # French typography: no line break inside « word », before ":" etc.
            prev_ch = chars[i - 1][0] if i > 0 else ""
            next_ch = chars[i + 1][0] if i + 1 < len(chars) else ""
            if prev_ch in _NO_BREAK_AFTER or next_ch in _NO_BREAK_BEFORE:
                kind = "word"
        if ch == "\t":
            g.adv *= 4
        prev = tokens[-1] if tokens else None
        if prev is not None and prev.kind == kind:
            # A hyphen inside a word is a break opportunity.
            if kind == "word" and prev.glyphs[-1].ch in "-‐" and ch.isalpha() and len(prev.glyphs) > 1:
                tokens.append(Token(kind, [g]))
                continue
            prev.glyphs.append(g)
        else:
            tokens.append(Token(kind, [g]))
    return tokens


def _break_lines(tokens: list[Token], width: float | None, first_indent: float) -> list[list[Token]]:
    lines: list[list[Token]] = []
    cur: list[Token] = []
    cur_w = 0.0

    def avail() -> float:
        return (width or 0) - (first_indent if not lines else 0.0)

    for tok in tokens:
        if tok.kind == "space" or width is None:
            cur.append(tok)
            cur_w += tok.width
            continue
        if any(t.kind == "word" for t in cur) and cur_w + tok.width > avail() + EPS:
            lines.append(cur)
            cur, cur_w = [], 0.0
        if not any(t.kind == "word" for t in cur):
            # Leading spaces after a soft break are dropped.
            if lines:
                cur, cur_w = [], 0.0
            # A word wider than the box is split between characters.
            if tok.width > avail() + EPS and len(tok.glyphs) > 1:
                piece: list[Glyph] = []
                pw = 0.0
                for g in tok.glyphs:
                    if piece and pw + g.adv > avail() + EPS:
                        lines.append(cur + [Token("word", piece)])
                        cur, cur_w = [], 0.0
                        piece, pw = [], 0.0
                    piece.append(g)
                    pw += g.adv
                tok = Token("word", piece)
        cur.append(tok)
        cur_w += tok.width
    lines.append(cur)
    return lines


def natural_width(runs: list[dict], fonts: DocFonts) -> float:
    """Width of a line of text set with the fonts Plume writes with (no wrapping)."""
    shaper = _Shaper(fonts)
    chars = [(ch, normalize_style(r)) for r in runs for ch in str(r.get("text", "")) if ch not in "\n\r­"]
    while chars and chars[-1][0].isspace():
        chars.pop()
    while chars and chars[0][0].isspace():
        chars.pop(0)
    return sum(t.width for t in _tokenize(chars, shaper))


def layout(runs: list[dict], box: dict, fonts: DocFonts) -> LayoutResult:
    """Lay out `runs` (text with styles, "\\n" separating paragraphs) in `box`.

    box: x (left edge), y (first baseline), width, wrap, align, lineHeight,
    indent (first-line indent), size (reference size for the line height).
    """
    shaper = _Shaper(fonts)
    x = float(box["x"])
    width = max(float(box.get("width") or 0), 1.0)
    wrap = bool(box.get("wrap"))
    align = box.get("align") or "left"
    indent = float(box.get("indent") or 0.0) if align in ("left", "justify") else 0.0
    styles = [normalize_style(r) for r in runs]
    ref_size = float(box.get("size") or (max((s["size"] for s in styles), default=12.0)))
    line_height = float(box.get("lineHeight") or 1.2 * ref_size)

    paragraphs: list[list[tuple[str, dict]]] = [[]]
    for run, style in zip(runs, styles):
        for ch in str(run.get("text", "")).replace("\r\n", "\n").replace("\r", "\n"):
            if ch == "\n":
                paragraphs.append([])
            elif ch == "­" or (ord(ch) < 32 and ch != "\t"):
                continue
            else:
                paragraphs[-1].append((ch, style))
    last_style = styles[-1] if styles else normalize_style({})

    out_lines: list[OutLine] = []
    baseline = float(box["y"])
    first = True
    for para in paragraphs:
        tokens = _tokenize(para, shaper)
        broken = _break_lines(tokens, width if wrap else None, indent)
        for li, line_tokens in enumerate(broken):
            while line_tokens and line_tokens[-1].kind == "space":
                line_tokens = line_tokens[:-1]
            glyphs = [g for t in line_tokens for g in t.glyphs]
            max_size = max((g.style["size"] for g in glyphs), default=(para[0][1] if para else last_style)["size"])
            if not first:
                advance = line_height * max(1.0, max_size / ref_size) if ref_size else line_height
                baseline += advance
            first = False
            line_w = sum(g.adv for g in glyphs)
            is_last = li == len(broken) - 1
            start = x + (indent if li == 0 else 0.0)
            avail = width - (indent if li == 0 else 0.0)
            extra = 0.0
            if align == "right":
                start = x + width - line_w
            elif align == "center":
                start = x + (width - line_w) / 2
            elif align == "justify" and wrap and not is_last:
                inner_spaces = sum(1 for g in glyphs if g.ch == " ")
                if inner_spaces and avail - line_w > 0:
                    extra = (avail - line_w) / inner_spaces
            ol = OutLine(baseline, start, start)
            cx = start
            for g in glyphs:
                ol.glyphs.append((g, cx))
                cx += g.adv + (extra if g.ch == " " else 0.0)
            ol.x1 = cx
            out_lines.append(ol)

    pieces, decos = _emit(out_lines)
    tops = [ol.baseline - max((g.style["size"] for g, _ in ol.glyphs), default=ref_size) for ol in out_lines]
    bottoms = [ol.baseline + 0.25 * max((g.style["size"] for g, _ in ol.glyphs), default=ref_size) for ol in out_lines]
    x0 = min([x] + [ol.x0 for ol in out_lines])
    x1 = max([x + (width if wrap else 0)] + [ol.x1 for ol in out_lines])
    bbox = (x0, min(tops) if tops else box["y"] - ref_size, x1, max(bottoms) if bottoms else box["y"])
    return LayoutResult(pieces, decos, out_lines, bbox)


def _emit(lines: list[OutLine]) -> tuple[list[Piece], list[Deco]]:
    pieces: list[Piece] = []
    decos: list[Deco] = []
    for li, ol in enumerate(lines):
        current: Piece | None = None
        for g, gx in ol.glyphs:
            y = ol.baseline - g.style["rise"]
            same = (
                current is not None and current.item is g.item and current.style is g.style
                and abs(current.x + sum(current.advances) - gx) < 1e-6
            )
            if same:
                current.text += g.ch
                current.advances.append(g.adv)
            else:
                current = Piece(gx, y, g.ch, g.item, g.style, [g.adv], li)
                pieces.append(current)
        # Underline / strike-through segments.
        for attr, offset in (("underline", UNDERLINE_OFFSET), ("strike", -STRIKE_OFFSET)):
            seg: Deco | None = None
            for i, (g, gx) in enumerate(ol.glyphs):
                on = g.style[attr]
                if on:
                    y = ol.baseline - g.style["rise"] + offset * g.style["size"]
                    th = max(0.4, UNDERLINE_THICKNESS * g.style["size"])
                    end = gx + g.adv
                    if seg is not None and seg.color == g.style["color"] and abs(seg.y - y) < 0.5:
                        seg.x1 = end
                        seg.thickness = max(seg.thickness, th)
                    else:
                        seg = Deco(gx, end, y, th, g.style["color"], g.style["alpha"], li)
                        decos.append(seg)
                else:
                    seg = None
    # Pieces made only of spaces draw nothing.
    pieces = [p for p in pieces if p.text.strip()]
    return pieces, decos
