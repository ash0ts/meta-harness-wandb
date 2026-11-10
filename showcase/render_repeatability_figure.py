#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render the five-repeat validation study without treating repeats as new tasks."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analysis',type=Path,required=True);p.add_argument('--noise',type=Path,required=True)
    p.add_argument('--output-prefix',type=Path,required=True);a=p.parse_args()
    d=json.loads(a.analysis.read_text());noise=json.loads(a.noise.read_text())
    assert d['task_outcomes_regraded']==1300 and d['task_system_groups']==260
    assert d['groups_with_identical_requests']==260 and d['temperature']==0 and d['repeats']==5
    systems=['fewshot_all','no_memory'];labels=['Full few-shot','No memory'];colors=['#195aa0','#69737d']
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':12,'svg.fonttype':'none','pdf.fonttype':42})
    fig,ax=plt.subplots(figsize=(8,5.5));fig.subplots_adjust(left=.24,right=.94,top=.77,bottom=.40)
    fig.text(.055,.93,'Temperature zero did not make scores repeatable',fontsize=17,weight='bold')
    fig.text(.055,.875,'5 repeats · 130 validation tasks · identical request bodies within each task/system group',fontsize=10.5,color='#50565d')
    offsets=[-.12,-.06,0,.06,.12]
    for y,(system,label,color) in enumerate(zip(systems,labels,colors)):
        values=noise['macro_validation_accuracy'][system]
        stats=d['macro_validation_accuracy'][system]
        assert abs(min(values)-stats['macro_min'])<1e-6 and abs(max(values)-stats['macro_max'])<1e-6
        ax.hlines(y,min(values),max(values),lw=2,color=color)
        ax.scatter(values,[y+off for off in offsets],color=color,s=70,edgecolors='white',linewidths=.7,zorder=3)
        ax.text((min(values)+max(values))/2,y+.28,
                f'{min(values):.2f}–{max(values):.2f}%  |  SD {stats["macro_std"]:.2f} points',
                ha='center',va='top',fontsize=10.5,color=color)
    ax.set_yticks([0,1],labels);ax.set_ylim(1.53,-.45);ax.set_xlim(20,47)
    ax.set_xticks([20,25,30,35,40,45]);ax.set_xlabel('Macro validation accuracy (%)',labelpad=8)
    ax.grid(axis='x',color='#e6e8eb');ax.set_axisbelow(True);ax.tick_params(length=0,pad=9)
    for spine in ax.spines.values():spine.set_visible(False)
    fig.text(.055,.225,f'{d["groups_with_correctness_changes"]} of 260 task/system groups changed correctness',fontsize=13,weight='bold')
    fig.text(.055,.175,f'{d["groups_with_normalized_label_changes"]} groups changed their normalized answer label.',fontsize=11.5)
    fig.text(.055,.07,'Each dot is a repeat; lines show observed ranges, not confidence intervals.\n'
             'Macro scores weight USPTO (30), Symptom2Disease (50), and LawBench (50) equally.\n'
             'The loader shuffle used seed 42; no API seed was forwarded. Five repeats describe this study only.',
             fontsize=9.5,color='#50565d',linespacing=1.5)
    a.output_prefix.parent.mkdir(parents=True,exist_ok=True)
    for ext in ('png','svg','pdf'):fig.savefig(a.output_prefix.with_suffix('.'+ext),dpi=200,facecolor='white')
    plt.close(fig)
    with a.output_prefix.with_suffix('.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['system','repeat','macro_validation_accuracy_percent'])
        for system in systems:
            for repeat,value in enumerate(noise['macro_validation_accuracy'][system],1):w.writerow([system,repeat,value])
    contract={'question':'Do identical temperature-zero requests produce repeatable scores?',
        'takeaway':'Observed correctness varied despite identical request bodies; repeats are not new independent tasks.',
        'analysis_sha256':hashlib.sha256(a.analysis.read_bytes()).hexdigest(),
        'noise_sha256':hashlib.sha256(a.noise.read_bytes()).hexdigest(),
        'renderer':{'name':'matplotlib','version':matplotlib.__version__,'font':'DejaVu Sans'},
        'metric':'Equal-dataset macro validation accuracy; five repeats of the same 130 tasks per baseline.',
        'privacy':'Aggregate values only; no questions, answers or prediction text.',
        'forbidden_claims':['universal nondeterminism rate','1,300 independent tasks','population confidence interval'],
        'text_alternative':f'Full few-shot ranged {noise["macro_validation_accuracy"]["fewshot_all"]}; no memory ranged {noise["macro_validation_accuracy"]["no_memory"]}. Correctness changed in 31 of 260 task/system groups.'}
    a.output_prefix.with_suffix('.evidence.json').write_text(json.dumps(contract,indent=2)+'\n')


if __name__=='__main__':main()
