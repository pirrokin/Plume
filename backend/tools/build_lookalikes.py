"""Build plume/data/lookalikes.bin: signatures of popular Google Fonts families.

Usage: python -m tools.build_lookalikes [number of families] [download cache dir]

For each family, a few weights are downloaded as tiny subsets (only the
letters that are compared), rendered, and their signatures stored. Plume uses
them to pick, for a font it cannot get, the open family that looks the most
like it, then downloads that family.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

from plume.fontnames import family_key
from plume.googlefonts import CSS_URL, METADATA_URL, _fetch
from plume.lookalike import DATA_PATH, SIG_CHARS, Candidate, font_signature, save_catalog

_FACE_RE = re.compile(
    r"@font-face\s*{[^}]*?font-style:\s*(\w+);[^}]*?font-weight:\s*(\d+);[^}]*?src:\s*url\(([^)]+)\)", re.S
)


def select_families(count: int) -> list[dict]:
    raw = _fetch(METADATA_URL, timeout=60).decode("utf-8")
    meta = json.loads(raw[raw.index("{"):])
    out = []
    for f in meta["familyMetadataList"]:
        name = f["family"]
        if "latin" not in f.get("subsets", []) or f.get("primaryScript") or f.get("isBrandFont"):
            continue
        if f.get("colorCapabilities") or re.search(r"icons|symbols|emoji|barcode", name, re.I):
            continue
        out.append(f)
    out.sort(key=lambda f: f.get("popularity") or 10**9)
    return out[:count]


def pick_styles(styles: list[str]) -> list[tuple[int, int]]:
    """Regular, bold, the extreme weights and the italic: enough to match any weight."""
    normal = sorted(int(s) for s in styles if s.isdigit())
    italic = sorted(int(s[:-1]) for s in styles if s.endswith("i") and s[:-1].isdigit())
    pick: set[tuple[int, int]] = set()
    if normal:
        pick.add((0, min(normal, key=lambda w: abs(w - 400))))
        bolds = [w for w in normal if w >= 600]
        if bolds:
            pick.add((0, min(bolds, key=lambda w: abs(w - 700))))
        if normal[-1] >= 800:
            pick.add((0, normal[-1]))
        if normal[0] <= 300:
            pick.add((0, normal[0]))
    if italic:
        pick.add((1, min(italic, key=lambda w: abs(w - 400))))
    return sorted(pick)


def download(family: dict, cache: Path) -> list[tuple[int, int, Path]]:
    key = family_key(family["family"])
    styles = pick_styles(list(family.get("fonts", {})))
    files = [(ital, w, cache / f"{key}-{w}{'i' if ital else ''}.ttf") for ital, w in styles]
    if all(p.exists() for *_, p in files):
        return files
    spec = ";".join(f"{i},{w}" for i, w in styles)
    url = f"{CSS_URL}?family={urllib.parse.quote(family['family'])}:ital,wght@{spec}&text={urllib.parse.quote(SIG_CHARS)}"
    try:
        css = _fetch(url).decode("utf-8")
    except Exception:
        spec = ";".join(str(w) for i, w in styles if not i)
        url = f"{CSS_URL}?family={urllib.parse.quote(family['family'])}:wght@{spec}&text={urllib.parse.quote(SIG_CHARS)}"
        css = _fetch(url).decode("utf-8")
    got = []
    for style, weight, font_url in _FACE_RE.findall(css):
        ital = 1 if style == "italic" else 0
        path = cache / f"{key}-{weight}{'i' if ital else ''}.ttf"
        if not path.exists():
            path.write_bytes(_fetch(font_url.strip("'\"")))
        got.append((ital, int(weight), path))
    return got


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    cache = Path(sys.argv[2] if len(sys.argv) > 2 else "data/cache/lookalike-build")
    cache.mkdir(parents=True, exist_ok=True)
    families = select_families(count)
    print(f"{len(families)} families")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda f: (f, _safe_download(f, cache)), families))
    faces: list[Candidate] = []
    for family, files in results:
        for ital, weight, path in files:
            try:
                sig = font_signature(pymupdf.Font(fontbuffer=path.read_bytes()))
            except Exception as exc:
                print("  skipped", path.name, exc)
                continue
            if len(sig.glyphs) < 40:
                continue
            faces.append(Candidate(family_key(family["family"]), family["family"], weight, bool(ital), sig,
                                   family.get("category", "")))
    save_catalog(DATA_PATH, faces)
    print(f"{len(faces)} faces -> {DATA_PATH} ({DATA_PATH.stat().st_size // 1024} KB)")


def _safe_download(family: dict, cache: Path) -> list:
    try:
        return download(family, cache)
    except Exception as exc:
        print("  not downloaded:", family["family"], exc)
        return []


if __name__ == "__main__":
    main()
