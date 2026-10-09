"""Move every image of many PDFs a little and check it is still entirely visible (dev tool).

Usage: python -m tools.image_sweep [dir ...]
Works on in-memory copies only; source files are never modified.
"""

import sys
from pathlib import Path

import pymupdf

from plume.images import list_images, transform_image

pymupdf.TOOLS.mupdf_display_errors(False)


def render(page):
    """Whole page at 72 dpi: one pixel per point, so moves by whole points are exact shifts."""
    pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csRGB, alpha=False)
    return pix.width, pix.height, pix.stride, pix.samples


def similarity(before, after, rect, dx, dy):
    """Share of the image's pixels found unchanged at the moved position."""
    w, h, stride, a = before
    _, _, _, b = after
    x0, y0 = max(0, int(rect.x0) + 1), max(0, int(rect.y0) + 1)
    x1, y1 = min(w - dx - 1, int(rect.x1) - 1), min(h - dy - 1, int(rect.y1) - 1)
    same = total = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            i = y * stride + x * 3
            j = (y + dy) * stride + (x + dx) * 3
            total += 1
            if abs(a[i] - b[j]) + abs(a[i + 1] - b[j + 1]) + abs(a[i + 2] - b[j + 2]) < 90:
                same += 1
    return same / total if total else 1.0


def main() -> None:
    dirs = [Path(d).expanduser() for d in (sys.argv[1:] or ["~/Documents", "~/Downloads"])]
    files = sorted({p for d in dirs for p in d.rglob("*.pdf") if "plume-pdf" not in str(p)})
    checked = bad = 0
    for f in files:
        try:
            doc = pymupdf.open(f)
        except Exception:
            continue
        if doc.needs_pass:
            continue
        for pno in range(min(2, doc.page_count)):
            page = doc[pno]
            area = pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
            for img in list_images(doc, page):
                if img.background or not img.movable or img.bbox not in area or img.bbox.width < 15:
                    continue
                dx = 12 if img.bbox.x1 + 12 <= area.x1 else -12
                dy = 9 if img.bbox.y1 + 9 <= area.y1 else -9
                if img.bounds is not None:
                    continue  # constrained by a frame: checked by the UI limits instead
                if page.rotation:
                    continue
                before = render(page)
                snapshot = doc.tobytes()
                transform_image(doc, page, img, dx, dy)
                page = doc[pno]
                after = render(page)
                score = similarity(before, after, img.bbox, dx, dy)
                checked += 1
                if score < 0.7:
                    bad += 1
                    print(f"!! {f.name[:50]:50} p{pno} {img.id} {img.drawing.kind} sim={score:.2f} bbox={[round(v) for v in img.bbox]}")
                doc = pymupdf.open("pdf", snapshot)  # back to the original for the next image
                page = doc[pno]
    print(f"images vérifiées : {checked}, problèmes : {bad}")


if __name__ == "__main__":
    main()
