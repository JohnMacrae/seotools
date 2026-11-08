import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main, targets  # noqa: E402

DOC = next(p for p in (Path("/docs/keyword-targets.md"), Path(__file__).resolve().parents[2] / "docs" / "keyword-targets.md") if p.exists())


def rows():
    return targets.parse_doc(DOC.read_text())


def test_parse_all_eleven_phase1_rows():
    r = rows()
    assert [x["seed_key"] for x in r] == list(range(1, 12))
    assert all(x["phase"] == 1 and x["product_line"] == "bridal" for x in r)
    a = r[0]
    assert (a["path"], a["primary_keyword"], a["volume"]) == ("/", "wedding dress shop Colchester", 1600)
    assert a["supporting"][0] == "wedding shop colchester" and len(a["supporting"]) == 7
    assert a["win_basis"] == "Competitor #1 organic and map pack"
    assert r[1]["volume"] == 135000 and r[1]["supporting"][-1] == "simple bridal dresses"
    assert r[2]["path"] == "/collections/bridal/short-wedding-dresses" and r[2]["is_new"] == 1
    assert r[1]["is_new"] == 0
    assert r[6]["path"].startswith("/blog/")
    assert r[10]["supporting"] == [] and r[10]["volume"] == 6600
    assert r[8]["volume"] is None and r[8]["primary_keyword"] == "{Designer} wedding dresses"


def test_close_variants_count_one_volume():
    r8 = rows()[7]
    assert r8["volume"] == 1300  # "cheap wedding dresses (5,400)" is a variant: not a second figure
    assert r8["supporting"] == ["cheap wedding dresses", "wedding dresses under £500"]


def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    targets.ensure_table(c)
    return c


def test_seed_idempotent_and_keeps_edits():
    c = con()
    assert targets.seed(c, rows()) == {"added": 11, "updated": 0, "kept_edited": 0}
    assert targets.seed(c, rows()) == {"added": 0, "updated": 11, "kept_edited": 0}
    assert len(targets.list_all(c)) == 11
    t = targets.list_all(c)[0]
    targets.update(c, t["id"], {"volume": 1700, "supporting": ["x"]})
    assert targets.seed(c, rows()) == {"added": 0, "updated": 10, "kept_edited": 1}
    t = targets.get(c, t["id"])
    assert t["volume"] == 1700 and t["supporting"] == ["x"] and t["edited"] == 1
    assert len(targets.list_all(c)) == 11


def test_crud():
    c = con()
    t = targets.create(c, {"path": "/x", "primary_keyword": "kw", "supporting": ["a", "b"], "volume": 10})
    assert t["phase"] == 1 and t["supporting"] == ["a", "b"] and t["seed_key"] is None
    t2 = targets.update(c, t["id"], {"win_basis": "Secondary"})
    assert t2["win_basis"] == "Secondary" and t2["path"] == "/x"
    assert targets.update(c, 999, {"path": "/y"}) is None
    assert targets.delete(c, t["id"]) and not targets.delete(c, t["id"])
    assert targets.get(c, t["id"]) is None


def test_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(main, "TARGETS_DOC", DOC)
    with TestClient(main.app) as c:
        assert c.get("/api/targets").json() == []
        assert c.post("/api/targets/seed").json()["added"] == 11
        assert c.post("/api/targets/seed").json()["added"] == 0
        lst = c.get("/api/targets").json()
        assert len(lst) == 11
        tid = lst[0]["id"]
        r = c.put(f"/api/targets/{tid}", json={"volume": 2000, "supporting": ["q"]})
        assert r.json()["volume"] == 2000 and c.get(f"/api/targets/{tid}").json()["supporting"] == ["q"]
        assert c.put("/api/targets/999", json={"volume": 1}).status_code == 404
        n = c.post("/api/targets", json={"path": "/n", "primary_keyword": "k"})
        assert n.status_code == 200
        assert c.post("/api/targets", json={"path": "", "primary_keyword": "k"}).status_code == 400
        assert c.delete(f"/api/targets/{n.json()['id']}").status_code == 200
        assert c.delete(f"/api/targets/{n.json()['id']}").status_code == 404
        assert len(c.get("/api/targets").json()) == 11
        assert c.get("/targets").status_code == 200


def test_volume_map_primary_and_variants():
    t = [dict(primary_keyword="Wedding Dress Shop Colchester", supporting=["wedding shop colchester", "Bridal Shop"], volume=1600),
         dict(primary_keyword="bridal shop", supporting=["x variant"], volume=900),
         dict(primary_keyword="{Designer} dresses", supporting=["no volume kw"], volume=None)]
    m = targets.volume_map(t)
    assert m["wedding dress shop colchester"] == 1600 and m["wedding shop colchester"] == 1600
    assert m["bridal shop"] == 900  # a primary's own figure beats another row's variant
    assert m["x variant"] == 900 and "no volume kw" not in m and "{designer} dresses" not in m
