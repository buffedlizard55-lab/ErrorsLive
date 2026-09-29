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
> I want to create a website that does the following:  I want to investigate if there is a way to accurately predict the outcome of any scoring decision such as a pending scoring decision, or if we can tell if an error would be overturned into another play such as a fielders choice or a hit.  There's also the possibility of if the play is initially ruled an error that the batter gets no RBI if there is a runner on 2nd or 3rd.  However if they rule it as a fielders choice or a hit, there's a chance that the batter would be awarded an RBI.
>
> *[The brief repeats that paragraph here, word for word — it is reproduced as written; see the editor's note below.]*
>
> Let's see if these sites help with figuring out a game plan and then executing it so that we get the best scientific model that can generate a live score out of 100 for live events.  Like if a ball gets hit, how will it be scored, let's see if baseballsavant live play by play batted ball game stats could help us.  Use advanced correlation methods to generate live scoring.
>
> `[[https://github.com/buffedlizard55-lab/MLB-overturned-calls](https://github.com/buffedlizard55-lab/MLB-overturned-calls)](https://github.com/buffedlizard55-lab/MLB-overturned-calls](https://github.com/buffedlizard55-lab/MLB-overturned-calls))`
> *(as received: a quadruple-wrapped link. Canonical target:  `https://github.com/buffedlizard55-lab/MLB-overturned-calls`)*
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
>
> **Editor's notes (facts, verified 2026-09-29 — not part of the brief):**
> 1. The brief was sent as one message with the opening paragraph posted twice; the second copy is
>    marked above rather than silently deleted.
> 2. `github.com/buffedlizard55-lab/MLB-overturned-calls` **does not exist**: the GitHub REST API
>    returns HTTP 404 for that path (`gh api repos/buffedlizard55-lab/MLB-overturned-calls`), as does
>    the HTML page. The archive the brief describes is the NY Daily News one,
>    `github.com/nydailynews/mlb-overturned-calls` (HTTP 200, public, 2014–2018). Both facts are
>    re-checked by `tests/test_pipeline.py` where they can be checked offline, and by
>    `tools/probe_api.py` where they need the network.
> 3. `MLB-overturned-calls` is a *sister* project name the brief's author controls; the pipeline in
>    this repo ingests the NYDN archive because that is the only overturn archive that exists.

---

## What this answers, and where

| # | Question from the brief | Answer, and where to check it |
|---|-------------------------|-------------------------------|
| a | Can a scoring decision be predicted — hit / error / fielder's choice / out — before the scorer rules? | Yes, better than chance and **not** well enough to replace a scorer. Grouped cross-validated AUC **0.781** on 30,206 batted balls from 590 official games; the model never once nominates "error" as its most likely call. `docs/model.html`, `docs/data/model.json` |
| a′ | Can a *pending* decision be watched? | Not seen directly — but a change can be detected the day it lands. `tools/watch_rulings.py` re-reads the official feed for a rolling window, diffs each ruling against the last observation, and appends every change (event type, RBI, description) to `data/ingest/ruling_changes.csv`. That table *is* the record of decisions that were still open. |
| b | The RBI stakes of error vs hit vs fielder's choice | `docs/rules.html`, quoting MLB's glossary **verbatim** with links — batters "do not receive RBIs for any runs that would not have scored without the help of an error"; no RBI when the run scores as a result of an error or a GIDP. Every scored row in the live board carries `rbi_if_error` / `rbi_if_hit` / `rbi_if_fc`. |
| c | A live score out of 100 per batted ball | `docs/index.html` (calculator), `docs/live.html` (slate board), `tools/live_score.py` (CLI). Two independent implementations — JavaScript in the browser and Python in the tool — are asserted to agree to 1e-9 by the audit suite. |
| d | All overturned calls, and video for the plays that removed a run | `docs/replays.html` + `docs/data/replays_site.json`: 11,528 reviewed plays, **6,149 overturned**, **17 flagged as removing a run** — each with a per-play watch page and a direct mp4 link when the league exposes a file rendition. 2014–18 archive on `docs/overturned.html`. |
| e | A GitHub Pages site | `/docs`, published at <https://buffedlizard55-lab.github.io/LiveScoringErrors/> |
| f | "Own the Outcome" as the focal point | Home-page footer block, `docs/methods.html`; and in practice: every number on the site is regenerated from official data, failures are published beside successes, and every irregularity the pipeline hits is written to an artifact instead of being smoothed over. |
| g | Up-to-date feed, no manual checking | `tools/ingest_official.py` (bulk history), `tools/fetch_live.py` (today's slate → `docs/data/live_now.json`), `tools/watch_rulings.py` (ruling changes). CI runs them; the site reads their output. |
| h | Remaining work and blockers | `docs/roadmap.html`, and the "Limitations" section below. |

## Data provenance — every number traces to an official endpoint

- **Play-by-play, rulings, reviews:** `statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live` (trimmed with `?fields=`).
- **Final scores used as ground truth:** `statsapi.mlb.com/api/v1/game/{gamePk}/linescore` and
  `statsapi.mlb.com/api/v1/schedule?...&hydrate=linescore`. Each ingested game is re-checked: the feed's own final
  score must equal the league's linescore, and any mismatch is written into `data/ingest/ingest_report.json` as a flag.
- **Per-play video:** `baseballsavant.mlb.com/sporty-videos?playId={playId}` (the play id comes from the league's own
  feed). The probe in `data/ingest/probe_report.json` records what that endpoint returned for real plays in 2014, 2017
  and 2026 — including the direct `sporty-clips.mlb.com/*.mp4` it embeds, which is what the download button uses.
- **Rules:** `mlb.com/glossary/standard-stats/error`, `.../runs-batted-in`, and the official rulebook PDF; quotations on
  `docs/rules.html` are word-for-word, dated, and linked.
- **2014–18 replay archive:** `github.com/nydailynews/mlb-overturned-calls`, vendored under `data/source/nydn/` so the
  build works offline. The repository named in the brief,
  `github.com/buffedlizard55-lab/MLB-overturned-calls`, **returns HTTP 404** (checked 2026-09-29 by `gh api` and by
  fetching the page). That is an irregularity in the brief, flagged rather than silently worked around.
- **Two datasets, never mixed:** `docs/data/bip.csv` is a 24-game, error-*enriched* audit sample (1,271 batted balls,
  24 errors) used to keep the historical record and to exercise the pipeline on a hand-checkable set;
  `docs/data/bip_official.csv` is the whole-population window sample (30,298 batted balls in 590 games (243 ruled errors; 241 of them with a complete vector),
  a real 0.80% error rate) that the published model is fitted on. The model metadata states which file it used.

## Repo layout

```
tools/            collectors, builders and scorers
  ingest_official.py   CI collector: schedule -> feeds -> bip_official.csv, replays.csv, replay_videos.csv,
                       nydn link resolution, ingest_report.json, docs/data/ingest_summary.json
  fetch_live.py        today's slate -> docs/data/live_now.json (+ CSV), same scorer as the site
  watch_rulings.py     rolling-window diff of official rulings -> ruling_snapshot.csv / ruling_changes.csv
  probe_api.py         records what each official endpoint really returns (CI; evidence committed)
  build_dataset.py     the 24-game audit sample from data/raw/*.json
  train_model.py       fits the published model on the ingested dataset (falls back to the audit sample)
  build_site_data.py   derives the compact, page-ready JSON from the collected tables
  live_score.py        pure-python scorer + CLI (mirror of the browser implementation)
  run_all.py           offline, byte-reproducible rebuild of every committed artifact
data/raw/           24 validated feed JSONs + integrity notes (audit trail)
data/source/        the live-board fixture feed + the vendored NYDN CSVs
data/ingest/        ingest plan, game ledger, reports, ruling snapshots/changes
docs/               the GitHub Pages site (8 pages + site.css/site.js)
docs/data/          published artifacts: bip.csv, bip_official.csv, replays.csv, replay_videos.csv,
                    overturned_calls.csv, nydn_links.csv, model.json, verification.json, site_kpis.json,
                    replays_site.json, nydn_site.json, live_sample.json, live_now.json
tests/test_pipeline.py   the audit suite (exit 0 = every check passed)
```

## Reproduce

```bash
pip install -r requirements.txt
python3 tools/run_all.py          # rebuilds model.json, live_sample.*, NYDN artifacts — byte-for-byte identical
python3 tools/build_site_data.py  # rebuilds the compact page data from the collected tables
python3 tests/test_pipeline.py    # the audit suite
```

Network collection runs in CI (`.github/workflows/ingest.yml`, `probe.yml`) because a sandboxed checkout has no
outbound route to `statsapi.mlb.com`; the collectors write the derived tables and CI commits them back. The site build
itself never needs the network, and `/.github/workflows/audit.yml` re-runs `tools/run_all.py` on every push and fails
if a single byte of a published artifact changed, so a hand-edited number cannot survive.

Serving the site locally: `cd docs && python3 -m http.server 8080` → <http://localhost:8080/>.

## Headline results (all recomputable; none typed by hand)

- **Ingested window** 2026-03-25 → 2026-09-27: **590 final games**, **11,528 reviewed plays** with
  **6,149 overturned**, and **17** of those overturned reviews flagged as removing a run from the scoreboard.
  Every game's final score was re-verified against the league's own linescore before it entered the dataset.
- **Model** (logistic, 18 features: exit velocity, launch angle, distance, trajectory, hardness, base state, outs,
  inning, batter/pitcher handedness, fitted on 30,206 batted balls from 590 games): grouped-by-game OOF **AUC 0.781**
  (95% CI 0.754–0.806), random-fold upper bound 0.778, gradient-boosted comparison 0.776 — the linear model is not
  leaving signal on the table, and the honest headline is the group-bootstrap interval, not a single number.
  Brier 0.0078, top-1 accuracy 0.747, **error recall 0.000**: the model is a filter, not a decision.
- **Live board**: game 823441 (2026-07-18, NYM @ PHI, 1–6) — 46 batted balls scored before the ruling; the model's top
  pick matches the official call on 32 (70%), and it never nominates "error".
- **Video**: the run-affected rows each carry a Baseball Savant per-play page (click to watch) and, where the league
  exposes a rendition, a direct `.mp4` (click to download). Rows without a rendition say so.
- **2014–18 archive**: 6,361 rows, 3,067 overturned, 230 flagged run-affected; every source line accounted for
  (6,361 rows + 1 junk fragment), 2,688 source irregularities categorised in `data/nydn_quality.json`, and every video
  short link resolved with its real HTTP status in `docs/data/nydn_links.csv` — dead links are labelled dead.

## Passes

- **Pass 1** — implemented: collectors, ingest, model, live board, video resolution, site, CI wiring.
- **Pass 2** — review pass: fixed the CLI cap that silently limited the ingest to 200 games, made the plan win over
  defaults, replaced a nonexistent summary path on the replays page, made the audit suite recompute the model from the
  dataset the model itself names, and added negative tests for phrases that must never come back.
- **Pass 3** — re-check against the brief, line by line; residual gaps and their blockers are on `docs/roadmap.html`.

## Limitations (also listed on `docs/roadmap.html`)

1. **2014–18 archive rows cannot be joined to a play id** for 2014: the league's own feeds for that season carry no
   `reviewDetails` and no per-play `playId` (recorded in `data/ingest/probe_report.json`). For 2015–2018 the join is
   possible and is implemented by `tools/watch_rulings.py`'s siblings; the 2014 rows therefore keep their original
   short link (resolved and labelled) and a Film Room search fallback instead of a direct clip.
2. **Run-removal is a labelled heuristic, not a ground truth.** The league publishes the corrected call, not the
   pre-review scoreboard, so a removal is inferred from the review text/type and re-checked by watching the video.
   The one rule that would be *proof* (runner movements vs. scoreboard delta) never fires on the ingested window —
   published as zero, with the explanation, rather than quietly dropped.
3. **No real-time push feed.** CI refreshes the slate and the ruling snapshot on a schedule; the browser also tries the
   official endpoint directly. A true live ticker would need a hosted proxy or an MLB data licence.
4. **The model is a ranking aid.** With a 0.8% base rate and no error recall at the arg-max, its honest use is to rank
   balls for review, not to call them.
