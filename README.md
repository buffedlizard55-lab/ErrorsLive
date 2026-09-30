# LiveScoringErrors

Can an MLB official scorer's decision — **hit vs. error vs. fielder's choice** — be predicted from
Statcast batted-ball physics, in real time? This repository contains the data pipeline, the
statistical model, and the public site.

**Site (GitHub Pages):** https://buffedlizard55-lab.github.io/LiveScoringErrors/
**Sister dataset researched:** https://github.com/nydailynews/mlb-overturned-calls (2014–2018 manager-replay overturns)

---

## Section 0 — the original brief (verbatim)

> **VERBATIM — DO NOT EDIT. Read this at the start of every session.**
>
> I want to investigate if there is a way to accurately predict the outcome of any scoring decision such as a pending scoring decision, or if we can tell if an error would be overturned into another play such as a fielders choice or a hit.  There's also the possibility of if the play is initially ruled an error that the batter gets no RBI if there is a runner on 2nd or 3rd.  However if they rule it as a fielders choice or a hit, there's a chance that the batter would be awarded an RBI.
>
> Review the repo.
>
> I want to create a website that does the following:
>
> I want to investigate if there is a way to accurately predict the outcome of any scoring decision such as a pending scoring decision, or if we can tell if an error would be overturned into another play such as a fielders choice or a hit.  There's also the possibility of if the play is initially ruled an error that the batter gets no RBI if there is a runner on 2nd or 3rd.  However if they rule it as a fielders choice or a hit, there's a chance that the batter would be awarded an RBI.
>
> Let's see if these sites help with figuring out a game plan and then executing it so that we get the best scientific model that can generate a live score out of 100 for live events.  Like if a ball gets hit, how will it be scored, let's see if baseballsavant live play by play batted ball game stats could help us.  Use advanced correlation methods to generate live scoring.
>
> `[[https://github.com/buffedlizard55-lab/MLB-overturned-calls](https://github.com/buffedlizard55-lab/MLB-overturned-calls)](https://github.com/buffedlizard55-lab/MLB-overturned-calls](https://github.com/buffedlizard55-lab/MLB-overturned-calls))`
>
> It should be pretty straight forward, research and understand all the MLB overturned calls, and then find the videos that correspond to the play that resulted in a run being removed from the score.  It should be as easy as click to download or make it so that i can click it and watch it in my browser.
>
> Put this prompt into the repo readme and read it everytime we work on the project as a starting point to make sure we are building what we are aiming for and have a strong base to continue building and improving on making something useful for everyday use.  It should solve the problem of having to manually check everything ourselves and have an up to date current feed.
>
> Review the repo.
>
> The following is taken from the Arena AI team and I think it makes a good point on building a successful project, so let's keep the Core Values and Own the Outcome as a focal point when building, developing, researching, suggesting upgrades, and implementing the work.
>
> **Own the Outcome**
>
> We own results end to end — not just our individual slice of the work. When problems arise and we have the means to act, we do so without waiting for permission or assignment. We treat failure and success as signals and use them to improve. At Arena, we stay accountable to the final outcome.
>
> Work line by line verifying from official verified trusted sources, provide links for manual review.  There should be no manual input, work on your own to complete tasks.  Flag any irregularities for review.  No hallucinations.
>
> Verify no hallucinations.
>
> The goal of this project is to get a full list that follow our requirements.  No hallucinations.  Verify line by line.
>
> Site creation
>
> Create a github page for this repo that has clean ui, user friendly, simple and easy to use.
>
> It should be organized and clean.  It should include all relevant information in an easy to read format with official verified links as sources for review.  Work line by line verify everything no hallucinations.
>
> Go ahead and create a pull request and then merge the pull request onto the main. Make suggestions for what work still needs to be done and any limitations that is in the way of a successful project.  It should be worked on in this next session or the next session.  Work line by line verify everything no hallucinations.
>
> Run this task through multiple passes.
>
> Pass 1: Implement the task completely and verify the result.
>
> Pass 2: Review your work for bugs, missing requirements, incorrect assumptions, and edge cases. Fix everything you find.
>
> Pass 3: Re-check the entire implementation against the original request. Improve accuracy, reliability, completeness, and code quality. Fix any remaining issues.
>
> Do not stop after the first pass. Each pass must build on the previous one. Before finishing, verify that the final result fully satisfies the original request.  Work line by line verify everything no hallucinations.

### Editorial verification notes (not part of the brief)

1. The brief repeats its opening scoring-decision paragraph verbatim; both copies are retained above. The malformed GitHub link is preserved as supplied rather than repaired inside the quoted brief.
2. The named `buffedlizard55-lab/MLB-overturned-calls` repository returned HTTP 404 in a GitHub API check recorded in the earlier review. The 2014–2018 archive used here is the separate public `nydailynews/mlb-overturned-calls` repository. The names are not treated as interchangeable.
3. A network-dependent re-check is in `tools/probe_api.py`; an offline test cannot independently re-contact GitHub or MLB.

---

## What this answers, and where

| # | Question from the brief | Current answer and verification path |
|---|---|---|
| a | Can a batted ball be classified as hit / error / fielder's choice / out before the feed settles? | The model estimates the final captured feed label; grouped out-of-fold ROC AUC is **0.7808** (95% CI **0.7542–0.8056**) and average precision is **0.02652** against a **0.798%** error-rate baseline. It never nominates “error” as the top multiclass call, so the score is a review ranking, not a ruling or a forecast of a later scorer change. See `docs/model.html` and `docs/data/model.json`. |
| a′ | Can the site identify a scorer's internal pending queue? | No public queue is exposed. `tools/watch_rulings.py` records only the observable API state `hitData` present with no `result.eventType`, then records later feed-value differences. A difference proves only that captured fields changed between observations—not that a decision was pending, why it changed, or exactly when. See `docs/methods.html` and `data/ingest/ruling_changes.csv`. |
| b | What are the RBI consequences of error vs. hit vs. fielder's choice? | `docs/rules.html` links the 2026 Official Baseball Rules, MLB's error/RBI glossaries, and MLB scoring-change examples. Live RBI notes are conditional Rule 9.04 reminders; they do not settle the scorer's counterfactual. The 2026 PDF is linked at Rule 9.04, but the full p.115 text was not independently extracted in this review; that verification gap is listed below. |
| c | Can each live batted ball receive a score out of 100? | Yes: `100 × P(final captured feed label = error)`, with class probabilities and pre-pitch context. `docs/index.html` has a calculator; `docs/live.html` attempts direct browser polling; `tools/live_score.py` is the offline CLI twin. Tests compare both implementations on the named model data. |
| d | Can users inspect overturned calls and watch/download video for run-impact cases? | `docs/replays.html` indexes the ingested 2026 review window (**11,528** reviews, **6,149** marked overturned). **17** overturned rows are heuristic run-impact candidates; **0** passed the separate movement/score consistency diagnostic. Candidate rows carry watch/download links where returned by the video probe. Neither count is a verified list of runs removed. The 2014–2018 archive is on `docs/overturned.html`. |
| e | Is there a GitHub Pages site? | Yes: `/docs`, published at <https://buffedlizard55-lab.github.io/LiveScoringErrors/>. |
| f | Is “Own the Outcome” a focal point? | The home page and methods page state it; in practice the pipeline records flags and failed fetches instead of silently smoothing them over, and the site surfaces model limits beside headline metrics. |
| g | Can collection run with minimal manual checking? | Best-effort automation is configured: browser refresh about every 2 minutes while live, static fallback refresh on a 30-minute schedule, active ruling observations every 15 minutes, and a daily recheck of recent games. GitHub schedules are not guaranteed real-time delivery; see the workflows and limitations below. |
| h | What remains? | See `docs/roadmap.html` and “Limitations and next steps” below. |

## Data provenance — scope is explicit

- **Official play-by-play and current rulings:** `statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live` (trimmed with `?fields=`). The collector records source URLs, failures, and discrepancies in its report.
- **Final scores:** `statsapi.mlb.com/api/v1/game/{gamePk}/linescore` and the official schedule. In the committed 2026-03-25 through 2026-09-27 game ledger, **2,429 of 2,430** games matched the league linescore; game **823490** is flagged unverified, not silently counted as verified.
- **Collection vs. model scope:** `data/ingest/games.csv` spans 2,430 games from 2026-03-25 to 2026-09-27. `docs/data/bip_official.csv` is the selected full-population modeling window, 2026-08-15 to 2026-09-27: 30,298 batted balls from 590 games, of which 92 lack a complete vector and are quarantined. The model uses 30,206 complete rows (241 errors; 0.798%). The separate `docs/data/bip.csv` is a 24-game error-enriched audit set (1,271 rows, 24 errors); it is not used as a population estimate.
- **Per-play video:** Baseball Savant's `sporty-videos?playId={playId}` page; where the official content endpoint exposed a matching rendition, the direct MP4 is offered. The current run-impact-candidate set has 17 rows with video links. Video availability is not universal, and links are not a verdict that a run was removed.
- **Replay review evidence:** the modern ingest window has 11,528 review rows, 6,149 marked overturned, 17 heuristic run-impact candidates, and zero rows passing the independent runner-movement/score-delta consistency diagnostic. The heuristic count is not confirmed scoreboard impact.
- **Rules:** the official [2026 Official Baseball Rules PDF](https://mktg.mlbstatic.com/mlb/official-information/2026-official-baseball-rules.pdf), MLB's [Error](https://www.mlb.com/glossary/standard-stats/error) and [Runs Batted In](https://www.mlb.com/glossary/standard-stats/runs-batted-in) glossaries, and [MLB Official Scoring Changes](https://www.mlb.com/official-information/scoring-changes). The Rule 9.04(a)(3) prose is a conditional project summary, not a verbatim quotation; direct extraction of PDF page 115 remains an open verification item.
- **2014–2018 replay archive:** `github.com/nydailynews/mlb-overturned-calls`, vendored under `data/source/nydn/` so the build works offline. This is not the repository named in the user's malformed link; that path previously returned 404 and should be network-checked again before relying on it.

## Repo layout

```
tools/            collectors, builders and scorers
  ingest_official.py   scheduled official collector and review/video index builder
  fetch_live.py        today's slate -> docs/data/live_slate.json (+ CSV); live_now is an offline fixture
  refresh_live.py      complete-snapshot fallback refresh with failure retention
  watch_rulings.py     rolling feed observations -> ruling_snapshot.csv / ruling_changes.csv
  probe_api.py         network-dependent endpoint, status, and CORS probe
  build_dataset.py     the 24-game audit sample from data/raw/*.json
  train_model.py       model fit, grouped/random CV, AP, risk bands, calibration
  build_site_data.py   derives compact page-ready JSON from committed tables
  live_score.py        pure-Python scorer + CLI (mirror of browser implementation)
  run_all.py           offline rebuild of reproducible artifacts
data/raw/           24 validated feed JSONs + integrity notes (audit trail)
data/source/        live-board fixture feed + vendored NYDN CSVs
data/ingest/        ingest plan, game ledger, reports, rolling ruling snapshots/changes
docs/               GitHub Pages site (8 pages + shared CSS/JS)
docs/data/          model, audit data, replay/video tables, live snapshots and page JSON
tests/test_pipeline.py   offline audit suite (exit 0 = every check passed)
.github/workflows/   ingest/probe/audit plus scheduled live-refresh and ruling-watch jobs
```

## Reproduce

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python tools/run_all.py
.venv/bin/python tests/test_pipeline.py
```

`tools/run_all.py` is offline and rebuilds the model, demo feed, and compact page data from committed inputs.
Network collection and CORS checks run in GitHub Actions; the live-refresh and ruling-watch workflows maintain
best-effort snapshots after they are enabled on the default branch. No local rebuild can prove that a scheduled
external fetch will keep succeeding.

`/.github/workflows/audit.yml` re-runs `tools/run_all.py` on every push and then runs
`tools/check_repro.py`, which enforces the reproducibility policy in the open:

- **Every derived table must be byte-identical** after the rebuild — `docs/data/*.csv`, the page JSON, the NYDN
  artifacts, the verification ledger. A hand-edited number, a changed rule or a non-deterministic sort fails the build,
  and the failure is reported as a GitHub annotation (readable through the API) because job-log download is unreliable
  for some clients.
- **`docs/data/model.json` is compared numerically**, with a stated 1e-6 tolerance and an exact comparison of every
  structural field. A logistic solver's last digits follow the BLAS build it is linked against — scikit-learn's
  multiclass fit ended 3e-9 apart on the dev machine and the CI runner with identical inputs and pinned package
  versions, which a byte-equality gate would report as a defect. Six orders of magnitude of headroom above that noise,
  and any material change (a coefficient, a CV number, a missing feature) still fails.

`tools/train_model.py` also pins itself to a single BLAS/OMP thread before importing numpy, so the numbers a reviewer
sees locally are the numbers CI produces.

Serving the site locally: `cd docs && python3 -m http.server 8080` → <http://localhost:8080/>.

## Headline results (recomputed from committed inputs)

- **Ingested 2026 window:** 2026-03-25 through 2026-09-27: 2,430 games in the ledger, 2,429 verified against the official linescore and one flagged unverified (game 823490). The reviewed-play ledger has **11,528** reviews, **6,149** marked overturned, **17** heuristic run-impact candidates, and **0** movement/score consistency discrepancies. Candidate status is not proof of a removed run.
- **Model window:** 30,206 complete batted balls from 590 games (2026-08-15 through 2026-09-27), 241 error labels, and an observed 0.798% error prevalence. Grouped by-game OOF ROC AUC **0.7808** (95% CI **0.7542–0.8056**); stratified random-fold comparator **0.7774**; grouped gradient-boosting comparator **0.7757**. Average precision is **0.02652** vs **0.00798** prevalence. Raw Brier is **0.007837**; nested isotonic Brier is **0.007867** (no improvement in this run). Four-class top-1 accuracy is **0.7471**, but the model never nominates error at top-1 and OOF error recall is **0.000**.
- **Grouped OOF review queues:** highest-scored 1% contains 18 errors among 303 plays (5.94% precision, 7.47% of errors found); top 5% finds 47/241 errors (19.50% recall); top 10% finds 77/241 (31.95% recall). These are retrospective held-out results in the collected window, not future-season guarantees.
- **Live score:** the browser tries the official API directly; a committed static snapshot is a fallback, not guaranteed current. The offline fixture for game 823441 is a regression demo, not live data.
- **2014–2018 archive:** 6,361 rows, 3,067 marked overturned, 230 archive rows labelled run-affected by that source's own fields; source irregularities and video link states are documented in `data/nydn_quality.json` and `docs/data/nydn_links.csv`. This archive is separate from the modern Stats API review window.

## Review passes

- **Pass 1 — implementation:** built the official-data collector, classifier, live score, replay/video index, pages, and scheduled workflows.
- **Pass 2 — evidence and edge cases:** corrected scoring/RBI wording, separated feed-state observation from claims about a scorer queue, labelled run-impact rows as heuristic candidates, and published grouped OOF average precision/top-risk-band metrics.
- **Pass 3 — final acceptance:** in progress until the rebuilt artifacts, complete offline audit suite, page-data contracts, workflow configuration, and PR/merge state are verified. Results and any blockers will be updated before finishing.

## Limitations and next steps

1. **Rule 9.04(a)(3) full-text verification remains open.** The official 2026 PDF is linked at page 115 and its table of contents identifies Rule 9.04 there, but automated retrieval in this review stopped before the official scorer section. The site does not quote that subsection verbatim; confirm its full wording directly in the linked PDF before treating the exception summary as fully verified.
2. **No public internal pending-decision queue.** Missing `result.eventType` is only an observable feed state. A later difference between two API captures does not prove that a scorer decision was pending, when it was made, or why.
3. **Run removal is not established for the modern rows.** The official feed exposes the final reviewed ruling, not a reliable before-review scoreboard. The 17 rows are heuristic candidates; the separate movement/score consistency diagnostic found zero rows. Neither is a verified list of runs removed.
4. **The model predicts the final captured macro-class, not reclassification.** It does not estimate whether an initial call will be overturned, does not include fielder positioning, hang time, or runner speed, and has zero error recall at its multiclass arg-max. The intended use is to rank plays for review.
5. **The model window is narrow.** It covers 590 games from 2026-08-15 through 2026-09-27, not the full season or future seasons. Its grouped cross-validation holds out games within that same window; it is not external validation.
6. **CORS and reachability are endpoint-specific.** The committed probe report records `Access-Control-Allow-Origin: *` for one schedule request. The updated probe separately checks the game-feed endpoint; its result must be reviewed before asserting that browser feed reads work. If blocked, the site shows a timestamped static snapshot.
7. **Refreshes are best-effort, not real time.** The browser polls live games about every two minutes; GitHub Actions refreshes the fallback every 30 minutes and observes active ruling feeds every 15 minutes, with a daily recent-final recheck. GitHub schedule delays, API changes, rate limits, or failed credentials/network can leave a stale snapshot; failures are written to status files.
8. **Video is not universal and is not re-hosted.** The current 17 candidate rows have watch/download links from the video probe, but other plays and the 2014 archive may have missing, dead, or unresolved links. Link presence is not evidence that a run was removed.
9. **PR/merge completion is not implied by this README.** The session branch is fixed by Arena. The pull request and merge status will be reported only after GitHub confirms those actions.

See `docs/roadmap.html` for the ordered backlog and evidence ledger. Priority next steps: verify the complete 2026 Rule 9.04 text; run the updated audit/reproducibility suite; validate the scheduled workflows and current CORS probe; then open the required PR from the Arena branch and merge only after checks pass.
