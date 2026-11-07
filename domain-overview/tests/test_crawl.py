import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crawl import parse_robots, parse_page  # noqa: E402

BASE = "https://example.com/dresses/a/"


def links_by_dst(page):
    return {l["dst"]: l for l in page["links"]}


def test_placements():
    html = """<html><body>
    <header><a href="/h">H</a></header>
    <nav><a href="/in-nav">N</a></nav>
    <div role="navigation"><a href="/role-nav">RN</a></div>
    <main><a href="/in-main">M</a><nav><a href="/nested-nav">NN</a></nav></main>
    <aside><a href="/in-aside">A</a></aside>
    <footer><a href="/in-footer">F</a></footer>
    </body></html>"""
    d = links_by_dst(parse_page(html, BASE))
    got = {k.rsplit("/", 1)[1]: v["placement"] for k, v in d.items()}
    assert got == {"h": "header", "in-nav": "nav", "role-nav": "nav", "in-main": "main",
                   "nested-nav": "nav", "in-aside": "aside", "in-footer": "footer"}
    body = parse_page('<body><div><a href="/x">x</a></div></body>', BASE)
    assert body["links"][0]["placement"] == "body"


def test_anchor_alt_and_nofollow():
    p = parse_page('<a href="/a"><img src="x.jpg" alt="A dress"></a><a href="/b" rel="nofollow noopener">  Hello \n world </a>', BASE)
    d = links_by_dst(p)
    assert d["https://example.com/a"]["anchor"] == "A dress"
    assert d["https://example.com/b"]["anchor"] == "Hello world"
    assert d["https://example.com/b"]["nofollow"] == 1
    assert d["https://example.com/a"]["nofollow"] == 0


def test_url_resolution_and_external():
    html = ('<a href="../b/?x=1#frag">r</a><a href="#top">t</a><a href="mailto:a@b.c">m</a>'
            '<a href="https://WWW.example.com/c#z">www</a><a href="https://other.com/">o</a><a href="/a">dup</a><a href="/a">dup</a>')
    p = parse_page(html, BASE, site_host="example.com")
    assert [l["dst"] for l in p["links"]] == [
        "https://example.com/dresses/b/", "https://example.com/c", "https://example.com/a", "https://example.com/a"]
    assert p["external_links"] == 1


def test_main_text_excludes_chrome():
    html = """<body><nav>NAVTEXT</nav><main><h1>Head</h1><p>Real words here</p>
    <script>var JS=1</script><style>.x{}</style></main><footer>FOOTTEXT</footer></body>"""
    p = parse_page(html, BASE)
    assert p["text"] == "Head Real words here"
    assert p["word_count"] == 4


def test_body_text_without_main():
    p = parse_page("<body><header>HDR</header><p>Content</p><aside>SIDE</aside><footer>F</footer></body>", BASE)
    assert p["text"] == "Content"


def test_metadata():
    html = """<html><head><title> My  Title </title><meta name="robots" content="noindex, follow">
    <meta name="description" content="Desc here"><link rel="canonical" href="/canon/"></head>
    <body><h1>First <em>H1</em></h1><h1>Second</h1></body></html>"""
    p = parse_page(html, BASE)
    assert p["title"] == "My Title"
    assert p["h1"] == "First H1"
    assert p["canonical"] == "https://example.com/canon/"
    assert p["meta_robots"] == "noindex, follow"
    assert p["meta_description"] == "Desc here"


def test_unclosed_tags_do_not_break_stack():
    html = """<main><ul><li>one<li>two <a href="/in-main">x</a><p>unclosed para</ul></main>
    <footer><a href="/in-footer">f</a></footer><a href="/after">a</a>"""
    p = parse_page(html, BASE)
    d = {l["dst"].rsplit("/", 1)[1]: l["placement"] for l in p["links"]}
    assert d == {"in-main": "main", "in-footer": "footer", "after": "body"}


def test_unclosed_anchor_and_void_tags():
    p = parse_page('<main><a href="/one">One<br><a href="/two">Two<img src=x></main>', BASE)
    assert [(l["dst"].rsplit("/", 1)[1], l["anchor"], l["placement"]) for l in p["links"]] == [
        ("one", "One", "main"), ("two", "Two", "main")]


def test_robots():
    dis, sm = parse_robots("User-agent: Googlebot\nDisallow: /g\n\nUser-agent: *\nDisallow: /private\nDisallow:\nSitemap: https://e.com/s.xml\n")
    assert dis == ["/private"] and sm == ["https://e.com/s.xml"]
