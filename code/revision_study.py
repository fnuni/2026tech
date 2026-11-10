"""Second-round study: reviewer-requested analyses for package 4.x.

Experiments (each writes one or more CSV files in ``data/``):

* weights      -- LB-LA penalty-weight grid on contrasting release patterns
* stochastic   -- processing/movement variability, unknown emulsions, 30 CRN
                  replications; three-day runs with carry-over
* design       -- structural alternatives (second Method-2 extractor, ...)
* robot        -- auxiliary robot work up to the industrial movement accounting
* profile      -- stage-specific waiting budgets
* cs15         -- complete CS15 release timeline (cadence check)
* scenarios    -- scenario provenance map used by README and Table 5

CP-SAT studies live in ``cp_study.py``. No parameter is fitted to the
reported outcomes; the weight grid and the alternative profiles are fixed a
priori and are reported in full.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from cell_model import Cell, Instance, Perturbation, simulate
from run_campaign import write_csv
from run_study import certificate, diagnostics

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
POLICIES = ['URG', 'SRPT', 'BNA', 'LB-LA']
DAY = 86400.


def load(name, **cell_options):
    cell = Cell(ROOT / 'instances' / f'{name}_config.json', **cell_options)
    return cell, Instance(cell, cell.cfg['samples'])


def pipc_share(result, inst):
    """Share of violation events whose destination is Pipet C (type 10)."""
    smap = {s.sid: s for s in inst.samples}
    budget = result['parameters']
    W = budget['wait_budget_s']
    ev = to_c = 0
    for a in result['tasks']:
        if a['from_stage'] == 0:
            continue
        s = smap[a['sample']]
        lim = budget.get('wait_budget_by_origin_type', {}).get(s.seq[a['from_stage']], W)
        if a['technological_wait_s'] > lim + 1e-8:
            ev += 1
            to_c += s.seq[a['to_stage']] == 10
    return 100. * to_c / ev if ev else 0.


# ------------------------------------------------------------------ weights --
class WeightSensitivity:
    W_VIOL = [0., 150., 300., 600., 1200., 2400.]
    W_OVER = [0., 30., 60., 120., 240., 480.]
    CASES = ['cs2_medium', 'cs6_fullday', 'cs8_day180', 'cs9_day60']

    def run(self):
        rows = []
        for name in self.CASES:
            _, inst = load(name)
            urg = simulate(inst, 'URG', trace=False)['kpi']
            for wv in self.W_VIOL:
                for wo in self.W_OVER:
                    k = simulate(inst, 'LB-LA', trace=False, w_viol=wv, w_over=wo)['kpi']
                    rows.append({'case': name, 'w_viol': wv, 'w_over': wo,
                                 'makespan_s': k['makespan_s'], 'violations': k['violations'],
                                 'affected_samples': k['affected_samples'],
                                 'urg_makespan_s': urg['makespan_s'],
                                 'urg_violations': urg['violations'],
                                 'urg_affected': urg['affected_samples'],
                                 'default': wv == 600. and wo == 120.})
            print('weights', name, flush=True)
        write_csv(DATA / 'weight_sensitivity.csv', rows)
        return rows


# --------------------------------------------------------------- stochastic --
class LognormalPerturbation(Perturbation):
    """Mean-preserving log-normal noise with common random numbers.

    Each draw is keyed by (replication, sample, stage, kind), so every policy
    faces the same realised duration for the same operation or transfer.
    """

    stochastic = True

    def __init__(self, seed, cv_proc, cv_move):
        self.seed, self.cv_proc, self.cv_move = seed, cv_proc, cv_move

    def _z(self, *key):
        h = hashlib.blake2b(repr((self.seed,) + key).encode(), digest_size=8).digest()
        u = (int.from_bytes(h, 'big') + 0.5) / 2**64
        from statistics import NormalDist
        return NormalDist().inv_cdf(u)

    def _factor(self, cv, *key):
        if cv <= 0:
            return 1.
        sigma = math.sqrt(math.log(1 + cv * cv))
        return math.exp(-0.5 * sigma * sigma + sigma * self._z(*key))

    def processing(self, sid, stage, nominal):
        return nominal * self._factor(self.cv_proc, sid, stage, 'p') if nominal > 0 else nominal

    def move(self, kind, sid, stage, nominal):
        return nominal * self._factor(self.cv_move, sid, stage, kind) if nominal > 0 else nominal


class ScenarioBuilder:
    """Release lists for multi-day runs and instances with hidden emulsions."""

    @staticmethod
    def repeat_days(spec, days):
        out = []
        n = len(spec)
        for d in range(days):
            for j, x in enumerate(spec):
                out.append({'id': f's{d * n + j + 1}', 'type': x['type'],
                            'release_s': x['release_s'] + d * DAY, 'day': d + 1})
        return out

    @staticmethod
    def instance(cell, spec, seed=None, p_emulsion=0.30):
        """Method-2 analytical jobs enter as SVOC; emulsion is revealed after stirring."""
        inst = Instance(cell, [{k: v for k, v in x.items() if k != 'day'} for x in spec])
        if seed is None:
            return inst
        rng = np.random.default_rng(seed)
        seq_e, p_e = cell.route('SVOC_E')
        seq_n, p_n = cell.route('SVOC')
        stir = seq_n.index(8)
        for s in inst.samples:
            if s.stype in ('SVOC', 'SVOC_E'):
                emulsion = rng.random() < p_emulsion
                s.stype, s.seq, s.p = 'SVOC', list(seq_n), list(p_n)
                if emulsion:
                    s.reveal = (stir, list(seq_e), list(p_e), 'SVOC_E')
        return inst


class StochasticStudy:
    REPS = 30
    SETTINGS = [  # label, case, days, cv_proc, cv_move, hidden emulsions
        ('CS6 low variability', 'cs6_fullday', 1, 0.10, 0.05, True),
        ('CS6 high variability', 'cs6_fullday', 1, 0.20, 0.10, True),
        ('CS8 low variability', 'cs8_day180', 1, 0.10, 0.05, True),
        ('CS6 three days, deterministic', 'cs6_fullday', 3, 0.0, 0.0, False),
        ('CS6 three days, low variability', 'cs6_fullday', 3, 0.10, 0.05, True),
    ]
    VARIANTS = [('URG', {}), ('SRPT', {}), ('BNA', {}), ('LB-LA', {}),
                ('LB-LA w_over=0', {'w_over': 0.})]

    def run(self):
        rows = []
        for label, case, days, cvp, cvm, hidden in self.SETTINGS:
            cell, base = load(case)
            spec = ScenarioBuilder.repeat_days(cell.cfg['samples'], days)
            deterministic = cvp == 0 and cvm == 0 and not hidden
            reps = 1 if deterministic else self.REPS
            for rep in range(reps):
                seed = 1000 + rep
                for name, opts in self.VARIANTS:
                    policy = name.split()[0]
                    inst = ScenarioBuilder.instance(cell, spec, seed if hidden else None)
                    pert = None if deterministic else LognormalPerturbation(seed, cvp, cvm)
                    r = simulate(inst, policy, trace=False, perturbation=pert, **opts)
                    rows.append(self.summarise(label, case, days, rep, name, inst, spec, r))
            print('stochastic', label, flush=True)
        write_csv(DATA / 'stochastic_replications.csv', rows)
        write_csv(DATA / 'stochastic_summary.csv', self.summary(rows))
        return rows

    def rebuild_summary(self):
        """Recompute the summary from the released replications (no new simulation)."""
        rows = list(csv.DictReader((DATA / 'stochastic_replications.csv').open()))
        write_csv(DATA / 'stochastic_summary.csv', self.summary(rows))

    @staticmethod
    def summarise(label, case, days, rep, name, inst, spec, r):
        k = r['kpi']
        day_of = {f's{j + 1}': x.get('day', 1) for j, x in enumerate(spec)}
        first = {a['sample']: a['pickup_s'] for a in r['tasks'] if a['from_stage'] == 0}
        events = defaultdict(int)
        for s in inst.samples:
            events[s.release] += 1
            events[first[s.sid]] -= 1
        backlog = peak = 0
        for t, n in sorted(events.items()):
            backlog += n
            peak = max(peak, backlog)
        row = {'setting': label, 'case': case, 'days': days, 'replication': rep,
               'policy': name, 'makespan_s': k['makespan_s'], 'violations': k['violations'],
               'affected_samples': k['affected_samples'], 'max_wait_s': k['max_wait_s'],
               'robot_util_pct': k['robot_util_pct'], 'max_external_backlog': peak,
               'mean_external_s': float(np.mean([first[s.sid] - s.release for s in inst.samples])),
               'emulsion_jobs': sum(s.stype == 'SVOC_E' for s in inst.samples),
               'pipc_violation_share_pct': pipc_share(r, inst)}
        for d in range(1, days + 1):
            end = d * DAY
            row[f'completed_by_day{d}'] = sum(c <= end for c in r['completions'].values())
            row[f'carry_over_day{d}'] = sum(1 for s in inst.samples if s.release < end
                                            and r['completions'][s.sid] > end)
            bad = {a['sample'] for a in r['tasks'] if a['from_stage'] > 0
                   and a['technological_wait_s'] > 300 + 1e-8 and day_of[a['sample']] == d}
            row[f'affected_day{d}'] = len(bad)
        return row

    @staticmethod
    def summary(rows):
        out = []
        keys = sorted({(r['setting'], r['policy']) for r in rows},
                      key=lambda x: (x[0], x[1]))
        by = defaultdict(list)
        for r in rows:
            by[r['setting'], r['policy']].append(r)
        for setting, policy in keys:
            rs = by[setting, policy]
            ref = {r['replication']: r for r in by[setting, 'URG']}
            row = {'setting': setting, 'policy': policy, 'replications': len(rs)}
            for m in ['makespan_s', 'violations', 'affected_samples', 'max_external_backlog',
                      'mean_external_s', 'pipc_violation_share_pct', 'robot_util_pct']:
                x = np.array([float(r[m]) for r in rs])
                row[m + '_mean'] = float(x.mean())
                row[m + '_ci95'] = float(1.96 * x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else 0.
            d = np.array([float(r['makespan_s']) - float(ref[r['replication']]['makespan_s']) for r in rs])
            v = np.array([float(r['affected_samples']) - float(ref[r['replication']]['affected_samples']) for r in rs])
            row['makespan_vs_urg_mean_s'] = float(d.mean())
            row['makespan_vs_urg_ci95_s'] = float(1.96 * d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else 0.
            row['affected_vs_urg_mean'] = float(v.mean())
            row['affected_vs_urg_ci95'] = float(1.96 * v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else 0.
            if policy == 'LB-LA w_over=0':  # direct paired comparison with the default LB-LA
                base = {r['replication']: r for r in by[setting, 'LB-LA']}
                for m, key in [('affected_samples', 'affected_vs_lbla'), ('makespan_s', 'makespan_vs_lbla')]:
                    x = np.array([float(r[m]) - float(base[r['replication']][m]) for r in rs])
                    row[key + '_mean'] = float(x.mean())
                    row[key + '_ci95'] = float(1.96 * x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else 0.
            row['share_within_24h_pct'] = 100. * np.mean([float(r['makespan_s']) <= DAY * int(r['days']) for r in rs])
            for dday in range(1, 4):
                col = f'carry_over_day{dday}'
                if col in rs[0] and rs[0][col] != '':
                    row[col + '_mean'] = float(np.mean([float(r[col]) for r in rs]))
                    row[f'affected_day{dday}_mean'] = float(np.mean([float(r[f'affected_day{dday}']) for r in rs]))
            out.append(row)
        # rank frequencies by (affected, makespan) per replication
        return out


# ------------------------------------------------------------------- design --
class DesignAlternatives:
    """Structural alternatives: additional single-unit modules."""

    T_DWELL = None

    @staticmethod
    def add_station(cfg, type_k, name, xy):
        cfg = copy.deepcopy(cfg)
        pos = cfg['_layout_positions']
        vids = list(pos)
        offd = [math.dist(pos[a], pos[b]) for a in vids for b in vids if a != b]
        v = cfg['_transport_model']['v_gantry_ms']
        dwell = 15. - (sum(offd) / len(offd)) / v
        new = f'v{len(vids) + 1}'
        pos[new] = list(xy)
        cfg['stations'][new] = {'type_k': type_k, 'name': name}
        T = cfg['transport_s']
        T[new] = {}
        for u in vids:
            t = round(dwell + math.dist(pos[u], xy) / v, 2)
            T[u][new] = t
            T[new][u] = t
        T[new][new] = 0.
        return cfg

    ALTERNATIVES = {
        'nominal': [],
        'second_pipet_c': [(10, 'Pipet C (extr. M2) #2', (1.675, 0.675))],
        'second_pipet_c_and_d': [(10, 'Pipet C (extr. M2) #2', (1.675, 0.675)),
                                 (14, 'Pipet D (GC-vial) #2', (1.925, 0.675))],
    }
    CASES = ['cs6_fullday', 'cs8_day180', 'cs12_m2heavy']

    def configs(self):
        for case in self.CASES:
            base = json.loads((ROOT / 'instances' / f'{case}_config.json').read_text())
            for alt, adds in self.ALTERNATIVES.items():
                cfg = base
                for k, nm, xy in adds:
                    cfg = self.add_station(cfg, k, nm, xy)
                name = case if alt == 'nominal' else f'{case}__{alt}'
                if alt != 'nominal':
                    cfg['instance_id'] = name
                    cfg['_comment'] = ('Hypothetical design alternative: ' + alt +
                                       '; added positions use Equation (1) with the original dwell and speed.')
                    path = ROOT / 'instances' / 'design' / f'{name}_config.json'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(cfg, separators=(',', ':')) + '\n')
                else:
                    path = ROOT / 'instances' / f'{case}_config.json'
                yield case, alt, path

    def run(self):
        rows = []
        for case, alt, path in self.configs():
            cell = Cell(path)
            inst = Instance(cell, cell.cfg['samples'])
            cert3, cert = cert_pair(inst)
            for policy in POLICIES:
                r = simulate(inst, policy, trace=False)
                k = r['kpi']
                _, _, op = diagnostics(inst, r, case, policy)
                rows.append({'case': case, 'alternative': alt, 'policy': policy,
                             'makespan_s': k['makespan_s'], 'violations': k['violations'],
                             'affected_samples': k['affected_samples'],
                             'max_external_backlog': op['max_external_backlog'],
                             'p95_external_s': op['p95_external_s'],
                             'certificate_v3_lb_s': cert3, 'certificate_lb_s': cert,
                             'pipc_violation_share_pct': pipc_share(r, inst)})
            print('design', case, alt, flush=True)
        write_csv(DATA / 'design_alternatives.csv', rows)
        return rows


# -------------------------------------------------------------------- robot --
class RobotLoadStudy:
    """Auxiliary robot service up to the industrial movement accounting."""

    AUX = [0., 5., 10., 15., 17.5, 20., 25., 30., 35.]
    INDUSTRIAL = {'movements': 5050, 'jobs': 160, 'movement_s': 15., 'load_pct': 88.}

    def accounting(self):
        _, inst = load('cs6_fullday')
        transfers = sum(len(s.seq) - 1 for s in inst.samples)
        per_type = defaultdict(list)
        for s in inst.samples:
            per_type[s.stype].append(len(s.seq) - 1)
        analytical = [len(s.seq) - 1 for s in inst.samples if s.stype in ('HC', 'SVOC', 'SVOC_E')]
        ind = self.INDUSTRIAL
        mean_tr = sum(analytical) / len(analytical)
        model_mov_job = 2 * mean_tr                      # loaded + empty leg per transfer
        ind_mov_job = ind['movements'] / ind['jobs']
        residual_job = ind_mov_job - model_mov_job
        a_equiv = residual_job * ind['movement_s'] / mean_tr
        rows = [{'item': 'loaded_transfers_per_job_' + t, 'value': sum(v) / len(v)} for t, v in sorted(per_type.items())]
        rows += [{'item': 'loaded_transfers_230_jobs', 'value': transfers},
                 {'item': 'model_movements_per_analytical_job', 'value': model_mov_job},
                 {'item': 'industrial_movements_per_analytical_job', 'value': ind_mov_job},
                 {'item': 'unresolved_movements_per_analytical_job', 'value': residual_job},
                 {'item': 'industrial_robot_s_160_jobs', 'value': ind['movements'] * ind['movement_s']},
                 {'item': 'model_robot_s_160_analytical_jobs', 'value': 160 * model_mov_job * 15.},
                 {'item': 'equivalent_auxiliary_s_per_transfer', 'value': a_equiv},
                 {'item': 'model_robot_load_230_jobs_at_equivalent_pct',
                  'value': 100 * transfers * (30. + a_equiv) / DAY}]
        # Robot term of the certificate with mandatory empty legs (Remark 1).
        cell = inst.cell
        r_min = sum(cell.m_min(s.seq[j - 1], s.seq[j]) for s in inst.samples for j in range(1, len(s.seq)))
        e_min = min(min(cell.empty(u, v) for u in cell.stations for v in cell.stations if u != v),
                    min(x for s in inst.samples for x in s.p if x > 0))
        a_crit = (DAY - r_min - (transfers - 1) * e_min) / transfers
        rows += [{'item': 'robot_min_loaded_work_s', 'value': r_min},
                 {'item': 'e_min_s', 'value': e_min},
                 {'item': 'robot_bound_nominal_s', 'value': r_min + (transfers - 1) * e_min},
                 {'item': 'robot_bound_at_equivalent_s', 'value': r_min + transfers * a_equiv + (transfers - 1) * e_min},
                 {'item': 'robot_certificate_threshold_s', 'value': a_crit}]
        write_csv(DATA / 'robot_accounting.csv', rows)
        return a_equiv

    def run(self):
        a_equiv = self.accounting()
        rows = []
        for a in self.AUX + [round(a_equiv, 2)]:
            _, inst = load('cs6_fullday', auxiliary_s=a)
            for policy in POLICIES:
                r = simulate(inst, policy, trace=False)
                k = r['kpi']
                rows.append({'case': 'cs6_fullday', 'auxiliary_s': a, 'policy': policy,
                             'makespan_s': k['makespan_s'], 'violations': k['violations'],
                             'affected_samples': k['affected_samples'],
                             'robot_util_pct': k['robot_util_pct'],
                             'robot_busy_s': k['robot_loaded_s'] + k['robot_empty_s'] + k['robot_auxiliary_s'],
                             'pipc_violation_share_pct': pipc_share(r, inst)})
            print('robot', a, flush=True)
        # Industrial robot-qualification volume: 160 analytical jobs per day.
        cell, _ = load('cs6_fullday', auxiliary_s=round(a_equiv, 2))
        spec = [x for x in cell.cfg['samples'] if x['type'] in ('HC', 'SVOC', 'SVOC_E')][:160]
        spec = [{'id': f's{j + 1}', 'type': x['type'], 'release_s': j * DAY / 160} for j, x in enumerate(spec)]
        for a in [0., round(a_equiv, 2)]:
            cell = Cell(ROOT / 'instances/cs6_fullday_config.json', auxiliary_s=a)
            inst = Instance(cell, spec)
            for policy in POLICIES:
                r = simulate(inst, policy, trace=False)
                k = r['kpi']
                rows.append({'case': 'rq160_540s', 'auxiliary_s': a, 'policy': policy,
                             'makespan_s': k['makespan_s'], 'violations': k['violations'],
                             'affected_samples': k['affected_samples'],
                             'robot_util_pct': k['robot_util_pct'],
                             'robot_busy_s': k['robot_loaded_s'] + k['robot_empty_s'] + k['robot_auxiliary_s'],
                             'pipc_violation_share_pct': pipc_share(r, inst)})
        (ROOT / 'instances' / 'derived').mkdir(parents=True, exist_ok=True)
        (ROOT / 'instances' / 'derived' / 'rq160_540s_samples.json').write_text(json.dumps(spec, indent=1) + '\n')
        write_csv(DATA / 'robot_load_sensitivity.csv', rows)
        return rows


# ------------------------------------------------------------------ profile --
PROFILE_A = {6: 600., 8: 600., 11: 150., 13: 150., 14: 600.}   # a priori, illustrative


class StageProfileStudy:
    """Uniform 300 s budget versus an a priori stage-specific profile."""

    def run(self):
        rows = []
        for case in ['cs6_fullday', 'cs8_day180']:
            _, inst = load(case)
            for policy in POLICIES:
                ref = simulate(inst, policy, trace=False)
                smap = {s.sid: s for s in inst.samples}
                ev = bad = 0
                badset = set()
                by_k = defaultdict(int)
                for a in ref['tasks']:
                    if a['from_stage'] == 0:
                        continue
                    k = smap[a['sample']].seq[a['from_stage']]
                    if a['technological_wait_s'] > PROFILE_A.get(k, 300.) + 1e-8:
                        ev += 1
                        badset.add(a['sample'])
                        by_k[k] += 1
                rer = simulate(inst, policy, trace=False, wait_budget=PROFILE_A)
                kk = rer['kpi']
                rows.append({'case': case, 'policy': policy,
                             'uniform_violations': ref['kpi']['violations'],
                             'uniform_affected': ref['kpi']['affected_samples'],
                             'profile_fixed_violations': ev, 'profile_fixed_affected': len(badset),
                             'profile_fixed_events_before_pipc': sum(
                                 1 for a in ref['tasks'] if a['from_stage'] > 0
                                 and a['technological_wait_s'] > PROFILE_A.get(smap[a['sample']].seq[a['from_stage']], 300.) + 1e-8
                                 and smap[a['sample']].seq[a['to_stage']] == 10),
                             'profile_fixed_events_from_concentrator': by_k.get(11, 0),
                             'profile_fixed_events_from_centrifuge': by_k.get(13, 0),
                             'profile_rerun_makespan_s': kk['makespan_s'],
                             'profile_rerun_violations': kk['violations'],
                             'profile_rerun_affected': kk['affected_samples']})
            print('profile', case, flush=True)
        write_csv(DATA / 'stage_budget_profile.csv', rows)
        return rows


# --------------------------------------------------------------- scenarios --
class ScenarioMap:
    DESCRIPTION = {
        'cs1_nominal': ('generator, seed 42, scale 0.1', '375 s analytical cadence; 23 jobs (10 HC, 7 SVOC, 3 SVOC_E, QC1, QC2, Blank)'),
        'cs2_medium': ('generator, seed 42, scale 0.1', 'CS1 sequence at 180 s cadence'),
        'cs3_high': ('generator, seed 42, scale 0.1', 'CS1 sequence at 60 s cadence'),
        'cs4_medium_s99': ('generator, seed 99, scale 0.1', 'CS2 release instants; seed 99 places emulsions on Method-2 slots 4, 7, 10 (CS2: 1, 2, 5) and swaps QC1/Blank'),
        'cs5_double': ('generator, seed 42, scale 0.2', '46 jobs at 150 s cadence'),
        'cs6_fullday': ('generator, seed 42, scale 1.0', 'design day: 230 jobs, 375 s analytical cadence'),
        'cs7_pipc480': ('generator, seed 42, scale 0.1', 'CS2 with Pipet C 480 s'),
        'cs8_day180': ('derived from CS6', 'CS6 release times x 180/375'),
        'cs9_day60': ('derived from CS6', 'CS6 release times x 60/375'),
        'cs10_m2first': ('derived from CS8', 'Method-2 analytical routes first at CS8 instants'),
        'cs11_hcfirst': ('derived from CS8', 'HC routes first at CS8 instants'),
        'cs12_m2heavy': ('derived from CS8', '40 HC slots replaced by 28 SVOC + 12 SVOC_E'),
        'cs13_day480': ('derived from CS6', 'CS6 with Pipet C 480 s'),
        'cs14_day180_480': ('derived from CS8', 'CS8 with Pipet C 480 s'),
        'cs15_tiled180': ('derived from CS2', 'ten CS2 blocks at 3600 s offsets (= 20 x 180 s)'),
    }

    def run(self):
        rows = []
        for i, (name, (source, change)) in enumerate(self.DESCRIPTION.items(), 1):
            cfg = json.loads((ROOT / 'instances' / f'{name}_config.json').read_text())
            rel = [x['release_s'] for x in cfg['samples']]
            pipc = next(p for k, p in zip(cfg['routes']['SVOC']['type_sequence'],
                                          cfg['routes']['SVOC']['p_s_sequence']) if k == 10)
            rows.append({'case': f'CS{i}', 'instance': name, 'input_file': f'instances/{name}_config.json',
                         'provenance': source, 'change': change, 'jobs': len(rel),
                         'first_release_s': min(rel), 'last_release_s': max(rel), 'pipc_s': pipc})
        write_csv(DATA / 'scenario_map.csv', rows)
        cfg = json.loads((ROOT / 'instances/cs15_tiled180_config.json').read_text())
        tl = []
        for j, x in enumerate(cfg['samples']):
            block = j // 23
            tl.append({'sample_id': x['id'], 'block': block + 1, 'type': x['type'],
                       'release_s': x['release_s'],
                       'analytical': x['type'] in ('HC', 'SVOC', 'SVOC_E')})
        an = sorted(r['release_s'] for r in tl if r['analytical'])
        gaps = sorted({round(b - a, 6) for a, b in zip(an, an[1:])})
        assert gaps == [180.0], gaps
        write_csv(DATA / 'cs15_timeline.csv', tl)
        return rows


# ------------------------------------------------------------------- bounds --
class StrengthenedCertificate:
    """Certificate v4: head/tail bound over release- and tail-ordered subsets.

    For every subset J' of the operations of module type k,
    min head(J') + p(J')/m_k + min tail(J') is a valid makespan bound
    (the argument of the head/tail proposition applied to J'). The v4
    certificate maximises it over the nested subsets {head >= h} and
    {tail >= t}; v3 used J' = J_k only. The robot term adds the mandatory
    empty-leg separation (Remark on mandatory empty legs).
    """

    @staticmethod
    def components(inst):
        from exact_study import Bound, CapacityBound, ReleasePathBound, HeadTailBound
        cell = inst.cell
        ops = defaultdict(list)
        for s in inst.samples:
            pre = Bound._prefix(cell, s)
            tot = pre[-1] + s.p[-1]
            for j, k in enumerate(s.seq):
                if s.p[j] > 0:
                    ops[k].append((s.release + pre[j], s.p[j], tot - pre[j] - s.p[j]))
        best, arg = 0., None
        for k, L in ops.items():
            m = len(cell.by_k[k])
            acc, mt = 0., float('inf')
            for h, p, t in sorted(L, reverse=True):
                acc += p; mt = min(mt, t)
                if h + acc / m + mt > best:
                    best, arg = h + acc / m + mt, k
            acc, mh = 0., float('inf')
            for h, p, t in sorted(L, key=lambda x: x[2], reverse=True):
                acc += p; mh = min(mh, h)
                if mh + acc / m + t > best:
                    best, arg = mh + acc / m + t, k
        n = sum(len(s.seq) - 1 for s in inst.samples)
        r_min = sum(cell.m_min(s.seq[j - 1], s.seq[j]) for s in inst.samples for j in range(1, len(s.seq)))
        e_min = min(min(cell.empty(u, v) for u in cell.stations for v in cell.stations if u != v),
                    min(x for s in inst.samples for x in s.p if x > 0))
        comp = {'capacity_lb_s': CapacityBound().value(inst),
                'robot_lb_s': r_min + n * cell.auxiliary_s + (n - 1) * e_min,
                'release_path_lb_s': ReleasePathBound().value(inst),
                'headtail_lb_s': HeadTailBound().value(inst),
                'headtail_subset_lb_s': best}
        comp['certificate_v3_lb_s'] = max(comp['capacity_lb_s'], r_min, comp['release_path_lb_s'], comp['headtail_lb_s'])
        keys = ['capacity_lb_s', 'robot_lb_s', 'release_path_lb_s', 'headtail_subset_lb_s']
        comp['certificate_lb_s'] = max(comp[k] for k in keys)
        comp['binding'] = {'capacity_lb_s': 'machine', 'robot_lb_s': 'robot', 'release_path_lb_s': 'path',
                           'headtail_subset_lb_s': f'head/tail ({arg})'}[max(keys, key=lambda k: comp[k])]
        return comp

    def run(self):
        rows = []
        for row in csv.DictReader((DATA / 'scenario_map.csv').open()):
            cell = Cell(ROOT / row['input_file'])
            inst = Instance(cell, cell.cfg['samples'])
            rows.append({'case': row['instance'], **self.components(inst)})
        write_csv(DATA / 'lower_bounds_v4.csv', rows)
        return rows


def cert_pair(inst):
    """(first-version certificate, strengthened certificate of Equation (cert))."""
    c = StrengthenedCertificate.components(inst)
    return c['certificate_v3_lb_s'], c['certificate_lb_s']


EXPERIMENTS = {'bounds': StrengthenedCertificate, 'weights': WeightSensitivity, 'stochastic': StochasticStudy,
               'design': DesignAlternatives, 'robot': RobotLoadStudy,
               'profile': StageProfileStudy, 'scenarios': ScenarioMap}

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('experiments', nargs='*', default=list(EXPERIMENTS))
    args = ap.parse_args()
    for name in args.experiments:
        EXPERIMENTS[name]().run()
