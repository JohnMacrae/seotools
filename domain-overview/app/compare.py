"""Competitor gap analysis: is the gap on-page (relevance/content) or off-page (links/authority)?

Data used (DataForSEO):
- Labs domain_intersection (intersections=true)  -> keywords both rank for, with each ranking page's
  referring domains and the domain's rank score
- Labs domain_intersection (intersections=false) -> keywords the competitor ranks for and we don't (content gap)
- Backlinks summary                              -> domain-level authority for every site
- Backlinks domain_intersection                  -> referring domains linking to competitors but not us (link gap)
- Backlinks backlinks (one_per_domain)           -> the page each gap domain links from, per competitor
- On-Page instant_pages                          -> homepage on-page audit for every site
"""
from __future__ import annotations

import asyncio
import os
import re

from . import dfs

SHARED_LIMIT = int(os.environ.get("COMPARE_SHARED_LIMIT", "200"))
MISSING_LIMIT = int(os.environ.get("COMPARE_MISSING_LIMIT", "200"))
LINKGAP_LIMIT = int(os.environ.get("COMPARE_LINKGAP_LIMIT", "200"))
MIN_VOLUME = int(os.environ.get("COMPARE_MIN_VOLUME", "10"))

# Thresholds for calling a lost keyword "off-page"
OFFPAGE_DOMAIN_RATIO = float(os.environ.get("OFFPAGE_DOMAIN_RATIO", "1.25"))  # their domain rank / ours
OFFPAGE_PAGE_RATIO = float(os.environ.get("OFFPAGE_PAGE_RATIO", "2.0"))       # their page ref. domains / ours


def _brand(domain: str) -> str:
    return re.sub(r"[^a-z0-9]", "", domain.split(".")[0])


def _is_branded(keyword: str, brands: list[str]) -> bool:
    k = re.sub(r"[^a-z0-9]", "", keyword.lower())
    return any(b and len(b) >= 4 and b in k for b in brands)


def _path(url: str | None) -> str | None:
    if not url:
        return None
    p = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
    return p or "/"


def _el(e: dict | None) -> dict:
    e = e or {}
    bl = e.get("backlinks_info") or {}
    ri = e.get("rank_info") or {}
    return {
        "position": e.get("rank_group"),
        "url": e.get("url"),
        "title": e.get("title"),
        "page_rd": bl.get("referring_main_domains", bl.get("referring_domains")),
        "page_rank": ri.get("page_rank"),
        "domain_rank": ri.get("main_domain_rank"),
        "traffic": round(e.get("etv") or 0, 1),
    }


def classify(us: dict, them: dict) -> tuple[str, str]:
    """Return (cause, reason) for a keyword where the competitor outranks us."""
    ud, td = us.get("domain_rank"), them.get("domain_rank")
    up, tp = us.get("page_rd"), them.get("page_rd")
    if None in (ud, td) and None in (up, tp):
        return "unknown", "No authority data for one of the pages"
    dom_ratio = (td + 1) / (ud + 1) if None not in (ud, td) else 1.0
    page_ratio = (tp + 1) / (up + 1) if None not in (up, tp) else 1.0
    bits = []
    if None not in (ud, td):
        bits.append(f"domain rank {td} vs your {ud}")
    if None not in (up, tp):
        bits.append(f"page ref. domains {tp} vs your {up}")
    reason = "; ".join(bits)
    if dom_ratio >= OFFPAGE_DOMAIN_RATIO or page_ratio >= OFFPAGE_PAGE_RATIO:
        return "off-page", f"They have more authority ({reason})"
    if dom_ratio <= 0.8 and page_ratio <= 1.0:
        return "on-page", f"You have more authority but still rank lower ({reason})"
    return "on-page", f"Authority is similar, so relevance/content decides ({reason})"


async def run(domain: str, competitors: list[str], location_code: int, language_code: str) -> dict:
    loc = {"location_code": location_code, "language_code": language_code}
    async with dfs.client() as c:
        jobs = {}
        for comp in competitors:
            jobs[("shared", comp)] = dfs.post_path(c, "dataforseo_labs/google/domain_intersection/live", {
                **loc, "target1": comp, "target2": domain, "intersections": True, "limit": SHARED_LIMIT,
                "item_types": ["organic"],
                "order_by": ["keyword_data.keyword_info.search_volume,desc"],
            }, mock_name="compare_shared")
            jobs[("missing", comp)] = dfs.post_path(c, "dataforseo_labs/google/domain_intersection/live", {
                **loc, "target1": comp, "target2": domain, "intersections": False, "limit": MISSING_LIMIT,
                "item_types": ["organic"],
                "filters": [["keyword_data.keyword_info.search_volume", ">=", MIN_VOLUME]],
                "order_by": ["keyword_data.keyword_info.search_volume,desc"],
            }, mock_name="compare_missing")
        for d in [domain, *competitors]:
            jobs[("summary", d)] = dfs.post_path(c, "backlinks/summary/live", {
                "target": d, "include_subdomains": True, "exclude_internal_backlinks": True,
            }, mock_name="compare_summary")
            jobs[("page", d)] = dfs.post_path(c, "on_page/instant_pages", {
                "url": f"https://{d}/", "enable_javascript": False,
            }, mock_name="compare_instant")
        jobs[("linkgap", "")] = _linkgap(c, domain, competitors)

        keys = list(jobs)
        out = await asyncio.gather(*jobs.values(), return_exceptions=True)

    res, errors, cost = {}, [], 0.0
    for k, v in zip(keys, out):
        if isinstance(v, Exception):
            errors.append(f"{k[0]} {k[1]}: {v}".strip())
            res[k] = None
        else:
            res[k], c_ = v
            cost += c_

    brands = [_brand(d) for d in [domain, *competitors]]

    # ---- Authority table -------------------------------------------------------------------
    sites = []
    for d in [domain, *competitors]:
        s = res.get(("summary", d)) or {}
        if "items" in s and s["items"]:
            s = s["items"][0]
        sites.append({
            "domain": d,
            "is_you": d == domain,
            "rank": s.get("rank"),
            "referring_domains": s.get("referring_main_domains", s.get("referring_domains")),
            "referring_domains_nofollow": s.get("referring_main_domains_nofollow"),
            "backlinks": s.get("backlinks"),
            "spam_score": s.get("backlinks_spam_score"),
            "onpage": _onpage(res.get(("page", d))),
        })

    # ---- Shared keywords: where they beat us, and why ---------------------------------------
    by_kw: dict[str, dict] = {}
    for comp in competitors:
        for it in (res.get(("shared", comp)) or {}).get("items") or []:
            kd = it.get("keyword_data") or {}
            kw = kd.get("keyword")
            if not kw or _is_branded(kw, brands):
                continue
            info = kd.get("keyword_info") or {}
            them, us = _el(it.get("first_domain_serp_element")), _el(it.get("second_domain_serp_element"))
            row = by_kw.setdefault(kw, {
                "keyword": kw,
                "volume": info.get("search_volume"),
                "kd": (kd.get("keyword_properties") or {}).get("keyword_difficulty"),
                "you": us,
                "competitors": {},
            })
            row["competitors"][comp] = them
    shared = []
    for row in by_kw.values():
        yp = row["you"]["position"] or 999
        beating = {c: e for c, e in row["competitors"].items() if (e["position"] or 999) < yp}
        if beating:
            best_c, best = min(beating.items(), key=lambda kv: kv[1]["position"] or 999)
            cause, reason = classify(row["you"], best)
            if _path(row["you"]["url"]) == "/" and _path(best["url"]) not in ("/", None):
                reason += ". Your homepage is the ranking page; they have a dedicated page"
                if cause == "off-page":
                    reason += " (a dedicated page may help too)"
            row.update(status="losing", leader=best_c, cause=cause, reason=reason)
        else:
            row.update(status="winning", leader=None, cause=None, reason="You rank above these competitors")
        shared.append(row)
    shared.sort(key=lambda r: (r["status"] != "losing", -(r["volume"] or 0)))

    # ---- Missing keywords (content gap) -----------------------------------------------------
    miss: dict[str, dict] = {}
    for comp in competitors:
        for it in (res.get(("missing", comp)) or {}).get("items") or []:
            kd = it.get("keyword_data") or {}
            kw = kd.get("keyword")
            if not kw or _is_branded(kw, brands):
                continue
            info = kd.get("keyword_info") or {}
            e = _el(it.get("first_domain_serp_element"))
            row = miss.setdefault(kw, {
                "keyword": kw,
                "volume": info.get("search_volume"),
                "kd": (kd.get("keyword_properties") or {}).get("keyword_difficulty"),
                "competitors": {},
            })
            row["competitors"][comp] = e
    missing = []
    for row in miss.values():
        best_c, best = min(row["competitors"].items(), key=lambda kv: kv[1]["position"] or 999)
        row.update(n_competitors=len(row["competitors"]), best_competitor=best_c,
                   best_position=best["position"], best_url=best["url"])
        missing.append(row)
    missing.sort(key=lambda r: (-r["n_competitors"], -(r["volume"] or 0)))

    # ---- Link gap -------------------------------------------------------------------------
    linkgap = res.get(("linkgap", "")) or []

    # ---- Verdict --------------------------------------------------------------------------
    losing = [r for r in shared if r["status"] == "losing"]
    vol = lambda rows: sum(r["volume"] or 0 for r in rows)
    lost_vol = vol(losing) or 1
    off = [r for r in losing if r["cause"] == "off-page"]
    on = [r for r in losing if r["cause"] == "on-page"]
    gap_vol = vol([r for r in missing if r["n_competitors"] >= 2]) if len(competitors) > 1 else vol(missing)
    you = sites[0]
    comp_ranks = [s["rank"] for s in sites[1:] if s["rank"] is not None]
    comp_rd = [s["referring_domains"] for s in sites[1:] if s["referring_domains"] is not None]
    verdict = {
        "shared_keywords": len(shared),
        "losing_keywords": len(losing),
        "winning_keywords": len(shared) - len(losing),
        "losing_volume": vol(losing),
        "offpage_keywords": len(off),
        "offpage_share": round(100 * vol(off) / lost_vol),
        "onpage_keywords": len(on),
        "onpage_share": round(100 * vol(on) / lost_vol),
        "missing_keywords": len(missing),
        "missing_volume_multi": gap_vol,
        "linkgap_domains": len(linkgap),
        "your_rank": you["rank"],
        "competitor_rank_median": sorted(comp_ranks)[len(comp_ranks) // 2] if comp_ranks else None,
        "your_rd": you["referring_domains"],
        "competitor_rd_median": sorted(comp_rd)[len(comp_rd) // 2] if comp_rd else None,
    }
    verdict["headline"] = _headline(verdict)

    return {
        "domain": domain,
        "competitors": competitors,
        "location_code": location_code,
        "language_code": language_code,
        "verdict": verdict,
        "sites": sites,
        "shared": shared,
        "missing": missing,
        "linkgap": linkgap,
        "cost": round(cost, 4),
        "errors": errors,
    }


def _headline(v: dict) -> str:
    parts = []
    if v["losing_keywords"]:
        main = "on-page" if v["onpage_share"] >= v["offpage_share"] else "off-page"
        parts.append(
            f"On keywords you share, competitors beat you on {v['losing_keywords']} "
            f"({v['losing_volume']:,} searches/mo). {v['onpage_share']}% of that volume looks on-page "
            f"(you have equal or more authority) and {v['offpage_share']}% looks off-page (they have more). "
            f"Main gap: {main}."
        )
    if v["missing_keywords"]:
        parts.append(f"{v['missing_keywords']} keywords rank for competitors but not for you at all — a content (on-page) gap.")
    if v["your_rank"] is not None and v["competitor_rank_median"] is not None:
        if v["your_rank"] >= v["competitor_rank_median"]:
            parts.append(f"Your domain rank ({v['your_rank']}) is at or above the competitor median ({v['competitor_rank_median']}), so links are not the bottleneck overall.")
        else:
            parts.append(f"Your domain rank ({v['your_rank']}) is below the competitor median ({v['competitor_rank_median']}) — link building will help.")
    return " ".join(parts)


async def _linkgap(c, domain: str, competitors: list[str]) -> tuple[list, float]:
    """Referring domains that link to at least one competitor but not to us."""
    task = {
        "targets": {str(i + 1): d for i, d in enumerate(competitors)},
        "exclude_targets": [domain],
        "limit": LINKGAP_LIMIT,
        "intersection_mode": "partial",
    }
    try:
        r, cost = await dfs.post_path(c, "backlinks/domain_intersection/live", task, mock_name="compare_linkgap")
    except dfs.DFSError as e:
        if "intersection_mode" not in str(e) and "Invalid Field" not in str(e):
            raise
        task.pop("intersection_mode")  # fall back to domains linking to all competitors
        r, cost = await dfs.post_path(c, "backlinks/domain_intersection/live", task, mock_name="compare_linkgap")
    rows = []
    for it in (r or {}).get("items") or []:
        inter = it.get("domain_intersection") or {}
        entries = {competitors[int(k) - 1]: v for k, v in inter.items() if v and k.isdigit() and int(k) <= len(competitors)}
        if not entries:
            continue
        any_e = next(iter(entries.values()))
        rows.append({
            "domain": any_e.get("target"),
            "links_to": sorted(entries),
            "n": len(entries),
            "rank": max((e.get("rank") or 0) for e in entries.values()),
            "spam_score": max((e.get("backlinks_spam_score") or 0) for e in entries.values()),
            "backlinks": sum(e.get("backlinks") or 0 for e in entries.values()),
        })
    rows = [r for r in rows if r["spam_score"] < 50]
    rows.sort(key=lambda r: (-r["n"], -r["rank"]))
    cost += await _linkgap_pages(c, rows, competitors)
    return rows, cost


async def _linkgap_pages(c, rows: list, competitors: list[str]) -> float:
    """Add row["pages"] = {competitor: {url_from, url_to, anchor, dofollow}} for each gap domain.

    Probe-verified: DataForSEO's domain_from carries "www." when the site uses it (e.g. "www.weddingwire.com"),
    so both d and "www."+d are requested; the ["domain_from","in",[...]] filter form works, a comma-string does not.
    A failed call never fails the link gap; those pages are just missing.
    """
    calls = []  # (comp, coroutine)
    for comp in competitors:
        values = []
        for r in rows:
            d = r["domain"]
            if d and comp in r["links_to"]:
                values.append(d)
                if not d.startswith("www."):
                    values.append("www." + d)
        for i in range(0, len(values), 200):
            calls.append((comp, dfs.post_path(c, "backlinks/backlinks/live", {
                "target": comp, "mode": "one_per_domain", "include_subdomains": True,
                "exclude_internal_backlinks": True, "limit": 1000,
                "filters": ["domain_from", "in", values[i:i + 200]],
            }, mock_name="compare_linkgap_pages")))
    out = await asyncio.gather(*(co for _, co in calls), return_exceptions=True)
    found: dict[tuple[str, str], dict] = {}
    cost = 0.0
    for (comp, _), res in zip(calls, out):
        if isinstance(res, BaseException):
            continue
        r, c_ = res
        cost += c_
        for it in (r or {}).get("items") or []:
            df = re.sub(r"^www\.", "", it.get("domain_from") or "")
            if df:
                found.setdefault((comp, df), {
                    "url_from": it.get("url_from"), "url_to": it.get("url_to"),
                    "anchor": it.get("anchor"), "dofollow": it.get("dofollow"),
                })
    for r in rows:
        d = re.sub(r"^www\.", "", r["domain"] or "")
        r["pages"] = {comp: found[(comp, d)] for comp in r["links_to"] if (comp, d) in found}
    return cost


ONPAGE_CHECKS = {
    "no_title": "No title",
    "title_too_long": "Title too long",
    "title_too_short": "Title too short",
    "no_description": "No meta description",
    "no_h1_tag": "No H1",
    "duplicate_title_tag": "Duplicate title tag",
    "low_content_rate": "Low text-to-HTML ratio",
    "no_image_alt": "Images missing alt text",
    "high_loading_time": "Slow loading",
    "is_redirect": "Homepage redirects",
    "has_render_blocking_resources": "Render-blocking resources",
}


def _onpage(r: dict | None) -> dict | None:
    items = (r or {}).get("items") or []
    if not items:
        return None
    p = items[0]
    m = p.get("meta") or {}
    content = m.get("content") or {}
    h = m.get("htags") or {}
    checks = p.get("checks") or {}
    return {
        "url": p.get("url"),
        "status": p.get("status_code"),
        "score": p.get("onpage_score"),
        "title": m.get("title"),
        "title_length": m.get("title_length"),
        "description": m.get("description"),
        "description_length": m.get("description_length"),
        "h1": (h.get("h1") or [])[:3],
        "h1_count": len(h.get("h1") or []),
        "h2_count": len(h.get("h2") or []),
        "words": content.get("plain_text_word_count"),
        "internal_links": m.get("internal_links_count"),
        "images": m.get("images_count"),
        "load_ms": (p.get("page_timing") or {}).get("duration_time"),
        "issues": [label for k, label in ONPAGE_CHECKS.items() if checks.get(k)],
    }
