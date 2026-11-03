#!/usr/bin/env python3
"""Recompute the frozen leaderboard from stored B, R and core-case counts.

This does not render or rejudge historical model outputs.
"""
import csv
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT


def reproduce():
    records = [json.loads(line) for line in (ROOT / 'results/scores.jsonl').read_text().splitlines()]
    ids = set(map(int, (ROOT / 'data/task_ids.txt').read_text().split()))
    groups = {}
    seen = set()
    for row in records:
        key = row['model'], row['sample_id']
        if key in seen or row['sample_id'] not in ids:
            raise ValueError('Historical population mismatch')
        seen.add(key)
        goals = row['core_goals']
        c_lo = min(100 * g['passed'] / g['cases'] for g in goals)
        c_hi = min(100 * (g['passed'] + g['unknown']) / g['cases'] for g in goals)
        r = 100 if row['reference'] is None else row['reference']
        lo, hi = min(row['base'], c_lo, r), min(row['base'], c_hi, r)
        if abs(lo - hi) > 1e-10:
            raise ValueError('Historical score no longer exact')
        groups.setdefault(row['model'], []).append(lo)
    board = list(csv.DictReader((ROOT / 'results/leaderboard.csv').open()))
    if len(records) != 3600 or len(board) != 12 or set(groups) != {r['model'] for r in board}:
        raise ValueError('Historical model population mismatch')
    for row in board:
        scores = groups[row['model']]
        if len(scores) != 300 or abs(statistics.mean(scores) - float(row['score'])) > 1e-10:
            raise ValueError('Historical mean mismatch')
    return {'status': 'passed', 'models': len(board), 'tasks_per_model': 300, 'cells': len(records),
            'validation': 'offline aggregate reproduction; no rejudging'}


if __name__ == '__main__':
    print(json.dumps(reproduce(), indent=2))
