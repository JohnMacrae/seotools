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

from . import compare, crawl, dfs, gsc, health, linkgraph, pagegap, progress, targets

DB_PATH = Path(os.environ.get("DB_PATH", "/data/overview.db"))
TARGETS_DOC = Path(os.environ.get("TARGETS_DOC", "/docs/keyword-targets.md"))
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
    con.execute(
        """CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY,
            crawl_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            progress TEXT,
            error TEXT,
            model TEXT,
            cost REAL,
            data TEXT)"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS suggestions (
            plan_id INTEGER NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            anchor TEXT,
            kind TEXT,
            reason TEXT,
            temporary INTEGER,
            status TEXT DEFAULT 'open',
            checked_crawl_id INTEGER,
            PRIMARY KEY(plan_id, src, dst))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gaps (
            id INTEGER PRIMARY KEY,
            crawl_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            progress TEXT,
            error TEXT,
            cost REAL,
            data TEXT)"""
    )
    have = {r[1] for r in con.execute("PRAGMA table_info(suggestions)")}
    for col, typ in (("done_at", "TEXT"), ("done_crawl_id", "INTEGER"), ("live_anchors", "TEXT"), ("anchor_data", "INTEGER")):
        if col not in have:
            con.execute(f"ALTER TABLE suggestions ADD COLUMN {col} {typ}")
    gsc.ensure_tables(con)
    targets.ensure_table(con)
    return con


@app.on_event("startup")
def _mark_interrupted():
    with closing(db()) as con:
        for table in ("crawls", "plans", "gaps"):
            con.execute(
                f"UPDATE {table} SET status='error', error='interrupted by restart', finished_at=? WHERE status='running'",
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


def _crawl_data(con, crawl_id: int, with_text: bool = False):
    cols = ("url,status,redirect_to,content_type,canonical,meta_robots,title,h1,meta_description,"
            "word_count,depth,in_sitemap,external_links" + (",text" if with_text else ""))
    pages = [dict(r) for r in con.execute(f"SELECT {cols} FROM pages WHERE crawl_id=?", (crawl_id,))]
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


def _strength(con, crawl_id: int, with_text: bool = False):
    """Returns (crawl_row, pages, links, strength_dict, gsc_info)."""
    crawl_row = con.execute("SELECT * FROM crawls WHERE id=?", (crawl_id,)).fetchone()
    if not crawl_row:
        raise HTTPException(404, "Crawl not found")
    pages, links = _crawl_data(con, crawl_id, with_text)
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
    return crawl_row, pages, links, linkgraph.strength(pages, links, gp, gr), info


@app.get("/api/crawl/{crawl_id}/strength")
def crawl_strength(crawl_id: int):
    with closing(db()) as con:
        crawl_row, _pages, _links, strength, info = _strength(con, crawl_id)
    return {"crawl": dict(crawl_row), **strength, "gsc": info}


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


async def _run_plan(plan_id: int, crawl_id: int):
    def set_progress(msg: str):
        with closing(db()) as con:
            con.execute("UPDATE plans SET progress=? WHERE id=?", (str(msg), plan_id))
            con.commit()

    try:
        from . import linkplan  # lazy: module is developed separately

        with closing(db()) as con:
            _row, pages, links, strength, _info = _strength(con, crawl_id, with_text=True)
            tg = targets.list_all(con)
        plan = await linkplan.build_plan(pages, links, strength, progress=set_progress, targets=tg)
        with closing(db()) as con:
            con.execute(
                "UPDATE plans SET status='done', finished_at=?, progress=NULL, model=?, cost=?, data=? WHERE id=?",
                (_now(), plan.get("model"), plan.get("cost"), json.dumps(plan), plan_id),
            )
            con.executemany(
                "INSERT OR REPLACE INTO suggestions(plan_id,src,dst,anchor,kind,reason,temporary) VALUES (?,?,?,?,?,?,?)",
                [(plan_id, s["src"], s["dst"], s.get("anchor"), s.get("kind"), s.get("reason"), int(bool(s.get("temporary"))))
                 for s in plan.get("suggestions", [])],
            )
            con.commit()
    except Exception as e:  # noqa: BLE001
        with closing(db()) as con:
            con.execute("UPDATE plans SET status='error', error=?, finished_at=? WHERE id=?",
                        (f"{type(e).__name__}: {e}"[:500], _now(), plan_id))
            con.commit()


@app.post("/api/crawl/{crawl_id}/plan")
async def start_plan(crawl_id: int):
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM crawls WHERE id=?", (crawl_id,)).fetchone():
            raise HTTPException(404, "Crawl not found")
        if con.execute("SELECT 1 FROM plans WHERE crawl_id=? AND status='running'", (crawl_id,)).fetchone():
            raise HTTPException(409, "A link plan for this crawl is already running")
        cur = con.execute("INSERT INTO plans(crawl_id, created_at, status, progress) VALUES (?,?,'running','Starting')",
                          (crawl_id, _now()))
        con.commit()
        plan_id = cur.lastrowid
    task = asyncio.create_task(_run_plan(plan_id, crawl_id))
    _crawl_tasks.add(task)
    task.add_done_callback(_crawl_tasks.discard)
    return {"id": plan_id}


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: int):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Plan not found")
    d = dict(row)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    return d


@app.get("/api/crawl/{crawl_id}/plans")
def list_plans(crawl_id: int):
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id,crawl_id,created_at,finished_at,status,progress,error,model,cost FROM plans "
            "WHERE crawl_id=? ORDER BY id DESC LIMIT 30", (crawl_id,)).fetchall()
    return [dict(r) for r in rows]


def _gsc_rows(con, crawl_row, pages) -> list[dict]:
    """Page x query rows from the latest Search Console fetch (call after _strength has refreshed it)."""
    if not gsc.configured():
        return []
    fetch = gsc.latest(con)
    if not fetch:
        return []
    host = urlsplit(pages[0]["url"]).hostname if pages else crawl_row["domain"]
    return gsc.load(con, fetch["id"], host)[1]


async def _run_gaps(gap_id: int, crawl_id: int):
    def set_progress(msg: str):
        with closing(db()) as con:
            con.execute("UPDATE gaps SET progress=? WHERE id=?", (str(msg), gap_id))
            con.commit()

    try:
        with closing(db()) as con:
            crawl_row, pages, _links, strength, _info = _strength(con, crawl_id)
            rows = _gsc_rows(con, crawl_row, pages)
            comp = con.execute("SELECT data FROM comparisons WHERE domain=? ORDER BY fetched_at DESC LIMIT 1",
                               (crawl_row["domain"],)).fetchone()
        data = await pagegap.build_gaps(pages, strength, rows, json.loads(comp["data"]) if comp else None,
                                        progress=set_progress)
        with closing(db()) as con:
            con.execute("UPDATE gaps SET status='done', finished_at=?, progress=NULL, cost=?, data=? WHERE id=?",
                        (_now(), data.get("cost"), json.dumps(data), gap_id))
            con.commit()
    except Exception as e:  # noqa: BLE001
        with closing(db()) as con:
            con.execute("UPDATE gaps SET status='error', error=?, finished_at=? WHERE id=?",
                        (f"{type(e).__name__}: {e}"[:500], _now(), gap_id))
            con.commit()


@app.post("/api/crawl/{crawl_id}/gaps")
async def start_gaps(crawl_id: int):
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM crawls WHERE id=?", (crawl_id,)).fetchone():
            raise HTTPException(404, "Crawl not found")
        if con.execute("SELECT 1 FROM gaps WHERE crawl_id=? AND status='running'", (crawl_id,)).fetchone():
            raise HTTPException(409, "A page-gap run for this crawl is already running")
        cur = con.execute("INSERT INTO gaps(crawl_id, created_at, status, progress) VALUES (?,?,'running','Starting')",
                          (crawl_id, _now()))
        con.commit()
        gap_id = cur.lastrowid
    task = asyncio.create_task(_run_gaps(gap_id, crawl_id))
    _crawl_tasks.add(task)
    task.add_done_callback(_crawl_tasks.discard)
    return {"id": gap_id}


@app.get("/api/gaps/{gap_id}")
def get_gaps(gap_id: int):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Gap run not found")
    d = dict(row)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    return d


@app.get("/api/crawl/{crawl_id}/gaps")
def list_gaps(crawl_id: int):
    with closing(db()) as con:
        rows = con.execute("SELECT id,crawl_id,created_at,finished_at,status,progress,error,cost FROM gaps "
                           "WHERE crawl_id=? ORDER BY id DESC LIMIT 30", (crawl_id,)).fetchall()
    return [dict(r) for r in rows]


def _gsc_change(con, rows: list[dict]) -> str | None:
    """Fills r['gsc'] for done rows from stored daily data. Returns a note when there is nothing to show."""
    done = [r for r in rows if r["status"] == "done" and r["done_at"]]
    if not done:
        return None
    end = con.execute("SELECT MAX(date) FROM gsc_daily").fetchone()[0]
    if not end:
        return "No daily Search Console data stored for the targets yet."
    end = dt.date.fromisoformat(end)
    for r in done:
        host = urlsplit(r["dst"]).hostname
        page = gsc.normalise(r["dst"], gsc.site_host() or host) or r["dst"]
        daily = [dict(x) for x in con.execute("SELECT date,clicks,impressions,position FROM gsc_daily WHERE page=?", (page,))]
        r["gsc"] = gsc.window_stats(daily, dt.date.fromisoformat(r["done_at"][:10]), end)
    return None


def _progress(con, plan_id: int, note: str | None = None) -> dict:
    plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not plan:
        raise HTTPException(404, "Plan not found")
    rows = [dict(r) for r in con.execute(
        "SELECT src,dst,anchor,kind,status,checked_crawl_id,done_at,done_crawl_id,live_anchors,anchor_data FROM suggestions WHERE plan_id=? "
        "ORDER BY dst,src", (plan_id,))]
    for r in rows:
        r["gsc"] = None
        r["planned_anchor"] = r["anchor"]
        r["live_anchors"] = json.loads(r["live_anchors"]) if r["live_anchors"] else []
        r["anchor_data"] = None if r["anchor_data"] is None else bool(r["anchor_data"])
    gsc_note = _gsc_change(con, rows)
    counts = {k: sum(r["status"] == k for r in rows) for k in ("done", "wrong anchor", "partial", "missing", "gone", "open")}
    checked = max((r["checked_crawl_id"] for r in rows if r["checked_crawl_id"]), default=None)
    built = []
    if checked:
        crawl_row = con.execute("SELECT domain FROM crawls WHERE id=?", (checked,)).fetchone()
        g = con.execute("SELECT g.data FROM gaps g JOIN crawls c ON c.id=g.crawl_id WHERE c.domain=? AND g.status='done' "
                        "ORDER BY g.id DESC LIMIT 1", (crawl_row["domain"],)).fetchone() if crawl_row else None
        if g:
            pages = [dict(p) for p in con.execute("SELECT url,status FROM pages WHERE crawl_id=?", (checked,))]
            built = progress.built_pages(json.loads(g["data"]).get("pages", []), pages)
    n = len(rows)
    return {"plan_id": plan_id, "crawl_id": plan["crawl_id"], "checked_crawl_id": checked, "counts": counts, "total": n,
            "pct_done": round(100 * counts["done"] / n, 1) if n else 0, "suggestions": rows, "built": built,
            "notes": [x for x in (note, gsc_note) if x]}


@app.post("/api/plan/{plan_id}/check")
def check_plan(plan_id: int, crawl: int | None = None):
    """Re-check a plan's suggestions against a crawl (default: latest done crawl of the plan's domain)."""
    with closing(db()) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan not found")
        dom = con.execute("SELECT domain FROM crawls WHERE id=?", (plan["crawl_id"],)).fetchone()["domain"]
        if crawl is None:
            row = con.execute("SELECT id FROM crawls WHERE domain=? AND status='done' ORDER BY id DESC LIMIT 1", (dom,)).fetchone()
            if not row:
                raise HTTPException(404, "No finished crawl for this domain")
            crawl = row["id"]
        crawl_row = con.execute("SELECT * FROM crawls WHERE id=?", (crawl,)).fetchone()
        if not crawl_row:
            raise HTTPException(404, "Crawl not found")
        pages, links = _crawl_data(con, crawl)
        sugg = [dict(r) for r in con.execute("SELECT * FROM suggestions WHERE plan_id=?", (plan_id,))]
        result = progress.check_links_detail(sugg, pages, links)
        when = (crawl_row["finished_at"] or crawl_row["started_at"])
        for s in sugg:
            res = result[(s["src"], s["dst"])]
            st = res["status"]
            con.execute("UPDATE suggestions SET status=?, checked_crawl_id=?, done_at=COALESCE(done_at, ?), "
                        "done_crawl_id=COALESCE(done_crawl_id, ?), live_anchors=?, anchor_data=? "
                        "WHERE plan_id=? AND src=? AND dst=?",
                        (st, crawl, when if st == "done" else None, crawl if st == "done" else None,
                         json.dumps(res["live"]), int(res["anchor_data"]), plan_id, s["src"], s["dst"]))
            s["status"] = st
        con.commit()
        note = None
        if not any(r["anchor_data"] for r in result.values()):
            note = "This crawl has no anchor text stored (older crawl); anchors were not checked. Re-crawl to check them."
        targets = sorted({s["dst"] for s in sugg if s["status"] == "done"})
        if targets:
            if gsc.configured():
                try:
                    gsc.fetch_daily(con, [gsc.normalise(t, gsc.site_host() or urlsplit(t).hostname) or t for t in targets])
                except gsc.GSCError as e:
                    note = f"Search Console daily data not updated: {e}"
            else:
                note = "Search Console is not configured; clicks and impressions before/after are skipped."
        return _progress(con, plan_id, note)


@app.get("/api/plan/{plan_id}/progress")
def plan_progress(plan_id: int):
    with closing(db()) as con:
        return _progress(con, plan_id)


@app.get("/targets")
def targets_page():
    return FileResponse(STATIC / "targets.html")


def _target_body(body: dict, partial: bool) -> dict:
    d = {k: body[k] for k in targets.FIELDS if k in body}
    if not partial and not (str(d.get("path", "")).strip() and str(d.get("primary_keyword", "")).strip()):
        raise HTTPException(400, "path and primary_keyword are required")
    if "supporting" in d and not (isinstance(d["supporting"], list) and all(isinstance(x, str) for x in d["supporting"])):
        raise HTTPException(400, "supporting must be a list of strings")
    return d


@app.get("/api/targets")
def targets_list():
    with closing(db()) as con:
        return targets.list_all(con)


@app.post("/api/targets/seed")
def targets_seed():
    if not TARGETS_DOC.exists():
        raise HTTPException(404, f"{TARGETS_DOC} not found")
    with closing(db()) as con:
        return targets.seed(con, targets.parse_doc(TARGETS_DOC.read_text()))


@app.get("/api/targets/{tid}")
def targets_get(tid: int):
    with closing(db()) as con:
        t = targets.get(con, tid)
    if t is None:
        raise HTTPException(404, "No such target")
    return t


@app.post("/api/targets")
def targets_create(body: dict):
    with closing(db()) as con:
        return targets.create(con, _target_body(body, False))


@app.put("/api/targets/{tid}")
def targets_update(tid: int, body: dict):
    with closing(db()) as con:
        t = targets.update(con, tid, _target_body(body, True))
    if t is None:
        raise HTTPException(404, "No such target")
    return t


@app.delete("/api/targets/{tid}")
def targets_delete(tid: int):
    with closing(db()) as con:
        if not targets.delete(con, tid):
            raise HTTPException(404, "No such target")
    return {"deleted": tid}


@app.get("/healthz")
def healthz():
    return {"ok": True, "mock": dfs.MOCK, "credentials": bool(dfs.LOGIN and dfs.PASSWORD)}
