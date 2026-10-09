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

# "fast" models: on a shared server they read a page about 6 times faster than
# the "best" ones, with the same result on ordinary printed documents.
TESSDATA = Path(os.environ.get("PLUME_TESSDATA", config.DATA_DIR / "tessdata-fast"))
MODEL_URL = os.environ.get(
    "PLUME_TESSDATA_URL", "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
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
_DARK_TABLES: dict[int, bytes] = {}


def _dark_table(threshold: int) -> bytes:
    """bytes.translate table turning pixels darker than `threshold` into 1, others into 0."""
    t = _DARK_TABLES.get(threshold)
    if t is None:
        t = bytes(1 if v < threshold else 0 for v in range(256))
        _DARK_TABLES[threshold] = t
    return t


class PageRaster:
    """One picture of a page reused for many measurements.

    Rendering a clip per line or per block is far too slow on a shared server;
    one rendering of the page, then plain byte operations, is not.
    """

    def __init__(self, page: pymupdf.Page, dpi: int, gray: bool):
        self.scale = dpi / 72.0
        self.rotation = page.rotation
        self.to_display = page.rotation_matrix
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB, alpha=False)
        self.n, self.w, self.h, self.stride = pix.n, pix.width, pix.height, pix.stride
        self.samples = pix.samples  # one copy: the property copies on every access

    def box(self, rect: pymupdf.Rect) -> tuple[int, int, int, int]:
        """Pixel box of a rectangle given in unrotated page coordinates."""
        r = rect.transform(self.to_display)
        x0 = max(0, min(self.w, int(r.x0 * self.scale)))
        x1 = max(0, min(self.w, int(r.x1 * self.scale + 0.999)))
        y0 = max(0, min(self.h, int(r.y0 * self.scale)))
        y1 = max(0, min(self.h, int(r.y1 * self.scale + 0.999)))
        return x0, y0, x1, y1


def line_metrics(page: pymupdf.Page, rect: pymupdf.Rect, text: str,
                 raster: PageRaster | None = None) -> tuple[float, float]:
    """Estimate (baseline, font size) of a scanned text line from its pixels."""
    fallback = (rect.y1, rect.height * 0.75)
    if raster is None or raster.rotation != 0 or raster.n != 1:
        raster = PageRaster(page, 200, gray=True) if page.rotation == 0 else None
    if raster is None:
        return fallback
    clip = rect + (-1, -2, 1, 2)
    x0, y0, x1, y1 = raster.box(clip)
    if x1 - x0 < 2 or y1 - y0 < 4:
        return fallback
    data, stride = raster.samples, raster.stride
    sample = bytearray()
    for y in range(y0, y1, max(1, (y1 - y0) // 12)):
        sample += data[y * stride + x0: y * stride + x1: 3]
    values = sorted(sample)
    if not values:
        return fallback
    paper = values[int(len(values) * 0.9)]
    ink = values[int(len(values) * 0.02)]
    table = _dark_table(int((paper + ink) / 2))
    rows = [data[y * stride + x0: y * stride + x1].translate(table).count(1) for y in range(y0, y1)]
    peak = max(rows)
    if peak == 0:
        return fallback
    scale = raster.scale
    dense = [i for i, c in enumerate(rows) if c >= 0.35 * peak]
    inked = [i for i, c in enumerate(rows) if c >= 0.04 * peak]
    base_row = dense[-1] + 1 if dense else len(rows)
    top_row = inked[0] if inked else 0
    x_rows = [i for i, c in enumerate(rows) if c >= 0.5 * peak]
    baseline = (y0 + base_row) / scale
    tall = any(c in text for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZbdfhklt0123456789ÉÈÀÇ")
    if tall and base_row > top_row:
        size = (base_row - top_row) / scale / 0.72
    elif x_rows:
        size = (base_row - x_rows[0]) / scale / 0.5
    else:
        size = rect.height * 0.75
    size = max(3.0, min(size, rect.height * 1.3))
    return baseline, size


def sample_colors(page: pymupdf.Page, rect: pymupdf.Rect, raster: PageRaster | None = None) -> tuple[str, str]:
    """Paper colour around `rect` and ink colour inside it, as #rrggbb."""
    if raster is None or raster.n != 3:
        raster = PageRaster(page, 100, gray=False)
    x0, y0, x1, y1 = raster.box(rect + (-3, -3, 3, 3))
    if x1 - x0 < 5 or y1 - y0 < 5:
        return "#ffffff", "#000000"
    data, stride = raster.samples, raster.stride

    def px(x: int, y: int) -> tuple[int, int, int]:
        i = y * stride + x * 3
        return data[i], data[i + 1], data[i + 2]

    border = [px(x, y) for x in range(x0, x1) for y in (y0, y1 - 1)]
    border += [px(x, y) for y in range(y0, y1) for x in (x0, x1 - 1)]
    bg = tuple(int(statistics.median(c[k] for c in border)) for k in range(3))
    lum_bg = sum(bg) / 3
    step = max(1, (x1 - x0) * (y1 - y0) // 6000)
    inner = [px(x, y) for y in range(y0 + 2, y1 - 2) for x in range(x0 + 2, x1 - 2, step)]
    far = [p for p in inner if abs(sum(p) / 3 - lum_bg) > 60]
    if far:
        # The core of the strokes; their anti-aliased edges are paler.
        far.sort(key=sum)
        darkest = far[: max(1, len(far) // 7)]
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
    raster = PageRaster(page, 200, gray=True) if page.rotation == 0 else None
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
        baseline, _ = line_metrics(page, union, full, raster)
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
