import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dfs, main, mappack, targets  # noqa: E402

FIX = json.loads((Path(__file__).resolve().parent.parent / "app" / "mock" / "serp_live.json").read_text())
G = "https://google.com/goto?url=CAESabc"


def test_organic_domain_from_breadcrumb_not_google_redirect():
    items = [i for i in FIX["items"] if i["type"] == "organic"]
    assert all(i["domain"] == "google.com" for i in items)  # the trap: DFS reports google.com
    got = [mappack.parse_organic(i) for i in items]
    assert got[0]["domain"] == "oshgoshoutlet.co.uk" and got[0]["url"].startswith("https://oshgoshoutlet.co.uk/collections/")
    assert all(g["domain"] and "google" not in g["domain"] for g in got)


def test_organic_unwraps_google_url_q_and_ignores_date_breadcrumb():
    p = mappack.parse_organic({"url": "https://www.google.com/url?q=https%3A%2F%2Fwww.a.co.uk%2Fx%2Fy&sa=U", "breadcrumb": "1 week ago"})
    assert (p["domain"], p["url"], p["url_exact"]) == ("a.co.uk", "https://www.a.co.uk/x/y", True)
    assert mappack.parse_organic({"url": G, "breadcrumb": "9 likes  ·  5 months ago", "website_name": "Facebook"})["domain"] == "facebook.com"
    assert mappack.parse_organic({"url": G, "breadcrumb": "https://theweddingshop.uk"})["url"] == "https://theweddingshop.uk/"


def test_analyse_fixture():
    a = mappack.analyse(FIX)
    assert a["pack_present"] == 1 and a["pack_size"] == 2
    assert (a["them_pack_rank"], a["them_rating"], a["them_reviews"]) == (1, 4.8, 668)
    assert (a["us_pack_rank"], a["us_rating"], a["us_reviews"]) == (2, 4.5, 356)
    assert a["them_org_pos"] == 2 and a["them_org_url"].startswith("https://theweddingshop.uk/mother-of-the-bride")
    assert a["us_org_pos"] == 6 and a["us_org_url"].startswith("https://serenitybrides.com/blog/")
    assert (a["win_set"], a["won"]) == (1, 1)  # they are in the pack and so are we


def test_no_pack_and_win_rules():
    a = mappack.analyse({"items": [{"type": "organic", "rank_group": 1, "url": G, "domain": "google.com",
                                    "breadcrumb": "https://theweddingshop.uk"}]})
    assert (a["pack_present"], a["them_org_pos"], a["us_org_pos"]) == (0, 1, None)
    assert (a["win_set"], a["won"]) == (1, 0)  # they are #1, we are not
    b = mappack.analyse({"items": []})
    assert (b["win_set"], b["won"]) == (0, 0)
    pack = {"type": "local_pack", "rank_group": 1, "title": "The Wedding Shop", "domain": "theweddingshop.uk"}
    assert mappack.analyse({"items": [pack]})["won"] == 0  # in the pack, we are not


def test_keyword_list_dedupes_case_insensitively_and_skips_templates():
    t = [{"primary_keyword": "Wedding Dresses", "supporting": ["wedding dress", "wedding  dresses"]},
         {"primary_keyword": "{Designer} wedding dresses", "supporting": ["Wedding Dress"]}]
    assert mappack.keyword_list(t) == ["Wedding Dresses", "wedding dress"]


def _db(path):
    def open_db():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        mappack.ensure_tables(c)
        return c
    return open_db


def test_run_stores_results_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(dfs, "MOCK", True)
    open_db = _db(tmp_path / "t.db")
    for n in (1, 2):
        con = open_db()
        rid = mappack.create_run(con, f"2026-1{n}-01T00:00:00", 2)
        con.close()
        asyncio.run(mappack.execute(open_db, rid, ["wedding dresses", "bridal shop"], lambda: "2026-11-01T00:00:01"))
    con = open_db()
    runs = mappack.list_runs(con)
    assert [r["id"] for r in runs] == [2, 1] and runs[0]["status"] == "done"
    s = runs[0]["summary"]
    assert (s["keywords"], s["pack_present"], s["us_in_pack"], s["them_in_pack"], s["wins"]) == (2, 2, 2, 2, 2)
    assert (s["us_avg_pack_rank"], s["them_avg_pack_rank"]) == (2.0, 1.0)
    assert mappack.get_run(con, 1)["results"][0]["them_org_pos"] == 2


def test_failed_keyword_is_recorded_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(dfs, "MOCK", False)

    async def boom(*a, **k):
        raise dfs.DFSError("no credit")
    monkeypatch.setattr(dfs, "post_path", boom)
    open_db = _db(tmp_path / "t.db")
    con = open_db()
    rid = mappack.create_run(con, "now", 1)
    asyncio.run(mappack.execute(open_db, rid, ["x"], lambda: "now"))
    r = mappack.get_run(open_db(), rid)
    assert r["status"] == "error" and r["results"][0]["error"] == "no credit"


def test_api(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "o.db")
    monkeypatch.setattr(dfs, "MOCK", True)
    c = TestClient(main.app)
    assert c.post("/api/mappack/run").status_code == 400  # no targets yet
    con = main.db()
    targets.create(con, {"path": "/", "primary_keyword": "wedding dress shop Colchester", "supporting": ["bridal shop"]})
    con.close()
    with c:
        r = c.post("/api/mappack/run?limit=1")
        assert r.json() == {"id": 1, "keywords": 1}
        import time
        for _ in range(50):
            run = c.get("/api/mappack/runs/1").json()
            if run["status"] != "running":
                break
            time.sleep(0.1)
    assert run["status"] == "done" and run["summary"]["pack_present"] == 1 and run["cost"] == 0
    assert c.get("/api/mappack/runs").json()[0]["summary"]["wins"] == 1
    assert c.get("/api/mappack/runs/99").status_code == 404
    assert c.get("/mappack").status_code == 200


def test_detail_includes_gsc_position(tmp_path, monkeypatch):
    from app import gsc
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "g.db")
    con = main.db()
    gsc.ensure_tables(con)
    mappack.create_run(con, "2026-11-01T00:00:00", 2)
    for kw in ("Bridal Shop", "no gsc kw"):
        con.execute("INSERT INTO mappack_results(run_id,keyword) VALUES (1,?)", (kw,))
    con.execute("INSERT INTO gsc_fetches(id,site,fetched_at,start_date,end_date,rows) VALUES (1,?,?,?,?,?)",
                (gsc.SITE_URL, "2026-11-02", "2026-08-01", "2026-10-30", 2))
    con.executemany("INSERT INTO gsc_rows VALUES (1,?,?,?,?,?,?)",
                    [("https://s/a", "bridal shop", 1, 100, 0.01, 4.0), ("https://s/b", "bridal shop", 0, 300, 0.0, 8.0)])
    con.commit()
    con.close()
    d = TestClient(main.app).get("/api/mappack/runs/1").json()
    pos = {r["keyword"]: r["us_gsc_pos"] for r in d["results"]}
    assert pos == {"Bridal Shop": 7.0, "no gsc kw": None}  # (4*100 + 8*300) / 400
    assert d["gsc"] == {"start": "2026-08-01", "end": "2026-10-30"}
