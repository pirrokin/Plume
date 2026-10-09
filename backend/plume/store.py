"""Documents on disk: upload, versions (undo/redo), page models and rendering."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pymupdf

from . import config
from .docfonts import DocFonts
from .editor import EditError, EditReport, add_block, delete_block, edit_block
from .extract import extract_page

log = logging.getLogger(__name__)

# MuPDF is not thread-safe: every call into it goes through this lock.
MUPDF_LOCK = threading.RLock()


class DocumentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Document:
    def __init__(self, doc_id: str, root: Path):
        self.id = doc_id
        self.root = root
        meta = json.loads((root / "meta.json").read_text())
        self.name: str = meta["name"]
        self.versions: list[int] = meta["versions"]
        self.cur: int = meta["cur"]
        self.doc: pymupdf.Document = pymupdf.open(self._path(self.version))
        self.fonts = DocFonts(self.doc)
        self._models: dict[tuple[int, int], dict] = {}
        try:
            self.fonts.request_downloads()
        except Exception as exc:  # downloads are a nice-to-have
            log.info("font downloads not started: %s", exc)
        self._revs: tuple[int, list[str]] | None = None

    # ---------------------------------------------------------------- state
    @property
    def version(self) -> int:
        return self.versions[self.cur]

    def _path(self, version: int) -> Path:
        return self.root / f"v{version}.pdf"

    def _write_meta(self) -> None:
        meta = {"name": self.name, "versions": self.versions, "cur": self.cur, "touched": time.time()}
        tmp = self.root / "meta.json.tmp"
        tmp.write_text(json.dumps(meta))
        tmp.replace(self.root / "meta.json")

    def _reopen(self) -> None:
        self.doc.close()
        self.doc = pymupdf.open(self._path(self.version))
        self.fonts = DocFonts(self.doc)

    def page_revs(self) -> list[str]:
        """Fingerprint of each page; it changes only when the page itself changes."""
        if self._revs is None or self._revs[0] != self.version:
            revs = []
            for page in self.doc:
                h = hashlib.sha1(self.doc.xref_object(page.xref, compressed=True).encode())
                for cx in page.get_contents():
                    h.update(self.doc.xref_stream_raw(cx) or b"")
                typ, val = self.doc.xref_get_key(page.xref, "Resources")
                if typ == "xref":
                    h.update(self.doc.xref_object(int(val.split()[0]), compressed=True).encode())
                revs.append(h.hexdigest()[:12])
            self._revs = (self.version, revs)
        return self._revs[1]

    def info(self) -> dict:
        revs = self.page_revs()
        pages = []
        for page in self.doc:
            unrot = page.rect * page.derotation_matrix
            m = page.rotation_matrix
            pages.append({
                "index": page.number,
                "width": page.rect.width,
                "height": page.rect.height,
                "rotation": page.rotation,
                "unrotated": [abs(unrot.width), abs(unrot.height)],
                "matrix": [m.a, m.b, m.c, m.d, m.e, m.f],
                "rev": revs[page.number],
            })
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "canUndo": self.cur > 0,
            "canRedo": self.cur < len(self.versions) - 1,
            "pages": pages,
        }

    # ---------------------------------------------------------------- reading
    def page_model(self, page_no: int) -> dict:
        self._check_page(page_no)
        key = (self.version, page_no)
        model = self._models.get(key)
        if model is None:
            model = extract_page(self.doc[page_no], self.fonts)
            if len(self._models) > 64:
                self._models.clear()
            self._models[key] = model
        return model

    def public_model(self, page_no: int) -> dict:
        model = self.page_model(page_no)
        families = sorted({r["family"] for b in model["blocks"] for r in b["runs"]})
        return {
            "page": page_no,
            "version": self.version,
            "rev": self.page_revs()[page_no],
            "blocks": model["blocks"],
            "others": model["others"],
            "families": [self.fonts.family(f).describe() for f in families],
        }

    def render(self, page_no: int, scale: float, hide: str | None = None) -> bytes:
        self._check_page(page_no)
        mat = pymupdf.Matrix(scale, scale)
        if not hide:
            return self.doc[page_no].get_pixmap(matrix=mat, alpha=False).tobytes("png")
        segments = self.page_model(page_no)["internal"].get(hide)
        if segments is None:
            raise DocumentError("Bloc introuvable", 404)
        from .writer import remove_chars

        tmp = pymupdf.open()
        tmp.insert_pdf(self.doc, from_page=page_no, to_page=page_no)
        remove_chars(tmp[0], [c for s in segments for c in s.chars])
        return tmp[0].get_pixmap(matrix=mat, alpha=False).tobytes("png")

    def _check_page(self, page_no: int) -> None:
        if not 0 <= page_no < self.doc.page_count:
            raise DocumentError("Page inexistante", 404)

    def _check_version(self, version: int | None) -> None:
        if version is not None and version != self.version:
            raise DocumentError("Le document a changé entre-temps, rechargez la page.", 409)

    def _segments(self, page_no: int, block_id: str, sig: str | None):
        model = self.page_model(page_no)
        segments = model["internal"].get(block_id)
        block = next((b for b in model["blocks"] if b["id"] == block_id), None)
        if segments is None or block is None:
            raise DocumentError("Bloc introuvable", 404)
        if sig and block["sig"] != sig:
            raise DocumentError("Le bloc a changé entre-temps, rechargez la page.", 409)
        if not block["editable"]:
            raise DocumentError("Ce bloc ne peut pas être modifié : " + " ".join(block["issues"]))
        return segments

    # ---------------------------------------------------------------- editing
    def _commit(self, action) -> EditReport:
        """Run `action` on the open document and save the result as a new version."""
        try:
            report = action()
        except EditError as exc:
            self._reopen()
            raise DocumentError(str(exc)) from exc
        except Exception:
            self._reopen()
            raise
        new_version = max(self.versions) + 1
        self.doc.save(self._path(new_version), garbage=1, deflate=True)
        # Drop the redo branch.
        for v in self.versions[self.cur + 1:]:
            self._path(v).unlink(missing_ok=True)
        self.versions = self.versions[: self.cur + 1] + [new_version]
        self.cur = len(self.versions) - 1
        while len(self.versions) > config.MAX_VERSIONS:
            old = self.versions.pop(0)
            self._path(old).unlink(missing_ok=True)
            self.cur -= 1
        self._write_meta()
        self._reopen()
        return report

    def edit(self, page_no: int, block_id: str, version: int | None, sig: str | None, runs: list[dict], box: dict) -> EditReport:
        self._check_version(version)
        segments = self._segments(page_no, block_id, sig)
        return self._commit(lambda: edit_block(self.doc, self.fonts, page_no, segments, runs, box))

    def delete(self, page_no: int, block_id: str, version: int | None, sig: str | None) -> EditReport:
        self._check_version(version)
        segments = self._segments(page_no, block_id, sig)
        return self._commit(lambda: delete_block(self.doc, self.fonts, page_no, segments))

    def add(self, page_no: int, version: int | None, runs: list[dict], box: dict) -> EditReport:
        self._check_version(version)
        self._check_page(page_no)
        if not "".join(r.get("text", "") for r in runs).strip():
            raise DocumentError("Texte vide")
        return self._commit(lambda: add_block(self.doc, self.fonts, page_no, runs, box))

    def undo(self) -> None:
        if self.cur == 0:
            raise DocumentError("Rien à annuler")
        self.cur -= 1
        self._write_meta()
        self._reopen()

    def redo(self) -> None:
        if self.cur >= len(self.versions) - 1:
            raise DocumentError("Rien à rétablir")
        self.cur += 1
        self._write_meta()
        self._reopen()

    def export(self) -> bytes:
        out = pymupdf.open(self._path(self.version))
        try:
            out.subset_fonts()
        except Exception as exc:  # subsetting is an optimisation only
            log.warning("font subsetting failed: %s", exc)
            out = pymupdf.open(self._path(self.version))
        return out.tobytes(garbage=3, deflate=True, use_objstms=1)


class Store:
    def __init__(self, root: Path | None = None, max_open: int = 6):
        self.root = root or config.DOCS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self._open: OrderedDict[str, Document] = OrderedDict()
        self.max_open = max_open

    def create(self, data: bytes, name: str) -> Document:
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise DocumentError("Ce fichier n'est pas un PDF valide.") from exc
        if doc.needs_pass:
            raise DocumentError("Ce PDF est protégé par un mot de passe.")
        if doc.page_count == 0:
            raise DocumentError("Ce PDF ne contient aucune page.")
        doc_id = secrets.token_urlsafe(12)
        root = self.root / doc_id
        root.mkdir(parents=True)
        if doc.is_repaired or doc.is_encrypted:
            doc.save(root / "v0.pdf", garbage=1, deflate=True)
        else:
            (root / "v0.pdf").write_bytes(data)
        doc.close()
        meta = {"name": name or "document.pdf", "versions": [0], "cur": 0, "touched": time.time()}
        (root / "meta.json").write_text(json.dumps(meta))
        return self.get(doc_id)

    def get(self, doc_id: str) -> Document:
        if not doc_id or not all(c.isalnum() or c in "-_" for c in doc_id):
            raise DocumentError("Document introuvable", 404)
        doc = self._open.get(doc_id)
        if doc is not None:
            self._open.move_to_end(doc_id)
            return doc
        root = self.root / doc_id
        if not (root / "meta.json").exists():
            raise DocumentError("Document introuvable", 404)
        doc = Document(doc_id, root)
        self._open[doc_id] = doc
        while len(self._open) > self.max_open:
            _, old = self._open.popitem(last=False)
            old.doc.close()
        return doc

    def cleanup(self, max_age_hours: float) -> int:
        removed = 0
        limit = time.time() - max_age_hours * 3600
        for d in self.root.iterdir():
            meta = d / "meta.json"
            try:
                touched = json.loads(meta.read_text()).get("touched", 0)
            except Exception:
                touched = 0
            if touched < limit and d.name not in self._open:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed
