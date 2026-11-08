import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import linkmap as lm, main  # noqa: E402

U = lambda p: "https://serenitybrides.com" + p
LACE, WD, SINC, VENI = (U("/collections/bridal/lace"), U("/collections/bridal"), U("/collections/bridal/sincerity"),
                        U("/collections/bridal/veni"))
ROWS = [
    {"id": 1, "path": "/collections/bridal/lace", "primary_keyword": "lace wedding dresses", "supporting": [],
     "product_line": "bridal"},
    {"id": 2, "path": "/collections/bridal", "primary_keyword": "wedding dresses", "supporting": [],
     "product_line": "bridal"},
]
PAGES = [{"url": u, "status": 200} for u in (LACE, WD, SINC)]
S = {u: {"url": u, "h1": "x", "line": "bridal"} for u in (LACE, WD)}


def link(src, dst, anchor, placement="main"):
    return {"src": U(src), "dst": dst, "anchor": anchor, "placement": placement}


def run(links, sugg=(), gsc=(), rows=ROWS, S=S, has_plan=True, ranges=()):
    res = lm.build(rows, PAGES, links, S, set(ranges), list(sugg), list(gsc), has_plan)
    return {e["target_id"]: e for e in res["entries"]}, res


def sg(src, dst, anchor="a", status="missing"):
    return {"src": U(src), "dst": dst, "planned_anchor": anchor, "status": status}


def test_needs_links():
    e, _ = run([link("/blog/a", LACE, "lace dresses"), link("/blog/b", LACE, "lace gowns")])
    assert "needs links" in e[1]["flags"]
    e, _ = run([link(f"/blog/{c}", LACE, f"anchor {c}") for c in "abc"])
    assert "needs links" not in e[1]["flags"]


def test_template_links_do_not_count_as_contextual():
    links = [link(f"/blog/{i}", LACE, "lace dresses", "footer") for i in range(5)]
    e, _ = run(links)
    assert e[1]["contextual"] == [] and e[1]["template"][0]["src_count"] == 5 and "needs links" in e[1]["flags"]


def test_anchor_monotony():
    same = [link(f"/blog/{i}", LACE, "Lace Dresses" if i % 2 else "lace dresses") for i in range(4)]
    e, _ = run(same + [link("/blog/x", LACE, "other")])
    assert "anchor monotony" in e[1]["flags"]  # 4 of 5 = 80%
    e, _ = run([link("/blog/a", LACE, "same"), link("/blog/b", LACE, "same")])
    assert "anchor monotony" not in e[1]["flags"]  # fewer than 3 links
    e, _ = run([link(f"/blog/{i}", LACE, f"anchor {i % 2}") for i in range(4)])
    assert "anchor monotony" not in e[1]["flags"]  # 50%


def test_anchor_conflict_and_own_keyword_exemption():
    e, _ = run([link("/blog/a", LACE, "our wedding dresses")])
    c = e[1]["conflicts"]
    assert "anchor conflict" in e[1]["flags"]
    assert c[0]["other_target_id"] == 2 and c[0]["keyword"] == "wedding dresses" and c[0]["src"] == U("/blog/a")
    e, _ = run([link("/blog/a", LACE, "Lace Wedding Dresses")])
    assert e[1]["conflicts"] == [] and "anchor conflict" not in e[1]["flags"]  # contains own keyword
    e, _ = run([link("/blog/a", LACE, "wedding dressesque")])
    assert e[1]["conflicts"] == []  # word boundary
    e, _ = run([link("/blog/a", LACE, "wedding dresses", "footer")])
    assert len(e[1]["conflicts"]) == 1  # template/nav links are checked too


def test_wrong_page_ranking():
    g = [{"page": SINC, "query": "Lace Wedding Dresses", "impressions": 90, "position": 4.0},
         {"page": LACE, "query": "lace wedding dresses", "impressions": 10, "position": 12.0}]
    e, _ = run([], gsc=g)
    x = e[1]
    assert x["wrong_page"] and "wrong page ranking" in x["flags"]
    assert x["gsc_top_page"] == SINC and x["gsc_top_page_position"] == 4.0 and x["gsc_target_page_position"] == 12.0
    e, _ = run([], gsc=g[1:])
    assert not e[1]["wrong_page"] and "wrong page ranking" not in e[1]["flags"]


def test_not_in_plan():
    e, _ = run([], sugg=[sg("/blog/a", WD)])
    assert "not in plan" in e[1]["flags"] and "not in plan" not in e[2]["flags"]
    e, _ = run([], has_plan=False)
    assert "not in plan" not in e[1]["flags"]


def test_contextual_status_and_planned_missing():
    sugg = [sg("/blog/a", LACE, "lace gowns", "wrong anchor"), sg("/blog/b", LACE, "lace", "missing"),
            sg("/blog/c", LACE, "lace", "done")]
    e, _ = run([link("/blog/a", LACE, "lace dresses"), link("/blog/z", LACE, "bridal lace")], sugg=sugg)
    st = {c["src"]: (c["status"], c["planned_anchor"]) for c in e[1]["contextual"]}
    assert st[U("/blog/a")] == ("wrong anchor", "lace gowns") and st[U("/blog/z")] == ("existing", None)
    assert [m["src"] for m in e[1]["planned_missing"]] == [U("/blog/a"), U("/blog/b")]


def test_template_link_detection_by_section_count():
    n = lm.TEMPLATE_MIN_SRCS
    many = [link(f"/dresses/d{i}", LACE, "Also in lace") for i in range(n)]
    e, _ = run(many)
    assert e[1]["contextual"] == [] and e[1]["template"] == [{"anchor": "Also in lace", "src_count": n}]
    spread = [link(f"/s{i}/d", LACE, "Also in lace") for i in range(n)]  # different sections: not a template
    e, _ = run(spread)
    assert len(e[1]["contextual"]) == n and e[1]["template"] == []
    e, _ = run(many[: n - 1])
    assert len(e[1]["contextual"]) == n - 1


def test_template_target_expands_per_page():
    rows = [{"id": 3, "path": "/collections/bridal/veni", "primary_keyword": "{Designer} wedding dresses",
             "supporting": ["veni bridal"], "product_line": "bridal"}]
    pages = {u: {"url": u, "h1": h, "line": "bridal"} for u, h in ((VENI, "Veni Wedding Dresses"), (SINC, "Sincerity Wedding Dresses"))}
    e, res = run([], rows=rows, S=pages, ranges=[VENI, SINC])
    kws = {x["target_page"]: x["keywords"] for x in res["entries"]}
    assert kws[VENI] == ["Veni wedding dresses", "veni bridal"]
    assert kws[SINC] == ["Sincerity wedding dresses"] and len(res["entries"]) == 2


def test_other_links_exclude_phase_two():
    sugg = [sg("/blog/a", U("/blog/x")), sg("/blog/a", U("/collections/mother-of-the-bride/lace")),
            sg("/blog/a", U("/collections/bridesmaids")), sg("/blog/a", LACE)]
    _, res = run([], sugg=sugg)
    assert [o["dst"] for o in res["other_links"]] == [U("/blog/x")]


def test_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (1,'serenitybrides.com','2026-01-01','done')")
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (2,'serenitybrides.com','2026-01-02','running')")
        for u in (LACE, WD):
            con.execute("INSERT INTO pages(crawl_id,url,status,content_type) VALUES (1,?,200,'text/html')", (u,))
        con.execute("INSERT INTO links(crawl_id,src,dst,anchor,placement) VALUES (1,?,?,?,'main')", (WD, LACE, "lace"))
        con.execute("INSERT INTO keyword_targets(path,primary_keyword,supporting) VALUES ('/collections/bridal/lace','lace wedding dresses','[]')")
        con.commit()
    with TestClient(main.app) as c:
        r = c.get("/api/linkmap")
        assert r.status_code == 200
        d = r.json()
        assert d["crawl_id"] == 1  # latest done, not the running crawl
        for k in ("plan_id", "checked_crawl_id", "gsc", "summary", "entries", "other_links"):
            assert k in d
        e = d["entries"][0]
        for k in ("target_id", "product_line", "keywords", "target_page", "crawl_status", "gsc_top_page", "contextual",
                  "planned_missing", "template", "flags", "conflicts", "wrong_page"):
            assert k in e
        assert e["crawl_status"] == "200" and len(e["contextual"]) == 1 and "needs links" in e["flags"]
        assert c.get("/api/linkmap?crawl=99").status_code == 404


def test_ready_to_wear_anchor_not_conflict():
    rows = ROWS + [{"id": 4, "path": "/collections/bridal/rtw", "primary_keyword": "ready to wear wedding dresses",
                    "supporting": [], "product_line": "bridal"}]
    e, _ = run([link("/blog/a", U("/collections/bridal/rtw"), "Ready to wear wedding dresses")], rows=rows)
    assert e[2]["conflicts"] == [] and e[4]["conflicts"] == []  # longest keyword is the rtw target's own
    e, _ = run([link("/blog/a", WD, "wedding dresses ready to wear")], rows=rows)
    assert e[2]["conflicts"][0]["other_target_id"] == 4 and e[2]["conflicts"][0]["keyword"] == "ready to wear wedding dresses"


def test_onpage_missing():
    pg = {"url": LACE, "status": 200, "title": "Lace Wedding Dresses | X", "h1": "Our collection",
          "text": "Browse lace wedding dress styles. More."}
    res = lm.build(ROWS, [pg, {"url": WD, "status": 200}], [], S, set(), [], [])
    e = {x["target_id"]: x for x in res["entries"]}
    assert e[1]["onpage_missing"] == ["h1"] and "keyword missing: h1" in e[1]["flags"]
    pg["h1"] = "Lace wedding dresses"
    res = lm.build(ROWS, [pg], [], S, set(), [], [])
    assert res["entries"][0]["onpage_missing"] == []  # /bridal/lace carries "lace wedding dresses"
    assert not any(f.startswith("keyword missing") for f in res["entries"][0]["flags"])
    res = lm.build(ROWS, [{"url": SINC, "status": 200}], [], S, set(), [], [])
    assert res["entries"][0]["onpage_missing"] is None
    assert not any(f.startswith("keyword missing") for f in res["entries"][0]["flags"])


def test_onpage_missing_template_per_page():
    rows = [{"id": 3, "path": "/collections/bridal/veni", "primary_keyword": "{Designer} wedding dresses",
             "supporting": [], "product_line": "bridal"}]
    pages = [{"url": VENI, "status": 200, "title": "Veni Wedding Dresses", "h1": "Veni wedding dresses",
              "text": "Veni wedding dresses are lovely."},
             {"url": SINC, "status": 200, "title": "Sincerity Wedding Dresses", "h1": "Dresses", "text": "Hello."}]
    Sx = {u: {"url": u, "h1": h, "line": "bridal"} for u, h in ((VENI, "Veni Wedding Dresses"), (SINC, "Sincerity Wedding Dresses"))}
    res = lm.build(rows, pages, [], Sx, {VENI, SINC}, [], [])
    e = {x["target_page"]: x for x in res["entries"]}
    assert e[VENI]["onpage_missing"] == []  # path /bridal/veni carries "Veni wedding dresses"
    assert e[SINC]["onpage_missing"] == ["h1", "first sentence"]  # checked against "Sincerity wedding dresses"
