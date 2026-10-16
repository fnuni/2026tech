"""Generate publication figures from the corrected CSVs and audited traces."""
import csv
import json
import argparse
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'figures'
OUT.mkdir(exist_ok=True)
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--only', choices=['fig_bars_makespan','fig_bars_violations',
                                     'fig_gantt_cs1','fig_wip_cs6'])
args=parser.parse_args()
plt.rcParams.update({'font.size':9,'font.family':'DejaVu Sans','pdf.fonttype':42,
                     'axes.spines.top':False,'axes.spines.right':False})
COLORS={'URG':'#245a81','URG-MECT':'#d17a20','LB-ROLL':'#257343'}
rows=list(csv.DictReader((ROOT/'data/kpi_campaign.csv').open()))
names=list(dict.fromkeys(r['case'] for r in rows))
data={(r['case'],r['policy']):r for r in rows}
policies=list(COLORS)

def save(fig,name):
    if args.only and args.only != name:
        plt.close(fig)
        return
    fig.savefig(OUT/(name+'.pdf'),bbox_inches='tight')
    fig.savefig(OUT/(name+'.png'),dpi=180,bbox_inches='tight')
    plt.close(fig)

fig,axes=plt.subplots(1,2,figsize=(8.2,3.25),gridspec_kw={'width_ratios':[1,1.1]})
for p,dx in [('URG',-.2),('URG-MECT',0),('LB-ROLL',.2)]:
    axes[0].bar(np.arange(7)+dx,[float(data[n,p]['makespan_s'])/3600 for n in names],
                width=.2,color=COLORS[p],label=p)
axes[0].set_xticks(range(7),[f'CS{i}' for i in range(1,8)])
axes[0].set_ylabel('Makespan (h)');axes[0].legend(fontsize=7,frameon=False)
axes[0].set_title('(a) Absolute completion time',fontsize=10)
deltas=[100*(float(data[n,'LB-ROLL']['makespan_s'])/float(data[n,'URG']['makespan_s'])-1) for n in names]
axes[1].barh(range(7),deltas,color=['#9d3030' if x>0 else COLORS['LB-ROLL'] for x in deltas])
axes[1].axvline(0,color='black',lw=.7)
axes[1].set_yticks(range(7),[f'CS{i}' for i in range(1,8)]);axes[1].invert_yaxis()
axes[1].set_xlim(-.65,.4);axes[1].set_xlabel('LB-ROLL change from URG (%)')
axes[1].set_title('(b) Policy effect at each load',fontsize=10)
for i,x in enumerate(deltas):axes[1].text(x+(.012 if x>=0 else -.012),i,f'{x:+.3f}',va='center',ha='left' if x>=0 else 'right',fontsize=8)
fig.tight_layout(w_pad=2);save(fig,'fig_bars_makespan')

fig,axes=plt.subplots(1,2,figsize=(8.2,3.1))
for ax,metric,title in [(axes[0],'violations','(a) Violated stage budgets'),
                         (axes[1],'affected_samples','(b) Distinct affected samples')]:
    for p,dx in [('URG',-.2),('URG-MECT',0),('LB-ROLL',.2)]:
        ax.bar(np.arange(7)+dx,[float(data[n,p][metric]) for n in names],width=.2,color=COLORS[p],label=p)
    ax.set_xticks(range(7),[f'CS{i}' for i in range(1,8)])
    ax.set_ylabel('Count');ax.set_title(title,fontsize=10);ax.grid(axis='y',alpha=.2)
    ax.set_axisbelow(True)
axes[0].legend(fontsize=7,frameon=False)
fig.tight_layout(w_pad=2);save(fig,'fig_bars_violations')

traces=json.loads((ROOT/'validation/plot_data.json').read_text())
r=traces['cs1_nominal'];cfg=json.loads((ROOT/'instances/cs1_nominal_config.json').read_text())
types={s['id']:s['type'] for s in cfg['samples']}
fig,ax=plt.subplots(figsize=(7.0,6.4))
stagecolors={6:'#85a8c1',7:'#9acb9a',8:'#dedede',9:'#a2d29a',10:'#a2d29a',11:'#d5a8b1',12:'#85a8c1',13:'#b7a3cd',14:'#9acb9a'}
ids=sorted(r['operations'],key=lambda x:int(x[1:]))
for i,sid in enumerate(ids):
    for op in r['operations'][sid]:
        if op['type_k']==5:continue
        start,end=op['start_s'],op['completion_s']
        ax.barh(i,(end-start)/3600,left=start/3600,height=.62,color=stagecolors[op['type_k']],edgecolor='#555',lw=.4)
    for task in [a for a in r['tasks'] if a['sample']==sid]:
        if task['from_stage']>0:
            prev=r['operations'][sid][task['from_stage']-1]['completion_s']
            wait=task['pickup_s']-prev
            if wait>1e-6:ax.barh(i,wait/3600,left=prev/3600,height=.5,color='#ffdf66',edgecolor='#b22222' if wait>300 else '#caaa44',lw=1.8 if wait>300 else .6,zorder=3)
        ax.plot(task['delivery_s']/3600,i,marker='d',ms=2.5,color='#245a81',zorder=4)
ax.set_yticks(range(len(ids)),[f'{s} ({types[s]})' for s in ids],fontsize=8.5)
ax.invert_yaxis();ax.set_xlabel('Time (h)');ax.grid(axis='x',alpha=.2)
ax.legend(handles=[Patch(facecolor='#dedede',edgecolor='#555',label='Stirring'),
                   Patch(facecolor='#ffdf66',edgecolor='#caaa44',label='Waiting: at most 300 s'),
                   Patch(facecolor='#ffdf66',edgecolor='#b22222',linewidth=1.8,label='Waiting: more than 300 s'),
                   Line2D([],[],color='#245a81',marker='d',linestyle='None',markersize=4,label='Loaded delivery')],
          loc='lower center',bbox_to_anchor=(.5,1.01),ncol=2,fontsize=8,frameon=False)
ax.set_title('CS1, LB-ROLL: processing, pickup waiting and deliveries',fontsize=10,pad=45)
fig.tight_layout();save(fig,'fig_gantt_cs1')

r=traces['cs6_fullday'];end=r['kpi']['makespan_s'];times=np.arange(0,end+600,600)
first={sid:ops[0]['start_s'] for sid,ops in r['operations'].items()}
wip=[sum(first[sid]<=t<r['completions'][sid] for sid in first) for t in times]
util=[]
for t in times:
    hi=min(t+600,end)
    busy=sum(max(0,min(a['delivery_s'],hi)-max(a['start_s'],t)) for a in r['tasks'])
    util.append(100*busy/(hi-t) if hi>t else 0)
fig,ax=plt.subplots(figsize=(8.2,2.9));other=ax.twinx()
ax.step(times/3600,wip,where='post',color='#245a81',lw=1.4,label='Physical WIP')
other.plot(times/3600,util,color='#c16c20',lw=1,ls='--',label='Robot utilisation')
ax.set(xlabel='Time (h)',ylabel='Samples inside cell',ylim=(0,max(wip)+2))
other.set(ylabel='Robot utilisation (%)',ylim=(0,105));ax.xaxis.set_major_locator(MultipleLocator(2))
ax.grid(alpha=.2);fig.legend(loc='upper center',ncol=2,frameon=False,fontsize=8)
fig.tight_layout(rect=[0,0,1,.92]);save(fig,'fig_wip_cs6')

print('Generated',args.only or 'four vector figures and PNG previews','in',OUT)
