"""Lifecycle SELECT acknowledgment through actual PostgREST + Flask, offline."""
import json
import unittest

from backend.tests import test_owned_read_acknowledgments as read_fixture
OWNER, OTHER, MIGRATION = read_fixture.OWNER, read_fixture.OTHER, read_fixture.MIGRATION
from backend.services.migration_status_service import MigrationStatusService
from backend.services.migration_repository import RepositoryUnavailableError

RUN = '55555555-5555-4555-8555-555555555555'
QUOTE = '66666666-6666-4666-8666-666666666666'
OPERATION = '77777777-7777-4777-8777-777777777777'
CREATED = '2026-10-16T12:00:00Z'


def legacy_run():
    # 032/037 permit these links to be NULL on historical runs.
    return {'id': RUN, 'user_id': OWNER, 'migration_id': MIGRATION, 'operation_id': None,
            'quote_id': None, 'legacy_session_id': None, 'old_inventory_id': None,
            'new_inventory_id': None, 'rerun_of': None, 'created_at': CREATED}


class StatusRowIdentity(unittest.TestCase):
    setUp = read_fixture.OwnedReadAcknowledgments.setUp
    get = read_fixture.OwnedReadAcknowledgments.get
    unavailable = read_fixture.OwnedReadAcknowledgments.unavailable

    def run_reply(self, row):
        self.responses['migration_runs'] = (200, json.dumps(row).encode())

    def test_empty_mapping_never_becomes_a_null_id_run(self):
        self.run_reply([{}])
        self.unavailable(self.get(self.root))
        self.assertEqual(sum(r.url.path == '/migration_runs' for r in self.requests), 1)
        self.assertFalse(any(r.url.path == '/migration_price_quotes' for r in self.requests))

    def test_projection_presence_distinguishes_missing_from_explicit_null(self):
        for field in legacy_run():
            with self.subTest(field=field):
                row=legacy_run(); del row[field]
                self.run_reply([row]); self.requests.clear()
                self.unavailable(self.get(self.root))
                self.assertFalse(any(r.url.path == '/migration_price_quotes' for r in self.requests))

    def test_invalid_or_mismatched_identity_stops_before_child_reads(self):
        changes=[{'id':value} for value in (None, True, '', 'bad-id')]
        changes += [{'user_id': OTHER}, {'migration_id': OTHER}, {'operation_id': 'bad-id'}, {'rerun_of': False}]
        for change in changes:
            with self.subTest(change=change):
                self.run_reply([{**legacy_run(), **change}]); self.requests.clear()
                self.unavailable(self.get(self.root))
                self.assertFalse(any(r.url.path == '/migration_price_quotes' for r in self.requests))

    def test_explicit_run_filter_and_limit_are_acknowledged(self):
        self.run_reply([legacy_run()])
        self.unavailable(self.get(self.root+'?run_id='+OTHER))
        self.run_reply([legacy_run(), legacy_run()])
        self.unavailable(self.get(self.root))

    def test_real_empty_and_complete_nullable_run_remain_distinct(self):
        self.run_reply([])
        empty=self.get(self.root)
        self.assertEqual(empty.status_code,200)
        self.assertIsNone(empty.json['data']['run'])
        self.run_reply([legacy_run()])
        result=self.get(self.root)
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['data']['run_id'],RUN)
        self.assertEqual(result.json['data']['run']['inventory_ids'],{'old':None,'new':None})
        self.assertIsNone(result.json['data']['run']['operation_id'])
        self.assertIsNone(result.json['data']['run']['rerun_of'])
        self.assertEqual(result.json['status'],'needs_input')
        self.assertNotIn('user_id', result.json['data']['run'])

    def test_custom_quote_null_amount_is_preserved_but_missing_column_is_not(self):
        quote={'id':QUOTE,'user_id':OWNER,'migration_id':MIGRATION,'operation_id':OPERATION,
               'kind':'custom','amount_cents':None,'currency':'usd','old_pages':16000,
               'expires_at':'2026-10-17T12:00:00Z','created_at':CREATED}
        self.responses['migration_price_quotes']=(200,json.dumps([quote]).encode())
        result=self.get(self.root)
        self.assertEqual(result.status_code,200,result.json)
        self.assertIsNone(result.json['data']['quote']['amount_cents'])
        del quote['amount_cents']
        self.responses['migration_price_quotes']=(200,json.dumps([quote]).encode())
        self.unavailable(self.get(self.root))

    def test_downstream_lookup_identity_and_literal_kind_are_not_reinterpreted(self):
        service=MigrationStatusService(self.repository)
        row={'id':OPERATION,'user_id':OWNER,'migration_id':MIGRATION,'status':'payment_required','created_at':CREATED,'kind':'other-operation'}
        self.responses['migration_operations']=(200,json.dumps([row]).encode())
        with self.assertRaises(RepositoryUnavailableError):
            service._one('migration_operations','id,status,created_at',OWNER,MIGRATION,kind='run_migration')
        row['kind']='run_migration'
        self.responses['migration_operations']=(200,json.dumps([row]).encode())
        result=service._one('migration_operations','id,status,created_at',OWNER,MIGRATION,kind='run_migration')
        self.assertEqual(result,{'id':OPERATION,'status':'payment_required','created_at':CREATED})
        self.assertNotIn('kind',result)

    def test_nullable_session_progress_and_deployment_observations_remain_null(self):
        service=MigrationStatusService(self.repository)
        cases=[
            ('migration_sessions', 'id,status,current_stage,total_stages,last_error,created_at', None,
             {'id':RUN,'user_id':OWNER,'status':'pending','current_stage':None,'total_stages':None,'last_error':None,'created_at':CREATED}),
            ('artifact_deployments', 'id,artifact_id,live_origin,status,installation_reported_at,live_verified_at,created_at', MIGRATION,
             {'id':RUN,'user_id':OWNER,'migration_id':MIGRATION,'artifact_id':QUOTE,'live_origin':'https://new.example',
              'status':'generated','installation_reported_at':None,'live_verified_at':None,'created_at':CREATED}),
        ]
        for table, columns, migration, row in cases:
            with self.subTest(table=table):
                self.responses[table]=(200,json.dumps([row]).encode())
                result=service._one(table,columns,OWNER,migration,id=RUN)
                self.assertEqual(result,{key:row[key] for key in columns.split(',')})
                self.assertNotIn('user_id',result)


# The older outcome fixture deliberately uses SELECT * for its transport. These
# checks restore the real column projection only while reading lifecycle status.
import os
from unittest.mock import patch
from backend.tests import test_migration_outcomes as outcome_fixture
from backend.tests.test_migration_verification import VerificationQuery
from backend.tests.test_migration_planning import Query, A


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class NativeStatusProjection(unittest.TestCase):
    setUpClass = classmethod(outcome_fixture.OutcomeSQLTests.setUpClass.__func__)
    cleanup_db = classmethod(outcome_fixture.OutcomeSQLTests.cleanup_db.__func__)
    setUp = outcome_fixture.OutcomeSQLTests.setUp
    sql = outcome_fixture.OutcomeSQLTests.sql
    fixture = outcome_fixture.OutcomeSQLTests.fixture
    start_run = outcome_fixture.OutcomeSQLTests.start_run
    pay = outcome_fixture.OutcomeSQLTests.pay
    claim_run = outcome_fixture.OutcomeSQLTests.claim_run
    deployment = outcome_fixture.OutcomeSQLTests.deployment
    sub = outcome_fixture.OutcomeSQLTests.sub
    begin = outcome_fixture.OutcomeSQLTests.begin
    finish_sweep = outcome_fixture.OutcomeSQLTests.finish_sweep

    def status(self, migration, **kwargs):
        with patch.object(VerificationQuery, 'select', Query.select):
            return MigrationStatusService(self.service.repository).get(A, migration, **kwargs)

    def test_actual_projection_for_queued_completed_and_nullable_progress(self):
        fixture=self.fixture(); run=self.start_run(fixture)
        result=self.status(fixture['migration'])
        self.assertEqual(result['next_action'],'poll')
        self.assertEqual(result['data']['run_id'],run['run_id'])
        self.assertIsNone(result['progress']['total_stages'])
        job=self.claim_run(); self.runs.authorize_dispatch(job,'worker-test')
        self.runs.finalize_session(job,'worker-test','completed')
        completed=self.status(fixture['migration'],run_id=run['run_id'])
        self.assertEqual(completed['next_action'],'resolve_matches')
        self.assertTrue(completed['data']['run']['progress']['complete'])
        self.assertIsNone(completed['data']['artifact'])

    def test_actual_projection_for_artifact_verification_and_monitor_scope(self):
        deployed=self.deployment(3)
        result=self.status(deployed['migration'])
        self.assertEqual(result['next_action'],'verify_redirects')
        self.assertEqual(result['data']['artifact_id'],deployed['artifact'])
        self.assertEqual(result['data']['deployment_id'],deployed['deployment'])
        self.assertIsNone(result['data']['deployment']['live_verified_at'])
        verification=self.verify.start(A,deployed['migration'],deployed['artifact'],deployed['deployment'],'projection-check')
        subscription,_=self.sub(); monitor=self.begin(deployed,subscription)
        result=self.status(deployed['migration'])
        self.assertEqual(result['next_action'],'poll')
        self.assertEqual(result['data']['verification_id'],verification['data']['verification_id'])
        self.assertEqual(result['data']['monitoring_id'],monitor['data']['monitoring_id'])
        self.assertNotIn('user_id',result['data']['deployment'])
        self.assertNotIn('verification_inputs',result['data']['artifact'])
