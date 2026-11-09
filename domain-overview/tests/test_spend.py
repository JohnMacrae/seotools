import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import spend  # noqa: E402

NOW = dt.datetime(2026, 11, 9, 12, 0, 0)


def ago(**kw):
    return (NOW - dt.timedelta(**kw)).isoformat(timespec="seconds")


def test_status_boundaries():
    assert spend.STALE_DAYS == 30
    assert spend.status(ago(days=30), NOW) == "green"
    assert spend.status(ago(days=30, seconds=1), NOW) == "red"
    assert spend.status(ago(days=1), NOW) == "green"
    assert spend.status(None, NOW) == "never"
    # volume_runs stores aware UTC timestamps
    aware = (NOW - dt.timedelta(days=31)).replace(tzinfo=dt.timezone.utc).isoformat()
    assert spend.status(aware, NOW) == "red"


def test_features():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE snapshots(id INTEGER PRIMARY KEY, fetched_at TEXT, cost REAL)")
    con.execute("CREATE TABLE gaps(id INTEGER PRIMARY KEY, created_at TEXT, finished_at TEXT, status TEXT, cost REAL)")
    con.executemany("INSERT INTO snapshots(fetched_at,cost) VALUES (?,?)", [(ago(days=40), 0.5), (ago(days=2), 0.25)])
    con.executemany("INSERT INTO gaps(created_at,finished_at,status,cost) VALUES (?,?,?,?)", [
        (ago(days=50), ago(days=45), "done", 1.0),
        (ago(days=1), None, "running", None),        # not completed: ignored for last_at
        (ago(days=3), ago(days=2), "error", 0.5),    # errored: ignored for last_at, cost still counted
    ])
    f = spend.features(con, NOW)
    assert set(f) == {"overview", "compare", "gaps", "plans", "supporting", "mappack", "volumes"}
    assert f["overview"] == {"last_at": ago(days=2), "last_cost": 0.25, "total_cost": 0.75, "status": "green"}
    assert f["gaps"]["last_at"] == ago(days=45) and f["gaps"]["status"] == "red"
    assert f["gaps"]["last_cost"] == 1.0 and f["gaps"]["total_cost"] == 1.5
    for k in ("compare", "plans", "supporting", "mappack", "volumes"):  # missing tables
        assert f[k] == {"last_at": None, "last_cost": None, "total_cost": 0.0, "status": "never"}
