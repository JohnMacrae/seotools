"""Journal markers on the rankings graphs (#31)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_rankings import LAST  # noqa: E402
from tests.test_rankings_detail import c, tid  # noqa: E402,F401


def add(c, **kw):
    e = {"live_date": (LAST).isoformat(), "page": "/collections/mob", "change_type": "title", "note": "n"}
    e.update(kw)
    assert c.post("/api/journal", json=e).status_code == 200


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


def test_detail_without_data_has_journal_key(c):
    add(c)
    with main_db(c) as con:
        con.execute("DELETE FROM gsc_query_daily"); con.commit()
    d = c.get(f"/api/rankings/{tid(c, 'mother of the bride dresses')}").json()
    assert d["journal"] == []


def main_db(c):
    from app import main
    return main.closing(main.db())
