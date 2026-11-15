# The journal is read from each site's repo: commit trailers plus deploy tags

The rank tracker covers several sites, and each one lives in its own git repo, built with different tech. To line up ranking changes with the work that caused them, every journal entry needs a page, a change type and a live date. Rather than have each site's pipeline push entries to the tracker (a different integration for every site), or infer changes from diffs (which can't tell an SEO change from a typo fix), the tracker reads each site's repo. Commits that change a page carry `SEO-Page: <url>` and `SEO-Change: <type> | <note>` trailers. Each deploy to live leaves a `live/<YYYY-MM-DD>` tag. An entry's live date is the first live tag that contains its commit, or the merge-to-main date marked "approx" when a site doesn't tag deploys.

## Consequences

- Onboarding a site means giving its repo path, adding the trailer rule to that repo's agent instructions, and adding a `commit-msg` hook that warns, without blocking, when page files change without an `SEO-Page` trailer.
- The `SEO-Page` trailer names URLs, not source files, because one template can render many pages. A template change names a pattern (`/dresses/*`) and is stored as one entry.
- Changes made off-site (Google Business Profile, backlinks) are entered by hand in the tracker.
- Before this convention existed there were no trailers, so older history has no automatic entries.
