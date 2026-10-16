import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { captureAccessibilityAudit } from './accessibilityAudit';
for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
  test(`historical review save, keyboard and export recovery at ${viewport.width}px`, async ({page,baseURL},testInfo) => {
    await page.setViewportSize(viewport);
    const user={id:'10000000-0000-4000-8000-000000000001',email:'fixture@example.com',full_name:'Fixture User',plan:'free'};
    const source='https://old.example/Case/%2F?a=1&a=2';
    let row={id:'row-1',oldUrl:source,newUrl:'https://new.example/Target?x=1&x=2',approved:false,confidence:90,confidenceBand:'high',matchScore:90,matchType:'semantic',warnings:[],pathSimilarity:90,titleSimilarity:90,contentSimilarity:90};
    let saves=0; const unexpected:string[]=[]; const errors:string[]=[]; const refWarnings:string[]=[];
    await page.addInitScript(user=>{
      const expires=Math.floor(Date.now()/1000)+3600;
      const token=`${btoa(JSON.stringify({alg:'HS256',typ:'JWT'}))}.${btoa(JSON.stringify({sub:user.id,exp:expires}))}.fixture-only`;
      localStorage.setItem('sb-127-auth-token',JSON.stringify({access_token:token,refresh_token:'fixture-refresh',token_type:'bearer',expires_in:3600,expires_at:expires,user:{...user,aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}}));
      localStorage.setItem('redirx_default_export_format','htaccess');
    },user);
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url());if(url.origin!==new URL(baseURL!).origin)return route.abort('blockedbyclient');
      const json=(body:unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
      if(url.pathname==='/api/auth/me')return json({success:true,user});
      if(url.pathname==='/api/user/onboarding')return json({success:true,onboarding_status:'dismissed',onboarding_state:{steps:{}}});
      if(url.pathname==='/api/gsc/status')return json({connected:false});
      if(url.pathname==='/api/results/fixture-session')return json({success:true,session:{id:'fixture-session',pipeline_type:'content'},mappings:[row]});
      if(url.pathname==='/api/results/fixture-session/mappings/row-1'){
        saves++;const body=route.request().postDataJSON();
        expect(body.expected_new_url).toBe(row.newUrl);
        if(saves===1)return json({success:false,error:'Fixture write outcome unknown'},503);
        row={...row,...(body.new_url?{newUrl:body.new_url}:{}),...(typeof body.approved==='boolean'?{approved:body.approved}:{})};return json({success:true});
      }
      if(url.pathname==='/api/results/fixture-session/alternatives/row-1')return json({success:true,alternatives:[]});
      if(url.pathname==='/api/watches')return json({success:true,watches:[]});
      if(url.pathname==='/api/results/fixture-session/export'){
        const included=row.approved?1:0;
        if(url.searchParams.get('format')==='apache' && included) return json({success:false,code:'unsupported_export_input',user_message:'This platform cannot express query-sensitive source rules safely.'},422);
        const content=included?`old_url,new_url,status\n${row.oldUrl},${row.newUrl},301`:'old_url,new_url,status';
        if(url.searchParams.get('preview')==='true')return json({success:true,content,filename:'redirects.csv',content_type:'text/csv',included_count:included,excluded_count:1-included,excluded:included?[]:[{mapping_id:'row-1',reason:'held_or_rejected'}],warning:null});
        return route.fulfill({status:200,contentType:'text/csv',body:content});
      }
      if(url.pathname.startsWith('/api/')){unexpected.push(url.pathname);return json({error:'Unexpected fixture request'},500);}
      return route.continue();
    });
    page.on('pageerror',error=>errors.push(error.message));page.on('console',message=>{if(message.text().includes('cannot be given refs'))refWarnings.push(message.text());});
    await page.goto('/review/fixture-session');
    const approve=page.getByRole('button',{name:`Approve ${source}`,exact:true});await expect(approve).toBeVisible();
    await expect(page.getByRole('heading',{name:'Review Redirects',level:1})).toHaveCount(1);
    await expect(page.getByRole('combobox',{name:'Filter by confidence'})).toBeVisible();
    await expect(page.getByRole('combobox',{name:'Sort redirects'})).toBeVisible();
    const progress=page.getByRole('progressbar',{name:'Redirect approval progress'});
    await expect(progress).toHaveAttribute('aria-valuenow','0');
    const selectRow=page.getByRole('checkbox',{name:`Select redirect from ${source}`,exact:true});
    await selectRow.focus();await page.keyboard.press('Space');await expect(selectRow).toBeChecked();await page.keyboard.press('Space');await expect(selectRow).not.toBeChecked();
    const expand=page.getByRole('button',{name:`Expand details for ${source}`,exact:true});await expand.focus();await page.keyboard.press('Enter');
    const collapse=page.getByRole('button',{name:`Collapse details for ${source}`,exact:true});await expect(collapse).toHaveAttribute('aria-expanded','true');await page.keyboard.press('Enter');await expect(expand).toBeFocused();
    const shortcuts=page.getByRole('button',{name:'Keyboard shortcuts',exact:true});await shortcuts.focus();await page.keyboard.press('Enter');
    const help=page.getByRole('dialog',{name:'Keyboard Shortcuts',exact:true});await expect(help).toBeVisible();await help.getByRole('button',{name:'Close',exact:true}).focus();await page.keyboard.press('Enter');await expect(help).toHaveCount(0);await expect(shortcuts).toBeFocused();
    const mod=await page.evaluate(()=>/Mac/.test(navigator.platform))?'Meta':'Control';
    await page.keyboard.press(`${mod}+k`);await expect(page.getByRole('textbox',{name:'Search old or new URLs'})).toBeFocused();
    await expect(page.getByRole('tooltip')).toHaveCount(0);
    await captureAccessibilityAudit(page,testInfo,`${viewport.width}-historical-review`);
    await approve.click();await expect(page.getByRole('alert')).toContainText('not confirmed');
    const exportButton=page.getByRole('button',{name:/^Export \(/});await expect(exportButton).toBeDisabled();expect(saves).toBe(1);
    await page.getByRole('button',{name:'Reload results',exact:true}).click();await expect(exportButton).toBeEnabled();
    await approve.click();await expect(page.getByRole('button',{name:`Remove approval for ${source}`,exact:true})).toBeVisible();expect(saves).toBe(2);
    await expect(progress).toHaveAttribute('aria-valuenow','100');
    const edit=page.getByRole('button',{name:`Edit redirect for ${source}`,exact:true});await edit.click();
    const editor=page.getByRole('dialog',{name:'Edit Redirect'});await expect(editor).toBeVisible();
    await page.getByLabel('Target URL').fill('https://new.example/Edited?x=2&x=1');await page.keyboard.press(`${mod}+e`);await expect(page.getByRole('dialog')).toHaveCount(1);
    await page.keyboard.press(`${mod}+k`);expect(await page.evaluate(()=>!!document.activeElement?.closest('[role="dialog"]'))).toBe(true);
    await page.getByRole('button',{name:'Save Changes'}).click();await expect(editor).toHaveCount(0);await expect(approve).toBeVisible();await expect(edit).toBeFocused();
    await exportButton.click();let modal=page.getByRole('dialog',{name:'Export Redirects'});await expect(modal).toBeVisible();await expect(page.getByText('Apache .htaccess',{exact:true})).toBeVisible();
    await expect(modal.getByText('0 redirects included. 1 excluded by export checks.')).toBeVisible();await expect(modal.getByRole('button',{name:'Download File'})).toBeDisabled();
    await modal.getByRole('button',{name:'Cancel',exact:true}).click();await approve.click();await expect(page.getByRole('button',{name:`Remove approval for ${source}`,exact:true})).toBeVisible();
    await exportButton.click();await expect(modal.getByRole('alert')).toContainText('query-sensitive');await expect(modal.getByRole('button',{name:'Download File'})).toBeDisabled();
    await modal.getByLabel('Export Format').click();await page.getByRole('option',{name:'Review CSV',exact:true}).click();await modal.getByLabel(/Full URLs from CSV/).check();
    await page.getByText('Preview Rules',{exact:true}).click();await expect(modal.locator('pre')).toContainText(source);await expect(modal.locator('pre')).toContainText('https://new.example/Edited?x=2&x=1');
    const preview=modal.getByRole('region',{name:'Redirect rules preview'});await preview.focus();await expect(preview).toBeFocused();
    if(viewport.width===390){expect(await preview.evaluate(element=>element.scrollWidth>element.clientWidth)).toBe(true);await page.keyboard.press('ArrowRight');await expect.poll(()=>preview.evaluate(element=>element.scrollLeft)).toBeGreaterThan(0);}
    await expect(page.getByText(/review change saved/, {exact:false})).toHaveCount(0, {timeout:7500});
    await page.screenshot({path:testInfo.outputPath('export-preview.png')});
    await captureAccessibilityAudit(page,testInfo,`${viewport.width}-historical-export`);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    for(let i=0;i<12;i++){await page.keyboard.press('Tab');expect(await page.evaluate(()=>!!document.activeElement?.closest('[role="dialog"]'))).toBe(true);}
    const download=page.waitForEvent('download');await page.getByRole('button',{name:'Download File'}).click();const file=await download;expect(file.suggestedFilename()).toBe('redirects.csv');
    const content=await readFile((await file.path())!,'utf8');expect(content).toBe(`old_url,new_url,status\n${source},https://new.example/Edited?x=2&x=1,301`);
    await expect(modal).toHaveCount(0);await expect(exportButton).toBeFocused();await expect(page.getByText('Download started: redirects.csv',{exact:true})).toBeVisible();
    await page.screenshot({path:testInfo.outputPath('review-confirmed.png'),fullPage:true});
    expect(errors).toEqual([]);expect(unexpected).toEqual([]);expect(refWarnings).toEqual([]);
  });
}
