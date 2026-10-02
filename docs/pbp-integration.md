# Replay + live scoring integration — 2026-10-02

## Delivered

- [Copied PBP scoreboard](pbp/index.html)
- [Combined replay and scoring feed](pbp/reviews.html)
- Per-game play-by-play (`pbp/game.html?gamePk=…`)
- Existing ErrorsLive pages remain available; shared navigation links to the new pages.

The source is MIT-licensed. The exact source revision and local modifications
are recorded in [pbp/UPSTREAM.md](pbp/UPSTREAM.md), with its license retained.

## What the numbers mean

The existing committed model is evaluated on each eligible current batted-ball
capture. The 11-class head supplies single, double, triple, home run, error,
fielder's choice, field out, force out, double play, sac fly, and sac bunt
estimates. A separate binary model supplies the 0–100 error review score.
These are different fitted model heads, not one common probability distribution.

**Neither predicts the probability that an already-issued ruling will change.**
The page labels estimates made from the current capture; they are not stored or
represented as forecasts made before the ruling. Missing/incomplete/non-numeric
Statcast measurements suppress the score. Missing model files also suppress it.
The scorer retains its existing contextual feature extraction/defaults; this
integration does not establish that the public feed captures scorer intent,
ordinary effort, or all pre-play context.

Exact prior-runner pending markers do not receive a batter-outcome estimate.
A pending marker retained in a payload is labelled “marker observed” on the
batted-ball row, with the current result code alongside it. The copied official
pending tracker handles its separate pending/resolved review row. Final-only
captures cannot reconstruct an unobserved earlier ruling.

## Verification performed

1. Inspected the source repository and MIT license through GitHub; pinned the
   copied revision rather than scraping screenshots.
2. Retrieved MLB's [official event registry](https://statsapi.mlb.com/api/v1/eventTypes)
   with the page-fetch tool on 2026-10-02. It identifies
   `os_ruling_pending_primary` as a plate-appearance event and
   `os_ruling_pending_prior` as a base-running event, both described as
   “Official Scorer Ruling Pending”. It lists the hit/error/FC/sacrifice types.
3. `npm test` passes: five new integration tests plus the upstream official
   scoring-change and pending test suites. The tests exercise the actual HTML
   script boot path in jsdom with the committed official capture, compare
   adapter scores with the shared scorer, check all 11 class displays, repeated
   polls, date-state clearing, outcome filters, missing/invalid measurements,
   prior-vs-primary pending, model HTTP failure, and safe text rendering.
   Synthetic edge cases are explicitly labelled synthetic in the test names.
4. Ran `tests/test_pipeline.py` both on an untouched `git archive HEAD` baseline
   and on the working tree with the pinned Python dependencies: **the same 26
   failures occur on both**. They concern model dataset/class counts, refitted
   coefficients, validation/calibration metrics, and RBI evidence calculations.
   These are not resolved by copying the UI. The new feed warns about them at
   the top and beside every estimate. No model artifact or published performance
   number was rewritten to make the tests pass.
5. JavaScript syntax checks and `git diff --check` pass.

## What was NOT verified

- Direct sandbox HTTPS to statsapi.mlb.com failed with a TLS connection error.
  The separate page-fetch tool retrieved the registry, but that does not prove
  end-to-end live browser access or fresh game-payload correctness.
- Chromium download failed with ECONNRESET. The DOM integration checks passed;
  they are not a real-browser visual or CORS test.
- No new accuracy, calibration, overturn probability, or replay outcome claim
  is established. The existing model's audit must be reconciled before its
  percentages can be treated as validated probabilities.
- This is a static-site implementation. Observation logs survive in the same
  browser (subject to storage availability); there is no all-users always-on
  collector and no guarantee of observing changes while the page is closed.
- ABS retains the upstream separate tab; the All view excludes ABS. Batted-ball
  rows and related review rows can both appear, representing different views of
  the same play. Review statistics count review entries, not batted balls.
- Changes are on the Arena working branch, not deployed or merged into main.

## Next accuracy work

Reconcile the committed model's training window with its dataset, rebuild model
and RBI artifacts reproducibly, and rerun the complete audit. For an actual
“error will become a hit/FC/out” forecast, collect timestamped initial/final
rulings and use time-separated held-out testing on those transitions. A final
batted-ball label classifier alone cannot validate that forecast.

---

# Simplification pass — 2026-10-02 (later the same day)

Following the brief ("the site is very confusing… give a simple score on whether
the model thinks that an error will be rescored to a hit, and official pending
scoring only"), the vendored UI's two entry pages were **rewritten** as the
simplified scoring watch. `pbp/index.html` is now a scoreboard with one card per
game and three scoring badges; `pbp/reviews.html` is now a scoring feed that
renders **only** official-scorer pending rulings, plays currently ruled an
error, and observed classification changes — no ABS, no challenges, no
umpire reviews, no boundary calls, no every-batted-ball rows.

The one score per row is the committed model's hit probability from the
11-outcome head (hit = single + double + triple + home run), rendered as
`HIT nn%` with the full distribution. On an error-ruled row the same number is
the honest reading of "could this be rescored to a hit": the probability that a
batted ball of this shape ends up scored a hit — never the probability that an
issued ruling changes. Verdicts (✓/✗) are computed only from the prediction
snapshot the browser ledger stored while a ruling was pending; rows without a
snapshot say so instead of getting a retroactive verdict. When the model file
cannot be loaded, rows still render with `Score unavailable` — never guessed
numbers (this required a small guard in `docs/site.js scoreLiveFeed()`, which
now returns the unscored row with the reason instead of throwing; the scorer's
math and the Python-mirror parity test are unchanged).

Both pages run on the same data layer as the existing full pages
(`docs/live-board.js` → `docs/site.js` + `docs/scoring-feed.js`), so the
scoreboard, this feed and `allgames.html` cannot disagree about a play.

## Verification performed in this pass

1. `npm test` passes: six rewritten integration tests (pure scoring-watch
   logic; feed page renders only scoring rows from the committed official
   capture; a synthetic pending → resolved cycle proving the verdict comes from
   the pre-ruling snapshot and the day tally matches the ledger; model-file
   failure leaves rows visible without numbers; scoreboard boot) plus the two
   upstream-origin suites (`tests/pbp/scoring-change-test.mjs`,
   `tests/pbp/official-scoring-test.mjs`), which still read the retained
   upstream modules.
2. `python3 tests/test_pipeline.py` reports exactly the same **26** pre-existing
   baseline failures documented above — no new failures. The two navigation
   assertions were updated because the shared nav labels changed
   (`scoreboard.html` → "Full scoreboard", `allgames.html` → "All-games feed");
   the checks still verify those pages are wired into the shared navigation.
3. Live registry re-check via the page-fetch tool, 2026-10-02:
   `GET /api/v1/eventTypes` again lists exactly `os_ruling_pending_primary`
   (plate appearance) and `os_ruling_pending_prior` (base running) with the
   description "Official Scorer Ruling Pending". The 2026 model's
   `event_type_groups` were re-read from the committed `docs/data/model.json`
   and the feed's outcome categories use exactly those groups.
4. End-to-end scorer check on live 2026 postseason data (game 849844,
   2026-10-01 Wild Card, fetched via the page-fetch tool from
   statsapi.mlb.com): four real batted balls transcribed verbatim into the
   shared `scoreLiveFeed()` — a sharp line-drive single scores HIT 62.3%
   (top pick single), a lazy flyout 10.2%, a groundout 16.8%, a sharp flyout
   24.1%; a pending-marker play scores while undecided; with no model the same
   row survives unscored with the reason on the row.
5. Schedule re-check: no MLB games 2026-10-02 (postseason off day); the slate
   fallback shows the most recent slate with games (Oct 1 Wild Cards), labelled
   on the page — nothing is silently re-dated.

## What was NOT verified in this pass

- Real-browser rendering/CORS (sandbox cannot reach statsapi.mlb.com directly);
  the same limitation as the first integration, unchanged.
- The verdict ✓/✗ tally accumulates only in the browser that observed the
  pending marker before its ruling (per-browser localStorage); it is not a
  backfilled or global accuracy number.
- No new accuracy, calibration, or overturn-probability claim is established.
  The 26 baseline audit failures remain the gating work item.
- Changes are on the Arena working branch, not deployed or merged into main.
