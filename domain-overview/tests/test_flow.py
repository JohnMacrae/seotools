"""Visitor flow builder (#56): GA4 session paths + Search Console rows in, flow structure out."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import flow  # noqa: E402

ORG, DIRECT = "Organic Search", "Direct"


def s(pages, sessions, channel=ORG):
    return {"channel": channel, "pages": pages, "sessions": sessions}


def col(res, i):
    return [(n["page"], n["sessions"]) for n in res["columns"][i]]


def test_empty_input():
    res = flow.build([], [])
    assert res["total_sessions"] == 0
    assert res["arrivals"] == [] and res["links"] == []
    assert all(c == [] for c in res["columns"])


def test_organic_default_and_all_traffic():
    rows = [s(["/a"], 10), s(["/b"], 30, DIRECT), s(["/a"], 5, DIRECT)]
    org = flow.build(rows, [])
    assert org["total_sessions"] == 10
    assert [(a["page"], a["sessions"]) for a in org["arrivals"]] == [("/a", 10)]
    every = flow.build(rows, [], organic=False)
    assert every["total_sessions"] == 45
    assert [(a["page"], a["sessions"]) for a in every["arrivals"]] == [("/b", 30), ("/a", 15)]


def test_arrivals_ordered_by_sessions_with_share_and_exit_rate():
    rows = [s(["/a"], 20), s(["/a", "/x"], 20), s(["/b", "/x"], 60)]
    res = flow.build(rows, [])
    a = res["arrivals"]
    assert [x["page"] for x in a] == ["/b", "/a"]
    assert a[0]["share"] == 0.6 and a[0]["exit_rate"] == 0.0
    assert a[1]["share"] == 0.4 and a[1]["exit_rate"] == 0.5


def test_three_following_steps_then_exit_page():
    res = flow.build([s(["/a", "/b", "/c", "/d", "/e", "/f"], 10)], [])
    assert len(res["columns"]) == 5  # arrival, 3 following steps, exit
    assert [col(res, i) for i in range(5)] == [[("/a", 10)], [("/b", 10)], [("/c", 10)], [("/d", 10)], [("/f", 10)]]
    assert [(l["from"], l["to"]) for l in res["links"]] == [((0, "/a"), (1, "/b")), ((1, "/b"), (2, "/c")),
                                                            ((2, "/c"), (3, "/d")), ((3, "/d"), (4, "/f"))]


def test_short_session_goes_straight_to_exit():
    res = flow.build([s(["/a"], 4), s(["/a", "/b"], 6)], [])
    assert col(res, 4) == [("/b", 6), ("/a", 4)]
    links = {(l["from"], l["to"]): l["sessions"] for l in res["links"]}
    assert links == {((0, "/a"), (4, "/a")): 4, ((0, "/a"), (1, "/b")): 6, ((1, "/b"), (4, "/b")): 6}


def test_steps_under_one_percent_collapse_into_other():
    # 1000 sessions: exactly 1% (10) stays, 9 collapses, two small ones merge into one "other"
    rows = [s(["/big"], 973), s(["/edge"], 10), s(["/tiny"], 9), s(["/tiny2"], 8)]
    res = flow.build(rows, [])
    assert col(res, 0) == [("/big", 973), ("/edge", 10), (flow.OTHER, 17)]
    assert col(res, 4) == [("/big", 973), ("/edge", 10), (flow.OTHER, 17)]
    assert {(l["from"], l["to"]): l["sessions"] for l in res["links"]}[((0, flow.OTHER), (4, flow.OTHER))] == 17
    # the arrivals table keeps every page
    assert [a["page"] for a in res["arrivals"]] == ["/big", "/edge", "/tiny", "/tiny2"]


def test_other_sorts_last_even_when_larger():
    rows = [s(["/a"], 50)] + [s([f"/p{i}"], 0.5) for i in range(100)]
    res = flow.build(rows, [])
    assert col(res, 0) == [("/a", 50), (flow.OTHER, 50)]


def test_focus_on_one_arrival_page():
    rows = [s(["/a", "/x"], 10), s(["/b", "/y"], 90)]
    res = flow.build(rows, [], focus="/a")
    assert col(res, 0) == [("/a", 10)] and col(res, 1) == [("/x", 10)]
    assert [a["page"] for a in res["arrivals"]] == ["/b", "/a"]  # the table still lists every arrival page


def test_top_queries_per_arrival_page():
    gsc_rows = [{"page": "/a", "query": q, "clicks": c, "impressions": i}
                for q, c, i in [("q1", 1, 100), ("q2", 5, 10), ("q3", 5, 50), ("q4", 0, 9), ("q5", 0, 8), ("q6", 0, 7)]]
    gsc_rows += [{"page": "/a", "query": "q1", "clicks": 1, "impressions": 1},  # same query on another day adds up
                 {"page": "/zzz", "query": "elsewhere", "clicks": 99, "impressions": 999}]
    res = flow.build([s(["/a"], 3)], gsc_rows)
    qs = res["arrivals"][0]["queries"]
    assert [(q["query"], q["clicks"], q["impressions"]) for q in qs] == [
        ("q3", 5, 50), ("q2", 5, 10), ("q1", 2, 101), ("q4", 0, 9), ("q5", 0, 8)]


def test_without_paths_only_arrivals_are_known():
    res = flow.build([s(["/a"], 10)], [], paths=False)
    assert col(res, 0) == [("/a", 10)]
    assert res["columns"][1:] == [] and res["links"] == []
    assert res["arrivals"][0]["exit_rate"] is None
