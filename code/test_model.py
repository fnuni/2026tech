"""Regression checks for the timing semantics underlying the paper."""
import tempfile
import unittest
from pathlib import Path
from cell_model import Cell, Instance, simulate
from exact_study import CapacityBound, ReleasePathBound, HeadTailBound

ROOT=Path(__file__).resolve().parents[1]

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.cell=Cell(ROOT/'instances/cs1_nominal_config.json')

    def test_single_sample_wait_excludes_loaded_travel(self):
        inst=Instance(self.cell,[('HC',0.)])
        r=simulate(inst)
        for a in r['tasks'][1:]:
            self.assertLessEqual(a['technological_wait_s'],a['empty_s']+1e-6)
        self.assertEqual(r['kpi']['affected_samples'],0)
        self.assertTrue(r['kpi']['hard_wait_feasible'])

    def test_external_queues_are_not_physical_storage_positions(self):
        inst=Instance(self.cell,[('HC',0.)]*8)
        r=simulate(inst)
        self.assertEqual(len(r['completions']),8)
        for task in r['tasks']:
            if task['from_stage']==0:
                self.assertEqual(task['technological_wait_s'],0.)

    def test_bounds_and_all_policies_are_audited(self):
        inst=Instance(self.cell,[('HC',0.),('SVOC',180.),('SVOC_E',360.)])
        lb=max(b.value(inst) for b in [CapacityBound(),ReleasePathBound(),HeadTailBound()])
        for policy in ['URG','URG-MECT','LB-ROLL']:
            r=simulate(inst,policy)
            self.assertGreaterEqual(r['kpi']['makespan_s']+1e-6,lb)
            self.assertLessEqual(r['kpi']['affected_samples'],r['kpi']['violations'])

    def test_equal_durations_do_not_remove_starvation_preference(self):
        # Counterexample to the old Proposition 8(iii), W=300, dur=30.
        less_overdue=30+1200+120*(302+30-300)/300
        more_overdue=30+1230+120*(301+30-300)/300
        self.assertLess(less_overdue,more_overdue)

if __name__=='__main__':unittest.main()
