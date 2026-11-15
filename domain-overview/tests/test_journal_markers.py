"""Journal markers on the rankings graphs (#31)."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests.test_rankings import LAST, TARGETS  # noqa: E402
from tests.test_rankings_detail import c, tid  # noqa: E402,F401


@pytest.fixture
def empty(tmp_path, monkeypatch):
    """A site with targets and journal entries but no Search Console data stored."""
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as client:
        for t in TARGETS.values():
            assert client.post("/api/targets", json=t).status_code == 200
        yield client


def add(client, **kw):
    e = {"live_date": LAST.isoformat(), "page": "/collections/mob", "change_type": "title/meta", "note": "n"}
    e.update(kw)
    assert client.post("/api/journal", json=e).status_code == 200


def test_overview_returns_entries_in_range(c):
    add(c, live_date=LAST.isoformat(), page="/collections/mob", note="inside")
    add(c, live_date="2020-01-01", note="before the range")
    add(c, live_date=LAST.isoformat(), page="/dresses/*", note="pattern")
    d = c.get("/api/rankings", params={"range": "all"}).json()
    assert sorted(j["note"] for j in d["journal"]) == ["inside", "pattern"]
    assert {"live_date", "page", "change_type", "approx", "note"} <= set(d["journal"][0])


def test_overview_range_excludes_old(c):
    add(c, live_date="2020-01-01")
    assert c.get("/api/rankings", params={"range": "7d"}).json()["journal"] == []


def test_detail_matches_page_or_pattern_only(c):
    add(c, page="/collections/mob", note="exact")
    add(c, page="/collections/*", note="pattern")
    add(c, page="/collections/bridal/veils", note="other page")
    add(c, page="/blog/*", note="other pattern")
    add(c, page="/collections/mob", live_date="2020-01-01", note="out of range")
    d = c.get(f"/api/rankings/{tid(c, 'mother of the bride dresses')}", params={"range": "all"}).json()
    assert sorted(j["note"] for j in d["journal"]) == ["exact", "pattern"]


def test_markers_carry_their_bucket(c):
    """The API places each marker on a graph point, so the page never recomputes bucketing."""
    add(c, live_date=LAST.isoformat(), note="last")
    d = c.get("/api/rankings", params={"range": "all", "grouping": "week"}).json()
    week = (LAST - dt.timedelta(days=LAST.weekday())).isoformat()
    starts = [p["start"] for p in d["overview"]]
    assert [j["bucket"] for j in d["journal"]] == [week] and week in starts
    m = c.get("/api/rankings", params={"range": "all", "grouping": "month"}).json()
    assert m["journal"][0]["bucket"] == LAST.replace(day=1).isoformat()
    det = c.get(f"/api/rankings/{tid(c, 'mother of the bride dresses')}", params={"range": "all", "grouping": "week"}).json()
    assert [j["bucket"] for j in det["journal"]] == [week] and week in [p["start"] for p in det["series"]]


def test_detail_without_data_has_journal_key(empty):
    add(empty)
    t = next(t["id"] for t in empty.get("/api/targets").json() if t["primary_keyword"] == "mother of the bride dresses")
    d = empty.get(f"/api/rankings/{t}").json()
    assert d["journal"] == [] and d["range"] is None
