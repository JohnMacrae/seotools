"""Weekly digest: page-2 queries and decaying pages from Search Console data (issue #18)."""
from __future__ import annotations

import datetime as dt
import re

from . import match

# Thresholds, all in one place.
POS_MIN = 8
POS_MAX = 20
MIN_IMPRESSIONS = 10
WINDOW_DAYS = 28
DECAY_PCT = 0.30
DECAY_MIN_CLICKS = 5
LOW_CTR_POS_MAX = 8  # exclusive
LOW_CTR_MIN_IMPRESSIONS = 100
LOW_CTR_RATIO = 0.5
EXPECTED_CTR = {1: .28, 2: .15, 3: .10, 4: .07, 5: .05, 6: .04, 7: .03}
BRAND_TERMS = ("serenity",)
VOLUME_TTL_DAYS = 30
PAGE2_TARGET_CTR = 0.05  # position-5 expected CTR, used for page-2 "clicks lost"
INTENT_TERMS = {
    "booking": ["book", "booking", "appointment", "near me", "shop", "shops", "store", "boutique", "colchester",
                "essex", "visit", "opening"],
    "sales": ["buy", "price", "prices", "cost", "sale", "sales", "cheap", "affordable", "under", "discount",
              "outlet", "sample"],
}
VOLUME_CHUNK = 1000
DFS_VOLUME_PATH = "keywords_data/google_ads/search_volume/live"
DFS_LOCATION = 2826  # UK, same default as the rest of the app
DFS_LANGUAGE = "en"

YEAR_SHIFT_DAYS = 364  # 52 weeks, so the year-ago window has the same weekdays as the current one


def thresholds() -> dict:
    return {"pos_min": POS_MIN, "pos_max": POS_MAX, "min_impressions": MIN_IMPRESSIONS, "window_days": WINDOW_DAYS,
            "decay_pct": DECAY_PCT, "decay_min_clicks": DECAY_MIN_CLICKS,
            "low_ctr_pos_max": LOW_CTR_POS_MAX, "low_ctr_min_impressions": LOW_CTR_MIN_IMPRESSIONS,
            "low_ctr_ratio": LOW_CTR_RATIO, "volume_ttl_days": VOLUME_TTL_DAYS,
            "page2_target_ctr": PAGE2_TARGET_CTR, "intent_terms": INTENT_TERMS, "expected_ctr": EXPECTED_CTR, "brand_terms": list(BRAND_TERMS)}


def _tag(row: dict, targets_by_page: dict[str, list[str]]) -> str:
    kws = targets_by_page.get(row["page"]) or []
    return "on-target" if any(match.contains_variant(row["query"], kw) for kw in kws) else "off-target"


_INTENT_RE = {k: re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(t) for t in v) + r")(?![a-z0-9])")
              for k, v in INTENT_TERMS.items()}


def intent(query: str) -> list[str]:
    """Intent tags for a query: whole-word, case-insensitive match against INTENT_TERMS ("near me" as a phrase)."""
    ql = " ".join(query.lower().split())
    return [k for k, rx in _INTENT_RE.items() if rx.search(ql)]


def _vol(volumes: dict[str, int] | None, query: str):
    return (volumes or {}).get(query.lower())


def page2(rows: list[dict], targets_by_page: dict[str, list[str]], volumes: dict[str, int] | None = None) -> list[dict]:
    """rows: {page, query, clicks, impressions, position}. targets_by_page: page URL -> its target keywords.
    Returns [{page, impressions, clicks, queries: [{query, impressions, clicks, position, tag}]}], biggest page first."""
    by_page: dict[str, list[dict]] = {}
    for r in rows:
        if not (POS_MIN <= r["position"] <= POS_MAX and r["impressions"] >= MIN_IMPRESSIONS):
            continue
        by_page.setdefault(r["page"], []).append({
            "query": r["query"], "impressions": r["impressions"], "clicks": r["clicks"],
            "position": r["position"], "tag": _tag(r, targets_by_page),
            "missed": max(0, r["impressions"] * PAGE2_TARGET_CTR - r["clicks"]),
            "intent": intent(r["query"]), "volume": _vol(volumes, r["query"])})
    out = []
    for page, qs in by_page.items():
        qs.sort(key=lambda q: -q["missed"])
        out.append({"page": page, "impressions": sum(q["impressions"] for q in qs),
                    "clicks": sum(q["clicks"] for q in qs), "missed": sum(q["missed"] for q in qs), "queries": qs})
    out.sort(key=lambda p: -p["missed"])
    return out


def low_ctr(rows: list[dict], targets_by_page: dict[str, list[str]], volumes: dict[str, int] | None = None) -> list[dict]:
    """Page-1 queries whose CTR is under LOW_CTR_RATIO x the expected CTR for their rounded position.
    Returns [{page, impressions, clicks, missed, queries: [{query, impressions, clicks, ctr, expected_ctr,
    position, missed, tag}]}], biggest summed clicks missed first."""
    by_page: dict[str, list[dict]] = {}
    for r in rows:
        imp = r["impressions"]
        if not (r["position"] < LOW_CTR_POS_MAX and imp >= LOW_CTR_MIN_IMPRESSIONS):
            continue
        ql = r["query"].lower()
        if any(b in ql for b in BRAND_TERMS):
            continue
        # positions in [7.5, 8) round to 8, which has no table entry: clamp to the last one
        expected = EXPECTED_CTR[min(max(EXPECTED_CTR), max(1, round(r["position"])))]
        ctr = r["clicks"] / imp
        if not ctr < LOW_CTR_RATIO * expected:
            continue
        by_page.setdefault(r["page"], []).append({
            "query": r["query"], "impressions": imp, "clicks": r["clicks"], "ctr": ctr, "expected_ctr": expected,
            "position": r["position"], "missed": expected * imp - r["clicks"], "tag": _tag(r, targets_by_page),
            "intent": intent(r["query"]), "volume": _vol(volumes, r["query"])})
    out = []
    for page, qs in by_page.items():
        qs.sort(key=lambda q: -q["missed"])
        out.append({"page": page, "impressions": sum(q["impressions"] for q in qs),
                    "clicks": sum(q["clicks"] for q in qs), "missed": sum(q["missed"] for q in qs), "queries": qs})
    out.sort(key=lambda p: -p["missed"])
    return out


def windows(data_end: dt.date) -> dict[str, tuple[dt.date, dt.date]]:
    n = WINDOW_DAYS
    cur = (data_end - dt.timedelta(days=n - 1), data_end)
    prev = (cur[0] - dt.timedelta(days=n), cur[0] - dt.timedelta(days=1))
    ya = (cur[0] - dt.timedelta(days=YEAR_SHIFT_DAYS), cur[1] - dt.timedelta(days=YEAR_SHIFT_DAYS))
    return {"current": cur, "previous": prev, "year_ago": ya}


def decay_dates(data_end: dt.date) -> dict:
    return {k: {"start": a.isoformat(), "end": b.isoformat()} for k, (a, b) in windows(data_end).items()}


def decay(daily_rows: list[dict], data_end: dt.date) -> list[dict]:
    """daily_rows: {page, date (ISO), clicks}. Flagged pages, biggest click loss first.
    Each comparison: {clicks, delta, pct}; year_ago is None when the page has no daily rows in that window."""
    w = windows(data_end)
    clicks: dict[str, dict[str, float]] = {}
    seen: dict[str, dict[str, bool]] = {}
    for r in daily_rows:
        d = dt.date.fromisoformat(r["date"])
        for name, (a, b) in w.items():
            if a <= d <= b:
                clicks.setdefault(r["page"], {k: 0.0 for k in w})[name] += r["clicks"]
                seen.setdefault(r["page"], {})[name] = True
    out = []
    for page, c in clicks.items():
        cur = c["current"]
        comps, flagged_on, loss = {}, [], 0.0
        for name in ("previous", "year_ago"):
            if not seen[page].get(name):
                comps[name] = None
                continue
            base = c[name]
            delta = cur - base
            pct = delta / base if base else None
            comps[name] = {"clicks": base, "delta": delta, "pct": pct}
            if pct is not None and -pct >= DECAY_PCT and -delta >= DECAY_MIN_CLICKS:
                flagged_on.append(name)
                loss = max(loss, -delta)
        if flagged_on:
            out.append({"page": page, "current_clicks": cur, "previous": comps["previous"],
                        "year_ago": comps["year_ago"], "flagged_on": flagged_on, "loss": loss})
    out.sort(key=lambda p: -p["loss"])
    return out


# ---- search-volume cache (issue #20) ----

def ensure_tables(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS volume_runs (
        id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, keywords INTEGER, cost REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS query_volumes (
        query TEXT PRIMARY KEY, volume INTEGER, fetched_at TEXT NOT NULL, run_id INTEGER)""")


def cached_volumes(con) -> dict[str, int]:
    """Known volumes (NULL volumes omitted), keyed by lower-cased query."""
    return {r[0]: r[1] for r in con.execute("SELECT query, volume FROM query_volumes WHERE volume IS NOT NULL")}


def volume_status(con) -> dict:
    run = con.execute("SELECT fetched_at, cost FROM volume_runs ORDER BY id DESC LIMIT 1").fetchone()
    n = con.execute("SELECT COUNT(*) FROM query_volumes").fetchone()[0]
    return {"fetched_at": run[0] if run else None, "cost": run[1] if run else None, "count": n}


def digest_queries(page2_groups: list[dict], low_ctr_groups: list[dict]) -> list[str]:
    return [q["query"] for g in (*page2_groups, *low_ctr_groups) for q in g["queries"]]


def _ads_keyword(q: str) -> str | None:
    """Google Ads search_volume rejects symbols (e.g. '?') and keywords over 80 chars or 10 words."""
    k = " ".join(re.sub(r"[^\w\s'&-]", " ", q).split())
    return k if k and len(k) <= 80 and len(k.split()) <= 10 else None


async def fetch_volumes(con, queries: list[str], now: dt.datetime | None = None) -> dict:
    """Fetch search volume for queries missing from query_volumes or older than VOLUME_TTL_DAYS.
    Returns {fetched, cached, cost}; makes no DFS call (and writes no run row) when nothing is needed."""
    from . import dfs  # late import: keeps digest.py pure for the non-fetch paths
    now = now or dt.datetime.now(dt.timezone.utc)
    wanted = list(dict.fromkeys(q.strip().lower() for q in queries if q and q.strip()))
    cutoff = (now - dt.timedelta(days=VOLUME_TTL_DAYS)).isoformat()
    fresh = {r[0] for r in con.execute("SELECT query FROM query_volumes WHERE fetched_at >= ?", (cutoff,))}
    todo = [q for q in wanted if q not in fresh]
    cached = len(wanted) - len(todo)
    if not todo:
        return {"fetched": 0, "cached": cached, "cost": 0.0}
    stamp = now.isoformat()
    cur = con.execute("INSERT INTO volume_runs(fetched_at, keywords, cost) VALUES (?,?,0)", (stamp, len(todo)))
    run_id, total = cur.lastrowid, 0.0
    try:
        async with dfs.client() as c:
            for i in range(0, len(todo), VOLUME_CHUNK):
                chunk = todo[i:i + VOLUME_CHUNK]
                clean = {q: _ads_keyword(q) for q in chunk}
                send = list(dict.fromkeys(k for k in clean.values() if k))
                got: dict = {}
                if send:
                    items, cost = await dfs.post_path(
                        c, DFS_VOLUME_PATH,
                        {"keywords": send, "location_code": DFS_LOCATION, "language_code": DFS_LANGUAGE},
                        mock_name="search_volume", all_results=True)
                    total += cost
                    got = {(it.get("keyword") or "").lower(): it.get("search_volume") for it in (items or [])}
                # queries Google Ads can't take are stored as NULL so they aren't re-sent until the TTL passes
                con.executemany(
                    "INSERT OR REPLACE INTO query_volumes(query, volume, fetched_at, run_id) VALUES (?,?,?,?)",
                    [(q, got.get(clean[q]) if clean[q] else None, stamp, run_id) for q in chunk])
                con.execute("UPDATE volume_runs SET cost=? WHERE id=?", (total, run_id))
                con.commit()
    except Exception:
        if total == 0:
            con.execute("DELETE FROM volume_runs WHERE id=?", (run_id,))
        con.commit()
        raise
    return {"fetched": len(todo), "cached": cached, "cost": total}
