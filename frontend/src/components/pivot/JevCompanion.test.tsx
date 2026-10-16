import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import { PivotMigrationDetail } from '../PivotMigrationDetail';
import { PivotCompanionPage } from '../PivotCompanionPage';
vi.mock('../ToolLayout', () => ({ ToolLayout: ({ children }: { children: ReactNode }) => <>{children}</> }));
const envelope = (data: unknown, changes = {}) => ({ contract_version: '1.0.0', migration_id: 'm1', operation_id: 'op1', status: 'succeeded', next_action: 'none', data, error: null, ...changes });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
const limits={old_pages:500,new_pages:2000,inventory_bytes:2097152,passes:3,new_runs_per_24h:5};
let calls: Array<{path:string; method:string; body:any}>;
let pass:number, seed:number, stale:boolean, status:string, failures:number;
function mount(){return render(<MemoryRouter initialEntries={['/migrations/m1']}><Routes><Route path="/migrations/:migrationId" element={<PivotMigrationDetail/>}/></Routes></MemoryRouter>);}
beforeEach(()=>{
  calls=[];pass=1;seed=0;stale=false;status='succeeded';failures=0;localStorage.setItem('access_token','owned-test-token');
  vi.stubGlobal('fetch', vi.fn(async (url:string,init:RequestInit={})=>{
    const path=new URL(url,'https://fixture.test').pathname,method=init.method||'GET',body=init.body?JSON.parse(String(init.body)):{};calls.push({path,method,body});
    expect(init.headers).toMatchObject({Authorization:'Bearer owned-test-token'});
    if(path.endsWith('/refine')){
      if(failures-- >0)return json(envelope({}, {error:{code:'unavailable',message:'Retry refinement.',retryable:true,next_action:'retry'}}),503);
      pass++;return json(envelope({run_id:'run1'},{status:'queued',next_action:'poll'}));
    }
    if(method==='PATCH'){seed++;return json(envelope({outcomes:[{code:'ok'}]}));}
    if(path.endsWith('/matches'))return json(envelope({items:[{mapping_id:'map1',old_url:'https://old.test/a',new_url:null,revision:3,review_status:'needs_review',traffic_observed:false,traffic_clicks:null,jev_proposal:{target_url:'https://new.test/a',confidence:.84,confidence_kind:'model_estimate',pass,seed_revision:seed,stale,confirmation_required:true}}],next_cursor:null,selection_revision:3}));
    return json(envelope({migration:{id:'m1',name:'Jev migration'},run_id:'run1',run:{status},jev:{engine:'jev-url-v1',model:'jev-1.13.0',pass,seed_revision:seed,limits}}));
  }));
});
afterEach(()=>{cleanup();localStorage.clear();vi.unstubAllGlobals();});
describe('free Jev companion with actual API transport',()=>{
 it.each([true, false])('billing link preserves exact run inventories when available=%s', async available => {
  vi.stubGlobal('fetch', vi.fn(async () => json(envelope({ migration: { id: 'm1' },
    run: { status: 'succeeded', ...(available ? { inventory_ids: { old: 'owned-old', new: 'owned-new' } } : {}) }
  }))));
  mount(); await screen.findByText('Status: succeeded');
  const link = screen.getByRole('link', { name: 'Plans and billing for this migration' });
  const url = new URL(link.getAttribute('href')!, 'https://fixture.test');
  expect(Object.fromEntries(url.searchParams)).toEqual(available
    ? { migration_id: 'm1', old_inventory_id: 'owned-old', new_inventory_id: 'owned-new' }
    : { migration_id: 'm1' });
 });

 it('labels model proposals and saves confirmation only through revision-bound set_target',async()=>{
  const user=userEvent.setup();mount();await screen.findByText('Proposed destination: https://new.test/a');
  expect(screen.getByRole('heading',{name:'Free URL mapping'})).toBeInTheDocument();
  expect(screen.queryByText('Free Jev URL mapping')).not.toBeInTheDocument();
  expect(screen.getByText(/Model estimate: 84.0%/)).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Approve',exact:true})).not.toBeInTheDocument();
  expect(screen.queryByText('Payment required')).not.toBeInTheDocument();expect(screen.queryByText('Monitoring')).not.toBeInTheDocument();
  expect(calls.some(c=>c.method==='PATCH')).toBe(false);
  await user.click(screen.getByRole('button',{name:'Confirm proposed destination'}));
  await screen.findByText(/Confirmed-example revision 1/);
  expect(calls.find(c=>c.method==='PATCH')!.body.decisions).toEqual([{mapping_id:'map1',expected_revision:3,action:'set_target',target_url:'https://new.test/a'}]);
  expect(calls.filter(c=>c.path.endsWith('/matches')).every(c=>c.path.includes('/runs/run1/'))).toBe(true);
 });
 it('uses stable refinement retry identity and adopts refreshed pass and seed',async()=>{
  failures=1;seed=2;const user=userEvent.setup();mount();await screen.findByText(/Confirmed-example revision 2/);
  await user.click(screen.getByRole('button',{name:'Refine with confirmed examples'}));await screen.findByRole('alert');
  await user.click(screen.getByRole('button',{name:'Refine with confirmed examples'}));await screen.findByText(/Pass 2 of 3/);
  const requests=calls.filter(c=>c.path.endsWith('/refine'));expect(requests).toHaveLength(2);expect(requests[0].body).toEqual(requests[1].body);expect(requests[0].body.expected_seed_revision).toBe(2);expect(requests[0].body.idempotency_key).toBeTruthy();
 });
 it('keeps stale proposals for review, stops after three passes, and allows failed-pass resume',async()=>{
  pass=3;stale=true;mount();await screen.findByText(/predates the latest confirmed/);
  expect(screen.getByText(/Pass 3 of 3/)).toBeInTheDocument();
  expect(screen.getByText('All three passes are used. Continue reviewing and exporting this run.')).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'Confirm proposed destination'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'Refine with confirmed examples'})).toBeDisabled();
  status='failed';await userEvent.setup().click(screen.getByRole('button',{name:'Refresh status'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'Resume matching pass'})).toBeEnabled());
 });
 it('explains the explicit import workflow and free bounds without a subscription offer',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>json(envelope({items:[],next_cursor:null}))));
  render(<MemoryRouter><PivotCompanionPage/></MemoryRouter>);await screen.findByText(/No migrations yet/);
  expect(screen.getByText(/import both URL inventories/)).toBeInTheDocument();expect(screen.getByText(/500 old URLs, 2,000 new URLs/)).toBeInTheDocument();expect(screen.queryByText(/Studio/)).not.toBeInTheDocument();
 });
});
