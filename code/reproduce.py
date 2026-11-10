"""Run the complete released study and all audits in dependency order.

``--revision-only`` runs only the second-round (4.x) experiments, tables,
figures and audits on top of the frozen 3.0.2 results.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = [['test_model.py'], ['run_study.py'], ['exact_study.py'], ['run_hard_wait.py'],
            ['make_tables.py'], ['make_figures.py'], ['verify_package.py']]
REVISION = [['revision_study.py', 'scenarios', 'bounds', 'weights', 'stochastic', 'design', 'robot', 'profile'],
            ['cp_study.py', 'targeted', '--step', '40', '--polish', '600', '--limit', '120'],
            ['cp_study.py', 'flow', '--step', '600', '--polish', '300'],
            ['cp_study.py', 'repair', '--threshold', '10800'],
            ['cp_study.py', 'full', '--step', '15'],
            ['cp_study.py', 'intermediate', '--limit', '120'],
            ['cp_study.py', 'design', '--step', '15'],
            ['cp_study.py', 'sensitivity', '--step', '15'],
            ['make_revision_tables.py'], ['make_revision_figures.py'], ['verify_revision.py']]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--revision-only', action='store_true')
    args = ap.parse_args()
    env = dict(os.environ)
    env.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / '2026tech-mpl'))
    steps = REVISION if args.revision_only else ORIGINAL + REVISION
    if not args.revision_only:
        subprocess.run([sys.executable, str(ROOT / 'code/verify_package.py'), '--check-hashes'],
                       cwd=ROOT, env=env, check=True)
    for step in steps:
        print('Running', ' '.join(step), flush=True)
        subprocess.run([sys.executable, str(ROOT / 'code' / step[0])] + step[1:], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
