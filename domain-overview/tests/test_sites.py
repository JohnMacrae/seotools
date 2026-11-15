import asyncio
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dfs, gsc, mappack, main, targets  # noqa: E402

NEW = {"name": "Wedding Shop", "gsc_property": "sc-domain:theweddingshop.uk",
       "repo_path": "/repos/tws", "dfs_location": 2826}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as c:
        yield c


def test_registry_lists_serenity_and_adds_a_site(client):
    r = client.get("/api/sites").json()
    assert [s["slug"] for s in r["sites"]] == ["serenity"] and r["active"] == "serenity"
    made = client.post("/api/sites", json=NEW)
    assert made.status_code == 200 and made.json()["slug"] == "wedding-shop"
    s = {x["slug"]: x for x in client.get("/api/sites").json()["sites"]}["wedding-shop"]
    assert (s["name"], s["gsc_property"], s["repo_path"], s["dfs_location"]) == (
        "Wedding Shop", "sc-domain:theweddingshop.uk", "/repos/tws", 2826)
    assert client.post("/api/sites", json=NEW).status_code == 409
    assert client.post("/api/sites", json={**NEW, "name": ""}).status_code == 400


def test_active_site_from_query_then_cookie_and_unknown_rejected(client):
    client.post("/api/sites", json=NEW)
    assert client.get("/api/sites?site=wedding-shop").json()["active"] == "wedding-shop"
    assert client.cookies.get("site") == "wedding-shop"
    assert client.get("/api/sites").json()["active"] == "wedding-shop"  # cookie alone
    assert client.get("/api/sites?site=serenity").json()["active"] == "serenity"  # query wins
    assert client.get("/api/sites?site=nope").status_code == 404


def test_data_is_isolated_between_sites(client):
    client.post("/api/sites", json=NEW)
    assert client.post("/api/targets?site=serenity", json={"path": "/a", "primary_keyword": "only on serenity"}).status_code == 200
    assert [t["primary_keyword"] for t in client.get("/api/targets?site=serenity").json()] == ["only on serenity"]
    assert client.get("/api/targets?site=wedding-shop").json() == []
    client.post("/api/targets?site=wedding-shop", json={"path": "/b", "primary_keyword": "only on tws"})
    assert [t["primary_keyword"] for t in client.get("/api/targets?site=serenity").json()] == ["only on serenity"]
    assert client.get("/api/targets?site=wedding-shop").json()[0]["primary_keyword"] == "only on tws"


def test_first_start_carries_existing_database_over_unchanged(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    legacy = data / "overview.db"
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "scratch" / "x.db")  # build a pre-sites database
    con = main.db()
    targets.create(con, {"path": "/old", "primary_keyword": "legacy kw"})
    con.close()
    shutil.move(str(tmp_path / "scratch" / "sites" / "serenity.db"), legacy)
    shutil.rmtree(tmp_path / "scratch")
    monkeypatch.setattr(main, "DB_PATH", legacy)
    with TestClient(main.app) as c:  # app start migrates
        assert (data / "sites" / "serenity.db").is_file()
        assert [t["primary_keyword"] for t in c.get("/api/targets").json()] == ["legacy kw"]
        assert [t["primary_keyword"] for t in c.get("/api/targets?site=serenity").json()] == ["legacy kw"]
    assert sqlite3.connect(legacy).execute("SELECT COUNT(*) FROM keyword_targets").fetchone()[0] == 1  # source untouched
    with TestClient(main.app) as c:
        c.post("/api/targets", json={"path": "/new", "primary_keyword": "added"})
    with TestClient(main.app) as c:  # a second start does not overwrite the site file
        assert len(c.get("/api/targets").json()) == 2


def test_background_task_keeps_the_site_it_started_on(client, monkeypatch):
    client.post("/api/sites", json=NEW)
    monkeypatch.setattr(dfs, "MOCK", True)
    client.post("/api/targets?site=serenity", json={"path": "/", "primary_keyword": "wedding dress shop Colchester"})
    real = mappack.execute

    async def slow(db, *a, **k):
        await asyncio.sleep(0.4)
        return await real(db, *a, **k)

    monkeypatch.setattr(mappack, "execute", slow)
    assert client.post("/api/mappack/run?site=serenity").status_code == 200
    client.get("/api/sites?site=wedding-shop")  # user switches site while it runs
    runs = []
    for _ in range(60):
        runs = client.get("/api/mappack/runs?site=serenity").json()
        if runs and runs[0]["status"] != "running":
            break
        time.sleep(0.1)
    assert runs[0]["status"] == "done"
    assert client.get("/api/mappack/runs?site=wedding-shop").json() == []


def test_search_console_property_comes_from_active_site(client, monkeypatch):
    client.post("/api/sites", json=NEW)
    seen = []
    monkeypatch.setattr(gsc, "KEY_FILE", __file__)  # any existing file counts as configured

    def fake(con, refresh=False):
        seen.append(gsc.site_url())
        return {}

    monkeypatch.setattr(gsc, "get_fetch", fake)
    client.get("/api/gsc/fetch?site=wedding-shop")
    assert seen == ["sc-domain:theweddingshop.uk"]


@pytest.mark.parametrize("route", ["/", "/compare", "/linking", "/targets", "/mappack", "/digest"])
def test_every_page_loads_the_site_menu(client, route):
    assert "sitemenu.js" in client.get(route).text
    js = client.get("/sitemenu.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


def test_serenity_repo_path_seeded_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENITY_REPO_PATH", "/repos/sweb-live")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as c:
        [s] = c.get("/api/sites").json()["sites"]
    assert s["slug"] == "serenity" and s["repo_path"] == "/repos/sweb-live"
