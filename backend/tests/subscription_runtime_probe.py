"""Called only by the disposable PostgreSQL harness; no provider network."""
import json
import os
from types import SimpleNamespace

import psycopg
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_config
from psycopg import sql

from backend.services.migration_subscription_service import MigrationSubscriptionService
from backend.tests.test_migration_subscription_webhook_service import RecurringWebhookTest

config=json.loads(os.environ['SUBSCRIPTION_FIXTURE_CONNECTION'])
validate_disposable_postgres_config(config)
if config['password']!='local-fixture-only' or config['dbname']!='postgres':
    raise RuntimeError('A disposable local PostgreSQL fixture is required.')
scope=json.loads(os.environ['SUBSCRIPTION_FIXTURE_SCOPE'])
connection=psycopg.connect(**config,autocommit=True)
connection.execute('SET ROLE service_role')
class Client:
    def rpc(self,name,params):
        statement=sql.SQL('SELECT {}({})').format(sql.Identifier(name),sql.SQL(',').join(
            sql.SQL('{} => %s').format(sql.Identifier(key)) for key in params))
        def execute():
            return SimpleNamespace(data=connection.execute(statement,list(params.values())).fetchone()[0],error=None)
        return SimpleNamespace(execute=execute)
repository=SimpleNamespace(client=Client())
fixture=RecurringWebhookTest();fixture.setUp()
fixture.service.service=MigrationSubscriptionService(repository)
result=fixture.handle();retry=fixture.handle()
assert retry['replayed'] is True
service=fixture.service.service
queued=service.start_studio_run(scope['user'],result['subscription_id'],scope['migration_id'],scope['old'],scope['new'],scope['quote_id'],'signed-provider-run')
assert queued['run_id'] and queued['studio_reservation_id']
job=connection.execute("SELECT * FROM claim_next_job('signed-webhook-worker',now()+interval '10 minutes')").fetchone()
columns=[d.name for d in connection.execute("SELECT * FROM claim_next_job('unused',now()+interval '10 minutes') LIMIT 0").description]
job=dict(zip(columns,job))
assert job['id']==__import__('uuid').UUID(queued['session_id'])
connection.execute('SELECT authorize_migration_run_dispatch(%s,%s,%s,%s,%s)',[job['id'],job['mcp_run_id'],'signed-webhook-worker',job['attempt_count'],'test_only'])
connection.execute('SELECT finalize_migration_run_session(%s,%s,%s,%s,%s,%s)',[job['id'],job['mcp_run_id'],'signed-webhook-worker',job['attempt_count'],'completed',None])
work=service.get_migration_reservation(scope['user'],queued['studio_reservation_id'])
assert work['state']=='succeeded' and work['first_success_at']
print(json.dumps({'subscription_id':result['subscription_id'],'reservation_id':work['reservation_id'],'run_id':queued['run_id'],'completed':True,'replayed':retry['replayed']}))
connection.close()
