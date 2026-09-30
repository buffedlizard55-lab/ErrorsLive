#!/usr/bin/env python3
"""Measure the RBI stake the brief asks about, from the committed official data.

The brief's RBI question is empirical: "if the play is initially ruled an error the batter gets
no RBI if there is a runner on 2nd or 3rd. However if they rule it as a fielders choice or a hit,
there's a chance that the batter would be awarded an RBI."

That is a claim about the official statistic, so it can be *measured* rather than modelled: every
batted ball in the model window carries the feed's own `result.rbi` and its pre-pitch base/out
state. This tool tabulates the observed RBI rate per final macro-class and per base/out state,
with Wilson intervals, and lists every play in the window that was ruled an error *and* still
credited the batter with a run batted in — with the play id and the official feed URL, so each one
can be checked by hand.

What it deliberately does not do:
  * it does not predict an RBI. The counterfactual a scorer must judge ("ordinarily would score")
    is not in the feed;
  * it does not treat the window as a full-season or future-season rate;
  * it does not repair or impute anything. Rows are counted as the feed recorded them.

Offline and deterministic: reads docs/data/bip_official.csv (the CI collector's output) plus
docs/data/model.json for the window, writes docs/data/rbi_evidence.json.
"""
import csv, json, math, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'docs' / 'data'
BIP = DATA / 'bip_official.csv'
MODEL = DATA / 'model.json'
OUT = DATA / 'rbi_evidence.json'

CLASSES = ('hit', 'error', 'fielders_choice', 'out')
CLASS_LABEL = {'hit': 'Hit', 'error': 'Error', 'fielders_choice': "Fielder's choice",
               'out': 'Out (includes sac flies / infield outs)'}

# Verbatim 2026 Official Baseball Rules text, printed page 115. The identical sentence is quoted on
# docs/rules.html; tests/test_pipeline.py asserts the two copies match, so neither can drift.
RULE_9_04_A3 = ("when, before two are out, an error is made on a play on which a runner from third "
                "base ordinarily would score")
RULE_9_04_A1 = ("unaided by an error and as part of a play begun by the batter's safe hit (including "
                "the batter's home run), sacrifice bunt, sacrifice fly, infield out or fielder's choice")
RULE_9_04_B1 = "when the batter grounds into a force double play or a reverse-force double play"
RULE_PDF = 'https://mktg.mlbstatic.com/mlb/official-information/2026-official-baseball-rules.pdf'

# Base/out states that matter to the RBI rule. 9.04(a)(3) is the only route to an RBI on an
# error-ruled play, and it is gated on two observable conditions plus one unobservable judgment.
STATES = (
    ('third_fewer_than_two_outs', 'Runner on 3rd, fewer than 2 outs',
     lambda r: r['on_3b'] == '1' and int(r['outs_before']) < 2),
    ('third_two_outs', 'Runner on 3rd, 2 outs',
     lambda r: r['on_3b'] == '1' and int(r['outs_before']) == 2),
    ('second_only', 'Runner on 2nd only (no runner on 3rd)',
     lambda r: r['on_3b'] == '0' and r['on_2b'] == '1'),
    ('no_risp', 'No runner in scoring position',
     lambda r: r['on_3b'] == '0' and r['on_2b'] == '0'),
)


def load_csv(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def wilson(k, n, z=1.959963985):
    """95% Wilson score interval for a binomial proportion (the repo's standard interval)."""
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)]


def rate(rows, pred=lambda r: True):
    sub = [r for r in rows if pred(r)]
    n = len(sub)
    k = sum(1 for r in sub if int(r['rbi'] or 0) > 0)
    lo, hi = wilson(k, n)
    return {'n': n, 'rbi_plays': k, 'rate': round(k / n, 6) if n else 0.0,
            'rate_pct': round(100 * k / n, 2) if n else 0.0, 'ci95': [lo, hi]}


def model_window():
    """The window the model itself declares, read from model.json rather than typed here."""
    if not MODEL.exists():
        return [], ''
    note = json.loads(MODEL.read_text()).get('meta', {}).get('dataset_note', '')
    m = re.search(r'(\d{4}-\d{2}-\d{2})\s+through\s+(\d{4}-\d{2}-\d{2})', note)
    if not m:
        return [], 'window not stated in docs/data/model.json meta.dataset_note'
    return [m.group(1), m.group(2)], 'docs/data/model.json meta.dataset_note'


def main():
    if not BIP.exists():
        OUT.write_text(json.dumps({'available': False,
                                   'note': 'docs/data/bip_official.csv is not committed '
                                           '(CI collector output); no RBI evidence published.'},
                                  indent=1))
        print('no bip_official.csv — wrote available:false')
        return 0
    rows = [r for r in load_csv(BIP) if r.get('numeric_ok') == '1']
    window, window_src = model_window()

    by_class = []
    for c in CLASSES:
        st = rate(rows, lambda r, c=c: r['macro_class'] == c)
        st['macro_class'] = c
        st['label'] = CLASS_LABEL[c]
        by_class.append(st)

    by_state = []
    for key, label, pred in STATES:
        for c in CLASSES:
            st = rate(rows, lambda r, pred=pred, c=c: pred(r) and r['macro_class'] == c)
            st.update({'state': key, 'state_label': label, 'macro_class': c,
                       'label': CLASS_LABEL[c]})
            by_state.append(st)

    # The rule's own gate, tested against the data: an RBI on an error-ruled play should only ever
    # appear where 9.04(a)(3)'s observable conditions hold.
    def err(pred):
        return rate(rows, lambda r, pred=pred: r['macro_class'] == 'error' and pred(r))

    rule_checks = {
        'a3_conditions_met': {
            'label': 'Error-ruled plays with a runner on 3rd and fewer than 2 outs '
                     '(Rule 9.04(a)(3) conditions observable)',
            **err(lambda r: r['on_3b'] == '1' and int(r['outs_before']) < 2)},
        'two_outs': {
            'label': 'Error-ruled plays with a runner on 3rd and 2 outs '
                     '(9.04(a)(3) requires "before two are out")',
            **err(lambda r: r['on_3b'] == '1' and int(r['outs_before']) == 2)},
        'no_runner_on_third': {
            'label': 'Error-ruled plays with no runner on third base '
                     '(9.04(a)(3) names third base)',
            **err(lambda r: r['on_3b'] == '0')},
    }

    cases = []
    for r in sorted((r for r in rows if r['macro_class'] == 'error' and int(r['rbi'] or 0) > 0),
                    key=lambda r: (r['game_pk'], int(r['at_bat']))):
        cases.append({
            'game_pk': r['game_pk'], 'at_bat': r['at_bat'], 'play_id': r['play_id'],
            'event_type': r['event_type'], 'rbi': int(r['rbi']),
            'outs_before': int(r['outs_before']),
            'on_1b': int(r['on_1b']), 'on_2b': int(r['on_2b']), 'on_3b': int(r['on_3b']),
            'risp': int(r['risp']),
            'launch_speed': r['launch_speed'], 'launch_angle': r['launch_angle'],
            'trajectory': r['trajectory'],
            'feed_url': f"https://statsapi.mlb.com/api/v1.1/game/{r['game_pk']}/feed/live",
            'gameday_url': f"https://www.mlb.com/gameday/{r['game_pk']}",
            'savant_url': (f"https://baseballsavant.mlb.com/sporty-videos?playId={r['play_id']}"
                           if r['play_id'] else ''),
        })

    payload = {
        'available': True,
        'source': 'docs/data/bip_official.csv (tools/ingest_official.py, CI collector output)',
        'window': window,
        'window_source': window_src,
        'n_rows': len(rows),
        'n_quarantined_excluded': 'rows without a complete Statcast vector are excluded, exactly as '
                                  'the model excludes them (see docs/data/data_quality.json)',
        'question': ('Does an error-ruled play cost the batter an RBI, and would a hit or a '
                     "fielder's choice have earned one?"),
        'by_class': by_class,
        'by_state': by_state,
        'rule_checks': rule_checks,
        'error_rbi_cases': cases,
        'rule_basis': {
            'source': '2026 Official Baseball Rules (official PDF)',
            'pdf': RULE_PDF,
            'rule_9_04_page': 115,
            'a1_quote': RULE_9_04_A1,
            'a3_quote': RULE_9_04_A3,
            'b1_quote': RULE_9_04_B1,
            'verbatim_on_page': 'docs/rules.html',
        },
        'limits': [
            'These are observed rates inside one late-season window, not a forecast. They describe '
            'what the official scorer credited, not what a future scorer will credit.',
            'Rule 9.04(a)(3) also requires that the runner from third "ordinarily would score". '
            'That counterfactual is the scorer\'s judgment and is not in the feed, which is why '
            'only part of the eligible group earned an RBI.',
            'Small cells: the error class has 241 plays in the whole window, so its state-level '
            'rates carry wide Wilson intervals and are printed with them.',
            'An RBI credited on an error-ruled play is evidence about the rule, not evidence that '
            'the ruling was wrong or later changed.',
        ],
        'official_sources': [
            {'label': '2026 Official Baseball Rules (Rule 9.04, printed page 115)',
             'url': f'{RULE_PDF}#page=115'},
            {'label': 'MLB glossary: Error', 'url': 'https://www.mlb.com/glossary/standard-stats/error'},
            {'label': 'MLB glossary: Runs Batted In',
             'url': 'https://www.mlb.com/glossary/standard-stats/runs-batted-in'},
            {'label': 'MLB Official Scoring Changes',
             'url': 'https://www.mlb.com/official-information/scoring-changes'},
        ],
    }
    OUT.write_text(json.dumps(payload, indent=1))
    print(json.dumps({'rows': len(rows), 'window': window, 'classes': len(by_class),
                      'states': len(by_state), 'error_rbi_cases': len(cases)}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
