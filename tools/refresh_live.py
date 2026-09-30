#!/usr/bin/env python3
"""Refresh the timestamped static fallback without replacing it with a partial/failed fetch.

The live page normally polls the public MLB Stats API in the visitor's browser. This scheduled
collector maintains a conservative GitHub Pages fallback for browsers/networks that cannot read the
API directly. A partial game-fetch failure is recorded in `docs/data/live_refresh_status.json` and the
last complete snapshot is retained.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import fetch_live  # noqa: E402

OUT_JSON = ROOT / 'docs' / 'data' / 'live_slate.json'
OUT_CSV = ROOT / 'docs' / 'data' / 'live_slate.csv'
STATUS = ROOT / 'docs' / 'data' / 'live_refresh_status.json'


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def previous_snapshot_time():
    try:
        return json.loads(OUT_JSON.read_text()).get('generated_utc', '')
    except (OSError, json.JSONDecodeError):
        return ''


def write_status(status):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, indent=1))


def stage_bytes(target, payload):
    """Write a same-filesystem temporary file so publication uses an atomic rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('wb', prefix=f'.{target.name}.', suffix='.tmp',
                            dir=target.parent, delete=False) as staged:
        staged.write(payload)
        staged.flush()
        os.fsync(staged.fileno())
        staged_path = Path(staged.name)
    try:
        os.chmod(staged_path, 0o644)
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise
    return staged_path


def install_snapshot(source_json, source_csv, out_json=OUT_JSON, out_csv=OUT_CSV):
    """Stage both artifacts first; publish JSON last so the page never sees a partial new snapshot."""
    staged = []
    try:
        staged_csv = stage_bytes(out_csv, source_csv.read_bytes())
        staged.append(staged_csv)
        staged_json = stage_bytes(out_json, source_json.read_bytes())
        staged.append(staged_json)
        os.replace(staged_csv, out_csv)
        staged.remove(staged_csv)
        os.replace(staged_json, out_json)
        staged.remove(staged_json)
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def main():
    attempted = utc_now()
    old_time = previous_snapshot_time()
    snapshot_updated = False
    new_snapshot_time = ''
    try:
        with TemporaryDirectory(prefix='.live-refresh-', dir=ROOT) as temp:
            temp_json = Path(temp) / 'live_slate.json'
            temp_csv = Path(temp) / 'live_slate.csv'
            rc = fetch_live.main(['--out', str(temp_json), '--csv-out', str(temp_csv)])
            if rc != 0 or not temp_json.exists():
                raise RuntimeError(f'fetch_live exited {rc} without a complete snapshot')
            doc = json.loads(temp_json.read_text())
            failures = doc.get('failures') or []
            if failures:
                write_status({
                    'ok': False, 'attempted_utc': attempted, 'exit_code': 1,
                    'error': 'One or more game feeds failed; the previous complete snapshot was retained.',
                    'failure_count': len(failures), 'failures': failures[:20],
                    'previous_snapshot_utc': old_time, 'snapshot_retained': True,
                })
                print('REFRESH PARTIAL; previous snapshot retained')
                return 1
            if not temp_csv.exists():
                raise RuntimeError('fetch_live did not produce its CSV companion')
            install_snapshot(temp_json, temp_csv)
            snapshot_updated = True
            new_snapshot_time = doc.get('generated_utc', '')
            write_status({
                'ok': True, 'attempted_utc': attempted,
                'snapshot_generated_utc': new_snapshot_time,
                'game_date': doc.get('game_date', ''), 'games': doc.get('summary', {}).get('games', 0),
                'unresolved_in_feed': doc.get('summary', {}).get('unresolved_in_feed', 0),
                'failure_count': 0, 'snapshot_retained': False,
            })
            print('REFRESH OK ' + json.dumps(doc.get('summary', {})))
            return 0
    except Exception as error:  # noqa: BLE001 - persist a visible failure for the static site
        write_status({
            'ok': False, 'attempted_utc': attempted,
            'error': f'{type(error).__name__}: {error}'[:400],
            'previous_snapshot_utc': old_time, 'snapshot_retained': not snapshot_updated,
            'snapshot_generated_utc': new_snapshot_time if snapshot_updated else '',
        })
        print(f'REFRESH FAILED {type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
