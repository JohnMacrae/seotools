"""Internal link-strength model. Pure: takes page, link and Search Console dicts, returns metrics."""
from __future__ import annotations

from fnmatch import fnmatchcase
import math
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

SITEWIDE_SHARE = 0.5          # a (dst, anchor) link on more than this share of 200 pages is boilerplate
BOILERPLATE_PLACEMENTS = ("nav", "header", "footer")
CONTEXTUAL_PLACEMENTS = ("main", "body", "aside")
W_CONTEXTUAL, W_BOILERPLATE = 1.0, 0.15
DAMPING, ITERATIONS, TOLERANCE = 0.85, 50, 1e-6
LOG_DECADES = 3               # PR this many orders of magnitude below the top page scores 0

LINES = ("bridal", "bridesmaids", "mob", "accessories", "general")


def _weights(raw: str) -> dict[str, float]:
    out = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


# Business priority per product line (bridal first, then MOB). Multiplies opportunity.
LINE_WEIGHTS = {"bridal": 1.0, "mob": 0.6, "bridesmaids": 0.4, "general": 0.5, "accessories": 0.2,
                **_weights(os.environ.get("LINE_WEIGHTS", ""))}
# Discontinued ranges (last stock only, can't reorder): still linkable while unsold; links to them are
# marked temporary and their traffic is routed to live ranges.
# Comma-separated path globs.
DISCONTINUED = tuple(p.strip() for p in os.environ.get(
    "DISCONTINUED", "/collections/mother-of-the-bride/jacques-vert,/dresses/jacques-vert-*,"
    "/collections/bridal/sophia-tolli,/dresses/sophia-tolli-*").split(",") if p.strip())


def is_discontinued(path: str) -> bool:
    path = path.rstrip("/") or "/"
    return any(fnmatchcase(path, d.rstrip("/") or "/") for d in DISCONTINUED)
COLLECTION_LINE = {"bridal": "bridal", "bridesmaids": "bridesmaids", "mother-of-the-bride": "mob",
                   "accessories": "accessories"}
LINE_ROOT = {"bridal": "/collections/bridal", "bridesmaids": "/collections/bridesmaids",
             "mob": "/collections/mother-of-the-bride", "accessories": "/collections/accessories"}

# Organic CTR by position (rounded): 1-10 individually, 11-20, 21-30.
_CTR = {1: .28, 2: .15, 3: .11, 4: .08, 5: .06, 6: .05, 7: .04, 8: .03, 9: .025, 10: .02}
MIN_POS, MAX_POS, TARGET_POS = 4, 30, 3


def ctr_at(position: float) -> float:
    p = int(position + 0.5)
    if p < 1:
        p = 1
    if p in _CTR:
        return _CTR[p]
    return .01 if p <= 20 else .005


def intent_factor(impressions: float, clicks: float, position: float) -> float:
    """Observed vs expected clicks at this position, smoothed, capped at 1.

    Discounts queries where we're shown but searchers want something else
    (e.g. "bustle": 89k impressions at pos 7, 0 clicks = Bustle magazine).
    Small queries with no clicks yet keep most of their weight.
    """
    return min(1.0, (clicks + 2) / (impressions * ctr_at(position) + 2))


def uplift(impressions: float, clicks: float, position: float) -> float:
    gain = impressions * max(0.0, ctr_at(TARGET_POS) - ctr_at(position))
    return gain * intent_factor(impressions, clicks, position)


def _path(u: str) -> str:
    return urlsplit(u).path or "/"


def _key(u: str) -> str:
    """URL compared without a trailing slash (root stays)."""
    p = urlsplit(u)
    return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}" if p.path.rstrip("/") else f"{p.scheme}://{p.netloc}/"


def _anchor(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def is_html_ok(p: dict) -> bool:
    return p["status"] == 200 and "html" in (p["content_type"] or "")


# --- boilerplate vs contextual ------------------------------------------------

def sitewide_links(pages: list[dict], links: list[dict]) -> set[tuple[str, str]]:
    """(dst, anchor) pairs that appear on more than half of the crawled 200 pages."""
    ok = {p["url"] for p in pages if is_html_ok(p)}
    if not ok:
        return set()
    srcs: dict[tuple[str, str], set] = defaultdict(set)
    for l in links:
        if l["src"] in ok and l["src"] != l["dst"]:
            srcs[(l["dst"], _anchor(l["anchor"]))].add(l["src"])
    return {k for k, s in srcs.items() if len(s) > SITEWIDE_SHARE * len(ok)}


def contextual_links(pages: list[dict], links: list[dict]) -> list[dict]:
    """Links that are not boilerplate: only non-sitewide links in main, body or aside."""
    wide = sitewide_links(pages, links)
    return [l for l in links
            if l["src"] != l["dst"] and l["placement"] in CONTEXTUAL_PLACEMENTS
            and (l["dst"], _anchor(l["anchor"])) not in wide]


# --- PageRank -----------------------------------------------------------------

def is_indexable(p: dict) -> bool:
    if not is_html_ok(p):
        return False
    if "noindex" in (p.get("meta_robots") or "").lower():
        return False
    c = p.get("canonical")
    return not (c and _key(c) != _key(p["url"]))


def pagerank(pages: list[dict], links: list[dict]) -> dict[str, float]:
    """Raw PageRank (sums to 1) for indexable pages."""
    nodes = {p["url"] for p in pages if is_indexable(p)}
    if not nodes:
        return {}
    by_key = {_key(p["url"]): p for p in pages}
    ctx = {(l["src"], l["dst"], _anchor(l["anchor"])) for l in contextual_links(pages, links)}

    def resolve(u: str) -> str | None:
        """Follow redirects and canonicals to an indexable crawled page, else None."""
        for _ in range(6):
            if u in nodes:
                return u
            p = by_key.get(_key(u))
            if not p:
                return None
            if p["url"] in nodes:
                return p["url"]
            nxt = p.get("redirect_to") if p["status"] and 300 <= p["status"] < 400 else None
            if p["status"] == 200 and "noindex" not in (p.get("meta_robots") or "").lower():
                nxt = p.get("canonical")
            if not nxt:
                return None
            u = nxt
        return None

    edges: dict[tuple[str, str], float] = {}
    for l in links:
        src = l["src"]
        if src not in nodes:
            continue
        dst = resolve(l["dst"])
        if dst is None or dst == src:
            continue
        w = W_CONTEXTUAL if (src, l["dst"], _anchor(l["anchor"])) in ctx else W_BOILERPLATE
        edges[(src, dst)] = max(edges.get((src, dst), 0.0), w)

    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (s, d), w in edges.items():
        out[s].append((d, w))
    total_w = {s: sum(w for _, w in es) for s, es in out.items()}

    n = len(nodes)
    pr = {u: 1 / n for u in nodes}
    for _ in range(ITERATIONS):
        dangling = sum(pr[u] for u in nodes if u not in out)
        base = (1 - DAMPING) / n + DAMPING * dangling / n
        new = {u: base for u in nodes}
        for s, es in out.items():
            share = DAMPING * pr[s] / total_w[s]
            for d, w in es:
                new[d] += share * w
        delta = sum(abs(new[u] - pr[u]) for u in nodes)
        pr = new
        if delta < TOLERANCE:
            break
    return pr


def pr_scores(pr: dict[str, float]) -> dict[str, float]:
    """Log-scale 0-100: top page 100, LOG_DECADES orders of magnitude lower (or less) 0."""
    if not pr:
        return {}
    top = max(pr.values())
    return {u: max(0.0, 100 * (1 + math.log10(v / top) / LOG_DECADES)) for u, v in pr.items()}


# --- product line -------------------------------------------------------------

def _mentions(text: str) -> str | None:
    t = text.lower()
    if "mother of the bride" in t or "mother-of-the-bride" in t or re.search(r"\bmob\b", t):
        return "mob"
    if "bridesmaid" in t:
        return "bridesmaids"
    return None


def _collection_line(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "collections":
        return COLLECTION_LINE.get(parts[1], "general")
    return None


def product_line(url: str, title: str | None, h1: str | None, outlink_dsts: list[str]) -> str:
    path = _path(url)
    cl = _collection_line(path)
    if cl:
        return cl
    parts = path.strip("/").split("/")
    if parts[0] == "dresses" and len(parts) > 1:
        votes: Counter = Counter()
        roots: Counter = Counter()
        for d in outlink_dsts:
            dp = _path(d)
            line = _collection_line(dp)
            if line and line != "general":
                votes[line] += 1
                if len(dp.strip("/").split("/")) == 2:
                    roots[line] += 1
        if not votes:
            return "bridal"
        top = votes.most_common()
        tied = [l for l, c in top if c == top[0][1]]
        if len(tied) == 1:
            return tied[0]
        return max(tied, key=lambda l: (roots[l], l == "bridal"))
    if parts[0] in ("blog", "faq") and len(parts) > 1:
        return _mentions(f"{title or ''} {h1 or ''} {path}") or "bridal"
    return "general"


# --- strength -----------------------------------------------------------------

def strength(pages: list[dict], links: list[dict], gsc_pages: list[dict], gsc_rows: list[dict]) -> dict:
    """Per-page link strength joined with Search Console data.

    gsc_pages: {page, clicks, impressions, ctr, position}; gsc_rows: {page, query, clicks, impressions, ctr,
    position}. GSC URLs must already be in the crawl's URL form."""
    ok = [p for p in pages if is_html_ok(p)]
    by_key = {_key(p["url"]): p for p in ok}
    ctx = contextual_links(pages, links)
    wide = sitewide_links(pages, links)

    ctx_in: dict[str, set] = defaultdict(set)
    ctx_out: dict[str, set] = defaultdict(set)
    boiler_in: dict[str, set] = defaultdict(set)
    for l in ctx:
        ctx_in[l["dst"]].add(l["src"])
        ctx_out[l["src"]].add(l["dst"])
    ctx_ids = {id(l) for l in ctx}
    for l in links:
        if l["src"] != l["dst"] and id(l) not in ctx_ids:
            boiler_in[l["dst"]].add(l["src"])

    pr = pagerank(pages, links)
    score = pr_scores(pr)

    # GSC joins by trailing-slash-insensitive URL.
    gp: dict[str, dict] = {}
    for g in gsc_pages:
        gp[_key(g["page"])] = g
    queries: dict[str, list[dict]] = defaultdict(list)
    for r in gsc_rows:
        queries[_key(r["page"])].append(r)

    # Outlinks used for /dresses/ line detection: non-sitewide links in any placement except footer
    # (this includes breadcrumbs wherever they sit).
    line_links: dict[str, list[str]] = defaultdict(list)
    for l in links:
        if l["src"] != l["dst"] and (l["dst"], _anchor(l["anchor"])) not in wide and l["placement"] != "footer":
            line_links[l["src"]].append(l["dst"])

    rows = []
    for p in ok:
        u, k = p["url"], _key(p["url"])
        g = gp.get(k)
        qs = sorted(queries.get(k, []), key=lambda r: -r["impressions"])
        potential = 0.0
        for r in qs:
            if MIN_POS <= r["position"] and int(r["position"] + 0.5) <= MAX_POS:
                potential += uplift(r["impressions"], r["clicks"], r["position"])
        s = score.get(u, 0.0)
        rows.append({
            "url": u, "title": p["title"], "line": product_line(u, p["title"], p["h1"], line_links.get(u, [])),
            "pagerank": round(s, 2), "indexable": u in pr,
            "contextual_inlinks": len(ctx_in.get(u, ())), "boilerplate_inlinks": len(boiler_in.get(u, ())),
            "contextual_outlinks": len(ctx_out.get(u, ())), "depth": p["depth"],
            "clicks": g["clicks"] if g else 0, "impressions": g["impressions"] if g else 0,
            "ctr": g["ctr"] if g else None, "position": round(g["position"], 1) if g else None,
            "top_queries": [{"query": r["query"], "impressions": r["impressions"], "clicks": r["clicks"],
                             "position": round(r["position"], 1)} for r in qs[:5]],
            "potential": round(potential, 2),
            "raw_opportunity": round(potential / (1 + s / 10), 2) if g else 0.0,
        })
        r = rows[-1]
        r["discontinued"] = is_discontinued(_path(u))
        # Discontinued = last stock: still a valid target while unsold; the label lets the plan
        # treat its links as temporary and send its traffic on to live ranges once sold.
        r["opportunity"] = round(r["raw_opportunity"] * LINE_WEIGHTS.get(r["line"], 1.0), 2)

    per_line: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        per_line[r["line"]].append(r)
    for rs in per_line.values():
        rank = 0
        for r in sorted(rs, key=lambda r: -r["opportunity"]):
            if r["opportunity"] > 0:
                rank += 1
                r["rank_in_line"] = rank
            else:
                r["rank_in_line"] = None
    rows.sort(key=lambda r: -r["opportunity"])

    lines = {l: {"pages": 0, "impressions": 0, "clicks": 0} for l in LINES}
    for r in rows:
        t = lines[r["line"]]
        t["pages"] += 1
        t["impressions"] += r["impressions"]
        t["clicks"] += r["clicks"]

    base = ""
    if ok:
        sp = urlsplit(ok[0]["url"])
        base = f"{sp.scheme}://{sp.netloc}"
    line_of = {_key(r["url"]): r["line"] for r in rows}

    # Wrong-page flags: line-specific queries whose best-ranked page belongs to another line.
    best: dict[str, dict] = {}
    for r in gsc_rows:
        k = _key(r["page"])
        if k not in line_of:
            continue
        cur = best.get(r["query"])
        if cur is None or (r["position"], -r["impressions"]) < (cur["position"], -cur["impressions"]):
            best[r["query"]] = r
    wrong = []
    for q, r in best.items():
        want = _mentions(q)
        if not want:
            continue
        have = line_of[_key(r["page"])]
        if have != want:
            wrong.append({"query": q, "page": r["page"], "page_line": have, "impressions": r["impressions"],
                          "clicks": r["clicks"], "position": round(r["position"], 1), "want_line": want,
                          "suggested": base + LINE_ROOT[want]})
    wrong.sort(key=lambda w: -w["impressions"])

    # GSC pages the crawl does not know as a 200 page.
    status = {_key(p["url"]): p["status"] for p in pages}
    unmatched = [{"page": g["page"], "clicks": g["clicks"], "impressions": g["impressions"],
                  "position": round(g["position"], 1), "status": status.get(_key(g["page"]))}
                 for g in gsc_pages if _key(g["page"]) not in by_key]
    unmatched.sort(key=lambda g: -g["impressions"])

    return {"lines": lines, "pages": rows, "wrong_page": wrong, "gsc_unmatched": unmatched}
