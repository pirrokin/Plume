import copy

import pymupdf

from plume.docfonts import DocFonts
from plume.editor import add_block, delete_block, edit_block
from plume.extract import extract_page


def _text(block):
    return "".join(r["text"] for r in block["runs"])


def _find(model, start):
    return next(b for b in model["blocks"] if _text(b).startswith(start))


def _box(block):
    box = {k: block[k] for k in ("x", "width", "wrap", "align", "lineHeight", "indent", "size")}
    box["y"] = block["baseline"]
    return box


def _page_words(page):
    return [w[4] for w in page.get_text("words")]


def test_paragraph_detection(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    model = extract_page(doc[0], DocFonts(doc))
    para = _find(model, "Le traitement")
    assert para["wrap"] and para["align"] == "justify"
    assert len(para["lines"]) >= 3
    assert "l'auteur du document." in _text(para)
    # Table cells stay separate blocks; the right column is right-aligned text? it is left in source.
    assert _find(model, "Total HT")["wrap"] is False
    assert _find(model, "1 250,00")["wrap"] is False
    # Tightly spaced lines that are separate paragraphs.
    assert _find(model, "Ligne serrée numéro un")["lines"][0]["text"].strip() == "Ligne serrée numéro un"


def test_edit_keeps_rest_of_page(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    fonts = DocFonts(doc)
    model = extract_page(doc[0], fonts)
    block = _find(model, "Ligne serrée numéro un")
    runs = copy.deepcopy(block["runs"])
    runs[0]["text"] = "Ligne modifiée avec un texte plus long"
    edit_block(doc, fonts, 0, model["internal"][block["id"]], runs, _box(block))
    text = doc[0].get_text()
    assert "Ligne modifiée avec un texte plus long" in text
    assert "Ligne serrée numéro deux" in text  # neighbour line 12pt below untouched
    assert "Ligne serrée numéro un" not in text
    assert "Rapport annuel 2025" in text
    assert len(doc[0].get_drawings()) >= 1  # vector frame kept


def test_edit_paragraph_reflows(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    fonts = DocFonts(doc)
    model = extract_page(doc[0], fonts)
    block = _find(model, "Le traitement")
    runs = copy.deepcopy(block["runs"])
    runs[0]["text"] = runs[0]["text"].replace("considérable", "vraiment très considérable")
    report = edit_block(doc, fonts, 0, model["internal"][block["id"]], runs, _box(block))
    assert report.kept_lines >= 1  # the first line is unchanged and left alone
    model2 = extract_page(doc[0], fonts)
    para = _find(model2, "Le traitement")
    assert "vraiment très considérable" in _text(para)
    assert para["align"] == "justify"
    assert abs(para["x"] - block["x"]) < 1 and abs(para["width"] - block["width"]) < 1.5


def test_delete_and_add(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    fonts = DocFonts(doc)
    model = extract_page(doc[0], fonts)
    block = _find(model, "TVA")
    delete_block(doc, fonts, 0, model["internal"][block["id"]])
    words = _page_words(doc[0])
    assert "TVA" not in words and "Total" in words
    add_block(doc, fonts, 0, [{"text": "Nouveau bloc", "family": "helvetica", "size": 14, "color": "#cc0000"}],
              {"x": 100, "y": 500, "width": 200, "wrap": False, "align": "left"})
    words = doc[0].get_text("words")
    hit = [w for w in words if w[4] == "Nouveau"]
    assert hit and abs(hit[0][0] - 100) < 0.5


def test_rotated_cropped_page(sample_pdf):
    doc = pymupdf.open(sample_pdf)
    fonts = DocFonts(doc)
    model = extract_page(doc[1], fonts)
    block = _find(model, "Page tournée")
    runs = copy.deepcopy(block["runs"])
    runs[0]["text"] = "Page tournée et modifiée"
    edit_block(doc, fonts, 1, model["internal"][block["id"]], runs, _box(block))
    model2 = extract_page(doc[1], fonts)
    new = _find(model2, "Page tournée et modifiée")
    assert abs(new["x"] - block["x"]) < 0.5
    assert abs(new["baseline"] - block["baseline"]) < 0.5
    assert _find(model2, "Texte de contrôle")


def test_written_text_round_trips(sample_pdf):
    """Spaces, hyphens and semicolons must not come back as look-alike characters."""
    doc = pymupdf.open(sample_pdf)
    fonts = DocFonts(doc)
    text = "Salle-G : prix; 12 € – « test »"
    add_block(doc, fonts, 0, [{"text": text, "family": "arial", "size": 12}],
              {"x": 72, "y": 600, "width": 400, "wrap": False, "align": "left"})
    got = doc[0].get_text("text", clip=pymupdf.Rect(60, 585, 500, 610)).strip()
    assert got == text
