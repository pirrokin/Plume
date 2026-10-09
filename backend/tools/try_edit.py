"""Edit one text block of a PDF from the command line and render before/after (dev tool).

Usage: python -m tools.try_edit file.pdf page "start of block text" "old" "new" out_prefix
"""

import copy
import sys
import time

import pymupdf

from plume.docfonts import DocFonts
from plume.editor import edit_block
from plume.extract import extract_page


def stack(images, path):
    w = max(p.width for p in images)
    h = sum(p.height for p in images) + 8 * (len(images) - 1)
    out = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    out.set_rect(out.irect, (255, 160, 160))
    y = 0
    for p in images:
        if p.alpha:
            p = pymupdf.Pixmap(p, 0)
        p.set_origin(0, y)
        out.copy(p, p.irect)
        y += p.height + 8
    out.save(path)


def main() -> None:
    path, page_no, prefix, old, new, out = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
    doc = pymupdf.open(path)
    fonts = DocFonts(doc)
    model = extract_page(doc[page_no], fonts)
    block = next(b for b in model["blocks"] if "".join(r["text"] for r in b["runs"]).startswith(prefix))
    print("block", block["id"], block["align"], "wrap" if block["wrap"] else "line", block["issues"])
    for r in block["runs"]:
        print("   run", repr(r["text"][:40]), r["family"], r["weight"], r["italic"], r["size"], r["color"], "sp", r["spacing"], "rise", r["rise"])
    runs = copy.deepcopy(block["runs"])
    done = False
    for r in runs:
        if old in r["text"]:
            r["text"] = r["text"].replace(old, new, 1)
            done = True
            break
    if not done:
        raise SystemExit(f"'{old}' not found in block")
    box = {k: block[k] for k in ("x", "width", "wrap", "align", "lineHeight", "indent", "size")}
    box["y"] = block["baseline"]
    before = doc[page_no].get_pixmap(dpi=200, clip=pymupdf.Rect(block["bbox"]) + (-20, -15, 60, 30))
    t = time.time()
    rep = edit_block(doc, fonts, page_no, model["internal"][block["id"]], runs, box)
    print("report", rep, f"{time.time() - t:.3f}s")
    after = doc[page_no].get_pixmap(dpi=200, clip=pymupdf.Rect(block["bbox"]) + (-20, -15, 60, 30))
    stack([before, after], out + "_crop.png")
    doc[page_no].get_pixmap(dpi=90).save(out + "_page.png")
    doc.save(out + ".pdf")


if __name__ == "__main__":
    main()
