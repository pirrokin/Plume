"""Parsing of PDF/PostScript font names into family, weight and style."""

from __future__ import annotations

import re

SUBSET_RE = re.compile(r"^[A-Z]{6}\+")

# Longest tokens first so that "semibold" is not read as "bold".
_WEIGHT_TOKENS: list[tuple[str, int]] = [
    ("extralight", 200), ("ultralight", 200), ("extrabold", 800), ("ultrabold", 800),
    ("semibold", 600), ("demibold", 600), ("hairline", 100), ("regular", 400),
    ("medium", 500), ("normal", 400), ("light", 300), ("heavy", 800), ("black", 900),
    ("thin", 100), ("book", 400), ("roman", 400), ("bold", 700), ("demi", 600),
]
_ITALIC_TOKENS = ("italic", "oblique", "inclined", "kursiv", "slanted")

# Words that end a PostScript family name without being part of the family.
_FAMILY_SUFFIXES = ("psmt", "mt", "ps")

_SERIF_HINTS = (
    "times", "serif", "roman", "georgia", "garamond", "cambria", "palatino", "baskerville",
    "bodoni", "didot", "caslon", "minion", "schoolbook", "century", "charter", "bookman",
    "antiqua", "merriweather", "lora", "playfair", "cochin", "hoefler", "constantia",
    "rockwell", "courier", "cmr", "lmroman", "tinos", "caladea", "gelasio", "athelas", "iowan",
    "newyork", "bigcaslon", "plantagenet", "sabon", "utopia", "pagella", "termes", "nimbusrom",
)
_MONO_HINTS = ("mono", "courier", "consol", "menlo", "code", "typewriter", "cousine", "inconsolata", "cmtt", "lmmono")


def strip_subset(name: str) -> str:
    return SUBSET_RE.sub("", name or "").strip()


def family_key(name: str) -> str:
    """Normalised key used to compare family names coming from different sources."""
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for suffix in _FAMILY_SUFFIXES:
        if key.endswith(suffix) and len(key) - len(suffix) >= 3:
            return key[: -len(suffix)]
    return key


def _style_info(style: str) -> tuple[int | None, bool, bool]:
    """Return (weight, italic, recognised) for the style part of a font name."""
    s = re.sub(r"[^a-z]", "", style.lower())
    weight = None
    recognised = False
    for token, w in _WEIGHT_TOKENS:
        if token in s:
            if weight is None:
                weight = w
            s = s.replace(token, "")
            recognised = True
    italic = False
    for token in _ITALIC_TOKENS:
        if token in s:
            italic = True
            recognised = True
            s = s.replace(token, "")
    # Adobe abbreviations: "It", "BoldIt", "LightIt".
    if s in ("it", "itmt", "itps") or s.endswith("it") and len(s) <= 3:
        italic = True
        recognised = True
        s = ""
    if s in ("", "mt", "psmt", "ps", "std", "pro", "lt"):
        recognised = recognised or s in ("mt", "psmt", "ps")
    return weight, italic, recognised


def split_font_name(name: str) -> tuple[str, str]:
    """Split "TimesNewRomanPS-BoldItalicMT" into ("TimesNewRomanPS", "BoldItalicMT")."""
    n = strip_subset(name)
    if "," in n:
        base, _, style = n.partition(",")
        return base, style
    if "-" in n:
        base, _, style = n.rpartition("-")
        if _style_info(style)[2]:
            return base, style
    # CamelCase name with the style glued at the end, e.g. "ArialBold".
    m = re.match(r"^(.+?)((?:Extra|Ultra|Semi|Demi)?(?:Bold|Light|Medium|Black|Heavy|Thin|Regular)?(?:Italic|Oblique)?)$", n)
    if m and m.group(2) and len(m.group(1)) >= 3:
        return m.group(1), m.group(2)
    return n, ""


def pretty_family(base: str) -> str:
    """"TimesNewRomanPS" -> "Times New Roman"."""
    b = base
    for suffix in ("PSMT", "MT", "PS"):
        if b.endswith(suffix) and len(b) - len(suffix) >= 3:
            b = b[: -len(suffix)]
            break
    b = b.replace("_", " ")
    b = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", b)
    return re.sub(r"\s+", " ", b).strip() or base


def parse_font_name(name: str, flags: int = 0) -> dict:
    """Describe a PDF font name.

    `flags` are MuPDF span flags (2 = italic, 4 = serif, 8 = monospaced, 16 = bold).
    """
    base, style = split_font_name(name)
    weight, italic, _ = _style_info(style)
    if weight is None:
        weight = 700 if flags & 16 else 400
    if flags & 2:
        italic = True
    if not base:
        base = "Unknown"
    return {
        "key": family_key(base),
        "name": pretty_family(base),
        "weight": weight,
        "italic": italic,
        "generic": generic_class(name, flags),
    }


def generic_class(name: str, flags: int = 0) -> str:
    """Classify a font as 'sans', 'serif' or 'mono'."""
    n = re.sub(r"[^a-z]", "", strip_subset(name).lower())
    if flags & 8 or any(h in n for h in _MONO_HINTS):
        return "mono"
    if "sans" in n or "gothic" in n or "grotesk" in n:
        return "sans"
    if any(h in n for h in _SERIF_HINTS):
        return "serif"
    if n:
        return "sans"
    return "serif" if flags & 4 else "sans"


def css_weight_order(desired: int, available: list[int]) -> list[int]:
    """Order available weights following the CSS font matching algorithm."""
    avail = sorted(set(available))
    if not avail:
        return []
    if desired in avail:
        first = [desired]
    else:
        first = []
    if 400 <= desired <= 500:
        up_to_500 = [w for w in avail if desired < w <= 500]
        below = [w for w in reversed(avail) if w < desired]
        above = [w for w in avail if w > 500]
        order = first + up_to_500 + below + above
    elif desired < 400:
        below = [w for w in reversed(avail) if w < desired]
        above = [w for w in avail if w > desired]
        order = first + below + above
    else:
        above = [w for w in avail if w > desired]
        below = [w for w in reversed(avail) if w < desired]
        order = first + above + below
    return order
