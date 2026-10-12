#!/usr/bin/env python3
"""Compare equivalent successful hosted CI evidence; durations are estimates, not billing."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path

FULL = {'manual-base-guard','change-risk','preflight','policy-and-security','migration-safety',
        'database-schema-contract','integration-workspace-contract','frontend-provider-ui-contract',
        'k8s-manifest-validate','backend-smoke','backend-pytest','core-image-size-gate',
        'cli-package-build','dev-bigquery-parity','frontend-build','frontend-tests','website-build'}
REQUIRED_FULL = FULL - {'migration-safety','integration-workspace-contract','frontend-provider-ui-contract','k8s-manifest-validate'}

def comparison(before, after, baseline_jobs, candidate_jobs):
    for summary in (before, after):
        if summary.get('schema_version') != 1 or summary.get('result') != 'passed':
            raise ValueError('successful canonical CI coverage summaries are required')
        if len(summary['selected_jobs']) != len(set(summary['selected_jobs'])):
            raise ValueError('duplicate logical coverage')
        if (not REQUIRED_FULL.issubset(summary['selected_jobs']) or not set(summary['selected_jobs']).issubset(FULL)
                or summary.get('run_attempt') != 1):
            raise ValueError('equivalent full coverage on first successful attempts is required')
    for key in ('selected_jobs','source_digest','frontend_inventory_digest'):
        if before[key] != after[key]:raise ValueError('coverage/input mismatch: '+key)
    def cost(summary, payload):
        rows=payload['jobs']; start=[]; end=[]; minutes=0; names=[]
        for job in rows:
            if (job.get('conclusion') not in {'success','skipped'} or str(job.get('run_id'))!=str(summary['run_id'])
                    or job.get('head_sha')!=summary.get('run_head_commit', summary['commit']) or job.get('run_attempt')!=1):
                raise ValueError('unsuccessful, incomplete or wrong-run/commit job evidence')
            names.append(job['name'])
            if job.get('conclusion')=='skipped':continue
            if 'self-hosted' in job.get('labels',[]):continue
            if not any(label.startswith('ubuntu-') for label in job.get('labels',[])):
                raise ValueError('unrecognized hosted placement')
            a=dt.datetime.fromisoformat(job['started_at'].replace('Z','+00:00'));b=dt.datetime.fromisoformat(job['completed_at'].replace('Z','+00:00'))
            seconds=(b-a).total_seconds()
            if seconds<0:raise ValueError('negative duration')
            minutes+=max(1,math.ceil(seconds/60));start.append(a);end.append(b)
        if not start:raise ValueError('no completed hosted jobs')
        physical = {name.split(' (')[0] for name in names}
        if (physical != set(summary['physical_jobs']) | {'ci-required'} or len(names)!=len(set(names))
                or sum(name.startswith('frontend-tests (') for name in names)!=4):
            raise ValueError('missing or duplicate physical job/shard timing evidence')
        return minutes,(max(end)-min(start)).total_seconds()
    prior,_=cost(before,baseline_jobs);current,elapsed=cost(after,candidate_jobs)
    saving=1-current/prior
    return {'baseline_rounded_minutes_estimate':prior,'candidate_rounded_minutes_estimate':current,
            'reduction_percent_estimate':round(saving*100,2),'candidate_execution_seconds':elapsed,
            'accepted':saving>=.15 and elapsed<=360,'official_billing':'measure separately; this report uses job timestamps'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['baseline_summary','candidate_summary','baseline_jobs','candidate_jobs']:p.add_argument('--'+name.replace('_','-'),type=Path,required=True)
    a=p.parse_args()
    try:r=comparison(*(json.loads(getattr(a,name).read_text()) for name in ['baseline_summary','candidate_summary','baseline_jobs','candidate_jobs']))
    except (ValueError,KeyError,TypeError,OSError) as exc:raise SystemExit('UNVERIFIED: '+str(exc))
    print(json.dumps(r,indent=2));raise SystemExit(0 if r['accepted'] else 1)
