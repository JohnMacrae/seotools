"""Journal repo reader (#30, ADR 0003): journal entries from a site repo's main-branch history.

A commit that changes a page carries `SEO-Page: <url or pattern>` (one or more) and `SEO-Change: <type> | <note>`
trailers. Its live date is the earliest `live/<YYYY-MM-DD>` tag containing it; a commit no live tag contains gets
the date of the main-branch commit that brought it in (its merge, or itself if committed straight to main), marked
approx. Commits shipped in the same deploy (tag, or merge when untagged) for the same page become one entry, with
ref = the tag name or `merge/<sha>`. A commit missing either trailer is skipped.

`sync` makes the journal's repo entries match the repo: upsert on source+ref+page, and drop repo entries that no
longer come out of the read (an approx entry whose commit has since been tagged). Re-reading never duplicates."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
import subprocess
from pathlib import Path

from .journal import _path

BRANCH = "main"
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


def read(repo: Path | str, branch: str = BRANCH) -> list[dict]:
    """Journal entries (source 'repo') from the repo, sorted by live date then page; each lists its commits."""
    repo = Path(repo)
    if not repo.is_dir():
        raise RepoError(f"repo not found: {repo}")
    tags = _live_tags(repo)
    head = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}").strip()
    raw = _git(repo, "log", "-z", "--topo-order", "--reverse", f"--format={_FMT}", head, *{t[2] for t in tags})
    graph: dict[str, dict] = {}
    for rec in raw.split("\x00"):
        if not rec.strip():
            continue
        sha, parents, cdate, subject, pages, changes = rec.strip("\n").split("\x1e")
        graph[sha] = {"parents": parents.split(), "date": cdate[:10], "subject": subject,
                      "pages": _split(pages), "changes": _split(changes)}

    def claim(start: str, owner: dict, value) -> None:
        stack = [start]
        while stack:
            x = stack.pop()
            if x in owner or x not in graph:
                continue
            owner[x] = value
            stack.extend(graph[x]["parents"])

    # which main-branch commit brought each commit in: walk the first-parent line oldest first
    chain, x = [], head
    while x in graph:
        chain.append(x)
        x = (graph[x]["parents"] or [None])[0]
    merged_by: dict[str, str] = {}
    for m in reversed(chain):
        claim(m, merged_by, m)
    live: dict[str, tuple[str, str]] = {}
    for date, name, sha in tags:  # earliest first, so each commit keeps its earliest live tag
        claim(sha, live, (date, name))

    groups: dict[tuple[str, str], dict] = {}
    for sha, c in graph.items():  # oldest first
        if sha not in merged_by or not c["pages"] or not c["changes"]:
            continue
        kind, _, note = c["changes"][0].partition("|")
        kind, note = kind.strip().lower(), note.strip() or c["subject"].strip()
        if not kind:
            continue
        if sha in live:
            date, ref, approx = *live[sha], 0
        else:
            m = merged_by[sha]
            date, ref, approx = graph[m]["date"], f"merge/{m}", 1
        for page in dict.fromkeys(_path(p) for p in c["pages"]):
            g = groups.setdefault((ref, page), {"live_date": date, "approx": approx, "page": page, "types": [],
                                                "notes": [], "source": "repo", "ref": ref, "commits": []})
            g["types"] += [kind] if kind not in g["types"] else []
            g["notes"] += [note] if note not in g["notes"] else []
            g["commits"].append(sha)
    out = []
    for g in groups.values():
        types, notes = g.pop("types"), g.pop("notes")
        out.append({**g, "change_type": ", ".join(types), "note": "; ".join(notes)})
    keys = ("live_date", "approx", "page", "change_type", "note", "source", "ref", "commits")
    return [{k: e[k] for k in keys} for e in sorted(out, key=lambda e: (e["live_date"], e["page"], e["ref"]))]


def sync(con: sqlite3.Connection, repo: Path | str) -> dict:
    """Make the journal's repo entries match the repo. Manual entries are untouched. Commits."""
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
    for r in con.execute("SELECT id, ref, page FROM journal WHERE source='repo'").fetchall():
        if (r[1], r[2]) not in keep:
            con.execute("DELETE FROM journal WHERE id=?", (r[0],))
    con.commit()
    return {"entries": len(entries)}
