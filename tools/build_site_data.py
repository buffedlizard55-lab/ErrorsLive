#!/usr/bin/env python3
"""Turn the collected tables into the small files the pages actually fetch.

The collector (`tools/ingest_official.py`) runs in CI and writes large, complete audit tables —
`docs/data/replays.csv` is every reviewed play in the window. A browser should not download that to
render a list, and a page should never carry a hand-typed count that can drift from the data.

So this step derives compact, page-ready JSON from the committed tables, offline and deterministically:

  docs/data/replays_site.json   overturned reviews in the ingested window, with per-play video links,
                                the run-removal flags, and the RBI consequence of each candidate ruling
  docs/data/nydn_site.json      the 2014-2018 archive summary + its run-affected rows and link states
  docs/data/site_kpis.json      every headline number the pages print, in one place

If an input is missing (a fresh clone before the first CI collection), the output is written with
`available: false` and empty lists — never with invented numbers.
"""
import csv, json, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'docs' / 'data'
OUT_DIR = DATA


def load_csv(path):
    if not path.exists() or not path.stat().st_size:
        return []
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def compact_replay(r, video):
    """One page row: the call, the stakes, and the two clicks (watch / download)."""
    return {
        'date': r['date'], 'matchup': r['matchup'], 'game_pk': r['game_pk'], 'at_bat': r['at_bat'],
        'inning': r['inning'], 'half': r['half'], 'batter': r['batter'],
        'event_type': r['event_type'], 'macro_class': r['macro_class'],
        'rbi_official': r['rbi_official'], 'runs_by_movement': r['runs_by_movement'],
        'score_delta': r['score_delta'],
        'run_removed_hard': int(r['run_removed_hard'] or 0),
        'run_removed_heuristic': int(r['run_removed_heuristic'] or 0),
        'rule': r['run_removed_rule'],
        'review_type': r['review_type'], 'review_subject': r['review_subject'],
        'overturned': int(r['review_overturned'] or 0),
        'description': r['description'][:400],
        'play_id': r['play_id'],
        'watch_url': (video or {}).get('watch_url') or r.get('savant_url') or '',
        'mp4': (video or {}).get('mp4', ''),
        'mp4_source': (video or {}).get('mp4_source', ''),
        'video_checked': bool(video),
        'feed_url': r.get('feed_url') or
        f'https://statsapi.mlb.com/api/v1.1/game/{r["game_pk"]}/feed/live',
        'savant_game_url': f'https://baseballsavant.mlb.com/gamefeed?gamePk={r["game_pk"]}',
        'rbi_if_error': r.get('rbi_if_error', ''), 'rbi_if_hit': r.get('rbi_if_hit', ''),
        'rbi_if_fc': r.get('rbi_if_fc', ''),
    }


def write_ingest_summary():
    """Site-safe copy of the collector's report, so the pages work from a plain checkout.

    The collector writes the same structure in CI; this offline path exists so `tools/run_all.py`
    produces a complete site without the network. It copies counts, it never invents them.
    """
    rep = ROOT / 'data' / 'ingest' / 'ingest_report.json'
    if not rep.exists():
        (DATA / 'ingest_summary.json').write_text(json.dumps(
            {'available': False, 'note': 'no ingest report committed yet'}, indent=1))
        return
    log = json.loads(rep.read_text())
    (DATA / 'ingest_summary.json').write_text(json.dumps({
        'generated_utc': log.get('generated_utc', ''), 'windows': log.get('windows', []),
        'summary': log.get('summary', {}), 'endpoints': log.get('endpoints', {}),
        'run_removal_rule': log.get('run_removal_rule', ''),
        'flags': (log.get('flags') or [])[:50], 'flag_count': len(log.get('flags') or []),
        'game_failures': (log.get('game_failures') or [])[:20],
        'game_failure_count': len(log.get('game_failures') or []),
        'hard_run_removal_examples': log.get('hard_run_removal_examples', []),
        'source': 'data/ingest/ingest_report.json (written by tools/ingest_official.py in CI)',
    }, indent=1))


def main():
    write_ingest_summary()
    reps = load_csv(DATA / 'replays.csv')
    vids = {(v['game_pk'], v['play_id']): v for v in load_csv(DATA / 'replay_videos.csv')}
    summary = json.loads((DATA / 'ingest_summary.json').read_text()) \
        if (DATA / 'ingest_summary.json').exists() else {}
    overt = [r for r in reps if r['review_overturned'] == '1']
    affected = [r for r in reps
                if int(r['run_removed_hard'] or 0) or int(r['run_removed_heuristic'] or 0)]
    # the full rows are only for plays that removed a run (the brief's target list); everything else
    # is an index, so a browser downloads ~1 MB instead of the 8 MB audit ledger
    rows = [compact_replay(r, vids.get((r['game_pk'], r['play_id']))) for r in affected]
    index = [{'d': r['date'], 'm': r['matchup'], 'g': r['game_pk'], 'n': r['inning'],
              'h': (r['half'] or '')[:3], 'b': r['batter'], 't': r['review_type'],
              's': r['review_subject'], 'c': r['macro_class'],
              'p': r['play_id'], 'v': int(r['run_removed_heuristic'] or 0)}
             for r in overt]

    replays_site = {
        'available': bool(reps),
        'source': 'docs/data/replays.csv (tools/ingest_official.py, CI)',
        'counts': {
            'reviewed_plays': len(reps),
            'overturned': len(overt),
            'run_removed_hard': sum(1 for r in reps if r['run_removed_hard'] == '1'),
            'run_removed_heuristic': sum(1 for r in reps if r['run_removed_heuristic'] == '1'),
            'with_watch_link': sum(1 for x in rows if x['watch_url']),
            'with_mp4': sum(1 for x in rows if x['mp4']),
            'pitch_challenges': sum(1 for r in reps if r.get('is_pitch_challenge') == '1'),
            'replay_reviews': sum(1 for r in reps if r.get('is_pitch_challenge') == '0'),
            'window': (summary.get('summary', {}).get('dates') or ['', '']),
        },
        'subjects': dict(Counter(r['review_subject'] or 'unstated' for r in reps).most_common()),
        'overturned_index': index,
        'overturned_index_note': ('compact index of every overturned review in the window: '
                                  'd=date m=matchup g=game_pk n=inning h=half b=batter t=review type '
                                  's=subject c=final call p=play id v=run-removed heuristic'),
        'review_types': dict(Counter(r['review_type'] for r in reps if r['review_type']).most_common()),
        'rows': rows,
    }
    (OUT_DIR / 'replays_site.json').write_text(json.dumps(replays_site, separators=(',', ':')))

    nydn = load_csv(DATA / 'overturned_calls.csv')
    links = load_csv(DATA / 'nydn_links.csv')
    link_by_key = {(r['date'], r['game'], r['player'], r['video']): r for r in links}

    def nydn_row(r):
        """Columns are the archive's own (verified against docs/data/overturned_calls.csv):
        it records the call, the play type and a run-removal heuristic — it has no run/out totals."""
        lk = link_by_key.get((r['date'], r['game'], r['player'], r['video']), {})
        return {
            'date': r['date'], 'season': r['season'], 'game': r['game'],
            'play_type': r['play_type'], 'player': r['player'], 'inning': r['inning'],
            'initial_call': r['initial_call'], 'result': r['result'],
            'manager_or_ump': r['manager_or_ump'], 'calling_umpire': r['calling_umpire'],
            'crew_chief': r['crew_chief'], 'time_to_ruling': r['time_to_ruling'],
            'postseason': int(r['postseason'] or 0),
            'run_removed': int(r['run_removed_heuristic'] or 0),
            'run_removed_heuristic': int(r['run_removed_heuristic'] or 0),
            'overturned': int(r['overturned'] or 0), 'video': r['video'],
            'link_status': lk.get('status', ''), 'link_final': lk.get('final_url', ''),
            'link_error': lk.get('error', ''),
            'film_room': 'https://www.mlb.com/video/?q=' + (
                f"{r['game'].replace(' ', '+')}+{r['player'].replace(' ', '+')}"),
            'source_file': r.get('source_file', ''),
        }

    nydn_affected = [nydn_row(r) for r in nydn
                     if r['overturned'] == '1' and r['run_removed_heuristic'] == '1']
    nydn_site = {
        'available': bool(nydn),
        'source': 'docs/data/overturned_calls.csv (github.com/nydailynews/mlb-overturned-calls, vendored)',
        'counts': {
            'rows': len(nydn),
            'overturned': sum(1 for r in nydn if r['overturned'] == '1'),
            'run_removed': len(nydn_affected),
            'seasons': sorted({r['season'] for r in nydn}),
            'links_checked': len(links),
            'links_ok': sum(1 for r in links if str(r.get('status')) == '200'),
            'links_dead': sum(1 for r in links if str(r.get('status')) not in ('200', '')),
            'links_unknown': sum(1 for r in links if str(r.get('status')) == ''),
        },
        'by_season': {s: sum(1 for r in nydn if r['season'] == s) for s in
                      sorted({r['season'] for r in nydn})},
        'rows': nydn_affected,
    }
    (OUT_DIR / 'nydn_site.json').write_text(json.dumps(nydn_site, separators=(',', ':')))

    model = json.loads((DATA / 'model.json').read_text()) if (DATA / 'model.json').exists() else {}
    bip = load_csv(DATA / 'bip.csv')
    bip_off = load_csv(DATA / 'bip_official.csv')
    gms = load_csv(DATA / 'games.csv')
    kpis = {
        'model': {
            'dataset': model.get('meta', {}).get('dataset'),
            'n_bip': model.get('meta', {}).get('n_bip'),
            'n_model': model.get('meta', {}).get('n_model'),
            'n_error': model.get('meta', {}).get('n_error'),
            'error_rate_pct': model.get('meta', {}).get('error_rate_pct'),
            'games': model.get('meta', {}).get('games'),
            'auc_grouped': model.get('primary', {}).get('cv_auc'),
            'auc_ci95': model.get('primary', {}).get('cv_auc_ci95'),
            'auc_random_kfold': model.get('primary', {}).get('cv_auc_random_kfold'),
            'auc_gbm_grouped': model.get('primary', {}).get('cv_auc_gradient_boosting_grouped'),
            'brier': model.get('primary', {}).get('cv_brier_raw'),
            'top1': model.get('honesty', {}).get('oof_top1_accuracy'),
            'p_error_max_x100': model.get('honesty', {}).get('p_error_observed_max_x100'),
            'error_recall': model.get('honesty', {}).get('oof_error_recall'),
            'error_nominated_top1': model.get('honesty', {}).get('oof_error_nominated_top1'),
        },
        'audit_sample': {'bip': len(bip), 'games': len({r.get('game_pk') for r in bip}),
                         'errors': sum(1 for r in bip if r['macro_class'] == 'error')},
        'ingested': {'bip': len(bip_off), 'games': len(gms),
                     'errors': sum(1 for r in bip_off if r['macro_class'] == 'error'),
                     'fielders_choice': sum(1 for r in bip_off if r['macro_class'] == 'fielders_choice')},
        'replays': replays_site['counts'],
        'nydn': nydn_site['counts'],
        'ingest': summary.get('summary', {}),
        'ingest_meta': {k: summary.get(k) for k in ('generated_utc', 'windows', 'run_removal_rule')},
    }
    (OUT_DIR / 'site_kpis.json').write_text(json.dumps(kpis, indent=1))
    print(json.dumps({'replays_site': len(rows), 'nydn_site': len(nydn_affected),
                      'kpis': list(kpis)}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
