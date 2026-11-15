"""Rankings (#26): per keyword target position maths from the Search Console daily store.

One interface: report(daily, targets, start, end, grouping) -> per-target rows + overview series.
- A query belongs to a target when it is a close variant (match.same_variant) of its primary or a supporting keyword.
- Positions are impression-weighted. Data days are the dates with any stored row (site-wide).
- current = last 7 data days of the range; start = first 7 data days; movement = start - current
  (positive = moved up, since a lower position is better); period_avg = the whole range.
- No impressions -> None ("no data" for a target, a gap in a series).
Range presets and grouping rules live here too (resolve_range, groupings); labels are derived at read time.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from . import match
from .clickstream import norm_keyword

PRESETS = ("all", "12m", "6m", "3m", "30d", "12w", "7d")
DEFAULT_PRESET = "3m"
GROUPINGS = ("day", "week", "month")
MOVEMENTS = ("all", "up", "down")
WINDOW = 7  # data days for current and period-start positions
LABEL_KINDS = ("line", "phase", "topic", "folder", "tag")


# ---------------------------------------------------------------- range and grouping

def _months_back(d: dt.date, n: int) -> dt.date:
    """Same day n months earlier (clamped to month end), plus one day: the start of an n-month range ending on d."""
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return min(dt.date(y, m, d.day), nxt - dt.timedelta(days=1)) + dt.timedelta(days=1)


def resolve_range(preset: str, first_day: dt.date | None, last_day: dt.date | None) -> dict | None:
    """Effective {preset, start, end, clamped} ending on the last data day; a start before the first data day
    is clamped to it. None when there is no data. Raises ValueError for an unknown preset."""
    if preset not in PRESETS:
        raise ValueError(f"range must be one of {', '.join(PRESETS)}")
    if not first_day or not last_day:
        return None
    end = last_day
    if preset == "all":
        start = first_day
    elif preset.endswith("m"):
        start = _months_back(end, int(preset[:-1]))
    else:
        days = int(preset[:-1]) * (7 if preset.endswith("w") else 1)
        start = end - dt.timedelta(days=days - 1)
    clamped = start < first_day
    return {"preset": preset, "start": max(start, first_day).isoformat(), "end": end.isoformat(), "clamped": clamped}


def bucket(d: dt.date, grouping: str) -> dt.date:
    """Start of the graph point (day, week or month) that holds day d."""
    if grouping == "week":
        return d - dt.timedelta(days=d.weekday())
    if grouping == "month":
        return d.replace(day=1)
    return d


def buckets(start: dt.date, end: dt.date, grouping: str) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        b = bucket(d, grouping)
        if not out or out[-1] != b:
            out.append(b)
        d += dt.timedelta(days=1)
    return out


def groupings(start: dt.date, end: dt.date) -> list[str]:
    """Groupings that give at least 2 points over the range."""
    return [g for g in GROUPINGS if len(buckets(start, end, g)) >= 2]


def default_grouping(start: dt.date, end: dt.date) -> str | None:
    offered = groupings(start, end)
    days = (end - start).days + 1
    want = "day" if days <= 31 else "week" if days <= 186 else "month"
    return want if want in offered else (offered[-1] if offered else None)


# ---------------------------------------------------------------- labels

def _path(url_or_path: str) -> str:
    p = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path
    return p.rstrip("/") or "/"


def folder(path: str) -> str:
    p = _path(path)
    return p.rsplit("/", 1)[0] or "/"


def labels(target: dict) -> list[str]:
    """Derived labels as kind:value (product line, phase, topic when set, target page folder)."""
    out = []
    if target.get("product_line"):
        out.append(f"line:{target['product_line']}")
    if target.get("phase") is not None:
        out.append(f"phase:{target['phase']}")
    if (target.get("topic") or "").strip():
        out.append(f"topic:{target['topic'].strip()}")
    out.append(f"folder:{folder(target['path'])}")
    out += [f"tag:{t}" for t in target.get("tags") or []]
    return out


def label_match(have: list[str], wanted: list[str]) -> bool:
    """Any wanted label of a kind (OR within a kind), every kind asked for (AND across kinds)."""
    kinds: dict[str, set[str]] = {}
    for w in wanted:
        kinds.setdefault(w.split(":", 1)[0], set()).add(w)
    h = set(have)
    return all(h & ws for ws in kinds.values())


# ---------------------------------------------------------------- maths

def _wavg(rows) -> float | None:
    imp = sum(r["impressions"] for r in rows)
    return sum(r["position"] * r["impressions"] for r in rows) / imp if imp else None


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def _variant_rows(daily: list[dict], target: dict) -> list[dict]:
    kws = [target["primary_keyword"], *(target.get("supporting") or [])]
    return [r for r in daily if any(match.same_variant(r["query"], k) for k in kws)]


def report(daily: list[dict], targets: list[dict], start: dt.date, end: dt.date, grouping: str,
           clickstream: dict[str, int] | None = None) -> dict:
    """daily: {date (ISO), query, page, clicks, impressions, position} rows (any dates; only start..end used).
    Returns {rows: one per target, overview: [{start, position}] = mean of the targets' bucket positions}."""
    s, e = start.isoformat(), end.isoformat()
    daily = [r for r in daily if s <= r["date"] <= e]
    data_days = sorted({r["date"] for r in daily})
    first_w, last_w = set(data_days[:WINDOW]), set(data_days[-WINDOW:])
    bks = buckets(start, end, grouping)
    rows = []
    for t in targets:
        vr = _variant_rows(daily, t)
        by_page: dict[str, float] = {}
        for r in vr:
            by_page[r["page"]] = by_page.get(r["page"], 0) + r["impressions"]
        top = max(by_page, key=lambda p: (by_page[p], p)) if any(by_page.values()) else None
        cur = _wavg([r for r in vr if r["date"] in last_w])
        st = _wavg([r for r in vr if r["date"] in first_w])
        mv = None if cur is None or st is None else st - cur
        per: dict[dt.date, list] = {}
        for r in vr:
            per.setdefault(bucket(dt.date.fromisoformat(r["date"]), grouping), []).append(r)
        imps = sum(r["impressions"] for r in vr)
        rows.append({
            "id": t["id"], "keyword": t["primary_keyword"], "path": t["path"], "labels": labels(t),
            "volume": t.get("volume"),
            "clickstream_volume": _clickstream_volume(clickstream, t),
            "clicks": sum(r["clicks"] for r in vr), "impressions": imps,
            "top_url": top, "top_url_off_target": None if top is None else _path(top) != _path(t["path"]),
            "current": _r(cur), "start": _r(st), "movement": _r(mv),
            "direction": None if mv is None else "up" if mv > 0 else "down" if mv < 0 else "flat",
            "period_avg": _r(_wavg(vr)), "no_data": imps == 0,
            "series": [{"start": b.isoformat(), "position": _r(_wavg(per.get(b, [])))} for b in bks],
            "_raw": [_wavg(per.get(b, [])) for b in bks],
        })
    overview = []
    for i, b in enumerate(bks):
        ps = [r["_raw"][i] for r in rows if r["_raw"][i] is not None]
        overview.append({"start": b.isoformat(), "position": _r(sum(ps) / len(ps)) if ps else None})
    for r in rows:
        del r["_raw"]
    return {"rows": rows, "overview": overview}


def _clickstream_volume(clickstream: dict[str, int] | None, target: dict) -> int | None:
    return (clickstream or {}).get(norm_keyword(target["primary_keyword"]))


def no_data(target: dict, clickstream: dict[str, int] | None = None) -> dict:
    """A list row's figures when the Search Console store is empty: everything missing, never 0 (ADR 0002)."""
    return {"labels": labels(target), "volume": target.get("volume"),
            "clickstream_volume": _clickstream_volume(clickstream, target),
            "clicks": None, "impressions": None,
            "top_url": None, "top_url_off_target": None, "current": None, "start": None, "movement": None,
            "direction": None, "period_avg": None, "no_data": True, "series": []}


def _cards(row: dict, target: dict) -> dict:
    cards = {k: row[k] for k in ("current", "start", "movement", "direction", "period_avg", "clicks", "impressions", "no_data")}
    cards["ads_volume"] = target.get("volume")
    cards["clickstream_volume"] = row["clickstream_volume"]
    return cards


def detail_no_data(target: dict, clickstream: dict[str, int] | None = None) -> dict:
    """detail() for an empty Search Console store."""
    row = no_data(target, clickstream)
    return {"id": target["id"], "keyword": target["primary_keyword"], "path": target["path"], "labels": row["labels"],
            "tags": list(target.get("tags") or []), "cards": _cards(row, target), "series": [], "variants": [],
            "top_url": None, "top_url_off_target": None}


def detail(daily: list[dict], target: dict, start: dt.date, end: dt.date, grouping: str,
           clickstream: dict[str, int] | None = None) -> dict:
    """One target over the range (#28): cards, series at the grouping and a table of its close-variant queries,
    each with its own impressions, clicks and impression-weighted position (queries differing only in case merge)."""
    row = report(daily, [target], start, end, grouping, clickstream)["rows"][0]
    s, e = start.isoformat(), end.isoformat()
    vr = _variant_rows([r for r in daily if s <= r["date"] <= e], target)
    per: dict[str, list] = {}
    for r in vr:
        per.setdefault(r["query"].strip().lower(), []).append(r)
    variants = sorted(({"query": q, "impressions": sum(r["impressions"] for r in rs), "clicks": sum(r["clicks"] for r in rs),
                        "position": _r(_wavg(rs))} for q, rs in per.items()),
                      key=lambda v: (-v["impressions"], v["query"]))
    return {"id": target["id"], "keyword": target["primary_keyword"], "path": target["path"], "labels": row["labels"],
            "tags": list(target.get("tags") or []), "cards": _cards(row, target), "series": row["series"], "variants": variants,
            "top_url": row["top_url"], "top_url_off_target": row["top_url_off_target"]}
