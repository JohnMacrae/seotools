"""Clickstream search volume for keyword targets (#33). Paid, on request, cached 30 days per keyword.

Endpoint: POST keywords_data/clickstream_data/bulk_search_volume/live (<=1000 keywords, location_code).
Price: one task of 3 keywords cost $0.0103 in DataForSEO's docs example, so ~$0.01 per task + ~$0.0001 per keyword
(an estimate: the real cost returned by the API is what gets recorded).
"""
from __future__ import annotations

import datetime as dt

SOURCE = "DataForSEO clickstream"
DFS_PATH = "keywords_data/clickstream_data/bulk_search_volume/live"
DFS_LOCATION = 2826  # UK
TTL_DAYS = 30
CHUNK = 1000
TASK_COST = 0.01
KEYWORD_COST = 0.0001


def ensure_tables(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS clickstream_runs (
        id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, keywords INTEGER, cost REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS clickstream_volumes (
        keyword TEXT PRIMARY KEY, volume INTEGER, fetched_at TEXT NOT NULL, run_id INTEGER)""")


def norm_keyword(keyword: str) -> str:
    """The cache key for a keyword: trimmed and lowercased. Every lookup into the cache goes through this."""
    return keyword.strip().lower()


def _norm(keywords) -> list[str]:
    return list(dict.fromkeys(norm_keyword(k) for k in keywords if k and k.strip()))


def _todo(con, keywords, now) -> tuple[list[str], int]:
    wanted = _norm(keywords)
    cutoff = (now - dt.timedelta(days=TTL_DAYS)).isoformat()
    fresh = {r[0] for r in con.execute("SELECT keyword FROM clickstream_volumes WHERE fetched_at >= ?", (cutoff,))}
    todo = [k for k in wanted if k not in fresh]
    return todo, len(wanted) - len(todo)


def estimate(con, keywords, now=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    todo, cached = _todo(con, keywords, now)
    chunks = -(-len(todo) // CHUNK)
    return {"keywords": len(todo), "cached": cached, "cost": chunks * TASK_COST + len(todo) * KEYWORD_COST,
            "source": SOURCE}


def volumes(con) -> dict[str, int]:
    return {r[0]: r[1] for r in con.execute("SELECT keyword, volume FROM clickstream_volumes WHERE volume IS NOT NULL")}


def label(con) -> dict:
    """Source and date for the column. The date is the oldest cached fetch, so it never overstates freshness."""
    oldest = con.execute("SELECT MIN(fetched_at) FROM clickstream_volumes").fetchone()[0]
    return {"source": SOURCE, "fetched_at": oldest}


def load(con) -> tuple[dict[str, int], dict]:
    """Cached volumes and their label, read together for the rankings endpoints."""
    return volumes(con), label(con)


async def fetch(con, keywords, now=None) -> dict:
    """Fetch keywords missing or older than TTL_DAYS. No DFS call and no run row when nothing is needed."""
    from . import dfs
    now = now or dt.datetime.now(dt.timezone.utc)
    todo, cached = _todo(con, keywords, now)
    if not todo:
        return {"fetched": 0, "cached": cached, "cost": 0.0}
    stamp, total, rows = now.isoformat(), 0.0, []
    async with dfs.client() as c:
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i:i + CHUNK]
            items, cost = await dfs.post_path(c, DFS_PATH, {"keywords": chunk, "location_code": DFS_LOCATION},
                                              mock_name="clickstream_volume", all_results=True)
            total += cost
            got = {norm_keyword(it.get("keyword") or ""): it.get("search_volume") for it in (items or [])}
            rows += [(k, got[k]) for k in chunk if k in got]  # unanswered keywords stay uncached
    run_id = con.execute("INSERT INTO clickstream_runs(fetched_at, keywords, cost) VALUES (?,?,?)",
                         (stamp, len(todo), total)).lastrowid
    con.executemany("INSERT OR REPLACE INTO clickstream_volumes(keyword, volume, fetched_at, run_id) VALUES (?,?,?,?)",
                    [(k, v, stamp, run_id) for k, v in rows])
    con.commit()
    return {"fetched": len(todo), "cached": cached, "cost": total}
