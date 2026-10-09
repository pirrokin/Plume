"""Build docs/presentation-plume.pdf, the document used for the README screenshots.

Usage (from the repository root): .venv/bin/python docs/make_presentation.py
Needs the Poppins and Lora fonts in data/fonts/google (downloaded by Plume).
"""

from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "data" / "fonts" / "google"
OUT = ROOT / "docs" / "presentation-plume.pdf"
SHOT = ROOT / "docs" / "screenshots" / "accueil.png"

BLUE = (0.18, 0.36, 0.91)
INK = (0.11, 0.13, 0.18)

CSS = """
@font-face { font-family: Poppins; src: url(poppins/poppins-600.ttf); font-weight: 600; }
@font-face { font-family: Poppins; src: url(poppins/poppins-700.ttf); font-weight: 700; }
@font-face { font-family: Lora; src: url(lora/lora-400.ttf); }
@font-face { font-family: Lora; src: url(lora/lora-400i.ttf); font-style: italic; }
@font-face { font-family: Lora; src: url(lora/lora-700.ttf); font-weight: bold; }
* { font-family: Lora; font-size: 10.5pt; color: #1c212e; line-height: 1.45; }
p { margin: 0 0 7pt 0; text-align: justify; }
h2 { font-family: Poppins; font-weight: 600; font-size: 15pt; color: #2e5ce8; margin: 10pt 0 5pt 0; }
h3 { font-family: Poppins; font-weight: 600; font-size: 11.5pt; margin: 6pt 0 3pt 0; }
td, th { padding: 2pt 8pt 2pt 0; text-align: left; font-size: 10pt; }
th { font-family: Poppins; font-weight: 600; font-size: 9.5pt; color: #2e5ce8; }
li { margin-bottom: 3pt; text-align: justify; }
.small { font-size: 9pt; color: #5b6270; }
"""


def band(page: pymupdf.Page, title: str, subtitle: str, archive: pymupdf.Archive) -> None:
    page.draw_rect(pymupdf.Rect(0, 0, page.rect.width, 150), color=None, fill=BLUE)
    page.insert_htmlbox(
        pymupdf.Rect(56, 34, 540, 140),
        f"<div style='font-family:Poppins;font-weight:700;font-size:48pt;color:white;line-height:1.1'>{title}</div>"
        f"<div style='font-family:Poppins;font-weight:600;font-size:13pt;color:#dfe7ff'>{subtitle}</div>",
        css=CSS, archive=archive,
    )


def footer(page: pymupdf.Page, n: int, archive: pymupdf.Archive) -> None:
    page.draw_line((56, 800), (539, 800), color=(0.85, 0.87, 0.9), width=0.6)
    page.insert_htmlbox(
        pymupdf.Rect(56, 806, 539, 830),
        f"<p class='small'>Plume — github.com/pirrokin/Plume — plume.kinam.fr — page {n}</p>",
        css=CSS, archive=archive,
    )


def main() -> None:
    archive = pymupdf.Archive(str(FONTS))
    doc = pymupdf.open()

    # ---------------------------------------------------------------- page 1
    page = doc.new_page(width=595, height=842)
    band(page, "Plume", "Modifier un PDF aussi simplement qu'un document texte", archive)
    page.insert_htmlbox(pymupdf.Rect(56, 172, 539, 470), """
<p>Un PDF n'est pas fait pour être modifié : il ne contient ni paragraphes, ni lignes, seulement
des lettres posées une à une sur la page, chacune avec sa police et sa position. Corriger une
faute de frappe dans un contrat, mettre à jour une date sur un CV ou changer un prix sur un devis
demande d'habitude un logiciel payant comme Acrobat Pro.</p>
<p><b>Plume</b> est un éditeur en ligne et gratuit qui fait la même chose : on dépose un PDF, on
clique sur un paragraphe et on tape. Le texte est réécrit <i>dans la police d'origine</i>, à la
même taille, avec le même interligne et le même alignement, sans toucher au reste de la page.</p>
<h2>Comment ça marche</h2>
<h3>1. Retrouver les paragraphes</h3>
<p>Plume lit chaque lettre du PDF, regroupe celles d'une même ligne, puis assemble les lignes en
paragraphes comme l'aurait fait un traitement de texte : alignement, justification, retrait,
listes à puces et cellules de tableau sont reconnus.</p>
<h3>2. Garder les polices</h3>
<p>Les polices intégrées au PDF sont réutilisées, même incomplètes. Pour les lettres absentes,
Plume prend la police installée correspondante ou, à défaut, la police libre qui lui ressemble
le plus à l'œil.</p>
<h3>3. Réécrire sans abîmer</h3>
<p>Seules les lettres du paragraphe modifié sont effacées ; le texte est recomposé puis réécrit.
Les lignes inchangées restent intactes, et Plume vérifie que rien d'autre n'a bougé.</p>
""", css=CSS, archive=archive)
    shot = pymupdf.Rect(110, 486, 485, 720)
    page.draw_rect(shot + (-1, -1, 1, 1), color=(0.82, 0.85, 0.9), width=0.8)
    pix = pymupdf.Pixmap(str(SHOT))
    page.insert_image(shot, pixmap=pix, keep_proportion=False)
    page.insert_htmlbox(pymupdf.Rect(56, 728, 539, 760),
                        "<p class='small' style='text-align:center'><i>La page d'accueil de Plume : "
                        "il suffit d'y déposer un PDF.</i></p>", css=CSS, archive=archive)
    footer(page, 1, archive)

    # ---------------------------------------------------------------- page 2
    page = doc.new_page(width=595, height=842)
    band(page, "Sous le capot", "Ce que Plume fait pour garder un document intact", archive)
    page.insert_htmlbox(pymupdf.Rect(56, 172, 539, 790), """
<h2>Des polices qui ressemblent vraiment</h2>
<p>Les PDF exportés par Canva, Word ou un Mac embarquent souvent des polices commerciales, et
seulement les lettres utilisées. Quand une lettre manque, Plume compare en images les lettres du
document à près de 700 familles de Google Fonts (forme, largeur, graisse, empattements,
inclinaison) et télécharge la plus proche.</p>
<table style='width:100%'>
<tr><th>Police du PDF</th><th>Remplaçante choisie</th><th>Origine</th></tr>
<tr><td>TT Chocolates</td><td>Quattrocento Sans</td><td>CV Canva</td></tr>
<tr><td>Intro</td><td>Figtree</td><td>Titres Canva</td></tr>
<tr><td>Aptos</td><td>Funnel Sans</td><td>Word</td></tr>
<tr><td>Helvetica Neue</td><td>Nimbus Sans</td><td>Mac</td></tr>
</table>
<h2>Les titres Canva en contour</h2>
<p>Un texte avec l'effet « contour » est exporté comme des dessins de lettres anonymes. Plume
retrouve la police d'origine et l'épaisseur du trait : un nom en contour modifié reste en contour.</p>
<h2>Et aussi</h2>
<ul>
<li><b>Documents scannés :</b> reconnaissance du texte (OCR) en français et en anglais, puis
modification comme un document normal.</li>
<li><b>Images :</b> déplacer, redimensionner ou supprimer une image, sans recompression.</li>
<li><b>Pages :</b> pivoter, dupliquer, insérer, supprimer, réordonner, fusionner deux PDF.</li>
<li><b>Confidentialité :</b> rien n'est conservé ; le document est supprimé du serveur à la
fermeture de l'onglet.</li>
</ul>
<h2>Prochaines étapes</h2>
<p>Ajouter ses propres images (PNG, JPG) sur une page, puis modifier le texte orienté.</p>
<p class='small'>Plume est un logiciel libre (licence AGPL) écrit en Python (FastAPI, PyMuPDF)
et TypeScript (React).</p>
""", css=CSS, archive=archive)
    footer(page, 2, archive)

    doc.set_metadata({"title": "Plume — présentation", "author": "Kinam Pirrottina"})
    doc.subset_fonts()
    doc.save(OUT, garbage=3, deflate=True)
    print(OUT, OUT.stat().st_size // 1024, "Ko")


if __name__ == "__main__":
    main()
