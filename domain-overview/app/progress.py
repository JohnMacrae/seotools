"""Progress tracking: which planned links exist in a newer crawl, and which proposed pages were built."""
from __future__ import annotations

from . import linkgraph
from .linkplan import norm, path


def check_links(suggestions: list[dict], pages: list[dict], links: list[dict]) -> dict[tuple[str, str], str]:
    """(src, dst) -> done | partial | missing | gone.
    gone = src or dst is not a 200 page; done = a contextual link exists; partial = any other link exists."""
    ok = {norm(p["url"]) for p in pages if p.get("status") == 200}
    ctx = {(norm(l["src"]), norm(l["dst"])) for l in linkgraph.contextual_links(pages, links)}
    anyl = {(norm(l["src"]), norm(l["dst"])) for l in links}
    out = {}
    for s in suggestions:
        k = (norm(s["src"]), norm(s["dst"]))
        out[(s["src"], s["dst"])] = ("gone" if k[0] not in ok or k[1] not in ok else
                                     "done" if k in ctx else "partial" if k in anyl else "missing")
    return out


def built_pages(gap_pages: list[dict], pages: list[dict]) -> list[dict]:
    """Proposed pages whose slug now exists as a 200 page in the crawl."""
    live = {path(p["url"]).rstrip("/") or "/": p["url"] for p in pages if p.get("status") == 200}
    return [{**g, "built": g["slug"].rstrip("/") in live, "url": live.get(g["slug"].rstrip("/"))} for g in gap_pages]
