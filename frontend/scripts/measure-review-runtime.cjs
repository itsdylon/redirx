// Offline production fixtures only. CDP task time is cumulative browser CPU, not field INP.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {gzipSync}=require('node:zlib');
const {writeFileSync}=require('node:fs');
const assert=require('node:assert/strict');
const user={id:'10000000-0000-4000-8000-000000000001',email:'fixture@example.com',plan:'free'};
async function trial(browser,base,count,blocked=false,path="/migrations/fixture"){
 assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'loopback fixture required');
 const page=await browser.newPage(); const errors=[],unexpected=[],reads=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(user=>{const expires=Math.floor(Date.now()/1000)+3600;const token=`${btoa(JSON.stringify({alg:'HS256'}))}.${btoa(JSON.stringify({sub:user.id,exp:expires}))}.fixture`;localStorage.setItem('sb-127-auth-token',JSON.stringify({access_token:token,refresh_token:'fixture',token_type:'bearer',expires_in:3600,expires_at:expires,user:{...user,aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}}));},user);
 const all=Array.from({length:count},(_,i)=>({mapping_id:`row-${i}`,old_url:`https://old.example/page-${i}`,new_url:null,review_status:'needs_review',revision:0,traffic_observed:false,traffic_clicks:null,jev_proposal:{target_url:`https://new.example/page-${i}`,confidence:.8,pass:1,seed_revision:0,stale:false}}));
 await page.route('**/*',async r=>{const u=new URL(r.request().url());if(u.origin!==base)return r.abort();const json=data=>r.fulfill({contentType:'application/json',body:JSON.stringify(data)});const env=data=>({contract_version:'1.0.0',status:'succeeded',next_action:'none',error:null,data});
  if(blocked&&u.pathname.includes('/assets/PivotMigrationDetail-'))return r.abort();
  if(u.pathname==='/api/auth/me')return json({success:true,user});if(u.pathname==='/api/user/onboarding')return json({success:true,onboarding_status:'dismissed',onboarding_state:{steps:{}}});
  if(u.pathname==='/api/v2/migrations')return json(env({items:[],next_cursor:null}));
  if(u.pathname==='/api/v2/migrations/fixture')return json(env({migration:{id:'fixture',name:'Performance fixture'},run_id:'run',run:{status:'succeeded'},jev:{engine:'jev-url-v1',model:'fixture',pass:1,seed_revision:0,limits:{old_pages:500,new_pages:2000,passes:3}}}));
  if(u.pathname.endsWith('/matches')){reads.push(u.search);assert.equal(r.request().method(),'GET');const limit=Number(u.searchParams.get('limit'));return json(env({items:all.slice(0,limit),selection_revision:0,next_cursor:count>limit?'opaque':null}));}
  if(u.pathname.startsWith('/api/')){unexpected.push(u.pathname);return json({error:'unexpected'});}return r.continue();
 });
 await page.coverage.startJSCoverage(); await page.goto(base+path);
 if(blocked){await page.waitForTimeout(300);const result={blocked:true,text:await page.locator('body').innerText(),errors,unexpected};await page.close();return result;}
 if(path==='/companion') {
  await page.getByRole('heading',{name:'Your migrations'}).waitFor();
  const coverage=await page.coverage.stopJSCoverage();const scripts=coverage.filter(e=>e.url.startsWith(base+'/assets/')&&e.source).map(e=>({file:e.url.split('/').pop(),bytes:Buffer.byteLength(e.source),gzipBytes:gzipSync(e.source).length}));
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  await page.close();return {path,scripts,bytes:scripts.reduce((s,x)=>s+x.bytes,0),gzipBytes:scripts.reduce((s,x)=>s+x.gzipBytes,0),errors,unexpected};
 }
 const list=page.getByRole('list',{name:'Mapping decisions'});await list.getByRole('listitem').first().waitFor();
 if(count>20)await page.getByRole('button',{name:'Load up to 500 mappings for this filter'}).click();
 await page.waitForFunction(count=>document.querySelectorAll('[aria-label="Mapping decisions"] > li').length===count,count);
 const coverage=await page.coverage.stopJSCoverage();const scripts=coverage.filter(e=>e.url.startsWith(base+'/assets/')&&e.source).map(e=>({file:e.url.split('/').pop(),bytes:Buffer.byteLength(e.source),gzipBytes:gzipSync(e.source).length}));
 const cdp=await page.context().newCDPSession(page);await cdp.send('Performance.enable');
 const input=list.getByRole('listitem').first().getByRole('textbox');const value='https://new.example/Corrected%2FCase?x=A%20B';
 await input.focus();await page.evaluate(()=>new Promise(requestAnimationFrame));
 const before=Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m=>[m.name,m.value]));
 await input.pressSequentially(value);await page.evaluate(()=>new Promise(requestAnimationFrame));
 const after=Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m=>[m.name,m.value]));
 assert.equal(await input.inputValue(),value);assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
 const result={count,reads,bytes:scripts.reduce((s,x)=>s+x.bytes,0),gzipBytes:scripts.reduce((s,x)=>s+x.gzipBytes,0),scripts,tasks:Object.fromEntries(['TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration'].map(k=>[k,(after[k]-before[k])*1000])),errors,unexpected};await page.close();return result;
}
(async()=>{const browser=await chromium.launch();const result=[];const mode=process.argv[2]||'baseline';const bases=['compare','startup'].includes(mode)?[['baseline',process.env.BENCH_BASELINE_URL || 'http://127.0.0.1:43931'],['candidate',process.env.BENCH_CANDIDATE_URL || 'http://127.0.0.1:43932']]:[[mode,process.env.BENCH_BASELINE_URL || 'http://127.0.0.1:43931']];const repeats=mode==='compare'?8:1;for(let i=0;i<repeats;i++){for(const [variant,base] of i%2?[...bases].reverse():bases){if(mode==='startup'){result.push({variant,...await trial(browser,base,0,false,'/companion')});continue;}for(const count of [20,500])result.push({variant,repeat:i,...await trial(browser,base,count)});}}if(!['compare','startup'].includes(mode))result.push(await trial(browser,bases[0][1],20,true));await browser.close();writeFileSync(process.argv[3],JSON.stringify({node:process.version,chromium:browser.version(),keystrokes:'https://new.example/Corrected%2FCase?x=A%20B'.length,rows:result},null,2));console.log(JSON.stringify(result.map(({scripts,...r})=>r),null,2));})().catch(e=>{console.error(e);process.exitCode=1});
