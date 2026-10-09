"""HTTP API and static frontend."""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, config
from . import lookalike
from .fontindex import get_index
from .fontnames import generic_class
from .googlefonts import google_fonts
from .store import MUPDF_LOCK, DocumentError, Store

log = logging.getLogger("plume")

config.ensure_dirs()
store = Store()


_started = threading.Event()
_start_lock = threading.Lock()


def _ensure_started() -> None:
    """Startup work, run once per process (lifespan events do not exist under WSGI)."""
    if _started.is_set():
        return
    with _start_lock:
        if _started.is_set():
            return
        _started.set()
        removed = store.sweep(force=True)
        if removed:
            log.info("removed %d expired documents", removed)
        # Build the font index in the background so that the first edit is fast.
        threading.Thread(target=_warm_up, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _ensure_started()
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
    # Signatures of the installed fonts that can stand in for missing ones.
    with MUPDF_LOCK:
        lookalike.candidates(index)


@app.exception_handler(DocumentError)
def _document_error(_request: Request, exc: DocumentError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(Exception)
def _unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    # Never answer 5xx: o2switch's front server replays such requests, which
    # could apply an edit twice. The browser just shows the message.
    log.exception("unexpected error")
    return JSONResponse({"detail": f"Erreur inattendue ({type(exc).__name__}). Réessayez."}, status_code=422)


@app.middleware("http")
async def _require_session(request: Request, call_next):
    _ensure_started()
    store.sweep()
    path = request.url.path
    if auth.enabled() and path.startswith("/api/") and path not in auth.PUBLIC_API:
        if not auth.valid(request.cookies.get(auth.COOKIE)):
            return JSONResponse({"detail": "Connexion requise"}, status_code=401)
    response = await call_next(request)
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return response


class LoginBody(BaseModel):
    password: str


@app.get("/api/session")
def session(request: Request) -> dict:
    return {"required": auth.enabled(), "authenticated": auth.valid(request.cookies.get(auth.COOKIE))}


@app.post("/api/login")
def login(body: LoginBody, request: Request) -> JSONResponse:
    if not auth.enabled():
        return JSONResponse({"authenticated": True})
    client = request.client.host if request.client else "?"
    try:
        ok = auth.check_password(body.password, client)
    except PermissionError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=429)
    if not ok:
        return JSONResponse({"detail": "Mot de passe incorrect"}, status_code=401)
    response = JSONResponse({"authenticated": True})
    secure = request.url.scheme == "https" or config.COOKIE_SECURE
    response.set_cookie(
        auth.COOKIE, auth.issue(), max_age=int(auth.SESSION_DAYS * 86400),
        httponly=True, samesite="lax", secure=secure, path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


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

    def forget(self, doc_id: str) -> None:
        for key in [k for k in self.items if k[0] == doc_id]:
            self.size -= len(self.items.pop(key))

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
async def _read_pdf(request: Request, name: str) -> tuple[bytes, str]:
    """PDF sent as the raw request body (multipart is still accepted for local use).

    Raw bodies are preferred: o2switch's web application firewall rejects
    multipart file uploads.
    """
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 4096:
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo).")
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(400, "Aucun fichier reçu.")
        data = await upload.read()
        name = upload.filename or name
    else:
        data = await request.body()
    if len(data) > limit:
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo).")
    if not data:
        raise HTTPException(400, "Aucun fichier reçu.")
    return data, (name or "document.pdf")[:200]


@app.post("/api/documents")
async def upload(request: Request, name: str = "document.pdf") -> dict:
    data, filename = await _read_pdf(request, name)
    with MUPDF_LOCK:
        doc = store.create(data, filename)
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
    hideImage: str | None = None,
) -> Response:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc._check_page(page_no)
        current = doc.page_revs()[page_no]
        # Block and image ids are only stable within a version.
        hidden = hide or hideImage
        key = (doc_id, page_no, current, round(scale, 3), hide, hideImage, doc.version if hidden else None)
        data = png_cache.get(key)
        if data is None:
            data = doc.render(page_no, scale, hide, hideImage)
            png_cache.put(key, data)
    immutable = rev == current and not hidden
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


class ImageBody(BaseModel):
    version: int | None = None
    sig: str | None = None
    action: str = "move"
    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0


@app.post("/api/documents/{doc_id}/pages/{page_no}/images/{image_id}")
def image_op(doc_id: str, page_no: int, image_id: str, body: ImageBody) -> dict:
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.image_op(page_no, image_id, body.version, body.sig, body.action, body.dx, body.dy, body.scale)
        return {"info": doc.info(), "page": doc.public_model(page_no)}


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
async def merge(doc_id: str, request: Request, at: int | None = None, version: int | None = None,
                name: str = "document.pdf") -> dict:
    data, _ = await _read_pdf(request, name)
    with MUPDF_LOCK:
        doc = store.get(doc_id)
        doc.merge(data, at, version)
        return doc.info()


@app.post("/api/documents/{doc_id}/ping")
def ping(doc_id: str) -> dict:
    """Heartbeat of an open tab: keeps the document alive."""
    return {"alive": store.ping(doc_id)}


@app.post("/api/documents/{doc_id}/close")
def close(doc_id: str) -> Response:
    """Sent by the browser when the tab goes away (reloads cancel it)."""
    store.close_soon(doc_id)
    return Response(status_code=204)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str) -> Response:
    with MUPDF_LOCK:
        store.delete(doc_id)
        png_cache.forget(doc_id)
    return Response(status_code=204)


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


@app.get("/robots.txt", include_in_schema=False)
def robots() -> Response:
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


# ---------------------------------------------------------------- frontend
if config.FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=config.FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse({"detail": "Introuvable"}, status_code=404)
        error = re.fullmatch(r"(\d{3})\.shtml", path)
        if error:
            # The web server replaced the request with its error page (e.g. the
            # hosting firewall answered 406): report it instead of the app.
            code = int(error.group(1))
            return JSONResponse({"detail": f"Requête refusée par l'hébergeur (erreur {code})."},
                                status_code=code if 400 <= code < 500 else 422)
        target = (config.FRONTEND_DIST / path).resolve()
        if path and target.is_file() and config.FRONTEND_DIST in target.parents:
            return FileResponse(target)
        return FileResponse(config.FRONTEND_DIST / "index.html")
