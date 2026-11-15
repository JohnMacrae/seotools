import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dfs, linkgraph, llm, pagegap  # noqa: E402

U = lambda p: "https://e.com" + p


def page(path, title="T", h1="H", status=200):
    return dict(url=U(path), status=status, title=title, h1=h1)


def spage(path, line="bridal", title="T"):
    return dict(url=U(path), line=line, title=title, indexable=True, pagerank=10)


PAGES = [page("/", "Wedding Dresses Colchester | E"), page("/collections/bridal", "Bridal"),
         page("/collections/mob", "Mother of the Bride"), page("/blog/exists", "Exists"),
         page("/blog/a", "A"), page("/blog/b", "B")]
STRENGTH = {"pages": [spage("/", "general", "Wedding Dresses Colchester | E"), spage("/collections/bridal"),
                      spage("/collections/mob", "mob"), spage("/blog/exists"), spage("/blog/a"), spage("/blog/b", "mob")]}
GSC = [
    {"page": U("/blog/a"), "query": "cheap wedding dresses", "clicks": 0, "impressions": 90, "position": 35.0},
    {"page": U("/blog/a"), "query": "bridal gowns", "clicks": 3, "impressions": 300, "position": 4.0},  # covered
    {"page": U("/blog/a"), "query": "tiny query", "clicks": 0, "impressions": 5, "position": 40.0},   # too few impressions
    {"page": U("/blog/b"), "query": "mother of the bride shoes", "clicks": 0, "impressions": 60, "position": 8.0},  # line mismatch? b is mob -> no
    {"page": U("/collections/bridal"), "query": "mob dresses", "clicks": 0, "impressions": 60, "position": 9.0},  # mob query on bridal page
]


def stub_ideas(monkeypatch, items):
    async def post_path(client, p, task, mock_name=None):
        assert p == "dataforseo_labs/google/keyword_ideas/live" and task["location_code"] == 2826
        return {"items": items}, 0.02
    monkeypatch.setattr(dfs, "post_path", post_path)
    monkeypatch.setattr(dfs, "LOGIN", "x")
    monkeypatch.setattr(dfs, "PASSWORD", "y")


def idea(kw, vol, kd=None):
    return {"keyword": kw, "keyword_info": {"search_volume": vol}, "keyword_properties": {"keyword_difficulty": kd},
            "search_intent_info": {"main_intent": "commercial"}}


def run(comparison=None):
    return asyncio.run(pagegap.build_gaps(PAGES, STRENGTH, GSC, comparison))


def test_gsc_candidates_rules():
    S = {p["url"]: p for p in STRENGTH["pages"]}
    gaps, agg = pagegap.gsc_candidates(GSC, S)
    assert set(gaps) == {"cheap wedding dresses", "mob dresses"}
    assert gaps["cheap wedding dresses"]["position"] == 35.0 and gaps["cheap wedding dresses"]["page"] == U("/blog/a")
    assert agg["bridal gowns"]["covered"]


def test_covered_filter(monkeypatch):
    monkeypatch.setattr(llm, "configured", lambda: False)
    stub_ideas(monkeypatch, [idea("bridal gowns", 500), idea("wedding dresses colchester", 900),
                             idea("dress in colchester wedding the", 100), idea("how to choose a wedding veil", 300, 20)])
    out = run()
    # clicked top-10 GSC row, and all words in the home title: both dropped; word order/stopwords ignored
    assert out["dropped_covered"] == 3
    kws = {k for p in out["pages"] for k in [p["primary_keyword"], *p["secondary_keywords"]]}
    assert "bridal gowns" not in kws and "wedding dresses colchester" not in kws and "how to choose a wedding veil" in kws
    assert out["llm_used"] is False and out["sources"]["ideas"] == 4  # same items from each line call are deduped


def test_competitor_source_and_proxy_volume(monkeypatch):
    monkeypatch.setattr(llm, "configured", lambda: False)
    monkeypatch.setattr(dfs, "LOGIN", "")
    monkeypatch.setattr(dfs, "PASSWORD", "")
    comp = {"missing": [{"keyword": "prom dresses essex", "volume": 200, "kd": 50, "best_url": "https://c.com/p",
                         "competitors": {"c.com": {"position": 3, "url": "https://c.com/p"}}}]}
    out = run(comp)
    by = {p["primary_keyword"]: p for p in out["pages"]}
    assert by["prom dresses essex"]["volume"] == 200 and not by["prom dresses essex"]["volume_is_proxy"]
    assert by["prom dresses essex"]["competitor_urls"] == ["https://c.com/p"]
    assert by["prom dresses essex"]["sources"] == ["competitor"]
    # GSC-only keyword: 90 impressions / 3 = 30 a month, flagged as proxy, GSC boost applied
    g = by["cheap wedding dresses"]
    assert g["volume"] == 30 and g["volume_is_proxy"] and g["sources"] == ["gsc"]
    assert g["score"] == round(30 * 0.5 * linkgraph.LINE_WEIGHTS["bridal"] * 1.5, 1)
    assert by["prom dresses essex"]["score"] == round(200 * 0.5 * linkgraph.LINE_WEIGHTS["bridal"], 1)
    assert [p["score"] for p in out["pages"]] == sorted((p["score"] for p in out["pages"]), reverse=True)
    assert any("credentials" in w for w in out["warnings"])


def test_model_output_is_validated(monkeypatch):
    monkeypatch.setattr(llm, "configured", lambda: True)
    stub_ideas(monkeypatch, [idea("wedding lace veil", 400, 20), idea("wedding veil length", 100, 30), idea("wedding hen party ideas", 50, 10)])

    async def ask_json(client, system, user, max_tokens=8000):
        if "Product line: bridal" not in user:
            return {"pages": [], "improve": []}, 0.01
        return {"pages": [
            {"title": "Veils", "slug": "/blog/veils", "page_type": "guide", "primary_keyword": "wedding lace veil",
             "secondary_keywords": ["wedding veil length", "not in list"], "why": "w",
             "hub": "/collections/bridal", "link_from": ["/blog/a", "/blog/nope", U("/blog/b")]},
            {"title": "Dup keyword", "slug": "/blog/veils-2", "primary_keyword": "wedding lace veil"},        # keyword already used
            {"title": "Existing", "slug": "/blog/exists", "primary_keyword": "wedding hen party ideas"},      # slug exists
            {"title": "Bad prefix", "slug": "/shop/x", "primary_keyword": "wedding hen party ideas"},
            {"title": "Hub", "slug": "/faq/hen", "primary_keyword": "wedding hen party ideas", "page_type": "weird",
             "hub": "/collections/ghost", "link_from": []},
        ], "improve": [{"url": "/blog/a", "keywords": ["wedding veil length", "wedding lace veil"], "why": "close"},
                       {"url": "/blog/ghost", "keywords": ["x"], "why": ""}]}, 0.05
    monkeypatch.setattr(llm, "ask_json", ask_json)
    out = run()
    by = {p["slug"]: p for p in out["pages"]}
    assert set(by) == {"/blog/veils", "/faq/hen"}
    v = by["/blog/veils"]
    assert v["secondary_keywords"] == ["wedding veil length"] and v["volume"] == 500
    assert v["hub"] == U("/collections/bridal") and v["link_from"] == [U("/blog/a"), U("/blog/b")]
    assert by["/faq/hen"]["hub"] is None and by["/faq/hen"]["page_type"] == "guide"
    assert out["improve"] == []  # its keywords were all taken by a proposed page; the unknown URL is dropped
    assert out["llm_used"] and out["model"] == llm.MODEL and out["cost"] >= 0.05
    assert any("already exists" in w for w in out["warnings"])
    assert any("blog/nope" in w for w in out["warnings"])


def test_improve_item_kept(monkeypatch):
    monkeypatch.setattr(llm, "configured", lambda: True)
    stub_ideas(monkeypatch, [idea("wedding lace veil", 400, 20)])

    async def ask_json(client, system, user, max_tokens=8000):
        return {"pages": [], "improve": [{"url": U("/blog/a"), "keywords": ["wedding lace veil"], "why": "nearly"}]}, 0.0
    monkeypatch.setattr(llm, "ask_json", ask_json)
    out = run()
    assert out["improve"] and out["improve"][0]["url"] == U("/blog/a") and out["pages"] == []


def test_candidate_volume_source():
    cands = {"a kw": {"volume": 10}, "b kw": {"volume": None, "impressions": 90}, "c kw": {"volume": 5}}
    pagegap.apply_target_volumes(cands, {"a kw": 1600})
    assert (cands["a kw"]["volume"], cands["a kw"]["volume_source"]) == (1600, "targets")
    assert cands["b kw"]["volume_source"] == "gsc" and cands["b kw"]["volume"] is None
    assert (cands["c kw"]["volume"], cands["c kw"]["volume_source"]) == (5, "labs")
