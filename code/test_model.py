"""Regression checks for the timing semantics underlying the paper."""
import tempfile
import copy
import json
import unittest
from pathlib import Path
from cell_model import Cell, Instance, simulate, audit
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

class AuditTamperTests(unittest.TestCase):
    def setUp(self):
        self.inst=Instance(Cell(ROOT/'instances/cs1_nominal_config.json'),[('HC',0.),('SVOC',0.)])
        self.trace=simulate(self.inst)

    def reject(self,mutate):
        changed=copy.deepcopy(self.trace);mutate(changed)
        with self.assertRaises((AssertionError,KeyError)):
            audit(self.inst,changed)

    def test_zeroed_saved_waits_are_rejected(self):
        def change(r):
            for a in r['tasks']: a['technological_wait_s']=0.
        self.reject(change)

    def test_changed_threshold_is_rejected(self):
        self.reject(lambda r:r['parameters'].update(wait_budget_s=900.))

    def test_instance_respects_released_extraction_duration(self):
        cell=Cell(ROOT/'instances/cs7_pipc480_config.json')
        sample=Instance(cell,[('SVOC',0.)]).samples[0]
        self.assertEqual(sample.p[sample.seq.index(10)],480.)

    def test_false_operation_station_is_rejected(self):
        self.reject(lambda r:r['operations']['s1'][0].update(station='v1'))

    def test_false_processing_start_is_rejected(self):
        self.reject(lambda r:r['operations']['s1'][0].update(start_s=r['operations']['s1'][0]['start_s']+1))

    def test_false_clearance_is_rejected(self):
        self.reject(lambda r:r['operations']['s1'][0].update(clearance_s=r['operations']['s1'][0]['clearance_s']+1))

    def test_false_final_completion_is_rejected(self):
        self.reject(lambda r:r['completions'].update(s1=r['completions']['s1']+1))

    def test_false_violation_kpi_is_rejected(self):
        self.reject(lambda r:r['kpi'].update(violations=r['kpi']['violations']+1))

    def test_false_external_wait_is_rejected(self):
        self.reject(lambda r:r['tasks'][0].update(external_wait_s=77.))

    def test_false_loaded_duration_is_rejected(self):
        self.reject(lambda r:r['tasks'][0].update(loaded_s=0.))

    def test_distinct_baselines_change_decisions(self):
        cell=Cell(ROOT/'instances/cs6_fullday_config.json')
        inst=Instance(cell,cell.cfg['samples'])
        signatures={p:tuple((a['sample'],a['destination']) for a in simulate(inst,p,trace=False)['tasks'])
                    for p in ['URG','SRPT','BNA','LB-LA']}
        self.assertEqual(len(set(signatures.values())),4)

    def test_motion_and_auxiliary_accounting(self):
        cell=Cell(ROOT/'instances/cs1_nominal_config.json',empty_dwell_s=0.,auxiliary_s=5.)
        r=simulate(Instance(cell,[('HC',0.)]))
        self.assertAlmostEqual(r['kpi']['robot_auxiliary_s'],5*r['kpi']['robot_tasks'])
        self.assertTrue(r['kpi']['hard_wait_feasible'])

if __name__=='__main__':unittest.main()
