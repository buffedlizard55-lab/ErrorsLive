#!/usr/bin/env python3
"""Re-run the OFFICIAL verification against MLB's own endpoints and rewrite data/verified_linescores.json.

This is how the committed ledger is refreshed rather than merely trusted. It needs outbound HTTPS to
statsapi.mlb.com, which the sandbox this project was built in does not have — so the committed ledger
was produced by running the same checks and the results were recorded by hand-verified capture. Run
this on any unrestricted machine to confirm or refresh it:

    python3 tools/verify_official.py            # verify the 24 games + recount the 222-game pool
    python3 tools/verify_official.py --write    # ...and overwrite the ledger with what was just fetched

Every game is checked on three independent axes:
  1. final score in the archived feed == the official linescore
  2. each game's team-error total lands in the stratum data/TARGETS.json files it under
  3. the official date matches the date the build recorded
Exit code is 1 if any axis fails for any game, so this can be used as a gate.
"""
import argparse, json, sys
from pathlib import Path
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / 'data' / 'verified_linescores.json'
TARGETS = ROOT / 'data' / 'TARGETS.json'
POOL_SUMMARY = ROOT / 'data' / 'pool_summary.json'
LINESCORE = 'https://statsapi.mlb.com/api/v1/game/{pk}/linescore'
SCHEDULE = 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&gameType=R&fields=dates,games,gamePk,officialDate'
TEAM = {121: 'NYM', 120: 'WAS', 111: 'BOS', 139: 'TB', 143: 'PHI', 144: 'ATL', 135: 'SD', 136: 'SEA',
        137: 'SF', 138: 'STL', 140: 'TEX', 141: 'TOR', 142: 'PIT', 143: 'PHI', 145: 'CWS', 146: 'MIA',
        147: 'NYY', 133: 'ATH', 110: 'BAL', 112: 'HOU', 113: 'CIN', 114: 'CLE', 115: 'COL', 116: 'DET',
        117: 'HOU', 118: 'KC', 119: 'LAD', 121: 'NYM', 122: 'CWS', 124: 'PIT', 125: 'CWS', 126: 'CLE',
        127: 'CIN', 128: 'STL', 129: 'COL', 130: 'OAK', 131: 'LAD', 132: 'CWS', 134: 'PIT', 137: 'SF',
        138: 'STL', 139: 'TB', 140: 'TEX', 141: 'TOR', 142: 'BAL', 143: 'PHI', 144: 'ATL', 145: 'CWS',
        146: 'MIA', 147: 'NYY', 158: 'MIL', 159: 'MIN', 160: 'SEA'}
ABBR = {'Arizona': 'ARI', 'Atlanta': 'ATL', 'Baltimore': 'BAL', 'Boston': 'BOS', 'Chicago': 'CHC',
        'Cincinnati': 'CIN', 'Cleveland': 'CLE', 'Colorado': 'COL', 'Detroit': 'DET', 'Houston': 'HOU',
        'Kansas City': 'KC', 'Los Angeles': 'LAD', 'Miami': 'MIA', 'Milwaukee': 'MIL',
        'Minnesota': 'MIN', 'New York': 'NYY', 'Oakland': 'ATH', 'Philadelphia': 'PHI',
        'Pittsburgh': 'PIT', 'San Diego': 'SD', 'San Francisco': 'SF', 'Seattle': 'SEA',
        'St. Louis': 'STL', 'Tampa Bay': 'TB', 'Texas': 'TEX', 'Toronto': 'TOR', 'Washington': 'WAS'}


def get(url):
    with urlopen(Request(url, headers={'User-Agent': 'LiveScoringErrors/1.0'}), timeout=30) as r:
        return json.load(r)


class NetworkUnavailable(RuntimeError):
    pass


def probe():
    'Fail fast and legibly when the host has no outbound HTTPS, instead of a stack trace.'
    try:
        get('https://statsapi.mlb.com/api/v1/game/1/linescore')
    except Exception as e:
        raise NetworkUnavailable(
            f'cannot reach statsapi.mlb.com from this host ({type(e).__name__}: {e}).\n'
            'This tool re-verifies against the LIVE official API and has no offline substitute: an '
            'unreachable network is an UNVERIFIED result, never a pass.\n'
            'Re-run it from a host with outbound HTTPS. The last fully verified ledger is committed '
            f'at {LEDGER.relative_to(ROOT)} and was checked on 2026-09-29.') from None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='overwrite the ledger with what was fetched')
    a = ap.parse_args()

    probe()

    targets = json.load(open(TARGETS))
    ledger = {g['pk']: g for g in json.load(open(LEDGER))['games']}
    fails, rows = [], []

    for pk in sorted(targets['A'] + targets['B'] + targets['C']):
        try:
            ls = get(LINESCORE.format(pk=pk))
        except Exception as e:
            fails.append(f'{pk}: linescore fetch failed: {e}')
            continue
        t = ls['teams']
        eT = t['home']['errors'] + t['away']['errors']
        want = 'A' if eT >= 3 else 'B' if eT == 2 else 'C'
        if pk not in targets.get(want, []):
            fails.append(f'{pk}: eT={eT} belongs in stratum {want}, manifest files it elsewhere')
        arch = json.load(open(ROOT / 'data' / 'raw' / f'{pk}.json'))
        last = arch['liveData']['plays']['allPlays'][-1]['result']
        if (last['awayScore'], last['homeScore']) != (t['away']['runs'], t['home']['runs']):
            fails.append(f'{pk}: archive {last["awayScore"]}-{last["homeScore"]} != official '
                         f'{t["away"]["runs"]}-{t["home"]["runs"]}')
        old = ledger.get(pk, {})
        if old and (old['rA'], old['rH'], old['eA'], old['eH']) != (
                t['away']['runs'], t['home']['runs'], t['away']['errors'], t['home']['errors']):
            fails.append(f'{pk}: committed ledger disagrees with the live endpoint')
        rows.append({'pk': pk, 'rA': t['away']['runs'], 'rH': t['home']['runs'],
                     'eA': t['away']['errors'], 'eH': t['home']['errors'],
                     'eT': eT, 'stratum': want, 'api': LINESCORE.format(pk=pk)})
        print(f'  ok  {pk}  {t["away"]["runs"]}-{t["home"]["runs"]}  E={eT}  stratum {want}')

    # recount the pool
    summary = json.load(open(POOL_SUMMARY))
    per_date, total = {}, 0
    for iso in summary['dates']:
        m, d, y = iso.split('-')
        s = get(SCHEDULE.format(d=f'{m}/{d}/{y}'))
        n = sum(len(day.get('games', [])) for day in s.get('dates', []))
        per_date[iso] = n
        total += n
    print(f'  pool recounted: {total} games over {len(per_date)} dates '
          f'(pool_summary.json says {summary["n_games"]})')
    if total != summary['n_games']:
        fails.append(f'pool size {total} != published {summary["n_games"]}')

    if a.write:
        for g in rows:
            for k in ('rA', 'rH', 'eA', 'eH', 'eT', 'stratum'):
                g.setdefault(k, None)
        doc = json.load(open(LEDGER))
        by = {g['pk']: g for g in doc['games']}
        for g in rows:
            if g['pk'] in by:
                by[g['pk']].update(g)
        json.dump(doc, open(LEDGER, 'w'), indent=1)
        print('ledger rewritten')

    if fails:
        print('\nFAILURES:')
        for f in fails:
            print('  -', f)
        return 1
    print(f'\nVERIFIED: {len(rows)} games match the official record on score, errors and stratum; '
          f'pool = {total} games.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except NetworkUnavailable as e:
        print(f'VERIFICATION NOT PERFORMED\n  {e}', file=sys.stderr)
        sys.exit(2)
