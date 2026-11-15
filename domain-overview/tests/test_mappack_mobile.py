import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compare, dfs, mappack  # noqa: E402

G = "https://google.com/goto?url=CAES"


def _db(path):
    def open_db():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        mappack.ensure_tables(c)
        return c
    return open_db


def _serp(us_org, them_org):
    items = []
    if them_org:
        items.append({"type": "organic", "rank_group": them_org, "url": G, "domain": "google.com", "breadcrumb": "https://theweddingshop.uk"})
    if us_org:
        items.append({"type": "organic", "rank_group": us_org, "url": "https://serenitybrides.com/x", "domain": "serenitybrides.com"})
    return {"items": items}


def _run(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(dfs, "MOCK", False)
    calls = []

    async def post(c, path, task, **k):
        calls.append(task["device"])
        return fake(task["device"])
    monkeypatch.setattr(dfs, "post_path", post)
    open_db = _db(tmp_path / "t.db")
    con = open_db()
    rid = mappack.create_run(con, "now", 1)
    asyncio.run(mappack.execute(open_db, rid, ["kw"], lambda: "now"))
    return mappack.get_run(open_db(), rid), calls


def test_mobile_columns_stored_and_cost_summed(tmp_path, monkeypatch):
    run, calls = _run(tmp_path, monkeypatch, lambda d: (_serp(15, 7), 0.004) if d == "desktop" else (_serp(5, 1), 0.004))
    r = run["results"][0]
    assert sorted(calls) == ["desktop", "mobile"]
    assert (r["us_org_pos"], r["them_org_pos"]) == (15, 7) and (r["m_us_org_pos"], r["m_them_org_pos"]) == (5, 1)
    assert r["m_us_org_url"] == "https://serenitybrides.com/x" and r["m_pack_present"] == 0 and r["error"] is None and r["m_error"] is None
    assert abs(r["cost"] - 0.008) < 1e-9 and abs(run["cost"] - 0.008) < 1e-9
    assert (r["win_set"], r["won"]) == (0, 0)  # desktop-based: they are #7, not #1


def test_one_device_failing_keeps_the_other(tmp_path, monkeypatch):
    def fake(d):
        if d == "mobile":
            raise dfs.DFSError("mobile down")
        return _serp(3, 1), 0.004
    run, _ = _run(tmp_path, monkeypatch, fake)
    r = run["results"][0]
    assert r["us_org_pos"] == 3 and r["error"] is None and r["m_error"] == "mobile down" and r["m_pack_present"] is None
    assert run["status"] == "done" and abs(r["cost"] - 0.004) < 1e-9


def test_desktop_failing_keeps_mobile(tmp_path, monkeypatch):
    def fake(d):
        if d == "desktop":
            raise dfs.DFSError("desktop down")
        return _serp(2, 4), 0.004
    run, _ = _run(tmp_path, monkeypatch, fake)
    r = run["results"][0]
    assert r["error"] == "desktop down" and r["m_us_org_pos"] == 2 and r["m_them_org_pos"] == 4 and run["status"] == "done"


def test_both_failing_is_an_error_run(tmp_path, monkeypatch):
    def fake(d):
        raise dfs.DFSError("no credit")
    run, _ = _run(tmp_path, monkeypatch, fake)
    assert run["status"] == "error" and run["results"][0]["error"] == "no credit"


def test_pack_ad_excluded_from_rank_numbering():
    ad = {"type": "local_pack", "rank_group": 1, "title": "Serenity Brides        My Ad Centre", "domain": None, "url": None, "is_paid": True}
    them = {"type": "local_pack", "rank_group": 2, "title": "The Wedding Shop", "domain": "theweddingshop.uk"}
    us = {"type": "local_pack", "rank_group": 3, "title": "Serenity Brides", "domain": "serenitybrides.com"}
    a = mappack.analyse({"items": [ad, them, us]})
    assert (a["pack_size"], a["them_pack_rank"], a["us_pack_rank"]) == (2, 1, 2)
    only_ad = mappack.analyse({"items": [ad]})
    assert (only_ad["pack_present"], only_ad["us_pack_rank"]) == (0, None)
    # ad detected by title alone when is_paid is absent
    assert mappack.is_ad({"title": "Serenity Brides   My Ad Centre"}) and not mappack.is_ad({"title": "Serenity Brides"})


def test_paid_organic_items_ignored():
    paid = {"type": "paid", "rank_group": 1, "url": "https://serenitybrides.com/", "domain": "serenitybrides.com", "title": "Serenity Brides"}
    assert mappack.analyse({"items": [paid]})["us_org_pos"] is None


def test_social_results_resolve_to_platform_domain():
    # copied from the real 'wedding shop colchester' desktop SERP (url shortened)
    fb = {"type": "organic", "rank_group": 10, "url": G, "domain": "google.com", "breadcrumb": "30+ comments  ·  9 months ago",
          "website_name": "Facebook\xa0·\xa0Colchester Community Group (UK)", "title": "Wedding dress shop recommendations"}
    ig = {"type": "organic", "rank_group": 4, "url": G, "domain": "google.com", "breadcrumb": "84.2k+ followers",
          "website_name": "Instagram\xa0·\xa0motherofthebrideessexandlondon", "title": "motherofthebrideessexandlondon"}
    assert mappack.parse_organic(fb)["domain"] == "facebook.com" and mappack.parse_organic(ig)["domain"] == "instagram.com"
    assert mappack.parse_organic({"url": G, "breadcrumb": "1 week ago", "website_name": "Some Brand"})["domain"] is None


def test_ensure_tables_idempotent_on_old_schema():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mappack_results (run_id INTEGER NOT NULL, keyword TEXT NOT NULL, error TEXT, cost REAL, PRIMARY KEY(run_id, keyword))")
    con.execute("INSERT INTO mappack_results(run_id,keyword) VALUES (1,'a')")
    mappack.ensure_tables(con)
    mappack.ensure_tables(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(mappack_results)")}
    assert set(mappack.MOBILE_COLS) <= cols
    assert con.execute("SELECT m_us_org_pos FROM mappack_results").fetchone() == (None,)


def _live(mobile=True, desktop_err=None):
    con = sqlite3.connect(":memory:")
    mappack.ensure_tables(con)
    con.execute("INSERT INTO mappack_runs(id,started_at,finished_at,status) VALUES (1,'2026-11-01','2026-11-02','done')")
    con.execute("INSERT INTO mappack_results(run_id,keyword,checked_at,error,us_org_pos,us_org_url,them_org_pos,them_org_url) "
                "VALUES (1,'kw','t',?,15,'https://s/d',7,'https://t/d')", (desktop_err,))
    if mobile:
        con.execute("UPDATE mappack_results SET m_pack_present=0, m_us_org_pos=5, m_us_org_url='https://s/m', m_them_org_pos=NULL")
    return con


def _apply(con):
    by = {"kw": {"keyword": "kw", "you": {"position": 9, "url": None, "source": "labs"},
                 "competitors": {"theweddingshop.uk": {"position": 3, "url": None, "source": "labs"}}}}
    compare.apply_live(by, "serenitybrides.com", ["theweddingshop.uk"], compare.load_live(con))
    return by["kw"]


def test_apply_live_prefers_mobile():
    row = _apply(_live())
    y, t = row["you"], row["competitors"]["theweddingshop.uk"]
    assert (y["position"], y["url"], y["device"], y["desktop_position"]) == (5, "https://s/m", "mobile", 15)
    assert (t["position"], t["device"], t["desktop_position"]) == (None, "mobile", 7)  # mobile: not in top 20


def test_apply_live_falls_back_to_desktop():
    y = _apply(_live(mobile=False))["you"]
    assert (y["position"], y["device"], "desktop_position" in y) == (15, "desktop", False)


def test_apply_live_uses_mobile_when_desktop_failed():
    y = _apply(_live(desktop_err="boom"))["you"]
    assert (y["position"], y["device"], y["desktop_position"] if "desktop_position" in y else "absent") == (5, "mobile", "absent")
