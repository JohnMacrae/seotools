"""Journal repo reader (#30, ADR 0003): SEO-Page / SEO-Change trailers on main, live/<date> tags, approx merge dates."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import gsc, journal, journal_repo, main  # noqa: E402


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.n = 0

    def git(self, *args, date="2026-10-01T12:00:00+00:00"):
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
               "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(self.path)}
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, msg: str, date="2026-10-01T12:00:00+00:00") -> str:
        self.n += 1
        (self.path / f"f{self.n}.txt").write_text(str(self.n))
        self.git("add", ".")
        self.git("commit", "-q", "-m", msg, date=date)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, ref: str = "HEAD"):
        self.git("tag", name, ref)


def trailers(page: str, change: str) -> str:
    return f"\n\nSEO-Page: {page}\nSEO-Change: {change}"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "site").mkdir()
    return Repo(tmp_path / "site")


def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    journal.ensure_table(c)
    return c


def rows(c):
    return [dict(r) for r in c.execute(
        "SELECT live_date, approx, page, change_type, note, source, ref FROM journal ORDER BY live_date, page")]


def test_tagged_commits_get_earliest_live_tag(repo):
    repo.commit("init")
    sha = repo.commit("New MOB title" + trailers("https://serenitybrides.com/collections/mob/", "title/meta | New MOB title"))
    repo.tag("live/2026-10-03")
    repo.commit("later")
    repo.tag("live/2026-10-09")
    repo.tag("not-a-live-tag")
    entries = journal_repo.read(repo.path)
    assert entries == [{"live_date": "2026-10-03", "approx": 0, "page": "/collections/mob", "change_type": "title/meta",
                        "note": "New MOB title", "source": "repo", "ref": "live/2026-10-03", "commits": [sha]}]


def test_untagged_merge_uses_merge_date_with_approx(repo):
    repo.commit("init", date="2026-09-01T10:00:00+00:00")
    repo.git("checkout", "-q", "-b", "feature")
    a = repo.commit("Headings" + trailers("/dresses/lace", "headings | H2s on lace page"), date="2026-09-20T10:00:00+00:00")
    b = repo.commit("Copy" + trailers("/dresses/lace", "copy | Longer intro"), date="2026-09-21T10:00:00+00:00")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "Merge feature", date="2026-09-25T18:00:00+00:00")
    merge = repo.git("rev-parse", "HEAD")
    [e] = journal_repo.read(repo.path)
    assert e["live_date"] == "2026-09-25" and e["approx"] == 1 and e["page"] == "/dresses/lace"
    assert e["ref"] == f"merge/{merge}"
    assert e["change_type"] == "headings, copy" and e["note"] == "H2s on lace page; Longer intro"
    assert e["commits"] == [a, b]


def test_same_deploy_same_page_merges_other_pages_and_deploys_stay_apart(repo):
    repo.commit("A" + trailers("/a", "copy | one"))
    repo.commit("A2" + trailers("/a/", "copy | two"))
    repo.commit("B" + trailers("/b", "technical | schema"))
    repo.tag("live/2026-10-02")
    repo.commit("A3" + trailers("/a", "internal links | links in"))
    repo.tag("live/2026-10-05")
    got = [(e["live_date"], e["page"], e["change_type"], e["note"]) for e in journal_repo.read(repo.path)]
    assert got == [("2026-10-02", "/a", "copy", "one; two"), ("2026-10-02", "/b", "technical", "schema"),
                   ("2026-10-05", "/a", "internal links", "links in")]


def test_pattern_entry_and_several_pages_in_one_commit(repo):
    repo.commit("Template\n\nSEO-Page: /dresses/*\nSEO-Page: /\nSEO-Change: technical | Also-in line")
    repo.tag("live/2026-10-04")
    entries = journal_repo.read(repo.path)
    assert [(e["page"], e["note"]) for e in entries] == [("/", "Also-in line"), ("/dresses/*", "Also-in line")]
    c = con()
    journal_repo.sync(c, repo.path)
    assert journal.matches(next(r for r in rows(c) if r["page"] == "/dresses/*")["page"], "/dresses/lace")


def test_commits_without_both_trailers_or_off_main_are_ignored(repo):
    repo.commit("no trailers")
    repo.commit("page only\n\nSEO-Page: /x")
    repo.commit("type without note" + trailers("/y", "copy"))
    repo.git("checkout", "-q", "-b", "unmerged")
    repo.commit("not on main" + trailers("/z", "copy | z"))
    repo.git("checkout", "-q", "main")
    got = [(e["page"], e["change_type"], e["note"]) for e in journal_repo.read(repo.path)]
    assert got == [("/y", "copy", "type without note")]  # note falls back to the subject


def test_resync_never_duplicates_and_approx_becomes_tagged(repo):
    repo.commit("init")
    repo.commit("A" + trailers("/a", "copy | one"), date="2026-10-01T09:00:00+00:00")
    c = con()
    assert journal_repo.sync(c, repo.path)["entries"] == 1
    assert journal_repo.sync(c, repo.path)["entries"] == 1
    [r] = rows(c)
    assert r["approx"] == 1 and r["live_date"] == "2026-10-01"
    repo.tag("live/2026-10-06")
    journal_repo.sync(c, repo.path)
    journal_repo.sync(c, repo.path)
    [r] = rows(c)
    assert r["approx"] == 0 and r["live_date"] == "2026-10-06" and r["ref"] == "live/2026-10-06"


def test_sync_leaves_manual_entries_alone(repo):
    repo.commit("A" + trailers("/a", "copy | one"))
    c = con()
    journal.insert(c, {"live_date": "2026-10-01", "page": "/gbp", "change_type": "technical", "ref": "m1"})
    journal_repo.sync(c, repo.path)
    journal_repo.sync(c, repo.path)
    assert sorted(r["source"] for r in rows(c)) == ["manual", "repo"]


def test_empty_read_keeps_stored_repo_entries(repo, tmp_path):
    repo.commit("A" + trailers("/a", "copy | one"))
    c = con()
    journal_repo.sync(c, repo.path)
    (tmp_path / "bare").mkdir()
    empty = Repo(tmp_path / "bare")  # e.g. a mount with history but no trailers left in it
    empty.commit("init")
    res = journal_repo.sync(c, empty.path)
    assert res["entries"] == 0 and res["removed"] == 0 and "prune_skipped" in res
    assert [r["page"] for r in rows(c)] == ["/a"]


def test_change_types_match_the_journal_list(repo):
    repo.commit("A" + trailers("/a", "Title/Meta | caps"))
    repo.commit("B" + trailers("/b", "gbp | profile"))
    repo.commit("C" + trailers("/c", "contnet | typo"))
    got = {e["page"]: e["change_type"] for e in journal_repo.read(repo.path)}
    assert got == {"/a": "title/meta", "/b": "GBP", "/c": "contnet"}
    res = journal_repo.sync(con(), repo.path)
    assert res["unknown_types"] == ["contnet"]


def test_every_seo_change_trailer_is_kept(repo):
    repo.commit("Two changes\n\nSEO-Page: /a\nSEO-Change: copy | longer intro\nSEO-Change: headings | new H2s")
    [e] = journal_repo.read(repo.path)
    assert e["change_type"] == "copy, headings" and e["note"] == "longer intro; new H2s"


def test_merge_date_is_the_london_date(repo):
    repo.commit("init", date="2026-09-01T10:00:00+00:00")
    # 23:30 UTC on 25 Sep is 00:30 BST on 26 Sep
    repo.commit("Late" + trailers("/late", "copy | late"), date="2026-09-25T23:30:00+00:00")
    [e] = journal_repo.read(repo.path)
    assert e["live_date"] == "2026-09-26" and e["approx"] == 1


def test_not_a_repo_raises(tmp_path):
    with pytest.raises(journal_repo.RepoError):
        journal_repo.read(tmp_path / "missing")


def test_daily_job_reads_repo_after_gsc_top_up(tmp_path, repo, monkeypatch):
    repo.commit("A" + trailers("/a", "copy | one"))
    repo.tag("live/2026-10-02")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    order = []
    monkeypatch.setattr(gsc, "sync_store", lambda con: order.append("gsc"))
    real = journal_repo.sync
    monkeypatch.setattr(journal_repo, "sync", lambda con, path: (order.append("repo"), real(con, path))[1])
    with TestClient(main.app) as c:
        assert c.post("/api/sites", json={"name": "Repo Shop", "repo_path": str(repo.path)}).status_code == 200
        assert c.post("/api/sites", json={"name": "Bad Repo", "repo_path": str(tmp_path / "nope")}).status_code == 200
        out = main.run_daily_job()
        assert out["serenity"] == "ok" and out["repo-shop"] == "ok" and out["bad-repo"].startswith("error")
        assert order[:3] == ["gsc", "gsc", "repo"]  # serenity has no repo; repo-shop: top-up first, then repo
        got = c.get("/api/journal?site=repo-shop").json()["entries"]
        assert [(e["page"], e["source"], e["live_date"]) for e in got] == [("/a", "repo", "2026-10-02")]
        main.run_daily_job()
        assert len(c.get("/api/journal?site=repo-shop").json()["entries"]) == 1
        assert c.get("/api/journal?site=serenity").json()["entries"] == []


def test_sync_endpoint(tmp_path, repo, monkeypatch):
    repo.commit("A" + trailers("/a", "copy | one"))
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    with TestClient(main.app) as c:
        assert c.post("/api/journal/sync?site=serenity").status_code == 400  # no repo_path set
        c.post("/api/sites", json={"name": "Repo Shop", "repo_path": str(repo.path)})
        r = c.post("/api/journal/sync?site=repo-shop")
        assert r.status_code == 200 and r.json()["entries"] == 1
