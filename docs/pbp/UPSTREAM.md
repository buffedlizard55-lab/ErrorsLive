# Source and local changes

Copied from https://github.com/buffedlizard55-lab/MLB-Live-PBP at
`7eac3717fa1feea1053591e3a961b809728c373e` on 2026-10-02.

The MIT license is preserved in `LICENSE`. Files copied: `index.html`,
`reviews.html`, `game.html`, and `assets/`. The two upstream scoring test files
are retained under `../../tests/pbp/` with their import paths adapted.
No upstream game logs, backend, Git metadata, or deployment workflows were copied.
Historical assertions in upstream comments are upstream documentation, not claims
that this integration reverified every example.

## Simplified pages (2026-10-02, later the same day as the initial copy)

On the same day as the initial integration, `index.html` (scoreboard) and
`reviews.html` (feed) were **rewritten** as the simplified ErrorsLive scoring
watch, per the project brief: a scoreboard plus an all-games feed that tracks
**only** official-scorer pending rulings and scoring changes, with one model
score per row.

What the simplified pages are:

- `index.html` — scoreboard. One card per game (score, state) with three
  scoring badges: pending rulings, errors on the board, observed scoring
  changes. Cards link into the feed; a small link opens the full PBP page.
- `reviews.html` — scoring feed. Rows only for: pending official-scorer
  rulings (`os_ruling_pending_primary` / `os_ruling_pending_prior`),
  plays the official feed currently rules an error (`field_error`), and
  observed classification changes. Each row shows the model's HIT score
  (11-outcome head, hit = single+double+triple+home run) with the full
  distribution, and verdicts only from scores captured before a ruling
  landed. No ABS, manager-challenge, umpire-review, boundary-call, or
  every-batted-ball rows.
- New page engines: `assets/js/scoring-watch.js` (shared), 
  `assets/js/scoring-feed-page.js`, `assets/js/scoring-scoreboard-page.js`.
  They run on the ErrorsLive data layer (`../live-board.js` → `../site.js`
  `scoreLiveFeed()` + `../scoring-feed.js` ledger) — one data path shared
  with `../scoreboard.html` / `../allgames.html`, so the pages cannot
  disagree about a play.

What was removed relative to the first integration:

- `assets/js/scoring-model.js` (the earlier adapter that scored every batted
  ball into the upstream feed) was deleted; its behaviour is superseded by
  the simplified pages and its adapter tests were replaced by
  `tests/pbp-integration.test.cjs` (rewritten for the new pages).
- The upstream tab set (ABS / Challenges / Reviews / Boundary / Runs at Risk)
  no longer renders on the two simplified pages.

What is retained unchanged:

- `game.html` and `assets/js/game.js`, `reviews.js`, `props.js`,
  `feed-log.js`, `api.js`, `ui.js` — the full per-game play-by-play page is
  untouched and still the deep-dive view.
- `assets/js/reviews-feed.js` and `reviews.js` remain in the tree: they are
  still read by the two upstream-origin test suites in `tests/pbp/`
  (`scoring-change-test.mjs`, `official-scoring-test.mjs`), and `reviews.js`
  is still loaded by `game.html`. The simplified pages do not load
  `reviews-feed.js`; change detection there is superseded by the shared
  `ScoringFeed` ledger in `../scoring-feed.js`.
- `docs/site.js` gained a guard so `scoreLiveFeed()` returns the row with
  `prediction_available = false` (and the reason on the row) instead of
  throwing when the committed model file is unavailable. The model math and
  the row shape are unchanged; the Python mirror parity test in
  `tests/test_pipeline.py` still holds.

Other local modifications from the initial copy remain as listed below.

## Local modifications carried over from the initial integration

- ErrorsLive navigation/branding and model styling.
- The play-by-play projection includes Statcast hitData fields and playId.
- The replay feed used to display current batted-ball captures alongside
  review rows; that behaviour moved to the simplified pages and the existing
  `../allgames.html` full feed.
- Official logs use an ErrorsLive-specific browser-storage namespace. Server
  logging/SSE is disabled: GitHub Pages cannot run the upstream Node server.
- The simplified pages poll on the shared LiveBoard cadence (30 s with live
  games, 5 min idle; finals re-scanned for 30 minutes). Network duration may
  extend these gaps.
- Prominent warnings disclose the unresolved baseline validation audit.

Read ../pbp-integration.md for verification results and limitations.
