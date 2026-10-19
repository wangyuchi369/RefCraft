#!/usr/bin/env python3
"""Export only website assets. Does not publish, evaluate models, or touch paper sources."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from build_data import build

ROOT = Path(__file__).resolve().parents[1]


def package():
    build()
    fixed = {
        'index.html', 'styles.css', 'app.js', 'leaderboard.js', 'leaderboard.css', 'visual.css', 'credits.html', 'README.md',
        'assets/favicon.svg', 'assets/RefCraft.pdf', 'assets/social-card.png',
        'data/config.json', 'data/examples.json', 'data/leaderboard.json',
        'data/leaderboard.csv', 'data/site-data.js', 'data/refcraft.bib',
        'data/reproducibility.md', 'data/reference-attribution.json',
        'scripts/build_data.py', 'scripts/package_site.py', 'scripts/build_standalone.py',
        'scripts/extract_model_marks.py', 'data/model-marks.json',
        'assets/paper-v2-framework.webp',
        'data/arena-alignment.csv',
        'assets/paper-v2-teaser.webp', 'assets/refcraft-mascot.png',
        'data/authors.json', 'data/example-scores.csv',
        'assets/brands/qwen.png', 'assets/brands/github.png', 'assets/brands/huggingface.png',
        'fragments/interactive-examples.html',
    }
    examples = json.loads((ROOT / 'data/examples.json').read_text())
    for case in examples:
        fixed.update(image['src'] for image in [*case['references'], case['output']])
        fixed.add(f"demos/task-{case['id']:03d}.html")
    fixed.update(str(path.relative_to(ROOT)) for path in (ROOT / 'assets/fonts').iterdir() if path.is_file())
    # The marks reach the browser as data URIs in site-data.js; the PNGs ship so the
    # recorded hashes in data/model-marks.json stay verifiable from the package alone.
    fixed.update(str(path.relative_to(ROOT)) for path in (ROOT / 'assets/brands/marks').iterdir() if path.is_file())
    dist = ROOT / 'dist'
    # This is a generated, website-owned directory, never a benchmark source.
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir()
    for name in sorted(fixed):
        source = ROOT / name
        if not source.is_file():
            raise FileNotFoundError(source)
        target = dist / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (dist / '.nojekyll').touch()
    manifest = {
        str(path.relative_to(dist)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(dist.rglob('*')) if path.is_file()
    }
    (dist / 'site-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    archive = ROOT / 'refcraft-project-site.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(dist.rglob('*')):
            if path.is_file():
                zip_file.write(path, str(path.relative_to(dist)))
    print(f'Packaged {len(manifest)} files; {archive.stat().st_size / 1024 / 1024:.2f} MiB.')
    print(f'ZIP SHA256: {hashlib.sha256(archive.read_bytes()).hexdigest()}')


if __name__ == '__main__':
    package()
