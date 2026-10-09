"""Draw detected text blocks of a PDF page on a PNG (dev tool).

Usage: python -m tools.inspect_blocks file.pdf page out.png
"""

import sys

import pymupdf

from plume.docfonts import DocFonts
from plume.extract import extract_page

COLORS = {"left": (0.9, 0.1, 0.1), "justify": (0.1, 0.5, 0.1), "center": (0.1, 0.1, 0.9), "right": (0.8, 0.5, 0)}


def main() -> None:
    path, page_no, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    doc = pymupdf.open(path)
    fonts = DocFonts(doc)
    page = doc[page_no]
    model = extract_page(page, fonts)
    shape = page.new_shape()
    for b in model["blocks"]:
        shape.draw_rect(pymupdf.Rect(b["bbox"]))
        shape.finish(color=COLORS.get(b["align"], (0, 0, 0)), width=0.7 if b["wrap"] else 0.4, dashes="[2] 0" if not b["wrap"] else None)
    shape.commit()
    for b in model["blocks"]:
        page.insert_text((b["bbox"][0], b["bbox"][1] - 1), f'{b["id"]} {b["align"][0]}', fontsize=4, color=(1, 0, 1))
    page.get_pixmap(dpi=110).save(out)
    for b in model["blocks"]:
        print(b["id"], b["align"], "wrap" if b["wrap"] else "line", f'lh={b["lineHeight"]}', repr("".join(r["text"] for r in b["runs"])[:90]), b["issues"] or "")


if __name__ == "__main__":
    main()
