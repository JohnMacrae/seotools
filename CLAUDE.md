## Agent skills

### Issue tracker

Issues live in GitHub Issues for JohnMacrae/seotools (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### UI checks

Rendered-page checks on http://localhost:8095 use the shared headless Chrome (`jev-ultrafast` skill, CDP on 127.0.0.1:9223, reaches localhost). Reading the DOM (titles, counts): `Runtime.evaluate` over CDP with the jev clone's `.venv` python. Clicking through flows (run a lookup, open a crawl): the jev Agent. Page routes: `domain-overview/README.md`.
