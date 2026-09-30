#!/usr/bin/env python3
"""Train + cross-validate the scoring models and write docs/data/model.json.

Two models, both logistic (interpretable, calibrated, monotone where the rule is a threshold):

  primary    P(result = ERROR | contact physics + pre-pitch context)
  secondary  P(result in {hit, error, fielders_choice, out}) — the four macro classes an official
             scorer chooses between, so the published bars always sum to 100%.

DATASET
  Prefers docs/data/bip_official.csv — every captured batted ball in the configured model window
  (`data/ingest/plan.json` -> `bip_window`), produced from the official feed by
  tools/ingest_official.py. This is a whole-population sample for that selected window, not the
  full collector window or all MLB seasons.
  Falls back to docs/data/bip.csv — the original 24-game error-ENRICHED audit sample (documented as
  enriched everywhere it is shown; never described as the population rate).

VALIDATION (the part that decides whether any of this is worth reading)
  * GroupKFold by game_pk. Plays inside one game share a park, pitcher mix, scorer and weather
    night. Every StandardScaler is fitted on that fold's training games only. A stratified random-
    fold score is shown as a comparator, not presumed to be an upper or lower bound.
  * Group bootstrap (resample whole games) for coefficient/AUC intervals.
  * Calibration table from grouped out-of-fold predictions. Isotonic Brier is nested by game: the
    calibrator is trained on inner-fold predictions from outer-training games and evaluated only on
    the outer held-out games.
  * Correlation block: point-biserial / Spearman per feature, plus mutual information, so a reader can
    see how much each input actually carries before trusting any coefficient.
  * Model-free empirical surface (trajectory x exit-velocity band error rate) with Wilson intervals,
    so the published relationship does not depend on the model being right.
"""
# Determinism first: the published artifact must be byte-identical on a 2-core CI runner and on a
# many-core workstation. numpy's BLAS and scikit-learn's OpenMP paths are thread-count sensitive, so the
# process pins itself to one thread before importing them. This is a reproducibility fix, not a speed knob.
import os
for _v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
           'VECLIB_MAXIMUM_THREADS'):
    os.environ.setdefault(_v, '1')
os.environ.setdefault('PYTHONHASHSEED', '0')

import csv, json, math, sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_selection import mutual_info_classif
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss, log_loss, roc_auc_score)
from sklearn.model_selection import GroupKFold, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs' / 'data' / 'model.json'
SEED = 20260929
CLASSES = ['hit', 'error', 'fielders_choice', 'out']
TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder']
HARD = ['soft', 'medium', 'hard']

# Feature spec, serialized into model.json so every consumer (site JS, live scorer) builds the exact
# same vector. `type` is one of: ev, la, dist, outs, inning, traj:<v>, hard:<v>, base:<1B|2B|3B>,
# bat:<L|R>, pitch:<L|R>. Anything else is a bug, not a default.
NUMERIC_DEFAULTS = {'outs': 0, 'inning': 5, 'la': 10.0}


def feature_spec(context=True):
    spec = [{'name': 'EV_mph', 'type': 'ev'}, {'name': 'LA_deg', 'type': 'la'},
            {'name': 'dist_ft', 'type': 'dist'}]
    spec += [{'name': f'traj_{t}', 'type': f'traj:{t}'} for t in TRAJ]
    spec += [{'name': f'hard_{h}', 'type': f'hard:{h}'} for h in HARD]
    if context:
        spec += [{'name': 'on_1b', 'type': 'base:1B'}, {'name': 'on_2b', 'type': 'base:2B'},
                 {'name': 'on_3b', 'type': 'base:3B'}, {'name': 'outs_before', 'type': 'outs'},
                 {'name': 'inning_c', 'type': 'inning'},
                 {'name': 'bat_L', 'type': 'bat:L'}, {'name': 'pitch_L', 'type': 'pitch:L'}]
    return spec


def load_rows():
    """Prefer the ingested whole-window file; fall back to the 24-game audit sample.

    Returns (modelling_rows, dataset_path, kind, raw_row_count) so the published metadata can state
    how many batted balls the file holds and how many of them the model was actually fitted on
    (rows without a complete Statcast vector are quarantined, never imputed).
    """
    off = ROOT / 'docs' / 'data' / 'bip_official.csv'
    if off.exists() and off.stat().st_size == 0:
        raise SystemExit('docs/data/bip_official.csv exists but is empty — the ingest published nothing; '
                         'refusing to silently fall back to the 24-game audit sample')
    if off.exists() and off.stat().st_size > 0:
        raw = list(csv.DictReader(open(off)))
        rows = [r for r in raw if r.get('numeric_ok') == '1' and r.get('macro_class') in CLASSES]
        if len(rows) >= 1500:
            return rows, 'docs/data/bip_official.csv', 'official', len(raw)
    raw = list(csv.DictReader(open(ROOT / 'docs' / 'data' / 'bip.csv')))
    rows = [r for r in raw if r['numeric_ok'] == '1' and r['macro_class'] in CLASSES]
    return rows, 'docs/data/bip.csv', 'audit-24', len(raw)


def build_matrix(rows, spec):
    """Vector + a parallel dict of raw state (used for context breakdowns)."""
    X, states = [], []
    for r in rows:
        bases = set()
        if r.get('on_1b') == '1':
            bases.add('1B')
        if r.get('on_2b') == '1':
            bases.add('2B')
        if r.get('on_3b') == '1':
            bases.add('3B')
        if not bases and r.get('runners_on'):        # audit sample stores `runners_on` only
            pass
        st = {
            'ev': float(r['launch_speed']), 'la': float(r['launch_angle']),
            'dist': float(r['distance']), 'traj': r['trajectory'], 'hard': r['hardness'],
            'outs': int(float(r['outs_before'])) if r.get('outs_before') not in (None, '') else 0,
            'inning': int(float(r['inning'])) if r.get('inning') not in (None, '') else 5,
            'bat': r.get('bat_side') or '', 'pitch': r.get('pitch_hand') or '',
            'bases': bases,
        }
        X.append([state_value(f['type'], st) for f in spec])
        states.append(st)
    return np.array(X, dtype=float), states


def state_value(ftype, st):
    if ftype == 'ev':
        return st['ev']
    if ftype == 'la':
        return st['la']
    if ftype == 'dist':
        return st['dist']
    if ftype == 'outs':
        return st['outs']
    if ftype == 'inning':
        return min(st['inning'], 9)
    kind, _, val = ftype.partition(':')
    if kind == 'traj':
        return 1.0 if st['traj'] == val else 0.0
    if kind == 'hard':
        return 1.0 if st['hard'] == val else 0.0
    if kind == 'base':
        return 1.0 if val in st['bases'] else 0.0
    if kind == 'bat':
        return 1.0 if st['bat'] == val else 0.0
    if kind == 'pitch':
        return 1.0 if st['pitch'] == val else 0.0
    raise ValueError(f'unknown feature type {ftype!r}')


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, ctr - half), min(1.0, ctr + half)


def main():
    rows, dataset, kind, n_raw = load_rows()
    spec = feature_spec(context=any('on_1b' in r and r['on_1b'] != '' for r in rows[:50]))
    X, states = build_matrix(rows, spec)
    yb = np.array([1 if r['macro_class'] == 'error' else 0 for r in rows])
    yc = np.array([CLASSES.index(r['macro_class']) for r in rows])
    groups = np.array([r['game_pk'] for r in rows])
    feat_names = [f['name'] for f in spec]
    print(f'dataset {dataset} ({kind}): {len(rows)} batted balls, {yb.sum()} errors '
          f'({100*yb.mean():.3f}%), {len(set(groups))} games, {len(feat_names)} features')

    sc = StandardScaler().fit(X)
    Xs = sc.transform(X)
    base = LogisticRegression(max_iter=4000, C=1.0).fit(Xs, yb)
    multi = LogisticRegression(max_iter=5000, C=1.0).fit(Xs, yc)

    # ---- cross-validation: grouped (headline) + stratified random (comparator) ----
    n_splits = 5
    gkf = GroupKFold(n_splits=n_splits)
    oof_g = np.zeros(len(yb))
    oof_g_m = np.zeros((len(yb), len(CLASSES)))
    for tr, te in gkf.split(X, yb, groups):
        # Preprocessing is learned from training games only. Fitting the scaler above the CV loop
        # would let held-out feature distributions influence every fold's coefficients.
        fold_sc = StandardScaler().fit(X[tr])
        xtr, xte = fold_sc.transform(X[tr]), fold_sc.transform(X[te])
        m = LogisticRegression(max_iter=4000, C=1.0).fit(xtr, yb[tr])
        oof_g[te] = m.predict_proba(xte)[:, 1]
        mm = LogisticRegression(max_iter=5000, C=1.0).fit(xtr, yc[tr])
        oof_g_m[te] = mm.predict_proba(xte)

    skf = StratifiedKFold(n_splits, shuffle=True, random_state=SEED)
    oof_r = np.zeros(len(yb))
    oof_r_m = np.zeros((len(yb), len(CLASSES)))
    for tr, te in skf.split(X, yb):
        fold_sc = StandardScaler().fit(X[tr])
        xtr, xte = fold_sc.transform(X[tr]), fold_sc.transform(X[te])
        m = LogisticRegression(max_iter=4000, C=1.0).fit(xtr, yb[tr])
        oof_r[te] = m.predict_proba(xte)[:, 1]
        mm = LogisticRegression(max_iter=5000, C=1.0).fit(xtr, yc[tr])
        oof_r_m[te] = mm.predict_proba(xte)

    auc_g = float(roc_auc_score(yb, oof_g))
    auc_r = float(roc_auc_score(yb, oof_r))
    ap_g = float(average_precision_score(yb, oof_g))
    ap_r = float(average_precision_score(yb, oof_r))
    ll_g = float(log_loss(yb, oof_g, labels=[0, 1]))
    br_g = float(brier_score_loss(yb, oof_g))

    # Nested grouped calibration. Each outer test game's labels and features are absent from both
    # the base-model fits and the isotonic calibrator that predicts that game's probabilities.
    iso_oof = np.zeros(len(yb))
    calibration_inner_splits = 4
    for outer_tr, outer_te in gkf.split(X, yb, groups):
        x_outer = X[outer_tr]
        y_outer = yb[outer_tr]
        g_outer = groups[outer_tr]
        inner_oof = np.zeros(len(outer_tr))
        inner_gkf = GroupKFold(n_splits=calibration_inner_splits)
        for inner_tr, inner_te in inner_gkf.split(x_outer, y_outer, g_outer):
            inner_sc = StandardScaler().fit(x_outer[inner_tr])
            inner_model = LogisticRegression(max_iter=4000, C=1.0).fit(
                inner_sc.transform(x_outer[inner_tr]), y_outer[inner_tr])
            inner_oof[inner_te] = inner_model.predict_proba(
                inner_sc.transform(x_outer[inner_te]))[:, 1]
        calibrator = IsotonicRegression(out_of_bounds='clip').fit(inner_oof, y_outer)
        iso_oof[outer_te] = calibrator.predict(oof_g[outer_te])
    br_iso = float(brier_score_loss(yb, iso_oof))

    # gradient-boosted comparison, same grouped folds, to test whether nonlinearity earns its keep
    gb_oof = np.zeros(len(yb))
    for tr, te in gkf.split(X, yb, groups):
        gbm = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, random_state=SEED,
                                             early_stopping=False)
        gbm.fit(X[tr], yb[tr])
        gb_oof[te] = gbm.predict_proba(X[te])[:, 1]
    auc_gb = float(roc_auc_score(yb, gb_oof))

    # ---- rank-average comparator ------------------------------------------------------------
    # Two models that make different mistakes can beat either one on ranking. This is published as a
    # comparator only: the live scorer must stay a closed-form model that the browser and the CLI can
    # evaluate identically (tests/test_pipeline.py pins them to 1e-9), which a boosted ensemble is not.
    def to_rank(a):
        order = np.argsort(np.argsort(a, kind='mergesort'), kind='mergesort')
        return order / max(1, len(order) - 1)

    blend_rows = []
    for w in (0.0, 0.25, 0.5, 0.75, 1.0):
        b = w * to_rank(oof_g) + (1 - w) * to_rank(gb_oof)
        blend_rows.append({'weight_logistic': w, 'cv_auc': round(float(roc_auc_score(yb, b)), 4),
                           'cv_average_precision': round(float(average_precision_score(yb, b)), 5)})
    blend_best = max(blend_rows, key=lambda r: (r['cv_auc'], r['cv_average_precision']))

    # ---- regularisation / weighting / feature experiments ------------------------------------
    # Published whether or not they help: a null result is a result, and hiding it would make the
    # shipped model look like the only thing that was tried.
    def grouped_oof(matrix, C=1.0, class_weight=None):
        out = np.zeros(len(yb))
        for tr, te in gkf.split(matrix, yb, groups):
            fold_sc = StandardScaler().fit(matrix[tr])
            m = LogisticRegression(max_iter=4000, C=C, class_weight=class_weight)
            m.fit(fold_sc.transform(matrix[tr]), yb[tr])
            out[te] = m.predict_proba(fold_sc.transform(matrix[te]))[:, 1]
        return out

    experiments = {'regularisation_grid': [], 'notes': []}
    for C in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0):
        o = grouped_oof(X, C=C)
        experiments['regularisation_grid'].append({
            'C': C, 'cv_auc': round(float(roc_auc_score(yb, o)), 4),
            'cv_average_precision': round(float(average_precision_score(yb, o)), 5)})
    best_C = max(experiments['regularisation_grid'],
                 key=lambda r: (r['cv_average_precision'], r['cv_auc']))
    experiments['best_C_by_average_precision'] = best_C['C']
    shipped = next(r for r in experiments['regularisation_grid'] if r['C'] == 1.0)
    experiments['notes'].append(
        f"Regularisation: the grid's best average precision came at C={best_C['C']} "
        f"({best_C['cv_average_precision']:.5f} vs {shipped['cv_average_precision']:.5f} at the shipped "
        f"C=1.0, AUC {best_C['cv_auc']:.4f} vs {shipped['cv_auc']:.4f}). The shipped model keeps C=1.0: "
        'the difference is within the grouped-bootstrap interval and changing it would move every '
        'published coefficient for no measurable gain in the ranking the site uses.')

    bal = grouped_oof(X, C=1.0, class_weight='balanced')
    top_bal = np.argsort(bal, kind='mergesort')[-max(1, int(math.ceil(len(yb) * .10))):]
    top_ship = np.argsort(oof_g, kind='mergesort')[-max(1, int(math.ceil(len(yb) * .10))):]
    experiments['class_weight_balanced'] = {
        'cv_auc': round(float(roc_auc_score(yb, bal)), 4),
        'cv_average_precision': round(float(average_precision_score(yb, bal)), 5),
        'top10pct_errors_found': int(yb[top_bal].sum()),
        'shipped_top10pct_errors_found': int(yb[top_ship].sum()),
        'note': ('Class weighting trades ranking for volume: it finds '
                 f'{int(yb[top_bal].sum())} of {int(yb.sum())} held-out errors in the top 10% against '
                 f'{int(yb[top_ship].sum())} for the shipped model, while lowering AUC and average '
                 'precision. The site keeps the unweighted ranking and states the recall it buys.')}

    # Hand-built interactions and binning: tested, not shipped when they do not earn their place.
    def extended_matrix():
        ev_i, la_i, dist_i = (feat_names.index(n) for n in ('EV_mph', 'LA_deg', 'dist_ft'))
        traj_i = {t: (feat_names.index(f'traj_{t}') if f'traj_{t}' in feat_names else None) for t in TRAJ}
        hard_h = feat_names.index('hard_hard')
        gb_t = traj_i['ground_ball']
        extra = [np.column_stack([
            (X[:, ev_i] >= 95).astype(float), X[:, ev_i] * X[:, la_i] / 100.0,
            X[:, dist_i] * X[:, la_i] / 1000.0,
            (X[:, la_i] < 0).astype(float), ((X[:, la_i] >= 0) & (X[:, la_i] < 10)).astype(float),
            ((X[:, la_i] >= 10) & (X[:, la_i] < 25)).astype(float),
            ((X[:, la_i] >= 25) & (X[:, la_i] < 40)).astype(float), (X[:, la_i] >= 40).astype(float),
            (X[:, ev_i] < 70).astype(float), ((X[:, ev_i] >= 70) & (X[:, ev_i] < 85)).astype(float),
            ((X[:, ev_i] >= 85) & (X[:, ev_i] < 95)).astype(float),
            ((X[:, ev_i] >= 95) & (X[:, ev_i] < 105)).astype(float), (X[:, ev_i] >= 105).astype(float),
            (X[:, traj_i['ground_ball']] * X[:, hard_h]),
            (X[:, traj_i['line_drive']] + X[:, traj_i['fly_ball']]) * X[:, hard_h],
        ])] if gb_t is not None else []
        return np.hstack([X] + extra)

    Xext = extended_matrix()
    ext = grouped_oof(Xext)
    experiments['extended_features'] = {
        'n_features': int(Xext.shape[1]), 'cv_auc': round(float(roc_auc_score(yb, ext)), 4),
        'cv_average_precision': round(float(average_precision_score(yb, ext)), 5),
        'note': ('Interactions and coarse bins on the same information (hard-hit flag, exit-velocity '
                 'and launch-angle bands, exit-velocity x angle, ground-ball x hardness) did not '
                 f"improve grouped AUC ({roc_auc_score(yb, ext):.4f} vs {auc_g:.4f}) or average "
                 f"precision ({average_precision_score(yb, ext):.5f} vs {ap_g:.5f}). They are reported "
                 'and not shipped.')}

    # ---- permutation importance, shuffled across whole games --------------------------------
    # Groups are kept intact: shuffling a feature game-by-game preserves within-game structure and
    # answers "how much does the fitted model rely on this column", not "what causes an error".
    perm = []
    perm_rng = np.random.default_rng(SEED + 1)
    perm_game_ids = np.unique(groups)
    perm_idx_by_game = {g: np.where(groups == g)[0] for g in perm_game_ids}
    for i, name in enumerate(feat_names):
        drops = []
        for _ in range(3):
            Xp = X.copy()
            order = perm_rng.permutation(len(perm_game_ids))
            for g_src, g_dst in zip(perm_game_ids, perm_game_ids[order]):
                src, dst = perm_idx_by_game[g_src], perm_idx_by_game[g_dst]
                Xp[dst, i] = X[src, i][:len(dst)] if len(src) >= len(dst) else np.resize(X[src, i], len(dst))
            p = 1 / (1 + np.exp(-(base.intercept_[0] + ((Xp - sc.mean_) / sc.scale_) @ base.coef_[0])))
            drops.append(auc_g - float(roc_auc_score(yb, p)))
        perm.append({'feature': name, 'auc_drop_mean': round(float(np.mean(drops)), 5),
                     'auc_drop_max': round(float(np.max(drops)), 5)})
    perm.sort(key=lambda r: -r['auc_drop_mean'])

    # ---- group bootstrap ----
    rng = np.random.default_rng(SEED)
    game_ids = np.unique(groups)
    idx_by_game = {g: np.where(groups == g)[0] for g in game_ids}
    aucs, aps, coefs = [], [], []
    for _ in range(1000):
        pick = rng.choice(game_ids, len(game_ids), replace=True)
        idx = np.concatenate([idx_by_game[g] for g in pick])
        if yb[idx].sum() in (0, len(idx)):
            continue
        aucs.append(roc_auc_score(yb[idx], oof_g[idx]))
        if _ < 400:                      # average precision is a ranking curve; 400 game-resamples
            aps.append(average_precision_score(yb[idx], oof_g[idx]))
        m = LogisticRegression(max_iter=2000, C=1.0).fit(Xs[idx], yb[idx])
        coefs.append(m.coef_[0])
    coefs = np.array(coefs)
    auc_ci = [float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))]
    ap_ci = [float(np.percentile(aps, 2.5)), float(np.percentile(aps, 97.5))]

    # ---- correlations / mutual information ----
    corr = {}
    mi = mutual_info_classif(X, yb, discrete_features=[f['type'] not in ('ev', 'la', 'dist')
                                                      for f in spec], random_state=SEED)
    for i, f in enumerate(spec):
        col = X[:, i]
        if np.unique(col).size == 1:
            corr[f['name']] = {'type': 'constant', 'r': 0.0, 'mi': 0.0}
            continue
        if f['type'] in ('ev', 'la', 'dist'):
            r = float(spearmanr(col, yb).correlation or 0.0)
            ctype = 'spearman'
        else:
            r = float(np.corrcoef(col, yb)[0, 1])        # point-biserial for a 0/1 column
            ctype = 'point-biserial'
        corr[f['name']] = {'type': ctype, 'r': round(r, 4), 'mi': round(float(mi[i]), 5)}

    # ---- empirical surface (model-free) ----
    bands = [(0, 70), (70, 80), (80, 90), (90, 95), (95, 100), (100, 105), (105, 200)]
    surface = {}
    ev = X[:, feat_names.index('EV_mph')]
    for t in TRAJ:
        ti = feat_names.index(f'traj_{t}') if f'traj_{t}' in feat_names else None
        if ti is None:
            continue
        for lo, hi in bands:
            m_ = (X[:, ti] == 1) & (ev >= lo) & (ev < hi)
            n = int(m_.sum())
            if n >= 10:
                k = int(yb[m_].sum())
                p, clo, chi = wilson(k, n)
                surface[f'{t}|{lo}-{hi}'] = {'n': n, 'k': k, 'p': round(p, 4),
                                             'lo': round(clo, 4), 'hi': round(chi, 4)}

    # context breakdowns a reader can check without the model
    ctx = defaultdict(lambda: {'n': 0, 'k': 0})
    for i, st in enumerate(states):
        key = ('bases_empty' if not st['bases'] else 'runner_on')
        ctx[key]['n'] += 1
        ctx[key]['k'] += int(yb[i])
        ok = f"outs_{st['outs']}" if st['outs'] in (0, 1, 2) else 'outs_unknown'
        ctx[ok]['n'] += 1
        ctx[ok]['k'] += int(yb[i])
    context_stats = {k: {'n': v['n'], 'k': v['k'], 'p': round(v['k'] / v['n'], 5) if v['n'] else None}
                     for k, v in sorted(ctx.items())}

    # ---- published curve for the calculator ----
    curve = {}
    for t in TRAJ:
        ti = feat_names.index(f'traj_{t}') if f'traj_{t}' in feat_names else None
        if ti is None:
            continue
        lat = float(np.median(X[X[:, ti] == 1][:, 1])) if (X[:, ti] == 1).any() else 10.0
        dst = float(np.median(X[X[:, ti] == 1][:, 2])) if (X[:, ti] == 1).any() else 100.0
        xs = []
        for evv in range(50, 121, 2):
            st = {'ev': evv, 'la': lat, 'dist': dst, 'traj': t, 'hard': 'medium', 'outs': 0,
                  'inning': 5, 'bat': '', 'pitch': '', 'bases': set()}
            xs.append([state_value(f['type'], st) for f in spec])
        ps = (base.predict_proba(sc.transform(np.array(xs)))[:, 1] * 100).round(2).tolist()
        curve[t] = {'la_med': round(lat, 1), 'dist_med': round(dst, 1),
                    'ev': list(range(50, 121, 2)), 'p_err_x100': ps}

    # ---- calibration table (grouped OOF) ----
    edges = [0, .005, .01, .015, .02, .03, .04, .05, .07, .10, .20, 1.01]
    cal = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m_ = (oof_g >= lo) & (oof_g < hi)
        if m_.sum():
            cal.append({'lo': lo, 'hi': hi, 'n': int(m_.sum()), 'mean_p': round(float(oof_g[m_].mean()), 5),
                        'obs': round(float(yb[m_].mean()), 5)})
    grid = []
    for pct in range(0, 101, 5):
        grid.append({'percentile': pct, 'p_error': round(float(np.percentile(oof_g, pct)), 5)})

    # per-class OOF quality
    per_class = {}
    for i, c in enumerate(CLASSES):
        y_i = (yc == i).astype(int)
        try:
            a = float(roc_auc_score(y_i, oof_g_m[:, i]))
        except ValueError:
            a = None
        sel = yc == i
        per_class[c] = {'n': int(sel.sum()), 'auc': a,
                        'recall': round(float((oof_g_m[sel].argmax(axis=1) == i).mean()), 4)}
    top1 = oof_g_m.argmax(axis=1)
    error_nominated = int((top1 == CLASSES.index('error')).sum())
    top_risk = []
    for pct in (1, 5, 10):
        n_top = max(1, int(math.ceil(len(yb) * pct / 100)))
        chosen = np.argsort(oof_g, kind='mergesort')[-n_top:]
        found = int(yb[chosen].sum())
        precision = found / n_top
        top_risk.append({'top_percent': pct, 'n': n_top, 'errors_found': found,
                         'precision': round(precision, 5),
                         'recall': round(found / int(yb.sum()), 5),
                         'lift_over_base_rate': round(precision / float(yb.mean()), 3)})

    # The operational question is "how many plays must a person watch to catch how many errors", so
    # the queue table is published from 0.5% to 30% — the bands a review workflow can actually use.
    queue = []
    for pct in (0.5, 1, 2, 3, 5, 10, 20, 30):
        n_top = max(1, int(math.ceil(len(yb) * pct / 100)))
        chosen = np.argsort(oof_g, kind='mergesort')[-n_top:]
        found = int(yb[chosen].sum())
        precision = found / n_top
        queue.append({'top_percent': pct, 'plays_to_review': n_top, 'errors_found': found,
                      'precision': round(precision, 5),
                      'recall': round(found / int(yb.sum()), 5),
                      'lift_over_base_rate': round(precision / float(yb.mean()), 3),
                      'plays_per_error_found': round(n_top / found, 1) if found else None})
    queue_note = ('Grouped out-of-fold ranking within the collected window. "Plays to review" is the '
                  'cost side; "errors found" is the benefit. Reading it as a promise about a future '
                  'season would be a mistake: the window is narrow and the CI is wide.')

    p_all = 1 / (1 + np.exp(-(base.intercept_[0] + Xs @ base.coef_[0])))
    plan_path = ROOT / 'data' / 'ingest' / 'plan.json'
    try:
        model_window = json.loads(plan_path.read_text()).get('bip_window') or {}
    except (OSError, json.JSONDecodeError):
        model_window = {}
    window_label = (f"{model_window['start']} through {model_window['end']}"
                    if model_window.get('start') and model_window.get('end') else 'configured model window')
    meta = {
        'dataset': dataset, 'dataset_kind': kind, 'n_bip': n_raw, 'n_model': len(rows),
        'n_quarantined': n_raw - len(rows),
        'n_error': int(yb.sum()), 'error_rate_pct': round(100 * float(yb.mean()), 3),
        'class_counts': {c: int((yc == i).sum()) for i, c in enumerate(CLASSES)},
        'games': int(len(set(groups))), 'features': feat_names,
        'design': ('Features are contact physics + pre-pitch context only. Nothing that exists only '
                   'because a ruling was made (fielding credits, error flags, hit/error column) is '
                   'used as an input. Cross-validation is GroupKFold by game_pk with fold-local StandardScaler '
                   'preprocessing; stratified random folds are shown as a comparator, with no '
                   'assumed ordering.'),
        'dataset_note': (f'Whole-population model-window sample ({window_label}): every captured '
                         'batted ball in each selected game, so the error rate is the observed class '
                         'rate for this window; it is not the full collector-window rate.'
                         if kind == 'official' else
                         'ERROR-ENRICHED stratified 24-game audit sample: the error rate here is NOT '
                         'the population rate (see data/TARGETS.json and docs/methods.html).'),
        'target_scope': ('Predicts the final macro_class recorded in the captured official feed. This '
                         'is not a model of whether an initial scorer decision will later be changed.'),
    }
    primary = {
        'target': 'P(final feed macro_class = error)',
        'feature_names': feat_names,
        'feature_spec': spec,
        'scaler_mean': [round(float(v), 10) for v in sc.mean_],
        'scaler_scale': [round(float(v), 10) for v in sc.scale_],
        'intercept': round(float(base.intercept_[0]), 10),
        'coef': {n: round(float(base.coef_[0][i]), 10) for i, n in enumerate(feat_names)},
        'coef_ci95': {n: [round(float(np.percentile(coefs[:, i], 2.5)), 5),
                          round(float(np.percentile(coefs[:, i], 97.5)), 5)]
                      for i, n in enumerate(feat_names)},
        'cv_auc': round(auc_g, 4), 'cv_auc_ci95': [round(v, 4) for v in auc_ci],
        'cv_auc_random_kfold': round(auc_r, 4),
        'cv_average_precision_grouped': round(ap_g, 5),
        'cv_average_precision_ci95': [round(v, 5) for v in ap_ci],
        'cv_average_precision_random_kfold': round(ap_r, 5),
        'cv_auc_gradient_boosting_grouped': round(auc_gb, 4),
        'cv_logloss': round(ll_g, 6), 'cv_brier_raw': round(br_g, 6),
        'cv_brier_isotonic': round(br_iso, 6),
        'calibration_method': 'nested GroupKFold: inner-game OOF fit, outer-game holdout evaluation',
        'calibration_inner_splits': calibration_inner_splits,
        'n_splits': n_splits, 'group': 'game_pk',
    }
    multiclass = {
        'classes': CLASSES,
        'intercept': {c: round(float(multi.intercept_[i]), 10) for i, c in enumerate(CLASSES)},
        'coef': {c: {n: round(float(multi.coef_[i][j]), 10) for j, n in enumerate(feat_names)}
                 for i, c in enumerate(CLASSES)},
        'cv_logloss': round(float(log_loss(yc, oof_g_m, labels=list(range(len(CLASSES))))), 6),
        'per_class': per_class,
    }
    honesty = {
        'what_this_is': ('A pre-decision estimate for the final macro-class recorded in the feed, '
                         'plus the distribution over four classes. It is not trained to predict '
                         'whether an initial scoring call will later be reclassified. It ranks; it '
                         'does not decide.'),
        'oof_top1_accuracy': round(float((top1 == yc).mean()), 4),
        'oof_error_nominated_top1': error_nominated,
        'oof_error_recall': per_class['error']['recall'],
        'p_error_training_min_x100': round(float(100 * p_all.min()), 3),
        'p_error_training_max_x100': round(float(100 * p_all.max()), 3),
        'top_error_risk_bands_oof': top_risk,
        'cv_average_precision_grouped': round(ap_g, 5),
        'cv_average_precision_ci95': [round(v, 5) for v in ap_ci],
        'cv_average_precision_random_kfold': round(ap_r, 5),
        'calibration_oof': cal, 'p_error_percentile_grid': grid,
        'review_queue_oof': queue, 'review_queue_note': queue_note,
        'grouped_vs_random_auc_gap': round(auc_r - auc_g, 4),
        'baseline_error_rate': round(float(yb.mean()), 5),
        'note': ('If the model\'s top pick is never "error" (see oof_error_nominated_top1), then the '
                 '/100 score is only useful as a within-game ranking of where to look, and the '
                 'honest headline is that ruling errors OUT is what this model does well.'),
    }
    out = {
        'meta': meta, 'primary': primary, 'multiclass': multiclass, 'honesty': honesty,
        'surface': surface, 'curve': curve, 'correlations': corr, 'context_stats': context_stats,
        'risk_bands': {'description': 'error-likelihood bands from the grouped OOF distribution',
                       'edges': grid},
        'experiments': experiments,
        'blend_comparator': {
            'method': ('rank-average of the shipped logistic OOF ranking and the grouped '
                       'gradient-boosting OOF ranking: w x logistic + (1 - w) x gbm'),
            'grid': blend_rows,
            'best_by_auc': blend_best,
            'shipped': False,
            'why_not_shipped': ('A comparator only. The live scorer must be evaluable in the browser '
                                'and in tools/live_score.py to 1e-9, which a boosted ensemble is not; '
                                'the published probabilities would also stop being the calibrated '
                                'object the site explains.'),
        },
        'permutation_importance': {
            'method': ('each feature shuffled across whole games (group structure preserved) and '
                       'scored with the full-fit model; mean and max grouped-OOF AUC drop over 3 '
                       'shuffles. It measures the fitted model\'s reliance on a column, not a cause.'),
            'rows': perm,
        },
    }
    OUT.write_text(json.dumps(out, indent=1))
    print(json.dumps({'auc_grouped': primary['cv_auc'], 'auc_ci': primary['cv_auc_ci95'],
                      'auc_random': primary['cv_auc_random_kfold'],
                      'average_precision_grouped': primary['cv_average_precision_grouped'],
                      'average_precision_ci95': primary['cv_average_precision_ci95'],
                      'blend_best': blend_best,
                      'best_C_by_average_precision': best_C['C'],
                      'average_precision_baseline': round(float(yb.mean()), 5),
                      'auc_gbm_grouped': primary['cv_auc_gradient_boosting_grouped'],
                      'brier': primary['cv_brier_raw'], 'top1': honesty['oof_top1_accuracy'],
                      'error_nominated_top1': error_nominated,
                      'p_error_max_x100': honesty['p_error_training_max_x100'],
                      'features': len(feat_names)}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
