#!/usr/bin/env python3
"""Run the whole pipeline: dataset -> model -> NYDN -> live board -> static site assets.

Every step reads only committed sources (data/raw/, data/source/) and writes only committed
artifacts (docs/data/, data/*.json), so a clean checkout can rebuild the published site with no
network and no scratch directory. Two consecutive runs are byte-identical; CI asserts it.

Order matters in two places: `refresh_rbi_notes.py` runs before `build_site_data.py` because the
page-ready JSON copies the Rule 9.04 note columns out of the review ledger, and `rbi_evidence.py`
runs after the model so the measured RBI table can quote the window the model declares.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / 'tools'


def run(*args):
    r = subprocess.run([sys.executable, *args])
    if r.returncode != 0:
        sys.exit(r.returncode)


for step in ('build_dataset.py', 'train_model.py', 'build_nydn.py'):
    print(f'--- {step} ---')
    run(str(TOOLS / step))

print('--- fetch_live.py (offline live-board fallback, labelled) ---')
run(str(TOOLS / 'fetch_live.py'), '--feed', 'data/source/feed_823441.json',
    '--out', 'docs/data/live_now.json', '--csv-out', 'docs/data/live_now.csv')

print('--- refresh_rbi_notes.py (re-derive the Rule 9.04 note columns of the review ledger) ---')
run(str(TOOLS / 'refresh_rbi_notes.py'))

print('--- build_site_data.py (compact page data, derived offline) ---')
run(str(TOOLS / 'build_site_data.py'))

print('--- rbi_evidence.py (measured RBI stake, derived offline) ---')
run(str(TOOLS / 'rbi_evidence.py'))

print('--- live_score.py (site demo board) ---')
# relative paths, so the committed artifact is identical on every machine
run(str(TOOLS / 'live_score.py'), '--feed', 'data/source/feed_823441.json', '--quiet',
    '--json', 'docs/data/live_sample.json', '--csv', 'docs/data/live_sample.csv')

print('pipeline complete')
