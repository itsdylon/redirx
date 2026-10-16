import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SearchConsolePanel } from './SearchConsolePanel';
let owner='owner-a';
vi.mock('../../contexts/AuthContext',()=>({useAuth:()=>({user:{id:owner}})}));
const json=(data:unknown)=>new Response(JSON.stringify({contract_version:'1.0.0',data,error:null,status:'succeeded',next_action:'none'}),{headers:{'Content-Type':'application/json'}});
let fetchMock:ReturnType<typeof vi.fn>;
beforeEach(()=>{owner='owner-a';fetchMock=vi.fn(async()=>json({properties:[{site_url:'sc-domain:private-a.test'}]}));vi.stubGlobal('fetch',fetchMock);localStorage.setItem('access_token','fixture');});
afterEach(()=>{cleanup();vi.unstubAllGlobals();localStorage.clear();});
it('clears loaded properties and selection when account changes',async()=>{
 const user=userEvent.setup();const view=render(<SearchConsolePanel migrationId="m1"/>);
 await user.click(screen.getByRole('button',{name:'Load properties'}));
 await user.selectOptions(await screen.findByRole('combobox'), 'sc-domain:private-a.test');
 expect(screen.getByRole('button',{name:'Sync selected property'})).toBeEnabled();
 owner='owner-b';view.rerender(<SearchConsolePanel migrationId="m1"/>);
 expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
 expect(screen.getByRole('button',{name:'Sync selected property'})).toBeDisabled();
});
it('aborts an in-flight property request and ignores its late account-A response',async()=>{
 let finish!:(response:Response)=>void;fetchMock.mockImplementation(()=>new Promise(resolve=>{finish=resolve}));
 const user=userEvent.setup();const view=render(<SearchConsolePanel migrationId="m1"/>);
 await user.click(screen.getByRole('button',{name:'Load properties'}));
 const signal=fetchMock.mock.calls[0][1].signal;owner='owner-b';view.rerender(<SearchConsolePanel migrationId="m1"/>);
 expect(signal.aborted).toBe(true);
 await act(async()=>finish(json({properties:[{site_url:'sc-domain:private-a.test'}]})));
 expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
});
