import pymupdf
import pytest

from plume.images import delete_image, find_image, list_images, transform_image


def _image_pdf(path, rotation=0, cropbox=None, in_form=False):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 60), "Titre au-dessus", fontsize=14, fontname="helv")
    if in_form:
        # Image inside a form that also holds text, like Canva pages.
        src = pymupdf.open()
        sp = src.new_page(width=595, height=842)
        sp.insert_image(pymupdf.Rect(100, 200, 300, 350), pixmap=pix)
        sp.insert_text((100, 400), "Texte dans le groupe", fontsize=12, fontname="helv")
        page.show_pdf_page(page.rect, src, 0)
    else:
        page.insert_image(pymupdf.Rect(100, 200, 300, 350), pixmap=pix)
    page.insert_text((72, 700), "Texte du bas", fontsize=12, fontname="helv")
    if cropbox:
        page.set_cropbox(pymupdf.Rect(*cropbox))
    if rotation:
        page.set_rotation(rotation)
    doc.save(path)
    return pymupdf.open(path)


def _bbox(page):
    infos = page.get_image_info()
    assert len(infos) == 1
    return pymupdf.Rect(infos[0]["bbox"])


@pytest.mark.parametrize("variant", [{}, {"rotation": 90}, {"cropbox": (50, 100, 545, 800)}, {"in_form": True}])
def test_move_scale_delete(tmp_path, variant):
    doc = _image_pdf(tmp_path / "img.pdf", **variant)
    page = doc[0]
    text_before = page.get_text()
    images = list_images(doc, page)
    assert len(images) == 1 and images[0].movable and not images[0].background
    before = _bbox(page)
    assert abs(images[0].bbox.x0 - before.x0) < 0.5 and abs(images[0].bbox.y0 - before.y0) < 0.5

    transform_image(doc, page, images[0], 50, 120)
    page = doc[0]
    moved = _bbox(page)
    assert abs(moved.x0 - (before.x0 + 50)) < 0.5 and abs(moved.y0 - (before.y0 + 120)) < 0.5
    assert abs(moved.width - before.width) < 0.5
    assert page.get_text() == text_before  # nothing else moved

    img = find_image(doc, page, list_images(doc, page)[0].id, None)
    transform_image(doc, page, img, 0, 0, 0.5)
    page = doc[0]
    half = _bbox(page)
    assert abs(half.width - before.width / 2) < 0.5 and abs(half.x0 - moved.x0) < 0.5 and abs(half.y0 - moved.y0) < 0.5

    delete_image(doc, page, list_images(doc, page)[0])
    page = doc[0]
    assert page.get_image_info() == [] and page.get_text() == text_before

    # The result survives a save / reload.
    reloaded = pymupdf.open("pdf", doc.tobytes(garbage=1))
    assert reloaded[0].get_image_info() == []
