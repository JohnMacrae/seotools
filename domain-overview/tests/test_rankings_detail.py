"""Rankings detail view and free tags (#28)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from tests.test_rankings import TARGETS, daily_rows  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as client:
        for t in TARGETS.values():
            assert client.post("/api/targets", json=t).status_code == 200
        with main.closing(main.db()) as con:
            con.executemany("INSERT INTO gsc_query_daily(date,query,page,clicks,impressions,position) VALUES (?,?,?,?,?,?)",
                            daily_rows())
            con.commit()
        yield client


def tid(c, kw):
    return next(t["id"] for t in c.get("/api/targets").json() if t["primary_keyword"] == kw)


def test_detail_cards_series_and_variants(c):
    r = c.get(f"/api/rankings/{tid(c, 'lace wedding dresses')}", params={"range": "all", "grouping": "week"})
    assert r.status_code == 200, r.text
    d = r.json()
    card = d["cards"]
    assert card["current"] == 10.0 and card["movement"] == 10.0 and card["direction"] == "up"
    assert card["period_avg"] == round((7 * 20 + 7 * 10 + 47 * 15) / 61, 1)
    assert card["ads_volume"] == 2400 and card["impressions"] == 61 * 40
    assert d["grouping"] == "week" and len(d["series"]) >= 9 and "position" in d["series"][0]
    v = {x["query"]: x for x in d["variants"]}
    assert set(v) == {"lace wedding dresses", "wedding dress lace"}
    assert v["lace wedding dresses"]["impressions"] == 610 and v["lace wedding dresses"]["clicks"] == 61
    assert v["wedding dress lace"]["clicks"] == 0 and v["wedding dress lace"]["impressions"] == 61 * 30
    assert v["lace wedding dresses"]["position"] == round((7 * 20 + 7 * 10 + 47 * 15) / 61, 1)
    assert d["tags"] == []


def test_detail_errors_and_bad_grouping(c):
    assert c.get("/api/rankings/9999").status_code == 404
    assert c.get(f"/api/rankings/{tid(c, 'ivory veils')}", params={"range": "7d", "grouping": "month"}).status_code == 400
    assert c.get(f"/api/rankings/{tid(c, 'ivory veils')}", params={"range": "nope"}).status_code == 400


def test_tags_add_remove_and_list(c):
    t = tid(c, "ivory veils")
    r = c.post(f"/api/targets/{t}/tags", json={"tag": " Services "})
    assert r.status_code == 200 and r.json()["tags"] == ["Services"]
    assert c.post(f"/api/targets/{t}/tags", json={"tag": "services"}).json()["tags"] == ["Services"]  # no dup
    assert c.post(f"/api/targets/{t}/tags", json={"tag": "  "}).status_code == 400
    assert c.post("/api/targets/9999/tags", json={"tag": "x"}).status_code == 404
    assert c.get(f"/api/rankings/{t}").json()["tags"] == ["Services"]
    assert c.get("/api/tags").json()["tags"] == ["Services"]
    assert c.delete(f"/api/targets/{t}/tags/Services").status_code == 200
    assert c.delete(f"/api/targets/{t}/tags/Services").status_code == 404
    assert c.get("/api/tags").json()["tags"] == []


def test_tags_are_labels_and_filter_the_list(c):
    t = tid(c, "ivory veils")
    c.post(f"/api/targets/{t}/tags", json={"tag": "PAA"})
    res = c.get("/api/rankings", params={"range": "all"}).json()
    row = next(r for r in res["rows"] if r["id"] == t)
    assert "tag:PAA" in row["labels"] and "tag:PAA" in res["labels"]
    f = c.get("/api/rankings", params={"range": "all", "labels": ["tag:PAA"]}).json()
    assert [r["id"] for r in f["rows"]] == [t]


def test_deleting_a_target_drops_its_tags(c):
    t = tid(c, "ivory veils")
    c.post(f"/api/targets/{t}/tags", json={"tag": "PAA"})
    assert c.delete(f"/api/targets/{t}").status_code == 200
    assert c.get("/api/tags").json()["tags"] == []
