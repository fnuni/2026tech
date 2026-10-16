"""Auditable deterministic cell model and three dispatching policies.

Time is in seconds. Inputs/outputs use zero-based route indices internally.
Entry ports are access points to external queues; only processing positions
hold vessels. Technological waiting is completion-to-pickup, excluding entry
queues and loaded travel. No completion-count QC gate is assumed.
"""
from __future__ import annotations
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache

PRIORITY = {'Blank': 0, 'QC2': 1, 'QC1': 2, 'HC': 3, 'SVOC': 3, 'SVOC_E': 3}

class Cell:
    def __init__(self, config_path, multiplicities=None):
        self.cfg = json.loads(Path(config_path).read_text())
        self.transport = self.cfg['transport_s']
        self.routes = self.cfg['routes']
        self.by_k = defaultdict(list)
        for station, data in self.cfg['stations'].items():
            self.by_k[int(data['type_k'])].append(station)
        for k in self.by_k:
            self.by_k[k].sort(key=lambda v: int(v[1:]))
            if multiplicities:
                self.by_k[k] = self.by_k[k][:multiplicities.get(k, len(self.by_k[k]))]
        self.by_k = dict(self.by_k)
        self.stations = [v for vs in self.by_k.values() for v in vs]
        self.type_of = {v: k for k, vs in self.by_k.items() for v in vs}
        self.initial_robot = self.by_k[1][0]

    def m(self, u, v):
        return float(self.transport[u][v])

    @lru_cache(maxsize=None)
    def m_min(self, k, kp):
        return min(self.m(u, v) for u in self.by_k[k] for v in self.by_k[kp])

    def route(self, sample_type, pipc=600.0):
        r = self.routes[sample_type]
        seq = list(r['type_sequence'])
        p = [float(pipc) if k == 10 else float(x)
             for k, x in zip(seq, r['p_s_sequence'])]
        return seq, p

@dataclass
class Sample:
    sid: str
    stype: str
    release: float
    seq: list[int] = field(default_factory=list)
    p: list[float] = field(default_factory=list)

class Instance:
    def __init__(self, cell, spec, pipc=600.0):
        self.cell = cell
        self.samples = []
        for n, item in enumerate(spec, 1):
            if isinstance(item, dict):
                sid, stype, release = item['id'], item['type'], item['release_s']
            else:
                stype, release = item
                sid = f's{n}'
            seq, p = cell.route(stype, pipc)
            self.samples.append(Sample(sid, stype, float(release), seq, p))

    @property
    def n(self):
        return len(self.samples)

def sid_key(sid):
    return int(sid[1:])

def simulate(inst, policy='URG', *, beta=1e6, w_viol=600., w_over=120.,
             gamma_qc=1., delta_qc=3, trace=True):
    """Execute the shared two-tier policy and return a complete event trace.

    URG-MECT uses the same pending key as URG. MECT reduces exactly to
    shortest loaded travel among empty identical positions. Its only extra
    admission term is the explicitly defined QC anticipation boost.
    """
    cell = inst.cell
    samples = {s.sid: s for s in inst.samples}
    stage = {s.sid: 0 for s in inst.samples}
    loc = {s.sid: cell.by_k[s.seq[0]][0] for s in inst.samples}
    ready = {s.sid: s.release for s in inst.samples}
    occupied = {v: None for v in cell.stations}
    ops = {s.sid: [] for s in inst.samples}
    done = {}
    t = 0.
    robot = cell.initial_robot
    tasks, decisions, timings = [], [], []
    completed_m1 = 0
    while len(done) < inst.n:
        waiting = [s.sid for s in inst.samples if s.sid not in done
                   and ready[s.sid] <= t + 1e-8]
        actions = []
        for sid in waiting:
            s = samples[sid]
            j = stage[sid] + 1
            for v in cell.by_k[s.seq[j]]:
                if s.seq[j] != 5 and occupied[v] is not None:
                    continue
                empty = cell.m(robot, loc[sid])
                loaded = cell.m(loc[sid], v)
                actions.append({'sid': sid, 'dest': v, 'next': j,
                                'empty': empty, 'loaded': loaded,
                                'dur': empty + loaded})
        if not actions:
            future = [ready[sid] for sid in samples if sid not in done
                      and ready[sid] > t + 1e-8]
            if not future:
                raise RuntimeError('Deadlock: waiting vessels have no empty destination')
            t = min(future)
            continue
        pending = [a for a in actions if stage[a['sid']] > 0]
        tier = pending or actions
        start = time.perf_counter()
        # Compute residual processing work once. Future unreleased demand is
        # included; release times are deliberately relaxed in the state bound.
        R = defaultdict(float)
        Rrob = 0.
        active = []
        for sid, s in samples.items():
            if sid in done:
                continue
            j = stage[sid]
            if j > 0 and ready[sid] > t:
                residual = ready[sid] - t
                R[s.seq[j]] += residual
                active.append((s.seq[j], residual))
            for jj in range(j + 1, len(s.seq)):
                R[s.seq[jj]] += s.p[jj]
                Rrob += cell.m_min(s.seq[jj-1], s.seq[jj])
        idle_types = {k for k, vs in cell.by_k.items()
                      if any(occupied[v] is None for v in vs)}

        def rank(a):
            sid = a['sid']; s = samples[sid]
            wait = t - ready[sid]
            slack = 300. - wait
            priority = PRIORITY[s.stype]
            if not pending and policy != 'URG' and s.stype in ('QC1','QC2','Blank'):
                rho = completed_m1 % 10
                if rho >= 10 - delta_qc:
                    priority -= gamma_qc * rho / 10.
            final = (a['dur'], sid_key(sid), int(a['dest'][1:]))
            if policy != 'LB-ROLL':
                return ((slack, priority) if pending else (priority, slack)) + final
            dur = a['dur']
            residual = dict(R)
            for k, r in active:
                residual[k] -= min(r, dur)
            # Selected transfer is completed in the post-action state.
            rob_after = max(0., Rrob - cell.m_min(s.seq[stage[sid]], s.seq[a['next']]))
            values = [(r + (dur if k in idle_types and k != s.seq[a['next']] else 0.))
                      / len(cell.by_k[k]) for k, r in residual.items() if r > 0]
            score_lb = max([rob_after] + values)
            nv = 0; ov = 0.
            for other in waiting:
                if other == sid or stage[other] == 0:
                    continue
                w = t - ready[other]
                nv += int(w <= 300. < w + dur)
                ov += max(0., w + dur - 300.) / 300.
            z = dur + score_lb + w_viol * nv + w_over * ov
            return ((math.floor(slack / beta), z, slack) if pending
                    else (priority, slack, z)) + final

        a = min(tier, key=rank)
        timings.append(1e3 * (time.perf_counter() - start))
        sid = a['sid']; s = samples[sid]; old = stage[sid]
        pickup = t + a['empty']; arrival = pickup + a['loaded']
        wait = pickup - ready[sid] if old > 0 else 0.
        external_wait = pickup - s.release if old == 0 else 0.
        task = {'sample': sid, 'from_stage': old, 'to_stage': a['next'],
                'origin': loc[sid], 'destination': a['dest'], 'start_s': t,
                'pickup_s': pickup, 'delivery_s': arrival,
                'empty_s': a['empty'], 'loaded_s': a['loaded'],
                'technological_wait_s': wait, 'external_wait_s': external_wait}
        if old > 0:
            assert occupied[loc[sid]] == sid
            occupied[loc[sid]] = None
            ops[sid][-1]['clearance_s'] = pickup
        next_j = a['next']
        completion = arrival + s.p[next_j]
        ops[sid].append({'stage': next_j, 'type_k': s.seq[next_j],
                         'station': a['dest'], 'start_s': arrival,
                         'completion_s': completion, 'clearance_s': completion})
        tasks.append(task)
        if trace:
            decisions.append({'epoch': len(tasks), 'time_s': t,
                              'policy': policy, 'tier': 'pending' if pending else 'entry',
                              'candidate_count': len(tier), 'selected': task,
                              'rank': list(rank(a)),
                              'waiting': [{'sample': v, 'stage': stage[v],
                                           'wait_s': t-ready[v]} for v in waiting]})
        stage[sid] = next_j; loc[sid] = a['dest']; ready[sid] = completion
        if s.seq[next_j] == 5:
            done[sid] = completion
            if s.stype == 'HC':
                completed_m1 += 1
        else:
            assert occupied[a['dest']] is None
            occupied[a['dest']] = sid
        robot = a['dest']; t = arrival
    makespan = max(done.values())
    waits = [a['technological_wait_s'] for a in tasks if a['from_stage'] > 0]
    bad = {a['sample'] for a in tasks if a['technological_wait_s'] > 300. + 1e-8}
    loaded = sum(a['loaded_s'] for a in tasks); empty = sum(a['empty_s'] for a in tasks)
    kpi = {'policy': policy, 'n_samples': inst.n, 'makespan_s': makespan,
           'violations': sum(w > 300. + 1e-8 for w in waits),
           'affected_samples': len(bad), 'budget_compliant_samples': inst.n-len(bad),
           'max_wait_s': max(waits, default=0.), 'mean_wait_s': sum(waits)/len(waits),
           'mean_flow_s': sum(done[s.sid]-s.release for s in inst.samples)/inst.n,
           'robot_util_pct': 100*(loaded+empty)/makespan,
           'robot_loaded_s': loaded, 'robot_empty_s': empty, 'robot_tasks': len(tasks),
           'decision_count': len(timings), 'mean_decision_ms': sum(timings)/len(timings),
           'max_decision_ms': max(timings), 'hard_wait_feasible': not bad}
    result = {'kpi': kpi, 'tasks': tasks, 'operations': ops, 'decisions': decisions,
              'completions': done}
    audit(inst, result)
    return result

def audit(inst, result):
    """Independent checks of precedence, robot/position capacity and releases."""
    eps = 1e-6
    tasks = result['tasks']; by_sample = defaultdict(list)
    robot_at = inst.cell.initial_robot; robot_free = 0.
    for a in tasks:
        assert a['start_s'] >= robot_free-eps
        assert abs(a['pickup_s']-a['start_s']-inst.cell.m(robot_at,a['origin'])) < eps
        assert abs(a['delivery_s']-a['pickup_s']-inst.cell.m(a['origin'],a['destination'])) < eps
        robot_at=a['destination'];robot_free=a['delivery_s'];by_sample[a['sample']].append(a)
    occupancy = defaultdict(list)
    for s in inst.samples:
        ts=by_sample[s.sid]; ops=result['operations'][s.sid]
        assert len(ts)==len(s.seq)-1
        assert ts[0]['pickup_s'] >= s.release-eps
        for j,(a,op) in enumerate(zip(ts,ops),1):
            assert op['type_k']==s.seq[j]
            assert abs(op['completion_s']-op['start_s']-s.p[j])<eps
            if j>1: assert a['pickup_s'] >= ops[j-2]['completion_s']-eps
            if s.seq[j]!=5:
                occupancy[op['station']].append((op['start_s'],op['clearance_s'],s.sid))
    for intervals in occupancy.values():
        intervals.sort()
        for a,b in zip(intervals,intervals[1:]): assert b[0]>=a[1]-eps
    assert len(result['completions'])==inst.n
