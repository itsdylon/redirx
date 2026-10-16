import {chromium,expect} from '@playwright/test';
import {readFile,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
let raw='';for await(const chunk of process.stdin)raw+=chunk;
const f=JSON.parse(raw);
if(new URL(f.origin).hostname!=='127.0.0.1')throw new Error('Loopback fixture origin required.');
const browser=await chromium.launch({headless:true});
try{
 const context=await browser.newContext({viewport:{width:f.width,height:900},acceptDownloads:true,serviceWorkers:'block'});
 const external=[],pageErrors=[];
 await context.route('**/*',route=>{
  if(new URL(route.request().url()).origin!==f.origin){external.push(route.request().url());return route.abort();}
  return route.continue(); // No v2 fulfillment: the Flask server supplies every response.
 });
 await context.addInitScript(({token,owner})=>{
  const expires=Math.floor(Date.now()/1000)+3600;
  localStorage.setItem('sb-127-auth-token',JSON.stringify({access_token:token,refresh_token:'fixture',token_type:'bearer',expires_in:3600,expires_at:expires,user:{id:owner,email:'fixture@example.invalid',aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}}));
 },{token:f.tokens.a,owner:f.owner});
 const page=await context.newPage();page.on('pageerror',error=>pageErrors.push(error.message));
 let downloadEvents=0;page.on('download',()=>downloadEvents++);
 const api=async(method,resource,data,owner='a')=>{
  const response=await context.request.fetch(f.origin+resource,{method,data,headers:{Authorization:`Bearer ${f.tokens[owner]}`}});
  return {status:response.status(),body:await response.json()};
 };
 const downloadHashes=[];
 const artifactFile=`native-artifact-${f.mcp_mode?'mcp':'http'}-${f.width}.csv`;
 if(f.mcp_mode){
  const denied=await context.request.post(f.origin+'/_fixture/mcp-export',{data:{arguments:{}}});
  expect(denied.status()).toBe(403);
  const malformed=await context.request.post(f.origin+'/_fixture/mcp-export',{data:{name:'plan_migration'},headers:{'X-Fixture-Control':f.control}});
  expect(malformed.status()).toBe(400);
 }
 const exportArtifact=async publication=>{
  if(!f.mcp_mode)return api('POST',`/api/v2/migrations/${f.migration}/artifacts`,publication);
  const response=await context.request.post(f.origin+'/_fixture/mcp-export',{
   data:{arguments:{migration_id:f.migration,...publication}},headers:{'X-Fixture-Control':f.control}});
  expect(response.status()).toBe(200);
  const body=await response.json();
  // The bridge is HTTP200 even for a failed MCP tool. Assert business envelope,
  // never invent a backend HTTP status from MCP success/failure.
  return {status:response.status(),body};
 };
 const expectExportDenied=result=>{
  if(f.mcp_mode){expect(result.body.status).toBe('failed');expect(result.body.error.code).toBe('inventory_incomplete');}
  else expect(result.status).toBe(409);
  expect(result.body.next_action).toBe('resolve_matches');
  expect(result.body.error.next_action).toBe('resolve_matches');
 };
 const matches=`/api/v2/migrations/${f.migration}/runs/${f.run}/matches`;
 const row=page.getByRole('list',{name:'Mapping decisions'}).getByRole('listitem');
 await page.goto(`${f.origin}/migrations/${f.migration}`);
 await expect(row).toHaveCount(1);await expect(row).toContainText(f.source);
 await expect(row).toContainText('Proposed destination');
 const premature=await exportArtifact({
  run_id:f.run,format:'csv',revision:0,allow_partial:false,idempotency_key:randomUUID()});
 expectExportDenied(premature);
 const input=row.getByRole('textbox');await input.fill(f.target);
 // Another explicit reviewer changes the persisted decision while the page is open.
 const other=await context.newPage();other.on('pageerror',error=>pageErrors.push(error.message));
 await other.goto(`${f.origin}/migrations/${f.migration}`);
 const otherRow=other.getByRole('list',{name:'Mapping decisions'}).getByRole('listitem');
 await otherRow.getByRole('button',{name:'Reject',exact:true}).click();
 await expect(otherRow.getByRole('status')).toContainText('Rejection saved');
 const [conflictResponse]=await Promise.all([
  page.waitForResponse(response=>response.url().endsWith(matches)&&response.request().method()==='PATCH'),input.press('Enter')]);
 expect(conflictResponse.status()).toBe(200);
 expect((await conflictResponse.json()).data.outcomes[0].code).toBe('revision_conflict');
 await expect(row.getByRole('alert')).toContainText('This mapping changed');
 await expect(row.getByRole('button',{name:'Set destination'})).toBeDisabled();await expect(input).toHaveValue(f.target);
 await page.screenshot({path:path.join(f.output,`native-conflict-${f.width}.png`)});
 await row.getByRole('button',{name:'Refresh mappings'}).focus();await page.keyboard.press('Enter');
 await expect(row.getByRole('status')).toContainText('Current mappings loaded');
 await expect(input).toHaveValue(f.target);await input.press('Enter');
 await expect(row.getByRole('status')).toContainText('Destination approved');
 let current=await api('GET',matches);expect(current.status).toBe(200);
 expect(current.body.data.items[0].decision_target).toBe(f.target);
 expect(current.body.data.items[0].old_url).toBe(f.source);
 const publication={run_id:f.run,format:'csv',revision:current.body.data.selection_revision,allow_partial:false,idempotency_key:randomUUID()};
 const exported=await exportArtifact(publication);
 expect(exported.status).toBe(200);expect(exported.body.data.included_count).toBe(1);
 const artifact=exported.body.data.artifact_id;
 const artifactPath=`/api/v2/migrations/${f.migration}/artifacts/${artifact}`;
 const expected=await api('GET',artifactPath);expect(expected.status).toBe(200);
 expect(expected.body.data.content).toContain(f.target);
 const expectedHash=createHash('sha256').update(expected.body.data.content).digest('hex');
 expect(exported.body.data.content_hash).toBe(expectedHash);
 await page.getByRole('button',{name:'Refresh status'}).click();
 const download=page.getByRole('button',{name:'Download redirects'});await expect(download).toBeEnabled();
 const readDownload=async()=>{
  const [file]=await Promise.all([page.waitForEvent('download'),download.click()]);
  const bytes=await readFile(await file.path());expect(bytes.toString()).toBe(expected.body.data.content);
  const hash=createHash('sha256').update(bytes).digest('hex');
  downloadHashes.push(hash);await writeFile(path.join(f.output,artifactFile),bytes);
  return hash;
 };
 const firstHash=await readDownload();
 // Later rejection changes current eligibility, never the published snapshot.
 await row.getByRole('button',{name:'Reject',exact:true}).click();
 await expect(row.getByRole('status')).toContainText('Rejection saved');
 expect(await readDownload()).toBe(firstHash);
 const replay=await exportArtifact(publication);
 expect(replay.status).toBe(200);expect(replay.body.data.artifact_id).toBe(artifact);
 current=await api('GET',matches);
 const denied=await exportArtifact({...publication,revision:current.body.data.selection_revision,idempotency_key:randomUUID()});
 expectExportDenied(denied);
 const wrongOwner=await api('GET',artifactPath,undefined,'b');expect(wrongOwner.status).toBe(404);
 // Revoke the fixture's external auth verdict; real backend decorator emits 401.
 await input.fill(f.target);
 const control=revoked=>context.request.post(f.origin+'/_fixture/access',{data:{revoked},headers:{'X-Fixture-Control':f.control}});
 expect((await control(true)).ok()).toBe(true);
 await input.press('Enter');await expect(row.getByRole('alert')).toContainText('Sign in or reconnect your agent');
 await expect(input).toHaveValue(f.target);await expect(download).toBeDisabled();
 expect((await control(false)).ok()).toBe(true);
 await page.getByRole('button',{name:'Refresh status'}).click();
 await expect(download).toBeEnabled();await expect(input).toHaveValue(f.target);
 expect(await readDownload()).toBe(firstHash);
 await expect(row).toContainText('Rejected.'); // recovery never re-submits the failed approval
 await page.screenshot({path:path.join(f.output,`native-recovered-${f.width}.png`)});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
 // Account transition through the installed SDK's real cross-tab subscriber.
 // OAuth session contents remain trusted fixtures; all profile/business replies
 // below are fetched from the actual Flask server before delaying delivery.
 const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done});return {promise,resolve}};
 const artifactSeen=deferred(),artifactGate=deferred(),artifactFinished=deferred();
 const profileSeen=deferred(),profileGate=deferred();
 let delayedArtifactHash,artifactDelivery;
 await context.route(f.origin+artifactPath,async route=>{
  const response=await route.fetch();expect(response.status()).toBe(200);
  const payload=await response.json();
  delayedArtifactHash=createHash('sha256').update(payload.data.content).digest('hex');
  artifactSeen.resolve();await artifactGate.promise;
  // The browser may cancel the client request after its owner unmounts.
  try{await route.fulfill({response});artifactDelivery='fulfilled'}catch(error){
   expect(route.request().failure()?.errorText).toContain('ERR_ABORTED');artifactDelivery='aborted';
  }
  finally{artifactFinished.resolve()}
 });
 await context.route(f.origin+'/api/auth/me',async route=>{
  if(route.request().headers().authorization===`Bearer ${f.tokens.b}`){
   const response=await route.fetch();expect(response.status()).toBe(200);
   expect((await response.json()).user.id).toBe(f.owner_b);
   profileSeen.resolve();await profileGate.promise;await route.fulfill({response});
  }else await route.continue();
 });
 const switchOwner=async(owner,token)=>page.evaluate(({owner,token})=>{
  const expires=Math.floor(Date.now()/1000)+3600;
  const session={access_token:token,refresh_token:'fixture',token_type:'bearer',expires_in:3600,expires_at:expires,
   user:{id:owner,email:'fixture@example.invalid',aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}};
  localStorage.setItem('sb-127-auth-token',JSON.stringify(session));
  const channel=new BroadcastChannel('sb-127-auth-token');
  channel.postMessage({event:'SIGNED_IN',session});channel.close();
 },{owner,token});
 try{
  await download.click();await artifactSeen.promise;
  expect(delayedArtifactHash).toBe(firstHash);
  await switchOwner(f.owner_b,f.tokens.b);await profileSeen.promise;
  await expect(page.getByRole('list',{name:'Mapping decisions'})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Download redirects'})).toHaveCount(0);
  await expect(page.getByRole('textbox',{name:`Destination for ${f.source}`})).toHaveCount(0);
  artifactGate.resolve();await artifactFinished.promise;
  const bRead=page.waitForResponse(response=>response.url()===f.origin+`/api/v2/migrations/${f.migration}`
   && response.request().headers().authorization===`Bearer ${f.tokens.b}`);
  profileGate.resolve();expect((await bRead).status()).toBe(404);
  await expect(page.getByRole('alert')).toContainText('not found');
  expect(downloadEvents).toBe(3);
  await page.screenshot({path:path.join(f.output,`native-other-owner-${f.width}.png`)});
  await switchOwner(f.owner,f.tokens.a);
  const returned=page.getByRole('list',{name:'Mapping decisions'}).getByRole('listitem');
  await expect(returned).toContainText(f.source);
  await expect(returned.getByRole('textbox')).toHaveValue(''); // no cross-owner retained draft
  await expect(page.getByRole('button',{name:'Download redirects'})).toBeEnabled();
  expect(downloadEvents).toBe(3);
 }finally{artifactGate.resolve();profileGate.resolve()}
 expect(pageErrors).toEqual([]);expect(external).toEqual([]);expect(downloadHashes).toEqual([firstHash,firstHash,firstHash]);
 console.log(JSON.stringify({passed:true,width:f.width,source:f.source,target:f.target,artifact_hash:firstHash,
  mode:f.mcp_mode?'mcp-companion':'http-companion',artifact_file:artifactFile,download_hashes:downloadHashes,
  account_switch:{old_artifact_hash:delayedArtifactHash,late_artifact_transport:artifactDelivery,download_events:downloadEvents,other_owner_status:404,returning_owner_draft:''},
  assertions:[f.mcp_mode?'native MCP import, Jev admission/replay and export/replay':'real HTTP import and Jev admission','unapproved full export denied','native SQL stale revision','raw target persistence','explicit approved export','exact downloaded bytes','immutable after rejection','new export denied','old export replay','other owner denied','auth expiry draft preservation and explicit recovery','owner switch hides prior state before profile readiness','late prior-owner artifact cannot download','returning owner does not inherit old draft'],page_errors:pageErrors,external_requests:external}));
 await context.close();
}finally{await browser.close();}
