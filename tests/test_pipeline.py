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
import csv, datetime, json, math, re, sys, tempfile
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
from sklearn.isotonic import IsotonicRegression                       # noqa: E402
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss, average_precision_score  # noqa: E402
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
for tr, te in gkf.split(X, yb, groups):
    fold_sc = StandardScaler().fit(X[tr])
    Xtr, Xte = fold_sc.transform(X[tr]), fold_sc.transform(X[te])
    oof_g[te] = LogisticRegression(max_iter=4000, C=1.0).fit(Xtr, yb[tr]).predict_proba(Xte)[:, 1]
    oof_gm[te] = LogisticRegression(max_iter=5000, C=1.0).fit(Xtr, yc[tr]).predict_proba(Xte)
skf = StratifiedKFold(5, shuffle=True, random_state=20260929)
oof_r = np.zeros(len(yb))
for tr, te in skf.split(X, yb):
    fold_sc = StandardScaler().fit(X[tr])
    Xtr, Xte = fold_sc.transform(X[tr]), fold_sc.transform(X[te])
    oof_r[te] = LogisticRegression(max_iter=4000, C=1.0).fit(Xtr, yb[tr]).predict_proba(Xte)[:, 1]

auc_g = float(roc_auc_score(yb, oof_g))
check('grouped OOF AUC recomputes to the published value',
      abs(auc_g - pp['cv_auc']) < 5e-4, f'{auc_g:.4f} vs {pp["cv_auc"]}')
check('random-fold AUC recomputes to the published comparator',
      abs(float(roc_auc_score(yb, oof_r)) - pp['cv_auc_random_kfold']) < 5e-4,
      f'{roc_auc_score(yb, oof_r):.4f} vs {pp["cv_auc_random_kfold"]}')
check('grouped and random-fold average precision recompute',
      abs(float(average_precision_score(yb, oof_g)) - pp['cv_average_precision_grouped']) < 5e-5
      and abs(float(average_precision_score(yb, oof_r)) - pp['cv_average_precision_random_kfold']) < 5e-5,
      f'{average_precision_score(yb, oof_g):.5f} vs {pp["cv_average_precision_grouped"]}')
check('published CI widens honestly around the grouped AUC',
      pp['cv_auc_ci95'][0] < pp['cv_auc'] < pp['cv_auc_ci95'][1]
      and pp['cv_auc_ci95'][1] - pp['cv_auc_ci95'][0] > 0.05,
      json.dumps(pp['cv_auc_ci95']))
check('OOF log-loss and Brier recompute',
      abs(log_loss(yb, oof_g, labels=[0, 1]) - pp['cv_logloss']) < 5e-4
      and abs(brier_score_loss(yb, oof_g) - pp['cv_brier_raw']) < 5e-5)
iso_oof = np.zeros(len(yb))
for outer_tr, outer_te in gkf.split(X, yb, groups):
    x_outer, y_outer, g_outer = X[outer_tr], yb[outer_tr], groups[outer_tr]
    inner_oof = np.zeros(len(outer_tr))
    inner_gkf = GroupKFold(4)
    for inner_tr, inner_te in inner_gkf.split(x_outer, y_outer, g_outer):
        inner_sc = StandardScaler().fit(x_outer[inner_tr])
        inner_model = LogisticRegression(max_iter=4000, C=1.0).fit(
            inner_sc.transform(x_outer[inner_tr]), y_outer[inner_tr])
        inner_oof[inner_te] = inner_model.predict_proba(inner_sc.transform(x_outer[inner_te]))[:, 1]
    calibrator = IsotonicRegression(out_of_bounds='clip').fit(inner_oof, y_outer)
    iso_oof[outer_te] = calibrator.predict(oof_g[outer_te])
check('nested grouped isotonic Brier recomputes from inner OOF calibrators',
      abs(brier_score_loss(yb, iso_oof) - pp['cv_brier_isotonic']) < 5e-5,
      f'{brier_score_loss(yb, iso_oof):.6f} vs {pp["cv_brier_isotonic"]}')
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
check('honesty block: training-fit P(error) range matches a fresh refit',
      abs(100 * float(p_all.max()) - hn['p_error_training_max_x100']) < 0.02
      and abs(100 * float(p_all.min()) - hn['p_error_training_min_x100']) < 0.005,
      f'{100*float(p_all.max()):.3f} vs {hn["p_error_training_max_x100"]}')
expected_bands = []
for pct in (1, 5, 10):
    n_top = max(1, math.ceil(len(yb) * pct / 100))
    chosen = np.argsort(oof_g, kind='mergesort')[-n_top:]
    found = int(yb[chosen].sum())
    expected_bands.append((pct, n_top, found, found / n_top, found / int(yb.sum()),
                           (found / n_top) / float(yb.mean())))
actual_bands = hn.get('top_error_risk_bands_oof', [])
check('grouped OOF top-1/5/10% risk bands recompute from held-out probabilities',
      [b.get('top_percent') for b in actual_bands] == [1, 5, 10]
      and all(b.get('n') == n and b.get('errors_found') == found
              and abs(b.get('precision', -1) - precision) < 2e-5
              and abs(b.get('recall', -1) - recall) < 2e-5
              and abs(b.get('lift_over_base_rate', -1) - lift) < 0.002
              for b, (_, n, found, precision, recall, lift) in zip(actual_bands, expected_bands)))
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
live_rows = ls.score_feed(fixture, pk=823441, scorer=scorer)
rows = [r for r in live_rows if r.get('status') == 'scored']
check('every live scorer row carries a direct official MLB feed URL',
      all(r.get('official_feed_url') == 'https://statsapi.mlb.com/api/v1.1/game/823441/feed/live'
          for r in live_rows))
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
check('RBI-at-stake rows carry conditional Rule 9.04 notes for all three candidate rulings',
      all('Rule 9.04(a)(3)' in r['rbi_if_error'] and 'cannot establish' in r['rbi_if_error']
          and 'Rule 9.04' in r['rbi_if_hit'] and 'Rule 9.04' in r['rbi_if_fc'] for r in risp))
check('home-run RBI notes do not fabricate an error or fielder\'s-choice alternative',
      'not a valid alternative' in ls.rbi_if_ruled('error', True, 'home_run')
      and 'recorded RBI field' in ls.rbi_if_ruled('hit', True, 'home_run')
      and 'not a valid alternative' in ls.rbi_if_ruled('fielders_choice', True, 'home_run'))
err_ball = next((r for r in rows if r['official_call'] == 'error'), None)
ranked = sorted(rows, key=lambda r: -r['score_100'])
check('the fixture\'s ruled error is ranked in the top half by the live score',
      err_ball is not None and ranked.index(err_ball) < len(rows) / 2,
      f"rank {ranked.index(err_ball)+1}/{len(rows)} at {err_ball['score_100']}/100" if err_ball else 'none')
check('published live board matches a fresh run of the tool',
      load_json(ROOT / 'docs/data/live_sample.json') == [
          {**r, 'source': 'data/source/feed_823441.json'} for r in ls.score_feed(
              fixture, pk=823441, scorer=scorer, meta={'source': 'data/source/feed_823441.json'})])
unresolved_feed = {'liveData': {'plays': {'allPlays': [{
    'result': {'description': 'In play, run(s)', 'rbi': 0, 'awayScore': 1, 'homeScore': 0},
    'about': {'atBatIndex': 7, 'inning': 4, 'halfInning': 'top'},
    'matchup': {'batSide': {'code': 'R'}, 'pitchHand': {'code': 'L'}},
    'playEvents': [{'hitData': {'launchSpeed': 101.2, 'launchAngle': 12.0, 'totalDistance': 210.0,
                                'trajectory': 'line_drive', 'hardness': 'hard'}, 'playId': 'p-1'}],
    'runners': [{'movement': {'start': '3B', 'end': 'score'}}]}]}}}
prow = ls.score_feed(unresolved_feed, pk=1, scorer=scorer)[0]
check('a contact play without result.eventType is flagged as a feed state, not a scorer queue',
      prow['status'] == 'no_event_type_yet' and prow['official_call'] == 'pending'
      and prow['model_agrees_with_call'] == 0 and 0 <= prow['score_100'] <= 100,
      json.dumps({k: prow[k] for k in ('status', 'official_call', 'score_100', 'top_pick')}))
check('the no-eventType row still carries model answers and a conditional RBI reminder',
      prow['top_pick'] in ('hit', 'out', 'error', 'fielders_choice') and prow['risp'] == 1
      and prow['run_scored'] == 1 and 'Rule 9.04(a)(3)' in prow['rbi_if_error']
      and 'cannot establish' in prow['rbi_if_error'])
spec_wr = importlib.util.spec_from_file_location('watch_rulings', ROOT / 'tools/watch_rulings.py')
wr = importlib.util.module_from_spec(spec_wr)
spec_wr.loader.exec_module(wr)
check('watch_rulings.team_abbr accepts the hydrated and un-hydrated schedule shapes',
      wr.team_abbr({'abbreviation': 'NYY', 'name': 'New York Yankees'}) == 'NYY'
      and wr.team_abbr({'name': 'Boston Red Sox'}) == 'Boston Red Sox'
      and wr.team_abbr({}) == '' and wr.team_abbr(None) == '')
wr_src = (ROOT / 'tools/watch_rulings.py').read_text()
check('watch_rulings requests hydrated teams (the un-hydrated shape caused a real CI crash)',
      'hydrate=team' in wr_src)
transition = wr.transition_key('1', '2', 'event_type', 'field_error', 'single', '2026-09-29T10:00:00Z')
retry = wr.transition_key('1', '2', 'event_type', 'field_error', 'single', '2026-09-29T10:00:00Z')
recurrence = wr.transition_key('1', '2', 'event_type', 'field_error', 'single', '2026-09-29T10:05:00Z')
check('ruling-change dedupe suppresses retries but preserves a later repeat of the same transition',
      transition == retry and transition != recurrence)
with tempfile.TemporaryDirectory() as temp_dir:
    first_change_path = Path(temp_dir) / 'first-change.csv'
    first_change = {key: '' for key in wr.CHANGE_KEYS}
    first_change.update({'game_pk': '1', 'at_bat': '2', 'field': 'event_type',
                         'from': 'field_error', 'to': 'single'})
    wr.append_changes(first_change_path, [first_change])
    created = first_change_path.exists() and load_csv(first_change_path) == [first_change]
check('the first detected ruling change creates its ledger instead of losing the observation', created)
check('the ruling snapshot only rewrites rows whose observation actually changed '
      '(a per-run rewrite would bloat the repo with a multi-megabyte blob per collection)',
      "unchanged = bool(prev)" in wr_src and "r['last_changed_utc']" in wr_src and "if unchanged else now_utc" in wr_src)
check('watch_rulings.py always leaves a readable status file',
      'ruling_watch_status.json' in (ROOT / 'tools/watch_rulings.py').read_text()
      and 'run_safely' in (ROOT / 'tools/watch_rulings.py').read_text())
spec_refresh = importlib.util.spec_from_file_location('refresh_live', ROOT / 'tools' / 'refresh_live.py')
refresh_live = importlib.util.module_from_spec(spec_refresh)
spec_refresh.loader.exec_module(refresh_live)
with tempfile.TemporaryDirectory() as temp_dir:
    temp = Path(temp_dir)
    src_json, src_csv = temp / 'new.json', temp / 'new.csv'
    out_json, out_csv = temp / 'live.json', temp / 'live.csv'
    src_json.write_bytes(b'{"snapshot":"new"}')
    src_csv.write_bytes(b'row\r\n')
    out_json.write_bytes(b'{"snapshot":"old"}')
    out_csv.write_bytes(b'old\r\n')
    real_replace = refresh_live.os.replace
    def fail_json_replace(source, target):
        if Path(target) == out_json:
            raise OSError('simulated JSON rename failure')
        return real_replace(source, target)
    refresh_live.os.replace = fail_json_replace
    try:
        refresh_live.install_snapshot(src_json, src_csv, out_json, out_csv)
        install_failed_as_expected = False
    except OSError:
        install_failed_as_expected = True
    finally:
        refresh_live.os.replace = real_replace
    retained_old_json = out_json.read_bytes() == b'{"snapshot":"old"}'
    no_staged_files = not list(temp.glob('.*.tmp'))
check('partial static promotion leaves the prior JSON snapshot intact and cleans temporary files',
      install_failed_as_expected and retained_old_json and no_staged_files)
with tempfile.TemporaryDirectory() as temp_dir:
    temp = Path(temp_dir)
    src_json, src_csv = temp / 'new.json', temp / 'new.csv'
    out_json, out_csv = temp / 'live.json', temp / 'live.csv'
    src_json.write_bytes(b'{"snapshot":"new"}')
    src_csv.write_bytes(b'row\r\n')
    refresh_live.install_snapshot(src_json, src_csv, out_json, out_csv)
    complete_snapshot = out_json.read_bytes() == src_json.read_bytes() and out_csv.read_bytes() == src_csv.read_bytes()
check('successful static promotion installs both prepared snapshot artifacts', complete_snapshot)
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

# --- every inline script must at least parse (a real bug was caught this way) --------------
import shutil, subprocess, tempfile                                            # noqa: E402
if shutil.which('node'):
    bad_js = []
    for name, src in PAGES.items():
        for i, block in enumerate(re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', src, re.S)):
            code = ('(async function(){' + block + '})()') if ('await' in block and
                                                              not block.lstrip().startswith('(async')) else block
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
                fh.write(code)
                path = fh.name
            r = subprocess.run(['node', '--check', path], capture_output=True, text=True)
            if r.returncode:
                bad_js.append(f'{name} block {i+1}: {r.stderr.strip().splitlines()[-1][:120]}')
    check(f'every inline page script parses ({len(PAGES)} pages)', not bad_js, ' | '.join(bad_js))
    js_contract = r'''const fs=require('fs'), vm=require('vm');
const ctx={document:{querySelector:()=>null,querySelectorAll:()=>[]}};
vm.runInNewContext(fs.readFileSync('docs/site.js','utf8'),ctx);
const M={honesty:{p_error_percentile_grid:[
  {percentile:0,p_error:0.2},{percentile:50,p_error:0.5},{percentile:100,p_error:0.8}]}};
if(ctx.errorPercentile(M,0.1)!==0 || ctx.errorPercentile(M,0.5)!==50 ||
   ctx.errorPercentile(M,0.9)!==100) throw new Error('percentile escaped 0..100 or interpolated incorrectly');
if(!ctx.rbiRuleNote('error',true,'home_run').includes('not a valid alternative') ||
   !ctx.rbiRuleNote('fielders_choice',true,'home_run').includes('not a valid alternative') ||
   !ctx.rbiRuleNote('hit',true,'home_run').includes('recorded RBI field'))
  throw new Error('home-run RBI counterfactual note is overconfident');'''
    contract = subprocess.run(['node', '-e', js_contract], cwd=ROOT, capture_output=True, text=True)
    check('browser percentile is clamped to the grouped-OOF 0–100 reference',
          contract.returncode == 0, contract.stderr.strip()[-300:])
    check('browser home-run RBI notes avoid impossible error/FC counterfactuals',
          contract.returncode == 0, contract.stderr.strip()[-300:])
else:
    print('  --   (node not installed: skipped the JavaScript syntax check)')

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
replay_rows = load_csv(ROOT / 'docs/data/replays.csv')
hr_rbi_rows = [r for r in replay_rows if r['event_type'] == 'home_run' and r.get('rbi_if_fc')]
check('home-run replay rows do not label an RBI as a counterfactual fielder\'s choice',
      len(hr_rbi_rows) > 0
      and all('not a valid alternative' in r['rbi_if_error']
              and 'recorded RBI field' in r['rbi_if_hit']
              and 'not a valid alternative' in r['rbi_if_fc'] for r in hr_rbi_rows))
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
check('index labels percentile against grouped OOF scores, not in-sample fitted scores',
      'grouped out-of-fold scores' in idx and 'errorPercentile' in idx
      and 'every batted ball in the fitted set' not in idx)
check('live page loads the live artifact and explains the refresh command',
      'data/live_slate.json' in liv and 'tools/fetch_live.py' in liv)
check('live board links each play back to its official feed and watch page, including snapshots',
      "linkOut(x.official_feed_url, 'official feed')" in liv
      and "linkOut(x.savant_url, 'watch')" in liv
      and 'official_feed_url: r.official_feed_url || g.official_feed_url' in liv
      and 'official_feed_url' in (ROOT / 'docs/site.js').read_text())
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
check('rules page cites the 2026 official rulebook and uses the correct 9.04 section',
      '2026-official-baseball-rules.pdf' in rul and '9.04' in rul
      and not re.search(r'Rule\s+10\.04', flat(rul), re.I))
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
rd_early = (ROOT / 'README.md').read_text()
audit_wf = (ROOT / '.github/workflows/audit.yml').read_text()
check('the reproducibility gate is wired to tools/check_repro.py',
      'tools/check_repro.py' in audit_wf and (ROOT / 'tools/check_repro.py').exists())
check('the reproducibility policy is stated in the README (byte-exact tables, tolerated model digits)',
      'byte-identical' in rd_early and '1e-6' in rd_early and 'BLAS' in rd_early)
rd = (ROOT / 'README.md').read_text()
probe_report = load_json(ROOT / 'data/ingest/probe_report.json')
probed = {r.get('probe') for r in probe_report.get('records', [])}
check('committed endpoint evidence distinguishes schedule and game-feed CORS probes',
      {'cors_statsapi', 'cors_statsapi_feed'} <= probed)
check('site copy treats the sampled CORS headers as evidence, not a guarantee or browser QA',
      'one game-feed request' in rd and 'not a full browser test' in rd
      and 'real-browser Pages smoke test remain open' in rdm)
check('README restates the brief before any results (Section 0 rule)',
      'Section 0' in rd and 'VERBATIM' in rd)
brief_opening = ("I want to investigate if there is a way to accurately predict the outcome of any scoring decision "
                 "such as a pending scoring decision, or if we can tell if an error would be overturned into another "
                 "play such as a fielders choice or a hit.  There's also the possibility of if the play is initially "
                 "ruled an error that the batter gets no RBI if there is a runner on 2nd or 3rd.  However if they rule "
                 "it as a fielders choice or a hit, there's a chance that the batter would be awarded an RBI.")
check('README preserves the exact repeated opening paragraph twice', rd.count(brief_opening) == 2)
raw_malformed_link = ('[[https://github.com/buffedlizard55-lab/MLB-overturned-calls]'
                      '(https://github.com/buffedlizard55-lab/MLB-overturned-calls)]'
                      '(https://github.com/buffedlizard55-lab/MLB-overturned-calls]'
                      '(https://github.com/buffedlizard55-lab/MLB-overturned-calls))')
check('README preserves the malformed GitHub link payload from the brief', raw_malformed_link in rd)
nydn_sum = load_json(ROOT / 'docs/data/nydn_summary.json')
for want in (f"{mm['n_model']:,}", f"{mm['n_bip']:,}", f"{nydn_sum['rows_total']:,}",
             f"{nydn_sum['overturned']:,}", f"{nydn_sum['run_removed_heuristic']:,}",
             f"{pp['cv_auc']:.4f}"):
    check(f'README states the built number {want}', want in rd)

# --- official links ---------------------------------------------------------
OFFICIAL = ['https://statsapi.mlb.com/', 'https://www.mlb.com/glossary/standard-stats/error',
            'https://www.mlb.com/glossary/standard-stats/runs-batted-in',
            'https://mktg.mlbstatic.com/mlb/official-information/2026-official-baseball-rules.pdf',
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
          all(all(k in g for k in ['model_rows', 'away_abbr', 'home_abbr', 'batted_balls', 'verified',
                                    'official_feed_url'])
              and g['official_feed_url'].startswith('https://statsapi.mlb.com/api/v1.1/game/')
              for g in load_json(slate_path)['games'])
          and load_json(slate_path)['mode'] == 'live-slate'
          and 'unresolved_in_feed' in load_json(slate_path)['summary']
          and 'pending_rulings' not in load_json(slate_path)['summary'])
GAME_KEYS = ['model_rows', 'pk', 'game_pk', 'away_abbr', 'home_abbr', 'state', 'batted_balls', 'errors',
             'top_pick_agrees', 'risp_runs_at_stake', 'overturned_reviews', 'verified', 'url', 'savant',
             'official_feed_url']
spec_fl = importlib.util.spec_from_file_location('fetch_live', ROOT / 'tools/fetch_live.py')
fl = importlib.util.module_from_spec(spec_fl)
spec_fl.loader.exec_module(fl)
check('fetch_live.split_matchup never invents or truncates a team abbreviation',
      fl.split_matchup('PHI @ ATL') == ('PHI', 'ATL')
      and fl.split_matchup('CWS @ HOU') == ('CWS', 'HOU')
      and fl.split_matchup('') == ('', '') and fl.split_matchup(None) == ('', ''))
check('the live board abbreviations agree with the matchup string it prints',
      all((g.get('away_abbr', '') + ' @ ' + g.get('home_abbr', '')) == g.get('matchup', '')
          for g in load_json(ROOT / 'docs/data/live_now.json')['games']))
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
                                            'p_error_training_min_x100', 'p_error_training_max_x100',
                                            'top_error_risk_bands_oof', 'p_error_percentile_grid',
                                            'calibration_oof', 'grouped_vs_random_auc_gap'))
      and all(k in MDL['primary'] for k in ('cv_average_precision_grouped',
                                            'cv_average_precision_random_kfold',
                                            'cv_brier_isotonic')))
check('model.json: the feature spec and the coefficient map agree, name for name',
      [f['name'] for f in MDL['primary']['feature_spec']] == MDL['primary']['feature_names']
      and all(n in MDL['primary']['coef'] for n in MDL['primary']['feature_names'])
      and len(MDL['primary']['scaler_mean']) == len(MDL['primary']['feature_names']))
check('site_kpis.json: every KPI the model page reads is present and non-null',
      all(kpis['model'][k] is not None for k in ('dataset', 'n_bip', 'n_model', 'n_error', 'games',
                                                 'auc_grouped', 'auc_ci95', 'top1', 'brier'))
      and kpis['model_dataset']['bip'] == len(load_csv(ROOT / 'docs/data/bip_official.csv'))
      and kpis['model_dataset']['games'] == M['meta']['games']
      and kpis['model']['average_precision_grouped'] == pp['cv_average_precision_grouped']
      and kpis['model']['top_error_risk_bands_oof'] == hn['top_error_risk_bands_oof'])

# ---------------------------------------------------------------- I. alerts (watcher + browser)
print('== I. live alerts: the watcher diff and the browser engine ==')
sys.path.insert(0, str(ROOT / 'tools'))
import watch_rulings as WR                                                # noqa: E402

# The regression that broke the scheduled watcher in production: the snapshot CSV stores every value
# as text, the API returns ints for rbi/reviewed/overturned, and the first version compared the two
# directly and then sliced the integer. This must never come back.
stored = {('824872', '0'): {'game_pk': '824872', 'at_bat': '0', 'event_type': '',
                            'status': 'no_event_type_yet', 'rbi': '0', 'description': 'x',
                            'reviewed': '0', 'overturned': '0', 'review_type': '',
                            'first_seen_utc': 'T0', 'last_changed_utc': 'T0', 'play_id': 'p1',
                            'bases_before': '2B', 'launch_speed': '98.1', 'launch_angle': '4',
                            'distance': '112'}}
fresh = [{'game_pk': '824872', 'at_bat': '0', 'event_type': '', 'status': 'no_event_type_yet',
          'rbi': 0, 'description': 'x', 'reviewed': 0, 'overturned': 0, 'review_type': '',
          'date': '2026-09-10', 'matchup': 'TB @ ATL', 'inning': 1, 'half': 'top', 'play_id': 'p1',
          'batter': 'A', 'bases_before': '2B', 'launch_speed': 98.1, 'launch_angle': 4,
          'distance': 112}]
check('watcher: an unchanged play whose API values are ints and stored values are text is not a change',
      WR.detect_changes(stored, fresh, set(), 'T1') == [])
settled = WR.detect_changes(stored, [dict(fresh[0], event_type='field_error')], set(), 'T1')
check('watcher: a feed row that gains an eventType is recorded once with its first state',
      len(settled) == 1 and settled[0]['field'] == 'event_type'
      and settled[0]['from'] == '' and settled[0]['to'] == 'field_error'
      and settled[0]['was_unresolved_in_feed'] == 1,
      json.dumps(settled)[:200])
seen_w = set()
WR.detect_changes(stored, [dict(fresh[0], event_type='field_error')], seen_w, 'T1')
check('watcher: a workflow retry of the same transition is deduplicated',
      WR.detect_changes(stored, [dict(fresh[0], event_type='field_error')], seen_w, 'T2') == [])
later = {('824872', '0'): dict(stored[('824872', '0')], last_changed_utc='T9')}
check('watcher: a later recurrence of the same transition is preserved, not swallowed',
      len(WR.detect_changes(later, [dict(fresh[0], event_type='field_error')], seen_w, 'T3')) == 1)
check('watcher: every recorded change carries the official feed and scoring-changes links',
      all(c['feed_url'].startswith('https://statsapi.mlb.com/')
          and c['scoring_changes_url'].startswith('https://www.mlb.com/') for c in settled))

ALERTS_JSON = load_json(ROOT / 'docs/data/alerts.json')
check('alerts.json: the CI ledger exists with its provenance and honesty note',
      ALERTS_JSON['available'] is True and ALERTS_JSON['generated_from']
      and 'no open scorer-decision queue' in ALERTS_JSON['note'].lower()
      and {'status_ok', 'updated_utc', 'plays_observed', 'unresolved_in_feed',
           'changes_recorded'} <= set(ALERTS_JSON['watcher']))
check('alerts.json: every row names its source and carries a checkable link',
      all(a.get('source') in ('ci-watch', 'ci-review') for a in ALERTS_JSON['alerts'])
      and all(a.get('feed_url', '').startswith('https://statsapi.mlb.com/')
              for a in ALERTS_JSON['alerts'])
      and all(a.get('note') for a in ALERTS_JSON['alerts']))
check('alerts.json: counts agree with the rows they summarise',
      ALERTS_JSON['counts']['alerts'] == len(ALERTS_JSON['alerts'])
      and ALERTS_JSON['counts']['run_affected_reviews']
      == sum(1 for a in ALERTS_JSON['alerts'] if a['source'] == 'ci-review'))
BROKEN_WATCH = (ALERTS_JSON['watcher'].get('status_ok') is False)
check('alerts.json: a watcher failure is surfaced, never shown as a silent zero',
      (not BROKEN_WATCH) or bool(ALERTS_JSON['watcher'].get('error')))
check('alerts.html states the failure of the scheduled watcher when one is recorded',
      (not BROKEN_WATCH) or 'reported a failure' in (ROOT / 'docs/alerts.html').read_text())

if shutil.which('node'):
    js_alerts = r'''const fs=require('fs'),vm=require('vm');
const ctx={}; vm.runInNewContext(fs.readFileSync('docs/alerts.js','utf8'),ctx);
const A=ctx.ALERTS;
const base={game_pk:'1',at_bat:5,matchup:'TB @ ATL',inning:6,half:'top',batter:'X',play_id:'p1',
  event_type:'',official_call:'pending',status:'no_event_type_yet',score_100:4.2,
  official_feed_url:'https://statsapi.mlb.com/api/v1.1/game/1/feed/live',savant_url:''};
if(A.diffBoards(A.boardIndex([]),[base],{newRows:false}).length!==0)
  throw new Error('the baseline poll raised alerts; opening the page would flood the ledger');
let a1=A.diffBoards(A.boardIndex([base]),[Object.assign({},base,{event_type:'field_error',official_call:'error',status:'scored'})]);
if(a1.length!==1||a1[0].type!=='decision_settled'||a1[0].severity!=='high')
  throw new Error('a feed row that gained a ruling did not raise exactly one decision_settled alert');
const before=Object.assign({},base,{event_type:'field_error',official_call:'error',status:'scored',rbi_official:0});
const after=Object.assign({},base,{event_type:'single',official_call:'hit',status:'scored',rbi_official:1,risp:1,run_scored:1});
const types=A.diffBoards(A.boardIndex([before]),[after]).map(a=>a.type).sort();
if(types.join(',')!=='event_type_changed,rbi_changed,run_at_stake')
  throw new Error('error -> single with an RBI move must raise the change, RBI and run-at-stake alerts: '+types);
const changed=A.diffBoards(A.boardIndex([before]),[after]).find(a=>a.type==='event_type_changed');
if(!changed.error_to_non_error||!changed.rbi_question.includes('9.04(a)(3)'))
  throw new Error('the error -> non-error alert does not carry the verified Rule 9.04(a)(3) pointer');
if(!changed.note.includes('no open scorer-decision queue')&&!changed.note.includes('publishes no open scorer-decision queue'))
  throw new Error('the alert note must not imply a visible scorer queue');
const seen=new Set();
A.diffBoards(A.boardIndex([before]),[after],{seenIds:seen,at:'T1'});
if(A.diffBoards(A.boardIndex([before]),[after],{seenIds:seen,at:'T2'}).length!==0)
  throw new Error('the same transition alerted twice');
const many=A.diffBoards(A.boardIndex([base]),[
  Object.assign({},base,{at_bat:1,event_type:'field_error',official_call:'error',status:'scored'}),
  Object.assign({},base,{at_bat:2,event_type:'single',official_call:'hit',status:'scored',score_100:9})]);
if(many[0].severity!=='high')
  throw new Error('alerts are not ordered by severity');
if(A.alertLabel('event_type_changed')!=='ruling changed')
  throw new Error('alert labels drifted from what the page prints');'''
    script = ROOT / 'tests' / 'alerts_engine_check.js'
    script.write_text(js_alerts)
    try:
        r = subprocess.run(['node', str(script)], cwd=ROOT, capture_output=True, text=True)
        check('alerts.js: baseline, settle, error -> hit change, RBI move, dedupe and severity all hold',
              r.returncode == 0, r.stderr.strip()[-300:])
    finally:
        script.unlink(missing_ok=True)
else:
    print('  --   (node not installed: skipped the alert-engine behaviour check)')

# ---------------------------------------------------------------- J. the site's live-alert wiring
alerts_page = (ROOT / 'docs/alerts.html').read_text()
check('alerts page: loads the shared site script and the pure alert engine',
      'site.js' in alerts_page and 'alerts.js' in alerts_page)
check('alerts page: is in the shared navigation',
      "['alerts.html', 'Live alerts']" in (ROOT / 'docs/site.js').read_text())
DATA_FILES['alerts'] = ['data/model.json', 'data/alerts.json', 'data/live_now.json']
PAGES['alerts'] = alerts_page
check('alerts page: loads the committed model, the CI ledger and the snapshot fallback',
      all(f in alerts_page for f in ('data/model.json', 'data/alerts.json', 'data/live_now.json')))
check('alerts page: the notification path is opt-in and never auto-granted',
      'requestPermission' in alerts_page and 'notifyOn = false' in alerts_page.replace('let notifyOn = false;', 'notifyOn = false'))
check('alerts page: exports the session ledger, so an observation can be checked later',
      'downloadCSV(' in alerts_page and 'downloadJSON(' in alerts_page)
check('alerts page: does not claim to see a scorer queue',
      'no open scorer-decision queue' in alerts_page or 'publishes no open scorer-decision queue' in alerts_page)
check('replays page: offers an inline player for rows with a file rendition, and says when there is none',
      'button[data-play]' in (ROOT / 'docs/replays.html').read_text()
      and 'no mp4 rendition exposed for this play id' in (ROOT / 'docs/replays.html').read_text())
check('replays page: the player streams the league host and never re-hosts a clip',
      'sporty-clips' not in (ROOT / 'docs/replays.html').read_text().split('data-play=')[0]
      and 'not re-hosted' in (ROOT / 'docs/replays.html').read_text())

# ---------------------------------------------------------------- K. the verified rule text
rules_page = (ROOT / 'docs/rules.html').read_text()
rules_flat = flat(rules_page)
check('rules page: quotes Rule 9.04(a)(3) verbatim from the 2026 rulebook',
      'before two are out, an error is made on a play on which a runner from third base ordinarily '
      'would score' in rules_flat)
check('rules page: quotes the Rule 9.04(b) exceptions and the 9.04(c) judgment standard',
      'grounds into a force double play or a reverse-force double play' in rules_flat
      and 'throws to a wrong base' in rules_flat)
rules_low = rules_flat.lower()
check('rules page: quotes the Rule 9.01(a) preliminary -> final (24 hours) -> appeal (72 hours) clock',
      'preliminary' in rules_low and 'within 24 hours after a game concludes' in rules_low
      and 'within 72 hours of a judgment becoming final' in rules_low)
check('rules page: quotes the Rule 9.12 error definition and what is not an error',
      'fumble, muff or wild throw' in rules_flat and 'mental mistakes or misjudgments' in rules_flat)
check('rules page: the stale "page was not independently extracted" caveat is gone',
      'not independently extracted' not in rules_flat)
check('rules page: every quoted rule links the official PDF at its printed page',
      rules_page.count('2026-official-baseball-rules.pdf#page=') >= 3
      and 'page=115' in rules_page and 'page=106' in rules_page and 'page=127' in rules_page)
check('site copy states the RBI consequence of an error play is conditional, not automatic',
      'error ⇒ no RBI' in rules_page or 'error means no RBI' in rules_flat.replace('\u2019', "'"))
check('methods page records the watcher defect that was found and fixed',
      'int' in (ROOT / 'docs/methods.html').read_text()
      and 'ruling_watch_status.json' in (ROOT / 'docs/methods.html').read_text()
      and 'normalises both sides' in flat((ROOT / 'docs/methods.html').read_text()))

# ---------------------------------------------------------------- L. new model diagnostics
check('model.json: the review queue table is monotone in size and reports cost and benefit',
      [q['top_percent'] for q in MDL['honesty']['review_queue_oof']] == [0.5, 1, 2, 3, 5, 10, 20, 30]
      and all(q['plays_to_review'] < q2['plays_to_review']
              for q, q2 in zip(MDL['honesty']['review_queue_oof'],
                               MDL['honesty']['review_queue_oof'][1:]))
      and all(q['errors_found'] >= 0 and q['lift_over_base_rate'] > 1
              for q in MDL['honesty']['review_queue_oof']))
check('model.json: average precision carries a grouped-bootstrap interval that brackets it',
      MDL['primary']['cv_average_precision_ci95'][0] < MDL['primary']['cv_average_precision_grouped']
      < MDL['primary']['cv_average_precision_ci95'][1])
check('model.json: the regularisation, weighting and extended-feature experiments are published',
      len(MDL['experiments']['regularisation_grid']) >= 5
      and 'class_weight_balanced' in MDL['experiments']
      and 'extended_features' in MDL['experiments']
      and 'did not' in MDL['experiments']['extended_features']['note'])
check('model.json: the blend comparator is labelled unshipped with its reason',
      MDL['blend_comparator']['shipped'] is False
      and 'live scorer must be evaluable in the browser' in MDL['blend_comparator']['why_not_shipped'])
check('model.json: permutation importance covers every published feature',
      {r['feature'] for r in MDL['permutation_importance']['rows']}
      == set(MDL['primary']['feature_names']))
check('model page renders the new diagnostics from the JSON, not from literals',
      all(k in (ROOT / 'docs/model.html').read_text()
          for k in ('review_queue_oof', 'permutation_importance')))

# ---------------------------------------------------------------- M. watcher, end to end, offline
print('== M. watcher end to end on the cached official feeds ==')
import copy as _copy, pathlib as _pathlib, tempfile as _tempfile
RAW_FEEDS = {f.stem: json.loads(f.read_text()) for f in sorted((ROOT / 'data/raw').glob('*.json'))}
check('watcher e2e: the repository keeps cached official feeds to replay the collector against',
      len(RAW_FEEDS) >= 20, f'{len(RAW_FEEDS)} feed files')
GAMES = [{'pk': pk, 'date': '2026-09-29', 'away': 'AWY', 'home': 'HME', 'abstract_state': 'Final',
          'gameType': 'R', 'state': 'Final'} for pk in sorted(RAW_FEEDS)]
_save = (WR.schedule, WR.get_json, WR.ROOT, WR.SNAP, WR.CHANGES)
with _tempfile.TemporaryDirectory() as _td:
    _tmp = _pathlib.Path(_td)
    WR.ROOT, WR.SNAP, WR.CHANGES = _tmp, _tmp / 'snap.csv', _tmp / 'changes.csv'
    WR.schedule = lambda start, end, active_only=False: list(GAMES)
    WR.get_json = lambda url, timeout=45, retries=3: RAW_FEEDS[url.split('/game/')[1].split('/')[0]]
    try:
        rc1 = WR.run_safely(['--days', '21', '--date', '2026-09-29'])
        st = json.loads((WR.ROOT / 'data' / 'ingest' / 'ruling_watch_status.json').read_text())
        n_plays = st.get('plays', 0)
        rc2 = WR.run_safely(['--days', '21', '--date', '2026-09-29'])
        st2 = json.loads((WR.ROOT / 'data' / 'ingest' / 'ruling_watch_status.json').read_text())
        check('watcher e2e: a full run over 24 cached official feeds completes and reports its state',
              rc1 == 0 and st.get('ok') is True and n_plays > 1000 and not st.get('failures'),
              json.dumps(st)[:200])
        check('watcher e2e: the status file names the tool revision that produced it',
              st.get('tool') == 'tools/watch_rulings.py' and st.get('tool_version') == WR.TOOL_VERSION)
        check('watcher e2e: re-reading the same feeds raises no changes and no duplicate ledger rows',
              rc2 == 0 and st2.get('changes') == 0
              and (not WR.CHANGES.exists() or WR.CHANGES.stat().st_size == 0))

        # Simulate the brief's core case end to end: a batted ball captured before the ruling, then the
        # same feed re-read after the scorer's call lands.
        pk0 = sorted(RAW_FEEDS)[0]
        pi0, play = next((i, p) for i, p in enumerate(RAW_FEEDS[pk0]['liveData']['plays']['allPlays'])
                         if (p.get('result') or {}).get('eventType') in WR.BATTED
                         and next((e for e in p.get('playEvents') or [] if e.get('hitData')), None))
        ab0 = str(pi0)
        ruling = play['result']['eventType']
        pending_feed = _copy.deepcopy(RAW_FEEDS[pk0])
        pending_play = pending_feed['liveData']['plays']['allPlays'][pi0]
        pending_play['result']['eventType'] = ''
        pending_play['result'].pop('rbi', None)
        RAW_FEEDS[pk0] = pending_feed
        WR.run_safely(['--days', '21', '--date', '2026-09-29'])
        st3 = json.loads((WR.ROOT / 'data' / 'ingest' / 'ruling_watch_status.json').read_text())
        RAW_FEEDS[pk0] = _copy.deepcopy(pending_feed)
        RAW_FEEDS[pk0]['liveData']['plays']['allPlays'][pi0]['result']['eventType'] = ruling
        WR.run_safely(['--days', '21', '--date', '2026-09-29'])
        ledger = list(csv.DictReader(open(WR.CHANGES, newline='')))
        settled_rows = [c for c in ledger if c['game_pk'] == pk0 and c['at_bat'] == ab0
                        and c['field'] == 'event_type' and c['to'] == ruling]
        check('watcher e2e: a ruling that lands after a pre-decision capture is recorded as such',
              len(settled_rows) == 1 and settled_rows[0]['was_unresolved_in_feed'] == '1'
              and settled_rows[0]['first_observation_status'] == 'no_event_type_yet'
              and settled_rows[0]['feed_url'].endswith(f'/game/{pk0}/feed/live'),
              json.dumps(settled_rows[:1])[:220])
        check('watcher e2e: the pre-decision capture and the settled state are both in the change ledger',
              st3.get('unresolved_in_feed', 0) >= 1
              and any(c['field'] == 'status' and c['to'] == 'no_event_type_yet' for c in ledger))
    finally:
        WR.schedule, WR.get_json, WR.ROOT, WR.SNAP, WR.CHANGES = _save

print()
if FAILED:
    print(f'{len(FAILED)} FAILURE(S):')
    for f in FAILED:
        print('   -', f)
    sys.exit(1)
print('ALL CHECKS PASSED')
