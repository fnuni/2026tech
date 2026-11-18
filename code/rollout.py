"""Rollout control for the robotic cell (package 4.2.0).

A rollout policy (Bertsekas) evaluates each candidate action at a decision
epoch by applying it and completing the whole schedule with a base
dispatching rule in the executable model; it then executes the candidate with
the best completed schedule. The implementation is fortified: the best
complete schedule found so far is retained, so the final schedule is never
worse than the base rule under the chosen cost. ``DispatchRule`` subclasses
reproduce the ranking of ``cell_model.simulate`` exactly (checked by
``Validation``); the executed schedule is audited by ``cell_model.audit``.

Two action spaces are compared. ``RolloutPolicy(hold=False)`` keeps the
non-idling action space of the rules: the robot always executes a transfer
when one is possible. ``hold=True`` adds, at admission epochs (no pending
transfer inside the cell), the option to keep the robot idle until the next
completion or release, so that admissions can be delayed.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path

from cell_model import PRIORITY, Budget, Cell, Instance, audit, sid_key, simulate

ROOT = Path(__file__).resolve().parents[1]
HOLD = ('hold',)


class CellState:
    """Copyable deterministic state of the event-driven model."""

    __slots__ = ('inst', 'cell', 'samples', 'order', 'stage', 'loc', 'ready', 'occupied',
                 'done', 't', 'robot', 'completed_m1', 'violations', 'affected', 'budget',
                 'tasks', 'ops')

    def __init__(self, inst, budget=300., record=False):
        self.inst, self.cell = inst, inst.cell
        assert all(s.reveal is None for s in inst.samples), 'deterministic instances only'
        self.samples = {s.sid: s for s in inst.samples}
        self.order = [s.sid for s in inst.samples]
        self.stage = {sid: 0 for sid in self.order}
        self.loc = {s.sid: self.cell.by_k[s.seq[0]][0] for s in inst.samples}
        self.ready = {s.sid: s.release for s in inst.samples}
        self.occupied = {v: None for v in self.cell.stations}
        self.done = {}
        self.t = 0.
        self.robot = self.cell.initial_robot
        self.completed_m1 = 0
        self.violations = 0
        self.affected = set()
        self.budget = Budget(budget)
        self.tasks = [] if record else None
        self.ops = {sid: [] for sid in self.order} if record else None

    def copy(self):
        c = object.__new__(CellState)
        c.inst, c.cell, c.samples, c.order, c.budget = self.inst, self.cell, self.samples, self.order, self.budget
        c.stage, c.loc, c.ready = dict(self.stage), dict(self.loc), dict(self.ready)
        c.occupied, c.done, c.affected = dict(self.occupied), dict(self.done), set(self.affected)
        c.t, c.robot, c.completed_m1, c.violations = self.t, self.robot, self.completed_m1, self.violations
        c.tasks = c.ops = None
        return c

    @property
    def finished(self):
        return len(self.done) == len(self.order)

    @property
    def makespan(self):
        return max(self.done.values())

    def waiting(self):
        t = self.t + 1e-8
        return [sid for sid in self.order if sid not in self.done and self.ready[sid] <= t]

    def next_event(self):
        future = [self.ready[sid] for sid in self.order if sid not in self.done and self.ready[sid] > self.t + 1e-8]
        return min(future) if future else None

    def actions(self, waiting, admission_cap=None):
        samples, stage, cell = self.samples, self.stage, self.cell
        upstream = None
        if admission_cap is not None:
            upstream = sum(1 for sid, s in samples.items() if sid not in self.done
                           and 10 in s.seq and 0 < stage[sid] <= s.seq.index(10))
        out = []
        for sid in waiting:
            s = samples[sid]
            j = stage[sid] + 1
            if admission_cap is not None and stage[sid] == 0 and 10 in s.seq and upstream >= admission_cap:
                continue
            for v in cell.by_k[s.seq[j]]:
                if s.seq[j] != 5 and self.occupied[v] is not None:
                    continue
                empty = cell.empty(self.robot, self.loc[sid])
                loaded = cell.m(self.loc[sid], v)
                out.append({'sid': sid, 'dest': v, 'next': j, 'empty': empty, 'loaded': loaded,
                            'dur': cell.auxiliary_s + empty + loaded})
        return out

    def hold(self):
        nxt = self.next_event()
        assert nxt is not None
        self.t = nxt

    def apply(self, a):
        cell, sid = self.cell, a['sid']
        s = self.samples[sid]
        old = self.stage[sid]
        pickup = self.t + cell.auxiliary_s + a['empty']
        arrival = pickup + a['loaded']
        wait = pickup - self.ready[sid] if old > 0 else 0.
        if old > 0 and wait > self.budget(s.seq[old]) + 1e-8:
            self.violations += 1
            self.affected.add(sid)
        if self.tasks is not None:
            self.tasks.append({'sample': sid, 'from_stage': old, 'to_stage': a['next'],
                               'origin': self.loc[sid], 'destination': a['dest'], 'start_s': self.t,
                               'pickup_s': pickup, 'delivery_s': arrival, 'empty_s': a['empty'],
                               'loaded_s': a['loaded'], 'auxiliary_s': cell.auxiliary_s,
                               'technological_wait_s': wait,
                               'external_wait_s': pickup - s.release if old == 0 else 0.})
        if old > 0:
            self.occupied[self.loc[sid]] = None
            if self.ops is not None:
                self.ops[sid][-1]['clearance_s'] = pickup
        j = a['next']
        completion = arrival + s.p[j]
        if self.ops is not None:
            self.ops[sid].append({'stage': j, 'type_k': s.seq[j], 'station': a['dest'],
                                  'start_s': arrival, 'completion_s': completion, 'clearance_s': completion})
        self.stage[sid] = j
        self.loc[sid] = a['dest']
        self.ready[sid] = completion
        if s.seq[j] == 5:
            self.done[sid] = completion
            if s.stype == 'HC':
                self.completed_m1 += 1
        else:
            self.occupied[a['dest']] = sid
        self.robot = a['dest']
        self.t = arrival

    def result(self, label):
        kpi = {'policy': label, 'n_samples': len(self.order), 'makespan_s': self.makespan,
               'violations': self.violations, 'affected_samples': len(self.affected)}
        return {'kpi': kpi, 'tasks': self.tasks, 'operations': self.ops, 'completions': dict(self.done),
                'parameters': {'wait_budget_s': self.budget.default}}


class DispatchRule:
    """Ranking of ``cell_model.simulate`` for one rule (deterministic defaults)."""

    name = None
    admission_cap = None
    qc_boost = True

    def __init__(self, beta=1e6, w_viol=600., w_over=120., gamma_qc=1., delta_qc=3):
        self.beta, self.w_viol, self.w_over = beta, w_viol, w_over
        self.gamma_qc, self.delta_qc = gamma_qc, delta_qc

    def prepare(self, state, waiting, pending):
        return None

    def key(self, state, a, pending, ctx):
        s = state.samples[a['sid']]
        slack = state.budget(s.seq[state.stage[a['sid']]]) - (state.t - state.ready[a['sid']])
        priority = PRIORITY[s.stype]
        if not pending and self.qc_boost and s.stype in ('QC1', 'QC2', 'Blank'):
            rho = state.completed_m1 % 10
            if rho >= 10 - self.delta_qc:
                priority -= self.gamma_qc * rho / 10.
        final = (a['dur'], sid_key(a['sid']), int(a['dest'][1:]))
        return self.order_key(state, a, s, slack, priority, pending, ctx) + final

    def order_key(self, state, a, s, slack, priority, pending, ctx):
        return (slack, priority) if pending else (priority, slack)

    def tier(self, state):
        while True:
            waiting = state.waiting()
            actions = state.actions(waiting, self.admission_cap)
            if actions:
                pending = [a for a in actions if state.stage[a['sid']] > 0]
                return waiting, pending, (pending or actions)
            nxt = state.next_event()
            if nxt is None:
                raise RuntimeError('Deadlock: waiting vessels have no empty destination')
            state.t = nxt

    def ranked(self, state, waiting, pending, tier):
        ctx = self.prepare(state, waiting, pending)
        return sorted(tier, key=lambda a: self.key(state, a, pending, ctx))

    def choose(self, state, waiting, pending, tier):
        ctx = self.prepare(state, waiting, pending)
        return min(tier, key=lambda a: self.key(state, a, pending, ctx))

    def complete(self, state, cost, log=None):
        while not state.finished:
            waiting, pending, tier = self.tier(state)
            a = self.choose(state, waiting, pending, tier)
            if log is not None:
                log.append((a['sid'], a['dest']))
            state.apply(a)
        return cost(state)


class URG(DispatchRule):
    name = 'URG'
    qc_boost = False


class SRPT(DispatchRule):
    name = 'SRPT'

    def order_key(self, state, a, s, slack, priority, pending, ctx):
        j = state.stage[a['sid']]
        rp = sum(s.p[j + 1:]) + sum(state.cell.m_min(s.seq[jj - 1], s.seq[jj]) for jj in range(j + 1, len(s.seq)))
        return (rp, slack, priority) if pending else (priority, rp, slack)


class BNA(DispatchRule):
    name = 'BNA'
    admission_cap = 2

    def order_key(self, state, a, s, slack, priority, pending, ctx):
        if pending:
            return (slack, priority)
        c_idle = any(state.occupied[v] is None for v in state.cell.by_k[10])
        return (priority, 0 if 10 in s.seq and c_idle else 1, slack)


class LBLA(DispatchRule):
    name = 'LB-LA'

    def prepare(self, state, waiting, pending):
        R, Rrob, active = defaultdict(float), 0., []
        cell, t = state.cell, state.t
        for sid, s in state.samples.items():
            if sid in state.done:
                continue
            j = state.stage[sid]
            if j > 0 and state.ready[sid] > t:
                r = state.ready[sid] - t
                R[s.seq[j]] += r
                active.append((s.seq[j], r))
            for jj in range(j + 1, len(s.seq)):
                R[s.seq[jj]] += s.p[jj]
                Rrob += cell.m_min(s.seq[jj - 1], s.seq[jj])
        idle = {k for k, vs in cell.by_k.items() if any(state.occupied[v] is None for v in vs)}
        return R, Rrob, active, idle, waiting

    def order_key(self, state, a, s, slack, priority, pending, ctx):
        R, Rrob, active, idle, waiting = ctx
        cell, sid, dur = state.cell, a['sid'], a['dur']
        residual = dict(R)
        for k, r in active:
            residual[k] -= min(r, dur)
        rob_after = max(0., Rrob - cell.m_min(s.seq[state.stage[sid]], s.seq[a['next']]))
        values = [(r + (dur if k in idle and k != s.seq[a['next']] else 0.)) / len(cell.by_k[k])
                  for k, r in residual.items() if r > 0]
        lb = max([rob_after] + values)
        nv, ov = 0, 0.
        for other in waiting:
            if other == sid or state.stage[other] == 0:
                continue
            w = state.t - state.ready[other]
            wb = state.budget(state.samples[other].seq[state.stage[other]])
            nv += int(w <= wb < w + dur)
            ov += max(0., w + dur - wb) / wb
        z = dur + lb + self.w_viol * nv + self.w_over * ov
        return (math.floor(slack / self.beta), z, slack) if pending else (priority, slack, z)


RULES = {r.name: r for r in (URG, SRPT, BNA, LBLA)}


class Cost:
    name = None

    def __call__(self, state):
        raise NotImplementedError


class MakespanCost(Cost):
    name = 'makespan'

    def __call__(self, state):
        return (round(state.makespan, 6),)


class ViolationsFirstCost(Cost):
    name = 'violations-first'

    def __call__(self, state):
        return (state.violations, round(state.makespan, 6))


COSTS = {c.name: c for c in (MakespanCost, ViolationsFirstCost)}


class RolloutPolicy:
    """Fortified one-step rollout with a base dispatching rule."""

    def __init__(self, base, cost, hold=False, candidates=5):
        self.base, self.cost, self.hold, self.candidates = base, cost, hold, candidates

    def label(self):
        return f"RO-{self.base.name}-{'H-' if self.hold else ''}{self.cost.name}"

    def _candidates(self, state, waiting, pending, tier):
        best, seen = [], set()
        for a in self.base.ranked(state, waiting, pending, tier):
            if a['sid'] not in seen:
                seen.add(a['sid'])
                best.append(a)
            if len(best) == self.candidates:
                break
        out = [('move', a) for a in best]
        if self.hold and not pending and state.next_event() is not None:
            out.append(('hold', None))
        return out

    def _evaluate(self, state, option):
        s = state.copy()
        log = []
        if option[0] == 'hold':
            s.hold()
            log.append(HOLD)
        else:
            s.apply(option[1])
            log.append((option[1]['sid'], option[1]['dest']))
        return self.base.complete(s, self.cost, log), log

    def _replay(self, state, step):
        if step == HOLD:
            state.hold()
            return
        waiting = state.waiting()
        for a in state.actions(waiting, self.base.admission_cap):
            if (a['sid'], a['dest']) == step:
                state.apply(a)
                return
        raise RuntimeError(f'stored action {step} not available')

    def run(self, inst, budget=300.):
        start = time.perf_counter()
        state = CellState(inst, budget, record=True)
        probe = state.copy()
        base_log = []
        best_cost = self.base.complete(probe, self.cost, base_log)
        base_cost = best_cost
        best_log = base_log
        decisions = evaluations = holds = deviations = 0
        while not state.finished:
            waiting = state.waiting()
            actions = state.actions(waiting, self.base.admission_cap)
            if not actions:
                state.hold()
                continue
            pending = [a for a in actions if state.stage[a['sid']] > 0]
            tier = pending or actions
            options = self._candidates(state, waiting, pending, tier)
            decisions += 1
            if len(options) > 1:
                for option in options:
                    c, log = self._evaluate(state, option)
                    evaluations += 1
                    if c < best_cost:
                        best_cost, best_log = c, log
            step, best_log = best_log[0], best_log[1:]
            if step != base_choice(self.base, state, waiting, pending, tier):
                deviations += 1
            holds += step == HOLD
            self._replay(state, step)
        result = state.result(self.label())
        metrics = audit(inst, result, wait_budget=budget)
        result['kpi'].update({k: metrics[k] for k in ('max_wait_s', 'mean_wait_s', 'mean_flow_s',
                                                      'robot_util_pct', 'hard_wait_feasible')})
        result['kpi'].update({'base_rule': self.base.name, 'cost': self.cost.name, 'hold_option': self.hold,
                              'candidates': self.candidates, 'decisions': decisions,
                              'rollout_evaluations': evaluations, 'deviations_from_base': deviations,
                              'holds': holds, 'base_cost': list(base_cost), 'final_cost': list(self.cost(state)),
                              'cpu_s': time.perf_counter() - start})
        assert self.cost(state) <= base_cost
        return result


def base_choice(rule, state, waiting, pending, tier):
    a = rule.choose(state, waiting, pending, tier)
    return (a['sid'], a['dest'])


class Validation:
    """Base rules in ``rollout`` reproduce ``cell_model.simulate`` action by action."""

    @staticmethod
    def check(inst, rule):
        ref = simulate(inst, rule.name, trace=False)
        state = CellState(inst, record=True)
        rule.complete(state, MakespanCost())
        got = [(a['sample'], a['destination'], round(a['pickup_s'], 6)) for a in state.tasks]
        exp = [(a['sample'], a['destination'], round(a['pickup_s'], 6)) for a in ref['tasks']]
        assert got == exp, (rule.name, next(i for i, (x, y) in enumerate(zip(got, exp)) if x != y))
        return len(got)


class RolloutStudy:
    """All scenarios: base rule = shortest-makespan rule of the deterministic campaign."""

    VARIANTS = [('makespan', False), ('violations-first', False), ('violations-first', True)]

    def __init__(self, out=ROOT / 'validation' / 'rollout', candidates=5):
        self.out, self.candidates = Path(out), candidates
        self.out.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def best_rules():
        import csv
        best = {}
        for r in csv.DictReader((ROOT / 'data' / 'kpi_campaign.csv').open()):
            if r['policy'] == 'URG-MECT':
                continue
            m = float(r['makespan_s'])
            if r['case'] not in best or m < best[r['case']][1] - 1e-9:
                best[r['case']] = (r['policy'], m, int(r['violations']), int(r['affected_samples']))
        return best

    @staticmethod
    def certificates():
        import csv
        return {r['case']: float(r['certificate_lb_s'])
                for r in csv.DictReader((ROOT / 'data' / 'lower_bounds_v4.csv').open())}

    def job(self, args):
        case, cost, hold = args
        base, bm, bv, ba = self.best_rules()[case]
        pol = RolloutPolicy(RULES[base](), COSTS[cost](), hold=hold, candidates=self.candidates)
        res = pol.run(instance(case))
        name = f"{case}__{pol.label()}"
        (self.out / f'{name}_trace.json').write_text(json.dumps({k: res[k] for k in ('tasks', 'operations', 'completions', 'parameters')}))
        k = res['kpi']
        lb = self.certificates()[case]
        row = {'case': case, 'method': pol.label(), 'base_rule': base, 'cost': cost, 'hold_option': hold,
                'candidates': self.candidates, 'makespan_s': k['makespan_s'], 'makespan_h': k['makespan_s'] / 3600,
                'violations': k['violations'], 'affected_samples': k['affected_samples'],
                'max_wait_s': k['max_wait_s'], 'hard_wait_feasible': k['hard_wait_feasible'],
                'base_makespan_s': bm, 'base_violations': bv, 'base_affected_samples': ba,
                'certificate_lb_s': lb, 'gap_pct': 100 * (k['makespan_s'] / lb - 1),
                'decisions': k['decisions'], 'rollout_evaluations': k['rollout_evaluations'],
                'deviations_from_base': k['deviations_from_base'], 'holds': k['holds'],
                'cpu_s': k['cpu_s'], 'mean_ms_per_decision': 1e3 * k['cpu_s'] / k['decisions']}
        (self.out / f'{name}_row.json').write_text(json.dumps(row, indent=1))
        return row

    def label(self, case, cost, hold):
        base = self.best_rules()[case][0]
        return f"{case}__{RolloutPolicy(RULES[base](), COSTS[cost](), hold=hold).label()}"

    def run(self, cases, workers=2):
        from multiprocessing import Pool
        from run_campaign import write_csv
        jobs = [(c, cost, hold) for c in cases for cost, hold in self.VARIANTS]
        rows = [json.loads((self.out / f'{self.label(*j)}_row.json').read_text()) for j in jobs
                if (self.out / f'{self.label(*j)}_row.json').exists()]
        todo = [j for j in jobs if not (self.out / f'{self.label(*j)}_row.json').exists()]
        with Pool(workers) as pool:
            for row in pool.imap_unordered(self.job, todo):
                print(row['case'], row['method'], round(row['makespan_h'], 3), row['violations'],
                      row['affected_samples'], round(row['cpu_s'], 1), flush=True)
                rows.append(row)
        order = {c: i for i, c in enumerate(cases)}
        rows.sort(key=lambda r: (order[r['case']], [v[0] + str(v[1]) for v in self.VARIANTS].index(r['cost'] + str(r['hold_option']))))
        write_csv(ROOT / 'data' / 'rollout_results.csv', rows)
        return rows


def instance(case):
    cell = Cell(ROOT / 'instances' / f'{case}_config.json')
    return Instance(cell, cell.cfg['samples'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cases', nargs='+')
    ap.add_argument('--base', default='URG', choices=sorted(RULES))
    ap.add_argument('--cost', default='violations-first', choices=sorted(COSTS))
    ap.add_argument('--hold', action='store_true')
    ap.add_argument('--candidates', type=int, default=5)
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--out', default=str(ROOT / 'validation' / 'rollout'))
    ap.add_argument('--study', action='store_true', help='all variants with the best rule of each case as base')
    ap.add_argument('--workers', type=int, default=2)
    args = ap.parse_args()
    if args.study:
        RolloutStudy(args.out, args.candidates).run(args.cases, args.workers)
        return
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for case in args.cases:
        inst = instance(case)
        if args.validate:
            for r in RULES.values():
                print(case, r.name, Validation.check(inst, r()), 'tasks reproduced', flush=True)
            continue
        pol = RolloutPolicy(RULES[args.base](), COSTS[args.cost](), hold=args.hold, candidates=args.candidates)
        res = pol.run(inst)
        name = f"{case}__{pol.label()}"
        (out / f'{name}_trace.json').write_text(json.dumps({k: res[k] for k in ('tasks', 'operations', 'completions', 'parameters')}))
        (out / f'{name}_kpi.json').write_text(json.dumps(res['kpi'], indent=1))
        k = res['kpi']
        print(case, pol.label(), round(k['makespan_s'], 2), k['violations'], k['affected_samples'],
              round(k['cpu_s'], 1), flush=True)


if __name__ == '__main__':
    main()
