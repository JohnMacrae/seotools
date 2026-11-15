"""Search Console daily store (#25): backfill, top-up, per-site isolation, first data day, 06:00 job."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import gsc, main  # noqa: E402

TODAY = dt.date(2026, 11, 15)
LAST = TODAY - dt.timedelta(days=gsc.LAG_DAYS)  # 2026-11-12, newest final day
NEW = {"name": "Wedding Shop", "gsc_property": "sc-domain:theweddingshop.uk", "repo_path": "", "dfs_location": 2826}


class FakeGSC:
    """Search Console per property: rows for each day from `first` to LAST (two URL forms that normalise together)."""

    def __init__(self, first: dict[str, dt.date]):
        self.first, self.calls, self.last = first, [], LAST

    def __call__(self, client, token, body):
        prop = gsc.site_url()
        self.calls.append((prop, body))
        assert body["dimensions"] == ["page", "query", "date"]
        host = prop.removeprefix("sc-domain:")
        a = max(dt.date.fromisoformat(body["startDate"]), self.first[prop])
        b = min(dt.date.fromisoformat(body["endDate"]), self.last)
        out = []
        while a <= b:
            d = a.isoformat()
            out += [{"keys": [f"https://www.{host}/dresses/", f"{host} query", d], "clicks": 1, "impressions": 10, "position": 4.0},
                    {"keys": [f"https://{host}/dresses", f"{host} query", d], "clicks": 1, "impressions": 30, "position": 8.0}]
            a += dt.timedelta(days=1)
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    monkeypatch.setattr(gsc, "SITE_URL", "sc-domain:serenitybrides.com")
    monkeypatch.setenv("GSC_SITE_URL", "sc-domain:serenitybrides.com")
    monkeypatch.setattr(gsc, "KEY_FILE", __file__)
    monkeypatch.setattr(gsc, "_token", lambda: "t")
    monkeypatch.setattr(gsc, "_today", lambda: TODAY)
    fake = FakeGSC({"sc-domain:serenitybrides.com": dt.date(2026, 9, 13),
                    "sc-domain:theweddingshop.uk": dt.date(2026, 10, 1)})
    monkeypatch.setattr(gsc, "_query", fake)
    with TestClient(main.app) as c:
        c.post("/api/sites", json=NEW)
        yield c, fake


def test_first_sync_backfills_full_history_and_stores_every_query(env):
    c, fake = env
    assert c.get("/api/gsc/store?site=serenity").json() == {"first_day": None, "last_day": None, "rows": 0}
    r = c.post("/api/gsc/store/sync?site=serenity")
    assert r.status_code == 200
    asked = min(dt.date.fromisoformat(b["startDate"]) for _, b in fake.calls)
    assert asked <= LAST - dt.timedelta(days=480)  # reaches back the full ~16 months the API holds
    assert max(b["endDate"] for _, b in fake.calls) == LAST.isoformat()
    days = (LAST - dt.date(2026, 9, 13)).days + 1
    s = c.get("/api/gsc/store?site=serenity").json()
    assert s == {"first_day": "2026-09-13", "last_day": LAST.isoformat(), "rows": days}  # URL forms merged per day
    assert r.json()["added"] == days


def test_rows_are_merged_impression_weighted(env):
    c, _ = env
    c.post("/api/gsc/store/sync?site=serenity")
    with main.closing(main.db()) as con:
        row = con.execute("SELECT date,query,page,clicks,impressions,position FROM gsc_query_daily "
                          "WHERE date='2026-09-13'").fetchone()
    assert tuple(row) == ("2026-09-13", "serenitybrides.com query", "https://serenitybrides.com/dresses", 2.0, 40.0, 7.0)


def test_top_up_fetches_only_from_last_stored_date(env, monkeypatch):
    c, fake = env
    c.post("/api/gsc/store/sync?site=serenity")
    fake.calls.clear()
    assert c.post("/api/gsc/store/sync?site=serenity").json()["added"] == 0  # nothing new yet
    assert fake.calls == []
    later = TODAY + dt.timedelta(days=2)
    monkeypatch.setattr(gsc, "_today", lambda: later)
    fake.last = new_last = later - dt.timedelta(days=gsc.LAG_DAYS)
    r = c.post("/api/gsc/store/sync?site=serenity").json()
    assert [(b["startDate"], b["endDate"]) for _, b in fake.calls] == [
        ((LAST + dt.timedelta(days=1)).isoformat(), new_last.isoformat())]
    assert r["added"] == 2
    assert c.get("/api/gsc/store?site=serenity").json()["last_day"] == new_last.isoformat()


def test_sites_are_isolated(env):
    c, fake = env
    c.post("/api/gsc/store/sync?site=wedding-shop")
    assert {p for p, _ in fake.calls} == {"sc-domain:theweddingshop.uk"}
    assert c.get("/api/gsc/store?site=serenity").json()["rows"] == 0
    tws = c.get("/api/gsc/store?site=wedding-shop").json()
    assert tws["first_day"] == "2026-10-01"
    with main.closing(main.db()) as con:  # default site = serenity
        assert con.execute("SELECT COUNT(*) FROM gsc_query_daily").fetchone()[0] == 0


def test_sync_without_search_console_is_503(env, monkeypatch):
    c, _ = env
    monkeypatch.setattr(gsc, "KEY_FILE", "/nonexistent")
    assert c.post("/api/gsc/store/sync?site=serenity").status_code == 503


def test_daily_job_tops_up_every_site_and_survives_one_failing(env, monkeypatch):
    c, fake = env
    real = fake.__call__

    def flaky(client, token, body):
        if gsc.site_url() == "sc-domain:serenitybrides.com":
            raise gsc.GSCError("boom")
        return real(client, token, body)

    monkeypatch.setattr(gsc, "_query", flaky)
    result = main.run_daily_job()
    assert result["serenity"].startswith("error") and result["wedding-shop"] == "ok"
    assert c.get("/api/gsc/store?site=wedding-shop").json()["rows"] > 0
    monkeypatch.setattr(gsc, "_query", fake)
    assert main.run_daily_job() == {"serenity": "ok", "wedding-shop": "ok"}
    assert c.get("/api/gsc/store?site=serenity").json()["first_day"] == "2026-09-13"


@pytest.mark.parametrize("now,expected", [
    ("2026-11-15T05:59:00+00:00", "2026-11-15T06:00:00+00:00"),  # winter: London = UTC
    ("2026-11-15T06:00:00+00:00", "2026-11-16T06:00:00+00:00"),
    ("2026-07-01T04:30:00+00:00", "2026-07-01T05:00:00+00:00"),  # summer: 06:00 BST = 05:00 UTC
    ("2026-03-28T07:00:00+00:00", "2026-03-29T05:00:00+00:00"),  # across the clocks-forward night
])
def test_next_run_is_six_am_london(now, expected):
    got = main.next_daily_run(dt.datetime.fromisoformat(now))
    assert got.astimezone(dt.timezone.utc) == dt.datetime.fromisoformat(expected)
