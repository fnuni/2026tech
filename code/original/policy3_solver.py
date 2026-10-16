#!/usr/bin/env python3
"""
policy3_solver.py - LB-guided Roll-out Policy (POLICY3)
=======================================================
Third-generation solver for the LabAnalysis Robotic Cell Planning
Problem.  Object-oriented refactoring of the solver family:

    SolverBase            abstract: JSON I/O contract, main loop
      +-- GreedyPolicy    two-tier urgency rule  (= greedy_solver.py)
            +-- CPRLAPolicy     composite priority rule + MECT
                  +-- LBRolloutPolicy   one-step roll-out with the
                                        aggregate-capacity lower bound
                                        as cost-to-go  (POLICY3, default)

The roll-out policy evaluates every candidate action a = (sample s ->
station v) with the score

    z(a) = dur(a) + LB(x | a) + w_viol * NV(a) + w_over * OV(a)

where LB(x|a) is the aggregate capacity lower bound on the residual
completion time of the post-decision state (lower_bound.py, from the
note "Spunti per un LB da usare in ADP, roll-out", May 2026), NV(a) is
the number of waiting samples whose W_max budget would be NEWLY
exceeded during the execution of a, and OV(a) is the normalised total
overshoot of the waiting budgets.  Ties are broken by slack
(W_max - w_s) and transport duration, which makes the policy collapse
to CPR-LA/greedy when the roll-out terms are uninformative.

JSON interface (identical to greedy_solver.py / cpr_la_solver.py):
    reads  config.json, input.json
    writes output.json, opt_meta.json
Exit codes: 0 = OK, 1 = error (simulator falls back to its heuristic).
"""

import json
import math
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime

from lower_bound import AggregateLowerBound

CONFIG_FILE = "config.json"
INPUT_FILE = "input.json"
OUTPUT_FILE = "output.json"
OPT_META_FILE = "opt_meta.json"

QC_TYPES = {"QC1", "QC2", "Blank"}
TYPE_PRIORITY = {"Blank": 0, "QC2": 1, "QC1": 2,
                 "HC": 3, "SVOC": 3, "SVOC_E": 3}

# Default POLICY3 parameters (overridable via config["opt_params"])
DEFAULTS = {
    "gamma_qc": 1.0,        # QC anticipation weight (entry tier)
    "delta_qc": 3,          # QC anticipation window (samples)
    "w_viol": 600.0,        # penalty per newly violated waiting budget
    "w_over": 120.0,        # penalty per unit of normalised overshoot
    "rollout_band": 1.0e6,  # slack quantisation band (s) for the pending
                            # tier.  Large value (default) = full roll-out
                            # ranking by z(a); small values (e.g. 60)
                            # progressively recover the lexicographic
                            # greedy urgency ordering, with z(a) acting
                            # only inside each slack band
    "rollout_enabled": 1,   # 0 -> collapse to CPR-LA behaviour
}


# ======================================================================
#  Base class: I/O contract
# ======================================================================
class SolverBase(ABC):
    """Abstract solver: loads the JSON state, delegates the decision to
    select_action(), serialises output.json / opt_meta.json."""

    name = "base"

    def __init__(self):
        with open(CONFIG_FILE) as fh:
            self.cfg = json.load(fh)
        with open(INPUT_FILE) as fh:
            self.inp = json.load(fh)

        self.W_max = self.cfg.get("W_max_s", 300)
        self.X_sam = self.inp.get("X_sam", {})
        self.X_st = self.inp.get("X_st", {})
        self.X_qc = self.inp.get("X_qc", {})
        self.t = self.inp.get("t_s", 0.0)
        self.epoch = self.inp.get("epoch", "tau_?")
        self.iid = self.inp.get("instance_id", "?")
        self.feasible = self.inp.get("feasible_actions", [])
        self.robot_loc = self.inp.get("X_rob", {}).get("location", "v1")
        self.mij = self.cfg["transport_s"]

        self.type2v = defaultdict(list)
        for v, info in self.cfg["stations"].items():
            self.type2v[int(info["type_k"])].append(v)

        opt = self.cfg.get("opt_params", {})
        self.params = {k: opt.get(k, d) for k, d in DEFAULTS.items()}
        self.diag = {}

    # -- helpers --------------------------------------------------------
    def split_tiers(self):
        """Two-tier split: in-process (stage > 1) vs entry samples."""
        pending, entry = [], []
        for a in self.feasible:
            sid = a.get("sample_id")
            if not sid or sid not in self.X_sam:
                continue
            stage = self.X_sam[sid].get("stage_j", 0)
            (pending if stage > 1 else entry).append(a)
        return pending, entry

    def reps_per_sample(self, actions):
        """One representative action per sample (shortest duration)."""
        by_s = defaultdict(list)
        for a in actions:
            by_s[a["sample_id"]].append(a)
        return {sid: min(acts, key=lambda a: a["duration_s"])
                for sid, acts in by_s.items()}

    @abstractmethod
    def select_action(self):
        """Return the chosen action dict (or None)."""

    # -- output serialisation --------------------------------------------
    def write_output(self, chosen):
        act = None
        if chosen:
            act = {
                "task_id": chosen.get("task_id"),
                "type": chosen.get("type", "loaded"),
                "sample_id": chosen.get("sample_id"),
                "origin": chosen.get("origin"),
                "pickup": chosen.get("pickup", chosen.get("origin")),
                "destination": chosen.get("destination"),
                "start_s": self.t,
                "duration_s": chosen.get("duration_s"),
                "end_s": self.t + (chosen.get("duration_s") or 0),
            }
        with open(OUTPUT_FILE, "w") as fh:
            json.dump({
                "_comment": f"{self.name} solver - next action decision.",
                "instance_id": self.iid,
                "epoch": self.epoch,
                "t_s": self.t,
                "solver": self.name,
                "selected_action": act,
            }, fh, indent=2)

    def write_meta(self, chosen, solve_s):
        meta = {
            "_comment": f"{self.name} - optimisation metadata.",
            "instance_id": self.iid,
            "epoch": self.epoch,
            "t_s": self.t,
            "solver": self.name,
            "status": "FEASIBLE" if chosen else "NO_ACTION",
            "solve_time_s": round(solve_s, 6),
            "n_feasible_actions": len(self.feasible),
            "parameters": self.params,
            "diagnostics": self.diag,
            "selected_task_id": chosen.get("task_id") if chosen else None,
            "selected_sample": chosen.get("sample_id") if chosen else None,
            "selected_dest": chosen.get("destination") if chosen else None,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(OPT_META_FILE, "w") as fh:
            json.dump(meta, fh, indent=2)

    # -- entry point ------------------------------------------------------
    def run(self):
        t0 = time.perf_counter()
        chosen = self.select_action()
        solve_s = time.perf_counter() - t0
        self.write_output(chosen)
        self.write_meta(chosen, solve_s)
        if chosen:
            sid = chosen.get("sample_id", "-")
            w_s = self.X_sam.get(sid, {}).get("w_s", 0) if sid else 0
            print(f"[{self.name.upper()}] {self.epoch} | t={self.t:.0f}s | "
                  f"n={len(self.feasible)} | -> {sid}@"
                  f"{chosen.get('destination')} (w={w_s:.0f}s, "
                  f"dur={chosen.get('duration_s')}s) | "
                  f"solve={solve_s*1000:.2f}ms")
        else:
            print(f"[{self.name.upper()}] {self.epoch} | NO ACTION")
        return 0


# ======================================================================
#  Tier 1: greedy two-tier urgency policy
# ======================================================================
class GreedyPolicy(SolverBase):
    """Greedy rule (GreedyAlgorithm_B): most urgent pending sample
    first, nearest idle station; entry queue ordered by QC priority."""

    name = "greedy_oop"

    def select_action(self):
        pending, entry = self.split_tiers()
        if pending:
            reps = self.reps_per_sample(pending)

            def key(sid):
                sm = self.X_sam[sid]
                return (self.W_max - sm.get("w_s", 0.0),
                        TYPE_PRIORITY.get(sm.get("type"), 99),
                        reps[sid]["duration_s"])
            self.diag["tier"] = "pending"
            return reps[min(reps, key=key)]
        if entry:
            reps = self.reps_per_sample(entry)

            def key(sid):
                sm = self.X_sam[sid]
                return (TYPE_PRIORITY.get(sm.get("type"), 99),
                        self.W_max - sm.get("w_s", 0.0),
                        reps[sid]["duration_s"])
            self.diag["tier"] = "entry"
            return reps[min(reps, key=key)]
        return None


# ======================================================================
#  Tier 2: CPR-LA (composite priority + MECT destination)
# ======================================================================
class CPRLAPolicy(GreedyPolicy):
    """Composite Priority Rule with one-step Look-Ahead (POLICY2)."""

    name = "cpr_la_oop"

    # -- candidate destinations ------------------------------------------
    def candidate_destinations(self, sid):
        """All (dest, dur, p_next) candidates for the next stage of
        sample sid.

        NOTE (v3 correction): POLICY2's MECT rule also admitted busy
        stations whose residual processing time was below the transport
        time ("free on arrival").  That rule violates the single-position
        constraint: the finished vial is still physically at the station
        until the robot removes it.  v3 therefore only admits stations
        that are idle AND empty."""
        sm = self.X_sam[sid]
        stype = sm["type"]
        route = self.cfg["routes"][stype]
        seq = route["type_sequence"]
        stage = sm["stage_j"]
        if stage >= len(seq):
            return []
        next_k = int(seq[stage])
        p_seq = route.get("p_s_sequence")
        p_next = (float(p_seq[stage]) if p_seq and stage < len(p_seq)
                  else float(self.cfg["station_types"]
                             [str(next_k)]["p_k_s"]))
        pickup = sm["station"]
        t_empty = self.mij.get(self.robot_loc, {}).get(pickup, 0)
        out = []
        for v in self.type2v.get(next_k, []):
            vst = self.X_st.get(v, {})
            busy = vst.get("b_i", 0) == 1
            occupied = vst.get("sample_id") is not None
            dur = t_empty + self.mij.get(pickup, {}).get(v, 999)
            if not busy and not occupied:
                out.append((v, dur, p_next))
        return out

    def qc_anticipation_key(self, sid, reps):
        """Entry-tier ordering with QC anticipation boost."""
        sm = self.X_sam[sid]
        stype = sm.get("type", "HC")
        B = self.cfg.get("qc_policy", {}).get("block_size", 10)
        rho = self.X_qc.get("N_HC", 0) % B
        boost = (self.params["gamma_qc"] * (rho / B)
                 if (stype in QC_TYPES
                     and rho >= B - self.params["delta_qc"]) else 0.0)
        return (TYPE_PRIORITY.get(stype, 99) - boost,
                self.W_max - sm.get("w_s", 0.0),
                reps[sid]["duration_s"])

    def select_action(self):
        pending, entry = self.split_tiers()
        tier, reps = (("pending", self.reps_per_sample(pending))
                      if pending else
                      ("entry", self.reps_per_sample(entry))
                      if entry else (None, None))
        if tier is None:
            return None
        self.diag["tier"] = tier

        if tier == "pending":
            def key(sid):
                sm = self.X_sam[sid]
                return (self.W_max - sm.get("w_s", 0.0),
                        reps[sid]["duration_s"])
            best_sid = min(reps, key=key)
        else:
            best_sid = min(reps,
                           key=lambda s: self.qc_anticipation_key(s, reps))

        base = reps[best_sid]
        # MECT destination refinement
        cands = self.candidate_destinations(best_sid)
        if cands:
            v, dur, _p = min(cands, key=lambda c: c[1])
            if dur <= base["duration_s"]:
                base = self._action_for(base, v, dur)
                self.diag["mect_override"] = v
        return base

    def _action_for(self, base, dest, dur):
        pickup = base.get("pickup", base.get("origin"))
        t_empty = self.mij.get(self.robot_loc, {}).get(pickup, 0)
        return {**base, "destination": dest, "duration_s": dur,
                "t_empty_s": t_empty, "t_loaded_s": dur - t_empty}


# ======================================================================
#  Tier 3: POLICY3 - one-step roll-out with aggregate LB cost-to-go
# ======================================================================
class LBRolloutPolicy(CPRLAPolicy):
    """For each candidate (sample, destination) the policy simulates the
    post-decision state one step ahead and evaluates it with the
    aggregate capacity lower bound; waiting-budget violations are
    penalised explicitly."""

    name = "policy3_lb_rollout"

    def __init__(self):
        super().__init__()
        self.lb = AggregateLowerBound(self.cfg)

    # -- penalty terms ---------------------------------------------------
    def _violation_terms(self, serving_sid, dur):
        """NV = waiting samples newly pushed past W_max while the robot
        executes the action; OV = normalised total overshoot."""
        nv, ov = 0, 0.0
        for sid, sm in self.X_sam.items():
            if sid == serving_sid or sm.get("status") != "waiting_robot":
                continue
            w_now = sm.get("w_s", 0.0)
            w_end = w_now + dur
            if w_end > self.W_max:
                ov += (w_end - self.W_max) / self.W_max
                if w_now <= self.W_max:
                    nv += 1
        return nv, ov

    def _idle_types(self):
        """Machine types with at least one idle physical unit."""
        idle = set()
        for v, vst in self.X_st.items():
            if vst.get("b_i", 0) == 0 and vst.get("sample_id") is None:
                idle.add(int(self.cfg["stations"][v]["type_k"]))
        return idle

    def _score(self, sid, dur, next_k):
        """Roll-out score z(a) of serving sample sid with total
        transport duration dur towards a station of type next_k.

        The aggregate LB is sharpened with a starvation term: a machine
        type with an idle unit and outstanding work that is NOT fed by
        the action loses dur seconds of capacity, i.e. its aggregate
        completion estimate increases by dur / m_j.  Feeding the
        bottleneck therefore lowers the estimate, which is exactly the
        roll-out behaviour suggested by the LB note."""
        R, R_rob = self.lb.residual_loads(self.X_sam, self.X_st,
                                          elapsed=dur)
        idle = self._idle_types()
        lb_mach, bottleneck = 0.0, None
        for j, Rj in R.items():
            m_j = self.lb.cap.m[j]
            if m_j <= 0 or Rj <= 0:
                continue
            lost = dur if (j in idle and j != next_k) else 0.0
            val = (Rj + lost) / m_j
            if val > lb_mach:
                lb_mach, bottleneck = val, j
        lb_after = max(lb_mach, R_rob)
        nv, ov = self._violation_terms(sid, dur)
        z = (dur + lb_after
             + self.params["w_viol"] * nv
             + self.params["w_over"] * ov)
        return z, {"lb_after": round(lb_after, 1), "nv": nv,
                   "ov": round(ov, 3), "bottleneck": bottleneck}

    # -- main decision ----------------------------------------------------
    def select_action(self):
        if not self.params.get("rollout_enabled", 1):
            return super().select_action()

        pending, entry = self.split_tiers()
        tier, reps = (("pending", self.reps_per_sample(pending))
                      if pending else
                      ("entry", self.reps_per_sample(entry))
                      if entry else (None, None))
        if tier is None:
            return None
        self.diag["tier"] = tier
        band = max(1.0, self.params["rollout_band"])

        # Candidate set: every (sample, destination) pair in the tier,
        # destinations enlarged with the MECT free-on-arrival stations.
        #
        # Ranking (non-regression by construction):
        #   pending tier: primary key  = slack quantised to rollout_band
        #                 (greedy urgency ordering, band-wise)
        #                 secondary    = roll-out score z(a)
        #   entry tier:   primary key  = QC-policy type priority with
        #                 QC anticipation (CPR-LA), secondary = z(a)
        scored = []
        for sid, base in reps.items():
            cands = self.candidate_destinations(sid) or \
                [(base["destination"], base["duration_s"], 0.0)]
            sm = self.X_sam[sid]
            slack = self.W_max - sm.get("w_s", 0.0)
            if tier == "pending":
                # greedy urgency ordering, quantised to rollout_band;
                # the roll-out score discriminates within a band
                key12 = (math.floor(slack / band), None)
            else:
                # QC-policy priority, then greedy waiting order; the
                # roll-out score only breaks exact ties (FIFO-safe)
                key12 = (self.qc_anticipation_key(sid, reps)[0], slack)
            for v, dur, _p in cands:
                next_k = int(self.cfg["stations"][v]["type_k"])
                z, zdiag = self._score(sid, dur, next_k)
                k2 = key12[1] if key12[1] is not None else z
                scored.append((key12[0], k2, z, slack, dur, sid, v,
                               zdiag, base))

        if not scored:
            return None
        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        _k1, _k2, z, _sl, dur, sid, dest, zdiag, base = scored[0]

        self.diag["rollout"] = {
            "n_candidates": len(scored),
            "selected": {"sample": sid, "dest": dest,
                         "z": round(z, 1), **zdiag},
            "top5": [{"sample": c[5], "dest": c[6], "z": round(c[2], 1)}
                     for c in scored[:5]],
        }
        if dest != base["destination"] or dur != base["duration_s"]:
            return self._action_for(base, dest, dur)
        return base


# ======================================================================
POLICIES = {
    "greedy": GreedyPolicy,
    "cprla": CPRLAPolicy,
    "policy3": LBRolloutPolicy,
}


def main():
    # Policy selectable via CLI arg or config["opt_params"]["policy"]
    name = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        if name is None:
            with open(CONFIG_FILE) as fh:
                name = json.load(fh).get("opt_params", {}).get(
                    "policy", "policy3")
        solver = POLICIES.get(name, LBRolloutPolicy)()
        return solver.run()
    except FileNotFoundError as exc:
        print(f"[POLICY3] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
