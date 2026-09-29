#!/usr/bin/env python3
"""Ingest official MLB games from the Stats API and derive every published artifact.

WHY THIS RUNS IN CI: the build sandbox has no route to statsapi.mlb.com, so *no number in this repo
may be typed by hand*. This tool fetches from the league's own endpoints, verifies each game against
the league's own linescore, and writes machine-derived CSVs. The GitHub Actions job
(.github/workflows/ingest.yml) runs it and commits the outputs, so the sandbox receives data that
was produced by a network-attached process, never by the model writing prose.

WHAT IT WRITES
  data/ingest/games.csv        one row per game  — the audit ledger: gamePk, date, matchup, final
                               score, the exact linescore URL, plays/bip/error counts, review and
                               overturn counts, and whether the feed agreed with the linescore.
  docs/data/bip_official.csv   one row per batted ball in play: Statcast vector + the pre-pitch
                               context that is knowable at contact (base state, outs, handedness).
  docs/data/replays.csv        one row per REVIEWED play: the official replay record with the
                               final call, the review type, the score before/after, whether a run
                               was removed (two independent flags, see below), the RBI each
                               candidate ruling would award, and a per-play video link.
  data/ingest/ingest_report.json  provenance: every request pattern, counts, flags, timings.

RUN-REMOVAL EVIDENCE (never guessed — each row carries its own proof)
  A) HARD: the feed records N runners whose movement ends at "score" but the scoreboard moved by
     fewer than N runs. If that can happen at all it means a run was removed; the count is reported
     either way, so the rule's real-world frequency is visible rather than assumed.
  B) HEURISTIC: overturned review whose own text is about a run (home-run review turned into a
     non-home-run, "out at home", scoring/timing play). Stored with the rule string verbatim.

USAGE
  python3 tools/ingest_official.py --plan data/ingest/plan.json
  python3 tools/ingest_official.py --dates 2026-09-26:2026-09-28 --max-games 40
  python3 tools/ingest_official.py --plan data/ingest/plan.json --videos --limit 25
"""
import argparse, csv, json, math, re, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
UA = {'User-Agent': 'LiveScoringErrors/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
STATS = 'https://statsapi.mlb.com'
SAVANT = 'https://baseballsavant.mlb.com'

# Only fields the model or the audit needs. The full feed is ~15x larger and carries pitch-by-pitch
# detail we never publish, so the ingest stays small enough to run over a whole season.
FIELDS = ','.join([
    'liveData,plays,allPlays,about,atBatIndex,inning,halfInning,isTopInning,isComplete,hasReview,'
    'captivatingIndex,result,eventType,event,description,rbi,awayScore,homeScore,isOut,'
    'reviewDetails,isOverturned,reviewType,challengeTeamId,inProgress,'
    'playEvents,playId,isPitch,index,count,balls,strikes,outs,details,hitData,launchSpeed,'
    'launchAngle,totalDistance,trajectory,hardness,hitCoordinates,coordX,coordY,'
    'matchup,batter,fullName,pitcher,batSide,pitchHand,code,'
    'runners,movement,start,end,outBase,isOut,originBase,credits,position,player,'
])

HIT_TYPES = {'single', 'double', 'triple', 'home_run'}
FC_TYPES = {'fielders_choice', 'fielders_choice_out'}
OUT_TYPES = {'field_out', 'force_out', 'grounded_into_double_play', 'double_play', 'sac_fly',
             'sac_bunt', 'sac_fly_double_play', 'triple_play'}
RISP = {'2B', '3B'}
CLASSES = ['hit', 'error', 'fielders_choice', 'out']

# Reviews whose subject is a run on the scoreboard. MLB's reviewType vocabulary (as returned by the
# feed); anything not listed is treated as "not a scoring review" rather than assumed.
# The feed states the challenged subject in the play text itself, in parentheses:
#   "Marlins challenged (tag play), call on the field was overturned: ..."
# Those strings are the official wording; the two-letter reviewType codes (MJ, MA, MF, ...) are MLB's
# internal vocabulary and are stored verbatim but never translated by guesswork.
SUBJECT_RE = re.compile(r'challenged \((.+?)\)')
# Subjects whose outcome decides whether a run is on the board.
SCORING_SUBJECTS = {'home run', 'scoring play', 'tag play at home plate', 'tag play',
                    'force play at home plate', 'force play', 'timing play', 'fan interference',
                    'stadium boundary call', 'fair or foul in outfield', 'catcher interference'}
PITCH_SUBJECT = 'pitch result'


def review_subject(desc):
    m = SUBJECT_RE.search(desc or '')
    return m.group(1).strip().lower() if m else ''


def macro_class(et):
    if et == 'field_error':
        return 'error'
    if et in HIT_TYPES:
        return 'hit'
    if et in FC_TYPES:
        return 'fielders_choice'
    if et in OUT_TYPES:
        return 'out'
    return 'other'


def get_json(url, timeout=45, retries=3, sleep=1.0):
    """GET with retries. Returns (obj, meta). Raises only after the retries are exhausted."""
    last = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            with urlopen(Request(url, headers=UA), timeout=timeout) as r:
                raw = r.read()
            return json.loads(raw), {'url': url, 'status': 200, 'bytes': len(raw),
                                     'ms': int(1000 * (time.time() - t0)), 'attempt': attempt + 1}
        except HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code in (400, 401, 403, 404):        # never retry a definitive "no"
                break
        except (URLError, OSError, ValueError) as e:
            last = f'{type(e).__name__}: {e}'
        time.sleep(sleep * (attempt + 1))
    raise RuntimeError(f'GET failed after {retries} attempts: {url} ({last})')


# ------------------------------------------------------------------ schedule
def schedule_days(start, end, game_types=None, log=None):
    """date -> [game meta]. One API call per calendar day, all game types unless filtered."""
    out = {}
    d = start
    while d <= end:
        url = (f'{STATS}/api/v1/schedule?sportId=1&date={d.isoformat()}'
               f'&hydrate=team,linescore&fields=dates,date,games,gamePk,officialDate,gameType,'
               f'status,detailedState,abstractGameState,teams,away,home,team,id,name,abbreviation,'
               f'score,isWinner,linescore,currentInning,scheduledInnings')
        try:
            obj, _ = get_json(url)
        except RuntimeError as e:
            if log is not None:
                log['schedule_failures'].append({'date': d.isoformat(), 'error': str(e)})
            d += timedelta(days=1)
            continue
        for day in obj.get('dates', []):
            for g in day.get('games', []):
                gt = g.get('gameType', 'R')
                if game_types and gt not in game_types:
                    continue
                away = g['teams']['away']
                home = g['teams']['home']
                if g.get('status', {}).get('abstractGameState') != 'Final':
                    continue
                out.setdefault(g.get('officialDate', day['date']), []).append({
                    'pk': g['gamePk'], 'date': g.get('officialDate', day['date']), 'gameType': gt,
                    'away': away['team'].get('abbreviation'), 'home': home['team'].get('abbreviation'),
                    'away_name': away['team'].get('name'), 'home_name': home['team'].get('name'),
                    'rA': away.get('score'), 'rH': home.get('score'),
                })
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ per-play parsing
def outs_before(play):
    """Outs already recorded when the play began: the first pitch event's count, else None."""
    for ev in play.get('playEvents', []):
        c = ev.get('count') or {}
        if 'outs' in c:
            return c['outs']
    return None


def review_of(play):
    rd = play.get('reviewDetails')
    if rd:
        return rd
    for ev in play.get('playEvents', []):
        if ev.get('reviewDetails'):
            return ev['reviewDetails']
    return {}


def hitdata_of(play):
    evs = [ev for ev in play.get('playEvents', []) if 'hitData' in ev]
    return evs[0]['hitData'] if evs else None, max(0, len(evs) - 1)


def play_id_of(play):
    """The play's own id: the last playEvent carrying one (the terminal event of the at-bat)."""
    ids = [ev['playId'] for ev in play.get('playEvents', []) if ev.get('playId')]
    return ids[-1] if ids else '', ids


def final_call_text(desc):
    """The part of a review description that states the ruling, and whether it was overturned."""
    if 'call on the field was overturned:' in desc:
        return desc.split('call on the field was overturned:', 1)[1].strip(), True
    if 'call on the field was confirmed:' in desc:
        return desc.split('call on the field was confirmed:', 1)[1].strip(), False
    if 'call on the field stands:' in desc or 'call on the field stands.' in desc:
        return re.split(r'call on the field stands[:.]', desc, 1)[-1].strip(), False
    return desc.strip(), None


def run_removal_evidence(play, et, cls, desc, rd, runs_by_movement, score_delta, subject):
    """Two independent flags, each carrying the reason it fired.

    HARD   #runners whose movement ends at "score" > runs added to the scoreboard. This is a
           consistency test on the feed, not a detector of reviews: the feed publishes the CORRECTED
           outcome, so it is expected to be consistent (and the report says how often it is not).
    HEURISTIC  an overturned review whose own subject and final text mean a run left the board:
           a home-run review overturned into a non-home-run (the batter's run is gone), or a
           scoring/tag/force review whose corrected text puts a runner out at home.
    """
    hard = max(0, runs_by_movement - score_delta)
    final_text, overturned = final_call_text(desc)
    reasons = []
    if hard:
        reasons.append(f'{runs_by_movement} runner(s) recorded at the plate vs {score_delta} run(s) '
                       f'on the scoreboard')
    subj = (subject or '').lower()
    ft = final_text.lower()
    if overturned is True and subj == 'home run' and et != 'home_run':
        reasons.append(f'home-run review overturned and the corrected call is {et!r}, not a home run')
    if overturned is True and subj in SCORING_SUBJECTS and 'out at home' in ft:
        reasons.append(f'{subj} review overturned with the runner put out at home')
    return hard, bool(reasons), '; '.join(reasons)


def rbi_if_ruled(cls, run_scored, official_et):
    """RBI consequence of each candidate ruling (MLB Rules 9.04 / 9.12 / 9.16 — see docs/rules.html)."""
    if not run_scored:
        return None
    if official_et == 'home_run':
        return 'RBI (home run scores the batter)' if cls != 'error' else 'no RBI (an HR cannot be an error)'
    if cls == 'error':
        return 'NO RBI (run scores only because of the error)'
    if official_et in ('grounded_into_double_play', 'double_play'):
        return 'no RBI (run comes on a double play)'
    if cls == 'fielders_choice':
        return "RBI possible (run-scoring fielder's choice is a scorer's call)"
    return 'RBI'


def points_of_interest(play):
    """One row per reviewed play — the site's replay ledger row."""
    res = play['result']
    et = res.get('eventType', '')
    desc = res.get('description', '')
    about = play.get('about', {})
    rd = review_of(play)
    hd, multi = hitdata_of(play)
    pid, all_ids = play_id_of(play)
    score_delta = None                                   # filled by the caller (needs previous play)
    bases = sorted({r['movement']['start'] for r in play.get('runners', [])
                    if isinstance(r.get('movement'), dict) and r['movement'].get('start') in ('1B', '2B', '3B')})
    scored_runners = [r for r in play.get('runners', []) if isinstance(r.get('movement'), dict)
                      and r['movement'].get('end') == 'score']
    row = {
        'game_pk': None, 'date': '', 'matchup': '', 'gameType': '',
        'at_bat': about.get('atBatIndex'), 'inning': about.get('inning'),
        'half': about.get('halfInning'), 'outs_before': outs_before(play),
        'batter': (play.get('matchup') or {}).get('batter', {}).get('fullName', ''),
        'pitcher': (play.get('matchup') or {}).get('pitcher', {}).get('fullName', ''),
        'bat_side': ((play.get('matchup') or {}).get('batSide') or {}).get('code', ''),
        'pitch_hand': ((play.get('matchup') or {}).get('pitchHand') or {}).get('code', ''),
        'event_type': et, 'macro_class': macro_class(et),
        'description': desc, 'rbi_official': res.get('rbi', 0),
        'review_type': rd.get('reviewType', ''),
        'review_subject': review_subject(desc),
        'is_pitch_challenge': int(review_subject(desc) == PITCH_SUBJECT),
        'review_overturned': (int(bool(rd['isOverturned'])) if 'isOverturned' in rd else ''),
        'challenge_team_id': rd.get('challengeTeamId', ''),
        'has_review': int(bool(rd)),
        'bases_before': ','.join(bases) or '-', 'risp': int(bool(RISP & set(bases))),
        'runs_by_movement': len(scored_runners), 'score_delta': score_delta,
        'run_removed_hard': '', 'run_removed_heuristic': '', 'run_removed_rule': '',
        'launch_speed': (hd or {}).get('launchSpeed', ''), 'launch_angle': (hd or {}).get('launchAngle', ''),
        'distance': (hd or {}).get('totalDistance', ''), 'trajectory': (hd or {}).get('trajectory', ''),
        'hardness': (hd or {}).get('hardness', ''),
        'hit_coord_x': (hd or {}).get('coordX', (hd or {}).get('hitCoordinates', {}).get('coordX', '')
                       if isinstance(hd, dict) else ''),
        'hit_coord_y': (hd or {}).get('coordY', (hd or {}).get('hitCoordinates', {}).get('coordY', '')
                       if isinstance(hd, dict) else ''),
        'multi_hitdata': multi,
        'play_id': pid, 'play_ids': ' '.join(all_ids),
        'savant_url': f'{SAVANT}/sporty-videos?playId={pid}' if pid else '',
    }
    # the final ruling text (for overturned plays) is what a human needs next to the video
    final_text, overturned = final_call_text(desc)
    row['final_call_text'] = final_text
    row['desc_says_overturned'] = '' if overturned is None else int(overturned)
    return row


def bip_row(play, pk):
    hd, multi = hitdata_of(play)
    if hd is None:
        return None
    res = play['result']
    about = play.get('about', {})
    bases = {r['movement']['start'] for r in play.get('runners', [])
             if isinstance(r.get('movement'), dict) and r['movement'].get('start') in ('1B', '2B', '3B')}
    numeric = all(k in hd for k in ('launchSpeed', 'launchAngle', 'totalDistance'))
    pid, _ = play_id_of(play)
    return {
        'game_pk': pk, 'at_bat': about.get('atBatIndex'), 'inning': about.get('inning'),
        'half': about.get('halfInning'), 'outs_before': outs_before(play),
        'on_1b': int('1B' in bases), 'on_2b': int('2B' in bases), 'on_3b': int('3B' in bases),
        'runners_on': len(bases), 'risp': int(bool(RISP & bases)),
        'bat_side': ((play.get('matchup') or {}).get('batSide') or {}).get('code', ''),
        'pitch_hand': ((play.get('matchup') or {}).get('pitchHand') or {}).get('code', ''),
        'event_type': res.get('eventType', ''), 'macro_class': macro_class(res.get('eventType', '')),
        'rbi': res.get('rbi', 0),
        'launch_speed': hd.get('launchSpeed', ''), 'launch_angle': hd.get('launchAngle', ''),
        'distance': hd.get('totalDistance', ''), 'trajectory': hd.get('trajectory', ''),
        'hardness': hd.get('hardness', ''), 'numeric_ok': int(numeric), 'multi_hitdata': multi,
        'review_overturned': review_of(play).get('isOverturned', ''),
        'review_type': review_of(play).get('reviewType', ''),
        'play_id': pid,
    }


# ------------------------------------------------------------------ one game
log_video_schema = {'done': False, 'schema': None, 'game_pk': None}


def ingest_game(meta, want_videos=False):
    """Fetch + verify one game. Returns (bip_rows, replay_rows, ledger_row, video_rows, flags)."""
    pk = meta['pk']
    feed_url = f'{STATS}/api/v1.1/game/{pk}/feed/live?fields={FIELDS}'
    ls_url = f'{STATS}/api/v1/game/{pk}/linescore'
    feed, fmeta = get_json(feed_url)
    ls, lmeta = get_json(ls_url)
    plays = feed['liveData']['plays']['allPlays']
    flags = []
    if not plays:
        flags.append(f'{pk}: feed contains no plays')
        return [], [], {'game_pk': pk, 'date': meta['date'], 'gameType': meta['gameType'],
                        'away': meta['away'], 'home': meta['home'], 'rA': '', 'rH': '',
                        'feed_final': '', 'verified': 0, 'plays': 0, 'bip': 0, 'errors': 0,
                        'fc': 0, 'reviews': 0, 'overturned': 0, 'run_removed_hard': 0,
                        'run_removed_heuristic': 0, 'linescore_url': ls_url, 'feed_url': feed_url,
                        'feed_bytes': fmeta['bytes'], 'linescore_bytes': lmeta['bytes']}, [], flags
    fa, fh = plays[-1]['result']['awayScore'], plays[-1]['result']['homeScore']
    ra, rh = ls['teams']['away']['runs'], ls['teams']['home']['runs']
    verified = (fa, fh) == (ra, rh)
    if not verified:
        flags.append(f'{pk}: feed final {fa}-{fh} != official linescore {ra}-{rh}')
    bip, reps = [], []
    prev_a = prev_h = 0
    for pi, pl in enumerate(plays):
        pl['_pk'] = pk
        res = pl['result']
        da, dh = res.get('awayScore', 0) - prev_a, res.get('homeScore', 0) - prev_h
        prev_a, prev_h = res.get('awayScore', 0), res.get('homeScore', 0)
        br = bip_row(pl, pk)
        if br:
            bip.append(br)
        rd = review_of(pl)
        if rd:
            row = points_of_interest(pl)
            row['game_pk'] = pk
            row['date'] = meta['date']
            row['matchup'] = f"{meta['away']} @ {meta['home']}"
            row['gameType'] = meta['gameType']
            row['score_delta'] = da + dh
            hard, heur, why = run_removal_evidence(pl, res.get('eventType', ''),
                                                   macro_class(res.get('eventType', '')),
                                                   res.get('description', ''), rd,
                                                   len([r for r in pl.get('runners', [])
                                                        if isinstance(r.get('movement'), dict)
                                                        and r['movement'].get('end') == 'score']),
                                                   da + dh, row['review_subject'])
            row['run_removed_hard'] = hard
            row['run_removed_heuristic'] = int(heur)
            row['run_removed_rule'] = why
            run_scored = row['runs_by_movement'] > 0
            et = res.get('eventType', '')
            row['rbi_if_error'] = rbi_if_ruled('error', run_scored, et) or ''
            row['rbi_if_hit'] = rbi_if_ruled('hit', run_scored, et) or ''
            row['rbi_if_fc'] = rbi_if_ruled('fielders_choice', run_scored, et) or ''
            row['mlb_gameday'] = f'https://www.mlb.com/gameday/{pk}'
            row['savant_gamefeed'] = f'{SAVANT}/gamefeed?gamePk={pk}'
            reps.append(row)
    ledger = {
        'game_pk': pk, 'date': meta['date'], 'gameType': meta['gameType'],
        'away': meta['away'], 'home': meta['home'], 'rA': ra, 'rH': rh,
        'feed_final': f'{fa}-{fh}', 'verified': int(verified),
        'plays': len(plays), 'bip': len(bip),
        'errors': sum(1 for r in bip if r['macro_class'] == 'error'),
        'fc': sum(1 for r in bip if r['macro_class'] == 'fielders_choice'),
        'reviews': len(reps),
        'overturned': sum(1 for r in reps if r['review_overturned'] is True),
        'run_removed_hard': sum(int(r['run_removed_hard'] or 0) for r in reps),
        'run_removed_heuristic': sum(int(r['run_removed_heuristic'] or 0) for r in reps),
        'linescore_url': ls_url, 'feed_url': feed_url,
        'feed_bytes': fmeta['bytes'], 'linescore_bytes': lmeta['bytes'],
    }
    vids = []
    fetch_videos.last_schema = None
    if want_videos and any(int(r['run_removed_heuristic'] or 0) or int(r['run_removed_hard'] or 0)
                           for r in reps):
        try:
            vids = fetch_videos(pk, reps)
        except Exception as e:                                            # noqa: BLE001
            flags.append(f'{pk}: video lookup failed ({type(e).__name__}: {e})')
        if fetch_videos.last_schema is not None and not log_video_schema['done']:
            log_video_schema['done'] = True
            log_video_schema['schema'] = fetch_videos.last_schema
            log_video_schema['game_pk'] = pk
    return bip, reps, ledger, vids, flags


def fetch_videos(pk, reps):
    """Attach a downloadable mp4 to each run-affected review, when the league's content API has one.

    The content endpoint returns highlight items; each item carries keyword tags and a list of
    playbacks (mp4 renditions). We match on the play id and take the first playback. If nothing
    matches, the row keeps its click-to-watch Savant link and is flagged `no_mp4_found` — the tool
    never substitutes a different play's video.
    """
    obj, _ = get_json(f'{STATS}/api/v1/game/{pk}/content', timeout=60)
    hl = (((obj.get('highlights') or {}).get('highlights') or {}).get('items')
          or (obj.get('highlights') or {}).get('items') or [])
    schema = []
    for it in hl[:2]:
        schema.append({'keys': sorted(it.keys())[:20],
                       'keyword_types': sorted({str(k.get('type')) for k in (it.get('keywordsAll') or [])}),
                       'playback_keys': sorted({k for p in (it.get('playbacks') or []) for k in p})})
    fetch_videos.last_schema = schema
    by_play = defaultdict(list)
    for it in hl:
        for kw in it.get('keywordsAll', []) or []:
            if str(kw.get('type', '')).lower() in ('play_id', 'playid') and kw.get('value'):
                by_play[str(kw['value'])].append(it)
    out = []
    for r in reps:
        if not (int(r['run_removed_heuristic'] or 0) or int(r['run_removed_hard'] or 0)):
            continue
        items = by_play.get(r['play_id'], [])
        pb = []
        for it in items:
            for p in it.get('playbacks', []) or []:
                if p.get('url'):
                    pb.append({'name': p.get('name'), 'url': p.get('url'),
                               'width': p.get('width'), 'height': p.get('height')})
        out.append({
            'game_pk': pk, 'play_id': r['play_id'], 'at_bat': r['at_bat'],
            'title': (items[0].get('title') if items else ''),
            'highlight_count': len(items),
            'mp4': (sorted(pb, key=lambda p: int(p.get('height') or 0))[-1]['url'] if pb else ''),
            'playback_count': len(pb),
            'no_mp4_found': int(not pb),
            'savant_url': r['savant_url'],
        })
    return out


# ------------------------------------------------------------------ NYDN link resolution
def _link_key(r):
    return '|'.join((r.get('date', ''), r.get('game', ''), r.get('player', ''), r.get('video', '')))


def resolve_links(sample=0, timeout=25, workers=16, resume=True):
    """Resolve the archived video short-links to their final destination. Facts, not guesses.

    Resumable: work already committed to docs/data/nydn_links.csv is kept and skipped, so a long
    resolution can be spread over several CI runs instead of timing out inside one.
    """
    src = ROOT / 'docs' / 'data' / 'overturned_calls.csv'
    rows = [r for r in csv.DictReader(open(src)) if r['video']]
    done = {}
    dest = ROOT / 'docs' / 'data' / 'nydn_links.csv'
    if resume and dest.exists() and dest.stat().st_size:
        for r in csv.DictReader(open(dest)):
            done[_link_key(r)] = r
    todo = [r for r in rows if _link_key(r) not in done]
    if sample:
        todo = todo[:sample]
    cache, out = {}, list(done.values())

    def one(r):
        url = r['video']
        if not url.lower().startswith(('http://', 'https://')):
            url = 'http://' + url.lstrip('/')
        if url in cache:
            return {**r, **cache[url]}
        try:
            req = Request(url, headers=UA)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read(2048)
                res = {'final_url': resp.geturl(), 'status': resp.status, 'bytes': len(body),
                       'error': ''}
        except HTTPError as e:
            res = {'final_url': url, 'status': e.code, 'bytes': 0, 'error': f'HTTP {e.code}'}
        except Exception as e:                                            # noqa: BLE001
            res = {'final_url': url, 'status': '', 'bytes': 0, 'error': f'{type(e).__name__}: {e}'}
        cache[url] = res
        return {**r, **res}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, row in enumerate(ex.map(one, todo)):
            out.append(row)
            if (i + 1) % 250 == 0:
                print(f'  resolved {i+1}/{len(todo)} (total stored {len(out)})', flush=True)
    return out


# ------------------------------------------------------------------ main
def parse_dates(spec):
    if ':' in spec:
        a, b = spec.split(':')
        return date.fromisoformat(a), date.fromisoformat(b)
    d = date.fromisoformat(spec)
    return d, d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--plan', help='JSON plan file: {windows:[{start,end,game_types}],max_games,workers,videos}')
    ap.add_argument('--dates', help='YYYY-MM-DD or YYYY-MM-DD:YYYY-MM-DD (overrides the plan)')
    ap.add_argument('--max-games', type=int, help='cap on games fetched (plan wins when omitted)')
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--videos', action='store_true', help='attach mp4 links to run-affected reviews')
    ap.add_argument('--skip-links', action='store_true', help='do not resolve NYDN short-links')
    ap.add_argument('--link-sample', type=int, default=0, help='resolve only the first N links')
    ap.add_argument('--out-dir', default='data/ingest')
    a = ap.parse_args(argv)

    t_start = time.time()
    plan = json.load(open(ROOT / a.plan)) if a.plan else {}
    windows = plan.get('windows') or [{'start': None, 'end': None}]
    if a.dates:
        s, e = parse_dates(a.dates)
        windows = [{'start': s.isoformat(), 'end': e.isoformat()}]
    max_games = a.max_games if a.max_games else (plan.get('max_games') or 200)
    workers = a.workers or plan.get('workers') or 12
    videos = a.videos or bool(plan.get('videos'))
    out_dir = ROOT / a.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    log = {'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'endpoints': {'feed': f'{STATS}/api/v1.1/game/{{pk}}/feed/live?fields={FIELDS[:40]}...',
                         'linescore': f'{STATS}/api/v1/game/{{pk}}/linescore',
                         'schedule': f'{STATS}/api/v1/schedule?sportId=1&date={{date}}',
                         'content': f'{STATS}/api/v1/game/{{pk}}/content'},
           'windows': [], 'schedule_failures': [], 'game_failures': [], 'flags': [],
           'hard_run_removal_examples': [], 'run_removal_rule': (
               'hard = (#runners whose movement ends at "score") - (scoreboard delta); '
               'heuristic = overturned review whose text/type concerns a run (HR review turned into '
               'a non-HR, runner out at home, scoring/timing review with a runner at the plate)')}

    metas, seen = [], set()
    for w in windows:
        start = date.fromisoformat(w['start'])
        end = date.fromisoformat(w['end'])
        gts = set(w['game_types']) if w.get('game_types') else None
        sched = schedule_days(start, end, gts, log)
        found = sum(len(v) for v in sched.values())
        for d in sorted(sched):
            for g in sched[d]:
                if g['pk'] not in seen:
                    seen.add(g['pk'])
                    metas.append(g)
        log['windows'].append({'start': w['start'], 'end': w['end'], 'game_types': sorted(gts) if gts else None,
                               'days_with_games': len([d for d in sched if sched[d]]),
                               'final_games_found': found, 'cumulative_unique_games': len(metas)})
        print(f"window {w['start']}..{w['end']}: {found} final games "
              f"({len(metas)} unique so far)", flush=True)

    metas = sorted(metas, key=lambda m: (m['date'], m['pk']))[:max_games]
    print(f'ingesting {len(metas)} games with {workers} workers', flush=True)

    bip_all, rep_all, ledger_all, vid_all = [], [], [], []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(ingest_game, m, videos): m for m in metas}
        for fut in futs:
            m = futs[fut]
            try:
                bip, reps, led, vids, flags = fut.result()
            except Exception as e:                                        # noqa: BLE001
                log['game_failures'].append({'pk': m['pk'], 'date': m['date'], 'error': str(e)[:300]})
                continue
            bip_all += bip; rep_all += reps; ledger_all.append(led); vid_all += vids
            log['flags'] += flags
            for r in reps:
                if int(r['run_removed_hard'] or 0) and len(log['hard_run_removal_examples']) < 25:
                    log['hard_run_removal_examples'].append(
                        {'game_pk': r['game_pk'], 'date': r['date'], 'matchup': r['matchup'],
                         'at_bat': r['at_bat'], 'description': r['description'][:200],
                         'runs_by_movement': r['runs_by_movement'], 'score_delta': r['score_delta'],
                         'play_id': r['play_id'], 'savant_url': r['savant_url']})
            done += 1
            if done % 25 == 0:
                print(f'  {done}/{len(metas)} games, {len(bip_all)} batted balls, '
                      f'{len(rep_all)} reviewed plays', flush=True)

    def write_csv(path, rows):
        if not rows:
            path.write_text('')
            return
        keys = list(rows[0].keys())
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader(); w.writerows(rows)

    # Published windows: the ledger and replay files can cover a longer span than the modelling CSV
    # (which stays a bounded, honestly-described window so the repo does not balloon).
    def in_window(day, key):
        w = plan.get(key) or {}
        if not w:
            return True
        return (not w.get('start') or day >= w['start']) and (not w.get('end') or day <= w['end'])

    date_of = {l['game_pk']: l['date'] for l in ledger_all}
    bip_pub = [r for r in bip_all if in_window(date_of.get(r['game_pk'], ''), 'bip_window')]
    rep_pub = [r for r in rep_all if in_window(r['date'], 'replays_window')]
    ledger_all.sort(key=lambda r: (r['date'], r['game_pk']))
    bip_all.sort(key=lambda r: (r['game_pk'], r['at_bat']))
    rep_all.sort(key=lambda r: (r['date'], r['game_pk'], r['at_bat']))
    print(f'publishing {len(bip_pub)} of {len(bip_all)} batted balls, '
          f'{len(rep_pub)} of {len(rep_all)} reviewed plays', flush=True)
    write_csv(out_dir / 'games.csv', ledger_all)
    write_csv(ROOT / 'docs' / 'data' / 'bip_official.csv', bip_pub)
    write_csv(ROOT / 'docs' / 'data' / 'replays.csv', rep_pub)
    write_csv(ROOT / 'docs' / 'data' / 'replay_videos.csv', vid_all)

    link_rows = []
    skip_links = a.skip_links or bool(plan.get('skip_links'))
    link_sample = a.link_sample or int(plan.get('link_sample') or 0)
    if not skip_links and (ROOT / 'docs' / 'data' / 'overturned_calls.csv').exists():
        print('resolving archived NYDN video short-links', flush=True)
        link_rows = resolve_links(sample=link_sample, workers=workers)
        write_csv(ROOT / 'docs' / 'data' / 'nydn_links.csv', link_rows)

    n_bip = len(bip_all)
    errs = sum(1 for r in bip_all if r['macro_class'] == 'error')
    summary = {
        'games': len(ledger_all), 'plays': sum(r['plays'] for r in ledger_all), 'bip': n_bip,
        'errors': errs, 'fc': sum(1 for r in bip_all if r['macro_class'] == 'fielders_choice'),
        'numeric_ok': sum(r['numeric_ok'] for r in bip_all),
        'class_counts': dict(Counter(r['macro_class'] for r in bip_all)),
        'reviews': len(rep_all), 'overturned': sum(1 for r in rep_all if r['review_overturned'] == 1),
        'run_removed_hard_rows': sum(int(r['run_removed_hard'] or 0) for r in rep_all),
        'run_removed_heuristic_rows': sum(int(r['run_removed_heuristic'] or 0) for r in rep_all),
        'games_verified_vs_linescore': sum(r['verified'] for r in ledger_all),
        'games_unverified': [r['game_pk'] for r in ledger_all if not r['verified']],
        'review_types': dict(Counter(r['review_type'] for r in rep_all if r['review_type'])),
        'dates': [min((r['date'] for r in ledger_all), default=''),
                  max((r['date'] for r in ledger_all), default='')],
        'videos_attached': sum(1 for v in vid_all if not v['no_mp4_found']),
        'video_rows': len(vid_all),
        'link_rows': len(link_rows),
        'link_status': dict(Counter(str(r['status']) for r in link_rows)),
        'elapsed_s': round(time.time() - t_start, 1),
    }
    log['content_api_schema_probe'] = log_video_schema
    log['summary'] = summary
    (out_dir / 'ingest_report.json').write_text(json.dumps(log, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
