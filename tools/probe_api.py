#!/usr/bin/env python3
"""PROBE (read-only): answer the questions the build sandbox cannot answer about official MLB endpoints.

The build sandbox has no route to statsapi.mlb.com / baseballsavant.mlb.com, so every claim about
"what the official API returns" has to be produced by a run that *does* have a route. This script
runs on a GitHub-hosted runner (see .github/workflows/ingest.yml) and prints a compact, auditable
report of exactly what came back — status codes, byte counts, key presence, CORS headers. It never
writes to the repo and never invents a value: an endpoint that fails is reported as a failure.

Questions it answers, in order:
  Q1  Is statsapi.mlb.com reachable from CI, and does it send CORS headers (i.e. can the
      GitHub Pages site call it straight from a visitor's browser)?
  Q2  How large is a live feed when trimmed to the fields we model? (ingest-cost planning)
  Q3  Do modern (2026) feeds carry reviewDetails / isOverturned / playId?
  Q4  Do 2014-2018 feeds carry reviewDetails too (the NYDN archive window)?
  Q5  Do the archived NYDN video short-links still resolve, and to what?
  Q6  Does baseballsavant.com expose a per-play video for a modern playId?

Usage: python3 tools/probe_api.py [--json out.json]
"""
import argparse, json, sys, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = {'User-Agent': 'LiveScoringErrors/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
STATS = 'https://statsapi.mlb.com'
SAVANT = 'https://baseballsavant.mlb.com'

SLIM = ('liveData,plays,allPlays,about,atBatIndex,inning,halfInning,isComplete,result,eventType,'
        'description,rbi,awayScore,homeScore,isOut,reviewDetails,isOverturned,reviewType,'
        'challengeTeamId,inProgress,playEvents,playId,isPitch,details,hitData,launchSpeed,'
        'launchAngle,totalDistance,trajectory,hardness,hitCoordinates,coordX,coordY,runners,'
        'movement,start,end,outBase,isOut,originBase,credits,position,player,fullName')

REPORT = {}


def fetch(url, timeout=45, want='json', max_bytes=2_000_000):
    """Return a dict describing the response. Never raises: failures are data here."""
    req = urllib.request.Request(url, headers=UA)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            status, final = r.status, r.geturl()
    except urllib.error.HTTPError as e:
        return {'url': url, 'status': e.code, 'error': f'HTTPError {e.code}',
                'headers': {k.lower(): v for k, v in (e.headers or {}).items()}, 'bytes': 0}
    except Exception as e:                                                # noqa: BLE001
        return {'url': url, 'status': None, 'error': f'{type(e).__name__}: {e}', 'bytes': 0}
    out = {'url': url, 'final_url': final, 'status': status, 'bytes': len(raw),
           'ms': int(1000 * (time.time() - t0)), 'cors': hdrs.get('access-control-allow-origin')}
    if want == 'json':
        try:
            out['json'] = json.loads(raw)
        except Exception as e:                                            # noqa: BLE001
            out['error'] = f'not JSON: {e}'
    else:
        out['body'] = raw.decode('utf-8', 'replace')
    return out


def q1_reachability_and_cors():
    r = fetch(f'{STATS}/api/v1/game/823441/linescore')
    REPORT['Q1_statsapi'] = {k: r.get(k) for k in ('status', 'bytes', 'cors', 'error')}
    pre = fetch(f'{STATS}/api/v1/schedule?sportId=1&date=2026-07-18&hydrate=linescore')
    REPORT['Q1_schedule'] = {k: pre.get(k) for k in ('status', 'bytes', 'cors', 'error')}
    REPORT['Q1_verdict'] = ('statsapi reachable from CI' if r.get('status') == 200
                            else f"NOT reachable ({r.get('error')})")
    cors = pre.get('cors') or (pre.get('headers') or {}).get('access-control-allow-origin')
    REPORT['Q1_cors_verdict'] = (f'Access-Control-Allow-Origin: {cors} -> browser fetch allowed'
                                 if cors else
                                 'no Access-Control-Allow-Origin header -> browser fetch NOT confirmed')


def q2_feed_size():
    full = fetch(f'{STATS}/api/v1.1/game/823441/feed/live')
    slim = fetch(f'{STATS}/api/v1.1/game/823441/feed/live?fields={SLIM}')
    REPORT['Q2_full_feed_bytes'] = full.get('bytes')
    REPORT['Q2_slim_feed_bytes'] = slim.get('bytes')
    if full.get('bytes') and slim.get('bytes'):
        REPORT['Q2_saving_pct'] = round(100 * (1 - slim['bytes'] / full['bytes']), 1)


def _play_facts(feed):
    plays = feed['liveData']['plays']['allPlays']
    def rev(p):
        return (p.get('reviewDetails')
                or next((e['reviewDetails'] for e in p.get('playEvents', []) if 'reviewDetails' in e), {}))
    with_rev = [p for p in plays if rev(p)]
    overturned = [p for p in with_rev if rev(p).get('isOverturned') is True]
    play_ids = sum(1 for p in plays for e in p.get('playEvents', []) if e.get('playId'))
    # a real playId to hand to baseballsavant: the last playId of a batted-ball play
    batted = [p for p in plays if any('hitData' in e for e in p.get('playEvents', []))]
    sample = None
    if batted:
        ids = [e['playId'] for e in batted[-1].get('playEvents', []) if e.get('playId')]
        sample = ids[-1] if ids else None
    return {'plays': len(plays), 'plays_with_reviewDetails': len(with_rev),
            'plays_overturned': len(overturned), 'play_events_with_playId': play_ids,
            'reviewTypes': sorted({rev(p).get('reviewType') for p in with_rev if rev(p).get('reviewType')}),
            'overturned_descriptions': [p['result']['description'][:110] for p in overturned[:3]],
            'sample_batted_ball_playId': sample,
            'hitData_coord_keys': sorted({k for p in plays for e in p.get('playEvents', [])
                                          if 'hitData' in e for k in e['hitData']})}


def _first_games(day, n=3):
    s = fetch(f'{STATS}/api/v1/schedule?sportId=1&date={day}&fields=dates,date,games,gamePk,'
              f'officialDate,status,detailedState,teams,away,home,team,name')
    return [g['gamePk'] for d in (s.get('json') or {}).get('dates', []) for g in d.get('games', [])][:n]


def q3_modern_feed():
    r = fetch(f'{STATS}/api/v1.1/game/823441/feed/live?fields={SLIM}')
    REPORT['Q3_status'] = r.get('status')
    REPORT['Q3_823441'] = _play_facts(r['json']) if 'json' in r else {'error': r.get('error')}
    # the busiest recent date we know of, to confirm playId/review coverage on a full slate
    return REPORT['Q3_823441'].get('sample_batted_ball_playId')


def q4_old_feed():
    out = {}
    for year, day in {2014: '2014-04-01', 2016: '2016-10-25', 2018: '2018-07-24'}.items():
        pks = _first_games(day)
        rows = []
        for pk in pks:
            f = fetch(f'{STATS}/api/v1.1/game/{pk}/feed/live?fields={SLIM}')
            if 'json' in f:
                rows.append({'pk': pk, **_play_facts(f['json'])})
            else:
                rows.append({'pk': pk, 'error': f.get('error'), 'status': f.get('status')})
        out[str(year)] = {'date': day, 'games_found': len(pks), 'probes': rows}
    REPORT['Q4_replay_eras'] = out


def q5_nydn_links():
    import csv
    path = ROOT / 'docs' / 'data' / 'overturned_calls.csv'
    rows = [r for r in csv.DictReader(open(path)) if r['video']][:8]
    out = []
    for r in rows:
        url = r['video']
        if not url.lower().startswith(('http://', 'https://')):
            url = 'http://' + url.lstrip('/')
        res = fetch(url, timeout=30, want='raw', max_bytes=4096)
        out.append({'source': r['video'], 'date': r['date'], 'player': r['player'],
                    'final_url': res.get('final_url'), 'status': res.get('status'),
                    'bytes': res.get('bytes'), 'error': res.get('error'),
                    'title': ((res.get('body') or '')[:0] or None)})
    REPORT['Q5_shortlink_sample'] = out


def q6_savant_video(play_id):
    if not play_id:
        REPORT['Q6_savant'] = 'no playId available from the feed probe'
        return
    url = f'{SAVANT}/sporty-videos?playId={play_id}'
    r = fetch(url, timeout=45, want='raw', max_bytes=400_000)
    body = r.get('body') or ''
    REPORT['Q6_savant'] = {
        'url': url, 'status': r.get('status'), 'bytes': r.get('bytes'), 'error': r.get('error'),
        'says_no_video': 'No Video Found' in body,
        'has_mp4': '.mp4' in body,
        'mp4_samples': sorted({('https://' + t).split('"')[0].split("'")[0]
                               for t in body.split('https://') if '.mp4' in t[:300]})[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', help='write the report here as well as printing it')
    a = ap.parse_args()
    play_id = None
    for fn in (q1_reachability_and_cors, q2_feed_size, q3_modern_feed, q4_old_feed, q5_nydn_links):
        try:
            r = fn()
            play_id = play_id or (r if isinstance(r, str) else None)
        except Exception as e:                                            # noqa: BLE001
            REPORT[fn.__name__ + '_ERROR'] = f'{type(e).__name__}: {e}'
    try:
        q6_savant_video(play_id)
    except Exception as e:                                                # noqa: BLE001
        REPORT['Q6_ERROR'] = f'{type(e).__name__}: {e}'
    txt = json.dumps(REPORT, indent=1, default=str)
    print(txt)
    if a.json:
        Path(a.json).write_text(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
