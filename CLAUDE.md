## Agent skills

### Issue tracker

Issues live in GitHub Issues for JohnMacrae/seotools (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### UI checks

Rendered-page checks on http://localhost:8095 use the shared headless Chrome (`jev-ultrafast` skill, CDP on 127.0.0.1:9223, reaches localhost). Reading the DOM (titles, counts): `scripts/cdp-eval <url> '<js>'`. Clicking through flows (run a lookup, open a crawl): the jev Agent. Page routes: `domain-overview/README.md`.

A branch's pages: `scripts/qa-branch <branch> <port>` serves a throwaway copy (live data copied, never spends); `--down <branch>` removes it. UI acceptance criteria you can't check where you run (Ralph's sandbox): list them in your final message under **For human QA**, with that command.

### Merging PR stacks

`scripts/merge-stack [--delete] <target> <PR>...` merges in order with merge commits and checks each step; it deletes a head branch only once its commit is in the target. Use it for any multi-PR merge.
