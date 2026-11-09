import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.health import health  # noqa: E402

U = lambda p: "https://e.com" + p


def page(path, status=200, depth=1, in_sitemap=1, **kw):
    d = dict(url=U(path), status=status, redirect_to=None, content_type="text/html", canonical=None,
             meta_robots=None, title=f"T{path}", h1="H", meta_description="d", word_count=100,
             depth=depth, in_sitemap=in_sitemap, external_links=0)
    d.update(kw)
    return d


def link(src, dst, placement="main"):
    return dict(src=U(src), dst=U(dst), anchor="a" + src, placement=placement, nofollow=0)  # anchor varies so tiny graphs have no sitewide link


def test_graph():
    pages = [
        page("/", depth=0),
        page("/a"),
        page("/b"),                      # only linked from nav -> contextual orphan, not orphan
        page("/c"),                      # no inlinks -> orphan
        page("/gone", status=404, in_sitemap=0),
        page("/old", status=301, redirect_to=U("/a"), in_sitemap=0),
        page("/n", meta_robots="noindex", title="T/a"),
    ]
    links = [
        link("/", "/a"), link("/", "/b", "nav"), link("/", "/n"),
        link("/a", "/gone"), link("/a", "/old"), link("/a", "/"),
        link("/b", "/a", "footer"),     # b has no main/body outlinks -> dead end
        link("/n", "/a"),
    ]
    h = health(pages, links)
    assert [r["url"] for r in h["orphans"]["rows"]] == [U("/c")]
    assert {r["url"] for r in h["contextual_orphans"]["rows"]} == {U("/b"), U("/c")}
    assert [(r["dst"], r["status"]) for r in h["broken_links"]["rows"]] == [(U("/gone"), 404)]
    assert [r["redirect_to"] for r in h["redirected_links"]["rows"]] == [U("/a")]
    assert {r["url"] for r in h["dead_ends"]["rows"]} == {U("/b"), U("/c")}
    assert [r["url"] for r in h["non_indexable"]["rows"]] == [U("/n")]
    assert h["duplicate_titles"]["count"] == 2
    assert h["totals"]["status"] == {"2xx": 5, "3xx": 1, "4xx": 1}
    assert h["orphans"]["count"] == 1
