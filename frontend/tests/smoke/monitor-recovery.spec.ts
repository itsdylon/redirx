import {test,expect} from '@playwright/test';

for (const width of [1280,390]) test(`monitor access recovery stops repeated reads at ${width}px`,async({page,baseURL},info)=>{
  await page.setViewportSize({width,height:900});
  const user={id:'10000000-0000-4000-8000-000000000001',email:'fixture@example.com',plan:'free'};
  await page.addInitScript(user=>{
    const expires=Math.floor(Date.now()/1000)+3600;
    const token=`${btoa(JSON.stringify({alg:'HS256'}))}.${btoa(JSON.stringify({sub:user.id,exp:expires}))}.fixture`;
    localStorage.setItem('sb-127-auth-token',JSON.stringify({access_token:token,refresh_token:'fixture',token_type:'bearer',expires_in:3600,expires_at:expires,user:{...user,aud:'authenticated',role:'authenticated',app_metadata:{},user_metadata:{}}}));
  },user);
  let reads=0,available=false;
  const errors:string[]=[],unexpected:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==new URL(baseURL!).origin)return route.abort();
    const json=(data:unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
    if(url.pathname==='/api/auth/me')return json({success:true,user});
    if(url.pathname==='/api/user/onboarding')return json({success:true,onboarding_status:'dismissed',onboarding_state:{steps:{}}});
    if(url.pathname==='/api/watches/fixture'){
      reads++;
      if(!available)return json({error:'Monitor access unavailable.'},401);
      return json({watch:{id:'fixture',session_id:'session',old_domain:'old.example',status:'active',last_checked_at:'2026-10-16T12:00:00Z',check_interval_minutes:1440,created_at:'2026-10-15T12:00:00Z'},issues:[],checks:[{id:'check',started_at:'2026-10-16T12:00:00Z',status:'completed',urls_checked:10,urls_ok:10,issues_open:0}],summary:{open_issues:0,critical:0,clicks_at_risk:0}});
    }
    if(url.pathname.startsWith('/api/')){unexpected.push(url.pathname);return json({},500);}
    return route.continue();
  });
  await page.clock.install();
  await page.goto('/watch/fixture');
  await expect(page.getByRole('alert')).toHaveText('Monitor access unavailable.');
  await page.clock.runFor(90_000);
  expect(reads).toBe(1);
  await page.screenshot({path:info.outputPath('monitor-error.png'),fullPage:false});
  available=true;
  await page.getByRole('button',{name:'Retry loading monitor'}).focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading',{name:'All 10 checked URLs passed'})).toBeVisible();
  expect(reads).toBe(2);
  await page.clock.runFor(30_010);
  await expect.poll(()=>reads).toBe(3);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.goto('/unknown-route');
  await page.clock.runFor(90_000);
  expect(reads).toBe(3);
  expect(errors).toEqual([]);
  expect(unexpected).toEqual([]);
});
