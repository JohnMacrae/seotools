"""Keyword target store (ADR 0001): curated rows, seeded from docs/keyword-targets.md.

Seed rule: rows are matched on seed_key (the doc's "#"). New keys are inserted; existing rows never edited
by hand are refreshed from the doc; rows edited via update() (edited=1) are left alone and reported as kept_edited.
"""
from __future__ import annotations

import json
import re

FIELDS = ("path", "primary_keyword", "supporting", "volume", "win_basis", "phase", "product_line", "is_new")


def ensure_table(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS keyword_targets (
            id INTEGER PRIMARY KEY,
            seed_key INTEGER UNIQUE,
            path TEXT NOT NULL,
            primary_keyword TEXT NOT NULL,
            supporting TEXT NOT NULL DEFAULT '[]',
            volume INTEGER,
            win_basis TEXT NOT NULL DEFAULT '',
            phase INTEGER NOT NULL DEFAULT 1,
            product_line TEXT NOT NULL DEFAULT 'bridal',
            is_new INTEGER NOT NULL DEFAULT 0,
            edited INTEGER NOT NULL DEFAULT 0)"""
    )


def _split_volume(s: str) -> tuple[str, int | None]:
    m = re.search(r"\(([\d,]+)\)\s*$", s)
    return (s[:m.start()].strip(), int(m.group(1).replace(",", ""))) if m else (s.strip(), None)


def parse_doc(text: str) -> list[dict]:
    """Rows of the first markdown table whose first cell is a number. The volume is the primary keyword's figure;
    figures on supporting keywords are close variants and are dropped (counted once)."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].isdigit():
            continue
        key, page, primary, supp, win = cells
        m = re.search(r"`([^`]+)`", page)
        kw, vol = _split_volume(primary)
        supporting = [] if supp in ("—", "-", "") else [
            _split_volume(re.sub(r"\([a-z ]+\)\s*$", "", s.strip()))[0] for s in supp.split(";") if s.strip()]
        out.append(dict(seed_key=int(key), path=m.group(1) if m else page, primary_keyword=kw, supporting=supporting,
                        volume=vol, win_basis=win, phase=1, product_line="bridal",
                        is_new=int("(new" in page)))
    return out


def volume_map(rows: list[dict]) -> dict[str, int]:
    """{keyword_lower: Google Ads UK monthly volume} for every primary and close-variant keyword of the targets
    (variants share the primary's figure; a primary's own figure wins over a variant's). ADR 0002."""
    out: dict[str, int] = {}
    for pass_primary in (True, False):
        for r in rows:
            if r.get("volume") is None:
                continue
            for kw in ([r["primary_keyword"]] if pass_primary else r.get("supporting") or []):
                k = (kw or "").strip().lower()
                if k:
                    out.setdefault(k, r["volume"])
    return out


def _row(r) -> dict:
    d = dict(r)
    d["supporting"] = json.loads(d["supporting"])
    return d


def list_all(con) -> list[dict]:
    return [_row(r) for r in con.execute("SELECT * FROM keyword_targets ORDER BY phase, COALESCE(seed_key, 1e9), id")]


def get(con, tid: int) -> dict | None:
    r = con.execute("SELECT * FROM keyword_targets WHERE id=?", (tid,)).fetchone()
    return _row(r) if r else None


def create(con, data: dict, seed_key: int | None = None) -> dict:
    d = {"supporting": [], "volume": None, "win_basis": "", "phase": 1, "product_line": "bridal", "is_new": 0, **data}
    cur = con.execute(
        "INSERT INTO keyword_targets(seed_key,path,primary_keyword,supporting,volume,win_basis,phase,product_line,is_new)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (seed_key, d["path"], d["primary_keyword"], json.dumps(d["supporting"]), d["volume"], d["win_basis"],
         d["phase"], d["product_line"], d["is_new"]))
    con.commit()
    return get(con, cur.lastrowid)


def update(con, tid: int, data: dict) -> dict | None:
    if get(con, tid) is None:
        return None
    sets, vals = [], []
    for k in FIELDS:
        if k in data:
            sets.append(f"{k}=?")
            vals.append(json.dumps(data[k]) if k == "supporting" else data[k])
    if sets:
        con.execute(f"UPDATE keyword_targets SET {','.join(sets)}, edited=1 WHERE id=?", (*vals, tid))
        con.commit()
    return get(con, tid)


def delete(con, tid: int) -> bool:
    n = con.execute("DELETE FROM keyword_targets WHERE id=?", (tid,)).rowcount
    con.commit()
    return n > 0


def seed(con, rows: list[dict]) -> dict:
    res = {"added": 0, "updated": 0, "kept_edited": 0}
    for r in rows:
        ex = con.execute("SELECT id, edited FROM keyword_targets WHERE seed_key=?", (r["seed_key"],)).fetchone()
        data = {k: r[k] for k in FIELDS}
        if ex is None:
            create(con, data, r["seed_key"])
            res["added"] += 1
        elif ex["edited"]:
            res["kept_edited"] += 1
        else:
            update(con, ex["id"], data)
            con.execute("UPDATE keyword_targets SET edited=0 WHERE id=?", (ex["id"],))
            con.commit()
            res["updated"] += 1
    return res
