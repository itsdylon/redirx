"""Offline diagnostics from persisted explicit import results; no network calls."""
import unittest
from unittest.mock import Mock
from backend.services.inventory_policy import preflight_inventory
from backend.services.migration_planning_service import inventory_summary, MigrationPlanningService
from backend.services.migration_repository import MigrationNotFoundError

class InventoryGuidanceTests(unittest.TestCase):
    def test_partial_import_explains_exclusions_without_sensitive_queries(self):
        inventory = preflight_inventory([
            'https://old.test/Keep%2FCase?key=retained',
            'https://foreign.test/Case%2FPath?token=secret#fragment',
            'https://user:password@old.test/private',
        ], declared_origins=['https://old.test'])
        result = inventory_summary(inventory)
        issues = result['input_issues']
        self.assertEqual(issues['total_excluded'], 2)
        self.assertEqual(issues['reason_counts'], {'invalid_url': 1, 'undeclared_origin': 1})
        self.assertFalse(issues['truncated'])
        self.assertIn({'code': 'undeclared_origin', 'url_hint': 'https://foreign.test/Case%2FPath'}, issues['examples'])
        self.assertNotIn('secret', str(result))
        self.assertNotIn('password', str(result))
        self.assertNotIn('exclusions', result)

    def test_diagnostics_are_bounded_and_do_not_reflect_unknown_errors(self):
        inventory = preflight_inventory([
            'https://foreign.test/Keep%2FCase' + str(i) + '?secret=value' for i in range(15)
        ], declared_origins=['https://old.test'])
        inventory['exclusions'].append({'original_url': 'https://foreign.test/' + 'x' * 600,
                                        'reason': 'database password=secret'})
        inventory['coverage']['excluded_count'] = 16
        issues = inventory_summary(inventory)['input_issues']
        self.assertEqual(issues['total_excluded'], 16)
        self.assertEqual(issues['diagnostic_count'], 16)
        self.assertTrue(issues['diagnostics_complete'])
        self.assertTrue(issues['truncated'])
        self.assertEqual(len(issues['examples']), 10)
        self.assertEqual(issues['reason_counts'], {'undeclared_origin': 15, 'invalid_input': 1})
        self.assertNotIn('secret', str(issues))
        inventory['exclusions'] = inventory['exclusions'][-1:]
        issues = inventory_summary(inventory)['input_issues']
        self.assertFalse(issues['diagnostics_complete'])
        self.assertEqual(issues['examples'], [{'code': 'invalid_input', 'url_hint': None}])

    def test_unavailable_historical_diagnostics_never_claim_zero_errors(self):
        row = {'coverage': {'kind': 'explicit_import', 'excluded_count': 3}}
        issues = inventory_summary(row)['input_issues']
        self.assertEqual(issues['total_excluded'], 3)
        self.assertEqual(issues['diagnostic_count'], 0)
        self.assertFalse(issues['diagnostics_complete'])
        self.assertTrue(issues['truncated'])
        self.assertEqual(issues['examples'], [])
        row['coverage'].pop('excluded_count')
        self.assertIsNone(inventory_summary(row)['input_issues']['total_excluded'])
        self.assertNotIn('input_issues', inventory_summary({'coverage': {'kind': 'crawl'}}))
        self.assertIsNone(inventory_summary(None))
        for coverage in ('historical-unknown', ['unknown'], 1, True):
            self.assertNotIn('input_issues', inventory_summary({'coverage': coverage}))

    def test_complete_sitemap_url_is_syntax_only_and_input_is_not_rewritten(self):
        urls = ['https://old.test/Sitemap%2F.xml?x=One#anchor']
        inventory = preflight_inventory(urls, declared_origins=['https://old.test'])
        self.assertEqual(inventory['status'], 'complete')
        self.assertFalse(inventory['coverage']['site_coverage_claimed'])
        self.assertFalse(inventory['coverage']['network_checked'])
        issues = inventory_summary(inventory)['input_issues']
        self.assertEqual(issues['total_excluded'], 0)
        self.assertTrue(issues['diagnostics_complete'])
        self.assertEqual(urls, ['https://old.test/Sitemap%2F.xml?x=One#anchor'])

    def test_all_policy_diagnostic_categories_and_malformed_saved_rows(self):
        inventory = preflight_inventory([None, {'url': 1}, 'ftp://old.test/a',
            {'url': 'https://old.test/a', 'provenance': []},
            {'url': 'https://old.test/b', 'metadata': {'bad': float('nan')}},
        ], declared_origins=['https://old.test'])
        issues = inventory_summary(inventory)['input_issues']
        self.assertEqual(issues['reason_counts'], {'invalid_row': 2, 'invalid_url': 1,
                                                 'invalid_provenance': 1, 'invalid_metadata': 1})
        inventory['exclusions'] = [None, {'reason': []}, {'original_url': 'https://user:secret@host.test/a'}]
        issues = inventory_summary(inventory)['input_issues']
        self.assertEqual(issues['reason_counts'], {'invalid_input': 3})
        self.assertTrue(all(item['url_hint'] is None for item in issues['examples']))

    def test_operation_reads_diagnostics_through_owned_inventory_and_rejects_foreign_read(self):
        repository = Mock()
        repository.get_operation.return_value = {'id': 'operation', 'migration_id': 'migration',
            'kind': 'import_inventory', 'result': {'inventory_id': 'inventory'}}
        repository.get_inventory.return_value = preflight_inventory([
            'https://old.test/a', 'https://foreign.test/a'], declared_origins=['https://old.test'])
        service = MigrationPlanningService(repository)
        result = service.operation('owner', 'operation', 'migration')
        repository.get_operation.assert_called_once_with('owner', 'operation', 'migration')
        repository.get_inventory.assert_called_once_with('owner', 'migration', 'inventory')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['next_action'], 'provide_inventory')
        self.assertIn('new idempotency key', result['data']['summary'])
        self.assertEqual(result['data']['inventory']['input_issues']['reason_counts'], {'undeclared_origin': 1})
        repository.get_operation.side_effect = MigrationNotFoundError('not_found')
        repository.get_inventory.reset_mock()
        with self.assertRaises(MigrationNotFoundError):
            service.operation('foreign-owner', 'operation', 'migration')
        repository.get_inventory.assert_not_called()
