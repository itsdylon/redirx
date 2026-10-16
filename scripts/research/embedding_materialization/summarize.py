"""Summarize recorded paired timings and allocation counters; never calls providers."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser()
p.add_argument('--receipt',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
a=p.parse_args()
if a.out.exists():p.error('Refusing to overwrite receipt')
r=json.loads(a.receipt.read_text())
rng=np.random.default_rng(20261016)
summary={}
for name,w in r['workloads'].items():
 rows={(row['arm'],row['repetition']):row for row in w['runs']}
 baseline=[row for row in w['runs'] if row['arm']=='baseline']
 metrics={}
 for metric in ('setup_prime_seconds','query_seconds','total_seconds'):
  savings=np.array([1-rows[('arrays',row['repetition'])][metric]/row[metric] for row in baseline])
  samples=np.median(savings[rng.integers(0,len(savings),size=(10000,len(savings)))],axis=1)
  metrics[metric]={'paired_fraction_saved_median':float(np.median(savings)),
                   'paired_fraction_saved_bootstrap95':np.quantile(samples,[.025,.975]).tolist()}
 memory={key:{'baseline':w['memory']['baseline'][key],'arrays':w['memory']['arrays'][key],
              'fraction_saved':1-w['memory']['arrays'][key]/w['memory']['baseline'][key]}
         for key in ('cache_payload_bytes','tracemalloc_retained_bytes','tracemalloc_peak_bytes')}
 summary[name]={'timing':metrics,'memory':memory}
report={'receipt_sha256':hashlib.sha256(a.receipt.read_bytes()).hexdigest(),'workloads':summary,
        'note':'Timing intervals describe eight pairs on this host, not site-population or production-network guarantees. Memory is Python/NumPy allocation accounting, not process RSS.'}
a.out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
