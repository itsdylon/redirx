// Real applied SQL in PostgreSQL (PGlite). Set CURRENT_BILLING_POSTGRES=1 and
// QUOTE_PG_MODULE_ROOT for separate TCP connections and the concurrency tests.
import assert from 'node:assert/strict';
import {readFile, readdir, mkdtemp} from 'node:fs/promises';
import {before, after, test} from 'node:test';
import {PGlite} from '@electric-sql/pglite';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {createServer} from 'node:net';
import {randomUUID} from 'node:crypto';
const real=process.env.CURRENT_BILLING_POSTGRES==='1';
let pg, second, cluster;
const read=p=>readFile(new URL(p,import.meta.url),'utf8');
const q=(sql,args=[])=>pg.query(sql,args);
const exec=sql=>real?pg.query(sql):pg.exec(sql);
const one=async(sql,args=[])=>(await q(sql,args)).rows[0];
const rpc=async(name,args,client=pg)=>(await client.query(`SELECT ${name}(${args.map((_,i)=>'$'+(i+1)).join(',')}) AS value`,args)).rows[0].value;
const A='10000000-0000-0000-0000-000000000001', B='10000000-0000-0000-0000-000000000002';
let serial=0;
async function fixture(old=501,newCount=2,user=A){
 const {id}=await one("INSERT INTO migration_records(user_id,old_origin,new_origin) VALUES($1,'https://old.example','https://new.example') RETURNING id",[user]);
 const f={id,user};
 for(const [side,n] of [['old',old],['new',newCount]]){
  const snap=await one("INSERT INTO inventory_snapshots(migration_id,user_id,side,policy_version) VALUES($1,$2,$3,'explicit_inventory_v1') RETURNING id",[id,user,side]);
  await q("INSERT INTO session_discovered_urls(inventory_id,side,url,count_key) SELECT $1,$2,'https://' || $2 || '.example/' || n,'https://' || $2 || '.example/' || n FROM generate_series(1,$3::int)n",[snap.id,side,n]);
  await q("UPDATE inventory_snapshots SET status='complete',page_count=$2,content_hash=repeat('a',64),completed_at=now() WHERE id=$1",[snap.id,n]); f[side]=snap.id;
 } return f;
}
const reserve=(f,sku='payg',key='checkout-'+(++serial),client=pg)=>rpc('reserve_current_checkout',[f.user,sku,sku==='payg'?f.id:null,sku==='payg'?f.old:null,sku==='payg'?f.new:null,key,sku!=='payg','price_'+sku,false],client);
async function pay(c,sub=null){
 return rpc('apply_current_checkout_payment',[c.id,'cs_'+c.id,'cus_'+c.user_id,sub,c.amount_cents,'usd','evt_pay_'+c.id]);
}
async function subscription(f,sku='studio'){
 const c=await reserve(f,sku); await pay(c,'sub_'+c.id);
 const period=await rpc('record_current_subscription_period',[c.id,'sub_'+c.id,'cus_'+c.user_id,'in_'+c.id,new Date(Date.now()-86400000).toISOString(),new Date(Date.now()+86400000*29).toISOString(),c.amount_cents,'evt_invoice_'+c.id]);
 return {c,period};
}
const run=(f,key='run-'+(++serial),checkout=null,sub=null,approve=false,max=0,client=pg)=>rpc('reserve_current_jev_run',[f.user,f.id,f.old,f.new,key,'[]',checkout,sub,approve,max,false],client);
before(async()=>{
 if(real){
  const root=process.env.QUOTE_PG_MODULE_ROOT;
  const {default:EmbeddedPostgres}=await import(pathToFileURL(join(root,'embedded-postgres/dist/index.js')).href);
  const server=createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r));
  cluster=new EmbeddedPostgres({databaseDir:await mkdtemp(join(tmpdir(),'current-billing-pg-')),user:'postgres',password:'fixture-only',port,persistent:false,initdbFlags:['--encoding=UTF8'],postgresFlags:['-h','127.0.0.1'],onLog:()=>{},onError:console.error});
  await cluster.initialise();await cluster.start();pg=cluster.getPgClient('postgres','127.0.0.1');second=cluster.getPgClient('postgres','127.0.0.1');await pg.connect();await second.connect();
 } else pg=new PGlite();
 await exec(await read('./legacy-fixture.sql'));
 await exec(await read('./current-billing-fixture.sql'));
 const names=await readdir(new URL('../migrations/',import.meta.url));
 for(const prefix of ['006','009','019','024','026','027','031','032','034','035','036','037','038','039','040','041','042','043','044','045','046','047','048','049','050','051','053','054','055','056','057','058','062','063','064','065','066','067','068','069']){
  const file=names.find(n=>n.startsWith(prefix+'_')&&n.endsWith('.sql'));
  try {await exec(await read('../migrations/'+file));}catch(e){e.message=file+': '+e.message;throw e;}
 }
 await q('INSERT INTO auth.users(id) VALUES($1),($2)',[A,B]);await q('INSERT INTO user_profiles(id) VALUES($1),($2)',[A,B]);
});
after(async()=>{if(real){await second?.end();await pg?.end();await cluster?.stop();}else await pg?.close();});

test('current quote is read-only, exact old-URL pricing, owned complete capacity',async()=>{
 const f=await fixture(501,2001);
 const quote=await rpc('current_billing_quote',[A,f.id,f.old,f.new]);assert.deepEqual(quote,{old_urls:501,amount_cents:1100,stripe_quantity:501});
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_checkouts')).n,0);
 await assert.rejects(rpc('current_billing_quote',[B,f.id,f.old,f.new]),/not_found/);
 const over=await fixture(15001);await assert.rejects(reserve(over),/capacity_exceeded/);
 const bigNew=await fixture(1,20001);await assert.rejects(reserve(bigNew),/capacity_exceeded/);
});
test('paid SQL accepts the full 15000/20000 envelope and rejects over 16 MiB',async()=>{
 const f=await fixture(15000,20000); const c=await reserve(f);assert.equal(c.amount_cents,30000);await pay(c);
 const admitted=await run(f,'max-capacity',c.id);assert.equal(admitted.sku,'payg');
 const job=await one('SELECT jsonb_array_length(old_urls) o,jsonb_array_length(new_urls) n FROM migration_sessions WHERE id=$1',[admitted.session_id]);assert.deepEqual(job,{o:15000,n:20000});
 const large=await fixture(1,1);
 const snap=await one("INSERT INTO inventory_snapshots(migration_id,user_id,side,policy_version) VALUES($1,$2,'old','explicit_inventory_v1') RETURNING id",[large.id,A]);
 await q("INSERT INTO session_discovered_urls(inventory_id,side,url,count_key) SELECT $1,'old','https://old.example/'||n||repeat('x',8000),'https://old.example/'||n||repeat('x',8000) FROM generate_series(1,2100)n",[snap.id]);
 await q("UPDATE inventory_snapshots SET status='complete',page_count=2100,content_hash=repeat('b',64),completed_at=now() WHERE id=$1",[snap.id]);
 await assert.rejects(reserve({...large,old:snap.id}),/capacity_exceeded/);
});
test('reserved payment before attach, atomic idempotency, immutable provider facts, event binding',async()=>{
 const f=await fixture();const c=await reserve(f,'payg','same-key');assert.equal(c.quantity,501);assert.equal(c.amount_cents,1100);
 assert.equal((await reserve(f,'payg','same-key')).id,c.id);
 await assert.rejects(reserve({...f,new:f.old},'payg','same-key'),/operation_conflict/);
 await assert.rejects(reserve(f),/operation_conflict/);
 const paid=await pay(c);assert.equal(paid.status,'paid');
 assert.equal((await rpc('attach_current_checkout',[c.id,'cs_'+c.id,'https://checkout.stripe.com/c/test'])).status,'paid');
 assert.equal((await pay(c)).status,'paid');
 await assert.rejects(rpc('apply_current_checkout_payment',[c.id,'cs_wrong','cus_'+A,null,c.amount_cents,'usd','evt_wrong']),/operation_conflict/);
 await assert.rejects(rpc('apply_current_checkout_payment',[c.id,'cs_'+c.id,'cus_'+A,null,c.amount_cents+1,'usd','evt_wrong']),/operation_conflict/);
 await assert.rejects(rpc('get_current_checkout',[B,c.id]),/not_found/);
 const result=await run(f,'paid-run',c.id);assert.equal(result.sku,'payg');assert.equal(result.status,'queued');
 const replay=await run(f,'paid-run',c.id);assert.equal(replay.run_id,result.run_id);assert.equal(replay.replayed,true);
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_usage WHERE checkout_id=$1',[c.id])).n,1);
 await assert.rejects(run(f,'another-run',c.id),/operation_conflict/);
 const job=await one('SELECT * FROM migration_sessions WHERE id=$1',[result.session_id]);assert.equal(job.old_urls.length,501);
 assert.equal((await one('SELECT quote_id,grant_id FROM migration_runs WHERE id=$1',[result.run_id])).quote_id,null);
 await q("UPDATE migration_sessions SET status='processing',locked_by='worker',attempt_count=1,lease_expires_at=now()+interval '1 hour' WHERE id=$1",[result.session_id]);
 await assert.rejects(rpc('authorize_migration_run_dispatch',[result.session_id,result.run_id,'wrong',1,'redirx_2026_10']),/operation_conflict/);
 await rpc('authorize_migration_run_dispatch',[result.session_id,result.run_id,'worker',1,'redirx_2026_10']);
 const state=await rpc('prepare_jev_pass',[result.session_id,result.run_id,'worker',1]);assert.equal(state.billing_sku,'payg');
 await q('UPDATE jev_runs SET provider_requests=4000 WHERE run_id=$1',[result.run_id]);
 const reservation=await rpc('reserve_jev_provider_call',[result.session_id,result.run_id,'worker',1,10000,100000,1000000]);assert.ok(reservation);
 await assert.rejects(rpc('reserve_jev_provider_call',[result.session_id,result.run_id,'worker',1,10000,100000,1000001]),/invalid_input/);
 for(let i=0;i<9;i++)await rpc('reserve_jev_provider_call',[result.session_id,result.run_id,'worker',1,10000,100000,1000000]);
 assert.equal(Number((await one('SELECT reserved_micro_usd n FROM jev_provider_daily_budget')).n),1000000);
 await assert.rejects(rpc('reserve_jev_provider_call',[result.session_id,result.run_id,'worker',1,10000,1,1000000]),/provider_budget_exhausted/);
 await assert.rejects(rpc('reserve_jev_provider_call',[result.session_id,result.run_id,'worker',0,10000,1,1000000]),/operation_conflict/);
 await rpc('finalize_migration_run_session',[result.session_id,result.run_id,'worker',1,'completed',null]);
 await q('UPDATE jev_runs SET seed_revision=1 WHERE run_id=$1',[result.run_id]);
 const refine=await rpc('refine_jev_run',[A,f.id,result.run_id,1,'refine']);assert.equal(refine.status,'queued');
 assert.equal((await rpc('refine_jev_run',[A,f.id,result.run_id,1,'refine'])).replayed,true);
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_usage WHERE checkout_id=$1',[c.id])).n,1);
 await rpc('revoke_current_billing',[c.id,null,'evt_refund','refunded']);
 await assert.rejects(rpc('validate_current_run_authority',[result.run_id]),/payment_required/);
 assert.equal((await pay(c)).status,'revoked');
});
test('browser-paid exact PAYG inventory resumes the original no-selection request',async()=>{
 const f=await fixture(502);await assert.rejects(run(f,'original-unpaid'),/payment_required/);
 const c=await reserve(f);await pay(c);
 const resumed=await run(f,'original-unpaid');assert.equal(resumed.checkout_id,c.id);assert.equal(resumed.sku,'payg');
 const replay=await run(f,'original-unpaid');assert.equal(replay.run_id,resumed.run_id);assert.equal(replay.replayed,true);
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_usage WHERE checkout_id=$1',[c.id])).n,1);
});
test('one separate free trial and untouched historical billing',async()=>{
 const f=await fixture(500,2000);const result=await run(f,'trial');assert.equal(result.sku,'trial');
 assert.equal((await run(f,'trial')).run_id,result.run_id);
 await assert.rejects(run(await fixture(1)),/payment_required/);
 assert.equal((await one('SELECT count(*)::int n FROM migration_purchase_grants')).n,0);
 assert.equal((await one('SELECT count(*)::int n FROM migration_price_quotes')).n,0);
});
test('subscription pool approval ceiling, once-rounded period, mode isolation and refund fence',async()=>{
 const f=await fixture(5000);const {c,period}=await subscription(f);
 await assert.rejects(reserve(f,'agency'),/operation_conflict/);
 await run(f,'included',null,c.id);
 const extra=await fixture(1);
 await assert.rejects(run(extra,'extra',null,c.id),/approval_required/);
 await run(extra,'approved',null,c.id,true,100);
 await run(await fixture(1),'approved2',null,c.id,true,100);
 const usage=await rpc('get_current_billing_usage',[A,false]);assert.equal(usage.used_old_urls,5002);assert.equal(usage.extra_amount_cents,100);assert.equal(usage.extra_old_urls,2);assert.equal(usage.subscription_id,c.id);
 assert.equal((await rpc('get_current_billing_usage',[A,true])).sku,'free');
 await assert.rejects(run(await fixture(49),'over-ceiling',null,c.id,true,100),/approval_required/);
 await assert.rejects(rpc('close_current_usage',['sub_'+c.id,period.period_end,'in_usage']),/not_ready/);
 await q("UPDATE redirx_billing_periods SET period_end=now()-interval '1 second' WHERE id=$1",[period.id]);
 const end=(await one('SELECT period_end::text AS period_end FROM redirx_billing_periods WHERE id=$1',[period.id])).period_end;
 const closed=await rpc('close_current_usage',['sub_'+c.id,end,'in_usage']);assert.equal(closed.extra_amount_cents,100);
 assert.equal((await rpc('close_current_usage',['sub_'+c.id,end,'in_usage'])).id,closed.id);
 await assert.rejects(rpc('close_current_usage',['sub_'+c.id,end,'in_other']),/operation_conflict/);
 await rpc('attach_current_usage_invoice_item',[period.id,'in_usage','ii_usage']);
 await assert.rejects(rpc('attach_current_usage_invoice_item',[period.id,'in_usage','ii_other']),/operation_conflict/);
 await assert.rejects(run(extra,'closed',null,c.id,true,1000),/payment_required/);
 await rpc('revoke_current_billing',[c.id,'sub_'+c.id,'evt_dispute','disputed','in_'+c.id]);
 assert.equal((await one('SELECT status FROM redirx_billing_periods WHERE id=$1',[period.id])).status,'revoked');
 const next=await rpc('record_current_subscription_period',[c.id,'sub_'+c.id,'cus_'+A,'in_next',new Date().toISOString(),new Date(Date.now()+86400000*30).toISOString(),7900,'evt_new_invoice']);
 assert.equal(next.status,'active');
 const newRun=await run(extra,'auto-monthly'); assert.equal(newRun.sku,'studio');
 await rpc('revoke_current_billing',[c.id,'sub_'+c.id,'evt_cancel','canceled']);
 await rpc('validate_current_run_authority',[newRun.run_id]);
 await assert.rejects(run(extra,'after-cancel',null,c.id),/payment_required/);
 const missing=await rpc('close_current_usage',['sub_'+c.id,new Date(Date.now()-86400000*90).toISOString(),'in_missing']);
 assert.deepEqual(missing,{extra_old_urls:0,extra_amount_cents:0,missing_period:true});
 await assert.rejects(rpc('close_current_usage',['sub_unknown',end,'in_unknown']),/not_found/);
});
test('SQL service boundary is private and denies direct billing writes',async()=>{
 await exec('SET ROLE authenticated');try{await assert.rejects(q('SELECT * FROM redirx_billing_checkouts'),/permission denied/);await assert.rejects(rpc('get_current_billing_usage',[A,false]),/permission denied/);}finally{await exec('RESET ROLE');}
 await exec('SET ROLE service_role');try{assert.ok(await rpc('get_current_billing_usage',[A,false]));await assert.rejects(q("UPDATE redirx_billing_periods SET used_old_urls=0"),/permission denied/);}finally{await exec('RESET ROLE');}
});
test('two independent PostgreSQL transactions cannot overspend an approved ceiling', {skip:!real}, async()=>{
 const f=await fixture(5000,2,B);const {c}=await subscription(f);await run(f,'B-included',null,c.id);
 const f1=await fixture(26,2,B),f2=await fixture(26,2,B);
 const results=await Promise.allSettled([run(f1,'race1',null,c.id,true,100,pg),run(f2,'race2',null,c.id,true,100,second)]);
 assert.equal(results.filter(r=>r.status==='fulfilled').length,1);assert.match(results.find(r=>r.status==='rejected').reason.message,/approval_required/);
 const usage=await rpc('get_current_billing_usage',[B,false]);assert.equal(usage.used_old_urls,5026);assert.equal(usage.extra_amount_cents,100);
});

async function newOwner(){
 const user=randomUUID();await q('INSERT INTO auth.users(id) VALUES($1)',[user]);await q('INSERT INTO user_profiles(id) VALUES($1)',[user]);return user;
}
test('early cancellation freezes approved extras, binds one terminal invoice and preserves run rights',async()=>{
 const user=await newOwner(),f=await fixture(5101,2,user),{c,period}=await subscription(f);
 const admitted=await run(f,'terminal-work',null,c.id,true,300);
 const frozen=await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_terminal_'+c.id]);
 assert.equal(frozen.id,period.id);assert.equal(frozen.terminal_settlement,true);assert.equal(frozen.extra_old_urls,101);assert.equal(frozen.extra_amount_cents,300);
 assert.ok(new Date(frozen.closed_at)<new Date(frozen.period_end));assert.equal(frozen.usage_invoice_id,null);
 await assert.rejects(run(await fixture(1,2,user),'canceled-work',null,c.id,true,1000),/payment_required/);
 await rpc('validate_current_run_authority',[admitted.run_id]);
 const retry=await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_terminal_'+c.id]);assert.equal(retry.closed_at,frozen.closed_at);
 const redelivery=await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_terminal_redelivery_'+c.id]);assert.equal(redelivery.id,frozen.id);assert.equal(redelivery.closed_at,frozen.closed_at);
 await assert.rejects(rpc('close_current_usage',['sub_'+c.id,period.period_end,'in_cycle_late']),/operation_conflict/);
 const invoice='in_terminal_'+c.id;
 assert.equal((await rpc('attach_current_terminal_usage_invoice',[period.id,invoice])).usage_invoice_id,invoice);
 assert.equal((await rpc('attach_current_terminal_usage_invoice',[period.id,invoice])).usage_invoice_id,invoice);
 await assert.rejects(rpc('attach_current_terminal_usage_invoice',[period.id,'in_changed']),/operation_conflict/);
 await rpc('attach_current_usage_invoice_item',[period.id,invoice,'ii_terminal_'+c.id]);
 assert.equal((await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_terminal_after_item_'+c.id])).stripe_invoice_item_id,'ii_terminal_'+c.id);
 // A delayed paid invoice can be recorded but may not revive a canceled sub.
 const start=new Date(Date.now()+86400000*31).toISOString(),end=new Date(Date.now()+86400000*61).toISOString();
 await rpc('record_current_subscription_period',[c.id,'sub_'+c.id,'cus_'+user,'in_late_'+c.id,start,end,7900,'evt_late_'+c.id]);
 const state=await rpc('get_current_checkout',[user,c.id]);assert.equal(state.status,'revoked');assert.equal(state.revoke_reason,'canceled');
 assert.equal((await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_terminal_after_late_'+c.id])).id,period.id);
});
test('terminal cancellation with zero usage or no recorded period never creates a charge',async()=>{
 const user=await newOwner(),f=await fixture(1,2,user),{c,period}=await subscription(f);
 const frozen=await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_zero_'+c.id]);assert.equal(frozen.extra_amount_cents,0);
 assert.equal(frozen.id,period.id);assert.equal(frozen.usage_settlement,'terminal');
 await assert.rejects(rpc('attach_current_terminal_usage_invoice',[period.id,'in_zero']),/operation_conflict/);
 const other=await newOwner(),g=await fixture(1,2,other),checkout=await reserve(g,'studio');await pay(checkout,'sub_'+checkout.id);
 const empty=await rpc('reserve_current_terminal_usage',['sub_'+checkout.id,'evt_empty_'+checkout.id]);
 assert.deepEqual(empty,{extra_old_urls:0,extra_amount_cents:0,missing_period:true,terminal_settlement:false});
 await rpc('record_current_subscription_period',[checkout.id,'sub_'+checkout.id,'cus_'+other,'in_backfill_'+checkout.id,new Date(Date.now()-10000).toISOString(),new Date(Date.now()+86400000).toISOString(),7900,'evt_backfill_'+checkout.id]);
 assert.deepEqual(await rpc('reserve_current_terminal_usage',['sub_'+checkout.id,'evt_empty_retry_'+checkout.id]),empty);
 await assert.rejects(run(g,'late-no-grant',null,checkout.id),/payment_required/);
 await assert.rejects(rpc('reserve_current_terminal_usage',['sub_unknown','evt_empty_'+checkout.id]),/not_found/);
 await assert.rejects(rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_empty_'+checkout.id]),/operation_conflict/);
});
test('cycle settlement already owns its invoice when cancellation arrives',async()=>{
 const user=await newOwner(),f=await fixture(5001,2,user),{c,period}=await subscription(f);await run(f,'cycle-work',null,c.id,true,100);
 await q("UPDATE redirx_billing_periods SET period_end=date_trunc('second',now())-interval '1 second' WHERE id=$1",[period.id]);
 const end=(await one('SELECT period_end::text AS t FROM redirx_billing_periods WHERE id=$1',[period.id])).t;
 const invoice='in_cycle_'+c.id;await rpc('close_current_usage',['sub_'+c.id,end,invoice]);
 const frozen=await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_after_cycle_'+c.id]);
 assert.equal(frozen.terminal_settlement,false);assert.equal(frozen.usage_invoice_id,invoice);assert.equal(frozen.extra_amount_cents,100);
 await assert.rejects(rpc('attach_current_terminal_usage_invoice',[period.id,'in_duplicate']),/operation_conflict/);
 await rpc('attach_current_usage_invoice_item',[period.id,invoice,'ii_cycle_'+c.id]);
});
test('terminal freeze races admission and cycle closure without losing or double-billing usage',{skip:!real},async()=>{
 const user=await newOwner(),f=await fixture(5001,2,user),{c,period}=await subscription(f);await run(f,'initial',null,c.id,true,100);
 const extra=await fixture(51,2,user);
 const raced=await Promise.allSettled([
  rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_race_terminal_'+c.id],pg),
  run(extra,'concurrent-start',null,c.id,true,200,second)]);
 assert.equal(raced[0].status,'fulfilled');
 const frozen=raced[0].value;
 if(raced[1].status==='fulfilled')assert.equal(frozen.extra_old_urls,52);
 else {assert.match(raced[1].reason.message,/payment_required/);assert.equal(frozen.extra_old_urls,1);}
 assert.equal((await one('SELECT used_old_urls FROM redirx_billing_periods WHERE id=$1',[period.id])).used_old_urls,frozen.used_old_urls);
 const other=await newOwner(),g=await fixture(5001,2,other),sub=await subscription(g);await run(g,'cycle-race-initial',null,sub.c.id,true,100);
 await q("UPDATE redirx_billing_periods SET period_end=date_trunc('second',now())-interval '1 second' WHERE id=$1",[sub.period.id]);
 const end=(await one('SELECT period_end::text AS t FROM redirx_billing_periods WHERE id=$1',[sub.period.id])).t;
 const settled=await Promise.allSettled([
  rpc('reserve_current_terminal_usage',['sub_'+sub.c.id,'evt_close_race_'+sub.c.id],pg),
  rpc('close_current_usage',['sub_'+sub.c.id,end,'in_race_'+sub.c.id],second)]);
 assert.equal(settled[0].status,'fulfilled');
 const saved=await one('SELECT * FROM redirx_billing_periods WHERE id=$1',[sub.period.id]);
 if(saved.usage_settlement==='cycle'){
  assert.equal(settled[1].status,'fulfilled');assert.equal(settled[0].value.terminal_settlement,false);assert.equal(saved.usage_invoice_id,'in_race_'+sub.c.id);
 } else {
  assert.equal(saved.usage_settlement,'terminal');assert.equal(settled[1].status,'rejected');assert.match(settled[1].reason.message,/operation_conflict/);assert.equal(saved.usage_invoice_id,null);
 }
});

test('owned graph deletion cascades billing and artifacts without restoring an account trial',async()=>{
 const user=await newOwner(),trial=await fixture(1,2,user);await run(trial,'cleanup-trial');
 await q('DELETE FROM migration_records WHERE id=$1',[trial.id]);
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_trials WHERE user_id=$1',[user])).n,1);
 const fresh=await fixture(1,2,user);await assert.rejects(run(fresh,'trial-again'),/payment_required/);
 const {c,period}=await subscription(fresh);await run(fresh,'cleanup-paid',null,c.id);
 const payg=await fixture(501,2,user),purchase=await reserve(payg);await pay(purchase);await run(payg,'cleanup-payg',purchase.id);
 await rpc('reserve_current_terminal_usage',['sub_'+c.id,'evt_cleanup_terminal_'+c.id]);
 await q('DELETE FROM auth.users WHERE id=$1',[user]);
 for(const table of ['migration_records','migration_runs','inventory_snapshots','redirx_billing_trials','redirx_billing_checkouts','redirx_billing_periods','redirx_billing_usage']){
  assert.equal((await one(`SELECT count(*)::int n FROM ${table} WHERE user_id=$1`,[user])).n,0,table);
 }
 assert.equal((await one('SELECT count(*)::int n FROM redirx_billing_events WHERE checkout_id IN($1,$2)',[c.id,purchase.id])).n,0);
 assert.ok(await rpc('get_current_billing_usage',[A,false]));
});

test('Python paid runner and completed output use real SQL without external providers', {skip:!real || !process.env.CURRENT_BILLING_PYTHON}, async()=>{
 const {spawn}=await import('node:child_process');
 const child=spawn(process.env.CURRENT_BILLING_PYTHON,['-m','unittest','backend.tests.test_current_paid_admission.PaidAdmission.test_paid_worker_retry_refine_and_permanent_export','-v'],{
  cwd:new URL('../../',import.meta.url),env:{...process.env,PREFLIGHT_TEST_DATABASE_URL:`postgresql://postgres:fixture-only@127.0.0.1:${cluster.options.port}/postgres`},stdio:'inherit'});
 const code=await new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',resolve);});assert.equal(code,0);
});
