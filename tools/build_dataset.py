#!/usr/bin/env python3
"""Pass-1 data builder: validated per-game feed JSONs -> tidy modeling rows.

Input : <work>/raw/{gamePk}.json  (validated, score-checked joined feeds)
        plus repo data/raw/ copies for reproducibility.
Output: docs/data/bip.csv   (one row per batted-ball-in-play event with hitData)
        docs/data/games.csv (one row per game)
        data/data_quality.json
Rules encoded here (audit):
 * Each play's label = play result eventType (the official call).
 * A play can contain ==1 hitData event (verified; flag if not).
 * hitData numeric fields may be missing (observed in the wild) - keep the row with flags.
 * Nothing is ever interpolated or invented; verification against the linescore pool
   happens upstream in fetch/join; here we re-verify final scores one last time.
"""
import csv, json, sys
from pathlib import Path

RAW_A = Path('/home/user/work/raw')
RAW_B = Path(__file__).resolve().parent.parent / 'data' / 'raw'
OUT_SITE = Path(__file__).resolve().parent.parent / 'docs' / 'data'
OUT_DATA = Path(__file__).resolve().parent.parent / 'data'
POOL = Path('/home/user/work/pool.json')

HIT_TYPES = {'single', 'double', 'triple', 'home_run'}
FC_TYPES = {'fielders_choice', 'fielders_choice_out'}
OUT_TYPES = {'field_out', 'force_out', 'grounded_into_double_play', 'double_play',
             'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play'}

def macro_class(et: str) -> str:
    if et == 'field_error':
        return 'error'
    if et in HIT_TYPES:
        return 'hit'
    if et in FC_TYPES:
        return 'fielders_choice'
    if et in OUT_TYPES:
        return 'out'
    return 'other'

def main() -> int:
    pool = {p['pk']: p for p in json.load(open(POOL))}
    games = {}
    for raw_dir in (RAW_A, RAW_B):
        if raw_dir.exists():
            for f in sorted(raw_dir.glob('*.json')):
                if f.name[0].isdigit():
                    games[int(f.stem)] = f
    assert games, 'no raw game JSONs found'
    OUT_SITE.mkdir(parents=True, exist_ok=True)
    OUT_DATA.mkdir(parents=True, exist_ok=True)

    bip_rows, game_rows, quality = [], [], {'games': {}, 'flags': []}
    total = {'plays': 0, 'bip': 0, 'bip_numeric': 0, 'errors': 0, 'multi_hitdata': 0,
             'missing_numeric': 0, 'other_class': 0}
    for pk in sorted(games):
        d = json.load(open(games[pk]))
        plays = d['liveData']['plays']['allPlays']
        g_stats = {'plays': len(plays), 'bip': 0, 'errors': 0, 'fc': 0, 'healed': False}
        integrity = games[pk].with_suffix('.integrity.txt')
        g_stats['healed'] = integrity.exists()
        for pi, pl in enumerate(plays):
            res = pl['result']
            et = res['eventType']
            hd_events = [ev for ev in pl.get('playEvents', []) if 'hitData' in ev]
            if len(hd_events) > 1:
                total['multi_hitdata'] += len(hd_events) - 1
            hd = hd_events[0]['hitData'] if hd_events else None
            if hd is None:
                if et == 'field_error':
                    # error with no batted-ball record (e.g., throwing error after a hit)
                    total['errors'] += 1
                continue
            total['plays'] += 0  # counted at game level
            total['bip'] += 1
            cls = macro_class(et)
            if cls == 'other':
                total['other_class'] += 1
            if et == 'field_error':
                total['errors'] += 1
                g_stats['errors'] += 1
            if cls == 'fielders_choice':
                g_stats['fc'] += 1
            numeric_ok = all(k in hd for k in ('launchSpeed', 'launchAngle', 'totalDistance'))
            total['bip_numeric'] += int(numeric_ok)
            total['missing_numeric'] += int(not numeric_ok)
            reviews = [ev.get('reviewDetails', {}) for ev in pl.get('playEvents', [])]
            rd = pl.get('reviewDetails', {}) or next((r for r in reviews if r), {})
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
            g_stats['bip'] += 1
        fa, fh = plays[-1]['result']['awayScore'], plays[-1]['result']['homeScore']
        p = pool.get(pk, {})
        score_ok = (fa == p.get('rA') and fh == p.get('rH'))
        if not score_ok:
            quality['flags'].append(f'{pk}: final score {fa}-{fh} != pool {p.get("rA")}-{p.get("rH")}')
        quality['games'][str(pk)] = {'plays': g_stats['plays'], 'bip': g_stats['bip'],
                                     'field_error_plays': g_stats['errors'],
                                     'fc_plays': g_stats['fc'], 'healed': g_stats['healed'],
                                     'final': [fa, fh], 'score_checked': score_ok,
                                     'pool_eT': (p.get('eH', 0) + p.get('eA', 0)) if p else None}
        game_rows.append({'game_pk': pk, 'date': p.get('date', ''), 'eT': (p.get('eH', 0) + p.get('eA', 0)) if p else '',
                          'rH': p.get('rH', ''), 'rA': p.get('rA', ''), 'plays': g_stats['plays'],
                          'bip': g_stats['bip'], 'fc': g_stats['fc'], 'healed': int(g_stats['healed'])})

    with open(OUT_SITE / 'bip.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(bip_rows[0].keys()))
        w.writeheader(); w.writerows(bip_rows)
    with open(OUT_SITE / 'games.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(game_rows[0].keys()))
        w.writeheader(); w.writerows(game_rows)
    quality['totals'] = total
    quality['totals']['plays'] = sum(g['plays'] for g in quality['games'].values())
    quality['rule'] = ('labels = official play-result eventType; rows never imputed; '
                       'final scores re-verified vs linescore pool; healed games keep integrity notes')
    json.dump(quality, open(OUT_DATA / 'data_quality.json', 'w'), indent=1)
    print('games:', len(games), 'plays:', quality['totals']['plays'], 'bip rows:', len(bip_rows),
          'errors:', total['errors'], 'fc:', sum(r['fc'] for r in game_rows),
          'multi-hitdata:', total['multi_hitdata'], 'missing-numeric:', total['missing_numeric'],
          'other-class:', total['other_class'])
    return 0

if __name__ == '__main__':
    sys.exit(main())
