import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import digest  # noqa: E402

P = "https://x.test/a"
END = dt.date(2026, 11, 6)


def q(query, pos, imp=50, clicks=1, page=P):
    return {"page": page, "query": query, "clicks": clicks, "impressions": imp, "position": pos}


def flat(res):
    return [(p["page"], x["query"], x["tag"]) for p in res for x in p["queries"]]


def test_page2_position_and_impression_boundaries():
    rows = [q("a", 7.9), q("b", 8), q("c", 20), q("d", 20.1), q("e", 10, imp=9), q("f", 10, imp=10)]
    assert [x[1] for x in flat(digest.page2(rows, {}))] == ["b", "c", "f"]


def test_page2_on_and_off_target_variants():
    rows = [q("wedding dresses short", 12), q("short wedding dress", 12, imp=40), q("prom dress", 12, imp=30)]
    res = digest.page2(rows, {P: ["short wedding dresses"]})
    tags = {x["query"]: x["tag"] for x in res[0]["queries"]}
    assert tags == {"wedding dresses short": "on-target", "short wedding dress": "on-target", "prom dress": "off-target"}
    assert [x["query"] for x in res[0]["queries"]] == ["wedding dresses short", "short wedding dress", "prom dress"]


def test_page2_no_target_is_off_target_and_pages_sorted():
    rows = [q("a", 10, imp=20), q("b", 10, imp=100, page="https://x.test/b")]
    res = digest.page2(rows, {"https://x.test/b": ["b"]})
    assert [p["page"] for p in res] == ["https://x.test/b", P]
    assert flat(res) == [("https://x.test/b", "b", "on-target"), (P, "a", "off-target")]


def daily(page, start, end, per_day):
    out, d = [], start
    while d <= end:
        out.append({"page": page, "date": d.isoformat(), "clicks": per_day})
        d += dt.timedelta(days=1)
    return out


def span(w):
    a, b = digest.windows(END)[w]
    return a, b


def test_windows_year_ago_is_364_days_back():
    w = digest.windows(END)
    assert (w["current"][1] - w["current"][0]).days == 27
    assert w["previous"][1] == w["current"][0] - dt.timedelta(days=1)
    assert w["year_ago"][0] == w["current"][0] - dt.timedelta(days=364)
    assert w["year_ago"][0].weekday() == w["current"][0].weekday()


def test_decay_flagged_on_previous_only():
    rows = daily(P, *span("current"), 1) + daily(P, *span("previous"), 2) + daily(P, *span("year_ago"), 1)
    r = digest.decay(rows, END)
    assert len(r) == 1 and r[0]["flagged_on"] == ["previous"]
    assert r[0]["previous"]["delta"] == -28 and r[0]["year_ago"]["delta"] == 0
    assert round(r[0]["previous"]["pct"], 2) == -0.5


def test_decay_flagged_on_year_ago_only():
    rows = daily(P, *span("current"), 1) + daily(P, *span("previous"), 1) + daily(P, *span("year_ago"), 3)
    r = digest.decay(rows, END)
    assert r[0]["flagged_on"] == ["year_ago"] and r[0]["loss"] == 56


def test_decay_big_pct_but_under_5_clicks_not_flagged():
    cur, prev = span("current"), span("previous")
    rows = [{"page": P, "date": cur[0].isoformat(), "clicks": 1}, {"page": P, "date": prev[0].isoformat(), "clicks": 5}]
    assert digest.decay(rows, END) == []  # -80% but only 4 clicks


def test_decay_5_clicks_but_under_30_pct_not_flagged():
    cur, prev = span("current"), span("previous")
    rows = [{"page": P, "date": cur[0].isoformat(), "clicks": 100}, {"page": P, "date": prev[0].isoformat(), "clicks": 110}]
    assert digest.decay(rows, END) == []  # -10 clicks but -9%


def test_decay_exact_thresholds_flag():
    cur, prev = span("current"), span("previous")
    rows = [{"page": P, "date": cur[0].isoformat(), "clicks": 7}, {"page": P, "date": prev[0].isoformat(), "clicks": 10}]
    r = digest.decay(rows, END)
    assert len(r) == 0  # -3 clicks
    rows[1]["clicks"] = 12
    r = digest.decay(rows, END)
    assert len(r) == 1  # -5 clicks, -41.7%


def test_decay_no_year_ago_data_is_none_and_not_flagged_there():
    rows = daily(P, *span("current"), 1) + daily(P, *span("previous"), 1)
    assert digest.decay(rows, END) == []
    rows = daily(P, *span("current"), 1) + daily(P, *span("previous"), 2)
    r = digest.decay(rows, END)
    assert r[0]["year_ago"] is None and r[0]["flagged_on"] == ["previous"]


def test_decay_sorted_by_loss_and_dates():
    big, small = "https://x.test/big", "https://x.test/small"
    rows = (daily(small, *span("current"), 0) + daily(small, *span("previous"), 1)
            + daily(big, *span("current"), 0) + daily(big, *span("previous"), 3))
    assert [p["page"] for p in digest.decay(rows, END)] == [big, small]
    d = digest.decay_dates(END)
    assert d["current"]["end"] == "2026-11-06" and set(d) == {"current", "previous", "year_ago"}


def lc(rows, tbp=None):
    return digest.low_ctr(rows, tbp or {})


def names(res):
    return [x["query"] for p in res for x in p["queries"]]


def test_low_ctr_position_boundary():
    rows = [q("a", 7.9, imp=1000, clicks=0), q("b", 8.0, imp=1000, clicks=0)]
    assert names(lc(rows)) == ["a"]


def test_low_ctr_impression_boundary():
    rows = [q("a", 3, imp=99, clicks=0), q("b", 3, imp=100, clicks=0)]
    assert names(lc(rows)) == ["b"]


def test_low_ctr_ratio_boundary():
    # position 2 -> expected .15, threshold .075; 100 impressions
    rows = [q("below", 2, imp=1000, clicks=74), q("at", 2, imp=1000, clicks=75)]
    assert names(lc(rows)) == ["below"]


def test_low_ctr_brand_excluded_case_insensitive():
    rows = [q("Serenity Brides colchester", 1, imp=1000, clicks=0), q("bustle", 1, imp=1000, clicks=0)]
    assert names(lc(rows)) == ["bustle"]


def test_low_ctr_rounding_extremes_use_position_1_expected():
    res = lc([q("a", 1.4, imp=1000, clicks=0), q("b", 0.4, imp=1000, clicks=0)])
    assert [x["expected_ctr"] for x in res[0]["queries"]] == [.28, .28]


def test_low_ctr_position_7_9_uses_last_table_entry():
    res = lc([q("a", 7.9, imp=1000, clicks=0)])
    assert res[0]["queries"][0]["expected_ctr"] == .03


def test_low_ctr_sorted_by_missed_and_shape():
    rows = [q("small", 7, imp=200, clicks=0), q("big", 1, imp=1000, clicks=10),
            q("other", 5, imp=500, clicks=0, page="https://x.test/b")]
    res = lc(rows)
    assert [p["page"] for p in res] == [P, "https://x.test/b"]  # 270 vs 25 missed
    p = res[0]
    assert [x["query"] for x in p["queries"]] == ["big", "small"]
    assert p["queries"][0]["missed"] == 270 and p["missed"] == 276
    assert p["impressions"] == 1200 and p["clicks"] == 10
    assert set(p["queries"][0]) == {"query", "impressions", "clicks", "ctr", "expected_ctr", "position", "missed", "tag", "intent", "volume"}


def test_low_ctr_tags():
    rows = [q("short wedding dresses", 2, imp=500, clicks=0), q("prom dress", 2, imp=400, clicks=0)]
    res = lc(rows, {P: ["short wedding dresses"]})
    assert {x["query"]: x["tag"] for x in res[0]["queries"]} == {"short wedding dresses": "on-target", "prom dress": "off-target"}


# ---- issue #20: intent, page-2 clicks lost, cached volumes ----

def test_intent_tagging():
    assert digest.intent("book a wedding dress appointment") == ["booking"]
    assert digest.intent("cheap wedding dresses") == ["sales"]
    assert digest.intent("Buy Dress Near Me") == ["booking", "sales"]
    assert digest.intent("lace wedding dress") == []
    assert digest.intent("wedding shops Colchester") == ["booking"]


def test_intent_whole_word_and_phrase():
    assert digest.intent("undercover bride") == []
    assert digest.intent("dress under 500") == ["sales"]
    assert digest.intent("bridal store nearby") == ["booking"]
    assert digest.intent("shop near me") == ["booking"]
    assert digest.intent("dress near meadow") == []
    assert digest.intent("nearme") == []


def test_page2_missed_formula_and_floor():
    rows = [q("a", 10, imp=200, clicks=3), q("b", 10, imp=100, clicks=9), q("c", 10, imp=40, clicks=0, page="https://x.test/b")]
    res = digest.page2(rows, {}, {"a": 700})
    by = {x["query"]: x for p in res for x in p["queries"]}
    assert by["a"]["missed"] == 200 * 0.05 - 3
    assert by["b"]["missed"] == 0
    assert by["a"]["volume"] == 700 and by["b"]["volume"] is None
    assert by["a"]["intent"] == []
    assert [p["page"] for p in res] == [P, "https://x.test/b"]
    assert res[0]["missed"] == 7 and res[1]["missed"] == 2


def test_low_ctr_rows_get_intent_and_volume():
    res = digest.low_ctr([q("Cheap Dress", 3, imp=1000, clicks=1)], {}, {"cheap dress": 90})
    r = res[0]["queries"][0]
    assert r["intent"] == ["sales"] and r["volume"] == 90


def _vol_con():
    import sqlite3
    con = sqlite3.connect(":memory:")
    digest.ensure_tables(con)
    return con


def _fake_dfs(monkeypatch, cost=0.08):
    import asyncio  # noqa: F401
    from app import dfs
    calls = []

    async def post(c, path, task, **k):
        calls.append((path, task))
        return [{"keyword": kw, "search_volume": None if kw == "nullkw" else 100} for kw in task["keywords"]], cost
    monkeypatch.setattr(dfs, "MOCK", False)
    monkeypatch.setattr(dfs, "post_path", post)
    return calls


def test_fetch_volumes_cache_ttl_and_cost(monkeypatch):
    import asyncio
    calls = _fake_dfs(monkeypatch)
    con = _vol_con()
    r1 = asyncio.run(digest.fetch_volumes(con, ["A", "b", "a", "nullkw"]))
    assert r1 == {"fetched": 3, "cached": 0, "cost": 0.08}
    assert len(calls) == 1
    path, task = calls[0]
    assert path == "keywords_data/google_ads/search_volume/live"
    assert task["keywords"] == ["a", "b", "nullkw"] and task["location_code"] == 2826 and task["language_code"] == "en"
    assert con.execute("SELECT cost, keywords FROM volume_runs").fetchall() == [(0.08, 3)]
    assert digest.cached_volumes(con) == {"a": 100, "b": 100}
    assert digest.volume_status(con) == {"fetched_at": con.execute("SELECT fetched_at FROM volume_runs").fetchone()[0],
                                         "cost": 0.08, "count": 3}
    # second call within TTL: nothing requested, no DFS call, no new run row (NULL volume not re-fetched)
    r2 = asyncio.run(digest.fetch_volumes(con, ["a", "b", "nullkw"]))
    assert r2 == {"fetched": 0, "cached": 3, "cost": 0.0}
    assert len(calls) == 1 and con.execute("SELECT COUNT(*) FROM volume_runs").fetchone()[0] == 1
    # a new query plus a stale row: only those are requested
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat()
    con.execute("UPDATE query_volumes SET fetched_at=? WHERE query='b'", (old,))
    r3 = asyncio.run(digest.fetch_volumes(con, ["a", "b", "c"]))
    assert r3 == {"fetched": 2, "cached": 1, "cost": 0.08}
    assert calls[1][1]["keywords"] == ["b", "c"]
    assert con.execute("SELECT COUNT(*) FROM volume_runs").fetchone()[0] == 2


def test_ads_keyword_cleaning():
    assert digest._ads_keyword("how long does it take to get a wedding dress?") == "how long does it take to get a wedding dress"
    assert digest._ads_keyword("mother's day & more") == "mother's day & more"
    assert digest._ads_keyword("???") is None
    assert digest._ads_keyword(" ".join(["word"] * 11)) is None
    assert digest._ads_keyword("x" * 81) is None
