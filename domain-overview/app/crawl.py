"""Self-hosted site crawler: pages, internal links and where on the page each link sits."""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import html
import os
import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx

MAX_PAGES = int(os.environ.get("CRAWL_MAX_PAGES", "2000"))
CONCURRENCY = 4
DELAY = 0.25
TIMEOUT = 20
UA = "seotools-crawler/1.0"
TEXT_LIMIT = 20000

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "svg", "template"}
LANDMARK_TAGS = {"nav": "nav", "header": "header", "footer": "footer", "aside": "aside", "main": "main"}
LANDMARK_ROLES = {"navigation": "nav", "banner": "header", "contentinfo": "footer", "complementary": "aside", "main": "main"}
NOT_CONTENT = {"nav", "header", "footer", "aside"}


def strip_www(host: str) -> str:
    return host.removeprefix("www.")


def normalise(url: str, base: str = "", site_host: str | None = None) -> str | None:
    """Absolute http(s) URL without fragment or query, lowercase host. Internal hosts are rewritten to site_host."""
    try:
        u = urlsplit(urldefrag(urljoin(base, url.strip()))[0])
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host:
        return None
    if site_host and strip_www(host) == strip_www(site_host):
        host = site_host
    port = f":{u.port}" if u.port and u.port not in (80, 443) else ""
    return urlunsplit((u.scheme, host + port, u.path or "/", "", ""))


def collapse(s: str) -> str:
    return " ".join(s.split())


class _Parser(HTMLParser):
    def __init__(self, base_url: str, site_host: str):
        super().__init__(convert_charrefs=True)
        self.base, self.site = base_url, site_host
        self.stack: list[tuple[str, str | None]] = []  # (tag, landmark)
        self.title = self.h1 = self.canonical = self.meta_robots = self.meta_description = None
        self.in_title = False
        self.title_buf: list[str] = []
        self.h1_buf: list[str] | None = None
        self.main_text: list[str] = []
        self.body_text: list[str] = []
        self.links: list[dict] = []
        self.external = 0
        self.cur: dict | None = None  # open <a>

    def _landmark(self, tag, attrs):
        lm = LANDMARK_TAGS.get(tag)
        role = (attrs.get("role") or "").strip().lower()
        return LANDMARK_ROLES.get(role) or lm

    def _placement(self):
        for _, lm in reversed(self.stack):
            if lm:
                return lm
        return "body"

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "meta":
            name = a.get("name", "").lower()
            if name == "robots" and self.meta_robots is None:
                self.meta_robots = a.get("content", "").strip()
            elif name == "description" and self.meta_description is None:
                self.meta_description = collapse(a.get("content", ""))
            return
        if tag == "link":
            if "canonical" in a.get("rel", "").lower().split() and self.canonical is None and a.get("href"):
                self.canonical = normalise(a["href"], self.base, self.site)
            return
        if tag == "img":
            if self.cur is not None and a.get("alt"):
                self.cur["alts"].append(a["alt"])
            return
        if tag in VOID:
            return
        if tag == "a" and self.cur is not None:  # <a> can't nest: the earlier one is unclosed
            self._pop_to_tag("a")
        self.stack.append((tag, self._landmark(tag, a)))
        if tag == "title":
            self.in_title = True
        elif tag == "h1" and self.h1 is None and self.h1_buf is None:
            self.h1_buf = []
        elif tag == "a" and a.get("href") is not None:
            self.cur = {"href": a["href"], "rel": a.get("rel", "").lower(), "placement": self._placement(),
                        "text": [], "alts": []}

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        self._pop_to_tag(tag)

    def _pop_to_tag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, _ in reversed(self.stack[i:]):
                    self._closed(t)
                del self.stack[i:]
                return

    def _closed(self, tag):
        if tag == "title":
            self.in_title = False
            self.title = collapse("".join(self.title_buf))
        elif tag == "h1" and self.h1_buf is not None:
            self.h1 = collapse("".join(self.h1_buf))
            self.h1_buf = None
        elif tag == "a" and self.cur is not None:
            self._finish_link()

    def _finish_link(self):
        c, self.cur = self.cur, None
        dst = normalise(c["href"], self.base, self.site)
        raw = c["href"].strip().lower()
        if dst is None or raw.startswith("#"):
            return
        if strip_www(urlsplit(dst).hostname or "") != strip_www(self.site):
            self.external += 1
            return
        anchor = collapse("".join(c["text"])) or collapse(" ".join(c["alts"]))
        self.links.append({"dst": dst, "anchor": anchor, "placement": c["placement"],
                           "nofollow": int("nofollow" in c["rel"].split())})

    def handle_data(self, data):
        if self.in_title:
            self.title_buf.append(data)
            return
        tags = [t for t, _ in self.stack]
        if any(t in SKIP for t in tags):
            return
        if self.cur is not None:
            self.cur["text"].append(data)
        if self.h1_buf is not None:
            self.h1_buf.append(data)
        lms = [lm for _, lm in self.stack if lm]
        if "main" in lms:
            self.main_text.append(data)
        if not any(lm in NOT_CONTENT for lm in lms):
            self.body_text.append(data)

    def finish(self):
        while self.stack:  # close whatever was left open
            self._closed(self.stack.pop()[0])


def parse_page(html_text: str, base_url: str, site_host: str | None = None) -> dict:
    """Pure: extract SEO fields, main-content text and internal links from one page."""
    site = site_host or urlsplit(base_url).hostname or ""
    p = _Parser(base_url, site)
    p.feed(html_text)
    p.close()
    p.finish()
    text = collapse(" ".join(p.main_text)) if p.main_text else collapse(" ".join(p.body_text))
    return {
        "title": p.title or None, "h1": p.h1 or None, "canonical": p.canonical,
        "meta_robots": p.meta_robots, "meta_description": p.meta_description or None,
        "word_count": len(text.split()), "text": text[:TEXT_LIMIT],
        "links": p.links, "external_links": p.external,
    }


# --- robots and sitemaps -------------------------------------------------

def parse_robots(body: str) -> tuple[list[str], list[str]]:
    """Return (Disallow prefixes for User-agent: *, Sitemap URLs)."""
    disallow, sitemaps, in_star = [], [], False
    last_was_ua = False
    for line in body.splitlines():
        line = line.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = (s.strip() for s in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            in_star = (v == "*") if not last_was_ua else (in_star or v == "*")
            last_was_ua = True
            continue
        last_was_ua = False
        if k == "sitemap" and v:
            sitemaps.append(v)
        elif k == "disallow" and in_star and v:
            disallow.append(v)
    return disallow, sitemaps


def sitemap_locs(body: str) -> tuple[bool, list[str]]:
    locs = [html.unescape(m.strip()) for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.S)]
    return "<sitemapindex" in body, locs


async def _get_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        data = r.content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")
    except (httpx.HTTPError, OSError, ValueError):
        return None


async def read_sitemaps(client, start: list[str], depth: int = 3) -> list[str]:
    urls, seen = [], set()
    todo = list(start)
    for _ in range(depth + 1):
        nxt = []
        for sm in todo:
            if sm in seen:
                continue
            seen.add(sm)
            body = await _get_text(client, sm)
            if not body:
                continue
            is_index, locs = sitemap_locs(body)
            (nxt if is_index else urls).extend(locs)
        todo = nxt
        if not todo:
            break
    return urls


# --- crawl ---------------------------------------------------------------

def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


async def _fetch(client, sem, url: str, site: str) -> dict:
    """Fetch one URL (no redirect following) and return a page record."""
    rec = {"url": url, "status": None, "redirect_to": None, "content_type": None, "canonical": None,
           "meta_robots": None, "title": None, "h1": None, "meta_description": None, "word_count": None,
           "external_links": 0, "text": None, "fetched_at": _now(), "links": []}
    async with sem:
        try:
            async with client.stream("GET", url) as r:
                rec["status"] = r.status_code
                rec["content_type"] = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if 300 <= r.status_code < 400 and r.headers.get("location"):
                    rec["redirect_to"] = normalise(r.headers["location"], url, site)
                elif "html" in rec["content_type"]:
                    body = (await r.aread()).decode(r.encoding or "utf-8", "replace")
                    rec.update(parse_page(body, url, site))
        except (httpx.HTTPError, OSError, ValueError) as e:
            rec["text"] = f"ERROR: {type(e).__name__}: {e}"[:300]
        await asyncio.sleep(DELAY)
    return rec


async def run_crawl(crawl_id: int, domain: str, db_factory, max_pages: int | None = None):
    max_pages = max_pages or MAX_PAGES
    con = db_factory()
    try:
        con.execute("UPDATE crawls SET status='running' WHERE id=?", (crawl_id,))
        con.commit()
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=False) as client:
            # Settle on one host form (www or not) by following the homepage redirect once.
            site = domain
            try:
                r = await client.get(f"https://{domain}/", follow_redirects=True)
                site = r.url.host or domain
            except httpx.HTTPError:
                pass
            home = normalise(f"https://{site}/", site_host=site)

            disallow, sitemaps = [], []
            robots = await _get_text(client, f"https://{site}/robots.txt")
            if robots:
                disallow, sitemaps = parse_robots(robots)
            sitemaps = sitemaps or [f"https://{site}/sitemap.xml", f"https://{site}/sitemap-index.xml"]
            in_sitemap: dict[str, None] = {}
            for u in await read_sitemaps(client, sitemaps):
                n = normalise(u, site_host=site)
                if n and strip_www(urlsplit(n).hostname or "") == strip_www(site):
                    in_sitemap[n] = None

            def allowed(u):
                path = urlsplit(u).path
                return not any(path.startswith(d) for d in disallow)

            sem = asyncio.Semaphore(CONCURRENCY)
            seen = {home}
            level, depth, done = [home], 0, 0
            leftovers_done = False
            while level and done < max_pages:
                level = level[: max_pages - done]
                recs = await asyncio.gather(*(_fetch(client, sem, u, site) for u in level))
                nxt = []
                for rec in recs:
                    rec["depth"] = None if leftovers_done else depth
                    rec["in_sitemap"] = int(rec["url"] in in_sitemap)
                    _store(con, crawl_id, rec)
                    done += 1
                    targets = [l["dst"] for l in rec["links"]] + ([rec["redirect_to"]] if rec["redirect_to"] else [])
                    for t in targets:
                        if t not in seen and strip_www(urlsplit(t).hostname or "") == strip_www(site) and allowed(t):
                            seen.add(t)
                            nxt.append(t)
                    if done % 10 == 0:
                        con.execute("UPDATE crawls SET pages=? WHERE id=?", (done, crawl_id))
                    con.commit()
                depth += 1
                level = nxt
                if not level and not leftovers_done:  # link graph exhausted: sitemap-only pages, depth unknown
                    leftovers_done = True
                    level = [u for u in in_sitemap if u not in seen and allowed(u)]
                    seen.update(level)
        con.execute("UPDATE crawls SET status='done', pages=?, finished_at=? WHERE id=?", (done, _now(), crawl_id))
        con.commit()
    except Exception as e:  # noqa: BLE001 - recorded on the crawl row
        con.execute("UPDATE crawls SET status='error', error=?, finished_at=? WHERE id=?",
                    (f"{type(e).__name__}: {e}"[:500], _now(), crawl_id))
        con.commit()
    finally:
        con.close()


def _store(con, crawl_id: int, rec: dict):
    con.execute(
        "INSERT OR REPLACE INTO pages(crawl_id,url,status,redirect_to,content_type,canonical,meta_robots,title,h1,"
        "meta_description,word_count,depth,in_sitemap,external_links,text,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (crawl_id, rec["url"], rec["status"], rec["redirect_to"], rec["content_type"], rec["canonical"],
         rec["meta_robots"], rec["title"], rec["h1"], rec["meta_description"], rec["word_count"], rec["depth"],
         rec["in_sitemap"], rec["external_links"], rec["text"], rec["fetched_at"]))
    con.executemany(
        "INSERT INTO links(crawl_id,src,dst,anchor,placement,nofollow) VALUES (?,?,?,?,?,?)",
        [(crawl_id, rec["url"], l["dst"], l["anchor"], l["placement"], l["nofollow"]) for l in rec["links"]])
