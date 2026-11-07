"""Domain Overview — a self-hosted, Semrush-style domain overview backed by DataForSEO."""
from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import compare, crawl, dfs, gsc, health, linkgraph

DB_PATH = Path(os.environ.get("DB_PATH", "/data/overview.db"))
CACHE_DAYS = int(os.environ.get("CACHE_DAYS", "7"))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Domain Overview")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_crawl_tasks: set[asyncio.Task] = set()  # keep refs so background crawls aren't garbage-collected


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
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
    con.execute(
        """CREATE TABLE IF NOT EXISTS comparisons (
            id INTEGER PRIMARY KEY,
            domain TEXT NOT NULL,
            competitors TEXT NOT NULL,
            location_code INTEGER NOT NULL,
            language_code TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            cost REAL,
            data TEXT NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS crawls (
            id INTEGER PRIMARY KEY,
            domain TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            pages INTEGER DEFAULT 0,
            error TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS pages (
            crawl_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            status INTEGER,
            redirect_to TEXT,
            content_type TEXT,
            canonical TEXT,
            meta_robots TEXT,
            title TEXT,
            h1 TEXT,
            meta_description TEXT,
            word_count INTEGER,
            depth INTEGER,
            in_sitemap INTEGER,
            external_links INTEGER,
            text TEXT,
            fetched_at TEXT,
            PRIMARY KEY(crawl_id, url))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS links (
            crawl_id INTEGER NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            anchor TEXT,
            placement TEXT,
            nofollow INTEGER)"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_links_dst ON links(crawl_id, dst)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_links_src ON links(crawl_id, src)")
    gsc.ensure_tables(con)
    return con


@app.on_event("startup")
def _mark_interrupted():
    with closing(db()) as con:
        con.execute(
            "UPDATE crawls SET status='error', error='interrupted by restart', finished_at=? WHERE status='running'",
            (dt.datetime.utcnow().isoformat(timespec="seconds"),),
        )
        con.commit()


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
        total = con.execute(
            "SELECT (SELECT COALESCE(SUM(cost),0) FROM snapshots) + (SELECT COALESCE(SUM(cost),0) FROM comparisons)"
        ).fetchone()[0]
    return {"domains": [dict(r) for r in rows], "total_spent": round(total, 4)}


@app.get("/compare")
def compare_page():
    return FileResponse(STATIC / "compare.html")


@app.get("/api/suggest")
def suggest(domain: str, location: int = 2826):
    """Competitors from the latest overview snapshot, best first."""
    d = clean_domain(domain)
    with closing(db()) as con:
        row = con.execute(
            "SELECT data FROM snapshots WHERE domain=? AND location_code=? ORDER BY fetched_at DESC LIMIT 1",
            (d, location),
        ).fetchone()
    if not row:
        return []
    return [c["domain"].removeprefix("www.") for c in json.loads(row["data"]).get("competitors", [])][:10]


@app.get("/api/compare")
async def compare_api(
    domain: str,
    competitors: str = Query(..., description="Comma-separated, up to 3"),
    location: int = 2826,
    language: str = "en",
    refresh: bool = False,
    id: int | None = None,
):
    d = clean_domain(domain)
    comps = []
    for c in competitors.split(","):
        if c.strip():
            cd = clean_domain(c)
            if cd != d and cd not in comps:
                comps.append(cd)
    if not 1 <= len(comps) <= 3:
        raise HTTPException(400, "Give 1 to 3 competitor domains")
    key = ",".join(sorted(comps))
    with closing(db()) as con:
        if id:
            row = con.execute("SELECT * FROM comparisons WHERE id=?", (id,)).fetchone()
            if not row:
                raise HTTPException(404, "Comparison not found")
            return _wrap(row, cached=True)
        if not refresh:
            cutoff = (dt.datetime.utcnow() - dt.timedelta(days=CACHE_DAYS)).isoformat()
            row = con.execute(
                "SELECT * FROM comparisons WHERE domain=? AND competitors=? AND location_code=? AND language_code=? "
                "AND fetched_at>=? ORDER BY fetched_at DESC LIMIT 1",
                (d, key, location, language, cutoff),
            ).fetchone()
            if row:
                return _wrap(row, cached=True)
    data = await compare.run(d, comps, location, language)
    if len(data["errors"]) >= 3 * len(comps) + 3:
        raise HTTPException(502, "; ".join(data["errors"]))
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO comparisons(domain, competitors, location_code, language_code, fetched_at, cost, data) VALUES (?,?,?,?,?,?,?)",
            (d, key, location, language, now, data["cost"], json.dumps(data)),
        )
        con.commit()
        row = con.execute("SELECT * FROM comparisons WHERE id=?", (cur.lastrowid,)).fetchone()
    return _wrap(row, cached=False)


@app.get("/api/comparisons")
def comparisons():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id, domain, competitors, location_code, fetched_at, cost FROM comparisons ORDER BY fetched_at DESC LIMIT 30"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/linking")
def linking_page():
    return FileResponse(STATIC / "linking.html")


@app.post("/api/crawl")
async def start_crawl(domain: str):
    d = clean_domain(domain)
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM crawls WHERE domain=? AND status='running'", (d,)).fetchone():
            raise HTTPException(409, f"A crawl of {d} is already running")
        cur = con.execute(
            "INSERT INTO crawls(domain, started_at, status) VALUES (?,?,'running')",
            (d, dt.datetime.utcnow().isoformat(timespec="seconds")),
        )
        con.commit()
        crawl_id = cur.lastrowid
    task = asyncio.create_task(crawl.run_crawl(crawl_id, d, db))
    _crawl_tasks.add(task)
    task.add_done_callback(_crawl_tasks.discard)
    return {"id": crawl_id}


@app.get("/api/crawl/{crawl_id}")
def get_crawl(crawl_id: int):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Crawl not found")
    return dict(row)


@app.get("/api/crawls")
def list_crawls(domain: str | None = None):
    with closing(db()) as con:
        if domain:
            rows = con.execute("SELECT * FROM crawls WHERE domain=? ORDER BY id DESC LIMIT 30", (clean_domain(domain),)).fetchall()
        else:
            rows = con.execute("SELECT * FROM crawls ORDER BY id DESC LIMIT 30").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/crawl/{crawl_id}/health")
def crawl_health(crawl_id: int):
    with closing(db()) as con:
        crawl_row = con.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
        if not crawl_row:
            raise HTTPException(404, "Crawl not found")
        pages, links = _crawl_data(con, crawl_id)
    return {"crawl": dict(crawl_row), **health.health(pages, links)}


def _crawl_data(con, crawl_id: int):
    pages = [dict(r) for r in con.execute(
        "SELECT url,status,redirect_to,content_type,canonical,meta_robots,title,h1,meta_description,"
        "word_count,depth,in_sitemap,external_links FROM pages WHERE crawl_id=?", (crawl_id,))]
    links = [dict(r) for r in con.execute(
        "SELECT src,dst,anchor,placement,nofollow FROM links WHERE crawl_id=?", (crawl_id,))]
    return pages, links


@app.get("/api/gsc/fetch")
def gsc_fetch(refresh: bool = False):
    with closing(db()) as con:
        try:
            return gsc.get_fetch(con, refresh)
        except gsc.GSCError as e:
            raise HTTPException(503 if not gsc.configured() else 502, str(e))


@app.get("/api/crawl/{crawl_id}/strength")
def crawl_strength(crawl_id: int):
    with closing(db()) as con:
        crawl_row = con.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
        if not crawl_row:
            raise HTTPException(404, "Crawl not found")
        pages, links = _crawl_data(con, crawl_id)
        info: dict = {"configured": gsc.configured(), "fetched_at": None, "start": None, "end": None}
        gp, gr = [], []
        if info["configured"]:
            fetch = gsc.latest(con)
            try:
                s = gsc.summary(con, fetch, True) if fetch else gsc.get_fetch(con)
            except gsc.GSCError as e:
                s = None
                info["error"] = str(e)
            if s:
                info.update(fetched_at=s["fetched_at"], start=s["start_date"], end=s["end_date"])
                host = urlsplit(pages[0]["url"]).hostname if pages else crawl_row["domain"]
                gp, gr = gsc.load(con, s["fetch_id"], host)
    return {"crawl": dict(crawl_row), **linkgraph.strength(pages, links, gp, gr), "gsc": info}


@app.get("/healthz")
def healthz():
    return {"ok": True, "mock": dfs.MOCK, "credentials": bool(dfs.LOGIN and dfs.PASSWORD)}
