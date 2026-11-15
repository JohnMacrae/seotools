"""Sites (#24, ADR 0004): a registry in <data>/sites.json and one SQLite file per site in <data>/sites/<slug>.db.

The active site is a context variable set per request by middleware. asyncio tasks copy the context when
created, so a background job keeps the site it started on."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextvars import ContextVar
from pathlib import Path

CURRENT: ContextVar[dict | None] = ContextVar("current_site", default=None)
FIELDS = ("slug", "name", "gsc_property", "repo_path", "dfs_location")


def _registry(base: Path) -> Path:
    return base / "sites.json"


def db_path(base: Path, slug: str) -> Path:
    return base / "sites" / f"{slug}.db"


def ensure(base: Path, legacy_db: Path) -> None:
    """First start: seed the registry with Serenity and copy the pre-sites database in as its site file."""
    reg = _registry(base)
    if reg.exists():
        return
    (base / "sites").mkdir(parents=True, exist_ok=True)
    target = db_path(base, "serenity")
    if legacy_db.is_file() and not target.exists():
        tmp = target.with_suffix(".db.tmp")
        src, dst = sqlite3.connect(legacy_db), sqlite3.connect(tmp)
        try:
            src.backup(dst)  # consistent copy even with WAL; the source is left as it was
        finally:
            dst.close()
            src.close()
        tmp.replace(target)
    seed = {"slug": "serenity", "name": "serenitybrides.com", "gsc_property": os.environ.get("GSC_SITE_URL", ""),
            "repo_path": "", "dfs_location": 2826}
    reg.write_text(json.dumps([seed], indent=1))


def load(base: Path, legacy_db: Path) -> list[dict]:
    ensure(base, legacy_db)
    return json.loads(_registry(base).read_text())


def get(base: Path, legacy_db: Path, slug: str | None) -> dict | None:
    """The site with this slug; no slug means the first (default) site."""
    allsites = load(base, legacy_db)
    if not slug:
        return allsites[0]
    return next((s for s in allsites if s["slug"] == slug), None)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def add(base: Path, legacy_db: Path, body: dict) -> dict:
    """Add a site. Raises ValueError (bad input) or KeyError (slug already exists)."""
    name = str(body.get("name") or "").strip()
    slug = slugify(str(body.get("slug") or name))
    if not name or not slug:
        raise ValueError("name is required")
    allsites = load(base, legacy_db)
    if any(s["slug"] == slug for s in allsites):
        raise KeyError(slug)
    try:
        loc = int(body.get("dfs_location") or 2826)
    except (TypeError, ValueError):
        raise ValueError("dfs_location must be a number")
    site = {"slug": slug, "name": name, "gsc_property": str(body.get("gsc_property") or "").strip(),
            "repo_path": str(body.get("repo_path") or "").strip(), "dfs_location": loc}
    allsites.append(site)
    _registry(base).write_text(json.dumps(allsites, indent=1))
    return site
