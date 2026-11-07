# Domain Overview + Competitor Gap

A self-hosted, Semrush-style domain overview built on the DataForSEO Labs API.

Panels: organic keywords (with new/up/down/lost movement), estimated traffic and traffic value,
a 12-month trend, ranking-position distribution, top organic keywords, top pages, main organic
competitors and a competitive positioning map.

Every lookup is saved as a dated snapshot in SQLite (`./data/overview.db`). A repeat lookup
within `CACHE_DAYS` is served from the cache for free; "Refresh (paid)" forces a new fetch.
Past snapshots can be picked from the dropdown.

## Competitor gap (`/compare`)

Compare your domain with up to 3 competitors and see whether the gap is **on-page** or **off-page**:

- **Verdict**: what share of the search volume you lose to competitors looks on-page vs off-page.
- **Keywords where they beat you**: every shared keyword with each site's position and ranking page. Each lost keyword
  is classified by comparing the competitor's ranking page and domain authority with yours:
  - *off-page*: their domain rank is >= 1.25x yours, or their page has >= 2x your page's referring domains
  - *on-page*: you have similar or more authority but still rank lower (relevance, content, page targeting)
  - It also flags when your homepage is ranking against a competitor's dedicated page.
- **Content gap**: keywords competitors rank for and you don't (brand terms removed). These need new pages or sections.
- **Authority**: domain rank, referring domains, spam score for every site.
- **Homepage audit**: title, description, H1s, word count, load time and issues, side by side.
- **Link gap**: sites linking to competitors but not to you (spammy ones hidden), with each site's domain rank (0–1000) and the exact page each link sits on, its target and anchor, for outreach.
- Click any column header in the link gap, keyword and authority tables to sort; click again to reverse.

Competitor suggestions come from your latest Domain overview of the same domain. Results are cached for `CACHE_DAYS`.
A fresh comparison of 3 competitors makes about 15 DataForSEO calls; the cost is shown on the page.

## Internal linking (`/linking`)

A free, self-hosted crawler for your own site. "Crawl now" fetches the homepage and every sitemap URL (via `robots.txt`
`Sitemap:` lines, falling back to `/sitemap.xml`), follows internal links breadth-first (4 at a time, 0.25s polite delay,
respects `robots.txt`), and stores every page plus every internal link with its placement (nav, header, footer, aside, main
or body). Redirects are recorded, not followed silently. Crawls are capped by `CRAWL_MAX_PAGES` (default 2000) and cost nothing.

The **Health** tab shows orphans, contextual orphans (linked only from nav/footer), broken and redirected internal links,
dead ends, non-indexable pages, title/H1/description problems, click depth and pages by section. Results are linkable with `?crawl=ID`.
Other tabs (strength, topic map, template rules, to-dos) are placeholders for later work.

Tests: `python3 -m pytest tests` (needs `pytest`, `httpx`, `fastapi`).

## Deploy on dnas

```bash
mkdir -p /volume1/docker/domain-overview && cd /volume1/docker/domain-overview
# copy the contents of this folder here, then:
cp .env.example .env    # add your DataForSEO login + API password
docker compose up -d --build
```

Open http://dnas:8095 (or over Tailscale at http://100.98.167.107:8095).
To put it behind the Pi's Caddy, add a site block that reverse-proxies to `100.98.167.107:8095`.

Try it without credits first: set `DFS_MOCK=1` in `.env` and restart.

## Cost per fresh lookup

Five DataForSEO Labs live calls run in parallel: `domain_rank_overview`, `historical_rank_overview`,
`ranked_keywords`, `relevant_pages`, `competitors_domain`. The actual cost of each fetch is shown
next to the snapshot and the running total is on the home page.

## API

- `GET /api/overview?domain=example.com&location=2826&language=en[&refresh=1][&snapshot=ID]`
- `GET /api/history?domain=example.com&location=2826`
- `GET /api/recent`
- `POST /api/crawl?domain=example.com` (409 if one is running), `GET /api/crawl/ID`, `GET /api/crawls?domain=`, `GET /api/crawl/ID/health`
- `GET /healthz`

Location codes: 2826 UK, 2840 US, 2372 Ireland, 2036 Australia, 2124 Canada.

## Notes

- The protocol and `www.` are removed from input, so a pasted URL works.
- Ranking-change arrows compare against DataForSEO's previous absolute rank, so treat them as indicative.
- The competitor list excludes giant sites (Amazon, Wikipedia and similar) via `exclude_top_domains`.
