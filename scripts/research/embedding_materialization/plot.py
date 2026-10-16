"""Render recorded time/memory tradeoffs; no benchmark or provider execution."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
p=argparse.ArgumentParser()
p.add_argument('--receipt',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
a=p.parse_args();r=json.loads(a.receipt.read_text())
names=['cache2500-threads1','cache2500-threads6','historical-capped-threads1','historical-capped-threads6']
titles=['Cache: 2,000 destinations + 500 queries · 1 thread','Cache: 2,000 destinations + 500 queries · 6 threads',
        'Historical: 357 queries / 10 capped sites · 1 thread','Historical: 357 queries / 10 capped sites · 6 threads']
for metric,suffix,title in [('tracemalloc_peak_bytes','peak','Peak traced allocations'),('cache_payload_bytes','payload','Retained cache payload')]:
 fig,axes=plt.subplots(2,2,figsize=(12,8.8),layout='constrained')
 for ax,name,panel_title in zip(axes.flat,names,titles):
  w=r['workloads'][name]
  for arm,label,color in [('baseline','Python lists','#64748b'),('arrays','Raw float32 copies','#0f766e')]:
   median=w['medians'][arm]['total_seconds']*1000
   times=[row['total_seconds']*1000 for row in w['runs'] if row['arm']==arm]
   memory=w['memory'][arm][metric]/1e6
   ax.scatter(times,[memory]*len(times),s=30,color=color,alpha=.35)
   ax.scatter([median],[memory],s=70,marker='D',color=color,edgecolors='white',linewidths=.8)
   ax.annotate(f'{label}\n{median:.1f} ms · {memory:.1f} MB',(median,memory),xytext=(0,13),textcoords='offset points',ha='center',fontsize=9,color=color)
  ax.set_title(panel_title,fontsize=10)
  ax.set_xlabel('Full setup + calibration + queries (ms)')
  ax.set_ylabel(title+' (MB)')
  ax.set_ylim(0,max(w['memory'][arm][metric]/1e6 for arm in ('baseline','arrays'))*1.28)
  ax.margins(x=.22)
  ax.grid(alpha=.2)
  ax.spines[['top','right']].set_visible(False)
 fig.suptitle(title+' versus full runtime — resource tradeoff, not Pareto dominance',fontsize=13)
 fig.supxlabel('Points: 8 cold-run timings · diamonds: medians · memory: separate allocation run · compare arms within each workload only',fontsize=9)
 for ext in ('svg','png'):fig.savefig(a.out.with_name(a.out.name+'-'+suffix).with_suffix('.'+ext),dpi=160)
