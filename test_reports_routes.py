import os
import re
import unittest
from unittest.mock import patch

import jwt
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.reports.analytics_pdf import generate_analytics_pdf


class ReportsRouteTests(unittest.TestCase):
    def setUp(self):
        key_lookup = patch("app.api.v1.routers.reports_routes.workspace_secret", return_value=None)
        key_lookup.start()
        self.addCleanup(key_lookup.stop)

    def test_monthly_pdf_links_to_keyword_sheet_only_above_ten_rows(self):
        url = b"https://docs.google.com/spreadsheets/d/keyword-sheet_123/edit"
        for count in (10, 11):
            with self.subTest(count=count):
                dashboard = {
                    "sources": {"google_sheets": {"spreadsheet_id": "keyword-sheet_123"}},
                    "tables": {"google_sheets": {
                        "columns": ["Keyword", "Position"],
                        "rows": [{"Keyword": f"Keyword {i}", "Position": i} for i in range(1, count + 1)],
                    }},
                }
                pdf = generate_analytics_pdf(dashboard, "monthly", "Test Client")
                self.assertEqual(b"/URI (" + url + b")" in pdf, count > 10)

    @patch("reportlab.rl_config.pageCompression", 0)
    def test_monthly_pdf_comparison_flag_hides_prior_period_and_insights_when_true(self):
        dashboard = {
            "period": {
                "start_date": "2026-10-01", "end_date": "2026-10-31",
                "comparison_start_date": "2026-09-01", "comparison_end_date": "2026-09-30",
            },
            "kpis": [
                {"key": "gsc_clicks", "value": 18, "previous_value": 10, "change": 80, "trend": "up"},
                {"key": "gsc_impressions", "value": 121, "previous_value": 782, "change": -84.5, "trend": "down"},
            ],
            "charts": {"gsc_daily": {
                "labels": ["2026-10-01"], "datasets": [{"label": "impressions", "data": [121]}],
            }},
            "tables": {"google_sheets": {
                "columns": ["Keyword", "Position", "Previous Position"],
                "rows": [{"Keyword": "Priority term", "Position": 3, "Previous Position": 5}],
            }},
        }
        compared = generate_analytics_pdf(dashboard, "monthly", "Test Client", comparison=False)
        current = generate_analytics_pdf(dashboard, "monthly", "Test Client", comparison=True)

        for label in (
            b"Compared with", b"October vs. September", b"Period changes", b"Positive signals",
            b"Declines to review", b"Dashed: previous period average", b"Previous Position",
        ):
            with self.subTest(label=label):
                self.assertIn(label, compared)
                self.assertNotIn(label, current)
        self.assertIn(b"Priority term", compared)
        self.assertIn(b"Priority term", current)

    def test_monthly_and_weekly_routes_return_database_backed_pdfs(self):
        secret = "test-secret-that-is-at-least-32-bytes"
        workspace_id = str(ObjectId())
        client_id = str(ObjectId())
        token = jwt.encode(
            {
                "id": workspace_id,
                "workspace_id": workspace_id,
                "username": "test-user",
                "fullname": "Test User",
            },
            secret,
            algorithm="HS256",
        )
        dashboard = {
            "client": {"client_id": client_id, "name": "Test Client"},
            "period": {
                "start_date": "2026-10-01",
                "end_date": "2026-10-31",
                "comparison_start_date": "2026-09-01",
                "comparison_end_date": "2026-09-30",
            },
            "sources": {"gsc": {"site_url": "https://example.com/"}},
            "kpis": [
                {
                    "label": "Search clicks",
                    "value": 20,
                    "change": 5.0,
                    "change_unit": "percent",
                    "trend": "up",
                }
            ],
            "charts": {
                "gsc_daily": {
                    "labels": ["2026-10-01", "2026-10-02"],
                    "datasets": [{"label": "Clicks", "data": [5, 15]}],
                }
            },
            "tables": {
                "gsc_daily": {
                    "columns": ["date", "clicks"],
                    "rows": [{"date": "2026-10-01", "clicks": 5}],
                },
                "top_queries": {"columns": ["query", "clicks"], "rows": []},
                "top_pages": {"columns": ["page", "clicks"], "rows": []},
                "ga4_daily": {"columns": ["date", "sessions"], "rows": []},
                "google_sheets": {"columns": [], "rows": []},
            },
        }

        with patch.dict(
            os.environ,
            {"JWT_SECRET": secret, "JWT_ALG": "HS256"},
        ):
            with TestClient(app) as client:
                for period in ("monthly", "weekly"):
                    with self.subTest(period=period):
                        with patch(
                            "app.api.v1.routers.reports_routes._dashboard_from_database",
                            return_value=dashboard,
                        ) as get_dashboard:
                            response = client.get(
                                f"/api/v1/clients/{client_id}/reports/{period}",
                                headers={"Authorization": f"Bearer {token}"},
                            )

                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.headers["content-type"], "application/pdf")
                        self.assertTrue(response.content.startswith(b"%PDF-"))
                        expected_pages = 6 if period == "monthly" else 1
                        self.assertEqual(len(re.findall(rb"/Type /Page\b", response.content)), expected_pages)
                        self.assertIn(
                            f"seo-report-{period}-2026-10-01-2026-10-31.pdf",
                            response.headers["content-disposition"],
                        )
                        get_dashboard.assert_called_once_with(
                            workspace_id,
                            client_id,
                            period,
                        )

    def test_report_can_render_when_no_integration_data_is_stored(self):
        secret = "test-secret-that-is-at-least-32-bytes"
        workspace_id = str(ObjectId())
        client_id = str(ObjectId())
        empty_dashboard = {
            "client": {"client_id": client_id, "name": "Test Client"},
            "period": {},
            "sources": {},
            "kpis": [],
            "charts": {
                "gsc_daily": {
                    "labels": [],
                    "datasets": [{"label": "Clicks", "data": []}],
                },
            },
            "tables": {},
        }
        token = jwt.encode(
            {
                "id": workspace_id,
                "workspace_id": workspace_id,
                "username": "test-user",
            },
            secret,
            algorithm="HS256",
        )

        with patch.dict(
            os.environ,
            {"JWT_SECRET": secret, "JWT_ALG": "HS256"},
        ):
            with TestClient(app) as client:
                with patch(
                    "app.api.v1.routers.reports_routes._dashboard_from_database",
                    return_value=empty_dashboard,
                ):
                    response = client.get(
                        f"/api/v1/clients/{client_id}/reports/weekly",
                        headers={"Authorization": f"Bearer {token}"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", response.content)), 1)
