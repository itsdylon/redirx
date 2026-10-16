"""Shared serializer refusals and exact counts at legacy handler boundaries.

Auth/ownership lookup is stubbed explicitly; no network or durable writes.
"""
import inspect
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from flask import Flask, request
from backend.routes import v1_routes, watch_routes
from backend.tests.test_migration_artifact_service import OWNER, MIGRATION, RUN
from backend.tests import test_migration_artifact_service as artifact_fixture
from backend.services.migration_repository import InvalidInputError

SESSION='10000000-0000-4000-8000-000000000001'
WATCH='20000000-0000-4000-8000-000000000001'

class ExportRouteOutcomes(unittest.TestCase):
    def handlers(self):
        return ((v1_routes.export_migration,SESSION),(v1_routes.export_watch_fixes,SESSION),(watch_routes.export_fixes,WATCH))

    def invoke(self,handler,identifier,rows,owned=True):
        with ExitStack() as stack:
            stack.enter_context(Flask(__name__).test_request_context('/?format=nginx'))
            request.api_user_id=OWNER;request.user=SimpleNamespace(id=OWNER)
            stack.enter_context(patch.object(v1_routes,'_owned_session',return_value=({},None) if owned else (None,('not found',404))))
            stack.enter_context(patch.object(watch_routes,'_owned_watch',return_value={'id':WATCH} if owned else None))
            quota=stack.enter_context(patch.object(v1_routes,'UserQuotaDB'));quota.return_value.get_plan.return_value='free'
            stack.enter_context(patch.object(v1_routes.entitlement_service,'check_export',return_value=SimpleNamespace(allowed=True)))
            record=stack.enter_context(patch.object(v1_routes.entitlement_service,'record_export'))
            mapping=stack.enter_context(patch.object(v1_routes,'URLMappingDB'));mapping.return_value.get_mappings_by_session.return_value=rows
            for module in (v1_routes,watch_routes):
                watch=stack.enter_context(patch.object(module,'WatchService'));watch.return_value.get_watch_for_session.return_value={'id':WATCH};watch.return_value.fix_rows.return_value=rows
            result=inspect.unwrap(handler)(identifier)
            if not owned:mapping.assert_not_called()
            return result,record.call_count

    def test_unsupported_format_input_is_recoverable_and_never_records_export(self):
        rows=[{'old_url':'https://old.example/a%2Fb','new_url':'https://new.example/b'}]
        for handler,identifier in self.handlers():
            with self.subTest(handler=handler.__name__):
                (response,status),records=self.invoke(handler,identifier,rows)
                self.assertEqual(status,422);self.assertIn('unsupported_export_input',str(response.json))
                self.assertIn('CSV/JSON',str(response.json));self.assertEqual(records,0)

    def test_headers_count_actual_rules_and_exclusions(self):
        rows=[{'old_url':'https://old.example/a','new_url':'https://new.example/b'},
              {'old_url':'https://old.example/held','new_url':'https://new.example/held','needs_review':True}]
        for handler,identifier in self.handlers():
            with self.subTest(handler=handler.__name__):
                response,records=self.invoke(handler,identifier,rows)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.headers['X-Redirect-Count'],'1')
                self.assertEqual(response.headers['X-Redirect-Excluded-Count'],'1')
                self.assertIn('https://new.example/b',response.get_data(as_text=True))
                self.assertNotIn('/held',response.get_data(as_text=True))
                self.assertEqual(records,1 if handler is v1_routes.export_migration else 0)

    def test_unowned_requests_do_not_reach_export_rows(self):
        for handler,identifier in self.handlers():
            with self.subTest(handler=handler.__name__):
                result,records=self.invoke(handler,identifier,[],owned=False)
                self.assertEqual(result[1],404);self.assertEqual(records,0)

    def test_artifact_refusal_is_safe_invalid_input_before_publication(self):
        service,client=artifact_fixture.TestMigrationArtifactService().make()
        snapshot={'selection_revision':'rev-1','target_origins':['https://new.example'],
                  'mappings':[{'id':'map-1','old_url':'https://old.example/a%2Fb','new_url':'https://new.example/b'}]}
        with patch.object(service,'selection_reader',return_value=snapshot):
            with self.assertRaisesRegex(InvalidInputError,'custom server rules'):
                service.create_artifact(OWNER,MIGRATION,RUN,idempotency_key='fixture',fmt='nginx',selection_revision='rev-1')
        self.assertEqual(client.rpcs,[])
