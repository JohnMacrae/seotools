"""Journal (#29): per-site table, CRUD API, wildcard page matching, Journal page."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import journal, main  # noqa: E402


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as client:
        yield client


E = {"live_date": "2026-10-01", "page": "/collections/mob", "change_type": "title", "note": "New title"}


def test_create_list_newest_first(c):
    a = c.post("/api/journal", json=E)
    assert a.status_code == 200
    a = a.json()
    assert a["source"] == "manual" and a["approx"] == 0 and a["ref"]
    c.post("/api/journal", json={**E, "live_date": "2026-11-05", "page": "/dresses/*", "approx": True})
    rows = c.get("/api/journal").json()["entries"]
    assert [r["live_date"] for r in rows] == ["2026-11-05", "2026-10-01"]
    assert rows[0]["approx"] == 1 and rows[0]["page"] == "/dresses/*"


def test_create_validates(c):
    assert c.post("/api/journal", json={**E, "live_date": "yesterday"}).status_code == 400
    assert c.post("/api/journal", json={**E, "page": " "}).status_code == 400
    assert c.post("/api/journal", json={**E, "change_type": ""}).status_code == 400
    assert c.post("/api/journal", json={**E, "source": "repo"}).status_code == 400


def test_update_and_delete_manual(c):
    eid = c.post("/api/journal", json=E).json()["id"]
    r = c.put(f"/api/journal/{eid}", json={"note": "Changed", "approx": True})
    assert r.status_code == 200 and r.json()["note"] == "Changed" and r.json()["approx"] == 1
    assert r.json()["page"] == E["page"]
    assert c.put("/api/journal/999", json={"note": "x"}).status_code == 404
    assert c.put(f"/api/journal/{eid}", json={"live_date": "bad"}).status_code == 400
    assert c.delete(f"/api/journal/{eid}").status_code == 200
    assert c.get("/api/journal").json()["entries"] == []
    assert c.delete(f"/api/journal/{eid}").status_code == 404


def test_unique_on_source_ref_page(c):
    assert c.post("/api/journal", json={**E, "ref": "gbp-1"}).status_code == 200
    assert c.post("/api/journal", json={**E, "ref": "gbp-1"}).status_code == 409
    assert c.post("/api/journal", json={**E, "ref": "gbp-1", "page": "/other"}).status_code == 200
    # without a ref, manual entries never collide
    assert c.post("/api/journal", json=E).status_code == 200
    assert c.post("/api/journal", json=E).status_code == 200
    # update into a duplicate is refused too
    eid = c.post("/api/journal", json={**E, "ref": "gbp-2"}).json()["id"]
    assert c.put(f"/api/journal/{eid}", json={"ref": "gbp-1"}).status_code == 409


def test_repo_entries_are_read_only_and_unique(c):
    with main.closing(main.db()) as con:
        assert journal.insert(con, {**E, "source": "repo", "ref": "abc123"}) is not None
        assert journal.insert(con, {**E, "source": "repo", "ref": "abc123"}) is None  # duplicate skipped
        assert journal.insert(con, {**E, "source": "repo", "ref": "abc123", "page": "/x"}) is not None
        con.commit()
    rows = c.get("/api/journal").json()["entries"]
    repo = next(r for r in rows if r["source"] == "repo")
    assert c.put(f"/api/journal/{repo['id']}", json={"note": "x"}).status_code == 403
    assert c.delete(f"/api/journal/{repo['id']}").status_code == 403


def test_journal_is_per_site(c):
    c.post("/api/journal", json=E)
    assert c.post("/api/sites", json={"name": "Other Shop"}).status_code == 200
    assert c.get("/api/journal?site=other-shop").json()["entries"] == []
    assert len(c.get("/api/journal?site=serenity").json()["entries"]) == 1


@pytest.mark.parametrize("pattern,page,ok", [
    ("/collections/mob", "/collections/mob", True),
    ("/collections/mob", "https://serenitybrides.com/collections/mob/", True),
    ("/collections/mob", "/collections/mob2", False),
    ("/dresses/*", "/dresses/lace", True),
    ("/dresses/*", "/dresses/lace/long", True),
    ("/dresses/*", "/dresses", False),
    ("/dresses/*", "/other/dresses/x", False),
    ("*/lace", "/a/b/lace", True),
    ("/a/*/c", "/a/b/c", True),
    ("/a/*/c", "/a/b/d", False),
    ("https://serenitybrides.com/dresses/*", "/dresses/x?utm=1", True),
    ("/Dresses/*", "/dresses/x", True),
    ("/a.b", "/axb", False),  # regex metacharacters are literal
    ("*", "/anything", True),
    ("/", "https://serenitybrides.com", True),
    ("", "/x", False),
])
def test_pattern_matching(pattern, page, ok):
    assert journal.matches(pattern, page) is ok


def test_matching_entries_helper(c):
    c.post("/api/journal", json={**E, "page": "/dresses/*"})
    c.post("/api/journal", json={**E, "page": "/collections/mob"})
    with main.closing(main.db()) as con:
        got = journal.for_page(con, "/dresses/lace")
    assert [g["page"] for g in got] == ["/dresses/*"]


def test_journal_page_served_and_wired(c):
    r = c.get("/journal")
    assert r.status_code == 200 and "api/journal" in r.text
