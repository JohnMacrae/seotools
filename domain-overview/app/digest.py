"""Weekly digest: page-2 queries and decaying pages from Search Console data (issue #18)."""
from __future__ import annotations

import datetime as dt

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

YEAR_SHIFT_DAYS = 364  # 52 weeks, so the year-ago window has the same weekdays as the current one


def thresholds() -> dict:
    return {"pos_min": POS_MIN, "pos_max": POS_MAX, "min_impressions": MIN_IMPRESSIONS, "window_days": WINDOW_DAYS,
            "decay_pct": DECAY_PCT, "decay_min_clicks": DECAY_MIN_CLICKS,
            "low_ctr_pos_max": LOW_CTR_POS_MAX, "low_ctr_min_impressions": LOW_CTR_MIN_IMPRESSIONS,
            "low_ctr_ratio": LOW_CTR_RATIO, "expected_ctr": EXPECTED_CTR, "brand_terms": list(BRAND_TERMS)}


def _tag(row: dict, targets_by_page: dict[str, list[str]]) -> str:
    kws = targets_by_page.get(row["page"]) or []
    return "on-target" if any(match.contains_variant(row["query"], kw) for kw in kws) else "off-target"


def page2(rows: list[dict], targets_by_page: dict[str, list[str]]) -> list[dict]:
    """rows: {page, query, clicks, impressions, position}. targets_by_page: page URL -> its target keywords.
    Returns [{page, impressions, clicks, queries: [{query, impressions, clicks, position, tag}]}], biggest page first."""
    by_page: dict[str, list[dict]] = {}
    for r in rows:
        if not (POS_MIN <= r["position"] <= POS_MAX and r["impressions"] >= MIN_IMPRESSIONS):
            continue
        by_page.setdefault(r["page"], []).append({
            "query": r["query"], "impressions": r["impressions"], "clicks": r["clicks"],
            "position": r["position"], "tag": _tag(r, targets_by_page)})
    out = []
    for page, qs in by_page.items():
        qs.sort(key=lambda q: -q["impressions"])
        out.append({"page": page, "impressions": sum(q["impressions"] for q in qs),
                    "clicks": sum(q["clicks"] for q in qs), "queries": qs})
    out.sort(key=lambda p: -p["impressions"])
    return out


def low_ctr(rows: list[dict], targets_by_page: dict[str, list[str]]) -> list[dict]:
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
            "position": r["position"], "missed": expected * imp - r["clicks"], "tag": _tag(r, targets_by_page)})
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
