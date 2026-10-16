"""Exact validation of the certifying bound on reduced instances.

Solves the static MILP of the manuscript to proven optimality with SCIP on
small, reduced-cell instances, and compares the optimum against
  (a) the capacity/release-path certificate,
  (b) the head/tail-augmented certificate,
  (c) the makespan realised by a reference implementation of the URG rule.

Object-oriented; every class corresponds to one modelling entity.
"""
from __future__ import annotations

import itertools
import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pulp


from cell_model import Cell, Sample, Instance, simulate


# ------------------------------------------------------------------ bounds ---
class Bound:
    """Base class for makespan lower bounds."""

    name = "bound"

    def value(self, inst: Instance) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    # shared helpers ---------------------------------------------------------
    @staticmethod
    def _prefix(cell: Cell, s: Sample) -> list[float]:
        pre = [0.0] * len(s.seq)
        for j in range(1, len(s.seq)):
            pre[j] = pre[j - 1] + s.p[j - 1] + cell.m_min(s.seq[j - 1], s.seq[j])
        return pre

    @classmethod
    def _total(cls, cell: Cell, s: Sample) -> float:
        return cls._prefix(cell, s)[-1] + s.p[-1]


class ReleasePathBound(Bound):
    name = "release_path"

    def value(self, inst: Instance) -> float:
        return max(s.release + self._total(inst.cell, s) for s in inst.samples)


class CapacityBound(Bound):
    name = "capacity"

    def value(self, inst: Instance) -> float:
        load: dict[int, float] = {}
        for s in inst.samples:
            for j, k in enumerate(s.seq):
                if s.p[j] > 0:
                    load[k] = load.get(k, 0.0) + s.p[j]
        return max(v / len(inst.cell.by_k[k]) for k, v in load.items())


class HeadTailBound(Bound):
    """Capacity bound augmented with minimum heads and tails (Carlier-style)."""

    name = "head_tail"

    def detail(self, inst: Instance) -> dict[int, tuple[float, float, float, float]]:
        cell = inst.cell
        load: dict[int, float] = {}
        head: dict[int, float] = {}
        tail: dict[int, float] = {}
        for s in inst.samples:
            pre = self._prefix(cell, s)
            tot = pre[-1] + s.p[-1]
            for j, k in enumerate(s.seq):
                if s.p[j] <= 0:
                    continue
                load[k] = load.get(k, 0.0) + s.p[j]
                head[k] = min(head.get(k, 1e18), s.release + pre[j])
                tail[k] = min(tail.get(k, 1e18), tot - pre[j] - s.p[j])
        out = {}
        for k, w in load.items():
            mk = len(cell.by_k[k])
            out[k] = (head[k], w / mk, tail[k], head[k] + w / mk + tail[k])
        return out

    def value(self, inst: Instance) -> float:
        return max(v[3] for v in self.detail(inst).values())


class CompositeBound(Bound):
    name = "certificate"

    def __init__(self, parts: list[Bound]):
        self.parts = parts

    def value(self, inst: Instance) -> float:
        return max(b.value(inst) for b in self.parts)

    def binding(self, inst: Instance) -> str:
        return max(self.parts, key=lambda b: b.value(inst)).name


# ------------------------------------------------------------------- MILP ----
class ExactModel:
    """Static operational model; technological waiting limits are relaxed."""

    def __init__(self, inst: Instance, horizon: float | None = None):
        self.inst = inst
        n_tasks = sum(len(s.seq) - 1 for s in inst.samples)
        max_move = max(
            inst.cell.m(u, v) for u in inst.cell.stations for v in inst.cell.stations)
        self.H = horizon or (
            max(s.release for s in inst.samples)
            + sum(sum(s.p) for s in inst.samples)
            + 2 * max_move * n_tasks
        )

    def solve(self, time_limit: int = 900, threads: int = 4,
              solver_name: str = "cbc"):
        inst, cell, H = self.inst, self.inst.cell, self.H
        prob = pulp.LpProblem("cell", pulp.LpMinimize)
        ops = [(s.sid, j) for s in inst.samples for j in range(len(s.seq))]
        smap = {s.sid: s for s in inst.samples}

        cand = {(sid, j): cell.by_k[smap[sid].seq[j]] for sid, j in ops}
        y = {
            (sid, j, i): pulp.LpVariable(f"y_{sid}_{j}_{i}", cat="Binary")
            for sid, j in ops
            for i in cand[(sid, j)]
        }
        S = {o: pulp.LpVariable(f"S_{o[0]}_{o[1]}", 0) for o in ops}
        C = {o: pulp.LpVariable(f"C_{o[0]}_{o[1]}", 0) for o in ops}
        tasks = [(sid, j) for sid, j in ops if j >= 1]
        d = {t: pulp.LpVariable(f"d_{t[0]}_{t[1]}", 0) for t in tasks}
        Cmax = pulp.LpVariable("Cmax", 0)
        prob += Cmax

        # (C1) assignment, (C2) processing
        for sid, j in ops:
            prob += pulp.lpSum(y[(sid, j, i)] for i in cand[(sid, j)]) == 1
            prob += C[(sid, j)] == S[(sid, j)] + smap[sid].p[j]
        for s in inst.samples:
            prob += S[(s.sid, 0)] >= s.release
            prob += Cmax >= C[(s.sid, len(s.seq) - 1)]

        # z[(t,i,i')] = y[prev,i] AND y[cur,i']  -> loaded leg duration
        z = {}
        for sid, j in tasks:
            for i in cand[(sid, j - 1)]:
                for ip in cand[(sid, j)]:
                    v = pulp.LpVariable(f"z_{sid}_{j}_{i}_{ip}", cat="Binary")
                    z[(sid, j, i, ip)] = v
                    prob += v <= y[(sid, j - 1, i)]
                    prob += v <= y[(sid, j, ip)]
                    prob += v >= y[(sid, j - 1, i)] + y[(sid, j, ip)] - 1
            prob += pulp.lpSum(z[(sid, j, i, ip)] for i in cand[(sid, j - 1)]
                               for ip in cand[(sid, j)]) == 1

        loaded = {
            t: pulp.lpSum(cell.m(i, ip) * z[(t[0], t[1], i, ip)]
                          for i in cand[(t[0], t[1] - 1)] for ip in cand[t])
            for t in tasks
        }

        # (C3) transfer coupling
        for t in tasks:
            sid, j = t
            prob += d[t] >= C[(sid, j - 1)]
            prob += d[t] >= min(cell.m(cell.initial_robot, u) for u in cand[(sid, j - 1)])
            prob += S[t] == d[t] + loaded[t]

        # (C5) single-position station capacity (delivery -> pick-up)
        def release_time(sid, j):
            """Instant at which sample sid frees its stage-j station."""
            return d[(sid, j + 1)] if (sid, j + 1) in d else C[(sid, j)]

        for k, sts in cell.by_k.items():
            k_ops = [(sid, j) for sid, j in ops if smap[sid].seq[j] == k]
            for i in sts:
                for (a, b) in itertools.combinations(k_ops, 2):
                    if a[0] == b[0]:
                        continue
                    pi = pulp.LpVariable(f"pi_{i}_{a[0]}{a[1]}_{b[0]}{b[1]}", cat="Binary")
                    prob += S[b] >= release_time(*a) - H * (
                        3 - pi - y[(a[0], a[1], i)] - y[(b[0], b[1], i)])
                    prob += S[a] >= release_time(*b) - H * (
                        2 + pi - y[(a[0], a[1], i)] - y[(b[0], b[1], i)])

        # (C6) robot capacity with assignment-dependent empty legs.
        # z[t,o,f] selects the loaded leg o->f; y[t_prev,o_next] selects
        # the pick-up station of the following task.  The inequalities below
        # are therefore the exact linearisation of m[o,f] + m[f,o_next].
        for (t1, t2) in itertools.combinations(tasks, 2):
            delta = pulp.LpVariable(f"dl_{t1[0]}{t1[1]}_{t2[0]}{t2[1]}", cat="Binary")
            for o1 in cand[(t1[0], t1[1] - 1)]:
                for f1 in cand[t1]:
                    for o2 in cand[(t2[0], t2[1] - 1)]:
                        prob += d[t2] >= d[t1] + cell.m(o1, f1) + cell.m(f1, o2) - H * (
                            3 - delta - z[(t1[0], t1[1], o1, f1)]
                            - y[(t2[0], t2[1] - 1, o2)])
            for o2 in cand[(t2[0], t2[1] - 1)]:
                for f2 in cand[t2]:
                    for o1 in cand[(t1[0], t1[1] - 1)]:
                        prob += d[t1] >= d[t2] + cell.m(o2, f2) + cell.m(f2, o1) - H * (
                            2 + delta - z[(t2[0], t2[1], o2, f2)]
                            - y[(t1[0], t1[1] - 1, o1)])

        if solver_name == "scip":
            solver = pulp.SCIP_PY(msg=False, timeLimit=time_limit, threads=threads)
        elif solver_name == "gurobi":
            solver = pulp.GUROBI(msg=False, timeLimit=time_limit, threads=threads)
        else:
            solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, threads=threads)
        prob.solve(solver)
        if solver_name == "scip":
            model = prob.solverModel
            if str(model.getStatus()) != "optimal":
                raise RuntimeError(f"SCIP did not prove optimality: {model.getStatus()}")
            self.proof = {"status": str(model.getStatus()),
                          "primal_bound": model.getPrimalbound(),
                          "dual_bound": model.getDualbound(),
                          "gap": model.getGap()}
        else:
            self.proof = {"status": pulp.LpStatus[prob.status]}
        status = pulp.LpStatus[prob.status]
        solution_status = pulp.LpSolution[prob.sol_status]
        best = pulp.value(Cmax)
        try:
            lb = prob.solverModel.getDualbound()  # not available with CBC_CMD
        except Exception:
            lb = None
        return status, solution_status, best, lb, len(prob.variables()), len(prob.constraints)


# ------------------------------------------------------------- URG policy ----
class UrgencyPolicy:
    """Reference implementation of the lexicographic urgency rule (URG)."""

    name = "URG"
    W_MAX = 300.0

    def simulate(self, inst: Instance) -> float:
        return simulate(inst, policy="URG", trace=False)["kpi"]["makespan_s"]


# -------------------------------------------------------------------- main ---
def default_paths() -> tuple[Path, Path]:
    """Resolve paths both in the paper tree and in the data repository."""
    root = Path(__file__).resolve().parents[1]
    package = root if (root / "instances").exists() else root / "github_data"
    return (package / "instances" / "cs1_nominal_config.json",
            package / "data" / "exact_reduced_instances.csv")


def main():
    default_config, default_output = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--time-limit", type=int, default=1200)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--solver", choices=("cbc", "scip", "gurobi"), default="scip")
    parser.add_argument("--instances", nargs="*", choices=("R1", "R2", "R3", "R4", "R5"))
    args = parser.parse_args()

    mult = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1, 8: 2, 9: 1, 10: 1,
            11: 2, 12: 1, 13: 1, 14: 1}
    cell = Cell(str(args.config), mult)
    print(f"reduced cell: {len(cell.stations)} stations, "
          f"multiplicities {[(k, len(v)) for k, v in sorted(cell.by_k.items())]}")

    families = {
        "R1": [("HC", 0.0), ("SVOC", 375.0), ("HC", 750.0)],
        "R2": [("HC", 0.0), ("SVOC", 375.0), ("SVOC_E", 750.0)],
        "R3": [("HC", 0.0), ("SVOC", 180.0), ("HC", 360.0), ("SVOC", 540.0)],
        "R4": [("HC", 0.0), ("SVOC", 375.0), ("SVOC_E", 750.0), ("HC", 1125.0)],
        "R5": [("HC", 0.0), ("SVOC", 180.0), ("SVOC_E", 360.0), ("HC", 540.0),
               ("SVOC", 720.0)],
    }
    labels = {
        "R1": ("2 M1, 1 M2", 375),
        "R2": ("1 M1, 1 M2, 1 M2E", 375),
        "R3": ("2 M1, 2 M2", 180),
        "R4": ("2 M1, 1 M2, 1 M2E", 375),
        "R5": ("2 M1, 2 M2, 1 M2E", 180),
    }
    cert = CompositeBound([CapacityBound(), ReleasePathBound()])
    ht = CompositeBound([CapacityBound(), ReleasePathBound(), HeadTailBound()])
    urg = UrgencyPolicy()

    rows = []
    selected = args.instances or list(families)
    for name in selected:
        spec = families[name]
        inst = Instance(cell, spec)
        lb0, lb1 = cert.value(inst), ht.value(inst)
        heur = urg.simulate(inst)
        started = time.perf_counter()
        exact_model = ExactModel(inst)
        status, solution_status, opt, _, nv, nc = exact_model.solve(
            time_limit=args.time_limit, threads=args.threads, solver_name=args.solver)
        elapsed = time.perf_counter() - started
        proof_dir = args.output.parent.parent / "validation" / "exact"
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / f"{name}_proof.json").write_text(json.dumps(exact_model.proof, indent=2)+"\n")
        if solution_status != "Optimal Solution Found":
            raise RuntimeError(
                f"{name}: {args.solver.upper()} returned {status}/{solution_status}, "
                "not a proven optimum")
        rows.append({
            "instance": name,
            "n": inst.n,
            "mix": labels[name][0],
            "pacing_s": labels[name][1],
            "lb_cap_path_s": round(lb0, 2),
            "lb_certificate_s": round(lb1, 2),
            "opt_makespan_s": round(opt, 2),
            "urg_makespan_s": round(heur, 2),
            "certificate_slack_pct": round(100 * (opt - lb1) / opt, 2),
            "urg_excess_pct": round(100 * (heur - opt) / opt, 2),
            "solver": args.solver.upper(),
            "milp_status": status,
            "solve_time_s": round(elapsed, 2),
            "n_variables": nv,
            "n_constraints": nc,
        })
        print(f"{name} n={inst.n} {status:10s} opt={opt:9.2f} "
              f"lb_v2={lb0:9.2f} lb_ht={lb1:9.2f} urg={heur:9.2f} "
              f"({nv} vars, {nc} cons)")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("written", args.output)


if __name__ == "__main__":
    main()
