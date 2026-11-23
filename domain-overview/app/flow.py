"""Visitor flow (#54): arrival page -> up to 3 following pages -> exit page, from GA4 session paths.

Pure: rows in, flow structure out. Fetching lives in `ga4.py` and the endpoint."""
from __future__ import annotations

ORGANIC = "Organic Search"  # GA4 sessionDefaultChannelGroup for organic search
STEPS = 3                   # following pages after the arrival page
OTHER_PCT = 0.01            # a column's page under this share of sessions collapses into OTHER
OTHER = "other"
TOP_QUERIES = 5             # Search Console queries listed per arrival page (keyword estimate)


def _sort(d: dict[str, float]) -> list[dict]:
    """Biggest first, ties by page, OTHER always last."""
    return [{"page": p, "sessions": n} for p, n in sorted(d.items(), key=lambda kv: (kv[0] == OTHER, -kv[1], kv[0]))]


def _queries(gsc_rows: list[dict]) -> dict[str, list[dict]]:
    acc: dict[str, dict[str, list[float]]] = {}
    for r in gsc_rows:
        a = acc.setdefault(r["page"], {}).setdefault(r["query"], [0, 0])
        a[0] += r["clicks"]
        a[1] += r["impressions"]
    return {page: [{"query": q, "clicks": c, "impressions": i}
                   for q, (c, i) in sorted(qs.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[:TOP_QUERIES]]
            for page, qs in acc.items()}


def build(rows: list[dict], gsc_rows: list[dict], organic: bool = True, focus: str | None = None,
          paths: bool = True) -> dict:
    """rows: GA4 sessions, {channel, pages: [arrival, ..., exit], sessions}. gsc_rows: {page, query, clicks, impressions}
    for the same window (the keyword estimate, joined on arrival page).
    organic: only ORGANIC sessions. focus: draw only sessions arriving on this page.
    paths: False when only the arrival page of each session is known (no following steps, no exit page).

    Returns {total_sessions, arrivals: [{page, sessions, share, exit_rate, queries}], columns, links}.
    columns: arrival, STEPS following steps, exit page (only the arrival column when paths is False); pages under
    OTHER_PCT of the drawn sessions collapse into OTHER. links: {from: (col, page), to: (col, page), sessions}."""
    rows = [r for r in rows if r["pages"] and (not organic or r["channel"] == ORGANIC)]
    total = sum(r["sessions"] for r in rows)
    arr: dict[str, list[float]] = {}
    for r in rows:
        a = arr.setdefault(r["pages"][0], [0, 0])
        a[0] += r["sessions"]
        a[1] += r["sessions"] if len(r["pages"]) == 1 else 0
    queries = _queries(gsc_rows)
    arrivals = [{"page": x["page"], "sessions": x["sessions"], "share": x["sessions"] / total if total else None,
                 "exit_rate": (arr[x["page"]][1] / x["sessions"] if x["sessions"] else None) if paths else None,
                 "queries": queries.get(x["page"], [])}
                for x in _sort({p: a[0] for p, a in arr.items()})]

    drawn = [r for r in rows if focus is None or r["pages"][0] == focus]
    ncols = STEPS + 2 if paths else 1
    chains = []  # (column, page) per session row, arrival first, exit last
    for r in drawn:
        p = r["pages"]
        chain = [(0, p[0])]
        if paths:
            chain += [(i, page) for i, page in enumerate(p[1:STEPS + 1], start=1)]
            chain.append((ncols - 1, p[-1]))
        chains.append((chain, r["sessions"]))

    shown = sum(n for _, n in chains)
    cols: list[dict[str, float]] = [{} for _ in range(ncols)]
    for chain, n in chains:
        for c, page in chain:
            cols[c][page] = cols[c].get(page, 0) + n
    keep = [{p for p, n in col.items() if n >= OTHER_PCT * shown} for col in cols]

    def name(c: int, page: str) -> str:
        return page if page in keep[c] else OTHER

    columns: list[dict[str, float]] = [{} for _ in range(ncols)]
    links: dict[tuple, float] = {}
    for chain, n in chains:
        named = [(c, name(c, page)) for c, page in chain]
        for c, page in named:
            columns[c][page] = columns[c].get(page, 0) + n
        for a, b in zip(named, named[1:]):
            links[(a, b)] = links.get((a, b), 0) + n
    return {"total_sessions": total, "arrivals": arrivals, "columns": [_sort(c) for c in columns],
            "links": [{"from": a, "to": b, "sessions": n}
                      for (a, b), n in sorted(links.items(), key=lambda kv: (kv[0][0][0], -kv[1], kv[0]))]}
