#!/usr/bin/env python3
"""Run a real Chromium action test and deterministic probes without paid APIs."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT
from refcraft.evaluation.worker import isolated
from refcraft.evaluation.browser import validate_plan
from refcraft.utils.render.probes import run_probe


def smoke():
    html = (ROOT / 'tests/fixtures/page.html').read_text()
    with tempfile.TemporaryDirectory(prefix='refcraft-smoke-') as tmp:
        root = Path(tmp)
        scope = {'outcomes': [{'key': 'save', 'test_cases': ['Submit a name and see confirmation']} ]}
        scenario = {'outcome_key': 'save', 'case_index': 0, 'expected': 'Saved: Ada', 'limitations': [],
                    'steps': [{'action': 'fill', 'selector': '#name', 'value': 'Ada'},
                              {'action': 'click', 'selector': '#submit'}]}
        validate_plan({'scenarios': [scenario]}, scope)
        trace = isolated('scenario', {'html': html, 'scenario': scenario}, root / 'action')
        assert not trace['test_error'], trace['test_error']
        assert 'Saved: Ada' in json.dumps(trace['snapshots'][-1], ensure_ascii=False)
        assert len(trace['snapshots']) == 3
        assert all((root / 'action' / s['screenshot']).is_file() for s in trace['snapshots'])
        bad = {**scenario, 'steps': [{'action': 'click', 'selector': '#does-not-exist'}]}
        blocked = isolated('scenario', {'html': html, 'scenario': bad}, root / 'blocked')
        assert blocked['test_error'], 'Missing selectors must remain evaluator errors'
        measure = isolated('measure', {'html': html, 'options': {
            'viewports': [1920, 390], 'fixtures': [], 'wait_ms': 50,
            'max_click_probes': 0, 'main_viewport_frames': 1, 'extra_viewport_frames': 1}}, root / 'measure')
        assert measure['ok'], measure['error']
        spec = {'id': 'overflow', 'probe': 'no_horizontal_overflow', 'viewports': [1920, 390]}
        assert run_probe(spec, measure)['status'] == 'pass'
        assert run_probe(spec, {'ok': False, 'error': 'test browser failure'})['status'] == 'blocked'
    return {'status': 'passed', 'real_browser': True, 'form_endpoint': 'Saved: Ada',
            'viewport_widths': [1920, 390], 'missing_selector': 'test_error',
            'failed_measurement': 'blocked', 'external_model_calls': 0}


if __name__ == '__main__':
    print(json.dumps(smoke(), indent=2))
