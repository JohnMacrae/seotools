import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

PLAN = {
    "model": "stub", "cost": 0.01, "llm_used": True, "warnings": [],
    "topics": [], "template_rules": [],
    "suggestions": [
        {"src": "https://x.com/a", "dst": "https://x.com/b", "anchor": "b dress", "anchor_type": "exact",
         "in_text": False, "sentence": "See our b dress.", "context": None, "reason": "boost",
         "kind": "boost", "temporary": False, "line": "bridal", "priority": 1.0},
        {"src": "https://x.com/a", "dst": "https://x.com/c", "anchor": "c", "anchor_type": "branded",
         "in_text": True, "sentence": None, "context": "about c here", "reason": "route",
         "kind": "route", "temporary": True, "line": "bridal", "priority": 0.5},
    ],
    "stats": {"targets": 2, "sources": 1, "by_line": {"bridal": 2}},
}


def test_plan_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    seen = []

    async def build_plan(pages, links, strength, *, progress=None, targets=None):
        seen.append(("text" in pages[0], strength is not None))
        progress("working")
        return PLAN

    stub = types.ModuleType("app.linkplan")
    stub.build_plan = build_plan
    monkeypatch.setitem(sys.modules, "app.linkplan", stub)
    monkeypatch.setattr(main.gsc, "configured", lambda: False)
    import app
    monkeypatch.setattr(app, "linkplan", stub, raising=False)

    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (1,'x.com','2026-01-01','done')")
        con.execute("INSERT INTO pages(crawl_id,url,status,text) VALUES (1,'https://x.com/a',200,'hello')")
        con.commit()

    with TestClient(main.app) as c:
        r = c.post("/api/crawl/1/plan")
        assert r.status_code == 200
        pid = r.json()["id"]
        assert c.post("/api/crawl/99/plan").status_code == 404
        # TestClient runs the app loop in a portal thread; poll until the task finishes
        import time
        for _ in range(50):
            d = c.get(f"/api/plan/{pid}").json()
            if d["status"] != "running":
                break
            time.sleep(0.1)
        assert d["status"] == "done", d
        assert d["data"]["stats"]["targets"] == 2 and d["model"] == "stub"
        assert [p["id"] for p in c.get("/api/crawl/1/plans").json()] == [pid]
        assert c.get("/api/plan/999").status_code == 404
    assert seen == [(True, True)]
    with main.closing(main.db()) as con:
        rows = con.execute("SELECT dst,temporary,status FROM suggestions WHERE plan_id=? ORDER BY dst", (pid,)).fetchall()
    assert [(r["dst"], r["temporary"], r["status"]) for r in rows] == [("https://x.com/b", 0, "open"), ("https://x.com/c", 1, "open")]


def test_plan_error_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")

    async def boom(*a, **k):
        raise RuntimeError("nope")

    stub = types.ModuleType("app.linkplan")
    stub.build_plan = boom
    monkeypatch.setitem(sys.modules, "app.linkplan", stub)
    import app
    monkeypatch.setattr(app, "linkplan", stub, raising=False)
    monkeypatch.setattr(main.gsc, "configured", lambda: False)
    with main.closing(main.db()) as con:
        con.execute("INSERT INTO crawls(id,domain,started_at,status) VALUES (1,'x.com','2026-01-01','done')")
        con.execute("INSERT INTO plans(id,crawl_id,created_at,status) VALUES (1,1,'t','running')")
        con.commit()
    asyncio.run(main._run_plan(1, 1))
    with main.closing(main.db()) as con:
        r = con.execute("SELECT status,error FROM plans WHERE id=1").fetchone()
    assert r["status"] == "error" and "nope" in r["error"]
