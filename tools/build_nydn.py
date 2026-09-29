#!/usr/bin/env python3
"""Parse NY Daily News 'mlb-overturned-calls' CSVs (2014-2018) -> tidy overturned_calls.csv.

Inputs are VENDORED in the repo at data/source/nydn/ (byte-identical copies of
github.com/nydailynews/mlb-overturned-calls @ master, data/*.csv) so the build is
reproducible offline; point NYDN_SRC elsewhere to rebuild from a fresh clone.

Every source quirk below was found by reading the vendored files line by line, not by guessing.
Each is documented with its real count in docs/methods.html and in data/nydn_quality.json.

QUOTES (271 rows were being lost before this was found):
 * 206 lines in 2014-mlb-replays.csv open with a stray double quote and never close it, so a plain
   csv.reader merged each of those records into the previous row and dropped it.
 * 65 postseason lines (2015: 29, 2016: 36) open a quoted cell that is never closed; same effect.
   -> both are repaired before parsing, losslessly: a delimiter character is removed, no data is.

COLUMNS:
 * regular-season files have 12 columns (some with a trailing empty column);
 * postseason files insert a 'series-game' column and sometimes mis-quote the game cell, which
   shifts the leading columns;
   -> realignment: anchor the 5 trailing columns from the END (calling_umpire, crew_chief,
      result, time_to_ruling, video) and the date from the FRONT; reconstruct the middle with
      an inning token match (^[TB]\\d+|^EX).

DATES (2,109 rows normalised, 5 deliberately not):
 * "2014-4-1"  -> zero-padded to "2014-04-01"      (2,109 rows)
 * '2018-05-29"' -> stray trailing quote removed    (279 rows)
 * "Sep. 1" / "Sat. July 23" -> resolved to ISO using the year of the file the row was
   published in, never guessed from the row; a prose date in an off-season month is left
   verbatim because the year would be ambiguous                                (234 rows)
 * 2014-04-31 (5 rows, the first data block of the 2014 file) is a source typo - April 31st does
   not exist and the archive gives nothing that would say whether it meant Mar 31 or Apr 30.
   These are published exactly as written and flagged. They are NEVER "fixed".
 * date_raw always holds the source string; date is ISO only where the source proves it.

DROPPED:
 * one junk line (2016-mlb-replays.csv:1501, ",,,,,,,,,,Time from,,") is a fragment of a re-inserted
   header, not a record. Flagged, then dropped - which is why 6,362 source lines yield 6,361 rows.
 * rows with an empty video cell are KEPT with an empty video (56 of them). A missing link is not a
   missing record; dropping those rows silently deleted 57 real replay decisions.

run_removed heuristic: result == Overturned AND the play involves the plate/scoring
   (initial-call mentioning score/home/plate or play-type Timing/Collisions-at-HP/etc.).
   This is a heuristic and is labeled as such everywhere it appears.
"""
import csv, datetime, json, os, re, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get('NYDN_SRC', ROOT / 'data' / 'source' / 'nydn'))
OUT = ROOT / 'docs' / 'data'
VALID_RESULTS = {'Confirmed', 'Stands', 'Overturned', 'Rules Check', 'Record Keeping'}
INNING = re.compile(r'^(B|T|EX)\s?\d{1,2}$')
SCORE_HINT = re.compile(r'(score|scored|home|plate|1st run|run)', re.I)
PLATE_TYPES = re.compile(r'(Timing|HP|Home|Plate|Collisions|Force play at home|Tag play at home)', re.I)
TIME = re.compile(r'^\d{1,2}:\d{2}$')

MONTHS = {m_: i for i, m_ in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

MLB_MONTHS = set(range(3, 12))   # Mar-Nov: months an MLB game is always in its own calendar year

def norm_date(raw, flags, file_year):
    """Normalise a source date to ISO YYYY-MM-DD. Never invent a calendar date.

    Four real source formats are handled, each losslessly and each counted:
      1. "2018-05-29"            - already ISO
      2. "2014-4-1"              - ISO with non-padded components (the 2014 spring rows)
      3. '2018-05-29"'           - ISO with a stray trailing quote (279 rows in the 2018 file)
      4. "Sep. 1" / "Sat. July 23" - a 2014 block written in prose form; the year comes from the
         file the row was read out of, never guessed from the row itself
    The original string is always preserved in date_raw. A date that is not a real calendar day
    (the archive's 2014-04-31 - April 31st does not exist) is left exactly as published and
    flagged, because "fixing" it would mean inventing a game that never happened.
    """
    txt = raw.strip()
    if txt != raw or txt.strip('"') != txt:
        flags.append(f'stray quote/whitespace in date {raw!r} stripped')
    txt = txt.strip('"').strip()
    m = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', txt)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        mm = re.fullmatch(r'(?:[A-Za-z]{3,9}\.?\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})', txt)
        if not mm or mm.group(1)[:3].lower() not in MONTHS:
            flags.append(f'unrecognised date format {txt!r} kept verbatim')
            return txt, False
        mo, d = MONTHS[mm.group(1)[:3].lower()], int(mm.group(2))
        if mo not in MLB_MONTHS:
            # The row only gives a month; taking the file's year would be a guess for an
            # off-season month, so it stays verbatim and is flagged instead.
            flags.append(f'prose date {txt!r} in off-season month {mo} - year ambiguous, kept verbatim')
            return txt, False
        flags.append(f'prose date {txt!r} resolved to ISO using the file year {file_year}')
        return _finish_iso(txt, file_year, mo, d, flags)
    return _finish_iso(txt, y, mo, d, flags)


def _finish_iso(txt, y, mo, d, flags):
    iso = f'{y:04d}-{mo:02d}-{d:02d}'
    try:
        datetime.date(y, mo, d)
    except ValueError:
        flags.append(f'impossible calendar date {iso} kept verbatim (never repaired)')
        return iso, False
    if iso != txt:
        flags.append(f'date zero-padded {txt!r} -> {iso}')
    return iso, iso != txt


def realign(row, year, flags):
    if sum(1 for c in row if c.strip()) <= 2:
        # 2016-mlb-replays.csv line 1501 is a fragment of a re-inserted header
        # (",,,,,,,,,,Time from,,") - not a record. Flagged, then dropped.
        flags.append(f'{year}: junk mid-file line {row} - not a record, skipped')
        return None
    if len(row) < 12:
        flags.append(f'{year}: short row len={len(row)}: {row[:3]}... skipped')
        return None
    date_raw = row[0].strip()
    date, date_repaired = norm_date(date_raw, flags, year)
    # Anchor the tail from the END: video, time-to-ruling, result, crew chief, calling umpire.
    # The archive sometimes shifts the leading columns (postseason files insert a series-game
    # column), but the last five columns are always in this order.
    tail_idx = next((j for j in range(len(row) - 1, max(len(row) - 6, 0), -1)
                     if row[j].strip().startswith('http')), None)
    if tail_idx is None:
        # The archive leaves the video cell blank on 57 rows. A missing link is not a missing
        # record, so the row is KEPT with an empty video and flagged - dropping it would silently
        # delete 57 real replay decisions from the published dataset.
        flags.append(f'{year}: no video link for {row[1]!r} - row kept, video cell left empty')
        video = ''
        time_to = row[-2].strip()
        result = row[-3].strip()
        crew = row[-4].strip() if len(row) >= 4 else ''
        ump = row[-5].strip() if len(row) >= 5 else ''
    else:
        video = row[tail_idx].strip()
        time_to = row[tail_idx - 1].strip()
        result = row[tail_idx - 2].strip()
        crew = row[tail_idx - 3].strip() if tail_idx - 3 >= 0 else ''
        ump = row[tail_idx - 4].strip() if tail_idx - 4 >= 0 else ''
    if result not in VALID_RESULTS:
        flags.append(f'{year}: unexpected result "{result}" date={date}')
    if not TIME.match(time_to):
        flags.append(f'{year}: bad time "{time_to}" date={date}')
    cut = tail_idx - 4 if tail_idx is not None and tail_idx - 4 >= 0 else len(row) - 5
    middle = [c.strip() for c in row[1:cut if cut > 0 else len(row)]]
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
        'date': date, 'date_raw': date_raw, 'date_repaired': int(date_repaired),
        'season': str(year), 'game': game, 'inning': middle[inn_i],
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
    if not files:
        sys.exit(f'no source CSVs in {SRC} (set NYDN_SRC to a clone of '
                 f'github.com/nydailynews/mlb-overturned-calls/data)')
    for f in files:
        year = int(f.name[:4])
        with open(f, newline='', encoding='utf-8-sig') as fh:
            # 206 lines in the 2014 file open with a stray double quote and never close it, so a
            # naive csv reader swallows each following record into the previous one. The rest of
            # those lines are well-formed, so the leading quote is stripped here - losslessly,
            # and counted. Nothing else about the line is touched.
            # Quote hygiene. The archive quotes some cells it did not need to, and in three
            # places the quoting is broken such that a plain csv reader loses rows. Each repair
            # below is lossless - it removes a delimiter character, never data - and is counted.
            DATEISH = re.compile(r'^"?\d{4}-\d{1,2}-\d{1,2}"?$')
            body, quote_fixes = [], Counter()
            for line in fh:
                if line.startswith('"') and not DATEISH.match(line[1:line.find(',') + 1] or line[1:]):
                    line = line[1:]                     # 207 lines in the 2014 file open with a
                    quote_fixes['spurious leading quote'] += 1
                if line.count('"') % 2 and line.startswith('"'):
                    pass
                while True:                            # an opening quote with no partner swallows
                    o = [i for i, c in enumerate(line)     # the rest of the line into one field
                         if c == '"' and i and line[i - 1] == ',' and '"' not in line[i + 1:]]
                    if not o:
                        break
                    line = line[:o[0]] + line[o[0] + 1:]
                    quote_fixes['unterminated opening quote'] += 1
                body.append(line)
            if quote_fixes:
                flags.append(f'{f.name}: quote repairs ' +
                             ', '.join(f'{v} x {k}' for k, v in sorted(quote_fixes.items())) +
                             ' - the CSV reader was otherwise dropping or swallowing these rows')
            r = csv.reader(body)
            next(r, None)  # header
            for row in r:
                if not any(c.strip() for c in row):
                    continue
                parsed = realign(row, year, flags)   # year, not the filename (bug fixed 2026-09-29)
                if parsed:
                    parsed['postseason'] = int('postseason' in f.name)
                    parsed['source_file'] = f.name
                    rows.append(parsed)
    rows.sort(key=lambda r: (r['date'], r['date_raw'], r['game']))
    with open(OUT / 'overturned_calls.csv', 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    n_over = sum(r['overturned'] for r in rows)
    n_rr = sum(r['run_removed_heuristic'] for r in rows)
    CATEGORIES = [
        ('date_prose_resolved',        'prose date '),
        ('quote_repair',               'quote repair'),        # "Sep. 1"  -> 2014-09-01
        ('date_stray_quote_stripped', 'stray quote/'),
        ('date_zero_padded',           'date zero-padded'),
        ('date_impossible_kept',       'impossible calendar date '),
        ('date_unrecognised_kept',     'unrecognised date format '),
        ('date_year_ambiguous_kept',   'year ambiguous'),

        ('bad_time_format', 'bad time'),
        ('short_row_comma_name?', 'short row'),
        ('no_video_link', 'no video'),
        ('junk_mid_file_line', 'junk mid-file line'),
        ('unexpected_result', 'unexpected result'),
    ]
    def categorise(f):
        for name, needle in CATEGORIES:
            if needle in f:
                return name
        return 'other'
    flag_summary = Counter(categorise(f) for f in flags)
    impossible = sorted({m.group(1) for m in
                         (re.search(r'impossible calendar date (\S+)', f) for f in flags) if m})
    summary = {'rows_total': len(rows), 'overturned': n_over, 'run_removed_heuristic': n_rr,
               'date_repaired_rows': sum(r['date_repaired'] for r in rows),
               'dates_kept_verbatim': sum(1 for r in rows
                                         if r['date'] in set(impossible)
                                         or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['date'])),
               'impossible_dates_kept_verbatim': impossible,
               'by_year': {}, 'flags': flags, 'flag_summary': dict(sorted(flag_summary.items())),
               'repair_policy': ('date_raw is always the source string; date is ISO only when the '
                                 'source proves it. Zero-padding, stray quotes and prose dates are '
                                 'lossless and applied; an impossible calendar date is NEVER '
                                 'altered, because the correct day cannot be derived from the source.'),
               'heuristic_rule': ('result==Overturned AND (initial_call matches score/home/plate/run '
                                  'OR play_type/initial_call matches Timing/HP/Home/Plate/Collisions)')}
    for r in rows:
        y = summary['by_year'].setdefault(r['season'], {'rows': 0, 'overturned': 0, 'rr': 0})
        y['rows'] += 1; y['overturned'] += r['overturned']; y['rr'] += r['run_removed_heuristic']
    json.dump(summary, open(ROOT / 'data' / 'nydn_quality.json', 'w'), indent=1)
    # A compact, site-safe copy so every number on the published page is the build's own number
    # rather than a hand-typed one that can drift.
    json.dump({k: v for k, v in summary.items() if k not in ('flags', 'by_year')},
              open(OUT / 'nydn_summary.json', 'w'), indent=1)
    print('source:', SRC)
    print('rows:', len(rows), 'overturned:', n_over, 'run-removed(heuristic):', n_rr, 'flags:', len(flags))
    print('date repairs:', summary['date_repaired_rows'],
          '| kept verbatim:', summary['dates_kept_verbatim'], summary['impossible_dates_kept_verbatim'])
    print('seasons:', sorted({r['season'] for r in rows}))
    for f_ in flags[:12]:
        print('  FLAG:', f_)

if __name__ == '__main__':
    main()
