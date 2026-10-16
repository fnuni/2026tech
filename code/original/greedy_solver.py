#!/usr/bin/env python3
"""
greedy_solver.py – Greedy Heuristic Solver for the Robotic Cell
================================================================
Called by simulator.py at each decision epoch where multiple samples
compete for the robot.  Implements the greedy policy from
GreedyAlgorithm.pdf: at each step select the sample with the minimum
remaining waiting time (W_max - w_s), breaking ties by transport
duration and sample type priority (QC > HC).

Input/output (same directory as simulator.py):
    Reads   ← config.json      (static instance data, loaded once)
    Reads   ← input.json       (current cell state + feasible actions)
    Writes  → output.json      (selected action for the controller)
    Writes  → opt_meta.json    (diagnostics for the research team)

Exit codes:  0 = success,  1 = error (fallback to simulator heuristic).
"""

import json
import sys
import time
from datetime import datetime

CONFIG_FILE   = "config.json"
INPUT_FILE    = "input.json"
OUTPUT_FILE   = "output.json"
OPT_META_FILE = "opt_meta.json"

# QC sample types have higher scheduling priority than ordinary HC samples
# (must be processed to avoid analytical block)
_TYPE_PRIORITY = {"Blank": 0, "QC2": 1, "QC1": 2, "HC": 3, "SVOC": 3, "SVOC_E": 3}


# ─── I/O helpers ─────────────────────────────────────────────────────────────

def _load():
    with open(CONFIG_FILE) as fh:
        config = json.load(fh)
    with open(INPUT_FILE) as fh:
        inp = json.load(fh)
    return config, inp


def _write_output(epoch, t_s, instance_id, chosen):
    """Write the controller-facing output.json (minimal payload)."""
    if chosen:
        action_out = {
            "task_id":     chosen.get("task_id"),
            "type":        chosen.get("type", "loaded"),
            "sample_id":   chosen.get("sample_id"),
            "origin":      chosen.get("origin"),
            "pickup":      chosen.get("pickup", chosen.get("origin")),
            "destination": chosen.get("destination"),
            "start_s":     t_s,
            "duration_s":  chosen.get("duration_s"),
            "end_s":       t_s + (chosen.get("duration_s") or 0),
        }
    else:
        action_out = None

    payload = {
        "_comment":        "Greedy solver – next action decision.",
        "instance_id":     instance_id,
        "epoch":           epoch,
        "t_s":             t_s,
        "solver":          "greedy_min_remaining_wait",
        "selected_action": action_out,
    }
    with open(OUTPUT_FILE, "w") as fh:
        json.dump(payload, fh, indent=2)


def _write_meta(epoch, t_s, instance_id, chosen, feasible,
                urgency_table, solve_s, W_max, X_sam):
    """Write opt_meta.json for analysis / Gantt reconstruction."""
    next_epoch_n = epoch.split("_")[-1]
    try:
        next_epoch = f"tau_{int(next_epoch_n)+1}"
    except ValueError:
        next_epoch = "tau_next"

    meta = {
        "_comment":              "Greedy solver – optimisation metadata.",
        "instance_id":           instance_id,
        "epoch":                 epoch,
        "t_s":                   t_s,
        "solver":                "greedy_min_remaining_wait",
        "status":                "FEASIBLE" if chosen else "NO_ACTION",
        "solve_time_s":          round(solve_s, 6),
        "n_feasible_actions":    len(feasible),
        "urgency_scores":        urgency_table,
        "selected_task_id":      chosen.get("task_id") if chosen else None,
        "selected_sample":       chosen.get("sample_id") if chosen else None,
        "selected_destination":  chosen.get("destination") if chosen else None,
        "selection_rationale": {
            "rule":    "min(W_max - w_s)",
            "W_max":   W_max,
            "w_s_selected": (
                X_sam.get(chosen["sample_id"], {}).get("w_s", 0)
                if chosen and chosen.get("sample_id") else None
            ),
        },
        "all_actions_ranked": _rank_actions(feasible, X_sam, W_max),
        "state_at_next_epoch": {
            "epoch":     next_epoch,
            "t_s":       t_s + (chosen.get("duration_s", 0) if chosen else 0),
            "X_rob": {
                "location": (chosen.get("destination")
                             if chosen else None),
                "status": "idle",
            },
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(OPT_META_FILE, "w") as fh:
        json.dump(meta, fh, indent=2)


def _rank_actions(feasible, X_sam, W_max):
    """Return feasible actions sorted by urgency score (for meta output)."""
    ranked = []
    for a in feasible:
        sid = a.get("sample_id")
        w_s = X_sam.get(sid, {}).get("w_s", 0) if sid else 0
        rem = W_max - w_s
        stype = X_sam.get(sid, {}).get("type", "?") if sid else "?"
        ranked.append({
            "task_id":      a.get("task_id"),
            "sample_id":    sid,
            "type":         stype,
            "w_s":          w_s,
            "remaining_w":  rem,
            "duration_s":   a.get("duration_s"),
            "destination":  a.get("destination"),
        })
    ranked.sort(key=lambda x: (x["remaining_w"],
                                _TYPE_PRIORITY.get(x["type"], 99),
                                x["duration_s"]))
    return ranked


# ─── Core greedy selection ────────────────────────────────────────────────────

def select_action(config, inp):
    """
    Greedy policy (ref: GreedyAlgorithm.pdf, Section 2):

    Step 1  For every feasible action that serves a PENDING sample
            (one that has already started its route and is now waiting
            for the robot after completing a processing step), compute:
                remaining_w = W_max - w_s
            Pick the action with the MINIMUM remaining_w (most urgent).

    Step 2  If no pending sample is actionable, accept a new sample from
            the input queue.  QC types (Blank > QC2 > QC1) take priority
            over ordinary HC/SVOC samples, as required by the QC policy.

    Tie-breaking (same remaining_w, same type priority):
            Prefer shorter transport duration.
    """
    feasible = inp.get("feasible_actions", [])
    if not feasible:
        return None

    W_max = config.get("W_max_s", 300)
    X_sam = inp.get("X_sam", {})
    X_st  = inp.get("X_st",  {})

    # Split actions into "pending" (already in process) vs "new entry"
    pending_actions = []
    entry_actions   = []

    for a in feasible:
        sid  = a.get("sample_id")
        if not sid:
            # Action targeting an unknown/null sample (shouldn't normally happen)
            entry_actions.append(a)
            continue

        sam  = X_sam.get(sid, {})
        stat = sam.get("status", "")
        stg  = sam.get("stage_j", 0)

        # "pending" = sample already processed ≥1 operation beyond entry
        # "entry"   = sample is still at its entry station (stage 1)
        if stg <= 1:
            entry_actions.append(a)
        else:
            pending_actions.append(a)

    # ── Step 1: serve the most urgent pending sample ──────────────────────────
    if pending_actions:
        def pending_key(a):
            sid = a["sample_id"]
            sam = X_sam.get(sid, {})
            w_s = sam.get("w_s", 0.0)
            stype = sam.get("type", "HC")
            return (
                W_max - w_s,                          # lower = more urgent
                _TYPE_PRIORITY.get(stype, 99),
                a.get("duration_s", 0),               # shorter transport first
            )
        return min(pending_actions, key=pending_key)

    # ── Step 2: pick from entry queue using QC policy ────────────────────────
    if entry_actions:
        def entry_key(a):
            sid   = a.get("sample_id", "")
            sam   = X_sam.get(sid, {})
            stype = sam.get("type", "HC")
            w_s   = sam.get("w_s", 0.0)
            return (
                _TYPE_PRIORITY.get(stype, 99),        # QC types first
                W_max - w_s,                          # then most waiting
                a.get("duration_s", 0),
            )
        return min(entry_actions, key=entry_key)

    return None   # no actionable sample


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    t0 = time.perf_counter()

    try:
        config, inp = _load()
    except FileNotFoundError as exc:
        print(f"[GREEDY] ERROR: {exc}", file=sys.stderr)
        return 1

    W_max       = config.get("W_max_s", 300)
    X_sam       = inp.get("X_sam", {})
    epoch       = inp.get("epoch", "tau_?")
    t_s         = inp.get("t_s", 0.0)
    instance_id = inp.get("instance_id", "?")
    feasible    = inp.get("feasible_actions", [])

    chosen = select_action(config, inp)

    solve_s = time.perf_counter() - t0

    # Urgency table (for meta)
    urgency_table = {}
    for a in feasible:
        sid = a.get("sample_id")
        if sid and sid in X_sam:
            w_s = X_sam[sid].get("w_s", 0.0)
            urgency_table[sid] = {
                "w_s":         round(w_s, 1),
                "remaining_w": round(W_max - w_s, 1),
                "type":        X_sam[sid].get("type", "?"),
                "stage":       X_sam[sid].get("stage_j", 0),
            }

    _write_output(epoch, t_s, instance_id, chosen)
    _write_meta(epoch, t_s, instance_id, chosen,
                feasible, urgency_table, solve_s, W_max, X_sam)

    # ── Console summary (captured by simulator subprocess logging) ────────────
    if chosen:
        sid  = chosen.get("sample_id", "-")
        dest = chosen.get("destination", "-")
        dur  = chosen.get("duration_s", 0)
        w_s  = X_sam.get(sid, {}).get("w_s", 0) if sid else 0
        print(
            f"[GREEDY] {epoch} | t={t_s:.0f}s | "
            f"n_feasible={len(feasible)} | "
            f"→ {chosen.get('task_id')} "
            f"(sample={sid}, dest={dest}, "
            f"w_s={w_s:.0f}s, remaining_w={W_max-w_s:.0f}s, "
            f"dur={dur}s) | "
            f"solve={solve_s*1000:.2f}ms"
        )
    else:
        print(f"[GREEDY] {epoch} | t={t_s:.0f}s | NO ACTION (0 feasible)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
