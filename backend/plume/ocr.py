"""Text recognition for scanned pages.

MuPDF embeds Tesseract; it only needs the language model files, which are
downloaded on first use. Recognised text is written into the page as an
invisible layer (like OCRmyPDF does), which makes the PDF searchable and lets
the editor treat scanned lines as editable blocks: editing one paints over the
scanned text with the paper colour and writes the new text on top.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import urllib.request
from pathlib import Path

import pymupdf

from . import config
from .docfonts import DocFonts
from .googlefonts import _SSL
from .writer import remove_chars, textwriter_correction

log = logging.getLogger(__name__)

TESSDATA = Path(os.environ.get("PLUME_TESSDATA", config.DATA_DIR / "tessdata"))
MODEL_URL = os.environ.get(
    "PLUME_TESSDATA_URL", "https://github.com/tesseract-ocr/tessdata_best/raw/main/{lang}.traineddata"
)
DEFAULT_LANGUAGES = os.environ.get("PLUME_OCR_LANGUAGES", "fra+eng")
_download_lock = threading.Lock()


class OcrError(Exception):
    pass


def ensure_languages(languages: str) -> Path:
    """Download the Tesseract models of `languages` ("fra+eng") if needed."""
    TESSDATA.mkdir(parents=True, exist_ok=True)
    with _download_lock:
        for lang in languages.split("+"):
            lang = lang.strip()
            if not lang.isalnum() and "_" not in lang:
                raise OcrError(f"Langue inconnue : {lang}")
            target = TESSDATA / f"{lang}.traineddata"
            if target.exists() and target.stat().st_size > 1000:
                continue
            url = MODEL_URL.format(lang=lang)
            log.info("downloading OCR model %s", url)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "plume-pdf"})
                with urllib.request.urlopen(req, timeout=120, context=_SSL) as res:
                    data = res.read()
            except Exception as exc:
                raise OcrError(f"Impossible de télécharger le modèle OCR « {lang} » : {exc}") from exc
            tmp = target.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(target)
    return TESSDATA


def page_needs_ocr(page: pymupdf.Page) -> bool:
    """A page mostly made of images with (almost) no text layer."""
    area = abs(page.rect)
    if not area:
        return False
    covered = 0.0
    for info in page.get_image_info():
        covered += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    if covered < 0.4 * area:
        return False
    # Any text layer, visible or not (a previous OCR), counts.
    chars = sum(len(t.strip()) for t in page.get_text("text").split("\n"))
    return chars < 40


# ---------------------------------------------------------------- image analysis
def _gray_rows(pix: pymupdf.Pixmap, threshold: int) -> list[int]:
    """Number of dark pixels in each row of a grey pixmap."""
    w, h, n = pix.width, pix.height, pix.n
    data = pix.samples
    rows = []
    for y in range(h):
        row = data[y * pix.stride: y * pix.stride + w * n: n]
        rows.append(sum(1 for v in row if v < threshold))
    return rows


def line_metrics(page: pymupdf.Page, rect: pymupdf.Rect, text: str) -> tuple[float, float]:
    """Estimate (baseline, font size) of a scanned text line from its pixels."""
    dpi = 200
    scale = dpi / 72.0
    clip = rect + (-1, -2, 1, 2)
    try:
        pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    except Exception:
        return rect.y1, rect.height * 0.75
    if pix.width < 2 or pix.height < 4:
        return rect.y1, rect.height * 0.75
    samples = pix.samples
    values = sorted(samples[:: max(1, len(samples) // 4000)])
    paper = values[int(len(values) * 0.9)] if values else 255
    ink = values[int(len(values) * 0.02)] if values else 0
    threshold = (paper + ink) / 2
    rows = _gray_rows(pix, int(threshold))
    peak = max(rows) if rows else 0
    if peak == 0:
        return rect.y1, rect.height * 0.75
    dense = [i for i, c in enumerate(rows) if c >= 0.35 * peak]
    inked = [i for i, c in enumerate(rows) if c >= 0.04 * peak]
    base_row = dense[-1] + 1 if dense else len(rows)
    top_row = inked[0] if inked else 0
    x_rows = [i for i in range(len(rows)) if rows[i] >= 0.5 * peak]
    baseline = clip.y0 + base_row / scale
    tall = any(c in text for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZbdfhklt0123456789ÉÈÀÇ")
    if tall and base_row > top_row:
        size = (base_row - top_row) / scale / 0.72
    elif x_rows:
        size = (base_row - x_rows[0]) / scale / 0.5
    else:
        size = rect.height * 0.75
    size = max(3.0, min(size, rect.height * 1.3))
    return baseline, size


def sample_colors(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[str, str]:
    """Paper colour around `rect` and ink colour inside it, as #rrggbb."""
    clip = (rect + (-3, -3, 3, 3)) & page.rect
    try:
        pix = page.get_pixmap(clip=clip, dpi=100, colorspace=pymupdf.csRGB, alpha=False)
    except Exception:
        return "#ffffff", "#000000"
    w, h = pix.width, pix.height
    if w < 3 or h < 3:
        return "#ffffff", "#000000"
    data = pix.samples
    stride = pix.stride
    border = []
    for x in range(w):
        for y in (0, 1, h - 2, h - 1):
            i = y * stride + x * 3
            border.append(tuple(data[i:i + 3]))
    for y in range(h):
        for x in (0, 1, w - 2, w - 1):
            i = y * stride + x * 3
            border.append(tuple(data[i:i + 3]))
    bg = tuple(int(statistics.median(c[k] for c in border)) for k in range(3))
    lum_bg = sum(bg) / 3
    pixels = [tuple(data[y * stride + x * 3: y * stride + x * 3 + 3]) for y in range(2, h - 2) for x in range(2, w - 2)]
    far = [p for p in pixels if abs(sum(p) / 3 - lum_bg) > 60]
    if far:
        far.sort(key=lambda p: sum(p))
        darkest = far[: max(1, len(far) // 3)]
        ink = tuple(int(statistics.median(c[k] for c in darkest)) for k in range(3))
    else:
        ink = (0, 0, 0)
    return "#%02x%02x%02x" % bg, "#%02x%02x%02x" % ink


# ---------------------------------------------------------------- OCR
def _plausible(text: str) -> bool:
    """Reject recognition noise (specks, rules, picture fragments)."""
    alnum = sum(ch.isalnum() for ch in text)
    if alnum == 0:
        return False
    if len(text) <= 2 and not text.isalnum():
        return False
    return alnum >= 0.4 * len(text.replace(" ", ""))


def recognize_page(doc: pymupdf.Document, fonts: DocFonts, page_no: int, languages: str = DEFAULT_LANGUAGES) -> int:
    """Add an invisible, positioned text layer to a page. Returns the number of words."""
    tessdata = ensure_languages(languages)
    page = doc[page_no]
    try:
        tp = page.get_textpage_ocr(language=languages, dpi=300, full=True, tessdata=str(tessdata))
    except Exception as exc:
        raise OcrError(f"La reconnaissance de texte a échoué : {exc}") from exc
    raw = page.get_text("rawdict", textpage=tp, flags=pymupdf.TEXT_PRESERVE_WHITESPACE)

    # Drop a previous invisible layer so text is not duplicated.
    from .extract import _page_segments

    old = [c for s in _page_segments(page, fonts)[0] if s.invisible for c in s.chars]
    if old:
        remove_chars(page, old)

    font = pymupdf.Font("helv")

    # Tesseract sometimes splits one printed line into several pieces
    # ("Montant total", ":", "1 250"): gather pieces sharing a row first.
    pieces = []
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = [c for span in line["spans"] for c in span["chars"]]
            text = "".join(c["c"] for c in chars).strip()
            if not text:
                continue
            sizes = []
            for span in line["spans"]:
                n = len("".join(c["c"] for c in span["chars"]).strip())
                sizes += [span["size"]] * n
            pieces.append((pymupdf.Rect(line["bbox"]), text, statistics.median(sizes) if sizes else 0, chars))
    pieces.sort(key=lambda p: ((p[0].y0 + p[0].y1) / 2, p[0].x0))
    rows: list[list[tuple]] = []
    for piece in pieces:
        rect = piece[0]
        for row in reversed(rows[-6:]):
            last = row[-1][0]
            overlap = min(rect.y1, last.y1) - max(rect.y0, last.y0)
            if overlap >= 0.5 * min(rect.height, last.height) and rect.x0 >= last.x1 - 1:
                row.append(piece)
                break
        else:
            rows.append([piece])

    tw = pymupdf.TextWriter(page.rect)
    words = 0
    for row in rows:
        row.sort(key=lambda p: p[0].x0)
        full = " ".join(p[1] for p in row)
        if not _plausible(full):
            continue
        words += len(full.split())
        weights = [(p[2], len(p[1])) for p in row if p[2] > 0]
        size = statistics.median([s for s, n in weights for _ in range(n)]) if weights else row[0][0].height * 0.75
        union = pymupdf.Rect(row[0][0])
        for p in row[1:]:
            union |= p[0]
        baseline, _ = line_metrics(page, union, full)
        # Words at their recognised positions, letters spaced by their natural
        # widths (scaled to the word), and a real space after every word so
        # that text extraction keeps words apart without splitting them.
        word: list[dict] = []
        gap = {"c": " ", "bbox": (0, 0, 0, 0)}
        for c in [c for p in row for c in p[3] + [gap]]:
            if not c["c"].isspace():
                word.append(c)
                continue
            if not word:
                continue
            text = "".join(ch["c"] if font.has_glyph(ord(ch["c"])) else "?" for ch in word)
            x0, x1 = word[0]["bbox"][0], word[-1]["bbox"][2]
            natural = [font.glyph_advance(ord(ch)) * size for ch in text]
            # Letters spread wider than ~15 % would read as separate words.
            k = min((x1 - x0) / (sum(natural) or 1), 1.15)
            x = x0
            for ch, adv in zip(text, natural):
                tw.append((x, baseline), ch, font=font, fontsize=size)
                x += adv * k
            tw.append((x1, baseline), " ", font=font, fontsize=size)
            word = []
    tw.write_text(page, render_mode=3, matrix=textwriter_correction(page))
    fonts.invalidate()
    return words
