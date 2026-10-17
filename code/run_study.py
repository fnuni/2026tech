"""Reproduce the matched deterministic study, diagnostics and sensitivities.

No parameter is fitted to observed policy performance. Unmeasured motion and
auxiliary-service settings are stress tests, not calibrated confidence ranges.
"""
from __future__ import annotations
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
from cell_model import Cell, Instance, simulate, audit
from exact_study import CapacityBound, ReleasePathBound, HeadTailBound
from run_campaign import write_csv

ROOT=Path(__file__).resolve().parents[1]
POLICIES=['URG','SRPT','BNA','LB-LA','URG-MECT']
ABLATIONS={'no_starvation':{'starvation':False},'no_new_violation':{'w_viol':0.},
           'no_overshoot':{'w_over':0.},'no_wait_penalties':{'w_viol':0.,'w_over':0.},
           'no_slack_band':{'slack_band':False},'selected_wait':{'self_wait':True}}

def dump(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,separators=(',',':'))+'\n')

def readcsv(name):return list(csv.DictReader((ROOT/'data'/name).open()))

def certificate(inst):
    c=CapacityBound().value(inst);p=ReleasePathBound().value(inst);h=HeadTailBound().value(inst)
    robot=sum(inst.cell.m_min(s.seq[j-1],s.seq[j]) for s in inst.samples for j in range(1,len(s.seq)))
    return {'capacity_lb_s':c,'release_path_lb_s':p,'robot_lb_s':robot,'headtail_lb_s':h,
            'certificate_lb_s':max(c,p,h,robot)}

def quant(x,p):return float(np.percentile(x,p)) if x else 0.

def diagnostics(inst,result,case,policy):
    smap={s.sid:s for s in inst.samples};by_transition=defaultdict(list);by_class=defaultdict(list)
    affected=defaultdict(set);W=result.get('parameters',{}).get('wait_budget_s',300.)
    for task in result['tasks']:
        if task['from_stage']==0:continue
        s=smap[task['sample']];j=task['from_stage']
        w=task['pickup_s']-result['operations'][s.sid][j-1]['completion_s']
        by_transition[(s.seq[j],s.seq[j+1])].append((s.sid,w))
        by_class[s.stype].append(w)
        if w>W+1e-8: affected[s.stype].add(s.sid)
    trans=[]
    for (origin,dest),items in sorted(by_transition.items()):
        ws=[w for _,w in items];bad=[w-W for w in ws if w>W+1e-8]
        trans.append({'case':case,'policy':policy,'origin_k':origin,'destination_k':dest,
                      'transitions':len(ws),'violations':len(bad),'p50_wait_s':quant(ws,50),
                      'p95_wait_s':quant(ws,95),'p99_wait_s':quant(ws,99),
                      'max_wait_s':max(ws),'mean_excess_s':sum(bad)/len(bad) if bad else 0.,
                      'max_excess_s':max(bad,default=0.)})
    first={a['sample']:a['pickup_s'] for a in result['tasks'] if a['from_stage']==0}
    classes=[]
    for typ in sorted(by_class):
        ss=[s for s in inst.samples if s.stype==typ];ws=by_class[typ]
        flow=[result['completions'][s.sid]-s.release for s in ss]
        ext=[first[s.sid]-s.release for s in ss]
        classes.append({'case':case,'policy':policy,'sample_type':typ,'n_samples':len(ss),
                        'affected_samples':len(affected[typ]),'violations':sum(w>W+1e-8 for w in ws),
                        'p50_wait_s':quant(ws,50),'p95_wait_s':quant(ws,95),
                        'p99_wait_s':quant(ws,99),'max_wait_s':max(ws),
                        'mean_flow_s':sum(flow)/len(flow),'p95_flow_s':quant(flow,95),
                        'mean_external_s':sum(ext)/len(ext),'p95_external_s':quant(ext,95)})
    events=defaultdict(int)
    for s in inst.samples:events[s.release]+=1;events[first[s.sid]]-=1
    backlog=peak=0
    for t,n in sorted(events.items()):backlog+=n;peak=max(peak,backlog)
    metrics={'case':case,'policy':policy,'max_external_backlog':peak,
             'mean_external_s':sum(first[s.sid]-s.release for s in inst.samples)/inst.n,
             'p95_external_s':quant([first[s.sid]-s.release for s in inst.samples],95),
             'p50_wait_s':quant([w for ws in by_class.values() for w in ws],50),
             'p95_wait_s':quant([w for ws in by_class.values() for w in ws],95),
             'p99_wait_s':quant([w for ws in by_class.values() for w in ws],99)}
    return trans,classes,metrics

def inputs():
    cases=readcsv('cases.csv');releases=readcsv('releases.csv')
    basecfg=json.loads((ROOT/'instances/cs6_fullday_config.json').read_text())
    base=basecfg['samples']
    added=[('cs8_day180',180.,'alternating',False,600.),
           ('cs9_day60',60.,'alternating',False,600.),
           ('cs10_m2first',180.,'M2-first',False,600.),
           ('cs11_hcfirst',180.,'HC-first',False,600.),
           ('cs12_m2heavy',180.,'alternating',True,600.),
           ('cs13_day480',375.,'alternating',False,480.),
           ('cs14_day180_480',180.,'alternating',False,480.)]
    all_inputs=[];design=[]
    for c in cases:
        spec=[{'id':r['sample_id'],'type':r['sample_type'],'release_s':float(r['release_s'])}
              for r in releases if r['case']==c['label']]
        all_inputs.append((c['label'],spec,float(c['pipc_s'])))
        design.append({'case':c['label'],'n_samples':len(spec),'interval_s':float(c['interval_s']),
                       'order':'released-input','mix':'standard','pipc_s':float(c['pipc_s'])})
    for name,pace,order,heavy,pipc in added:
        spec=[dict(x) for x in base]
        slots=[i for i,x in enumerate(spec) if x['type'] in ('HC','SVOC','SVOC_E')]
        types=[spec[i]['type'] for i in slots]
        if heavy:
            # Keep all releases and control positions fixed; replace 40 HC
            # routes by 28 SVOC and 12 emulsion routes at evenly spaced slots.
            hc=[j for j,t in enumerate(types) if t=='HC']
            selected=[hc[int(j*len(hc)/40)] for j in range(40)]
            for j,idx in enumerate(selected):types[idx]='SVOC_E' if j%10<3 else 'SVOC'
        if order!='alternating':
            types.sort(key=lambda t: (t=='HC') if order=='M2-first' else (t!='HC'))
        for idx,typ in zip(slots,types):spec[idx]['type']=typ
        for item in spec:item['release_s']*=pace/375.
        all_inputs.append((name,spec,pipc))
        design.append({'case':name,'n_samples':230,'interval_s':pace,'order':order,
                       'mix':'M2-heavy' if heavy else 'standard','pipc_s':pipc})
    # Exact ten-block replication of CS2 isolates volume while retaining
    # each block's routes, control releases and 180 s analytical cadence.
    tile=next(spec for name,spec,_ in all_inputs if name=='cs2_medium')
    tiled=[{'id':f's{b*len(tile)+j+1}','type':x['type'],
            'release_s':x['release_s']+b*3600.}
           for b in range(10) for j,x in enumerate(tile)]
    all_inputs.append(('cs15_tiled180',tiled,600.))
    design.append({'case':'cs15_tiled180','n_samples':230,'interval_s':180.,
                   'order':'tiled-CS2','mix':'standard','pipc_s':600.})
    for name,spec,pipc in all_inputs:
        cfg=json.loads(json.dumps(basecfg));cfg['samples']=spec;cfg['instance_id']=name
        cfg['_comment']='Deterministic public input; QC releases are exogenous.'
        for route in cfg['routes'].values():
            for j,k in enumerate(route['type_sequence']):
                if k==10:route['p_s_sequence'][j]=pipc
        dump(ROOT/'instances'/f'{name}_config.json',cfg)
    write_csv(ROOT/'data/expanded_cases.csv',design)
    return all_inputs

def main():
    all_inputs=inputs();kpis=[];bounds=[];stages=[];classes=[];operating=[];runs={};insts={};plots={}
    for name,spec,pipc in all_inputs:
        cell=Cell(ROOT/'instances'/f'{name}_config.json');inst=Instance(cell,spec,pipc);insts[name]=inst
        bound=certificate(inst);best=float('inf')
        for policy in POLICIES:
            result=simulate(inst,policy,trace=False);runs[name,policy]=result
            values=audit(inst,result);assert values['makespan_s']+1e-5>=bound['certificate_lb_s']
            row={'case':name,**result['kpi'],**bound,
                 'gap_pct':100*(values['makespan_s']/bound['certificate_lb_s']-1),
                 'excess_over_bound_s':values['makespan_s']-bound['certificate_lb_s']}
            kpis.append(row);best=min(best,values['makespan_s'])
            path=ROOT/'validation/corrected_runs'/name/policy
            dump(path/'trace.json',result);dump(path/'audit.json',{'timestamp_reconstruction':'passed','recomputed_kpi':values})
            tr,cl,op=diagnostics(inst,result,name,policy);stages+=tr;classes+=cl;operating.append(op)
            if policy=='LB-LA' and name in ('cs1_nominal','cs6_fullday'):plots[name]=result
            print(name,policy,round(values['makespan_s'],2),values['violations'],values['affected_samples'],flush=True)
        assert [(a['sample'],a['destination']) for a in runs[name,'URG']['tasks']]==[(a['sample'],a['destination']) for a in runs[name,'URG-MECT']['tasks']]
        bounds.append({'case':name,**bound,'best_makespan_s':best,
                       'certified_gap_pct':100*(best/bound['certificate_lb_s']-1)})
    for file,rows in [('kpi_campaign.csv',kpis),('lower_bounds.csv',bounds),('waiting_by_transition.csv',stages),
                       ('waiting_by_class.csv',classes),('operating_metrics.csv',operating)]:write_csv(ROOT/'data'/file,rows)
    dump(ROOT/'validation/plot_data.json',plots)
    ablations=[]
    for name in ['cs2_medium','cs6_fullday','cs9_day60']:
        inst=insts[name];ref=simulate(inst,'LB-LA',trace=False,counterfactuals=ABLATIONS)
        counts={key:sum(tuple(d['counterfactual_selected'][key])!=(d['selected']['sample'],d['selected']['destination']) for d in ref['decisions']) for key in ABLATIONS}
        for variant,options in [('full',{})]+list(ABLATIONS.items()):
            result=ref if variant=='full' else simulate(inst,'LB-LA',trace=False,**options)
            changed=sum((a['sample'],a['destination'])!=(b['sample'],b['destination']) for a,b in zip(ref['tasks'],result['tasks']))
            row={'case':name,'variant':variant,**result['kpi'],'local_changed_decisions':counts.get(variant,0),
                 'sequence_changed_epochs':changed,'reference_epochs':len(ref['tasks'])}
            ablations.append(row)
            dump(ROOT/'validation/ablations'/name/(variant+'.json'),result)
            print('ablation',name,variant,counts.get(variant,0),changed,flush=True)
    write_csv(ROOT/'data/ablations.csv',ablations)
    sensitivity=[]
    physical=[('separated_nominal',{'empty_dwell_s':14.27,'loaded_dwell_s':14.27}),
              ('empty_no_dwell',{'empty_dwell_s':0.,'loaded_dwell_s':14.27}),
              ('empty_half_dwell',{'empty_dwell_s':7.135,'loaded_dwell_s':14.27}),
              ('loaded_dwell_minus20',{'empty_dwell_s':14.27,'loaded_dwell_s':11.416}),
              ('loaded_dwell_plus20',{'empty_dwell_s':14.27,'loaded_dwell_s':17.124}),
              ('traverse_half_speed',{'empty_dwell_s':14.27,'loaded_dwell_s':14.27,'empty_speed_m_s':.75,'loaded_speed_m_s':.75}),
              ('traverse_double_speed',{'empty_dwell_s':14.27,'loaded_dwell_s':14.27,'empty_speed_m_s':3.,'loaded_speed_m_s':3.})]
    for name in ['cs6_fullday','cs8_day180']:
        for variant,options in physical+[(f'auxiliary_{s}s',{'auxiliary_s':s}) for s in (5.,10.,15.,30.)]:
            inst0=insts[name];cell=Cell(ROOT/'instances'/f'{name}_config.json',**options)
            inst=Instance(cell,cell.cfg['samples'])
            for policy in ['URG','LB-LA']:
                r=simulate(inst,policy,trace=False)
                sensitivity.append({'case':name,'variant':variant,**r['kpi'],
                                    'day_margin_s':86400-r['kpi']['makespan_s']})
                dump(ROOT/'validation/physical'/name/policy/(variant+'.json'),{'cell_parameters':options,'trace':r})
            print('physical',name,variant,flush=True)
    write_csv(ROOT/'data/physical_sensitivity.csv',sensitivity)
    thresholds=[]
    for name in ['cs6_fullday','cs9_day60']:
        for W in [150.,300.,600.,900.]:
            for policy in ['URG','LB-LA','BNA']:
                r=simulate(insts[name],policy,wait_budget=W,trace=False)
                fixed=runs[name,policy]
                ws=[(a['sample'],a['pickup_s']-fixed['operations'][a['sample']][a['from_stage']-1]['completion_s']) for a in fixed['tasks'] if a['from_stage']>0]
                thresholds.append({'case':name,'threshold_s':W,**r['kpi'],
                                   'fixed_schedule_violations':sum(w>W+1e-8 for _,w in ws),
                                   'fixed_schedule_affected':len({sid for sid,w in ws if w>W+1e-8})})
                dump(ROOT/'validation/thresholds'/name/policy/(str(int(W))+'.json'),r)
            print('threshold',name,W,flush=True)
    write_csv(ROOT/'data/threshold_sensitivity.csv',thresholds)
    dump(ROOT/'validation/study_design.json',{'policies':POLICIES,'ablation_settings':ABLATIONS,
          'physical_settings':dict(physical),'auxiliary_s_per_transfer':[5,10,15,30],
          'wait_thresholds_s':[150,300,600,900],'interpretation':'Deterministic stress tests; no measured uncertainty intervals or chemical validation.'})
    from run_admission_sweep import main as admission_sweep
    admission_sweep()
    print('Complete study:',len(kpis),'primary runs,',len(ablations),'ablation rows,',len(sensitivity),'physical runs,',len(thresholds),'threshold runs',flush=True)

if __name__=='__main__':main()
