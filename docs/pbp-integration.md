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
