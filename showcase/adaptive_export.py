"""Hash-only per-decision Lens turns and a fully regraded W&B study table."""
from pathlib import Path
from adaptive_core import digest
from analyze_adaptive import inspect
from mhforge import util,manifest as mm,cli
from mhforge.ledger import Ledger
from mhforge.telemetry.exporter import Exporter,ExportUnit,_Built,_sha
from mhforge.telemetry import plan_spans as ps,plan_runs as pr,policy

class AdaptiveExporter(Exporter):
    def discover(self):
        units=[ExportUnit('adaptive:'+p.stem,'job',('agent_lens',),()) for p in sorted((self.paths.root/'adaptive/blocks').glob('*.json'))]
        if (self.paths.root/'adaptive/completed.json').exists():units.append(ExportUnit('study:adaptive','study',('wandb',),tuple(u.unit for u in units)))
        return units

    def _plan_lens(self,u,k):
        block=util.read_json(self.paths.root/'adaptive/blocks'/(u.unit.split(':',1)[1]+'.json'))
        m=self.manifest;ds=block['block']['dataset'];prefix=m['telemetry']['attribute_prefix'];text=ps._Text();turns=[];sources={}
        for rel in block['decisions']:
            p=self.paths.root/rel;r=util.read_json(p);sources[rel]=util.sha256_file(p)
            candidate=f"{r['policy']}_t{r['temperature']}_i{r['index']:04}"
            key=f"test.{candidate}.{ds}"+('' if r['repeat']==1 else '.r'+str(r['repeat']))
            records=[util.read_json(p) for p in sorted(self.paths.calls_root.glob(key+'.s*.a1/*.json'))]
            spans=[ps._llm_span(c,c['outcome'] or 'interrupted',r['example_sha256'],r['ended_utc'],policy.rules_for(m,ds),m,prefix,text) for c in records]
            attrs=ps._attrs(prefix,dataset=ds,policy=r['policy'],temperature=r['temperature'],repeat=r['repeat'],task_index=r['index'],was_correct=bool(r['correct']),status=r['status'],early_stop=r.get('early_stop',False),logical_calls=len(records),latency_s=r['latency_s'],request_sha256=r['request_sha256'])
            turns.append(ps._turn(f"task-{r['index']}",'fixed-adaptive-vote','1',r['started_utc'],r['ended_utc'],attrs,spans))
        conv={'conversation_id':self.campaign+'-'+digest(u.unit)[:20]+f'-e{k}','name':u.unit,'attributes':ps._attrs(prefix,campaign=self.campaign,cohort=util.read_json(self.paths.root/'adaptive/contract.json')['cohort']),'turns':turns}
        plan=ps._finish('job',u.unit,m,[conv],export_attempt=k,dataset=ds,text=text,sources=sources,problems=[])
        problems=ps.validate_plan(plan,m)
        if problems:raise ValueError(str(problems))
        return plan

    def _plan_wandb(self,u,k):
        summary,rows,_=inspect(self.paths,bootstrap=False)
        for r in rows:
            rec=self._doc(self.paths.receipt_file('adaptive:'+r['block_id'],'agent_lens'))
            if not self.dry_run and (not rec or rec['status']!='verified'):raise ValueError('Unverified Lens block')
            r['trace_url']=rec['urls'][0] if rec and rec.get('urls') else None
        metrics={'adaptive/decisions':len(rows),'adaptive/calls':sum(r['calls'] for r in rows),'adaptive/cost_usd':summary['costs']['usage_priced_micro_usd']/1e6,'adaptive/unknown_upper_usd':summary['costs']['unknown_cost_upper_micro_usd']/1e6,'adaptive/same_output_answer_changes':summary['same_output_audit'].get('answer_changes',0)}
        for temp,t in summary['by_temperature'].items():
            for pol,values in t['policies'].items():
                for key in ('macro_accuracy','mean_calls','early_stops','failed_policies','usage_priced_usd','latency_p50_s','latency_p95_s'):
                    metrics[f'adaptive/t{temp}/{pol}/{key}']=values[key]
        columns=list(rows[0]);table={'key':'study/adaptive','columns':columns,'rows':[[r[x] for x in columns] for r in rows],'backend':'wandb_table','input_columns':[],'output_columns':[],'score_columns':['correct']}
        tables={'schema':'mhforge.study-tables/1','backend':'wandb_table','tables':[table]}
        config=pr._config(self.manifest,{'protocol_sha256':summary['contract_sha256'],'cohort':summary['cohort'],'execution':'fixed_host_orchestration'})
        config.update(frozen_manifest_sha256=mm.manifest_sha256(util.read_json(self.paths.manifest)),effective_manifest_sha256=mm.manifest_sha256(self.manifest),export_amendment=dict(self.amendment))
        runs=pr._run(self.manifest,unit=u.unit,rid=digest(self.campaign+u.unit)[:24]+f'e{k}',job_type='adaptive-study',name=self.campaign,tags=['adaptive','fixed-policy','previously-inspected-tasks'],resume=False,config=config,summary=metrics,define_metrics=[],history=[metrics],tables=[table['key']],artifacts=[],export_attempt=k)
        return _Built(sha=_sha({'runs':runs,'tables':tables}),runs=runs,tables=tables,tables_sent=[table],skipped_tables=[],withheld=[],upload=[])

def export_all(m,paths,keys,offline=False):
    from mhforge.telemetry.adapter_agentlens import CoreweaveSink
    from mhforge.telemetry.adapter_wandb import WandbSdkSink
    with cli.sdk_process_environment(paths):
        exporter=AdaptiveExporter(m,paths,ledger=Ledger(paths.ledger,m),lens=None if offline else CoreweaveSink(m,environ=dict(keys)),wandb=None if offline else WandbSdkSink(m,environ=dict(keys),paths=paths),known_secrets=list(keys.values()),example_dir=Path(m['upstream']['example_dir']),dry_run=offline)
        failed=[]
        for u in exporter.discover():
            receipts=exporter.export_unit(u,retry_partial=True,retry_budget=True)
            status={s:r['status'] for s,r in receipts.items()}
            print({'event':'export','unit':u.unit,'status':status},flush=True)
            if any(s!=('dry_run' if offline else 'verified') for s in status.values()):failed.append(u.unit)
        return int(bool(failed))
