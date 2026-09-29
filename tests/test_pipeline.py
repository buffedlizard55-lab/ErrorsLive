#!/usr/bin/env python3
"""Audit suite: every number shipped to the public pages must reconcile with repo data.

Run from repo root:  python3 tests/test_pipeline.py
Exit 0 = all checks pass.  Own the Outcome: if a page claims a number, this suite
must be able to recompute that same number from raw artifacts.
"""
import csv, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'data' / 'raw'
FAILED = []

def check(name, ok, detail=''):
    print(('  ok ' if ok else 'FAIL ') + name + (f' — {detail}' if detail and not ok else ''))
    if not ok:
        FAILED.append(name)

def load_json(p):
    with open(p) as f: return json.load(f)

def load_csv(p):
    with open(p, newline='') as f: return list(csv.DictReader(f))

print('== A. raw archives ==')
raws = sorted(RAW.glob('8*.json'))
check('24 raw game archives', len(raws) == 24, f'found {len(raws)}')
targets = load_json(ROOT / 'data' / 'TARGETS.json')
chosen = set(targets['A'] + targets['B'] + targets['C'])
games = {r['game_pk']: r for r in load_csv(ROOT / 'docs/data/games.csv')}
verif, healed = 0, list(RAW.glob('*.integrity.txt'))
for p in raws:
    g = load_json(p)
    pk = p.stem.split('.')[0]
    plays = g['liveData']['plays']['allPlays']
    rs = plays[-1]['result']
    m = games.get(pk)
    if m and int(m['rA']) == rs['awayScore'] and int(m['rH']) == rs['homeScore']:
        verif += 1
check('sample manifest == raw set', chosen == {int(p.stem.split('.')[0]) for p in raws})
check('every game final score == manifest ', verif == 24, f'{verif}/24')
check('integrity logs present for heals', len(healed) >= 4, f'{len(healed)}')

print('== B. dataset ==')
bip = load_csv(ROOT / 'docs/data/bip.csv')
dq = load_json(ROOT / 'data/data_quality.json')
check('bip rows == 1271', len(bip) == 1271, f'{len(bip)}')
check('errors labeled == 24', sum(1 for r in bip if r['macro_class'] == 'error') == 24)
check('data_quality matches csv', dq['totals']['bip'] == len(bip) and dq['totals']['errors'] == 24)
check('all 24 final scores verified in qa', len(dq['games']) == 24 and all(g['score_checked'] for g in dq['games'].values()))
check('missing-numeric quarantine == 12', dq['totals']['missing_numeric'] == 12)

print('== C. model.json internals ==')
M = load_json(ROOT / 'docs/data/model.json')
mm, pp = M['meta'], M['primary']
check('meta n', mm['n_bip'] == 1271 and mm['n_model'] == 1258 and mm['n_error'] == 24)
check('error rate 1.91', abs(mm['error_rate_pct'] - 1.91) < .01)
check('AUC 0.641', abs(pp['cv_auc'] - 0.641) < .001)
check('AUC CI [0.518,0.748]', abs(pp['cv_auc_ci95'][0] - 0.518) < .001 and abs(pp['cv_auc_ci95'][1] - 0.748) < .001)
check('Brier 0.0186', abs(pp['cv_brier_raw'] - 0.0186) < .0001)
check('ground_ball coef +0.692', abs(pp['coef']['traj_ground_ball'] - 0.692) < .001)
check('class mix', mm['class_counts'] == {'hit': 428, 'error': 24, 'fielders_choice': 10, 'out': 796})
check('design narrative is honest (enrichment stated, uniform-fraction fiction removed)',
      'ENRICHED' in mm['design'] and 'NOT' in mm['design'] and '37.75' in mm['design']
      and 'uniform 0.5 sampling fraction' not in mm['design'])
s = M['surface'].get('popup|70-80')
check('surface popup 70-80: n=27, k=1, p=3.7% (verifies errors rare on popups)',
      s and s['n'] == 27 and s['k'] == 1 and abs(s['p'] - 0.037) < .001, str(s))

print('== D. pages vs data consistency ==')
idx = (ROOT / 'docs/index.html').read_text()
mod = (ROOT / 'docs/model.html').read_text()
ovt = (ROOT / 'docs/overturned.html').read_text()
rul = (ROOT / 'docs/rules.html').read_text()
met = (ROOT / 'docs/methods.html').read_text()
check('index KPI ids', all(f'id="{i}"' in idx for i in ['kMeta', 'kAuc', 'kCoef']))
check('index copy: labels+filters', 'Labeled hits' in idx and 'Labeled FC' in idx and 'Labeled outs' in idx)
check('index filmroom chips + explainer', 'filmroom' in idx and 'No other ' in idx)
check('index lead anchor links', '· MVP →' in idx and '· Model →' in idx)
check('model KPI ids', all(f'id="{i}"' in mod for i in ['mAuc', 'mN']))
check('overturned KPI ids', all(f'id="{i}"' in ovt for i in ['kTot', 'kOv', 'kRr']))
css = (ROOT / 'docs/site.css').read_text()
check('overturned chip CSS + AMBER fence', 'class="chip"' in ovt and 'match check' in ovt and '.chip{' in css)
check('rules: official glossary links', 'mlb.com/glossary/standard-stats/error' in rul and
      'mlb.com/glossary/standard-stats/runs-batted-in' in rul)
check('rules: RBI-no-error quote present', 'does not receive an RBI when the run scores as a result of an error' in rul)
check('rules + methods cross-linked from index', 'rules.html' in idx and 'methods.html' in idx)
check('RBI text mentions rules', 'Rule 9.16' in idx or '9.16' in rul)

print('== E. NYDN ==')
ny = load_csv(ROOT / 'docs/data/overturned_calls.csv')
nq = load_json(ROOT / 'data/nydn_quality.json')
check('rows 6168', len(ny) == 6168, f'{len(ny)}')
check('overturned 2984', sum(r['overturned'] == '1' for r in ny) == 2984)
check('run_removed 227', sum(r['run_removed_heuristic'] == '1' for r in ny) == 227)
check('quality flags logged (bad time 1, short-row 3, no-video 57 = 61 total)',
      nq['flag_summary'] == {'bad_time_format': 1, 'short_row_comma_name?': 3, 'no_video_link': 57}
      and len(nq['flags']) == 61, json.dumps(nq.get('flag_summary')))
check('typos kept verbatim', any(r['date'] == '2014-04-31' for r in ny) and
      any(r['time_to_ruling'] == '1;21' for r in ny))
ho17 = [r for r in ny if r['date'] == '2017-10-17' and r['game'].startswith('HOU @ NYY')]
check('2017 ALCS G4 rows parsed (Judge overturned force play kept)',
      len(ho17) == 2 and ho17[0]['player'] == 'A. Judge' and ho17[0]['play_type'] == 'Force Play'
      and ho17[0]['result'] == 'Overturned' and ho17[0]['postseason'] == '1')
check('video links official-only (t.co/bit.ly/atmlb)', all(r['video'].startswith(('http://t.co/', 'https://t.co/', 'bit.ly/', 'http://bit.ly/', 'https://bit.ly/', 'atmlb.com', 'http://atmlb', 'https://atmlb')) for r in ny))

print()
if FAILED:
    print(f'{len(FAILED)} FAILURES'); sys.exit(1)
print('ALL CHECKS PASSED')
