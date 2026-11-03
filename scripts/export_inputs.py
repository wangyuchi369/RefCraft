#!/usr/bin/env python3
"""Export only model-visible instructions and ordered reference images."""
import argparse
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.common import ROOT, definitions, read, write, sha
from refcraft.utils.answer.prompt_body import build_response_prompt


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True, help='New directory outside the release')
    ap.add_argument('--sample-ids', help='Comma-separated paper task IDs (1–300); default all 300')
    args = ap.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        ap.error('Place model workspaces outside the release to prevent rubric leakage')
    defs = definitions()
    ids = [int(s) for s in args.sample_ids.split(',')] if args.sample_ids else list(defs['queries'])
    if len(ids) != len(set(ids)) or not set(ids) <= set(defs['queries']):
        ap.error('IDs must be unique members of the frozen 300 tasks')
    output.mkdir(parents=True, exist_ok=False)
    template = (ROOT / 'refcraft/prompts/generation.txt').read_text()
    manifest = []
    for sid in ids:
        task = defs['queries'][sid]
        directory = output / str(sid)
        directory.mkdir()
        meta = defs['reference_metadata'][str(sid)]
        prompt, _ = build_response_prompt(template, task, meta, len(task['ref_screenshots']))
        if not prompt:
            raise ValueError(f'Missing prompt: {sid}')
        (directory / 'prompt.txt').write_text(prompt, encoding='utf-8')
        images = []
        for n, ref in enumerate(task['ref_screenshots'], 1):
            target = directory / f'reference_{n:02}.png'
            shutil.copyfile(ROOT / 'data' / ref, target)
            images.append({'path': str(target.relative_to(output)), 'sha256': sha(target),
                           'source_name': Path(ref).name})
        manifest.append({'sample_id': sid, 'prompt': f'{sid}/prompt.txt',
                         'language': task['language'], 'prompt_sha256': sha(directory / 'prompt.txt'),
                         'references': images, 'output': f'{sid}/index.html'})
    write(output / 'manifest.json', manifest)
    print(json.dumps({'tasks': len(ids), 'output': str(output)}))


if __name__ == '__main__':
    main()
