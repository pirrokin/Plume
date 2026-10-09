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


def test_page_operations(sample_pdf):
    info = _upload(sample_pdf)
    doc_id = info["id"]
    first_rev, second_rev = info["pages"][0]["rev"], info["pages"][1]["rev"]

    def op(page, **body):
        r = client.post(f"/api/documents/{doc_id}/pages/{page}/op", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    info = op(0, version=info["version"], op="rotate", angle=90)
    assert info["pages"][0]["rotation"] == 90 and info["pages"][1]["rev"] == second_rev
    info = op(0, version=info["version"], op="duplicate")
    assert len(info["pages"]) == 3
    info = op(0, version=info["version"], op="move", to=2)
    assert info["pages"][2]["rotation"] == 90 and info["pages"][0]["rotation"] == 90
    info = op(1, version=info["version"], op="delete")
    assert len(info["pages"]) == 2
    info = op(0, version=info["version"], op="insert_blank")
    assert len(info["pages"]) == 3
    with open(sample_pdf, "rb") as fh:
        r = client.post(f"/api/documents/{doc_id}/merge", params={"version": info["version"]},
                        files={"file": ("other.pdf", fh, "application/pdf")})
    assert r.status_code == 200 and len(r.json()["pages"]) == 5
    assert client.post(f"/api/documents/{doc_id}/undo").json()["pages"].__len__() == 3
