"""Second-round publication figures from released traces and CSV files."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'figures'
OUT.mkdir(exist_ok=True)
plt.rcParams.update({'font.size': 10, 'font.family': 'DejaVu Sans', 'pdf.fonttype': 42,
                     'axes.spines.top': False, 'axes.spines.right': False})
COLORS = {'URG': '#245a81', 'SRPT': '#00a000', 'BNA': '#d62728', 'LB-LA': '#e6c200'}
STAGE_COLORS = {6: '#80a9c4', 7: '#99c998', 8: '#dedede', 9: '#4e9b8e', 10: '#b28abc', 11: '#d7a2b2',
                12: '#b7cbd6', 13: '#6580ae', 14: '#9c9c9c'}
STAGE_NAMES = {6: 'De-capper, 1000 mL', 7: 'Dosing (Pipet A)', 8: 'Stirring', 9: 'HC extraction (Pipet B)',
               10: 'SVOC extraction (Pipet C)', 11: 'Concentration', 12: 'De-capper, centrifuge',
               13: 'Centrifugation', 14: 'GC-vial transfer (Pipet D)'}


def save(fig, name):
    fig.savefig(OUT / (name + '.pdf'), bbox_inches='tight')
    fig.savefig(OUT / (name + '.png'), dpi=180, bbox_inches='tight')
    plt.close(fig)


def rows(name):
    return list(csv.DictReader((ROOT / 'data' / name).open()))


def gantt_cs6_detail(first_release=36000., last_release=40200.):
    cfg = json.loads((ROOT / 'instances/cs6_fullday_config.json').read_text())
    rel = {s['id']: s['release_s'] for s in cfg['samples']}
    types = {s['id']: s['type'] for s in cfg['samples']}
    ids = [s['id'] for s in cfg['samples'] if first_release <= s['release_s'] <= last_release]
    urg = json.loads((ROOT / 'validation/corrected_runs/cs6_fullday/URG/trace.json').read_text())
    cp = json.loads((ROOT / 'validation/cp/cs6_fullday/targeted_cp-ref_trace.json').read_text())
    fig, axes = plt.subplots(2, 1, figsize=(9.2, 7.6), sharex=True)
    for ax, r, title in zip(axes, [urg, cp], ['URG (operational problem)',
                                             'CP-REF (hard-wait-constrained problem)']):
        for i, sid in enumerate(ids):
            first = next(a for a in r['tasks'] if a['sample'] == sid and a['from_stage'] == 0)
            ext = first['pickup_s'] - rel[sid]
            if ext > 1:
                ax.barh(i, ext / 3600, left=rel[sid] / 3600, height=.5, color='white',
                        edgecolor='#c16c20', hatch='////', lw=.6)
            for op in r['operations'][sid]:
                if op['type_k'] == 5:
                    continue
                ax.barh(i, (op['completion_s'] - op['start_s']) / 3600, left=op['start_s'] / 3600,
                        height=.65, color=STAGE_COLORS[op['type_k']], edgecolor='#555', lw=.5)
            for a in [a for a in r['tasks'] if a['sample'] == sid and a['from_stage'] > 0]:
                prev = r['operations'][sid][a['from_stage'] - 1]['completion_s']
                w = a['pickup_s'] - prev
                if w > 1e-6:
                    ax.barh(i, w / 3600, left=prev / 3600, height=.52, color='#ffdf66',
                            edgecolor='#b22222' if w > 300 + 1e-8 else '#b99a35',
                            lw=2.0 if w > 300 + 1e-8 else .6, zorder=3)
        ax.set_yticks(range(len(ids)), [f'{s} ({types[s].replace("SVOC_E", "SVOC-E")})' for s in ids],
                      fontsize=8.5)
        ax.invert_yaxis()
        ax.set_title(title, fontsize=10.5, loc='left')
        ax.grid(axis='x', alpha=.2)
    axes[-1].set_xlabel('Time (h)')
    handles = [Patch(facecolor=STAGE_COLORS[k], edgecolor='#555', label=STAGE_NAMES[k]) for k in sorted(STAGE_NAMES)]
    handles += [Patch(facecolor='white', edgecolor='#c16c20', hatch='////', label='External queue (release to admission)'),
                Patch(facecolor='#ffdf66', edgecolor='#b99a35', label='Internal wait $\\leq$ 300 s'),
                Patch(facecolor='#ffdf66', edgecolor='#b22222', linewidth=2, label='Internal wait > 300 s')]
    fig.legend(handles=handles, loc='upper center', ncol=3, fontsize=8, frameon=False)
    fig.tight_layout(rect=[0, 0, 1, .86])
    save(fig, 'fig_gantt_cs6_detail')


def admission_tradeoff():
    primary = {(r['case'], r['policy']): r for r in rows('kpi_campaign.csv')}
    ad = rows('admission_sensitivity.csv')
    cp = {(r['case'], r['problem'], r['method']): r for r in rows('cp_reference.csv')}
    tgt = rows('cp_targeted_cs6.csv')
    with plt.rc_context({'font.size': 12, 'axes.labelsize': 12, 'xtick.labelsize': 11, 'ytick.labelsize': 11}):
        fig, axes = plt.subplots(1, 2, figsize=(10, 4.8), sharey=True)
        for ax, case, title in zip(axes, ['cs6_fullday', 'cs8_day180'],
                                   ['CS6: 375 s releases', 'CS8: 180 s releases']):
            subset = [r for r in ad if r['case'] == case]
            xs = [float(r['makespan_s']) / 3600 for r in subset]
            ys = [int(r['affected_samples']) for r in subset]
            ax.plot(xs, ys, 'o--', color=COLORS['BNA'], ms=8, lw=1.6, markeredgecolor='white',
                    label='BNA, caps 1-4')
            for r, x, y in zip(subset, xs, ys):
                ax.annotate('q=' + r['admission_cap'], (x, y), xytext=(5, 6), textcoords='offset points', fontsize=10)
            for p, marker, size, filled in [('URG', 's', 170, False), ('SRPT', '^', 150, False), ('LB-LA', 'D', 55, True)]:
                r = primary[case, p]
                ax.scatter(float(r['makespan_s']) / 3600, int(r['affected_samples']),
                           facecolors=COLORS[p] if filled else 'none',
                           edgecolors='black' if filled else COLORS[p], linewidths=1.2 if filled else 1.8,
                           marker=marker, s=size, label=p, zorder=4)
            r = cp.get((case, 'operational', 'RH-CP'))
            if r:
                ax.scatter(float(r['makespan_s']) / 3600, int(r['affected_samples']), marker='P', s=110,
                           color='#7f7f7f', label='RH-CP, operational', zorder=4)
            r = cp.get((case, 'hard_wait', 'RH-CP'))
            if r:
                ax.scatter(float(r['makespan_s']) / 3600, 0, marker='*', s=220, color='#6a3d9a',
                           label='RH-CP, hard-wait', zorder=5)
            if case == 'cs6_fullday':
                best = min(tgt, key=lambda r: float(r['makespan_s']))
                ax.scatter(float(best['makespan_s']) / 3600, 0, marker='*', s=260, facecolors='none',
                           edgecolors='black', linewidths=1.5, label='CP-REF, hard-wait', zorder=6)
            ax.axvline(24, color='#777', ls=':', lw=1.4)
            ax.text(24.3, 215, '24 h', color='#666', fontsize=10)
            ax.set_title(title)
            ax.set_xlabel('Makespan (h)')
            ax.grid(alpha=.2)
            ax.set_xlim(19, 50)
            ax.set_ylim(-12, 232)
        axes[0].set_ylabel('Affected samples (of 230)')
        axes[0].legend(fontsize=9.5, frameon=True, facecolor='white', edgecolor='none', loc='upper right')
        fig.tight_layout(w_pad=1.5)
        save(fig, 'fig_admission_tradeoff')


def robot_load():
    data = [r for r in rows('robot_load_sensitivity.csv') if r['case'] == 'cs6_fullday']
    acc = {r['item']: float(r['value']) for r in rows('robot_accounting.csv')}
    a_eq = acc['equivalent_auxiliary_s_per_transfer']
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.9))
    for p in ['URG', 'SRPT', 'BNA', 'LB-LA']:
        sub = sorted([r for r in data if r['policy'] == p], key=lambda r: float(r['auxiliary_s']))
        a = [float(r['auxiliary_s']) for r in sub]
        axes[0].plot(a, [float(r['makespan_s']) / 3600 for r in sub], 'o-', ms=4, color=COLORS[p],
                     mec='black' if p == 'LB-LA' else COLORS[p], label=p)
        axes[1].plot(a, [float(r['pipc_violation_share_pct']) for r in sub], 'o-', ms=4, color=COLORS[p],
                     mec='black' if p == 'LB-LA' else COLORS[p], label=p)
    for ax in axes:
        ax.axvline(a_eq, color='#555', ls='--', lw=1)
        ax.grid(alpha=.2)
        ax.set_xlabel('Auxiliary robot service per transfer, $a$ (s)')
    axes[0].axhline(24, color='#777', ls=':', lw=1.3)
    axes[0].text(a_eq - 0.5, 20.6, 'industrial\naccounting', ha='right', fontsize=8.5, color='#444')
    a_crit = acc.get('robot_certificate_threshold_s')
    if a_crit:
        axes[0].axvline(a_crit, color='#b22222', ls='-.', lw=1)
        axes[0].text(a_crit + 0.4, 34.5, 'robot bound\n> 24 h', fontsize=8.5, color='#b22222')
    axes[0].set_ylabel('Makespan (h)')
    axes[1].set_ylabel('Violation events before Pipet C (%)')
    axes[1].set_ylim(0, 100)
    axes[0].legend(frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, 'fig_robot_load')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*')
    args = ap.parse_args()
    figs = {'gantt': gantt_cs6_detail, 'admission': admission_tradeoff, 'robot': robot_load}
    for name, fn in figs.items():
        if not args.only or name in args.only:
            fn()
