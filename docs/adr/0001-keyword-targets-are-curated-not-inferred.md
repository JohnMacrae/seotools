# Keyword targets are curated and stored, not inferred from Search Console

Until November 2026 the Linking tool guessed what each page should rank for from its highest-impression Search Console query, recomputed on every request (`linkgraph.strength` → `top_queries[0]`). That guess drifted month to month, was wrong when the top query was off-topic, and was blind to the competitor. We decided that each keyword target (close-variant keywords plus exactly one target page) is chosen by the owner from the competitor gap against theweddingshop.uk, stored in the tool, and changed only by a deliberate edit at a scheduled review. Search Console and DataForSEO feed the review and the measurement; they never reassign a target on their own.

## Consequences

- The link plan, anchors and progress checks read keyword targets; they no longer read `top_queries` to decide what a page is for.
- A page with no keyword target is a supporting page by default.
- "Search Console shows page X ranking for Y" is a signal to raise at review, not an automatic re-target.
