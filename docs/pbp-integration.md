# Focused PBP scoreboard and scoring feed

## Scope

The first-use flow is limited to an all-games scoreboard and one focused feed:

- `docs/pbp/index.html` shows the selected date's scheduled games, score, inning,
  and game status. Its page behavior is in `docs/pbp/assets/js/scoreboard.js`.
- `docs/pbp/reviews.html` shows current official `field_error` plays, exact
  `os_ruling_pending_primary` scorer markers, and error-involving scoring
  classification changes actually observed between browser polls. Its feed
  behavior is in `docs/pbp/assets/js/reviews-feed.js`.
- Manager challenges, umpire/replay reviews, ABS, boundary calls, prior
  base-running pending markers, and ordinary non-error batted balls are not
  admitted as visible feed rows. The review parser is reused to identify
  official pending markers, but unrelated parser output is filtered out.

The browser reads the public MLB schedule and play-by-play endpoints. A
classification change is an observed difference between two captures; a
final-only capture cannot establish what the earlier call was.

## What the model output means

`docs/pbp/assets/js/scoring-model.js` uses the committed model's 11-class
`event_type_model` distribution and groups it as Hit, Error, Fielder's choice,
and Out / sacrifice. For an active exact-primary pending play, the panel shows
the model's leading group and detailed class. For a current official Error, it
shows the final-hit-label score and the distribution.

That score is **not** the probability that an existing Error call will be
changed. The committed model predicts a final captured label; it was not fit or
validated on initial-call-to-final-ruling transitions. A real rescore forecast
needs transition observations and separate validation. Model estimates are
experimental and are not official scorer decisions.

A current official Error stays visible when Statcast measurements or the model
file are unavailable; the panel gives no guessed number. Missing measurements
also suppress estimates on eligible pending plays.

## Verification

- `npm ci && npm test` passes: 8 DOM integration tests plus the official
  scoring-change and pending-marker regression suites. Coverage includes the
  official fixture, no-vector errors, exact primary-vs-prior marker filtering,
  missing model files, duplicate suppression, error-involving observed changes,
  and a scoring change that remains visible when a replay-review row is hidden.
- JavaScript syntax checks and `git diff --check` pass.
- The current `tests/test_pipeline.py` run reports 26 failures in sections C
  (model/data recomputation) and O (RBI evidence recomputation), including
  `n_model` 30,206 vs. 30,207 and dependent metrics/tables. The focused page and
  feed contract sections pass. These data/model reproducibility discrepancies
  are not fixed by the UI integration.
- GitHub Actions' `audit` check also fails in its reproducibility step: it
  reports byte differences in generated live/RBI/site KPI artifacts and
  fixture-row differences. The full run-all audit therefore needs the generated
  artifact discrepancies reconciled before it can be green.
- These DOM tests do not establish real-browser visual behavior or current
  live API/CORS availability. The static pages cannot observe a change while
  closed and do not provide a shared always-on collector.

## Source attribution

The MIT-licensed MLB-Live-PBP source and pinned upstream revision are recorded
in [`docs/pbp/UPSTREAM.md`](pbp/UPSTREAM.md). Only the scoreboard and feed entry
pages were simplified for this focused workflow; other research and per-game
pages remain separate from first use.
