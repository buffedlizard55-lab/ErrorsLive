#!/usr/bin/env python3
"""LIVE scoring board — turn a raw MLB live feed into a /100 scoring-prediction per batted ball.

For every ball in play it prints, BEFORE the human scoring judgement is final:

  SCORE /100        100 x P(ruled ERROR)          (binary logistic, the same number the site shows)
  P(hit) P(fc) P(out)                             (4-class multinomial)
  top pick          the most likely macro call and how confident we are
  runners on        bases occupied BEFORE the ball was hit (2nd/3rd -> RBI is at stake)
  RBI if ruled ...  what the batter's RBI would be under each possible ruling (Rules 9.04/9.12)

Usage
  python3 tools/live_score.py --feed data/source/feed_823441.json
  python3 tools/live_score.py --pk 823441                 # needs network
  python3 tools/live_score.py --today                      # every MLB game scheduled today
  python3 tools/live_score.py --feed F.json --json out.json --csv out.csv
  python3 tools/live_score.py --backtest                  # score all 24 archived games

The model coefficients are read from docs/data/model.json — this tool never refits, and it
re-implements the site's evalModel() math in pure Python (no numpy/sklearn needed at run time).
tests/test_pipeline.py asserts the two implementations agree to 1e-9 on every archived batted ball.
"""
import argparse, csv, json, math, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / 'docs' / 'data' / 'model.json'
API = 'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live'
SCHED = 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&gameType=R'
SLIM = ('liveData,plays,allPlays,result,eventType,description,rbi,awayScore,homeScore,atBatIndex,'
        'about,atBatIndex,halfInning,inning,isComplete,batter,fullName,pitcher,matchup,batSide,'
        'pitchHand,code,runners,movement,start,end,outBase,isOut,playEvents,playId,count,outs,'
        'hitData,launchSpeed,launchAngle,totalDistance,trajectory,hardness,'
        'reviewDetails,isOverturned,reviewType')

HIT_TYPES = {'single', 'double', 'triple', 'home_run'}
FC_TYPES = {'fielders_choice', 'fielders_choice_out'}
OUT_TYPES = {'field_out', 'force_out', 'grounded_into_double_play', 'double_play', 'sac_fly',
             'sac_bunt', 'sac_fly_double_play', 'triple_play'}
RISP_BASES = {'2B', '3B'}
LEGACY_SPEC = [{'name': 'EV_mph', 'type': 'ev'}, {'name': 'LA_deg', 'type': 'la'},
               {'name': 'dist_ft', 'type': 'dist'}] + \
              [{'name': f'traj_{t}', 'type': f'traj:{t}'} for t in
               ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder']] + \
              [{'name': f'hard_{h}', 'type': f'hard:{h}'} for h in ['soft', 'medium', 'hard']]


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


def feature_value(ftype, st):
    """Mirror of the feature spec in tools/train_model.py (kept in lockstep by the audit suite)."""
    if ftype == 'ev':
        return st['ev']
    if ftype == 'la':
        return st['la']
    if ftype == 'dist':
        return st['dist']
    if ftype == 'outs':
        return st['outs']
    if ftype == 'inning':
        return min(st['inning'], 9)
    kind, _, val = ftype.partition(':')
    if kind == 'traj':
        return 1.0 if st['traj'] == val else 0.0
    if kind == 'hard':
        return 1.0 if st['hard'] == val else 0.0
    if kind == 'base':
        return 1.0 if val in st['bases'] else 0.0
    if kind == 'bat':
        return 1.0 if st.get('bat') == val else 0.0
    if kind == 'pitch':
        return 1.0 if st.get('pitch') == val else 0.0
    raise ValueError(f'unknown feature type {ftype!r}')


class Scorer:
    """Pure-python twin of docs/site.js evalModel()."""

    def __init__(self, model=None):
        m = model or json.load(open(MODEL))
        self.m = m
        p = m['primary']
        self.spec = p.get('feature_spec') or LEGACY_SPEC
        self.names = [f['name'] for f in self.spec] if self.spec else p['feature_names']
        self.mean, self.scale = p['scaler_mean'], p['scaler_scale']
        self.b, self.c = p['intercept'], p['coef']
        mc = m['multiclass']
        self.classes = list(mc['coef'].keys())
        self.mc_int, self.mc_coef = mc['intercept'], mc['coef']

    def vector(self, state):
        return [feature_value(f['type'], state) for f in self.spec]

    def _z(self, x):
        return [(x[i] - self.mean[i]) / (self.scale[i] or 1.0) for i in range(len(x))]

    def predict_state(self, st):
        z = self._z(self.vector(st))
        logit = self.b + sum(z[i] * self.c[self.names[i]] for i in range(len(z)))
        p_err = 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, logit))))
        exps = [math.exp(self.mc_int[c] + sum(z[i] * self.mc_coef[c][self.names[i]]
                                              for i in range(len(z)))) for c in self.classes]
        tot = sum(exps) or 1.0
        probs = {c: e / tot for c, e in zip(self.classes, exps)}
        top = max(probs, key=probs.get)
        return p_err, probs, top

    def predict(self, ev, la, dist, traj, hard, bases=(), outs=0, inning=5, bat='', pitch=''):
        """Backwards-compatible entry point (the five Statcast inputs, plus optional context)."""
        return self.predict_state({'ev': ev, 'la': la, 'dist': dist, 'traj': traj, 'hard': hard,
                                   'bases': set(bases), 'outs': outs, 'inning': inning,
                                   'bat': bat, 'pitch': pitch})


def state_from_play(play, hd):
    """Everything the model is allowed to see, taken from the feed BEFORE the ruling is used."""
    bases = {r['movement']['start'] for r in play.get('runners', [])
             if isinstance(r, dict) and isinstance(r.get('movement'), dict)
             and r['movement'].get('start') in ('1B', '2B', '3B')}
    outs = 0
    for ev in play.get('playEvents', []):
        c = ev.get('count') or {}
        if 'outs' in c:
            outs = c['outs']
            break
    about = play.get('about', {}) or {}
    matchup = play.get('matchup', {}) or {}
    return {'ev': hd['launchSpeed'], 'la': hd['launchAngle'], 'dist': hd['totalDistance'],
            'traj': hd.get('trajectory', ''), 'hard': hd.get('hardness', ''), 'bases': bases,
            'outs': outs, 'inning': about.get('inning', 5),
            'bat': (matchup.get('batSide') or {}).get('code', ''),
            'pitch': (matchup.get('pitchHand') or {}).get('code', '')}


def rbi_if_ruled(cls, run_scored, official_et):
    """What the batter's RBI would be under each candidate ruling.

    Rule basis (official MLB glossary, quoted verbatim on docs/rules.html):
      - Error  : "batters do not receive RBIs for any runs that would not have scored without the
                 help of an error"  -> no RBI when the run exists only because of the misplay.
      - RBI    : "A player does not receive an RBI when the run scores as a result of an error or
                 ground into double play."
      - A hit, a home run and a run-scoring fielder's choice can all put an RBI on the books.
    Returns (label, why) or None when the ruling cannot change the RBI on this play.
    """
    if official_et == 'home_run':
        return 'RBI (home run scores the batter)' if cls != 'error' else 'no RBI (HR is never an error)'
    if not run_scored:
        return None
    if cls == 'error':
        return 'NO RBI (run exists only because of the error)'
    if official_et in ('grounded_into_double_play', 'double_play'):
        return None
    if cls == 'fielders_choice':
        return 'RBI possible (run-scoring fielder\u2019s choice; scorer\u2019s call under Rule 9.04)'
    return 'RBI'


def score_feed(feed, pk=None, scorer=None, meta=None):
    """Walk a feed/live payload and emit one row per batted ball in play."""
    sc = scorer or Scorer()
    plays = feed['liveData']['plays']['allPlays']
    rows = []
    for pl in plays:
        res = pl['result']
        # In a live game the current play can have contact data and no `eventType` yet: the scorer has
        # not ruled. That is the project's central case, so it is carried as its own status instead of
        # being skipped or crashing the walk.
        et = res.get('eventType') or ''
        hd = next((e['hitData'] for e in pl.get('playEvents', []) if 'hitData' in e), None)
        if hd is None:
            continue
        need = ('launchSpeed', 'launchAngle', 'totalDistance')
        if not all(k in hd for k in need):
            rows.append({'game_pk': pk, 'at_bat': pl.get('about', {}).get('atBatIndex'),
                         'event_type': et, 'official_call': macro_class(et), 'status': 'no_vector',
                         'description': res.get('description', '')})
            continue
        st = state_from_play(pl, hd)
        bases = sorted(st['bases'])
        run_scored = any(isinstance(r, dict) and isinstance(r.get('movement'), dict)
                         and r['movement'].get('end') == 'score' for r in pl.get('runners', []))
        p_err, probs, top = sc.predict_state(st)
        rev = pl.get('reviewDetails') or next(
            (e['reviewDetails'] for e in pl.get('playEvents', []) if 'reviewDetails' in e), {})
        pid = ''
        for ev in pl.get('playEvents', []):
            if ev.get('playId'):
                pid = ev['playId']
        ruling_in = bool(et)
        row = {
            'game_pk': pk, 'at_bat': pl.get('about', {}).get('atBatIndex'),
            'inning': pl.get('about', {}).get('inning'),
            'half': pl.get('about', {}).get('halfInning'),
            'event_type': et, 'official_call': macro_class(et) if ruling_in else 'pending',
            'status': 'scored' if ruling_in else 'pending_ruling',
            'description': res.get('description', ''),
            'launch_speed': hd['launchSpeed'], 'launch_angle': hd['launchAngle'],
            'distance': hd['totalDistance'], 'trajectory': hd.get('trajectory', ''),
            'hardness': hd.get('hardness', ''),
            'score_100': round(100 * p_err, 2),
            'p_hit': round(probs['hit'], 4), 'p_error': round(p_err, 4),
            'p_fielders_choice': round(probs['fielders_choice'], 4), 'p_out': round(probs['out'], 4),
            'top_pick': top, 'top_prob': round(probs[top], 4),
            'model_agrees_with_call': int(ruling_in and top == macro_class(et)),
            'runners_on': ','.join(bases) or '-', 'risp': int(bool(RISP_BASES & set(bases))),
            'outs_before': st['outs'],
            'run_scored': int(run_scored), 'rbi_official': res.get('rbi', 0),
            'reviewed': int(bool(rev)),
            'review_overturned': rev.get('isOverturned', ''), 'review_type': rev.get('reviewType', ''),
            'play_id': pid,
            'savant_url': f'https://baseballsavant.mlb.com/sporty-videos?playId={pid}' if pid else '',
        }
        if row['risp'] and row['run_scored']:
            row['rbi_if_error'] = rbi_if_ruled('error', True, et)
            row['rbi_if_hit'] = rbi_if_ruled('hit', True, et)
            row['rbi_if_fc'] = rbi_if_ruled('fielders_choice', True, et)
        rows.append(row)
    if meta:
        for r in rows:
            r.update(meta)
    return rows


# ---------------------------------------------------------------- fetch helpers
def http_json(url, timeout=30):
    with urlopen(Request(url, headers={'User-Agent': 'LiveScoringErrors/1.0'}), timeout=timeout) as r:
        return json.load(r)


def fetch_feed(pk):
    return http_json(API.format(pk=pk) + '?fields=' + SLIM)


def todays_games():
    d = datetime.now(timezone.utc).strftime('%m/%d/%Y')
    sched = http_json(SCHED.format(d=d))
    out = []
    for day in sched.get('dates', []):
        for g in day.get('games', []):
            out.append({'pk': g['gamePk'],
                        'matchup': f"{g['teams']['away']['team']['name']} @ "
                                   f"{g['teams']['home']['team']['name']}",
                        'state': g['status']['detailedState'],
                        'url': f"https://www.mlb.com/gameday/{g['gamePk']}"})
    return out


# ---------------------------------------------------------------- reporting
def print_table(rows, title=''):
    if not rows:
        print('  (no batted balls with a Statcast vector)')
        return
    if title:
        print(f'\n{title}')
    print(f"  {'AB':>3} {'call':<7} {'SCORE/100':>9} {'top pick':<16} {'p':>5} "
          f"{'EV':>5} {'LA':>6} {'dist':>5} {'traj':<14} {'hard':<7} {'on':<7} {'o':>1} {'RBI':>3}  ok")
    for r in rows:
        if r.get('status') != 'scored':
            print(f"  {str(r.get('at_bat','')):>3} {r.get('event_type','?'):<7} "
                  f"{'--':>9} {'(no hitData vector)':<16}")
            continue
        flag = 'Y' if r['model_agrees_with_call'] else 'n'
        print(f"  {str(r['at_bat']):>3} {r['official_call']:<7} {r['score_100']:>9} "
              f"{r['top_pick']:<16} {r['top_prob']:>5.2f} {r['launch_speed']:>5} "
              f"{r['launch_angle']:>6} {r['distance']:>5} {r['trajectory']:<14} "
              f"{r['hardness']:<7} {r['runners_on']:<7} {r['outs_before']:>1} {r['rbi_official']:>3}  {flag}")
    scored = [r for r in rows if r.get('status') == 'scored']
    if scored:
        n = len(scored)
        agree = sum(r['model_agrees_with_call'] for r in scored)
        err = [r for r in scored if r['official_call'] == 'error']
        caught = sum(r['model_agrees_with_call'] for r in err)
        print(f"  -- {n} batted balls | model top pick == official call {agree}/{n} "
              f"({100*agree/n:.1f}%) | errors {caught}/{len(err)} called 'error' by the model")
    for r in rows:
        if r.get('reviewed') and r.get('review_overturned') is True:
            print(f"  ** challenge OVERTURNED on AB {r['at_bat']} "
                  f"({r['review_type']}) — model score was {r['score_100']}/100, "
                  f"final call '{r['official_call']}'")


def write_csv(rows, path):
    if not rows:
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--feed', help='path to a feed/live JSON (works offline)')
    ap.add_argument('--pk', type=int, action='append', help='gamePk to fetch (needs network)')
    ap.add_argument('--today', action='store_true', help="score every game on today's MLB slate")
    ap.add_argument('--backtest', action='store_true', help='score the 24 archived games')
    ap.add_argument('--json', help='write all rows to this JSON file')
    ap.add_argument('--csv', help='write all rows to this CSV file')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)

    feeds, sc = [], Scorer()
    if a.feed:
        stem = Path(a.feed).stem.replace('feed_', '')
        feeds.append((int(stem) if stem.isdigit() else stem, json.load(open(a.feed)), a.feed))
    for pk in (a.pk or []):
        try:
            feeds.append((pk, fetch_feed(pk), API.format(pk=pk)))
        except Exception as e:                                   # network-dependent path
            print(f'could not fetch gamePk {pk}: {e}', file=sys.stderr)
    if a.today:
        try:
            for g in todays_games():
                print(f"fetching {g['pk']} {g['matchup']} ({g['state']}) {g['url']}")
                feeds.append((g['pk'], fetch_feed(g['pk']), g['url']))
        except Exception as e:
            print(f'schedule fetch failed: {e}', file=sys.stderr)
    if a.backtest:
        for f in sorted((ROOT / 'data' / 'raw').glob('8*.json')):
            feeds.append((int(f.stem), json.load(open(f)), str(f.relative_to(ROOT))))

    if not feeds:
        ap.print_help()
        return 1

    all_rows = []
    for pk, feed, src in feeds:
        rows = score_feed(feed, pk=pk, scorer=sc, meta={'source': src})
        all_rows += rows
        if not a.quiet:
            print_table(rows, title=f'gamePk {pk}  ({src})')
    if not a.quiet:
        scored = [r for r in all_rows if r.get('status') == 'scored']
        if len(feeds) > 1:
            n = len(scored)
            agree = sum(r['model_agrees_with_call'] for r in scored)
            print(f'\nTOTAL: {len(feeds)} games, {n} batted balls scored, '
                  f'model top pick == official call {agree}/{n} ({100*agree/n:.1f}%)')
        risp = [r for r in scored if r['risp'] and r['run_scored']]
        if risp:
            print(f'RBI-at-stake plays (runner on 2nd/3rd and a run scored): {len(risp)}')
    if a.json:
        json.dump(all_rows, open(a.json, 'w'), indent=1)
        print('wrote', a.json)
    if a.csv:
        write_csv(all_rows, a.csv)
        print('wrote', a.csv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
