# One source per kind of number, labelled and dated

DataForSEO Labs is a database snapshot that lags weeks to months, and for serenitybrides.com it disagreed with Search Console and with live Google results (issue #13). Single live SERP checks also swing a lot between calls (the same keyword gave us #11, #3 and "not in top 20" within an hour) and differ between desktop and mobile. So we decided that each kind of number has one source across every tool:

- **Our site's positions, clicks, impressions, CTR:** Google Search Console (90-day window, impression-weighted average position). Never Labs, never a single live check.
- **Competitor positions on keyword targets:** the latest monthly map-pack run (live SERP from Colchester, mobile preferred, desktop as fallback), labelled "live, single check".
- **Competitor positions elsewhere:** Labs, labelled as an estimate.
- **Volume of a keyword target:** the targets doc (`docs/keyword-targets.md`, Google Ads UK monthly), wherever that keyword appears. Amended 2026-11-15: the rank tracker also shows DataForSEO clickstream volume beside it, labelled and dated, fetched only on request and cached for 30 days. The Ads figure stays the one used for sorting and decisions.
- **Volume of other keywords, KD, traffic and traffic value:** Labs, labelled as an estimate. Search Console impressions are shown alongside where we have them.
- **Sessions, arrival pages, exit pages and paths (visitor flow):** GA4 (Data API, read-only), labelled with the property and an "as of" time. Added 2026-11-20. The Search Console queries shown beside each arrival page are labelled a keyword estimate: GA4 hides organic keywords, so the join is by arrival page and window only. GA4 sessions and Search Console clicks are different counts and are never labelled alike.
- **Map pack ranks (ours and theirs):** the map-pack run, since Search Console has no pack data. Our organic position on that page is shown next to the Search Console figure, which is the one to trust.

## Consequences

- Every table that shows these numbers names its source and an "as of" date.
- Labs stays for discovery (keyword ideas, competitor lists, gaps); its positions for our own site are not used for decisions.
- Our personal Google searches are not evidence: they are personalised.
