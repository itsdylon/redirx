// Existing embedded-postgres dev dependency; isolated native DB and built UI.
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createServer} from 'node:net';
import {spawn} from 'node:child_process';
const root=fileURLToPath(new URL('../../',import.meta.url));
const require=createRequire(new URL('../../mcp-auth-server/package.json',import.meta.url));
const python=process.env.JOURNEY_PYTHON;
if(!python)throw new Error('Set JOURNEY_PYTHON to the installed application virtualenv Python.');
const run=(command,args,env={})=>new Promise((resolve,reject)=>{
 const child=spawn(command,args,{cwd:root,stdio:'inherit',env:{...process.env,...env}});
 child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(new Error(`${command} exited ${code}`)));
});
// One authoritative libpq guard, before builds or embedded database initialization.
await run(python,['scripts/testing/run_companion_journey.py','--validate-launch']);
if(process.env.MCP_COMPANION_JOURNEY==='true')await run(process.execPath,['mcp-server/node_modules/typescript/bin/tsc','-p','mcp-server/tsconfig.json']);
await run(process.execPath,['frontend/node_modules/vite/bin/vite.js','build','frontend'],{
 VITE_MCP_PIVOT_ENABLED:'true',VITE_SUPABASE_URL:'http://127.0.0.1:54321',
 VITE_SUPABASE_ANON_KEY:'native-browser-fixture',VITE_API_BASE_URL:'',VITE_PUBLIC_POSTHOG_KEY:''});
const suppliedDsn=process.env.PREFLIGHT_TEST_DATABASE_URL;
if(suppliedDsn){
 await run(python,['scripts/testing/run_companion_journey.py'],{JOURNEY_NODE:process.execPath});
}else{
const {default:EmbeddedPostgres}=await import(pathToFileURL(process.env.JOURNEY_PG_MODULE || require.resolve('embedded-postgres')));
const listener=createServer();await new Promise((resolve,reject)=>{listener.on('error',reject);listener.listen(0,'127.0.0.1',resolve)});
const port=listener.address().port;await new Promise(resolve=>listener.close(resolve));
const directory=await mkdtemp(path.join(tmpdir(),'redirx-native-browser-'));
const pg=new EmbeddedPostgres({databaseDir:directory,port,user:'postgres',password:'fixture-only',persistent:false,
 postgresFlags:['-h','127.0.0.1','-k',directory],onLog(){},onError:console.error});
try{
 await pg.initialise();await pg.start();
 await run(python,['scripts/testing/run_companion_journey.py'],{JOURNEY_NODE:process.execPath,
  PREFLIGHT_TEST_DATABASE_URL:`postgresql://postgres:fixture-only@127.0.0.1:${port}/postgres`});
}finally{await pg.stop();await rm(directory,{recursive:true,force:true});}

}
