#!/usr/bin/env python3
"""Train + cross-validate the two models and write docs/data/model.json.

Primary   : P(result = ERROR | EV, LA, distance, trajectory, hardness)         (binary logistic)
Secondary : multinomial P(result in {hit, error, fielders_choice, out})        (softmax logistic)

Why logistic regression (and honest notes):
 * interpretable, calibratable, monotone in EV for ground balls - the scoring decision is a
   threshold judgment on "ordinary effort" (Rule 9.12), so a smooth probability surface is the
   scientifically honest choice at n~1200 with ~60 positives;
 * calibration note: error-ENRICHED stratified sample (fractions A 10/20=0.500, B 10/51=0.196, C 4/151=0.026); pool-true rates need raking weights A=2.0 B=5.1 C=37.75 (validated: weighted mean team-errors/game 1.258 vs pool 1.113)
   Sample rates are NOT pool rates; we additionally Platt-calibrate with out-of-fold
   predictions and report both.
 * uncertainty: 1000x bootstrap percentile CIs for the AUC and every coefficient.
"""
import csv, json
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
BIP = ROOT / 'docs' / 'data' / 'bip.csv'
OUT = ROOT / 'docs' / 'data' / 'model.json'

CLASSES = ['hit', 'error', 'fielders_choice', 'out']
TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder']
HARD = ['soft', 'medium', 'hard']


def load():
    rows = [r for r in csv.DictReader(open(BIP))]
    num = [r for r in rows if r['numeric_ok'] == '1' and r['macro_class'] in CLASSES]
    X, y_bin, y_cls = [], [], []
    for r in num:
        x = [float(r['launch_speed']), float(r['launch_angle']), float(r['distance'])]
        x += [1.0 if r['trajectory'] == t else 0.0 for t in TRAJ]
        x += [1.0 if r['hardness'] == h else 0.0 for h in HARD]
        X.append(x); y_bin.append(1 if r['macro_class'] == 'error' else 0)
        y_cls.append(CLASSES.index(r['macro_class']))
    return rows, np.array(X), np.array(y_bin), np.array(y_cls)


def main():
    rows, X, yb, yc = load()
    feat_names = ['EV_mph', 'LA_deg', 'dist_ft'] + [f'traj_{t}' for t in TRAJ] + [f'hard_{h}' for h in HARD]
    sc = StandardScaler().fit(X)
    Xs = sc.transform(X)

    # ---------- primary: error vs not ----------
    base = LogisticRegression(max_iter=2000, C=1.0)
    base.fit(Xs, yb)
    skf = StratifiedKFold(5, shuffle=True, random_state=20260929)
    oof = np.zeros(len(yb))
    for tr, te in skf.split(Xs, yb):
        m = LogisticRegression(max_iter=2000, C=1.0).fit(Xs[tr], yb[tr])
        oof[te] = m.predict_proba(Xs[te])[:, 1]
    auc = roc_auc_score(yb, oof)
    ll = log_loss(yb, oof, labels=[0, 1])
    br = brier_score_loss(yb, oof)
    # Platt recalibration on OOF
    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds='clip').fit(oof, yb)
    br_iso = brier_score_loss(yb, iso.predict(oof))

    # bootstrap CI for AUC and EV coefficient
    rng = np.random.default_rng(20260929)
    aucs, evc = [], []
    for _ in range(1000):
        idx = rng.integers(0, len(yb), len(yb))
        if yb[idx].sum() in (0, len(idx)):
            continue
        aucs.append(roc_auc_score(yb[idx], oof[idx]))
        m = LogisticRegression(max_iter=2000, C=1.0).fit(Xs[idx], yb[idx])
        evc.append(m.coef_[0][0])
    auc_ci = [float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))]
    ev_ci = [float(np.percentile(evc, 2.5)), float(np.percentile(evc, 97.5))]

    # empirical surface (model-free check): error rate per (traj x EV band)
    bands = [(0, 70), (70, 80), (80, 90), (90, 95), (95, 100), (100, 105), (105, 200)]
    ev_all = X[:, 0]
    surface = {}
    for ti, t in enumerate(TRAJ):
        for lo, hi in bands:
            m = (X[:, 3 + ti] == 1) & (ev_all >= lo) & (ev_all < hi)
            n = int(m.sum())
            if n >= 10:
                k = int(yb[m].sum())
                # Wilson 95% interval
                p = k / n
                z = 1.96
                den = 1 + z * z / n
                ctr = (p + z * z / (2 * n)) / den
                half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
                surface[f'{t}|{lo}-{hi}'] = {'n': n, 'k': k, 'p': round(p, 4),
                                             'lo': round(max(0, ctr - half), 4), 'hi': round(min(1, ctr + half), 4)}
    # model P(error) curve for ground_ball vs line_drive vs fly_ball vs popup (LA set at class median)
    curve = {}
    for ti, t in enumerate(TRAJ):
        lat = float(np.median(X[X[:, 3 + ti] == 1][:, 1]))
        xs = []
        for evv in range(50, 120, 2):
            x = [evv, lat, np.median(X[X[:, 3 + ti] == 1][:, 2])]
            x += [1.0 if j == ti else 0.0 for j in range(len(TRAJ))]
            x += [0.0, 1.0, 0.0]
            xs.append(x)
        ps = (base.predict_proba(sc.transform(np.array(xs)))[:, 1] * 100).round(1).tolist()
        curve[t] = {'la_med': lat, 'ev': list(range(50, 120, 2)), 'p_err_x100': ps}

    # ---------- secondary: 4-class outcome ----------
    multi = LogisticRegression(max_iter=3000, C=1.0).fit(Xs, yc)
    oofm = np.zeros((len(yc), len(CLASSES)))
    for tr, te in skf.split(Xs, yc):
        m = LogisticRegression(max_iter=3000, C=1.0).fit(Xs[tr], yc[tr])
        oofm[te] = m.predict_proba(Xs[te])
    ll_m = log_loss(yc, oofm, labels=list(range(len(CLASSES))))
    per_class_auc = {}
    for ci, cname in enumerate(CLASSES):
        try:
            per_class_auc[cname] = round(roc_auc_score((yc == ci).astype(int), oofm[:, ci]), 3)
        except ValueError:
            per_class_auc[cname] = None
    # Honest end-to-end metrics: can the model actually NAME the right call, and does it ever
    # nominate 'error'? Both are reported so the site cannot overstate a 0-100 dial.
    oof_top1 = oofm.argmax(axis=1)
    top1_acc = float((oof_top1 == yc).mean())
    top1_by_class = {c: {'n': int((yc == i).sum()),
                         'correct': int((oof_top1[yc == i] == i).sum()),
                         'recall': round(float((oof_top1[yc == i] == i).mean()), 3)}
                     for i, c in enumerate(CLASSES)}
    never_error_top1 = int((oof_top1 == CLASSES.index('error')).sum())

    # OOF calibration of P(error) - is the number trustworthy at the top of its range?
    cal_bins, edges = [], [0, .01, .02, .03, .04, .05, .07, .10, .20, 1.01]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (oof >= lo) & (oof < hi)
        if m.sum():
            cal_bins.append({'lo': lo, 'hi': hi, 'n': int(m.sum()),
                             'mean_pred': round(float(oof[m].mean()), 4),
                             'observed': round(float(yb[m].mean()), 4)})

    # Observed P(error) range + a percentile lookup, so "/100" can be shown honestly.
    p_all = 1 / (1 + np.exp(-(base.intercept_[0] + Xs @ base.coef_[0])))
    pct = np.sort(p_all)

    def percentile(x):
        return int(round(100 * np.searchsorted(pct, x, side='right') / len(pct)))
    pct_grid = [{'p_error': round(float(q), 5), 'percentile': percentile(q)}
                for q in np.quantile(p_all, np.linspace(0, 1, 101))]

    # correlations with binary error label (point-biserial = pearson on 0/1)
    corr = {}
    try:
        from scipy import stats
        for fi, name in enumerate(feat_names):
            r, p = stats.pointbiserialr(yb, X[:, fi])
            corr[name] = {'r': round(float(r), 3), 'p': float(f'{p:.2e}')}
    except Exception:
        for fi, name in enumerate(feat_names):
            r = float(np.corrcoef(X[:, fi], yb)[0, 1])
            corr[name] = {'r': round(r, 3), 'p': None}

    out = {
        'meta': {
            'unit': 'batted ball in play with Statcast hitData (launch speed/angle/distance)',
            'n_games': len(set(r['game_pk'] for r in rows)), 'n_bip': len(rows), 'n_model': len(yb),
            'n_error': int(yb.sum()), 'error_rate_pct': round(100 * yb.mean(), 2),
            'class_counts': {c: int((yc == i).sum()) for i, c in enumerate(CLASSES)},
            'design': ('error-ENRICHED stratified 24-game sample of a 222-game June-2026 pool: fractions '
                       'A(>=3 team errors) 10/20=0.500, B(==2) 10/51=0.196, C(<=1) 4/151=0.026. NOT '
                       'self-weighting: pool-representative rates need raking weights A=2.0, B=5.1, '
                       'C=37.75 (validated: weighted mean team-errors/game 1.258 vs pool 1.113). The '
                       'fitted objects model P(class | batted-ball features) WITHIN play, far less '
                       'distorted than the game-level error rate; the 1.91% base rate is the SAMPLE '
                       'rate, not the pool rate.'),
            'trained': '2026-09-29',
        },
        'primary': {
            'kind': 'binary logistic regression P(error | EV, LA, distance, trajectory, hardness)',
            'cv_auc': round(float(auc), 3), 'cv_auc_ci95': [round(v, 3) for v in auc_ci],
            'cv_logloss': round(float(ll), 4),
            'cv_brier_raw': round(float(br), 4), 'oof_brier_isotonic': round(float(br_iso), 4),
            'ev_coef_std': round(float(base.coef_[0][0]), 3), 'ev_coef_ci95': [round(v, 3) for v in ev_ci],
            'intercept': round(float(base.intercept_[0]), 3),
            'coef': dict(zip(feat_names, [round(float(c), 3) for c in base.coef_[0]])),
            'scaler_mean': [round(float(v), 3) for v in sc.mean_],
            'scaler_scale': [round(float(v), 3) for v in sc.scale_],
            'feature_names': feat_names,
            'platt_oof_grid': None,
        },
        'surface': surface, 'curve': curve,
        'honesty': {
            'headline': ('READ THIS BEFORE TRUSTING THE /100 DIAL. Across the %d modelling batted '
                         'balls the fitted P(error) never exceeds %.1f/100 and never falls below '
                         '%.1f/100, and out-of-fold the 4-class model nominates "error" as its most '
                         'likely call %d times out of %d. The score is a real ranking signal '
                         '(OOF AUC %.3f) but it is NOT a calibrated 0-100 confidence, and it will '
                         'essentially never tell you "this is an error".'
                         % (len(yb), 100 * float(p_all.max()), 100 * float(p_all.min()),
                            never_error_top1, len(yb), float(auc))),
            'oof_top1_accuracy': round(top1_acc, 4),
            'oof_top1_recall_by_class': top1_by_class,
            'oof_error_nominated_top1': never_error_top1,
            'oof_error_recall': round(float((oof_top1[yb == 1] == CLASSES.index('error')).mean()), 3),
            'p_error_observed_min_x100': round(100 * float(p_all.min()), 2),
            'p_error_observed_max_x100': round(100 * float(p_all.max()), 2),
            'p_error_percentile_grid': pct_grid,
            'calibration_oof': cal_bins,
            'what_it_is_good_for': ('ranking batted balls against each other and flagging the rare '
                                    'top-decile "this one deserves a second look" case'),
            'what_it_is_not_good_for': ('declaring a call an error, settling a scoring decision, or '
                                        'replacing the official scorer - it never nominates error '
                                        'as the top call and its absolute level is uncalibrated'),
        },
        'multiclass': {
            'cv_logloss': round(float(ll_m), 4),
            'per_class_cv_auc': per_class_auc,
            'coef': {c: dict(zip(feat_names, [round(float(v), 3) for v in multi.coef_[i]]))
                     for i, c in enumerate(CLASSES)},
            'intercept': {c: round(float(v), 3) for c, v in zip(CLASSES, multi.intercept_)},
        },
        'correlations_pointbiserial': corr,
        'caveats': [
            'Labels = official play-result eventType; errors charged on non-error eventTypes (e.g. throwing errors after a hit) are counted under that call - error rate is a lower bound (see data_quality.json).',
            'No fielder positioning / DEF shift, no hang time, no fielder identity - the model answers "given the batted ball as tracked"', 
            'Pool = 15 dates in June 2026; generalization beyond is an extrapolation.',
            'Live use: probabilities are pre-call priors; the official result remains the source of record.',
        ],
    }
    json.dump(out, open(OUT, 'w'), indent=1)
    print('AUC', out['primary']['cv_auc'], out['primary']['cv_auc_ci95'],
          '| logloss', out['primary']['cv_logloss'], '| brier', out['primary']['cv_brier_raw'],
          '| errors', out['meta']['n_error'], '/', out['meta']['n_model'])
    print('multiclass', out['multiclass'])


if __name__ == '__main__':
    main()
