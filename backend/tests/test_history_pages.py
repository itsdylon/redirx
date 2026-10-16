"""Native historical list/search paging, including full-list compatibility."""
import base64
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.tests import test_dashboard_snapshot as dashboard
from backend.tests.test_migration_planning import B, ROOT
from backend.services.legacy_session_history import read_session_page,read_all_sessions,InvalidHistoryPage


class HistoryQuery(dashboard.CappedQuery):
    def execute(self):
        if not self.rpc_name and self.mode=='select':self.max_rows=min(self.max_rows or 37,37)
        return super().execute()
    def lte(self,key,value):self.filters.append((key,'<=',value));return self


class HistoryClient(dashboard.CappedClient):
    def table(self,name):return HistoryQuery(self,table=name)


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class HistoryPages(unittest.TestCase):
    cleanup_db=classmethod(dashboard.DashboardSnapshot.cleanup_db.__func__)
    sql=dashboard.DashboardSnapshot.sql
    history=dashboard.DashboardSnapshot.history

    @classmethod
    def setUpClass(cls):
        dashboard.DashboardSnapshot.setUpClass.__func__(cls)
        cls.sql(cls,(ROOT/'database/migrations/067_legacy_session_pages.sql').read_text())
        cls.sql(cls,'ALTER TABLE migration_sessions ALTER COLUMN created_at DROP NOT NULL')

    def setUp(self):
        dashboard.DashboardSnapshot.setUp(self)
        self.native=HistoryClient(self.dsn)

    def request(self,suffix=''):
        with patch('services.auth_service.AuthService.verify_token',return_value=SimpleNamespace(id=self.owner)),patch(
            'backend.routes.user_routes.SupabaseClient.get_admin_client',return_value=self.native):
            return self.app.test_client().get('/api/user/sessions'+suffix,headers={'Authorization':'Bearer fixture-only'})

    def page(self,**kw):return read_session_page(self.native,self.owner,**kw)

    def test_more_than_cap_is_complete_bounded_and_full_list_compatible(self):
        self.history(1205);self.history(3,owner=B);self.history(2,tutorial=True);self.history(2,preview=True)
        ids=[];cursor=None
        while True:
            page=self.page(cursor=cursor,limit=100)
            self.assertEqual(page['total_count'],1205)
            self.assertLessEqual(len(page['sessions']),100)
            for row in page['sessions']:self.assertNotIn('old_urls',row)
            ids.extend(row['id'] for row in page['sessions']);cursor=page['next_cursor']
            if cursor is None:break
        self.assertEqual(len(ids),1205);self.assertEqual(len(set(ids)),1205)
        self.assertEqual(ids,sorted(ids,reverse=True))
        response=self.request()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(set(response.json),{'success','sessions'})
        self.assertEqual([row['id'] for row in response.json['sessions']],ids)
        self.assertTrue(all('old_urls' in row for row in response.json['sessions']))

    def test_search_is_literal_and_covers_history_outside_first_page(self):
        self.history(1100)
        sid=self.sql('SELECT id FROM migration_sessions WHERE user_id=%s ORDER BY id LIMIT 1',[self.owner])[0]['id']
        name='Older 100%_\\ literal café'
        self.sql('UPDATE migration_sessions SET project_name=%s,pipeline_type=%s WHERE id=%s',[name,'url_only',sid])
        for search in ('100%_\\','CAFÉ','url_only'):
            page=self.page(search=search)
            self.assertEqual(page['total_count'],1)
            self.assertEqual(page['sessions'][0]['id'],str(sid))
        self.assertEqual(self.page(search='never in this history')['total_count'],0)
        self.assertEqual(self.page(search='completed')['total_count'],1100)
        self.history(1,owner=B)
        self.sql("UPDATE migration_sessions SET project_name='Owner B unique' WHERE user_id=%s",[B])
        self.assertEqual(self.page(search='Owner B unique')['total_count'],0)

    def test_microsecond_cursor_ties_null_dates_and_deleted_anchor(self):
        ids=[str(row['id']) for row in self.history(4)]
        stamps=['2026-01-01T00:00:00.123456+00:00','2026-01-01T00:00:00.123455+00:00',None,None]
        for sid,stamp in zip(ids,stamps):self.sql('UPDATE migration_sessions SET created_at=%s WHERE id=%s',[stamp,sid])
        all_ids=[];cursor=None
        while True:
            result=self.page(limit=1,cursor=cursor);all_ids.extend(row['id'] for row in result['sessions']);cursor=result['next_cursor']
            if cursor is None:break
        self.assertEqual(all_ids,sorted(ids[2:],reverse=True)+ids[:2])
        for sid in ids[:2]:self.sql("UPDATE migration_sessions SET project_name='precision' WHERE id=%s",[sid])
        first=self.page(limit=1,search='precision')
        self.assertEqual(first['sessions'][0]['id'],ids[0])
        token=first['next_cursor']
        decoded=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4)))
        self.assertIn('.123456',decoded['created_at'])
        self.sql('DELETE FROM migration_sessions WHERE id=%s',[ids[0]])
        inserted=self.history(1)[0]['id']
        self.sql("UPDATE migration_sessions SET created_at=now(),project_name='precision newest' WHERE id=%s",[inserted])
        self.sql("UPDATE migration_sessions SET project_name='Precision renamed while paging' WHERE id=%s",[ids[1]])
        rest=[]
        while token:
            page=self.page(cursor=token,limit=1,search='precision');rest.extend(page['sessions']);token=page['next_cursor']
        self.assertEqual([row['id'] for row in rest],[ids[1]])
        self.assertEqual(rest[0]['project_name'],'Precision renamed while paging')
        self.assertEqual(self.page(limit=1,search='precision')['sessions'][0]['id'],str(inserted))

    def test_cursor_is_scoped_and_invalid_input_never_becomes_empty_success(self):
        self.history(2);token=self.page(limit=1)['next_cursor']
        for kwargs in ({'search':'other','cursor':token},{'cursor':'?'},{'cursor':'a'*2049},{'search':'x'*201},{'limit':0},{'limit':101}):
            with self.assertRaises(InvalidHistoryPage):self.page(**kwargs)
        with self.assertRaises(InvalidHistoryPage):read_session_page(self.native,B,cursor=token,limit=1)
        for suffix in ('?pagination=unknown','?pagination=cursor&limit=oops','?pagination=cursor&cursor=bad','?q=unlabeled'):
            response=self.request(suffix);self.assertEqual(response.status_code,400,response.json)
            self.assertFalse(response.json['success'])
        response=self.request('?pagination=cursor&limit=1')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['pagination'],'cursor')
        self.assertEqual(response.json['total_count'],2)

    def test_mcp_linked_sessions_remain_in_both_history_modes(self):
        from backend.tests import test_migration_run_service as runs
        with patch.dict(os.environ,runs.ACTIVATION):
            f=runs.RunDatabaseAcceptance.fixture(self,owner=self.owner)
            run=self.runs.start_run(self.owner,f['migration'],f['old'],f['new'],f['quote'],uuid4().hex)
        paged=self.page()
        self.assertEqual(paged['total_count'],1)
        self.assertEqual(paged['sessions'][0]['id'],run['session_id'])
        legacy=self.request()
        self.assertEqual(legacy.status_code,200,legacy.json)
        self.assertEqual(legacy.json['sessions'][0]['mcp_run_id'],run['run_id'])

    def test_unicode_query_produces_a_cursor_accepted_on_continuation(self):
        query='🌍'*200
        self.history(2)
        self.sql('UPDATE migration_sessions SET project_name=%s WHERE user_id=%s',[query,self.owner])
        first=self.page(search=query,limit=1)
        self.assertLessEqual(len(first['next_cursor']),2048)
        second=self.page(search=query,limit=1,cursor=first['next_cursor'])
        self.assertEqual(len(second['sessions']),1)
        self.assertIsNone(second['next_cursor'])

    def test_empty_and_search_fallback_and_privileges(self):
        self.assertEqual(self.page()['sessions'],[]);self.assertEqual(self.page()['total_count'],0)
        sid=self.history(1)[0]['id'];self.sql("UPDATE migration_sessions SET project_name='' WHERE id=%s",[sid])
        self.assertEqual(self.page(search='untitled session')['total_count'],1)
        signature='public.legacy_session_page(uuid,text,timestamp with time zone,uuid,integer)'
        for role in ('anon','authenticated'):
            self.assertFalse(self.sql('SELECT has_function_privilege(%s,%s,\'EXECUTE\') AS allowed',[role,signature])[0]['allowed'])
        self.assertTrue(self.sql('SELECT has_function_privilege(%s,%s,\'EXECUTE\') AS allowed',['service_role',signature])[0]['allowed'])


class LegacyProgress(unittest.TestCase):
    def test_repeated_or_malformed_drain_never_returns_partial_success(self):
        owner=str(uuid4());low='10000000-0000-0000-0000-000000000001';high='20000000-0000-0000-0000-000000000001'
        good={'id':low,'user_id':owner,'created_at':'2026-01-01T00:00:00+00:00'}
        for bad in (good,{'id':'bad','user_id':owner},{**good,'id':high,'user_id':str(uuid4())}):
            batches=iter([[{'id':high}], [good], [bad]])
            class Query:
                def __getattr__(self,name):return lambda *a,**kw:self
                def execute(self):return SimpleNamespace(data=next(batches),error=None)
            client=SimpleNamespace(table=lambda name:Query())
            with self.assertRaises((RuntimeError,ValueError)):read_all_sessions(client,owner)
