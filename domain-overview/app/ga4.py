"""GA4 Data API client (#56): sessions by arrival page and channel, read-only, free.

Uses the service account already mounted for Search Console (GSC_KEY_FILE); it must be a Viewer on the property.
The Data API returns aggregates only: it has a landing page dimension but no per-session page sequence and no
exit page, so following steps and exit pages need the GA4 BigQuery export (see PATHS_NOTE)."""
from __future__ import annotations

import datetime as dt
import time

import httpx

from . import gsc, sites

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
API = "https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
PROPERTIES = {"serenity": "292463555"}  # site slug -> GA4 property ID (a site's `ga4_property` overrides)
PAGE_LIMIT = 100000
CACHE_SECONDS = 3600  # short cache to stay within the Data API's quotas
PATHS_NOTE = ("GA4's Data API reports sessions per arrival page but not the pages visited after it or the exit page. "
              "Following steps and exit pages need the GA4 BigQuery export.")

_CACHE: dict[tuple, tuple[float, dict]] = {}


class GA4Error(Exception):
    pass


def property_id() -> str | None:
    site = sites.CURRENT.get() or {}
    return site.get("ga4_property") or PROPERTIES.get(site.get("slug", ""))


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(gsc.KEY_FILE, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def _client() -> httpx.Client:
    return httpx.Client(timeout=60)


def _report(client: httpx.Client, token: str, prop: str, body: dict) -> list[dict]:
    out, offset = [], 0
    while True:
        r = client.post(API.format(prop=prop), headers={"Authorization": f"Bearer {token}"},
                        json={**body, "limit": PAGE_LIMIT, "offset": offset})
        if r.status_code == 403:
            raise GA4Error(f"GA4 not reachable: the service account has no access to property {prop} "
                           f"(add it as a Viewer in GA4 admin). {r.text[:200]}")
        if r.status_code != 200:
            raise GA4Error(f"GA4 not reachable: Data API {r.status_code}: {r.text[:300]}")
        j = r.json()
        rows = j.get("rows", [])
        out.extend(rows)
        offset += len(rows)
        if not rows or offset >= j.get("rowCount", 0):
            return out


def sessions(start: dt.date, end: dt.date) -> dict:
    """{property, fetched_at, first_day, rows: [{channel, pages: [arrival path], sessions}]}; cached CACHE_SECONDS.
    first_day: the first day in the window with any sessions (None when there are none)."""
    prop = property_id()
    if not prop:
        raise LookupError("no GA4 property configured for this site")
    key = (prop, start, end)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    dates = [{"startDate": start.isoformat(), "endDate": end.isoformat()}]
    try:
        token = _token()
        with _client() as client:
            land = _report(client, token, prop, {
                "dateRanges": dates, "metrics": [{"name": "sessions"}],
                "dimensions": [{"name": "landingPage"}, {"name": "sessionDefaultChannelGroup"}]})
            first = _report(client, token, prop, {
                "dateRanges": dates, "metrics": [{"name": "sessions"}], "dimensions": [{"name": "date"}],
                "orderBys": [{"dimension": {"dimensionName": "date"}}]})
    except GA4Error:
        raise
    except Exception as e:  # noqa: BLE001 - auth / network problems surface as one error type
        raise GA4Error(f"GA4 not reachable: {type(e).__name__}: {e}"[:400])
    day = next((r["dimensionValues"][0]["value"] for r in first if float(r["metricValues"][0]["value"]) > 0), None)
    out = {"property": prop, "fetched_at": dt.datetime.utcnow().isoformat(timespec="seconds"),
           "first_day": dt.datetime.strptime(day, "%Y%m%d").date().isoformat() if day else None,
           "rows": [{"pages": [r["dimensionValues"][0]["value"]], "channel": r["dimensionValues"][1]["value"],
                     "sessions": int(r["metricValues"][0]["value"])} for r in land]}
    _CACHE[key] = (time.time(), out)
    return out
