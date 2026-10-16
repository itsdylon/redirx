import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GscTrafficCard } from './GscTrafficCard';

let owner='owner-a';
const success=vi.fn(),info=vi.fn(),error=vi.fn();
vi.mock('../contexts/AuthContext',()=>({useAuth:()=>({user:{id:owner}})}));
vi.mock('@posthog/react',()=>({usePostHog:()=>({capture:vi.fn()})}));
vi.mock('sonner',()=>({toast:{success:(...a:unknown[])=>success(...a),info:(...a:unknown[])=>info(...a),error:(...a:unknown[])=>error(...a)}}));
let fetchMock:ReturnType<typeof vi.fn>;
const json=(data:unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
function mount(search=''){
 const client=new QueryClient({defaultOptions:{queries:{retry:false,staleTime:Infinity},mutations:{retry:false}}});
 const tree=()=> <QueryClientProvider client={client}><MemoryRouter initialEntries={['/review/s1'+search]}><GscTrafficCard sessionId="s1" gscMeta={null}/></MemoryRouter></QueryClientProvider>;
 const view=render(tree());return {...view,switchOwner:()=>{owner='owner-b';view.rerender(tree());}};
}
beforeEach(()=>{owner='owner-a';success.mockReset();info.mockReset();error.mockReset();fetchMock=vi.fn(async()=>json({success:true,configured:true,connected:false}));vi.stubGlobal('fetch',fetchMock);localStorage.setItem('access_token','fixture');});
afterEach(()=>{cleanup();vi.unstubAllGlobals();localStorage.clear();});
describe('Search Console server authority and owner scope',()=>{
 it('does not treat a connected return parameter as proof or start a traffic sync',async()=>{
  mount('?gsc=connected');await screen.findByRole('button',{name:'Connect Search Console'});
  await waitFor(()=>expect(info).toHaveBeenCalledWith('Search Console is not connected. Connect to continue.'));
  expect(fetchMock.mock.calls.some(([,init])=>init?.method==='POST')).toBe(false);
  expect(success).not.toHaveBeenCalledWith('Google Search Console connected');
 });
 it('syncs on return only after a positive server connection result',async()=>{
  fetchMock.mockImplementation(async(url:string)=>url.includes('/sync')?json({success:true,matched_urls:2,session_url_count:3,total_clicks:8,property:'sc-domain:example.test'}):json({success:true,configured:true,connected:true}));
  mount('?gsc=connected');await waitFor(()=>expect(fetchMock.mock.calls.some(([url])=>String(url).includes('/sync'))).toBe(true));
  expect(success).toHaveBeenCalledWith('Google Search Console connected');
 });
 it('does not expose the previous owner’s connected state while the next owner loads',async()=>{
  let finish!:(response:Response)=>void;
  fetchMock.mockResolvedValueOnce(json({success:true,configured:true,connected:true})).mockImplementation(()=>new Promise(resolve=>{finish=resolve}));
  const view=mount();await screen.findByRole('button',{name:'Pull Traffic Data'});view.switchOwner();
  expect(screen.queryByRole('button',{name:'Pull Traffic Data'})).not.toBeInTheDocument();
  await act(async()=>finish(json({success:true,configured:true,connected:false})));
  await screen.findByRole('button',{name:'Connect Search Console'});expect(fetchMock).toHaveBeenCalledTimes(2);
 });
 it('provides status recovery rather than claiming a disconnected account on fetch errors',async()=>{
  fetchMock.mockResolvedValue(json({user_message:'Unavailable'},503));mount();
  await screen.findByRole('alert');expect(screen.getByRole('button',{name:'Retry Search Console status'})).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Connect Search Console'})).not.toBeInTheDocument();
 });
});

describe('unconfirmed OAuth callback recovery',()=>{
 it.each([true,false])('reads saved status (%s) without syncing or repeating consent',async connected=>{
  fetchMock.mockImplementation(async()=>json({success:true,configured:true,connected}));
  mount('?gsc=unconfirmed');
  await waitFor(()=>expect(info).toHaveBeenCalledWith(connected
   ?'A Search Console connection is saved. You can pull traffic when ready.'
   :'No Search Console connection is saved. Start a new connection if needed.'));
  await screen.findByRole('button',{name:connected?'Pull Traffic Data':'Connect Search Console'});
  expect(fetchMock.mock.calls.every(([url])=>String(url).includes('/gsc/status'))).toBe(true);
  expect(success).not.toHaveBeenCalled();
 });
 it('keeps a failed read unconfirmed and offers status retry',async()=>{
  fetchMock.mockResolvedValue(json({user_message:'Unavailable'},503));mount('?gsc=unconfirmed');
  await waitFor(()=>expect(error).toHaveBeenCalledWith('Connection status is still unconfirmed. Retry Search Console status.'));
  expect(screen.getByRole('button',{name:'Retry Search Console status'})).toBeInTheDocument();
  expect(screen.queryByRole('button',{name:'Connect Search Console'})).not.toBeInTheDocument();
  expect(fetchMock.mock.calls.every(([url])=>String(url).includes('/gsc/status'))).toBe(true);
  expect(success).not.toHaveBeenCalled();
 });
});

it('does not confirm a malformed status response during callback recovery',async()=>{
 fetchMock.mockResolvedValue(json({success:true,connected:'false'}));mount('?gsc=unconfirmed');
 await screen.findByRole('button',{name:'Retry Search Console status'});
 expect(screen.queryByRole('button',{name:'Pull Traffic Data'})).not.toBeInTheDocument();
 expect(info).not.toHaveBeenCalledWith('A Search Console connection is saved. You can pull traffic when ready.');
 expect(fetchMock.mock.calls.every(([url])=>String(url).includes('/gsc/status'))).toBe(true);
});
