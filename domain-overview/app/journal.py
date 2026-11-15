"""Journal (#29, ADR 0003): per-site log of SEO changes with a live date, a page or URL pattern and a change type.

Repo entries (source 'repo') come from the site's commit trailers and are read-only here; manual entries are
edited through the API. Unique on source + ref + page."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

SOURCES = ("repo", "manual")
EDITABLE = ("live_date", "approx", "page", "change_type", "note", "ref")


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY,
        live_date TEXT NOT NULL,
        approx INTEGER NOT NULL DEFAULT 0,
        page TEXT NOT NULL,
        change_type TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        ref TEXT NOT NULL DEFAULT '')""")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_journal ON journal(source, ref, page)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_journal_date ON journal(live_date)")


def _path(page: str) -> str:
    """Path form used for matching: no scheme/host/query, lower case, no trailing slash (root is '/')."""
    page = page.strip()
    if "://" in page:
        page = urlsplit(page).path
    page = page.split("?")[0].split("#")[0]
    return (re.sub(r"/{2,}", "/", page).rstrip("/") or "/").lower()


def matches(pattern: str, page: str) -> bool:
    """Does a journal entry's page or URL pattern cover a target page? `*` matches any run of characters."""
    if not pattern.strip() or not page.strip():
        return False
    pat = _path(pattern)
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pat)
    return re.fullmatch(regex, _path(page)) is not None


def _clean(body: dict, partial: bool) -> dict:
    """Validated fields from a request body; raises ValueError."""
    out: dict = {}
    for k in ("live_date", "page", "change_type"):
        if k in body or not partial:
            v = str(body.get(k) or "").strip()
            if not v:
                raise ValueError(f"{k} is required")
            out[k] = v
    if "live_date" in out:
        try:
            dt.date.fromisoformat(out["live_date"])
        except ValueError:
            raise ValueError("live_date must be YYYY-MM-DD")
    if "approx" in body:
        out["approx"] = 1 if body["approx"] else 0
    for k in ("note", "ref"):
        if k in body:
            out[k] = str(body[k] or "").strip()
    return out


def list_entries(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT * FROM journal ORDER BY live_date DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def get(con: sqlite3.Connection, eid: int) -> dict | None:
    r = con.execute("SELECT * FROM journal WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None


def insert(con: sqlite3.Connection, e: dict) -> int | None:
    """Insert an entry; None when source+ref+page already exists. Caller commits."""
    cur = con.execute(
        """INSERT OR IGNORE INTO journal(live_date, approx, page, change_type, note, source, ref)
           VALUES (?,?,?,?,?,?,?)""",
        (e["live_date"], 1 if e.get("approx") else 0, e["page"], e["change_type"], e.get("note", ""),
         e.get("source", "manual"), e.get("ref", "")))
    return cur.lastrowid if cur.rowcount else None


def create(con: sqlite3.Connection, body: dict) -> dict:
    """Create a manual entry. Raises ValueError (bad input) or KeyError (duplicate)."""
    if body.get("source", "manual") != "manual":
        raise ValueError("only manual entries can be created here")
    e = _clean(body, False)
    e["ref"] = e.get("ref") or f"manual-{uuid.uuid4().hex[:12]}"
    e["source"] = "manual"
    eid = insert(con, e)
    if eid is None:
        raise KeyError("duplicate")
    con.commit()
    return get(con, eid)


def update(con: sqlite3.Connection, eid: int, body: dict) -> dict | None:
    """Update a manual entry. None if missing; PermissionError for repo entries; ValueError/KeyError as create."""
    cur = get(con, eid)
    if cur is None:
        return None
    if cur["source"] != "manual":
        raise PermissionError
    e = _clean(body, True)
    if "ref" in e and not e["ref"]:
        e["ref"] = cur["ref"]
    if e:
        sets = ", ".join(f"{k}=?" for k in e)
        try:
            con.execute(f"UPDATE journal SET {sets} WHERE id=?", (*e.values(), eid))
        except sqlite3.IntegrityError:
            raise KeyError("duplicate")
        con.commit()
    return get(con, eid)


def delete(con: sqlite3.Connection, eid: int) -> bool | None:
    """True deleted, False missing; PermissionError for repo entries."""
    cur = get(con, eid)
    if cur is None:
        return False
    if cur["source"] != "manual":
        raise PermissionError
    con.execute("DELETE FROM journal WHERE id=?", (eid,))
    con.commit()
    return True


def for_page(con: sqlite3.Connection, page: str) -> list[dict]:
    """Entries (newest first) whose page or pattern covers `page`; used for chart markers."""
    return [e for e in list_entries(con) if matches(e["page"], page)]
