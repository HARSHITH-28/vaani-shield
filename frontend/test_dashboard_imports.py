"""Regression checks for dashboard helper exports and app startup imports."""

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

from frontend.pages import banking, financial_intelligence


class DashboardImportTests(unittest.TestCase):
    def test_banking_and_financial_intelligence_export_dashboard_helpers(self):
        self.assertTrue(callable(banking.load_dashboard_context))
        self.assertTrue(callable(banking.render_demo_scope_notice))
        self.assertIs(
            financial_intelligence.load_dashboard_context,
            banking.load_dashboard_context,
        )
        self.assertIs(
            financial_intelligence.render_demo_scope_notice,
            banking.render_demo_scope_notice,
        )

    def test_streamlit_app_runs_through_its_imports(self):
        app_path = Path(__file__).with_name("app.py")
        result = AppTest.from_file(str(app_path)).run(timeout=20)
        self.assertEqual(len(result.exception), 0)


if __name__ == "__main__":
    unittest.main()
