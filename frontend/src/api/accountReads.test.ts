import {afterEach, expect, it, vi} from 'vitest';
import {getUserProfile} from './user';
import {getBillingStatus} from './billing';
afterEach(()=>vi.unstubAllGlobals());
it.each([getUserProfile,getBillingStatus])('forwards cancellation to the actual account fetch', async read=>{
 const controller=new AbortController();
 let received: AbortSignal | null | undefined;
 vi.stubGlobal('fetch',vi.fn((_url: string,init: RequestInit)=>new Promise((_resolve,reject)=>{
   received=init.signal;
   init.signal?.addEventListener('abort',()=>reject(new DOMException('Cancelled','AbortError')));
 })));
 const result=read(controller.signal);
 const rejected=expect(result).rejects.toMatchObject({name:'AbortError'});
 expect(received).toBe(controller.signal);
 controller.abort(); await rejected;
});
