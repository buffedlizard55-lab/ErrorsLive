#!/usr/bin/env python3
"""Reproducibility gate: did the rebuild from source reproduce the committed artifacts?

Everything this project publishes is derived, and CI rebuilds all of it on every push. Two different
kinds of artifact need two different checks, and blurring them would either hide a real drift or fail
on noise:

* **Derived tables** (CSVs and the page JSON built by pure Python) must be **byte-identical**. Any
  difference is a real defect: a hand-edited number, a non-deterministic sort, a changed rule.
* **`docs/data/model.json`** is the output of a numerical solver. Its last digits depend on the BLAS
  implementation that ships with the wheel (observed: scikit-learn's multiclass lbfgs ended 3e-9 apart
  between the dev machine and the GitHub runner, with identical inputs and identical package versions).
  Byte-equality there would be a false alarm, so model.json is compared **numerically** with a stated
  tolerance (1e-6, i.e. six orders of magnitude looser than the observed noise and far tighter than any
  material difference in a coefficient), and any *structural* change — a new field, a missing feature,
  a changed CV number — is an error regardless of size.

Both checks report through GitHub annotations as well as stdout, because log download through the
REST API is unreliable for some clients and an unexplained failure is worthless.

Usage: python3 tools/check_repro.py [--ref HEAD]
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BYTE_EXACT_PATHS = ('docs/data', 'data/data_quality.json', 'data/nydn_quality.json')
TOLERATED = {'docs/data/model.json': 1e-6}
TOL = 1e-6


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True).stdout


def annotate(level, title, message):
    one_line = ' '.join(str(message).split())
    print(f'::{level} title={title}::{one_line[:900]}')


def compare_numeric(committed, rebuilt, path, tol=TOL):
    """Recursive numeric comparison. Structural differences always fail; numbers may differ by tol."""
    problems = []
    if isinstance(committed, dict):
        if not isinstance(rebuilt, dict):
            return [f'{path}: type changed ({type(committed).__name__} -> {type(rebuilt).__name__})']
        missing = sorted(set(committed) - set(rebuilt))
        added = sorted(set(rebuilt) - set(committed))
        if missing:
            problems.append(f'{path}: missing keys {missing[:6]}')
        if added:
            problems.append(f'{path}: unexpected new keys {added[:6]}')
        for k in committed:
            if k in rebuilt:
                problems += compare_numeric(committed[k], rebuilt[k], f'{path}.{k}', tol)
        return problems
    if isinstance(committed, list):
        if not isinstance(rebuilt, list):
            return [f'{path}: type changed (list -> {type(rebuilt).__name__})']
        if len(committed) != len(rebuilt):
            return [f'{path}: length changed ({len(committed)} -> {len(rebuilt)})']
        for i, (a, b) in enumerate(zip(committed, rebuilt)):
            problems += compare_numeric(a, b, f'{path}[{i}]', tol)
        return problems
    if isinstance(committed, bool) or isinstance(rebuilt, bool):
        return [] if committed == rebuilt else [f'{path}: {committed} -> {rebuilt}']
    if isinstance(committed, (int, float)) and isinstance(rebuilt, (int, float)):
        if abs(committed - rebuilt) <= tol * max(1.0, abs(committed)):
            return []
        return [f'{path}: {committed} -> {rebuilt} (beyond {tol:g})']
    if committed != rebuilt:
        return [f'{path}: {str(committed)[:60]!r} -> {str(rebuilt)[:60]!r}']
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ref', default='HEAD', help='git ref holding the committed artifacts')
    a = ap.parse_args()

    changed = [l for l in git('diff', '--name-only', a.ref, '--', *BYTE_EXACT_PATHS).split('\n') if l]
    hard = [f for f in changed if f not in TOLERATED]
    tolerance_hits = [f for f in changed if f in TOLERATED]

    failures = 0
    if hard:
        annotate('error', 'not byte-reproducible', ' '.join(hard))
        diff = git('diff', '--unified=0', a.ref, '--', *hard).split('\n')
        shown = [l for l in diff if l[:1] in '+-' and not l.startswith(('+++', '---'))][:10]
        for line in shown:
            annotate('error', 'repro diff', line[:220])
        print(f'FAIL: {len(hard)} artifact(s) are not byte-identical to {a.ref}: {hard}')
        failures += len(hard)

    for f in tolerance_hits:
        committed_raw = git('show', f'{a.ref}:{f}')
        rebuilt = json.loads((ROOT / f).read_text())
        problems = compare_numeric(json.loads(committed_raw), rebuilt, f, TOLERATED[f])
        if problems:
            for p in problems[:10]:
                annotate('error', f'{Path(f).name} changed beyond tolerance', p)
            print(f'FAIL: {f} differs structurally or beyond tolerance:')
            for p in problems[:10]:
                print('   -', p)
            failures += 1
        else:
            print(f'ok   {f}: numerically identical to {a.ref} within {TOLERATED[f]:g} '
                  f'(solver digits only; structural fields compared exactly)')

    if failures:
        return 1
    print(f'reproducibility gate passed ({len(changed)} changed file(s), all within tolerance)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
