#!/usr/bin/env python3
"""
simulator_v2.py – Robotic Cell Discrete-Event Simulator (v2)
=============================================================
Extends simulator.py with:
  • --solver SCRIPT   select which solver to invoke (default: greedy_solver.py)
  • --all-actions      expose ALL idle destinations per sample (required by
                       MECT-based solvers such as cpr_la_solver.py)
  • Enhanced action matching: if the solver selects a destination that was
    not pre-computed (MECT override), the simulator constructs the action
    on-the-fly from the transport matrix.

Usage:
    python simulator_v2.py [--seed N] [--samples N] [--interval S] [--speed V]
                           [--solver cpr_la_solver.py] [--all-actions]

Interface contract (same as simulator.py):
    Writes  → config.json, input.json
    Reads   ← output.json, opt_meta.json
"""

import argparse
import heapq
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.chart import BarChart, Reference
    from openpyxl.utils import get_column_letter
    EXCEL_OK = True
except ImportError:
    EXCEL_OK = False

# ─── File names ──────────────────────────────────────────────────────────────
CONFIG_FILE   = "config.json"
INPUT_FILE    = "input.json"
OUTPUT_FILE   = "output.json"
OPT_META_FILE = "opt_meta.json"
LOG_FILE      = "simulation.log"
EXCEL_FILE    = "simulation_report.xlsx"

# ─── Station-type catalog (mirrors the formal model K = {1,…,10}) ───────────
STATION_TYPES = {
    1:  {"name": "HC/SVOC Entry", "p_k_s": 0,   "io": True},
    2:  {"name": "QC1 Entry",     "p_k_s": 0,   "io": True},
    3:  {"name": "QC2 Entry",     "p_k_s": 0,   "io": True},
    4:  {"name": "Blank Entry",   "p_k_s": 0,   "io": True},
    5:  {"name": "Sample Exit",   "p_k_s": 0,   "io": True},
    6:  {"name": "Pipetting",     "p_k_s": 60,  "io": False},
    7:  {"name": "Evaporator",    "p_k_s": 90,  "io": False},
    8:  {"name": "Centrifuge",    "p_k_s": 120, "io": False},
    9:  {"name": "Decapper",      "p_k_s": 30,  "io": False},
    10: {"name": "Auxiliary",     "p_k_s": 20,  "io": False},
}

# Sample route type-sequences (ordered station-type indices)
ROUTES = {
    "HC":    {"type_sequence": [1, 9, 6, 8, 7, 5]},
    "QC1":   {"type_sequence": [2, 6, 5]},
    "QC2":   {"type_sequence": [3, 6, 5]},
    "Blank": {"type_sequence": [4, 6, 5]},
}

ROBOT_SPEED  = 0.5   # m/s (default gantry speed)
PICKUP_TIME  = 5.0   # s   (pick + place per operation)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def _euclidean(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def _transport_time(p1, p2, speed, pickup):
    """Round-trip travel time including pick-up and place operations."""
    return max(1, round(_euclidean(p1, p2) / speed + 2.0 * pickup))

def generate_config(seed=42, n_hc=8, sample_interval=15,
                    robot_speed=ROBOT_SPEED):
    """
    Generate a pseudo-random config.json.

    Physical layout (metres, x=0 far-left … x=3.0 far-right):
      • I/O stations are fixed on the right column (x ≈ 2.9).
      • Processing modules are distributed left → right, matching the
        annotated image (Aux → Evap → Centr → Pip → Decap).
    Transport time = dist / speed + 2 * PICKUP_TIME, rounded to seconds.
    """
    rng = random.Random(seed)

    # Station counts per type
    counts = {1:1, 2:1, 3:1, 4:1, 5:1, 6:4, 7:2, 8:2, 9:2, 10:3}

    # Bounding boxes (x_lo,x_hi) × (y_lo,y_hi) per type
    zones = {
        10: ((0.05, 0.30), (0.10, 0.55)),   # Auxiliary  – far left
        7:  ((0.35, 0.65), (0.10, 0.55)),   # Evaporator
        8:  ((0.70, 0.95), (0.10, 0.55)),   # Centrifuge
        6:  ((1.00, 1.80), (0.05, 0.58)),   # Pipetting (4 units, spread)
        9:  ((1.85, 2.20), (0.12, 0.50)),   # Decapper
        1:  ((2.85, 2.95), (0.05, 0.15)),   # HC/SVOC Entry
        2:  ((2.85, 2.95), (0.18, 0.28)),   # QC1 Entry
        3:  ((2.85, 2.95), (0.30, 0.38)),   # QC2 Entry
        4:  ((2.85, 2.95), (0.40, 0.48)),   # Blank Entry
        5:  ((2.85, 2.95), (0.52, 0.60)),   # Sample Exit
    }

    positions = {}   # station_id → (x, y)
    stations  = {}   # station_id → {type_k, name}
    v_idx = 1

    for k_type in [10, 7, 8, 6, 9, 1, 2, 3, 4, 5]:
        xr, yr = zones[k_type]
        for inst in range(counts[k_type]):
            vid = f"v{v_idx}"
            x   = rng.uniform(*xr)
            y   = rng.uniform(*yr)
            positions[vid] = (round(x, 4), round(y, 4))
            stations[vid]  = {
                "type_k": k_type,
                "name": f"{STATION_TYPES[k_type]['name']} #{inst+1}"
            }
            v_idx += 1

    # Transport time matrix (seconds)
    vids = list(stations.keys())
    transport_s = {a: {b: (_transport_time(positions[a], positions[b],
                                           robot_speed, PICKUP_TIME)
                           if a != b else 0)
                       for b in vids}
                   for a in vids}

    # ── Sample demand ────────────────────────────────────────────────────────
    # HC samples at fixed intervals; QC sets after every QC_BLOCK HC samples
    QC_BLOCK = 10
    samples  = []
    s_idx    = 1

    for i in range(n_hc):
        samples.append({
            "id": f"s{s_idx}",
            "type": "HC",
            "release_s": i * sample_interval
        })
        s_idx += 1

    n_qc_sets = max(1, math.ceil(n_hc / QC_BLOCK))
    for blk in range(n_qc_sets):
        t_blk = (blk + 1) * QC_BLOCK * sample_interval
        for j, qtype in enumerate(["QC1", "QC2", "Blank"]):
            samples.append({
                "id": f"s{s_idx}",
                "type": qtype,
                "release_s": t_blk + j * 5   # small stagger
            })
            s_idx += 1

    samples.sort(key=lambda s: (s["release_s"], s["id"]))

    config = {
        "_comment": (
            f"Auto-generated by simulator.py | "
            f"seed={seed}, n_hc={n_hc}, interval={sample_interval}s, "
            f"speed={robot_speed}m/s | {datetime.now().isoformat(timespec='seconds')}"
        ),
        "_layout_positions": positions,
        "instance_id": f"sim_seed{seed}_nhc{n_hc}",
        "robot_speed_ms": robot_speed,
        "pickup_time_s":  PICKUP_TIME,
        "station_types": {
            str(k): {"name": v["name"], "p_k_s": v["p_k_s"]}
            for k, v in STATION_TYPES.items()
        },
        "stations":    stations,
        "transport_s": transport_s,
        "routes":      ROUTES,
        "samples":     samples,
        "qc_policy": {
            "block_size":     QC_BLOCK,
            "qc1_per_block":  1,
            "qc2_per_block":  1,
            "blank_per_block": 1,
        },
        "W_max_s":   300,
        "objective": "minimize_makespan",
    }

    with open(CONFIG_FILE, "w") as fh:
        json.dump(config, fh, indent=2)

    return config


# ═══════════════════════════════════════════════════════════════════════════════
#  EVENT-DRIVEN SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════════

class RoboticCellSimulator:
    """
    Discrete-event simulation of the robotic analytical cell.

    Decision model
    ──────────────
    The robot is a single shared resource.  At each decision epoch τ_ℓ
    (= moment the robot becomes idle):
      • If 0 samples need transport   → robot waits (no event pushed).
      • If exactly 1 sample can be served → trivial choice, no solver needed.
      • If ≥ 2 samples compete        → write input.json, call greedy_solver.py,
                                         read output.json.
    After selecting an action, the robot is scheduled to arrive at the
    delivery station at t + duration_s (combined empty + loaded leg).
    """

    # ── Event type tokens ────────────────────────────────────────────────────
    EV_RELEASE  = "SAMPLE_RELEASE"
    EV_PROC_END = "STATION_COMPLETE"
    EV_ROB_DONE = "ROBOT_TASK_DONE"

    def __init__(self, config: dict, solver_script: str = "greedy_solver.py",
                 all_actions: bool = False):
        self.cfg   = config
        self.W_max = config["W_max_s"]

        # Build lookup tables
        self.stype  = {v: info["type_k"]
                       for v, info in config["stations"].items()}
        self.type2v = defaultdict(list)
        for v, info in config["stations"].items():
            self.type2v[info["type_k"]].append(v)
        self.p_k  = {int(k): v["p_k_s"]
                     for k, v in config["station_types"].items()}
        self.mij  = config["transport_s"]

        # ── Simulation state ─────────────────────────────────────────────────
        self.t = 0.0
        self._seq = 0        # tie-breaking sequence counter

        # Robot state
        init_robot_pos = self.type2v[1][0]  # start at HC/SVOC entry
        self.robot = {
            "location":   init_robot_pos,
            "status":     "idle",
            "carrying":   None,
            "busy_until": 0.0,
        }

        # Station state: busy / sample_id / finish_t / remaining_s
        self.st_state = {
            v: {"busy": False, "sample_id": None,
                "finish_t": 0.0, "r_s": 0.0}
            for v in config["stations"]
        }

        # Sample state
        self.sam = {}

        # QC counters
        self.qc = {"N_HC": 0, "N_QC1": 0, "N_QC2": 0, "N_Blank": 0}

        # Event heap
        self._eq = []

        # ── Logs (filled during run) ─────────────────────────────────────────
        self.event_log    = []          # all simulation events
        self.robot_tasks  = []          # completed robot task records
        self.sample_ops   = defaultdict(list)  # sample_id → list of ops
        self.greedy_calls = []          # greedy solver invocation records

        self._task_ctr   = 0
        self._epoch_ctr  = 0

        # ── Solver configuration ──────────────────────────────────────────────
        self.solver_script = solver_script
        self.all_actions   = all_actions

        # ── Logging ──────────────────────────────────────────────────────────
        logging.basicConfig(
            filename=LOG_FILE, filemode="w", level=logging.DEBUG,
            format="%(asctime)s %(levelname)-8s %(message)s",
        )
        self._log = logging.getLogger("simulator")
        _ch = logging.StreamHandler(sys.stdout)
        _ch.setLevel(logging.INFO)
        _ch.setFormatter(logging.Formatter("[SIM] %(message)s"))
        self._log.addHandler(_ch)

        # Seed release events
        for s in config["samples"]:
            self._push(s["release_s"], self.EV_RELEASE, {"sample": s})

    # ─── Heap helpers ────────────────────────────────────────────────────────

    def _push(self, t, etype, data):
        self._seq += 1
        heapq.heappush(self._eq, (t, self._seq, etype, data))

    def _log_ev(self, t, etype, details):
        self.event_log.append({"t": round(t, 2), "type": etype,
                                "details": details})
        msg = f"t={t:8.1f}s | {etype:<28s} | {details}"
        self._log.debug(msg)

    # ─── Main loop ───────────────────────────────────────────────────────────

    def run(self) -> float:
        self._log.info(
            f"Simulation start | {len(self.cfg['samples'])} samples | "
            f"instance={self.cfg['instance_id']}"
        )
        self._log_ev(0, "SIM_START",
                     {"n_samples": len(self.cfg["samples"]),
                      "instance_id": self.cfg["instance_id"]})

        while self._eq:
            t, _seq, etype, data = heapq.heappop(self._eq)
            self.t = t

            if   etype == self.EV_RELEASE:  self._on_release(data)
            elif etype == self.EV_PROC_END: self._on_station_complete(data)
            elif etype == self.EV_ROB_DONE: self._on_robot_done(data)

        makespan = self.t
        self._log.info(f"Simulation finished | Makespan = {makespan:.0f} s")
        self._log_ev(makespan, "SIM_END",
                     {"makespan_s": makespan, "qc": self.qc})
        return makespan

    # ─── Event handlers ──────────────────────────────────────────────────────

    def _on_release(self, data):
        s    = data["sample"]
        sid  = s["id"]
        styp = s["type"]
        route = self.cfg["routes"][styp]["type_sequence"]

        # Stage 1 = entry station (p_k = 0 → instant)
        entry_k = route[0]
        entry_v = self.type2v[entry_k][0]

        self.sam[sid] = {
            "type":       styp,
            "route":      route,
            "stage":      1,              # stage 1 complete (p=0)
            "station":    entry_v,
            "status":     "waiting_robot",
            "w_s":        0.0,
            "w_start":    self.t,
            "completed_ops": [
                {"j": 1, "station": entry_v,
                 "S_s": self.t, "C_s": self.t}
            ],
        }
        self.st_state[entry_v]["sample_id"] = sid

        self.sample_ops[sid].append({
            "stage": 1, "type_k": route[0], "station": entry_v,
            "S_s": self.t, "C_s": self.t, "wait_s": 0.0,
        })

        self._log_ev(self.t, "SAMPLE_RELEASE",
                     {"id": sid, "type": styp, "entry": entry_v})

        if self.robot["status"] == "idle":
            self._trigger_decision()

    def _on_station_complete(self, data):
        vid = data["station"]
        sid = data["sample_id"]
        st  = self.st_state[vid]
        sm  = self.sam[sid]

        st["busy"]   = False
        st["r_s"]    = 0.0
        sm["status"] = "waiting_robot"
        sm["w_start"] = self.t

        self._log_ev(self.t, "STATION_COMPLETE",
                     {"station": vid, "sample": sid, "stage": sm["stage"]})

        if self.robot["status"] == "idle":
            self._trigger_decision()

    def _on_robot_done(self, data):
        task = data["task"]
        sid  = task["sample_id"]
        dest = task["destination"]
        org  = task["pickup"]       # station where sample was picked up

        # Update robot
        self.robot["location"]   = dest
        self.robot["status"]     = "idle"
        self.robot["carrying"]   = None
        self.robot["busy_until"] = self.t

        if sid:
            sm = self.sam[sid]
            st = self.st_state[dest]
            k  = self.stype[dest]
            pk = self.p_k[k]

            # Record operation
            wait_s = (self.t - self.sample_ops[sid][-1]["C_s"]
                      if self.sample_ops[sid] else 0.0)
            self.sample_ops[sid].append({
                "stage":  sm["stage"] + 1,
                "type_k": k,
                "station": dest,
                "S_s":    self.t,
                "C_s":    self.t + pk if pk > 0 else self.t,
                "wait_s": round(max(0.0, wait_s), 1),
            })
            sm["completed_ops"].append({
                "j": sm["stage"] + 1, "station": dest,
                "S_s": self.t, "C_s": self.t + pk if pk > 0 else self.t,
            })
            sm["stage"]   += 1
            sm["station"]  = dest
            sm["w_s"]      = 0.0

            # Occupy destination station
            st["sample_id"] = sid
            st["busy"]      = (pk > 0)
            st["finish_t"]  = self.t + pk
            st["r_s"]       = pk

            self._log_ev(self.t, "ROBOT_DELIVERED",
                         {"task": task["task_id"], "sample": sid,
                          "dest": dest, "pk": pk})

            if pk > 0:
                self._push(self.t + pk, self.EV_PROC_END,
                           {"station": dest, "sample_id": sid})
                sm["status"] = "processing"
            else:
                # p=0 station (exit or entry mid-route)
                if k == 5:   # Sample Exit
                    self._complete_sample(sid, dest)
                else:
                    sm["status"] = "waiting_robot"
                    sm["w_start"] = self.t

        self._log_ev(self.t, "ROBOT_IDLE",
                     {"location": self.robot["location"]})
        self._trigger_decision()

    # ─── Sample completion ────────────────────────────────────────────────────

    def _complete_sample(self, sid, exit_v):
        sm = self.sam[sid]
        sm["status"] = "completed"
        self.st_state[exit_v]["sample_id"] = None

        qc_key = {"HC": "N_HC", "QC1": "N_QC1",
                  "QC2": "N_QC2", "Blank": "N_Blank"}.get(sm["type"])
        if qc_key:
            self.qc[qc_key] += 1

        self._log_ev(self.t, "SAMPLE_COMPLETED",
                     {"id": sid, "type": sm["type"],
                      "n_stages": sm["stage"], "qc": dict(self.qc)})

    # ─── Decision logic ──────────────────────────────────────────────────────

    def _waiting_robot(self):
        return [sid for sid, sm in self.sam.items()
                if sm["status"] == "waiting_robot"]

    def _generate_feasible_actions(self):
        """
        For every waiting_robot sample generate transport action(s).

        Default mode (all_actions=False): one action per sample, targeting
        the nearest idle station of the required type (greedy-compatible).

        Extended mode (all_actions=True): one action per (sample, idle-dest)
        pair, so MECT-based solvers can choose the optimal destination.
        """
        actions = []
        rpos    = self.robot["location"]

        for sid in self._waiting_robot():
            sm     = self.sam[sid]
            route  = sm["route"]
            stage  = sm["stage"]
            pickup = sm["station"]

            if stage >= len(route):
                continue

            next_k = route[stage]

            # Collect all idle destinations of the required type
            candidates = []
            for v in self.type2v[next_k]:
                if (not self.st_state[v]["busy"]
                        and self.st_state[v]["sample_id"] is None):
                    t_empty  = self.mij[rpos][pickup]
                    t_loaded = self.mij[pickup][v]
                    candidates.append((t_empty + t_loaded, v, t_empty, t_loaded))

            if not candidates:
                continue

            candidates.sort()   # nearest first

            if self.all_actions:
                # Expose all idle destinations
                emit_list = candidates
            else:
                # Nearest only (original behaviour)
                emit_list = [candidates[0]]

            w_s = (self.t - sm["w_start"]) if "w_start" in sm else 0.0

            for total, best_v, t_empty, t_loaded in emit_list:
                actions.append({
                    "task_id":          f"l{self._task_ctr+1}_{sid}_{best_v}",
                    "type":             "loaded",
                    "sample_id":        sid,
                    "origin":           rpos,
                    "pickup":           pickup,
                    "destination":      best_v,
                    "earliest_start_s": self.t,
                    "duration_s":       total,
                    "t_empty_s":        t_empty,
                    "t_loaded_s":       t_loaded,
                    "_w_s":             round(w_s, 1),
                    "_remaining_w":     round(self.W_max - w_s, 1),
                })

        return actions

    def _trigger_decision(self):
        """Called whenever the robot becomes idle. Select and execute action."""
        # Update w_s for all waiting samples before deciding
        for sm in self.sam.values():
            if sm["status"] == "waiting_robot" and "w_start" in sm:
                sm["w_s"] = round(self.t - sm["w_start"], 1)

        actions = self._generate_feasible_actions()

        if not actions:
            pending = [s for s in self.sam.values()
                       if s["status"] not in ("completed",)]
            if not pending:
                self._log_ev(self.t, "ALL_SAMPLES_DONE", {})
            else:
                self._log_ev(self.t, "ROBOT_WAITING",
                             {"n_pending": len(pending)})
            return

        if len(actions) == 1:
            chosen = actions[0]
            self._log_ev(self.t, "DECISION_TRIVIAL",
                         {"sample": chosen["sample_id"],
                          "dest": chosen["destination"]})
        else:
            self._epoch_ctr += 1
            epoch_id = f"tau_{self._epoch_ctr}"
            self._write_input_json(epoch_id, actions)
            chosen = self._call_greedy(epoch_id, actions)

        self._execute_action(chosen)

    def _write_input_json(self, epoch_id, actions):
        """Serialize current cell state to input.json for the greedy solver."""
        X_st = {}
        for v, st in self.st_state.items():
            X_st[v] = {
                "b_i":       1 if st["busy"] else 0,
                "r_i_s":     max(0.0, round(st["finish_t"] - self.t, 1))
                             if st["busy"] else 0.0,
                "sample_id": st["sample_id"],
            }

        X_sam = {}
        for sid, sm in self.sam.items():
            if sm["status"] == "completed":
                continue
            X_sam[sid] = {
                "stage_j":      sm["stage"],
                "station":      sm["station"],
                "status":       sm["status"],
                "type":         sm["type"],
                "w_s":          sm.get("w_s", 0.0),
                "completed_ops": sm.get("completed_ops", []),
                "pending_ops":  [
                    {"j": sm["stage"] + 1 + i, "type_k": t}
                    for i, t in enumerate(sm["route"][sm["stage"]:])
                ],
            }

        # Strip internal (_-prefixed) keys from actions before writing
        clean_actions = [
            {k: v for k, v in a.items() if not k.startswith("_")}
            for a in actions
        ]

        payload = {
            "_comment":        f"Decision epoch {epoch_id} at t={self.t:.1f}s",
            "instance_id":     self.cfg["instance_id"],
            "epoch":           epoch_id,
            "t_s":             self.t,
            "X_rob": {
                "location": self.robot["location"],
                "status":   self.robot["status"],
            },
            "X_st":            X_st,
            "X_sam":           X_sam,
            "X_qc":            self.qc,
            "completed_tasks": [t["task_id"] for t in self.robot_tasks],
            "feasible_actions": clean_actions,
        }

        with open(INPUT_FILE, "w") as fh:
            json.dump(payload, fh, indent=2)

        self._log_ev(self.t, "INPUT_JSON_WRITTEN",
                     {"epoch": epoch_id, "n_actions": len(actions)})

    def _call_greedy(self, epoch_id, actions):
        """
        Invoke the configured solver as a subprocess.
        Supports MECT destination overrides: if the solver requests a
        destination not originally in feasible_actions but reachable from
        the same sample's pickup station, the action is constructed on-the-fly.
        """
        t0 = time.perf_counter()
        rpos = self.robot["location"]
        try:
            result = subprocess.run(
                [sys.executable, self.solver_script],
                capture_output=True, text=True, timeout=30,
            )
            solve_time = time.perf_counter() - t0

            if result.returncode != 0:
                self._log_ev(self.t, "SOLVER_ERROR",
                             {"solver": self.solver_script,
                              "stderr": result.stderr[:300]})
                raise RuntimeError(f"{self.solver_script} returned non-zero")

            with open(OUTPUT_FILE) as fh:
                out = json.load(fh)

            sel  = out.get("selected_action")
            sid  = sel.get("sample_id") if sel else None
            dest = sel.get("destination") if sel else None

            if sel and sel.get("type") == "empty":
                # Pre-positioning action from CPR-LA Module 4
                chosen = {
                    "task_id":    sel.get("task_id", f"prepos_{epoch_id}"),
                    "type":       "empty",
                    "sample_id":  None,
                    "origin":     rpos,
                    "pickup":     rpos,
                    "destination": dest,
                    "duration_s":  sel.get("duration_s",
                                          self.mij.get(rpos, {}).get(dest, 10)),
                    "t_empty_s":  sel.get("duration_s",
                                          self.mij.get(rpos, {}).get(dest, 10)),
                    "t_loaded_s": 0,
                    "_w_s": 0.0, "_remaining_w": self.W_max,
                }
                # Greedy calls log
                self.greedy_calls.append({
                    "epoch":     epoch_id,
                    "t_s":       self.t,
                    "solve_ms":  round(solve_time * 1000, 2),
                    "n_actions": len(actions),
                    "selected":  chosen["task_id"],
                    "sample":    None,
                })
                return chosen

            # Try exact match (sample_id + destination)
            chosen = next(
                (a for a in actions
                 if a.get("sample_id") == sid and a.get("destination") == dest),
                None,
            )

            # MECT override: solver chose a destination not in feasible_actions
            if chosen is None and sid and dest:
                base = next(
                    (a for a in actions if a.get("sample_id") == sid),
                    None,
                )
                if base:
                    pickup   = base["pickup"]
                    t_empty  = self.mij[rpos][pickup]
                    t_loaded = self.mij[pickup][dest]
                    chosen = {
                        **base,
                        "destination":  dest,
                        "duration_s":   t_empty + t_loaded,
                        "t_empty_s":    t_empty,
                        "t_loaded_s":   t_loaded,
                    }
                    self._log_ev(self.t, "MECT_OVERRIDE",
                                 {"sample": sid, "dest": dest,
                                  "dur": t_empty + t_loaded})

            if chosen is None:
                chosen = actions[0]  # final fallback

            self.greedy_calls.append({
                "epoch":     epoch_id,
                "t_s":       self.t,
                "solve_ms":  round(solve_time * 1000, 2),
                "n_actions": len(actions),
                "selected":  sel.get("task_id") if sel else None,
                "sample":    sid,
            })
            self._log_ev(self.t, "SOLVER_RESULT",
                         {"solver": self.solver_script,
                          "epoch":  epoch_id,
                          "sample": sid,
                          "dest":   dest,
                          "solve_ms": round(solve_time * 1000, 2)})
            return chosen

        except Exception as exc:
            self._log_ev(self.t, "SOLVER_EXCEPTION", {"error": str(exc)})
            return min(actions, key=lambda a: a["_remaining_w"])

    def _execute_action(self, action):
        """Schedule a combined empty+loaded robot transport task."""
        self._task_ctr += 1
        tid    = f"l{self._task_ctr}"
        sid    = action["sample_id"]
        pickup = action.get("pickup", action.get("origin"))
        dest   = action["destination"]
        dur    = action["duration_s"]

        self.robot["status"]    = "busy"
        self.robot["carrying"]  = sid
        self.robot["busy_until"] = self.t + dur

        # Free pickup station immediately (sample leaves the station)
        self.st_state[pickup]["sample_id"] = None
        self.st_state[pickup]["busy"]       = False

        # Mark sample as in-transit
        if sid and sid in self.sam:
            sm = self.sam[sid]
            sm["status"] = "in_transit"
            sm["w_s"]    = round(self.t - sm.get("w_start", self.t), 1)

        task_rec = {
            "task_id":    tid,
            "sample_id":  sid,
            "origin":     self.robot["location"],
            "pickup":     pickup,
            "destination": dest,
            "start_s":    self.t,
            "t_empty_s":  action.get("t_empty_s", self.mij[self.robot["location"]][pickup]),
            "t_loaded_s": action.get("t_loaded_s", self.mij[pickup][dest]),
            "duration_s": dur,
            "end_s":      self.t + dur,
        }
        self.robot_tasks.append(task_rec)

        self._log_ev(self.t, "ROBOT_TASK_START",
                     {"task": tid, "sample": sid,
                      "pickup": pickup, "dest": dest,
                      "dur_s": dur, "end_s": self.t + dur})

        self._push(
            self.t + dur,
            self.EV_ROB_DONE,
            {"task": {"task_id": tid, "sample_id": sid,
                      "pickup": pickup, "destination": dest}},
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  EXCEL REPORT
# ═══════════════════════════════════════════════════════════════════════════════

# Palette
_C = {
    "hdr":      "1F3864",
    "sub":      "2E75B6",
    "green":    "375623",
    "bg_green": "E2EFDA",
    "bg_blue":  "BDD7EE",
    "bg_amber": "FFF2CC",
    "bg_red":   "FCE4D6",
    "bg_grey":  "F2F2F2",
    "amber":    "ED7D31",
    "white":    "FFFFFF",
    "dark":     "404040",
}

FONT = "Arial"

def _thin():
    s = Side(border_style="thin", color="AAAAAA")
    return Border(left=s, right=s, top=s, bottom=s)

def _hdr_cell(ws, r, c, val, bg="1F3864", fg="FFFFFF", sz=10, bold=True):
    cell = ws.cell(r, c, val)
    cell.font      = Font(name=FONT, bold=bold, color=fg, size=sz)
    cell.fill      = PatternFill("solid", fgColor=bg)
    cell.alignment = Alignment(horizontal="center", vertical="center",
                                wrap_text=True)
    cell.border    = _thin()
    return cell

def _data_cell(ws, r, c, val, bg=None, bold=False,
               align="left", fg="000000", sz=10):
    cell = ws.cell(r, c, val)
    cell.font      = Font(name=FONT, bold=bold, color=fg, size=sz)
    cell.alignment = Alignment(horizontal=align, vertical="center")
    cell.border    = _thin()
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)
    return cell

def _col_w(ws, widths: list):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

# ── Sample colour assignment (consistent across sheets) ──────────────────────
_SAMPLE_PALETTE = [
    _C["bg_blue"], _C["bg_green"], _C["bg_amber"], _C["bg_red"],
    "DAEEF3", "E8E0F0", "FDE9D9", "D9E1F2", "EAF1DD", "FFF2CC",
]


def _sample_colors(sample_ids):
    return {sid: _SAMPLE_PALETTE[i % len(_SAMPLE_PALETTE)]
            for i, sid in enumerate(sorted(sample_ids))}


# ──────────────────────────────────────────────────────────────────────────────
def _build_dashboard(wb, sim, makespan):
    ws = wb.create_sheet("Dashboard", 0)
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = "1F3864"

    # Title
    ws.row_dimensions[1].height = 34
    ws.merge_cells("A1:J1")
    c = ws.cell(1, 1,
                "ROBOTIC CELL SIMULATION  —  DASHBOARD")
    c.font      = Font(name=FONT, bold=True, size=16, color="FFFFFF")
    c.fill      = PatternFill("solid", fgColor=_C["hdr"])
    c.alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells("A2:J2")
    sub = ws.cell(2, 1,
                  f"Instance: {sim.cfg['instance_id']}   |   "
                  f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    sub.font      = Font(name=FONT, italic=True, size=10, color="FFFFFF")
    sub.fill      = PatternFill("solid", fgColor=_C["sub"])
    sub.alignment = Alignment(horizontal="center", vertical="center")

    # ── KPI row ──────────────────────────────────────────────────────────────
    ws.row_dimensions[4].height = 20
    ws.row_dimensions[5].height = 30
    kpis = [
        ("Makespan (s)",      f"{makespan:.0f}",       _C["hdr"]),
        ("HC Processed",      str(sim.qc["N_HC"]),      _C["sub"]),
        ("QC1 Processed",     str(sim.qc["N_QC1"]),     "375623"),
        ("QC2 Processed",     str(sim.qc["N_QC2"]),     "375623"),
        ("Blank Processed",   str(sim.qc["N_Blank"]),   "375623"),
        ("Robot Tasks",       str(len(sim.robot_tasks)),_C["amber"]),
        ("Greedy Calls",      str(len(sim.greedy_calls)),_C["amber"]),
        ("Total Samples",     str(len(sim.cfg["samples"])), _C["dark"]),
        ("Stations",          str(len(sim.cfg["stations"])), _C["dark"]),
        ("W_max (s)",         str(sim.W_max),             _C["dark"]),
    ]
    for col, (label, val, bg) in enumerate(kpis, 1):
        _hdr_cell(ws, 4, col, label, bg=bg, sz=9)
        c = ws.cell(5, col, val)
        c.font      = Font(name=FONT, bold=True, size=13, color=bg)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill      = PatternFill("solid", fgColor=_C["bg_grey"])
        c.border    = _thin()
        ws.column_dimensions[get_column_letter(col)].width = 14

    # ── Greedy solver performance ─────────────────────────────────────────────
    row = 7
    ws.merge_cells(f"A{row}:D{row}")
    _hdr_cell(ws, row, 1, "Greedy Solver – Decision Log", bg=_C["sub"])
    row += 1
    for h, lbl in enumerate(["Epoch","t (s)","# Actions","Solve (ms)","Sample","Task"], 1):
        _hdr_cell(ws, row, h, lbl, bg=_C["dark"], sz=9)
    row += 1
    for gc in sim.greedy_calls:
        _data_cell(ws, row, 1, gc["epoch"],    bg=_C["bg_blue"], align="center")
        _data_cell(ws, row, 2, gc["t_s"],      bg=_C["bg_blue"], align="center")
        _data_cell(ws, row, 3, gc["n_actions"],bg=_C["bg_blue"], align="center")
        _data_cell(ws, row, 4, gc["solve_ms"], bg=_C["bg_blue"], align="center")
        _data_cell(ws, row, 5, gc.get("sample",""), bg=_C["bg_blue"])
        _data_cell(ws, row, 6, gc.get("selected",""), bg=_C["bg_blue"])
        row += 1

    # ── Sample summary table ──────────────────────────────────────────────────
    row += 1
    ws.merge_cells(f"A{row}:F{row}")
    _hdr_cell(ws, row, 1, "Sample Summary", bg=_C["sub"])
    row += 1
    for h, lbl in enumerate(["Sample","Type","Stages","Start (s)","End (s)","Total (s)"], 1):
        _hdr_cell(ws, row, h, lbl, bg=_C["dark"], sz=9)
    row += 1
    sc = _sample_colors([s["id"] for s in sim.cfg["samples"]])
    for sid in sorted(sim.sample_ops.keys()):
        ops = sim.sample_ops[sid]
        if not ops: continue
        stype = sim.sam.get(sid, {}).get("type", "?")
        bg    = sc.get(sid, _C["bg_grey"])
        start = ops[0]["S_s"]
        end   = ops[-1]["C_s"]
        _data_cell(ws, row, 1, sid,        bg=bg, bold=True)
        _data_cell(ws, row, 2, stype,      bg=bg)
        _data_cell(ws, row, 3, len(ops),   bg=bg, align="center")
        _data_cell(ws, row, 4, start,      bg=bg, align="center")
        _data_cell(ws, row, 5, end,        bg=bg, align="center")
        _data_cell(ws, row, 6, f"=F{row}-E{row}", bg=bg, align="center")
        row += 1

    # ── Chart: completion times ───────────────────────────────────────────────
    chart_data_start = row + 2
    ws.cell(chart_data_start - 1, 8, "Sample")
    ws.cell(chart_data_start - 1, 9, "Completion Time (s)")
    cdr = chart_data_start
    for sid in sorted(sim.sample_ops.keys()):
        ops = sim.sample_ops[sid]
        if ops:
            ws.cell(cdr, 8, sid)
            ws.cell(cdr, 9, ops[-1]["C_s"])
            cdr += 1

    chart = BarChart()
    chart.type  = "col"
    chart.title = "Sample Completion Times"
    chart.y_axis.title = "Time (s)"
    chart.x_axis.title = "Sample ID"
    chart.height = 12
    chart.width  = 20
    d_ref = Reference(ws, min_col=9, min_row=chart_data_start - 1, max_row=cdr - 1)
    c_ref = Reference(ws, min_col=8, min_row=chart_data_start,     max_row=cdr - 1)
    chart.add_data(d_ref, titles_from_data=True)
    chart.set_categories(c_ref)
    chart.series[0].graphicalProperties.solidFill = "2E75B6"
    ws.add_chart(chart, "H6")


def _build_event_log(wb, sim):
    ws = wb.create_sheet("Event_Log")
    ws.sheet_view.showGridLines = False

    _hdr_cell(ws, 1, 1, "Time (s)",   bg=_C["hdr"])
    _hdr_cell(ws, 1, 2, "Event Type", bg=_C["hdr"])
    _hdr_cell(ws, 1, 3, "Details",    bg=_C["hdr"])
    _col_w(ws, [12, 28, 100])

    EV_BG = {
        "SAMPLE_RELEASE":    _C["bg_green"],
        "SAMPLE_COMPLETED":  "C6EFCE",
        "ROBOT_TASK_START":  _C["bg_blue"],
        "ROBOT_IDLE":        _C["bg_grey"],
        "STATION_COMPLETE":  _C["bg_amber"],
        "GREEDY_RESULT":     _C["bg_red"],
        "DECISION_TRIVIAL":  "EAF1DD",
    }

    for i, ev in enumerate(sim.event_log, 2):
        bg = EV_BG.get(ev["type"], _C["white"])
        _data_cell(ws, i, 1, ev["t"],   bg=bg, align="center")
        _data_cell(ws, i, 2, ev["type"], bg=bg)
        det = "; ".join(f"{k}={v}" for k, v in ev["details"].items())
        _data_cell(ws, i, 3, det, bg=bg)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:C{len(sim.event_log)+1}"


def _build_sample_timeline(wb, sim):
    ws = wb.create_sheet("Sample_Timeline")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:I1")
    c = ws.cell(1, 1, "SAMPLE OPERATION SCHEDULE")
    c.font = Font(name=FONT, bold=True, size=13, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=_C["hdr"])
    c.alignment = Alignment(horizontal="center", vertical="center")

    hdrs = ["Sample","Type","Stage","Type k","Station",
            "Start (s)","End (s)","Wait (s)","W_max OK?"]
    for col, h in enumerate(hdrs, 1):
        _hdr_cell(ws, 2, col, h, bg=_C["sub"])
    _col_w(ws, [10, 8, 7, 7, 18, 10, 10, 10, 10])

    sc  = _sample_colors([s["id"] for s in sim.cfg["samples"]])
    row = 3
    for sid in sorted(sim.sample_ops.keys()):
        sm   = sim.sam.get(sid, {})
        stype = sm.get("type", "?")
        bg   = sc.get(sid, _C["bg_grey"])
        for op in sim.sample_ops[sid]:
            ok = "✓" if op["wait_s"] <= sim.W_max else "✗ VIOLATION"
            fg = "000000" if ok == "✓" else "FF0000"
            _data_cell(ws, row, 1, sid,         bg=bg, bold=True)
            _data_cell(ws, row, 2, stype,       bg=bg)
            _data_cell(ws, row, 3, op["stage"],  bg=bg, align="center")
            _data_cell(ws, row, 4, op["type_k"], bg=bg, align="center")
            _data_cell(ws, row, 5, op["station"],bg=bg)
            _data_cell(ws, row, 6, op["S_s"],    bg=bg, align="center")
            _data_cell(ws, row, 7, op["C_s"],    bg=bg, align="center")
            _data_cell(ws, row, 8, op["wait_s"], bg=bg, align="center")
            c9 = ws.cell(row, 9, ok)
            c9.font = Font(name=FONT, color=fg, bold=(ok != "✓"), size=10)
            c9.fill = PatternFill("solid", fgColor=bg)
            c9.alignment = Alignment(horizontal="center", vertical="center")
            c9.border = _thin()
            row += 1

    ws.freeze_panes = "A3"


def _build_robot_schedule(wb, sim):
    ws = wb.create_sheet("Robot_Schedule")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:I1")
    c = ws.cell(1, 1, "ROBOT TRANSPORTATION SCHEDULE")
    c.font = Font(name=FONT, bold=True, size=13, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=_C["hdr"])
    c.alignment = Alignment(horizontal="center", vertical="center")

    hdrs = ["Task","Sample","From (robot)","Pickup","Destination",
            "Start (s)","End (s)","Empty (s)","Loaded (s)"]
    for col, h in enumerate(hdrs, 1):
        _hdr_cell(ws, 2, col, h, bg=_C["sub"])
    _col_w(ws, [8, 8, 14, 14, 14, 10, 10, 10, 10])

    sc = _sample_colors([s["id"] for s in sim.cfg["samples"]])
    for i, t in enumerate(sim.robot_tasks, 3):
        bg = sc.get(t.get("sample_id"), _C["bg_grey"])
        _data_cell(ws, i, 1, t["task_id"],                      bg=bg)
        _data_cell(ws, i, 2, t.get("sample_id") or "(empty)", bg=bg, bold=bool(t.get("sample_id")))
        _data_cell(ws, i, 3, t["origin"],      bg=bg)
        _data_cell(ws, i, 4, t["pickup"],      bg=bg)
        _data_cell(ws, i, 5, t["destination"], bg=bg)
        _data_cell(ws, i, 6, t["start_s"],     bg=bg, align="center")
        _data_cell(ws, i, 7, t["end_s"],       bg=bg, align="center")
        _data_cell(ws, i, 8, t["t_empty_s"],   bg=bg, align="center")
        _data_cell(ws, i, 9, t["t_loaded_s"],  bg=bg, align="center")

    ws.freeze_panes = "A3"

    # Robot utilization summary
    row = len(sim.robot_tasks) + 4
    ws.merge_cells(f"A{row}:C{row}")
    _hdr_cell(ws, row, 1, "Robot Utilization Summary", bg=_C["sub"])
    row += 1
    total_dur = sum(t["duration_s"] for t in sim.robot_tasks)
    makespan  = sim.robot_tasks[-1]["end_s"] if sim.robot_tasks else 1
    _data_cell(ws, row, 1, "Total transport time (s)")
    _data_cell(ws, row, 2, round(total_dur, 1), align="center", bold=True)
    row += 1
    _data_cell(ws, row, 1, "Makespan (s)")
    _data_cell(ws, row, 2, round(makespan, 1), align="center", bold=True)
    row += 1
    _data_cell(ws, row, 1, "Robot utilization (%)")
    pct = f"={get_column_letter(2)}{row-2}/{get_column_letter(2)}{row-1}*100"
    c = ws.cell(row, 2, pct)
    c.number_format = "0.0"
    c.font      = Font(name=FONT, bold=True, size=10)
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.border    = _thin()


def _build_station_stats(wb, sim, makespan):
    ws = wb.create_sheet("Station_Stats")
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:H1")
    c = ws.cell(1, 1, "STATION UTILISATION STATISTICS")
    c.font = Font(name=FONT, bold=True, size=13, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor=_C["hdr"])
    c.alignment = Alignment(horizontal="center", vertical="center")

    hdrs = ["Station","Type k","Name","# Ops",
            "Busy Time (s)","Util (%)","Avg Process (s)","Bottleneck?"]
    for col, h in enumerate(hdrs, 1):
        _hdr_cell(ws, 2, col, h, bg=_C["sub"])
    _col_w(ws, [10, 8, 22, 8, 14, 10, 16, 12])

    st_busy = defaultdict(float)
    st_ops  = defaultdict(int)
    for sid, ops in sim.sample_ops.items():
        for op in ops:
            dur = op["C_s"] - op["S_s"]
            if dur > 0:
                st_busy[op["station"]] += dur
                st_ops[op["station"]]  += 1

    row = 3
    for vid in sorted(sim.cfg["stations"]):
        info  = sim.cfg["stations"][vid]
        k     = info["type_k"]
        name  = sim.cfg["station_types"][str(k)]["name"]
        busy  = st_busy.get(vid, 0.0)
        n_ops = st_ops.get(vid, 0)
        util  = (busy / makespan * 100) if makespan > 0 else 0
        avg   = (busy / n_ops) if n_ops > 0 else 0
        bottle = "⚠ YES" if util > 70 else ""

        bg = _C["bg_red"] if util > 80 else (_C["bg_amber"] if util > 50 else _C["bg_grey"])
        _data_cell(ws, row, 1, vid,           bg=bg, bold=True)
        _data_cell(ws, row, 2, k,             bg=bg, align="center")
        _data_cell(ws, row, 3, name,          bg=bg)
        _data_cell(ws, row, 4, n_ops,         bg=bg, align="center")
        _data_cell(ws, row, 5, round(busy,1), bg=bg, align="center")
        _data_cell(ws, row, 6, round(util,1), bg=bg, align="center")
        _data_cell(ws, row, 7, round(avg,1),  bg=bg, align="center")
        c8 = ws.cell(row, 8, bottle)
        c8.font = Font(name=FONT, color="C00000" if bottle else "000000",
                       bold=bool(bottle), size=10)
        c8.fill = PatternFill("solid", fgColor=bg)
        c8.alignment = Alignment(horizontal="center", vertical="center")
        c8.border = _thin()
        row += 1

    # Utilisation bar chart
    chart_row = row + 2
    ws.cell(chart_row, 1, "Station")
    ws.cell(chart_row, 2, "Util (%)")
    cdr = chart_row + 1
    for vid in sorted(sim.cfg["stations"]):
        busy = st_busy.get(vid, 0.0)
        util = (busy / makespan * 100) if makespan > 0 else 0
        ws.cell(cdr, 1, vid)
        ws.cell(cdr, 2, round(util, 1))
        cdr += 1

    chart = BarChart()
    chart.type  = "col"
    chart.title = "Station Utilisation (%)"
    chart.y_axis.title = "Utilisation (%)"
    chart.height = 12
    chart.width  = 22
    d_ref = Reference(ws, min_col=2, min_row=chart_row, max_row=cdr - 1)
    c_ref = Reference(ws, min_col=1, min_row=chart_row + 1, max_row=cdr - 1)
    chart.add_data(d_ref, titles_from_data=True)
    chart.set_categories(c_ref)
    chart.series[0].graphicalProperties.solidFill = "2E75B6"
    ws.add_chart(chart, f"D{row+2}")

    ws.freeze_panes = "A3"


def create_excel_report(sim: RoboticCellSimulator, makespan: float):
    if not EXCEL_OK:
        print("[EXCEL] openpyxl not installed — skipping report.")
        return

    wb = openpyxl.Workbook()

    _build_dashboard(wb, sim, makespan)
    _build_event_log(wb, sim)
    _build_sample_timeline(wb, sim)
    _build_robot_schedule(wb, sim)
    _build_station_stats(wb, sim, makespan)

    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    wb.save(EXCEL_FILE)
    print(f"[EXCEL] Report saved → {EXCEL_FILE}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Robotic Cell Simulator v2 – configurable solver"
    )
    parser.add_argument("--seed",     type=int,   default=42)
    parser.add_argument("--samples",  type=int,   default=8)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--speed",    type=float, default=ROBOT_SPEED)
    parser.add_argument("--solver",   type=str,   default="greedy_solver.py",
                        help="Solver script to call (default: greedy_solver.py)")
    parser.add_argument("--all-actions", action="store_true",
                        help="Expose all idle destinations per sample (for MECT solvers)")
    args = parser.parse_args()

    print("=" * 60)
    print("  ROBOTIC CELL SIMULATOR v2")
    print("=" * 60)
    print(f"  seed={args.seed}  n_hc={args.samples}  "
          f"interval={args.interval}s  speed={args.speed}m/s")
    print(f"  solver={args.solver}  all_actions={args.all_actions}")
    print()

    config = generate_config(
        seed=args.seed,
        n_hc=args.samples,
        sample_interval=args.interval,
        robot_speed=args.speed,
    )
    n_st = len(config["stations"])
    n_sa = len(config["samples"])
    print(f"  Config written → {CONFIG_FILE}")
    print(f"  Stations: {n_st}   Samples: {n_sa}")
    print()

    sim      = RoboticCellSimulator(config,
                                    solver_script=args.solver,
                                    all_actions=args.all_actions)
    makespan = sim.run()

    print()
    print("─" * 60)
    print(f"  Makespan       : {makespan:.0f} s")
    print(f"  HC completed   : {sim.qc['N_HC']}")
    print(f"  QC1 completed  : {sim.qc['N_QC1']}")
    print(f"  QC2 completed  : {sim.qc['N_QC2']}")
    print(f"  Blank completed: {sim.qc['N_Blank']}")
    print(f"  Robot tasks    : {len(sim.robot_tasks)}")
    print(f"  Solver calls   : {len(sim.greedy_calls)}")
    print(f"  Log written    → {LOG_FILE}")

    print()
    print("  Generating Excel report…")
    create_excel_report(sim, makespan)
    print("─" * 60)


if __name__ == "__main__":
    main()
