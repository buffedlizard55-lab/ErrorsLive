#!/usr/bin/env python3
"""Observe official play-by-play rulings and record changes between scheduled snapshots.

MLB does not expose a queue of open scorer decisions, so this collector cannot claim to see whether
a decision is officially pending. It can record a narrower, checkable fact: a batted-ball play had
Statcast hitData while `result.eventType` was absent in one captured feed, and a later snapshot added
or changed the eventType/RBI/description. The project labels that feed state `no_event_type_yet`; it
is not proof of an internal scorer queue.

Each run reads current LIVE and FINAL games in a rolling date window, stores the feed state and
contact/context fields, and appends a before/after observation when scoring fields change. It never
infers a reason for the change; the official feed URL, RBI values, first-observation state and link to
MLB's scoring-changes page are retained for review. RBI is a rule-dependent scoring judgement, not a
categorical consequence of an error/hit/fielder's-choice label.

The snapshot itself is bounded to the configured rolling window. The change ledger is permanent.

Usage
  python3 tools/watch_rulings.py                     # rolling window around today's Eastern date
  python3 tools/watch_rulings.py --days 30 --date 2026-09-29
All endpoints are official MLB domains (statsapi.mlb.com).
"""
import argparse, csv, json, re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
STATS = 'https://statsapi.mlb.com'
UA = {'User-Agent': 'LiveScoringErrors-research/1.0 (+https://github.com/buffedlizard55-lab/ErrorsLive)'}
SNAP = ROOT / 'data' / 'ingest' / 'ruling_snapshot.csv'
CHANGES = ROOT / 'data' / 'ingest' / 'ruling_changes.csv'
FIELDS = ('liveData,plays,allPlays,result,eventType,event,rbi,description,about,inning,halfInning,'
          'atBatIndex,playEvents,playId,reviewDetails,isOverturned,reviewType,matchup,batter,pitcher,'
          'fullName,teams,away,home,abbreviation,gameData,datetime,officialDate,status,detailedState,'
          'abstractGameState,hitData,launchSpeed,launchAngle,totalDistance,trajectory,hardness,'
          'hitCoordinates,coordX,coordY,count,outs')

# Which plays are worth watching: the ones where a scorer's judgement decides whether the batter is
# credited. A pitch-result or tag-up ruling cannot turn a hit into an error.
BATTED = {'single', 'double', 'triple', 'home_run', 'field_out', 'force_out', 'field_error',
          'fielders_choice', 'fielders_choice_out', 'grounded_into_double_play', 'double_play',
          'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play', 'fielders_choice_double_play'}


WATCHABLE_STATES = {'Live', 'Final'}
WATCH_FIELDS = ('event_type', 'rbi', 'description', 'status', 'reviewed', 'overturned', 'review_type')
SCORING_CHANGES_URL = 'https://www.mlb.com/official-information/scoring-changes'


def eastern_today(now=None):
    """Return the official MLB calendar date in America/New_York, including DST transitions."""
    current = now or datetime.now(timezone.utc)
    return current.astimezone(ZoneInfo('America/New_York')).date()


def get_json(url, timeout=45, retries=3):
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8', 'replace'))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                continue
            raise
        except Exception:                                        # noqa: BLE001
            if attempt == retries - 1:
                raise
    raise RuntimeError('unreachable')


def team_abbr(team):
    """Abbreviation if the payload hydrates it, else the club name.

    The schedule endpoint only returns `abbreviation` when `hydrate=team` is requested; the first CI
    run of this tool crashed on the un-hydrated shape (its own status file recorded the KeyError),
    so both fields are accepted and the abbreviation is preferred when present.
    """
    if not isinstance(team, dict):
        return ''
    if team.get('abbreviation'):
        return str(team['abbreviation'])
    return str(team.get('name') or team.get('teamName') or '')


def schedule(start, end, active_only=False):
    """Official schedule for live and final games; previews are not yet scoreable."""
    url = (f'{STATS}/api/v1/schedule?sportId=1&startDate={start}&endDate={end}'
           f'&gameType=R,F,D,L,W&hydrate=team')
    out = []
    for d in get_json(url).get('dates', []):
        for g in d['games']:
            status = g.get('status') or {}
            abstract_state = status.get('abstractGameState', '')
            if abstract_state not in WATCHABLE_STATES or (active_only and abstract_state != 'Live'):
                continue
            out.append({'pk': str(g['gamePk']), 'date': d['date'],
                        'away': team_abbr(g['teams']['away']['team']),
                        'home': team_abbr(g['teams']['home']['team']),
                        'gameType': g.get('gameType', 'R'),
                        'abstract_state': abstract_state,
                        'state': status.get('detailedState', '')})
    return out


def review_of_play(play):
    """Review metadata may live on the play or one of its playEvents."""
    return (play.get('reviewDetails') or next(
        (e['reviewDetails'] for e in (play.get('playEvents') or []) if e.get('reviewDetails')), {})) or {}


def hit_data_of_play(play):
    """Return the first Statcast hitData object present in the play's events."""
    return next((e['hitData'] for e in (play.get('playEvents') or [])
                 if isinstance(e, dict) and isinstance(e.get('hitData'), dict)), None)


def plays_from_feed(game, feed):
    """Parse a Stats API feed without network access, retaining feed-unresolved contact plays."""
    rows = []
    for pi, p in enumerate(feed.get('liveData', {}).get('plays', {}).get('allPlays', [])):
        result = p.get('result') or {}
        event_type = result.get('eventType') or ''
        review = review_of_play(p)
        hit_data = hit_data_of_play(p)
        if event_type not in BATTED and not review and hit_data is None:
            continue

        about = p.get('about') or {}
        matchup = p.get('matchup') or {}
        bases = sorted({m.get('start') for r in p.get('runners', [])
                        if isinstance(r, dict) and isinstance(r.get('movement'), dict)
                        for m in [r['movement']] if m.get('start') in ('1B', '2B', '3B')})
        outs = next((e.get('count', {}).get('outs') for e in (p.get('playEvents') or [])
                     if isinstance(e, dict) and isinstance(e.get('count'), dict)
                     and e['count'].get('outs') is not None), '')
        play_id = next((e.get('playId') for e in (p.get('playEvents') or [])
                        if isinstance(e, dict) and e.get('playId')), '')
        status = ('scored' if event_type else
                  'no_event_type_yet' if hit_data is not None else 'review_metadata_only')
        rows.append({
            'game_pk': str(game['pk']), 'date': game['date'],
            'matchup': f"{game['away']} @ {game['home']}",
            'game_state': game.get('abstract_state', ''),
            'at_bat': str(about.get('atBatIndex', pi)), 'play_id': play_id,
            'event_type': event_type, 'status': status,
            'rbi': result.get('rbi', ''), 'description': result.get('description', '')[:400],
            'batter': ((matchup.get('batter') or {}).get('fullName', '')),
            'inning': about.get('inning', ''), 'half': about.get('halfInning', ''),
            'reviewed': int(bool(review)),
            'overturned': int(bool(review.get('isOverturned'))),
            'review_type': review.get('reviewType', ''),
            'launch_speed': (hit_data or {}).get('launchSpeed', ''),
            'launch_angle': (hit_data or {}).get('launchAngle', ''),
            'distance': (hit_data or {}).get('totalDistance', ''),
            'trajectory': (hit_data or {}).get('trajectory', ''),
            'hardness': (hit_data or {}).get('hardness', ''),
            'bases_before': ','.join(bases) or '-', 'outs_before': outs,
        })
    return rows


def plays_of(game):
    """Fetch and parse the current official ruling state for one game."""
    pk = game['pk']
    feed = get_json(f'{STATS}/api/v1.1/game/{pk}/feed/live?fields={FIELDS}')
    return plays_from_feed(game, feed)


def load_snapshot():
    if not SNAP.exists() or not SNAP.stat().st_size:
        return {}
    return {(str(r['game_pk']), str(r['at_bat'])): r
            for r in csv.DictReader(open(SNAP, newline=''))}


def write_csv(path, rows, keys):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def transition_key(game_pk, at_bat, field, before, after, previous_change_utc=''):
    """Deduplicate workflow retries without hiding a later recurrence of the same transition."""
    return (str(game_pk), str(at_bat), field, before, after, previous_change_utc or '')


def normalize(value):
    """One comparable text form for a stored snapshot value.

    The Stats API returns some watched fields as numbers (`rbi`, `reviewed`, `overturned`) while the
    snapshot CSV stores every column as text, so `0` and `'0'` describe the same observation. The
    first version of this comparison tested them for equality directly: the mismatch registered as a
    change and the ledger builder then sliced the integer (`current[:300]`), which crashed every CI
    run with `TypeError: 'int' object is not subscriptable` and left the change ledger empty. Both
    sides now pass through here before they are compared, stored or printed.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def detect_changes(previous, current_rows, seen, now_utc, feed_url_for=None):
    """Diff the stored feed state against one fresh observation.

    Pure function on purpose: `tests/test_pipeline.py` feeds it a stored shape and a fresh shape
    offline, so the int/text regression above cannot come back unnoticed, and a workflow retry is
    proven to be deduplicated without another CI round-trip.
    """
    changes = []
    for r in current_rows:
        key = (normalize(r.get('game_pk')), normalize(r.get('at_bat')))
        prev = previous.get(key)
        if not prev:
            continue
        for field in WATCH_FIELDS:
            was, current = normalize(prev.get(field, '')), normalize(r.get(field, ''))
            if was == current:
                continue
            previous_change_utc = normalize(prev.get('last_changed_utc') or prev.get('last_seen_utc'))
            change_key = transition_key(key[0], key[1], field, was, current, previous_change_utc)
            if change_key in seen:
                continue
            first_status = normalize(prev.get('status')) or (
                'scored' if prev.get('event_type') else 'no_event_type_yet')
            if field == 'event_type':
                rbi_note = ('The official feed eventType changed. Compare the recorded RBI values and '
                            "check Rule 9.04 and MLB's scoring-changes log; this feed label alone does "
                            'not decide the counterfactual RBI.')
            elif field == 'rbi':
                rbi_note = ('The official feed RBI field changed; verify the scoring rationale against '
                            "the 2026 rulebook (Rule 9.04) and MLB's scoring-changes log.")
            else:
                rbi_note = ''
            changes.append({
                'detected_utc': now_utc, 'game_pk': key[0], 'date': normalize(r.get('date')),
                'matchup': normalize(r.get('matchup')), 'inning': normalize(r.get('inning')),
                'half': normalize(r.get('half')),
                'at_bat': key[1], 'play_id': normalize(r.get('play_id')),
                'batter': normalize(r.get('batter')),
                'field': field, 'from': was[:300], 'to': current[:300],
                'previous_last_changed_utc': previous_change_utc,
                'first_observation_status': first_status,
                'was_unresolved_in_feed': int(first_status == 'no_event_type_yet'),
                'first_seen_utc': normalize(prev.get('first_seen_utc')),
                'launch_speed': normalize(prev.get('launch_speed')),
                'launch_angle': normalize(prev.get('launch_angle')),
                'distance': normalize(prev.get('distance')),
                'trajectory': normalize(prev.get('trajectory')),
                'hardness': normalize(prev.get('hardness')),
                'bases_before': normalize(prev.get('bases_before')),
                'outs_before': normalize(prev.get('outs_before')),
                'rbi_from': normalize(prev.get('rbi')), 'rbi_to': normalize(r.get('rbi')),
                'rbi_note': rbi_note,
                'reviewed': normalize(r.get('reviewed')), 'overturned': normalize(r.get('overturned')),
                'review_type': normalize(r.get('review_type')),
                'feed_url': (feed_url_for or (lambda pk: f'{STATS}/api/v1.1/game/{pk}/feed/live'))(key[0]),
                'scoring_changes_url': SCORING_CHANGES_URL,
                'savant_url': (f'https://baseballsavant.mlb.com/sporty-videos?playId='
                               f'{normalize(r.get("play_id"))}' if normalize(r.get('play_id')) else ''),
            })
            seen.add(change_key)
    return changes


CHANGE_KEYS = ['detected_utc', 'game_pk', 'date', 'matchup', 'inning', 'half', 'at_bat', 'play_id',
               'batter', 'field', 'from', 'to', 'previous_last_changed_utc', 'first_observation_status',
               'was_unresolved_in_feed', 'first_seen_utc', 'launch_speed', 'launch_angle', 'distance',
               'trajectory', 'hardness', 'bases_before', 'outs_before', 'rbi_from', 'rbi_to',
               'rbi_note', 'reviewed', 'overturned', 'review_type', 'feed_url', 'scoring_changes_url',
               'savant_url']


def append_changes(path, changes):
    """Append new observations, creating the ledger on the first detected change."""
    if not changes:
        return
    if path.exists() and path.stat().st_size:
        with open(path, newline='') as f:
            existing = list(csv.DictReader(f))
    else:
        existing = []
    write_csv(path, existing + changes, CHANGE_KEYS)


LAST_SUMMARY = {}


def main(argv=None):
    global LAST_SUMMARY
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', default=eastern_today().isoformat(),
                    help='anchor date in America/New_York (Eastern today by default)')
    ap.add_argument('--days', type=int, default=21, help='rolling window length in days')
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--active-only', action='store_true',
                    help='fetch live games only (for frequent lightweight polling)')
    ap.add_argument('--dry-run', action='store_true', help='report without writing the snapshot')
    a = ap.parse_args(argv)

    anchor = date.fromisoformat(a.date)
    start = (anchor - timedelta(days=a.days - 1)).isoformat()
    games = schedule(start, anchor.isoformat(), active_only=a.active_only)
    mode = 'live-only' if a.active_only else 'live-and-final'
    print(f'window {start}..{anchor}: {len(games)} games ({mode})', flush=True)

    rows, failures = [], []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(plays_of, g): g for g in games}
        for i, fut in enumerate(futs):
            try:
                rows += fut.result()
            except Exception as e:                                # noqa: BLE001
                failures.append({'game_pk': futs[fut]['pk'], 'error': str(e)[:200]})
            if (i + 1) % 40 == 0:
                print(f'  {i+1}/{len(games)} games, {len(rows)} scoring-relevant plays', flush=True)

    old = load_snapshot()
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'fetched {len(rows)} scoring-relevant plays from {len(games)} games', flush=True)
    changes = []
    seen = set()
    if CHANGES.exists() and CHANGES.stat().st_size:
        with open(CHANGES, newline='') as f:
            for r in csv.DictReader(f):
                seen.add(transition_key(r['game_pk'], r['at_bat'], r['field'], r['from'], r['to'],
                                        r.get('previous_last_changed_utc', '')))

    changes = detect_changes(old, rows, seen, now_utc)
    for c in changes:
        print(f"CHANGE {c['date']} {c['matchup']} ab{c['at_bat']} {c['field']}: "
              f"{c['from']!r} -> {c['to']!r}", flush=True)

    unresolved = sum(r['status'] == 'no_event_type_yet' for r in rows)
    print(f'observed {len(rows)} plays, {len(changes)} changes vs the stored snapshot, '
          f'{len(failures)} fetch failures; {unresolved} with hitData but no eventType', flush=True)
    if not a.dry_run:
        for r in rows:
            key = (str(r['game_pk']), str(r['at_bat']))
            prev = old.get(key)
            r['first_seen_utc'] = (prev or {}).get('first_seen_utc') or now_utc
            unchanged = bool(prev) and all(normalize(prev.get(k, '')) == normalize(r.get(k, ''))
                                            for k in WATCH_FIELDS)
            # This timestamp means last changed, not last fetched: restamping an unchanged historical
            # window on each hourly run would create needless multi-megabyte Git blobs.
            r['last_changed_utc'] = ((prev or {}).get('last_changed_utc')
                                     or (prev or {}).get('last_seen_utc') or now_utc) if unchanged else now_utc
        keep = {(str(r['game_pk']), str(r['at_bat'])) for r in rows}
        prev_only = [r for key, r in old.items()
                     if key not in keep and start <= r.get('date', '') <= anchor.isoformat()]
        write_csv(SNAP, rows + prev_only,
                  ['game_pk', 'date', 'matchup', 'game_state', 'at_bat', 'play_id', 'event_type',
                   'status', 'rbi', 'description', 'batter', 'inning', 'half', 'reviewed',
                   'overturned', 'review_type', 'launch_speed', 'launch_angle', 'distance',
                   'trajectory', 'hardness', 'bases_before', 'outs_before', 'first_seen_utc',
                   'last_changed_utc'])
        append_changes(CHANGES, changes)
        print(f'wrote {SNAP.relative_to(ROOT)} ({len(rows) + len(prev_only)} rows) '
              f'and {CHANGES.relative_to(ROOT)} ({len(changes)} new)', flush=True)

    summary = {'ok': not failures, 'mode': mode, 'window': [start, anchor.isoformat()],
               'games': len(games), 'live_games': sum(g['abstract_state'] == 'Live' for g in games),
               'final_games': sum(g['abstract_state'] == 'Final' for g in games),
               'plays': len(rows), 'unresolved_in_feed': unresolved, 'changes': len(changes),
               'failures': failures[:20], 'detected_utc': now_utc}
    LAST_SUMMARY = summary
    print(json.dumps(summary))
    return 1 if failures else 0


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_safely(argv=None):
    """Run the watch and always leave a status file behind.

    A silent no-op is worse than a visible failure: this is the tool that answers the brief's
    "pending scoring decision" question, so if it could not do its job the reason has to be in the
    repository, not in a workflow log nobody can download.
    """
    status_path = ROOT / 'data' / 'ingest' / 'ruling_watch_status.json'
    try:
        rc = main(argv)
        status = {**LAST_SUMMARY, 'ok': rc == 0, 'exit_code': rc}
    except BaseException as e:                                   # noqa: BLE001 - report, never hide
        status = {'ok': False, 'exit_code': 1, 'error': f'{type(e).__name__}: {e}'[:400]}
        print('WATCH FAILED ' + json.dumps(status), flush=True)
    for name in ('ruling_snapshot.csv', 'ruling_changes.csv'):
        path = ROOT / 'data' / 'ingest' / name
        status[name] = {'rows': (sum(1 for _ in open(path)) - 1) if path.exists() else None,
                        'bytes': path.stat().st_size if path.exists() else 0}
    previous = {}
    if status_path.exists():
        try:
            previous = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            previous = {}
    stable = lambda value: {k: v for k, v in value.items() if k != 'detected_utc'}
    if previous and stable(previous) == stable(status):
        status['detected_utc'] = previous.get('detected_utc', utc_now())
    else:
        status['detected_utc'] = utc_now()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=1))
    print('WROTE ' + str(status_path.relative_to(ROOT)))
    return 0 if status.get('ok') else 1


if __name__ == '__main__':
    sys.exit(run_safely())
