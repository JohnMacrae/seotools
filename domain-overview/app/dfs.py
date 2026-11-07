"""DataForSEO Labs client: fetches and normalises the data for one domain overview."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path

import httpx

BASE = "https://api.dataforseo.com/v3/dataforseo_labs/google"
LOGIN = os.environ.get("DFS_LOGIN", "")
PASSWORD = os.environ.get("DFS_PASSWORD", "")
MOCK = os.environ.get("DFS_MOCK", "") == "1"
KEYWORD_LIMIT = int(os.environ.get("KEYWORD_LIMIT", "100"))
COMPETITOR_LIMIT = int(os.environ.get("COMPETITOR_LIMIT", "20"))
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "20"))

POS_BUCKETS = [
    ("1", ["pos_1"]),
    ("2-3", ["pos_2_3"]),
    ("4-10", ["pos_4_10"]),
    ("11-20", ["pos_11_20"]),
    ("21-50", ["pos_21_30", "pos_31_40", "pos_41_50"]),
    ("51-100", ["pos_51_60", "pos_61_70", "pos_71_80", "pos_81_90", "pos_91_100"]),
]


class DFSError(Exception):
    pass


async def _post(client: httpx.AsyncClient, endpoint: str, task: dict) -> tuple[dict | None, float]:
    """POST one task to a live endpoint. Returns (result[0] or None, cost)."""
    if MOCK:
        return _mock(endpoint), 0.0
    r = await client.post(f"{BASE}/{endpoint}/live", json=[task])
    r.raise_for_status()
    body = r.json()
    if body.get("status_code") != 20000:
        raise DFSError(f"{endpoint}: {body.get('status_message')}")
    t = (body.get("tasks") or [{}])[0]
    if t.get("status_code") != 20000:
        raise DFSError(f"{endpoint}: {t.get('status_message')}")
    res = (t.get("result") or [None])[0]
    return res, float(body.get("cost") or 0)


def _mock(endpoint: str) -> dict | None:
    p = Path(__file__).parent / "mock" / f"{endpoint}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _organic(metrics: dict | None) -> dict:
    return (metrics or {}).get("organic") or {}


async def fetch_overview(domain: str, location_code: int, language_code: str) -> dict:
    base = {"target": domain, "location_code": location_code, "language_code": language_code}
    date_from = (dt.date.today().replace(day=1) - dt.timedelta(days=365)).isoformat()

    auth = httpx.BasicAuth(LOGIN, PASSWORD)
    async with httpx.AsyncClient(auth=auth, timeout=90) as client:
        calls = await asyncio.gather(
            _post(client, "domain_rank_overview", base),
            _post(client, "historical_rank_overview", {**base, "date_from": date_from}),
            _post(client, "ranked_keywords", {
                **base,
                "limit": KEYWORD_LIMIT,
                "item_types": ["organic"],
                "order_by": ["ranked_serp_element.serp_item.etv,desc"],
            }),
            _post(client, "relevant_pages", {
                **base,
                "limit": PAGE_LIMIT,
                "order_by": ["metrics.organic.etv,desc"],
            }),
            _post(client, "competitors_domain", {
                **base,
                "limit": COMPETITOR_LIMIT + 1,
                "exclude_top_domains": True,
            }),
            return_exceptions=True,
        )

    errors, cost, results = [], 0.0, []
    for name, c in zip(["overview", "history", "keywords", "pages", "competitors"], calls):
        if isinstance(c, Exception):
            errors.append(f"{name}: {c}")
            results.append(None)
        else:
            results.append(c[0])
            cost += c[1]
    if all(r is None for r in results) and errors:
        raise DFSError("; ".join(errors))
    rank, hist, kws, pages, comps = results

    # Headline
    org = {}
    if rank and rank.get("items"):
        org = _organic(rank["items"][0].get("metrics"))
    headline = {
        "keywords": org.get("count", 0),
        "traffic": round(org.get("etv") or 0),
        "traffic_value": round(org.get("estimated_paid_traffic_cost") or 0),
        "new": org.get("is_new", 0),
        "up": org.get("is_up", 0),
        "down": org.get("is_down", 0),
        "lost": org.get("is_lost", 0),
    }
    positions = [{"bucket": label, "count": sum(org.get(k) or 0 for k in keys)} for label, keys in POS_BUCKETS]

    # Trend
    trend = []
    for it in (hist or {}).get("items") or []:
        o = _organic(it.get("metrics"))
        trend.append({
            "month": f"{it.get('year')}-{int(it.get('month') or 0):02d}",
            "traffic": round(o.get("etv") or 0),
            "keywords": o.get("count") or 0,
            "top3": (o.get("pos_1") or 0) + (o.get("pos_2_3") or 0),
            "top10": (o.get("pos_1") or 0) + (o.get("pos_2_3") or 0) + (o.get("pos_4_10") or 0),
        })
    trend.sort(key=lambda r: r["month"])

    # Keywords
    keywords = []
    for it in (kws or {}).get("items") or []:
        kd = it.get("keyword_data") or {}
        info = kd.get("keyword_info") or {}
        props = kd.get("keyword_properties") or {}
        serp = (it.get("ranked_serp_element") or {}).get("serp_item") or {}
        ch = serp.get("rank_changes") or {}
        prev = ch.get("previous_rank_absolute")
        keywords.append({
            "keyword": kd.get("keyword"),
            "position": serp.get("rank_group"),
            "previous": prev,
            "is_new": bool(ch.get("is_new")),
            "volume": info.get("search_volume"),
            "cpc": info.get("cpc"),
            "kd": props.get("keyword_difficulty"),
            "traffic": round(serp.get("etv") or 0, 1),
            "url": serp.get("url"),
        })

    # Pages
    top_pages = []
    for it in (pages or {}).get("items") or []:
        o = _organic(it.get("metrics"))
        top_pages.append({
            "url": it.get("page_address"),
            "keywords": o.get("count") or 0,
            "traffic": round(o.get("etv") or 0, 1),
        })

    # Competitors
    competitors = []
    for it in (comps or {}).get("items") or []:
        d = it.get("domain")
        if not d or d.removeprefix("www.") == domain:
            continue
        full = _organic(it.get("full_domain_metrics"))
        competitors.append({
            "domain": d,
            "common": it.get("intersections") or 0,
            "avg_position": round(it.get("avg_position") or 0, 1),
            "keywords": full.get("count") or 0,
            "traffic": round(full.get("etv") or 0),
        })
    competitors = competitors[:COMPETITOR_LIMIT]

    return {
        "domain": domain,
        "location_code": location_code,
        "language_code": language_code,
        "headline": headline,
        "positions": positions,
        "trend": trend,
        "keywords": keywords,
        "pages": top_pages,
        "competitors": competitors,
        "cost": round(cost, 4),
        "errors": errors,
    }
