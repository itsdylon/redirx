"""
Server-side redirect exports.

These formats previously existed only in the React export modal, so an agent
could never fetch the artifact its job ends with. The risk in porting them is
silent divergence: a file that downloads cleanly and redirects nothing.
"""
import json
import os
import sys
import unittest
from time import monotonic

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from backend.services import redirect_export as rx

ROWS = [
    {"old_url": "https://old.example.com/about-us", "new_url": "https://new.example.com/company/about"},
    {"old_url": "https://old.example.com/pricing", "new_url": "https://new.example.com/plans"},
]


class TestPathHandling(unittest.TestCase):
    def test_absolute_url_reduces_to_path(self):
        self.assertEqual(rx.to_path("https://e.com/a/b?x=1"), "/a/b?x=1")

    def test_bare_path_survives_untouched(self):
        # Rows can already be paths; mangling them would break the file.
        self.assertEqual(rx.to_path("/pricing"), "/pricing")

    def test_root_url_becomes_slash(self):
        self.assertEqual(rx.to_path("https://e.com"), "/")

    def test_unparseable_input_is_returned_as_is(self):
        self.assertEqual(rx.to_path("not a url"), "not a url")

    def test_rehost_swaps_origin_and_keeps_path(self):
        self.assertEqual(
            rx.rehost("https://old.com/a/b?x=1", "https://new.com"),
            "https://new.com/a/b?x=1",
        )

    def test_rehost_accepts_a_bare_domain(self):
        self.assertEqual(rx.rehost("https://old.com/a", "new.com"), "https://new.com/a")


class TestRawSuffixAndRuntimeLimits(unittest.TestCase):
    def test_raw_destination_suffix_and_empty_delimiters_survive_transforms(self):
        for suffix in ('/Case%2FPath;view?Q=A%2FB#Section', '/a?#', '/a?', '/a#', '?', '#section'):
            with self.subTest(suffix=suffix):
                raw = 'HTTPS://old.example' + suffix
                self.assertEqual(rx.to_path(raw), suffix if suffix.startswith('/') else '/' + suffix)
                self.assertEqual(rx.rehost(raw, 'https://new.example'), 'https://new.example' + suffix)

    def test_same_origin_target_fragment_is_not_silently_dropped(self):
        rows = [{'old_url': 'https://same.example/old', 'new_url': 'https://same.example/new?x=1#section'}]
        for fmt in rx.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertIn('/new?x=1#section', rx.build_export(rows, fmt))

    def test_empty_query_is_not_a_no_op_or_dropped_in_data_exports(self):
        rows = [{'old_url': 'https://same.example/a', 'new_url': 'https://same.example/a?'}]
        result = rx.select_export_mappings(rows)
        self.assertEqual(result['included_count'], 1)
        self.assertEqual(result['mappings'][0]['new_rendered'], '/a?')
        self.assertIn('/a?', rx.build_export(rows, rx.CSV_FORMAT))

    def test_normalized_server_matchers_refuse_raw_identity_loss(self):
        for fmt in (rx.APACHE, rx.NGINX):
            for source in ('/a%20b', '/a%2Fb', '/%61bc', '/a//b', '/a/../b', '/a/./b'):
                with self.subTest(fmt=fmt, source=source), self.assertRaises(rx.UnsupportedExportInput):
                    rx.build_export([{'old_url': source, 'new_url': '/destination'}], fmt)

    def test_source_fragments_and_empty_queries_are_explicitly_unsupported(self):
        for fmt in rx.FORMATS:
            if fmt not in (rx.CSV_FORMAT, rx.JSON_FORMAT):
                with self.subTest(fmt=fmt), self.assertRaises(rx.UnsupportedExportInput):
                    rx.build_export([{'old_url': '/a#section', 'new_url': '/destination'}], fmt)
        for fmt in rx.PATH_ONLY_FORMATS:
            with self.subTest(fmt=fmt), self.assertRaises(rx.UnsupportedExportInput):
                rx.build_export([{'old_url': '/a?', 'new_url': '/destination'}], fmt)
        for fmt in (rx.CSV_FORMAT, rx.JSON_FORMAT):
            for source in ('/a#section', '/a?', '/a%2Fb'):
                with self.subTest(fmt=fmt, source=source):
                    self.assertIn(source, rx.build_export([{'old_url': source, 'new_url': '/destination'}], fmt))


class TestFormats(unittest.TestCase):
    def test_apache(self):
        out = rx.build_export(ROWS, rx.APACHE)
        self.assertEqual(
            out.splitlines()[0],
            'RedirectMatch 301 "^/about\\\\-us$" "https://new.example.com/company/about"',
        )

    def test_nginx_is_a_closed_map_block(self):
        out = rx.build_export(ROWS, rx.NGINX)
        lines = out.splitlines()
        self.assertEqual(lines[0], "# Include inside the target server block.")
        self.assertIn('location = "/pricing" { return 301 "https://new.example.com/plans"; }', lines)

    def test_nginx_with_no_rows_still_closes_the_block(self):
        # An unclosed map block is a config file nginx refuses to start with.
        out = rx.build_export([], rx.NGINX)
        self.assertEqual(out, "# No redirect rules generated.")

    def test_wordpress_rows_carry_the_status(self):
        out = rx.build_export(ROWS, rx.WORDPRESS)
        self.assertEqual(out.splitlines()[0], "/about-us,https://new.example.com/company/about,0,301,url")

    def test_wordpress_import_columns_keep_literal_regex_and_quoted_urls(self):
        import csv
        import io
        out = rx.build_export([{'old_url': 'https://old.example/a,b.*', 'new_url': 'https://new.example/d,e'}], rx.WORDPRESS)
        row = next(csv.reader(io.StringIO(out)))
        source, target, regex, http_code, match_type = row
        self.assertEqual((source, target), ('/a,b.*', 'https://new.example/d,e'))
        self.assertEqual((regex, http_code, match_type), ('0', '301', 'url'))

    def test_vercel_is_valid_json_with_permanent_true(self):
        parsed = json.loads(rx.build_export(ROWS, rx.VERCEL))
        self.assertEqual(len(parsed["redirects"]), 2)
        self.assertTrue(parsed["redirects"][0]["permanent"])
        self.assertEqual(parsed["redirects"][0]["source"], "/about-us")

    def test_cloudflare(self):
        self.assertEqual(
            rx.build_export(ROWS, rx.CLOUDFLARE).splitlines()[0],
            "/about-us https://new.example.com/company/about 301",
        )

    def test_shopify_has_its_required_header(self):
        out = rx.build_export(ROWS, rx.SHOPIFY)
        self.assertEqual(out.splitlines()[0], "Redirect from,Redirect to")

    def test_cloudflare_static_rule_count_limit_is_checked_after_selection(self):
        rows = [{'old_url': f'/old-{i}', 'new_url': f'/new-{i}'} for i in range(2001)]
        self.assertEqual(len(rx.build_export(rows[:2000], rx.CLOUDFLARE).splitlines()), 2000)
        with self.assertRaisesRegex(rx.UnsupportedExportInput, '2,000 static'):
            rx.build_export(rows, rx.CLOUDFLARE)
        rows[-1]['approved'] = False
        self.assertEqual(len(rx.build_export(rows, rx.CLOUDFLARE).splitlines()), 2000)

    def test_cloudflare_rule_character_limit_is_not_silently_truncated(self):
        # '/a ' + destination + ' 301' is exactly 1,000 characters.
        destination = '/' + 'b' * 992
        row = {'old_url': '/a', 'new_url': destination}
        self.assertEqual(len(rx.build_export([row], rx.CLOUDFLARE)), 1000)
        row['new_url'] += 'b'
        with self.assertRaisesRegex(rx.UnsupportedExportInput, '1,000 characters'):
            rx.build_export([row], rx.CLOUDFLARE)
        self.assertIn(row['new_url'], rx.build_export([row], rx.JSON_FORMAT))

    def test_csv_has_a_header(self):
        out = rx.build_export(ROWS, rx.CSV_FORMAT)
        self.assertEqual(out.splitlines()[0], "old_url,new_url,status")

    def test_json_is_a_list_of_rules(self):
        parsed = json.loads(rx.build_export(ROWS, rx.JSON_FORMAT))
        self.assertEqual(parsed[0]["status"], 301)
        self.assertEqual(parsed[0]["from"], "/about-us")

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(rx.UnknownExportFormat):
            rx.build_export(ROWS, "iis")


class TestUrlFormat(unittest.TestCase):
    def test_full_keeps_absolute_urls(self):
        with self.assertRaises(rx.UnsupportedExportInput):
            rx.build_export(ROWS, rx.APACHE, url_format="full")

    def test_paths_is_the_default(self):
        # Most targets match on the request path, and an agent has nobody to
        # warn it that absolute URLs silently never match.
        out = rx.build_export(ROWS, rx.APACHE)
        self.assertNotIn("https://old.example.com", out)
        self.assertIn("https://new.example.com", out)

    def test_custom_domain_rehosts_each_side(self):
        out = rx.build_export(
            ROWS,
            rx.CSV_FORMAT,
            url_format="custom",
            old_domain="https://legacy.test",
            new_domain="https://fresh.test",
        )
        self.assertIn("https://legacy.test/about-us,https://fresh.test/company/about", out)


class TestWarnings(unittest.TestCase):
    def test_absolute_urls_on_a_path_matcher_warn(self):
        self.assertIsNotNone(rx.warning_for(rx.APACHE, "full"))
        self.assertIn("server-block", rx.warning_for(rx.NGINX, "paths"))
        self.assertIn("Pages", rx.warning_for(rx.CLOUDFLARE, "paths"))

    def test_paths_never_warn(self):
        for fmt in rx.FORMATS:
            if fmt not in (rx.NGINX, rx.CLOUDFLARE):
                self.assertIsNone(rx.warning_for(fmt, "paths"))

    def test_formats_carrying_full_urls_do_not_warn(self):
        # CSV and JSON are consumed by tooling, not by a path matcher.
        self.assertIsNone(rx.warning_for(rx.CSV_FORMAT, "full"))
        self.assertIsNone(rx.warning_for(rx.JSON_FORMAT, "full"))


class TestSafeSelection(unittest.TestCase):
    def test_cross_domain_same_path_preserves_destination_origin(self):
        out = rx.build_export([
            {"old_url": "https://old.example/a", "new_url": "https://new.example/a"},
        ], rx.APACHE)
        self.assertEqual(out, 'RedirectMatch 301 "^/a$" "https://new.example/a"')

    def test_noops_and_held_or_rejected_rows_are_excluded(self):
        selection = rx.select_export_mappings([
            {"old_url": "https://old.example/a", "new_url": "https://old.example/a"},
            {"old_url": "https://old.example/b", "new_url": "https://new.example/b", "approved": False},
            {"old_url": "https://old.example/c", "new_url": "https://new.example/c", "status": "held"},
            {"old_url": "https://old.example/d", "new_url": "https://new.example/d", "needs_review": True},
        ])
        self.assertEqual(selection["mappings"], [])
        self.assertEqual(selection["excluded_count"], 4)

    def test_conflicts_and_loops_are_excluded(self):
        selection = rx.select_export_mappings([
            {"old_url": "/conflict", "new_url": "/one"},
            {"old_url": "/conflict", "new_url": "/two"},
            {"old_url": "/a", "new_url": "/b"},
            {"old_url": "/b", "new_url": "/a"},
        ])
        self.assertEqual(selection["mappings"], [])
        self.assertEqual({item["reason"] for item in selection["excluded"]}, {
            "conflicting_source", "redirect_loop",
        })

    def test_invalid_destination_and_config_injection_are_excluded(self):
        selection = rx.select_export_mappings([
            {"old_url": "/safe", "new_url": "javascript:alert(1)"},
            {"old_url": "/newline", "new_url": "/bad\nlocation"},
            {"old_url": "/credential", "new_url": "https://user:pass@new.example/x"},
        ])
        self.assertEqual(selection["included_count"], 0)
        self.assertEqual(selection["excluded_count"], 3)

    def test_csv_and_json_continue_to_escape_values(self):
        csv_out = rx.build_export([
            {"old_url": "https://old.example/a,b", "new_url": "https://new.example/c"},
        ], rx.CSV_FORMAT)
        self.assertIn('"/a,b",https://new.example/c', csv_out)
        parsed = json.loads(rx.build_export([
            {"old_url": "https://old.example/\"q", "new_url": "https://new.example/q"},
        ], rx.JSON_FORMAT))
        self.assertEqual(parsed[0]["from"], "/\"q")

    def test_malformed_ipv6_is_an_exclusion(self):
        result = rx.select_export_mappings([
            {"old_url": "https://[broken/a", "new_url": "/new"},
            {"old_url": "/old", "new_url": "https://[broken"},
        ])
        self.assertEqual(result["included_count"], 0)
        self.assertEqual(result["excluded_count"], 2)

    def test_duplicate_rules_are_deduplicated_and_accounted_for(self):
        result = rx.select_export_mappings([
            {"old_url": "/a", "new_url": "/b"},
            {"old_url": "/a", "new_url": "/b", "id": "duplicate"},
        ])
        self.assertEqual(result["included_count"], 1)
        self.assertEqual(result["excluded"], [{"index": 1, "reason": "duplicate_rule"}])

    def test_mixed_absolute_relative_forms_use_effective_origin(self):
        result = rx.select_export_mappings([
            {"old_url": "https://old.example/a", "new_url": "/a"},
        ])
        self.assertEqual(result["included_count"], 0)
        result = rx.select_export_mappings([
            {"old_url": "/a", "new_url": "/b"},
        ], old_domain="https://old.example", new_domain="https://new.example")
        self.assertEqual(result["mappings"][0]["new_rendered"], "https://new.example/b")

    def test_decision_actions_are_excluded(self):
        rows = [
            {"old_url": f"/a{index}", "new_url": f"/b{index}", "action": action}
            for index, action in enumerate(("intentional_removal", "defer", "reject"))
        ]
        result = rx.select_export_mappings(rows)
        self.assertEqual(result["included_count"], 0)
        self.assertEqual(result["excluded_count"], 3)

    def test_large_chain_cycle_scan_is_linear_enough(self):
        count = 15_000
        rows = [
            {"old_url": f"/chain/{index}", "new_url": f"/chain/{index + 1}"}
            for index in range(count)
        ]
        started = monotonic()
        result = rx.select_export_mappings(rows)
        elapsed = monotonic() - started
        self.assertEqual(result["included_count"], count)
        self.assertLess(elapsed, 1.0)

    def test_query_sensitive_path_exports_fail_explicitly(self):
        with self.assertRaises(rx.UnsupportedExportInput):
            rx.build_export([
                {"old_url": "https://old.example/a?x=1", "new_url": "/b"},
            ], rx.NGINX)

    def test_config_escaping_is_not_raw_interpolation(self):
        with self.assertRaises(rx.UnsupportedExportInput):
            rx.build_export([
                {"old_url": "/a;\"$", "new_url": "https://new.example/b;\"$"},
            ], rx.APACHE)
        with self.assertRaises(rx.UnsupportedExportInput):
            rx.build_export([
                {"old_url": "/a$", "new_url": "https://new.example/b$"},
            ], rx.NGINX)

    def test_path_parameters_are_preserved(self):
        self.assertEqual(rx.to_path("https://old.example/a;view?x=1"), "/a;view?x=1")
        out = rx.build_export([
            {"old_url": "https://old.example/a;view", "new_url": "https://new.example/b;view"},
        ], rx.APACHE)
        self.assertIn("a;view", out)

    def test_pattern_syntax_is_rejected_for_pattern_platforms(self):
        for fmt, source in ((rx.VERCEL, "/:id"), (rx.CLOUDFLARE, "/a*")):
            with self.subTest(fmt=fmt), self.assertRaises(rx.UnsupportedExportInput):
                rx.build_export([
                    {"old_url": source, "new_url": "/destination"},
                ], fmt)


class TestRobustness(unittest.TestCase):
    def test_rows_missing_a_side_are_skipped(self):
        rows = ROWS + [{"old_url": "https://old.example.com/orphan", "new_url": ""}]
        self.assertEqual(len(rx.build_export(rows, rx.CLOUDFLARE).splitlines()), 2)

    def test_camelcase_rows_are_accepted(self):
        # The frontend speaks camelCase; the database speaks snake_case.
        out = rx.build_export(
            [{"oldUrl": "https://o.com/a", "newUrl": "https://n.com/b"}], rx.APACHE
        )
        self.assertEqual(out, 'RedirectMatch 301 "^/a$" "https://n.com/b"')

    def test_commas_in_urls_are_quoted_not_corrupted(self):
        # String-joined CSV would produce an extra column here.
        out = rx.build_export(
            [{"old_url": "https://o.com/a,b", "new_url": "https://n.com/c"}],
            rx.CSV_FORMAT,
        )
        self.assertIn('"/a,b"', out)

    def test_empty_input_produces_empty_output_not_an_error(self):
        for fmt in (rx.APACHE, rx.CLOUDFLARE, rx.WORDPRESS):
            self.assertEqual(rx.build_export([], fmt), "")

    def test_every_format_has_a_filename_and_content_type(self):
        for fmt in rx.FORMATS:
            self.assertTrue(rx.filename_for(fmt))
            self.assertTrue(rx.content_type_for(fmt))


if __name__ == "__main__":
    unittest.main()
