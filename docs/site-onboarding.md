# Onboarding a site to the rank tracker journal

The tracker reads each site's git repo (ADR 0003). A site needs:

1. **Registry entry**: name, Search Console property, repo path, DataForSEO location. Add the Search Console service account to the site's property.
2. **Trailer rule** in the repo's agent instructions (AGENTS.md / CLAUDE.md). Commits that change what a page says or how it is found carry:
   ```
   SEO-Page: <url path, or a pattern such as /dresses/*>
   SEO-Change: <new-page|copy|headings|title-meta|internal-links|technical> | <one-line note>
   ```
   - One `SEO-Page` line per page; repeat it for several pages. A template change names a pattern.
   - Non-SEO commits (typos, data syncs, tooling) carry no trailers.
   - Copy the "SEO commit trailers" section from sweb-live's AGENTS.md.
3. **Live tags**: each deploy to live tags the shipped commit `live/<YYYY-MM-DD>` (Europe/London), force-moved on a same-day second deploy, and pushes it. Warn-only; never fails a deploy. Reference: sweb-live `scripts/lib/live-tag.sh`, called from `scripts/promote-gate.sh`. Without tags, entries fall back to the merge-to-main date, marked "approx".
4. **Warn-only `commit-msg` hook** in `.githooks/commit-msg`, always exit 0; warns when page/content paths are staged without an `SEO-Page` trailer. Enable once per clone: `git config core.hooksPath .githooks`. Adjust the path regex to the site's layout; chain any existing hooks.
5. **Repo mount**: add a read-only bind mount of the site repo to `domain-overview/docker-compose.yml` (`<host path>:/repos/<repo folder name>:ro`) and set the site's repo path to `/repos/<repo folder name>`; the 06:00 job then reads it after the Search Console top-up. Serenity is set up already: its mount is in the compose file and `SERENITY_REPO_PATH` seeds its repo path when the registry is first created.

History before the convention has no trailers, so it has no automatic entries.
