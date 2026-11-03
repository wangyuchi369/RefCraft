#!/usr/bin/env python3
"""Verify all released file hashes, task identities and evaluation mappings."""
import argparse
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT, read, sha, definitions


def validate(decode_images=False):
    manifest = read(ROOT / 'data/manifest.json')
    if manifest.get('schema') != 'refcraft.dataset.v2' or manifest.get('task_id_scheme') != 'paper-1-300':
        raise ValueError('Expected the paper-numbered dataset release')
    for name, digest in manifest['files_sha256'].items():
        path = ROOT / 'data' / name
        if path.is_symlink() or sha(path) != digest:
            raise ValueError('Changed or linked release file: ' + name)
    defs = definitions()
    ids = list(defs['queries'])
    selected = list(map(int, (ROOT / 'data/task_ids.txt').read_text().split()))
    if ids != list(range(1, 301)) or ids != selected:
        raise ValueError('Expected paper task IDs 1–300 in order')
    with (ROOT / 'data/task_id_map.csv').open(newline='') as stream:
        mapping = list(csv.DictReader(stream))
    source_ids = [int(r['source_sample_id']) for r in mapping]
    if ([int(r['paper_task_id']) for r in mapping] != ids
            or len(set(source_ids)) != 300 or source_ids != sorted(source_ids)
            or any(r['source_task_id'] != f'vref_{s:03d}' for r, s in zip(mapping, source_ids))):
        raise ValueError('Invalid paper/source task ID mapping')
    expected = set(map(str, ids))
    for name in ['checklists', 'cores', 'programmatic', 'reference_metadata']:
        if set(defs[name]) != expected:
            raise ValueError(name + ': population mismatch')
    if set(defs['references']) - expected:
        raise ValueError('Reference population mismatch')
    projections = {}
    for name in ['queries', 'keypoints']:
        rows = [json.loads(line) for line in (ROOT / 'data' / (name + '.jsonl')).read_text().splitlines()]
        if [r['sample_id'] for r in rows] != ids:
            raise ValueError('Projection identity/order mismatch')
        projections[name] = {r['sample_id']: r for r in rows}
    images = set()
    attribution = read(ROOT / 'data/attribution.json')
    attributed = {}
    for row in attribution['task_references']:
        key = row['sample_id'], row['label']
        if key in attributed or row['sample_id'] not in ids:
            raise ValueError('Attribution task/label mismatch')
        attributed[key] = row
    expected_attribution = set()
    n_keypoints = n_scored = n_programmatic = n_goals = n_cases = 0
    for sid, q in defs['queries'].items():
        key = str(sid)
        cl = defs['checklists'][key]
        keys = [x['id'] for x in cl]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate keypoint ID')
        n_keypoints += len(cl)
        n_scored += sum(x['importance'] != 'zero' for x in cl)
        scored = {x['id']: x for x in cl if x['importance'] != 'zero'}
        progs = defs['programmatic'][key]['verifiable']
        if {p['id'] for p in progs} != {x['id'] for x in scored.values() if x.get('deterministic')}:
            raise ValueError('Programmatic population mismatch')
        for p in progs:
            if any(p.get(k) != scored[p['id']].get(k) for k in ['question', 'probe']):
                raise ValueError('Programmatic definition mismatch')
        n_programmatic += len(progs)
        for group in defs['references'].get(key, []):
            if not group['items'] or any(i['id'] not in scored for i in group['items']):
                raise ValueError('Reference item mapping mismatch')
        goals = defs['cores'][key]['outcomes']
        if not goals or len({g['key'] for g in goals}) != len(goals) or any(not g['test_cases'] for g in goals):
            raise ValueError('Invalid core scope')
        n_goals += len(goals)
        n_cases += sum(len(g['test_cases']) for g in goals)
        qq = projections['queries'][sid]
        if (q['id'] != f'refcraft_{sid:03d}' or qq['id'] != q['id']
                or q['recipe']['sample_id'] != sid
                or defs['reference_metadata'][key]['sample_id'] != sid):
            raise ValueError('Task metadata identity mismatch')
        raw_query = q['user_prompt_zh'] if q['language'].startswith('zh') else q['user_prompt_en']
        if qq['query'] != raw_query or qq['reference_images'] != q['ref_screenshots']:
            raise ValueError('Model query projection changed')
        kk = projections['keypoints'][sid]
        if kk['keypoints'] != cl or kk['core_scope'] != defs['cores'][key] or kk['reference_groups'] != defs['references'].get(key, []):
            raise ValueError('Keypoint projection changed')
        for ref in q['ref_screenshots']:
            if Path(ref).parent != Path('references') / key:
                raise ValueError('Reference path does not use its paper task ID')
            p = ROOT / 'data' / ref
            if not p.is_file() or ROOT not in p.resolve().parents:
                raise ValueError('Missing/escaping reference image')
            images.add(p)
        groups = defs['reference_metadata'][key].get('ref_groups') or []
        frames = [f['name'] for g in groups for f in g['frames']]
        if frames != [Path(p).name for p in q['ref_screenshots']]:
            raise ValueError('Reference group frame order drift')
        for group in groups:
            attr_key = sid, group['label']
            expected_attribution.add(attr_key)
            row = attributed.get(attr_key)
            if (row is None or row['pool_sample_id'] != group['src_sample_id']
                    or str(row['pool_sample_id']) not in attribution['sources']
                    or row['images'] != [f'references/{sid}/{f["name"]}' for f in group['frames']]):
                raise ValueError('Attribution reference mapping mismatch')
    if set(attributed) != expected_attribution:
        raise ValueError('Attribution population mismatch')
    if len(images) != 3491 or images != set((ROOT / 'data/references').glob('*/*.png')):
        raise ValueError('Expected exactly 3491 referenced PNGs')
    if decode_images:
        from PIL import Image
        for path in images:
            with Image.open(path) as im:
                if im.format != 'PNG' or min(im.size) < 1:
                    raise ValueError('Invalid reference PNG')
                im.verify()
    return {'status': 'passed', 'tasks': len(ids), 'task_id_scheme': 'paper-1-300',
            'task_id_mapping': 'passed', 'reference_assignments': len(attributed), 'reference_images': len(images),
            'keypoints_including_zero_weight': n_keypoints, 'scored_keypoints': n_scored,
            'programmatic_keypoints': n_programmatic, 'core_goals': n_goals, 'core_cases': n_cases,
            'hashed_files': len(manifest['files_sha256']), 'png_decoding': decode_images}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--decode-images', action='store_true', help='Also verify all PNG file structures with Pillow')
    args = ap.parse_args()
    print(json.dumps(validate(args.decode_images), indent=2))
