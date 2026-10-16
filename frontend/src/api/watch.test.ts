import {afterEach,expect,it,vi} from 'vitest';
import {checkNow,getWatch,listWatches,setWatchStatus} from './watch';
afterEach(()=>vi.unstubAllGlobals());
const reply=(data:unknown)=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
it('requires the server’s positive queue acknowledgment',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>reply({success:true})));
 await expect(checkNow('w1')).rejects.toThrow('not confirmed as queued');
 vi.stubGlobal('fetch',vi.fn(async()=>reply({success:true,queued:true})));
 await expect(checkNow('w1')).resolves.toBeUndefined();
});
it('requires the returned watch identity and status to match a requested mutation',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>reply({success:true,watch:{id:'other',status:'paused'}})));
 await expect(setWatchStatus('w1','paused')).rejects.toThrow('not confirmed');
 vi.stubGlobal('fetch',vi.fn(async()=>reply({success:true,watch:{id:'w1',status:'paused'}})));
 await expect(setWatchStatus('w1','paused')).resolves.toEqual({id:'w1',status:'paused'});
});
it('never turns incomplete history into an empty account',async()=>{
 vi.stubGlobal('fetch',vi.fn(async()=>reply({success:true})));
 await expect(listWatches()).rejects.toThrow('history was incomplete');
});

it.each([401,403,404,503])('retains HTTP %s status and message for lifecycle decisions',async status=>{
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({error:'Monitor unavailable.'}),{status})));
 await expect(getWatch('w1')).rejects.toMatchObject({name:'ApiError',status,message:'Monitor unavailable.'});
});
