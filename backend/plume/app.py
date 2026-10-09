"""HTTP API and static frontend."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config
from .fontindex import get_index
from .fontnames import generic_class
from .googlefonts import google_fonts
from .store import MUPDF_LOCK, DocumentError, Store

log = logging.getLogger("plume")

config.ensure_dirs()
store = Store()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    removed = store.cleanup(config.RETENTION_HOURS)
    if removed:
        log.info("removed %d old documents", removed)
    # Build the font index in the background so that the first edit is fast.
    threading.Thread(target=_warm_up, daemon=True).start()
    yield


app = FastAPI(title="Plume", version=__version__, lifespan=lifespan)


def _warm_up() -> None:
    index = get_index()
    gf = google_fonts()
    gf.catalog(block=True)
    # Servers without office fonts get metric-compatible open fonts.
    for generic in ("sans", "serif", "mono"):
        if index.generic_key(generic) is None:
            for key in {"sans": ["arimo"], "serif": ["tinos"], "mono": ["cousine"]}[generic]:
                gf.ensure(key)


@app.exception_handler(DocumentError)
def _document_error(_request: Request, exc: DocumentError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------- models
class Run(BaseModel):
    text: str
    family: str = "arial"
    weight: int = 400
    italic: bool = False
    size: float = 12.0
    color: str = "#000000"
    alpha: float = 1.0
    underline: bool = False
    strike: bool = False
    rise: float = 0.0
    spacing: float = 0.0
    render: str = "fill"


class Box(BaseModel):
    x: float
    y: float
    width: float = Field(gt=0)
    wrap: bool = False
    align: str = "left"
    lineHeight: float | None = None
    indent: float = 0.0
    size: float | None = None


class EditBody(BaseModel):
    version: int | None = None
    sig: str | None = None
    runs: list[Run]
    box: Box


class AddBody(BaseModel):
    version: int | None = None
    runs: list[Run]
    box: Box


# ---------------------------------------------------------------- render cache
class _PngCache:
    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.size = 0
        self.items: OrderedDict[tuple, bytes] = OrderedDict()

    def get(self, key: tuple) -> bytes | None:
        data = self.items.get(key)
        if data is not None:
            self.items.move_to_end(key)
        return data

    def put(self, key: tuple, data: bytes) -> None:
        self.items[key] = data
        self.size += len(data)
        while self.size > self.max_bytes and self.items:
            _, old = self.items.popitem(last=False)
            self.size -= len(old)


png_cache = _PngCache()


def _report(report: Any) -> dict:
    return {
        "keptLines": report.kept_lines,
        "rewrittenLines": report.rewritten_lines,
        "removedChars": report.removed_chars,
        "restoredChars": report.restored_chars,
        "bbox": list(report.bbox) if report.bbox else None,
    }


# ---------------------------------------------------------------- documents
@app.post("/api/documents")
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo).")
    with MUPDF_LOCK:
        doc = store.create(data, file.filename or "document.pdf")
        return doc.info()


@app.get("/api/documents/{doc_id}")
def document_info(doc_id: str) -> dict:
    with MUPDF_LOCK:
        return store.get(doc_id).info()


@app.get("/api/documents/{doc_id}/pages/{page_no}/image")
def page_image(
    doc_id: str,
    page_no: int,
    scale: float = Query(1.5, gt=0.05, le=8),
    rev: str | None = None,
    hide: str | None = None,
) -> Response:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc._check_page(page_no)
        current = doc.page_revs()[page_no]
        # Block ids are only stable within a version.
        key = (doc_id, page_no, current, round(scale, 3), hide, doc.version if hide else None)
        data = png_cache.get(key)
        if data is None:
            data = doc.render(page_no, scale, hide)
            png_cache.put(key, data)
    immutable = rev == current and not hide
    headers = {"Cache-Control": "private, max-age=31536000, immutable" if immutable else "no-cache"}
    return Response(data, media_type="image/png", headers=headers)


@app.get("/api/documents/{doc_id}/pages/{page_no}/text")
def page_text(doc_id: str, page_no: int) -> dict:
    with MUPDF_LOCK:
        return store.get(doc_id).public_model(page_no)


@app.post("/api/documents/{doc_id}/pages/{page_no}/blocks/{block_id}")
def edit(doc_id: str, page_no: int, block_id: str, body: EditBody) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        report = doc.edit(page_no, block_id, body.version, body.sig,
                          [r.model_dump() for r in body.runs], body.box.model_dump())
        return {"info": doc.info(), "page": doc.public_model(page_no), "report": _report(report)}


@app.delete("/api/documents/{doc_id}/pages/{page_no}/blocks/{block_id}")
def delete(doc_id: str, page_no: int, block_id: str, version: int | None = None, sig: str | None = None) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        report = doc.delete(page_no, block_id, version, sig)
        return {"info": doc.info(), "page": doc.public_model(page_no), "report": _report(report)}


@app.post("/api/documents/{doc_id}/pages/{page_no}/blocks")
def add(doc_id: str, page_no: int, body: AddBody) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        report = doc.add(page_no, body.version, [r.model_dump() for r in body.runs], body.box.model_dump())
        return {"info": doc.info(), "page": doc.public_model(page_no), "report": _report(report)}


class OcrBody(BaseModel):
    version: int | None = None
    languages: str | None = None


@app.post("/api/documents/{doc_id}/pages/{page_no}/ocr")
def ocr(doc_id: str, page_no: int, body: OcrBody) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        words = doc.ocr(page_no, body.version, body.languages)
        return {"info": doc.info(), "page": doc.public_model(page_no), "words": words}


class PageOpBody(BaseModel):
    version: int | None = None
    op: str
    angle: int = 90
    to: int | None = None


@app.post("/api/documents/{doc_id}/pages/{page_no}/op")
def page_op(doc_id: str, page_no: int, body: PageOpBody) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.page_op(body.op, page_no, body.version, body.angle, body.to)
        return doc.info()


@app.post("/api/documents/{doc_id}/merge")
async def merge(doc_id: str, file: UploadFile = File(...), at: int | None = None, version: int | None = None) -> dict:
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo).")
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.merge(data, at, version)
        return doc.info()


@app.post("/api/documents/{doc_id}/undo")
def undo(doc_id: str) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.undo()
        return doc.info()


@app.post("/api/documents/{doc_id}/redo")
def redo(doc_id: str) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.redo()
        return doc.info()


@app.get("/api/documents/{doc_id}/download")
def download(doc_id: str) -> Response:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        data = doc.export()
        name = doc.name if doc.name.lower().endswith(".pdf") else doc.name + ".pdf"
    disposition = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": disposition})


# ---------------------------------------------------------------- fonts
@app.get("/api/documents/{doc_id}/fonts.css")
def document_fonts_css(doc_id: str, families: str = "") -> Response:
    keys = [k for k in families.split(",") if k]
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        css = doc.fonts.css(f"/api/documents/{doc_id}", keys)
    return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/api/documents/{doc_id}/fonts/emb/{xref}")
def embedded_font(doc_id: str, xref: int) -> Response:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        pf = doc.fonts.prepared(xref)
        if not pf.usable or not pf.web_ok:
            raise HTTPException(404, "Police non disponible")
        data = pf.buffer
        mime = pf.web_mime
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/fonts")
def fonts() -> list[dict]:
    return [dict(f, generic=generic_class(f["name"])) for f in get_index().families()]


@app.get("/api/fonts/sys/{face_id}")
def system_font(face_id: str) -> Response:
    index = get_index()
    face = index.face(face_id)
    if face is None:
        raise HTTPException(404, "Police inconnue")
    data = index.face_bytes(face_id)
    mime = "font/otf" if face.cff else "font/ttf"
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


# ---------------------------------------------------------------- frontend
if config.FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        target = (config.FRONTEND_DIST / path).resolve()
        if path and target.is_file() and config.FRONTEND_DIST in target.parents:
            return FileResponse(target)
        return FileResponse(config.FRONTEND_DIST / "index.html")
