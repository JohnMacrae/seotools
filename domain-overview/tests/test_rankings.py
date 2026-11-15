"""Rankings list API (#26): maths, gaps, top-URL flag, range clamping, grouping, labels and filters."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

FIRST, LAST = dt.date(2026, 9, 13), dt.date(2026, 11, 12)
H = "https://serenitybrides.com"
DAYS = [FIRST + dt.timedelta(days=i) for i in range((LAST - FIRST).days + 1)]  # 61 data days

TARGETS = {
    "A": {"path": "/collections/bridal/lace-wedding-dresses", "primary_keyword": "lace wedding dresses",
          "supporting": ["lace wedding dress"], "volume": 2400, "product_line": "bridal", "phase": 1, "topic": "lace"},
    "B": {"path": "/collections/mob", "primary_keyword": "mother of the bride dresses", "volume": 900,
          "product_line": "mob", "phase": 2},
    "C": {"path": "/collections/bridesmaids", "primary_keyword": "bridesmaid dresses colchester", "volume": 300,
          "product_line": "bridesmaids", "phase": 2},
    "E": {"path": "/collections/bridal/veils", "primary_keyword": "ivory veils", "volume": 100,
          "product_line": "bridal", "phase": 1},
}


def daily_rows():
    out = []
    for i, d in enumerate(DAYS):
        day = d.isoformat()
        out.append((day, "unrelated query", f"{H}/", 1, 5, 30.0))  # every day is a data day
        x = 20.0 if i < 7 else 10.0 if i >= len(DAYS) - 7 else 15.0
        # A: primary on its page, a word-order variant on a blog page with more impressions (cannibalisation)
        out.append((day, "lace wedding dresses", H + TARGETS["A"]["path"], 1, 10, x))
        out.append((day, "wedding dress lace", f"{H}/blog/lace", 0, 30, x))
        # B: only in the first and last 7 data days (gap between), and it moves down
        if i < 7:
            out.append((day, "mother of the bride dresses", H + TARGETS["B"]["path"], 2, 20, 3.0))
        elif i >= len(DAYS) - 7:
            out.append((day, "Mother of the Bride Dress", H + TARGETS["B"]["path"], 1, 20, 6.0))
    # E: one day only, two rows to check impression weighting: (10 @ 4 + 30 @ 8) / 40 = 7.0
    out.append((LAST.isoformat(), "ivory veils", H + TARGETS["E"]["path"], 1, 10, 4.0))
    out.append((LAST.isoformat(), "ivory veil", f"{H}/blog/veils", 0, 30, 8.0))
    return out


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


def get(c, **params):
    r = c.get("/api/rankings", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def by_kw(res):
    return {r["keyword"]: r for r in res["rows"]}


def test_default_range_is_3_months_clamped_to_first_data_day(c):
    res = get(c)
    assert res["first_day"] == "2026-09-13" and res["last_day"] == "2026-11-12"
    assert res["range"] == {"preset": "3m", "start": "2026-09-13", "end": "2026-11-12", "clamped": True}


def test_one_row_per_target_with_current_movement_and_period_average(c):
    rows = by_kw(get(c, range="all"))
    assert len(rows) == 4
    a = rows["lace wedding dresses"]
    assert a["current"] == 10.0 and a["start"] == 20.0
    assert a["movement"] == 10.0 and a["direction"] == "up"  # lower position is better
    assert a["period_avg"] == round((7 * 20 + 7 * 10 + 47 * 15) / 61, 1)
    assert a["clicks"] == 61 and a["impressions"] == 61 * 40  # close variants rolled up
    assert a["volume"] == 2400 and a["no_data"] is False
    b = rows["mother of the bride dresses"]
    assert (b["current"], b["start"], b["movement"], b["direction"]) == (6.0, 3.0, -3.0, "down")
    assert b["period_avg"] == 4.5


def test_current_and_period_average_are_impression_weighted(c):
    e = by_kw(get(c, range="7d"))["ivory veils"]
    assert e["current"] == 7.0 and e["period_avg"] == 7.0


def test_movement_is_against_first_7_data_days_of_the_period(c):
    rows = by_kw(get(c, range="30d"))
    a = rows["lace wedding dresses"]
    assert (a["start"], a["current"], a["movement"]) == (15.0, 10.0, 5.0)
    b = rows["mother of the bride dresses"]
    assert b["start"] is None and b["movement"] is None and b["direction"] is None


def test_target_without_impressions_is_no_data(c):
    r = by_kw(get(c, range="all"))["bridesmaid dresses colchester"]
    assert r["no_data"] is True
    assert (r["current"], r["movement"], r["period_avg"], r["top_url"]) == (None, None, None, None)
    assert all(p["position"] is None for p in r["series"])


def test_top_url_is_most_impressions_across_variants_and_flagged_off_target(c):
    rows = by_kw(get(c, range="all"))
    a = rows["lace wedding dresses"]
    assert a["top_url"] == f"{H}/blog/lace" and a["top_url_off_target"] is True
    b = rows["mother of the bride dresses"]
    assert b["top_url"] == H + "/collections/mob" and b["top_url_off_target"] is False


def test_gaps_where_no_impressions(c):
    res = get(c, range="all", grouping="week")
    b = by_kw(res)["mother of the bride dresses"]["series"]
    assert b[0]["position"] == 3.0 and b[-1]["position"] == 6.0
    assert any(p["position"] is None for p in b[1:-1])
    assert all(p["position"] != 0 for p in b)
    e = by_kw(get(c, range="7d", grouping="day"))["ivory veils"]["series"]
    assert [p["start"] for p in e] == [(LAST - dt.timedelta(days=6 - i)).isoformat() for i in range(7)]
    assert [p["position"] for p in e] == [None] * 6 + [7.0]


def test_overview_is_average_across_targets_per_bucket(c):
    ov = get(c, range="7d", grouping="day")["overview"]
    assert len(ov) == 7
    assert ov[-1] == {"start": LAST.isoformat(), "position": round((10 + 6 + 7) / 3, 1)}
    assert ov[0]["position"] == 8.0  # A at 10, B at 6, E has no data that day


def test_grouping_only_offered_with_at_least_two_points(c):
    res = get(c, range="7d")
    assert res["groupings"] == ["day", "week"]  # 2026-11-06..12 spans two ISO weeks, one month
    assert res["grouping"] in res["groupings"]
    assert c.get("/api/rankings", params={"range": "7d", "grouping": "month"}).status_code == 400
    assert get(c, range="all")["groupings"] == ["day", "week", "month"]
    m = by_kw(get(c, range="all", grouping="month"))["lace wedding dresses"]["series"]
    assert [p["start"] for p in m] == ["2026-09-01", "2026-10-01", "2026-11-01"]


def test_range_presets_and_clamping(c):
    assert get(c, range="30d")["range"] == {"preset": "30d", "start": "2026-10-14", "end": "2026-11-12", "clamped": False}
    assert get(c, range="12w")["range"]["start"] == "2026-09-13"  # 84 days back, clamped
    assert get(c, range="12w")["range"]["clamped"] is True
    assert get(c, range="7d")["range"]["start"] == "2026-11-06"
    for p in ("12m", "6m", "all"):
        assert get(c, range=p)["range"]["start"] == "2026-09-13"
    assert get(c, range="all")["range"]["clamped"] is False
    assert c.get("/api/rankings", params={"range": "2y"}).status_code == 400


def test_labels_derived_from_line_phase_topic_and_folder(c):
    res = get(c)
    a = by_kw(res)["lace wedding dresses"]
    assert a["labels"] == ["line:bridal", "phase:1", "topic:lace", "folder:/collections/bridal"]
    assert by_kw(res)["mother of the bride dresses"]["labels"] == ["line:mob", "phase:2", "folder:/collections"]
    assert "topic:lace" in res["labels"] and "line:bridesmaids" in res["labels"]


def test_label_filter_or_within_kind_and_across_kinds(c):
    assert set(by_kw(get(c, labels="line:bridal"))) == {"lace wedding dresses", "ivory veils"}
    both = c.get("/api/rankings", params=[("labels", "line:bridal"), ("labels", "line:mob")]).json()
    assert set(by_kw(both)) == {"lace wedding dresses", "ivory veils", "mother of the bride dresses"}
    none = c.get("/api/rankings", params=[("labels", "line:bridal"), ("labels", "phase:2")]).json()
    assert none["rows"] == []
    assert "line:mob" in none["labels"]  # all labels stay listed for the filter control


def test_overview_follows_label_filter(c):
    ov = get(c, range="7d", grouping="day", labels="line:mob")["overview"]
    assert [p["position"] for p in ov] == [6.0] * 7


def test_movement_direction_filter(c):
    assert set(by_kw(get(c, range="all", movement="up"))) == {"lace wedding dresses"}
    assert set(by_kw(get(c, range="all", movement="down"))) == {"mother of the bride dresses"}
    assert len(get(c, range="all", movement="all")["rows"]) == 4
    assert c.get("/api/rankings", params={"movement": "sideways"}).status_code == 400


def test_empty_store_returns_no_data_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as client:
        client.post("/api/targets", json=TARGETS["A"])
        res = client.get("/api/rankings").json()
    assert res["first_day"] is None and res["range"] is None and res["overview"] == []
    assert res["rows"][0]["no_data"] is True
