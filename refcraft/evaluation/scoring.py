"""Frozen release arithmetic with fixed denominators and explicit unknowns."""
import csv
from fractions import Fraction
import io

from .common import definitions, read, write, definition_digest, sha
from refcraft.utils.score.reference_core_score import (
    CAP, checklist_bounds, core_bounds, reference_bounds, minimum_bounds, serialized,
)


def task_score(sid, record, defs):
    key = str(sid)
    specs = [x for x in defs['checklists'][key] if x['importance'] != 'zero']
    spec_map = {x['id']: x for x in specs}
    observations = {}
    for row in record['items']:
        iid = row['id']
        if iid not in spec_map or iid in observations:
            raise ValueError(f'{sid}: unexpected/duplicate keypoint {iid!r}')
        observations[iid] = row
        if row['status'] not in {'assessed', 'unresolved'}:
            raise ValueError('Invalid keypoint status')
        if row['status'] == 'assessed' and (type(row['score']) is not int or not 0 <= row['score'] <= CAP[spec_map[iid]['depth']]):
            raise ValueError('Invalid keypoint score')
    if set(observations) != set(spec_map):
        raise ValueError(f'{sid}: incomplete keypoint population')
    b = checklist_bounds(specs, observations)
    c = core_bounds(defs['cores'][key], record['cases'])
    r = reference_bounds(defs['references'].get(key, []), observations)
    s = minimum_bounds(b, c, r)
    return {'sample_id': sid, 'B': serialized(b), 'C': serialized(c),
            'R': serialized(r), 'Overall': serialized(s)}


def aggregate(output):
    defs = definitions()
    contract = read(output / 'run.json')
    if contract['definitions_sha256'] != definition_digest():
        raise ValueError('Dataset changed since evaluation')
    rows = []
    observed = missing = unresolved_items = unresolved_cases = 0
    from .common import cases_for
    for sid in defs['queries']:
        path = output / 'tasks' / str(sid) / 'result.json'
        if path.exists():
            record = read(path)
            if record['sample_id'] != sid or record['definitions_sha256'] != contract['definitions_sha256']:
                raise ValueError('Result identity/definition mismatch')
            if record.get('html_sha256') and sha(path.parent / 'submission.html') != record['html_sha256']:
                raise ValueError('Scored HTML changed')
            observed += record.get('status') == 'evaluated'
        else:
            missing += 1
            record = {'items': [{'id': x['id'], 'status': 'unresolved', 'score': None}
                       for x in defs['checklists'][str(sid)] if x['importance'] != 'zero'],
                      'cases': cases_for(defs['cores'][str(sid)])}
        rows.append(task_score(sid, record, defs))
        unresolved_items += sum(x['status'] == 'unresolved' for x in record['items'])
        unresolved_cases += sum(x['status'] == 'unresolved' for x in record['cases'])
    def mean_component(name):
        present = [r[name] for r in rows if r[name] is not None]
        lo = sum(Fraction(r['rational'][0]) for r in present) / len(present)
        hi = sum(Fraction(r['rational'][1]) for r in present) / len(present)
        return {**serialized((lo, hi)), 'n': len(present)}
    summary = {'model': contract['model'], 'policy': 'release-v2-depth-normalized',
               'tasks': len(rows), 'evaluated_tasks': observed, 'missing_results': missing,
               'unresolved_keypoints': unresolved_items, 'unresolved_core_cases': unresolved_cases,
               'all_evidence_resolved': unresolved_items == unresolved_cases == 0,
               'components': {k: mean_component(k) for k in ['B', 'C', 'R', 'Overall']}}
    summary['complete'] = observed == 300 and all(r['Overall']['exact'] for r in rows)
    summary['status'] = 'complete' if summary['complete'] else 'incomplete'
    write(output / 'scores.json', rows)
    write(output / 'summary.json', summary)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=['sample_id', 'B', 'C', 'R', 'Overall', 'lower', 'upper'])
    writer.writeheader()
    for row in rows:
        writer.writerow({'sample_id': row['sample_id'],
                         **{k: row[k]['score'] if row[k] else None for k in ['B', 'C', 'R', 'Overall']},
                         'lower': row['Overall']['lower'], 'upper': row['Overall']['upper']})
    (output / 'scores.csv').write_text(buffer.getvalue(), encoding='utf-8')
    return summary
