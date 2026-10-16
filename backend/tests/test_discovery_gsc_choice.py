"""Discovery request validation and explicit GSC choice, without auth/network/database."""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flask import Flask, request
from backend.tests.test_ingestion_service import IngestionService, FakeDiscovery, gsc, _async
from backend.routes.discovery_routes import discover


class TestDiscoveryGscChoice(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.route = inspect.unwrap(discover)

    def test_invalid_flags_are_rejected_before_service_or_database(self):
        for flag in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(flag=flag), self.app.test_request_context(json={"url": "e.com", "use_gsc": flag}), patch(
                "backend.routes.discovery_routes.UserQuotaDB"
            ) as quota, patch("backend.routes.discovery_routes.IngestionService") as service:
                request.user = SimpleNamespace(id="u1")
                response, status = self.route()
                self.assertEqual(status, 400)
                self.assertEqual(response.json["code"], "invalid_use_gsc")
                quota.assert_not_called()
                service.assert_not_called()

    def test_default_and_explicit_choices_reach_real_ingestion(self):
        svc = IngestionService(gsc_service=object(), connection_db=object(), baseline_db=object())
        svc.gsc_urls_for_domain = Mock(return_value=([gsc("https://e.com/ranked", 300, 900)], "sc-domain:e.com"))
        svc.capture_baseline = Mock(return_value="baseline-1")
        for body, expected_gsc in (({"url": "e.com"}, 1), ({"url": "e.com", "use_gsc": True}, 1), ({"url": "e.com", "use_gsc": False, "gsc_property": "sc-domain:e.com"}, 0)):
            svc.gsc_urls_for_domain.reset_mock()
            svc.capture_baseline.reset_mock()
            with self.subTest(body=body), self.app.test_request_context(json=body), patch(
                "backend.routes.discovery_routes.UserQuotaDB"
            ) as quota, patch("backend.routes.discovery_routes.IngestionService", return_value=svc), patch(
                "backend.services.ingestion_service.discover_site", side_effect=_async(FakeDiscovery(["https://e.com/page"]))
            ) as discovery:
                quota.return_value.get_plan.return_value = "free"
                request.user = SimpleNamespace(id="u1")
                response, status = self.route()
                self.assertEqual(status, 200)
                self.assertEqual(svc.gsc_urls_for_domain.call_count, expected_gsc)
                self.assertEqual(svc.capture_baseline.call_count, expected_gsc)
                self.assertEqual(discovery.call_count, 1)
                self.assertEqual(response.json["gsc_url_count"], expected_gsc)
                self.assertEqual(response.json["summary"]["total_clicks"], 300 * expected_gsc)
                self.assertEqual(response.json["count"], 1 + expected_gsc)
