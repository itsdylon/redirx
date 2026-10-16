const { chromium } = require('@playwright/test');
const { writeFileSync } = require('node:fs');
const { gzipSync } = require('node:zlib');
const base = process.env.STARTUP_BASE_URL || 'http://127.0.0.1:4285';
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('Use a loopback fixture server.');
if (!process.argv[2]) throw new Error('Usage: node scripts/measure-startup.cjs OUTPUT.json');
const user = { id:'10000000-0000-4000-8000-000000000001', email:'fixture@example.com', full_name:'Fixture User', plan:'free' };
(async () => {
  const browser = await chromium.launch();
  try {
  const results=[];
  for (const path of ['/companion', '/migrations/fixture', '/account', '/settings']) {
    for (let run=0;run<3;run++) {
      const context = await browser.newContext(); const page = await context.newPage();
      await page.addInitScript(user => {
        const expires=Math.floor(Date.now()/1000)+3600;
        const token=`${btoa(JSON.stringify({alg:'HS256',typ:'JWT'}))}.${btoa(JSON.stringify({sub:user.id,exp:expires}))}.fixture-only`;
        localStorage.setItem('sb-127-auth-token',JSON.stringify({access_token:token,refresh_token:'fixture-refresh',token_type:'bearer',expires_in:3600,expires_at:expires,user:{...user,aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}}));
      }, user);
      const unexpected=[]; const errors=[]; const requestedScripts=new Set();
      page.on('pageerror',e=>errors.push(e.message));
      page.on('request',request=>{if(request.url().startsWith(base+'/assets/')&&request.url().endsWith('.js'))requestedScripts.add(request.url().split('/').pop());});
      await page.route('**/*',async route=>{
        const url=new URL(route.request().url()); if(url.origin!==base)return route.abort('blockedbyclient');
        const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
        if(url.pathname==='/api/auth/me')return json({success:true,user});
        if(url.pathname==='/api/user/onboarding')return json({success:true,onboarding_status:'dismissed',onboarding_state:{steps:{}}});
        if(url.pathname==='/api/user/profile')return json({success:true,profile:{...user,company:'Fixture company'}});
        if(url.pathname==='/api/user/sessions')return json({success:true,sessions:[]});
        if(url.pathname==='/api/billing/status')return json({plan:'free',manage_portal_available:false,agency:{has_subscription:false,usage_pages:0}});
        if(url.pathname==='/api/v2/migrations')return json({contract_version:'1.0.0',error:null,data:{items:[],next_cursor:null}});
        if(url.pathname==='/api/v2/migrations/fixture')return json({contract_version:'1.0.0',status:'succeeded',next_action:'none',error:null,data:{migration:{id:'fixture',name:'Fixture migration'},jev:{engine:'jev-url-v1',model:'jev',pass:1,seed_revision:0,limits:{old_pages:500,new_pages:2000,inventory_bytes:2097152,passes:3,new_runs_per_24h:5}}}});
        if(url.pathname==='/api/v2/migrations/fixture/gsc')return json({contract_version:'1.0.0',error:null,data:{state:'disconnected'}});
        if(url.pathname==='/api/email/preferences')return json({success:true,preferences:{}});
        if(url.pathname.startsWith('/api/')){unexpected.push(url.pathname);return json({error:'Unexpected fixture request'});}
        return route.continue();
      });
      await page.coverage.startJSCoverage();
      const started=performance.now(); await page.goto(base+path);
      if(path==='/companion')await page.getByText('No migrations yet.',{exact:false}).waitFor();
      if(path==='/migrations/fixture')try { await page.getByRole('heading',{name:'Free URL mapping',exact:true}).waitFor({timeout:5000}); } catch(error) { console.error({url:page.url(),body:await page.locator('body').innerText(),errors,unexpected});throw error; }
      if(path==='/account')await page.getByText('Fixture company',{exact:true}).waitFor();
      if(path==='/settings')await page.getByRole('tab',{name:'Profile',exact:true}).waitFor();
      const readyMs=performance.now()-started;
      const coverage=await page.coverage.stopJSCoverage();
      const scripts=coverage.filter(entry=>entry.url.startsWith(base+'/assets/')&&entry.source).map(entry=>{
        const used=new Uint8Array(entry.source.length);
        const ranges=entry.functions.flatMap(fn=>fn.ranges).sort((a,b)=>(b.endOffset-b.startOffset)-(a.endOffset-a.startOffset));
        for(const range of ranges)used.fill(range.count>0?1:0,range.startOffset,range.endOffset);
        return {file:entry.url.split('/').pop(),bytes:Buffer.byteLength(entry.source),gzipBytes:gzipSync(entry.source).length,usedCharacters:used.reduce((sum,value)=>sum+value,0)};
      });
      if(errors.length || unexpected.length)throw new Error(JSON.stringify({path,errors,unexpected}));
      for(const file of requestedScripts)if(!scripts.some(script=>script.file===file))throw new Error(`Requested script missing from coverage: ${file}`);
      results.push({path,run,readyMs,scripts,bytes:scripts.reduce((s,x)=>s+x.bytes,0),gzipBytes:scripts.reduce((s,x)=>s+x.gzipBytes,0),errors,unexpected});
      await context.close();
    }
  }
  writeFileSync(process.argv[2],JSON.stringify(results,null,2));
  console.log(JSON.stringify(results.map(({scripts,...row})=>row),null,2));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
