#!/usr/bin/env python3
"""Probe the official MLB endpoints from CI and record what they actually return.

The published site is rebuilt offline from committed sources, so every network collection step runs
in GitHub Actions. This tool is the discovery step: it asks the official endpoints the questions the
project needs answered, and writes the *observed* answers (status codes, byte counts, detected keys,
sample URLs) to `data/ingest/probe_report.json`, which the workflow commits back to the branch. That
keeps the evidence in the repository where a reviewer can read it, and it means the ingest is written
against observed reality rather than an assumed schema.

Questions this probe answers, all with sources:
  1. Does the schedule API cover the 2014-2018 replay era? (needed to map archive rows to gamePks)
  2. Do those era feeds carry `reviewDetails` and per-event `playId`? (needed to join archive -> feed)
  3. Does the modern feed carry Statcast hitData + reviewDetails + playId? (the model dataset)
  4. Does /api/v1/game/{pk}/content expose a direct .mp4 (the click-to-download half of the brief)?
  5. Which of those mp4s is attached to which playId? (join key for one-click video download)
  6. Does baseballsavant.mlb.com/sporty-videos?playId=... answer per play (click-to-watch half)?
Everything is an official MLB domain; nothing is inferred.
"""
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'data' / 'ingest' / 'probe_report.json'
UA = {'User-Agent': 'LiveScoringErrors-research/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
RECORDS = []
FEED_FIELDS = ('gameData,datetime,officialDate,teams,away,home,name,abbreviation,'
               'liveData,plays,allPlays,result,event,eventType,rbi,description,about,inning,'
               'halfInning,isComplete,reviewDetails,isOverturned,reviewType,playEvents,playId,'
               'hitData,launchSpeed,launchAngle,totalDistance,trajectory,hardness')


def get(url, timeout=45):
    """GET that never raises. status -1 means a transport failure (recorded, not crashed on)."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                try:
                    body = zlib.decompress(body, 16 + zlib.MAX_WBITS)
                except zlib.error:
                    pass
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:8000]
    except Exception as e:                                       # noqa: BLE001
        return -1, str(e).encode()


def record(probe, url, status, body, **facts):
    text = body.decode('utf-8', 'replace')
    rec = {'probe': probe, 'url': url, 'status': status, 'bytes': len(body)}
    rec.update(facts)
    mp4 = sorted({m for m in re.findall(r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', text)})
    if mp4:
        rec['mp4_hits'] = mp4[:3]
        rec['mp4_hits_n'] = len(mp4)
    RECORDS.append(rec)
    print(json.dumps(rec)[:700], flush=True)
    return rec


def games_on(date):
    url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={date}&endDate={date}'
    status, body = get(url)
    try:
        dates = json.loads(body.decode())['dates']
        games = dates[0]['games'] if dates else []
        return status, body, games
    except Exception:                                            # noqa: BLE001
        return status, body, []


def feed_probe(tag, pk):
    url = f'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FEED_FIELDS}'
    status, body = get(url)
    facts = {'game_pk': pk}
    play_ids = []
    try:
        j = json.loads(body.decode())
        plays = j['liveData']['plays']['allPlays']
        reviewed = [p for p in plays if p.get('reviewDetails')]
        hit_events = [e for p in plays for e in p['playEvents'] if e.get('hitData')]
        coord_keys = sorted({k for e in hit_events for k in (e['hitData'].get('coordinates') or {})})
        facts.update({
            'date': j['gameData']['datetime'].get('officialDate'),
            'teams': [j['gameData']['teams'][s]['abbreviation'] for s in ('away', 'home')],
            'plays': len(plays), 'reviewed': len(reviewed),
            'reviewTypes': sorted({(p['reviewDetails'].get('reviewType') or '') for p in reviewed}),
            'playIds': sum(1 for p in plays for e in p['playEvents'] if e.get('playId')),
            'hitData_events': len(hit_events),
            'hitData_keys': sorted({k for e in hit_events for k in e['hitData']})[:14],
            'hitData_coord_keys': coord_keys,
        })
        for p in reviewed:
            pid = next((e.get('playId') for e in p['playEvents'] if e.get('playId')), None)
            if pid:
                play_ids.append({'play_id': pid, 'game_pk': pk,
                                 'eventType': p['result']['eventType'],
                                 'overturned': p['reviewDetails'].get('isOverturned'),
                                 'desc': p['result']['description'][:80]})
    except Exception as e:                                       # noqa: BLE001
        facts['parse_error'] = str(e)[:200]
    record(f'feed_{tag}', url, status, body, **facts)
    return play_ids


def content_probe(tag, pk, play_id=None):
    """The download half: find .mp4 URLs and, if a playId is known, which one belongs to it."""
    url = f'https://statsapi.mlb.com/api/v1/game/{pk}/content'
    status, body = get(url)
    facts = {'game_pk': pk, 'play_id': play_id}
    try:
        j = json.loads(body.decode())
        facts['top_keys'] = sorted(j)
        hl = (j.get('highlights') or {}).get('highlights', {}).get('items', [])
        facts['highlight_items'] = len(hl)
        if hl:
            facts['item_keys'] = sorted(hl[0])
            facts['playback_keys'] = sorted((hl[0].get('playbacks') or [{}])[0])
            kw = {(k.get('type'), k.get('value')) for it in hl for k in (it.get('keywordsAll') or [])}
            facts['keyword_types'] = sorted({t for t, _ in kw if t})[:10]
            facts['items_with_play_id'] = sum(
                1 for it in hl if any(k.get('type') == 'play_id' for k in (it.get('keywordsAll') or [])))
            if play_id:
                for it in hl:
                    if any(k.get('value') == play_id for k in (it.get('keywordsAll') or [])):
                        facts['matched_item'] = {
                            'title': it.get('title'), 'duration': it.get('duration'),
                            'playbacks': [{'name': p.get('name'), 'url': p.get('url')}
                                          for p in (it.get('playbacks') or [])][:4]}
                        break
        facts['mp4_in_body'] = len(re.findall(r'https?://[^"\'\\\s]+?\.mp4', body.decode('utf-8', 'replace')))
    except Exception as e:                                       # noqa: BLE001
        facts['parse_error'] = str(e)[:200]
    record(f'content_{tag}', url, status, body, **facts)


def main():
    eras = [('2014', '2014-04-01'), ('2017', '2017-06-01'), ('2026', '2026-06-01')]
    ids = {}
    for tag, date in eras:
        status, body, games = games_on(date)
        record(f'schedule_{tag}', f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={date}&endDate={date}',
               status, body, games=len(games), gameTypes=sorted({g.get('gameType') for g in games}),
               first_pk=games[0]['gamePk'] if games else None,
               first_teams=[games[0]['teams']['away']['team']['name'], games[0]['teams']['home']['team']['name']]
               if games else None)
        if games:
            ids[tag] = feed_probe(tag, games[0]['gamePk'])
    for tag in ('2014', '2017', '2026'):
        picks = ids.get(tag) or []
        pk = picks[0]['game_pk'] if picks else None
        if not pk:
            continue
        # content endpoint: does it hand out mp4s, and can a playId be joined to one?
        content_probe(tag, pk, picks[0]['play_id'] if picks else None)
        if picks:
            pid = picks[0]['play_id']
            status, body = get(f'https://baseballsavant.mlb.com/sporty-videos?playId={pid}')
            text = body.decode('utf-8', 'replace')
            record(f'sporty_videos_{tag}', f'https://baseballsavant.mlb.com/sporty-videos?playId={pid}',
                   status, body, play_id=pid, no_video_found='No Video Found' in text,
                   mlb_video_links=len(re.findall(r'mlb\.com/video', text)),
                   savant_gamefeed='gamefeed?gamePk' in text)
    status, body = get('https://www.mlb.com/video/?q=overturned+call')
    record('film_room_search', 'https://www.mlb.com/video/?q=overturned+call', status, body)

    # the exact schedule URL tools/watch_rulings.py uses (multi game-type filter, rolling window)
    wurl = ('https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-09'
            '&endDate=2026-09-29&gameType=R,F,D,L,W')
    status, body = get(wurl)
    try:
        dates = json.loads(body.decode())['dates']
        ngames = sum(len(d.get('games', [])) for d in dates)
        finals = sum(1 for d in dates for g in d.get('games', [])
                     if g['status']['abstractGameState'] == 'Final')
    except Exception:                                            # noqa: BLE001
        ngames = finals = None
    record('schedule_watcher_window', wurl, status, body, games=ngames, final_games=finals)

    # Can a browser call the official endpoint directly? The live page tries exactly this before
    # falling back to the committed snapshot, so the answer belongs in the committed evidence.
    req = urllib.request.Request(
        'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-27',
        headers={**UA, 'Origin': 'https://buffedlizard55-lab.github.io'})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            acao = r.headers.get('Access-Control-Allow-Origin', '')
            record('cors_statsapi', req.full_url, r.status, r.read(),
                   origin_sent='https://buffedlizard55-lab.github.io',
                   access_control_allow_origin=acao or '(absent)',
                   browser_bundle_cannot_read=not acao)
    except Exception as e:                                       # noqa: BLE001
        record('cors_statsapi', req.full_url, -1, str(e).encode(), error=str(e)[:160])

    REPORT = {'generated_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'note': 'Observed official-endpoint behaviour; committed by .github/workflows/probe.yml. '
                      'Re-run: gh workflow run probe.yml',
              'records': RECORDS}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(REPORT, indent=1))
    print(f'WROTE {OUT.relative_to(ROOT)} ({len(RECORDS)} records)')


if __name__ == '__main__':
    sys.exit(main())
