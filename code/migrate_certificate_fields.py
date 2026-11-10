"""One-off migration (package 4.1.0): make every CSV state which certificate it uses.

Files written by package 4.0.0 stored the first-version certificate (v3:
head/tail term on complete operation sets) in ``certificate_lb_s``. The
manuscript uses the strengthened certificate of Equation (cert) (v4: nested
subsets and mandatory empty legs). This script renames the old field to
``certificate_v3_lb_s``, stores the strengthened value in
``certificate_lb_s`` and recomputes the dependent gaps. Schedules and KPIs
are not changed. ``cp_study.py`` and ``revision_study.py`` now write the same
schema directly. The 3.0.2 campaign files ``kpi_campaign.csv`` and
``lower_bounds.csv`` contain only the first-version certificate; their fields
are renamed with the ``v3`` suffix, as now written by ``run_study.py``; the
strengthened values are in ``lower_bounds_v4.csv``.
"""
import csv
from pathlib import Path

from cell_model import Cell, Instance
from revision_study import StrengthenedCertificate
from run_campaign import write_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
POLICIES = ['URG', 'SRPT', 'BNA', 'LB-LA']
_cache = {}


def v4(config, prefix=None):
    key = (str(config), prefix)
    if key not in _cache:
        cell = Cell(config)
        spec = cell.cfg['samples'] if prefix is None else cell.cfg['samples'][:prefix]
        _cache[key] = StrengthenedCertificate.components(Instance(cell, spec))
    return _cache[key]


def rows(name):
    return list(csv.DictReader((DATA / name).open()))


def already(rs):
    return rs and 'certificate_v3_lb_s' in rs[0]


def migrate_reference():
    rs = rows('cp_reference.csv')
    if already(rs):
        return
    out = []
    for r in rs:
        c = v4(ROOT / 'instances' / f"{r['case']}_config.json")
        new = {}
        for k, v in r.items():
            if k == 'certificate_lb_s':
                new['certificate_v3_lb_s'] = v
                new['certificate_lb_s'] = c['certificate_lb_s']
            elif k == 'gap_pct':
                new['gap_v3_pct'] = v
                new['gap_pct'] = 100 * (float(r['makespan_s']) / c['certificate_lb_s'] - 1)
            else:
                new[k] = v
        out.append(new)
    write_csv(DATA / 'cp_reference.csv', out)


def migrate_targeted():
    rs = rows('cp_targeted_cs6.csv')
    if already(rs):
        return
    c = v4(ROOT / 'instances/cs6_fullday_config.json')
    out = []
    for r in rs:
        new = {}
        for k, v in r.items():
            if k == 'certificate_lb_s':
                new['certificate_v3_lb_s'] = v
                new['certificate_lb_s'] = c['certificate_lb_s']
            elif k == 'gap_to_certificate_pct':
                new['gap_pct'] = 100 * (float(r['makespan_s']) / c['certificate_lb_s'] - 1)
            else:
                new[k] = v
        out.append(new)
    write_csv(DATA / 'cp_targeted_cs6.csv', out)


def migrate_intermediate():
    rs = rows('cp_intermediate.csv')
    if already(rs):
        return
    out = []
    for r in rs:
        inst = r['instance']
        base, prefix = (inst.split('_first')[0], int(inst.split('_first')[1])) if '_first' in inst else (inst, None)
        c = v4(ROOT / 'instances' / f'{base}_config.json', prefix)['certificate_lb_s']
        rb = float(r['relaxed_cp_bound_s']) if r['relaxed_cp_bound_s'] not in ('', 'None') else 0.
        lb = max(c, rb)
        best_rule = min(float(r[f'{p}_makespan_s']) for p in POLICIES)
        prim = float(r['primal_s'])
        ub = min(prim, best_rule) if r['problem'] == 'operational' else prim
        new = {}
        for k, v in r.items():
            if k == 'certificate_lb_s':
                new['certificate_v3_lb_s'] = v
                new['certificate_lb_s'] = c
            elif k == 'best_lower_bound_s':
                new[k] = lb
            elif k == 'bound_gap_pct':
                new[k] = 100 * (ub / lb - 1)
            else:
                new[k] = v
        out.append(new)
    write_csv(DATA / 'cp_intermediate.csv', out)


CAMPAIGN = {'certificate_lb_s': 'certificate_v3_lb_s', 'gap_pct': 'gap_v3_pct',
            'excess_over_bound_s': 'excess_over_v3_bound_s', 'certified_gap_pct': 'certified_gap_v3_pct'}


def migrate_campaign():
    for name in ['kpi_campaign.csv', 'lower_bounds.csv']:
        p = DATA / name
        lines = p.read_text().splitlines(True)
        head = lines[0].rstrip('\r\n').split(',')
        if 'certificate_v3_lb_s' in head:
            continue
        end = lines[0][len(lines[0].rstrip('\r\n')):]
        lines[0] = ','.join(CAMPAIGN.get(h, h) for h in head) + end
        p.write_text(''.join(lines))


def migrate_exact():
    from exact_study import default_paths
    rs = rows('exact_reduced_instances.csv')
    if already(rs):
        return
    mult = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1, 8: 2, 9: 1, 10: 1, 11: 2, 12: 1, 13: 1, 14: 1}
    families = {'R1': [('HC', 0.), ('SVOC', 375.), ('HC', 750.)],
                'R2': [('HC', 0.), ('SVOC', 375.), ('SVOC_E', 750.)],
                'R3': [('HC', 0.), ('SVOC', 180.), ('HC', 360.), ('SVOC', 540.)],
                'R4': [('HC', 0.), ('SVOC', 375.), ('SVOC_E', 750.), ('HC', 1125.)],
                'R5': [('HC', 0.), ('SVOC', 180.), ('SVOC_E', 360.), ('HC', 540.), ('SVOC', 720.)]}
    cell = Cell(str(default_paths()[0]), mult)
    out = []
    for r in rs:
        c = StrengthenedCertificate.components(Instance(cell, families[r['instance']]))['certificate_lb_s']
        opt = float(r['opt_makespan_s'])
        new = {}
        for k, v in r.items():
            if k == 'lb_certificate_s':
                new['certificate_v3_lb_s'] = v
                new['certificate_lb_s'] = round(c, 2)
            elif k == 'certificate_slack_pct':
                new['certificate_v3_slack_pct'] = v
                new['certificate_slack_pct'] = round(100 * (opt - c) / opt, 2)
            else:
                new[k] = v
        out.append(new)
    write_csv(DATA / 'exact_reduced_instances.csv', out)


def design_config(case, alt):
    return ROOT / 'instances' / f'{case}_config.json' if alt == 'nominal' else \
        ROOT / 'instances' / 'design' / f'{case}__{alt}_config.json'


def migrate_design():
    for name in ['design_alternatives.csv', 'cp_design.csv']:
        rs = rows(name)
        if already(rs):
            continue
        out = []
        for r in rs:
            c = v4(design_config(r['case'], r['alternative']))['certificate_lb_s']
            new = {}
            for k, v in r.items():
                if k == 'certificate_lb_s':
                    new['certificate_v3_lb_s'] = v
                    new['certificate_lb_s'] = c
                else:
                    new[k] = v
            out.append(new)
        write_csv(DATA / name, out)


if __name__ == '__main__':
    migrate_reference()
    migrate_targeted()
    migrate_intermediate()
    migrate_design()
    migrate_campaign()
    migrate_exact()
    print('certificate fields migrated')
