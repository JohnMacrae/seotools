"""Supporting-page review: per keyword target, an LLM picks the pages a reader would naturally go on to the target
page from (shortlist), then finds the sentence and anchor for each (placement). Proposals are stored; John approves
or rejects them, and the planner links from approved supporting pages first.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re

import httpx

from . import linkgraph, linkmap, linkplan as lp, llm
from .match import _stem_key

SHORTLIST_MAX = 10
BLURB_CHARS = 200
PLACEMENT_TEXT_CHARS = 6000
DRAFT_MAX_WORDS = 30
ANCHOR_MAX_CHARS = 80
CONCURRENCY = 4
STATUSES = ("proposed", "approved", "rejected")


def ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS supporting_pages (
            id INTEGER PRIMARY KEY,
            target_id INTEGER NOT NULL,
            target_page TEXT NOT NULL,
            supporting_page TEXT NOT NULL,
            crawl_id INTEGER NOT NULL,
            sentence TEXT,
            in_text INTEGER NOT NULL DEFAULT 0,
            anchor TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            decided_at TEXT,
            UNIQUE(target_page, supporting_page))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS supporting_runs (
            id INTEGER PRIMARY KEY,
            crawl_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            progress TEXT,
            error TEXT,
            model TEXT,
            cost REAL,
            data TEXT)"""
    )


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------- storage

def save(con, crawl_id: int, proposals: list[dict]) -> dict:
    """Insert new proposals; refresh rows still 'proposed'; never touch approved or rejected rows."""
    added = refreshed = 0
    for p in proposals:
        key = (p["target_page"], p["supporting_page"])
        cur = con.execute("SELECT status FROM supporting_pages WHERE target_page=? AND supporting_page=?", key).fetchone()
        if cur is None:
            con.execute(
                "INSERT INTO supporting_pages(target_id,target_page,supporting_page,crawl_id,sentence,in_text,anchor,reason,"
                "status,created_at) VALUES (?,?,?,?,?,?,?,?,'proposed',?)",
                (p["target_id"], *key, crawl_id, p["sentence"], int(bool(p["in_text"])), p["anchor"], p["reason"], _now()))
            added += 1
        elif cur[0] == "proposed":
            con.execute("UPDATE supporting_pages SET target_id=?,crawl_id=?,sentence=?,in_text=?,anchor=?,reason=? "
                        "WHERE target_page=? AND supporting_page=?",
                        (p["target_id"], crawl_id, p["sentence"], int(bool(p["in_text"])), p["anchor"], p["reason"], *key))
            refreshed += 1
    con.commit()
    return {"added": added, "refreshed": refreshed}


def rows(con, status: str | None = None, crawl_id: int | None = None) -> list[dict]:
    q, args = "SELECT * FROM supporting_pages", []
    cond = []
    if status:
        cond.append("status=?"); args.append(status)
    if crawl_id is not None:
        cond.append("crawl_id=?"); args.append(crawl_id)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    out = [dict(r) for r in con.execute(q + " ORDER BY target_page, supporting_page", args)]
    for r in out:
        r["in_text"] = bool(r["in_text"])
    return out


def decided_pairs(con) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in con.execute(
        "SELECT target_page, supporting_page FROM supporting_pages WHERE status IN ('approved','rejected')")}


def validate_edit(anchor: str, sentence: str | None, in_text: bool) -> str | None:
    """Error message, or None when the (possibly edited) anchor/sentence are consistent."""
    anchor = (anchor or "").strip()
    if not anchor:
        return "anchor must not be empty"
    if len(anchor) > ANCHOR_MAX_CHARS:
        return f"anchor must be at most {ANCHOR_MAX_CHARS} characters"
    if not sentence or not lp._find(sentence, anchor):
        return "anchor must occur in the sentence"
    return None


def update(con, row_id: int, body: dict) -> tuple[dict | None, str | None]:
    """Apply status and/or anchor/sentence/in_text edits. Returns (row, error); row None = not found."""
    r = con.execute("SELECT * FROM supporting_pages WHERE id=?", (row_id,)).fetchone()
    if not r:
        return None, None
    r = dict(r)
    if "status" in body and body["status"] not in STATUSES:
        return r, "status must be proposed, approved or rejected"
    new = {"anchor": r["anchor"], "sentence": r["sentence"], "in_text": bool(r["in_text"])}
    edited = False
    for k in new:
        if k in body:
            new[k] = body[k]
            edited = True
    if edited:
        if not isinstance(new["anchor"], str) or not isinstance(new["sentence"], (str, type(None))):
            return r, "anchor and sentence must be strings"
        new["anchor"] = new["anchor"].strip()
        err = validate_edit(new["anchor"], new["sentence"], bool(new["in_text"]))
        if err:
            return r, err
    status = body.get("status", r["status"])
    decided = None if status == "proposed" else (_now() if "status" in body else r["decided_at"])
    con.execute("UPDATE supporting_pages SET status=?, decided_at=?, anchor=?, sentence=?, in_text=? WHERE id=?",
                (status, decided, new["anchor"], new["sentence"], int(bool(new["in_text"])), row_id))
    con.commit()
    out = dict(con.execute("SELECT * FROM supporting_pages WHERE id=?", (row_id,)).fetchone())
    out["in_text"] = bool(out["in_text"])
    return out, None


# ---------------------------------------------------------------- candidates

def eligible(pages: list[dict], target_page: str) -> list[dict]:
    """Indexable 200 HTML pages with body text, minus the target, phase-2 lines and dress product pages."""
    out, seen = [], set()
    for p in pages:
        u = lp.norm(p["url"])
        if u in seen or u == lp.norm(target_page) or not linkgraph.is_indexable(p):
            continue
        if lp.path(u).startswith(linkmap.PHASE2_PREFIXES) or lp.is_dress(u):
            continue
        if len(p.get("text") or "") < lp.MIN_TEXT:
            continue
        seen.add(u)
        out.append({**p, "url": u})
    return out


# ---------------------------------------------------------------- prompts

SHORTLIST_SYSTEM = """You plan internal links for a bridal shop website (Serenity Brides, Colchester). British English.
You choose supporting pages: pages whose readers would naturally go on to the target page. Reply with JSON only."""

SHORTLIST_USER = """Target page: {url} — {title} | H1: {h1}
Keywords it should rank for: {keywords}

Candidate pages (url | title | H1 | opening text):
{cands}

Choose up to {n} candidate pages that a reader of that page would naturally go on to the target page from, with a
one-line reason each. Only choose pages where the link would genuinely help the reader; fewer is fine.
Example: ready-to-wear ("buy off the peg, take home on the day") fits timeline FAQs such as "how long does it take to
get a wedding dress" and "when should I start shopping", because late brides need it.
Reply: {{"pages": [{{"url": "<candidate url>", "reason": "<one line>"}}]}}"""

PLACEMENT_SYSTEM = """You write internal links for a bridal shop website (Serenity Brides, Colchester). British English.
Links must help the reader and read naturally, never like SEO stuffing. Reply with JSON only."""

PLACEMENT_USER = """Supporting page: {src} — {title}
Page text:
<<<
{text}
>>>

Target page: {dst} — {dtitle}
The anchor must be one of these keywords or a close variant (plural/singular or word order): {keywords}

Find where a link to the target page fits on this page. Return:
{{"skip": false,
  "sentence": "<the EXACT existing sentence from the page text where the link fits>",
  "anchor": "<the keyword or close variant, copied EXACTLY from that sentence>",
  "in_text": true}}
If no existing sentence can carry the anchor, draft ONE new sentence (max {words} words) in the page's tone that contains
the anchor, and return {{"skip": false, "sentence": "<new sentence>", "anchor": "<anchor>", "in_text": false}}.
If there is no natural place, return {{"skip": true, "why": "<reason>"}}."""


def is_variant(anchor: str, keywords: list[str]) -> bool:
    """Anchor is a keyword or a close variant (case, plural/singular, word order)."""
    k = _stem_key(anchor)
    return bool(k) and any(k == _stem_key(kw) for kw in keywords)


def place(data: dict | None, text: str, keywords: list[str]) -> tuple[dict | None, str | None]:
    """Validate a placement answer. Returns (proposal fields, None) or (None, why-dropped)."""
    if not isinstance(data, dict) or data.get("skip"):
        return None, f"model skipped: {str((data or {}).get('why') or '')[:100]}"
    anchor = str(data.get("anchor") or "").strip()
    sentence = str(data.get("sentence") or "").strip()
    if not anchor or len(anchor) > ANCHOR_MAX_CHARS:
        return None, "no usable anchor"
    if not is_variant(anchor, keywords):
        return None, f"anchor '{anchor}' is not a target keyword or close variant"
    span = lp._find(text, anchor)
    if data.get("in_text") and span:
        # prefer the model's sentence when it really is in the text; else the page's own surrounding sentence
        flat = " ".join(text.split())
        sent = " ".join(sentence.split())
        if not (sent and sent in flat and lp._find(sent, anchor)):
            sent = lp._context(text, span)
        return {"anchor": text[span[0]:span[1]], "in_text": True, "sentence": sent}, None
    # not in the text (or model said so): needs a drafted sentence containing the anchor
    if sentence and lp._find(sentence, anchor) and len(sentence.split()) <= DRAFT_MAX_WORDS:
        return {"anchor": anchor, "in_text": False, "sentence": sentence}, None
    return None, f"anchor '{anchor}' not in page text and no valid drafted sentence"


# ---------------------------------------------------------------- run

def _blurb(p: dict) -> str:
    return " ".join((p.get("text") or "")[:BLURB_CHARS].split())


async def review_target(client, entry: dict, tpage: dict, cands: list[dict], sem) -> tuple[list[dict], float, list[str]]:
    kws = entry["keywords"]
    warn, cost, out = [], 0.0, []
    if not cands:
        return [], 0.0, [f"{lp.path(entry['target_page'])}: no candidate pages"]
    listing = "\n".join(f"{c['url']} | {(c.get('title') or '')[:90]} | {(c.get('h1') or '')[:90]} | {_blurb(c)}" for c in cands)
    async with sem:
        data, c1 = await llm.ask_json(client, SHORTLIST_SYSTEM, SHORTLIST_USER.format(
            url=entry["target_page"], title=tpage.get("title") or "", h1=tpage.get("h1") or "",
            keywords="; ".join(kws), cands=listing, n=SHORTLIST_MAX), max_tokens=2000)
    cost += c1
    by_url = {c["url"]: c for c in cands}
    picks, seen = [], set()
    for it in (data or {}).get("pages", []) if isinstance(data, dict) else []:
        u = lp.norm(str((it or {}).get("url") or "")) if isinstance(it, dict) else ""
        if u in by_url and u not in seen:
            seen.add(u)
            picks.append((u, str(it.get("reason") or "").strip()[:300]))
    picks = picks[:SHORTLIST_MAX]

    async def one(u, reason):
        p = by_url[u]
        text = (p.get("text") or "")[:PLACEMENT_TEXT_CHARS]
        async with sem:
            d, c = await llm.ask_json(client, PLACEMENT_SYSTEM, PLACEMENT_USER.format(
                src=u, title=p.get("title") or "", text=text, dst=entry["target_page"],
                dtitle=tpage.get("title") or tpage.get("h1") or "", keywords="; ".join(kws), words=DRAFT_MAX_WORDS),
                max_tokens=1000)
        return u, reason, d, c, text

    res = await asyncio.gather(*(one(u, r) for u, r in picks), return_exceptions=True)
    for (u, reason), r in zip(picks, res):
        if isinstance(r, Exception):
            if isinstance(r, (llm.LLMError, httpx.HTTPError)):
                warn.append(f"{lp.path(u)} -> {lp.path(entry['target_page'])}: placement failed: {r}")
                continue
            raise r
        _u, _r, d, c, text = r
        cost += c
        v, why = place(d, text, kws)
        if not v:
            warn.append(f"{lp.path(u)} -> {lp.path(entry['target_page'])}: {why}")
            continue
        out.append({"target_id": entry["target_id"], "target_page": entry["target_page"], "supporting_page": u,
                    "reason": reason, **v})
    return out, cost, warn


async def run(pages: list[dict], links: list[dict], strength: dict, target_rows: list[dict], decided: set,
              progress=None) -> dict:
    """pages must carry text. Returns {proposals, cost, warnings, model}."""
    say = progress or (lambda m: None)
    if not llm.configured():
        raise llm.LLMError("LLM is not configured (OPENROUTER_API_KEY)")
    S = {lp.norm(p["url"]): {**p, "url": lp.norm(p["url"])} for p in strength["pages"]}
    ranges = {lp.norm(u) for u in linkgraph.last_stock(links)["ranges"]}
    bridal = [r for r in target_rows if (r.get("product_line") or "bridal") == "bridal" and (r.get("phase") or 1) == 1]
    by_url = {lp.norm(p["url"]): p for p in pages if linkgraph.is_html_ok(p)}
    linked = linkgraph.linked_pairs(pages, links, lp.norm)  # pills, related blocks; not the site-wide menu
    entries, warnings = [], []
    for e in linkmap.expand(bridal, pages, S, ranges):
        tp = by_url.get(lp.norm(e["target_page"]))
        if not tp or not linkgraph.is_indexable(tp) or lp.path(e["target_page"]).startswith(linkmap.PHASE2_PREFIXES):
            warnings.append(f"{lp.path(e['target_page'])}: target page not live in this crawl; skipped")
            continue
        entries.append((e, tp))
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    proposals, cost = [], 0.0
    async with httpx.AsyncClient() as client:
        async def one(e, tp):
            nonlocal done
            tgt = lp.norm(e["target_page"])
            cands = [c for c in eligible(pages, e["target_page"])
                     if (tgt, c["url"]) not in decided and (c["url"], tgt) not in linked]
            try:
                return await review_target(client, e, tp, cands, sem)
            except (llm.LLMError, httpx.HTTPError) as ex:
                return [], 0.0, [f"{lp.path(e['target_page'])}: review failed: {ex}"]
            finally:
                done += 1
                say(f"Reviewed {done} of {len(entries)} targets")

        say(f"Reviewing {len(entries)} targets")
        for ps, c, w in await asyncio.gather(*(one(e, tp) for e, tp in entries)):
            proposals += ps
            cost += c
            warnings += w
    say("Done")
    return {"proposals": proposals, "cost": round(cost, 4), "warnings": warnings, "model": llm.MODEL,
            "targets": len(entries)}
