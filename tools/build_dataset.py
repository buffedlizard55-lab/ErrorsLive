#!/usr/bin/env python3
"""Build the modeling dataset from the verified per-game feed archives.

Input : data/raw/{gamePk}.json          (score-checked joined feeds, audit trail)
        data/verified_linescores.json    (OFFICIAL linescore re-verification, one row per game)
        optionally POOL_JSON (env)       (a full 222-game pool manifest, if you have one)
Output: docs/data/bip.csv                one row per batted-ball-in-play event with hitData
        docs/data/games.csv              one row per game
        data/data_quality.json           machine-readable audit

Audit rules encoded here:
 * Label of a play = the official scorer's own play-result eventType. Nothing is imputed.
 * A play carries at most one hitData event (verified; counted and flagged if not).
 * hitData numeric fields may be missing in the wild - the row is kept and flagged, never filled.
 * Each game's parsed final score is re-checked against the OFFICIAL linescore record
   (data/verified_linescores.json, re-fetched from statsapi.mlb.com - see that file's
   "how"/"result" keys for the audit trail). A mismatch is a hard error, not a warning.
 * Chunk-seam repairs from the original collection are recorded in data/raw/*.integrity.txt
   and surfaced per game as `healed`; they are never silently absorbed.
"""
import csv, json, os, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'data' / 'raw'
VERIFIED = ROOT / 'data' / 'verified_linescores.json'
OUT_SITE = ROOT / 'docs' / 'data'
OUT_DATA = ROOT / 'data'
POOL = Path(os.environ['POOL_JSON']) if os.environ.get('POOL_JSON') else None

HIT_TYPES = {'single', 'double', 'triple', 'home_run'}
FC_TYPES = {'fielders_choice', 'fielders_choice_out'}
OUT_TYPES = {'field_out', 'force_out', 'grounded_into_double_play', 'double_play',
             'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play'}


def macro_class(et: str) -> str:
    """Collapse the 20+ official eventTypes into the four macro classes a scorer chooses between."""
    if et == 'field_error':
        return 'error'
    if et in HIT_TYPES:
        return 'hit'
    if et in FC_TYPES:
        return 'fielders_choice'
    if et in OUT_TYPES:
        return 'out'
    return 'other'


def load_pool():
    """gamePk -> {date, rA, rH, eA, eH, eT, api, away, home}. External pool.json wins if supplied."""
    pool = {}
    ver = json.load(open(VERIFIED))
    for g in ver['games']:
        pool[g['pk']] = {'date': g['date'], 'rA': g['rA'], 'rH': g['rH'], 'eA': g['eA'],
                         'eH': g['eH'], 'eT': g['eT'], 'api': g['api'],
                         'away': g['away'], 'home': g['home'], 'source': 'verified_linescores'}
    if POOL and POOL.exists():
        for p in json.load(open(POOL)):
            pool.setdefault(p['pk'], {**p, 'source': 'pool_json'})
    return pool


def main() -> int:
    pool = load_pool()
    games = {int(f.stem): f for f in sorted(RAW.glob('*.json')) if f.stem[0].isdigit()}
    if not games:
        print('no raw game JSONs found under data/raw/', file=sys.stderr)
        return 2
    OUT_SITE.mkdir(parents=True, exist_ok=True)
    OUT_DATA.mkdir(parents=True, exist_ok=True)

    bip_rows, game_rows, quality = [], [], {'games': {}, 'flags': []}
    total = {'plays': 0, 'bip': 0, 'bip_numeric': 0, 'errors': 0, 'errors_without_hitdata': 0,
             'multi_hitdata': 0, 'missing_numeric': 0, 'other_class': 0}
    event_types = Counter()
    for pk in sorted(games):
        d = json.load(open(games[pk]))
        plays = d['liveData']['plays']['allPlays']
        g = {'plays': len(plays), 'bip': 0, 'errors': 0, 'fc': 0, 'healed': False}
        g['healed'] = games[pk].with_suffix('.integrity.txt').exists()
        for pi, pl in enumerate(plays):
            res = pl['result']
            et = res['eventType']
            event_types[et] += 1
            hd_events = [ev for ev in pl.get('playEvents', []) if 'hitData' in ev]
            total['multi_hitdata'] += max(0, len(hd_events) - 1)
            if et == 'field_error':
                total['errors'] += 1
            if not hd_events:
                # No Statcast vector: not a batted ball in play (walk/K/etc) or an error the feed
                # never tracked. Counted as coverage loss, never modelled, never invented.
                if et == 'field_error':
                    total['errors_without_hitdata'] += 1
                    g['errors_no_hd'] = g.get('errors_no_hd', 0) + 1
                continue
            total['bip'] += 1
            hd = hd_events[0]['hitData']
            cls = macro_class(et)
            if cls == 'other':
                total['other_class'] += 1
            if cls == 'error':
                g['errors'] += 1
            if cls == 'fielders_choice':
                g['fc'] += 1
            numeric_ok = all(k in hd for k in ('launchSpeed', 'launchAngle', 'totalDistance'))
            total['bip_numeric'] += int(numeric_ok)
            total['missing_numeric'] += int(not numeric_ok)
            rd = pl.get('reviewDetails') or next((ev.get('reviewDetails', {}) for ev in
                                                 pl.get('playEvents', []) if ev.get('reviewDetails')), {})
            bip_rows.append({
                'game_pk': pk, 'play_index': pi, 'event_type': et, 'macro_class': cls,
                'rbi': res.get('rbi', 0),
                'away_score': res.get('awayScore', ''), 'home_score': res.get('homeScore', ''),
                'launch_speed': hd.get('launchSpeed', ''), 'launch_angle': hd.get('launchAngle', ''),
                'distance': hd.get('totalDistance', ''),
                'trajectory': hd.get('trajectory', ''), 'hardness': hd.get('hardness', ''),
                'numeric_ok': int(numeric_ok),
                'review_overturned': rd.get('isOverturned', ''), 'review_type': rd.get('reviewType', ''),
            })
            g['bip'] += 1
        fa, fh = plays[-1]['result']['awayScore'], plays[-1]['result']['homeScore']
        p = pool.get(pk)
        if p is None:
            quality['flags'].append(f'{pk}: no official linescore record in verified_linescores.json')
            score_ok = False
        else:
            score_ok = (fa == p['rA'] and fh == p['rH'])
            if not score_ok:
                quality['flags'].append(
                    f'{pk}: archive final {fa}-{fh} != OFFICIAL linescore {p["rA"]}-{p["rH"]} ({p["api"]})')
        quality['games'][str(pk)] = {
            'plays': g['plays'], 'bip': g['bip'], 'field_error_plays': g['errors'],
            'field_error_plays_no_hitdata': g.get('errors_no_hd', 0), 'fc_plays': g['fc'],
            'healed': g['healed'], 'final': [fa, fh], 'score_checked_against_official': score_ok,
            'official_eT': (p or {}).get('eT'), 'matchup': f"{(p or {}).get('away', '?')} @ "
                                                   f"{(p or {}).get('home', '?')}",
            'official_url': (p or {}).get('api', '')}
        game_rows.append({'game_pk': pk, 'date': p['date'] if p else '',
                          'away_team': p['away'] if p else '', 'home_team': p['home'] if p else '',
                          'eA': p['eA'] if p else '', 'eH': p['eH'] if p else '',
                          'eT': p['eT'] if p else '',
                          'rH': p['rH'] if p else '', 'rA': p['rA'] if p else '',
                          'plays': g['plays'], 'bip': g['bip'], 'fc': g['fc'],
                          'healed': int(g['healed']),
                          'official_url': p['api'] if p else ''})

    with open(OUT_SITE / 'bip.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(bip_rows[0].keys()))
        w.writeheader(); w.writerows(bip_rows)
    with open(OUT_SITE / 'games.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(game_rows[0].keys()))
        w.writeheader(); w.writerows(game_rows)

    total['plays'] = sum(g['plays'] for g in quality['games'].values())
    quality['totals'] = total
    quality['event_type_counts'] = dict(sorted(event_types.items(), key=lambda kv: -kv[1]))
    quality['strata'] = {'A_eT>=3': 0, 'B_eT==2': 0, 'C_eT<=1': 0}
    for r in game_rows:
        e = int(r['eT'])
        quality['strata']['A_eT>=3' if e >= 3 else 'B_eT==2' if e == 2 else 'C_eT<=1'] += 1
    quality['rule'] = ('labels = official play-result eventType; rows never imputed; every archived final '
                       'score re-checked against the official linescore endpoint; seam heals keep integrity notes')
    quality['official_source'] = VERIFIED.relative_to(ROOT).as_posix()
    json.dump(quality, open(OUT_DATA / 'data_quality.json', 'w'), indent=1)
    # Publish the verification ledger next to the site data so the site is self-contained.
    ver = json.load(open(VERIFIED))
    json.dump(ver, open(OUT_SITE / 'verification.json', 'w'), indent=1)

    bad = [f for f in quality['flags'] if '!=' in f]
    print('games:', len(games), 'plays:', total['plays'], 'bip rows:', len(bip_rows),
          'errors:', total['errors'], 'errors w/o hitData:', total['errors_without_hitdata'],
          'fc:', sum(r['fc'] for r in game_rows), 'multi-hitdata:', total['multi_hitdata'],
          'missing-numeric:', total['missing_numeric'], 'other-class:', total['other_class'])
    print('strata:', quality['strata'])
    if bad:
        for b in bad:
            print('  SCORE MISMATCH:', b)
        return 1
    print('all final scores match the official linescore record')
    return 0


if __name__ == '__main__':
    sys.exit(main())
