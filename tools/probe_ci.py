#!/usr/bin/env python3
"""Round-trip probe: what can GitHub Actions reach that the offline build sandbox cannot?

The published site is built from committed sources with no network, so the *collection* step has to
run somewhere with an outbound route. This script runs in CI, and because GitHub's log download is
unreliable from some clients it writes its findings to `data/ingest/probe_report.json`, which the
workflow commits back to the branch. A maintainer then reads observed HTTP statuses, byte counts and
extracted fragments from the repository instead of guessing at an API's shape.

Two-step probes matter here: the interesting video endpoints need a real `playId` from a real feed,
so the script derives playIds from feeds first and then asks the video endpoints about *those*.
Everything targets an official MLB domain.
"""
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / 'data' / 'ingest' / 'probe_report.json'
UA = {'User-Agent': 'LiveScoringErrors-research/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
REPORT = []
STDOUT = []

FEED_FIELDS = ('gameData,datetime,officialDate,teams,away,home,name,abbreviation,'
               'liveData,plays,allPlays,result,event,eventType,rbi,description,about,inning,'
               'halfInning,isComplete,reviewDetails,isOverturned,reviewType,playEvents,playId,'
               'hitData,launchSpeed,launchAngle,totalDistance,trajectory,hardness')


def get(url, timeout=45):
    """GET that never raises: returns (status, body, headers). -1 status means transport failure."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                try:
                    body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
                except zlib.error:
                    pass
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:8000], dict(e.headers or {})
    except Exception as e:                                   # noqa: BLE001 - a probe must never crash
        return -1, str(e).encode(), {}


def emit(name, url, status, body, headers=None, notes=None):
    text = body.decode('utf-8', 'replace')
    rec = {'probe': name, 'status': status, 'bytes': len(body), 'url': url}
    if notes:
        rec.update(notes)
    mp4 = sorted({m for m in re.findall(r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', text)})
    if mp4:
        rec['mp4_hits'] = mp4[:4]
        rec['mp4_hits_n'] = len(mp4)
    if headers:
        rec['ctype'] = headers.get('Content-Type', '')[:60]
    REPORT.append(rec)
    line = json.dumps(rec)[:1000]
    STDOUT.append(line)
    print(line, flush=True)
    return rec


def schedule_probe(name, date):
    url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={date}&endDate={date}'
    s, b, _ = get(url)
    try:
        dates = json.loads(b.decode())['dates']
        games = dates[0]['games'] if dates else []
        pks = [g['gamePk'] for g in games]
        emit(name, url, s, b, notes={'games': len(games), 'first_pk': pks[0] if pks else None,
                                     'last_pk': pks[-1] if pks else None,
                                     'postseason': sorted({g.get('gameType') for g in games})})
        return pks
    except Exception as e:                                   # noqa: BLE001
        emit(name, url, s, b, notes={'parse_error': str(e)[:120]})
        return []


def feed_probe(name, pk):
    """Fetch one game feed, report review coverage, hitData coverage, and hand back playIds."""
    url = f'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FEED_FIELDS}'
    s, b, _ = get(url)
    playids = []
    try:
        j = json.loads(b.decode())
        plays = j['liveData']['plays']['allPlays']
        rev = [p for p in plays if p.get('reviewDetails')]
        bip = [e['hitData'] for p in plays for e in p['playEvents']
               if e.get('hitData') and e['hitData'].get('launchSpeed')]
        for p in rev:
            for e in p['playEvents']:
                if e.get('playId'):
                    playids.append({'play_id': e['playId'], 'event': p['result']['eventType'],
                                    'overturned': (p.get('reviewDetails') or {}).get('isOverturned'),
                                    'review_type': (p.get('reviewDetails') or {}).get('reviewType'),
                                    'desc': p['result']['description'][:90]})
                    break
        emit(name, url, s, b, notes={'game_pk': pk, 'plays': len(plays), 'reviewed': len(rev),
                                     'hitdata': len(bip),
                                     'date': j['gameData']['datetime'].get('officialDate'),
                                     'teams': [j['gameData']['teams'][s_]['abbreviation']
                                               for s_ in ('away', 'home')],
                                     'reviews': playids[:3]})
    except Exception as e:                                   # noqa: BLE001
        emit(name, url, s, b, notes={'game_pk': pk, 'parse_error': str(e)[:200]})
    return playids


def video_probes(tag, play_id, game_pk):
    """Ask the two candidate video sources about one real playId."""
    if not play_id:
        return
    s, b, h = get(f'https://baseballsavant.mlb.com/sporty-videos?playId={play_id}')
    emit(f'sporty_videos_{tag}', f'https://baseballsavant.mlb.com/sporty-videos?playId={play_id}', s, b, h,
         notes={'no_video_found': 'No Video Found' in b.decode('utf-8', 'replace'),
                'broadcast_links': b.decode('utf-8', 'replace').count('mlb.com/video')})
    for fields in ('media,playbacks,url,name,title,duration,guid,id',
                   'media,playbacks,url'):
        u = f'https://statsapi.mlb.com/api/v1/game/{game_pk}/content?fields={fields}'
        s, b, _ = get(u)
        try:
            pb = json.loads(b.decode()).get('media', {}).get('playbacks', [])
            emit(f'content_{tag}', u, s, b, notes={
                'game_pk': game_pk, 'playbacks': len(pb),
                'names': sorted({p.get('name') for p in pb if p.get('name')})[:4],
                'mp4_n': sum(1 for p in pb if str(p.get('url', '')).endswith('.mp4')),
                'mp4_urls': [p['url'] for p in pb if str(p.get('url', '')).endswith('.mp4')][:2]})
        except Exception as e:                               # noqa: BLE001
            emit(f'content_{tag}', u, s, b, notes={'game_pk': game_pk, 'parse_error': str(e)[:120]})


def main():
    pks_2014 = schedule_probe('schedule_2014_04_01', '2014-04-01')
    pks_2017 = schedule_probe('schedule_2017_06_01', '2017-06-01')
    pks_2026 = schedule_probe('schedule_2026_06_01', '2026-06-01')

    old_ids = feed_probe('feed_2014', pks_2014[0]) if pks_2014 else []
    mid_ids = feed_probe('feed_2017', pks_2017[0]) if pks_2017 else []
    now_ids = feed_probe('feed_2026', pks_2026[0]) if pks_2026 else []

    # two-step: ask the video endpoints about a reviewed play from each era (needs gamePks in hand)
    if old_ids and pks_2014:
        video_probes('2014', old_ids[0]['play_id'], pks_2014[0])
    if mid_ids and pks_2017:
        video_probes('2017', mid_ids[0]['play_id'], pks_2017[0])
    if now_ids and pks_2026:
        video_probes('2026', now_ids[0]['play_id'], pks_2026[0])

    # the search fallback the site offers when a direct video is gone
    emit('film_room_search', 'https://www.mlb.com/video/?q=overturned%20call',
         *get('https://www.mlb.com/video/?q=overturned%20call'))

    REPORT.append({'probe': 'SUMMARY', 'probes': len(REPORT),
                   'reachable': sum(1 for r in REPORT if r.get('status') == 200)})
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({'generated_utc': __import__('datetime').datetime.utcnow()
                                    .strftime('%Y-%m-%dT%H:%M:%SZ'), 'probes': REPORT}, indent=1))
    print('WROTE ' + str(OUT_PATH.relative_to(ROOT)) + f' ({len(REPORT)} probes)')


if __name__ == '__main__':
    main()
