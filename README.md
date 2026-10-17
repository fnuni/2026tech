# Robotic water-sample preparation cell

Replication package **3.0.1**, released 17 October 2026, for **Design and Scheduling of an Automated Water-Sample Preparation Cell: Capacity Diagnosis and Pre-Commissioning Assessment**, prepared for **Technologies**.

Repository: https://github.com/fnuni/2026apsc

Frozen version: https://github.com/fnuni/2026apsc/tree/v3.0.1

Version 3.0.1 updates figure readability and fits publication tables to the manuscript text width. Experimental inputs, policies, traces, and numerical results are unchanged from 3.0.0.

The study diagnoses a nominal 230-job day that is close to its relaxed operational makespan bound but has substantial internal waiting violations. It compares four distinct rules, includes an equivalent implementation control, and separates internal timing, external delay, and completion time. No chemical recovery, economic return, or sustainability benefit is measured.

## Reproduce

Verified with Python 3.10.10, PuLP 2.9.0, PySCIPOpt 5.2.1 / SCIP 9.1.1, NumPy and Matplotlib on macOS 15.7.2 ARM64. Exact runs use one thread. Install dependencies, then execute:

```sh
python -m pip install -r requirements.txt
python code/reproduce.py
```

The individual steps are:

```sh
python code/test_model.py
python code/run_study.py
python code/exact_study.py
python code/run_hard_wait.py
python code/make_tables.py
python code/make_figures.py
python code/verify_package.py
```

The simulator is deterministic. Runtime measurements can differ. HW8 is stopped after 120 seconds, so its incumbent, dual bound and gap may differ with machine speed. The released HW8 trace is an audited feasible schedule, not a claimed optimum. The five operational optima and two reduced hard-wait optima require matching solver primal and dual bounds.

All inputs are public. The confidential project plan is unnecessary for reproduction. Generated tables use the released CSVs; figures use timestamps and CSVs. `make_tables.py --paper-dir PATH` additionally copies the tables into a local manuscript directory. `validation/input_code_sha256.json` fingerprints the frozen inputs, code, data, tables, figures and released trace/model artifacts. `python code/verify_package.py --check-hashes` checks those bytes before regeneration; `reproduce.py` performs this check first. Regenerated CPU timings, PDF metadata and time-limited solver results can change artifact hashes. Plain verification audits the regenerated physical content without rewriting the frozen manifest; `--freeze` explicitly creates a new snapshot.

## Study design

- **75 primary runs:** four distinct rules on 15 scenarios (60 pairs), plus 15 URG-MECT equivalence controls.
- **21 ablation runs:** full score and six variants on CS2, CS6 and CS9.
- **44 physical-time runs:** separately specified loaded/empty dwell and traverse, plus added robot-service budgets, on CS6 and CS8 under URG and LB-LA.
- **24 threshold runs:** 150, 300, 600 and 900 seconds under URG, BNA and LB-LA on CS6 and CS9, alongside fixed-trace reclassification.
- **8 admission-cap runs:** caps 1–4 on CS6 and CS8.
- **5 operational MILP checks** and **3 reduced hard-wait MILP checks**, with complete primal trace audits.

`data/expanded_cases.csv` lists all cases. `instances/` contains explicit route and release lists. CS8/CS9 scale CS6 release times; CS10/CS11 change analytical type order at the same instants; CS12 changes the mix at the same instants; CS13/CS14 change extraction time only. CS15 repeats the complete CS2 block ten times at 3600-second offsets, isolating volume at the same within-block cadence and mix. These configurations are deterministic contrasts, not statistical replications.

The four rules are URG, SRPT (residual nominal processing/transport work), BNA (bottleneck-aware admission with urgency internally), and LB-LA (lower-bound-guided one-step lookahead). URG-MECT exactly duplicates URG under the released assumptions and is not an independent competitive baseline. The legacy identifier LB-ROLL remains a code alias for LB-LA; the method does not execute a classical rollout base policy to horizon completion.

BNA's cap counts admitted Method-2-route jobs, including QC2, until pickup from Pipet C to concentration. The primary cap is 2; the sensitivity uses caps 1–4. A compliant CS6 cap-1 trace exists at 46.16 hours, not within the 24-hour target.

## Metrics and audits

All times are seconds unless a field name says otherwise. Route indices are zero-based; stage zero is the external entry interface.

| Field | Meaning |
|---|---|
| `makespan_s` | Last exit delivery from the common time origin |
| `technological_wait_s` | Actual pickup minus preceding processing completion, including empty approach and any added service before pickup |
| `external_wait_s` | Release to first pickup, excluded from internal violations |
| `violations` | Internal waits strictly above the specified budget; default 300 s |
| `affected_samples` | Distinct jobs with at least one such violation |
| `budget_compliant_samples` | Jobs without a model timing violation; not a chemical validity metric |
| `gap_pct` | 100 × (makespan − operational certificate) / certificate |
| `excess_over_bound_s` | Makespan minus operational certificate |
| `robot_util_pct` | Loaded, empty and added auxiliary robot occupation / makespan |
| `mean_decision_ms` | In-process state/score computation, excluding process startup and I/O |

The audit reconstructs waits from timestamps and checks route stages, physical station assignments, transfer/operation links, start=delivery, clearance=next pickup, completion=exit, movement times, robot seriality, position occupancy, and physical KPI aggregates. Negative tests require rejection of altered waits, stations, starts, clearances, completions, external waits, movement durations and violation counts. These checks establish trace consistency; they do not independently reimplement a policy's ranking function.

`data/waiting_by_transition.csv` and `waiting_by_class.csv` provide linear-interpolated quantiles, counts and severity. `operating_metrics.csv` gives exact event-based external queue peaks and flow/wait summaries. In the reference CS6-URG trace, 100 of 114 violating transitions precede Pipet C.

`data/ablations.csv` reports two change counts: counterfactual action changes at identical full-score states, and sequence differences by epoch after separately running a variant. The second includes propagated state changes. It is not a local comparison.

Physical-time settings are **uncalibrated stress scenarios**, not measured uncertainty intervals. Auxiliary service is added before each pickup, with no claim to resolve actual cleaning or consumable routes. Threshold sensitivity does not validate chemical holding times. QC uses explicit exogenous releases, not a regulatory completion gate.

## Exact artifacts

`validation/exact/` and `validation/hard_wait/` contain LP models, explicit reduced inputs, solver logs, full variable values, SCIP `.sol` files, status summaries, and audited robot/operation traces. Status summaries report solver termination, primal bound, dual bound and gap; they are not formally checkable branch-and-bound proof logs. HW8 has a feasible incumbent at timeout. Its solver gap does not imply infeasibility.

## Main files

- `code/cell_model.py`: simulation, dispatch/admission rules and physical audit.
- `code/run_study.py`: primary campaign, ablations, motion and threshold experiments.
- `code/run_admission_sweep.py`: cap sensitivity, also called by `run_study.py`.
- `code/exact_study.py`, `run_hard_wait.py`: MILP studies and full primal exports.
- `code/test_model.py`, `verify_package.py`: regression/negative tests and frozen-package audit.
- `code/make_tables.py`, `make_figures.py`: publication artifacts.
- `data/`, `instances/`, `tables/`, `figures/`, `validation/`: reproducible inputs and results.

## Original simulator provenance

The original engines and all three original policy scripts are preserved under `code/original/`. Their independent executions are under `validation/original_runs/`; prior archived inputs/results are under `data/legacy/`. These are provenance artifacts with different waiting semantics, not the primary study. The optional command is:

```sh
python code/run_original_campaign.py
```

The original bound module uses mean movements and estimated empty work and is not the article's certificate. The primary study uses minimum mandatory loaded work and the explicit workload, release-path and head/tail certificate.

## Licence and citation

Code is MIT (`LICENSE-CODE`); data are CC BY 4.0 (`LICENSE`). `CITATION.cff` identifies the five authors and frozen package version. Confidential source documents, credentials and manuscript backups are not distributed.
