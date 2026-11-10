# Robotic water-sample preparation cell

Replication package **4.1.0**, released 10 November 2026, for **Design and Scheduling of an Automated Water-Sample Preparation Cell: Capacity Diagnosis and Pre-Commissioning Assessment** (Technologies, revised manuscript).

Repository: https://github.com/fnuni/2026tech
Frozen version: https://github.com/fnuni/2026tech/tree/v4.1.0

Version 4.0.0 adds the second-round analyses requested during peer review: constraint-programming (CP-SAT) reference schedules with completion-to-pickup time lags and relaxation bounds, a rolling-horizon optimiser (RH-CP), a targeted hard-wait feasibility search on the design day, intermediate instances with primal and dual bounds, structural design alternatives, stochastic and three-day runs, robot-workload accounting, stage-specific waiting limits and an LB-LA weight grid. All inputs, traces and results of version 3.0.2 are retained unchanged; `code/cell_model.py` keeps bit-identical behaviour for every 3.0.2 setting.

Version 4.1.0 (pre-submission corrections): (i) the conservative CP model requires the robot to reach the origin of the first pickup from its initial position (`Transfer.init_max`, `CpScheduleModel.initial_lead`), the relaxed model uses the corresponding minimum, and `test_model.py` adds a regression case (single QC1 sample at time zero); every released CP trace satisfies this condition and passes the physical audit, and the three auxiliary-service schedules of `cp_sensitivity.csv` were regenerated with the corrected model; (ii) every data file states which certificate a field refers to: `certificate_lb_s` is the strengthened certificate of Equation (cert), `certificate_v3_lb_s` the first-version certificate, with the matching gaps (`code/migrate_certificate_fields.py` converted the 4.0.0 files; schedules and KPIs are unchanged); (iii) the stochastic summary adds paired comparisons with the default LB-LA. RH-CP windows minimise the weighted objective n·Cmax + ΣCj (n jobs in the window), which is not lexicographic.

## Scenario map

Every scenario is an explicit input file. `data/scenario_map.csv` gives the same information in machine-readable form; `data/cs15_timeline.csv` lists the complete CS15 release timeline.

| Case | Input file | Jobs | Analytical cadence (s) | Release list and distinguishing change | Pipet C (s) |
|---|---|---|---|---|---|
| CS1 | `instances/cs1_nominal_config.json` | 23 | 375 | Generator, seed 42, scale 0.1: 10 HC, 7 SVOC, 3 SVOC_E, QC1, QC2, Blank | 600 |
| CS2 | `instances/cs2_medium_config.json` | 23 | 180 | CS1 sequence at 180 s | 600 |
| CS3 | `instances/cs3_high_config.json` | 23 | 60 | CS1 sequence at 60 s | 600 |
| CS4 | `instances/cs4_medium_s99_config.json` | 23 | 180 | Same instants and mix as CS2; seed 99 places the three emulsions on Method-2 slots 4, 7, 10 (CS2: 1, 2, 5) and swaps QC1/Blank (CS2: QC2 905 s, Blank 1985 s, QC1 3065 s; CS4: QC2, QC1, Blank) | 600 |
| CS5 | `instances/cs5_double_config.json` | 46 | 150 | Generator, seed 42, scale 0.2 | 600 |
| CS6 | `instances/cs6_fullday_config.json` | 230 | 375 | Design day: generator, seed 42, scale 1.0 | 600 |
| CS7 | `instances/cs7_pipc480_config.json` | 23 | 180 | CS2 with Pipet C 480 s | 480 |
| CS8 | `instances/cs8_day180_config.json` | 230 | 180 | CS6 release times × 180/375 | 600 |
| CS9 | `instances/cs9_day60_config.json` | 230 | 60 | CS6 release times × 60/375 | 600 |
| CS10 | `instances/cs10_m2first_config.json` | 230 | 180 | CS8 instants; Method-2 analytical routes first | 600 |
| CS11 | `instances/cs11_hcfirst_config.json` | 230 | 180 | CS8 instants; HC routes first | 600 |
| CS12 | `instances/cs12_m2heavy_config.json` | 230 | 180 | CS8 instants; 40 evenly spaced HC routes replaced by 28 SVOC + 12 SVOC_E | 600 |
| CS13 | `instances/cs13_day480_config.json` | 230 | 375 | CS6 with Pipet C 480 s | 480 |
| CS14 | `instances/cs14_day180_480_config.json` | 230 | 180 | CS8 with Pipet C 480 s | 480 |
| CS15 | `instances/cs15_tiled180_config.json` | 230 | 180 | Ten CS2 blocks at 3600 s offsets | 600 |

**Generator.** CS1–CS7 were produced by `code/original/simulator_v3.py::generate_ppla_config` from the project mix (100 HC, 100 SVOC, 30% of SVOC drawn as emulsions without replacement, 10 QC1, 10 QC2, 10 blanks per day at scale 1.0). HC and Method-2 analytical samples alternate at the stated cadence; one control is released 5 s after every sixth analytical release, in a seeded random order. CS8–CS15 are deterministic transformations implemented in `code/run_study.py::inputs`.

**CS15 cadence.** Each CS2 block contains 20 analytical releases at 0, 180, …, 3420 s and three controls at 905, 1985 and 3065 s. Blocks are offset by 3600 s = 20 × 180 s, so the first release of block b+1 follows the last analytical release of block b by exactly 180 s: the analytical cadence is continuous and blocks do not overlap. `code/verify_revision.py` asserts this property.

**Design alternatives.** `instances/design/*_config.json` add a second Pipet C (and optionally a second Pipet D) next to the existing module; added movement costs follow the dwell-plus-traverse model with the nominal dwell and speed. `instances/derived/rq160_540s_samples.json` is the 160-analytical-job robot-qualification workload.

## Reproduce

Python 3.10–3.13. Install dependencies, then run either the complete pipeline or individual steps.

```sh
python -m pip install -r requirements.txt
python code/reproduce.py             # 3.0.2 study + 4.x revision study + audits
python code/reproduce.py --revision-only
```

| Manuscript item | Command | Output |
|---|---|---|
| Primary campaign: design day, scenarios, admission caps, thresholds, ablations, motion tests | `python code/run_study.py` | `data/kpi_campaign.csv`, `lower_bounds.csv`, `waiting_by_*.csv`, `operating_metrics.csv`, `admission_sensitivity.csv`, `threshold_sensitivity.csv`, `physical_sensitivity.csv`, `ablations.csv` |
| Targeted hard-wait search (CS6) | `python code/cp_study.py targeted --step 40 --polish 600 --limit 120`; `python code/cp_study.py flow --step 600 --polish 300`; `python code/cp_study.py repair --threshold 10800` | `data/cp_targeted_cs6.csv`, `validation/cp/cs6_fullday/targeted_*` |
| RH-CP on CS1–CS15 (operational and hard-wait) | `python code/cp_study.py full --step 15` | `data/cp_reference.csv`, `validation/cp/<case>/` |
| Intermediate instances, primal/dual CP bounds | `python code/cp_study.py intermediate --limit 120` | `data/cp_intermediate.csv` |
| Design alternatives | `python code/revision_study.py design`; `python code/cp_study.py design --step 15` | `data/design_alternatives.csv`, `data/cp_design.csv` |
| Stochastic and three-day runs | `python code/revision_study.py stochastic` | `data/stochastic_replications.csv`, `data/stochastic_summary.csv` |
| Robot accounting and workload sweep | `python code/revision_study.py robot`; `python code/cp_study.py sensitivity --step 15` | `data/robot_accounting.csv`, `data/robot_load_sensitivity.csv`, `data/cp_sensitivity.csv` |
| Stage-specific waiting limits | `python code/revision_study.py profile` | `data/stage_budget_profile.csv` |
| Strengthened certificate (subset head/tail, mandatory empty legs) | `python code/revision_study.py bounds` | `data/lower_bounds_v4.csv` |
| LB-LA weight grid | `python code/revision_study.py weights` | `data/weight_sensitivity.csv` |
| Scenario map, CS15 timeline | `python code/revision_study.py scenarios` | `data/scenario_map.csv`, `data/cs15_timeline.csv` |
| Reduced MILP checks | `python code/exact_study.py`; `python code/run_hard_wait.py` | `data/exact_reduced_instances.csv`, `data/hard_wait_results.csv` |
| All manuscript tables | `python code/make_revision_tables.py --paper-dir PATH` | `tables/*.tex` |
| All manuscript figures | `python code/make_figures.py`; `python code/make_revision_figures.py` | `figures/*.pdf` |
| Audits | `python code/test_model.py`; `python code/verify_package.py`; `python code/verify_revision.py` | console report |

Simulations are deterministic and reproduce bit for bit. CP-SAT runs are wall-clock limited (two workers; values reported in the paper were obtained on a two-core Linux virtual machine) and can return different schedules on other hardware; every released CP schedule is stored with its pickup times and complete trace and can be re-audited independently.

## Methods

- **Dispatching rules:** URG, SRPT, BNA (bottleneck-aware admission, cap 2; caps 1–4 as sensitivity) and LB-LA (lower-bound-guided one-step lookahead; legacy alias LB-ROLL). URG-MECT duplicates URG by construction and is reported only as an implementation-equivalence control.
- **CP models** (`code/cp_reference.py`): the conservative interval model reserves the longest admissible empty and loaded movements and the longest initial robot travel to the first pickup; its solutions are re-timed with the true movement matrix and audited (primal schedules). The relaxed model reserves only the shortest loaded movement and the mandatory empty-leg separation; its proven bound is a valid makespan lower bound. Completion-to-pickup limits are maximum time lags.
- **RH-CP:** release-ordered rolling horizon (window 30, commit 15, 15 s per window, warm start by a time-lag insertion heuristic). Each window minimises n·Cmax + ΣCj (n jobs in the window), a weighted, not lexicographic, objective. It uses the planned release list of the look-ahead window.
- **CP-REF:** RH-CP with 40 s per window, followed by 600 s of monolithic CP-SAT makespan polishing, 300 s of flow-time polishing at no larger makespan, and a repair step that re-inserts samples whose admission was deferred by more than 3 h at their earliest feasible admission (design day; `cp_study.py targeted`, `flow`, `repair`).

## Metrics and audits

All times are seconds unless a field name says otherwise. Route indices are zero-based; stage zero is the external entry interface.

| Field | Meaning |
|---|---|
| `makespan_s` | Last exit delivery from the common time origin |
| `technological_wait_s` | Pickup minus preceding processing completion, including empty approach and any auxiliary service before pickup |
| `external_wait_s` | Release to first pickup, excluded from internal violations |
| `violations` | Violation events: internal waits strictly above their limit (default 300 s, a representative admissible value; the specified tolerance is confidential) |
| `affected_samples` | Distinct samples with at least one violation event |
| `certificate_lb_s` | Strengthened certificate of Equation (cert) (nested head/tail subsets, mandatory empty legs) |
| `certificate_v3_lb_s` | First-version certificate (head/tail on complete operation sets), kept for provenance |
| `gap_pct`, `gap_v3_pct` | 100 × (makespan − certificate) / certificate for the strengthened and the first-version certificate |
| `robot_util_pct` | Loaded, empty and auxiliary robot occupation / makespan |
| `pipc_violation_share_pct` | Share of violation events whose next module is Pipet C |

The audit reconstructs waits from timestamps and checks route stages, station assignments, transfer/operation links, start = delivery, clearance = next pickup, completion = exit, movement times (recorded realisations in stochastic runs), robot seriality, position occupancy and KPI aggregates. Negative tests require rejection of altered records. The audit establishes trace consistency; it does not re-implement a controller's ranking function.

Stage-specific limits (`Budget` in `cell_model.py`, Profile A in `revision_study.py`) are keyed by the module at which the sample waits. Physical-time, auxiliary-service, threshold and stochastic settings are uncalibrated sensitivity scenarios, not measured uncertainty intervals. QC uses explicit exogenous releases, not a regulatory completion gate.

## Main files

- `code/cell_model.py`: simulation, dispatch/admission rules, stage-specific limits, stochastic execution and physical audit.
- `code/cp_reference.py`: CP-SAT interval models, time-lag insertion, re-timing LP, rolling horizon.
- `code/cp_study.py`, `code/revision_study.py`: second-round experiments.
- `code/run_study.py`, `run_admission_sweep.py`, `exact_study.py`, `run_hard_wait.py`: 3.0.2 study.
- `code/make_tables.py`, `make_figures.py` (3.0.2) and `make_revision_tables.py`, `make_revision_figures.py` (4.x): publication artifacts.
- `code/test_model.py`, `verify_package.py`, `verify_revision.py`: regression, negative tests and audits.

## Original simulator provenance

The original engines and policy scripts are preserved under `code/original/`, their executions under `validation/original_runs/` and prior archived inputs/results under `data/legacy/`. They are provenance artifacts with different waiting semantics, not the primary study.

## Licence and citation

Code is MIT (`LICENSE-CODE`); data are CC BY 4.0 (`LICENSE`). `CITATION.cff` identifies the five authors and the frozen package version. Confidential source documents are not distributed; every numerical input used is public.
