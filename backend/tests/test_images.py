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


def _red_share(page, rect):
    """Share of clearly red pixels of the rendered page inside `rect`."""
    pix = page.get_pixmap(dpi=72, clip=rect)
    data = pix.samples
    n = pix.n
    red = total = 0
    for i in range(0, len(data) - n + 1, n * 3):
        total += 1
        r, g, b = data[i], data[i + 1], data[i + 2]
        if r > 150 and g < 90 and b < 90:
            red += 1
    return red / max(total, 1)


def _word_style_pdf(path, outer_clip=False):
    """Images drawn like Microsoft Word: q, clip rectangle, cm, Do, Q."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(0, 0, 10, 10), pixmap=pix)  # registers the image resource
    xref = page.get_images()[0][0]
    name = page.get_images(full=True)[0][7]
    prefix = "q 50 400 300 300 re W n " if outer_clip else ""
    suffix = " Q" if outer_clip else ""
    content = f"{prefix}q 100 500 200 150 re W* n 200 0 0 150 100 500 cm /{name} Do Q{suffix}"
    doc.update_stream(page.get_contents()[0], content.encode())
    doc.save(path)
    return pymupdf.open(path), xref


def test_word_clipped_image_moves_with_its_clip(tmp_path):
    doc, _ = _word_style_pdf(tmp_path / "word.pdf")
    page = doc[0]
    img = list_images(doc, page)[0]
    old = pymupdf.Rect(img.bbox)
    assert _red_share(page, old) > 0.9
    transform_image(doc, page, img, 150, -170)
    page = doc[0]
    new = old + (150, -170, 150, -170)
    assert (old & new).is_empty
    # Entirely visible at the new place (its clip moved too), gone from the old one.
    assert _red_share(page, new) > 0.9
    assert _red_share(page, old) < 0.05


def test_outer_clip_is_reported_as_bounds(tmp_path):
    doc, _ = _word_style_pdf(tmp_path / "cell.pdf", outer_clip=True)
    page = doc[0]
    img = list_images(doc, page)[0]
    assert img.bounds is not None
    assert abs(img.bounds.x0 - 50) < 0.5 and abs(img.bounds.y1 - (842 - 400)) < 0.5
