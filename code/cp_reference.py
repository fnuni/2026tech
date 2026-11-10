"""Constraint-programming reference schedules with completion-to-pickup time lags.

Two CP-SAT models share one transfer graph (polymorphism through duration
providers):

* ConservativeCpModel -- every robot task reserves the largest admissible empty
  approach and loaded leg; every solution is re-timed with the true movement
  matrix and audited by ``cell_model.audit``.  It yields audited primal
  schedules for the operational and for the hard-wait-constrained problem.
* RelaxedCpModel -- every robot task reserves only the shortest loaded leg and
  the shortest empty leg between distinct positions; deliveries may take any
  admissible loaded duration.  Any feasible schedule of the true model maps to
  a feasible schedule of this model, so its proven CP-SAT bound is a valid
  makespan lower bound for the same problem.

The time-lag insertion heuristic constructs a feasible conservative schedule
(warm start) and ``RollingHorizonCp`` applies the conservative model on
release-ordered windows (rolling-horizon optimisation).  Time unit inside the
models: 0.01 s.
"""
from __future__ import annotations

import bisect
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from ortools.sat.python import cp_model
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from cell_model import Instance, audit

SCALE = 100            # 0.01 s
EXIT_TYPE = 5
ENTRY_TYPES = (1, 2, 3, 4)


def ceil_t(x: float) -> int:
    return int(math.ceil(round(x * SCALE, 6)))


def floor_t(x: float) -> int:
    return int(math.floor(round(x * SCALE, 6)))


class WaitProfile:
    """Uniform or origin-type-specific completion-to-pickup budgets."""

    def __init__(self, budget: float | dict = 300., default: float = 300.):
        if isinstance(budget, dict):
            self.by_type = {int(k): float(v) for k, v in budget.items()}
            self.default = float(default)
        else:
            self.by_type = {}
            self.default = float(budget)

    def __call__(self, origin_type: int) -> float:
        return self.by_type.get(int(origin_type), self.default)

    def label(self) -> str:
        if not self.by_type:
            return f'{self.default:g}'
        return 'profile:' + ','.join(f'{k}={v:g}' for k, v in sorted(self.by_type.items()))


@dataclass
class Transfer:
    """Robot task delivering stage j of a sample (stage j-1 -> stage j)."""
    sid: str
    j: int
    k_from: int
    k_to: int
    p_to: float                 # processing time of destination stage
    l_min: float
    l_max: float
    e_max: float                # largest empty approach to any origin of type k_from
    release: float | None       # only for the admission transfer (j == 1)
    budget: float | None        # wait budget of the pickup (None: external)
    init_max: float = 0.        # aux + longest move from the initial robot position to an origin
    init_min: float = 0.        # aux + shortest move from the initial robot position to an origin


class TransferGraph:
    """Static transfer data of an instance; shared by all CP models."""

    def __init__(self, inst: Instance, wait: WaitProfile | float = 300.):
        self.inst = inst
        self.cell = cell = inst.cell
        self.wait = wait if isinstance(wait, WaitProfile) else WaitProfile(wait)
        self.aux = cell.auxiliary_s
        stations = cell.stations
        p_pos = [x for s in inst.samples for x in s.p if x > 0]
        self.e_min = min(min(cell.empty(u, v) for u in stations for v in stations if u != v),
                         min(p_pos))
        emax_to = {k: max(cell.empty(w, o) for w in stations for o in cell.by_k[k])
                   for k in cell.by_k}
        self.samples = {s.sid: s for s in inst.samples}
        self.transfers: dict[str, list[Transfer]] = {}
        for s in inst.samples:
            chain = []
            for j in range(1, len(s.seq)):
                k0, k1 = s.seq[j - 1], s.seq[j]
                moves = [cell.m(u, v) for u in cell.by_k[k0] for v in cell.by_k[k1]]
                init = [cell.empty(cell.initial_robot, o) for o in cell.by_k[k0]]
                chain.append(Transfer(s.sid, j, k0, k1, s.p[j], min(moves), max(moves),
                                      emax_to[k0], s.release if j == 1 else None,
                                      None if j == 1 else self.wait(k0),
                                      self.aux + max(init), self.aux + min(init)))
            self.transfers[s.sid] = chain

    def all_transfers(self):
        for chain in self.transfers.values():
            yield from chain


# ----------------------------------------------------------------- calendars --
class IntervalCalendar:
    """Unit-capacity calendar of half-open busy intervals [a, b)."""

    def __init__(self):
        self.starts: list[int] = []
        self.ends: list[int] = []

    def is_free(self, a: int, b: int) -> bool:
        i = bisect.bisect_right(self.starts, a)
        if i > 0 and self.ends[i - 1] > a:
            return False
        return not (i < len(self.starts) and self.starts[i] < b)

    def earliest(self, a: int, length: int) -> int:
        """Earliest start >= a of a free window of the given length."""
        i = bisect.bisect_right(self.starts, a)
        if i > 0 and self.ends[i - 1] > a:
            a = self.ends[i - 1]
        while i < len(self.starts) and self.starts[i] < a + length:
            a = max(a, self.ends[i])
            i += 1
        return a

    def add(self, a: int, b: int):
        i = bisect.bisect_left(self.starts, a)
        self.starts.insert(i, a)
        self.ends.insert(i, b)


class CumulativeCalendar:
    """Calendar of a module type with m identical single-vessel positions."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.intervals: list[tuple[int, int]] = []

    def load(self, a: int, b: int) -> int:
        events = []
        for s, e in self.intervals:
            if s < b and e > a:
                events.append((max(s, a), 1))
                events.append((min(e, b), -1))
        events.sort(key=lambda x: (x[0], x[1]))
        cur = peak = 0
        for _, d in events:
            cur += d
            peak = max(peak, cur)
        return peak

    def is_free(self, a: int, b: int) -> bool:
        return self.load(a, b) < self.capacity

    def next_change(self, a: int) -> int | None:
        ends = [e for s, e in self.intervals if e > a]
        return min(ends) if ends else None

    def add(self, a: int, b: int):
        self.intervals.append((a, b))


# ------------------------------------------------------------ CP-SAT models --
@dataclass
class CpResult:
    status: str
    objective_s: float | None
    bound_s: float | None
    wall_s: float
    pickups: dict = field(default_factory=dict)     # (sid, j) -> int pickup
    deliveries: dict = field(default_factory=dict)  # (sid, j) -> int planned delivery


class CpScheduleModel:
    """Common CP-SAT structure; subclasses define robot and delivery semantics."""

    name = 'cp'

    def __init__(self, graph: TransferGraph, *, hard_wait: bool, horizon_s: float,
                 jobs: list[str] | None = None, fixed: dict | None = None,
                 objective: str = 'makespan', fifo: bool = False):
        self.g = graph
        self.fifo = fifo
        self.hard_wait = hard_wait
        self.H = ceil_t(horizon_s)
        self.jobs = jobs if jobs is not None else list(graph.transfers)
        self.fixed = fixed or {}          # committed (sid, j) -> (pickup, delivery)
        self.objective = objective
        self.model = cp_model.CpModel()
        self.P: dict = {}
        self.D: dict = {}
        self.cmax = None
        self._build()

    # duration semantics ------------------------------------------------------
    def robot_window(self, tr: Transfer) -> tuple[int, int]:
        """(lead before pickup, length after pickup) reserved on the robot."""
        raise NotImplementedError

    def delivery(self, tr: Transfer, P):
        raise NotImplementedError

    def initial_lead(self, tr: Transfer) -> int:
        raise NotImplementedError

    def wait_cap(self, tr_from: Transfer, budget: float) -> int:
        raise NotImplementedError

    # model -------------------------------------------------------------------
    def _build(self):
        m, g, H = self.model, self.g, self.H
        robot_iv = []
        occ_by_type = defaultdict(list)
        ends = []
        lead_min = min(self.robot_window(tr)[0] for tr in g.all_transfers())
        # Committed tasks are constants.
        for (sid, j), (pick, dlv) in self.fixed.items():
            tr = g.transfers[sid][j - 1]
            lead, length = self.robot_window(tr)
            robot_iv.append(m.new_fixed_size_interval_var(pick - lead, lead + length,
                                                          f'fr_{sid}_{j}'))
        for sid in {sid for sid, _ in self.fixed}:
            chain = g.transfers[sid]
            for tr in chain[:-1]:
                key, nxt = (sid, tr.j), (sid, tr.j + 1)
                if key in self.fixed and nxt in self.fixed:
                    a, b = self.fixed[key][1], self.fixed[nxt][0]
                    occ_by_type[tr.k_to].append(m.new_fixed_size_interval_var(a, b - a, f'fo_{sid}_{tr.j}'))
                elif key in self.fixed:
                    raise ValueError('partial commitment of a job is not supported')
        for sid in self.jobs:
            chain = g.transfers[sid]
            for tr in chain:
                lo = ceil_t(tr.release) if tr.release is not None else 0
                # Initial robot travel: no pickup before the robot can reach the
                # origin from its initial position (Proposition on conservative
                # schedules); in the relaxed model the shortest such move is used.
                lo = max(lo, self.initial_lead(tr))
                P = m.new_int_var(lo, H, f'P_{sid}_{tr.j}')
                self.P[sid, tr.j] = P
                lead, length = self.robot_window(tr)
                robot_iv.append(m.new_fixed_size_interval_var(P - lead, lead + length, f'r_{sid}_{tr.j}'))
                self.D[sid, tr.j] = self.delivery(tr, P)
            for tr, nxt in zip(chain, chain[1:]):
                # occupancy of stage tr.j: from delivery to next pickup
                p = ceil_t(tr.p_to) if isinstance(self, ConservativeCpModel) else floor_t(tr.p_to)
                cap = self.wait_cap(tr, nxt.budget) if self.hard_wait else H
                size = m.new_int_var(p, p + cap, f'o_{sid}_{tr.j}')
                occ_by_type[tr.k_to].append(
                    m.new_interval_var(self.D[sid, tr.j], size, self.P[sid, nxt.j], f'oi_{sid}_{tr.j}'))
            ends.append(self.D[sid, chain[-1].j])
        m.add_no_overlap(robot_iv)
        if self.fifo:
            self._add_fifo()
        for k, ivs in occ_by_type.items():
            cap = len(g.cell.by_k[k])
            if cap == 1:
                m.add_no_overlap(ivs)
            else:
                m.add_cumulative(ivs, [1] * len(ivs), cap)
        self.cmax = m.new_int_var(0, H + ceil_t(100), 'cmax')
        for e in ends:
            m.add(self.cmax >= e)
        if self.objective == 'makespan':
            m.minimize(self.cmax)
        else:  # makespan with flow-time tie-breaking (rolling horizon windows)
            weight = max(1, len(ends))
            m.minimize(weight * self.cmax + sum(ends))

    def _add_fifo(self):
        """Optional search restriction: first-in-first-out by release at every
        entry interface and every single-unit module (not valid for optimality)."""
        g, m = self.g, self.model
        order = sorted(self.jobs, key=lambda s: (g.samples[s].release, int(s[1:])))
        last_entry, last_unit = {}, {}
        for sid in order:
            chain = g.transfers[sid]
            k0 = chain[0].k_from
            if k0 in last_entry:
                m.add(self.P[sid, 1] >= self.P[last_entry[k0]])
            last_entry[k0] = (sid, 1)
            for tr in chain:
                if tr.k_to in ENTRY_TYPES or tr.k_to == EXIT_TYPE or len(g.cell.by_k[tr.k_to]) > 1:
                    continue
                if tr.k_to in last_unit:
                    m.add(self.P[sid, tr.j] >= self.P[last_unit[tr.k_to]])
                last_unit[tr.k_to] = (sid, tr.j)

    def add_hint(self, pickups: dict):
        for key, var in self.P.items():
            if key in pickups:
                self.model.add_hint(var, int(pickups[key]))

    def solve(self, time_limit: float, workers: int = 2, seed: int = 1, log=False,
              stop_at: float | None = None) -> CpResult:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(time_limit)
        solver.parameters.num_workers = workers
        solver.parameters.random_seed = seed
        solver.parameters.log_search_progress = log
        if stop_at is not None:
            solver.parameters.absolute_gap_limit = 0.
        t0 = time.time()
        status = solver.solve(self.model)
        wall = time.time() - t0
        name = solver.status_name(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            pick = {k: solver.value(v) for k, v in self.P.items()}
            dlv = {k: solver.value(v) for k, v in self.D.items()}
            obj = solver.value(self.cmax) / SCALE
            bound = solver.best_objective_bound
            if self.objective != 'makespan':
                bound = None
            else:
                bound = bound / SCALE
            return CpResult(name, obj, bound, wall, pick, dlv)
        bound = solver.best_objective_bound / SCALE if self.objective == 'makespan' else None
        return CpResult(name, None, bound if status != cp_model.INFEASIBLE else None, wall)


class ConservativeCpModel(CpScheduleModel):
    name = 'conservative'

    def robot_window(self, tr):
        return ceil_t(self.g.aux + tr.e_max), ceil_t(tr.l_max)

    def delivery(self, tr, P):
        return P + ceil_t(tr.l_max)

    def initial_lead(self, tr):
        return ceil_t(tr.init_max)

    def wait_cap(self, tr, budget):
        # A shorter true loaded leg completes processing earlier; the reserve
        # keeps the re-timed wait within the budget.
        return floor_t(budget - (tr.l_max - tr.l_min))


class RelaxedCpModel(CpScheduleModel):
    name = 'relaxed'

    def robot_window(self, tr):
        return floor_t(self.g.aux + self.g.e_min), floor_t(tr.l_min)

    def delivery(self, tr, P):
        d = self.model.new_int_var(0, self.H + ceil_t(100), f'D_{tr.sid}_{tr.j}')
        self.model.add(d >= P + floor_t(tr.l_min))
        self.model.add(d <= P + ceil_t(tr.l_max))
        return d

    def wait_cap(self, tr, budget):
        return ceil_t(budget)

    def initial_lead(self, tr):
        return floor_t(tr.init_min)


# ------------------------------------------------- constructive time lags --
class TimeLagInsertion:
    """Serial schedule generation with completion-to-pickup time lags.

    Jobs are inserted one at a time (release order by default) at the earliest
    admission instant from which their whole route can be timed with every
    internal wait inside its budget, given the already inserted jobs.
    Conservative robot windows make every insertion executable.
    """

    def __init__(self, graph: TransferGraph, hard_wait=True):
        self.g = graph
        self.hard_wait = hard_wait
        self.model = ConservativeCpModel.__new__(ConservativeCpModel)
        self.model.g = graph
        self.robot = IntervalCalendar()
        self.res = {k: CumulativeCalendar(len(vs)) for k, vs in graph.cell.by_k.items()
                    if k not in ENTRY_TYPES and k != EXIT_TYPE}
        self.pickups: dict = {}

    def _robot_free_from(self, tr, t):
        lead, length = self.model.robot_window(tr)
        return self.robot.earliest(t - lead, lead + length) + lead

    def _try(self, chain, P1):
        plan = []
        P = self._robot_free_from(chain[0], P1)
        if P != P1:
            return None, P
        for idx, tr in enumerate(chain):
            D = P + ceil_t(tr.l_max)
            plan.append((tr, P, D))
            if idx == len(chain) - 1:
                return plan, None
            nxt = chain[idx + 1]
            C = D + ceil_t(tr.p_to)
            latest = C + (self.model.wait_cap(tr, nxt.budget) if self.hard_wait else 10**12)
            cal = self.res[tr.k_to]
            if not cal.is_free(D, C):
                return None, P1 + SCALE
            t = C
            while True:
                t = self._robot_free_from(nxt, t)
                if t > latest:
                    return None, P1 + max(SCALE, t - latest)
                ok_here = cal.is_free(D, t)
                if not ok_here:
                    return None, P1 + SCALE
                if nxt.k_to in self.res:
                    D2 = t + ceil_t(nxt.l_max)
                    C2 = D2 + ceil_t(nxt.p_to)
                    if not self.res[nxt.k_to].is_free(D2, C2):
                        ch = self.res[nxt.k_to].next_change(D2)
                        t = max(t + SCALE, (ch - ceil_t(nxt.l_max)) if ch else t + SCALE)
                        continue
                break
            P = t

    def insert(self, sid):
        chain = self.g.transfers[sid]
        P1 = max(ceil_t(chain[0].release), ceil_t(chain[0].init_max))
        while True:
            plan, nxt = self._try(chain, P1)
            if plan is not None:
                break
            P1 = max(nxt, P1 + 1)
        for idx, (tr, P, D) in enumerate(plan):
            lead, length = self.model.robot_window(tr)
            self.robot.add(P - lead, P + length)
            self.pickups[sid, tr.j] = P
            if idx + 1 < len(plan):
                self.res[tr.k_to].add(D, plan[idx + 1][1])

    def run(self, order=None):
        order = order or sorted(self.g.transfers, key=lambda s: (self.g.samples[s].release,
                                                                  int(s[1:])))
        for sid in order:
            self.insert(sid)
        return self.pickups


# ------------------------------------------------ exact re-timing + audit --
class ScheduleReconstructor:
    """Assign physical positions, re-time with true durations and audit."""

    def __init__(self, graph: TransferGraph):
        self.g = graph

    def assign_positions(self, pickups: dict) -> dict:
        g, cell = self.g, self.g.cell
        intervals = []
        for sid, chain in g.transfers.items():
            for tr, nxt in zip(chain, chain[1:]):
                a = pickups[sid, tr.j] + ceil_t(tr.l_max)
                b = pickups[sid, nxt.j]
                intervals.append((a, b, sid, tr.j, tr.k_to))
        intervals.sort()
        free_at = {v: -10**15 for v in cell.stations}
        station = {}
        for sid, chain in g.transfers.items():
            station[sid, 0] = cell.by_k[chain[0].k_from][0]
            station[sid, len(chain)] = cell.by_k[EXIT_TYPE][0]
        for a, b, sid, j, k in intervals:
            origin = station[sid, j - 1]
            options = [v for v in cell.by_k[k] if free_at[v] <= a]
            if not options:
                raise RuntimeError(f'position assignment failed for {sid} stage {j}')
            v = min(options, key=lambda v: (cell.m(origin, v), int(v[1:])))
            station[sid, j] = v
            free_at[v] = b
        return station

    def retime(self, pickups: dict, station: dict, tighten=True) -> dict:
        """Earliest-makespan timing for fixed robot order and position sequences."""
        g, cell = self.g, self.g.cell
        keys = sorted(pickups, key=lambda k: (pickups[k], k[0], k[1]))
        if not tighten:
            return {k: pickups[k] / SCALE for k in keys}
        idx = {k: i for i, k in enumerate(keys)}
        n = len(keys) + 1
        rows, cols, vals, rhs = [], [], [], []
        r = 0

        def ge(a, b, c):          # x_a - x_b >= c  ->  -x_a + x_b <= -c
            nonlocal r
            rows.extend([r, r]); cols.extend([a, b]); vals.extend([-1., 1.]); rhs.append(-c); r += 1

        loaded = {k: cell.m(station[k[0], k[1] - 1], station[k]) for k in keys}
        # robot order
        prev = None
        for k in keys:
            if prev is not None:
                e = cell.empty(station[prev], station[k[0], k[1] - 1])
                ge(idx[k], idx[prev], loaded[prev] + g.aux + e)
            else:
                e = cell.empty(cell.initial_robot, station[k[0], k[1] - 1])
                rows.append(r); cols.append(idx[k]); vals.append(-1.); rhs.append(-(g.aux + e)); r += 1
            prev = k
        lb = [0.] * n
        for sid, chain in g.transfers.items():
            lb[idx[sid, 1]] = max(lb[idx[sid, 1]], chain[0].release)
            for tr, nxt in zip(chain, chain[1:]):
                a, b = idx[sid, tr.j], idx[sid, nxt.j]
                ge(b, a, loaded[sid, tr.j] + tr.p_to)
                if nxt.budget is not None and self.hard_wait:
                    # x_b - x_a - L - p <= W
                    rows.extend([r, r]); cols.extend([b, a]); vals.extend([1., -1.])
                    rhs.append(nxt.budget + loaded[sid, tr.j] + tr.p_to - 1e-6); r += 1
            last = chain[-1]
            rows.extend([r, r]); cols.extend([idx[sid, last.j], n - 1]); vals.extend([1., -1.])
            rhs.append(-loaded[sid, last.j]); r += 1
        # position sequences: delivery of next occupant after pickup of previous
        occ = defaultdict(list)
        for sid, chain in g.transfers.items():
            for tr, nxt in zip(chain, chain[1:]):
                occ[station[sid, tr.j]].append((pickups[sid, tr.j], (sid, tr.j), (sid, nxt.j)))
        for v, items in occ.items():
            items.sort()
            for (_, _, out_a), (_, in_b, _) in zip(items, items[1:]):
                ge(idx[in_b], idx[out_a], -loaded[in_b])
        A = coo_matrix((vals, (rows, cols)), shape=(r, n)).tocsr()
        c = np.full(n, 1e-6); c[-1] = 1.
        res = linprog(c, A_ub=A, b_ub=np.array(rhs), bounds=[(l, None) for l in lb],
                      method='highs')
        if res.status != 0:
            raise RuntimeError('re-timing LP failed: ' + res.message)
        x = res.x
        return {k: float(x[idx[k]]) for k in keys}

    def trace(self, pickups: dict, *, hard_wait: bool, tighten=True, label='CP') -> dict:
        self.hard_wait = hard_wait
        g, cell = self.g, self.g.cell
        station = self.assign_positions(pickups)
        times = self.retime(pickups, station, tighten)
        if tighten:
            # Snap to 1e-6 s to avoid accumulated floating noise.
            times = {k: round(v, 6) for k, v in times.items()}
        keys = sorted(times, key=lambda k: (times[k], k[0], k[1]))
        tasks, ops, done = [], {sid: [] for sid in g.transfers}, {}
        robot = cell.initial_robot
        for k in keys:
            sid, j = k
            s = g.samples[sid]
            origin, dest = station[sid, j - 1], station[k]
            empty, loaded = cell.empty(robot, origin), cell.m(origin, dest)
            pickup = times[k]
            start = pickup - g.aux - empty
            delivery = pickup + loaded
            prev_c = ops[sid][-1]['completion_s'] if j > 1 else None
            task = {'sample': sid, 'from_stage': j - 1, 'to_stage': j, 'origin': origin,
                    'destination': dest, 'start_s': start, 'pickup_s': pickup,
                    'delivery_s': delivery, 'empty_s': empty, 'loaded_s': loaded,
                    'auxiliary_s': g.aux,
                    'technological_wait_s': pickup - prev_c if j > 1 else 0.,
                    'external_wait_s': pickup - s.release if j == 1 else 0.}
            if j > 1:
                ops[sid][-1]['clearance_s'] = pickup
            completion = delivery + s.p[j]
            ops[sid].append({'stage': j, 'type_k': s.seq[j], 'station': dest,
                             'start_s': delivery, 'completion_s': completion,
                             'clearance_s': completion})
            if s.seq[j] == EXIT_TYPE:
                done[sid] = completion
            tasks.append(task)
            robot = dest
        result = {'kpi': {}, 'tasks': tasks, 'operations': ops, 'completions': done,
                  'decisions': [], 'parameters': {'wait_budget_s': g.wait.default,
                                                  'policy': label}}
        metrics = audit_profile(g.inst, result, g.wait)
        result['kpi'] = {'policy': label, **metrics}
        return result


def audit_profile(inst, result, wait: WaitProfile):
    """Physical audit with uniform or stage-specific limits (per-transition budgets)."""
    from cell_model import Budget
    budget = Budget(wait.by_type, wait.default) if wait.by_type else wait.default
    return audit(inst, result, wait_budget=budget)


# --------------------------------------------------------- rolling horizon --
class RollingHorizonCp:
    """Release-ordered rolling-horizon optimisation with the conservative model.

    At each step the next ``window`` jobs (by release) are scheduled by CP-SAT
    around the committed schedule; the first ``commit`` of them are frozen.
    The planner uses the release list of the look-ahead window, a planning
    assumption stronger than that of the dispatching rules.
    """

    def __init__(self, graph: TransferGraph, *, hard_wait: bool, window=30, commit=15,
                 step_limit=10., workers=2, horizon_s=3 * 86400., fifo=False):
        self.g = graph
        self.fifo = fifo
        self.hard_wait = hard_wait
        self.window, self.commit = window, commit
        self.step_limit, self.workers, self.horizon_s = step_limit, workers, horizon_s

    def run(self, seed=1):
        g = self.g
        order = sorted(g.transfers, key=lambda s: (g.samples[s].release, int(s[1:])))
        fixed: dict = {}
        steps, wall = 0, 0.
        pos = 0
        while pos < len(order):
            jobs = order[pos:pos + self.window]
            # warm start: insertion of window jobs around committed schedule
            ins = TimeLagInsertion(g, self.hard_wait)
            for (sid, j), (pick, dlv) in fixed.items():
                tr = g.transfers[sid][j - 1]
                lead, length = ins.model.robot_window(tr)
                ins.robot.add(pick - lead, pick + length)
            for sid in {s for s, _ in fixed}:
                chain = g.transfers[sid]
                for tr, nxt in zip(chain, chain[1:]):
                    ins.res[tr.k_to].add(fixed[sid, tr.j][1], fixed[sid, nxt.j][0])
            for sid in jobs:
                ins.insert(sid)
            relevant = self._relevant(fixed, jobs)
            model = ConservativeCpModel(g, hard_wait=self.hard_wait, horizon_s=self.horizon_s,
                                        jobs=jobs, fixed=relevant, objective='flow', fifo=self.fifo)
            model.add_hint(ins.pickups)
            res = model.solve(self.step_limit, self.workers, seed)
            wall += res.wall_s
            steps += 1
            if res.pickups:
                pick = res.pickups
            else:  # fall back to the feasible insertion
                pick = {k: v for k, v in ins.pickups.items() if k[0] in jobs}
            last = pos + self.window >= len(order)
            frozen = jobs if last else jobs[:self.commit]
            for sid in frozen:
                for tr in g.transfers[sid]:
                    P = pick[sid, tr.j]
                    fixed[sid, tr.j] = (P, P + ceil_t(tr.l_max))
            pos += len(frozen)
        pickups = {k: v[0] for k, v in fixed.items()}
        return pickups, {'steps': steps, 'cp_wall_s': wall}

    def _relevant(self, fixed, jobs):
        """Committed jobs whose intervals can still interact with the window."""
        if not fixed:
            return {}
        g = self.g
        earliest = min(ceil_t(g.samples[s].release) for s in jobs)
        keep = set()
        last_pick = defaultdict(int)
        for (sid, j), (pick, _) in fixed.items():
            last_pick[sid] = max(last_pick[sid], pick)
        margin = ceil_t(60.)
        for sid, t in last_pick.items():
            if t + margin >= earliest:
                keep.add(sid)
        out = {k: v for k, v in fixed.items() if k[0] in keep}
        # Robot tasks of dropped jobs that end after `earliest` must stay.
        return out
