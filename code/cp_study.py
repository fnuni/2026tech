"""CP-SAT reference study (package 4.x).

* ``full``          -- rolling-horizon CP (RH-CP) for the operational and the
                       hard-wait-constrained problem on CS1--CS15, followed by
                       a monolithic CP-SAT polishing phase warm-started with
                       the RH-CP schedule;
* ``intermediate``  -- monolithic conservative (primal) and relaxed (dual)
                       CP-SAT on intermediate instances with explicit limits;
* ``design``        -- hard-wait search on the structural design alternatives;
* ``sensitivity``   -- hard-wait search with auxiliary robot work and with the
                       stage-specific budget profile.

Every primal schedule is re-timed with the true movement matrix and passes
``cell_model.audit``. Time limits are wall-clock limits on the stated
hardware; time-limited results can differ between machines.
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import time
from pathlib import Path

import ortools

from cell_model import Cell, Instance, simulate
from cp_reference import (TransferGraph, WaitProfile, ConservativeCpModel, RelaxedCpModel,
                          TimeLagInsertion, ScheduleReconstructor, RollingHorizonCp)
from run_campaign import write_csv
from run_study import certificate
from revision_study import PROFILE_A, DesignAlternatives, cert_pair

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'validation' / 'cp'
CASES = ['cs1_nominal', 'cs2_medium', 'cs3_high', 'cs4_medium_s99', 'cs5_double', 'cs6_fullday',
         'cs7_pipc480', 'cs8_day180', 'cs9_day60', 'cs10_m2first', 'cs11_hcfirst',
         'cs12_m2heavy', 'cs13_day480', 'cs14_day180_480', 'cs15_tiled180']
POLICIES = ['URG', 'SRPT', 'BNA', 'LB-LA']


def environment():
    return {'python': platform.python_version(), 'ortools': ortools.__version__,
            'platform': platform.platform(), 'cpu_workers': 2}


def save(path, pickups, result, extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'pickups_cs': {f'{k[0]}|{k[1]}': v for k, v in pickups.items()},
                                'kpi': result['kpi'], **extra,
                                'environment': environment()}, separators=(',', ':')) + '\n')
    trace = path.with_name(path.stem + '_trace.json')
    trace.write_text(json.dumps(result, separators=(',', ':')) + '\n')


class ReferenceSearch:
    """RH-CP construction followed by monolithic polishing for one instance."""

    def __init__(self, inst, *, hard_wait, wait=300., window=30, commit=15, step=8.,
                 polish=120., seed=1):
        self.inst, self.hard_wait = inst, hard_wait
        self.g = TransferGraph(inst, wait)
        self.window, self.commit, self.step, self.polish, self.seed = window, commit, step, polish, seed
        self.rec = ScheduleReconstructor(self.g)

    def run(self):
        t0 = time.time()
        rh = RollingHorizonCp(self.g, hard_wait=self.hard_wait, window=self.window,
                              commit=self.commit, step_limit=self.step)
        pk, info = rh.run(self.seed)
        r_rh = self.rec.trace(pk, hard_wait=self.hard_wait, label='RH-CP')
        t_rh = time.time() - t0
        best_pk, best = pk, r_rh
        polish_status = ''
        if self.polish > 0:
            m = ConservativeCpModel(self.g, hard_wait=self.hard_wait, horizon_s=4 * 86400.)
            m.add_hint(pk)
            res = m.solve(self.polish, 2, self.seed)
            polish_status = res.status
            if res.pickups:
                r2 = self.rec.trace(res.pickups, hard_wait=self.hard_wait, label='CP-REF')
                if r2['kpi']['makespan_s'] < best['kpi']['makespan_s']:
                    best_pk, best = res.pickups, r2
        return {'rh': (pk, r_rh, t_rh, info), 'best': (best_pk, best, time.time() - t0),
                'polish_status': polish_status}


def full(args):
    rows = []
    for name in args.cases or CASES:
        cell = Cell(ROOT / 'instances' / f'{name}_config.json')
        inst = Instance(cell, cell.cfg['samples'])
        cert3, cert = cert_pair(inst)
        for hw in (True, False):
            search = ReferenceSearch(inst, hard_wait=hw, window=args.window, commit=args.commit,
                                     step=args.step, polish=args.polish)
            out = search.run()
            pk, r_rh, t_rh, info = out['rh']
            bpk, best, t_all = out['best']
            label = 'hard_wait' if hw else 'operational'
            save(OUT / name / f'{label}_rhcp.json', pk, r_rh, {'rh': info, 'wall_s': t_rh})
            save(OUT / name / f'{label}_best.json', bpk, best,
                 {'wall_s': t_all, 'polish_status': out['polish_status']})
            for method, r, wall in [('RH-CP', r_rh, t_rh), ('CP-REF', best, t_all)]:
                k = r['kpi']
                first = {a['sample']: a['pickup_s'] for a in r['tasks'] if a['from_stage'] == 0}
                ext = [first[s.sid] - s.release for s in inst.samples]
                rows.append({'case': name, 'problem': label, 'method': method,
                             'makespan_s': k['makespan_s'], 'violations': k['violations'],
                             'affected_samples': k['affected_samples'], 'max_wait_s': k['max_wait_s'],
                             'mean_external_s': sum(ext) / len(ext), 'max_external_s': max(ext),
                             'robot_util_pct': k['robot_util_pct'], 'certificate_v3_lb_s': cert3,
                             'certificate_lb_s': cert, 'gap_v3_pct': 100 * (k['makespan_s'] / cert3 - 1),
                             'gap_pct': 100 * (k['makespan_s'] / cert - 1), 'wall_s': wall,
                             'window': args.window, 'commit': args.commit, 'step_s': args.step,
                             'polish_s': args.polish})
            print(name, label, round(r_rh['kpi']['makespan_s'], 2), round(best['kpi']['makespan_s'], 2),
                  best['kpi']['violations'], flush=True)
            write_csv(ROOT / 'data' / args.output, rows)


def targeted(args):
    """Targeted hard-wait feasibility search on the design day (CS6)."""
    name = 'cs6_fullday'
    cell = Cell(ROOT / 'instances' / f'{name}_config.json')
    inst = Instance(cell, cell.cfg['samples'])
    cert3, cert = cert_pair(inst)
    g = TransferGraph(inst, 300.)
    rows = []
    t0 = time.time()
    ins = TimeLagInsertion(g, hard_wait=True).run()
    rec = ScheduleReconstructor(g)
    r_ins = rec.trace(ins, hard_wait=True, label='insertion')
    rows.append(('time-lag insertion', r_ins, time.time() - t0, ins))
    out = ReferenceSearch(inst, hard_wait=True, window=args.window, commit=args.commit,
                          step=args.step, polish=args.polish).run()
    pk, r_rh, t_rh, info = out['rh']
    rows.append((f'RH-CP ({args.step:g} s per window)', r_rh, t_rh, pk))
    bpk, best, t_all = out['best']
    rows.append((f'CP-REF (RH-CP + {args.polish:g} s polishing)', best, t_all, bpk))
    dual = RelaxedCpModel(g, hard_wait=True, horizon_s=4 * 86400.).solve(args.limit, 2, 1)
    out_rows = []
    for label, r, wall, pick in rows:
        k = r['kpi']
        first = {a['sample']: a['pickup_s'] for a in r['tasks'] if a['from_stage'] == 0}
        ext = [first[s.sid] - s.release for s in inst.samples]
        events = {}
        for s in inst.samples:
            events[s.release] = events.get(s.release, 0) + 1
            events[first[s.sid]] = events.get(first[s.sid], 0) - 1
        backlog = peak = 0
        for t, n in sorted(events.items()):
            backlog += n; peak = max(peak, backlog)
        out_rows.append({'method': label, 'makespan_s': k['makespan_s'], 'violations': k['violations'],
                         'affected_samples': k['affected_samples'], 'max_wait_s': k['max_wait_s'],
                         'mean_external_s': sum(ext) / len(ext), 'max_external_s': max(ext),
                         'p95_external_s': sorted(ext)[int(0.95 * len(ext)) - 1],
                         'max_external_backlog': peak, 'robot_util_pct': k['robot_util_pct'],
                         'mean_flow_s': k['mean_flow_s'], 'certificate_v3_lb_s': cert3, 'certificate_lb_s': cert,
                         'relaxed_cp_bound_s': dual.bound_s, 'relaxed_cp_status': dual.status,
                         'gap_pct': 100 * (k['makespan_s'] / cert - 1), 'wall_s': wall})
        save(OUT / name / f"targeted_{label.split()[0].lower()}.json", pick, r, {'wall_s': wall})
        print(out_rows[-1], flush=True)
    write_csv(ROOT / 'data' / 'cp_targeted_cs6.csv', out_rows)


def flow_polish(args):
    """Second polishing phase of CP-REF: keep the makespan, minimise total flow time.

    A pure makespan objective may defer individual admissions arbitrarily
    within the horizon; this phase removes such deferrals without
    increasing the makespan.
    """
    import csv as _csv
    name = 'cs6_fullday'
    cell = Cell(ROOT / 'instances' / f'{name}_config.json')
    inst = Instance(cell, cell.cfg['samples'])
    g = TransferGraph(inst, 300.)
    src = json.loads((OUT / name / 'targeted_cp-ref.json').read_text())
    pick = {(k.split('|')[0], int(k.split('|')[1])): v for k, v in src['pickups_cs'].items()}
    m = ConservativeCpModel(g, hard_wait=True, horizon_s=4 * 86400., objective='flow')
    m.model.add(m.cmax <= max(pick[sid, g.transfers[sid][-1].j] + int(round(g.transfers[sid][-1].l_max * 100 + 0.5))
                              for sid in g.transfers))
    m.add_hint(pick)
    res = m.solve(args.polish, 2, 3)
    rec = ScheduleReconstructor(g)
    old = json.loads((OUT / name / 'targeted_cp-ref_trace.json').read_text())
    if res.pickups:
        r = rec.trace(res.pickups, hard_wait=True, label='CP-REF')
        if r['kpi']['makespan_s'] <= old['kpi']['makespan_s'] + 1e-6:
            save(OUT / name / 'targeted_cp-ref.json', res.pickups, r,
                 {'wall_s': src.get('wall_s', 0.) + res.wall_s, 'flow_polish_s': res.wall_s})
    rows = list(_csv.DictReader((ROOT / 'data/cp_targeted_cs6.csv').open()))
    tr = json.loads((OUT / name / 'targeted_cp-ref_trace.json').read_text())
    first = {a['sample']: a['pickup_s'] for a in tr['tasks'] if a['from_stage'] == 0}
    ext = [first[s.sid] - s.release for s in inst.samples]
    ev = {}
    for s in inst.samples:
        ev[s.release] = ev.get(s.release, 0) + 1
        ev[first[s.sid]] = ev.get(first[s.sid], 0) - 1
    b = peak = 0
    for t, n in sorted(ev.items()):
        b += n; peak = max(peak, b)
    import numpy as np
    for row in rows:
        if row['method'].startswith('CP-REF'):
            k = tr['kpi']
            row.update({'method': f'CP-REF (RH-CP + {args.step:g}/{args.polish:g} s polishing)',
                        'makespan_s': k['makespan_s'], 'max_wait_s': k['max_wait_s'],
                        'mean_external_s': float(np.mean(ext)), 'max_external_s': max(ext),
                        'p95_external_s': float(np.percentile(ext, 95)), 'max_external_backlog': peak,
                        'robot_util_pct': k['robot_util_pct'], 'mean_flow_s': k['mean_flow_s'],
                        'gap_pct': 100 * (k['makespan_s'] / float(row['certificate_lb_s']) - 1),
                        'wall_s': float(row['wall_s']) + (res.wall_s if res.pickups else 0.)})
    write_csv(ROOT / 'data' / 'cp_targeted_cs6.csv', rows)
    print('flow polish', res.status, tr['kpi']['makespan_s'], max(ext), np.mean(ext), flush=True)


def repair(args):
    """Re-insert jobs with very long external waits at their earliest feasible admission.

    All other jobs keep their pickup times; the time-lag insertion places the
    removed job in the remaining robot and position capacity, so the repaired
    schedule stays hard-wait feasible and the makespan cannot increase unless
    the re-inserted job becomes the last one (checked).
    """
    import numpy as np
    name = 'cs6_fullday'
    cell = Cell(ROOT / 'instances' / f'{name}_config.json')
    inst = Instance(cell, cell.cfg['samples'])
    g = TransferGraph(inst, 300.)
    src = json.loads((OUT / name / 'targeted_cp-ref.json').read_text())
    pick = {(k.split('|')[0], int(k.split('|')[1])): v for k, v in src['pickups_cs'].items()}
    rec = ScheduleReconstructor(g)
    base = rec.trace(pick, hard_wait=True, label='CP-REF')
    for _ in range(10):
        first = {a['sample']: a['pickup_s'] for a in base['tasks'] if a['from_stage'] == 0}
        worst = max(inst.samples, key=lambda s: first[s.sid] - s.release)
        if first[worst.sid] - worst.release <= args.threshold:
            break
        ins = TimeLagInsertion(g, hard_wait=True)
        for (sid, j), P in pick.items():
            if sid == worst.sid:
                continue
            tr = g.transfers[sid][j - 1]
            lead, length = ins.model.robot_window(tr)
            ins.robot.add(P - lead, P + length)
        for sid, chain in g.transfers.items():
            if sid == worst.sid:
                continue
            for tr, nxt in zip(chain, chain[1:]):
                ins.res[tr.k_to].add(pick[sid, tr.j] + int(round(tr.l_max * 100 + 0.4999)), pick[sid, nxt.j])
        ins.insert(worst.sid)
        cand = dict(pick)
        cand.update({k: v for k, v in ins.pickups.items() if k[0] == worst.sid})
        r = rec.trace(cand, hard_wait=True, label='CP-REF')
        if r['kpi']['makespan_s'] <= base['kpi']['makespan_s'] + 1e-6 and r['kpi']['violations'] == 0:
            pick, base = cand, r
            print('repaired', worst.sid, round(first[worst.sid] - worst.release), '->',
                  round(next(a['pickup_s'] for a in r['tasks'] if a['sample'] == worst.sid and a['from_stage'] == 0) - worst.release), flush=True)
        else:
            break
    save(OUT / name / 'targeted_cp-ref.json', pick, base, {'wall_s': src.get('wall_s', 0.), 'repaired': True})
    rows = list(csv.DictReader((ROOT / 'data/cp_targeted_cs6.csv').open()))
    first = {a['sample']: a['pickup_s'] for a in base['tasks'] if a['from_stage'] == 0}
    ext = [first[s.sid] - s.release for s in inst.samples]
    ev = {}
    for s in inst.samples:
        ev[s.release] = ev.get(s.release, 0) + 1
        ev[first[s.sid]] = ev.get(first[s.sid], 0) - 1
    b = peak = 0
    for t, n in sorted(ev.items()):
        b += n; peak = max(peak, b)
    for row in rows:
        if row['method'].startswith('CP-REF'):
            k = base['kpi']
            row.update({'makespan_s': k['makespan_s'], 'max_wait_s': k['max_wait_s'],
                        'mean_external_s': float(np.mean(ext)), 'max_external_s': max(ext),
                        'p95_external_s': float(np.percentile(ext, 95)), 'max_external_backlog': peak,
                        'robot_util_pct': k['robot_util_pct'], 'mean_flow_s': k['mean_flow_s']})
    write_csv(ROOT / 'data' / 'cp_targeted_cs6.csv', rows)
    print('repair done', base['kpi']['makespan_s'], max(ext), float(np.mean(ext)), flush=True)


def intermediate(args):
    """Monolithic primal/dual CP-SAT on intermediate instances."""
    defs = [('cs1_nominal', None), ('cs2_medium', None), ('cs3_high', None), ('cs4_medium_s99', None),
            ('cs7_pipc480', None), ('cs5_double', None), ('cs8_day180', 69), ('cs8_day180', 115)]
    rows = []
    for name, prefix in defs:
        cell = Cell(ROOT / 'instances' / f'{name}_config.json')
        spec = cell.cfg['samples'] if prefix is None else cell.cfg['samples'][:prefix]
        inst = Instance(cell, spec)
        label = name if prefix is None else f'{name}_first{prefix}'
        cert3, cert = cert_pair(inst)
        pol = {p: simulate(inst, p, trace=False)['kpi'] for p in POLICIES}
        for hw in ((False, True) if (prefix is None or prefix <= 69) else (False,)):
            g = TransferGraph(inst, 300.)
            ins = TimeLagInsertion(g, hard_wait=hw).run()
            prim = ConservativeCpModel(g, hard_wait=hw, horizon_s=4 * 86400.)
            prim.add_hint(ins)
            rp = prim.solve(args.limit, 2, 1)
            rec = ScheduleReconstructor(g)
            tr = rec.trace(rp.pickups if rp.pickups else ins, hard_wait=hw, label='CP-primal')
            dual = RelaxedCpModel(g, hard_wait=hw, horizon_s=4 * 86400.)
            rd = dual.solve(args.limit, 2, 1)
            lb = max(cert, rd.bound_s or 0.)
            best_rule = min(POLICIES, key=lambda p: pol[p]['makespan_s'] if (not hw or pol[p]['hard_wait_feasible']) else 1e18)
            row = {'instance': label, 'jobs': inst.n, 'problem': 'hard_wait' if hw else 'operational',
                   'certificate_v3_lb_s': cert3, 'certificate_lb_s': cert, 'relaxed_cp_status': rd.status,
                   'relaxed_cp_bound_s': rd.bound_s, 'best_lower_bound_s': lb,
                   'primal_cp_status': rp.status, 'primal_s': tr['kpi']['makespan_s'],
                   'primal_violations': tr['kpi']['violations'], 'limit_s': args.limit,
                   'primal_wall_s': rp.wall_s, 'dual_wall_s': rd.wall_s}
            for p in POLICIES:
                row[f'{p}_makespan_s'] = pol[p]['makespan_s']
                row[f'{p}_affected'] = pol[p]['affected_samples']
            best_rule_ms = min(pol[p]['makespan_s'] for p in POLICIES)
            ub = min(tr['kpi']['makespan_s'], best_rule_ms) if not hw else tr['kpi']['makespan_s']
            row['best_upper_bound_s'] = ub
            row['bound_gap_pct'] = 100 * (ub / lb - 1)
            row['best_rule_excess_pct'] = 100 * (best_rule_ms / ub - 1) if not hw else ''
            rows.append(row)
            save(OUT / 'intermediate' / f"{label}_{row['problem']}.json",
                 rp.pickups if rp.pickups else ins, tr, {'relaxed_bound_s': rd.bound_s})
            print(label, row['problem'], round(cert, 1), rd.status, rd.bound_s, rp.status,
                  round(tr['kpi']['makespan_s'], 2), round(best_rule_ms, 2), flush=True)
            write_csv(ROOT / 'data' / 'cp_intermediate.csv', rows)


def design(args):
    rows = []
    for case, alt, path in DesignAlternatives().configs():
        if alt == 'nominal':
            continue
        cell = Cell(path)
        inst = Instance(cell, cell.cfg['samples'])
        out = ReferenceSearch(inst, hard_wait=True, window=args.window, commit=args.commit,
                              step=args.step, polish=args.polish).run()
        bpk, best, wall = out['best']
        save(OUT / 'design' / f'{case}__{alt}_hard_wait.json', bpk, best, {'wall_s': wall})
        k = best['kpi']
        rows.append({'case': case, 'alternative': alt, 'problem': 'hard_wait',
                     'makespan_s': k['makespan_s'], 'violations': k['violations'],
                     'certificate_lb_s': cert_pair(inst)[1], 'wall_s': wall})
        print('design', case, alt, k['makespan_s'], flush=True)
        write_csv(ROOT / 'data' / 'cp_design.csv', rows)


def sensitivity(args):
    variants = [('auxiliary_5s', {'auxiliary_s': 5.}, 300.), ('auxiliary_10s', {'auxiliary_s': 10.}, 300.),
                ('auxiliary_15s', {'auxiliary_s': 15.}, 300.),
                ('profile_A', {}, WaitProfile(PROFILE_A))]
    path = ROOT / 'data' / 'cp_sensitivity.csv'
    keep = {r['variant']: r for r in csv.DictReader(path.open())} if path.exists() else {}
    for name, opts, wait in variants:
        if args.variants and name not in args.variants:
            continue
        cell = Cell(ROOT / 'instances/cs6_fullday_config.json', **opts)
        inst = Instance(cell, cell.cfg['samples'])
        out = ReferenceSearch(inst, hard_wait=True, wait=wait, window=args.window,
                              commit=args.commit, step=args.step, polish=args.polish).run()
        bpk, best, wall = out['best']
        save(OUT / 'sensitivity' / f'cs6_{name}_hard_wait.json', bpk, best, {'wall_s': wall})
        k = best['kpi']
        keep[name] = {'case': 'cs6_fullday', 'variant': name, 'problem': 'hard_wait',
                      'makespan_s': k['makespan_s'], 'violations': k['violations'],
                      'robot_util_pct': k['robot_util_pct'], 'wall_s': wall}
        print('sensitivity', name, k['makespan_s'], k['violations'], flush=True)
        write_csv(path, [keep[v[0]] for v in variants if v[0] in keep])


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('study', choices=['full', 'targeted', 'flow', 'repair', 'intermediate', 'design', 'sensitivity'])
    ap.add_argument('--threshold', type=float, default=3 * 3600.)
    ap.add_argument('--cases', nargs='*')
    ap.add_argument('--window', type=int, default=30)
    ap.add_argument('--commit', type=int, default=15)
    ap.add_argument('--step', type=float, default=15.)
    ap.add_argument('--polish', type=float, default=0.)
    ap.add_argument('--limit', type=float, default=120.)
    ap.add_argument('--output', default='cp_reference.csv')
    ap.add_argument('--variants', nargs='*')
    args = ap.parse_args()
    {'full': full, 'targeted': targeted, 'flow': flow_polish, 'repair': repair, 'intermediate': intermediate, 'design': design,
     'sensitivity': sensitivity}[args.study](args)
