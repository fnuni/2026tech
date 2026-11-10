"""Manuscript tables (second round) generated from released CSV files and traces."""
import argparse
import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tables'
OUT.mkdir(exist_ok=True)
POLICIES = ['URG', 'SRPT', 'BNA', 'LB-LA']
CASES = ['cs1_nominal', 'cs2_medium', 'cs3_high', 'cs4_medium_s99', 'cs5_double', 'cs6_fullday',
         'cs7_pipc480', 'cs8_day180', 'cs9_day60', 'cs10_m2first', 'cs11_hcfirst',
         'cs12_m2heavy', 'cs13_day480', 'cs14_day180_480', 'cs15_tiled180']
LABEL = {n: f'CS{i + 1}' for i, n in enumerate(CASES)}


def rows(name):
    return list(csv.DictReader((ROOT / 'data' / name).open()))


def cert_v4(config_path, prefix=None):
    import sys
    sys.path.insert(0, str(ROOT / 'code'))
    from cell_model import Cell, Instance
    from revision_study import StrengthenedCertificate
    cell = Cell(config_path)
    spec = cell.cfg['samples'] if prefix is None else cell.cfg['samples'][:prefix]
    return StrengthenedCertificate.components(Instance(cell, spec))


LB4 = {r['case']: r for r in rows('lower_bounds_v4.csv')} if (ROOT / 'data/lower_bounds_v4.csv').exists() else {}


def f(x, d=0):
    """MDPI style: thousands separator only from five digits."""
    x = float(x)
    return f'{x:,.{d}f}' if abs(x) >= 10000 else f'{x:.{d}f}'


def h(x, d=2):
    return f'{float(x) / 3600:.{d}f}'


def table(name, caption, label, align, header, body, wide=False, full=False, size='footnotesize', notes=None):
    t = '\\begin{table}[tbp]\n'
    if full:
        t += '\\begin{adjustwidth}{-\\extralength}{0cm}\n'
    t += '\\caption{' + caption + '\\label{' + label + '}}\n\\centering\n\\' + size + '\n'
    width = '\\fulllength' if full else '\\textwidth'
    if wide or full:
        t += '\\setlength{\\tabcolsep}{3pt}\n\\begin{tabular*}{' + width + '}{@{\\extracolsep{\\fill}}' + align + '@{}}\n'
    else:
        t += '\\begin{tabular}{' + align + '}\n'
    t += '\\toprule\n' + header + '\\\\\n\\midrule\n'
    t += '\n'.join(r if isinstance(r, str) else ' & '.join(map(str, r)) + '\\\\' for r in body)
    t += '\n\\bottomrule\n\\end{tabular*}\n' if (wide or full) else '\n\\bottomrule\n\\end{tabular}\n'
    if notes:
        t += '\\par\\smallskip\\parbox{' + width + '}{\\scriptsize ' + notes + '}\n'
    if full:
        t += '\\end{adjustwidth}\n'
    t += '\\end{table}\n'
    (OUT / name).write_text(t)


def trace_metrics(trace, cfg_samples):
    rel = {s['id']: s['release_s'] for s in cfg_samples}
    first = {a['sample']: a['pickup_s'] for a in trace['tasks'] if a['from_stage'] == 0}
    ext = [first[s] - rel[s] for s in rel]
    waits = [a['technological_wait_s'] for a in trace['tasks'] if a['from_stage'] > 0]
    ev = defaultdict(int)
    for s in rel:
        ev[rel[s]] += 1
        ev[first[s]] -= 1
    b = peak = 0
    for t, n in sorted(ev.items()):
        b += n
        peak = max(peak, b)
    flow = [trace['completions'][s] - rel[s] for s in rel]
    return {'p95_wait_s': float(np.percentile(waits, 95)), 'p95_external_s': float(np.percentile(ext, 95)),
            'peak': peak, 'mean_flow_s': float(np.mean(flow))}


CASE_TEXT = {
    'cs1_nominal': (23, 375, 'Generator, seed 42; 10 HC, 7 SVOC, 3 SVOC$_E$, QC1, QC2, blank'),
    'cs2_medium': (23, 180, 'CS1 sequence at 180~s cadence'),
    'cs3_high': (23, 60, 'CS1 sequence at 60~s cadence'),
    'cs4_medium_s99': (23, 180, 'CS2 instants and mix; seed 99: emulsions on Method-2 slots 4, 7, 10 (CS2: 1, 2, 5), QC1 and blank swapped'),
    'cs5_double': (46, 150, 'Generator, seed 42, double CS1 volume'),
    'cs6_fullday': (230, 375, 'Design day: generator, seed 42, full project mix'),
    'cs7_pipc480': (23, 180, 'CS2 with Pipet C 480~s'),
    'cs8_day180': (230, 180, 'CS6 release times $\\times180/375$'),
    'cs9_day60': (230, 60, 'CS6 release times $\\times60/375$'),
    'cs10_m2first': (230, 180, 'CS8 instants; Method-2 analytical routes first'),
    'cs11_hcfirst': (230, 180, 'CS8 instants; HC routes first'),
    'cs12_m2heavy': (230, 180, 'CS8 instants; 40 HC replaced by 28 SVOC and 12 SVOC$_E$'),
    'cs13_day480': (230, 375, 'CS6 with Pipet C 480~s'),
    'cs14_day180_480': (230, 180, 'CS8 with Pipet C 480~s'),
    'cs15_tiled180': (230, 180, 'Ten CS2 blocks at 3600~s offsets (continuous 180~s cadence)'),
}


def t_cases():
    body = []
    for n in CASES:
        jobs, cad, txt = CASE_TEXT[n]
        pipc = 480 if n in ('cs7_pipc480', 'cs13_day480', 'cs14_day180_480') else 600
        body.append([LABEL[n], jobs, cad, txt, pipc])
    table('tab_cases_expanded.tex', 'Deterministic scenarios. Cadence is the analytical release interval; controls are released 5~s after an analytical release. Each row states the input that distinguishes the scenario from its reference.',
          'tab:cases', 'lrrp{8.6cm}r', 'Case & Jobs & \\shortstack{Cadence\\\\(s)} & Release list and distinguishing change & \\shortstack{Pipet C\\\\(s)}', body, size='footnotesize')


def t_cs6():
    k = {(r['case'], r['policy']): r for r in rows('kpi_campaign.csv')}
    op = {(r['case'], r['policy']): r for r in rows('operating_metrics.csv')}
    adm = {(r['case'], r['admission_cap']): r for r in rows('admission_sensitivity.csv')}
    cfg = json.loads((ROOT / 'instances/cs6_fullday_config.json').read_text())['samples']
    body = []
    for p, lab in [('URG', 'URG'), ('SRPT', 'SRPT'), ('BNA', 'BNA ($q=2$)'), ('LB-LA', 'LB-LA')]:
        r, o = k['cs6_fullday', p], op['cs6_fullday', p]
        body.append([lab, h(r['makespan_s']), r['violations'], r['affected_samples'], f(o['p95_wait_s']),
                     h(o['p95_external_s']), o['max_external_backlog'], h(r['mean_flow_s'])])
    a = adm['cs6_fullday', '1']
    body.append(['BNA ($q=1$)', h(a['makespan_s']), a['violations'], a['affected_samples'], f(a['p95_wait_s']),
                 h(a['p95_external_s']), a['max_external_backlog'], h(a['mean_flow_s'])])
    body.append('\\midrule')
    for path, lab in [('operational_rhcp', 'RH-CP, operational'), ('hard_wait_rhcp', 'RH-CP, hard-wait'),
                      ('targeted_cp-ref', 'CP-REF, hard-wait')]:
        tp = ROOT / 'validation/cp/cs6_fullday' / f'{path}_trace.json'
        if not tp.exists():
            continue
        tr = json.loads(tp.read_text())
        m = trace_metrics(tr, cfg)
        kk = tr['kpi']
        body.append([lab, h(kk['makespan_s']), kk['violations'], kk['affected_samples'], f(m['p95_wait_s']),
                     h(m['p95_external_s']), m['peak'], h(m['mean_flow_s'])])
    table('tab_cs6_expanded.tex', 'Design-day (CS6) performance at the uniform 300~s limit. Events count violating transitions; affected counts distinct samples. Wait $P_{95}$ includes all internal transitions; external waiting runs from release to admission.',
          'tab:kpi-cs6', 'lrrrrrrr', 'Method & \\shortstack{$C_{\\max}$\\\\(h)} & Events & Affected & \\shortstack{Wait\\\\$P_{95}$ (s)} & \\shortstack{External\\\\$P_{95}$ (h)} & \\shortstack{Peak ext.\\\\queue} & \\shortstack{Mean flow\\\\(h)}', body, wide=True)


def t_wait():
    body = []
    total = sum(int(r['violations']) for r in rows('waiting_by_transition.csv') if r['case'] == 'cs6_fullday' and r['policy'] == 'URG')
    for r in rows('waiting_by_transition.csv'):
        if r['case'] == 'cs6_fullday' and r['policy'] == 'URG' and int(r['violations']):
            body.append([f"{r['origin_k']} $\\to$ {r['destination_k']}", r['transitions'], r['violations'],
                         f"{100 * int(r['violations']) / total:.1f}", f(r['p50_wait_s'], 1), f(r['p95_wait_s'], 1),
                         f(r['max_wait_s'], 1), f(r['mean_excess_s'], 1)])
    table('tab_wait_stages.tex', 'Violating transitions in CS6-URG (module codes as in Table~\\ref{tab:modules}). Shares refer to the 114 events; mean excess averages violating occurrences only.',
          'tab:wait-stages', 'lrrrrrrr', 'Transition & Count & Events & \\shortstack{Share\\\\(\\%)} & \\shortstack{$P_{50}$\\\\(s)} & \\shortstack{$P_{95}$\\\\(s)} & \\shortstack{Max\\\\(s)} & \\shortstack{Mean excess\\\\(s)}', body, wide=True)
    body = []
    for r in rows('waiting_by_class.csv'):
        if r['case'] == 'cs6_fullday' and r['policy'] == 'URG':
            body.append([r['sample_type'].replace('SVOC_E', 'SVOC$_E$'), r['n_samples'], r['affected_samples'], r['violations'],
                         f(r['p95_wait_s'], 1), h(r['mean_flow_s']), h(r['p95_external_s'])])
    table('tab_wait_classes.tex', 'CS6-URG waiting and flow by sample class.', 'tab:wait-classes', 'lrrrrrr',
          'Class & Jobs & Affected & Events & \\shortstack{Wait\\\\$P_{95}$ (s)} & \\shortstack{Mean flow\\\\(h)} & \\shortstack{External\\\\$P_{95}$ (h)}', body, wide=True)


def t_primary():
    k = {(r['case'], r['policy']): r for r in rows('kpi_campaign.csv')}
    cp = {(r['case'], r['problem'], r['method']): r for r in rows('cp_reference.csv')}
    lb = LB4
    body = []
    for n in CASES:
        row = [LABEL[n]]
        for p in POLICIES:
            r = k[n, p]
            row.append(f"{h(r['makespan_s'])} / {r['violations']} / {r['affected_samples']}")
        o = cp.get((n, 'operational', 'RH-CP'))
        row.append(f"{h(o['makespan_s'])} / {o['violations']} / {o['affected_samples']}" if o else '--')
        w = cp.get((n, 'hard_wait', 'RH-CP'))
        row.append(h(w['makespan_s']) if w else '--')
        row.append(h(lb[n]['certificate_lb_s']))
        body.append(row)
    table('tab_primary.tex', 'Scenario comparison: makespan (h) / violation events / affected samples. RH-CP solves the operational problem or the hard-wait-constrained problem (whose schedules have no event); $LB$ is the certificate~\\eqref{eq:cert}, valid for both problems.',
          'tab:summary', 'lrrrrrrr', 'Case & URG & SRPT & BNA & LB-LA & RH-CP, operational & \\shortstack{RH-CP,\\\\hard-wait} & \\shortstack{$LB$\\\\(h)}', body, full=True)


def t_gaps():
    k = {(r['case'], r['policy']): r for r in rows('kpi_campaign.csv')}
    cp = {(r['case'], r['problem'], r['method']): r for r in rows('cp_reference.csv')}
    body = []
    for n in CASES:
        b = LB4[n]
        cert = float(b['certificate_lb_s'])
        v3 = float(b['certificate_v3_lb_s'])
        best_rule = min(float(k[n, p]['makespan_s']) for p in POLICIES)
        o = cp.get((n, 'operational', 'RH-CP'))
        w = cp.get((n, 'hard_wait', 'RH-CP'))
        cands = [best_rule] + ([float(o['makespan_s'])] if o else []) + ([float(w['makespan_s'])] if w else [])
        ub = min(cands)
        body.append([LABEL[n], f(v3), f(cert), b['binding'].replace('head/tail ', 'h/t~'), f(best_rule),
                     f'{100 * (best_rule / cert - 1):.2f}', f'{100 * (ub / cert - 1):.2f}',
                     f(w['makespan_s']) if w else '--', f"{100 * (float(w['makespan_s']) / cert - 1):.2f}" if w else '--'])
    table('tab_gaps_expanded.tex', 'Lower bounds and gaps (times in s). $LB_{\\mathrm{v1}}$: certificate of the first version (head/tail term on complete operation sets); $LB$: certificate~\\eqref{eq:cert} with nested subsets and mandatory empty legs, valid for both problems; binding term with module type. UB: best operational upper bound among the rules and both RH-CP schedules (a hard-wait-feasible schedule is also operationally feasible); the monolithic CP schedules of Table~\\ref{tab:intermediate} are shorter on CS1, CS2, CS4, CS5 and CS7 (e.g., CS1: 10,785~s), and on CS6 the best hard-wait-feasible schedule is CP-REF (23.20~h, gap 6.82\\%, Table~\\ref{tab:targeted}).',
          'tab:gaps', 'lrrlrrrrr', 'Case & $LB_{\\mathrm{v1}}$ & $LB$ & Binding & \\shortstack{Best\\\\rule} & \\shortstack{Rule\\\\gap (\\%)} & \\shortstack{UB\\\\gap (\\%)} & \\shortstack{RH-CP\\\\hard-wait} & \\shortstack{Hard-wait\\\\gap (\\%)}', body, wide=True)


def t_intermediate():
    if not (ROOT / 'data/cp_intermediate.csv').exists():
        return
    body = []
    cache = {}
    for r in rows('cp_intermediate.csv'):
        inst = r['instance']
        if 'first' in inst:
            base, pre = inst.split('_first')
            lab, key = f'CS8[1:{pre}]', (base, int(pre))
        else:
            lab, key = LABEL[inst], (inst, None)
        if key not in cache:
            cache[key] = float(cert_v4(ROOT / 'instances' / f'{key[0]}_config.json', key[1])['certificate_lb_s'])
        cert = cache[key]
        rb = float(r['relaxed_cp_bound_s']) if r['relaxed_cp_bound_s'] not in ('', 'None') else 0.
        lb = max(cert, rb)
        prob = 'oper.' if r['problem'] == 'operational' else 'hard-wait'
        best_rule = min(float(r[f'{p}_makespan_s']) for p in POLICIES)
        prim = float(r['primal_s'])
        ub = min(prim, best_rule) if r['problem'] == 'operational' else prim
        body.append([lab, r['jobs'], prob, f(cert), f(rb) if rb else '--', f(prim),
                     f(best_rule) if r['problem'] == 'operational' else '--', f'{100 * (ub / lb - 1):.2f}',
                     f'{100 * (best_rule / ub - 1):.2f}' if r['problem'] == 'operational' else '--'])
    table('tab_intermediate.tex', 'Intermediate instances on the full layout, CP-SAT with a 120~s limit per model and two workers (no model was solved to proven optimality). $LB$: certificate~\\eqref{eq:cert}; the relaxed-model bound is valid by Proposition~\\ref{prop:cpdual}; the CP primal value is an audited conservative-model schedule. Bound gap: best upper bound over best lower bound; rule excess: best rule over best upper bound.',
          'tab:intermediate', 'lrlrrrrrr', 'Instance & $n$ & Problem & $LB$ & \\shortstack{Relaxed CP\\\\bound} & \\shortstack{CP\\\\primal} & \\shortstack{Best\\\\rule} & \\shortstack{Bound\\\\gap (\\%)} & \\shortstack{Rule\\\\excess (\\%)}', body, wide=True)


def t_design():
    if not (ROOT / 'data/design_alternatives.csv').exists():
        return
    d = {(r['case'], r['alternative'], r['policy']): r for r in rows('design_alternatives.csv')}
    cpd = {(r['case'], r['alternative']): r for r in rows('cp_design.csv')} if (ROOT / 'data/cp_design.csv').exists() else {}
    cpf = {(r['case'], r['problem'], r['method']): r for r in rows('cp_reference.csv')} if (ROOT / 'data/cp_reference.csv').exists() else {}
    names = {'nominal': 'Nominal (one Pipet C)', 'second_pipet_c': 'Second Pipet C', 'second_pipet_c_and_d': 'Second Pipet C and D'}
    body = []
    for case in ['cs6_fullday', 'cs8_day180', 'cs12_m2heavy']:
        for alt in names:
            row = [LABEL[case], names[alt]]
            for p in ['URG', 'SRPT', 'BNA', 'LB-LA']:
                r = d[case, alt, p]
                row.append(f"{h(r['makespan_s'])} / {r['affected_samples']}")
            if alt == 'nominal':
                c = cpf.get((case, 'hard_wait', 'RH-CP'))
            else:
                c = cpd.get((case, alt))
            row.append(h(c['makespan_s']) if c else '--')
            cfgp = ROOT / 'instances' / f'{case}_config.json' if alt == 'nominal' else ROOT / 'instances' / 'design' / f'{case}__{alt}_config.json'
            row.append(h(cert_v4(cfgp)['certificate_lb_s']))
            body.append(row)
        if case != 'cs12_m2heavy':
            body.append('\\midrule')
    table('tab_design.tex', 'Structural alternatives: makespan (h) / affected samples under the dispatching rules, hard-wait-feasible RH-CP makespan (h) and certificate~\\eqref{eq:cert} (h).',
          'tab:design', 'llrrrrrr', 'Case & Alternative & URG & SRPT & BNA & LB-LA & \\shortstack{RH-CP,\\\\hard-wait} & \\shortstack{$LB$\\\\(h)}', body, wide=True)


def t_stochastic():
    if not (ROOT / 'data/stochastic_summary.csv').exists():
        return
    rs = rows('stochastic_summary.csv')
    order = ['CS6 low variability', 'CS6 high variability', 'CS8 low variability']
    pol = ['URG', 'SRPT', 'BNA', 'LB-LA', 'LB-LA w_over=0']
    by = {(r['setting'], r['policy']): r for r in rs}
    body = []
    for s in order:
        for p in pol:
            r = by[s, p]
            vs_lbla = (f"{float(r['affected_vs_lbla_mean']):+.1f} $\\pm$ {float(r['affected_vs_lbla_ci95']):.1f}"
                       if p == 'LB-LA w_over=0' else '--')
            body.append([s.replace(' low variability', ', low CV').replace(' high variability', ', high CV') if p == 'URG' else '', p.replace('w_over=0', '($w_{over}=0$)'),
                         f"{float(r['makespan_s_mean']) / 3600:.2f} $\\pm$ {float(r['makespan_s_ci95']) / 3600:.2f}",
                         f"{float(r['affected_samples_mean']):.1f} $\\pm$ {float(r['affected_samples_ci95']):.1f}",
                         f"{float(r['affected_vs_urg_mean']):+.1f} $\\pm$ {float(r['affected_vs_urg_ci95']):.1f}" if p != 'URG' else '--',
                         vs_lbla,
                         f"{float(r['pipc_violation_share_pct_mean']):.0f}",
                         f"{float(r['share_within_24h_pct']):.0f}"])
        body.append('\\midrule')
    body = body[:-1]
    table('tab_stochastic.tex', 'Single-day stochastic runs: 30 replications with common random numbers; means $\\pm$ approximate 95\\% half-widths (normal approximation, exploratory, no correction for multiple comparisons). Paired differences in affected samples refer to URG and, for the variant without overshoot penalty, to the default LB-LA in the same replication. Low and high CV: coefficients of variation of 10\\%/5\\% and 20\\%/10\\% for processing/movement times. Pipet C share: violation events before Pipet C. Within 24~h: observed share of replications.',
          'tab:stochastic', 'llrrrrrr', 'Setting & Rule & $C_{\\max}$ (h) & Affected & \\shortstack{Affected\\\\vs URG} & \\shortstack{Affected vs\\\\LB-LA} & \\shortstack{Pipet C\\\\share (\\%)} & \\shortstack{Within\\\\24 h (\\%)}', body, wide=True)
    body = []
    for s in ['CS6 three days, deterministic', 'CS6 three days, low variability']:
        for p in ['URG', 'SRPT', 'BNA', 'LB-LA']:
            r = by[s, p]
            body.append([s.replace('CS6 three days, ', '') if p == 'URG' else '', p,
                         f"{float(r['makespan_s_mean']) / 3600:.2f}",
                         f"{float(r['carry_over_day1_mean']):.1f}", f"{float(r['carry_over_day2_mean']):.1f}",
                         f"{float(r['affected_day1_mean']):.1f}", f"{float(r['affected_day2_mean']):.1f}",
                         f"{float(r['affected_day3_mean']):.1f}"])
    table('tab_multiday.tex', 'Three consecutive CS6 days with carry-over (stochastic: means over 30 replications). Carry-over: samples released but unfinished at the end of the day; affected samples are attributed to their release day.',
          'tab:multiday', 'llrrrrrr', 'Setting & Rule & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Carry-over\\\\day 1} & \\shortstack{Carry-over\\\\day 2} & \\shortstack{Affected\\\\day 1} & \\shortstack{Affected\\\\day 2} & \\shortstack{Affected\\\\day 3}', body, wide=True)


def t_robot():
    if not (ROOT / 'data/robot_load_sensitivity.csv').exists():
        return
    acc = {r['item']: float(r['value']) for r in rows('robot_accounting.csv')}
    body = [['Loaded sample transfers per job', '7 (9 for SVOC$_E$)', '--', 'Modelled'],
            ['Movements per analytical job (loaded + empty)', f"{acc['model_movements_per_analytical_job']:.1f}", f"{acc['industrial_movements_per_analytical_job']:.1f}", 'Model vs industrial'],
            ['Unresolved movements per analytical job', '--', f"{acc['unresolved_movements_per_analytical_job']:.1f}", 'Auxiliary, cleaning, consumables'],
            ['Robot time for 160 analytical jobs (s)', f(acc['model_robot_s_160_analytical_jobs']), f(acc['industrial_robot_s_160_jobs']), '15~s per movement'],
            ['Equivalent auxiliary service $a$ (s per transfer)', f"{acc['equivalent_auxiliary_s_per_transfer']:.2f}", '--', 'Closes the difference'],
            ['Robot time, 230-job day at that $a$ (\\% of 24~h)', f"{acc['model_robot_load_230_jobs_at_equivalent_pct']:.0f}", '--', 'Exceeds the day'],
            ['Robot bound with empty legs, nominal (s)', f(acc['robot_bound_nominal_s']), '--', 'Remark~\\ref{rem:empty}'],
            ['Auxiliary service making the robot bound exceed 24~h (s)', f"{acc['robot_certificate_threshold_s']:.2f}", '--', 'Provable infeasibility']]
    table('tab_robot_accounting.tex', 'Common accounting of robot work: modelled tasks versus the industrial movement count (160 analytical jobs, about 5050 movements of 15~s, 88\\% robot load).',
          'tab:robot-accounting', 'p{5.6cm}rrp{3.4cm}', 'Quantity & Model & Industrial & Note', body, size='footnotesize')
    d = {(float(r['auxiliary_s']), r['policy'], r['case']): r for r in rows('robot_load_sensitivity.csv')}
    cps = {r['variant']: r for r in rows('cp_sensitivity.csv')} if (ROOT / 'data/cp_sensitivity.csv').exists() else {}
    body = []
    avals = sorted({a for a, p, c in d if c == 'cs6_fullday'})
    for a in avals:
        row = [f'{a:g}']
        for p in ['URG', 'BNA', 'LB-LA']:
            r = d[a, p, 'cs6_fullday']
            row.append(f"{h(r['makespan_s'])} / {r['affected_samples']}")
        u = d[a, 'URG', 'cs6_fullday']
        row += [f"{float(u['robot_util_pct']):.0f}", f"{float(u['pipc_violation_share_pct']):.0f}"]
        key = {5.0: 'auxiliary_5s', 10.0: 'auxiliary_10s', 15.0: 'auxiliary_15s'}.get(a)
        row.append(h(cps[key]['makespan_s']) if key in cps else ('--'))
        body.append(row)
    table('tab_robot.tex', 'CS6 with auxiliary robot service $a$ before every pickup: makespan (h) / affected samples, URG robot utilisation, share of URG violation events before Pipet C, and best hard-wait-feasible RH-CP makespan (h) where computed. The last row uses the industrial-equivalent value.',
          'tab:robot', 'rrrrrrr', '$a$ (s) & URG & BNA & LB-LA & \\shortstack{Robot\\\\util. (\\%)} & \\shortstack{Pipet C\\\\share (\\%)} & \\shortstack{RH-CP,\\\\hard-wait}', body, wide=True)


def t_thresholds():
    body = []
    th = rows('threshold_sensitivity.csv')
    for W in ['150.0', '300.0', '600.0', '900.0']:
        sub = [r for r in th if r['case'] == 'cs6_fullday' and r['threshold_s'] == W]
        ur = next(r for r in sub if r['policy'] == 'URG')
        la = next(r for r in sub if r['policy'] == 'LB-LA')
        bn = next(r for r in sub if r['policy'] == 'BNA')
        body.append([f'Uniform {float(W):.0f}', f"{ur['fixed_schedule_violations']} / {ur['fixed_schedule_affected']}",
                     f"{ur['violations']} / {ur['affected_samples']}", f"{la['violations']} / {la['affected_samples']}",
                     f"{bn['violations']} / {bn['affected_samples']}"])
    if (ROOT / 'data/stage_budget_profile.csv').exists():
        pr = {r['policy']: r for r in rows('stage_budget_profile.csv') if r['case'] == 'cs6_fullday'}
        body.append(['Profile A', f"{pr['URG']['profile_fixed_violations']} / {pr['URG']['profile_fixed_affected']}",
                     f"{pr['URG']['profile_rerun_violations']} / {pr['URG']['profile_rerun_affected']}",
                     f"{pr['LB-LA']['profile_rerun_violations']} / {pr['LB-LA']['profile_rerun_affected']}",
                     f"{pr['BNA']['profile_rerun_violations']} / {pr['BNA']['profile_rerun_affected']}"])
    table('tab_thresholds.tex', 'CS6 waiting-limit sensitivity: events / affected samples. ``Fixed'' re-classifies the unchanged 300~s URG schedule; reruns use the new limits in the controllers.',
          'tab:thresholds', 'lrrrr', 'Limits (s) & Fixed URG & Rerun URG & Rerun LB-LA & Rerun BNA', body)


def t_weights():
    if not (ROOT / 'data/weight_sensitivity.csv').exists():
        return
    rs = rows('weight_sensitivity.csv')
    body = []
    for n in ['cs2_medium', 'cs6_fullday', 'cs8_day180', 'cs9_day60']:
        sub = [r for r in rs if r['case'] == n]
        d = next(r for r in sub if r['default'] == 'True')
        ms = [float(r['makespan_s']) for r in sub]
        af = [int(r['affected_samples']) for r in sub]
        dom = sum(1 for r in sub if float(r['makespan_s']) <= float(d['makespan_s']) + 1e-6 and int(r['affected_samples']) <= int(d['affected_samples'])
                  and (float(r['makespan_s']) < float(d['makespan_s']) - 1e-6 or int(r['affected_samples']) < int(d['affected_samples'])))
        body.append([LABEL[n], f"{h(d['urg_makespan_s'])} / {d['urg_affected']}", f"{h(d['makespan_s'])} / {d['affected_samples']}",
                     f"{min(ms) / 3600:.2f}--{max(ms) / 3600:.2f}", f'{min(af)}--{max(af)}', dom])
    table('tab_weights.tex', 'LB-LA weight sensitivity over $w_{viol}\\in\\{0,150,300,600,1200,2400\\}$~s and $w_{over}\\in\\{0,30,60,120,240,480\\}$~s (36 settings per case): makespan (h) / affected samples, ranges over the grid, and number of settings that dominate the default.',
          'tab:weights', 'lrrrrr', 'Case & URG & \\shortstack{LB-LA\\\\default} & \\shortstack{Grid range\\\\$C_{\\max}$ (h)} & \\shortstack{Grid range\\\\affected} & \\shortstack{Dominating\\\\settings}', body, wide=True)
    body = []
    pretty = {'full': 'Full score', 'no_starvation': 'No starvation', 'no_new_violation': '$w_{viol}=0$', 'no_overshoot': '$w_{over}=0$',
              'no_wait_penalties': '$w_{viol}=w_{over}=0$', 'no_slack_band': 'Slack band removed', 'selected_wait': 'Selected-wait penalty'}
    for r in rows('ablations.csv'):
        body.append([LABEL[r['case']], pretty[r['variant']], f(r['makespan_s'], 2), r['violations'], r['affected_samples'],
                     r['local_changed_decisions'], r['sequence_changed_epochs']])
    table('tab_ablations.tex', 'LB-LA score ablations: outcomes, local action changes at identical full-score states and executed-sequence changes.',
          'tab:ablations', 'llrrrrr', 'Case & Variant & \\shortstack{$C_{\\max}$\\\\(s)} & Events & Affected & \\shortstack{Local\\\\changes} & \\shortstack{Sequence\\\\changes}', body, wide=True)


def t_admission():
    ar = {(r['case'], r['admission_cap']): r for r in rows('admission_sensitivity.csv')}
    body = []
    for cap in ['1', '2', '3', '4']:
        a, c = ar['cs6_fullday', cap], ar['cs8_day180', cap]
        body.append([cap, h(a['makespan_s']), f"{a['violations']} / {a['affected_samples']}", a['max_external_backlog'],
                     h(a['p95_external_s']), h(c['makespan_s']), f"{c['violations']} / {c['affected_samples']}"])
    table('tab_admission.tex', 'BNA admission-cap sensitivity; events / affected samples at 300~s.', 'tab:admission', 'rrrrrrr',
          ' & \\multicolumn{4}{c}{CS6} & \\multicolumn{2}{c}{CS8}\\\\\\cmidrule(lr){2-5}\\cmidrule(lr){6-7} Cap & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Events /\\\\affected} & \\shortstack{Peak ext.\\\\queue} & \\shortstack{External\\\\$P_{95}$ (h)} & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Events /\\\\affected}', body, wide=True)


def t_physical():
    physical = {(r['case'], r['variant']): r for r in rows('physical_sensitivity.csv') if r['policy'] == 'LB-LA'}
    variants = [('separated_nominal', 'Separate nominal matrices'), ('empty_no_dwell', 'Empty dwell 0 s'), ('empty_half_dwell', 'Empty dwell 7.135 s'),
                ('loaded_dwell_minus20', 'Loaded dwell $-20\\%$'), ('loaded_dwell_plus20', 'Loaded dwell $+20\\%$'),
                ('traverse_half_speed', 'Traverse speed 0.75 m/s'), ('traverse_double_speed', 'Traverse speed 3 m/s')]
    body = []
    for v, label in variants:
        a, c = physical['cs6_fullday', v], physical['cs8_day180', v]
        body.append([label, h(a['makespan_s']), f"{a['violations']} / {a['affected_samples']}", h(c['makespan_s']), f"{c['violations']} / {c['affected_samples']}"])
    table('tab_physical.tex', 'Uncalibrated movement stress tests under LB-LA; events / affected samples at 300~s.', 'tab:physical', 'lrrrr',
          ' & \\multicolumn{2}{c}{CS6} & \\multicolumn{2}{c}{CS8}\\\\\\cmidrule(lr){2-3}\\cmidrule(lr){4-5} Timing scenario & $C_{\\max}$ (h) & Events / affected & $C_{\\max}$ (h) & Events / affected', body, wide=True)


def t_hard_wait_milp():
    b = []
    for r in rows('hard_wait_results.csv'):
        b.append([r['instance'], r['n_samples'], r['status'], f(r['incumbent_s'], 2), f(r['dual_bound_s'], 2), f(r['max_wait_s'], 2)])
    table('tab_hard_wait.tex', 'Reduced hard-wait-constrained MILP checks with a 120~s SCIP limit.', 'tab:hardwait', 'lrlrrr',
          'Instance & Jobs & Solver status & \\shortstack{Incumbent\\\\(s)} & \\shortstack{Dual bound\\\\(s)} & \\shortstack{Max wait\\\\(s)}', b, wide=True)


def t_targeted():
    if not (ROOT / 'data/cp_targeted_cs6.csv').exists():
        return
    cert = float(LB4['cs6_fullday']['certificate_lb_s'])
    body = []
    names = {'time-lag': 'Time-lag insertion', 'RH-CP': 'RH-CP (40 s per window)', 'CP-REF': 'CP-REF (RH-CP + polishing)'}
    for r in rows('cp_targeted_cs6.csv'):
        body.append([names[r['method'].split()[0]], h(r['makespan_s']), f(r['max_wait_s'], 1), h(r['mean_external_s']),
                     h(r['p95_external_s']), r['max_external_backlog'], f"{100 * (float(r['makespan_s']) / cert - 1):.2f}", f(r['wall_s'])])
    r0 = rows('cp_targeted_cs6.csv')[0]
    table('tab_targeted.tex', 'Targeted hard-wait feasibility search on CS6. All schedules have zero violation events and pass the timestamp audit. The gap refers to the certificate (78,177.52~s); the relaxed CP model reaches ' + f(r0['relaxed_cp_bound_s'], 1) + '~s within 120~s. Wall-clock times include the preceding phases.',
          'tab:targeted', 'lrrrrrrr', 'Method & \\shortstack{$C_{\\max}$\\\\(h)} & \\shortstack{Max wait\\\\(s)} & \\shortstack{Mean ext.\\\\(h)} & \\shortstack{Ext. $P_{95}$\\\\(h)} & \\shortstack{Peak ext.\\\\queue} & \\shortstack{Gap\\\\(\\%)} & \\shortstack{Wall\\\\(s)}', body, wide=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--paper-dir', type=Path)
    args = ap.parse_args()
    for fn in [t_cases, t_cs6, t_wait, t_primary, t_gaps, t_intermediate, t_design, t_stochastic, t_robot,
               t_thresholds, t_weights, t_admission, t_physical, t_hard_wait_milp, t_targeted]:
        try:
            fn()
        except (FileNotFoundError, KeyError, StopIteration) as e:
            print('skip', fn.__name__, e)
    if args.paper_dir:
        (args.paper_dir / 'tables').mkdir(exist_ok=True)
        for p in OUT.glob('*.tex'):
            shutil.copy2(p, args.paper_dir / 'tables' / p.name)
    print('tables written')
