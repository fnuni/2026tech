"""Verify released traces, bounds, exact proofs and original provenance."""
import csv
import hashlib
import json
from pathlib import Path
from cell_model import Cell, Instance, audit
from run_campaign import write_csv

ROOT=Path(__file__).resolve().parents[1]

def main():
    rows=list(csv.DictReader((ROOT/'data/kpi_campaign.csv').open()))
    assert len(rows)==21
    checked=[]
    for row in rows:
        name=row['case'];policy=row['policy']
        cfg=ROOT/'instances'/f'{name}_config.json'
        cell=Cell(cfg)
        pipc=cell.cfg['routes']['SVOC']['p_s_sequence'][4]
        inst=Instance(cell,cell.cfg['samples'],pipc)
        result=json.loads((ROOT/'validation/corrected_runs'/name/policy/'trace.json').read_text())
        audit(inst,result)
        waits=[a['technological_wait_s'] for a in result['tasks'] if a['from_stage']>0]
        bad={a['sample'] for a in result['tasks'] if a['technological_wait_s']>300+1e-8}
        assert int(row['violations'])==sum(w>300+1e-8 for w in waits)
        assert int(row['affected_samples'])==len(bad)
        assert abs(float(row['makespan_s'])-max(result['completions'].values()))<1e-6
        assert float(row['makespan_s'])+1e-6>=float(row['lb_makespan_s'])
        checked.append({'case':name,'policy':policy,'operational_audit':'passed',
                        'hard_wait_feasible':not bad})
    for proof in (ROOT/'validation/exact').glob('*_proof.json'):
        obj=json.loads(proof.read_text())
        assert obj['status']=='optimal' and obj['gap']==0
        assert abs(obj['primal_bound']-obj['dual_bound'])<1e-6
    assert len(list((ROOT/'validation/exact').glob('*_proof.json')))==5
    old=list(csv.DictReader((ROOT/'data/legacy/kpi_campaign.csv').open()))
    original=json.loads((ROOT/'validation/original_campaign.json').read_text())
    compare=[]
    for fresh in original:
        archived=next(r for r in old if r['case']==fresh['case'] and r['policy']==fresh['policy'])
        compare.append({'case':fresh['case'],'policy':fresh['policy'],
                        'archived_makespan_s':float(archived['makespan_s']),
                        'fresh_original_makespan_s':fresh['makespan_s'],
                        'delta_s':round(fresh['makespan_s']-float(archived['makespan_s']),3),
                        'archived_violations':int(archived['violations']),
                        'fresh_original_violations':fresh['n_violations']})
    write_csv(ROOT/'validation/provenance_comparison.csv',compare)
    write_csv(ROOT/'validation/operational_audits.csv',checked)
    manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ['instances','code','data'] for p in sorted((ROOT/folder).rglob('*'))
              if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    (ROOT/'validation/input_code_sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
    summary={'corrected_runs':21,'operational_audits_passed':21,
             'hard_wait_feasible_runs':sum(x['hard_wait_feasible'] for x in checked),
             'proven_optima':5,'original_runs':len(original)}
    (ROOT/'validation/summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))

if __name__=='__main__':main()
