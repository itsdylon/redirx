import { afterEach, describe, expect, it, vi } from 'vitest';
import { previewExport, fetchExportFile } from './exportAPI';
vi.mock('./config',()=>({API_BASE_URL:'',getAuthHeaders:()=>({Authorization:'Bearer fixture-only'})}));
const options={format:'csv',confidenceLevels:['high','low'],urlFormat:'full' as const};
const preview={success:true,content:'old_url,new_url,status\n/a,/b,301',filename:'redirects.csv',content_type:'text/csv',included_count:1,excluded_count:0,excluded:[],warning:null};
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});
describe('historical export client',()=>{
  it('authenticates preview with the precise selection and consumes server bytes',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(preview),{headers:{'content-type':'application/json'}}));vi.stubGlobal('fetch',fetch);
    expect(await previewExport('session/1',options)).toEqual(preview);
    const [url,init]=fetch.mock.calls[0];expect(url).toContain('session%2F1/export');const params=new URL(url,'https://fixture.test').searchParams;expect(params.get('confidence_levels')).toBe('high,low');expect(params.get('preview')).toBe('true');expect(init.headers.Authorization).toBe('Bearer fixture-only');
  });
  it('does not reinterpret 422 unsupported input or retry automatically',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({success:false,user_message:'Source query cannot be represented',code:'unsupported_export_input'}),{status:422}));vi.stubGlobal('fetch',fetch);
    await expect(previewExport('s',options)).rejects.toMatchObject({status:422,message:'Source query cannot be represented'});expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each([{...preview,success:false},{...preview,content:null},{...preview,filename:'../bad.csv'},{...preview,included_count:-1}])('rejects malformed successful preview %j',async data=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify(data))));await expect(previewExport('s',options)).rejects.toThrow('valid export preview');
  });
  it('rejects an HTML fallback instead of downloading a login page',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('<html>Sign in</html>',{headers:{'content-type':'text/html'}})));await expect(fetchExportFile('s',options)).rejects.toThrow('export file');
  });
  it('cancels reads when the owning modal is disposed',async()=>{
    const fetch=vi.fn((_url,init)=>new Promise((_resolve,reject)=>init.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')))));vi.stubGlobal('fetch',fetch);const controller=new AbortController();const request=previewExport('s',options,controller.signal);controller.abort();await expect(request).rejects.toThrow('Aborted');expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  });
  it('bounds a request that never finishes without retrying',async()=>{
    vi.useFakeTimers();const fetch=vi.fn((_url,init)=>new Promise((_resolve,reject)=>init.signal.addEventListener('abort',()=>reject(init.signal.reason))));vi.stubGlobal('fetch',fetch);const request=expect(previewExport('s',options)).rejects.toThrow('timed out');await vi.advanceTimersByTimeAsync(20000);await request;expect(fetch).toHaveBeenCalledTimes(1);
  });
});
