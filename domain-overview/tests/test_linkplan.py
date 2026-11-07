from app import linkplan as lp

U = lambda p: "https://serenitybrides.com" + p
T = {"url": U("/collections/bridal/sincerity"), "h1": "Sincerity Wedding Dresses in Colchester",
     "top_queries": [{"query": "sincerity bridal"}], "line": "bridal"}
TEXT = "We stock Sincerity Bridal gowns in many sizes. Book an appointment to try them."


def test_validate_accepts_only_real_phrases():
    ok = lp.validate({"anchor": "Sincerity Bridal gowns", "in_text": True}, TEXT, T)
    assert ok["in_text"] and "Sincerity Bridal gowns" in ok["context"]
    assert lp.validate({"anchor": "made up phrase", "in_text": True}, TEXT, T) is None
    new = lp.validate({"anchor": "Sincerity range", "sentence": "See our Sincerity range too."}, TEXT, T)
    assert new and not new["in_text"]
    assert lp.validate({"anchor": "x", "skip": True}, TEXT, T) is None


def test_anchor_types():
    assert lp.anchor_type("sincerity bridal", T) == "exact"
    assert lp.anchor_type("Sincerity Bridal gowns", T) == "partial"
    assert lp.anchor_type("Serenity's own range", T) == "branded"
    assert lp.anchor_type("romantic lace gowns", T) == "descriptive"


def test_select_enforces_caps():
    S = {U(f"/t{i}"): {"line": "bridal"} for i in range(12)}
    cands = [{"src": U(f"/s{j}"), "dst": U(f"/t{i}"), "kind": "boost", "score": 100 - i - j, "reason": ""}
             for i in range(12) for j in range(12)]
    out = lp.select(cands, S)
    from collections import Counter
    assert max(Counter(c["src"] for c in out).values()) <= lp.PER_SOURCE
    assert max(Counter(c["dst"] for c in out).values()) <= lp.PER_TARGET
    assert len(out) <= lp.MAX_SUGGESTIONS


def test_no_exact_share_cap():
    assert not hasattr(lp, "EXACT_SHARE") and not hasattr(lp, "cap_exact")


ROWS = [
    {"path": "/collections/bridal/lace", "primary_keyword": "lace wedding dresses", "supporting": ["bride lace dress"],
     "product_line": "bridal"},
    {"path": "/collections/bridal/veni-infantino", "primary_keyword": "{Designer} wedding dresses",
     "supporting": ["veni infantino bridal"], "product_line": "bridal"},
]
LACE = {"url": U("/collections/bridal/lace"), "h1": "Lace", "line": "bridal", "top_queries": []}
VENI = {"url": U("/collections/bridal/veni-infantino"), "h1": "Veni Infantino Wedding Dresses in Colchester",
        "line": "bridal", "top_queries": []}
SINC = {**T, "h1": "Sincerity Wedding Dresses in Colchester"}
S = {p["url"]: p for p in (LACE, VENI, SINC)}


def test_target_map_matches_paths_and_expands_designer():
    m = lp.target_map(ROWS, S, {VENI["url"], SINC["url"]})
    assert m[LACE["url"]] == ["lace wedding dresses", "bride lace dress"]
    assert m[VENI["url"]] == ["Veni Infantino wedding dresses", "veni infantino bridal"]
    assert m[SINC["url"]] == ["Sincerity wedding dresses"]  # template: primary only
    assert U("/other") not in m


def _run(items, text, tmap, llm_data, monkeypatch, usage=None):
    import asyncio
    from collections import Counter
    from app import llm

    async def ask(*a, **k):
        return {"links": llm_data}, 0.0
    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(llm, "ask_json", ask)
    return asyncio.run(lp.anchors_for(None, U("/s"), {"text": text, "title": "t"}, items, S, asyncio.Semaphore(1),
                                      tmap, usage if usage is not None else Counter()))


def test_target_anchor_from_keywords_and_model_cannot_invent(monkeypatch):
    items = [{"src": U("/s"), "dst": LACE["url"], "kind": "boost", "score": 1, "reason": ""}]
    tmap = {LACE["url"]: ["lace wedding dresses", "bride lace dress"]}
    text = "Our bride lace dress range is lovely."
    # model invents an anchor present in the text: rejected, falls back to the first keyword (primary) via a sentence
    out, _, _ = _run(items, text, tmap, [{"dst": LACE["url"], "anchor": "range is lovely", "in_text": True}], monkeypatch)
    assert out[0]["anchor"] == "lace wedding dresses" and not out[0]["in_text"]
    # model picks an allowed keyword: honoured
    out, _, _ = _run(items, text, tmap, [{"dst": LACE["url"], "anchor": "bride lace dress", "in_text": True}], monkeypatch)
    assert out[0]["anchor"] == "bride lace dress" and out[0]["in_text"]


def test_target_anchors_vary_across_sources(monkeypatch):
    from collections import Counter
    usage = Counter()
    items = [{"src": U("/s"), "dst": LACE["url"], "kind": "boost", "score": 1, "reason": ""}]
    tmap = {LACE["url"]: ["lace wedding dresses", "bride lace dress"]}
    first, _, _ = _run(items, "x", tmap, [], monkeypatch, usage)
    second, _, _ = _run(items, "x", tmap, [], monkeypatch, usage)
    assert first[0]["anchor"] == "lace wedding dresses" and second[0]["anchor"] == "bride lace dress"


def test_no_repeated_anchor_within_source(monkeypatch):
    items = [{"src": U("/s"), "dst": LACE["url"], "kind": "boost", "score": 2, "reason": ""},
             {"src": U("/s"), "dst": SINC["url"], "kind": "boost", "score": 1, "reason": ""}]
    tmap = {LACE["url"]: ["lace gowns"]}
    text = "We sell lace gowns and Sincerity Bridal gowns."
    data = [{"dst": SINC["url"], "anchor": "Lace Gowns", "in_text": True, "alt_anchor": "Sincerity Bridal gowns"}]
    out, _, _ = _run(items, text, tmap, data, monkeypatch)
    anchors = [_k for _k in (lp._key(o["anchor"]) for o in out)]
    assert len(anchors) == len(set(anchors)) == 2 and "sincerity bridal gowns" in anchors
    # a second target page with only the same keyword is dropped, not repeated
    items2 = [{**items[0]}, {**items[0], "dst": VENI["url"], "score": 1}]
    out, _, warn = _run(items2, text, {LACE["url"]: ["lace gowns"], VENI["url"]: ["Lace gowns"]}, [], monkeypatch)
    assert len(out) == 1 and any("already used" in w for w in warn)
