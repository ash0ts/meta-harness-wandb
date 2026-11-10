#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render a task-paired comparison from analyze_campaign.py's reconciled JSON."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analysis', type=Path, required=True)
    p.add_argument('--output-prefix', type=Path, required=True)
    p.add_argument('--left-label', required=True)
    p.add_argument('--right-label', required=True)
    p.add_argument('--stage', default='Calibration')
    a = p.parse_args()
    doc = json.loads(a.analysis.read_text())
    names = list(doc['datasets'])
    data = [doc['datasets'][ds] for ds in names]
    assert sum(d['total'] for d in data) == len(doc['tasks'])
    for d in data:
        assert sum(d[k] for k in ('both_correct', 'gain', 'loss', 'both_wrong')) == d['total']
        assert d['left_correct'] + d['gain'] - d['loss'] == d['right_correct']
    totals = {k: sum(d[k] for d in data) for k in ('total', 'gain', 'loss', 'both_wrong')}
    macro_left = sum(d['left_correct']/d['total'] for d in data)/len(data)*100
    macro_right = sum(d['right_correct']/d['total'] for d in data)/len(data)*100
    assert abs((macro_right-macro_left)/100-doc['macro_delta']) < 1e-12
    colors = dict(left='#69737d', right='#195aa0', both_correct='#bccbd7', gain='#218467',
                  loss='#b94f30', both_wrong='#5b6067')
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11.5, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.spines.left': False, 'axes.spines.bottom': False,
                         'svg.fonttype': 'none', 'pdf.fonttype': 42})
    fig, (ax, bx) = plt.subplots(2, 1, figsize=(8.4, 8.3), gridspec_kw={'height_ratios': [1, 1]})
    fig.subplots_adjust(left=.24, right=.95, top=.81, bottom=.20, hspace=.73)
    fig.text(.055, .955, 'Few-shot gains were uneven across datasets', fontsize=18, weight='bold')
    fig.text(.055, .919, f'{a.stage} · gpt-oss-120b · {totals["total"]} paired test tasks', fontsize=12, color='#50565d')
    y = list(range(len(names)))
    for i, d in enumerate(data):
        left, right = d['left_correct']/d['total']*100, d['right_correct']/d['total']*100
        ax.plot([left, right], [i, i], color='#aeb9c2', lw=3, zorder=1)
        ax.scatter(left, i, color=colors['left'], marker='s', s=70, zorder=2)
        ax.scatter(right, i, color=colors['right'], marker='o', s=85, zorder=2)
        ax.text(left, i+.24, f'{left:.1f}%', ha='center', va='top', color=colors['left'], fontsize=10.5)
        ax.text(right, i-.24, f'{right:.1f}%', ha='center', va='bottom', color=colors['right'], weight='bold', fontsize=10.5)
    ax.set_yticks(y, [f'{ds}\n(n={d["total"]})' for ds, d in zip(names, data)])
    ax.set_ylim(len(names)-.45, -.55)
    ax.set_xlim(0, 100); ax.set_xticks([0,25,50,75,100]); ax.set_xlabel('Correct tasks (%)')
    ax.grid(axis='x', color='#e6e8eb'); ax.set_axisbelow(True); ax.tick_params(length=0, pad=9)
    ax.legend(handles=[Line2D([],[], color=colors['left'], marker='s', ls='', label=a.left_label),
                       Line2D([],[], color=colors['right'], marker='o', ls='', label=a.right_label)],
              loc='lower left', bbox_to_anchor=(-.02,1.16), frameon=False, ncol=2, borderaxespad=0)
    order = ('both_correct', 'gain', 'loss', 'both_wrong')
    labels = ('Both correct', 'Gain', 'Loss', 'Both wrong')
    for i, d in enumerate(data):
        pos=0
        for key in order:
            width=d[key]/d['total']*100
            bx.barh(i, width, left=pos, height=.50, color=colors[key], edgecolor='white', linewidth=1)
            if width >= 8:
                bx.text(pos+width/2, i, str(d[key]), ha='center', va='center', fontsize=11,
                        color='#26313b' if key=='both_correct' else 'white', weight='bold')
            pos+=width
        bx.text(0, i+.35, f'{d["gain"]} gains · {d["loss"]} '+('loss' if d['loss']==1 else 'losses')+f' · {d["both_wrong"]} shared failures',
                fontsize=10, va='top', color='#50565d')
    bx.set_yticks(y, names); bx.set_ylim(len(names)-.35,-.65)
    bx.set_xlim(0,100); bx.set_xticks([]); bx.tick_params(length=0, pad=9)
    bx.legend(handles=[Patch(facecolor=colors[k], label=label) for k,label in zip(order,labels)],
              loc='lower left', bbox_to_anchor=(-.02,1.04), ncol=4, frameon=False, fontsize=10,
              handlelength=1.1, columnspacing=1.1, borderaxespad=0)
    ci=[v*100 for v in doc['macro_delta_ci95']]
    delta=doc['macro_delta']*100
    fig.text(.055,.135,f'Equal-weight macro accuracy: {macro_left:.2f}% → {macro_right:.2f}%',fontsize=12,weight='bold')
    fig.text(.055,.105,f'Difference: {delta:+.2f} points · paired 95% interval [{ci[0]:.2f}, {ci[1]:.2f}]',fontsize=11.5)
    fig.text(.055,.055,'Each dataset has equal macro weight. Bars show task outcomes; labels are counts.\n'
             'Interval conditions on these fixed datasets and runs; it excludes model-sampling uncertainty.',
             fontsize=9.3,color='#50565d',linespacing=1.5)
    a.output_prefix.parent.mkdir(parents=True,exist_ok=True)
    for ext in ('png','svg','pdf'):
        fig.savefig(a.output_prefix.with_suffix('.'+ext), dpi=200, facecolor='white')
    plt.close(fig)
    with a.output_prefix.with_suffix('.csv').open('w',newline='') as f:
        fields=['dataset','total','left_correct','right_correct','both_correct','gain','loss','both_wrong','delta']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for name,d in zip(names,data):writer.writerow({'dataset':name,**{k:d[k] for k in fields[1:]}})
    contract={'question':'How much did full few-shot help, and what failures remain?',
              'stage':a.stage,'takeaway':f'{totals["gain"]} gains, {totals["loss"]} losses, {totals["both_wrong"]} tasks wrong in both runs.',
              'source_sha256':hashlib.sha256(a.analysis.read_bytes()).hexdigest(),
              'renderer':{'name':'matplotlib','version':matplotlib.__version__,'font':'DejaVu Sans'},
              'metric':'Correct tasks divided by all released test tasks per dataset; macro gives each dataset equal weight.',
              'missingness':'Every task has both boolean outcomes; missing outcomes would stop analysis.',
              'privacy':'Only aggregate counts, no question, answer or prediction text.',
              'forbidden_claims':['population-wide causal benefit','optimized harness result','independent replication','model-sampling confidence'],
              'text_alternative':f'{a.left_label} macro {macro_left:.2f}%; {a.right_label} macro {macro_right:.2f}%. '+
                  f'{delta:+.2f} percentage points, paired 95% interval [{ci[0]:.2f}, {ci[1]:.2f}]. See the companion CSV for all counts.'}
    a.output_prefix.with_suffix('.evidence.json').write_text(json.dumps(contract,indent=2)+'\n')


if __name__=='__main__':
    main()
