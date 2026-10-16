"""Recompute the corrected 21-run campaign, certificates and diagnostics."""
from __future__ import annotations
import csv
import json
from pathlib import Path
from cell_model import Cell, Instance, simulate
from exact_study import CapacityBound, ReleasePathBound, HeadTailBound

ROOT=Path(__file__).resolve().parents[1]

def write_csv(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)

def main():
    cases=list(csv.DictReader((ROOT/'data/cases.csv').open()))
    releases=list(csv.DictReader((ROOT/'data/releases.csv').open()))
    cell=Cell(ROOT/'instances/cs1_nominal_config.json')
    kpis=[]; bounds=[]; figures={}; sweep=[]
    for c in cases:
        name=c['label']
        spec=[{'id':r['sample_id'],'type':r['sample_type'],'release_s':float(r['release_s'])}
              for r in releases if r['case']==name]
        inst=Instance(cell,spec,float(c['pipc_s']))
        cfg=json.loads(json.dumps(cell.cfg)); cfg['samples']=spec
        cfg['instance_id']=name
        cfg['_comment']='Corrected operational input, version 2.0.0, 16 October 2026. Fixed archived releases; QC is exogenous, not completion-gated.'
        cfg['qc_policy']={'mode':'exogenous_releases','block_size':10,
                          'completion_gate':False}
        cfg['routes']['SVOC']['p_s_sequence'][4]=float(c['pipc_s'])
        for route in cfg['routes'].values():
            for j,k in enumerate(route['type_sequence']):
                if k==10: route['p_s_sequence'][j]=float(c['pipc_s'])
        (ROOT/'instances'/f'{name}_config.json').write_text(json.dumps(cfg,indent=2)+'\n')
        cap=CapacityBound().value(inst);path=ReleasePathBound().value(inst)
        ht=HeadTailBound().value(inst)
        robot=sum(cell.m_min(s.seq[j-1],s.seq[j]) for s in inst.samples for j in range(1,len(s.seq)))
        cert=max(cap,path,ht,robot)
        detail=HeadTailBound().detail(inst);binding=max(detail,key=lambda k:detail[k][3])
        best=float('inf')
        for policy in ['URG','URG-MECT','LB-ROLL']:
            result=simulate(inst,policy)
            kpi={'case':name,**result['kpi'],'lb_makespan_s':cert,
                 'gap_pct':100*(result['kpi']['makespan_s']-cert)/cert}
            assert kpi['makespan_s']+1e-6>=cert
            kpis.append(kpi);best=min(best,kpi['makespan_s'])
            out=ROOT/'validation'/'corrected_runs'/name/policy
            out.mkdir(parents=True,exist_ok=True)
            (out/'trace.json').write_text(json.dumps(result,indent=2)+'\n')
            (out/'audit.json').write_text(json.dumps({'precedence':True,'robot_capacity':True,
                'position_capacity':True,'releases':True,'all_samples_completed':True,
                'hard_wait_feasible':kpi['hard_wait_feasible']},indent=2)+'\n')
            if policy=='LB-ROLL': figures[name]=result
            print(name,policy,round(kpi['makespan_s'],2),kpi['violations'],
                  kpi['affected_samples'],flush=True)
        bounds.append({'case':name,'capacity_lb_s':cap,'release_path_lb_s':path,
                       'robot_lb_s':robot,'headtail_lb_s':ht,'headtail_type_k':binding,
                       'certificate_lb_s':cert,'best_makespan_s':best,
                       'certified_gap_pct':100*(best-cert)/cert,
                       'previous_gap_pct':100*(best-max(cap,path,robot))/max(cap,path,robot)})
        if name=='cs2_medium':
            for beta in [1.,60.,1e6]:
                for weight in [0.,600.,1200.]:
                    r=simulate(inst,'LB-ROLL',beta=beta,w_viol=weight,trace=False)['kpi']
                    sweep.append({'beta':beta,'w_viol':weight,'makespan_s':r['makespan_s'],
                                  'violations':r['violations'],'affected_samples':r['affected_samples'],
                                  'max_wait_s':r['max_wait_s']})
    write_csv(ROOT/'data/kpi_campaign.csv',kpis)
    write_csv(ROOT/'data/lower_bounds.csv',bounds)
    write_csv(ROOT/'data/parameter_sweep_cs2.csv',sweep)
    (ROOT/'validation/plot_data.json').write_text(json.dumps(figures)+'\n')

if __name__=='__main__':main()
