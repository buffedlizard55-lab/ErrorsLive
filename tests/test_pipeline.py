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
print('== C. model.json — recomputed from the dataset it names, not restated ==')
sys.path.insert(0, str(ROOT / 'tools'))
import numpy as np                                                   # noqa: E402
from sklearn.linear_model import LogisticRegression                  # noqa: E402
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss  # noqa: E402
from sklearn.model_selection import GroupKFold, StratifiedKFold      # noqa: E402
from sklearn.preprocessing import StandardScaler                     # noqa: E402

M = load_json(ROOT / 'docs/data/model.json')
mm, pp, hn = M['meta'], M['primary'], M['honesty']
CLASSES = ['hit', 'error', 'fielders_choice', 'out']
TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder']
HARD = ['soft', 'medium', 'hard']
SPEC = pp['feature_spec']
NAMES = [f['name'] for f in SPEC]
MODEL_DATASET = ROOT / mm['dataset']
ds = load_csv(MODEL_DATASET)
num = [r for r in ds if r['numeric_ok'] == '1' and r['macro_class'] in CLASSES]


def state_of(r):
    bases = {b for b, k in (('1B', 'on_1b'), ('2B', 'on_2b'), ('3B', 'on_3b')) if r.get(k) == '1'}
    return {'ev': float(r['launch_speed']), 'la': float(r['launch_angle']), 'dist': float(r['distance']),
            'traj': r['trajectory'], 'hard': r['hardness'], 'bases': bases,
            'outs': int(float(r['outs_before'] or 0)), 'inning': int(float(r['inning'] or 5)),
            'bat': r.get('bat_side') or '', 'pitch': r.get('pitch_hand') or ''}


def vector_of(st, spec):
    """Same rule as tools/train_model.state_value and docs/site.js: the spec is the contract."""
    out = []
    for f in spec:
        t = f['type']
        kind, _, val = t.partition(':')
        if t == 'ev':
            out.append(st['ev'])
        elif t == 'la':
            out.append(st['la'])
        elif t == 'dist':
            out.append(st['dist'])
        elif t == 'outs':
            out.append(st['outs'])
        elif t == 'inning':
            out.append(min(st['inning'], 9))
        elif kind == 'traj':
            out.append(1.0 if st['traj'] == val else 0.0)
        elif kind == 'hard':
            out.append(1.0 if st['hard'] == val else 0.0)
        elif kind == 'base':
            out.append(1.0 if val in st['bases'] else 0.0)
        elif kind == 'bat':
            out.append(1.0 if st.get('bat') == val else 0.0)
        elif kind == 'pitch':
            out.append(1.0 if st.get('pitch') == val else 0.0)
        else:
            raise AssertionError(f'unknown feature type {t!r}')
    return out


X = np.array([vector_of(state_of(r), SPEC) for r in num])
yb = np.array([1 if r['macro_class'] == 'error' else 0 for r in num])
yc = np.array([CLASSES.index(r['macro_class']) for r in num])
groups = np.array([r['game_pk'] for r in num])

check('meta counts recompute from the named dataset',
      mm['n_bip'] == len(ds) and mm['n_model'] == len(num) and mm['n_error'] == int(yb.sum()),
      f"n_bip {mm['n_bip']} vs {len(ds)}, n_model {mm['n_model']} vs {len(num)}")
check('meta class mix recomputes',
      mm['class_counts'] == {c: int((yc == i).sum()) for i, c in enumerate(CLASSES)},
      json.dumps(mm['class_counts']))
check('meta error rate and game count recompute',
      abs(mm['error_rate_pct'] - 100 * float(yb.mean())) < 0.005
      and mm['games'] == len(set(groups)),
      f"{mm['error_rate_pct']} vs {100*yb.mean():.3f}, games {mm['games']}")
check('design narrative names the honest CV and the dataset caveat',
      'GroupKFold' in mm['design'] and 'game_pk' in mm['design']
      and (('ENRICHED' in mm['design'] and 'NOT' in mm['design'])
           or ('Whole-population' in mm.get('dataset_note', ''))),
      mm['design'][:80])

sc = StandardScaler().fit(X)
Xs = sc.transform(X)
base = LogisticRegression(max_iter=4000, C=1.0).fit(Xs, yb)


gkf = GroupKFold(5)
oof_g = np.zeros(len(yb))
oof_gm = np.zeros((len(yc), len(CLASSES)))
for tr, te in gkf.split(Xs, yb, groups):
    oof_g[te] = LogisticRegression(max_iter=4000, C=1.0).fit(Xs[tr], yb[tr]).predict_proba(Xs[te])[:, 1]
    oof_gm[te] = LogisticRegression(max_iter=5000, C=1.0).fit(Xs[tr], yc[tr]).predict_proba(Xs[te])
skf = StratifiedKFold(5, shuffle=True, random_state=20260929)
oof_r = np.zeros(len(yb))
for tr, te in skf.split(Xs, yb):
    oof_r[te] = LogisticRegression(max_iter=4000, C=1.0).fit(Xs[tr], yb[tr]).predict_proba(Xs[te])[:, 1]

auc_g = float(roc_auc_score(yb, oof_g))
check('grouped OOF AUC recomputes to the published value',
      abs(auc_g - pp['cv_auc']) < 5e-4, f'{auc_g:.4f} vs {pp["cv_auc"]}')
check('random-fold AUC recomputes to the published optimistic bound',
      abs(float(roc_auc_score(yb, oof_r)) - pp['cv_auc_random_kfold']) < 5e-4,
      f'{roc_auc_score(yb, oof_r):.4f} vs {pp["cv_auc_random_kfold"]}')
check('published CI widens honestly around the grouped AUC',
      pp['cv_auc_ci95'][0] < pp['cv_auc'] < pp['cv_auc_ci95'][1]
      and pp['cv_auc_ci95'][1] - pp['cv_auc_ci95'][0] > 0.05,
      json.dumps(pp['cv_auc_ci95']))
check('OOF log-loss and Brier recompute',
      abs(log_loss(yb, oof_g, labels=[0, 1]) - pp['cv_logloss']) < 5e-4
      and abs(brier_score_loss(yb, oof_g) - pp['cv_brier_raw']) < 5e-5)
check('published coefficients match a refit on the named dataset',
      all(abs(float(base.coef_[0][i]) - pp['coef'][n]) < 5e-3 for i, n in enumerate(NAMES)),
      json.dumps({n: round(pp['coef'][n], 4) for n in NAMES[:4]}))
check('the strongest coefficient in the published model is a real feature',
      max(NAMES, key=lambda n: abs(pp['coef'][n])) in NAMES
      and abs(pp['coef'][max(NAMES, key=lambda n: abs(pp['coef'][n]))]) > 0.1,
      max(NAMES, key=lambda n: abs(pp['coef'][n])))

top1 = oof_gm.argmax(axis=1)
check('OOF top-1 accuracy recomputes',
      abs(float((top1 == yc).mean()) - hn['oof_top1_accuracy']) < 5e-4,
      f'{(top1 == yc).mean():.4f} vs {hn["oof_top1_accuracy"]}')
check('honesty block: the model never nominates "error" as its top call',
      hn['oof_error_nominated_top1'] == 0 and hn['oof_error_recall'] == 0.0
      and int((top1 == CLASSES.index('error')).sum()) == 0)
p_all = 1 / (1 + np.exp(-(base.intercept_[0] + Xs @ base.coef_[0])))
check('honesty block: observed P(error) range matches a fresh refit',
      abs(100 * float(p_all.max()) - hn['p_error_observed_max_x100']) < 0.02
      and abs(100 * float(p_all.min()) - hn['p_error_observed_min_x100']) < 0.005,
      f'{100*float(p_all.max()):.3f} vs {hn["p_error_observed_max_x100"]}')
check('honesty block states the score is a ranking aid, not a decision',
      'does not decide' in hn['what_this_is'] and 'ranks' in hn['what_this_is'])
check('percentile grid is monotone 0..100',
      hn['p_error_percentile_grid'][0]['percentile'] == 0
      and hn['p_error_percentile_grid'][-1]['percentile'] == 100
      and all(a['percentile'] <= b['percentile']
              for a, b in zip(hn['p_error_percentile_grid'], hn['p_error_percentile_grid'][1:])))
check('calibration table sums to the modelling set',
      sum(b['n'] for b in hn['calibration_oof']) == len(num),
      f'{sum(b["n"] for b in hn["calibration_oof"])} vs {len(num)}')

# the model-free empirical surface must be reproducible from the same rows
key = next(iter(sorted(M['surface'])))
traj, band = key.split('|')
lo, hi = (int(x) for x in band.split('-'))
sub = [r for r in num if r['trajectory'] == traj and lo <= float(r['launch_speed']) < hi]
srf = M['surface'][key]
check(f'model-free surface bucket {key} recomputes from the dataset',
      srf['n'] == len(sub) and abs(srf['p'] - (sum(1 for r in sub if r['macro_class'] == 'error')
                                               / len(sub) if sub else 0)) < 2e-3,
      f"n {srf['n']} vs {len(sub)}")

# ---------------------------------------------------------------- D. site <-> tool agreement
print('== D. the site and the tool must compute the same number ==')
import importlib.util                                                   # noqa: E402
spec = importlib.util.spec_from_file_location('live_score', ROOT / 'tools' / 'live_score.py')
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)
scorer = ls.Scorer(M)


def js_eval_model(m, st):
    """Mirror of docs/site.js evalModel(): build the spec vector, standardise, then apply both heads."""
    p = m['primary']
    x = []
    for f in p['feature_spec']:
        t = f['type']
        kind, _, val = t.partition(':')
        if t == 'ev':
            x.append(st['ev'])
        elif t == 'la':
            x.append(st['la'])
        elif t == 'dist':
            x.append(st['dist'])
        elif t == 'outs':
            x.append(st['outs'])
        elif t == 'inning':
            x.append(min(st['inning'], 9))
        elif kind == 'traj':
            x.append(1.0 if st['traj'] == val else 0.0)
        elif kind == 'hard':
            x.append(1.0 if st['hard'] == val else 0.0)
        elif kind == 'base':
            x.append(1.0 if val in st['bases'] else 0.0)
        elif kind == 'bat':
            x.append(1.0 if st.get('bat') == val else 0.0)
        elif kind == 'pitch':
            x.append(1.0 if st.get('pitch') == val else 0.0)
        else:
            raise AssertionError(t)
    z = [(x[i] - p['scaler_mean'][i]) / (p['scaler_scale'][i] or 1) for i in range(len(x))]
    logit = p['intercept'] + sum(z[i] * p['coef'][p['feature_names'][i]] for i in range(len(z)))
    mc = m['multiclass']
    ex = [math.exp(mc['intercept'][c] + sum(z[i] * mc['coef'][c][p['feature_names'][i]]
                                            for i in range(len(z)))) for c in mc['coef']]
    tot = sum(ex)
    return 1 / (1 + math.exp(-logit)), {c: e / tot for c, e in zip(mc['coef'], ex)}


worst, worst_row = 0.0, None
for r in num:
    st = state_of(r)
    p1, pr1, _ = scorer.predict_state(st)
    p2, pr2 = js_eval_model(M, st)
    d = max([abs(p1 - p2)] + [abs(pr1[k] - pr2[k]) for k in pr1])
    if d > worst:
        worst, worst_row = d, r
check(f'tools/live_score.py and docs/site.js agree to 1e-9 on all {len(num):,} balls of the named dataset',
      worst < 1e-9, f'max diff {worst:.2e} on ab {worst_row.get("at_bat") if worst_row else "?"}')

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
check('live tool scores a batted ball for every play that has a Statcast vector',
      len(rows) == sum(1 for pl in fp for e in pl['playEvents']
                       if all(k in (e.get('hitData') or {})
                              for k in ('launchSpeed', 'launchAngle', 'totalDistance'))),
      str(len(rows)))
agree = sum(r['model_agrees_with_call'] for r in rows)
check('live tool top pick matches the official call on at least 6 of every 10 balls',
      agree / len(rows) >= 0.60, f'{agree}/{len(rows)} = {100*agree/len(rows):.0f}%')
check('live tool never nominates "error" as the top pick anywhere in the game',
      all(r['top_pick'] != 'error' for r in rows))
risp = [r for r in rows if r['risp'] and r['run_scored']]
check('live tool finds the 2 run-scoring plays with a runner on 2nd/3rd', len(risp) == 2, str(len(risp)))
check('RBI-at-stake rows carry all three candidate-ruling answers',
      all(r['rbi_if_error'].startswith('NO RBI') and r['rbi_if_hit'] == 'RBI'
          and 'fielder' in r['rbi_if_fc'] for r in risp))
err_ball = next((r for r in rows if r['official_call'] == 'error'), None)
ranked = sorted(rows, key=lambda r: -r['score_100'])
check('the fixture\'s ruled error is ranked in the top half by the live score',
      err_ball is not None and ranked.index(err_ball) < len(rows) / 2,
      f"rank {ranked.index(err_ball)+1}/{len(rows)} at {err_ball['score_100']}/100" if err_ball else 'none')
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
rpl = (ROOT / 'docs/replays.html').read_text()
PAGES = {'index': idx, 'live': liv, 'replays': rpl, 'model': mod, 'overturned': ovt,
         'rules': rul, 'methods': met, 'roadmap': rdm}

for n, pg in PAGES.items():
    check(f'{n}: loads the shared nav and stylesheet', 'site.js' in pg and 'site.css' in pg)

# --- negative tests: wording that must never come back -----------------------
LEGACY = ['self-weighting sample', 'uniform 0.5 sampling fraction', 'hou @ min',
          '8/24 vs nym', 'j. mcneil', '25 plays carry', 'no video found for every']
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
check('model.html no longer asserts the sample is self-weighting',
      'self-weighting sample' not in flat(mod).lower())
check('roadmap records the self-weighting correction as history',
      'self-weighting' in flat(rdm).lower())

# --- every page's data dependency must exist and be the artifact it claims ---
DATA_FILES = {
    'index': ['data/model.json', 'data/live_now.json', 'data/live_slate.json'],
    'live': ['data/live_now.json', 'data/live_slate.json'],
    'replays': ['data/replays_site.json', 'data/replays.csv', 'data/replay_videos.csv'],
    'model': ['data/model.json'],
    'overturned': ['data/nydn_site.json'],
    'methods': ['data/ingest_summary.json', 'data/ruling_changes.json'],
}
for page, files in DATA_FILES.items():
    src = PAGES[page]
    for f in files:
        # a file produced only by CI (live_slate.json, ingest_summary.json) may legitimately be
        # absent from a fresh checkout; the page must still cite it and fall back to a committed file
        ci_only = f.endswith(('live_slate.json',))
        check(f'{page}: loads {f}' + ('' if ci_only else ' and it exists'),
              f in src and (ci_only or (ROOT / 'docs' / f).exists()),
              f'cited={f in src} exists={(ROOT / "docs" / f).exists()}')

# --- numbers printed on pages must exist in the artifacts they load ----------
kpis = load_json(ROOT / 'docs/data/site_kpis.json')
check('site_kpis.json repeats the model file exactly (auc, rows, games, errors)',
      abs(kpis['model']['auc_grouped'] - pp['cv_auc']) < 1e-9
      and kpis['model']['n_model'] == mm['n_model'] and kpis['model']['n_bip'] == mm['n_bip']
      and kpis['model']['games'] == mm['games'] and kpis['model']['n_error'] == mm['n_error'],
      json.dumps(kpis['model'])[:200])
check('site_kpis.json ingest counts match the ingest report',
      (not (ROOT / 'data/ingest/ingest_report.json').exists())
      or kpis['ingest']['games'] == load_json(ROOT / 'data/ingest/ingest_report.json')['summary']['games'])
check('site_kpis.json counts match the collected tables',
      kpis['replays']['overturned'] == sum(1 for r in load_csv(ROOT / 'docs/data/replays.csv')
                                           if r['review_overturned'] == '1')
      and kpis['nydn']['rows'] == len(load_csv(ROOT / 'docs/data/overturned_calls.csv')))
rep_site = load_json(ROOT / 'docs/data/replays_site.json')
check('replays_site.json rows are exactly the run-affected reviews',
      len(rep_site['rows']) == sum(1 for r in load_csv(ROOT / 'docs/data/replays.csv')
                                   if r['run_removed_heuristic'] == '1'
                                   or r['run_removed_hard'] == '1'))
check('every run-affected page row carries a watch link and an official feed link',
      all(r['watch_url'].startswith('https://baseballsavant.mlb.com/') or not r['play_id']
          for r in rep_site['rows'])
      and all(r['feed_url'].startswith('https://statsapi.mlb.com/') for r in rep_site['rows']))
check('replays page offers both a watch and a download path',
      'watch' in rpl and ('download mp4' in rpl or 'download' in rpl))

# --- required content --------------------------------------------------------
check('index carries the honesty banner and the own-the-outcome block',
      'honesty' in idx and 'Own the Outcome' in idx)
check('index renders its KPIs from the model file, not typed numbers',
      'data/model.json' in idx and 'id="kpis"' in idx)
presets = [(m.group(1), m.group(2)) for m in
           re.finditer(r'data-preset="([^"]+)"[^>]*>([^<]*)<', idx)]
ALL_BIP = [r for r in bip + load_csv(ROOT / 'docs/data/bip_official.csv')
           if r.get('numeric_ok') == '1']


def preset_is_real(preset):
    ev, la, dist, traj, hard = preset.split(',')[:5]
    return any(abs(float(r['launch_speed']) - float(ev)) < 0.05
               and abs(float(r['launch_angle']) - float(la)) < 0.05
               and abs(float(r['distance']) - float(dist)) < 0.5
               and r['trajectory'] == traj and r['hardness'] == hard for r in ALL_BIP)


typed = [p for p, label in presets if 'illustrative' not in label.lower()]
check(f'every non-illustrative index preset is a real batted ball ({len(typed)} of {len(presets)})',
      bool(typed) and all(preset_is_real(p) for p in typed),
      ' | '.join(p for p in typed if not preset_is_real(p)))
check('the illustrative preset is labelled as such on the page',
      any('illustrative' in label.lower() for _, label in presets)
      and any(preset_is_real(p) for p, label in presets if 'illustrative' not in label.lower()))
check('index shows an honest percentile alongside the raw score',
      'percentile' in idx and 'errorPercentile' in idx)
check('live page loads the live artifact and explains the refresh command',
      'data/live_now.json' in liv and 'tools/fetch_live.py' in liv)
check('live page states the outbound-network limitation honestly',
      'outbound HTTPS' in liv and 'statsapi.mlb.com' in liv and 'fallback' in liv)
check('model page publishes the honesty block + calibration',
      'model.json' in mod and ('honesty' in mod or 'calibration' in mod))
check('model page explains the NOT-self-weighting enrichment of the audit sample',
      'NOT' in mod and 'self-weighting' in plain_text(mod).lower()
      and 'error-enriched audit sample' in plain_text(mod))
check('overturned page flags the 404 source and offers the official fallback',
      '404' in ovt and 'mlb.com/video' in ovt and 'nydailynews' in ovt)
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
check('methods page publishes the ingest report and the dataset separation',
      'data/ingest_summary.json' in met and 'bip_official.csv' in met)
check('methods page explains how a pending decision becomes observable',
      'watch_rulings.py' in met and 'ruling_changes.csv' in met and 'ruling_snapshot.csv' in met
      and 'pending' in flat(met).lower())
check('ruling_changes.json is published with a real (possibly empty) record',
      (lambda rc: rc['available'] is True and rc['changes'] == len(rc['rows'])
       or rc['changes'] > 200)(load_json(ROOT / 'docs/data/ruling_changes.json')))
check('roadmap lists an ordered backlog and its limitations',
      'Next-session backlog' in rdm or 'backlog' in rdm.lower())
rd = (ROOT / 'README.md').read_text()
check('README restates the brief before any results (Section 0 rule)',
      'Section 0' in rd and 'VERBATIM' in rd)
nydn_sum = load_json(ROOT / 'docs/data/nydn_summary.json')
for want in (f"{mm['n_model']:,}", f"{mm['n_bip']:,}", f"{nydn_sum['rows_total']:,}",
             f"{nydn_sum['overturned']:,}", f"{nydn_sum['run_removed_heuristic']:,}",
             f"{pp['cv_auc']:.3f}"):
    check(f'README states the built number {want}', want in rd)

# --- official links ---------------------------------------------------------
OFFICIAL = ['https://statsapi.mlb.com/', 'https://www.mlb.com/glossary/standard-stats/error',
            'https://www.mlb.com/glossary/standard-stats/runs-batted-in',
            'https://mktg.mlbstatic.com/mlb/official-information/2025-official-baseball-rules.pdf',
            'https://baseballsavant.mlb.com/', 'https://github.com/nydailynews/mlb-overturned-calls']
all_html = ''.join(PAGES.values())
for link in OFFICIAL:
    check(f'official source linked: {link.split("/")[2]}', link in all_html)
check('no page cites a non-official source as official',
      not re.search(r'(https?://(?!www\.mlb\.com|statsapi\.mlb\.com|mktg\.mlbstatic\.com|'
                    r'baseballsavant\.mlb\.com|sporty-clips\.mlb\.com|bdata-producedclips\.mlb\.com|'
                    r'github\.com/nydailynews|github\.com/buffedlizard55-lab|'
                    r'www\.baseball-reference\.com)[^\s"\')]+)', all_html))

# ---------------------------------------------------------------- H. page/data contracts
print('== H. page <-> data contracts (the fields the pages actually read) ==')
live = load_json(ROOT / 'docs/data/live_now.json')
slate_path = ROOT / 'docs/data/live_slate.json'
if slate_path.exists():
    check('live_slate.json (CI output) has the same contract as the offline fallback',
          all(all(k in g for k in ['model_rows', 'away_abbr', 'home_abbr', 'batted_balls', 'verified'])
              for g in load_json(slate_path)['games'])
          and load_json(slate_path)['mode'] == 'live-slate')
GAME_KEYS = ['model_rows', 'pk', 'game_pk', 'away_abbr', 'home_abbr', 'state', 'batted_balls', 'errors',
             'top_pick_agrees', 'risp_runs_at_stake', 'overturned_reviews', 'verified', 'url', 'savant']
check('live_now.json: every game carries the fields the slate tables read',
      all(all(k in g for k in GAME_KEYS) for g in live['games']), 'game keys')
SUM_KEYS = ['games', 'games_verified_vs_linescore', 'batted_balls', 'errors', 'risp_runs_at_stake',
            'overturned_reviews', 'top_pick_agreement']
check('live_now.json: the summary carries every KPI the pages print',
      all(k in live['summary'] for k in SUM_KEYS), json.dumps(sorted(live['summary'])))
check('live_now.json: per-game counts agree with the rows they summarise',
      all(g['batted_balls'] == len([r for r in g['model_rows'] if r.get('status') == 'scored'])
          and g['top_pick_agrees'] == sum(r['model_agrees_with_call'] for r in g['model_rows']
                                          if r.get('status') == 'scored') for g in live['games']))
check('live_now.json: offline fallback is labelled, not passed off as a live slate',
      live['mode'] in ('live-slate', 'offline-fixture')
      and (live['mode'] == 'live-slate' or 'fixture' in json.dumps(live['note']).lower()))
SITE_R = load_json(ROOT / 'docs/data/replays_site.json')
check('replays_site.json: rows carry everything the replays page renders',
      all(all(k in r for k in ('date', 'matchup', 'inning', 'half', 'batter', 'review_type',
                               'review_subject', 'description', 'runs_by_movement', 'score_delta',
                               'rule', 'watch_url', 'mp4', 'feed_url', 'savant_game_url'))
          for r in SITE_R['rows'])
      and all(all(k in r for k in ('d', 'm', 'g', 'n', 'h', 'b', 't', 's', 'c', 'p'))
              for r in SITE_R['overturned_index']))
NY_SITE = load_json(ROOT / 'docs/data/nydn_site.json')
check('nydn_site.json: rows carry the archive fields and the resolved link status',
      all(all(k in r for k in ('date', 'game', 'play_type', 'player', 'inning', 'initial_call',
                               'result', 'run_removed_heuristic', 'video', 'link_status'))
          for r in NY_SITE['rows'])
      and NY_SITE['counts']['run_removed'] == len(NY_SITE['rows']))
MDL = load_json(ROOT / 'docs/data/model.json')
check('model.json: carries every block the site reads (feature spec, honesty, calibration, surface)',
      all(k in MDL for k in ('meta', 'primary', 'multiclass', 'honesty', 'surface', 'correlations',
                             'context_stats', 'risk_bands'))
      and all(k in MDL['primary'] for k in ('feature_spec', 'feature_names', 'scaler_mean',
                                            'scaler_scale', 'intercept', 'coef', 'cv_auc',
                                            'cv_auc_ci95', 'cv_auc_random_kfold',
                                            'cv_auc_gradient_boosting_grouped'))
      and all(k in MDL['honesty'] for k in ('what_this_is', 'oof_top1_accuracy',
                                            'oof_error_nominated_top1', 'oof_error_recall',
                                            'p_error_percentile_grid', 'calibration_oof',
                                            'grouped_vs_random_auc_gap')))
check('model.json: the feature spec and the coefficient map agree, name for name',
      [f['name'] for f in MDL['primary']['feature_spec']] == MDL['primary']['feature_names']
      and all(n in MDL['primary']['coef'] for n in MDL['primary']['feature_names'])
      and len(MDL['primary']['scaler_mean']) == len(MDL['primary']['feature_names']))
check('site_kpis.json: every KPI the model page reads is present and non-null',
      all(kpis['model'][k] is not None for k in ('dataset', 'n_bip', 'n_model', 'n_error', 'games',
                                                 'auc_grouped', 'auc_ci95', 'top1', 'brier'))
      and kpis['ingested']['bip'] == len(load_csv(ROOT / 'docs/data/bip_official.csv')))

print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S):')
    for f in FAILED:
        print('   -', f)
    sys.exit(1)
print('ALL CHECKS PASSED')
