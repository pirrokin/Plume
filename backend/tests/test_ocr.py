import copy

import pymupdf
import pytest

from plume.docfonts import DocFonts
from plume.editor import edit_block
from plume.extract import extract_page
from plume.ocr import TESSDATA, page_needs_ocr, recognize_page

pytestmark = pytest.mark.skipif(not (TESSDATA / "fra.traineddata").exists(), reason="OCR model not downloaded")


def _scanned_pdf(path):
    src = pymupdf.open()
    page = src.new_page(width=595, height=842)
    page.insert_text((72, 120), "Facture numéro 2026-041", fontsize=16, fontname="hebo")
    page.insert_text((72, 160), "Montant total : 1 250 euros", fontsize=13, fontname="helv")
    page.insert_text((72, 185), "Merci pour votre confiance.", fontsize=13, fontname="helv")
    pix = page.get_pixmap(dpi=200)
    doc = pymupdf.open()
    scan = doc.new_page(width=595, height=842)
    scan.insert_image(scan.rect, pixmap=pix)
    doc.save(path)
    return path


def test_ocr_makes_scans_editable(tmp_path):
    doc = pymupdf.open(_scanned_pdf(tmp_path / "scan.pdf"))
    fonts = DocFonts(doc)
    assert page_needs_ocr(doc[0])
    words = recognize_page(doc, fonts, 0, "fra")
    assert words >= 8
    assert not page_needs_ocr(doc[0])
    model = extract_page(doc[0], fonts)
    blocks = {"".join(r["text"] for r in b["runs"]): b for b in model["blocks"]}
    target = next(b for t, b in blocks.items() if "Montant" in t)
    assert target["scan"] and target["editable"] and target["bg"] == "#ffffff"
    runs = copy.deepcopy(target["runs"])
    runs[0]["text"] = "Montant total : 2 500 euros"
    box = {k: target[k] for k in ("x", "width", "wrap", "align", "lineHeight", "indent", "size")}
    box["y"] = target["baseline"]
    edit_block(doc, fonts, 0, model["internal"][target["id"]], runs, box, target["bg"])
    text = doc[0].get_text()
    assert "2 500 euros" in text
    assert "1 250" not in text  # the invisible OCR text of the line was replaced too
