"""Page-gap analysis: which pages should the site build? Recommendations only; nothing is created.

Keyword candidates come from Search Console (queries Google tests us on), the competitor gap and DataForSEO
keyword ideas. Covered keywords are dropped, the rest are grouped into proposed pages by the model, and every
number and URL the model returns is re-checked or recomputed here.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from urllib.parse import urlsplit

import httpx

from . import dfs, linkgraph, llm
from .linkplan import norm, path

GAP_MIN_IMPRESSIONS = int(os.environ.get("GAP_MIN_IMPRESSIONS", "20"))
GAP_IDEAS_LIMIT = int(os.environ.get("GAP_IDEAS_LIMIT", "100"))
IDEAS_MIN_VOLUME = 10
GSC_FAR = 20            # best position beyond this = not really ranking
COVERED_POS = 10        # a clicked row in the top 10 means the keyword is covered
GSC_MONTHS = 3          # GSC window is ~90 days: impressions / 3 = monthly proxy
GSC_BOOST = 1.5
MAX_KW_PER_LINE = 120
MAX_PAGES_LISTED = 250
MAX_LINK_FROM = 5
LOCATION = 2826
PAGE_TYPES = ("guide", "FAQ", "collection", "local landing")
SLUG_PREFIXES = ("/blog/", "/faq/", "/collections/")

DEFAULT_SEEDS = {
    "bridal": "wedding dresses colchester,wedding dresses essex,bridal shop essex",
    "mob": "mother of the bride outfits essex,mother of the bride dresses colchester",
    "bridesmaids": "bridesmaid dresses essex,bridesmaid dresses colchester",
}
STOPWORDS = {"the", "a", "an", "in", "near", "me", "for", "of", "to", "and", "on", "at", "with", "by", "my", "your",
             "is", "are", "do", "i", "or", "what", "how", "uk"}


RELEVANT = re.compile(r"wedding|bride|bridal|bridesmaid|\bmob\b|\bgown")


def seeds() -> dict[str, list[str]]:
    out = {}
    for line, default in DEFAULT_SEEDS.items():
        raw = os.environ.get(f"GAP_SEEDS_{line.upper()}") or default
        out[line] = [s.strip().lower() for s in raw.split(",") if s.strip()]
    return out


# ---------------------------------------------------------------- text helpers

def _stem(w: str) -> str:
    if w.endswith("sses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def words(text: str) -> list[str]:
    """Significant words in order, lightly stemmed so dress/dresses match."""
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS]


def keyword_line(kw: str) -> str | None:
    k = kw.lower()
    if "mother of the bride" in k or "mother-of-the-bride" in k or re.search(r"\bmob\b", k):
        return "mob"
    if "bridesmaid" in k:
        return "bridesmaids"
    if "wedding dress" in k or "bridal" in k:
        return "bridal"
    return None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------------------------------------------------------------- candidates

def gsc_candidates(gsc_rows: list[dict], S: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """(gap candidates, per-query aggregate for every query). Page x query rows are folded per query."""
    agg: dict[str, dict] = {}
    for r in gsc_rows:
        q = (r["query"] or "").lower().strip()
        if not q:
            continue
        a = agg.setdefault(q, {"impressions": 0.0, "clicks": 0.0, "position": 999.0, "page": None, "_pi": 0.0,
                               "covered": False})
        a["impressions"] += r["impressions"]
        a["clicks"] += r["clicks"]
        if r["position"] <= COVERED_POS and r["clicks"] >= 1:
            a["covered"] = True
        if (r["position"], -r["impressions"]) < (a["position"], -a["_pi"]):
            a["position"], a["page"], a["_pi"] = r["position"], norm(r["page"]), r["impressions"]
    gaps = {}
    for q, a in agg.items():
        if a["impressions"] < GAP_MIN_IMPRESSIONS:
            continue
        ql = keyword_line(q)
        page_line = S.get(a["page"], {}).get("line")
        if a["position"] > GSC_FAR or (ql and page_line and page_line != ql):
            gaps[q] = {"keyword": q, "line": ql, "sources": {"gsc"}, "impressions": a["impressions"],
                       "position": round(a["position"], 1), "page": a["page"], "volume": None, "kd": None,
                       "intent": None, "competitor_urls": []}
    return gaps, agg


def competitor_candidates(comparison: dict | None) -> dict[str, dict]:
    out = {}
    for m in (comparison or {}).get("missing") or []:  # brand terms were already dropped in compare.py
        kw = (m.get("keyword") or "").lower().strip()
        if not kw:
            continue
        urls = [c.get("url") for c in (m.get("competitors") or {}).values() if c and c.get("url")]
        out[kw] = {"keyword": kw, "line": keyword_line(kw), "sources": {"competitor"}, "impressions": None,
                   "position": None, "page": None, "volume": m.get("volume"), "kd": m.get("kd"), "intent": None,
                   "competitor_urls": urls or ([m["best_url"]] if m.get("best_url") else [])}
    return out


async def idea_candidates(seed_map: dict[str, list[str]], warnings: list[str]) -> tuple[dict[str, dict], float]:
    out, cost = {}, 0.0
    if not (dfs.MOCK or (dfs.LOGIN and dfs.PASSWORD)):
        warnings.append("Keyword ideas skipped: DataForSEO credentials not set")
        return out, cost
    async with dfs.client() as c:
        for line, seed in seed_map.items():
            try:
                res, cst = await dfs.post_path(c, "dataforseo_labs/google/keyword_ideas/live", {
                    "keywords": seed, "location_code": LOCATION, "language_code": "en", "limit": GAP_IDEAS_LIMIT,
                    "filters": [["keyword_info.search_volume", ">=", IDEAS_MIN_VOLUME]],
                    "order_by": ["keyword_info.search_volume,desc"],
                }, mock_name="gap_keyword_ideas")
            except (dfs.DFSError, httpx.HTTPError) as e:
                warnings.append(f"Keyword ideas for {line} failed: {e}")
                continue
            cost += cst
            for it in (res or {}).get("items") or []:
                kw = (it.get("keyword") or "").lower().strip()
                info = it.get("keyword_info") or {}
                vol = info.get("search_volume")
                if not kw or kw in out or (vol or 0) < IDEAS_MIN_VOLUME or not RELEVANT.search(kw):
                    continue  # keyword ideas wander ("yellow dress"); keep only the wedding vocabulary
                out[kw] = {"keyword": kw, "line": keyword_line(kw) or line, "sources": {"ideas"}, "impressions": None,
                           "position": None, "page": None, "volume": vol,
                           "kd": (it.get("keyword_properties") or {}).get("keyword_difficulty"),
                           "intent": (it.get("search_intent_info") or {}).get("main_intent"), "competitor_urls": []}
    return out, cost


def merge(*groups: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for g in groups:
        for kw, c in g.items():
            o = out.get(kw)
            if not o:
                out[kw] = {**c, "sources": set(c["sources"]), "competitor_urls": list(c["competitor_urls"])}
                continue
            o["sources"] |= c["sources"]
            for k in ("impressions", "position", "page", "kd", "intent", "line"):
                if o.get(k) is None:
                    o[k] = c.get(k)
            if c.get("volume") is not None:
                o["volume"] = max(o["volume"] or 0, c["volume"])
            o["competitor_urls"] += [u for u in c["competitor_urls"] if u not in o["competitor_urls"]]
    return out


def is_covered(kw: str, gsc_agg: dict, page_words: list[set]) -> bool:
    a = gsc_agg.get(kw)
    if a and a["covered"]:
        return True
    w = set(words(kw))
    return bool(w) and any(w <= pw for pw in page_words)


def monthly(c: dict) -> tuple[float, bool]:
    """(monthly volume, is_proxy). DFS volume when known, else GSC impressions over the window / 3."""
    if c.get("volume") is not None:
        return float(c["volume"]), False
    return round((c.get("impressions") or 0) / GSC_MONTHS, 1), True


# ---------------------------------------------------------------- grouping

SYSTEM = """You plan new pages for a bridal shop website (Serenity Brides, Colchester, Essex). British English.
Product lines: bridal (wedding dresses), mob (mother of the bride outfits), bridesmaids. Recommendations only.
Reply with JSON only."""

USER = """Product line: {line}

Keywords not yet covered by any page. Format: keyword | sources | monthly volume (or Google impressions over 90 days) | intent
{keywords}

Existing pages (url | line | title):
{pages}

Group these keywords into proposed NEW pages: a guide, an FAQ, a collection page or a local landing page.
One page can target several closely related keywords; a keyword belongs to at most one page.
If an existing page nearly covers a keyword, do not propose a new page: put it in "improve" instead.

Return:
{{"pages": [{{"title": "working title", "slug": "/blog/... or /faq/... or /collections/{folder}/...",
  "page_type": "guide|FAQ|collection|local landing", "primary_keyword": "<from the list>",
  "secondary_keywords": ["<from the list>"], "why": "one sentence with the numbers",
  "hub": "<existing page URL this belongs under>", "link_from": ["<up to 5 existing page URLs that should link to it>"]}}],
 "improve": [{{"url": "<existing page URL>", "keywords": ["<from the list>"], "why": "one sentence"}}]}}

Ignore keywords that are not about wedding wear for this shop. Use only keywords and URLs from the lists above. The slug must be new (not an existing URL)."""


def fallback_proposals(kws: list[dict], hub: str | None, link_from: list[str]) -> dict:
    """One page per cluster sharing the same two leading significant words. No model."""
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for c in kws:
        clusters[tuple(words(c["keyword"])[:2])].append(c)
    pages = []
    for key, cs in clusters.items():
        cs.sort(key=lambda c: -monthly(c)[0])
        primary = cs[0]["keyword"]
        pages.append({"title": f"{primary.title()}: a guide", "slug": f"/blog/{slugify(primary)}", "page_type": "guide",
                      "primary_keyword": primary, "secondary_keywords": [c["keyword"] for c in cs[1:]],
                      "why": "", "hub": hub, "link_from": link_from})
    return {"pages": pages, "improve": []}


def validate(proposal: dict, line: str, byk: dict[str, dict], used: set, ok_urls: set, existing_paths: set,
             base: str, S: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Keep only what checks out: new slug, real URLs, unused keywords. Returns (pages, improve, warnings)."""
    warn, pages, improve = [], [], []

    def url(u):
        u = str(u or "").strip()
        if u.startswith("/"):
            b = urlsplit(base)
            u = f"{b.scheme}://{b.netloc}{u}"
        u = norm(u) if u else ""
        return u if u in ok_urls else None

    def kw_ok(k):
        k = (k or "").lower().strip()
        return k if k in byk and k not in used else None

    for p in (proposal or {}).get("pages", []):
        if not isinstance(p, dict):
            continue
        slug = "/" + str(p.get("slug") or "").strip().strip("/")
        primary = kw_ok(p.get("primary_keyword"))
        if not primary:
            warn.append(f"dropped page '{p.get('title')}': primary keyword unknown or already used")
            continue
        if not slug.startswith(SLUG_PREFIXES):
            warn.append(f"dropped page '{p.get('title')}': slug {slug} is not under /blog/, /faq/ or /collections/")
            continue
        if slug in existing_paths:
            warn.append(f"dropped page '{p.get('title')}': {slug} already exists on the site")
            continue
        used.add(primary)
        sec = []
        for k in p.get("secondary_keywords") or []:
            k = kw_ok(k)
            if k and k not in sec:
                sec.append(k)
                used.add(k)
        existing_paths.add(slug)  # two proposals must not share a slug either
        hub = url(p.get("hub"))
        if p.get("hub") and not hub:
            warn.append(f"{slug}: hub {p.get('hub')} is not a crawled page; dropped")
        link_from = []
        for u in p.get("link_from") or []:
            n = url(u)
            if n and n not in link_from and n != hub:
                link_from.append(n)
            elif not n:
                warn.append(f"{slug}: link-from {u} is not a crawled page; dropped")
        ptype = p.get("page_type") if p.get("page_type") in PAGE_TYPES else "guide"
        pline = p.get("line") if p.get("line") in linkgraph.LINE_WEIGHTS else line
        pages.append({"title": str(p.get("title") or primary.title())[:120], "slug": slug, "line": pline,
                      "page_type": ptype, "primary_keyword": primary, "secondary_keywords": sec,
                      "why": str(p.get("why") or "")[:300], "hub": hub, "link_from": link_from[:MAX_LINK_FROM]})
    for i in (proposal or {}).get("improve", []):
        if not isinstance(i, dict):
            continue
        u = url(i.get("url"))
        ks = [k for k in (kw_ok(x) for x in i.get("keywords") or []) if k]
        if u and ks:
            improve.append({"url": u, "keywords": ks, "why": str(i.get("why") or "")[:300], "line": line})
    return pages, improve, warn


def finish(page: dict, byk: dict[str, dict]) -> dict:
    """Numbers computed in code: volume, sources, difficulty, score."""
    cs = [byk[k] for k in [page["primary_keyword"], *page["secondary_keywords"]]]
    vols = [monthly(c) for c in cs]
    volume = round(sum(v for v, _ in vols))
    kds = [c["kd"] for c in cs if c.get("kd") is not None]
    kd = byk[page["primary_keyword"]].get("kd")
    kd = kd if kd is not None else (sum(kds) / len(kds) if kds else None)
    sources = sorted({s for c in cs for s in c["sources"]})
    weight = linkgraph.LINE_WEIGHTS.get(page["line"], linkgraph.LINE_WEIGHTS["general"])
    score = volume * (1 - kd / 100 if kd is not None else 0.5) * weight * (GSC_BOOST if "gsc" in sources else 1)
    gsc = [c for c in cs if "gsc" in c["sources"]]
    if not page.get("why"):
        page = {**page, "why": f"{len(cs)} keyword{'s' if len(cs) > 1 else ''}, about {volume:,} searches a month"
                               f"{' (estimated from Search Console)' if any(p for _, p in vols) else ''}"}
    return {**page, "volume": volume, "volume_is_proxy": any(p for _, p in vols), "kd": kd, "sources": sources,
            "score": round(score, 1),
            "gsc": {"impressions": round(sum(c["impressions"] or 0 for c in gsc)),
                    "best_position": min((c["position"] for c in gsc if c.get("position") is not None), default=None)},
            "competitor_urls": list(dict.fromkeys(u for c in cs for u in c["competitor_urls"]))[:5]}


# ---------------------------------------------------------------- entry point

async def build_gaps(pages: list[dict], strength: dict, gsc_rows: list[dict], comparison: dict | None,
                     progress=None) -> dict:
    say = progress or (lambda m: None)
    S = {norm(p["url"]): {**p, "url": norm(p["url"])} for p in strength["pages"]}
    ok_pages = [p for p in pages if p.get("status") == 200]
    ok_urls = {norm(p["url"]) for p in ok_pages}
    existing_paths = {path(u).rstrip("/") or "/" for u in ok_urls}
    base = pages[0]["url"] if pages else "https://example.com/"
    page_words = [set(words(f"{p.get('title') or ''} {p.get('h1') or ''}")) for p in ok_pages]
    warnings: list[str] = []
    cost = 0.0

    say("Collecting keywords from Search Console and competitors")
    gsc_c, gsc_agg = gsc_candidates(gsc_rows, S)
    comp_c = competitor_candidates(comparison)
    if comparison is None:
        warnings.append("No competitor comparison stored for this domain; competitor gap skipped")
    say("Fetching keyword ideas")
    idea_c, c = await idea_candidates(seeds(), warnings)
    cost += c
    merged = merge(gsc_c, comp_c, idea_c)
    counts = {"gsc": len(gsc_c), "competitor": len(comp_c), "ideas": len(idea_c)}

    remaining = {}
    dropped = 0
    for kw, cand in merged.items():
        if is_covered(kw, gsc_agg, page_words):
            dropped += 1
        else:
            cand["line"] = cand.get("line") or "bridal"
            remaining[kw] = cand

    by_line: dict[str, list[dict]] = defaultdict(list)
    for cand in remaining.values():
        by_line[cand["line"]].append(cand)
    for cs in by_line.values():
        cs.sort(key=lambda c: -monthly(c)[0])

    indexable = [u for u, s in S.items() if s.get("indexable") and not (len(path(u).strip("/").split("/")) == 2
                                                                      and path(u).startswith("/dresses/"))]
    use_llm = llm.configured()
    llm_ok = False
    used: set = set()
    out_pages, improve = [], []
    async with httpx.AsyncClient() as client:
        for line, cs in by_line.items():
            say(f"Grouping {line} keywords" + (" with the model" if use_llm else ""))
            cs = cs[:MAX_KW_PER_LINE]
            byk = {c["keyword"]: c for c in cs}
            hubs = [u for u in indexable if S[u]["line"] == line and len(path(u).strip("/").split("/")) == 2
                    and path(u).startswith("/collections/")]
            by_pr = sorted((u for u in indexable if S[u]["line"] == line), key=lambda u: -S[u].get("pagerank", 0))
            proposal = None
            if use_llm:
                listed = sorted(indexable, key=lambda u: (S[u]["line"] != line, -S[u].get("pagerank", 0)))[:MAX_PAGES_LISTED]
                kl = "\n".join(f"{c['keyword']} | {', '.join(sorted(c['sources']))} | "
                               f"{('~' if monthly(c)[1] else '') + format(monthly(c)[0], '.0f')}"
                               f"{' (GSC impressions/3)' if monthly(c)[1] else ''} | {c.get('intent') or 'n/a'}" for c in cs)
                pl = "\n".join(f"{u} | {S[u]['line']} | {S[u].get('title') or ''}" for u in listed)
                folder = {"mob": "mother-of-the-bride"}.get(line, line)
                try:
                    proposal, c_ = await llm.ask_json(client, SYSTEM, USER.format(line=line, keywords=kl, pages=pl,
                                                                                  folder=folder), max_tokens=8000)
                    cost += c_
                    llm_ok = True
                except (llm.LLMError, httpx.HTTPError) as e:
                    warnings.append(f"Model failed for {line}; used rules instead: {e}")
            if proposal is None:
                proposal = fallback_proposals(cs, hubs[0] if hubs else None,
                                              [u for u in by_pr if u not in hubs and not facet_or_dress(u)][:MAX_LINK_FROM])
            pg, im, w = validate(proposal, line, byk, used, ok_urls, existing_paths, base, S)
            warnings += w
            out_pages += [finish(p, byk) for p in pg]
            improve += im
    # keywords left unused by pages are dropped from improve items (a keyword has one home)
    for i in improve:
        i["keywords"] = [k for k in i["keywords"] if k not in {x["primary_keyword"] for x in out_pages}
                         and all(k not in x["secondary_keywords"] for x in out_pages)]
    improve = [i for i in improve if i["keywords"]]
    for i in improve:
        i["title"] = S.get(i["url"], {}).get("title")
    out_pages.sort(key=lambda p: -p["score"])
    say("Done")
    return {"pages": out_pages, "improve": improve, "dropped_covered": dropped, "sources": counts,
            "cost": round(cost, 4), "model": llm.MODEL if llm_ok else None, "llm_used": llm_ok, "warnings": warnings}


def facet_or_dress(u: str) -> bool:
    return path(u).startswith("/dresses/")
