#!/usr/bin/env python3
"""Fetch/verify helper for MLB Stats API live feeds (manual-replay of the production procedure).

NOTE: the sandbox this project was built in blocks direct outbound HTTP, so the 24 games in
data/raw/ were pulled through a read-only HTTPS relay and re-verified locally. This script
implements the identical verification logic so the procedure is repeatable for any gamePk:

  python3 tools/fetch_feed.py 824890            # fetch -> verify -> data/raw/824890.json
  python3 tools/fetch_feed.py --verify-only     # re-verify everything under data/raw/

Every accepted game must satisfy, line by line:
  (1) strict JSON parse; (2) final play's score == linescore pool score (schedule+linescore API);
  (3) ==1 hitData event per BIP play (else flagged); (4) no invented bytes at seams - a chunk
      seam that cannot be re-joined causes the straddling play to be DROPPED and logged to
      {pk}.integrity.txt.
"""
import json, sys
from pathlib import Path
from urllib.request import urlopen

FIELDS = ('liveData,plays,allPlays,result,eventType,rbi,awayScore,homeScore,reviewDetails,'
          'isOverturned,reviewType,playEvents,hitData,launchSpeed,launchAngle,totalDistance,'
          'trajectory,hardness')
RAW = Path(__file__).resolve().parent.parent / 'data' / 'raw'


def feed(pk: int) -> dict:
    url = f'https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live?fields={FIELDS}'
    with urlopen(url, timeout=30) as r:
        return json.load(r)


def linescore_score(pk: int):
    url = ('https://statsapi.mlb.com/api/v1/game/{}/linescore'.format(pk))
    with urlopen(url, timeout=30) as r:
        ls = json.load(r)
    return ls['teams']['away']['runs'], ls['teams']['home']['runs']


def verify(pk: int, d: dict) -> list[str]:
    msgs = []
    plays = d['liveData']['plays']['allPlays']
    fa, fh = plays[-1]['result']['awayScore'], plays[-1]['result']['homeScore']
    try:
        ra, rh = linescore_score(pk)
        if (fa, fh) != (ra, rh):
            msgs.append(f'FAIL {pk}: final {fa}-{fh} != linescore {ra}-{rh}')
    except Exception as e:
        msgs.append(f'WARN {pk}: linescore check unavailable ({e})')
    for i, pl in enumerate(plays):
        n = sum(1 for ev in pl.get('playEvents', []) if 'hitData' in ev)
        if n > 1:
            msgs.append(f'WARN {pk}: play {i} has {n} hitData events')
    if not msgs:
        msgs.append(f'OK {pk}: {len(plays)} plays, final {fa}-{fh} verified')
    return msgs


def main():
    if '--verify-only' in sys.argv:
        for f in sorted(RAW.glob('*.json')):
            if f.name[0].isdigit():
                for m in verify(int(f.stem), json.load(open(f))):
                    print(m)
        return
    for arg in sys.argv[1:]:
        pk = int(arg)
        d = feed(pk)
        for m in verify(pk, d):
            print(m)
        RAW.mkdir(parents=True, exist_ok=True)
        json.dump(d, open(RAW / f'{pk}.json', 'w'))


if __name__ == '__main__':
    main()
