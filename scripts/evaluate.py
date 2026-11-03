#!/usr/bin/env python3
"""Evaluate self-contained HTML files against the frozen 300-task dataset."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT, definitions, definition_digest, write
from refcraft.evaluation.scoring import aggregate


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--responses', type=Path, help='Directory containing <sample_id>.html or <sample_id>/index.html')
    ap.add_argument('--output', type=Path, required=True, help='New output directory')
    ap.add_argument('--model-name', default='custom-model', help='Public name of the evaluated model')
    ap.add_argument('--sample-ids', help='Comma-separated paper task IDs (1–300); full denominator stays 300')
    ap.add_argument('--judge-base-url', default=os.environ.get('REFCRAFT_JUDGE_BASE_URL'))
    ap.add_argument('--judge-model', default=os.environ.get('REFCRAFT_JUDGE_MODEL'))
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--api-timeout', type=int, default=180)
    ap.add_argument('--browser-timeout', type=int, default=180)
    ap.add_argument('--max-tokens', type=int, default=16384)
    ap.add_argument('--collect-only', action='store_true', help='Browser/programmatic evidence only; no API calls')
    ap.add_argument('--aggregate-only', action='store_true', help='Recompute scores in an existing run')
    args = ap.parse_args()
    args.output = args.output.resolve()
    if args.aggregate_only:
        print(json.dumps(aggregate(args.output), ensure_ascii=False, indent=2))
        return
    if not args.responses or not args.responses.is_dir():
        ap.error('--responses must be an existing directory')
    if not args.collect_only and not (args.judge_base_url and args.judge_model and os.environ.get('REFCRAFT_JUDGE_API_KEY')):
        ap.error('Set judge base URL, model, and REFCRAFT_JUDGE_API_KEY, or use --collect-only')
    if args.workers < 1 or min(args.api_timeout, args.browser_timeout, args.max_tokens) < 1:
        ap.error('Worker count, timeouts and token limit must be positive')
    defs = definitions()
    ids = [int(s) for s in args.sample_ids.split(',')] if args.sample_ids else list(defs['queries'])
    if len(set(ids)) != len(ids) or not set(ids) <= set(defs['queries']):
        ap.error('IDs must be unique members of the frozen 300 tasks')
    args.output.mkdir(parents=True, exist_ok=False)
    contract = {'schema': 'refcraft.evaluation.v1', 'model': args.model_name,
                'definitions_sha256': definition_digest(), 'requested_ids': ids,
                'judge_model': args.judge_model, 'workers': args.workers,
                'collect_only': args.collect_only, 'desktop': [1920, 1080], 'mobile': [390, 844]}
    write(args.output / 'run.json', contract)
    from refcraft.evaluation.runner import evaluate_one
    def run(sid):
        paths = [args.responses / f'{sid}.html', args.responses / str(sid) / 'index.html']
        found = [p for p in paths if p.is_file()]
        if len(found) > 1:
            raise ValueError(f'Ambiguous submission for {sid}; keep one naming format')
        return evaluate_one(sid, found[0] if found else None, args.output / 'tasks' / str(sid), defs, contract, args)
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, sid): sid for sid in ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                result = future.result()
                print(json.dumps({'sample_id': sid, 'status': result['status']}, ensure_ascii=False), flush=True)
            except Exception as exc:
                errors += 1
                print(json.dumps({'sample_id': sid, 'status': 'execution_error', 'error': type(exc).__name__}), flush=True)
    summary = aggregate(args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors or (not args.collect_only and not summary['complete']):
        sys.exit(2)


if __name__ == '__main__':
    main()
