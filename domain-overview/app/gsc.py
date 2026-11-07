"""Google Search Console client (service account, REST via httpx) with a SQLite cache."""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, urldefrag, urlsplit

import httpx

KEY_FILE = os.environ.get("GSC_KEY_FILE", "/secrets/gsc-key.json")
SITE_URL = os.environ.get("GSC_SITE_URL", "")
DAYS = int(os.environ.get("GSC_DAYS", "90"))
LAG_DAYS = 3
ROW_LIMIT = 25000
CACHE_HOURS = 24
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"


class GSCError(Exception):
    pass


def configured() -> bool:
    return bool(SITE_URL) and Path(KEY_FILE).is_file()


def site_host() -> str:
    """Bare host for the configured property (sc-domain:x or https://x/)."""
    s = SITE_URL
    if s.startswith("sc-domain:"):
        return s[len("sc-domain:"):].lower()
    return (urlsplit(s).hostname or "").lower()


def normalise(url: str, site_host: str) -> str | None:
    """GSC page URL in the crawl's form: https, crawl host, no fragment or query, no trailing slash (root kept)."""
    try:
        u = urlsplit(urldefrag(url.strip())[0])
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host:
        return None
    if host.removeprefix("www.") == site_host.removeprefix("www."):
        host = site_host
    path = re.sub(r"/{2,}", "/", u.path).rstrip("/") or "/"
    return f"https://{host}{path}"


def ensure_tables(con: sqlite3.Connection):
    con.execute("""CREATE TABLE IF NOT EXISTS gsc_fetches (
        id INTEGER PRIMARY KEY, site TEXT NOT NULL, fetched_at TEXT NOT NULL,
        start_date TEXT NOT NULL, end_date TEXT NOT NULL, rows INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS gsc_rows (
        fetch_id INTEGER NOT NULL, page TEXT NOT NULL, query TEXT NOT NULL,
        clicks REAL, impressions REAL, ctr REAL, position REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS gsc_pages (
        fetch_id INTEGER NOT NULL, page TEXT NOT NULL,
        clicks REAL, impressions REAL, ctr REAL, position REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS gsc_daily (
        page TEXT NOT NULL, date TEXT NOT NULL, clicks REAL, impressions REAL, position REAL,
        PRIMARY KEY(page, date))""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_gsc_rows ON gsc_rows(fetch_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_gsc_pages ON gsc_pages(fetch_id)")


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def _query(client: httpx.Client, token: str, body: dict) -> list[dict]:
    url = API.format(site=quote(SITE_URL, safe=""))
    out, start = [], 0
    while True:
        r = client.post(url, headers={"Authorization": f"Bearer {token}"},
                        json={**body, "rowLimit": ROW_LIMIT, "startRow": start})
        if r.status_code != 200:
            raise GSCError(f"Search Console API {r.status_code}: {r.text[:400]}")
        rows = r.json().get("rows", [])
        out.extend(rows)
        if len(rows) < ROW_LIMIT:
            return out
        start += len(rows)


def _merge(items, host: str, key_len: int):
    """Normalise URLs and merge rows that collapse to the same (page[, query]); position is impression-weighted."""
    acc: dict[tuple, list] = {}
    for r in items:
        page = normalise(r["keys"][0], host)
        if not page:
            continue
        k = (page, *r["keys"][1:key_len])
        a = acc.setdefault(k, [0.0, 0.0, 0.0])
        a[0] += r["clicks"]
        a[1] += r["impressions"]
        a[2] += r["position"] * r["impressions"]
    return [(*k, a[0], a[1], (a[0] / a[1]) if a[1] else 0.0, (a[2] / a[1]) if a[1] else 0.0)
            for k, a in acc.items()]


def latest(con: sqlite3.Connection) -> sqlite3.Row | None:
    ensure_tables(con)
    return con.execute("SELECT * FROM gsc_fetches WHERE site=? ORDER BY id DESC LIMIT 1", (SITE_URL,)).fetchone()


def summary(con: sqlite3.Connection, row, cached: bool) -> dict:
    t = con.execute("SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(impressions),0), COUNT(*) FROM gsc_pages WHERE fetch_id=?",
                    (row["id"],)).fetchone()
    return {"fetch_id": row["id"], "site": row["site"], "fetched_at": row["fetched_at"], "start_date": row["start_date"],
            "end_date": row["end_date"], "rows": row["rows"], "pages": t[2], "clicks": t[0], "impressions": t[1],
            "cached": cached}


def get_fetch(con: sqlite3.Connection, refresh: bool = False) -> dict:
    """Latest fetch if under 24h old (and not refresh), else call the API and store a new one."""
    if not configured():
        raise GSCError("Search Console not configured")
    row = latest(con)
    if row and not refresh:
        age = dt.datetime.utcnow() - dt.datetime.fromisoformat(row["fetched_at"])
        if age < dt.timedelta(hours=CACHE_HOURS):
            return summary(con, row, True)
    host = site_host()
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=DAYS)
    base = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dataState": "final"}
    try:
        token = _token()
        with httpx.Client(timeout=120) as client:
            pq = _query(client, token, {**base, "dimensions": ["page", "query"]})
            pg = _query(client, token, {**base, "dimensions": ["page"]})
    except GSCError:
        raise
    except Exception as e:  # noqa: BLE001 - auth / network problems surface as one error type
        raise GSCError(f"{type(e).__name__}: {e}"[:400])
    rows, pages = _merge(pq, host, 2), _merge(pg, host, 1)
    cur = con.execute("INSERT INTO gsc_fetches(site,fetched_at,start_date,end_date,rows) VALUES (?,?,?,?,?)",
                      (SITE_URL, dt.datetime.utcnow().isoformat(timespec="seconds"), start.isoformat(), end.isoformat(), len(rows)))
    fid = cur.lastrowid
    con.executemany("INSERT INTO gsc_rows VALUES (?,?,?,?,?,?,?)", [(fid, *r) for r in rows])
    con.executemany("INSERT INTO gsc_pages VALUES (?,?,?,?,?,?)", [(fid, *p) for p in pages])
    con.commit()
    return summary(con, latest(con), False)


def load(con: sqlite3.Connection, fetch_id: int, host: str) -> tuple[list[dict], list[dict]]:
    """(page totals, page x query rows) with URLs re-normalised to the crawl's host."""
    def conv(rs):
        out = []
        for r in rs:
            d = dict(r)
            d["page"] = normalise(d["page"], host) or d["page"]
            out.append(d)
        return out
    pages = conv(con.execute("SELECT page,clicks,impressions,ctr,position FROM gsc_pages WHERE fetch_id=?", (fetch_id,)))
    rows = conv(con.execute("SELECT page,query,clicks,impressions,ctr,position FROM gsc_rows WHERE fetch_id=?", (fetch_id,)))
    return pages, rows


DAILY_CHUNK = 25  # pages per request: the page filter is one regex, so keep it short


def _page_regex(pages: list[str], host: str) -> str:
    """RE2 regex matching these crawl-form URLs with or without www and a trailing slash."""
    paths = "|".join(re.escape(urlsplit(p).path.rstrip("/") or "/") for p in pages)
    return rf"^https?://(www\.)?{re.escape(host.removeprefix('www.'))}({paths})/?$"


def fetch_daily(con: sqlite3.Connection, pages: list[str], days: int = 120) -> int:
    """Daily clicks/impressions/position for these pages (dimensions page, date) into gsc_daily. Returns rows stored."""
    if not configured():
        raise GSCError("Search Console not configured")
    ensure_tables(con)
    host = site_host()
    end = dt.date.today() - dt.timedelta(days=LAG_DAYS)
    start = end - dt.timedelta(days=days)
    acc: dict[tuple[str, str], list] = {}
    try:
        token = _token()
        with httpx.Client(timeout=120) as client:
            for i in range(0, len(pages), DAILY_CHUNK):
                rows = _query(client, token, {
                    "startDate": start.isoformat(), "endDate": end.isoformat(), "dataState": "final",
                    "dimensions": ["page", "date"],
                    "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "includingRegex",
                                                            "expression": _page_regex(pages[i:i + DAILY_CHUNK], host)}]}]})
                for r in rows:
                    page = normalise(r["keys"][0], host)
                    if not page:
                        continue
                    a = acc.setdefault((page, r["keys"][1]), [0.0, 0.0, 0.0])
                    a[0] += r["clicks"]
                    a[1] += r["impressions"]
                    a[2] += r["position"] * r["impressions"]
    except GSCError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GSCError(f"{type(e).__name__}: {e}"[:400])
    con.executemany(
        "INSERT INTO gsc_daily(page,date,clicks,impressions,position) VALUES (?,?,?,?,?) "
        "ON CONFLICT(page,date) DO UPDATE SET clicks=excluded.clicks, impressions=excluded.impressions, position=excluded.position",
        [(p, d, a[0], a[1], (a[2] / a[1]) if a[1] else 0.0) for (p, d), a in acc.items()])
    con.commit()
    return len(acc)


def window_stats(rows: list[dict], done: dt.date, data_end: dt.date, days: int = 28) -> dict | None:
    """Compare n days before `done` with n days from `done` on, n = min(days, days of data after done).
    rows: {date (ISO), clicks, impressions, position} for one page. Position is impression-weighted.
    Returns None when there is no data after the done date yet."""
    n = min(days, (data_end - done).days + 1)
    if n < 1:
        return None

    def tot(a: dt.date, b: dt.date):
        sel = [r for r in rows if a <= dt.date.fromisoformat(r["date"]) <= b]
        imp = sum(r["impressions"] for r in sel)
        return {"clicks": sum(r["clicks"] for r in sel), "impressions": imp,
                "position": round(sum(r["position"] * r["impressions"] for r in sel) / imp, 1) if imp else None}

    return {"days": n, "before": tot(done - dt.timedelta(days=n), done - dt.timedelta(days=1)),
            "after": tot(done, done + dt.timedelta(days=n - 1))}
