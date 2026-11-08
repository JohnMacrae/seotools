import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import linkmap, linkplan as lp, llm, main, supporting as sp  # noqa: E402

U = lambda p: "https://serenitybrides.com" + p
TARGET = U("/collections/bridal/short")
FAQ = U("/faq/how-long-does-it-take-to-get-a-wedding-dress")
BLOG = U("/blog/wedding-planning")
FAQTEXT = ("Most brides order a gown six to nine months ahead. If your date is close, ask about short wedding dresses "
           "you can take home on the day. " + "Filler sentence about the appointment. " * 10)
PAGES = [
    {"url": TARGET, "status": 200, "content_type": "text/html", "title": "Short wedding dresses", "h1": "Short", "text": "x" * 300},
    {"url": FAQ, "status": 200, "content_type": "text/html", "title": "How long", "h1": "How long", "text": FAQTEXT},
    {"url": BLOG, "status": 200, "content_type": "text/html", "title": "Planning", "h1": "Planning", "text": "Plain text. " * 40},
    {"url": U("/dresses/some-dress"), "status": 200, "content_type": "text/html", "title": "d", "h1": "d", "text": "d" * 300},
    {"url": U("/collections/mother-of-the-bride/lace"), "status": 200, "content_type": "text/html", "title": "m", "h1": "m", "text": "m" * 300},
    {"url": U("/collections/bridesmaids"), "status": 200, "content_type": "text/html", "title": "b", "h1": "b", "text": "b" * 300},
    {"url": U("/faq/noindex"), "status": 200, "content_type": "text/html", "meta_robots": "noindex", "title": "n", "h1": "n", "text": "n" * 300},
    {"url": U("/faq/gone"), "status": 404, "content_type": "text/html", "title": "g", "h1": "g", "text": "g" * 300},
]
KW = ["short wedding dresses", "short bridal gowns"]
ROW = {"id": 1, "path": "/collections/bridal/short", "primary_keyword": "short wedding dresses",
       "supporting": ["short bridal gowns"], "product_line": "bridal", "phase": 1}
STRENGTH = {"pages": [{"url": TARGET, "line": "bridal", "indexable": True}]}


def test_eligible_exclusions():
    got = {p["url"] for p in sp.eligible(PAGES, TARGET)}
    assert got == {FAQ, BLOG}


def test_variants_and_place_validation():
    assert sp.is_variant("Short Wedding Dress", KW) and sp.is_variant("gowns short bridal", KW)
    assert not sp.is_variant("lace dresses", KW)
    text = FAQTEXT
    v, _ = sp.place({"anchor": "short wedding dresses", "in_text": True, "sentence": "If your date is close, ask about short wedding dresses you can take home on the day."}, text, KW)
    assert v["in_text"] and v["anchor"] == "short wedding dresses" and v["sentence"].startswith("If your date")
    # in_text claimed but anchor absent: downgraded to the drafted sentence
    v, _ = sp.place({"anchor": "short bridal gowns", "in_text": True, "sentence": "Browse our short bridal gowns today."}, text, KW)
    assert v == {"anchor": "short bridal gowns", "in_text": False, "sentence": "Browse our short bridal gowns today."}
    # absent and no usable sentence: dropped
    v, why = sp.place({"anchor": "short bridal gowns", "in_text": True, "sentence": "Nothing relevant."}, text, KW)
    assert v is None and "no valid drafted" in why
    # too long a draft, non-variant anchor, skip
    long = "Browse our short bridal gowns " + "word " * 30
    assert sp.place({"anchor": "short bridal gowns", "in_text": False, "sentence": long}, text, KW)[0] is None
    assert sp.place({"anchor": "lace dresses", "in_text": False, "sentence": "See lace dresses."}, text, KW)[0] is None
    assert sp.place({"skip": True, "why": "no fit"}, text, KW)[0] is None


def _mock(monkeypatch, shortlist, placement, calls):
    async def ask(client, system, user, max_tokens=8000):
        calls.append(system[:20])
        if system == sp.SHORTLIST_SYSTEM:
            calls.append(user)
            return {"pages": shortlist}, 0.01
        for url, d in placement.items():
            if f"Supporting page: {url}" in user:
                return d, 0.002
        return {"skip": True, "why": "x"}, 0.0
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(llm, "ask_json", ask)


def test_run_shortlist_placement_and_exclusions(monkeypatch):
    calls = []
    _mock(monkeypatch, [{"url": FAQ, "reason": "late brides"}, {"url": U("/dresses/some-dress"), "reason": "no"},
                        {"url": BLOG, "reason": "planning"}],
          {FAQ: {"anchor": "short wedding dresses", "in_text": True, "sentence": "ask about short wedding dresses you can take home on the day."},
           BLOG: {"skip": True, "why": "nothing fits"}}, calls)
    res = asyncio.run(sp.run(PAGES, [], STRENGTH, [ROW], set()))
    prompt = next(c for c in calls if "Candidate pages" in c)
    assert FAQ in prompt and "/dresses/" not in prompt and "mother-of-the-bride" not in prompt and "noindex" not in prompt
    assert "short wedding dresses; short bridal gowns" in prompt
    assert [(p["supporting_page"], p["in_text"]) for p in res["proposals"]] == [(FAQ, True)]
    assert res["proposals"][0]["reason"] == "late brides" and res["proposals"][0]["target_id"] == 1
    assert res["cost"] == 0.014 and any("nothing fits" in w for w in res["warnings"])


def test_rerun_keeps_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    prop = lambda u, s="s": {"target_id": 1, "target_page": TARGET, "supporting_page": u, "sentence": s,
                             "in_text": False, "anchor": "a", "reason": "r"}
    with main.closing(main.db()) as con:
        assert sp.save(con, 1, [prop(FAQ), prop(BLOG), prop(U("/faq/c"))]) == {"added": 3, "refreshed": 0}
        ids = {r["supporting_page"]: r["id"] for r in sp.rows(con)}
        assert sp.update(con, ids[FAQ], {"status": "approved"})[1] is None
        sp.update(con, ids[BLOG], {"status": "rejected"})
        out = sp.save(con, 2, [prop(FAQ, "new"), prop(BLOG, "new"), prop(U("/faq/c"), "new")])
        assert out == {"added": 0, "refreshed": 1}
        by = {r["supporting_page"]: r for r in sp.rows(con)}
        assert len(by) == 3 and by[FAQ]["status"] == "approved" and by[FAQ]["sentence"] == "s"
        assert by[BLOG]["status"] == "rejected" and by[U("/faq/c")]["sentence"] == "new"
        assert sp.decided_pairs(con) == {(TARGET, FAQ), (TARGET, BLOG)}
    # decided pairs are not offered to the model again
    calls = []
    _mock(monkeypatch, [], {}, calls)
    asyncio.run(sp.run(PAGES, [], STRENGTH, [ROW], {(TARGET, FAQ), (TARGET, BLOG)}))
    assert not any("Candidate pages" in c for c in calls)  # no candidates left, no call


def test_api_flow_and_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main.gsc, "configured", lambda: False)
    _mock(monkeypatch, [{"url": FAQ, "reason": "late brides"}],
          {FAQ: {"anchor": "short wedding dresses", "in_text": True, "sentence": "x"}}, [])
    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (1,'serenitybrides.com','2026-01-01','done')")
        for p in PAGES:
            con.execute("INSERT INTO pages(crawl_id,url,status,content_type,title,h1,text,meta_robots) VALUES (1,?,?,?,?,?,?,?)",
                        (p["url"], p["status"], p["content_type"], p["title"], p["h1"], p["text"], p.get("meta_robots")))
        con.execute("INSERT INTO keyword_targets(path,primary_keyword,supporting,phase,product_line) "
                    "VALUES ('/collections/bridal/short','short wedding dresses','[\"short bridal gowns\"]',1,'bridal')")
        con.commit()
    with TestClient(main.app) as c:
        assert c.post("/api/crawl/99/supporting").status_code == 404
        rid = c.post("/api/crawl/1/supporting").json()["id"]
        for _ in range(50):
            d = c.get(f"/api/supporting/run/{rid}").json()
            if d["status"] != "running":
                break
            time.sleep(0.1)
        assert d["status"] == "done", d
        assert d["data"]["added"] == 1
        rows = c.get("/api/supporting?crawl=1").json()
        assert len(rows) == 1
        r = rows[0]
        assert {"id", "target_id", "target_page", "supporting_page", "sentence", "anchor", "in_text", "reason", "status"} <= set(r)
        assert r["status"] == "proposed" and r["in_text"] is True and c.get("/api/supporting?crawl=2").json() == []
        assert [x["id"] for x in c.get("/api/crawl/1/supporting-runs").json()] == [rid]
        # approve / reject / bad input
        assert c.post(f"/api/supporting/{r['id']}", json={"status": "approved"}).json()["status"] == "approved"
        assert c.get("/api/supporting?status=approved").json()[0]["decided_at"]
        assert c.post(f"/api/supporting/{r['id']}", json={"status": "rejected"}).json()["status"] == "rejected"
        assert c.post(f"/api/supporting/{r['id']}", json={"status": "maybe"}).status_code == 400
        assert c.post("/api/supporting/999", json={"status": "approved"}).status_code == 404
        # edits, with and without a status change
        sid = r["id"]
        ok = c.post(f"/api/supporting/{sid}", json={"anchor": "short bridal gowns", "in_text": False,
                                                    "sentence": "Try our short bridal gowns."})
        assert ok.status_code == 200 and ok.json()["anchor"] == "short bridal gowns" and ok.json()["status"] == "rejected"
        both = c.post(f"/api/supporting/{sid}", json={"status": "approved", "anchor": "short wedding dresses",
                                                      "in_text": True, "sentence": "We love short wedding dresses."}).json()
        assert both["status"] == "approved" and both["in_text"] is True
        for bad in ({"anchor": ""}, {"anchor": "x" * 81, "sentence": "x" * 81},
                    {"anchor": "lace", "in_text": True}, {"sentence": "No anchor here.", "in_text": True}):
            assert c.post(f"/api/supporting/{sid}", json=bad).status_code == 400, bad
        assert c.get("/api/supporting?crawl=1").json()[0]["anchor"] == "short wedding dresses"
        # a run while another is running -> 409
        with main.closing(main.db()) as con:
            con.execute("INSERT INTO supporting_runs(crawl_id,created_at,status) VALUES (1,'now','running')")
            con.commit()
        assert c.post("/api/crawl/1/supporting").status_code == 409


# ---- planner ----

def _plan_world(n_src=6, volume=14800, extra_rows=()):
    new = {"url": TARGET, "h1": "Short", "line": "bridal", "indexable": True, "top_queries": [], "opportunity": 0, "pagerank": 0}
    srcs = [{"url": U(f"/blog/a{i}"), "h1": "A", "line": "bridal", "indexable": True, "top_queries": [],
             "opportunity": 0, "pagerank": 1} for i in range(n_src)]
    S = {p["url"]: p for p in [new, *srcs]}
    P = {s["url"]: {"text": "x" * 300} for s in srcs}
    topics = {"topics": [], "page_targets": {s["url"]: [TARGET] for s in srcs}}
    row = {"path": "/collections/bridal/short", "primary_keyword": "short wedding dresses", "volume": volume,
           "product_line": "bridal"}
    return S, P, topics, row


def test_planner_approved_first_with_their_anchor(monkeypatch):
    S, P, topics, row = _plan_world()
    appr = {"target_page": TARGET, "supporting_page": U("/blog/a5"), "anchor": "short wedding dresses", "in_text": False,
            "sentence": "Our short wedding dresses go home on the day.", "reason": "late brides"}
    cands = lp.candidates(S, P, defaultdict(set), topics, [], set(), set(), [row], [appr])
    sup = [c for c in cands if c["kind"] == "supporting"]
    assert len(sup) == 1 and sup[0]["src"] == U("/blog/a5") and sup[0]["approved"] is appr
    chosen = lp.select(cands, S)
    assert chosen[0]["kind"] == "supporting"
    assert len([c for c in chosen if c["dst"] == TARGET]) >= lp.STORE_MIN_LINKS  # supporting counts toward the minimum
    # already linked live -> not proposed
    out_ctx = defaultdict(set, {U("/blog/a5"): {TARGET}})
    assert not [c for c in lp.candidates(S, P, out_ctx, topics, [], set(), set(), [row], [appr]) if c["kind"] == "supporting"]
    # anchors_for uses the approved anchor and sentence without asking the model about it
    monkeypatch.setattr(llm, "configured", lambda: True)

    async def ask(*a, **k):
        raise AssertionError("model must not be called")
    monkeypatch.setattr(llm, "ask_json", ask)
    out, _, _ = asyncio.run(lp.anchors_for(None, U("/blog/a5"), {"text": "x" * 300, "title": "t"}, sup, S, asyncio.Semaphore(1)))
    assert out[0]["anchor"] == "short wedding dresses" and out[0]["sentence"] == appr["sentence"] and not out[0]["in_text"]


def test_planner_edited_in_text_anchor_must_be_on_page():
    row = {"anchor": "short wedding dresses", "in_text": True, "sentence": "We sell short wedding dresses."}
    assert lp._approved_anchor(row, "We sell short wedding dresses.")["in_text"] is True
    v = lp._approved_anchor(row, "Nothing here at all.")
    assert v["in_text"] is False and v["sentence"] == row["sentence"]


def test_no_volume_target_gets_candidates_and_min_links():
    S, P, topics, row = _plan_world(volume=None)
    cands = lp.candidates(S, P, defaultdict(set), topics, [], set(), set(), [row])
    mine = [c for c in cands if c["dst"] == TARGET]
    assert mine and "no stored volume" in mine[0]["reason"]
    assert len([c for c in lp.select(cands, S) if c["dst"] == TARGET]) >= lp.STORE_MIN_LINKS == 3
    assert lp.store_opportunity({"volume": None}, "bridal") > 0
    assert lp.DEFAULT_STORE_OPPORTUNITY > 0


def test_template_targets_get_one_link_each():
    assert lp.STORE_MIN_LINKS_TEMPLATE == 1
    des = [U(f"/collections/bridal/d{i}") for i in range(2)]
    pages = [{"url": u, "h1": f"D{i} Wedding Dresses", "line": "bridal", "indexable": True, "top_queries": [],
              "opportunity": 0, "pagerank": 1} for i, u in enumerate(des)]
    srcs = [{"url": U(f"/blog/a{i}"), "h1": "A", "line": "bridal", "indexable": True, "top_queries": [],
             "opportunity": 0, "pagerank": 1} for i in range(8)]
    S = {p["url"]: p for p in [*pages, *srcs]}
    P = {s["url"]: {"text": "x" * 300} for s in srcs}
    topics = {"topics": [], "page_targets": {s["url"]: des for s in srcs}}
    row = {"path": "/collections/bridal/other", "primary_keyword": "{Designer} wedding dresses", "volume": 900,
           "product_line": "bridal"}
    cands = lp.candidates(S, P, defaultdict(set), topics, [], set(des), set(), [row])
    assert {c["dst"] for c in cands if c.get("store")} == set(des)
    assert all(c["store_min"] == 1 for c in cands if c.get("store"))
    chosen = lp.select(cands, S)
    for d in des:
        assert any(c["dst"] == d for c in chosen)


def test_linkmap_no_supporting_flag():
    rows = [{"id": 1, "path": "/collections/bridal/lace", "primary_keyword": "lace wedding dresses", "supporting": [],
             "product_line": "bridal"}]
    LACE = U("/collections/bridal/lace")
    pages = [{"url": LACE, "status": 200}]
    S = {LACE: {"url": LACE, "h1": "x", "line": "bridal"}}
    sup = lambda st: {"id": 1, "target_page": LACE, "supporting_page": U("/faq/a"), "status": st}
    e = linkmap.build(rows, pages, [], S, set(), [], [], True, [sup("proposed")])["entries"][0]
    assert "no supporting pages" in e["flags"] and e["supporting_approved"] == 0 and len(e["supporting"]) == 1
    e = linkmap.build(rows, pages, [], S, set(), [], [], True, [sup("approved")])["entries"][0]
    assert "no supporting pages" not in e["flags"] and e["supporting_approved"] == 1
    e = linkmap.build(rows, pages, [], S, set(), [], [], True, [sup("rejected")])["entries"][0]
    assert "no supporting pages" in e["flags"] and e["supporting"] == []
