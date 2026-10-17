"""Solve reduced hard-wait instances; export every incumbent for physical audit.

A time limit is not an infeasibility result. Full-day hard-wait feasibility
within 24 hours is not inferred from these reduced instances.
"""
import json
from pathlib import Path
from cell_model import Cell, Instance
from exact_study import ExactModel
from run_campaign import write_csv

ROOT=Path(__file__).resolve().parents[1]
MULT={k:1 for k in range(1,15)};MULT.update({8:2,11:2})
FAMILIES={'HW3':[('HC',0.),('SVOC',375.),('SVOC_E',750.)],
          'HW5':[('HC',0.),('SVOC',180.),('SVOC_E',360.),('HC',540.),('SVOC',720.)],
          'HW8':[(t,j*180.) for j,t in enumerate(['HC','SVOC','SVOC_E','HC','SVOC','HC','SVOC_E','HC'])]}

def main():
    rows=[]
    for name,spec in FAMILIES.items():
        cell=Cell(ROOT/'instances/cs6_fullday_config.json',MULT);inst=Instance(cell,spec)
        model=ExactModel(inst,hard_wait=True)
        prefix=ROOT/'validation/hard_wait'/name
        status,solstatus,opt,dual,nv,nc=model.solve(time_limit=120,threads=1,solver_name='scip',export_prefix=prefix,require_optimal=False)
        trace=getattr(model,'trace',None)
        rows.append({'instance':name,'n_samples':len(spec),'status':model.proof['status'],
                     'incumbent_s':trace['kpi']['makespan_s'] if trace else '',
                     'dual_bound_s':model.proof['dual_bound'],'gap':model.proof['gap'],
                     'max_wait_s':trace['kpi']['max_wait_s'] if trace else '',
                     'audited_primal':bool(trace),'hard_wait_feasible':bool(trace and trace['kpi']['max_wait_s']<=300+1e-5)})
        (prefix.parent/(name+'_instance.json')).write_text(json.dumps({'multiplicities':MULT,'samples':[{'id':s.sid,'type':s.stype,'release_s':s.release} for s in inst.samples]},indent=2)+'\n')
        print(name,rows[-1],flush=True)
    write_csv(ROOT/'data/hard_wait_results.csv',rows)
if __name__=='__main__':main()
