"""Monthly Map pack check (issue #7): Google SERP live advanced from Colchester for every stored keyword target.

DataForSEO organic items carry a Google redirect in `domain`/`url` (google.com/goto?url=...); the real site is in
`website_name` (when it is a domain) or the `breadcrumb` ("https://host › path › seg..."). parse_organic() resolves it.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

from . import dfs

PATH = "serp/google/organic/live/advanced"
LOCATION = {"location_name": "Colchester,England,United Kingdom"}  # same location as the one-off run
OURS = {"domain": "serenitybrides.com", "brand": "serenity brides"}
THEIRS = {"domain": "theweddingshop.uk", "brand": "the wedding shop"}
DEPTH = 20
SEP = "›"


def ensure_tables(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS mappack_runs (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        location TEXT, keywords INTEGER DEFAULT 0, done INTEGER DEFAULT 0, cost REAL DEFAULT 0, error TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS mappack_results (
        run_id INTEGER NOT NULL, keyword TEXT NOT NULL, checked_at TEXT, error TEXT,
        pack_present INTEGER, pack_size INTEGER,
        us_pack_rank INTEGER, us_rating REAL, us_reviews INTEGER,
        them_pack_rank INTEGER, them_rating REAL, them_reviews INTEGER,
        us_org_pos INTEGER, us_org_url TEXT, them_org_pos INTEGER, them_org_url TEXT,
        win_set INTEGER, won INTEGER, cost REAL, PRIMARY KEY(run_id, keyword))""")


# ---- parsing -------------------------------------------------------------------------------------------------
def _host(s: str) -> str:
    h = (urlsplit(s if "//" in s else "//" + s).hostname or "").lower()
    return h.removeprefix("www.")


def unwrap(url: str | None) -> str | None:
    """Google redirect -> target URL when it is recoverable (/url?q=...); otherwise the URL unchanged."""
    if not url:
        return None
    p = urlsplit(url)
    if "google." in (p.hostname or "") and p.path == "/url":
        q = parse_qs(p.query)
        for k in ("q", "url"):
            if q.get(k):
                return q[k][0]
    return url


def _is_google(url: str | None) -> bool:
    return "google." in (urlsplit(url or "").hostname or "")


def _breadcrumb(bc: str | None) -> tuple[str | None, str | None]:
    """'https://host › a › b...' -> (host, approximate URL). None when the breadcrumb is not a site (e.g. '1 week ago')."""
    if not bc:
        return None, None
    parts = [p.strip() for p in bc.split(SEP)]
    first = parts[0]
    if not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/.*)?$", first, re.I):
        return None, None
    host = _host(first)
    segs = [p for p in parts[1:] if p and p != "..."]
    url = "https://" + host + "".join("/" + s.replace(" ", "-") for s in segs)
    return host, url.rstrip(".") if segs else "https://" + host + "/"


def parse_organic(it: dict) -> dict:
    """Real domain and best-known URL for one organic item. `url_exact` is False when rebuilt from the breadcrumb."""
    url = unwrap(it.get("url"))
    if url and not _is_google(url):
        return {"domain": _host(url), "url": url, "url_exact": True}
    host, approx = _breadcrumb(it.get("breadcrumb"))
    if host is None:
        wn = (it.get("website_name") or "").strip().lower()
        if re.match(r"^[\w-]+(\.[\w-]+)+$", wn):
            host = wn.removeprefix("www.")
    return {"domain": host, "url": approx, "url_exact": False}


def _matches(who: dict, domain: str | None, title: str | None = None, url: str | None = None) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    t = (title or "").lower()
    return d == who["domain"] or d.endswith("." + who["domain"]) or who["brand"] in t or \
        (bool(url) and not _is_google(url) and _host(url) == who["domain"])


def _find_organic(items: list[dict], who: dict) -> tuple[int | None, str | None]:
    for it in items:
        if it.get("type") != "organic":
            continue
        o = parse_organic(it)
        if _matches(who, o["domain"]) or (o["domain"] is None and who["brand"] in (it.get("website_name") or "").lower()):
            return it.get("rank_group"), o["url"] or ("https://" + who["domain"] + "/")
    return None, None


def analyse(result: dict | None) -> dict:
    """Per-keyword record from one SERP result (result[0] of the task). Pack rank = order within the pack."""
    items = (result or {}).get("items") or []
    pack = sorted([i for i in items if i.get("type") in ("local_pack", "map")], key=lambda i: i.get("rank_group") or 99)
    out = dict(pack_present=int(bool(pack)), pack_size=len(pack))
    for tag, who in (("us", OURS), ("them", THEIRS)):
        rank = rating = votes = None
        for n, e in enumerate(pack, 1):
            if _matches(who, e.get("domain"), e.get("title"), e.get("url")):
                rank, r = n, e.get("rating") or {}
                rating, votes = r.get("value"), r.get("votes_count")
                break
        pos, url = _find_organic(items, who)
        out.update({f"{tag}_pack_rank": rank, f"{tag}_rating": rating, f"{tag}_reviews": votes,
                    f"{tag}_org_pos": pos, f"{tag}_org_url": url})
    # Win (GLOSSARY): competitor #1 organically -> we are #1; competitor in the pack -> we are in the pack.
    need = []
    if out["them_org_pos"] == 1:
        need.append(out["us_org_pos"] == 1)
    if out["them_pack_rank"]:
        need.append(bool(out["us_pack_rank"]))
    out["win_set"] = int(bool(need))
    out["won"] = int(bool(need) and all(need))
    return out


def keyword_list(target_rows: list[dict]) -> list[str]:
    seen, out = set(), []
    for t in target_rows:
        for k in [t["primary_keyword"], *t["supporting"]]:
            k = " ".join(k.split())
            if k and "{" not in k and k.lower() not in seen:  # skip template rows like "{Designer} wedding dresses"
                seen.add(k.lower())
                out.append(k)
    return out


# ---- run ----------------------------------------------------------------------------------------------------
def create_run(con, now: str, keywords: int) -> int:
    cur = con.execute("INSERT INTO mappack_runs(started_at,status,location,keywords) VALUES (?,'running',?,?)",
                      (now, LOCATION["location_name"], keywords))
    con.commit()
    return cur.lastrowid


async def execute(open_db, run_id: int, keywords: list[str], now) -> None:
    """Check every keyword (DFS concurrency is capped in dfs.py), saving each result as it arrives."""
    async with dfs.client() as c:
        async def one(kw: str):
            task = {"keyword": kw, "language_code": "en", "device": "desktop", "depth": DEPTH, **LOCATION}
            try:
                res, cost = await dfs.post_path(c, PATH, task, mock_name="serp_live")
                row, err = analyse(res), None
            except Exception as e:  # one bad keyword must not sink the run
                res, cost, row, err = None, 0.0, {}, str(e)[:300]
            con = open_db()
            try:
                cols = ["run_id", "keyword", "checked_at", "error", "cost", *row]
                con.execute(f"INSERT OR REPLACE INTO mappack_results({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [run_id, kw, now(), err, cost, *row.values()])
                con.execute("UPDATE mappack_runs SET done=done+1, cost=cost+? WHERE id=?", (cost, run_id))
                con.commit()
            finally:
                con.close()
        await asyncio.gather(*(one(k) for k in keywords))
    con = open_db()
    try:
        failed = con.execute("SELECT COUNT(*) FROM mappack_results WHERE run_id=? AND error IS NOT NULL", (run_id,)).fetchone()[0]
        con.execute("UPDATE mappack_runs SET status=?, finished_at=?, error=? WHERE id=?",
                    ("done" if failed < len(keywords) else "error", now(),
                     f"{failed} of {len(keywords)} keywords failed" if failed else None, run_id))
        con.commit()
    finally:
        con.close()


def _avg(v):
    v = [x for x in v if x]
    return round(sum(v) / len(v), 2) if v else None


def summary(results: list[dict]) -> dict:
    ok = [r for r in results if not r["error"]]
    pack = [r for r in ok if r["pack_present"]]
    return {
        "keywords": len(results), "errors": len(results) - len(ok), "pack_present": len(pack),
        "us_in_pack": sum(1 for r in pack if r["us_pack_rank"]), "them_in_pack": sum(1 for r in pack if r["them_pack_rank"]),
        "us_avg_pack_rank": _avg(r["us_pack_rank"] for r in pack), "them_avg_pack_rank": _avg(r["them_pack_rank"] for r in pack),
        "win_set": sum(1 for r in ok if r["win_set"]), "wins": sum(1 for r in ok if r["won"]),
    }


def get_run(con, run_id: int) -> dict | None:
    r = con.execute("SELECT * FROM mappack_runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    res = [dict(x) for x in con.execute("SELECT * FROM mappack_results WHERE run_id=? ORDER BY keyword COLLATE NOCASE", (run_id,))]
    return {**dict(r), "summary": summary(res), "results": res}


def list_runs(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM mappack_runs ORDER BY id DESC"):
        res = [dict(x) for x in con.execute("SELECT * FROM mappack_results WHERE run_id=?", (r["id"],))]
        out.append({**dict(r), "summary": summary(res)})
    return out
