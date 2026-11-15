"""Journal repo reader (#30, ADR 0003): journal entries from a site repo's main-branch history.

A commit that changes a page carries `SEO-Page: <url or pattern>` (one or more) and `SEO-Change: <type> | <note>`
trailers. Its live date is the earliest `live/<YYYY-MM-DD>` tag containing it; a commit no live tag contains gets
the date of the main-branch commit that brought it in (its merge, or itself if committed straight to main), marked
approx (dates are Europe/London). Every SEO-Change trailer counts. Commits shipped in the same deploy (tag, or
merge when untagged) for the same page become one entry, with ref = the tag name or `merge/<sha>`. A commit missing either trailer is skipped.

`sync` makes the journal's repo entries match the repo: upsert on source+ref+page, and drop repo entries that no
longer come out of the read (an approx entry whose commit has since been tagged), unless the read found nothing.
Re-reading never duplicates."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from . import journal

BRANCH = "main"
LONDON = ZoneInfo("Europe/London")
TYPE_SEP = ", "  # several change types in one entry
TAG_RE = re.compile(r"live/(\d{4}-\d{2}-\d{2})")
_FMT = "%H%x1e%P%x1e%cI%x1e%s%x1e%(trailers:key=SEO-Page,valueonly,separator=%x1f)%x1e" \
       "%(trailers:key=SEO-Change,valueonly,separator=%x1f)"


class RepoError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    # safe.directory: the repo is a read-only mount owned by another user inside the container
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RepoError(f"git failed: {e}") from e
    if r.returncode:
        raise RepoError(f"git {args[0]} failed in {repo}: {r.stderr.strip()[:200]}")
    return r.stdout


def _live_tags(repo: Path) -> list[tuple[str, str, str]]:
    """(date, tag name, commit sha) for every valid live/<date> tag, earliest first."""
    out = []
    for line in _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname) %(*objectname)",
                     "refs/tags/live/").splitlines():
        name, sha, *peeled = line.split(" ")
        m = TAG_RE.fullmatch(name)
        if not m:
            continue
        try:
            dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        out.append((m.group(1), name, (peeled[0] if peeled and peeled[0] else sha)))
    return sorted(out)


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.split("\x1f") if x.strip()]


def _history(repo: Path, head: str, tags: list[tuple[str, str, str]]) -> dict[str, dict]:
    """Every commit reachable from main or a live tag, oldest first: parents, London date, subject, trailers."""
    raw = _git(repo, "log", "-z", "--topo-order", "--reverse", f"--format={_FMT}", head, *{t[2] for t in tags})
    commits: dict[str, dict] = {}
    for rec in raw.split("\x00"):
        if not rec.strip():
            continue
        sha, parents, cdate, subject, pages, changes = rec.strip("\n").split("\x1e")
        commits[sha] = {"parents": parents.split(), "date": london_date(cdate), "subject": subject,
                        "pages": _split(pages), "changes": _split(changes)}
    return commits


def london_date(iso: str) -> str:
    """The Europe/London calendar date of a git ISO timestamp (live tags are London dates too)."""
    return dt.datetime.fromisoformat(iso).astimezone(LONDON).date().isoformat()


def _mark_ancestors(commits: dict[str, dict], start: str, owner: dict, value) -> None:
    """Give `value` to start and every ancestor of it that has no owner yet."""
    stack = [start]
    while stack:
        sha = stack.pop()
        if sha in owner or sha not in commits:
            continue
        owner[sha] = value
        stack.extend(commits[sha]["parents"])


def _brought_in_by(commits: dict[str, dict], head: str) -> dict[str, str]:
    """For each commit on main, the first-parent main commit that brought it in (its merge, or itself)."""
    line, sha = [], head
    while sha in commits:
        line.append(sha)
        sha = (commits[sha]["parents"] or [None])[0]
    owner: dict[str, str] = {}
    for main_sha in reversed(line):  # oldest first, so each commit goes to the earliest merge
        _mark_ancestors(commits, main_sha, owner, main_sha)
    return owner


def _first_live_tag(commits: dict[str, dict], tags: list[tuple[str, str, str]]) -> dict[str, tuple[str, str]]:
    """For each commit, (date, tag name) of the earliest live tag that contains it."""
    owner: dict[str, tuple[str, str]] = {}
    for date, name, sha in tags:  # earliest first
        _mark_ancestors(commits, sha, owner, (date, name))
    return owner


def _changes(c: dict) -> list[tuple[str, str]]:
    """(change type, note) per SEO-Change trailer; the type in its listed spelling when listed, the note
    falling back to the commit subject."""
    out = []
    for raw in c["changes"]:
        kind, _, note = raw.partition("|")
        if kind.strip():
            out.append((journal.canonical_type(kind) or kind.strip().lower(), note.strip() or c["subject"].strip()))
    return out


def read(repo: Path | str, branch: str = BRANCH) -> list[dict]:
    """Journal entries (source 'repo') from the repo, sorted by live date then page; each lists its commits."""
    repo = Path(repo)
    if not repo.is_dir():
        raise RepoError(f"repo not found: {repo}")
    tags = _live_tags(repo)
    head = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}").strip()
    commits = _history(repo, head, tags)
    merge_of, live_of = _brought_in_by(commits, head), _first_live_tag(commits, tags)

    groups: dict[tuple[str, str], dict] = {}  # (ref, page) -> entry being built
    for sha, c in commits.items():  # oldest first
        changes = _changes(c)
        if sha not in merge_of or not c["pages"] or not changes:
            continue
        if sha in live_of:
            (date, ref), approx = live_of[sha], 0
        else:
            date, ref, approx = commits[merge_of[sha]]["date"], f"merge/{merge_of[sha]}", 1
        for page in dict.fromkeys(journal.path_key(p) for p in c["pages"]):
            g = groups.setdefault((ref, page), {"live_date": date, "approx": approx, "page": page, "types": [],
                                                "notes": [], "source": "repo", "ref": ref, "commits": []})
            for kind, note in changes:
                g["types"] += [kind] if kind not in g["types"] else []
                g["notes"] += [note] if note not in g["notes"] else []
            g["commits"].append(sha)
    out = []
    for g in groups.values():
        types, notes = g.pop("types"), g.pop("notes")
        out.append({**g, "change_type": TYPE_SEP.join(types), "note": "; ".join(notes)})
    keys = ("live_date", "approx", "page", "change_type", "note", "source", "ref", "commits")
    return [{k: e[k] for k in keys} for e in sorted(out, key=lambda e: (e["live_date"], e["page"], e["ref"]))]


def sync(con: sqlite3.Connection, repo: Path | str) -> dict:
    """Make the journal's repo entries match the repo. Manual entries are untouched. Commits.
    A read that finds no entries while repo entries are stored is treated as a bad read: nothing is removed.
    unknown_types lists trailer change types that aren't on the journal's list."""
    entries = read(repo)
    keep = set()
    for e in entries:
        con.execute(
            """INSERT INTO journal(live_date, approx, page, change_type, note, source, ref)
               VALUES (?,?,?,?,?,'repo',?)
               ON CONFLICT(source, ref, page) DO UPDATE SET live_date=excluded.live_date, approx=excluded.approx,
                 change_type=excluded.change_type, note=excluded.note""",
            (e["live_date"], e["approx"], e["page"], e["change_type"], e["note"], e["ref"]))
        keep.add((e["ref"], e["page"]))
    stored = con.execute("SELECT id, ref, page FROM journal WHERE source='repo'").fetchall()
    res: dict = {"entries": len(entries), "removed": 0}
    if not entries and stored:
        res["prune_skipped"] = f"the read found no entries; kept the {len(stored)} stored repo entries"
    else:
        for r in stored:
            if (r[1], r[2]) not in keep:
                con.execute("DELETE FROM journal WHERE id=?", (r[0],))
                res["removed"] += 1
    con.commit()
    unknown = {t for e in entries for t in e["change_type"].split(TYPE_SEP) if t not in journal.CHANGE_TYPES}
    if unknown:
        res["unknown_types"] = sorted(unknown)
    return res
