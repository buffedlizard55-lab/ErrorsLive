#!/usr/bin/env python3
"""Collect the current slate from the official feed and publish a live board artifact.

The Pages site reads a timestamped snapshot produced by this collector. GitHub Actions refreshes
`docs/data/live_slate.json` on a best-effort schedule; `docs/data/live_now.json` is an explicitly
labelled offline fixture, not a live fallback. If a refresh fails, the previous snapshot is retained
and the failure is recorded separately rather than replacing real data with a fixture.

Every number here comes from the same `tools/live_score.py` scorer the audit suite pins against
`docs/site.js`, so the browser board and the CLI board cannot disagree.

Usage
  python3 tools/fetch_live.py                          # today's slate (US Eastern game day)
  python3 tools/fetch_live.py --date 2026-09-28
  python3 tools/fetch_live.py --pk 823441 --out /tmp/live.json
Offline (fixture) mode, used by the audit suite; it is not a current-game fallback:
  python3 tools/fetch_live.py --feed data/source/feed_823441.json --out /tmp/live_fixture.json
"""
import argparse, csv, json, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import live_score as ls                                                     # noqa: E402

STATS = 'https://statsapi.mlb.com'
SAVANT = 'https://baseballsavant.mlb.com'


def game_day(date_arg=None, now=None):
    """Return MLB's current calendar day in America/New_York, including DST transitions."""
    if date_arg:
        return date_arg
    eastern = ZoneInfo('America/New_York')
    current = now or datetime.now(timezone.utc)
    return current.astimezone(eastern).date().isoformat()


def slate(day):
    url = (f'{STATS}/api/v1/schedule?sportId=1&startDate={day}&endDate={day}'
           f'&hydrate=linescore,team&gameType=R,F,D,L,W')
    sched = ls.http_json(url)
    out = []
    for d in sched.get('dates', []):
        for g in d['games']:
            out.append({
                'game_pk': g['gamePk'],
                'matchup': f"{g['teams']['away']['team']['abbreviation']} @ "
                           f"{g['teams']['home']['team']['abbreviation']}",
                'away': g['teams']['away']['team']['name'],
                'home': g['teams']['home']['team']['name'],
                'game_type': g.get('gameType', 'R'),
                'state': g['status']['detailedState'],
                'abstract_state': g['status']['abstractGameState'],
                'away_score': ((g.get('linescore') or {}).get('teams') or {}).get('away', {}).get('runs'),
                'home_score': ((g.get('linescore') or {}).get('teams') or {}).get('home', {}).get('runs'),
                'official_feed_url': f'{STATS}/api/v1.1/game/{g["gamePk"]}/feed/live',
                'savant_url': f'{SAVANT}/gamefeed?gamePk={g["gamePk"]}',
                'schedule_url': url,
            })
    return out


def split_matchup(matchup):
    """'PHI @ ATL' -> ('PHI', 'ATL').

    Indexing the string (`matchup[-1]`) is how the live board once rendered the home team as a single
    letter; the abbreviation is taken from the split or not at all.
    """
    if ' @ ' in (matchup or ''):
        away, home = matchup.split(' @ ', 1)
        return away.strip(), home.strip()
    return (matchup or '').strip(), ''


def game_stats(rows):
    """Per-game counts the pages print, derived only from the scored rows."""
    played = [r for r in rows if r.get('status') == 'scored']
    unresolved = [r for r in rows if r.get('status') == 'no_event_type_yet']
    return {'batted_balls': len(played), 'unresolved_in_feed': len(unresolved),
            'errors': sum(1 for r in played if r['official_call'] == 'error'),
            'top_pick_agrees': sum(r['model_agrees_with_call'] for r in played),
            'risp_runs_at_stake': sum(1 for r in played if r['risp'] and r['run_scored']),
            'overturned_reviews': sum(1 for r in played if r.get('review_overturned') is True)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', help='game day YYYY-MM-DD (default: today, US Eastern)')
    ap.add_argument('--pk', type=int, action='append', help='specific gamePk(s)')
    ap.add_argument('--feed', action='append', help='offline fixture feed JSON (repeatable)')
    ap.add_argument('--max-games', type=int, default=20)
    ap.add_argument('--out', default='docs/data/live_slate.json')
    ap.add_argument('--csv-out', default='docs/data/live_slate.csv')
    a = ap.parse_args(argv)

    day = '' if a.feed else game_day(a.date)
    sc = ls.Scorer()
    games, failures = [], []

    if a.feed:                                     # offline mode: score committed fixtures only
        for f in a.feed:
            p = Path(f)
            pk = int(p.stem.replace('feed_', '')) if p.stem.replace('feed_', '').isdigit() else p.stem
            feed = json.load(open(p))
            rows = ls.score_feed(feed, pk=pk, scorer=sc, meta={'source': f})
            plays = (feed.get('liveData') or {}).get('plays', {}).get('allPlays', [])
            # the fixture is stored with only the `liveData` projection, so its official identity comes
            # from the committed verification ledger (date/teams/final score, each with its own URL)
            led = {}
            vpath = ROOT / 'docs' / 'data' / 'verification.json'
            if vpath.exists():
                led = {g['pk']: g for g in json.loads(vpath.read_text()).get('games', [])}
            g = led.get(pk, {})
            games.append({
                'game_pk': pk,
                'matchup': f"{g.get('away', '')} @ {g.get('home', '')}".strip(' @'),
                'game_day': g.get('date', ''),
                'state': 'Final (committed fixture)',
                'official_feed_url': f'{STATS}/api/v1.1/game/{pk}/feed/live',
                'savant_url': f'{SAVANT}/gamefeed?gamePk={pk}',
                'final_away': (plays[-1]['result'].get('awayScore') if plays else None)
                if not g else g.get('rA'),
                'final_home': (plays[-1]['result'].get('homeScore') if plays else None)
                if not g else g.get('rH'),
                'linescore_url': g.get('api', ''),
                'linescore_away': g.get('rA'), 'linescore_home': g.get('rH'),
                'pk': pk, 'away_abbr': g.get('away', ''), 'home_abbr': g.get('home', ''),
                'model_rows': rows, 'plays': rows, 'source': f,
                'url': f'{STATS}/api/v1.1/game/{pk}/feed/live',
                'savant': f'{SAVANT}/gamefeed?gamePk={pk}',
                **game_stats(rows)})
    else:
        meta = slate(day)
        if a.pk:
            meta = [m for m in meta if m['game_pk'] in set(a.pk)] or [
                {'game_pk': pk, 'matchup': '', 'state': 'requested'} for pk in a.pk]
        for m in meta[:a.max_games]:
            try:
                feed = ls.fetch_feed(m['game_pk'])
                rows = ls.score_feed(feed, pk=m['game_pk'], scorer=sc,
                                     meta={'source': m.get('official_feed_url', '')})
                plays = feed['liveData']['plays']['allPlays']
                info = {'plays': len(plays),
                        'final_away': plays[-1]['result'].get('awayScore'),
                        'final_home': plays[-1]['result'].get('homeScore'),
                        'pk': m['game_pk'],
                        'away_abbr': split_matchup(m.get('matchup'))[0],
                        'home_abbr': split_matchup(m.get('matchup'))[1],
                        'linescore_away': m.get('away_score'), 'linescore_home': m.get('home_score')}
                games.append({**m, **info, 'model_rows': rows, 'plays': rows,
                              'url': m.get('official_feed_url', ''), 'savant': m.get('savant_url', ''),
                              **game_stats(rows)})
            except Exception as e:                                   # noqa: BLE001
                failures.append({'game_pk': m['game_pk'], 'error': f'{type(e).__name__}: {e}'[:200]})

    played = [r for g in games for r in g.get('model_rows', []) if r.get('status') == 'scored']
    agree = sum(r['model_agrees_with_call'] for r in played)
    verified = 0
    for g in games:
        la, lh = g.get('linescore_away'), g.get('linescore_home')
        fa, fh = g.get('final_away'), g.get('final_home')
        ok = la is not None and fa is not None and (fa, fh) == (la, lh)
        g['verified'] = int(ok)
        verified += int(ok)
    model = json.load(open(ROOT / 'docs' / 'data' / 'model.json'))
    offline = bool(a.feed)
    doc = {
        'mode': 'offline-fixture' if offline else 'live-slate',
        'generated_utc': '' if offline else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'note': ('Built by tools/run_all.py from a committed historical fixture for offline '
                 'reproducibility; it is not a current game or a live fallback.'
                 if offline else 'Fetched from the official schedule + feed endpoints.'),
        'game_day': day, 'game_date': day,
        'source': (f'{STATS}/api/v1/schedule?sportId=1&startDate={day}&endDate={day}'
                   if not offline else 'data/source/feed_823441.json'),
        'model': {'dataset': model['meta'].get('dataset'), 'n_model': model['meta'].get('n_model'),
                  'n_bip': model['meta'].get('n_bip'), 'games': model['meta'].get('games'),
                  'auc_grouped': model['primary'].get('cv_auc'),
                  'auc_ci95': model['primary'].get('cv_auc_ci95'),
                  'error_recall': model['honesty'].get('oof_error_recall'),
                  'dataset_note': model['meta'].get('dataset_note', '')},
        'summary': {
            'games': len(games),
            'games_verified_vs_linescore': verified,
            'batted_balls': len(played),
            'errors': sum(1 for r in played if r['official_call'] == 'error'),
            'risp_runs_at_stake': sum(1 for r in played if r['risp'] and r['run_scored']),
            'overturned_reviews': sum(1 for r in played if r.get('review_overturned') is True),
            'unresolved_in_feed': sum(1 for g in games for r in g.get('model_rows', [])
                                     if r.get('status') == 'no_event_type_yet'),
            'top_pick_agreement': f'{agree}/{len(played)}' if played else '0/0',
            'failures': len(failures),
        },
        'flags': [f"{f['game_pk']}: {f['error']}" for f in failures],
        'failures': failures,
        'games': games,
    }
    if offline and games and games[0].get('game_day'):
        day = games[0]['game_day']
        doc['game_day'] = doc['game_date'] = day
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    flat = [{k: v for k, v in r.items() if not isinstance(v, (list, dict))} for r in played]
    keys = []
    for r in flat:
        for k in r:
            if k not in keys:
                keys.append(k)
    if not keys:
        keys = ['game_pk', 'at_bat', 'status', 'event_type', 'official_call', 'description']
    with open(ROOT / a.csv_out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(flat)
    print(json.dumps(doc['summary'], indent=1))
    print(f'wrote {a.out} and {a.csv_out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
