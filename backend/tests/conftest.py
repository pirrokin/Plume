import os
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PLUME_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data" / "test"))

LOREM = (
    "Le traitement automatique des documents permet de gagner un temps considérable. "
    "Chaque paragraphe doit pouvoir être modifié sans altérer la mise en page du reste "
    "de la page, ni les polices, ni les couleurs utilisées par l'auteur du document."
)


def make_sample_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 80), "Rapport annuel 2025", fontsize=20, fontname="hebo", color=(0.1, 0.2, 0.6))
    html = f"<p style='text-align:justify;font-family:serif;font-size:12pt;line-height:1.3'>{LOREM}</p>"
    page.insert_htmlbox(pymupdf.Rect(72, 100, 400, 220), html)
    page.insert_text((72, 260), "Total HT", fontsize=11, fontname="helv")
    page.insert_text((300, 260), "1 250,00 EUR", fontsize=11, fontname="helv")
    page.insert_text((72, 276), "TVA", fontsize=11, fontname="helv")
    page.insert_text((300, 276), "250,00 EUR", fontsize=11, fontname="helv")
    page.insert_text((72, 320), "Ligne serrée numéro un", fontsize=12, fontname="tiro")
    page.insert_text((72, 332), "Ligne serrée numéro deux", fontsize=12, fontname="tiro")
    page.draw_rect(pymupdf.Rect(60, 300, 400, 340), color=(0.8, 0.1, 0.1), width=1)
    # Rotated page with a crop box offset.
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((100, 200), "Page tournée", fontsize=14, fontname="helv")
    p2.insert_text((100, 400), "Texte de contrôle", fontsize=14, fontname="helv")
    p2.set_cropbox(pymupdf.Rect(50, 100, 545, 742))
    p2.set_rotation(90)
    doc.save(path)
    return path


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    return make_sample_pdf(tmp_path / "sample.pdf")
