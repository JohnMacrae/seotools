"""GA4 client for the visitor flow (#56, ADR 0002): arrival pages from the Data API and the pages that follow
them from the funnel report (v1alpha; `landingPage` is not allowed inside funnel steps, so a page is matched by
path). Counts from the funnel report are GA4 active users, not sessions."""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import sites

GA4_PROPERTY = os.environ.get("GA4_PROPERTY_ID", "")  # serenitybrides.com's property; other sites have none yet
GA4_SITE = "serenity"
KEY_FILE = os.environ.get("GSC_KEY_FILE", "/secrets/gsc-key.json")
SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
API = "https://analyticsdata.googleapis.com/{version}/properties/{prop}:{method}"
ARRIVALS = 5            # arrival pages followed, by organic sessions
BRANCH = (3, 2, 2)      # pages followed after each step (a funnel next action returns at most 5)
NO_NEXT = "(no next action)"
CACHE_SECONDS = 3600
CACHE: dict[tuple, tuple[float, list[dict]]] = {}


class GA4Error(Exception):
    pass


def property_id() -> str:
    return GA4_PROPERTY if (sites.CURRENT.get() or {}).get("slug") == GA4_SITE else ""


def configured() -> bool:
    return bool(property_id()) and Path(KEY_FILE).is_file()


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def _request(version: str, method: str, body: dict, prop: str) -> dict:
    """One GA4 call; `prop` is passed in because worker threads do not see the active site."""
    r = httpx.post(API.format(version=version, prop=prop, method=method), json=body, timeout=120,
                   headers={"Authorization": f"Bearer {_token()}"})
    if r.status_code != 200:
        raise GA4Error(f"GA4 API {r.status_code}: {r.text[:300]}")
    return r.json()


def clean(page: str) -> str:
    """A page path without its query string (ad click ids and campaign tags would split one page into many)."""
    return urlsplit(page).path or "/"


def _field(name: str, value: str, match: str = "EXACT") -> dict:
    return {"funnelFieldFilter": {"fieldName": name, "stringFilter": {"matchType": match, "value": value}}}


def _page_step(i: int, page: str, organic: bool) -> dict:
    expr = _field("pagePathPlusQueryString", "^" + re.escape(page) + r"(\?.*)?$", "FULL_REGEXP")
    if i == 0 and organic:
        expr = {"andGroup": {"expressions": [expr, _field("sessionDefaultChannelGroup", "Organic Search")]}}
    step = {"name": f"s{i + 1}", "filterExpression": expr}
    if i:
        step["isDirectlyFollowedBy"] = True
    return step


def arrival_pages(prop: str, start: str, end: str, organic: bool) -> list[str]:
    body = {"dateRanges": [{"startDate": start, "endDate": end}], "dimensions": [{"name": "landingPage"}],
            "metrics": [{"name": "sessions"}], "limit": 1000}
    if organic:
        body["dimensionFilter"] = {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                              "stringFilter": {"matchType": "EXACT", "value": "Organic Search"}}}
    totals: dict[str, int] = {}
    for r in _request("v1beta", "runReport", body, prop).get("rows", []):
        p = clean(r["dimensionValues"][0]["value"])
        totals[p] = totals.get(p, 0) + int(r["metricValues"][0]["value"])
    return [p for p, _ in sorted(totals.items(), key=lambda x: -x[1])][:ARRIVALS]


def _expand(prop: str, chain: list[str], start: str, end: str, organic: bool) -> dict:
    """The node for the last page of `chain`: users reaching it through the chain, users leaving, and next pages."""
    body = {"dateRanges": [{"startDate": start, "endDate": end}],
            "funnel": {"isOpenFunnel": False, "steps": [_page_step(i, p, organic) for i, p in enumerate(chain)]},
            "funnelNextAction": {"nextActionDimension": {"name": "pagePathPlusQueryString"}, "limit": 5}}
    users, exits, nexts = 0, 0, {}
    for r in _request("v1alpha", "runFunnelReport", body, prop).get("funnelVisualization", {}).get("rows", []):
        step, action = (d["value"] for d in r["dimensionValues"])
        if int(step.split(".")[0]) != len(chain):
            continue
        n = int(r["metricValues"][0]["value"])
        if action == "RESERVED_TOTAL":
            users = n
        elif action == NO_NEXT:
            exits = n
        elif clean(action) != chain[-1]:  # same-page events (scrolls, clicks) are not a move
            nexts[clean(action)] = nexts.get(clean(action), 0) + n
    return {"page": chain[-1], "users": users, "exits": exits,
            "next": [{"page": p, "users": n, "exits": 0, "next": []}
                     for p, n in sorted(nexts.items(), key=lambda x: -x[1])]}


def _grow(prop: str, chain: list[str], start: str, end: str, organic: bool, pool: ThreadPoolExecutor) -> dict:
    node = _expand(prop, chain, start, end, organic)
    if len(chain) <= len(BRANCH):
        keep = node["next"][:BRANCH[len(chain) - 1]]
        node["next"] = list(pool.map(lambda k: _grow(prop, chain + [k["page"]], start, end, organic, pool), keep)) \
            if len(chain) < len(BRANCH) else keep
    return node


def fetch_roots(start: str, end: str, organic: bool, refresh: bool = False) -> list[dict]:
    """Arrival trees for the window, cached an hour (the funnel report costs a call per node)."""
    if not configured():
        raise GA4Error("No GA4 property configured for this site")
    prop = property_id()
    key = (prop, start, end, organic)
    hit = CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        pages = arrival_pages(prop, start, end, organic)
        with ThreadPoolExecutor(6) as arrivals_pool, ThreadPoolExecutor(6) as pool:
            roots = list(arrivals_pool.map(lambda p: _grow(prop, [p], start, end, organic, pool), pages))
    except httpx.HTTPError as e:
        raise GA4Error(f"GA4 API: {type(e).__name__}: {e}"[:300])
    CACHE[key] = (time.time(), roots)
    return roots


def fetched_at(start: str, end: str, organic: bool) -> str | None:
    """When this window was last fetched from GA4 (UTC ISO), for the page's "as of"."""
    hit = CACHE.get((property_id(), start, end, organic))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hit[0])) if hit else None
