"""Built browser → actual Flask routes/serializers → disposable native SQL.

Trusted fixtures: external auth/profile/onboarding and saved model proposals.
Business API responses, review CAS, exports and DB ownership are not mocked.
"""
from contextlib import ExitStack
import base64
import json
import os
import queue
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.serving import make_server
from backend.tests import test_jev_pipeline as jev_fixture
from backend.tests import test_migration_run_service as run_fixture
from backend.tests.test_migration_planning import A, B, ROOT
from backend.tests.test_pivot_product_journey import QuietHTTP, SECRET
from backend.tests import test_pivot_product_journey as mcp_fixture
from backend.routes.internal_routes import internal_blueprint
from src.redirx.config import Config
from backend.services.auth_service import AuthService
from backend.services.jev_store import DurableStore
from backend.services.mcp_delegation_service import MCPDelegationService
from backend.routes.v2_routes import v2_blueprint
from backend.routes.migration_mapping_routes import create_migration_mapping_blueprint
from backend.routes.migration_artifact_routes import create_migration_artifact_blueprint
from src.redirx.database import SupabaseClient

SOURCE='https://old.example/Case%2FSource?utm_source=A%20B&X=%2FKeep'
TARGET='https://new.example/Case%2FKeep?x=A%20B&next=%2FHome'
PROPOSED='https://new.example/Proposed'


def token(owner):
    part=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return part({'alg':'HS256'})+'.'+part({'sub':owner,'exp':int(time.time())+3600})+'.trusted-fixture-only'


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL') and os.getenv('JOURNEY_NODE'), 'requires native browser launcher')
class CompanionNativeJourney(unittest.TestCase):
    cleanup_db=classmethod(run_fixture.RunDatabaseAcceptance.cleanup_db.__func__)
    sql=run_fixture.RunDatabaseAcceptance.sql
    claim=run_fixture.RunDatabaseAcceptance.claim
    stop_bridge=mcp_fixture.NativeProductJourney.stop_bridge
    read_bridge=mcp_fixture.NativeProductJourney.read_bridge
    tool=mcp_fixture.NativeProductJourney.tool
    @classmethod
    def setUpClass(cls):
        from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_dsn
        validate_disposable_postgres_dsn(os.environ['PREFLIGHT_TEST_DATABASE_URL'])
        jev_fixture.NativeJev.setUpClass.__func__(cls)

    def setUp(self):
        run_fixture.RunDatabaseAcceptance.setUp(self)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.mcp_mode=os.getenv('MCP_COMPANION_JOURNEY')=='true'
        self.mcp_calls=[];self.bridge_lock=threading.Lock()
        self.stack.enter_context(patch.dict(os.environ,{'MCP_PIVOT_ENABLED':'true','JEV_MVP_ENABLED':'true','MCP_PIVOT_DISCOVERY_ENABLED':'false',
            'MCP_PIVOT_VERIFICATION_ENABLED':'false','MCP_PIVOT_MONITORING_ENABLED':'false','POSTHOG_API_KEY':''}))
        self.stack.enter_context(patch.object(Config,'MCP_INTERNAL_SECRET',SECRET))
        self.stack.enter_context(patch.object(SupabaseClient,'get_client',return_value=self.native))
        self.stack.enter_context(patch.object(SupabaseClient,'get_admin_client',return_value=self.native))
        self.tokens={A:token(A),B:token(B)};self.revoked=False;self.control=uuid4().hex
        def verify(value):
            owner=next((owner for owner,saved in self.tokens.items() if saved==value),None)
            return SimpleNamespace(id=owner,email='fixture@example.invalid') if owner and not self.revoked else None
        self.stack.enter_context(patch.object(AuthService,'verify_token',side_effect=verify))
        # External analytics is a sink, not a replacement business handler.
        for module in ('backend.routes.v2_routes','backend.routes.migration_mapping_routes','backend.routes.internal_routes'):
            self.stack.enter_context(patch(module+'.capture'))
        app=Flask('native-companion',static_folder=None)
        app.config.update(TESTING=True,RATELIMIT_ENABLED=False,MAX_CONTENT_LENGTH=4_000_000)
        if self.mcp_mode:app.register_blueprint(internal_blueprint,url_prefix='/api/internal')
        app.register_blueprint(v2_blueprint,url_prefix='/api/v2')
        app.register_blueprint(create_migration_mapping_blueprint(),url_prefix='/api/v2')
        app.register_blueprint(create_migration_artifact_blueprint(),url_prefix='/api/v2')
        self.http_records=[]
        @app.after_request
        def recorded(response):
            if request.path.startswith(('/api/v2/','/api/internal/')):
                self.http_records.append({'method':request.method,'path':request.path,'status':response.status_code,
                    'owner':getattr(request,'api_user_id',None),
                    'credential_kind':'delegation' if MCPDelegationService().resolve(request.headers.get('Authorization','')[7:]) else 'browser_or_internal'})
            return response
        @app.get('/api/auth/me')
        def auth_profile():
            user=verify(request.headers.get('Authorization','')[7:])
            if not user:return jsonify({'error':'Fixture session unavailable'}),401
            return jsonify({'success':True,'user':{'id':user.id,'email':user.email,'plan':'free'}})
        @app.get('/api/user/onboarding')
        def onboarding():
            return jsonify({'success':True,'onboarding_status':'dismissed','onboarding_state':{'steps':{}}})
        @app.post('/_fixture/access')
        def access():
            if request.headers.get('X-Fixture-Control')!=self.control:abort(403)
            self.revoked=request.json['revoked'] is True
            return jsonify({'revoked':self.revoked})
        @app.post('/_fixture/mcp-export')
        def mcp_export():
            # Test-only bridge: no synthetic business response. The actual MCP
            # client calls export_redirects over HTTP, then delegates to Flask.
            if not self.mcp_mode or request.headers.get('X-Fixture-Control')!=self.control:abort(403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or set(body)!={'arguments'} or not isinstance(body['arguments'],dict):abort(400)
            return jsonify(self.mcp('export_redirects',body['arguments']))
        build=ROOT/'frontend/build'
        self.assertTrue((build/'index.html').is_file(),'Build the fixture frontend first.')
        @app.get('/')
        @app.get('/<path:name>')
        def assets(name='index.html'):
            if name.startswith('api/'):abort(404)
            return send_from_directory(build,name if (build/name).is_file() else 'index.html')
        self.http=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHTTP)
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.origin='http://127.0.0.1:'+str(self.http.server_port)

        if self.mcp_mode:
            self.log=tempfile.TemporaryFile(mode='w+t');self.addCleanup(self.log.close)
            env={**os.environ,'REDIRX_BACKEND_URL':self.origin,'MCP_INTERNAL_SECRET':SECRET,
                 'MCP_AUTH_MODE':'oauth','MCP_PIVOT_ENABLED':'true','POSTHOG_API_KEY':'',
                 'JOURNEY_OWNER_A':A,'JOURNEY_OWNER_B':B}
            self.bridge=subprocess.Popen([os.environ['JOURNEY_NODE'],str(ROOT/'mcp-server/scripts/pivot-journey-client.mjs')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1,env=env)
            self.addCleanup(self.stop_bridge)
            self.lines=queue.Queue()
            def read():
                for line in self.bridge.stdout:self.lines.put(line)
                self.lines.put(None)
            threading.Thread(target=read,daemon=True).start()
            self.assertTrue(self.read_bridge()['ready'])
            self.bridge.stdin.write(json.dumps({'method':'list','account':'A'})+'\n');self.bridge.stdin.flush()
            self.assertEqual(len(self.read_bridge()['result']['tools']),9)

    def mcp(self,name,arguments,account='A'):
        with self.bridge_lock:
            result=self.tool(name,arguments,account)
            self.mcp_calls.append({'name':name,'account':account,'status':result['status'],
                'next_action':result['next_action'],'migration_id':result.get('migration_id'),
                'run_id':result['data'].get('run_id'),'error_code':(result.get('error') or {}).get('code')})
            return result

    def api(self,path,body=None):
        headers={'Authorization':'Bearer '+self.tokens[A],'Content-Type':'application/json'}
        with urlopen(Request(self.origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers),timeout=15) as response:
            self.assertEqual(response.status,200)
            return json.load(response)

    def journey(self,width):
        # Both modes use real business HTTP/SQL. MCP mode additionally traverses
        # native SDK transport, tool argument conversion and signed delegation.
        plan={'old_site':'https://old.example','new_site':'https://new.example',
              'name':'Native browser review','idempotency_key':uuid4().hex}
        planned=self.mcp('plan_migration',plan) if self.mcp_mode else self.api('/api/v2/migrations',plan)
        mid=planned['migration_id'];self.assertIsNotNone(mid,planned);inventories={}
        for side,urls in [('old',[SOURCE]),('new',[TARGET,PROPOSED])]:
            key=uuid4().hex
            imported=(self.mcp('import_inventory',{'migration_id':mid,'side':side,'urls':urls,'idempotency_key':key})
                if self.mcp_mode else self.api(f'/api/v2/migrations/{mid}/inventories',{'side':side,
                    'rows':[{'url':url,'provenance':['csv']} for url in urls],'idempotency_key':key}))
            self.assertEqual(imported['data']['inventory']['status'],'complete',imported)
            inventories[side]=imported['data']['inventory']['id']
        run_key=uuid4().hex
        run_args={'migration_id':mid,'old_inventory_id':inventories['old'],'new_inventory_id':inventories['new'],'idempotency_key':run_key}
        queued=(self.mcp('run_migration',run_args) if self.mcp_mode
                else self.api(f'/api/v2/migrations/{mid}/runs',{'inventory_ids':inventories,'idempotency_key':run_key}))
        self.assertEqual(queued['status'],'queued',queued)
        rid=queued['data']['run_id']
        if self.mcp_mode:
            replay=self.mcp('run_migration',run_args)
            self.assertEqual(replay['data']['run_id'],rid)
            self.assertEqual(self.sql('SELECT count(*) n FROM migration_runs WHERE migration_id=%s',[mid])[0]['n'],1)
        self.assertEqual(self.sql('SELECT count(*) n FROM jev_runs WHERE run_id=%s',[rid])[0]['n'],1)
        # Trusted model-output fixture enters through the same leased proposal RPC
        # as the adapter; it cannot skip SQL fencing or implicitly approve a row.
        job=self.claim();self.runs.authorize_dispatch(job,'worker-test')
        store=DurableStore(self.native,job,'worker-test');state=store.rpc('prepare_jev_pass')
        mapping=store.rpc('save_jev_proposal',p_pass=state['pass'],p_seed_revision=state['pass_seed_revision'],
            p_old_url=SOURCE,p_proposal={'target_url':PROPOSED,'confidence':.73,'confirmation_required':True})
        self.runs.finalize_session(job,'worker-test','completed')
        self.assertEqual(self.sql('SELECT count(*) n FROM migration_mapping_decisions WHERE run_id=%s',[rid])[0]['n'],0)
        output=Path(os.environ['COMPANION_JOURNEY_OUTPUT_DIR']) if os.getenv('COMPANION_JOURNEY_OUTPUT_DIR') else Path(tempfile.mkdtemp(prefix='native-companion-evidence-'))
        output.mkdir(parents=True,exist_ok=True)
        config={'origin':self.origin,'migration':mid,'run':rid,'mapping':mapping,'source':SOURCE,'target':TARGET,
            'tokens':{'a':self.tokens[A],'b':self.tokens[B]},'owner':A,'owner_b':B,'control':self.control,'width':width,'output':str(output),'mcp_mode':self.mcp_mode}
        browser=subprocess.run([os.environ['JOURNEY_NODE'],str(ROOT/'frontend/tests/native/companion.mjs')],
            input=json.dumps(config),text=True,capture_output=True,timeout=90)
        self.assertEqual(browser.returncode,0,browser.stdout+'\n'+browser.stderr)
        report=json.loads(browser.stdout.strip().splitlines()[-1])
        self.assertTrue(report['passed'])
        artifact=self.sql('SELECT id,content_hash,verification_inputs FROM migration_artifacts WHERE run_id=%s',[rid])
        self.assertEqual(len(artifact),1)
        self.assertEqual(artifact[0]['verification_inputs']['redirects'][0]['source_url'],SOURCE)
        self.assertEqual(artifact[0]['verification_inputs']['redirects'][0]['expected_url'],TARGET)
        events=self.sql("SELECT action,resulting_values->>'target_url' AS target_url FROM migration_mapping_decision_events WHERE mapping_id=%s ORDER BY created_at,id",[mapping])
        self.assertEqual([row['action'] for row in events],['reject','set_target','reject'])
        self.assertEqual(events[1]['target_url'],TARGET)
        self.assertEqual(self.sql('SELECT url FROM session_discovered_urls WHERE inventory_id=%s',[inventories['old']])[0]['url'],SOURCE)
        if self.mcp_mode:
            self.assertEqual([call['name'] for call in self.mcp_calls],['plan_migration','import_inventory','import_inventory',
                'run_migration','run_migration']+['export_redirects']*4)
            self.assertEqual(sum(row['path']=='/api/internal/mcp/resolve' and row['status']==200 for row in self.http_records),1)
            delegated=[row for row in self.http_records if row['credential_kind']=='delegation']
            self.assertEqual(len(delegated),9)
            self.assertTrue(all(row['owner']==A for row in delegated))
        report.update(mcp_calls=self.mcp_calls,mode='mcp-companion' if self.mcp_mode else 'http-companion',database_version=self.sql('SHOW server_version')[0]['server_version'],
            native_decision_events=[row['action'] for row in events],artifact_hash=artifact[0]['content_hash'],http=self.http_records)
        (output/f"native-{'mcp-' if self.mcp_mode else ''}companion-{width}.json").write_text(json.dumps(report,indent=2)+'\n')

    def test_desktop_native_review_export_recovery(self):self.journey(1280)
    def test_mobile_native_review_export_recovery(self):self.journey(390)
