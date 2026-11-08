import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gsc, main, progress  # noqa: E402

U = lambda p: "https://e.com" + p


def page(path, status=200):
    return dict(url=U(path), status=status, redirect_to=None, content_type="text/html", canonical=None,
                meta_robots=None, title="t", h1="h", meta_description="d", word_count=1, depth=1, in_sitemap=1,
                external_links=0)


def link(src, dst, placement="main", anchor="a"):
    return dict(src=U(src), dst=U(dst), anchor=anchor, placement=placement, nofollow=0)


PAGES = [page(p) for p in ("/", "/a", "/b", "/c", "/d")] + [page("/gone", 404)]
LINKS = [link("/a", "/b"), link("/a", "/c", "nav", "c nav"), link("/", "/d")]
SUG = [{"src": U("/a"), "dst": U("/b"), "anchor": "A"}, {"src": U("/a"), "dst": U("/c"), "anchor": "c nav"},
       {"src": U("/b"), "dst": U("/c"), "anchor": "c"},
       {"src": U("/a"), "dst": U("/gone"), "anchor": "g"}, {"src": U("/nope"), "dst": U("/c"), "anchor": "c"}]


def test_check_links():
    r = progress.check_links(SUG, PAGES, LINKS)
    assert [r[(s["src"], s["dst"])] for s in SUG] == ["done", "partial", "missing", "gone", "gone"]


def test_anchor_match_is_case_and_space_insensitive():
    r = progress.check_links([{"src": U("/a"), "dst": U("/b"), "anchor": "  Blue   DRESS "}], PAGES,
                             [link("/a", "/b", anchor="blue dress")])
    assert r[(U("/a"), U("/b"))] == "done"


def test_wrong_anchor():
    sug = [{"src": U("/a"), "dst": U("/b"), "anchor": "blue dress"}]
    links = [link("/a", "/b", anchor="click here"), link("/a", "/b", "body", "read more")]
    assert progress.check_links(sug, PAGES, links)[(U("/a"), U("/b"))] == "wrong anchor"
    d = progress.check_links_detail(sug, PAGES, links)[(U("/a"), U("/b"))]
    assert d["status"] == "wrong anchor" and sorted(d["live"]) == ["click here", "read more"] and d["anchor_data"]


def test_one_matching_anchor_among_several_is_done():
    sug = [{"src": U("/a"), "dst": U("/b"), "anchor": "blue dress"}]
    links = [link("/a", "/b", anchor="click here"), link("/a", "/b", "body", "Blue dress")]
    assert progress.check_links(sug, PAGES, links)[(U("/a"), U("/b"))] == "done"


def test_missing_link_with_anchor_planned():
    sug = [{"src": U("/b"), "dst": U("/c"), "anchor": "c"}]
    d = progress.check_links_detail(sug, PAGES, LINKS)[(U("/b"), U("/c"))]
    assert d["status"] == "missing" and d["live"] == []


def test_boilerplate_link_stays_partial_even_if_anchor_matches():
    d = progress.check_links_detail([{"src": U("/a"), "dst": U("/c"), "anchor": "c nav"}], PAGES, LINKS)
    assert d[(U("/a"), U("/c"))]["status"] == "partial"


def test_crawl_without_anchor_data_falls_back():
    links = [{**l, "anchor": None} for l in LINKS]
    sug = [{"src": U("/a"), "dst": U("/b"), "anchor": "whatever"}]
    d = progress.check_links_detail(sug, PAGES, links)[(U("/a"), U("/b"))]
    assert d["status"] == "done" and d["anchor_data"] is False and d["live"] == []


def test_built_pages():
    b = progress.built_pages([{"slug": "/a"}, {"slug": "/b/"}, {"slug": "/zzz"}, {"slug": "/gone"}], PAGES)
    assert [x["built"] for x in b] == [True, True, False, False]


def daily(start, n, clicks=1, imp=10, pos=5.0):
    d0 = dt.date.fromisoformat(start)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "clicks": clicks, "impressions": imp, "position": pos}
            for i in range(n)]


def test_window_stats_full_and_partial():
    done = dt.date(2026, 3, 1)
    rows = daily("2026-01-01", 59, 1, 10, 10.0) + daily("2026-03-01", 28, 3, 20, 4.0)
    w = gsc.window_stats(rows, done, dt.date(2026, 3, 28))
    assert w["days"] == 28
    assert w["before"] == {"clicks": 28, "impressions": 280, "position": 10.0}
    assert w["after"] == {"clicks": 84, "impressions": 560, "position": 4.0}
    # only 5 days of data after: both windows shrink to 5 days
    w = gsc.window_stats(rows, done, dt.date(2026, 3, 5))
    assert w["days"] == 5 and w["before"]["clicks"] == 5 and w["after"]["clicks"] == 15
    assert gsc.window_stats(rows, done, dt.date(2026, 2, 28)) is None


def test_window_position_is_impression_weighted():
    done = dt.date(2026, 3, 1)
    rows = [{"date": "2026-03-01", "clicks": 0, "impressions": 10, "position": 2.0},
            {"date": "2026-03-02", "clicks": 0, "impressions": 30, "position": 6.0}]
    assert gsc.window_stats(rows, done, dt.date(2026, 3, 2))["after"]["position"] == 5.0


def test_page_regex_and_daily_upsert(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(gsc, "SITE_URL", "sc-domain:e.com")
    monkeypatch.setattr(gsc, "KEY_FILE", str(tmp_path / "k.json"))
    (tmp_path / "k.json").write_text("{}")
    rx = gsc._page_regex([U("/a"), U("/")], "e.com")
    assert re.match(rx, "https://www.e.com/a/") and re.match(rx, "http://e.com/") and not re.match(rx, "https://e.com/ab")
    calls = []

    def q(client, token, body):
        calls.append(body)
        return [{"keys": ["https://www.e.com/a/", "2026-03-01"], "clicks": 2, "impressions": 10, "position": 3.0},
                {"keys": ["https://e.com/a", "2026-03-01"], "clicks": 1, "impressions": 30, "position": 7.0}]
    monkeypatch.setattr(gsc, "_query", q)
    monkeypatch.setattr(gsc, "_token", lambda: "t")
    import sqlite3
    con = sqlite3.connect(tmp_path / "d.db")
    assert gsc.fetch_daily(con, [U("/a")]) == 1 and gsc.fetch_daily(con, [U("/a")]) == 1  # upsert, no duplicates
    assert con.execute("SELECT clicks,impressions,position FROM gsc_daily").fetchall() == [(3.0, 40.0, 6.0)]
    assert calls[0]["dimensions"] == ["page", "date"]


def test_check_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main.gsc, "configured", lambda: False)
    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,finished_at,status) VALUES (1,'e.com','2026-01-01','2026-01-01','done')")
        con.execute("INSERT INTO crawls(id,domain,started_at,finished_at,status) VALUES (2,'e.com','2026-02-01','2026-02-01T10:00:00','done')")
        con.execute("INSERT INTO plans(id,crawl_id,created_at,status) VALUES (1,1,'t','done')")
        con.executemany("INSERT INTO pages(crawl_id,url,status,content_type) VALUES (2,?,?, 'text/html')",
                        [(p["url"], p["status"]) for p in PAGES])
        con.executemany("INSERT INTO links(crawl_id,src,dst,anchor,placement,nofollow) VALUES (2,?,?,?,?,0)",
                        [(l["src"], l["dst"], l["anchor"], l["placement"]) for l in LINKS])
        con.executemany("INSERT INTO suggestions(plan_id,src,dst,anchor) VALUES (1,?,?,?)",
                        [(s["src"], s["dst"], s["anchor"]) for s in SUG] + [(U("/"), U("/d"), "different")])
        con.commit()
    with TestClient(main.app) as c:
        r = c.post("/api/plan/1/check")
        assert r.status_code == 200
        d = r.json()
        assert d["checked_crawl_id"] == 2 and d["counts"]["done"] == 1 and d["counts"]["gone"] == 2
        assert d["counts"]["wrong anchor"] == 1
        w = [s for s in d["suggestions"] if s["status"] == "wrong anchor"][0]
        assert w["planned_anchor"] == "different" and w["live_anchors"] == ["a"] and w["anchor_data"] is True
        assert d["pct_done"] == 16.7 and any("not configured" in n for n in d["notes"])
        done = [s for s in d["suggestions"] if s["status"] == "done"][0]
        assert done["done_at"] == "2026-02-01T10:00:00" and done["done_crawl_id"] == 2
        assert c.post("/api/plan/9/check").status_code == 404
        assert c.get("/api/plan/1/progress").json()["counts"] == d["counts"]


def test_gaps_lifecycle(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main.gsc, "configured", lambda: False)
    seen = []

    async def build_gaps(pages, strength, gsc_rows, comparison, progress=None, tvol=None):
        seen.append((len(pages), comparison))
        progress("working")
        return {"pages": [], "improve": [], "dropped_covered": 0, "sources": {}, "cost": 0.0, "model": None,
                "llm_used": False, "warnings": []}
    monkeypatch.setattr(main.pagegap, "build_gaps", build_gaps)
    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (1,'x.com','2026-01-01','done')")
        con.execute("INSERT INTO pages(crawl_id,url,status,content_type) VALUES (1,'https://x.com/a',200,'text/html')")
        con.execute("INSERT INTO comparisons(domain,competitors,location_code,language_code,fetched_at,data) "
                    "VALUES ('x.com','c.com',2826,'en','2026-01-02','{\"missing\": []}')")
        con.commit()
    with TestClient(main.app) as c:
        gid = c.post("/api/crawl/1/gaps").json()["id"]
        assert c.post("/api/crawl/9/gaps").status_code == 404
        for _ in range(50):
            d = c.get(f"/api/gaps/{gid}").json()
            if d["status"] != "running":
                break
            time.sleep(0.1)
        assert d["status"] == "done", d
        assert [g["id"] for g in c.get("/api/crawl/1/gaps").json()] == [gid]
    assert seen == [(1, {"missing": []})]
