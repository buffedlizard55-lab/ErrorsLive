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
        'video_status': (video or {}).get('video_status', ''),
        'video_checked': bool(video),
        'play_id_url': (f'https://baseballsavant.mlb.com/sporty-videos?playId={r["play_id"]}'
                        if r.get('play_id') else ''),
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


def write_ruling_changes():
    """The watcher observation-diff ledger and its explicit coverage/freshness metadata."""
    rows = load_csv(ROOT / 'data' / 'ingest' / 'ruling_changes.csv')
    snap = load_csv(ROOT / 'data' / 'ingest' / 'ruling_snapshot.csv')
    dates = [r.get('date', '') for r in snap if r.get('date')]
    status_path = ROOT / 'data' / 'ingest' / 'ruling_watch_status.json'
    watch_status = json.loads(status_path.read_text()) if status_path.exists() else {}
    unresolved = sum(r.get('status') == 'no_event_type_yet' for r in snap)
    (DATA / 'ruling_changes.json').write_text(json.dumps({
        'available': True,
        'source': 'data/ingest/ruling_changes.csv + ruling_snapshot.csv (tools/watch_rulings.py, CI)',
        'watched': {
            'plays': len(snap), 'window': [min(dates), max(dates)] if dates else [],
            'unresolved_in_feed': unresolved, 'status_ok': watch_status.get('ok'),
            'status_updated_utc': watch_status.get('detected_utc', ''),
            'mode': watch_status.get('mode', ''), 'status_error': watch_status.get('error', ''),
        },
        'changes': len(rows),
        'rows': [{k: r.get(k, '') for k in (
            'detected_utc', 'date', 'matchup', 'inning', 'half', 'at_bat', 'play_id', 'batter',
            'field', 'from', 'to', 'first_observation_status', 'was_unresolved_in_feed',
            'first_seen_utc', 'launch_speed', 'launch_angle', 'distance', 'trajectory', 'hardness',
            'bases_before', 'outs_before', 'rbi_from', 'rbi_to', 'rbi_note', 'reviewed',
            'overturned', 'feed_url', 'scoring_changes_url', 'savant_url')}
                 for r in rows[-200:]],
    }, indent=1))


def write_alerts():
    """One ledger of scoring-relevant observations, from the two machine sources that exist.

    The browser watches the live feed itself (docs/alerts.js + docs/alerts.html). This file is what
    the page shows when the browser cannot reach statsapi.mlb.com, and it is the auditable record of
    what the scheduled watcher actually saw:

      * `ci-watch`  — field-level changes recorded by tools/watch_rulings.py between captures
      * `ci-review` — overturned replay reviews the collector flagged as run-affected (heuristic),
                      each with its per-play video link and its Rule 9.04 conditional RBI note

    Nothing here is inferred: a row exists only because a captured value differed, or because the
    collector's own labelled heuristic flagged it. Both are stated on every row and on the page.
    """
    changes = load_csv(ROOT / 'data' / 'ingest' / 'ruling_changes.csv')
    snap = load_csv(ROOT / 'data' / 'ingest' / 'ruling_snapshot.csv')
    status_path = ROOT / 'data' / 'ingest' / 'ruling_watch_status.json'
    watch_status = json.loads(status_path.read_text()) if status_path.exists() else {}
    replays = load_csv(DATA / 'replays.csv') if (DATA / 'replays.csv').exists() \
        else load_csv(ROOT / 'data' / 'ingest' / 'replays.csv')
    videos = {(v['game_pk'], v['play_id']): v for v in load_csv(DATA / 'replay_videos.csv')}

    # Map a recorded field transition onto the alert vocabulary the browser engine uses. Several
    # fields move together for one play (a review posts `reviewed`, `review_type` and often
    # `overturned` in the same capture, and a settled ruling moves `status` with `event_type`), so
    # the redundant members are folded away rather than published as three separate alerts.
    by_play = {}
    for r in changes:
        by_play.setdefault((r.get('game_pk'), r.get('at_bat'), r.get('detected_utc')), set()).add(
            r.get('field', ''))
    alerts, suppressed = [], 0
    for r in changes:
        field = r.get('field', '')
        group = by_play.get((r.get('game_pk'), r.get('at_bat'), r.get('detected_utc')), set())
        if field == 'review_type' or (field == 'reviewed' and 'overturned' in group) \
                or (field == 'status' and 'event_type' in group):
            suppressed += 1
            continue
        if field == 'event_type':
            alert_type = 'decision_settled' if r.get('from') == '' else 'event_type_changed'
        elif field == 'rbi':
            alert_type = 'rbi_changed'
        elif field == 'overturned':
            alert_type = 'review_overturned'
        elif field == 'reviewed':
            alert_type = 'review_added'
        else:
            alert_type = 'feed_field_changed'
        severity = {'event_type_changed': 'critical', 'rbi_changed': 'critical',
                    'decision_settled': 'high', 'review_overturned': 'high',
                    'review_added': 'medium'}.get(alert_type, 'medium')
        alerts.append({
            'id': f"ci-watch|{r.get('game_pk')}|{r.get('at_bat')}|{field}|{r.get('detected_utc')}",
            'type': alert_type, 'severity': severity, 'at': r.get('detected_utc', ''),
            'observed_field': field,
            'source': 'ci-watch', 'game_pk': r.get('game_pk', ''), 'date': r.get('date', ''),
            'matchup': r.get('matchup', ''), 'inning': r.get('inning', ''), 'half': r.get('half', ''),
            'batter': r.get('batter', ''), 'play_id': r.get('play_id', ''),
            'field': field, 'from': r.get('from', ''), 'to': r.get('to', ''),
            'rbi_from': r.get('rbi_from', ''), 'rbi_to': r.get('rbi_to', ''),
            'was_unresolved_in_feed': int(r.get('was_unresolved_in_feed') or 0),
            'first_observation_status': r.get('first_observation_status', ''),
            'description': r.get('description', '')[:300],
            'note': (r.get('rbi_note') or '') + (
                ' The scheduled watcher saw this official-feed field differ between two captures; '
                'that is what is recorded here, not a claim about when or why the league changed it. '
                'A feed re-read can also carry backfilled metadata, so check the linked feed.'),
            'feed_url': r.get('feed_url', ''),
            'watch_url': r.get('savant_url', ''),
        })

    watch_updated = watch_status.get('detected_utc', '')
    affected = [r for r in replays
                if (r.get('review_overturned') == '1'
                    and (r.get('run_removed_heuristic') == '1' or r.get('run_removed_hard') == '1'))]
    for r in affected:
        v = videos.get((r['game_pk'], r['play_id']), {})
        alerts.append({
            'id': f"ci-review|{r['game_pk']}|{r['at_bat']}|run-affected",
            'type': 'run_at_stake', 'severity': 'high',
            # The collector observes the final reviewed state, so there is no honest
            # "when did this change" clock for these rows: the play date is the timeline.
            'at': '', 'observed_at': watch_updated,
            'source': 'ci-review', 'game_pk': r.get('game_pk', ''), 'date': r.get('date', ''),
            'matchup': r.get('matchup', ''), 'inning': r.get('inning', ''), 'half': r.get('half', ''),
            'batter': r.get('batter', ''), 'play_id': r.get('play_id', ''),
            'field': 'review', 'from': r.get('initial_call', ''), 'to': r.get('event_type', ''),
            'rbi_from': '', 'rbi_to': r.get('rbi_official', ''),
            'runs_by_movement': r.get('runs_by_movement', ''),
            'score_delta': r.get('score_delta', ''),
            'description': r.get('description', '')[:300],
            'note': ('Collector heuristic: an overturned review whose subject/text concerns a run. '
                     'Candidate, not a confirmed removed run. ' + (r.get('run_removed_rule') or '')),
            'rbi_rule_note': r.get('rbi_if_error', ''),
            'feed_url': f"https://statsapi.mlb.com/api/v1.1/game/{r['game_pk']}/feed/live",
            'watch_url': v.get('watch_url') or r.get('savant_url', ''),
            'video_url': v.get('mp4', ''),
        })

    alerts.sort(key=lambda a: (a['at'] or '', a['id']), reverse=True)
    # The page shows a bounded ledger. Keep every run-affected review row (the brief's target list)
    # and the most severe, most recent observations; never let a bulk backfill push the review rows
    # out of the published file.
    rank = {'critical': 0, 'high': 1, 'medium': 2, 'info': 3}
    ranked = sorted(alerts, key=lambda a: (rank.get(a['severity'], 3), a['at'] or '', a['id']),
                    reverse=False)
    published = ranked[:400]
    published.sort(key=lambda a: (a['at'] or a.get('observed_at') or '', a['id']), reverse=True)
    dates = [r.get('date', '') for r in snap if r.get('date')]
    unresolved = sum(r.get('status') == 'no_event_type_yet' for r in snap)
    counts = Counter(a['type'] for a in published)
    (DATA / 'alerts.json').write_text(json.dumps({
        'available': True,
        'generated_from': ['data/ingest/ruling_changes.csv', 'data/ingest/ruling_snapshot.csv',
                           'data/ingest/ruling_watch_status.json', 'docs/data/replays.csv',
                           'docs/data/replay_videos.csv'],
        'note': ('Scheduled observations only. MLB exposes no open scorer-decision queue: a changed '
                 'feed value is evidence that a captured field differed between two observations, '
                 'not that a decision was pending, when it was made, or why.'),
        'watcher': {
            'status_ok': watch_status.get('ok'), 'updated_utc': watch_status.get('detected_utc', ''),
            'mode': watch_status.get('mode', ''), 'error': watch_status.get('error', ''),
            'window': watch_status.get('window') or ([min(dates), max(dates)] if dates else []),
            'plays_observed': len(snap), 'unresolved_in_feed': unresolved,
            'games': watch_status.get('games'), 'changes_recorded': len(changes),
            'tool': watch_status.get('tool', ''), 'tool_version': watch_status.get('tool_version'),
            'fields_compared': watch_status.get('fields_compared') or [],
        },
        'counts': {
            'alerts': len(published), 'alerts_available': len(alerts),
            'ci_changes': len(changes), 'run_affected_reviews': len(affected),
            'ci_alerts_folded_away': suppressed, 'by_type': dict(sorted(counts.items())),
        },
        'alerts': published,
    }, indent=1))
    return {'alerts': len(alerts), 'published': len(published), 'ci_changes': len(changes),
            'affected': len(affected)}


def main():
    write_ingest_summary()
    write_ruling_changes()
    write_alerts()
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

    # What the feed actually exposes about reviews, per final call. These are umpire replay
    # reviews (and automated pitch-result challenges, counted separately above) as recorded in the
    # FINAL feed state; the feed publishes no pre-review ruling, so a rate here describes what the
    # league's own record shows, not a scorer's pending decision.
    profile = []
    for c in ('hit', 'error', 'fielders_choice', 'out', 'other'):
        sub = [r for r in reps if r['macro_class'] == c]
        if not sub:
            continue
        ov = sum(1 for r in sub if r['review_overturned'] == '1')
        profile.append({'macro_class': c, 'reviewed': len(sub), 'overturned': ov,
                        'overturned_rate': round(ov / len(sub), 6),
                        'with_runner_scoring': sum(1 for r in sub
                                                   if int(r['runs_by_movement'] or 0) > 0),
                        'overturned_with_runner_scoring': sum(
                            1 for r in sub if r['review_overturned'] == '1'
                            and int(r['runs_by_movement'] or 0) > 0)})

    replays_site = {
        'available': bool(reps),
        'source': 'docs/data/replays.csv (tools/ingest_official.py, CI)',
        'counts': {
            'reviewed_plays': len(reps),
            'overturned': len(overt),
            'overturned_with_runner_scoring': sum(1 for r in reps if r['review_overturned'] == '1'
                                                  and int(r['runs_by_movement'] or 0) > 0),
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
        'review_profile': profile,
        'review_profile_note': ('Per final call, as recorded in the captured final feed: how many '
                                'plays were reviewed, how many of those reviews the feed marks '
                                'overturned, and how many show a runner scoring. The feed carries no '
                                'pre-review ruling, so these are not overturn probabilities and not '
                                'a count of runs removed.'),
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
    # docs/data/games.csv is the 24-game audit sample; the full official collector ledger lives
    # under data/ingest/games.csv. Keep those scopes separate in published counts.
    gms = load_csv(ROOT / 'data' / 'ingest' / 'games.csv')
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
            'average_precision_grouped': model.get('primary', {}).get('cv_average_precision_grouped'),
            'average_precision_random_kfold': model.get('primary', {}).get('cv_average_precision_random_kfold'),
            'auc_gbm_grouped': model.get('primary', {}).get('cv_auc_gradient_boosting_grouped'),
            'brier': model.get('primary', {}).get('cv_brier_raw'),
            'brier_isotonic_nested': model.get('primary', {}).get('cv_brier_isotonic'),
            'top1': model.get('honesty', {}).get('oof_top1_accuracy'),
            'p_error_training_min_x100': model.get('honesty', {}).get('p_error_training_min_x100'),
            'p_error_training_max_x100': model.get('honesty', {}).get('p_error_training_max_x100'),
            'top_error_risk_bands_oof': model.get('honesty', {}).get('top_error_risk_bands_oof'),
            'error_recall': model.get('honesty', {}).get('oof_error_recall'),
            'error_nominated_top1': model.get('honesty', {}).get('oof_error_nominated_top1'),
        },
        'audit_sample': {'bip': len(bip), 'games': len({r.get('game_pk') for r in bip}),
                         'errors': sum(1 for r in bip if r['macro_class'] == 'error')},
        'ingested': {'games': len(gms),
                     'games_verified_vs_linescore': summary.get('summary', {}).get('games_verified_vs_linescore'),
                     'games_unverified': summary.get('summary', {}).get('games_unverified', [])},
        'model_dataset': {'bip': len(bip_off), 'games': model.get('meta', {}).get('games'),
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
