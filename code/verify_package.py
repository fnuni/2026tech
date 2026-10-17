"""Independent timestamp/KPI audit and consistency checks for the frozen study.

The physical audit does not replay or certify policy scores. The URG-MECT
control checks equality of complete task sequences with URG.
"""
import argparse,csv,hashlib,json,math
from pathlib import Path
from cell_model import Cell,Instance,audit
from run_campaign import write_csv
ROOT=Path(__file__).resolve().parents[1]
def read(n):return list(csv.DictReader((ROOT/'data'/n).open()))
def load(p):return json.loads(p.read_text())
def instance(name,options=None):
    c=Cell(ROOT/'instances'/f'{name}_config.json',**(options or {}))
    return Instance(c,c.cfg['samples'],float(c.cfg['routes']['SVOC']['p_s_sequence'][4]))
def compare(row,metrics):
    for key,value in metrics.items():
        if key not in row:continue
        if isinstance(value,bool):assert row[key]==str(value),(key,row[key],value)
        else: assert math.isclose(float(row[key]),float(value),abs_tol=1e-5),(key,row[key],value)
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze',action='store_true',help='Write a SHA-256 manifest after all content audits')
    parser.add_argument('--check-hashes',action='store_true',help='Check the frozen artifact bytes before regenerating outputs')
    args=parser.parse_args()
    if args.check_hashes:
        for name,digest in load(ROOT/'validation/input_code_sha256.json').items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    checked=[];results={}
    for row in read('kpi_campaign.csv'):
        name=row['case'];policy=row['policy'];inst=instance(name)
        r=load(ROOT/'validation/corrected_runs'/name/policy/'trace.json');m=audit(inst,r);compare(row,m)
        assert float(row['makespan_s'])+1e-5>=float(row['certificate_lb_s'])
        checked.append({'family':'primary','case':name,'policy':policy,'operational_audit':'passed','hard_wait_feasible':m['hard_wait_feasible']});results[name,policy]=r
    # Recalculate certificate components directly from explicit routes and
    # movement minima, independently of the bound-class implementation.
    for row in read('lower_bounds.csv'):
        inst=instance(row['case']);work={};heads={};tails={};robot=0.;path=0.
        for ss in inst.samples:
            moves=[min(inst.cell.m(u,v) for u in inst.cell.by_k[ss.seq[j-1]]
                       for v in inst.cell.by_k[ss.seq[j]]) for j in range(1,len(ss.seq))]
            robot+=sum(moves);total=sum(ss.p)+sum(moves)
            path=max(path,ss.release+total);prefix=0.
            for j,k in enumerate(ss.seq):
                if j:prefix+=ss.p[j-1]+moves[j-1]
                if ss.p[j]>0:
                    work[k]=work.get(k,0.)+ss.p[j]
                    heads[k]=min(heads.get(k,float('inf')),ss.release+prefix)
                    tails[k]=min(tails.get(k,float('inf')),total-prefix-ss.p[j])
        cap=max(w/len(inst.cell.by_k[k]) for k,w in work.items())
        ht=max(heads[k]+w/len(inst.cell.by_k[k])+tails[k] for k,w in work.items())
        compare(row,{'capacity_lb_s':cap,'release_path_lb_s':path,'robot_lb_s':robot,
                     'headtail_lb_s':ht,'certificate_lb_s':max(cap,path,robot,ht)})
    for name in {r['case'] for r in read('kpi_campaign.csv')}:
        a=results[name,'URG'];b=results[name,'URG-MECT']
        assert [(x['sample'],x['destination'],x['pickup_s']) for x in a['tasks']]==[(x['sample'],x['destination'],x['pickup_s']) for x in b['tasks']]
    for filename,family in [('ablations.csv','ablations'),('physical_sensitivity.csv','physical'),('threshold_sensitivity.csv','thresholds'),('admission_sensitivity.csv','admission')]:
        for row in read(filename):
            name=row['case'];policy=row['policy'];inst=instance(name)
            if family=='ablations':p=ROOT/'validation'/family/name/(row['variant']+'.json')
            elif family=='physical':p=ROOT/'validation'/family/name/policy/(row['variant']+'.json')
            elif family=='thresholds':p=ROOT/'validation'/family/name/policy/(str(int(float(row['threshold_s'])))+'.json')
            else:p=ROOT/'validation'/family/name/(row['admission_cap']+'.json')
            r=load(p)
            if family=='physical':inst=instance(name,r['cell_parameters']);r=r['trace']
            expected_budget=float(row['threshold_s']) if family=='thresholds' else 300.
            m=audit(inst,r,wait_budget=expected_budget);compare(row,m)
            checked.append({'family':family,'case':name,'policy':policy,'operational_audit':'passed','hard_wait_feasible':m['hard_wait_feasible']})
    for folder in ['exact','hard_wait']:
        for p in sorted((ROOT/'validation'/folder).glob('*_status.json')):
            prefix=p.name.replace('_status.json','');status=load(p);spec=load(p.with_name(prefix+'_input.json'))
            cell=Cell(ROOT/'instances/cs1_nominal_config.json',{int(k):v for k,v in spec['multiplicities'].items()})
            inst=Instance(cell,spec['samples']);r=load(p.with_name(prefix+'_trace.json'));m=audit(inst,r)
            assert math.isclose(m['makespan_s'],status['primal_bound'],abs_tol=1e-5)
            assert status['dual_bound']<=status['primal_bound']+1e-5
            if folder=='exact':assert status['status']=='optimal' and status['gap']<=1e-8
            else:assert m['max_wait_s']<=300+1e-5
            for ext in ['.lp','.sol','.log','_variables.json']:assert p.with_name(prefix+ext).stat().st_size>0
    # Independently recompute the two central breakdown counts from events.
    r=results['cs6_fullday','URG'];viol=[]
    for a in r['tasks']:
        if a['from_stage']:
            op=r['operations'][a['sample']][a['from_stage']-1]
            if a['pickup_s']-op['completion_s']>300+1e-8:viol.append(a)
    assert len(viol)==114
    assert sum(r['operations'][a['sample']][a['to_stage']-1]['type_k']==10 for a in viol)==100
    write_csv(ROOT/'validation/operational_audits.csv',checked)
    if args.freeze:
        files=[p for folder in ['instances','code','data','tables','figures','validation']
               for p in sorted((ROOT/folder).rglob('*')) if p.is_file()]
        files += [ROOT/name for name in ['README.md','CITATION.cff','requirements.txt','LICENSE','LICENSE-CODE']]
        def distributable(p):
            if '__pycache__' in p.parts or p.suffix=='.pyc' or p.name=='.DS_Store':return False
            if p.name in ['input_code_sha256.json','summary.json']:return False
            if 'original_runs' in p.parts and (p.suffix=='.py' or p.name in ['simulation.log','stdout.log']):return False
            return True
        manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in files if distributable(p)}
        (ROOT/'validation/input_code_sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
    summary={'primary_runs':len(read('kpi_campaign.csv')),'distinct_policy_scenario_pairs':60,
             'equivalent_urg_controls':15,'ablations':len(read('ablations.csv')),
             'physical_sensitivity_runs':len(read('physical_sensitivity.csv')),
             'threshold_sensitivity_runs':len(read('threshold_sensitivity.csv')),
             'admission_sensitivity_runs':len(read('admission_sensitivity.csv')),
             'audited_simulation_traces':len(checked),'operational_optima':5,
             'reduced_hard_wait_incumbents':3,'reduced_hard_wait_optima':2,
             'cs6_urg_violations_before_pipet_c':100,'cs6_urg_violations':114}
    (ROOT/'validation/summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
