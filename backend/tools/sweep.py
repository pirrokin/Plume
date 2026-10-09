"""Robustness sweep: random edits on many PDFs, checking nothing else changes (dev tool).

Usage: python -m tools.sweep [dir ...]
Works on in-memory copies only; source files are never modified.
"""

import copy
import random
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)

from plume.docfonts import DocFonts
from plume.editor import edit_block
from plume.extract import extract_page


def sweep_file(path: Path, rng: random.Random, max_pages=3, edits_per_page=3):
    stats = Counter()
    problems = []
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return stats, [f"open failed: {exc}"]
    if doc.needs_pass:
        return stats, ["encrypted"]
    fonts = DocFonts(doc)
    for pno in range(min(max_pages, doc.page_count)):
        t = time.time()
        try:
            model = extract_page(doc[pno], fonts)
        except Exception:
            problems.append(f"p{pno} extract: {traceback.format_exc(limit=3)}")
            continue
        stats["extract_ms"] += int((time.time() - t) * 1000)
        stats["blocks"] += len(model["blocks"])
        editable = [b for b in model["blocks"] if b["editable"] and "".join(r["text"] for r in b["runs"]).strip()]
        stats["locked"] += len(model["blocks"]) - len(editable)
        picks = [b["sig"] for b in rng.sample(editable, min(edits_per_page, len(editable)))]
        for sig in picks:
            block = next((b for b in model["blocks"] if b["sig"] == sig), None)
            if block is None:
                continue
            runs = copy.deepcopy(block["runs"])
            runs[-1]["text"] = runs[-1]["text"] + " modifié"
            box = {k: block[k] for k in ("x", "width", "wrap", "align", "lineHeight", "indent", "size")}
            box["y"] = block["baseline"]
            page = doc[pno]
            region = pymupdf.Rect(block["bbox"])
            words_before = page.get_text("words")
            t = time.time()
            try:
                report = edit_block(doc, fonts, pno, model["internal"][block["id"]], runs, box, block.get("bg") if block.get("scan") else None)
            except Exception:
                problems.append(f"p{pno} {block['id']} edit: {traceback.format_exc(limit=4)}")
                stats["edit_errors"] += 1
                break
            stats["edit_ms"] += int((time.time() - t) * 1000)
            stats["edits"] += 1
            if report.bbox:
                region |= pymupdf.Rect(report.bbox)
            region = region + (-1, -1, 1, 1)
            before = Counter((w[4], round(w[0]), round(w[1])) for w in words_before if not pymupdf.Rect(w[:4]).intersects(region))
            after = Counter((w[4], round(w[0]), round(w[1])) for w in doc[pno].get_text("words") if not pymupdf.Rect(w[:4]).intersects(region))
            lost = before - after
            if lost:
                stats["collateral"] += 1
                problems.append(f"p{pno} {block['id']} lost words outside block: {list(lost)[:6]} region={tuple(round(v) for v in region)}")
            if "modifié" not in doc[pno].get_text():
                stats["missing_new_text"] += 1
                problems.append(f"p{pno} {block['id']} new text not found ({''.join(r['text'] for r in block['runs'])[:40]!r})")
            model = extract_page(doc[pno], fonts)
    return stats, problems


def main() -> None:
    dirs = [Path(d).expanduser() for d in (sys.argv[1:] or ["~/Documents", "~/Downloads"])]
    files = []
    for d in dirs:
        files += [p for p in d.rglob("*.pdf") if "plume-pdf" not in str(p) and p.stat().st_size < 40_000_000]
    rng = random.Random(42)
    total = Counter()
    bad_files = 0
    for i, f in enumerate(sorted(files)):
        t = time.time()
        stats, problems = sweep_file(f, rng)
        total.update(stats)
        dt = time.time() - t
        flag = "!!" if problems else "ok"
        print(f"[{i + 1}/{len(files)}] {flag} {f.name[:60]:60} edits={stats['edits']} blocks={stats['blocks']} {dt:.1f}s")
        for p in problems[:4]:
            print("      ", p.strip().replace("\n", "\n       "))
        bad_files += bool(problems)
    print("\nTOTAL", dict(total), "files with problems:", bad_files, "/", len(files))


if __name__ == "__main__":
    main()
