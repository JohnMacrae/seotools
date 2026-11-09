"""Spend lights (#21): how fresh is each paid dataset. Read-only; makes no paid calls."""
from __future__ import annotations

import datetime as dt
import sqlite3

STALE_DAYS = 30

# feature -> (table, timestamp columns in preference order, completed-row filter or None)
FEATURES = {
    "overview": ("snapshots", ("fetched_at",), None),
    "compare": ("comparisons", ("fetched_at",), None),
    "gaps": ("gaps", ("finished_at", "created_at"), "status='done'"),
    "plans": ("plans", ("finished_at", "created_at"), "status='done'"),
    "supporting": ("supporting_runs", ("finished_at", "created_at"), "status='done'"),
    "mappack": ("mappack_runs", ("finished_at", "started_at"), "status='done'"),
    "volumes": ("volume_runs", ("fetched_at",), None),
}


def status(ts: str | None, now: dt.datetime) -> str:
    """green <= STALE_DAYS old, red older, never when there is no timestamp. `now` and `ts` are naive UTC."""
    if not ts:
        return "never"
    t = dt.datetime.fromisoformat(ts)
    if t.tzinfo:  # volume_runs stores aware UTC; the other tables store naive UTC
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return "green" if now - t <= dt.timedelta(days=STALE_DAYS) else "red"


def _one(con: sqlite3.Connection, table: str, cols: tuple, where: str | None) -> dict:
    out = {"last_at": None, "last_cost": None, "total_cost": 0.0}
    try:
        ts = cols[0] if len(cols) == 1 else "COALESCE(" + ",".join(cols) + ")"
        w = f" WHERE {where}" if where else ""
        row = con.execute(f"SELECT {ts}, cost FROM {table}{w} ORDER BY {ts} DESC, id DESC LIMIT 1").fetchone()
        if row:
            out.update(last_at=row[0], last_cost=row[1])
        out["total_cost"] = con.execute(f"SELECT COALESCE(SUM(cost),0) FROM {table}").fetchone()[0] or 0.0
    except sqlite3.OperationalError:  # table not created yet (e.g. volume_runs before #20 runs)
        pass
    return out


def features(con: sqlite3.Connection, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.utcnow()
    res = {}
    for name, (table, cols, where) in FEATURES.items():
        f = _one(con, table, cols, where)
        f["status"] = status(f["last_at"], now)
        res[name] = f
    return res
