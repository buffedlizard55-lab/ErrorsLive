#!/usr/bin/env python3
"""Run the whole pipeline: dataset -> model -> NYDN -> static site assets."""
import subprocess, sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
steps = ['build_dataset.py', 'train_model.py', 'build_nydn.py']
for step in steps:
    print(f'--- {step} ---')
    r = subprocess.run([sys.executable, str(TOOLS / step)])
    if r.returncode != 0:
        sys.exit(r.returncode)
print('pipeline complete')
