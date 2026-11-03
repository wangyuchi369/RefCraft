"""Credential-free subprocess for browser measurements and action scenarios."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from refcraft.evaluation.common import read, write


def isolated(kind, data, directory, timeout=180):
    directory.mkdir(parents=True, exist_ok=False)
    write(directory / 'input.json', data)
    env = {k: v for k, v in os.environ.items() if k in {
        'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'LD_LIBRARY_PATH', 'PLAYWRIGHT_BROWSERS_PATH'}}
    with (directory / 'worker.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), kind, str(directory)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            raise
    if code != 0 or not (directory / 'output.json').exists():
        raise RuntimeError(f'browser_worker_exit_{code}; inspect worker.log')
    return read(directory / 'output.json')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('kind', choices=['measure', 'scenario'])
    ap.add_argument('directory', type=Path)
    args = ap.parse_args()
    data = read(args.directory / 'input.json')
    if args.kind == 'measure':
        from refcraft.utils.render.measure_response import measure_html
        result = measure_html(data['html'], str(args.directory), **data['options'])
    else:
        from refcraft.evaluation.browser import run_browser
        result = run_browser(data['html'], {'scenarios': [data['scenario']]}, args.directory)[0]
    write(args.directory / 'output.json', result)


if __name__ == '__main__':
    main()
