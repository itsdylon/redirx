"""Native live-cursor semantics under owner isolation and interleaved changes."""
import os
import unittest
from uuid import UUID

from backend.services.mapping_decision_service import MappingDecisionService
from backend.services.migration_repository import MigrationRepository, MigrationNotFoundError
from backend.tests import test_artifact_interleavings as interleavings
from backend.tests import test_artifact_decision_projection as artifacts
from backend.tests import test_atomic_session_delete as schema
from backend.tests.helpers.pivot_native_client import NativeClient, NativeQuery
from backend.tests.test_migration_planning import B


@unittest.skipUnless(os.getenv('PREFLIGHT_TEST_DATABASE_URL'), 'requires disposable loopback PostgreSQL')
class ReadPaginationConsistency(unittest.TestCase):
    cleanup_db = classmethod(schema.AtomicSessionDelete.cleanup_db.__func__)
    sql = artifacts.ArtifactDecisionProjection.sql
    claim = artifacts.ArtifactDecisionProjection.claim
    completed_run = artifacts.ArtifactDecisionProjection.completed_run
    decide = artifacts.ArtifactDecisionProjection.decide
    setUp = interleavings.ArtifactInterleavings.setUp

    @classmethod
    def setUpClass(cls):
        schema.AtomicSessionDelete.setUpClass.__func__(cls)

    def test_approved_match_pages_expose_revision_change_after_other_reviewer_rejects(self):
        mid,rid,_ = self.completed_run(unmatched_root=False)
        owner=artifacts.A
        reader=MappingDecisionService(self.native)
        first=reader.list_matches(owner,mid,rid,match_filter='approved',limit=1)
        self.assertEqual(first['selection_revision'],0)
        self.assertIsNotNone(first['next_cursor'])
        seen=first['items'][0]['mapping_id']
        self.decide(mid,rid,seen,'reject')
        following=reader.list_matches(owner,mid,rid,match_filter='approved',limit=1,cursor=first['next_cursor'])
        self.assertEqual(following['selection_revision'],1)
        self.assertEqual(len(following['items']),1)
        self.assertIsNone(following['next_cursor'])
        fresh=reader.list_matches(owner,mid,rid,match_filter='approved')
        self.assertEqual(fresh['selection_revision'],1)
        self.assertEqual(fresh['items'],following['items'])
        # A UI that blindly concatenates different revisions still shows the
        # rejected first row as approved. The revision is the readback signal.
        mixed=first['items']+following['items']
        self.assertEqual(len(mixed),2)
        self.assertNotIn(seen,[row['mapping_id'] for row in fresh['items']])
        with self.assertRaises(MigrationNotFoundError):
            reader.list_matches(B,mid,rid,match_filter='approved',cursor=first['next_cursor'])

    def test_migration_cursor_survives_small_caps_and_interleaved_insert_without_owner_leak(self):
        owner=artifacts.A
        calls=[]
        class SmallPage(NativeQuery):
            def execute(self):
                self.max_rows=min(self.max_rows or 2,2)
                calls.append(self.max_rows)
                return super().execute()
        class SmallClient(NativeClient):
            def table(self,name):return SmallPage(self,table=name)
        def insert(n,user=owner):
            self.sql('''INSERT INTO migration_records(id,user_id,old_origin,new_origin,name)
                VALUES(%s,%s,'https://old.example','https://new.example','raw fixture')''',[str(UUID(int=n)),user])
        for n in range(100,1800,100):insert(n)
        for n in (150,450,1850):insert(n,B)
        repository=MigrationRepository(SmallClient(self.dsn))
        first=repository.list_migrations(owner,limit=3)
        self.assertEqual([row['id'] for row in first['items']],[str(UUID(int=n)) for n in (100,200,300)])
        self.assertGreaterEqual(len(calls),2)
        insert(250)  # Already behind cursor: visible on refresh, not this traversal.
        insert(350)  # Ahead of cursor: visible on the continuation.
        self.sql('UPDATE migration_records SET name=%s WHERE id=%s',['renamed',str(UUID(int=200))])
        rows=first['items'];cursor=first['next_cursor']
        while cursor:
            page=repository.list_migrations(owner,after_id=cursor,limit=3)
            rows.extend(page['items']);cursor=page['next_cursor']
        ids=[row['id'] for row in rows]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertNotIn(str(UUID(int=250)),ids)
        self.assertIn(str(UUID(int=350)),ids)
        self.assertTrue(all(row['user_id']==owner for row in rows))
        fresh=repository.list_migrations(owner,limit=50)
        expected=[str(row['id']) for row in self.sql('SELECT id FROM migration_records WHERE user_id=%s ORDER BY id',[owner])]
        self.assertEqual([row['id'] for row in fresh['items']],expected)
        self.assertIsNone(fresh['next_cursor'])
        self.assertIn(str(UUID(int=250)),expected)
