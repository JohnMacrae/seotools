"""Link map: per keyword target, the contextual links in, their anchors and plan status, template links, flags.
Pure: takes target rows, crawl pages and links, plan suggestions and Search Console rows; returns dicts."""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import urlsplit

from . import linkplan as lp
from .match import _stem_key, contains_loose, contains_variant, first_sentence, url_has

TEMPLATE_MIN_SRCS = 10    # same (dst, anchor) from this many src pages of one URL section = template link
TEMPLATE_PLACEMENTS = ("nav", "header", "footer", "aside", "trail")
MIN_CONTEXTUAL = 3        # distinct supporting pages with contextual links below this = "needs links"
MONOTONY_SHARE = 0.6      # more than this share of contextual anchors identical = "anchor monotony"
MONOTONY_MIN_LINKS = 3
PHASE2_PREFIXES = ("/collections/mother-of-the-bride", "/collections/bridesmaids")


def ukey(u: str) -> str:
    """URL compared without scheme, www or trailing slash."""
    p = urlsplit(u)
    host = (p.hostname or "").removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" if p.path.rstrip("/") else f"{host}/"


def _a(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _section(u: str) -> str:
    segs = [x for x in urlsplit(u).path.split("/") if x]
    return segs[0] if segs else ""


def template_pairs(links: list[dict], dst_keys: set[str]) -> set[tuple[str, str]]:
    """(dst key, anchor) pairs linked from >= TEMPLATE_MIN_SRCS src pages of one URL section."""
    srcs: dict[tuple[str, str, str], set] = defaultdict(set)
    for l in links:
        k = ukey(l["dst"])
        if k in dst_keys and l["src"] != l["dst"]:
            srcs[(k, _a(l["anchor"]), _section(l["src"]))].add(ukey(l["src"]))
    return {(k, a) for (k, a, _s), s in srcs.items() if len(s) >= TEMPLATE_MIN_SRCS}


def _wavg(rows: list[dict]) -> float | None:
    imp = sum(r["impressions"] or 0 for r in rows)
    if not imp:
        return None
    return round(sum((r["position"] or 0) * (r["impressions"] or 0) for r in rows) / imp, 2)


def expand(rows: list[dict], pages: list[dict], S: dict, ranges: set) -> list[dict]:
    """One {target_id, product_line, keywords, target_page} per target page. Template rows expand per matching page;
    rows whose page is not in S (not crawled, or new) still get an entry."""
    base = ""
    if pages:
        sp = urlsplit(pages[0]["url"])
        base = f"{sp.scheme}://{sp.netloc}"
    direct = {lp.path(r["path"]).rstrip("/") or "/" for r in rows}
    S = {lp.norm(u): v for u, v in S.items()}
    out = []
    for r in rows:
        p = lp.path(r["path"]).rstrip("/") or "/"
        tm = lp.target_map([r], S, ranges)
        for u, kws in tm.items():
            if (lp.path(u).rstrip("/") or "/") != p and (lp.path(u).rstrip("/") or "/") in direct:
                continue  # has a target of its own
            out.append({"target_id": r["id"], "product_line": r.get("product_line"), "keywords": kws, "target_page": u})
        if not any(e["target_id"] == r["id"] for e in out):
            u = lp.norm(base + p) if base else p
            kws = lp.target_keywords(r, {"url": u, "h1": None})
            out.append({"target_id": r["id"], "product_line": r.get("product_line"), "keywords": kws, "target_page": u})
    out.sort(key=lambda e: (e["target_id"], e["target_page"]))
    return out


def build(rows: list[dict], pages: list[dict], links: list[dict], S: dict, ranges: set,
          suggestions: list[dict], gsc_rows: list[dict], has_plan: bool = True,
          supporting: list[dict] | None = None) -> dict:
    """rows: keyword targets (targets.list_all). suggestions: plan progress rows (src, dst, planned_anchor, status).
    gsc_rows: {page, query, impressions, position}."""
    entries = expand(rows, pages, S, ranges)
    tkeys = {ukey(e["target_page"]) for e in entries}
    page_status = {ukey(p["url"]): p["status"] for p in pages}
    page_by_key = {ukey(p["url"]): p for p in pages}
    tpl = template_pairs(links, tkeys)

    in_links: dict[str, list[dict]] = defaultdict(list)
    for l in links:
        if l["src"] != l["dst"]:
            in_links[ukey(l["dst"])].append(l)
    sugg_by_dst: dict[str, list[dict]] = defaultdict(list)
    sugg_by_pair = {}
    for s in suggestions:
        sugg_by_dst[ukey(s["dst"])].append(s)
        sugg_by_pair[(ukey(s["src"]), ukey(s["dst"]))] = s

    sup_by_dst: dict[str, list[dict]] = defaultdict(list)
    for r in supporting or []:
        if r["status"] != "rejected":
            sup_by_dst[ukey(r["target_page"])].append(r)

    gsc_by_q: dict[str, list[dict]] = defaultdict(list)
    for g in gsc_rows:
        gsc_by_q[g["query"].lower()].append(g)

    out = []
    for e in entries:
        tk = ukey(e["target_page"])
        kws = e["keywords"]
        # Search Console
        mine = [g for kw in {k.lower() for k in kws} for g in gsc_by_q.get(kw, [])]
        by_page: dict[str, list[dict]] = defaultdict(list)
        for g in mine:
            by_page[ukey(g["page"])].append(g)
        top = max(by_page, key=lambda k: sum(r["impressions"] or 0 for r in by_page[k])) if by_page else None
        top_url = next(g["page"] for g in by_page[top]) if top else None
        tgt_rows = by_page.get(tk, [])
        # links in
        contextual, template, tpl_all = [], {}, []
        seen = set()
        for l in in_links.get(tk, []):
            a = _a(l["anchor"])
            is_tpl = (tk, a) in tpl or l["placement"] in TEMPLATE_PLACEMENTS
            if is_tpl:
                t = template.setdefault(a, {"anchor": (l["anchor"] or "").strip(), "srcs": set()})
                t["srcs"].add(l["src"])
            elif l["placement"] == "main" and (ukey(l["src"]), a) not in seen:
                seen.add((ukey(l["src"]), a))
                s = sugg_by_pair.get((ukey(l["src"]), tk))
                contextual.append({"src": l["src"], "anchor": (l["anchor"] or "").strip(),
                                   "planned_anchor": s["planned_anchor"] if s else None,
                                   "status": s["status"] if s else "existing"})
        contextual.sort(key=lambda c: (c["src"], c["anchor"]))
        template_out = sorted(({"anchor": t["anchor"], "src_count": len(t["srcs"]), "_srcs": sorted(t["srcs"])}
                               for t in template.values()), key=lambda t: (-t["src_count"], t["anchor"]))
        planned_missing = [{"src": s["src"], "planned_anchor": s["planned_anchor"], "status": s["status"]}
                           for s in sugg_by_dst.get(tk, []) if s["status"] not in ("done", "gone")]
        planned_missing.sort(key=lambda s: s["src"])

        # flags
        flags = []
        if len({ukey(c["src"]) for c in contextual}) < MIN_CONTEXTUAL:
            flags.append("needs links")
        counts = Counter(_a(c["anchor"]) for c in contextual)
        if len(contextual) >= MONOTONY_MIN_LINKS and counts.most_common(1)[0][1] > MONOTONY_SHARE * len(contextual):
            flags.append("anchor monotony")
        own = [k for k in kws if "{" not in k]
        conflicts = []
        checks = [(c["src"], c["anchor"], 1, "contextual") for c in contextual] + \
                 [(t["_srcs"][0], t["anchor"], t["src_count"], "template") for t in template_out]
        for src, anchor, n, kind in checks:
            # the longest keyword (any target) the anchor contains decides whose anchor it is
            matches = [(k, x) for x in entries for k in x["keywords"] if "{" not in k and contains_variant(anchor, k)]
            if not matches:
                continue
            longest = max(len(k.split()) for k, _ in matches)
            best = [(k, x) for k, x in matches if len(k.split()) == longest]
            if any(x["target_page"] == e["target_page"] for _, x in best):
                continue
            kw, o = best[0]
            conflicts.append({"src": src, "anchor": anchor, "other_target_id": o["target_id"],
                              "keyword": kw, "kind": kind, "count": n})
        pg = page_by_key.get(tk)
        missing = None
        if pg is not None and kws:
            prim, accept = kws[0], [kws[0]] + [k for k in kws[1:] if _stem_key(k) == _stem_key(kws[0])]
            fields = {"title": pg.get("title"), "h1": pg.get("h1"),
                      "first sentence": first_sentence(pg.get("text") or "")}
            missing = [f for f, v in fields.items() if not any(contains_loose(v or "", k) for k in accept)]
            if urlsplit(pg["url"]).path.strip("/") and not any(url_has(pg["url"], k) for k in accept):  # home page: no slug
                missing.insert(1, "url")
            if missing:
                flags.append("keyword missing: " + ", ".join(missing))
        if conflicts:
            flags.append("anchor conflict")
        wrong = bool(top) and top != tk
        if wrong:
            flags.append("wrong page ranking")
        if has_plan and not sugg_by_dst.get(tk) and not lp.line_hub(e["target_page"]):  # planner prefers specific ranges over hubs
            flags.append("not in plan")
        sup = sup_by_dst.get(tk, [])
        n_approved = sum(r["status"] == "approved" for r in sup)
        if not n_approved and (e.get("product_line") or "bridal") == "bridal":
            flags.append("no supporting pages")
        for t in template_out:
            del t["_srcs"]
        out.append({
            **e,
            "crawl_status": str(page_status[tk]) if tk in page_status else "not crawled",
            "gsc_top_page": top_url,
            "gsc_top_page_position": _wavg(by_page[top]) if top else None,
            "gsc_target_page_position": _wavg(tgt_rows) if tgt_rows else None,
            "wrong_page": wrong,
            "contextual": contextual, "planned_missing": planned_missing, "template": template_out,
            "conflicts": conflicts, "flags": flags, "onpage_missing": missing,
            "supporting_approved": n_approved, "supporting": sup,
        })

    other = []
    for s in sorted(suggestions, key=lambda s: (s["dst"], s["src"])):
        if ukey(s["dst"]) in tkeys:
            continue
        if urlsplit(s["dst"]).path.rstrip("/").startswith(PHASE2_PREFIXES):
            continue
        other.append({"src": s["src"], "dst": s["dst"], "planned_anchor": s["planned_anchor"], "status": s["status"]})
    return {"entries": out, "other_links": other}
