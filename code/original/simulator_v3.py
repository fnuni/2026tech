#!/usr/bin/env python3
"""
simulator_v3.py - Robotic Cell Discrete-Event Simulator (v3, POLICY3)
=====================================================================
Extends simulator_v2.py (inheritance) with:

  1. PPLA instance generator
     Builds config.json from the LabAnalysis/Alsys project-plan data
     (report 20240710PPLA.1, Chapter 8 "Capacity Calculation"):
       - sample mix HC / SVOC / QC / Blank  (230 samples per 24 h at
         full scale, 30 % of Method-2 samples contain emulsion),
       - module catalogue and processing times (de-capper 300 s,
         pipetting A/B/C/D 180/300/600/180 s, stirrer 1800/60 s,
         concentrator 600/1800 s, centrifuge 300 s),
       - module positions recommended by Alsys (2 de-cappers 1000 mL,
         10 stirrer positions, 10 concentrator positions, ...),
       - robot movement time 15 s per pick-and-place movement.

  2. Per-route processing times
     Stirrer and concentrator durations depend on the analytical method
     (Method 1 vs Method 2).  Routes therefore carry an optional
     "p_s_sequence" with the stage-wise processing times; the simulator
     uses it when present and falls back to the station-type catalogue
     otherwise (full backward compatibility with v2 instances).

  3. KPI export
     Writes kpi.json (makespan, violations, robot utilisation, flow
     times, solver statistics) consumed by compare_results.py.

Usage:
    python simulator_v3.py --instance ppla --scale 0.1 --interval 60 \
                           --seed 42 --solver policy3_solver.py --all-actions
    python simulator_v3.py --instance synthetic ...   (v2 behaviour)
"""

import argparse
import json
import math
import random
from datetime import datetime

import simulator_v2 as v2

CONFIG_FILE = v2.CONFIG_FILE
KPI_FILE = "kpi.json"

# ---------------------------------------------------------------------------
#  PPLA station-type catalogue (taxonomy: 1-5 I/O as in the formal model,
#  6-14 processing modules from report 20240710PPLA.1, Chapter 8)
# ---------------------------------------------------------------------------
PPLA_STATION_TYPES = {
    1:  {"name": "HC/SVOC Entry",        "p_k_s": 0,    "count": 1},
    2:  {"name": "QC1 Entry",            "p_k_s": 0,    "count": 1},
    3:  {"name": "QC2 Entry",            "p_k_s": 0,    "count": 1},
    4:  {"name": "Blank Entry",          "p_k_s": 0,    "count": 1},
    5:  {"name": "Sample Exit",          "p_k_s": 0,    "count": 1},
    6:  {"name": "De-capper 1000mL",     "p_k_s": 300,  "count": 2},
    7:  {"name": "Pipet A (dosing)",     "p_k_s": 180,  "count": 1},
    8:  {"name": "Stirrer",              "p_k_s": 1800, "count": 10},
    9:  {"name": "Pipet B (extr. M1)",   "p_k_s": 300,  "count": 1},
    10: {"name": "Pipet C (extr. M2)",   "p_k_s": 600,  "count": 1},
    11: {"name": "Concentrator",         "p_k_s": 600,  "count": 10},
    12: {"name": "De-capper centrifuge", "p_k_s": 300,  "count": 1},
    13: {"name": "Centrifuge",           "p_k_s": 300,  "count": 1},
    14: {"name": "Pipet D (GC-vial)",    "p_k_s": 180,  "count": 1},
}

# Routes: type_sequence + stage-wise processing times (p_s_sequence).
# Method 1 (HC): stir 30 min, concentrate 10 min, extraction on Pipet B.
# Method 2 (SVOC): stir 1 min, concentrate 30 min, extraction on Pipet C;
# 30 % of Method-2 samples contain emulsion -> de-cap + centrifuge detour.
PPLA_ROUTES = {
    "HC": {
        "method": 1,
        "type_sequence": [1, 6, 7, 8, 9, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 1800, 300, 600, 180, 0],
    },
    "SVOC": {
        "method": 2,
        "type_sequence": [1, 6, 7, 8, 10, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 60, 600, 1800, 180, 0],
    },
    "SVOC_E": {
        "method": 2,
        "type_sequence": [1, 6, 7, 8, 12, 13, 10, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 60, 300, 300, 600, 1800, 180, 0],
    },
    "QC1":   {  # spiked Method-1 sample
        "method": 1,
        "type_sequence": [2, 6, 7, 8, 9, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 1800, 300, 600, 180, 0],
    },
    "QC2":   {  # spiked Method-2 sample
        "method": 2,
        "type_sequence": [3, 6, 7, 8, 10, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 60, 600, 1800, 180, 0],
    },
    "Blank": {  # contamination check, processed as Method 1
        "method": 1,
        "type_sequence": [4, 6, 7, 8, 9, 11, 14, 5],
        "p_s_sequence":  [0, 300, 180, 1800, 300, 600, 180, 0],
    },
}

ROBOT_MOVE_S = 15.0       # PPLA: nominal (mean) pick&place cycle time = 15 s
V_GANTRY = 1.5            # PPLA/D2.1: gantry traverse speed = 1.5 m/s
EMULSION_FRACTION = 0.30  # PPLA: ~30 % of Method-2 samples


def _ppla_layout(rng):
    """2-D positions (metres) for visualisation only; transport times
    are the constant PPLA movement time, not distance-based."""
    zones = {
        8:  ((0.05, 0.60), (0.05, 0.62)),   # stirrers, far left
        11: ((0.70, 1.25), (0.05, 0.62)),   # concentrators
        12: ((1.35, 1.50), (0.10, 0.25)),
        13: ((1.35, 1.50), (0.35, 0.50)),
        9:  ((1.60, 1.75), (0.10, 0.25)),
        10: ((1.60, 1.75), (0.35, 0.50)),
        7:  ((1.85, 2.00), (0.10, 0.25)),
        14: ((1.85, 2.00), (0.35, 0.50)),
        6:  ((2.15, 2.45), (0.12, 0.50)),
        1:  ((2.85, 2.95), (0.05, 0.13)),
        2:  ((2.85, 2.95), (0.17, 0.25)),
        3:  ((2.85, 2.95), (0.29, 0.37)),
        4:  ((2.85, 2.95), (0.41, 0.49)),
        5:  ((2.85, 2.95), (0.53, 0.61)),
    }
    positions, stations = {}, {}
    v_idx = 1
    for k in [8, 11, 12, 13, 9, 10, 7, 14, 6, 1, 2, 3, 4, 5]:
        (x0, x1), (y0, y1) = zones[k]
        n = PPLA_STATION_TYPES[k]["count"]
        for i in range(n):
            vid = f"v{v_idx}"
            if n > 1:                       # regular grid inside zone
                cols = max(1, round(math.sqrt(n)))
                r, c = divmod(i, cols)
                x = x0 + (x1 - x0) * (c + 0.5) / cols
                y = y0 + (y1 - y0) * (r + 0.5) / math.ceil(n / cols)
            else:
                x, y = (x0 + x1) / 2, (y0 + y1) / 2
            positions[vid] = (round(x, 4), round(y, 4))
            stations[vid] = {
                "type_k": k,
                "name": f"{PPLA_STATION_TYPES[k]['name']} #{i+1}",
            }
            v_idx += 1
    return positions, stations


def build_distance_transport(positions, mean_s=ROBOT_MOVE_S,
                             v_gantry=V_GANTRY):
    """Physically-grounded transfer-time matrix.

    A robot pick&place cycle is decomposed into a fixed dwell component
    (positioning, gripper close/verify, open, retract -- distance
    independent) and a traverse component along the inter-station path:

        t(a,b) = t_dwell + d_ab / v_gantry ,   t(a,a) = 0,

    with the gantry traverse speed ``v_gantry`` = 1.5 m/s (PPLA Sec. 4.1,
    D2.1 Sec. 3.3).  The fixed dwell is calibrated so that the mean cycle
    over all ordered station pairs equals the nominal PPLA value
    ``mean_s`` = 15 s:

        t_dwell = mean_s - d_mean / v_gantry .

    Because the traverse is a small fraction of the 15 s cycle, the
    distance-dependent spread is naturally small (a few percent), in
    contrast with a constant matrix; this is consistent with the gantry
    kinematics of D2.1 (1.5 m/s, a<=3 m/s^2) and supersedes the earlier
    affine +/-20 % model.
    """
    vids = list(positions.keys())
    dist = {a: {b: math.dist(positions[a], positions[b]) for b in vids}
            for a in vids}
    offdiag = [dist[a][b] for a in vids for b in vids if a != b]
    d_mean = sum(offdiag) / len(offdiag)
    t_dwell = mean_s - d_mean / v_gantry
    transport_s = {}
    for a in vids:
        transport_s[a] = {}
        for b in vids:
            transport_s[a][b] = (0.0 if a == b
                                 else round(t_dwell + dist[a][b] / v_gantry, 2))
    t_off = [transport_s[a][b] for a in vids for b in vids if a != b]
    stats = {
        "model": "dwell_plus_traverse",
        "v_gantry_ms": v_gantry,
        "t_dwell_s": round(t_dwell, 2),
        "mean_distance_m": round(d_mean, 4),
        "t_mean_s": round(sum(t_off) / len(t_off), 2),
        "t_min_s": round(min(t_off), 2),
        "t_max_s": round(max(t_off), 2),
    }
    return transport_s, stats


def generate_ppla_config(scale=0.1, seed=42, interval=375.0,
                         w_max=300.0, qc_stagger=5.0, pipc_s=600.0):
    """Build config.json for a PPLA instance.

    scale    : fraction of the daily PPLA workload
               (1.0 -> 100 HC + 100 SVOC + 10 QC1 + 10 QC2 + 10 Blank
                = 230 samples / 24 h, report 20240710PPLA.1 Sec. 8.1)
    interval : analytical inter-arrival time in seconds
               (375 s ~ 86400/230 reproduces the nominal daily flow)
    pipc_s   : Pipet C (Method-2 extraction) processing time; the PPLA
               report (Sec. 8.4.5) flags the nominal 600 s as a likely
               over-estimate and evaluates 480 s (8 min) as well
    """
    rng = random.Random(seed)

    # Routes / station catalogue with the requested Pipet C duration
    station_types = {k: dict(v) for k, v in PPLA_STATION_TYPES.items()}
    station_types[10]["p_k_s"] = pipc_s
    routes = {}
    for name, r in PPLA_ROUTES.items():
        rr = {"method": r["method"],
              "type_sequence": list(r["type_sequence"]),
              "p_s_sequence": [
                  (pipc_s if k == 10 else p)
                  for k, p in zip(r["type_sequence"], r["p_s_sequence"])]}
        routes[name] = rr

    n_hc = max(1, round(100 * scale))
    n_svoc = max(1, round(100 * scale))
    n_qc1 = max(1, round(10 * scale))
    n_qc2 = max(1, round(10 * scale))
    n_blank = max(1, round(10 * scale))

    # emulsion draw on Method-2 samples
    n_emul = round(EMULSION_FRACTION * n_svoc)
    emul_idx = set(rng.sample(range(n_svoc), n_emul))

    positions, stations = _ppla_layout(rng)

    # distance-dependent transport time (affine model, mean = ROBOT_MOVE_S,
    # max deviation +/- MOVE_MAX_VAR); see build_distance_transport().
    transport_s, transport_stats = build_distance_transport(positions)

    # ---- demand: HC/SVOC alternate; QC/Blank interleaved per block ----
    samples, s_idx = [], 1
    analytical = []
    for i in range(max(n_hc, n_svoc)):
        if i < n_hc:
            analytical.append("HC")
        if i < n_svoc:
            analytical.append("SVOC_E" if i in emul_idx else "SVOC")

    qc_every = max(1, len(analytical) // (n_qc1 + n_qc2 + n_blank))
    qc_cycle = (["QC1"] * n_qc1 + ["QC2"] * n_qc2 + ["Blank"] * n_blank)
    rng.shuffle(qc_cycle)
    qc_iter = iter(qc_cycle)

    t = 0.0
    for i, stype in enumerate(analytical):
        samples.append({"id": f"s{s_idx}", "type": stype,
                        "release_s": round(t, 1)})
        s_idx += 1
        t += interval
        if (i + 1) % qc_every == 0:
            q = next(qc_iter, None)
            if q:
                samples.append({"id": f"s{s_idx}", "type": q,
                                "release_s": round(t - interval
                                                   + qc_stagger, 1)})
                s_idx += 1
    for q in qc_iter:                       # leftover QC at the end
        samples.append({"id": f"s{s_idx}", "type": q,
                        "release_s": round(t + qc_stagger, 1)})
        s_idx += 1
        t += qc_stagger

    samples.sort(key=lambda s: (s["release_s"], int(s["id"][1:])))

    config = {
        "_comment": (
            f"PPLA instance generated by simulator_v3.py | scale={scale}, "
            f"seed={seed}, interval={interval}s, pipc_s={pipc_s} | "
            f"data: 20240710PPLA.1 "
            f"Ch.8 | {datetime.now().isoformat(timespec='seconds')}"
        ),
        "_layout_positions": positions,
        "_transport_model": transport_stats,
        "instance_id": (f"ppla_s{int(scale*100):03d}_seed{seed}"
                        + (f"_pc{int(pipc_s)}" if pipc_s != 600 else "")),
        "robot_speed_ms": transport_stats["v_gantry_ms"],
        "pickup_time_s": transport_stats["t_dwell_s"],
        "station_types": {
            str(k): {"name": v["name"], "p_k_s": v["p_k_s"]}
            for k, v in station_types.items()
        },
        "stations": stations,
        "transport_s": transport_s,
        "routes": routes,
        "samples": samples,
        "qc_policy": {"block_size": 10, "qc1_per_block": 1,
                      "qc2_per_block": 1, "blank_per_block": 1},
        "W_max_s": w_max,
        "objective": "minimize_makespan",
        "opt_params": {},
    }
    with open(CONFIG_FILE, "w") as fh:
        json.dump(config, fh, indent=2)
    return config


# ---------------------------------------------------------------------------
#  Simulator subclass: per-route processing times
# ---------------------------------------------------------------------------
class RoboticCellSimulatorV3(v2.RoboticCellSimulator):
    """v2 engine + stage-dependent processing times (p_s_sequence)."""

    def _proc_time(self, sid, dest_k):
        """Processing time of the NEXT stage of sample sid (the stage it
        is being delivered to)."""
        sm = self.sam[sid]
        route = self.cfg["routes"][sm["type"]]
        p_seq = route.get("p_s_sequence")
        idx = sm["stage"]                   # 0-based index of new stage
        if p_seq and idx < len(p_seq):
            return float(p_seq[idx])
        return float(self.p_k[dest_k])

    # override: identical to v2 except for the pk lookup
    def _on_robot_done(self, data):
        task = data["task"]
        sid = task["sample_id"]
        dest = task["destination"]

        self.robot["location"] = dest
        self.robot["status"] = "idle"
        self.robot["carrying"] = None
        self.robot["busy_until"] = self.t

        if sid:
            sm = self.sam[sid]
            st = self.st_state[dest]
            k = self.stype[dest]
            pk = self._proc_time(sid, k)          # << v3 change

            wait_s = (self.t - self.sample_ops[sid][-1]["C_s"]
                      if self.sample_ops[sid] else 0.0)
            self.sample_ops[sid].append({
                "stage": sm["stage"] + 1,
                "type_k": k,
                "station": dest,
                "S_s": self.t,
                "C_s": self.t + pk if pk > 0 else self.t,
                "wait_s": round(max(0.0, wait_s), 1),
            })
            sm["completed_ops"].append({
                "j": sm["stage"] + 1, "station": dest,
                "S_s": self.t, "C_s": self.t + pk if pk > 0 else self.t,
            })
            sm["stage"] += 1
            sm["station"] = dest
            sm["w_s"] = 0.0

            st["sample_id"] = sid
            st["busy"] = (pk > 0)
            st["finish_t"] = self.t + pk
            st["r_s"] = pk

            self._log_ev(self.t, "ROBOT_DELIVERED",
                         {"task": task["task_id"], "sample": sid,
                          "dest": dest, "pk": pk})

            if pk > 0:
                self._push(self.t + pk, self.EV_PROC_END,
                           {"station": dest, "sample_id": sid})
                sm["status"] = "processing"
            else:
                if k == 5:
                    self._complete_sample(sid, dest)
                else:
                    sm["status"] = "waiting_robot"
                    sm["w_start"] = self.t

        self._log_ev(self.t, "ROBOT_IDLE",
                     {"location": self.robot["location"]})
        self._trigger_decision()


# ---------------------------------------------------------------------------
#  Instance-level aggregate lower bound (lower_bound.py)
# ---------------------------------------------------------------------------
def instance_lower_bound(config):
    """Valid LB on the makespan of the whole instance:
         max( machine-capacity bound, robot bound,
              release-based path bound )            (cf. D3.2.1, Sec. LB)
    """
    from lower_bound import AggregateLowerBound
    alb = AggregateLowerBound(config)
    X_sam = {s["id"]: {"type": s["type"], "stage_j": 1,
                       "status": "waiting_robot", "station": None}
             for s in config["samples"]}
    lb_res, diag = alb.lb_remaining(X_sam, {})
    # release-based bound: a sample cannot finish earlier than its
    # release + its own route (processing + transfers)
    lb_path = 0.0
    for s in config["samples"]:
        seq = config["routes"][s["type"]]["type_sequence"]
        proc = sum(alb.cap.stage_time(s["type"], i)
                   for i in range(len(seq)))
        moves = (len(seq) - 1) * 2.0 * alb.cap.move_s
        lb_path = max(lb_path, s["release_s"] + proc + moves)
    lb = max(lb_res, lb_path)
    diag["lb_path"] = round(lb_path, 1)
    diag["lb_makespan_s"] = round(lb, 1)
    return lb, diag


# ---------------------------------------------------------------------------
#  KPI extraction
# ---------------------------------------------------------------------------
def compute_kpis(sim, makespan):
    W = sim.W_max
    n_viol = sum(1 for ops in sim.sample_ops.values()
                 for op in ops if op.get("wait_s", 0) > W)
    loaded = sum(t["t_loaded_s"] for t in sim.robot_tasks)
    empty = sum(t["t_empty_s"] for t in sim.robot_tasks)
    idle = max(0.0, makespan - loaded - empty)
    flow = []
    for sid, ops in sim.sample_ops.items():
        if sim.sam.get(sid, {}).get("status") == "completed":
            flow.append(ops[-1]["C_s"] - ops[0]["S_s"])
    waits = [op.get("wait_s", 0) for ops in sim.sample_ops.values()
             for op in ops[1:]]
    solve_ms = [c["solve_ms"] for c in sim.greedy_calls]
    return {
        "instance_id": sim.cfg["instance_id"],
        "solver": sim.solver_script,
        "n_samples": len(sim.cfg["samples"]),
        "n_completed": sum(1 for s in sim.sam.values()
                           if s["status"] == "completed"),
        "makespan_s": round(makespan, 1),
        "n_violations": n_viol,
        "max_wait_s": round(max(waits), 1) if waits else 0.0,
        "mean_wait_s": round(sum(waits) / len(waits), 1) if waits else 0.0,
        "n_robot_tasks": len(sim.robot_tasks),
        "n_solver_calls": len(sim.greedy_calls),
        "mean_solve_ms": (round(sum(solve_ms) / len(solve_ms), 1)
                          if solve_ms else 0.0),
        "robot_loaded_s": round(loaded, 1),
        "robot_empty_s": round(empty, 1),
        "robot_idle_s": round(idle, 1),
        "robot_util_pct": round(100 * (loaded + empty) / makespan, 1)
        if makespan else 0.0,
        "mean_flow_s": (round(sum(flow) / len(flow), 1) if flow else 0.0),
        "max_flow_s": round(max(flow), 1) if flow else 0.0,
    }


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Robotic Cell Simulator v3 - PPLA instances + POLICY3")
    ap.add_argument("--instance", choices=["ppla", "synthetic"],
                    default="ppla")
    ap.add_argument("--scale", type=float, default=0.1,
                    help="fraction of the PPLA daily workload (1.0=230)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--interval", type=float, default=375.0,
                    help="analytical inter-arrival time (s)")
    ap.add_argument("--samples", type=int, default=8,
                    help="(synthetic only) number of HC samples")
    ap.add_argument("--speed", type=float, default=v2.ROBOT_SPEED)
    ap.add_argument("--w-max", type=float, default=300.0)
    ap.add_argument("--pipc-s", type=float, default=600.0,
                    help="Pipet C processing time (PPLA 8.4.5: 600 or 480)")
    ap.add_argument("--solver", type=str, default="policy3_solver.py")
    ap.add_argument("--all-actions", action="store_true")
    ap.add_argument("--no-xlsx", action="store_true")
    args = ap.parse_args()

    print("=" * 60)
    print("  ROBOTIC CELL SIMULATOR v3  (POLICY3)")
    print("=" * 60)

    if args.instance == "ppla":
        config = generate_ppla_config(scale=args.scale, seed=args.seed,
                                      interval=args.interval,
                                      w_max=args.w_max,
                                      pipc_s=args.pipc_s)
    else:
        config = v2.generate_config(seed=args.seed, n_hc=args.samples,
                                    sample_interval=args.interval,
                                    robot_speed=args.speed)

    print(f"  instance={config['instance_id']}  "
          f"stations={len(config['stations'])}  "
          f"samples={len(config['samples'])}")
    print(f"  solver={args.solver}  all_actions={args.all_actions}")

    sim = RoboticCellSimulatorV3(config, solver_script=args.solver,
                                 all_actions=args.all_actions)
    makespan = sim.run()

    kpi = compute_kpis(sim, makespan)
    with open(KPI_FILE, "w") as fh:
        json.dump(kpi, fh, indent=2)

    # aggregate lower bound and optimality gap
    try:
        lb, lb_diag = instance_lower_bound(config)
        lb_diag["makespan_s"] = round(makespan, 1)
        lb_diag["gap_pct"] = (round(100 * (makespan - lb) / lb, 1)
                              if lb > 0 else None)
        with open("lb.json", "w") as fh:
            json.dump(lb_diag, fh, indent=2)
        print(f"  LB(makespan) = {lb:.0f} s  "
              f"(gap {lb_diag['gap_pct']}%)")
    except Exception as exc:
        print(f"  LB computation skipped: {exc}")

    print()
    print("-" * 60)
    for key in ["makespan_s", "n_completed", "n_violations",
                "robot_util_pct", "n_robot_tasks", "n_solver_calls"]:
        print(f"  {key:18s}: {kpi[key]}")
    print(f"  kpi.json written")

    if not args.no_xlsx and v2.EXCEL_OK:
        print("  Generating Excel report...")
        v2.create_excel_report(sim, makespan)


if __name__ == "__main__":
    main()
