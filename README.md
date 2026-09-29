# LiveScoringErrors

Can an MLB official scorer's decision — **hit vs. error vs. fielder's choice** — be predicted from
Statcast batted-ball physics, in real time? This repository contains the data pipeline, the
statistical model, and the public site.

**Site (GitHub Pages):** https://buffedlizard55-lab.github.io/LiveScoringErrors/
**Sister dataset researched:** https://github.com/nydailynews/mlb-overturned-calls (2014–2018 manager-replay overturns)

---

## Section 0 — The original brief (verbatim)

> 1. Investigate whether MLB official-scoring decisions can be predicted:
>    (a) predict the outcome of pending scoring decisions — whether an error gets overturned into a
>    fielder's choice or a hit;
>    (b) RBI impact — an error ruling means no RBI with a runner on 2nd/3rd, but a fielder's choice
>    or hit may award an RBI (cite MLB Rules 9.12 / 9.16 / 10.04);
>    (c) build the "best scientific model" producing a live score out of 100 for live events
>    ("if a ball gets hit, how will it be scored") using Baseball Savant live play-by-play batted-ball
>    stats and "advanced correlation methods";
>    (d) research all MLB overturned calls from
>    https://github.com/buffedlizard55-lab/MLB-overturned-calls and find videos for plays where a run
>    was removed — click-to-download or click-to-watch in the browser;
>    (e) create a GitHub Pages site for buffedlizard55-lab/LiveScoringErrors: clean, user-friendly,
>    organized UI with all relevant info and official verified links;
>    (f) "Own the Outcome" core values from the Arena AI team as a focal point;
>    (g) create a pull request and merge it onto main;
>    (h) make suggestions for remaining work / limitations for the next session;
>    (i) run 3 passes (implement+verify → bug/edge-case review+fix → re-check vs. the original
>    request + improve); do not stop after pass 1.
>
> Standing constraints: no hallucinations — verify line by line from official, verified, trusted
> sources and provide links for manual review; no manual input — work autonomously and flag
> irregularities; re-read this brief at the start of every session; the site must be GitHub Pages
> with a clean/simple UI and official verified source links; create the PR and merge to main; run
> all 3 passes; solve the problem of manually checking everything and aim for an up-to-date live feed.

---

## What this answers (and what it carefully does not)

| # | Question | Where |
|---|----------|-------|
| a | Can call outcomes (hit / error / FC / out) be predicted from EV/LA/trajectory *before* the scorer rules? | `docs/model.html`, `docs/data/model.json` |
| b | RBI stakes of the hit-vs-error distinction | `docs/rules.html` (MLB Rules 9.12, 9.16(a), 10.04 — official links) |
| c | Live "Scoring Likelihood Score /100" per batted ball | `docs/index.html` (calculator), `docs/data/model.json` |
| d | Overturned replay calls 2014–2018 + run-removal subset + watch links | `docs/overturned.html`, `docs/data/overturned_calls.csv` |
| d† | **Video status** — the 2014–2018 source links are bit.ly short links to MLB.com video pages; many MLB video URLs from that era now redirect or 404. The site provides click-to-watch links and marks each link's last-checked state. | same |
| e | Pages site | this repo `/docs` |
| f | "Own the Outcome" values | `docs/index.html` footer + `docs/methods.html` |
| h | Limitations & next-session roadmap | `docs/roadmap.html` |

## Data provenance (audit trail)

- **Source of record:** MLB Stats API — `statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live`
  (per-play `hitData` launch speed/angle/distance/trajectory/hardness) and
  `statsapi.mlb.com/api/v1/schedule?...&hydrate=linescore` (final R/H/E ground truth).
- **Population:** all 222 games played on 15 dates in June 2026 (pool), 247 team-error records,
  1.113 team-errors/game — `data/pool_summary.json`.
- **Sample:** stratified by team-error count (A: ≥3, B: =2, C: ≤1), then trimmed per the same
  fraction rule; **final N = 24 games** (A 10, B 10, C 4 — see `data/TARGETS.json`).
  Every stratum is sampled with a uniform per-stratum fraction, so pool-level statements about this
  sample need no post-hoc weights. Verified: `tests/test_pipeline.py` asserts the manifest set equals
  the archived set and that all 24 final scores re-match the pool.
- **Integrity rules:** every game's parsed final score is re-verified against the linescore pool;
  chunk-boundary losses are never repaired by invention — the affected straddling play is dropped and
  logged (`data/raw/*.integrity.txt`). Parser: `tools/build_dataset.py`.

## Repo layout

```
tools/            Pipeline sources (build_dataset.py, train_model.py, build_nydn.py, live_score.py,
                  run_all.py, verify_official.py, fetch_feed.py)
data/raw/         24 validated per-game feed JSONs + 4 integrity notes (audit trail; heals logged, never invented)
data/*.json       pool_summary.json, TARGETS.json, data_quality.json, nydn_quality.json (structured flag summary)
data/source/      feed_823441.json (the published live-board fixture) + nydn/ (9 vendored archive CSVs)
docs/             GitHub Pages site (index, live, model, overturned, rules, methods, roadmap + site.css/site.js)
docs/data/        Site artifacts: bip.csv (1,271 rows), games.csv (24), model.json, verification.json,
                  overturned_calls.csv (6,361), nydn_summary.json, live_sample.json/csv
tests/            test_pipeline.py — audit suite: raw-set, scores, model internals, page↔data consistency, NYDN
```

## Reproduce

```bash
pip install -r requirements.txt
python3 tools/run_all.py          # rebuilds docs/data/* from data/raw/*.json (exit 0 on green)
python3 tests/test_pipeline.py    # full audit suite (exit 0 = all checks pass)
python3 tools/verify_official.py  # re-check all 24 games + the 222-game pool against the live API
```

`run_all.py` is **byte-for-byte reproducible**: run it twice and `git diff` is empty. CI
(`.github/workflows/audit.yml`) enforces that on every push, then runs the audit suite and a
live-scorer smoke run.

Serving the site locally for review: `cd docs && python3 -m http.server 8080` → http://localhost:8080/.
Live-fetching new games from the Stats API is documented in `tools/fetch_feed.py`; scoring a live
feed is `tools/live_score.py` (see [docs/live.html](docs/live.html)).

## Headline results (verified 2026-09-29)

- 24 games / 1,868 plays / **1,271 batted balls** with Statcast vectors; **24 labeled errors** (1.91% base rate),
  every one of them with a complete Statcast vector (`errors_without_hitdata: 0`, `multi_hitdata: 0` in
  `data/data_quality.json`). 1,259 balls have a full numeric vector; 12 are quarantined out of the model rather
  than imputed.
- Binary P(error) model: OOF **AUC 0.641 [0.518, 0.748]**, Brier 0.0186; multinomial per-class AUC
  hit .78 / error .65 / FC .78 / out .77. Strongest feature: `trajectory = ground_ball` (+0.69 standardized CI-free coef).
- Replay archive: **6,361** rows 2014–2018, **3,067 overturned**, **230 run-affected** (printed heuristic);
  2,688 source irregularities categorised in `data/nydn_quality.json`. Every one of the 6,362 source data
  lines is accounted for (6,361 rows + 1 junk header fragment).
- Live board: game 823441 (NYY @ NYM, 1–6) — 75 plays, 46 batted balls scored, the model's top pick agrees
  with the official ruling on 34 of 46; the highest-scoring ball in the game (6.4/100) is its only ruled error.
  Full-season backtest: 918/1,259 top picks (72.9%), 0/24 errors flagged — which is the honest headline:
  the model is useful for *ruling out* errors, not for finding them.

## Passes (user requirement i)

- **Pass 1** — implemented + verified (final scores cross-checked vs. the 222-game linescore pool;
  pipeline green; site shipped).
- **Pass 2** — review found and fixed: missing-hitData quarantine, sklearn 1.9 compat, NYDN vocab gap,
  link rot (rules hub 404 / video decay — fallbacks added), four integrity-log gaps closed
  (`data/raw/*.integrity.txt`), plus a dedicated page↔data audit suite.
- **Pass 3** — re-check vs. Section 0 letter by letter; residual limitations and the ordered next-session
  backlog live on `docs/roadmap.html`.
