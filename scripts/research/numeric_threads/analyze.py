"""Summarize frozen resource trials; no quality labels or tuning."""
import argparse
import json
from pathlib import Path
import random
import statistics as st

ARMS = ('default', '1', '2')
METRICS = ('full_seconds', 'cpu_seconds', 'peak_rss_bytes', 'preload_seconds', 'child_seconds')
PHASES = ('prepare_seconds', 'setup_prime_calibration_seconds', 'calibration_seconds',
          'page_seconds', 'teardown_seconds', 'stub_thread_summed_seconds')


def interval(values):
    rng = random.Random(20261016)
    boot = sorted(st.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return {'median': st.median(values), 'bootstrap95': [boot[249], boot[9749]], 'paired_values': values}


def analyze(directory):
    manifest = json.loads((directory/'manifest.json').read_text())
    assert manifest['exact_output_match'] and len(manifest['runs']) == 36
    records = []
    for item in manifest['runs']:
        row = json.loads((directory/item['file']).read_text())
        row['repetition'] = item['repetition']
        row['child_launch_to_exit_seconds'] = item['parent_elapsed_seconds']
        assert not row['memory'] and not row['validation'] and row['cache_released']
        for phase in PHASES: row[phase] = sum(j[phase] for j in row['jobs'])
        row['cpu_wall_ratio'] = row['cpu_seconds']/row['full_seconds']
        records.append(row)
    output = {}
    for workload in ('historical', 'synthetic'):
        rows = [r for r in records if r['workload'] == workload]
        assert len({r['output_sha256'] for r in rows}) == 1
        medians = {arm: {metric: st.median(r[metric] for r in rows if r['arm'] == arm)
            for metric in (*METRICS, *PHASES, 'cpu_wall_ratio', 'child_launch_to_exit_seconds')}
            for arm in ARMS}
        comparisons = {}
        for control, candidate in [('default', '1'), ('default', '2'), ('2', '1')]:
            a = {r['repetition']:r for r in rows if r['arm'] == control}
            b = {r['repetition']:r for r in rows if r['arm'] == candidate}
            comparisons[f'{candidate}-minus-{control}'] = {
                metric: interval([b[k][metric]-a[k][metric] for k in sorted(a)])
                for metric in ('full_seconds', 'cpu_seconds', 'peak_rss_bytes')}
        sites = {job['site']: {arm: {metric:st.median(j[metric] for r in rows if r['arm'] == arm
            for j in r['jobs'] if j['site'] == job['site'])
            for metric in ('full_seconds','cpu_seconds',*PHASES)} for arm in ARMS} for job in rows[0]['jobs']}
        ranges = {arm:{metric:[min(r[metric] for r in rows if r['arm']==arm), max(r[metric] for r in rows if r['arm']==arm)] for metric in METRICS} for arm in ARMS}
        output[workload] = {'medians': medians, 'ranges': ranges, 'paired': comparisons, 'sites': sites,
            'counts': [{k:j[k] for k in ('site','old_count','new_count','seeds','proposals','stub_calls','cache_reads','cache_keys')}
                       for j in rows[0]['jobs']], 'excluded': rows[0]['excluded']}
    return output, records


def plot(records, path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    colors = {'default':'#bc4b37', '1':'#237b69', '2':'#4076b2'}
    fig, axes = plt.subplots(2,2,figsize=(11,8))
    for column, workload in enumerate(('historical','synthetic')):
        for arm in ARMS:
            rows = [r for r in records if r['workload'] == workload and r['arm'] == arm]
            x = [r['full_seconds'] for r in rows]
            cpu = [r['cpu_seconds'] for r in rows]
            rss = [r['peak_rss_bytes']/1048576 for r in rows]
            n = rows[0]['threadpools_before'][0]['num_threads']
            label = f'Default ({n} BLAS)' if arm == 'default' else f'{arm} BLAS thread(s)'
            for ax,y in [(axes[0,column],cpu),(axes[1,column],rss)]:
                ax.scatter(x,y,color=colors[arm],alpha=.5,s=25)
                ax.scatter([st.median(x)],[st.median(y)],color=colors[arm],s=100,marker='D',label=label)
                ax.set_xlabel('Complete offline runner seconds (lower is better)')
                ax.grid(alpha=.15)
        axes[0,column].set_title('Historical: 10 sequential site runs' if workload=='historical' else 'Synthetic: 500 old / 2000 new')
        axes[0,column].set_ylabel('Process CPU seconds (lower is better)')
        axes[1,column].set_ylabel('Process high-water RSS, MiB')
        axes[0,column].legend(fontsize=8)
    fig.suptitle('Jev six-page-thread resource tradeoffs — macOS only\n6 fresh processes per arm; diamonds=medians; stub judgments, no network',fontsize=13)
    fig.text(.5,.013,'Separate workloads; no provider cost, deployed capacity or model-quality claim. RSS axis zoomed; includes preloaded fixture/imports.',ha='center',fontsize=9)
    fig.tight_layout(rect=(0,.035,1,.92))
    for extension in ('png','svg'): fig.savefig(path.with_suffix('.'+extension),dpi=160)
    plt.close(fig)


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--plot',action='store_true');args=p.parse_args()
    report,records=analyze(args.directory)
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    if args.plot: plot(records,args.out.parent/'resource-frontier')
