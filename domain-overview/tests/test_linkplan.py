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


def test_cap_exact_swaps_or_drops():
    S = {T["url"]: T}
    ls = [{"src": U(f"/s{i}"), "dst": T["url"], "anchor": "sincerity bridal", "score": 10 - i,
           "context": "try sincerity bridal or our lace gowns", "alt_anchor": "lace gowns" if i else None}
          for i in range(4)]
    keep, warn = lp.cap_exact(ls, S)
    assert sum(l["anchor_type"] == "exact" for l in keep) <= max(1, int(4 * lp.EXACT_SHARE))
    assert any(l["anchor"] == "lace gowns" for l in keep)
