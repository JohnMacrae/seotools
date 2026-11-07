# Domain Overview

A self-hosted, Semrush-style domain overview built on the DataForSEO Labs API.

Panels: organic keywords (with new/up/down/lost movement), estimated traffic and traffic value,
a 12-month trend, ranking-position distribution, top organic keywords, top pages, main organic
competitors and a competitive positioning map.

Every lookup is saved as a dated snapshot in SQLite (`./data/overview.db`). A repeat lookup
within `CACHE_DAYS` is served from the cache for free; "Refresh (paid)" forces a new fetch.
Past snapshots can be picked from the dropdown.

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
- `GET /healthz`

Location codes: 2826 UK, 2840 US, 2372 Ireland, 2036 Australia, 2124 Canada.

## Notes

- The protocol and `www.` are removed from input, so a pasted URL works.
- Ranking-change arrows compare against DataForSEO's previous absolute rank, so treat them as indicative.
- The competitor list excludes giant sites (Amazon, Wikipedia and similar) via `exclude_top_domains`.
