from fastapi.testclient import TestClient

from plume.app import app

client = TestClient(app)


def _upload(path):
    with open(path, "rb") as fh:
        r = client.post("/api/documents", files={"file": ("sample.pdf", fh, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow(sample_pdf):
    info = _upload(sample_pdf)
    doc_id = info["id"]
    assert len(info["pages"]) == 2 and info["version"] == 0

    r = client.get(f"/api/documents/{doc_id}/pages/0/image", params={"scale": 1})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"

    page = client.get(f"/api/documents/{doc_id}/pages/0/text").json()
    block = next(b for b in page["blocks"] if b["runs"][0]["text"].startswith("Rapport"))
    assert page["families"]

    hidden = client.get(f"/api/documents/{doc_id}/pages/0/image", params={"scale": 1, "hide": block["id"]})
    assert hidden.status_code == 200 and hidden.content != r.content

    css = client.get(f"/api/documents/{doc_id}/fonts.css", params={"families": block["runs"][0]["family"]})
    assert css.status_code == 200 and "@font-face" in css.text

    runs = [dict(block["runs"][0], text="Rapport annuel 2026")]
    box = {"x": block["x"], "y": block["baseline"], "width": block["width"], "wrap": False, "align": "left"}
    r = client.post(f"/api/documents/{doc_id}/pages/0/blocks/{block['id']}",
                    json={"version": 0, "sig": block["sig"], "runs": runs, "box": box})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["info"]["version"] == 1 and data["info"]["canUndo"]
    assert any(b["runs"][0]["text"].startswith("Rapport annuel 2026") for b in data["page"]["blocks"])

    # A stale version is refused.
    r = client.post(f"/api/documents/{doc_id}/pages/0/blocks/{block['id']}",
                    json={"version": 0, "sig": block["sig"], "runs": runs, "box": box})
    assert r.status_code == 409

    assert client.post(f"/api/documents/{doc_id}/undo").json()["version"] == 0
    page = client.get(f"/api/documents/{doc_id}/pages/0/text").json()
    assert any(b["runs"][0]["text"].startswith("Rapport annuel 2025") for b in page["blocks"])
    assert client.post(f"/api/documents/{doc_id}/redo").json()["version"] == 1

    pdf = client.get(f"/api/documents/{doc_id}/download")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert "sample.pdf" in pdf.headers["content-disposition"]


def test_rejects_non_pdf(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"hello")
    with open(p, "rb") as fh:
        r = client.post("/api/documents", files={"file": ("x.pdf", fh, "application/pdf")})
    assert r.status_code == 400
