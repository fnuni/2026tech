"""Publication vector figures from released, audited traces and CSV tables."""
import argparse,csv,json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator
import numpy as np
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'figures';OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.size':10,'font.family':'DejaVu Sans','pdf.fonttype':42,'axes.spines.top':False,'axes.spines.right':False})
parser=argparse.ArgumentParser();parser.add_argument('--only');args=parser.parse_args()
def save(fig,name):
    if not args.only or args.only==name:
        fig.savefig(OUT/(name+'.pdf'),bbox_inches='tight')
        fig.savefig(OUT/(name+'.png'),dpi=180,bbox_inches='tight')
    plt.close(fig)
def csvrows(n):return list(csv.DictReader((ROOT/'data'/n).open()))
def trace(case,policy):return json.loads((ROOT/'validation/corrected_runs'/case/policy/'trace.json').read_text())
COLORS={'URG':'#245a81','SRPT':'#00c000','BNA':'#ff0000','LB-LA':'#ffff00'}
stagecolors={6:'#80a9c4',7:'#99c998',8:'#dedede',9:'#4e9b8e',10:'#b28abc',11:'#d7a2b2',12:'#b7cbd6',13:'#6580ae',14:'#9c9c9c'}
stagenames={6:'De-capper, 1000 mL',7:'Dosing (Pipet A)',8:'Stirring',9:'HC extraction (Pipet B)',10:'SVOC extraction (Pipet C)',11:'Concentration',12:'De-capper, centrifuge',13:'Centrifugation',14:'GC-vial transfer (Pipet D)'}
r=trace('cs1_nominal','LB-LA');cfg=json.loads((ROOT/'instances/cs1_nominal_config.json').read_text());types={s['id']:s['type'] for s in cfg['samples']}
for full in [False,True]:
    ids=sorted(r['operations'],key=lambda x:int(x[1:]));ids=ids if full else ids[:8]
    fig,ax=plt.subplots(figsize=(8.6,7.2 if full else 4.2))
    for i,sid in enumerate(ids):
        for op in r['operations'][sid]:
            if op['type_k']==5:continue
            ax.barh(i,(op['completion_s']-op['start_s'])/3600,left=op['start_s']/3600,height=.65,color=stagecolors[op['type_k']],edgecolor='#555',lw=.5)
        for a in [a for a in r['tasks'] if a['sample']==sid]:
            if a['from_stage']>0:
                prev=r['operations'][sid][a['from_stage']-1]['completion_s'];w=a['pickup_s']-prev
                if w>1e-6:ax.barh(i,w/3600,left=prev/3600,height=.52,color='#ffdf66',edgecolor='#b22222' if w>300 else '#b99a35',lw=2.0 if w>300 else .6,zorder=3)
            ax.plot(a['delivery_s']/3600,i,marker='d',ms=3,color='#245a81',zorder=4)
    ax.set_yticks(range(len(ids)),[f'{s} ({types[s]})' for s in ids],fontsize=9.5)
    ax.invert_yaxis();ax.set_xlabel('Time (h)');ax.grid(axis='x',alpha=.2)
    handles=[Patch(facecolor=stagecolors[k],edgecolor='#555',label=stagenames[k]) for k in sorted(stagenames)]
    handles += [Patch(facecolor='#ffdf66',edgecolor='#b99a35',label='Waiting: at most 300 s'),Patch(facecolor='#ffdf66',edgecolor='#b22222',linewidth=2,label='Waiting: more than 300 s'),Line2D([],[],color='#245a81',marker='d',linestyle='None',markersize=4,label='Loaded delivery')]
    fig.legend(handles=handles,loc='upper center',ncol=3,fontsize=8.5,columnspacing=1.0,frameon=False)
    fig.tight_layout(rect=[0,0,1,.77 if not full else .86]);save(fig,'fig_gantt_cs1_full' if full else 'fig_gantt_cs1')

r=trace('cs6_fullday','URG');groups={}
for a in r['tasks']:
    if a['from_stage']>0:
        prev=r['operations'][a['sample']][a['from_stage']-1]
        k=(prev['type_k'],r['operations'][a['sample']][a['to_stage']-1]['type_k'])
        groups.setdefault(k,[]).append(a['pickup_s']-prev['completion_s'])
keys=sorted(groups);fig,ax=plt.subplots(figsize=(8.6,4.5))
ax.boxplot([groups[k] for k in keys],vert=False,labels=[f'{a} → {b}' for a,b in keys],showfliers=True,
           patch_artist=True,boxprops={'facecolor':'#a6c5d9'},medianprops={'color':'#1f3864','linewidth':1.5},
           flierprops={'marker':'.','markersize':3,'markerfacecolor':'#777','markeredgecolor':'#777'})
ax.axvline(300,color='#b22222',lw=1.7,ls='--',label='300 s budget')
ax.set_xscale('log');ax.set_xlim(1,4000);ax.invert_yaxis();ax.set_xlabel('Internal waiting (s, logarithmic scale)');ax.set_ylabel('Module transition')
ax.grid(axis='x',alpha=.2);ax.legend(loc='lower right',frameon=False);fig.tight_layout();save(fig,'fig_waiting_cs6')

primary={(r['case'],r['policy']):r for r in csvrows('kpi_campaign.csv')};ad=csvrows('admission_sensitivity.csv')
# Open, differently sized outlines keep coincident policies visible at their
# actual coordinates; no jitter or displacement is applied to the results.
with plt.rc_context({'font.size':13,'axes.labelsize':13,'axes.titlesize':14,
                     'xtick.labelsize':12,'ytick.labelsize':12}):
    fig,axes=plt.subplots(1,2,figsize=(10,4.8),sharey=True)
    for ax,case,title in zip(axes,['cs6_fullday','cs8_day180'],['CS6: 375 s releases','CS8: 180 s releases']):
        subset=[r for r in ad if r['case']==case]
        xs=[float(r['makespan_s'])/3600 for r in subset]
        ys=[int(r['affected_samples']) for r in subset]
        ax.plot(xs,ys,'o--',color=COLORS['BNA'],ms=8,lw=1.8,
                markeredgecolor='white',markeredgewidth=.7,label='BNA admission cap')
        for r,x,y in zip(subset,xs,ys):
            cap=r['admission_cap']
            offset={'1':(-4,10),'2':(6,9),'3':(27,-18),'4':(27,14)}[cap]
            ax.annotate('q='+cap,(x,y),xytext=offset,textcoords='offset points',
                        fontsize=11,ha='right' if cap=='1' else 'left',arrowprops={'arrowstyle':'-','color':'#777','lw':.6}
                        if cap=='4' else None)
        for p,marker,size,filled in [('URG','s',190,False),('SRPT','^',170,False),('LB-LA','D',60,True)]:
            r=primary[case,p];x=float(r['makespan_s'])/3600;y=int(r['affected_samples'])
            ax.scatter(x,y,facecolors=COLORS[p] if filled else 'none',
                       edgecolors='black' if filled else COLORS[p],linewidths=1.2 if filled else 1.9,
                       marker=marker,s=size,label=p,zorder=4)
        ax.axvline(24,color='#777',ls=':',lw=1.4)
        ax.text(24.5,218,'24 h',color='#666',fontsize=11)
        ax.set_title(title,pad=11);ax.set_xlabel('Makespan (h)')
        ax.grid(alpha=.2);ax.set_xlim(19,50);ax.set_ylim(-9,232)
    axes[0].set_ylabel('Distinct affected samples (of 230)')
    axes[0].legend(fontsize=11,frameon=True,facecolor='white',edgecolor='none',
                   framealpha=.95,loc='upper right',borderpad=.6,labelspacing=.6)
    fig.tight_layout(w_pad=1.7);save(fig,'fig_admission_tradeoff')

fig,axes=plt.subplots(1,2,figsize=(9,3.2),sharey=True)
for ax,case,title in zip(axes,['cs6_fullday','cs8_day180'],['CS6, URG','CS8, URG']):
    r=trace(case,'URG');cfg=json.loads((ROOT/'instances'/f'{case}_config.json').read_text());end=r['kpi']['makespan_s']
    times=np.arange(0,end+300,300);first={a['sample']:a['pickup_s'] for a in r['tasks'] if a['from_stage']==0}
    inside=[sum(first[sid]<=t<r['completions'][sid] for sid in first) for t in times]
    outside=[sum(s['release_s']<=t<first[s['id']] for s in cfg['samples']) for t in times]
    ax.step(times/3600,inside,where='post',color='#245a81',label='Inside cell')
    ax.step(times/3600,outside,where='post',color='#c16c20',label='External queue')
    ax.set_title(title);ax.set_xlabel('Time (h)');ax.grid(alpha=.2);ax.set_ylim(0,110)
axes[0].set_ylabel('Samples');axes[1].legend(frameon=False,fontsize=8);fig.tight_layout();save(fig,'fig_queue_profiles')
print('Generated audited publication figures in',OUT)
