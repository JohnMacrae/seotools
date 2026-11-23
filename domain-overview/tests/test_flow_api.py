"""Visitor flow endpoint (#54) with GA4 faked at the HTTP client: labelling, unreachable GA4, site without a property."""
import datetime as dt
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dfs, ga4, gsc, main  # noqa: E402

NEW = {"name": "Wedding Shop", "gsc_property": "sc-domain:theweddingshop.uk", "repo_path": "", "dfs_location": 2826}


class FakeGA4:
    """GA4 Data API runReport: landing page x channel sessions, and the first day with data."""

    def __init__(self, status=200):
        self.status, self.calls = status, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "User does not have sufficient permissions"}})
        dims = [d["name"] for d in body["dimensions"]]
        if dims == ["date"]:
            rows = [{"dimensionValues": [{"value": "20261001"}], "metricValues": [{"value": "4"}]}]
        else:
            data = [("/dresses/", "Organic Search", 60), ("/", "Organic Search", 40), ("/", "Direct", 100),
                    ("/dresses", "Organic Search", 5)]  # two forms of one page merge
            rows = [{"dimensionValues": [{"value": p}, {"value": c}], "metricValues": [{"value": str(n)}]}
                    for p, c, n in data]
        return httpx.Response(200, json={"rows": rows, "rowCount": len(rows)})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    monkeypatch.setattr(gsc, "SITE_URL", "sc-domain:serenitybrides.com")
    monkeypatch.setenv("GSC_SITE_URL", "sc-domain:serenitybrides.com")
    monkeypatch.setattr(gsc, "KEY_FILE", __file__)
    monkeypatch.setattr(ga4, "_token", lambda: "t")
    monkeypatch.setattr(ga4, "_CACHE", {})
    fake = FakeGA4()
    monkeypatch.setattr(ga4, "_client", fake.client)

    async def no_spend(*a, **k):
        raise AssertionError("the flow page must never call DataForSEO")
    monkeypatch.setattr(dfs, "_post", no_spend)
    with TestClient(main.app) as c:
        c.post("/api/sites", json=NEW)
        with main.closing(main.db()) as con:
            con.executemany("INSERT INTO gsc_query_daily(date,query,page,clicks,impressions,position) VALUES (?,?,?,?,?,?)", [
                ("2026-10-10", "wedding dresses colchester", "https://serenitybrides.com/dresses", 7, 70, 3.0),
                ("2026-10-11", "bridal shop", "https://serenitybrides.com/dresses", 2, 90, 6.0),
                ("2026-08-01", "out of window", "https://serenitybrides.com/dresses", 50, 500, 2.0)])
            con.commit()
        yield c, fake


def test_flow_labels_sources_and_dates(env):
    c, fake = env
    r = c.get("/api/flow?start=2026-10-01&end=2026-10-28&site=serenity")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["window"] == {"start": "2026-10-01", "end": "2026-10-28"}
    assert j["traffic"] == "organic"
    s = j["sources"]["sessions"]
    assert s["source"] == "GA4" and s["property"] == ga4.PROPERTIES["serenity"]
    assert s["as_of"] and s["first_day"] == "2026-10-01"
    q = j["sources"]["queries"]
    assert q["source"] == "Search Console" and q["estimate"] is True and q["as_of"] == "2026-10-11"
    a = j["flow"]["arrivals"]
    assert [(x["page"], x["sessions"]) for x in a] == [("https://serenitybrides.com/dresses", 65),
                                                     ("https://serenitybrides.com/", 40)]
    assert [x["query"] for x in a[0]["queries"]] == ["wedding dresses colchester", "bridal shop"]
    assert j["paths"] is False and "BigQuery" in j["paths_note"]  # the Data API gives no per-session paths
    assert fake.calls and all(p.endswith(f"properties/{ga4.PROPERTIES['serenity']}:runReport") for p, _ in fake.calls)


def test_defaults_to_last_28_days_and_all_traffic_toggle(env):
    c, fake = env
    j = c.get("/api/flow?site=serenity").json()
    end = dt.date.fromisoformat(j["window"]["end"])
    assert end == dt.date.today() - dt.timedelta(days=1)
    assert (end - dt.date.fromisoformat(j["window"]["start"])).days == 27
    every = c.get("/api/flow?site=serenity&traffic=all&start=2026-10-01&end=2026-10-28").json()
    assert every["traffic"] == "all" and every["flow"]["total_sessions"] == 205


def test_ga4_not_reachable(env, monkeypatch):
    c, _ = env

    def down():
        raise httpx.ConnectError("name resolution failed")
    monkeypatch.setattr(ga4, "_token", down)
    r = c.get("/api/flow?site=serenity")
    assert r.status_code == 502
    assert r.json()["detail"].startswith("GA4 not reachable")


def test_ga4_access_missing_names_the_cause(env, monkeypatch):
    c, _ = env
    fake = FakeGA4(status=403)
    monkeypatch.setattr(ga4, "_client", fake.client)
    r = c.get("/api/flow?site=serenity")
    assert r.status_code == 502
    d = r.json()["detail"]
    assert d.startswith("GA4 not reachable") and "Viewer" in d and ga4.PROPERTIES["serenity"] in d


def test_site_without_ga4_property(env):
    c, fake = env
    r = c.get("/api/flow?site=wedding-shop")
    assert r.status_code == 404
    assert "no GA4 property" in r.json()["detail"]
    assert fake.calls == []


def test_bad_window_is_rejected(env):
    c, _ = env
    assert c.get("/api/flow?site=serenity&start=2026-10-28&end=2026-10-01").status_code == 400
    assert c.get("/api/flow?site=serenity&traffic=paid").status_code == 400


def test_flow_page_is_served(env):
    c, _ = env
    assert c.get("/flow").status_code == 200


def test_queries_follow_the_selected_window(env):
    c, _ = env
    def top(start, end):
        j = c.get(f"/api/flow?site=serenity&start={start}&end={end}").json()
        d = next(x for x in j["flow"]["arrivals"] if x["page"].endswith("/dresses"))
        return [x["query"] for x in d["queries"]], j["sources"]["queries"]["as_of"]
    assert top("2026-10-01", "2026-10-28") == (["wedding dresses colchester", "bridal shop"], "2026-10-11")
    assert top("2026-07-25", "2026-08-05") == (["out of window"], "2026-08-01")  # a different window, different queries
    assert top("2026-09-01", "2026-09-10") == ([], None)  # no Search Console data: empty and no as-of, not zeros


def test_arrival_focus_narrows_flow_but_keeps_the_table(env):
    c, _ = env
    j = c.get("/api/flow?site=serenity&start=2026-10-01&end=2026-10-28&arrival=https://serenitybrides.com/").json()
    assert j["arrival"] == "https://serenitybrides.com/"
    assert [(n["page"], n["sessions"]) for n in j["flow"]["columns"][0]] == [("https://serenitybrides.com/", 40)]
    assert len(j["flow"]["arrivals"]) == 2


def test_ga4_responses_are_cached_briefly(env):
    c, fake = env
    url = "/api/flow?site=serenity&start=2026-10-01&end=2026-10-28"
    c.get(url)
    n = len(fake.calls)
    assert n
    c.get(url + "&traffic=all")  # same window: served from the cache, no Data API quota spent
    assert len(fake.calls) == n
    c.get("/api/flow?site=serenity&start=2026-10-02&end=2026-10-28")  # another window is a new fetch
    assert len(fake.calls) > n
    for k, (t, v) in list(ga4._CACHE.items()):  # age every entry past CACHE_SECONDS
        ga4._CACHE[k] = (t - ga4.CACHE_SECONDS - 1, v)
    m = len(fake.calls)
    c.get(url)  # expired: fetched again
    assert len(fake.calls) > m


def test_malformed_ga4_response_is_a_ga4_error_not_a_missing_property(env, monkeypatch):
    c, fake = env
    monkeypatch.setattr(ga4, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"rows": [{"metricValues": []}], "rowCount": 1}))))
    r = c.get("/api/flow?site=serenity&start=2026-10-01&end=2026-10-28")
    assert r.status_code == 502 and r.json()["detail"].startswith("GA4 not reachable")
