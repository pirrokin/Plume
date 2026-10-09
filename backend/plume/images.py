"""Moving, resizing and deleting images on a page.

The drawing operation of the image is wrapped in a new transformation (or
removed) in the content stream that holds it; the image data itself is never
re-encoded, so there is no quality loss.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pymupdf

from .contentstream import ContentError, Drawing, _resources_of, _xobjects, find_drawings, wrap

# Images covering most of the page are backgrounds: not selectable by a click.
BACKGROUND_SHARE = 0.85
MIN_SIDE = 3.0


class ImageError(Exception):
    pass


@dataclass
class PageImage:
    id: str
    sig: str
    bbox: pymupdf.Rect  # unrotated page coordinates
    drawing: Drawing
    background: bool
    movable: bool
    reason: str = ""

    def public(self) -> dict:
        return {
            "id": self.id,
            "sig": self.sig,
            "bbox": [self.bbox.x0, self.bbox.y0, self.bbox.x1, self.bbox.y1],
            "background": self.background,
            "movable": self.movable,
            "reason": self.reason,
        }


def _stream_bytes(doc: pymupdf.Document, page: pymupdf.Page, stream: int) -> bytes:
    if stream:
        return doc.xref_stream(stream) or b""
    return b"\n".join(doc.xref_stream(x) or b"" for x in page.get_contents())


def _set_stream(doc: pymupdf.Document, page: pymupdf.Page, stream: int, data: bytes) -> None:
    if stream:
        doc.update_stream(stream, data)
        return
    xrefs = page.get_contents()
    if not xrefs:
        raise ImageError("Page sans contenu")
    doc.update_stream(xrefs[0], data)
    if len(xrefs) > 1:
        doc.xref_set_key(page.xref, "Contents", f"{xrefs[0]} 0 R")


def _shared_elsewhere(doc: pymupdf.Document, page: pymupdf.Page, stream: int) -> bool:
    """Whether a form is also drawn by other pages (editing it would change them too)."""
    if not stream:
        return False
    for other in doc:
        if other.number == page.number:
            continue
        xobjs = _xobjects(doc, _resources_of(doc, other.xref) or "")
        if stream in xobjs.values():
            return True
        for xref in xobjs.values():
            if doc.xref_get_key(xref, "Subtype")[1] == "/Form" and stream in _xobjects(doc, _resources_of(doc, xref)).values():
                return True
    return False


def list_images(doc: pymupdf.Document, page: pymupdf.Page) -> list[PageImage]:
    try:
        drawings = find_drawings(doc, page, _stream_bytes(doc, page, 0))
    except ContentError:
        return []
    to_page = page.transformation_matrix
    page_area = abs(page.rect) or 1.0
    shared: dict[int, bool] = {}
    images = []
    for i, d in enumerate(drawings):
        bbox = d.bbox_pdf.transform(to_page)
        if bbox.width < MIN_SIDE or bbox.height < MIN_SIDE:
            continue
        visible = abs(bbox & pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height))
        # Backgrounds: covering most of the page, or big decorations lying mostly
        # outside of it. They are not selected by a click.
        background = visible >= BACKGROUND_SHARE * page_area or (
            visible < 0.5 * abs(bbox) and abs(bbox) > 0.15 * page_area
        )
        if d.stream not in shared:
            shared[d.stream] = _shared_elsewhere(doc, page, d.stream)
        movable = not shared[d.stream]
        reason = "" if movable else "Cette image est partagée avec d'autres pages."
        sig = hashlib.sha1(f"{d.stream}:{d.start}:{d.end}:{tuple(d.ctm)}".encode()).hexdigest()[:10]
        images.append(PageImage(f"i{i}", sig, bbox, d, background, movable, reason))
    return images


def find_image(doc: pymupdf.Document, page: pymupdf.Page, image_id: str, sig: str | None) -> PageImage:
    for img in list_images(doc, page):
        if img.id == image_id:
            if sig and img.sig != sig:
                raise ImageError("L'image a changé entre-temps, rechargez la page.")
            if not img.movable:
                raise ImageError(img.reason or "Cette image ne peut pas être modifiée.")
            return img
    raise ImageError("Image introuvable")


def page_transform(dx: float, dy: float, scale: float, anchor: pymupdf.Point) -> pymupdf.Matrix:
    """Scale about `anchor` then move by (dx, dy), in page coordinates."""
    return (
        pymupdf.Matrix(1, 0, 0, 1, -anchor.x, -anchor.y)
        * pymupdf.Matrix(scale, 0, 0, scale, 0, 0)
        * pymupdf.Matrix(1, 0, 0, 1, anchor.x + dx, anchor.y + dy)
    )


def transform_image(doc: pymupdf.Document, page: pymupdf.Page, img: PageImage, dx: float, dy: float,
                    scale: float = 1.0) -> pymupdf.Rect:
    """Move (and optionally scale, about the top-left corner) an image; returns its new box."""
    if not 0.02 <= scale <= 50:
        raise ImageError("Taille invalide")
    a_page = page_transform(dx, dy, scale, img.bbox.tl)
    to_page = page.transformation_matrix
    a_pdf = to_page * a_page * ~to_page
    d = img.drawing
    data = _stream_bytes(doc, page, d.stream)
    _set_stream(doc, page, d.stream, wrap(data, d, a_pdf))
    return img.bbox.transform(a_page)


def delete_image(doc: pymupdf.Document, page: pymupdf.Page, img: PageImage) -> None:
    d = img.drawing
    data = _stream_bytes(doc, page, d.stream)
    _set_stream(doc, page, d.stream, wrap(data, d, None))


def render_without(doc: pymupdf.Document, page: pymupdf.Page, img: PageImage, matrix: pymupdf.Matrix) -> bytes:
    """Picture of the page with one image hidden (shown under the image being dragged)."""
    d = img.drawing
    if d.stream:
        original = doc.xref_stream(d.stream) or b""
        contents = None
    else:
        contents = [(x, doc.xref_stream(x) or b"") for x in page.get_contents()]
        original = b""
    page_contents_key = doc.xref_get_key(page.xref, "Contents")
    try:
        delete_image(doc, page, img)
        return doc[page.number].get_pixmap(matrix=matrix, alpha=False).tobytes("png")
    finally:
        if d.stream:
            doc.update_stream(d.stream, original)
        else:
            for x, data in contents:
                doc.update_stream(x, data)
            if page_contents_key[0] in ("array", "xref"):
                doc.xref_set_key(page.xref, "Contents", page_contents_key[1])
