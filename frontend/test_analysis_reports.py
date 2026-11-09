"""Frontend state and navigation coverage for unified Analysis Reports."""

import unittest

from frontend.navigation import apply_pending_navigation
from frontend.pages.scam_detection import (
    load_analysis_reports,
    open_analysis_report,
    queue_sms_report_navigation,
    report_content_notice,
    sms_report_id_from_notification,
)


REPORT_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeReportsAPI:
    def __init__(self):
        self.list_calls = []
        self.detail_calls = []
        self.pages = []
        self.details = {}

    def list_scam_reports(self, **kwargs):
        self.list_calls.append(kwargs)
        return self.pages.pop(0) if self.pages else {"items": [], "next_cursor": None, "has_more": False}

    def get_scam_report(self, source_type, report_id):
        self.detail_calls.append((source_type, report_id))
        return self.details[(source_type, report_id)]


class AnalysisReportsFrontendTests(unittest.TestCase):
    def test_empty_report_page_and_source_risk_filter_forwarding(self):
        api = FakeReportsAPI()
        api.pages = [{"items": [], "next_cursor": None, "has_more": False}]
        state = {
            "customer": {"customer_key": "demo-arjun"},
            "analysis_reports_filters": {
                "source_type": "INCOMING_SMS", "risk_level": "HIGH",
                "date_from": "2026-01-01", "date_to": "2026-01-31", "search": "bank",
            },
        }
        self.assertEqual(load_analysis_reports(api, state), [])
        self.assertEqual(api.list_calls[0], {
            "limit": 25, "cursor": None, "source_type": "INCOMING_SMS",
            "risk_level": "HIGH", "date_from": "2026-01-01",
            "date_to": "2026-01-31", "search": "bank",
        })

    def test_cursor_pagination_appends_without_duplicates_and_is_customer_scoped(self):
        api = FakeReportsAPI()
        first = {"report_id": REPORT_ID, "source_type": "INCOMING_SMS"}
        other = {"report_id": "223e4567-e89b-42d3-a456-426614174000", "source_type": "VOICE_CALL"}
        api.pages = [
            {"items": [first], "next_cursor": "opaque-cursor", "has_more": True},
            {"items": [first, other], "next_cursor": None, "has_more": False},
            {"items": [], "next_cursor": None, "has_more": False},
        ]
        state = {"customer": {"customer_key": "demo-arjun"}, "analysis_reports_filters": {}}
        load_analysis_reports(api, state)
        load_analysis_reports(api, state, append=True)
        self.assertEqual([x["report_id"] for x in state["analysis_reports_items"]], [REPORT_ID, other["report_id"]])
        self.assertEqual(api.list_calls[1]["cursor"], "opaque-cursor")
        state["customer"] = {"customer_key": "demo-priya"}
        load_analysis_reports(api, state)
        self.assertEqual(api.list_calls[2]["cursor"], None)
        self.assertEqual(state["analysis_reports_items"], [])

    def test_detail_api_supports_each_source_and_content_availability_messages(self):
        api = FakeReportsAPI()
        state = {}
        for source in ("MANUAL_MESSAGE", "INCOMING_SMS", "VOICE_CALL"):
            api.details[(source, REPORT_ID)] = {"source_type": source, "report_id": REPORT_ID}
            self.assertEqual(open_analysis_report(api, state, source, REPORT_ID)["source_type"], source)
        self.assertEqual([call[0] for call in api.detail_calls], ["MANUAL_MESSAGE", "INCOMING_SMS", "VOICE_CALL"])
        self.assertIn("not retained", report_content_notice({"source_type": "VOICE_CALL", "content_availability": "SOURCE_CONTENT_NOT_STORED"}))
        self.assertIn("expired after 30 days", report_content_notice({"source_type": "VOICE_CALL", "content_availability": "TRANSCRIPT_EXPIRED"}))
        self.assertIn("original message", report_content_notice({"source_type": "MANUAL_MESSAGE"}))

    def test_notification_reference_validation_and_analysis_navigation(self):
        notification = {"category": "security_alert", "event_ref": f"sms-threat-{REPORT_ID}"}
        self.assertEqual(sms_report_id_from_notification(notification), REPORT_ID)
        self.assertIsNone(sms_report_id_from_notification({"event_ref": "sms-threat-not-a-uuid"}))
        self.assertEqual(
            sms_report_id_from_notification({
                "metadata_json": f'{{"source":"android_sms_threat_intelligence","sms_event_id":"{REPORT_ID}"}}'
            }),
            REPORT_ID,
        )
        state = {"main_feature": "Banking Information", "subfeature_banking_information": "Accounts"}
        self.assertTrue(queue_sms_report_navigation(state, notification))
        self.assertEqual(state["analysis_reports_pending_detail"], {"source_type": "INCOMING_SMS", "report_id": REPORT_ID})
        self.assertTrue(apply_pending_navigation(state))
        self.assertEqual(state["main_feature"], "Scam Detection")
        self.assertEqual(state["subfeature_scam_detection"], "Analysis Reports")


if __name__ == "__main__":
    unittest.main()
