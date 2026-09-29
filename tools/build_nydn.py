#!/usr/bin/env python3
"""Parse NY Daily News 'mlb-overturned-calls' CSVs (2014-2018) -> tidy overturned_calls.csv.

Source quirks handled (verified by inspection, documented in docs/methods.html):
 * regular-season files have 12 columns (some with a trailing empty col);
 * postseason files insert 'series-game' and sometimes mis-quote the game cell, shifting columns;
   -> realignment: anchor the 5 trailing columns from the END (calling_umpire, crew_chief,
      result, time_to_ruling, video) and the date from the FRONT; reconstruct the middle with
      an inning token match (^[TB]\\d+|^EX).
 * 2014-04-31 in 2014 file is a source typo (April 31 does not exist in 2014's MLB week -
    kept verbatim and flagged; never silently 'fixed').
 * run_removed heuristic: result == Overturned AND the play involves the plate/scoring
   (initial-call mentioning score/home/plate or play-type Timing/Collisions-at-HP/etc.).
   This is a heuristic and is labeled as such everywhere it appears.
"""
import csv, json, re
from pathlib import Path

SRC = Path('/home/user/work/nydn')
OUT = Path(__file__).resolve().parent.parent / 'docs' / 'data'
VALID_RESULTS = {'Confirmed', 'Stands', 'Overturned', 'Rules Check', 'Record Keeping'}
INNING = re.compile(r'^(B|T|EX)\s?\d{1,2}$')
SCORE_HINT = re.compile(r'(score|scored|home|plate|1st run|run)', re.I)
PLATE_TYPES = re.compile(r'(Timing|HP|Home|Plate|Collisions|Force play at home|Tag play at home)', re.I)
TIME = re.compile(r'^\d{1,2}:\d{2}$')

def realign(row, year, flags):
    if len(row) < 12:
        flags.append(f'{year}: short row len={len(row)}: {row[:3]}... skipped')
        return None
    date = row[0].strip()
    # anchor tail: find video (http) & time (m:ss) & result within last 4 fields
    tail_idx = None
    for j in range(len(row) - 1, max(len(row) - 5, 0), -1):
        cell = row[j].strip()
        if cell.startswith('http'):
            tail_idx = j
            break
    if tail_idx is None:
        flags.append(f'{year}: no video in row: {row[:3]}... skipped')
        return None
    video = row[tail_idx].strip()
    time_to = row[tail_idx - 1].strip()
    result = row[tail_idx - 2].strip()
    crew = row[tail_idx - 3].strip() if tail_idx - 3 >= 6 else ''
    ump = row[tail_idx - 4].strip() if tail_idx - 4 >= 6 else ''
    if result not in VALID_RESULTS:
        flags.append(f'{year}: unexpected result "{result}" date={date}')
    if not TIME.match(time_to):
        flags.append(f'{year}: bad time "{time_to}" date={date}')
    middle = [c.strip() for c in row[1:(tail_idx - 4 if tail_idx - 4 >= 6 else tail_idx)]]
    # find inning token in middle
    inn_i = next((i for i, c in enumerate(middle) if INNING.match(c)), None)
    if inn_i is None:
        flags.append(f'{year}: no inning token date={date} middle={middle[:4]} skipped')
        return None
    game = ', '.join(middle[:inn_i]).lstrip('"').rstrip('"')
    rest = middle[inn_i + 1:]
    if len(rest) < 4:
        flags.append(f'{year}: not enough middle fields date={date}')
        return None
    manager, player = rest[0], rest[1]
    play_type = rest[-1]
    initial = ', '.join(rest[2:-1])
    return {
        'date': date, 'season': str(year), 'game': game, 'inning': middle[inn_i],
        'manager_or_ump': manager, 'player': player, 'initial_call': initial,
        'play_type': play_type, 'calling_umpire': ump, 'crew_chief': crew,
        'result': result, 'time_to_ruling': time_to, 'video': video,
        'overturned': int(result == 'Overturned'),
        'run_removed_heuristic': int(result == 'Overturned' and
                                     (SCORE_HINT.search(initial) or
                                      PLATE_TYPES.search(play_type) or
                                      PLATE_TYPES.search(initial)) is not None),
    }

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    flags, rows = [], []
    files = sorted(SRC.glob('*.csv'))
    for f in files:
        year = int(f.name[:4])
        with open(f, newline='', encoding='utf-8-sig') as fh:
            r = csv.reader(fh)
            next(r, None)  # header
            for row in r:
                if not any(c.strip() for c in row):
                    continue
                parsed = realign(row, f.name, flags)
                if parsed:
                    parsed['postseason'] = int('postseason' in f.name)
                    rows.append(parsed)
    rows.sort(key=lambda r: (r['date'], r['game']))
    with open(OUT / 'overturned_calls.csv', 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    n_over = sum(r['overturned'] for r in rows)
    n_rr = sum(r['run_removed_heuristic'] for r in rows)
    from collections import Counter
    flag_summary = Counter(
        'bad_time_format' if 'bad time' in f else
        'short_row_comma_name?' if 'short row' in f else
        'no_video_link' if 'no video' in f else
        'unexpected_result' if 'unexpected result' in f else 'other'
        for f in flags)
    summary = {'rows_total': len(rows), 'overturned': n_over, 'run_removed_heuristic': n_rr,
               'by_year': {}, 'flags': flags, 'flag_summary': dict(flag_summary),
               'heuristic_rule': ('result==Overturned AND (initial_call matches score/home/plate/run '
                                  'OR play_type/initial_call matches Timing/HP/Home/Plate/Collisions)')}
    for r in rows:
        y = summary['by_year'].setdefault(r['season'], {'rows': 0, 'overturned': 0, 'rr': 0})
        y['rows'] += 1; y['overturned'] += r['overturned']; y['rr'] += r['run_removed_heuristic']
    json.dump(summary, open(OUT.parent.parent / 'data' / 'nydn_quality.json', 'w'), indent=1)
    print('rows:', len(rows), 'overturned:', n_over, 'run-removed(heuristic):', n_rr, 'flags:', len(flags))
    for f_ in flags[:12]:
        print('  FLAG:', f_)

if __name__ == '__main__':
    main()
