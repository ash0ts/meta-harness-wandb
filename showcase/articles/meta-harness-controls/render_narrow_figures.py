#!/usr/bin/env python3
"""Readable narrow equivalents of the article's saved-data figures; no model calls."""
from pathlib import Path
import csv,json,collections
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parent
DATA=json.loads((ROOT/'evidence/aggregates.json').read_text())
plt.rcParams.update({'font.family':'DejaVu Sans','svg.fonttype':'path','font.size':10})
rows=list(csv.DictReader((ROOT/'evidence/validation-scores.csv').open()))
fig,ax=plt.subplots(figsize=(4,4.8));fig.subplots_adjust(left=.12,right=.93,top=.65,bottom=.27)
fig.text(.09,.92,'Identical requests,\nvarying scores',fontsize=16,weight='bold',linespacing=1.15)
fig.text(.09,.77,'Temperature 0 · five validation repeats',fontsize=9)
for y,sys,label,col in [(0,'fewshot_all','Examples','#195aa0'),(1,'no_memory','Direct prompt','#69737d')]:
 vals=[]
 for rep in range(1,6):
  ss=[r for r in rows if r['baseline']==sys and int(r['repeat'])==rep]
  ds=sorted({r['dataset'] for r in ss})
  vals.append(100*sum(sum(int(r['correct']) for r in ss if r['dataset']==d)/sum(r['dataset']==d for r in ss) for d in ds)/len(ds))
 stats=DATA['repeatability']['macro_validation_accuracy'][sys]
 assert abs(min(vals)-stats['macro_min'])<1e-6
 ax.hlines(y,min(vals),max(vals),color=col,lw=2)
 ax.scatter(vals,[y+o for o in [-.12,-.06,0,.06,.12]],c=col,s=42,edgecolors='white',linewidth=.7,zorder=3)
 ax.text(20,y-.33,f'{label}: {min(vals):.2f}–{max(vals):.2f}%',fontsize=10,color=col)
ax.set_xlim(19,47);ax.set_ylim(1.32,-.5);ax.set_yticks([]);ax.set_xticks([20,30,40]);ax.set_xlabel('Mean dataset accuracy (%)',fontsize=10)
ax.grid(axis='x',color='#e6e8eb');ax.set_axisbelow(True)
for s in ax.spines.values():s.set_visible(False)
fig.text(.09,.15,'31 / 260 groups changed correctness',fontsize=10,weight='bold')
fig.text(.09,.065,'130 tasks · two baselines\nLines show ranges, not confidence intervals.',fontsize=9,linespacing=1.5,color='#50565d')
for ext in ['png','svg']:fig.savefig(ROOT/'assets'/('validation-repeatability-narrow.'+ext),dpi=180,facecolor='white')
plt.close(fig)
fig=plt.figure(figsize=(4,5.9));fig.text(.09,.93,'What the vote changed',fontsize=16,weight='bold');fig.text(.09,.88,'412 tasks · same tasks in both policies',fontsize=9)
colors=['#bdceda','#23876e','#b94f34','#5a6066']
for x,label,c in zip([.09,.40,.55,.70],['Both correct','Gain','Loss','Both wrong'],colors):
 fig.text(x,.835,label,fontsize=8,color=c if c!='#bdceda' else '#536775')
for i,ds in enumerate(['LawBench','Symptom2Disease','USPTO']):
 d=DATA['voting_paired']['datasets'][ds];y=.78-i*.185
 fig.text(.09,y,f'{ds} · {d["total"]} tasks',fontsize=11,weight='bold')
 fig.text(.09,y-.044,f'Single: {100*d["left_correct"]/d["total"]:.1f}%   →   Vote: {100*d["right_correct"]/d["total"]:.1f}%',fontsize=10)
 ax=fig.add_axes([.09,y-.11,.82,.042]);offset=0
 for k,c in zip(['both_correct','gain','loss','both_wrong'],colors):
  ax.barh(0,d[k],left=offset,height=.8,color=c,edgecolor='white');offset+=d[k]
 ax.set_xlim(0,d['total']);ax.axis('off')
 fig.text(.09,y-.14,f'{d["gain"]} gains · {d["loss"]} losses · {d["both_wrong"]} both wrong',fontsize=9)
fig.text(.09,.23,'Mean: 43.16% → 45.83%',fontsize=12,weight='bold')
fig.text(.09,.18,'Difference: +2.67 points\n95% paired interval: −0.20 to +5.65',fontsize=10,linespacing=1.5)
fig.text(.09,.06,'Single answer: temperature 0\nVote of five: temperature 1\nInterval holds the saved model outputs fixed.',fontsize=9,linespacing=1.5,color='#50565d')
for ext in ['png','svg']:fig.savefig(ROOT/'assets'/('voting-paired-narrow.'+ext),dpi=180,facecolor='white')
plt.close(fig)
for path in (ROOT/'assets').glob('*-narrow.svg'):
 path.write_text('\n'.join(line.rstrip() for line in path.read_text().splitlines())+'\n')
print('Rendered two narrow saved-data figures')
