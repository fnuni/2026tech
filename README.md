# Robotic water-sample preparation cell: replication package

Version **2.0.2**, 16 October 2026, for *Design and Scheduling of an Automated Water-Sample Preparation Cell: Capacity Diagnosis and Pre-Commissioning Assessment*, prepared for Applied Sciences.

Repository: https://github.com/fnuni/2026apsc

The corrected study distinguishes operational schedules from schedules respecting hard technological waiting limits. Makespan gaps certify only the operational problem. The full-day URG run records 114 violated budgets affecting 102 distinct samples. Reanalysis and chemical recovery are not simulated.

## Reproduce the corrected study

Verified with Python 3.10.10, PuLP 2.9.0, PySCIPOpt 5.2.1 and SCIP 9.0.1 on macOS 15.7.2 ARM64. Exact optimisation uses one thread. Hardware timing fields need not match exactly.

```sh
python -m pip install -r requirements.txt
python code/test_model.py
python code/run_campaign.py
python code/exact_study.py
python code/make_figures.py
python code/verify_package.py
```

These commands read the released, explicit input lists and rewrite corrected outputs. The confidential industrial project plan is not needed. Original archived outputs remain under `data/legacy/`.

## Contents

- `code/cell_model.py`: corrected simulation, the three policy modes and independent operational constraint audit. The exact benchmark calls this same URG implementation.
- `code/exact_study.py`: operational MILP, capacity/path/head-tail bounds, shared urgency benchmark and SCIP primal/dual optimality certificates.
- `code/run_campaign.py`: 21 corrected runs and the nine-point CS2 parameter sweep.
- `code/make_figures.py`: four vector figures and PNG previews from corrected CSVs and audited traces.
- `code/test_model.py`, `code/verify_package.py`: timing regression checks and cross-file consistency verification.
- `code/original/`: six recovered original modules: v2/v3 engines, greedy, CPR-LA and rollout solvers, and their legacy lower-bound module.
- `code/run_original_campaign.py`: independent real executions of all 21 original simulator/policy combinations.
- `instances/`: explicit releases, routes, positions in metres and the 33 by 33 movement matrix.
- `data/`: corrected campaign and exact results, certificate components, sensitivity grid, case definitions, releases and aggregate process data.
- `validation/corrected_runs/`: 21 complete robot/operation/decision traces and operational audit outputs.
- `validation/exact/`: five optimality proofs including primal bound, dual bound, zero solver gap and status.
- `validation/original_campaign.json`, `validation/original_runs/`: fresh original-code outputs, kept separately.
- `validation/provenance_comparison.csv`: differences between fresh original runs and the previously archived campaign.
- `interfaces/`: recovered original example controller/policy JSON records; not corrected campaign records.
- `figures/`: vector PDFs and PNG previews.

## Metric definitions

All times are seconds unless stated otherwise. Corrected trace route indices are zero-based, with stage zero representing the external entry interface.

| Field | Meaning |
|---|---|
| `makespan_s` | Last exit delivery from the common origin |
| `technological_wait_s` | Internal processing completion to actual pickup, including empty approach and excluding loaded travel |
| `external_wait_s` | Release to entry pickup, excluded from technological violations |
| `violations` | Internal stage waits strictly greater than 300 s |
| `affected_samples` | Distinct samples with at least one violated stage budget |
| `budget_compliant_samples` | Jobs without a model stage-wait violation; not a chemical-validity measure |
| `hard_wait_feasible` | True only when all internal waiting limits hold |
| `gap_pct` | 100 times (makespan - certificate) / certificate, for the operational problem |
| `robot_util_pct` | Empty plus loaded robot occupation / makespan |
| `mean_decision_ms` | In-process state/score computation, excluding process start-up and I/O |

The exact table's slack and heuristic excess instead use the proven optimum as denominator. They cannot directly partition the campaign gap.

Entry queues are external, destinations must be empty, and pickup and delivery are distinct events. Every corrected trace is audited for releases, precedence, robot capacity, physical-position occupancy and completion. None of the 21 corrected schedules satisfies all hard waiting limits.

## QC and movement scope

The full released mix contains 200 analytical jobs and ten jobs of each QC/blank class. Their explicit release lists do not enforce a QC completion after every ten analytical completions. The period 10 is used only in the anticipation score. Loaded and empty moves share a nominal dwell-plus-traverse matrix. Cleaning, consumables, auxiliary handling and reanalysis are excluded.

URG-MECT and URG coincide on idle interchangeable destinations with the default admission ordering. They form one effective baseline in this experiment. The starvation-augmented LB-ROLL score is heuristic and is not used as a certifying lower bound.

## Execute the recovered original code

```sh
python code/run_original_campaign.py
```

The runner uses isolated directories and the original subprocess interface, allowing three independent runs concurrently. Original waiting metrics include entry waiting and loaded travel and do not implement the corrected technological-wait definition. Fresh original rollout outputs also differ from several archived rollout rows. Both are retained explicitly; original outputs are not described as corrected or chemically compliant schedules. The original lower-bound module uses mean movements and estimated empty work, so its legacy `lb.json` values are not accepted as valid certificates; only the corrected minimum-loaded-work certificate is used in the article. Large original text logs and duplicate code copies inside run directories are omitted from Git, while aggregate outputs and JSON snapshots are retained.

## Licence and citation

Data retain CC BY 4.0 (`LICENSE`). Code uses MIT (`LICENSE-CODE`). `CITATION.cff` supplies the real five authors and versioned repository reference. Confidential source documents, instrument records, credentials and manuscript backups are not distributed.

## Version 2.0.1

The author affiliation is LabAnalysis Environmental Science S.r.l. The CS1 Gantt plot uses light grey for stirring, yellow for waiting, thicker red outlines for waits above 300 s, and an explicit legend. These presentation changes leave every numerical result unchanged.

## Version 2.0.2

The CS1 Gantt legend identifies all nine processing activities with distinct colours, plus the two waiting categories and loaded deliveries. Stirring remains light grey and waits above 300 s retain thick red borders. Numerical results are unchanged.
