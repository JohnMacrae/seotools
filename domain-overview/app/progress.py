"""Progress tracking: which planned links exist in a newer crawl, and which proposed pages were built."""
from __future__ import annotations

from . import linkgraph
from .linkplan import norm, path


def _a(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def check_links_detail(suggestions: list[dict], pages: list[dict], links: list[dict]) -> dict[tuple[str, str], dict]:
    """(src, dst) -> {status, live, anchor_data}.
    status: gone (src or dst not a 200 page) | done (contextual link with the planned anchor) |
    wrong anchor (contextual link exists, none uses the planned anchor) | partial (only a non-contextual link) | missing.
    live = anchors of the contextual links. anchor_data is False when the crawl stored no anchor text at all
    (older crawls); status then ignores the anchor, so a contextual link is done."""
    ok = {norm(p["url"]) for p in pages if p.get("status") == 200}
    has_data = any(l.get("anchor") is not None for l in links)
    ctx: dict[tuple[str, str], list[str]] = {}
    for l in linkgraph.contextual_links(pages, links):
        ctx.setdefault((norm(l["src"]), norm(l["dst"])), []).append(l.get("anchor") or "")
    anyl = {(norm(l["src"]), norm(l["dst"])) for l in links}
    out = {}
    for s in suggestions:
        k = (norm(s["src"]), norm(s["dst"]))
        live = ctx.get(k, []) if has_data else []
        planned = _a(s.get("anchor"))
        if k[0] not in ok or k[1] not in ok:
            st = "gone"
        elif k in ctx:
            st = "wrong anchor" if has_data and planned and planned not in {_a(x) for x in live} else "done"
        else:
            st = "partial" if k in anyl else "missing"
        out[(s["src"], s["dst"])] = {"status": st, "live": sorted(set(live)), "anchor_data": has_data}
    return out


def check_links(suggestions: list[dict], pages: list[dict], links: list[dict]) -> dict[tuple[str, str], str]:
    """(src, dst) -> done | wrong anchor | partial | missing | gone. See check_links_detail."""
    return {k: v["status"] for k, v in check_links_detail(suggestions, pages, links).items()}


def built_pages(gap_pages: list[dict], pages: list[dict]) -> list[dict]:
    """Proposed pages whose slug now exists as a 200 page in the crawl."""
    live = {path(p["url"]).rstrip("/") or "/": p["url"] for p in pages if p.get("status") == 200}
    return [{**g, "built": g["slug"].rstrip("/") in live, "url": live.get(g["slug"].rstrip("/"))} for g in gap_pages]
