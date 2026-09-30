#!/usr/bin/env python3
"""Watch official scoring decisions change — the 'pending scoring decision' half of the brief.

A pending decision cannot be *seen*: MLB does not publish a queue of open scorer judgements. What can
be seen, and what this tool records, is the moment a decision lands. Every run re-reads the official
play-by-play for a rolling window of recent dates and compares each play's ruling against the last
observation stored in `data/ingest/ruling_snapshot.csv`. When an eventType, its RBI or its description
changes, the before/after pair is appended to `data/ingest/ruling_changes.csv` with the official URLs
for the game, so the change is auditable by hand.

That turns "was this decision still open?" into evidence: a ruling observed on day 1 and different on
day 2 was, by definition, open in between — and the row shows exactly what the batter lost or gained
under the official RBI rule (MLB Rules 9.04/9.12: a run that scores because of an error is not an RBI;
the same run on a hit or a run-scoring fielder's choice can be).

The snapshot is a rolling window on purpose (default 21 days): a correction almost always lands within
days of the game, and keeping only the live window stops the committed file from growing without bound.
The changes table has no window — it is the permanent record.

Usage
  python3 tools/watch_rulings.py                     # rolling window around today, commit-ready output
  python3 tools/watch_rulings.py --days 30 --date 2026-09-29
All endpoints are official MLB domains (statsapi.mlb.com).
"""
import argparse, csv, json, re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
STATS = 'https://statsapi.mlb.com'
UA = {'User-Agent': 'LiveScoringErrors-research/1.0 (+https://github.com/buffedlizard55-lab/LiveScoringErrors)'}
SNAP = ROOT / 'data' / 'ingest' / 'ruling_snapshot.csv'
CHANGES = ROOT / 'data' / 'ingest' / 'ruling_changes.csv'
FIELDS = ('liveData,plays,allPlays,result,eventType,event,rbi,description,about,inning,halfInning,'
          'atBatIndex,playEvents,playId,reviewDetails,isOverturned,reviewType,matchup,batter,pitcher,'
          'fullName,teams,away,home,abbreviation,gameData,datetime,officialDate,status,detailedState,'
          'abstractGameState')

# Which plays are worth watching: the ones where a scorer's judgement decides whether the batter is
# credited. A pitch-result or tag-up ruling cannot turn a hit into an error.
BATTED = {'single', 'double', 'triple', 'home_run', 'field_out', 'force_out', 'field_error',
          'fielders_choice', 'fielders_choice_out', 'grounded_into_double_play', 'double_play',
          'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play', 'fielders_choice_double_play'}


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


def schedule(start, end):
    """Official schedule, one row per final game (postponed/suspended games are skipped and logged)."""
    url = (f'{STATS}/api/v1/schedule?sportId=1&startDate={start}&endDate={end}'
           f'&gameType=R,F,D,L,W&hydrate=team')
    out = []
    for d in get_json(url).get('dates', []):
        for g in d['games']:
            if g['status']['abstractGameState'] != 'Final':
                continue
            out.append({'pk': g['gamePk'], 'date': d['date'],
                        'away': team_abbr(g['teams']['away']['team']),
                        'home': team_abbr(g['teams']['home']['team']),
                        'gameType': g.get('gameType', 'R')})
    return out


def plays_of(game):
    """Current official ruling for every scoring-relevant play in one game."""
    pk = game['pk']
    feed = get_json(f'{STATS}/api/v1.1/game/{pk}/feed/live?fields={FIELDS}')
    rows = []
    for pi, p in enumerate(feed['liveData']['plays']['allPlays']):
        et = p['result'].get('eventType', '')
        rd = p.get('reviewDetails') or {}
        if et not in BATTED and not rd:
            continue
        pid = next((e.get('playId') for e in p.get('playEvents', []) if e.get('playId')), '')
        rows.append({
            'game_pk': pk, 'date': game['date'], 'matchup': f"{game['away']} @ {game['home']}",
            'at_bat': pi, 'play_id': pid, 'event_type': et,
            'rbi': p['result'].get('rbi', ''), 'description': p['result'].get('description', '')[:400],
            'batter': ((p.get('matchup') or {}).get('batter') or {}).get('fullName', ''),
            'inning': (p.get('about') or {}).get('inning', ''),
            'half': (p.get('about') or {}).get('halfInning', ''),
            'reviewed': int(bool(rd)), 'overturned': int(bool(rd.get('isOverturned'))),
            'review_type': rd.get('reviewType', ''),
        })
    return rows


def load_snapshot():
    if not SNAP.exists() or not SNAP.stat().st_size:
        return {}
    return {(r['game_pk'], r['at_bat']): r for r in csv.DictReader(open(SNAP))}


def write_csv(path, rows, keys):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


CHANGE_KEYS = ['detected_utc', 'game_pk', 'date', 'matchup', 'inning', 'half', 'at_bat', 'play_id',
               'batter', 'field', 'from', 'to', 'rbi_from', 'rbi_to', 'rbi_note', 'reviewed',
               'overturned', 'review_type', 'feed_url', 'savant_url']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', default=datetime.now(timezone.utc).date().isoformat(),
                    help='anchor date (UTC today by default)')
    ap.add_argument('--days', type=int, default=21, help='rolling window length in days')
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--dry-run', action='store_true', help='report without writing the snapshot')
    a = ap.parse_args(argv)

    anchor = date.fromisoformat(a.date)
    start = (anchor - timedelta(days=a.days - 1)).isoformat()
    games = schedule(start, anchor.isoformat())
    print(f'window {start}..{anchor}: {len(games)} final games', flush=True)

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
    changes_path = CHANGES if CHANGES.exists() else None
    seen = set()
    if changes_path:
        for r in csv.DictReader(open(changes_path)):
            seen.add((r['game_pk'], r['at_bat'], r['field'], r['from'], r['to']))
    for r in rows:
        prev = old.get((r['game_pk'], r['at_bat']))
        if not prev:
            continue
        for field in ('event_type', 'rbi', 'description'):
            was, now = prev.get(field, ''), r.get(field, '')
            if was == now:
                continue
            key = (r['game_pk'], r['at_bat'], field, was, now)
            if key in seen:
                continue
            rbi_note = ''
            if field == 'event_type':
                if was == 'field_error' and now in ('single', 'double', 'triple', 'home_run'):
                    rbi_note = 'error -> hit: runs that an error alone produced now count for the batter'
                elif now == 'field_error':
                    rbi_note = 'hit/FC -> error: any run that depended on the misplay loses its RBI'
                elif 'fielders_choice' in now and was == 'field_error':
                    rbi_note = 'error -> fielder\u2019s choice: a run-scoring FC can carry an RBI'
            changes.append({
                'detected_utc': now_utc, 'game_pk': r['game_pk'], 'date': r['date'],
                'matchup': r['matchup'], 'inning': r['inning'], 'half': r['half'],
                'at_bat': r['at_bat'], 'play_id': r['play_id'], 'batter': r['batter'],
                'field': field, 'from': was[:300], 'to': now[:300],
                'rbi_from': prev.get('rbi', ''), 'rbi_to': r.get('rbi', ''), 'rbi_note': rbi_note,
                'reviewed': r['reviewed'], 'overturned': r['overturned'],
                'review_type': r['review_type'],
                'feed_url': f'{STATS}/api/v1.1/game/{r["game_pk"]}/feed/live',
                'savant_url': (f'https://baseballsavant.mlb.com/sporty-videos?playId={r["play_id"]}'
                               if r['play_id'] else ''),
            })
            print(f"CHANGE {r['date']} {r['matchup']} ab{r['at_bat']} {field}: {was!r} -> {now!r}",
                  flush=True)

    print(f'observed {len(rows)} plays, {len(changes)} changes vs the stored snapshot, '
          f'{len(failures)} fetch failures', flush=True)
    if not a.dry_run:
        for r in rows:
            prev = old.get((r['game_pk'], r['at_bat']))
            r['first_seen_utc'] = (prev or {}).get('first_seen_utc') or now_utc
            # `last_seen_utc` only moves when the observation actually changes. Stamping it on every
            # run would rewrite the whole multi-megabyte snapshot on each collection and bloat the
            # repository with a new blob per run for no information gain.
            unchanged = bool(prev) and all(prev.get(k, '') == r.get(k, '')
                                           for k in ('event_type', 'rbi', 'description'))
            r['last_seen_utc'] = (prev or {}).get('last_seen_utc') or now_utc if unchanged else now_utc
        keep = [(r['game_pk'], r['at_bat']) for r in rows]
        prev_only = [r for k, r in old.items() if k not in set(keep)]
        write_csv(SNAP, rows + prev_only,
                  ['game_pk', 'date', 'matchup', 'at_bat', 'play_id', 'event_type', 'rbi',
                   'description', 'batter', 'inning', 'half', 'reviewed', 'overturned',
                   'review_type', 'first_seen_utc', 'last_seen_utc'])
        if changes:
            existing = list(csv.DictReader(open(CHANGES))) if CHANGES.exists() else []
            write_csv(CHANGES, existing + changes, CHANGE_KEYS)
        print(f'wrote {SNAP.relative_to(ROOT)} ({len(rows) + len(prev_only)} rows) '
              f'and {CHANGES.relative_to(ROOT)} ({len(changes)} new)', flush=True)
    summary = {'ok': True, 'window': [start, anchor.isoformat()], 'games': len(games),
               'plays': len(rows), 'changes': len(changes), 'failures': failures[:20],
               'detected_utc': now_utc}
    print(json.dumps(summary))
    return 0


def run_safely(argv=None):
    """Run the watch and always leave a status file behind.

    A silent no-op is worse than a visible failure: this is the tool that answers the brief's
    "pending scoring decision" question, so if it could not do its job the reason has to be in the
    repository, not in a workflow log nobody can download.
    """
    status_path = ROOT / 'data' / 'ingest' / 'ruling_watch_status.json'
    try:
        rc = main(argv)
        status = {'ok': rc == 0, 'exit_code': rc,
                  'detected_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    except BaseException as e:                                   # noqa: BLE001 - report, never hide
        status = {'ok': False, 'error': f'{type(e).__name__}: {e}'[:400],
                  'detected_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
        print('WATCH FAILED ' + json.dumps(status), flush=True)
    for name in ('ruling_snapshot.csv', 'ruling_changes.csv'):
        path = ROOT / 'data' / 'ingest' / name
        status[name] = {'rows': (sum(1 for _ in open(path)) - 1) if path.exists() else None,
                        'bytes': path.stat().st_size if path.exists() else 0}
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=1))
    print('WROTE ' + str(status_path.relative_to(ROOT)))
    return 0 if status.get('ok') else 1


if __name__ == '__main__':
    sys.exit(run_safely())
