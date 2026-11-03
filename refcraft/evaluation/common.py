"""Shared paths and JSON helpers. No dependency on the source workspace."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def definitions():
    dataset = ROOT / 'data'
    rows = [json.loads(line) for line in (dataset / 'keypoints.jsonl').read_text().splitlines()]
    ids = [str(row['sample_id']) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate task IDs in keypoints.jsonl')
    return {
        'queries': {x['sample_id']: x for x in read(dataset / 'task_metadata.json')},
        'checklists': {str(row['sample_id']): row['keypoints'] for row in rows},
        'cores': {str(row['sample_id']): row['core_scope'] for row in rows},
        'references': {str(row['sample_id']): row['reference_groups'] for row in rows if row['reference_groups']},
        'programmatic': read(dataset / 'programmatic.json'),
        'reference_metadata': read(dataset / 'reference_metadata.json'),
    }


def definition_digest():
    names = ['task_metadata.json', 'keypoints.jsonl', 'programmatic.json']
    return hashlib.sha256(json.dumps({n: sha(ROOT / 'data' / n) for n in names}, sort_keys=True).encode()).hexdigest()


def cases_for(scope):
    return [{'outcome_key': g['key'], 'case_index': i, 'status': 'unresolved',
             'reason': 'not_run', 'evidence_steps': []}
            for g in scope['outcomes'] for i in range(len(g['test_cases']))]
