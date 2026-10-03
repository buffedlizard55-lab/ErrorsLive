# Source and local changes

Copied from <https://github.com/buffedlizard55-lab/MLB-Live-PBP> at
`7eac3717fa1feea1053591e3a961b809728c373e` on 2026-10-02.

The MIT license is preserved in `docs/pbp/LICENSE`. The vendored scoreboard,
feed, game page, assets, and upstream scoring regression tests retain their
source attribution. No upstream game logs, backend, Git metadata, or deployment
workflows were copied. Historical assertions in upstream comments are source
documentation, not claims that this integration reverified every example.

## Local focused first-use pages

- `index.html` is a simple all-games scoreboard using the public schedule and
  `assets/js/scoreboard.js`.
- `reviews.html` uses `assets/js/reviews-feed.js` and the `ScoringModel` adapter
  in `assets/js/scoring-model.js`. It displays current official field errors,
  exact `os_ruling_pending_primary` markers, and only error-involving
  classification changes observed between polls.
- Manager challenges, replay/umpire reviews, ABS, boundary calls, prior
  base-running pending markers and ordinary non-error plays are excluded from
  the visible feed. `reviews.js` is reused to parse official markers, but its
  unrelated review rows are not admitted to the feed.
- The model panel groups the committed 11-class final-label distribution.
  Its score on an Error row is **not** a probability that the Error call will
  be changed; the model has no validated ruling-transition forecast. Missing
  measurements or a failed model fetch never produce a guessed score, and the
  official Error row remains visible.

The browser-local observation log is not an always-on or cross-user collector.
`docs/pbp-integration.md` records the feed contract, test results, unresolved
model/data audit discrepancies, and remaining verification limits.
