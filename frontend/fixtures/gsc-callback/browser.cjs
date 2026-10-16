const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const out=path.resolve(process.argv[2] || '../docs/research/historical-authority/gsc-callback');
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true});
 const errors=[],writes=[],views=[];
 try {
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:900}});
   page.on('pageerror',e=>errors.push(e.message));
   let state='connected',reads=0;
   await page.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/api/gsc/status'){
     reads++;
     if(state==='failure') return route.fulfill({status:503,json:{user_message:'Fixture status unavailable'}});
     return route.fulfill({json:{success:true,configured:true,connected:state==='connected'}});
    }
    if(url.pathname.startsWith('/api/')) {writes.push(url.pathname); return route.abort();}
    if(url.origin!=='http://127.0.0.1:4297') return route.abort();
    return route.continue();
   });
   for(state of ['connected','disconnected','failure']){
    const before=reads;
    await page.goto('http://127.0.0.1:4297/fixtures/gsc-callback/index.html?gsc=unconfirmed');
    const label=state==='connected'?'Pull Traffic Data':state==='disconnected'?'Connect Search Console':'Retry Search Console status';
    await page.getByRole('button',{name:label}).waitFor();
    const message=state==='connected'?'A Search Console connection is saved. You can pull traffic when ready.':state==='disconnected'?'No Search Console connection is saved. Start a new connection if needed.':'Connection status is still unconfirmed. Retry Search Console status.';
    await page.getByText(message,{exact:true}).waitFor();
    assert.ok(reads>before);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    await page.screenshot({path:path.join(out,`${state}-${width}.png`),fullPage:true});
    views.push({width,state,statusRead:true,noOverflow:true});
    if(state==='failure'){
     state='disconnected';
     await page.getByRole('button',{name:'Retry Search Console status'}).focus();
     await page.keyboard.press('Enter');
     await page.getByRole('button',{name:'Connect Search Console'}).waitFor();
     state='failure';
    }
   }
   await page.close();
  }
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify({views,keyboardStatusRetry:true,unexpectedApiCalls:writes,pageErrors:errors,providerCalls:0},null,2));
  console.log(JSON.stringify({views:views.length,keyboardStatusRetry:true,unexpectedApiCalls:writes,pageErrors:errors}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
