#!/usr/bin/env python3
"""Re-derive the three conditional RBI-note columns of docs/data/replays.csv.

Why this exists. `tools/ingest_official.py` writes `rbi_if_error` / `rbi_if_hit` / `rbi_if_fc`
into the review ledger as it collects each game, using the 2026 Rule 9.04 text quoted verbatim on
docs/rules.html. When that rule text was verified and the note wording was corrected, the columns
already committed in `docs/data/replays.csv` still carried the older, vaguer wording.

Those three columns are a pure function of other columns in the same row (`event_type`,
`macro_class`, `runs_by_movement`, `bases_before`, `outs_before`), so they can be re-derived
offline from the committed CSV instead of being re-fetched from the league. This tool does exactly
that, and nothing else:

  * it recomputes the note with the *current* `tools/live_score.py:rbi_if_ruled`, so the committed
    ledger always matches the code that would produce it today;
  * it touches only those three columns - the byte layout of every other column is preserved
    (asserted by tests/test_pipeline.py);
  * a row whose play scored no run keeps an empty note, exactly as the collector writes it;
  * it never invents, repairs or re-orders anything else.

Run from the repo root:  python3 tools/refresh_rbi_notes.py
"""
import csv, io, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / 'docs' / 'data' / 'replays.csv'

sys.path.insert(0, str(ROOT / 'tools'))
import live_score as ls                                                     # noqa: E402

NOTE_COLUMNS = {'error': 'rbi_if_error', 'hit': 'rbi_if_hit',
                'fielders_choice': 'rbi_if_fc'}
CLASS_OF_COLUMN = {v: k for k, v in NOTE_COLUMNS.items()}


def main():
    if not CSV.exists():
        print(f'{CSV} is not committed (CI collector output) — nothing to refresh')
        return 0
    raw = CSV.read_bytes()
    with open(CSV, newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        rows = list(reader)
    missing = [c for c in list(NOTE_COLUMNS.values()) + ['runs_by_movement', 'bases_before',
                                                         'outs_before', 'event_type']
               if c not in fields]
    if missing:
        print(f'committed ledger is missing columns {missing} — refusing to rewrite it')
        return 1

    changed = 0
    for r in rows:
        run_scored = int(r.get('runs_by_movement') or 0) > 0
        bases = (r.get('bases_before') or '-').split(',') if r.get('bases_before') != '-' else ()
        outs = int(r.get('outs_before') or 0)
        et = r.get('event_type') or ''
        for cls, col in NOTE_COLUMNS.items():
            note = ls.rbi_if_ruled(cls, run_scored, et, bases, outs) or ''
            if r.get(col, '') != note:
                changed += 1
            r[col] = note

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    out = buf.getvalue().encode('utf-8')
    if len(rows) != len(list(csv.DictReader(io.StringIO(out.decode('utf-8'))))):
        print('row count changed — refusing to write')
        return 1
    CSV.write_bytes(out)
    print(f'refreshed {changed} note cell(s) across {len(rows)} rows '
          f'({len(raw)} -> {len(out)} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
