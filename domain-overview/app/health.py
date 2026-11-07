"""Site health from a stored crawl. Pure: takes page and link dicts, returns a report."""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import urlsplit

CAP = 500


def _path(u: str) -> str:
    return urlsplit(u).path or "/"


def _bucket(rows: list[dict]) -> dict:
    return {"count": len(rows), "rows": rows[:CAP]}


def _status_class(s) -> str:
    return "error" if s is None else f"{s // 100}xx"


def _same(a: str | None, b: str) -> bool:
    return bool(a) and a.rstrip("/") == b.rstrip("/")


def health(pages: list[dict], links: list[dict]) -> dict:
    by_url = {p["url"]: p for p in pages}
    ok = [p for p in pages if p["status"] == 200 and "html" in (p["content_type"] or "")]

    inlinks: Counter = Counter()
    contextual: Counter = Counter()
    outlinks: Counter = Counter()  # internal outlinks in main/body
    for l in links:
        if l["src"] == l["dst"]:
            continue
        inlinks[l["dst"]] += 1
        if l["placement"] in ("main", "body"):
            contextual[l["dst"]] += 1
            outlinks[l["src"]] += 1

    def row(p, **extra):
        return {"url": p["url"], "title": p["title"], **extra}

    home_like = lambda p: p["depth"] == 0
    orphans = [row(p) for p in ok if p["in_sitemap"] and not inlinks[p["url"]] and not home_like(p)]
    ctx_orphans = [row(p, inlinks=inlinks[p["url"]]) for p in ok if not contextual[p["url"]] and not home_like(p)]

    broken, redirected = [], []
    for l in links:
        d = by_url.get(l["dst"])
        if not d:
            continue
        if d["status"] is None or d["status"] >= 400:
            broken.append({"src": l["src"], "dst": l["dst"], "anchor": l["anchor"], "status": d["status"]})
        elif 300 <= d["status"] < 400:
            redirected.append({"src": l["src"], "dst": l["dst"], "anchor": l["anchor"],
                               "status": d["status"], "redirect_to": d["redirect_to"]})

    dead_ends = [row(p) for p in ok if not outlinks[p["url"]]]

    non_indexable = []
    for p in ok:
        robots = (p["meta_robots"] or "").lower()
        if "noindex" in robots:
            non_indexable.append(row(p, reason="noindex"))
        elif p["canonical"] and not _same(p["canonical"], p["url"]):
            non_indexable.append(row(p, reason="canonical", canonical=p["canonical"]))

    titles = defaultdict(list)
    for p in ok:
        if p["title"]:
            titles[p["title"].strip().lower()].append(p)
    dup_titles = [row(p, count=len(g)) for g in titles.values() if len(g) > 1 for p in g]

    sections: dict = {}
    for p in pages:
        seg = _path(p["url"]).strip("/").split("/")[0]
        s = sections.setdefault("/" + seg if seg else "/", {"section": "/" + seg if seg else "/", "pages": 0, "ok": 0})
        s["pages"] += 1
        s["ok"] += p["status"] == 200

    depths = Counter(p["depth"] for p in pages)
    depth_hist = [{"depth": d, "pages": depths[d]} for d in sorted(k for k in depths if k is not None)]
    if depths.get(None):
        depth_hist.append({"depth": None, "pages": depths[None]})

    return {
        "totals": {
            "pages": len(pages), "html_ok": len(ok), "links": len(links),
            "status": dict(sorted(Counter(_status_class(p["status"]) for p in pages).items())),
        },
        "sections": sorted(sections.values(), key=lambda s: -s["pages"]),
        "depth_histogram": depth_hist,
        "orphans": _bucket(orphans),
        "contextual_orphans": _bucket(ctx_orphans),
        "broken_links": _bucket(broken),
        "redirected_links": _bucket(redirected),
        "dead_ends": _bucket(dead_ends),
        "non_indexable": _bucket(non_indexable),
        "missing_title": _bucket([row(p) for p in ok if not p["title"]]),
        "duplicate_titles": _bucket(dup_titles),
        "missing_h1": _bucket([row(p) for p in ok if not p["h1"]]),
        "missing_description": _bucket([row(p) for p in ok if not p["meta_description"]]),
        "deep_pages": _bucket([row(p, depth=p["depth"]) for p in ok if p["depth"] is not None and p["depth"] > 3]),
    }
