"""Generate manuscript tables directly from audited CSV results."""
import argparse,csv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tables';OUT.mkdir(exist_ok=True)
def rows(name):return list(csv.DictReader((ROOT/'data'/name).open()))
def num(r,key,d=0):return f'{float(r[key]):,.{d}f}'
def table(name,caption,label,align,header,body,textwidth=False):
    text='\\begin{table}[!htbp]\n'
    text+='\\caption{'+caption+'\\label{'+label+'}}\n\\centering\n\\footnotesize\n'
    environment='tabular*' if textwidth else 'tabular'
    if textwidth:text+='\\setlength{\\tabcolsep}{3pt}\n'
    spec='{\\textwidth}{@{\\extracolsep{\\fill}}'+align+'@{}}' if textwidth else '{'+align+'}'
    text+='\\begin{'+environment+'}'+spec+'\n\\toprule\n'+header+'\\\\\n\\midrule\n'
    text+='\n'.join(' & '.join(map(str,row))+'\\\\' for row in body)
    text+='\n\\bottomrule\n\\end{'+environment+'}\n'
    text+='\\end{table}\n';(OUT/name).write_text(text)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--paper-dir',type=Path);args=parser.parse_args()
    cases=rows('expanded_cases.csv');names=[r['case'] for r in cases];labels={n:f'CS{i+1}' for i,n in enumerate(names)}
    data={(r['case'],r['policy']):r for r in rows('kpi_campaign.csv')}
    op={(r['case'],r['policy']):r for r in rows('operating_metrics.csv')}
    policies=['URG','SRPT','BNA','LB-LA']
    b=[]
    for r in cases:
        order={'released-input':'released sequence','alternating':'alternating','M2-first':'M2 first','HC-first':'HC first','tiled-CS2':'10 replicated CS2 blocks'}[r['order']]
        if r['mix']=='M2-heavy':order+='; M2-heavy'
        b.append([labels[r['case']],r['n_samples'],num(r,'interval_s'),order,num(r,'pipc_s')])
    table('tab_cases_expanded.tex','Deterministic case design. All cases use the same layout. CS8--CS15 contain 230 jobs; only the indicated input factor changes from the paired reference.','tab:cases','lrrlr','Case & Jobs & Interval (s) & Release order / mix & Pipet C (s)',b)
    b=[]
    for p in policies:
        r=data['cs6_fullday',p];o=op['cs6_fullday',p]
        b.append([p,f"{float(r['makespan_s'])/3600:.2f}",num(r,'violations'),num(r,'affected_samples'),
                  num(o,'p95_wait_s',1),num(o,'p95_external_s',1),num(o,'max_external_backlog'),f"{float(r['mean_flow_s'])/3600:.2f}"])
    table('tab_cs6_expanded.tex','Design-day performance at the 300 s waiting budget.','tab:kpi-cs6','lrrrrrrr','Policy & \\shortstack{$C_{\\max}$\\\\(h)} & Events & Affected & \\shortstack{Wait\\\\$P_{95}$ (s)} & \\shortstack{External\\\\$P_{95}$ (s)} & \\shortstack{Peak\\\\queue} & \\shortstack{Mean flow\\\\(h)}',b,True)
    b=[]
    for n in names:
        b.append([labels[n]]+[f"{float(data[n,p]['makespan_s'])/3600:.2f} / {data[n,p]['violations']} / {data[n,p]['affected_samples']}" for p in policies])
    table('tab_primary.tex','Policy comparison: makespan (h) / violation events / affected samples.','tab:summary','lrrrr','Case & URG & SRPT & BNA & LB-LA',b,True)
    b=[]
    for r in rows('lower_bounds.csv'):
        excess=float(r['best_makespan_s'])-float(r['certificate_lb_s'])
        b.append([labels[r['case']],num(r,'capacity_lb_s'),num(r,'robot_lb_s'),num(r,'release_path_lb_s'),num(r,'headtail_lb_s'),num(r,'best_makespan_s'),f'{excess:,.0f}',num(r,'certified_gap_pct',2)])
    table('tab_gaps_expanded.tex','Operational certificate components and best-policy gaps; times in seconds.','tab:gaps','lrrrrrrr','Case & Machine & \\shortstack{Loaded\\\\robot} & Path & Head/tail & \\shortstack{Best\\\\$C_{\\max}$} & Excess & \\shortstack{Gap\\\\(\\%)}',b,True)
    b=[]
    for r in rows('waiting_by_transition.csv'):
        if r['case']=='cs6_fullday' and r['policy']=='URG' and int(r['violations']):
            b.append([f"{r['origin_k']} $\\to$ {r['destination_k']}",r['transitions'],r['violations'],f"{100*int(r['violations'])/114:.1f}",num(r,'p50_wait_s',1),num(r,'p95_wait_s',1),num(r,'max_wait_s',1),num(r,'mean_excess_s',1)])
    table('tab_wait_stages.tex','Violating transitions in CS6-URG; module codes follow Table~\\ref{tab:modules}.','tab:wait-stages','lrrrrrrr','Transition & Count & Events & \\shortstack{Share\\\\(\\%)} & \\shortstack{$P_{50}$\\\\(s)} & \\shortstack{$P_{95}$\\\\(s)} & \\shortstack{Max\\\\(s)} & \\shortstack{Mean excess\\\\(s)}',b,True)
    b=[]
    for r in rows('waiting_by_class.csv'):
        if r['case']=='cs6_fullday' and r['policy']=='URG':
            b.append([r['sample_type'].replace('SVOC_E',r'SVOC$_E$'),r['n_samples'],r['affected_samples'],r['violations'],num(r,'p95_wait_s',1),num(r,'mean_flow_s',1),num(r,'p95_external_s',1)])
    table('tab_wait_classes.tex','CS6-URG waiting and flow metrics by sample class.','tab:wait-classes','lrrrrrr','Class & Jobs & Affected & Events & \\shortstack{Wait\\\\$P_{95}$ (s)} & \\shortstack{Mean flow\\\\(s)} & \\shortstack{External\\\\$P_{95}$ (s)}',b,True)
    b=[]
    pretty={'full':'Full score','no_starvation':'No starvation','no_new_violation':r'$w_{viol}=0$','no_overshoot':r'$w_{over}=0$','no_wait_penalties':r'$w_{viol}=w_{over}=0$','no_slack_band':'Slack band removed','selected_wait':'Selected-wait penalty'}
    for r in rows('ablations.csv'):
        b.append([labels[r['case']],pretty[r['variant']],num(r,'makespan_s',2),r['violations'],r['affected_samples'],r['local_changed_decisions'],r['sequence_changed_epochs']])
    table('tab_ablations.tex','Score ablations: outcomes and changes in local actions or executed sequences.','tab:ablations','llrrrrr','Case & Variant & \\shortstack{$C_{\\max}$\\\\(s)} & Events & Affected & \\shortstack{Local\\\\changes} & \\shortstack{Sequence\\\\changes}',b,True)
    physical={(r['case'],r['variant']):r for r in rows('physical_sensitivity.csv') if r['policy']=='LB-LA'}
    variants=[('separated_nominal','Separate nominal matrices'),('empty_no_dwell','Empty dwell 0 s'),('empty_half_dwell','Empty dwell 7.135 s'),('loaded_dwell_minus20','Loaded dwell $-20\\%$'),('loaded_dwell_plus20','Loaded dwell $+20\\%$'),('traverse_half_speed','Traverse speed 0.75 m/s'),('traverse_double_speed','Traverse speed 3 m/s'),('auxiliary_5.0s','Auxiliary 5 s/transfer'),('auxiliary_10.0s','Auxiliary 10 s/transfer'),('auxiliary_15.0s','Auxiliary 15 s/transfer'),('auxiliary_30.0s','Auxiliary 30 s/transfer')]
    b=[]
    for v,label in variants:
        a=physical['cs6_fullday',v];c=physical['cs8_day180',v]
        b.append([label,f"{float(a['makespan_s'])/3600:.2f}",f"{a['violations']} / {a['affected_samples']}",f"{float(c['makespan_s'])/3600:.2f}",f"{c['violations']} / {c['affected_samples']}"])
    table('tab_physical.tex','Uncalibrated physical-time stress tests under LB-LA; events / affected samples at 300 s.','tab:physical','lrrrr',' & \\multicolumn{2}{c}{CS6} & \\multicolumn{2}{c}{CS8}\\\\\\cmidrule(lr){2-3}\\cmidrule(lr){4-5} Timing scenario & $C_{\\max}$ (h) & Events / affected & $C_{\\max}$ (h) & Events / affected',b,True)
    ar={(r['case'],r['admission_cap']):r for r in rows('admission_sensitivity.csv')};b=[]
    for cap in ['1','2','3','4']:
        a=ar['cs6_fullday',cap];c=ar['cs8_day180',cap]
        b.append([cap,f"{float(a['makespan_s'])/3600:.2f}",f"{a['violations']} / {a['affected_samples']}",a['max_external_backlog'],f"{float(a['p95_external_s'])/3600:.2f}",f"{float(c['makespan_s'])/3600:.2f}",f"{c['violations']} / {c['affected_samples']}"])
    table('tab_admission.tex','BNA admission-cap sensitivity; events / affected samples at 300 s.','tab:admission','rrrrrrr',' & \\multicolumn{4}{c}{CS6} & \\multicolumn{2}{c}{CS8}\\\\\\cmidrule(lr){2-5}\\cmidrule(lr){6-7} Cap & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Events /\\\\affected} & \\shortstack{Peak external\\\\queue} & \\shortstack{External\\\\$P_{95}$ (h)} & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Events /\\\\affected}',b,True)
    b=[]
    for W in ['150.0','300.0','600.0','900.0']:
        subset=[r for r in rows('threshold_sensitivity.csv') if r['case']=='cs6_fullday' and r['threshold_s']==W]
        ur=next(r for r in subset if r['policy']=='URG');la=next(r for r in subset if r['policy']=='LB-LA')
        b.append([f'{float(W):.0f}',f"{ur['violations']} / {ur['affected_samples']}",f"{la['fixed_schedule_violations']} / {la['fixed_schedule_affected']}",f"{la['violations']} / {la['affected_samples']}"])
    table('tab_thresholds.tex','CS6 threshold sensitivity. Fixed schedules are evaluated against each threshold without changing actions; rerun LB-LA also uses the new threshold in its score. Entries are events / affected samples.','tab:thresholds','rrrr','Budget (s) & Fixed URG & Fixed LB-LA & Rerun LB-LA',b)
    if (ROOT/'data/hard_wait_results.csv').exists():
        b=[]
        for r in rows('hard_wait_results.csv'):
            b.append([r['instance'],r['n_samples'],r['status'],num(r,'incumbent_s',2),num(r,'dual_bound_s',2),num(r,'max_wait_s',2)])
        table('tab_hard_wait.tex','Reduced hard-wait checks with a 120 s SCIP limit.','tab:hardwait','lrlrrr','Instance & Jobs & Solver status & \\shortstack{Incumbent\\\\(s)} & \\shortstack{Dual bound\\\\(s)} & \\shortstack{Max wait\\\\(s)}',b,True)
    if args.paper_dir:
        import shutil
        (args.paper_dir/'tables').mkdir(exist_ok=True)
        for path in OUT.glob('*.tex'):shutil.copy2(path,args.paper_dir/'tables'/path.name)
    print('Generated',len(list(OUT.glob('*.tex'))),'tables')
if __name__=='__main__':main()
