#!/usr/bin/env python3
"""Score MLB feed events with separate macro-error and detailed outcome estimates.

For represented batted-ball events, the tool reports (when a complete Statcast vector exists):

  SCORE /100        100 x P(final feed macro-class = ERROR) (binary logistic)
  macro probabilities and top pick for hit / error / fielder's choice / out
  11-category event probabilities and top category (single, double, triple, HR, outs, etc.)
  exact official-scorer-pending marker state, kept distinct from a missing result.eventType
  runners on        bases occupied BEFORE the ball was hit (2nd/3rd -> RBI is at stake)
  RBI notes          conditional Rule 9.04 reminders, not counterfactual scorer decisions

Model outputs are estimates of the final captured feed class, not official rulings or predictions
of whether a provisional scorer decision will later change. A prior-event pending marker is not
scored as a plate-appearance outcome; missing vectors remain visible without invented probabilities.

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
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / 'docs' / 'data' / 'model.json'
API = 'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live'
SCHED = 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&gameType=R,F,D,L,W'
SLIM = ('liveData,plays,allPlays,result,eventType,event,description,rbi,awayScore,homeScore,atBatIndex,'
        'about,atBatIndex,halfInning,inning,isComplete,batter,fullName,pitcher,matchup,batSide,'
        'pitchHand,code,runners,movement,start,end,outBase,isOut,playEvents,playId,details,type,count,outs,'
        'hitData,launchSpeed,launchAngle,totalDistance,trajectory,hardness,'
        'reviewDetails,isOverturned,reviewType')

HIT_TYPES = {'single', 'double', 'triple', 'home_run'}
FC_TYPES = {'fielders_choice', 'fielders_choice_out'}
OUT_TYPES = {'field_out', 'force_out', 'grounded_into_double_play', 'double_play', 'sac_fly',
             'sac_bunt', 'sac_fly_double_play', 'triple_play'}
OFFICIAL_PENDING_CODES = {'os_ruling_pending_primary', 'os_ruling_pending_prior'}
OFFICIAL_PENDING_TEXT = 'Official Scorer Ruling Pending'
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


def is_official_scoring_pending_event(candidate):
    """Match only the exact official eventTypes registry codes/description.

    Verified source: GET https://statsapi.mlb.com/api/v1/eventTypes. The codes may
    appear on an event's `details` or on `play.result`; no substring/text guess is used.
    """
    if not isinstance(candidate, dict):
        return False
    details = candidate.get('details') if isinstance(candidate.get('details'), dict) else {}
    result = candidate.get('result') if isinstance(candidate.get('result'), dict) else {}
    values = [details.get('eventType'), details.get('event'), details.get('description'),
              candidate.get('eventType'), candidate.get('event'), candidate.get('type'),
              result.get('eventType'), result.get('event'), result.get('description')]
    return any(isinstance(value, str) and
               (value in OFFICIAL_PENDING_CODES or value == OFFICIAL_PENDING_TEXT)
               for value in values)


def find_official_scoring_pending_play(play):
    """Return exact pending markers on one play, or None when no marker was observed."""
    if not isinstance(play, dict):
        return None
    events, codes = [], []
    for event in play.get('playEvents', []) or []:
        if not isinstance(event, dict):
            continue
        details = event.get('details') if isinstance(event.get('details'), dict) else {}
        values = [details.get('eventType'), details.get('event'), details.get('description'),
                  event.get('eventType'), event.get('event'), event.get('type')]
        if any(isinstance(value, str) and
               (value in OFFICIAL_PENDING_CODES or value == OFFICIAL_PENDING_TEXT)
               for value in values):
            events.append(event)
            for value in values:
                if isinstance(value, str) and value in OFFICIAL_PENDING_CODES and value not in codes:
                    codes.append(value)

    result = play.get('result') if isinstance(play.get('result'), dict) else {}
    result_values = [result.get('eventType'), result.get('event'), result.get('description')]
    at_result = any(isinstance(value, str) and
                    (value in OFFICIAL_PENDING_CODES or value == OFFICIAL_PENDING_TEXT)
                    for value in result_values)
    if at_result:
        for value in result_values:
            if isinstance(value, str) and value in OFFICIAL_PENDING_CODES and value not in codes:
                codes.append(value)
    if not events and not at_result:
        return None
    return {'pending_events': events, 'pending_codes': codes,
            'primary': 'os_ruling_pending_primary' in codes,
            'prior': 'os_ruling_pending_prior' in codes, 'at_result': at_result}


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
        self.event_model = m.get('event_type_model') or {}
        self.event_classes = list(self.event_model.get('classes', []))
        self.event_int = self.event_model.get('intercept', {})
        self.event_coef = self.event_model.get('coef', {})
        self.event_spec = self.event_model.get('feature_spec') or self.spec
        self.event_names = self.event_model.get('feature_names') or [f['name'] for f in self.event_spec]
        self.event_mean = self.event_model.get('scaler_mean', self.mean)
        self.event_scale = self.event_model.get('scaler_scale', self.scale)
        self.event_type_groups = self.event_model.get('event_type_groups', {})
        self.event_type_to_class = {code: label for label, codes in self.event_type_groups.items()
                                    for code in codes}

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

    def predict_event_state(self, state):
        """Return the separately trained 11-category event distribution and its top class."""
        if not self.event_classes:
            return {}, None
        x = [feature_value(f['type'], state) for f in self.event_spec]
        z = [(x[i] - self.event_mean[i]) / (self.event_scale[i] or 1.0)
             for i in range(len(x))]
        logits = {
            category: self.event_int[category] + sum(
                z[i] * self.event_coef[category][self.event_names[i]] for i in range(len(z)))
            for category in self.event_classes
        }
        max_logit = max(logits.values())
        exps = {category: math.exp(logits[category] - max_logit) for category in self.event_classes}
        total = sum(exps.values()) or 1.0
        probs = {category: value / total for category, value in exps.items()}
        top = max(probs, key=probs.get)
        return probs, top

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


def rbi_if_ruled(cls, run_scored, official_et, bases=(), outs=0):
    """Return a conditional RBI reminder, never a deterministic scorer decision.

    The eligibility tests below are the verbatim 2026 text of Rule 9.04 (Official Baseball Rules,
    printed page 115), quoted on docs/rules.html and linked to the official PDF there:

      (a)(1) the scorer credits an RBI for a run that scores "unaided by an error and as part of a
             play begun by the batter's safe hit ... sacrifice bunt, sacrifice fly, infield out or
             fielder's choice";
      (a)(3) and "when, before two are out, an error is made on a play on which a runner from third
             base ordinarily would score";
      (b)(1) no RBI "when the batter grounds into a force double play or a reverse-force double play".

    Two of (a)(3)'s conditions are observable in the feed (the out count and a runner on third).
    The third - that the runner "ordinarily would score" - is the scorer's counterfactual and is
    never in the feed, which is why this function states which rule applies and stops there.
    docs/rbi.html publishes the measured outcome of exactly this test on the collected window.
    """
    if not run_scored:
        return None
    if official_et == 'home_run':
        if cls == 'hit':
            return 'The official play is a home run (a hit); check the recorded RBI field for the official result.'
        return ('not a valid alternative: the official play is a home run; this feed row cannot model '
                'a counterfactual error or fielder\'s-choice ruling')
    if cls == 'error':
        if outs < 2 and '3B' in bases:
            return ('Rule 9.04(a)(3) can apply: fewer than two outs with a runner on third base. It '
                    'credits an RBI when that runner "ordinarily would score" - a counterfactual the '
                    'feed does not record, so this is not a ruling.')
        return ('Rule 9.04(a)(3) cannot apply: it requires fewer than two outs and a runner on third '
                'base. Rule 9.04(a)(1) requires the run to be "unaided by an error", so no RBI is '
                'credited for this error-dependent run.')
    if cls == 'fielders_choice':
        return ("A run-scoring fielder's choice is one of the plays Rule 9.04(a)(1) lists, so an RBI "
                'can be credited, subject to the Rule 9.04(b) exceptions and the scorer\'s judgment; '
                'this feed row does not settle any exception.')
    if cls == 'hit':
        return ('A run that scores on a hit is credited an RBI under Rule 9.04(a)(1), subject to the '
                "Rule 9.04(b) exceptions and the scorer's judgment; this feed row does not settle any "
                'exception.')
    return 'RBI depends on the official scoring rule for this play.'


def score_feed(feed, pk=None, scorer=None, meta=None):
    """Score supported batted balls and expose exact official scorer-pending markers."""
    sc = scorer or Scorer()
    plays = (((feed.get('liveData') or {}).get('plays') or {}).get('allPlays') or [])
    rows = []
    feed_url = API.format(pk=pk) if pk is not None else ''
    for pl in plays:
        if not isinstance(pl, dict):
            continue
        res = pl.get('result') if isinstance(pl.get('result'), dict) else {}
        et = res.get('eventType') or ''
        pending = find_official_scoring_pending_play(pl)
        pending_codes = pending['pending_codes'] if pending else []
        pending_primary = bool(pending and pending['primary'])
        pending_prior = bool(pending and pending['prior'])
        pending_kind = ('both' if pending_primary and pending_prior else
                        'primary' if pending_primary else 'prior' if pending_prior else
                        'description_only' if pending else '')
        events = pl.get('playEvents') if isinstance(pl.get('playEvents'), list) else []
        hd = next((e['hitData'] for e in events if isinstance(e, dict)
                   and isinstance(e.get('hitData'), dict)), None)
        model_type_known = et in sc.event_type_to_class if sc.event_type_to_class else macro_class(et) != 'other'
        # Include every exact scorer-pending marker even if Statcast has no vector. Also retain
        # completed supported outcomes with missing vectors so the feed can show their call honestly.
        if hd is None and not pending and not model_type_known:
            continue

        about = pl.get('about') if isinstance(pl.get('about'), dict) else {}
        matchup = pl.get('matchup') if isinstance(pl.get('matchup'), dict) else {}
        batter = matchup.get('batter') if isinstance(matchup.get('batter'), dict) else {}
        pitcher = matchup.get('pitcher') if isinstance(matchup.get('pitcher'), dict) else {}
        pid = next((e.get('playId') for e in reversed(events)
                    if isinstance(e, dict) and e.get('playId')), '')
        marker_text = OFFICIAL_PENDING_TEXT
        if pending:
            marker = pending['pending_events'][0] if pending['pending_events'] else None
            details = marker.get('details') if isinstance(marker, dict) else {}
            if isinstance(details, dict):
                marker_text = details.get('description') or details.get('event') or marker_text
            if pending['at_result']:
                marker_text = res.get('description') or res.get('event') or marker_text
        row = {
            'game_pk': pk, 'at_bat': about.get('atBatIndex'), 'official_feed_url': feed_url,
            'inning': about.get('inning'), 'half': about.get('halfInning'),
            'event_type': et,
            'official_call': 'pending' if pending or not et else macro_class(et),
            'status': ('official_scoring_pending' if pending else
                       'no_event_type_yet' if not et else 'no_vector'),
            'official_scoring_pending': bool(pending),
            'scoring_pending_kind': pending_kind,
            'scoring_pending_codes': ','.join(pending_codes),
            'scoring_pending_text': marker_text if pending else '',
            'prediction_available': False, 'prediction_unavailable_reason': '',
            'description': res.get('description') or res.get('event') or marker_text,
            'batter': batter.get('fullName', ''), 'pitcher': pitcher.get('fullName', ''),
            'away_score': res.get('awayScore'), 'home_score': res.get('homeScore'),
            'play_id': pid,
            'savant_url': f'https://baseballsavant.mlb.com/sporty-videos?playId={pid}' if pid else '',
        }
        # os_ruling_pending_prior is a base-running ruling, not the plate-appearance label. Only
        # a primary marker licenses applying the batted-ball classification model to a pending play.
        event_model_eligible = not pending or pending_primary
        if hd is None:
            if pending and pending_prior and not pending_primary:
                row['prediction_unavailable_reason'] = (
                    'The exact pending marker is for a prior base-running event, not a plate-appearance ruling.')
            elif not pending and not et:
                continue
            else:
                row['prediction_unavailable_reason'] = 'The feed has not supplied a Statcast hitData vector.'
            rows.append(row)
            continue

        need = ('launchSpeed', 'launchAngle', 'totalDistance')
        if not all(k in hd and hd[k] is not None and hd[k] != '' for k in need):
            row['prediction_unavailable_reason'] = 'The Statcast hitData vector is incomplete.'
            rows.append(row)
            continue
        if et and not pending and not model_type_known:
            row['prediction_unavailable_reason'] = (
                'This result.eventType is outside the represented training categories.')
            rows.append(row)
            continue
        if not event_model_eligible:
            row['prediction_unavailable_reason'] = (
                'The exact pending marker is for a prior base-running event, not a plate-appearance ruling.')
            rows.append(row)
            continue

        st = state_from_play(pl, hd)
        bases = sorted(st['bases'])
        run_scored = any(isinstance(r, dict) and isinstance(r.get('movement'), dict)
                         and r['movement'].get('end') == 'score' for r in pl.get('runners', []))
        p_err, probs, top = sc.predict_state(st)
        event_probs, event_top = sc.predict_event_state(st)
        rev = pl.get('reviewDetails') or next(
            (e['reviewDetails'] for e in events if isinstance(e, dict) and 'reviewDetails' in e), {})
        ruling_in = bool(et) and not pending
        row.update({
            'status': 'official_scoring_pending' if pending else
                      'scored' if ruling_in else 'no_event_type_yet',
            'prediction_available': True, 'prediction_unavailable_reason': '',
            'launch_speed': hd['launchSpeed'], 'launch_angle': hd['launchAngle'],
            'distance': hd['totalDistance'], 'trajectory': hd.get('trajectory', ''),
            'hardness': hd.get('hardness', ''), 'score_100': round(100 * p_err, 2),
            'p_hit': round(probs['hit'], 4), 'p_error': round(p_err, 4),
            'p_error_binary': round(p_err, 4), 'p_error_macro': round(probs['error'], 4),
            'p_fielders_choice': round(probs['fielders_choice'], 4), 'p_out': round(probs['out'], 4),
            'top_pick': top, 'top_prob': round(probs[top], 4),
            'event_top_pick': event_top,
            'event_top_prob': round(event_probs[event_top], 4) if event_top else None,
            'model_agrees_with_call': int(ruling_in and macro_class(et) in sc.classes
                                          and top == macro_class(et)),
            'runners_on': ','.join(bases) or '-', 'risp': int(bool(RISP_BASES & set(bases))),
            'outs_before': st['outs'], 'run_scored': int(run_scored),
            'rbi_official': res.get('rbi', 0), 'reviewed': int(bool(rev)),
            'review_overturned': rev.get('isOverturned', '') if isinstance(rev, dict) else '',
            'review_type': rev.get('reviewType', '') if isinstance(rev, dict) else '',
        })
        for category, probability in event_probs.items():
            row[f'event_p_{category}'] = round(probability, 4)
        if run_scored:
            # Rule 9.04 governs every run-scoring play — not only runners in scoring position.
            row['rbi_if_error'] = rbi_if_ruled('error', True, et, st['bases'], st['outs'])
            row['rbi_if_hit'] = rbi_if_ruled('hit', True, et, st['bases'], st['outs'])
            row['rbi_if_fc'] = rbi_if_ruled('fielders_choice', True, et, st['bases'], st['outs'])
        rows.append(row)
    if meta:
        for r in rows:
            r.update(meta)
    return rows


def scoring_observations(feed, pk=None, meta=None):
    """Compact official play states for pending-resolution and scoring-change comparisons.

    The values remain exactly as the API supplied them. They are not classifier features or
    inferred rulings.
    """
    plays = (((feed.get('liveData') or {}).get('plays') or {}).get('allPlays') or [])
    feed_url = API.format(pk=pk) if pk is not None else ''
    out = []
    for play in plays:
        if not isinstance(play, dict):
            continue
        result = play.get('result') if isinstance(play.get('result'), dict) else {}
        about = play.get('about') if isinstance(play.get('about'), dict) else {}
        matchup = play.get('matchup') if isinstance(play.get('matchup'), dict) else {}
        batter = matchup.get('batter') if isinstance(matchup.get('batter'), dict) else {}
        pitcher = matchup.get('pitcher') if isinstance(matchup.get('pitcher'), dict) else {}
        pending = find_official_scoring_pending_play(play)
        events = play.get('playEvents') if isinstance(play.get('playEvents'), list) else []
        play_id = next((e.get('playId') for e in reversed(events)
                        if isinstance(e, dict) and e.get('playId')), '')

        def event_reference(event):
            if not isinstance(event, dict):
                return ''
            if event.get('playId') is not None and event.get('playId') != '':
                return f"playId:{event['playId']}"
            if isinstance(event.get('index'), int):
                return f"index:{event['index']}"
            return ''

        pending_event_refs = []
        if pending:
            for event in pending['pending_events']:
                key = event_reference(event)
                if key:
                    pending_event_refs.append({'event_key': key,
                                               'play_id': str(event.get('playId', '')),
                                               'index': event.get('index')})
        event_states = []
        for event in events:
            key = event_reference(event)
            if not key:
                continue
            details = event.get('details') if isinstance(event.get('details'), dict) else {}
            event_states.append({
                'event_key': key,
                'event_type': details.get('eventType') or event.get('eventType') or '',
                'description': details.get('description') or '',
                'official_scoring_pending': is_official_scoring_pending_event(event),
            })
        observation = {
            'game_pk': pk, 'at_bat': about.get('atBatIndex'),
            'inning': about.get('inning'), 'half': about.get('halfInning'),
            'event_type': result.get('eventType') or '',
            'description': result.get('description') or result.get('event') or '',
            'official_scoring_pending': bool(pending),
            'pending_codes': pending['pending_codes'] if pending else [],
            'pending_kind': ('both' if pending and pending['primary'] and pending['prior'] else
                             'primary' if pending and pending['primary'] else
                             'prior' if pending and pending['prior'] else
                             'description_only' if pending else ''),
            'pending_text': OFFICIAL_PENDING_TEXT if pending else '',
            'pending_event_refs': pending_event_refs, 'event_states': event_states,
            'batter': batter.get('fullName', ''), 'pitcher': pitcher.get('fullName', ''),
            'matchup': (meta or {}).get('matchup', ''),
            'away_score': result.get('awayScore'), 'home_score': result.get('homeScore'),
            'play_id': play_id, 'official_feed_url': feed_url,
        }
        if meta:
            observation.update(meta)
        out.append(observation)
    return out


# ---------------------------------------------------------------- fetch helpers
def http_json(url, timeout=30):
    with urlopen(Request(url, headers={'User-Agent': 'LiveScoringErrors/1.0'}), timeout=timeout) as r:
        return json.load(r)


def fetch_feed(pk):
    return http_json(API.format(pk=pk) + '?fields=' + SLIM)


def eastern_game_day(now=None):
    """Today's official MLB schedule date in US Eastern time, including daylight-saving changes."""
    current = now or datetime.now(timezone.utc)
    return current.astimezone(ZoneInfo('America/New_York')).date().strftime('%m/%d/%Y')


def todays_games():
    d = eastern_game_day()
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
        print('  (no represented scoring events in this feed)')
        return
    if title:
        print(f'\n{title}')
    print(f"  {'AB':>3} {'feed state':<16} {'eventType':<22} {'SCORE/100':>9} "
          f"{'macro estimate':<24} {'event estimate':<25} {'EV/LA/dist':<19} "
          f"{'on':<7} {'o':>1} {'RBI':>3}  ok")
    for r in rows:
        pending = r.get('official_scoring_pending') is True
        if pending:
            state = f"PENDING/{r.get('scoring_pending_kind') or 'exact'}"
        elif r.get('status') == 'no_event_type_yet':
            state = 'NO eventType'
        elif r.get('prediction_available'):
            state = 'feed result'
        else:
            state = 'NO model vector'
        event_type = r.get('event_type') or '—'
        if not r.get('prediction_available'):
            print(f"  {str(r.get('at_bat') if r.get('at_bat') is not None else '—'):>3} "
                  f"{state:<16} {event_type:<22} {'--':>9} {'(no estimate)':<24} {'—':<25}")
            reason = r.get('prediction_unavailable_reason')
            if reason:
                print(f"      no score: {reason}")
            continue
        score = float(r['score_100'])
        macro = f"{r['top_pick']} ({100 * r['top_prob']:.1f}%)"
        detailed = f"{r.get('event_top_pick') or 'unavailable'}"
        if r.get('event_top_prob') is not None:
            detailed += f" ({100 * r['event_top_prob']:.1f}%)"
        vector = (f"{r.get('launch_speed', '—')}/{r.get('launch_angle', '—')}/"
                  f"{r.get('distance', '—')}")
        flag = ('Y' if r.get('model_agrees_with_call') else 'n') if r.get('status') == 'scored' else '-'
        print(f"  {str(r.get('at_bat') if r.get('at_bat') is not None else '—'):>3} "
              f"{state:<16} {event_type:<22} {score:>9.2f} {macro:<24} {detailed:<25} "
              f"{vector:<19} {r.get('runners_on', '—'):<7} {r.get('outs_before', '—'):>1} "
              f"{r.get('rbi_official', 0):>3}  {flag}")
        if pending:
            print('      estimate only; exact official-scorer pending marker observed, not a ruling')
    scored = [r for r in rows if r.get('status') == 'scored']
    if scored:
        n = len(scored)
        agree = sum(r['model_agrees_with_call'] for r in scored)
        err = [r for r in scored if r['official_call'] == 'error']
        caught = sum(r['model_agrees_with_call'] for r in err)
        print(f"  -- {n} resolved feed labels with predictions | macro top pick == feed class "
              f"{agree}/{n} ({100*agree/n:.1f}%) | errors {caught}/{len(err)} nominated 'error'")
    pending = [r for r in rows if r.get('official_scoring_pending') is True]
    unresolved = [r for r in rows if r.get('status') == 'no_event_type_yet'
                  and not r.get('official_scoring_pending')]
    if pending or unresolved:
        primary = sum(r.get('scoring_pending_kind') in ('primary', 'both') for r in pending)
        prior = sum(r.get('scoring_pending_kind') in ('prior', 'both') for r in pending)
        print(f"  -- exact official-scorer pending markers: {len(pending)} "
              f"(primary {primary}, prior {prior}); no result.eventType without marker: {len(unresolved)}")
    for r in rows:
        if r.get('reviewed') and r.get('review_overturned') is True:
            score_text = (f"model SCORE/100 {r['score_100']}" if r.get('prediction_available')
                          else 'no model score available')
            print(f"  ** challenge OVERTURNED on AB {r.get('at_bat', '—')} "
                  f"({r.get('review_type', '')}) — {score_text}; captured eventType "
                  f"'{r.get('event_type') or 'not supplied'}'")


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
            agreement = f'{agree}/{n} ({100 * agree / n:.1f}%)' if n else 'no resolved feed labels with predictions'
            print(f'\nTOTAL: {len(feeds)} games, {n} batted balls scored, '
                  f'model top pick == official call {agreement}')
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
