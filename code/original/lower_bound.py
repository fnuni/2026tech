#!/usr/bin/env python3
"""
lower_bound.py - Aggregate Capacity Lower Bound (POLICY3)
=========================================================
Implements the aggregate linear-programming relaxation proposed in the
note "Spunti per lo sviluppo di un lower bound da innestare in un
algoritmo di programmazione dinamica approssimata, roll-out o simili"
(May 2026).

Model recap
-----------
The cell has k machine types, type j available in m_j parallel units.
Sample (product) i requires t_ij seconds on a machine of type j.
Robot moves and sequencing are neglected, hence every quantity derived
here is a valid LOWER BOUND on the residual completion time (and the
closed forms of Sections 2-3 of the note are valid UPPER BOUNDS on the
achievable throughput).

Provided quantities
-------------------
1.  residual_loads(...)   : R_j = sum of outstanding processing seconds
                            per machine type (plus in-process residuals).
2.  lb_remaining(...)     : max( max_j R_j / m_j ,  R_rob )
                            where R_rob is the outstanding robot
                            transport load (single shared robot).
3.  max_throughput(...)   : closed form  x*_i = min_j m_j T / t_ij
                            (Section 2 of the note).
4.  residual_throughput() : max units of a second product i** once the
                            volume of i* is fixed at x*_i (Section 3).

All classes are dependency-free (standard library only).
"""

from collections import defaultdict


class CapacityModel:
    """Static aggregate view of the cell: machine counts and per-type
    processing requirements derived from config.json."""

    def __init__(self, config: dict):
        self.cfg = config
        # m_j : number of physical stations per type
        self.m = defaultdict(int)
        for _v, info in config["stations"].items():
            self.m[int(info["type_k"])] += 1

        # Fallback per-type processing time (station-type catalogue)
        self.p_type = {int(k): float(v.get("p_k_s", 0.0))
                       for k, v in config["station_types"].items()}

        # Robot movement time: average off-diagonal transport entry
        mij = config.get("transport_s", {})
        vals = [t for a, row in mij.items()
                for b, t in row.items() if a != b]
        self.move_s = (sum(vals) / len(vals)) if vals else 15.0

    # -- per-stage processing time of a sample type ------------------------
    def stage_time(self, sample_type: str, stage_idx: int) -> float:
        """Processing time of route stage `stage_idx` (0-based) for the
        given sample type.  Uses the per-route p_s_sequence if present
        (POLICY3 extension), otherwise the station-type catalogue."""
        route = self.cfg["routes"][sample_type]
        seq = route["type_sequence"]
        if stage_idx >= len(seq):
            return 0.0
        p_seq = route.get("p_s_sequence")
        if p_seq and stage_idx < len(p_seq):
            return float(p_seq[stage_idx])
        return self.p_type.get(int(seq[stage_idx]), 0.0)

    def stage_type(self, sample_type: str, stage_idx: int) -> int:
        seq = self.cfg["routes"][sample_type]["type_sequence"]
        return int(seq[stage_idx]) if stage_idx < len(seq) else -1

    # -- total processing vector t_ij of one unit of sample type ----------
    def t_vector(self, sample_type: str) -> dict:
        """t_ij aggregated over the route: seconds required on each
        machine type by ONE sample of the given type."""
        t = defaultdict(float)
        seq = self.cfg["routes"][sample_type]["type_sequence"]
        for idx in range(len(seq)):
            t[self.stage_type(sample_type, idx)] += self.stage_time(
                sample_type, idx)
        return dict(t)


class AggregateLowerBound:
    """Aggregate LP lower bound on residual completion time, used as the
    cost-to-go approximation of the POLICY3 roll-out policy."""

    def __init__(self, config: dict):
        self.cap = CapacityModel(config)
        self.cfg = config

    # ------------------------------------------------------------------
    #  Residual workloads
    # ------------------------------------------------------------------
    def residual_loads(self, X_sam: dict, X_st: dict,
                       elapsed: float = 0.0):
        """R_j (seconds of outstanding work per machine type) and R_rob
        (outstanding robot transport seconds).

        X_sam : current sample states (input.json schema)
        X_st  : current station states
        elapsed : seconds of station progress to credit (used by the
                  roll-out to evaluate the post-action state at t+dur).
        """
        R = defaultdict(float)
        n_moves = 0

        for _sid, sm in X_sam.items():
            if sm.get("status") == "completed":
                continue
            stype = sm["type"]
            stage = sm.get("stage_j", 1)          # stages completed
            seq = self.cfg["routes"][stype]["type_sequence"]

            if sm.get("status") == "processing":
                # residual of the CURRENT op is read from the station
                v = sm.get("station")
                r = max(0.0, X_st.get(v, {}).get("r_i_s", 0.0) - elapsed)
                k = self.cap.stage_type(stype, stage - 1)
                R[k] += r

            # all future stages count in full
            for idx in range(stage, len(seq)):
                R[self.cap.stage_type(stype, idx)] += \
                    self.cap.stage_time(stype, idx)
                n_moves += 1                      # one transfer per stage

        # each transfer = empty leg + loaded leg
        R_rob = max(0.0, n_moves * 2.0 * self.cap.move_s - elapsed)
        return dict(R), R_rob

    def lb_remaining(self, X_sam: dict, X_st: dict,
                     elapsed: float = 0.0):
        """LB on the residual completion time:
              max( max_j R_j / m_j ,  R_rob )
        Returns (lb, diagnostics)."""
        R, R_rob = self.residual_loads(X_sam, X_st, elapsed)
        lb_j = {j: (R[j] / self.cap.m[j]) for j in R if self.cap.m[j] > 0}
        bottleneck = max(lb_j, key=lb_j.get) if lb_j else None
        lb_mach = lb_j.get(bottleneck, 0.0) if bottleneck is not None else 0.0
        lb = max(lb_mach, R_rob)
        diag = {
            "R_j": {int(j): round(v, 1) for j, v in R.items()},
            "lb_j": {int(j): round(v, 1) for j, v in lb_j.items()},
            "R_rob": round(R_rob, 1),
            "bottleneck_type": bottleneck,
            "lb_machines": round(lb_mach, 1),
            "lb": round(lb, 1),
        }
        return lb, diag

    # ------------------------------------------------------------------
    #  Closed-form throughput bounds (Sections 2-3 of the note)
    # ------------------------------------------------------------------
    def max_throughput(self, sample_type: str, T: float):
        """x*_i = min_{j : t_ij > 0}  m_j T / t_ij   (Section 2).
        Returns (x_star, bottleneck_type)."""
        t = self.cap.t_vector(sample_type)
        best, arg = float("inf"), None
        for j, tij in t.items():
            if tij <= 0:
                continue
            val = self.cap.m[j] * T / tij
            if val < best:
                best, arg = val, j
        return best, arg

    def residual_throughput(self, first_type: str, second_type: str,
                            T: float):
        """Max units of `second_type` once `first_type` is produced at
        its maximum volume x*_{i*} (Section 3)."""
        x1, _ = self.max_throughput(first_type, T)
        t1 = self.cap.t_vector(first_type)
        t2 = self.cap.t_vector(second_type)
        best = float("inf")
        for j, t2j in t2.items():
            if t2j <= 0:
                continue
            residual = self.cap.m[j] * T - t1.get(j, 0.0) * x1
            if residual <= 0:
                return 0.0, j
            best = min(best, residual / t2j)
        return best, None
