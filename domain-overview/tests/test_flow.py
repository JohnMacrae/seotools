"""Visitor flow builder: arrival tree in, columns and links out (seam 1)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import flow  # noqa: E402


def node(page, users, *kids, exits=0):
    return {"page": page, "users": users, "exits": exits, "next": list(kids)}


def links(f):
    return {(l["source"], l["target"]): l["users"] for l in f["links"]}


def labels(f):
    return [[n["label"] for n in col] for col in f["columns"]]


def test_arrival_next_page_and_exit():
    roots = [node("/", 100, node("/bridal", 60, exits=60), exits=40)]
    f = flow.build_flow(roots)
    assert f["total"] == 100
    assert labels(f) == [["/"], ["/bridal", "Exit"], ["Exit"]]
    assert links(f) == {("0:/", "1:/bridal"): 60, ("0:/", "x1"): 40, ("1:/bridal", "x2"): 60}


def test_pages_reached_from_two_arrivals_share_one_node():
    roots = [node("/", 50, node("/bridal", 50)), node("/book", 50, node("/bridal", 50))]
    f = flow.build_flow(roots)
    assert [n["users"] for n in f["columns"][1]] == [100]
    assert links(f)[("0:/", "1:/bridal")] == 50 and links(f)[("0:/book", "1:/bridal")] == 50


def test_users_not_accounted_for_flow_to_other():
    roots = [node("/", 100, node("/bridal", 30), exits=10)]
    f = flow.build_flow(roots)
    assert links(f)[("0:/", "o1")] == 60


def test_steps_under_one_percent_collapse_into_other():
    roots = [node("/", 990, node("/a", 990)), node("/rare", 10, node("/b", 10))]
    f = flow.build_flow(roots, collapse=0.01)  # 10 of 1000 = exactly 1%: kept
    assert "/rare" in labels(f)[0]
    f = flow.build_flow([node("/", 1000, node("/a", 995), node("/tiny", 5))], collapse=0.01)
    assert "/tiny" not in labels(f)[1] and "Other" in labels(f)[1]
    assert links(f)[("0:/", "o1")] == 5


def test_following_steps_stop_after_three():
    deep = node("/a", 10, node("/b", 10, node("/c", 10, node("/d", 10, node("/e", 10)))))
    f = flow.build_flow([deep])
    assert labels(f)[0] == ["/a"] and len(f["columns"]) == 4


def test_empty_input():
    assert flow.build_flow([]) == {"total": 0, "columns": [], "links": []}


def test_next_page_with_no_users_is_ignored():
    f = flow.build_flow([node("/", 10, node("/ghost", 0), node("/bridal", 10))])
    assert labels(f)[1] == ["/bridal"]


def test_nodes_say_what_kind_they_are():
    f = flow.build_flow([node("/", 10, node("/bridal", 5), exits=3)])
    assert [(n["label"], n["kind"]) for n in f["columns"][1]] == [("/bridal", "page"), ("Other", "other"), ("Exit", "exit")]
