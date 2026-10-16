"""Full checkout flow over disposable SQL + signed SDK events; Stripe is mocked."""
import json
import os
from types import SimpleNamespace
import psycopg
from backend.tests.helpers.disposable_postgres import validate_disposable_postgres_config
from psycopg import sql
from backend.services.migration_subscription_service import MigrationSubscriptionService
from backend.tests import test_migration_subscription_checkout_service as checkout_fixtures

config=json.loads(os.environ['SUBSCRIPTION_FIXTURE_CONNECTION'])
validate_disposable_postgres_config(config)
if config['password']!='local-fixture-only' or config['dbname']!='postgres':
    raise RuntimeError('A disposable local PostgreSQL fixture is required.')
scope=json.loads(os.environ['SUBSCRIPTION_FIXTURE_SCOPE'])
connection=psycopg.connect(**config,autocommit=True);connection.execute('SET ROLE service_role')
class Table:
    def __init__(self,name):self.name=name;self.filters=[]
    def select(self,_):return self
    def eq(self,key,value):self.filters.append((key,value));return self
    def maybe_single(self):return self
    def execute(self):
        query=sql.SQL('SELECT to_jsonb(t) FROM {} t WHERE {}').format(sql.Identifier(self.name),sql.SQL(' AND ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k,_ in self.filters))
        row=connection.execute(query,[v for _,v in self.filters]).fetchone();return SimpleNamespace(data=row[0] if row else None,error=None)
class Client:
    def table(self,name):return Table(name)
    def rpc(self,name,params):
        statement=sql.SQL('SELECT {}({})').format(sql.Identifier(name),sql.SQL(',').join(sql.SQL('{} => %s').format(sql.Identifier(k)) for k in params))
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=connection.execute(statement,list(params.values())).fetchone()[0],error=None))
repo=SimpleNamespace(client=Client())
def replacements(value,old,new):
    if isinstance(value,dict):
        for k,v in value.items():value[k]=replacements(v,old,new)
    elif isinstance(value,list):
        for i,v in enumerate(value):value[i]=replacements(v,old,new)
    elif value==old:return new
    return value
results={}
for sku in ('studio','monitoring'):
    f=checkout_fixtures.SubscriptionCheckoutTest();f.setUp();provider=f.provider;service=f.service
    del service._row
    service.repository=repo;service.verifier.service=MigrationSubscriptionService(repo)
    originals=[f.fixture.customer,f.fixture.sub,f.fixture.invoice,f.fixture.intent,f.fixture.charge]
    for value in originals:
        replacements(value,checkout_fixtures.U,scope['user'])
        replacements(value,'cus_fixture','cus_checkoutflow')
        for prefix in ('sub','in','pi','ch'):
            replacements(value,f'{prefix}_fixture',f'{prefix}_checkout{sku}')
    if sku=='monitoring':
        f.fixture.price.update(id='price_monitoring',unit_amount=2900)
        for obj,fields in [(f.fixture.invoice,['total','subtotal','amount_due','amount_paid']),(f.fixture.intent,['amount','amount_received']),
                          (f.fixture.charge,['amount']),(f.fixture.invoice['lines']['data'][0],['amount'])]:
            for field in fields:obj[field]=2900
    def create_session(*,params,options):
        f.session.update(id=f'cs_test_checkout{sku}',metadata=params['metadata'],client_reference_id=params['client_reference_id'],
            customer=params['customer'],expires_at=params['expires_at'],amount_total=9900 if sku=='studio' else 2900,
            amount_subtotal=9900 if sku=='studio' else 2900,subscription=f.fixture.sub['id'])
        f.fixture.sub['metadata']=params['subscription_data']['metadata']
        return f.session
    provider.v1.checkout.sessions.create.side_effect=create_session
    c=service.create_checkout(scope['user'],sku,f'explicit-{sku}',recurring_consent=True,
                              deployment_id=scope['deployment_id'] if sku=='monitoring' else None)
    assert c['state']=='open' and c['subscription_id'] is None
    assert service.get_checkout(scope['user'],c['checkout_id'])['state']=='open'
    assert service.create_checkout(scope['user'],sku,f'explicit-{sku}',recurring_consent=True,
            deployment_id=scope['deployment_id'] if sku=='monitoring' else None)['checkout_id']==c['checkout_id']
    assert provider.v1.checkout.sessions.create.call_count==1
    # Invoice delivery may precede the Checkout event, using the same durable consent.
    body=f.fixture.body(oid=f.fixture.invoice['id'],id=f'evt_invoicecheckout{sku}')
    paid=service.handle_webhook(body,f.fixture.signed(body));assert paid['status']=='active'
    assert service.handle_webhook(body,f.fixture.signed(body))['replayed'] is True
    f.session.update(status='complete',payment_status='paid',url=None)
    done_body=f.fixture.body(kind='checkout.session.completed',oid=f.session['id'],id=f'evt_sessioncheckout{sku}')
    service.handle_webhook(done_body,f.fixture.signed(done_body))
    done=service.get_checkout(scope['user'],c['checkout_id']);assert done['state']=='complete' and done['subscription']['eligible']
    results[sku]={'checkout_id':c['checkout_id'],'subscription_id':done['subscription_id']}
    if sku=='studio':
        subs=service.verifier.service
        selected=subs.select_run_subscription(scope['user'],scope['migration_id'],scope['old'],scope['new'],scope['quote_id'],'checkout-to-run')
        assert selected['subscription_id']==done['subscription_id'] and selected['use_studio']
        queued=subs.start_studio_run(scope['user'],selected['subscription_id'],scope['migration_id'],scope['old'],scope['new'],scope['quote_id'],'checkout-to-run')
        assert queued['run_id'];results[sku]['run_id']=queued['run_id']
    else:
        slot=service.verifier.service.reserve_monitoring_site(scope['user'],done['subscription_id'],scope['deployment_id'],'checkout-to-monitor')
        assert slot['eligible'];results[sku]['site_slot_id']=slot['slot_id']
    # Provider cancellation and an old paid-event replay cannot recreate active rights.
    f.fixture.sub['status']='canceled'
    cancelled=f.fixture.body(kind='customer.subscription.deleted',oid=f.fixture.sub['id'],id=f'evt_cancelcheckout{sku}')
    service.handle_webhook(cancelled,f.fixture.signed(cancelled))
    assert service.handle_webhook(body,f.fixture.signed(body))['status']=='canceled'
    assert service.get_checkout(scope['user'],c['checkout_id'])['subscription']['eligible'] is False
print(json.dumps(results));connection.close()
