#!/usr/bin/env python3
"""Exercise the full runner with real browsers and an explicitly fake local judge.

No semantic grades are generated: every fake judgment is unresolved/null.
This tests integration, not model quality or judge accuracy. No paid APIs used.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT, definitions, read


def main():
    defs = definitions()
    sid = min(defs['queries'], key=lambda s: len(defs['checklists'][str(s)]))
    requests_seen = {'plan': 0, 'keypoint': 0, 'core': 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            data = json.loads(body['messages'][1]['content'][0]['text'])
            if 'submitted_html' in data:
                requests_seen['plan'] += 1
                result = {'scenarios': [{'outcome_key': g['key'], 'case_index': i,
                     'expected': case, 'limitations': [], 'steps': [{'action': 'inspect'}]}
                     for g in data['scope']['outcomes'] for i, case in enumerate(g['test_cases'])]}
            elif 'keypoint' in data:
                requests_seen['keypoint'] += 1
                result = {'id': data['keypoint']['id'], 'status': 'unresolved', 'score': None,
                          'expected': 'Synthetic integration test', 'observed': 'Real browser trace supplied',
                          'deviation': 'Not assessed', 'reason': 'Fake local endpoint; no semantic judgment'}
            else:
                requests_seen['core'] += 1
                result = {'cases': [{'outcome_key': t['outcome_key'], 'case_index': t['case_index'],
                          'status': 'unresolved', 'reason': 'Fake local endpoint', 'evidence_steps': []}
                          for t in data['traces']], 'scope_issues': []}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(result)}}]}).encode())
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='refcraft-pipeline-smoke-') as tmp:
            directory = Path(tmp)
            inputs = directory / 'inputs'
            inputs.mkdir()
            shutil.copyfile(ROOT / 'tests/fixtures/page.html', inputs / f'{sid}.html')
            env = {**os.environ, 'REFCRAFT_JUDGE_API_KEY': 'synthetic-local-test-token'}
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/evaluate.py'),
                '--responses', str(inputs), '--output', str(directory / 'run'), '--sample-ids', str(sid),
                '--model-name', 'synthetic-smoke-only', '--judge-model', 'fake-local-judge',
                '--judge-base-url', f'http://127.0.0.1:{server.server_port}/v1'],
                env=env, capture_output=True, text=True, timeout=900)
            if result.returncode != 2:
                raise AssertionError(f'Expected incomplete exit 2: {result.stdout[-1500:]} {result.stderr[-1500:]}')
            summary = read(directory / 'run/summary.json')
            task = read(directory / 'run/tasks' / str(sid) / 'result.json')
            expected_items = sum(x['importance'] != 'zero' and not x.get('deterministic') for x in defs['checklists'][str(sid)])
            assert requests_seen == {'plan': 1, 'keypoint': expected_items, 'core': 1}, requests_seen
            assert task['status'] == 'evaluated' and not task['errors'], task['errors']
            assert summary['tasks'] == 300 and summary['evaluated_tasks'] == 1
            assert summary['components']['Overall']['score'] is None and not summary['complete']
            print(json.dumps({'status': 'passed', 'sample_id': sid, 'real_browser': True,
                'judge': 'fake_local_endpoint', 'requests': requests_seen, 'external_model_calls': 0,
                'overall': None, 'full_300_task_denominator_retained': True}, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
