"""Close-variant keyword matching (case, plural/singular, word order) shared by supporting pages and the link map."""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlsplit

_WORDS = r"[a-z0-9']+"


def _stem(w: str) -> str:
    if w.endswith("sses"):
        return w[:-2]
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def _stem_key(s: str) -> tuple:
    return tuple(sorted(_stem(w) for w in re.findall(_WORDS, s.lower())))


def contains_variant(text: str, kw: str) -> bool:
    """Some contiguous run of words in text is kw or a close variant (plural/singular, word order)."""
    k = _stem_key(kw)
    n = len(k)
    if not n:
        return False
    words = [_stem(w) for w in re.findall(_WORDS, (text or "").lower())]
    return any(tuple(sorted(words[i:i + n])) == k for i in range(len(words) - n + 1))


def contains_loose(text: str, kw: str, gap: int = 2) -> bool:
    """All of kw's words (close variants) within a run of len(kw)+gap words, e.g.
    "Wedding dress & bridal shop in Colchester" holds "wedding dress shop colchester"."""
    k = Counter(_stem_key(kw))
    n = sum(k.values())
    if not n:
        return False
    words = [_stem(w) for w in re.findall(_WORDS, (text or "").lower())]
    return any(not (k - Counter(words[i:i + n + gap])) for i in range(max(1, len(words) - n + 1)))


def first_sentence(text: str) -> str:
    """First sentence of the body copy: split on . ! ? followed by whitespace, or on a newline."""
    for piece in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if piece.strip():
            return piece.strip()
    return ""



GENERIC_URL_WORDS = {"wedding", "dress", "bridal"}  # covered by a /collections/bridal/ path


def url_has(url: str, kw: str) -> bool:
    """The URL path carries kw: its distinctive words (minus wedding/dress/bridal) as a close variant;
    a keyword of only generic words needs one of them in the path."""
    path = " ".join(re.sub(r"[-_]+", " ", s) for s in urlsplit(url).path.split("/") if s)
    rest = [w for w in re.findall(_WORDS, kw.lower()) if _stem(w) not in GENERIC_URL_WORDS]
    if rest:
        return contains_variant(path, " ".join(rest))
    return any(_stem(w) in GENERIC_URL_WORDS for w in re.findall(_WORDS, path.lower()))
