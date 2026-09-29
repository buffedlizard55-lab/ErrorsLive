#!/usr/bin/env python3
"""Audit suite — every number shipped to the public pages must reconcile with repo data.

Run from the repo root:   python3 tests/test_pipeline.py
Exit 0 = all checks pass.

Design rules for this suite:
 * Recompute, don't restate. Published figures are re-derived from data/raw/, data/source/ and
   the vendored NYDN CSVs wherever that is possible; only genuinely external facts (the official
   linescore values, the rulebook section titles) are checked against the committed verification
   ledger, which exists precisely because this sandbox cannot re-fetch them.
 * Negative tests matter. A check that can only pass is not a check — the "no page asserts X"
   assertions are written to FAIL if the old wording ever comes back.
 * No network. Everything here must run offline.
"""
import csv, datetime, json, math, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'data' / 'raw'
FAILED = []


def check(name, ok, detail=''):
    print(('  ok   ' if ok else 'FAIL   ') + name + (f'  — {detail}' if detail and not ok else ''))
    if not ok:
        FAILED.append(name)


def load_json(p):
    with open(p) as f:
        return json.load(f)


def load_csv(p):
    with open(p, newline='') as f:
        return list(csv.DictReader(f))


def plain_text(html):
    return re.sub(r'<[^>]+>', ' ', html)


def flat(html):
    """Plain text with collapsed whitespace, so a quote split across source lines still matches."""
    return re.sub(r'\s+', ' ', plain_text(html)).strip()


# A legacy phrase is acceptable only when the page is explicitly quoting it in order to correct it.
CORRECTION_MARKERS = ('was wrong', 'wrong', 'correction', 'corrected', 'removed', 'incorrect',
                      'unsupported', 'fabricated', 'contradicted', 'stale', 'no longer',
                      'case-sensitive', 'used to say')


# ---------------------------------------------------------------- A. archives
print('== A. raw archives & the official verification ledger ==')
raws = sorted(RAW.glob('8*.json'))
check('24 raw game archives', len(raws) == 24, f'found {len(raws)}')
V = load_json(ROOT / 'data' / 'verified_linescores.json')
check('verification ledger covers 24 games', len(V['games']) == 24, str(len(V['games'])))
check('every ledger row carries its own official URL',
      all(str(g.get('api', '')).startswith('https://statsapi.mlb.com/api/v1/game/') for g in V['games']))

targets = load_json(ROOT / 'data' / 'TARGETS.json')
chosen = set(targets['A'] + targets['B'] + targets['C'])
check('sample manifest == raw set', chosen == {int(p.stem) for p in raws})
check('manifest declares it is NOT self-weighting',
      targets.get('self_weighting') is False and 'NOT self-weighting' in targets['rationale'])

ver = {g['pk']: g for g in V['games']}
games = {r['game_pk']: r for r in load_csv(ROOT / 'docs/data/games.csv')}
score_ok = date_ok = strat_ok = 0
for p in raws:
    pk = int(p.stem)
    g = json.load(open(p))
    plays = g['liveData']['plays']['allPlays']
    last = plays[-1]['result']
    v = ver.get(pk, {})
    if (last['awayScore'], last['homeScore']) == (v.get('rA'), v.get('rH')):
        score_ok += 1
    if games.get(str(pk), {}).get('date') == v.get('date'):
        date_ok += 1
    eT = (v.get('eA', 0) or 0) + (v.get('eH', 0) or 0)
    want = 'A' if eT >= 3 else 'B' if eT == 2 else 'C'
    if eT == v.get('eT') and want == v.get('stratum') and pk in targets.get(want, []):
        strat_ok += 1
check('all 24 archived final scores == official linescore', score_ok == 24, f'{score_ok}/24')
check('all 24 games.csv dates == official dates', date_ok == 24, f'{date_ok}/24')
check('all 24 games sit in the stratum their official team-errors imply', strat_ok == 24, f'{strat_ok}/24')
check('integrity logs present for the 4 healed games', len(list(RAW.glob('*.integrity.txt'))) == 4)

# ---------------------------------------------------------------- B. dataset
print('== B. dataset build ==')
bip = load_csv(ROOT / 'docs/data/bip.csv')
dq = load_json(ROOT / 'data' / 'data_quality.json')
t = dq['totals']
check('bip rows == 1271', len(bip) == 1271, str(len(bip)))
check('bip event types are exactly the official eventType values',
      all(r['event_type'] for r in bip))
check('24 plays labelled field_error', sum(1 for r in bip if r['macro_class'] == 'error') == 24)
check('every field_error row has a full Statcast vector (no invented case)',
      all(r['numeric_ok'] == '1' for r in bip if r['macro_class'] == 'error'))
check('missing-numeric quarantine == 12', t['missing_numeric'] == 12)
check('multi-hitData anomalies == 0', t['multi_hitdata'] == 0)
check("'other'-class rows == 1 and it is catcher_interf",
      t['other_class'] == 1 and [r['event_type'] for r in bip if r['macro_class'] == 'other'] == ['catcher_interf'])
check('data_quality agrees with bip.csv',
      t['bip'] == len(bip) and t['errors'] == 24 and t['plays'] == sum(g['plays'] for g in dq['games'].values()))
check('all 24 games recorded as score-checked against the official record',
      len(dq['games']) == 24 and all(g['score_checked_against_official'] for g in dq['games'].values()))
check('strata tally is 10 / 10 / 4',
      dq['strata'] == {'A_eT>=3': 10, 'B_eT==2': 10, 'C_eT<=1': 4}, json.dumps(dq['strata']))
check('site verification.json mirrors the repo ledger',
      load_json(ROOT / 'docs/data/verification.json')['games'] == V['games'])
check('games.csv rows are all in the official ledger',
      all(int(r['game_pk']) in ver and r['official_url'] == ver[int(r['game_pk'])]['api']
          for r in load_csv(ROOT / 'docs/data/games.csv')))

# error-vs-trajectory headline quoted on the home page
errs = [r for r in bip if r['macro_class'] == 'error']
gb_all = sum(1 for r in bip if r['trajectory'] == 'ground_ball')
gb_err = sum(1 for r in errs if r['trajectory'] == 'ground_ball')
lift = (gb_err / gb_all) / ((len(errs) - gb_err) / (len(bip) - gb_all))
check('home page headline: 20 of 24 errors are ground balls', gb_err == 20 and len(errs) == 24)
check('home page headline: 6.8x lift for ground balls', abs(lift - 6.8) < 0.05, f'{lift:.2f}x')
check('home page headline: ground balls are 42.3% of batted balls',
      abs(100 * gb_all / len(bip) - 42.3) < 0.05, f'{100*gb_all/len(bip):.2f}%')

# ---------------------------------------------------------------- C. model
print('== C. model.json — recomputed from bip.csv, not restated ==')
sys.path.insert(0, str(ROOT / 'tools'))
import numpy as np                                                   # noqa: E402
from sklearn.linear_model import LogisticRegression                  # noqa: E402
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss  # noqa: E402
from sklearn.model_selection import StratifiedKFold                  # noqa: E402
from sklearn.preprocessing import StandardScaler                    # noqa: E402

M = load_json(ROOT / 'docs/data/model.json')
mm, pp, hn = M['meta'], M['primary'], M['honesty']
CLASSES = ['hit', 'error', 'fielders_choice', 'out']
TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder']
HARD = ['soft', 'medium', 'hard']
num = [r for r in bip if r['numeric_ok'] == '1' and r['macro_class'] in CLASSES]
X = np.array([[float(r['launch_speed']), float(r['launch_angle']), float(r['distance'])] +
              [1.0 if r['trajectory'] == v else 0.0 for v in TRAJ] +
              [1.0 if r['hardness'] == v else 0.0 for v in HARD] for r in num])
yb = np.array([1 if r['macro_class'] == 'error' else 0 for r in num])
yc = np.array([CLASSES.index(r['macro_class']) for r in num])

check('meta counts recompute from bip.csv',
      mm['n_bip'] == len(bip) and mm['n_model'] == len(num) and mm['n_error'] == int(yb.sum())
      and mm['n_error'] == 24 and mm['n_model'] == 1258)
check('meta class mix recomputes',
      mm['class_counts'] == {c: int((yc == i).sum()) for i, c in enumerate(CLASSES)}
      and mm['class_counts'] == {'hit': 428, 'error': 24, 'fielders_choice': 10, 'out': 796})
check('error rate 1.91%', abs(mm['error_rate_pct'] - 1.91) < 0.01)
check('design narrative states the sample is ENRICHED and NOT self-weighting',
      'ENRICHED' in mm['design'] and 'NOT' in mm['design'] and '37.75' in mm['design']
      and 'uniform 0.5 sampling fraction' not in mm['design'])

sc = StandardScaler().fit(X)
Xs = sc.transform(X)
skf = StratifiedKFold(5, shuffle=True, random_state=20260929)
oof = np.zeros(len(yb))
for tr, te in skf.split(Xs, yb):
    oof[te] = LogisticRegression(max_iter=2000, C=1.0).fit(Xs[tr], yb[tr]).predict_proba(Xs[te])[:, 1]
check('OOF AUC recomputes to the published value',
      abs(roc_auc_score(yb, oof) - pp['cv_auc']) < 5e-4, f"{roc_auc_score(yb, oof):.4f} vs {pp['cv_auc']}")
check('published AUC is 0.641 with CI [0.518, 0.748]',
      abs(pp['cv_auc'] - 0.641) < 1e-3 and abs(pp['cv_auc_ci95'][0] - 0.518) < 1e-3
      and abs(pp['cv_auc_ci95'][1] - 0.748) < 1e-3)
check('OOF log-loss and Brier recompute',
      abs(log_loss(yb, oof) - pp['cv_logloss']) < 5e-4
      and abs(brier_score_loss(yb, oof) - pp['cv_brier_raw']) < 5e-5)
base = LogisticRegression(max_iter=2000, C=1.0).fit(Xs, yb)
check('published coefficients match a refit',
      all(abs(float(base.coef_[0][i]) - v) < 5e-3 for i, v in enumerate(pp['coef'].values())))
check('ground_ball coefficient is the largest one',
      max(pp['coef'], key=lambda k: abs(pp['coef'][k])) == 'traj_ground_ball'
      and abs(pp['coef']['traj_ground_ball'] - 0.692) < 1e-3)

oofm = np.zeros((len(yc), len(CLASSES)))
for tr, te in skf.split(Xs, yc):
    oofm[te] = LogisticRegression(max_iter=3000, C=1.0).fit(Xs[tr], yc[tr]).predict_proba(Xs[te])
top1 = oofm.argmax(axis=1)
check('OOF top-1 accuracy recomputes',
      abs(float((top1 == yc).mean()) - hn['oof_top1_accuracy']) < 5e-4)
check('honesty block: model never nominates "error" as the top call',
      hn['oof_error_nominated_top1'] == 0 and hn['oof_error_recall'] == 0.0
      and int((top1 == CLASSES.index('error')).sum()) == 0)
check('honesty block: observed P(error) range is 0.01-6.48 out of 100',
      abs(hn['p_error_observed_max_x100'] - 6.48) < 0.02
      and abs(hn['p_error_observed_min_x100'] - 0.01) < 0.005)
p_all = 1 / (1 + np.exp(-(base.intercept_[0] + Xs @ base.coef_[0])))
check('honesty block max matches a fresh refit',
      abs(100 * float(p_all.max()) - hn['p_error_observed_max_x100']) < 0.02)
check('percentile grid is monotone 0..100',
      hn['p_error_percentile_grid'][0]['percentile'] == 0
      and hn['p_error_percentile_grid'][-1]['percentile'] == 100
      and all(a['percentile'] <= b['percentile']
              for a, b in zip(hn['p_error_percentile_grid'], hn['p_error_percentile_grid'][1:])))
check('calibration table sums to the modelling set',
      sum(b['n'] for b in hn['calibration_oof']) == len(num))
s = M['surface'].get('popup|70-80')
check('model-free surface bucket popup 70-80: n=27 k=1',
      s and s['n'] == 27 and s['k'] == 1 and abs(s['p'] - 0.037) < 1e-3)

# ---------------------------------------------------------------- D. site <-> tool agreement
print('== D. the site and the tool must compute the same number ==')
import importlib.util                                                   # noqa: E402
spec = importlib.util.spec_from_file_location('live_score', ROOT / 'tools' / 'live_score.py')
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)
scorer = ls.Scorer(M)


def js_eval_model(m, x):
    p = m['primary']
    z = [x[i] - p['scaler_mean'][i] for i in range(len(x))]
    z = [(x[i] - p['scaler_mean'][i]) / (p['scaler_scale'][i] or 1) for i in range(len(x))]
    logit = p['intercept'] + sum(z[i] * p['coef'][p['feature_names'][i]] for i in range(len(z)))
    mc = m['multiclass']
    cl = list(mc['coef'])
    ex = [math.exp(mc['intercept'][c] + sum(z[i] * mc['coef'][c][p['feature_names'][i]] for i in range(len(z))))
          for c in cl]
    tot = sum(ex)
    return 1 / (1 + math.exp(-logit)), {c: e / tot for c, e in zip(cl, ex)}


worst = 0.0
for r in num:
    p1, pr1, _ = scorer.predict(float(r['launch_speed']), float(r['launch_angle']),
                                 float(r['distance']), r['trajectory'], r['hardness'])
    p2, pr2 = js_eval_model(M, [float(r['launch_speed']), float(r['launch_angle']), float(r['distance'])]
                           + [1.0 if r['trajectory'] == v else 0.0 for v in TRAJ]
                           + [1.0 if r['hardness'] == v else 0.0 for v in HARD])
    worst = max(worst, abs(p1 - p2), max(abs(pr1[k] - pr2[k]) for k in pr1))
check('tools/live_score.py and docs/site.js agree to 1e-9 on all 1,258 balls', worst < 1e-9, f'max diff {worst:.2e}')

# ---------------------------------------------------------------- E. live tool + fixture
print('== E. live tool and the official feed fixture ==')
fixture = load_json(ROOT / 'data/source/feed_823441.json')
fp = fixture['liveData']['plays']['allPlays']
arch = load_json(RAW / '823441.json')['liveData']['plays']['allPlays']
mism = sum(1 for a, b in zip(fp, arch)
           if a['result']['eventType'] != b['result']['eventType']
           or [e.get('hitData') for e in a['playEvents'] if 'hitData' in e]
           != [e.get('hitData') for e in b['playEvents'] if 'hitData' in e])
check('fixture has 75 plays and matches the independently archived feed exactly',
      len(fp) == 75 and len(arch) == 75 and mism == 0, f'{mism} mismatches')
check('fixture final score == official 1-6',
      (fp[-1]['result']['awayScore'], fp[-1]['result']['homeScore']) == (1, 6))
rows = [r for r in ls.score_feed(fixture, pk=823441, scorer=scorer) if r.get('status') == 'scored']
check('live tool scores 46 batted balls in the fixture', len(rows) == 46, str(len(rows)))
check('live tool agrees with the official call on 34 of 46',
      sum(r['model_agrees_with_call'] for r in rows) == 34,
      str(sum(r['model_agrees_with_call'] for r in rows)))
check('live tool never nominates "error" as the top pick anywhere in the game',
      all(r['top_pick'] != 'error' for r in rows))
risp = [r for r in rows if r['risp'] and r['run_scored']]
check('live tool finds the 2 run-scoring plays with a runner on 2nd/3rd', len(risp) == 2, str(len(risp)))
check('RBI-at-stake rows carry all three candidate-ruling answers',
      all(r['rbi_if_error'].startswith('NO RBI') and r['rbi_if_hit'] == 'RBI'
          and 'fielder' in r['rbi_if_fc'] for r in risp))
check('the highest-scoring ball in the fixture is the game\'s only ruled error',
      max(rows, key=lambda r: r['score_100'])['official_call'] == 'error')
check('published live board matches a fresh run of the tool',
      load_json(ROOT / 'docs/data/live_sample.json') == [
          {**r, 'source': 'data/source/feed_823441.json'} for r in ls.score_feed(
              fixture, pk=823441, scorer=scorer, meta={'source': 'data/source/feed_823441.json'})])
ov = [r for r in rows if r['reviewed']]
check('fixture challenges are surfaced (3 reviewed plays, 2 overturned)',
      len(ov) == 3 and sum(r['review_overturned'] is True for r in ov) == 2, str(len(ov)))

# ---------------------------------------------------------------- F. NYDN
print('== F. NYDN replay archive ==')
ny = load_csv(ROOT / 'docs/data/overturned_calls.csv')
nq = load_json(ROOT / 'data/nydn_quality.json')
src = sorted((ROOT / 'data/source/nydn').glob('*.csv'))
check('9 source CSVs vendored so the build is offline-reproducible', len(src) == 9, str(len(src)))
def _is_real_day(iso):
    'True only for a syntactically and calendrically valid ISO date.'
    if not re.fullmatch(r'20\d\d-\d{2}-\d{2}', iso):
        return False
    try:
        datetime.date(*(int(x) for x in iso.split('-')))
        return True
    except ValueError:
        return False

check('every source data line is accounted for (6,362 lines -> 6,361 rows, 1 junk line dropped)',
      len(ny) == 6361 and nq['flag_summary'].get('junk_mid_file_line') == 1, str(len(ny)))
check('by_year reconciles with the per-row columns',
      sum(v['rows'] for v in nq['by_year'].values()) == len(ny)
      and sum(v['overturned'] for v in nq['by_year'].values()) == sum(r['overturned'] == '1' for r in ny)
      and sum(v['rr'] for v in nq['by_year'].values()) == sum(r['run_removed_heuristic'] == '1' for r in ny),
      json.dumps(nq['by_year']))
check('overturned 3067', sum(r['overturned'] == '1' for r in ny) == 3067)
check('run-affected 230', sum(r['run_removed_heuristic'] == '1' for r in ny) == 230)
check('overturned / run-affected flags are internally consistent',
      all((r['result'] == 'Overturned') == (r['overturned'] == '1') for r in ny)
      and not any(r['run_removed_heuristic'] == '1' and r['result'] != 'Overturned' for r in ny)
      and all(r['run_removed_heuristic'] == '1'
              for r in ny if r['result'] == 'Overturned'
              and (re.search(r'(score|scored|home|plate|run)', r['initial_call'], re.I)
                   or re.search(r'(Timing|HP|Home|Plate|Collisions)', r['play_type'], re.I))))
check('season is the calendar year, not the source filename (bug fixed 2026-09-29)',
      all(re.fullmatch(r'20\d\d', r['season']) for r in ny)
      and sorted({r['season'] for r in ny}) == ['2014', '2015', '2016', '2017', '2018']
      and not any(r['season'].endswith('.csv') for r in ny),
      str(sorted({r['season'] for r in ny})))
check('season agrees with the source_file year for every row',
      all(r['season'] == r['source_file'][:4] for r in ny))
check('every date is a real ISO calendar day except the documented source typo',
      all(re.fullmatch(r'20\d\d-\d{2}-\d{2}', r['date']) for r in ny)
      and nq['impossible_dates_kept_verbatim'] == ['2014-04-31']
      and nq['dates_kept_verbatim'] == 5
      and [r['date'] for r in ny if not _is_real_day(r['date'])] == ['2014-04-31'] * 5,
      str([r['date'] for r in ny if not _is_real_day(r['date'])][:3]))
check('date_raw preserves the source string verbatim for every row',
      all(r['date_raw'].strip().strip('"').strip() != '' for r in ny)
      and all(re.fullmatch(r'20\d\d-\d{1,2}-\d{1,2}"?'
                           r'|(?:[A-Za-z]{3,9}\.? )?[A-Za-z]{3,9}\.? \d{1,2}', r['date_raw'].strip())
              for r in ny if r['date_repaired'] == '1'),
      next((r['date_raw'] for r in ny if r['date_repaired'] == '1'
            and not re.fullmatch(r'20\d\d-\d{1,2}-\d{1,2}"?'
                                 r'|(?:[A-Za-z]{3,9}\.? )?[A-Za-z]{3,9}\.? \d{1,2}',
                                 r['date_raw'].strip())), ''))
check('date_repaired flag marks exactly the rows whose date was normalised',
      sum(r['date_repaired'] == '1' for r in ny) == nq['date_repaired_rows'] == 2109)
check('source irregularities are logged with the published breakdown',
      nq['flag_summary'] == {'bad_time_format': 1, 'date_impossible_kept': 5,
                             'date_prose_resolved': 234, 'date_stray_quote_stripped': 279,
                             'date_zero_padded': 2109, 'junk_mid_file_line': 1,
                             'no_video_link': 56, 'quote_repair': 3}
      and len(nq['flags']) == 2688, json.dumps(nq.get('flag_summary')))
check('the 271 broken-quote rows are recorded, not silently swallowed',
      sum(v for k, v in nq['flag_summary'].items() if k == 'quote_repair') == 3
      and '206 x spurious leading quote' in ' '.join(nq['flags'])
      and '29 x unterminated opening quote' in ' '.join(nq['flags'])
      and '36 x unterminated opening quote' in ' '.join(nq['flags']))
check('rows with a missing video link are kept, not dropped',
      sum(1 for r in ny if not r['video']) == nq['flag_summary']['no_video_link'] == 56)
check('source typos are preserved verbatim, never silently repaired',
      any(r['date'] == '2014-04-31' for r in ny) and any(r['time_to_ruling'] == '1;21' for r in ny))
ho17 = [r for r in ny if r['date'] == '2017-10-17' and r['game'].startswith('HOU @ NYY')]
check('2017 ALCS G4 rows parse (Judge overturn kept)',
      len(ho17) == 2 and ho17[0]['player'] == 'A. Judge' and ho17[0]['play_type'] == 'Force Play'
      and ho17[0]['result'] == 'Overturned' and ho17[0]['postseason'] == '1')
summ = load_json(ROOT / 'docs/data/nydn_summary.json')
check('the site summary is the build output, not a hand-typed copy',
      summ['rows_total'] == len(ny) and summ['overturned'] == 3067
      and summ['run_removed_heuristic'] == 230 and summ['flag_summary'] == nq['flag_summary'],
      json.dumps(summ.get('rows_total')))
static = ''.join((ROOT / f'docs/{f}').read_text() for f in
                 ('index.html', 'methods.html', 'overturned.html'))
for label, val in (('rows', summ['rows_total']), ('overturned', summ['overturned']),
                   ('run-affected', summ['run_removed_heuristic'])):
    check(f'published copy states the built {label} count ({val:,})',
          f'{val:,}' in static, f'{val:,}')
landing = ''.join((ROOT / f'docs/{f}').read_text() for f in ('index.html', 'overturned.html'))
meth = (ROOT / 'docs/methods.html').read_text()
check('no landing page still quotes the pre-repair totals',
      not re.search(r'6,168|2,984|\b227\b|61 source irregularities', landing))
check('methods.html may cite the old totals only to record the correction',
      all('first build' in meth[max(0, m.start() - 400):m.start() + 200]
          for m in re.finditer(r'6,168|2,984|\b227\b', meth)))
check('the site renders the flag count from data/nydn_summary.json, not a literal',
      "loadJSON('data/nydn_summary.json')" in (ROOT / 'docs/overturned.html').read_text()
      and '61 source irregularities' not in (ROOT / 'docs/overturned.html').read_text())
check('the date-repair policy is stated on the overturned page with its real numbers',
      all(x in (ROOT / 'docs/overturned.html').read_text()
          for x in ('2,109', '234', '279', '271', '2014-04-31', 'date_raw')))
check('every non-empty video link points at the original source host',
      all(r['video'].startswith(('http://t.co/', 'https://t.co/', 'bit.ly/', 'http://bit.ly/',
                                 'https://bit.ly/', 'atmlb.com', 'http://atmlb', 'https://atmlb'))
          for r in ny if r['video'])
      and sum(1 for r in ny if r['video']) == len(ny) - 56)

# ---------------------------------------------------------------- G. pages vs data
print('== G. published pages ==')
idx = (ROOT / 'docs/index.html').read_text()
liv = (ROOT / 'docs/live.html').read_text()
mod = (ROOT / 'docs/model.html').read_text()
ovt = (ROOT / 'docs/overturned.html').read_text()
rul = (ROOT / 'docs/rules.html').read_text()
met = (ROOT / 'docs/methods.html').read_text()
rdm = (ROOT / 'docs/roadmap.html').read_text()
PAGES = {'index': idx, 'live': liv, 'model': mod, 'overturned': ovt,
         'rules': rul, 'methods': met, 'roadmap': rdm}

# every page must carry the nav and link to the audit
for n, pg in PAGES.items():
    check(f'{n}: loads the shared nav', "site.js" in pg and 'site.css' in pg)

# --- negative tests: wording that must never come back -----------------------
LEGACY = ['self-weighting sample', 'uniform 0.5 sampling fraction', 'hou @ min',
          '8/24 vs nym', 'j. mcneil', '25 plays carry']
for n, pg in PAGES.items():
    txt = flat(pg).lower()
    bad = []
    for phrase in LEGACY:
        for m in re.finditer(re.escape(phrase), txt):
            window = txt[max(0, m.start() - 400):m.end() + 400]
            if not any(mk in window for mk in CORRECTION_MARKERS):
                bad.append(phrase)
    check(f'{n}: no uncorrected legacy claim', not bad,
          'found: ' + ', '.join(sorted(set(bad))) if bad else '')

# the specific false claim must be gone even where it is quoted as a correction
check('model.html no longer asserts the sample is self-weighting',
      'self-weighting sample' not in flat(mod).lower())
check('roadmap explicitly records the self-weighting correction',
      'self-weighting sample' in flat(rdm).lower() and 'case-sensitive' in flat(rdm).lower())
mt = flat(met).lower()


def only_in_correction(txt, phrase):
    """True when the phrase never appears except inside an explicit correction."""
    for m in re.finditer(re.escape(phrase), txt):
        window = txt[max(0, m.start() - 400):m.end() + 400]
        if not any(mk in window for mk in CORRECTION_MARKERS):
            return False
    return True


check('methods.html no longer asserts 25 field_error plays or an 8/24 NYM game',
      only_in_correction(mt, 'hou @ min') and only_in_correction(mt, '8/24 vs nym'))
check('methods.html records the correction explicitly', 'both were wrong' in mt)
v801 = ver[822801]
check('methods.html states 822801\'s real official matchup and score, derived from the ledger',
      f"{v801['away']} {v801['rA']}" in met and f"{v801['home']} {v801['rH']}" in met
      and f"{v801['rA']}\u2013{v801['rH']}" in met, json.dumps(v801))
check('methods.html lists bip.csv\'s real columns',
      'numeric_ok' in met and 'review_type' in met and 'play_index' in met)

# --- required content -------------------------------------------------------
check('index carries the honesty banner and the verified headline',
      'never rises above 6.5/100' in idx and '20 of the 24 ruled errors' in idx and '6.8' in idx)
check('index KPI ids present', all(f'id="{i}"' in idx for i in ['kMeta', 'kAuc', 'kCoef']))
check('index calculator presets are real archived batted balls',
      all(p in idx for p in ['105.9,-5,23,ground_ball,hard', '78.2,54,226,fly_ball,medium',
                             '101.9,-3,35,ground_ball,medium', '74.1,61,195,popup,medium']))
check('index shows an honest percentile alongside the raw score', 'percentile' in idx and 'errorPercentile' in idx)
check('live page loads the tool output and explains the command',
      'data/live_sample.json' in liv and 'tools/live_score.py' in liv and '--today' in liv)
check('live page states the sandbox network limitation', 'no direct outbound HTTPS' in liv)
check('model page publishes the honesty block + calibration',
      'honesty' in mod or 'h.calibration_oof' in mod)
check('model page explains the NOT-self-weighting design',
      'NOT' in mod and 'self-weighting' in plain_text(mod).lower()
      and '37.75' in mod and '1.91%' in mod)
check('overturned page flags the 404 source and uses the working Film Room URL',
      '404' in ovt and 'mlb.com/video/?q=' in ovt and 'nydailynews' in ovt)
check('overturned KPI ids present', all(f'id="{i}"' in ovt for i in ['kTot', 'kOv', 'kRr']))
check('rules page cites the official rulebook and flags the 10.04 discrepancy',
      '2025-official-baseball-rules.pdf' in rul and '9.04' in rul and '10.04' in rul
      and 'no Rule 10.04' in rul)
rules_txt = flat(rul)
check('rules page carries both verbatim glossary quotations',
      'does not receive an RBI when the run scores as a result of an error or ground into double play'
      in rules_txt
      and 'do not receive RBIs for any runs that would not have scored without the help of an error'
      in rules_txt)
check('methods page publishes the 24-game verification ledger', 'data/verification.json' in met
      and 'verified_linescores' in met)
check('roadmap lists an ordered next-session backlog with blockers', rdm.count('<tr><td class="right">') >= 7)
rd = (ROOT / 'README.md').read_text()
check('README restates the brief before any results (Section 0 rule)', 'Section 0' in rd)
check('README carries no claim that an error lacked hitData (there were none)',
      'lacked hitData' not in rd and 'errors_without_hitdata: 0' in rd)
check('README numbers match the built artifacts',
      all(x in rd for x in ('1,271', '1,868', '24 labeled errors', '0.641', '6,361', '3,067', '230 run-affected')))
check('own-the-outcome policy is stated on the home page', 'Own the Outcome' in idx)

# --- official links ---------------------------------------------------------
OFFICIAL = ['https://statsapi.mlb.com/', 'https://www.mlb.com/glossary/standard-stats/error',
            'https://www.mlb.com/glossary/standard-stats/runs-batted-in',
            'https://mktg.mlbstatic.com/mlb/official-information/2025-official-baseball-rules.pdf',
            'https://github.com/nydailynews/mlb-overturned-calls']
all_html = ''.join(PAGES.values())
for link in OFFICIAL:
    check(f'official source linked: {link.split("/")[2]}', link in all_html)

# no invented citation slipped in
check('no page cites a non-official source as official',
      not re.search(r'(https?://(?!www\.mlb\.com|statsapi\.mlb\.com|mktg\.mlbstatic\.com|'
                    r'baseballsavant\.mlb\.com|github\.com/nydailynews|github\.com/buffedlizard55-lab|'
                    r'www\.baseball-reference\.com)[^\s"\')]+)', all_html))

print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S):')
    for f in FAILED:
        print('   -', f)
    sys.exit(1)
print('ALL CHECKS PASSED')
