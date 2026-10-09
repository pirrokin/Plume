"""Minimal PDF content stream reader, enough to find and move image drawings.

Content streams are sequences of operands followed by an operator
("q 1 0 0 1 72 600 cm /Im1 Do Q"). We tokenise them, follow the graphics
state (q / Q / cm) and record where every image is drawn, with the current
transformation matrix at that point and the byte range of the drawing
operation, so that it can be wrapped in a new transformation or removed
without touching anything else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


@dataclass
class Op:
    operator: str
    operands: list
    start: int  # offset of the first operand (or of the operator)
    end: int  # offset just after the operator (after EI for inline images)


class ContentError(Exception):
    pass


def _skip_ws(data: bytes, i: int) -> int:
    n = len(data)
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
        else:
            break
    return i


def _read_string(data: bytes, i: int) -> tuple[bytes, int]:
    depth = 0
    j = i
    n = len(data)
    while j < n:
        c = data[j]
        if c == 0x5C:  # backslash escape
            j += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return data[i:j + 1], j + 1
        j += 1
    raise ContentError("unterminated string")


def _read_token(data: bytes, i: int):
    """Return (value, kind, next_index). Kinds: num, name, str, hex, op, [ ] << >>."""
    c = data[i]
    if c == 0x28:  # (
        s, j = _read_string(data, i)
        return s, "str", j
    if c == 0x3C:  # <
        if data[i + 1:i + 2] == b"<":
            return b"<<", "<<", i + 2
        j = data.index(b">", i)
        return data[i:j + 1], "hex", j + 1
    if c == 0x3E and data[i + 1:i + 2] == b">":
        return b">>", ">>", i + 2
    if c in b"[]{}":
        return bytes([c]), chr(c), i + 1
    if c == 0x2F:  # /name
        j = i + 1
        while j < len(data) and data[j] not in _WS and data[j] not in _DELIM:
            j += 1
        return data[i:j], "name", j
    j = i
    while j < len(data) and data[j] not in _WS and data[j] not in _DELIM:
        j += 1
    if j == i:
        raise ContentError(f"unexpected byte {c!r} at {i}")
    word = data[i:j]
    if _NUMBER_RE.fullmatch(word):
        return float(word), "num", j
    return word, "op", j


def _inline_image_end(data: bytes, i: int) -> int:
    """Offset just after the EI closing an inline image whose data starts at i."""
    j = i
    while True:
        k = data.find(b"EI", j)
        if k < 0:
            raise ContentError("unterminated inline image")
        before = data[k - 1:k] if k > 0 else b" "
        after = data[k + 2:k + 3]
        if before in (b" ", b"\n", b"\r", b"\t") and (after == b"" or after in _WS or after in _DELIM):
            return k + 2
        j = k + 2


def parse_ops(data: bytes) -> list[Op]:
    ops: list[Op] = []
    operands: list = []
    start = None
    stack: list[list] = []
    i = _skip_ws(data, 0)
    n = len(data)
    while i < n:
        value, kind, j = _read_token(data, i)
        if start is None:
            start = i
        if kind in ("[", "<<"):
            stack.append([])
        elif kind in ("]", ">>"):
            if not stack:
                raise ContentError("unbalanced array or dictionary")
            done = stack.pop()
            (stack[-1] if stack else operands).append(done)
        elif kind == "op":
            if stack:
                stack[-1].append(value)  # e.g. "true" inside an array
            else:
                op = value.decode("latin-1")
                if op == "BI":
                    k = data.find(b"ID", j)
                    if k < 0:
                        raise ContentError("inline image without ID")
                    end = _inline_image_end(data, k + 3)
                    ops.append(Op("BI", operands, start, end))
                    j = end
                else:
                    ops.append(Op(op, operands, start, j))
                operands = []
                start = None
        else:
            (stack[-1] if stack else operands).append((kind, value))
        i = _skip_ws(data, j)
    return ops


# ---------------------------------------------------------------- graphics state
@dataclass
class Drawing:
    """An image drawn on the page, directly or through a form XObject."""

    kind: str  # "image" (XObject), "inline" or "form" (form holding only images)
    name: str | None
    xref: int
    ctm: pymupdf.Matrix  # maps the unit square (image) or form space to PDF user space
    start: int  # byte range to transform: the drawing group (q … Q) or the operation alone
    end: int
    bbox_pdf: pymupdf.Rect = field(default_factory=pymupdf.Rect)  # visible area, PDF user space
    stream: int = 0  # 0: the page content; otherwise the xref of the form holding the drawing
    anchor: pymupdf.Matrix | None = None  # CTM where the range starts (defaults to `ctm`)
    bounds_pdf: pymupdf.Rect | None = None  # clip outside the range, which still applies once moved


def _num(operand) -> float:
    kind, value = operand
    if kind != "num":
        raise ContentError("number expected")
    return value


def _xobjects(doc: pymupdf.Document, resources: str | None) -> dict[str, int]:
    """Map XObject resource names to xrefs from a /Resources dictionary source."""
    if not resources:
        return {}
    m = re.search(r"/XObject\s*(<<.*?>>|\d+\s+0\s+R)", resources, re.S)
    if not m:
        return {}
    body = m.group(1)
    ref = re.fullmatch(r"(\d+)\s+0\s+R", body.strip())
    if ref:
        body = doc.xref_object(int(ref.group(1)), compressed=True)
    return {name: int(x) for name, x in re.findall(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R", body)}


def _resources_of(doc: pymupdf.Document, xref: int) -> str | None:
    typ, val = doc.xref_get_key(xref, "Resources")
    if typ == "xref":
        return doc.xref_object(int(val.split()[0]), compressed=True)
    if typ == "dict":
        return val
    return None


def _form_is_image_only(doc: pymupdf.Document, xref: int, depth: int = 0) -> bool:
    """A form that only draws images (with clipping): it can be moved as one image."""
    if depth > 3:
        return False
    try:
        ops = parse_ops(doc.xref_stream(xref) or b"")
    except Exception:
        return False
    xobjs = _xobjects(doc, _resources_of(doc, xref))
    painting = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "sh", "BT", "Tj", "TJ", "'", '"'}
    drew = False
    for op in ops:
        if op.operator in painting:
            return False
        if op.operator == "BI":
            drew = True
        elif op.operator == "Do" and op.operands:
            name = op.operands[-1][1].decode("latin-1").lstrip("/")
            sub = xobjs.get(name)
            if sub is None:
                return False
            subtype = doc.xref_get_key(sub, "Subtype")[1]
            if subtype == "/Image":
                drew = True
            elif subtype == "/Form" and _form_is_image_only(doc, sub, depth + 1):
                drew = True
            else:
                return False
    return drew


def _unit_bbox(m: pymupdf.Matrix) -> pymupdf.Rect:
    return pymupdf.Rect(0, 0, 1, 1).transform(m)


def _form_bbox(doc: pymupdf.Document, xref: int, ctm: pymupdf.Matrix) -> pymupdf.Rect:
    typ, val = doc.xref_get_key(xref, "BBox")
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", val)] if typ == "array" else []
    box = pymupdf.Rect(nums) if len(nums) == 4 else pymupdf.Rect(0, 0, 1, 1)
    fm = doc.xref_get_key(xref, "Matrix")
    mnums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", fm[1])] if fm[0] == "array" else []
    matrix = pymupdf.Matrix(*mnums) if len(mnums) == 6 else pymupdf.Matrix(1, 0, 0, 1, 0, 0)
    return box.transform(matrix * ctm)


def _form_matrix(doc: pymupdf.Document, xref: int) -> pymupdf.Matrix:
    typ, val = doc.xref_get_key(xref, "Matrix")
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", val)] if typ == "array" else []
    return pymupdf.Matrix(*nums) if len(nums) == 6 else pymupdf.Matrix(1, 0, 0, 1, 0, 0)


# Operators allowed in a drawing group besides the image itself: graphics
# state, clipping paths and marked content. Anything that paints makes the
# group belong to more than the image.
_GROUP_OPS = {
    "q", "Q", "cm", "re", "m", "l", "c", "v", "y", "h", "W", "W*", "n", "gs", "w", "J", "j", "M", "d",
    "ri", "i", "BDC", "BMC", "EMC", "MP", "DP", "BX", "EX",
}
_PATH_OPS = {"re", "m", "l", "c", "v", "y", "h"}


@dataclass
class _Block:
    start: int
    ctm: pymupdf.Matrix
    clip: pymupdf.Rect | None
    clean: bool = True
    images: int = 0
    drawings: list = field(default_factory=list)


def _points(op: Op) -> list[tuple[float, float]]:
    nums = [v for k, v in op.operands if k == "num"] if all(isinstance(x, tuple) for x in op.operands) else []
    if op.operator == "re" and len(nums) == 4:
        x, y, w, h = nums
        return [(x, y), (x + w, y + h)]
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _scan(doc: pymupdf.Document, data: bytes, resources: str | None, base: pymupdf.Matrix, stream: int,
          found: list[Drawing], depth: int, seen: set[int]) -> None:
    ops = parse_ops(data)
    xobjs = _xobjects(doc, resources)
    ctm = pymupdf.Matrix(base)
    clip: pymupdf.Rect | None = None
    path: pymupdf.Rect | None = None
    pending_clip = False
    state: list[tuple[pymupdf.Matrix, pymupdf.Rect | None]] = []
    blocks: list[_Block] = []

    def draw(d: Drawing) -> None:
        d.anchor = pymupdf.Matrix(ctm)
        d.bounds_pdf = pymupdf.Rect(clip) if clip is not None else None
        if clip is not None:
            visible = d.bbox_pdf & clip
            if not visible.is_empty:
                d.bbox_pdf = visible
        for b in blocks:
            b.images += 1
        if blocks:
            blocks[-1].drawings.append(d)
        found.append(d)

    for op in ops:
        o = op.operator
        if o not in _GROUP_OPS and o not in ("Do", "BI"):
            for b in blocks:
                b.clean = False
        if o == "q":
            state.append((pymupdf.Matrix(ctm), pymupdf.Rect(clip) if clip is not None else None))
            blocks.append(_Block(op.start, pymupdf.Matrix(ctm), pymupdf.Rect(clip) if clip is not None else None))
        elif o == "Q":
            if state:
                ctm, clip = state.pop()
            if blocks:
                b = blocks.pop()
                # The innermost group drawing exactly one image (with its own clip,
                # as Word and LibreOffice do) moves as a whole, clip included.
                if b.clean and b.images == 1 and len(b.drawings) == 1:
                    d = b.drawings[0]
                    d.start, d.end = b.start, op.end
                    d.anchor = b.ctm
                    d.bounds_pdf = b.clip
                elif blocks:
                    blocks[-1].drawings.extend(b.drawings)
        elif o == "cm" and len(op.operands) == 6:
            ctm = pymupdf.Matrix(*[_num(x) for x in op.operands]) * ctm
        elif o in _PATH_OPS:
            for x, y in _points(op):
                p = pymupdf.Point(x, y) * ctm
                path = pymupdf.Rect(p, p) if path is None else path | p
        elif o in ("W", "W*"):
            pending_clip = True
        elif o in ("n", "f", "F", "f*", "S", "s", "B", "B*", "b", "b*"):
            if pending_clip and path is not None:
                clip = pymupdf.Rect(path) if clip is None else clip & path
            pending_clip = False
            path = None
        elif o == "BI":
            draw(Drawing("inline", None, 0, pymupdf.Matrix(ctm), op.start, op.end, _unit_bbox(ctm), stream))
        elif o == "Do" and op.operands and op.operands[-1][0] == "name":
            name = op.operands[-1][1].decode("latin-1").lstrip("/")
            xref = xobjs.get(name)
            if not xref:
                for b in blocks:
                    b.clean = False
                continue
            subtype = doc.xref_get_key(xref, "Subtype")[1]
            if subtype == "/Image":
                draw(Drawing("image", name, xref, pymupdf.Matrix(ctm), op.start, op.end, _unit_bbox(ctm), stream))
            elif subtype == "/Form" and _form_is_image_only(doc, xref):
                draw(Drawing("form", name, xref, pymupdf.Matrix(ctm), op.start, op.end,
                             _form_bbox(doc, xref, ctm), stream))
            else:
                for b in blocks:
                    b.clean = False
                if subtype == "/Form" and depth < 4 and xref not in seen:
                    # A group holding other things too (Canva puts the whole page in one):
                    # look for the images inside it.
                    seen.add(xref)
                    try:
                        inner = doc.xref_stream(xref) or b""
                        _scan(doc, inner, _resources_of(doc, xref), _form_matrix(doc, xref) * ctm, xref,
                              found, depth + 1, seen)
                    except ContentError:
                        pass


def find_drawings(doc: pymupdf.Document, page: pymupdf.Page, data: bytes) -> list[Drawing]:
    """Images drawn on a page (`data` is its content stream), in drawing order."""
    found: list[Drawing] = []
    _scan(doc, data, _resources_of(doc, page.xref) or "", pymupdf.Matrix(1, 0, 0, 1, 0, 0), 0, found, 0, set())
    return found


def wrap(data: bytes, drawing: Drawing, transform: pymupdf.Matrix | None) -> bytes:
    """Content with `drawing` placed through `transform` (in PDF user space), or removed."""
    if transform is None:
        return data[:drawing.start] + b" " + data[drawing.end:]
    # Inserting "M cm" where the CTM is A gives everything after it M × A × …;
    # we want A × T, hence M = A × T × A⁻¹.
    a = drawing.anchor if drawing.anchor is not None else drawing.ctm
    m = a * transform * ~a
    cm = " ".join(f"{v:.6f}".rstrip("0").rstrip(".") or "0" for v in (m.a, m.b, m.c, m.d, m.e, m.f))
    return (data[:drawing.start] + f"q {cm} cm ".encode() + data[drawing.start:drawing.end] + b" Q"
            + data[drawing.end:])
