"""Internal linking plan: template rules (code), topic map + anchors (LLM), guardrails (code).

The model only proposes; every rule that keeps the plan non-spammy is enforced here:
per-source / per-target caps, line quotas, one cross-line link per source, anchors that
must really appear in the source text (or in a new sentence), and no anchor repeated within one source page. Anchors for keyword-target pages come from the
target store, not the model.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit

import httpx

from . import linkgraph, llm

MAX_SUGGESTIONS = int(os.environ.get("PLAN_MAX_SUGGESTIONS", "60"))
MAX_TARGETS = int(os.environ.get("PLAN_MAX_TARGETS", "30"))
PER_SOURCE = 3          # new contextual links per source page per plan
PER_TARGET = 8          # new inlinks per target per plan
CROSS_LINE_PER_SOURCE = 1
LINE_MIN = 3            # minimum suggestions per line that has candidates
MIN_TEXT = 200          # chars of main-content text a source needs
SOURCE_TEXT_CHARS = 8000
CONCURRENCY = 4
COMMERCIAL_LINES = ("bridal", "mob", "bridesmaids", "accessories")


def norm(u: str) -> str:
    p = urlsplit(u)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}" if path else f"{p.scheme}://{p.netloc}/"


def path(u: str) -> str:
    return urlsplit(u).path or "/"


def parts(u: str) -> list[str]:
    return [x for x in path(u).strip("/").split("/") if x]


def is_article(u: str) -> bool:
    p = parts(u)
    return len(p) == 2 and p[0] in ("blog", "faq")


def is_dress(u: str) -> bool:
    p = parts(u)
    return len(p) == 2 and p[0] == "dresses"


def line_hub(u: str) -> bool:
    p = parts(u)
    return len(p) == 2 and p[0] == "collections"


def facet(u: str) -> bool:
    p = parts(u)
    return len(p) == 3 and p[0] == "collections"


def short_name(page: dict) -> str:
    t = (page.get("h1") or page.get("title") or "").strip()
    return re.split(r"\s+[|–-]\s+", t)[0].strip() or path(page["url"]).rsplit("/", 1)[-1].replace("-", " ")


def _status(failing: int, total: int) -> str:
    return "pass" if failing == 0 else "fail" if failing == total else "partial"


# ---------------------------------------------------------------- template rules

def template_rules(pages: list[dict], links: list[dict], strength: dict) -> list[dict]:
    S = {norm(p["url"]): p for p in strength["pages"]}
    P = {norm(p["url"]): p for p in pages if linkgraph.is_html_ok(p)}
    wide = linkgraph.sitewide_links(pages, links)
    out_any, out_ctx = defaultdict(set), defaultdict(set)
    for l in linkgraph.contextual_links(pages, links):
        out_ctx[norm(l["src"])].add(norm(l["dst"]))
    for l in links:
        if (l["dst"], linkgraph._anchor(l["anchor"])) in wide or l["placement"] == "footer":
            continue
        out_any[norm(l["src"])].add(norm(l["dst"]))
    stock = linkgraph.last_stock(links)
    ranges = {norm(u) for u in stock["ranges"]}
    last = {norm(u) for u in stock["pages"]}
    idx = [u for u, s in S.items() if s.get("indexable")]
    rules = []

    def rule(rid, name, desc, fix, items):
        """items: list of (url, ok: bool, detail)."""
        fails = [(u, d) for u, ok, d in items if not ok]
        rules.append({"id": rid, "name": name, "description": desc, "fix": fix, "total": len(items),
                      "failing": len(fails), "status": _status(len(fails), len(items)) if items else "pass",
                      "examples": [{"url": u, "detail": d} for u, d in fails[:50]]})

    dresses = [u for u in idx if is_dress(u)]
    items, no_range = [], []
    for d in dresses:
        slug = parts(d)[1]
        line = S[d].get("line")
        own = [r for r in ranges if slug.startswith(parts(r)[2] + "-") and S.get(r, {}).get("line") == line]
        if not own:
            no_range.append((d, False, "no range page exists for this designer"))
            continue
        items.append((d, bool(out_any[d] & set(own)), f"should link to {path(own[0])}"))
    rule("dress-range", "Dress → its range", "Every dress page links to its designer range page (breadcrumb or copy).",
         "Add a breadcrumb or 'More from <designer>' link on the dress template.", items)
    rule("dress-has-range", "Dress has a range page", "Every dress belongs to a designer range page that exists.",
         "Create the missing designer range page, or map the designer to an existing one.", no_range)
    rule("dress-siblings", "Dress → similar dresses", "Each dress links to at least 2 other dresses in its copy or a 'similar' block.",
         "Add a 'You may also like' block (same range or same line) to the dress template.",
         [(d, len({x for x in out_ctx[d] if is_dress(x)}) >= 2, f"{len({x for x in out_ctx[d] if is_dress(x)})} dress links")
          for d in dresses])
    facets = [u for u in idx if facet(u)]
    rule("facet-hub", "Collection → its line hub", "Each range/colour/price page links up to its line hub.",
         "Add a breadcrumb to the collection template.",
         [(f, norm(f.rsplit("/", 1)[0]) in out_any[f], f"should link to {path(f.rsplit('/', 1)[0])}") for f in facets])
    items = []
    for h in [u for u in idx if line_hub(u)]:
        for f in [x for x in facets if x.startswith(h + "/")]:
            items.append((f, f in out_any[h], f"not linked from {path(h)}"))
    rule("hub-facets", "Line hub → every collection", "Each line hub links to all of its range, colour and price pages.",
         "List every sub-collection on the hub page.", items)
    rule("range-related", "Range → related ranges", "Each designer range links to at least 2 other ranges in the same line in its copy.",
         "Add 'Also see' copy linking similar ranges (style, price, silhouette).",
         [(r, len({x for x in out_ctx[r] if x in ranges and x.rsplit('/', 1)[0] == r.rsplit('/', 1)[0]}) >= 2,
           "fewer than 2 related-range links") for r in ranges if r in S])
    live = ranges - last
    rule("laststock-route", "Last-stock range → live ranges",
         "A last-stock range sends visitors on to at least 2 live ranges in the same line.",
         "Add 'Can't find your size? Try …' copy linking live ranges.",
         [(r, len({x for x in out_ctx[r] if x in live and x.rsplit('/', 1)[0] == r.rsplit('/', 1)[0]}) >= 2,
           "fewer than 2 links to live ranges") for r in ranges & last if r in S])
    arts = [u for u in idx if is_article(u)]
    rule("article-commercial", "Article → a collection", "Every blog/FAQ page links to at least one collection in its copy.",
         "Link the most relevant collection from the article body.",
         [(a, any("/collections/" in x for x in out_ctx[a]), "no collection link in the copy") for a in arts])
    rule("article-inlinks", "Article has in-content links", "Every blog/FAQ page is linked from at least one other page's copy.",
         "Link it from a related article or collection intro.",
         [(a, (S[a].get("contextual_inlinks") or 0) >= 1, "only reachable via nav/listing") for a in arts])
    rule("depth", "Within 3 clicks", "Every indexable page is reachable from the home page in 3 clicks or fewer.",
         "Link deep pages from a hub or listing.",
         [(u, P.get(u, {}).get("depth") is not None and P[u]["depth"] <= 3, f"depth {P.get(u, {}).get('depth')}") for u in idx])
    return rules


# ---------------------------------------------------------------- topic map

TOPIC_SYSTEM = """You organise a bridal shop's website (Serenity Brides, Colchester, Essex) into topic clusters for internal linking.
Product lines: bridal (wedding dresses), mob (mother of the bride outfits), bridesmaids, accessories.
Reply with JSON only."""

TOPIC_USER = """Content pages (blog and FAQ):
{content}

Collection pages (commercial pages that content should feed):
{collections}

Return:
{{"topics": [{{"name": "short topic name", "line": "bridal|mob|bridesmaids|accessories|general",
              "hub": "<one collection URL from the list>", "members": ["<content URLs>"]}}],
  "page_targets": {{"<content URL>": ["<up to 3 collection URLs this page could naturally link to>"]}}}}

Rules: every content page is a member of exactly one topic. Pick hubs and targets only from the collection list.
A target in a different product line is allowed only when a reader of that page would genuinely want it
(e.g. a wedding-planning article mentioning the bridal party -> bridesmaids). Prefer specific ranges over line hubs
when the page is about that designer, style or price."""


def _fallback_topics(content: list[dict], colls: list[dict]) -> dict:
    hubs = {c["line"]: c["url"] for c in colls if line_hub(c["url"])}
    topics: dict[str, dict] = {}
    targets = {}
    for p in content:
        line = p["line"] if p["line"] in hubs else "bridal"
        t = topics.setdefault(line, {"name": f"{line} guides", "line": line, "hub": hubs.get(line), "members": []})
        t["members"].append(p["url"])
        text = f"{p.get('title') or ''} {p.get('text') or ''}".lower()
        named = [c["url"] for c in colls if facet(c["url"]) and c["line"] == line and c["name"].lower() in text]
        targets[p["url"]] = (named + [hubs[line]])[:3] if line in hubs else named[:3]
    return {"topics": list(topics.values()), "page_targets": targets}


async def topic_map(client, content: list[dict], colls: list[dict]) -> tuple[dict, float, list[str]]:
    if not content:
        return {"topics": [], "page_targets": {}}, 0.0, []
    if not llm.configured():
        return _fallback_topics(content, colls), 0.0, []
    cl = "\n".join(f"- {p['url']} | {p['line']} | {p.get('title') or ''} | queries: "
                   f"{', '.join(q['query'] for q in p.get('top_queries', [])[:3])} | {(p.get('text') or '')[:300]}"
                   for p in content)
    co = "\n".join(f"- {c['url']} | {c['line']} | {c['name']}{' | last stock' if c['last'] else ''}" for c in colls)
    data, cost = await llm.ask_json(client, TOPIC_SYSTEM, TOPIC_USER.format(content=cl, collections=co))
    cset, pset = {c["url"] for c in colls}, {p["url"] for p in content}
    warn = []
    topics = []
    seen = set()
    for t in (data or {}).get("topics", []):
        hub = norm(t.get("hub") or "")
        members = [norm(m) for m in t.get("members", []) if norm(m) in pset and norm(m) not in seen]
        seen.update(members)
        if hub not in cset:
            warn.append(f"topic '{t.get('name')}' had an unknown hub; dropped hub")
            hub = None
        topics.append({"name": str(t.get("name") or "topic")[:80], "line": t.get("line") or "general",
                       "hub": hub, "members": members})
    missing = pset - seen
    if missing:
        fb = _fallback_topics([p for p in content if p["url"] in missing], colls)
        topics += fb["topics"]
    targets = {}
    for u, ts in ((data or {}).get("page_targets") or {}).items():
        if norm(u) in pset:
            targets[norm(u)] = [norm(x) for x in ts if norm(x) in cset][:3]
    for t in topics:
        for m in t["members"]:
            targets.setdefault(m, [t["hub"]] if t["hub"] else [])
    return {"topics": topics, "page_targets": targets}, cost, warn


# ---------------------------------------------------------------- candidates and guardrails

STORE_MIN_LINKS = 3     # links each normal keyword-target page is guaranteed in select()
STORE_MIN_LINKS_TEMPLATE = 1  # per designer page (template target)
DEFAULT_STORE_OPPORTUNITY = 500.0  # for keyword targets with no stored volume (~1,700 searches/mo equivalent)
NEW_PAGE_BUMP = 1.25    # new pages have no history to earn an opportunity score


def store_opportunity(row: dict, line: str) -> float:
    """Opportunity for a keyword-target page, in the same unit as linkgraph's: extra clicks over the GSC window.
    Monthly volume x months in the window x CTR gain from a typical unranked position (~20) to TARGET_POS,
    times the line weight. E.g. 14,800/mo -> ~4,400, the order of a page with ~40k impressions at position 8."""
    months = int(os.environ.get("GSC_DAYS", "90")) / 30
    gain = linkgraph.ctr_at(linkgraph.TARGET_POS) - linkgraph.ctr_at(20)
    if (row.get("volume") or 0) > 0 and not row.get("template"):
        opp = row["volume"] * months * gain * linkgraph.LINE_WEIGHTS.get(line, 1.0)
    else:  # no stored volume (or a designer-template page, whose row volume is another page's): default
        opp = DEFAULT_STORE_OPPORTUNITY * linkgraph.LINE_WEIGHTS.get(line, 1.0)
    return round(opp * (NEW_PAGE_BUMP if row.get("is_new") else 1), 2)


def candidates(S: dict, P: dict, out_ctx: dict, topics: dict, wrong_page: list[dict], ranges: set, last: set,
               targets: list[dict] | None = None, supporting: list[dict] | None = None) -> list[dict]:
    """Scored (src, dst, kind) candidates before caps."""
    home = next((u for u in S if path(u) == "/"), None)
    targets_of = topics["page_targets"]
    topic_of = {m: t for t in topics["topics"] for m in t["members"]}

    def ok_src(s):
        p = P.get(s)
        return p and S.get(s, {}).get("indexable") and len(p.get("text") or "") >= MIN_TEXT

    cands = []

    def add(src, dst, kind, score, reason):
        if src == dst or dst in out_ctx[src] or not ok_src(src) or not S.get(dst, {}).get("indexable"):
            return
        cands.append({"src": src, "dst": dst, "kind": kind, "score": score, "reason": reason})

    # 1. Boost the pages with the most to gain.
    tlist = sorted((s for s in S.values() if s.get("opportunity", 0) > 0 and s.get("indexable")),
                   key=lambda s: -s["opportunity"])[:MAX_TARGETS]
    # Keyword-target-store pages are boosted even with no Google data, outside the MAX_TARGETS cap.
    # Already in the GSC list: the higher of the two opportunities, not the sum.
    store = {}
    for u, (r, tpl) in target_rows(targets or [], S, ranges).items():
        if S[u].get("indexable"):
            rr = {**r, "template": tpl}
            opp = store_opportunity(rr, S[u]["line"])
            if u not in store or opp > store[u][0]:
                store[u] = (opp, rr)
    tl = {norm(t["url"]): t for t in tlist}
    for u, (opp, r) in store.items():
        cur = tl.get(u) or S[u]
        tl[u] = {**S[u], "opportunity": opp, "store_row": r} if opp > cur.get("opportunity", 0) else cur
    for t in tl.values():
        dst, line = norm(t["url"]), t["line"]
        q = (t.get("top_queries") or [{}])[0]
        reason = (f"{t.get('impressions', 0):,.0f} Google impressions at avg position {t.get('position')}"
                  f" but {t.get('contextual_inlinks', 0)} in-content links"
                  + (f"; top query “{q.get('query')}”" if q.get("query") else ""))
        if t.get("store_row"):
            r = t["store_row"]
            vol = f"{r['volume']:,.0f}/mo" if (r.get("volume") or 0) > 0 and not r.get("template") else "no stored volume"
            reason = (f"Keyword target: “{r['primary_keyword']}” ({vol})"
                      + (", new page with no Google data yet" if r.get("is_new") and not t.get("impressions") else ""))
        srcs: dict[str, float] = {}
        for a, ts in targets_of.items():
            if dst in ts:
                srcs[a] = 1.0
        if dst in topic_of:  # article target: other members of its topic
            for m in topic_of[dst]["members"]:
                srcs.setdefault(m, 0.8)
        if facet(dst):
            hub = dst.rsplit("/", 1)[0]
            srcs.setdefault(hub, 0.7)
            for r in ranges:
                if r != dst and r.rsplit("/", 1)[0] == hub and not (r in last and dst in last):
                    srcs.setdefault(r, 0.9 if r in last else 0.6)  # last-stock ranges pass visitors on
        if line_hub(dst) and home:
            srcs.setdefault(home, 0.7)
        for s, rel in srcs.items():
            sl = S.get(s, {}).get("line")
            cross = sl in COMMERCIAL_LINES and line in COMMERCIAL_LINES and sl != line
            if cross and rel < 1.0:
                continue  # cross-line only where the topic map says the reader wants it
            pr = S.get(s, {}).get("pagerank", 0)
            add(s, dst, "cross_line" if cross else "boost", t["opportunity"] * rel * (1 + pr / 100), reason)
            if dst in store and cands and cands[-1]["dst"] == dst and cands[-1]["src"] == s:
                cands[-1]["store"] = True
                cands[-1]["store_min"] = STORE_MIN_LINKS_TEMPLATE if store[dst][1].get("template") else STORE_MIN_LINKS

    # 1b. Approved supporting pages link to their target page first, with the approved anchor and sentence.
    for r in supporting or []:
        src, dst = norm(r["supporting_page"]), norm(r["target_page"])
        if dst not in S:
            continue
        before = len(cands)
        add(src, dst, "supporting", (tl.get(dst) or S[dst]).get("opportunity", 0) + 1,
            f"Approved supporting page: {r.get('reason') or 'readers of this page go on to the target'}")
        if len(cands) > before:
            cands[-1]["approved"] = r
            cands[-1]["store"] = dst in store

    # 2. Queries landing on the wrong page: link from where Google sends them to the right page.
    by_pair = defaultdict(list)
    for w in wrong_page:
        by_pair[(norm(w["page"]), norm(w["suggested"]))].append(w)
    for (src, dst), ws in by_pair.items():
        impr = sum(w["impressions"] for w in ws)
        qs = ", ".join(f"“{w['query']}”" for w in sorted(ws, key=lambda w: -w["impressions"])[:3])
        add(src, dst, "wrong_page", impr / 5, f"Google shows this page for {qs} ({impr:,.0f} impressions); the right page is {path(dst)}")

    # 3. Last-stock ranges route visitors to live ranges in the same line.
    for r in last & ranges:
        hub = r.rsplit("/", 1)[0]
        live = sorted((x for x in ranges - last if x.rsplit("/", 1)[0] == hub and x in S),
                      key=lambda x: -(S[x].get("opportunity", 0) + S[x].get("pagerank", 0) / 10))
        have = sum(1 for x in out_ctx[r] if x in live)
        for x in live[:max(0, 2 - have)]:
            add(r, x, "route", 5 + S[x].get("opportunity", 0),
                f"Last-stock range: send visitors who can't find their size on to {short_name(P.get(x, S[x]))}")
    return cands


def select(cands: list[dict], S: dict) -> list[dict]:
    """Apply caps and per-line quotas. Fixes (route, wrong_page) go first."""
    per_src, per_dst, cross = Counter(), Counter(), Counter()
    chosen, seen = [], set()

    def take(c):
        key = (c["src"], c["dst"])
        if key in seen or per_src[c["src"]] >= PER_SOURCE or per_dst[c["dst"]] >= PER_TARGET:
            return False
        if c["kind"] == "cross_line" and cross[c["src"]] >= CROSS_LINE_PER_SOURCE:
            return False
        seen.add(key)
        per_src[c["src"]] += 1
        per_dst[c["dst"]] += 1
        if c["kind"] == "cross_line":
            cross[c["src"]] += 1
        chosen.append(c)
        return True

    for c in sorted((c for c in cands if c["kind"] in ("route", "wrong_page")), key=lambda c: -c["score"]):
        take(c)
    # Approved supporting pages come next; they count toward the target's minimum below.
    have = Counter()
    for c in sorted((c for c in cands if c["kind"] == "supporting"), key=lambda c: -c["score"]):
        if take(c):
            have[c["dst"]] += 1
    # Keyword-target-store pages get their best links (minimum per page) before the line quotas can crowd them out.
    by_store = defaultdict(list)
    for c in cands:
        if c.get("store") and c["kind"] == "boost":
            by_store[c["dst"]].append(c)
    for dst, cs in by_store.items():
        n = have[dst]
        for c in sorted(cs, key=lambda c: -c["score"]):
            if n < cs[0].get("store_min", STORE_MIN_LINKS) and take(c):
                n += 1
    boost = [c for c in cands if c["kind"] in ("boost", "cross_line")]
    by_line = defaultdict(list)
    for c in boost:
        by_line[S[c["dst"]]["line"]].append(c)
    budget = max(0, MAX_SUGGESTIONS - len(chosen))
    weight = {l: sum(c["score"] for c in cs) for l, cs in by_line.items()}
    total = sum(weight.values()) or 1
    quota = {l: max(min(LINE_MIN, len(cs)), math.floor(budget * weight[l] / total)) for l, cs in by_line.items()}
    leftovers = []
    for l, cs in by_line.items():
        n = 0
        for c in sorted(cs, key=lambda c: -c["score"]):
            if n < quota[l] and take(c):
                n += 1
            elif n >= quota[l]:
                leftovers.append(c)
    for c in sorted(leftovers, key=lambda c: -c["score"]):
        if len(chosen) >= MAX_SUGGESTIONS:
            break
        take(c)
    return chosen


# ---------------------------------------------------------------- anchors

ANCHOR_SYSTEM = """You write internal links for a bridal shop website (Serenity Brides, Colchester). British English.
Links must help the reader, read naturally, and never look like SEO stuffing.
"Last stock" pages are still for sale and worth linking while stock lasts: ready to wear, take it home on the day,
often a lower price. Don't skip a link just because the target is last stock. Reply with JSON only."""

ANCHOR_USER = """Source page: {src} — {title}
Source text:
<<<
{text}
>>>

Proposed links from this page:
{targets}

For each target return an object:
{{"dst": "<target URL>", "skip": false,
  "anchor": "<2-6 word anchor text>",
  "in_text": true if the anchor is a phrase copied EXACTLY from the source text, else false,
  "sentence": null if in_text, else ONE new sentence (max 30 words) to add to the page, in its tone, containing the anchor,
  "alt_anchor": "<a different, less keyword-heavy anchor that also appears in the same text or sentence>"}}
Set "skip": true (with "why") when there is no natural way to link this target from this page.
Prefer existing phrases. Vary anchors; don't just repeat the target's search query.
Where a target lists "anchor must be one of", use exactly one of those phrases as the anchor and never invent another.
Reply: {{"links": [ ... ]}}"""


def _approved_anchor(row: dict, text: str) -> dict | None:
    """The anchor and sentence John approved. An in-text anchor must still be on the page; else the sentence is drafted."""
    anchor, sentence = (row.get("anchor") or "").strip(), (row.get("sentence") or "").strip()
    if not anchor:
        return None
    if row.get("in_text"):
        span = _find(text, anchor)
        if span:
            return {"anchor": text[span[0]:span[1]], "in_text": True, "sentence": None,
                    "context": _context(text, span), "alt_anchor": None}
    if sentence and _find(sentence, anchor):
        return {"anchor": anchor, "in_text": False, "sentence": sentence, "context": None, "alt_anchor": None}
    return None


def _find(text: str, phrase: str) -> tuple[int, int] | None:
    m = re.search(r"\b" + re.escape(phrase.strip()).replace(r"\ ", r"\s+") + r"\b", text, re.I)
    return (m.start(), m.end()) if m else None


def _context(text: str, span: tuple[int, int]) -> str:
    a = max(text.rfind(". ", 0, span[0]) + 2, span[0] - 160, 0)
    b = text.find(". ", span[1])
    b = min(b + 1 if b >= 0 else len(text), span[1] + 160)
    return text[a:b].strip()


def anchor_type(anchor: str, target: dict, brand: str = "serenity") -> str:
    a = re.sub(r"\s+", " ", anchor.lower()).strip()
    if brand in a:
        return "branded"
    qs = {q["query"].lower() for q in target.get("top_queries", [])[:3]}
    if a in qs or a == short_name(target).lower():
        return "exact"
    words = set(a.split())
    if any(len(words & set(q.split())) >= 2 for q in qs):
        return "partial"
    return "descriptive"


def _fallback_anchor(src_text: str, target: dict) -> dict:
    name = short_name(target)
    span = _find(src_text, name)
    if span:
        return {"anchor": src_text[span[0]:span[1]], "in_text": True, "sentence": None, "alt_anchor": None}
    return {"anchor": name, "in_text": False, "sentence": f"Take a look at our {name}.", "alt_anchor": None}


def validate(link: dict, text: str, target: dict) -> dict | None:
    """Accept a model link only if the anchor really is in the text, or in the new sentence."""
    anchor = (link.get("anchor") or "").strip()
    if link.get("skip") or not anchor or len(anchor.split()) > 8:
        return None
    span = _find(text, anchor)
    if span:
        return {"anchor": text[span[0]:span[1]], "in_text": True, "sentence": None,
                "context": _context(text, span), "alt_anchor": link.get("alt_anchor")}
    sentence = (link.get("sentence") or "").strip()
    if sentence and _find(sentence, anchor) and len(sentence.split()) <= 40:
        return {"anchor": anchor, "in_text": False, "sentence": sentence, "context": None,
                "alt_anchor": link.get("alt_anchor")}
    return None


def _key(a: str) -> str:
    return re.sub(r"\s+", " ", a.lower()).strip()


def _designer(page: dict) -> str:
    n = re.sub(r"\s+wedding dresses.*$", "", short_name(page), flags=re.I).strip()
    return n or path(page["url"]).rsplit("/", 1)[-1].replace("-", " ").title()


def target_keywords(row: dict, page: dict, template: bool = False) -> list[str]:
    """Primary first, then supporting; {Designer} expands to the page's designer name.
    A template row applied to a page other than its example keeps only the primary (supporting is example-specific)."""
    kws = [row["primary_keyword"]] + ([] if template else list(row.get("supporting") or []))
    out, seen = [], set()
    for k in kws:
        k = k.replace("{Designer}", _designer(page)).strip()
        if k and _key(k) not in seen:
            seen.add(_key(k))
            out.append(k)
    return out


def target_rows(rows: list[dict], S: dict, ranges: set) -> dict[str, tuple[dict, bool]]:
    """dst URL -> (target row, is_template_page). Same matching as target_map."""
    by_path = {}
    for r in rows or []:
        by_path.setdefault(path(r["path"]).rstrip("/") or "/", r)
    out = {}
    for u in S:
        r = by_path.get(path(u).rstrip("/") or "/")
        if r:
            out[u] = (r, False)
    for r in (rows or []):
        if "{Designer}" not in r["primary_keyword"]:
            continue
        for u in ranges:
            if u in S and u not in out and S[u].get("line") == r.get("product_line"):
                out[u] = (r, True)
    return out


def target_map(rows: list[dict], S: dict, ranges: set) -> dict[str, list[str]]:
    """dst URL -> keywords, matching target paths to site URLs via path(). A row whose primary contains {Designer}
    is the designer template: it also covers every designer range page (same line) without a target of its own."""
    return {u: target_keywords(r, S[u], template=t) for u, (r, t) in target_rows(rows, S, ranges).items()}


def _target_anchor(kws: list[str], text: str, used: set, usage: Counter, dst: str, choice: dict | None) -> dict | None:
    """Pick the anchor for a target page from its keywords: least used across sources first, primary first on ties.
    The model's choice is honoured only if it is one of the keywords, unused here and valid in the text."""
    order = sorted((k for k in kws if _key(k) not in used), key=lambda k: (usage[(dst, _key(k))], kws.index(k)))
    if not order:
        return None
    if choice:
        v = validate(choice, text, {})
        if v and _key(v["anchor"]) in {_key(k) for k in order}:
            return v
    k = order[0]
    span = _find(text, k)
    if span:
        return {"anchor": text[span[0]:span[1]], "in_text": True, "sentence": None,
                "context": _context(text, span), "alt_anchor": None}
    return {"anchor": k, "in_text": False, "sentence": f"Browse our {k}.", "context": None, "alt_anchor": None}


async def anchors_for(client, src: str, page: dict, items: list[dict], S: dict, sem,
                      tmap: dict | None = None, usage: Counter | None = None) -> tuple[list[dict], float, list[str]]:
    tmap = tmap or {}
    usage = usage if usage is not None else Counter()
    text = (page.get("text") or "")[:SOURCE_TEXT_CHARS]
    got, cost = {}, 0.0
    asked = [c for c in items if not c.get("approved")]
    if llm.configured() and asked:
        tl = "\n".join(
            f"- {c['dst']} | {short_name(S[c['dst']])} | line: {S[c['dst']]['line']} | searches: "
            f"{', '.join(q['query'] for q in S[c['dst']].get('top_queries', [])[:3]) or 'n/a'}"
            f"{' | last stock: ready to wear, still for sale' if S[c['dst']].get('discontinued') else ''}"
            f"{' | anchor must be one of: ' + '; '.join(tmap[c['dst']]) if c['dst'] in tmap else ''}"
            for c in asked)
        async with sem:
            data, cost = await llm.ask_json(client, ANCHOR_SYSTEM, ANCHOR_USER.format(
                src=src, title=page.get("title") or "", text=text, targets=tl), max_tokens=4000)
        got = {norm(l.get("dst") or ""): l for l in (data or {}).get("links", []) if isinstance(l, dict)}
    out, warn, used = [], [], set()
    for c in sorted(items, key=lambda c: (not c.get("approved"), -c["score"])):
        l = got.get(c["dst"])
        if c.get("approved"):
            v = _approved_anchor(c["approved"], text)
            if not v or _key(v["anchor"]) in used:
                warn.append(f"dropped approved {path(c['src'])} → {path(c['dst'])}: anchor not usable on this page")
                continue
        elif c["dst"] in tmap:
            v = _target_anchor(tmap[c["dst"]], text, used, usage, c["dst"], l)
            if not v:
                warn.append(f"dropped {path(c['src'])} → {path(c['dst'])}: every target keyword already used as an anchor on this page")
                continue
        elif llm.configured():
            v = validate(l, text, S[c["dst"]]) if l else None
            if v and _key(v["anchor"]) in used:
                alt = (l.get("alt_anchor") or "").strip()
                hay = v.get("context") or v.get("sentence") or ""
                v = {**v, "anchor": alt} if alt and _key(alt) not in used and _find(hay, alt) else None
            if not v:
                if l and l.get("skip"):
                    warn.append(f"model skipped {path(c['src'])} → {path(c['dst'])}: {str(l.get('why') or '')[:100]}")
                else:
                    warn.append(f"dropped {path(c['src'])} → {path(c['dst'])}: anchor not found in text or sentence")
                continue
        else:
            v = _fallback_anchor(text, S[c["dst"]])
            span = _find(text, v["anchor"]) if v["in_text"] else None
            v["context"] = _context(text, span) if span else None
            if _key(v["anchor"]) in used:
                warn.append(f"dropped {path(c['src'])} → {path(c['dst'])}: anchor already used on this page")
                continue
        used.add(_key(v["anchor"]))
        if c["dst"] in tmap:
            usage[(c["dst"], _key(v["anchor"]))] += 1
        out.append({**c, **v, "anchor_type": anchor_type(v["anchor"], S[c["dst"]])})
    return out, cost, warn


# ---------------------------------------------------------------- entry point

async def build_plan(pages: list[dict], links: list[dict], strength: dict, *, progress=None,
                     targets: list[dict] | None = None, supporting: list[dict] | None = None) -> dict:
    say = progress or (lambda m: None)
    S = {norm(p["url"]): {**p, "url": norm(p["url"])} for p in strength["pages"]}
    P = {norm(p["url"]): p for p in pages if linkgraph.is_html_ok(p)}
    for u, s in S.items():
        s.setdefault("h1", P.get(u, {}).get("h1"))
    out_ctx = defaultdict(set)
    for l in linkgraph.contextual_links(pages, links):
        out_ctx[norm(l["src"])].add(norm(l["dst"]))
    stock = linkgraph.last_stock(links)
    ranges = {norm(u) for u in stock["ranges"]}
    last = {norm(u) for u in stock["pages"]}
    warnings: list[str] = []
    cost = 0.0

    say("Checking template rules")
    rules = template_rules(pages, links, strength)

    content = [{**S[u], "text": P[u].get("text")} for u in S if is_article(u) and S[u].get("indexable") and u in P]
    colls = [{"url": u, "line": S[u]["line"], "name": short_name(S[u]), "last": u in last}
             for u in S if (line_hub(u) or facet(u)) and S[u].get("indexable")]
    async with httpx.AsyncClient() as client:
        say("Building topic map" + (" with the model" if llm.configured() else ""))
        try:
            topics, c, w = await topic_map(client, content, colls)
        except (llm.LLMError, httpx.HTTPError) as e:
            warnings.append(f"Topic map fell back to rules: {e}")
            topics, c, w = _fallback_topics(content, colls), 0.0, []
        cost += c
        warnings += w

        say("Choosing link candidates")
        chosen = select(candidates(S, P, out_ctx, topics, strength.get("wrong_page", []), ranges, last, targets, supporting), S)
        by_src = defaultdict(list)
        for c_ in chosen:
            by_src[c_["src"]].append(c_)

        say(f"Writing anchors for {len(chosen)} links on {len(by_src)} pages")
        sem = asyncio.Semaphore(CONCURRENCY)
        tmap = target_map(targets or [], S, ranges)
        usage: Counter = Counter()

        async def one(src, items):
            try:
                return await anchors_for(client, src, P[src], items, S, sem, tmap, usage)
            except (llm.LLMError, httpx.HTTPError) as e:
                return [], 0.0, [f"anchors failed for {path(src)}: {e}"]

        results = await asyncio.gather(*(one(s, it) for s, it in by_src.items()))
    linked = []
    for ls, c_, w in results:
        linked += ls
        cost += c_
        warnings += w

    suggestions = [{
        "src": l["src"], "dst": l["dst"], "anchor": l["anchor"], "anchor_type": l["anchor_type"],
        "in_text": l["in_text"], "sentence": l.get("sentence"), "context": l.get("context"),
        "reason": l["reason"], "kind": l["kind"], "temporary": l["dst"] in last,
        "line": S[l["dst"]]["line"], "priority": round(l["score"], 2),
    } for l in sorted(linked, key=lambda l: -l["score"])]
    by_line = Counter(s["line"] for s in suggestions)
    say("Done")
    return {
        "model": llm.MODEL if llm.configured() else None, "cost": round(cost, 4), "llm_used": llm.configured(),
        "warnings": warnings,
        "topics": [{"id": f"t{i}", **t} for i, t in enumerate(topics["topics"], 1)],
        "template_rules": rules, "suggestions": suggestions,
        "stats": {"targets": len({s["dst"] for s in suggestions}), "sources": len({s["src"] for s in suggestions}),
                  "by_line": dict(by_line)},
    }
