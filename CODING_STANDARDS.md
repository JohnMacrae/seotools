# Coding standards

Read at review time. Judgement calls only: mechanical rules are tests, so skip what they enforce
(`domain-overview/tests/test_pages.py`: every page's nav links every page, no date literals in pages,
no unused CSS, inline scripts parse).

## Numbers (ADR 0002)

- Every number on a page carries its source and as-of date, and both come from the API
  (for example `/api/targets/meta`), never from text in the page.
- Missing data shows as missing: `–` or "no data" in tables, gaps in graphs. Never 0, never a stand-in value.
- Sorts put missing values last, whichever way they sort.

## One home per rule

- A rule (which labels hide, what counts as a close variant, how movement is signed) lives in one function
  that every caller uses. Two places deciding the same thing drift apart.
- Logic the API already computes is read from the API, not recomputed in page JS.

## Pages (`app/static/*.html`)

- Start from a sibling page for the house style (header, nav, `sitemenu.js`, `spendlight.js`, theme tokens),
  then keep only what this page uses.
- Every `fetch` fails visibly: a network error or non-2xx shows an `.err` box with the API's `detail`.
  A CDN library that doesn't load degrades its own part only (the table still draws without Chart.js).
- Colours come from the CSS variables (`--accent`, `--bad`, ...), so dark mode follows.
- Every interpolated string goes through `esc()`.
- Paid controls show their cost before spending and carry a spend light.

## Words

- Code and UI use `GLOSSARY.md` terms, not its _Avoid_ words. "Label" is the grouping concept, so a
  time bucket control is "Interval", not "Group by".

## Never guess

- A missing spec detail, path or mapping is flagged in the PR and the issue, not invented
  (a guessed repo mount, table columns inferred without the user story).
- A job that refreshes stored data keeps the last good data when its source is unavailable or returns
  implausibly little, and writes only on success.

## Tests

- Test behaviour through the HTTP API or the page route, not strings in the source or private helpers.
- Paid APIs are mocked; no test spends money.
