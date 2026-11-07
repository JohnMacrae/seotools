"""Domain Overview — a self-hosted, Semrush-style domain overview backed by DataForSEO."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import dfs

DB_PATH = Path(os.environ.get("DB_PATH", "/data/overview.db"))
CACHE_DAYS = int(os.environ.get("CACHE_DAYS", "7"))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Domain Overview")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            domain TEXT NOT NULL,
            location_code INTEGER NOT NULL,
            language_code TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            cost REAL,
            data TEXT NOT NULL)"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(domain, location_code, fetched_at)")
    return con


def clean_domain(raw: str) -> str:
    d = raw.strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0]
    d = d.removeprefix("www.")
    try:
        d = d.encode("idna").decode("ascii")  # unicode domains -> punycode
    except UnicodeError:
        raise HTTPException(400, f"Not a valid domain: {raw!r}")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.(?:[a-z]{2,}|xn--[a-z0-9-]+)", d):
        raise HTTPException(400, f"Not a valid domain: {raw!r}")
    return d


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
async def overview(
    domain: str,
    location: int = Query(2826, description="DataForSEO location code (2826 = UK)"),
    language: str = "en",
    refresh: bool = False,
    snapshot: int | None = None,
):
    d = clean_domain(domain)
    with closing(db()) as con:
        if snapshot:
            row = con.execute("SELECT * FROM snapshots WHERE id=? AND domain=?", (snapshot, d)).fetchone()
            if not row:
                raise HTTPException(404, "Snapshot not found")
            return _wrap(row, cached=True)
        if not refresh:
            cutoff = (dt.datetime.utcnow() - dt.timedelta(days=CACHE_DAYS)).isoformat()
            row = con.execute(
                "SELECT * FROM snapshots WHERE domain=? AND location_code=? AND language_code=? "
                "AND fetched_at>=? ORDER BY fetched_at DESC LIMIT 1",
                (d, location, language, cutoff),
            ).fetchone()
            if row:
                return _wrap(row, cached=True)

    try:
        data = await dfs.fetch_overview(d, location, language)
    except dfs.DFSError as e:
        raise HTTPException(502, str(e))

    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO snapshots(domain, location_code, language_code, fetched_at, cost, data) VALUES (?,?,?,?,?,?)",
            (d, location, language, now, data["cost"], json.dumps(data)),
        )
        con.commit()
        row = con.execute("SELECT * FROM snapshots WHERE id=?", (cur.lastrowid,)).fetchone()
    return _wrap(row, cached=False)


def _wrap(row: sqlite3.Row, cached: bool) -> dict:
    data = json.loads(row["data"])
    data.update(snapshot_id=row["id"], fetched_at=row["fetched_at"], cached=cached)
    return data


@app.get("/api/history")
def history(domain: str, location: int = 2826):
    d = clean_domain(domain)
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id, fetched_at, cost, json_extract(data,'$.headline') AS h FROM snapshots "
            "WHERE domain=? AND location_code=? ORDER BY fetched_at DESC",
            (d, location),
        ).fetchall()
    return [{"id": r["id"], "fetched_at": r["fetched_at"], "cost": r["cost"], "headline": json.loads(r["h"])} for r in rows]


@app.get("/api/recent")
def recent():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT domain, MAX(fetched_at) AS last, COUNT(*) AS n, SUM(cost) AS spent "
            "FROM snapshots GROUP BY domain ORDER BY last DESC LIMIT 30"
        ).fetchall()
        total = con.execute("SELECT COALESCE(SUM(cost),0) FROM snapshots").fetchone()[0]
    return {"domains": [dict(r) for r in rows], "total_spent": round(total, 4)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "mock": dfs.MOCK, "credentials": bool(dfs.LOGIN and dfs.PASSWORD)}
