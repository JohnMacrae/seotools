import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.gsc import normalise as gsc_normalise  # noqa: E402
from app.linkgraph import (contextual_links, ctr_at, pagerank, product_line, pr_scores,  # noqa: E402
                           sitewide_links, strength)

U = lambda p: "https://e.com" + p


def page(path, status=200, **kw):
    d = dict(url=U(path), status=status, redirect_to=None, content_type="text/html", canonical=None,
             meta_robots=None, title=f"T{path}", h1="H", meta_description="d", word_count=100, depth=1,
             in_sitemap=1, external_links=0)
    d.update(kw)
    return d


def link(src, dst, placement="main", anchor="a"):
    return dict(src=U(src), dst=U(dst), anchor=anchor, placement=placement, nofollow=0)


def test_sitewide_rule():
    pages = [page(p) for p in ("/", "/a", "/b", "/c", "/privacy")]
    # /privacy linked from body on 4 of 5 pages (cookie banner): sitewide despite body placement
    links = [link(s, "/privacy", "body", "Privacy") for s in ("/", "/a", "/b", "/c")]
    links += [link("/", "/a"), link("/a", "/b", "nav"), link("/b", "/c", "footer")]
    assert sitewide_links(pages, links) == {(U("/privacy"), "privacy")}
    ctx = {(l["src"], l["dst"]) for l in contextual_links(pages, links)}
    assert ctx == {(U("/"), U("/a"))}


def test_pagerank_weights():
    # hub links to x contextually and to y from the footer; both link back. x must beat y.
    pages = [page(p) for p in ("/hub", "/x", "/y", "/z", "/w")]
    links = [link("/hub", "/x"), link("/hub", "/y", "footer"), link("/x", "/hub"), link("/y", "/hub"),
             link("/z", "/hub"), link("/w", "/z", "nav", "to z")]
    pr = pagerank(pages, links)
    assert abs(sum(pr.values()) - 1) < 1e-6
    assert pr[U("/x")] > pr[U("/y")] > 0
    assert pr[U("/hub")] == max(pr.values())
    s = pr_scores(pr)
    assert s[U("/hub")] == 100 and all(0 <= v <= 100 for v in s.values())


def test_pagerank_canonical_and_noindex():
    pages = [page("/a"), page("/dup", canonical=U("/a")), page("/n", meta_robots="noindex"), page("/b")]
    links = [link("/b", "/dup"), link("/b", "/n"), link("/a", "/b")]
    pr = pagerank(pages, links)
    assert set(pr) == {U("/a"), U("/b")}
    assert pr[U("/a")] > pr[U("/b")] * 0.9  # b's weight flows to a via the canonical, not into noindex


def test_product_line():
    assert product_line(U("/faq/how-to-choose-a-mother-of-the-bride-outfit"), "How to choose", "H", []) == "mob"
    assert product_line(U("/collections/bridesmaids/pink"), None, None, []) == "bridesmaids"
    assert product_line(U("/collections/mother-of-the-bride/kevan-jon"), None, None, []) == "mob"
    assert product_line(U("/collections/accessories"), None, None, []) == "accessories"
    kj = [U("/collections/mother-of-the-bride"), U("/collections/mother-of-the-bride/kevan-jon"),
          U("/collections/mother-of-the-bride/kevan-jon")]
    assert product_line(U("/dresses/kevan-jon-mila-navy"), "Kevan Jon Mila", "H", kj) == "mob"
    assert product_line(U("/dresses/x"), "X", "X", []) == "bridal"
    tie = [U("/collections/bridal/mori-lee"), U("/collections/bridesmaids/pink"), U("/collections/bridesmaids")]
    assert product_line(U("/dresses/x"), "X", "X", tie) == "bridesmaids"  # tie: more root links
    assert product_line(U("/blog/bridesmaid-colours"), "Bridesmaid colours", None, []) == "bridesmaids"
    assert product_line(U("/blog/summer"), "Summer", "Summer weddings", []) == "bridal"
    assert product_line(U("/blog/mob-guide"), "Guide for the MOB", None, []) == "mob"
    assert product_line(U("/about"), "About", None, []) == "general"
    assert product_line(U("/"), "Home", None, []) == "general"


def test_ctr_curve():
    assert ctr_at(3) == .11 and ctr_at(4.2) == .08 and ctr_at(15) == .01 and ctr_at(25) == .005


def test_opportunity_and_flags():
    pages = [page("/", depth=0), page("/collections/bridal"), page("/collections/mother-of-the-bride"),
             page("/collections/mother-of-the-bride/kevan-jon")]
    links = [link("/", "/collections/bridal"), link("/", "/collections/mother-of-the-bride"),
             link("/collections/mother-of-the-bride", "/collections/mother-of-the-bride/kevan-jon")]
    gp = [dict(page=U("/collections/bridal"), clicks=5, impressions=1000, ctr=.005, position=8.0),
          dict(page=U("/old-page"), clicks=2, impressions=300, ctr=.01, position=5.0)]
    gr = [dict(page=U("/collections/bridal"), query="mother of the bride dresses", clicks=3, impressions=800, ctr=.004, position=8.0),
          dict(page=U("/collections/bridal"), query="wedding dresses", clicks=2, impressions=200, ctr=.01, position=2.0)]
    s = strength(pages, links, gp, gr)
    bridal = next(p for p in s["pages"] if p["url"] == U("/collections/bridal"))
    # only the position-8 query counts: 800 * (0.11 - 0.03) = 64
    # 800 impr at pos 8, 3 clicks vs 24 expected: intent factor (3+2)/(24+2)
    assert bridal["potential"] == round(800 * (0.11 - 0.03) * 5 / 26, 2)
    assert abs(bridal["opportunity"] - bridal["potential"] / (1 + bridal["pagerank"] / 10)) < 0.01
    assert bridal["rank_in_line"] == 1
    assert s["pages"][0]["url"] == bridal["url"]
    assert all(p["opportunity"] == 0 for p in s["pages"] if p is not bridal)
    assert [w["query"] for w in s["wrong_page"]] == ["mother of the bride dresses"]
    assert s["wrong_page"][0]["suggested"] == U("/collections/mother-of-the-bride")
    assert [u["page"] for u in s["gsc_unmatched"]] == [U("/old-page")]
    assert s["lines"]["bridal"]["impressions"] == 1000 and s["lines"]["mob"]["pages"] == 2


def test_gsc_normalise():
    n = lambda u, h="e.com": gsc_normalise(u, h)
    assert n("http://www.e.com/a/b/?x=1#f") == "https://e.com/a/b"
    assert n("https://e.com/") == "https://e.com/"
    assert n("https://E.com") == "https://e.com/"
    assert n("https://e.com/a//") == "https://e.com/a"
    assert n("https://e.com/a", "www.e.com") == "https://www.e.com/a"
    assert n("https://other.com/a") == "https://other.com/a"
    assert n("ftp://e.com/a") is None


def test_intent_factor_discounts_navigational_mismatch():
    from app.linkgraph import uplift
    # "bustle": huge impressions, zero clicks at pos 7 -> almost nothing
    assert uplift(89720, 0, 7.4) < 5
    # small query with no clicks yet keeps meaningful weight
    assert uplift(50, 0, 8) > 0.5 * 50 * (0.11 - 0.03)
    # query clicking at/above expected rate keeps full weight
    assert uplift(1000, 40, 7) == 1000 * (0.11 - 0.04)


def test_line_weight_and_discontinued():
    from app.linkgraph import is_discontinued, LINE_WEIGHTS
    assert is_discontinued("/collections/mother-of-the-bride/jacques-vert/")
    assert is_discontinued("/dresses/jacques-vert-jv-ebony")
    assert is_discontinued("/dresses/disney-fairytale-weddings-by-allure-ariel-2021-zip-back")
    assert is_discontinued("/collections/bridal/sophia-tolli") and is_discontinued("/dresses/sophia-tolli-elsa-y3145")
    assert not is_discontinued("/collections/mother-of-the-bride/john-charles")
    assert not is_discontinued("/dresses/john-charles-65432")
    assert LINE_WEIGHTS["bridal"] > LINE_WEIGHTS["mob"] > LINE_WEIGHTS["bridesmaids"]


def test_discontinued_still_gets_opportunity():
    from app.linkgraph import strength
    pages = [page("/"), page("/collections/bridal/sophia-tolli")]
    links = [link("/", "/collections/bridal/sophia-tolli")]
    gp = [dict(page=U("/collections/bridal/sophia-tolli"), clicks=5, impressions=500, ctr=.01, position=9.0)]
    gr = [dict(page=U("/collections/bridal/sophia-tolli"), query="sophia tolli", clicks=5, impressions=500, ctr=.01, position=9.0)]
    st = next(p for p in strength(pages, links, gp, gr)["pages"] if "tolli" in p["url"])
    assert st["discontinued"] and st["opportunity"] > 0
