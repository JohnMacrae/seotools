"""Visitor flow (#56): turns the arrival tree from GA4 into columns of pages and the links between them.

A tree node is {"page", "users", "exits", "next": [nodes]}: `users` reached the page, `exits` of them left the site
from it, and `next` are the pages they went to next. Users a node's children and exits don't account for are
shown as "Other" (pages GA4 was not asked about), never dropped."""
from __future__ import annotations

MAX_FOLLOWING = 3     # pages after the arrival page
COLLAPSE = 0.01       # a page with under this share of all users is folded into "Other"


def _kind(node_id: str) -> str:
    return "exit" if node_id.startswith("x") else "other" if node_id.startswith("o") else "page"


def build_flow(roots: list[dict], collapse: float = COLLAPSE) -> dict:
    total = sum(r["users"] for r in roots)
    if not total:
        return {"total": 0, "columns": [], "links": []}
    label: dict[str, str] = {}
    col_of: dict[str, int] = {}
    links: dict[tuple[str, str], int] = {}

    def put(col: int, page: str) -> str:
        nid = f"{col}:{page}"
        label[nid], col_of[nid] = page, col
        return nid

    def special(col: int, kind: str) -> str:
        nid = f"{'x' if kind == 'exit' else 'o'}{col}"
        label[nid], col_of[nid] = "Exit" if kind == "exit" else "Other", col
        return nid

    def add(a: str, b: str, users: int):
        if users > 0:
            links[(a, b)] = links.get((a, b), 0) + users

    def walk(n: dict, col: int, nid: str):
        if col >= MAX_FOLLOWING:
            return
        kids = sum(k["users"] for k in n["next"])
        for k in n["next"]:
            kid = put(col + 1, k["page"])
            add(nid, kid, k["users"])
            walk(k, col + 1, kid)
        add(nid, special(col + 1, "exit"), n["exits"])
        add(nid, special(col + 1, "other"), n["users"] - kids - n["exits"])

    users: dict[str, int] = {}
    for r in roots:
        nid = put(0, r["page"])
        users[nid] = users.get(nid, 0) + r["users"]
        walk(r, 0, nid)

    inflow: dict[str, int] = {}
    for (a, b), u in links.items():
        inflow[b] = inflow.get(b, 0) + u
    users.update({k: v for k, v in inflow.items()})

    # Fold small pages into their column's "Other"; their own onward links go with them.
    small = {n for n in label if _kind(n) == "page" and users[n] < collapse * total}
    fold = {n: special(col_of[n], "other") for n in small}
    merged: dict[tuple[str, str], int] = {}
    for (a, b), u in links.items():
        if a in small:
            continue
        key = (a, fold.get(b, b))
        merged[key] = merged.get(key, 0) + u
    for n in small:
        if col_of[n] == 0:
            users[fold[n]] = users.get(fold[n], 0) + users[n]
    for b in {b for _a, b in merged if col_of[b] > 0}:
        users[b] = sum(u for (_a, t), u in merged.items() if t == b)

    order = {"page": 0, "other": 1, "exit": 2}
    ids = {n for pair in merged for n in pair} | {n for n in label if col_of[n] == 0 and n not in small}
    columns: list[list[dict]] = [[] for _ in range(max(col_of[n] for n in ids) + 1)]
    for n in sorted(ids, key=lambda n: (col_of[n], order[_kind(n)], -users[n], label[n])):
        columns[col_of[n]].append({"id": n, "label": label[n], "users": users[n]})
    return {"total": total, "columns": columns,
            "links": [{"source": a, "target": b, "users": u} for (a, b), u in merged.items()]}
