"""Independent audit of the second-round artifacts (package 4.1.0).

* every CP schedule (RH-CP, CP-REF, intermediate, design, sensitivity) is
  re-audited from its timestamps with ``cell_model.audit``;
* hard-wait schedules must contain no internal wait above its limit;
* the first pickup of every CP schedule leaves time for the conservative
  initial robot travel (auxiliary service plus the longest empty movement
  from the initial robot position to a station of the origin type);
* the CS15 analytical cadence is continuous at 180 s (no block overlap);
* URG and URG-MECT action sequences are identical in all fifteen scenarios.
"""
import csv
import json
import math
from pathlib import Path

from cell_model import Cell, Instance, audit, simulate
from cp_reference import WaitProfile, audit_profile
from revision_study import PROFILE_A

ROOT = Path(__file__).resolve().parents[1]


def instance_for(path):
    name = path.parent.name
    stem = path.name
    opts, wait = {}, 300.
    if path.parent.name == 'design':
        case, alt = stem.split('__')[0], stem.split('__')[1].replace('_hard_wait_trace.json', '')
        cfg = ROOT / 'instances' / 'design' / f'{case}__{alt}_config.json'
    elif path.parent.name == 'sensitivity':
        cfg = ROOT / 'instances/cs6_fullday_config.json'
        if 'auxiliary' in stem:
            opts['auxiliary_s'] = float(stem.split('auxiliary_')[1].split('s_')[0])
        if 'profile_A' in stem:
            wait = WaitProfile(PROFILE_A)
    elif path.parent.name == 'intermediate':
        base = stem.split('_operational')[0].split('_hard_wait')[0]
        prefix = None
        if '_first' in base:
            base, prefix = base.split('_first')
            prefix = int(prefix)
        cfg = ROOT / 'instances' / f'{base}_config.json'
        cell = Cell(cfg)
        spec = cell.cfg['samples'] if prefix is None else cell.cfg['samples'][:prefix]
        return Instance(cell, spec), WaitProfile(wait)
    else:
        cfg = ROOT / 'instances' / f'{name}_config.json'
    cell = Cell(cfg, **opts)
    return Instance(cell, cell.cfg['samples']), wait if isinstance(wait, WaitProfile) else WaitProfile(wait)


def initial_travel_ok(inst, tr):
    cell = inst.cell
    first = min(tr['tasks'], key=lambda x: (x['start_s'], x['pickup_s']))
    lead = float(first['auxiliary_s']) + max(cell.empty(cell.initial_robot, o)
                                             for o in cell.by_k[cell.type_of[first['origin']]])
    return float(first['pickup_s']) + 1e-6 >= lead


def main():
    n = 0
    for path in sorted((ROOT / 'validation/cp').rglob('*_trace.json')):
        inst, wait = instance_for(path)
        tr = json.loads(path.read_text())
        m = audit_profile(inst, tr, wait)
        for key in ('makespan_s', 'violations', 'affected_samples', 'max_wait_s'):
            assert math.isclose(float(tr['kpi'][key]), float(m[key]), abs_tol=1e-5), (path, key)
        if 'hard_wait' in path.name or 'targeted' in path.name:
            assert m['violations'] == 0, path
        assert initial_travel_ok(inst, tr), (path, 'initial robot travel')
        n += 1
    cfg = json.loads((ROOT / 'instances/cs15_tiled180_config.json').read_text())
    an = sorted(x['release_s'] for x in cfg['samples'] if x['type'] in ('HC', 'SVOC', 'SVOC_E'))
    assert {round(b - a, 6) for a, b in zip(an, an[1:])} == {180.0}
    for row in csv.DictReader((ROOT / 'data/scenario_map.csv').open()):
        cell = Cell(ROOT / row['input_file'])
        inst = Instance(cell, cell.cfg['samples'])
        a = simulate(inst, 'URG', trace=False)
        b = simulate(inst, 'URG-MECT', trace=False)
        assert [(x['sample'], x['destination']) for x in a['tasks']] == [(x['sample'], x['destination']) for x in b['tasks']]
    print(f'Audited {n} CP schedules (initial travel included); CS15 cadence continuous; URG = URG-MECT on 15 scenarios')


if __name__ == '__main__':
    main()
