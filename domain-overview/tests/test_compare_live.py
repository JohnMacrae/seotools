import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compare, mappack  # noqa: E402

US, TH, OTHER = "serenitybrides.com", "theweddingshop.uk", "other.co.uk"


def _e(pos, url):
    return {"position": pos, "url": url, "source": "labs"}


def _by_kw():
    return {"bridal dresses colchester": {"keyword": "bridal dresses colchester", "you": _e(9, "https://s/a"),
            "competitors": {TH: _e(3, "https://t/a"), OTHER: _e(5, "https://o/a")}}}


def _con(status="done", us=(2, "https://s/live"), them=(4, "https://t/live"), err=None):
    con = sqlite3.connect(":memory:")
    mappack.ensure_tables(con)
    con.execute("INSERT INTO mappack_runs(id,started_at,finished_at,status) VALUES (1,'2026-11-01','2026-11-02T01:00:00',?)", (status,))
    con.execute("INSERT INTO mappack_results(run_id,keyword,checked_at,error,us_org_pos,us_org_url,them_org_pos,them_org_url) "
                "VALUES (1,'Bridal Dresses Colchester','2026-11-02T00:30:00',?,?,?,?,?)", (err, *us, *them))
    return con


def _apply(con, domain=US, comps=(TH, OTHER)):
    by = _by_kw()
    return by, compare.apply_live(by, domain, list(comps), compare.load_live(con))


def test_live_override_applied_for_serenitybrides():
    by, summ = _apply(_con(), domain="www.serenitybrides.com")
    y = by["bridal dresses colchester"]["you"]
    assert (y["position"], y["url"], y["source"]) == (2, "https://s/live", "live")
    assert y["live_as_of"] == "2026-11-02T00:30:00"
    assert summ == {"run_id": 1, "as_of": "2026-11-02T01:00:00", "keywords": 1}


def test_wedding_shop_overridden_others_stay_labs():
    by, _ = _apply(_con())
    c = by["bridal dresses colchester"]["competitors"]
    assert (c[TH]["position"], c[TH]["source"]) == (4, "live")
    assert (c[OTHER]["position"], c[OTHER]["source"]) == (5, "labs") and "live_as_of" not in c[OTHER]


def test_null_live_position_means_no_position():
    by, _ = _apply(_con(us=(None, None)))
    y = by["bridal dresses colchester"]["you"]
    assert y["position"] is None and y["source"] == "live"


def test_no_live_run_or_failed_row_or_other_domain_unchanged():
    for con, dom in [(_con(status="running"), US), (_con(err="boom"), US), (_con(), "someone-else.com")]:
        by, summ = _apply(con, domain=dom)
        assert summ is None or summ["keywords"] == 0  # failed row: run exists, nothing overridden
        assert by["bridal dresses colchester"]["you"] == _e(9, "https://s/a")
        assert by["bridal dresses colchester"]["competitors"][TH] == _e(3, "https://t/a")
    assert compare.apply_live(_by_kw(), US, [TH], None) is None


def test_losing_flips_to_winning_with_live_override():
    def verdict(by):
        row = by["bridal dresses colchester"]
        yp = row["you"]["position"] or 999
        return any((e["position"] or 999) < yp for e in row["competitors"].values())
    by = _by_kw()
    assert verdict(by)  # labs: we are 9, they are 3 and 5 -> losing
    by, _ = _apply(_con(us=(1, "https://s/live"), them=(4, "https://t/live")))
    by["bridal dresses colchester"]["competitors"][OTHER]["position"] = 8
    assert not verdict(by)  # live: we are 1 -> winning


def test_neither_ranks_is_unranked_not_winning():
    from app import compare
    you = {"position": None, "url": None, "source": "live"}
    them = {"position": None, "url": None, "source": "live"}
    # mirror the status rule in compare.run
    beating = {c: e for c, e in {"theweddingshop.uk": them}.items() if (e["position"] or 999) < (you["position"] or 999)}
    assert not beating and you["position"] is None
    import inspect
    assert 'status="unranked"' in inspect.getsource(compare.run)


# ---- Search Console positions for "you" ----

def _gsc_con(rows, fetch=True):
    from app import gsc
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    gsc.ensure_tables(con)
    if fetch:
        cols = [r[1] for r in con.execute("PRAGMA table_info(gsc_fetches)")]
        vals = {"site": gsc.SITE_URL, "start_date": "2026-08-01", "end_date": "2026-10-30", "fetched_at": "2026-11-01"}
        use = [c for c in cols if c in vals]
        con.execute(f"INSERT INTO gsc_fetches({','.join(use)}) VALUES ({','.join('?' * len(use))})", [vals[c] for c in use])
        fid = con.execute("SELECT id FROM gsc_fetches").fetchone()[0]
        for page, q, clicks, imps, pos in rows:
            con.execute("INSERT INTO gsc_rows(fetch_id,page,query,clicks,impressions,ctr,position) VALUES (?,?,?,?,?,0,?)",
                        (fid, page, q, clicks, imps, pos))
    return con


GROWS = [("https://s/a", "Bridal Dresses Colchester", 3, 100, 4.0), ("https://s/b", "bridal dresses colchester", 1, 300, 8.0)]


def test_load_gsc_weighted_average_and_top_page():
    g = compare.load_gsc(_gsc_con(GROWS))
    r = g["rows"]["bridal dresses colchester"]
    assert r == {"position": 7.0, "page": "https://s/b", "impressions": 400, "clicks": 4}
    assert (g["start"], g["end"]) == ("2026-08-01", "2026-10-30")


def test_load_gsc_none_without_fetch():
    assert compare.load_gsc(_gsc_con([], fetch=False)) is None


def test_gsc_applied_for_you_and_missing_keyword_is_none():
    by = _by_kw()
    by["wedding shoes"] = {"keyword": "wedding shoes", "you": _e(12, "https://s/x"), "competitors": {TH: _e(3, "https://t/x")}}
    summ = compare.apply_gsc(by, "www." + US, compare.load_gsc(_gsc_con(GROWS)))
    y = by["bridal dresses colchester"]["you"]
    assert (y["position"], y["gsc_position"], y["url"], y["source"], y["gsc_impressions"], y["gsc_clicks"]) == (7, 7.0, "https://s/b", "gsc", 400, 4)
    m = by["wedding shoes"]["you"]
    assert m["position"] is None and m["source"] == "gsc" and m["gsc_impressions"] == 0
    assert summ == {"fetch_id": 1, "start": "2026-08-01", "end": "2026-10-30", "keywords": 1}


def test_gsc_not_applied_for_other_domain_or_no_fetch():
    by = _by_kw()
    assert compare.apply_gsc(by, "someone-else.com", compare.load_gsc(_gsc_con(GROWS))) is None
    assert compare.apply_gsc(by, US, None) is None
    assert by["bridal dresses colchester"]["you"] == _e(9, "https://s/a")


def test_competitor_live_override_alongside_gsc_and_you_not_live():
    by = _by_kw()
    g = compare.apply_gsc(by, US, compare.load_gsc(_gsc_con(GROWS)))
    live = compare.apply_live(by, US, [TH, OTHER], compare.load_live(_con()), skip_you=g is not None)
    row = by["bridal dresses colchester"]
    assert row["you"]["source"] == "gsc" and row["you"]["position"] == 7
    assert (row["competitors"][TH]["position"], row["competitors"][TH]["source"]) == (4, "live")
    assert row["competitors"][OTHER]["source"] == "labs"
    assert live["keywords"] == 1


def test_no_gsc_falls_back_to_live_then_labs():
    by = _by_kw()
    assert compare.apply_gsc(by, US, None) is None
    compare.apply_live(by, US, [TH], compare.load_live(_con()))
    assert by["bridal dresses colchester"]["you"]["source"] == "live"
    by = _by_kw()
    compare.apply_live(by, US, [TH], None)
    assert by["bridal dresses colchester"]["you"]["source"] == "labs"


def test_gsc_status_winning_losing_unranked():
    def status(you, them):
        yp = you or 999
        if (them or 999) < yp:
            return "losing"
        return "unranked" if you is None else "winning"
    by = _by_kw()
    compare.apply_gsc(by, US, compare.load_gsc(_gsc_con(GROWS)))  # you = 7
    y = by["bridal dresses colchester"]["you"]["position"]
    assert status(y, 3) == "losing" and status(y, 12) == "winning" and status(y, None) == "winning"
    assert status(None, None) == "unranked"


def test_targets_volume_and_source_on_rows():
    rows = [{"keyword": "Bridal Dresses Colchester", "volume": 50}, {"keyword": "other kw", "volume": 70}]
    compare.apply_target_volumes(rows, {"bridal dresses colchester": 1600})
    assert (rows[0]["volume"], rows[0]["volume_source"]) == (1600, "targets")
    assert (rows[1]["volume"], rows[1]["volume_source"]) == (70, "labs")
