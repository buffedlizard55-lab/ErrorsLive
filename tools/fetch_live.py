#!/usr/bin/env python3
"""Build the LIVE BOARD: every game on a date, scored play by play from the official MLB feed.

This is the "up to date current feed" half of the brief. It needs outbound HTTPS to
statsapi.mlb.com, which the build sandbox does not have — so it is run (a) on a GitHub Actions runner
(.github/workflows/ingest.yml, workflow_dispatch), which commits docs/data/live_now.json, and (b) by
any user on an unrestricted machine, including for a genuinely live in-progress game.

  python3 tools/fetch_live.py                       # today's slate (US Eastern game date)
  python3 tools/fetch_live.py --date 2026-09-28     # a specific date
  python3 tools/fetch_live.py --pk 823441           # one game
  python3 tools/fetch_live.py --out docs/data/live_now.json --quiet

Every game's final (or in-progress) score is re-checked against the official linescore endpoint before
its rows are published, and the report records that check per game. A game that cannot be verified is
published with `verified: 0` and its error text — never silently.
"""
import argparse, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import live_score as ls                                                     # noqa: E402

STATS = 'https://statsapi.mlb.com'
SCHED = (f'{STATS}/api/v1/schedule?sportId=1&date={{d}}&hydrate=team,linescore'
         '&fields=dates,date,games,gamePk,officialDate,gameType,status,detailedState,'
         'abstractGameState,teams,away,home,team,id,name,abbreviation,score')
LINESCORE = f'{STATS}/api/v1/game/{{pk}}/linescore'
# game types: R regular, F wild card, D division series, L championship series, W world series,
# S spring, E exhibition, A all-star. The live board cares about games that count.
COUNTING = ('R', 'F', 'D', 'L', 'W')


def game_date_default():
    """MLB's 'today' is the US Eastern game date, not the UTC date."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
    except Exception:                                                       # noqa: BLE001
        return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime('%Y-%m-%d')


def slate(day):
    obj = ls.http_json(SCHED.format(d=datetime.strptime(day, '%Y-%m-%d').strftime('%m/%d/%Y')))
    out = []
    for d in obj.get('dates', []):
        for g in d.get('games', []):
            away, home = g['teams']['away'], g['teams']['home']
            out.append({
                'pk': g['gamePk'], 'date': d.get('date'), 'gameType': g.get('gameType'),
                'away': away['team'].get('name'), 'home': home['team'].get('name'),
                'away_abbr': away['team'].get('abbreviation'),
                'home_abbr': home['team'].get('abbreviation'),
                'state': g.get('status', {}).get('detailedState'),
                'abstract': g.get('status', {}).get('abstractGameState'),
                'rA': away.get('score'), 'rH': home.get('score'),
                'url': f'https://www.mlb.com/gameday/{g["gamePk"]}',
                'savant': f'https://baseballsavant.mlb.com/gamefeed?gamePk={g["gamePk"]}',
            })
    return out


def verify(pk):
    """Official linescore -> (rA, rH) or an error string."""
    try:
        obj = ls.http_json(LINESCORE.format(pk=pk))
        return obj['teams']['away']['runs'], obj['teams']['home']['runs'], ''
    except Exception as e:                                                  # noqa: BLE001
        return None, None, f'{type(e).__name__}: {e}'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', help='YYYY-MM-DD (default: today, US Eastern)')
    ap.add_argument('--pk', type=int, action='append', help='gamePk(s) to fetch instead of the slate')
    ap.add_argument('--include-noncounting', action='store_true',
                    help='also fetch spring/exhibition/all-star games')
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'data' / 'live_now.json'))
    ap.add_argument('--csv', help='also write the flat play rows here')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)

    day = a.date or game_date_default()
    sc = ls.Scorer()
    games, flags = [], []
    if a.pk:
        todo = [{'pk': pk, 'state': 'selected', 'gameType': '?'} for pk in a.pk]
    else:
        try:
            todo = slate(day)
        except Exception as e:                                              # noqa: BLE001
            print(f'schedule fetch failed for {day}: {e}', file=sys.stderr)
            return 2
    for g in todo:
        if not a.include_noncounting and not a.pk and g.get('gameType') not in COUNTING:
            continue
        try:
            feed = ls.fetch_feed(g['pk'])
        except Exception as e:                                              # noqa: BLE001
            flags.append(f"{g['pk']}: feed fetch failed ({type(e).__name__}: {e})")
            continue
        rows = ls.score_feed(feed, pk=g['pk'], scorer=sc)
        rA, rH, err = verify(g['pk'])
        final = None
        plays = feed['liveData']['plays']['allPlays']
        if plays:
            final = [plays[-1]['result'].get('awayScore'), plays[-1]['result'].get('homeScore')]
        verified = bool(rA is not None and final == [rA, rH])
        if rA is None:
            flags.append(f"{g['pk']}: linescore unavailable ({err})")
        elif not verified:
            flags.append(f"{g['pk']}: feed {final} != official linescore [{rA}, {rH}]")
        scored = [r for r in rows if r.get('status') == 'scored']
        errs = [r for r in scored if r['official_call'] == 'error']
        agree = sum(r['model_agrees_with_call'] for r in scored)
        g2 = dict(g)
        g2.update({
            'plays': len(plays) if plays else 0, 'batted_balls': len(scored),
            'errors': len(errs), 'top_pick_agrees': agree,
            'risp_runs_at_stake': sum(1 for r in scored if r['risp'] and r['run_scored']),
            'overturned_reviews': sum(1 for r in scored if r.get('review_overturned') is True),
            'final': final, 'official_linescore': [rA, rH], 'verified': int(verified),
            'feed_url': ls.API.format(pk=g['pk']) + '?fields=' + ls.SLIM,
            'linescore_url': LINESCORE.format(pk=g['pk']),
            'model_rows': scored,
        })
        games.append(g2)
        if not a.quiet:
            print(f"{g['pk']} {g.get('away_abbr') or g.get('away')} @ "
                  f"{g.get('home_abbr') or g.get('home')} [{g.get('state')}] "
                  f"{len(scored)} batted balls, top pick agrees {agree}/{len(scored)}, "
                  f"{len(errs)} error(s), verified={int(verified)}")
            for r in scored[:0]:
                print(r)

    report = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'game_date': day,
        'source': 'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live (official MLB Stats API)',
        'note': ('Rows are pre-ruling model estimates produced before the human scoring judgment is '
                 'final. The official scorer\u2019s ruling in the feed is the source of record.'),
        'model': {'model_json': 'docs/data/model.json',
                  'score_100_meaning': '100 x P(the play is ruled an error)'},
        'summary': {
            'games': len(games),
            'games_verified_vs_linescore': sum(g['verified'] for g in games),
            'batted_balls': sum(g['batted_balls'] for g in games),
            'errors': sum(g['errors'] for g in games),
            'risp_runs_at_stake': sum(g['risp_runs_at_stake'] for g in games),
            'overturned_reviews': sum(g['overturned_reviews'] for g in games),
            'top_pick_agreement': (
                f"{sum(g['top_pick_agrees'] for g in games)}/"
                f"{sum(g['batted_balls'] for g in games)}"),
        },
        'flags': flags,
        'games': games,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=1))
    if a.csv:
        rows = [r for g in games for r in g['model_rows']]
        ls.write_csv(rows, a.csv)
    if not a.quiet:
        print(json.dumps(report['summary'], indent=1))
        for f in flags:
            print('FLAG:', f)
    print('wrote', a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
