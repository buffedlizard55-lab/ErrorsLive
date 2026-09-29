#!/usr/bin/env python3
"""Round-trip probe: what can GitHub Actions reach that the page-build sandbox cannot?

The published site must be rebuildable offline, but the *sources* it is built from have to be
collected from official MLB endpoints somewhere with outbound network access. This script runs in
CI and prints one compact JSON object per probe (status, bytes, and the fragments that matter) so a
maintainer can read the real answer from a run log instead of guessing at an API's shape.

Every probe targets an official MLB domain. Nothing here is used by the offline build.
"""
import json
import re
import sys
import urllib.error
import urllib.request
import zlib

UA = {'User-Agent': 'LiveScoringErrors-research/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
OUT = []


def get(url, timeout=45):
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
        return e.code, e.read()[:4000], dict(e.headers or {})
    except Exception as e:                                   # noqa: BLE001 - probe must never crash
        return -1, str(e).encode(), {}


def emit(name, url, status, body, headers=None, notes=None):
    text = body.decode('utf-8', 'replace')
    rec = {'probe': name, 'status': status, 'bytes': len(body), 'url': url}
    if notes:
        rec.update(notes)
    for pat, key in ((r'https?://[^"\'\\\s]+\.mp4[^"\'\\\s]*', 'mp4_hits'),
                     (r'"playId"\s*:\s*"([0-9a-f\-]{20,})"', 'playId_hits')):
        hits = re.findall(pat, text)
        if hits:
            rec[key] = sorted(set(hits))[:3] if key == 'mp4_hits' else sorted(set(hits))[:2]
            rec[key + '_n'] = len(set(hits))
    if headers:
        rec['ctype'] = headers.get('Content-Type', '')[:60]
    sys.stdout.write(json.dumps(rec)[:900] + '\n')
    sys.stdout.flush()
    OUT.append(rec)


def main():
    # 1. does the schedule API answer for an old season (needed to map 2014-2018 records to gamePks)?
    s, b, _ = get('https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2014-04-01&endDate=2014-04-01')
    dates = json.loads(b.decode())['dates'] if s == 200 else []
    emit('schedule_2014', 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2014-04-01&endDate=2014-04-01',
         s, b, notes={'games': len(dates[0]['games']) if dates else 0,
                      'first_pk': dates[0]['games'][0]['gamePk'] if dates else None})

    # 2. trimmed 2014 feed: are reviewDetails + playIds present in the archive for the NYDN era?
    url = ('https://statsapi.mlb.com/api/v1.1/game/446296/feed/live'
           '?fields=gameData,datetime,officialDate,teams,away,home,name,abbreviation,'
           'liveData,plays,allPlays,result,event,eventType,rbi,description,about,inning,halfInning,'
           'isComplete,reviewDetails,isOverturned,reviewType,playEvents,playId')
    s, b, _ = get(url)
    try:
        j = json.loads(b.decode())
        plays = j['liveData']['plays']['allPlays']
        rev = [(p['result']['eventType'], (p.get('reviewDetails') or {}).get('reviewType'),
                (p.get('reviewDetails') or {}).get('isOverturned'),
                [e.get('playId') for e in p['playEvents'] if e.get('playId')][:1])
               for p in plays if p.get('reviewDetails')]
        emit('feed_2014_446296', url, s, b, notes={'plays': len(plays), 'reviewed': len(rev),
                                                   'reviews': rev[:4]})
    except Exception as e:                                   # noqa: BLE001
        emit('feed_2014_446296', url, s, b, notes={'parse_error': str(e)[:120]})

    # 3. 2026 feed: same question for the current season, plus hitData availability
    url26 = ('https://statsapi.mlb.com/api/v1.1/game/824713/feed/live'
             '?fields=gameData,datetime,officialDate,teams,away,home,name,abbreviation,'
             'liveData,plays,allPlays,result,event,eventType,rbi,description,about,inning,halfInning,'
             'isComplete,reviewDetails,isOverturned,reviewType,playEvents,playId,hitData,'
             'launchSpeed,launchAngle,totalDistance,trajectory,hardness')
    s, b, _ = get(url26)
    try:
        j = json.loads(b.decode())
        plays = j['liveData']['plays']['allPlays']
        bip = [e['hitData'] for p in plays for e in p['playEvents'] if e.get('hitData')
               and e['hitData'].get('launchSpeed')]
        emit('feed_2026_824713', url26, s, b, notes={'plays': len(plays), 'hitdata': len(bip),
                                                     'example': bip[0] if bip else None})
    except Exception as e:                                   # noqa: BLE001
        emit('feed_2026_824713', url26, s, b, notes={'parse_error': str(e)[:120]})

    # 4. game content API: does it hand out a direct mp4 (the 'download' half of click-to-watch)?
    for fields in ('media,playbacks,url,name,title,duration,guid,id,playId',
                   'media,playbacks,url,name,title,duration'):
        u = f'https://statsapi.mlb.com/api/v1/game/823441/content?fields={fields}'
        s, b, _ = get(u)
        try:
            j = json.loads(b.decode())
            pb = j.get('media', {}).get('playbacks', [])
            names = {p.get('name') for p in pb}
            urls = [p.get('url') for p in pb if str(p.get('url', '')).endswith('.mp4')]
            emit('content_823441', u, s, b, notes={'playbacks': len(pb),
                                                   'playback_names': sorted(n for n in names if n)[:4],
                                                   'mp4_hits_n': len(urls), 'mp4_hits': urls[:2]})
        except Exception as e:                               # noqa: BLE001
            emit('content_823441', u, s, b, notes={'parse_error': str(e)[:120]})

    # 5. savant sporty-videos page for a real playId: does the HTML carry a downloadable mp4?
    s, b, _ = get('https://baseballsavant.mlb.com/sporty-videos?playId=1ce3ae12-fa1d-375d-9004-2a5defd8544e')
    emit('sporty_videos', 'https://baseballsavant.mlb.com/sporty-videos?playId=1ce3ae12-fa1d-375d-9004-2a5defd8544e',
         s, b)

    # 6. NYDN source shortlinks: does resolving them from CI give a live MLB video page?
    s, b, h = get('https://www.mlb.com/video/?q=overturned%20call')
    emit('film_room_search', 'https://www.mlb.com/video/?q=overturned%20call', s, b, h)

    print('\nPROBE_SUMMARY ' + json.dumps({'probes': len(OUT),
                                           'reachable': sum(1 for r in OUT if r['status'] == 200)}))


if __name__ == '__main__':
    main()
