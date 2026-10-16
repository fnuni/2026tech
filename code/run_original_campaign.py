"""Execute the recovered original simulator and policy scripts in isolation.

These runs audit the archived campaign. The corrected study is reproduced by
run_campaign.py; original waiting KPIs include transport and are not C4 KPIs.
"""
import csv
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'validation' / 'original_runs'

def run(item):
    case, policy = item
    dest = OUT / case['label'] / policy
    dest.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / 'code' / 'original').glob('*.py'):
        shutil.copy2(path, dest / path.name)
    scripts = {'URG': 'greedy_solver.py', 'URG-MECT': 'cpr_la_solver.py',
               'LB-ROLL': 'policy3_solver.py'}
    cmd = [sys.executable, 'simulator_v3.py', '--scale', case['scale'],
           '--interval', case['interval_s'], '--seed', case['seed'],
           '--pipc-s', case['pipc_s'], '--solver', scripts[policy], '--no-xlsx']
    with (dest / 'stdout.log').open('w') as log:
        subprocess.run(cmd, cwd=dest, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=300)
    kpi = json.loads((dest / 'kpi.json').read_text())
    result = {'case': case['label'], 'policy': policy, **kpi}
    print(case['label'], policy, kpi['makespan_s'], kpi['n_violations'], flush=True)
    return result

def main():
    cases = list(csv.DictReader((ROOT / 'data' / 'cases.csv').open()))
    jobs = [(c, p) for c in cases for p in ('URG', 'URG-MECT', 'LB-ROLL')]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, jobs))
    (ROOT / 'validation' / 'original_campaign.json').write_text(
        json.dumps(results, indent=2) + '\n')

if __name__ == '__main__':
    main()
