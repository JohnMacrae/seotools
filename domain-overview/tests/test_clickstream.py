"""Clickstream volume with spend light (#33). DataForSEO is mocked: no paid calls."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import clickstream, dfs, main  # noqa: E402
from tests.test_rankings import TARGETS, daily_rows  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    calls = []

    async def fake_post(client, path, task, mock_name=None, all_results=False):
        calls.append((path, task))
        return [{"keyword": k, "search_volume": 100 + len(k)} for k in task["keywords"]], 0.0103

    monkeypatch.setattr(dfs, "post_path", fake_post)
    with TestClient(main.app) as client:
        for t in TARGETS.values():
            assert client.post("/api/targets", json=t).status_code == 200
        with main.closing(main.db()) as con:
            con.executemany("INSERT INTO gsc_query_daily(date,query,page,clicks,impressions,position) VALUES (?,?,?,?,?,?)",
                            daily_rows())
            con.commit()
        client.calls = calls
        yield client


def test_estimate_shows_cost_before_fetching(c):
    e = c.get("/api/clickstream/estimate").json()
    n = len(TARGETS)
    assert e["keywords"] == n and e["cached"] == 0
    assert e["cost"] == pytest.approx(clickstream.TASK_COST + n * clickstream.KEYWORD_COST)
    assert c.calls == []


def test_fetch_caches_for_30_days_and_skips_second_call(c):
    r = c.post("/api/clickstream").json()
    assert r["fetched"] == len(TARGETS) and r["cost"] == pytest.approx(0.0103)
    assert len(c.calls) == 1 and c.calls[0][0] == clickstream.DFS_PATH
    again = c.post("/api/clickstream").json()
    assert again["fetched"] == 0 and again["cached"] == len(TARGETS) and len(c.calls) == 1
    e = c.get("/api/clickstream/estimate").json()
    assert e["keywords"] == 0 and e["cost"] == 0


def test_stale_after_30_days_refetches(c):
    c.post("/api/clickstream")
    with main.closing(main.db()) as con:
        con.execute("UPDATE clickstream_volumes SET fetched_at='2020-01-01T00:00:00+00:00'")
        con.commit()
    assert c.post("/api/clickstream").json()["fetched"] == len(TARGETS)
    assert len(c.calls) == 2


def test_rankings_show_clickstream_beside_ads_volume(c):
    before = c.get("/api/rankings", params={"range": "all"}).json()
    assert before["clickstream"] == {"source": "DataForSEO clickstream", "fetched_at": None}
    assert all(r["clickstream_volume"] is None for r in before["rows"])
    c.post("/api/clickstream")
    res = c.get("/api/rankings", params={"range": "all"}).json()
    assert res["clickstream"]["fetched_at"]
    for r in res["rows"]:
        assert r["clickstream_volume"] == 100 + len(r["keyword"])
    assert {r["id"]: r["volume"] for r in res["rows"]} == {r["id"]: r["volume"] for r in before["rows"]}  # Ads untouched
    t = res["rows"][0]
    d = c.get(f"/api/rankings/{t['id']}", params={"range": "all"}).json()
    assert d["cards"]["clickstream_volume"] == t["clickstream_volume"] and d["cards"]["ads_volume"] == t["volume"]


def test_spend_light(c):
    s = c.get("/api/spend-status").json()["features"]["clickstream"]
    assert s["status"] == "never"
    c.post("/api/clickstream")
    s = c.get("/api/spend-status").json()["features"]["clickstream"]
    assert s["status"] == "green" and s["last_cost"] == pytest.approx(0.0103)


def test_dfs_failure_is_502_and_records_nothing(c, monkeypatch):
    async def boom(*a, **k):
        raise dfs.DFSError("nope")
    monkeypatch.setattr(dfs, "post_path", boom)
    assert c.post("/api/clickstream").status_code == 502
    assert c.get("/api/spend-status").json()["features"]["clickstream"]["status"] == "never"
