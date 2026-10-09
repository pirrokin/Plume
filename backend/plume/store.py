"""Documents of a browser session: upload, versions (undo/redo), rendering.

Nothing is kept: a document lives in a temporary folder while its tab is open
(the page sends a heartbeat) and is deleted when the tab closes, after a period
of inactivity, or after a maximum lifetime. Several server processes may share
the folder (o2switch/Passenger), so state is re-read from disk and changes are
serialised with a file lock.
"""

from __future__ import annotations

import contextlib
import fcntl
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
        self.downloaded: int | None = meta.get("downloaded")
        self.fonts = DocFonts(self.doc)
        self._models: dict[tuple[int, int], dict] = {}
        try:
            self.fonts.request_downloads()
        except Exception as exc:  # downloads are a nice-to-have
            log.info("font downloads not started: %s", exc)
        self._revs: tuple[int, list[str]] | None = None
        self._scanned: tuple[int, list[int]] | None = None

    # ---------------------------------------------------------------- state
    @property
    def version(self) -> int:
        return self.versions[self.cur]

    def _path(self, version: int) -> Path:
        return self.root / f"v{version}.pdf"

    def _write_meta(self) -> None:
        meta = {"name": self.name, "versions": self.versions, "cur": self.cur, "downloaded": self.downloaded}
        tmp = self.root / f"meta.json.{secrets.token_hex(4)}"
        tmp.write_text(json.dumps(meta))
        tmp.replace(self.root / "meta.json")

    def _reopen(self) -> None:
        self.doc.close()
        self.doc = pymupdf.open(self._path(self.version))
        self.fonts = DocFonts(self.doc)

    def sync(self) -> None:
        """Pick up changes made by another server process."""
        try:
            meta = json.loads((self.root / "meta.json").read_text())
        except (OSError, ValueError) as exc:
            raise DocumentError("Ce document a expiré. Rouvrez le fichier PDF.", 404) from exc
        self.downloaded = meta.get("downloaded")
        if meta["versions"] != self.versions or meta["cur"] != self.cur:
            self.versions, self.cur = meta["versions"], meta["cur"]
            self._reopen()

    @contextlib.contextmanager
    def locked(self):
        """Exclusive access across processes while the document changes."""
        with open(self.root / ".lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                self.sync()
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def page_revs(self) -> list[str]:
        """Fingerprint of each page; it changes only when the page itself changes."""
        if self._revs is None or self._revs[0] != self.version:
            revs = []
            for page in self.doc:
                h = hashlib.sha1(self.doc.xref_object(page.xref, compressed=True).encode())
                for cx in page.get_contents():
                    # Decompressed, so that re-compressing on save does not change it.
                    h.update(self.doc.xref_stream(cx) or b"")
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
            "unsaved": self.cur > 0 and self.downloaded != self.version,
            "pages": pages,
            "scanned": self.needs_ocr(),
            "expiresAfterMinutes": config.SESSION_IDLE_MINUTES,
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
        model = self.page_model(page_no)
        segments = model["internal"].get(hide)
        block = next((b for b in model["blocks"] if b["id"] == hide), None)
        if segments is None or block is None:
            raise DocumentError("Bloc introuvable", 404)
        from .editor import scan_cover
        from .writer import cover_rect, remove_chars

        tmp = pymupdf.open()
        tmp.insert_pdf(self.doc, from_page=page_no, to_page=page_no)
        if block.get("scan"):
            for rect in scan_cover(segments):
                cover_rect(tmp[0], rect, block.get("bg") or "#ffffff")
        else:
            remove_chars(tmp[0], [c for s in segments for c in s.chars])
        return tmp[0].get_pixmap(matrix=mat, alpha=False).tobytes("png")

    def ocr(self, page_no: int, version: int | None, languages: str | None = None) -> int:
        """Recognise the text of a scanned page; returns the number of words found."""
        from .ocr import DEFAULT_LANGUAGES, OcrError, recognize_page

        self._check_version(version)
        self._check_page(page_no)
        words = {"n": 0}

        def action():
            try:
                words["n"] = recognize_page(self.doc, self.fonts, page_no, languages or DEFAULT_LANGUAGES)
            except OcrError as exc:
                raise EditError(str(exc)) from exc
            return None

        self._commit(action)
        return words["n"]

    def needs_ocr(self) -> list[int]:
        """Pages that look scanned and have no text layer yet (cached per version)."""
        if self._scanned is None or self._scanned[0] != self.version:
            from .ocr import page_needs_ocr

            self._scanned = (self.version, [p.number for p in self.doc if page_needs_ocr(p)])
        return self._scanned[1]

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
        return segments, (block.get("bg") or "#ffffff") if block.get("scan") else None

    # ---------------------------------------------------------------- editing
    def _commit(self, action) -> EditReport:
        """Run `action` on the open document and save the result as a new version."""
        with self.locked():
            return self._commit_locked(action)

    def _commit_locked(self, action) -> EditReport:
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
        segments, paper = self._segments(page_no, block_id, sig)
        return self._commit(lambda: edit_block(self.doc, self.fonts, page_no, segments, runs, box, paper))

    def delete(self, page_no: int, block_id: str, version: int | None, sig: str | None) -> EditReport:
        self._check_version(version)
        segments, paper = self._segments(page_no, block_id, sig)
        return self._commit(lambda: delete_block(self.doc, self.fonts, page_no, segments, paper))

    def add(self, page_no: int, version: int | None, runs: list[dict], box: dict) -> EditReport:
        self._check_version(version)
        self._check_page(page_no)
        if not "".join(r.get("text", "") for r in runs).strip():
            raise DocumentError("Texte vide")
        return self._commit(lambda: add_block(self.doc, self.fonts, page_no, runs, box))

    # ---------------------------------------------------------------- pages
    def page_op(self, op: str, page_no: int, version: int | None, angle: int = 90, to: int | None = None) -> None:
        self._check_version(version)
        self._check_page(page_no)
        count = self.doc.page_count

        def action():
            doc = self.doc
            if op == "rotate":
                if angle % 90:
                    raise EditError("Angle de rotation invalide")
                page = doc[page_no]
                page.set_rotation((page.rotation + angle) % 360)
            elif op == "delete":
                if count == 1:
                    raise EditError("Impossible de supprimer l'unique page du document.")
                doc.delete_page(page_no)
            elif op == "duplicate":
                doc.fullcopy_page(page_no, page_no + 1 if page_no + 1 < count else -1)
            elif op == "move":
                if to is None or not 0 <= to < count:
                    raise EditError("Position invalide")
                if to != page_no:
                    # PyMuPDF puts the page before `to` (-1: at the end).
                    if to < page_no:
                        doc.move_page(page_no, to)
                    else:
                        doc.move_page(page_no, to + 1 if to + 1 < count else -1)
            elif op == "insert_blank":
                ref = doc[page_no].rect
                doc.new_page(pno=page_no + 1 if page_no + 1 < count else -1, width=ref.width, height=ref.height)
            else:
                raise EditError(f"Opération inconnue : {op}")

        self._commit(action)

    def merge(self, data: bytes, at: int | None, version: int | None) -> int:
        """Insert all pages of another PDF; returns the number of pages added."""
        self._check_version(version)
        try:
            other = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise DocumentError("Ce fichier n'est pas un PDF valide.") from exc
        if other.needs_pass:
            raise DocumentError("Ce PDF est protégé par un mot de passe.")
        added = other.page_count
        start = self.doc.page_count if at is None else max(0, min(at, self.doc.page_count))
        self._commit(lambda: self.doc.insert_pdf(other, start_at=start))
        return added

    def undo(self) -> None:
        with self.locked():
            if self.cur == 0:
                raise DocumentError("Rien à annuler")
            self.cur -= 1
            self._write_meta()
            self._reopen()

    def redo(self) -> None:
        with self.locked():
            if self.cur >= len(self.versions) - 1:
                raise DocumentError("Rien à rétablir")
            self.cur += 1
            self._write_meta()
            self._reopen()

    def export(self) -> bytes:
        with self.locked():
            self.downloaded = self.version
            self._write_meta()
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
        self._last_sweep = 0.0

    def create(self, data: bytes, name: str) -> Document:
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise DocumentError("Ce fichier n'est pas un PDF valide.") from exc
        if doc.needs_pass:
            raise DocumentError("Ce PDF est protégé par un mot de passe.")
        if doc.page_count == 0:
            raise DocumentError("Ce PDF ne contient aucune page.")
        if doc.page_count > config.MAX_PAGES:
            raise DocumentError(f"Ce PDF a trop de pages (maximum {config.MAX_PAGES}).")
        self.sweep()
        doc_id = secrets.token_urlsafe(16)
        root = self.root / doc_id
        root.mkdir(parents=True)
        if doc.is_repaired or doc.is_encrypted:
            doc.save(root / "v0.pdf", garbage=1, deflate=True)
        else:
            (root / "v0.pdf").write_bytes(data)
        doc.close()
        meta = {"name": name or "document.pdf", "versions": [0], "cur": 0, "created": time.time()}
        (root / "meta.json").write_text(json.dumps(meta))
        (root / "created").touch()
        (root / "alive").touch()
        return self.get(doc_id)

    def _root_of(self, doc_id: str) -> Path:
        if not doc_id or len(doc_id) > 64 or not all(c.isalnum() or c in "-_" for c in doc_id):
            raise DocumentError("Document introuvable", 404)
        return self.root / doc_id

    def get(self, doc_id: str) -> Document:
        root = self._root_of(doc_id)
        if not (root / "meta.json").exists():
            self._forget(doc_id)
            raise DocumentError("Ce document a expiré. Rouvrez le fichier PDF.", 404)
        doc = self._open.get(doc_id)
        if doc is not None:
            self._open.move_to_end(doc_id)
            doc.sync()
        else:
            doc = Document(doc_id, root)
            self._open[doc_id] = doc
            while len(self._open) > self.max_open:
                _, old = self._open.popitem(last=False)
                old.doc.close()
        self.touch(doc_id)
        return doc

    # ---------------------------------------------------------------- lifetime
    def touch(self, doc_id: str) -> None:
        """The tab is still open: postpone expiry (and cancel a pending close)."""
        root = self._root_of(doc_id)
        try:
            (root / "alive").touch()
            (root / "closing").unlink(missing_ok=True)
        except OSError:
            pass

    def ping(self, doc_id: str) -> bool:
        root = self._root_of(doc_id)
        if not (root / "meta.json").exists():
            return False
        self.touch(doc_id)
        return True

    def close_soon(self, doc_id: str) -> None:
        """The tab is going away; delete after a short grace period unless it comes back (reload)."""
        root = self._root_of(doc_id)
        if root.exists():
            try:
                (root / "closing").touch()
            except OSError:
                pass

    def delete(self, doc_id: str) -> None:
        root = self._root_of(doc_id)
        self._forget(doc_id)
        shutil.rmtree(root, ignore_errors=True)

    def _forget(self, doc_id: str) -> None:
        doc = self._open.pop(doc_id, None)
        if doc is not None:
            try:
                doc.doc.close()
            except Exception:
                pass

    def sweep(self, force: bool = False) -> int:
        """Delete expired sessions; cheap enough to call on every upload."""
        now = time.time()
        if not force and now - self._last_sweep < 30:
            return 0
        self._last_sweep = now
        removed = 0
        idle = config.SESSION_IDLE_MINUTES * 60
        grace = config.SESSION_CLOSE_GRACE_SECONDS
        lifetime = config.SESSION_MAX_HOURS * 3600
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            try:
                alive = (d / "alive").stat().st_mtime
            except OSError:
                alive = 0
            try:
                created = (d / "created").stat().st_mtime
            except OSError:
                created = alive
            closing = None
            try:
                closing = (d / "closing").stat().st_mtime
            except OSError:
                pass
            expired = now - alive > idle or now - created > lifetime
            if closing is not None and closing >= alive and now - closing > grace:
                expired = True
            if expired:
                self._forget(d.name)
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed

    def cleanup(self, max_age_hours: float | None = None) -> int:
        return self.sweep(force=True)
