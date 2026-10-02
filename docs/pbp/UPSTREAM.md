# Source and local changes

Copied from https://github.com/buffedlizard55-lab/MLB-Live-PBP at
`7eac3717fa1feea1053591e3a961b809728c373e` on 2026-10-02.

The MIT license is preserved in `LICENSE`. Files copied: `index.html`,
`reviews.html`, `game.html`, and `assets/`. The two upstream scoring test files
are retained under `../../tests/pbp/` with their import paths adapted.
No upstream game logs, backend, Git metadata, or deployment workflows were copied.
Historical assertions in upstream comments are upstream documentation, not claims
that this integration reverified every example.

Local modifications:
- ErrorsLive navigation/branding and model styling.
- The play-by-play projection includes Statcast hitData fields and playId.
- `scoring-model.js` calls the existing ErrorsLive scorer/model; no retraining.
- The replay feed displays current batted-ball captures alongside review rows,
  outcome filters, and model panels for pending/change rows. Model rows stay out
  of official review counters and the official observation ledger.
- Model state clears on date changes, deduplicates by game/at-bat, and replaces
  vanished current rows. Restored settled games receive a fresh model capture.
- Official logs use an ErrorsLive-specific browser-storage namespace. Server
  logging/SSE is disabled: GitHub Pages cannot run the upstream Node server.
- Live and review polling gaps are 5 seconds, with four concurrent PBP fetches;
  the replay schedule TTL is 15 seconds. Network duration may extend these gaps.
- Prominent warnings disclose the unresolved baseline validation audit.

Read ../pbp-integration.md for verification results and limitations.
