"""Offline tests for the authenticated frontend HTTP client."""

import unittest
from unittest.mock import Mock
import requests

from frontend.api_client import (
    APIError,
    VaaniShieldAPI,
    build_speech_turn_request,
    build_text_turn_request,
)
from frontend.pages.banking import load_dashboard_context


class APIClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.api = VaaniShieldAPI("http://localhost:8000", session=self.session)

    def response(self, status=200, payload=None, content=b"", headers=None):
        response = Mock()
        response.status_code = status
        response.json.return_value = payload
        response.content = content
        response.headers = headers or {}
        return response

    def test_text_turn_contains_server_conversation_and_unique_client_id(self):
        first = build_text_turn_request("c-123", "  Hi  ", "speech_and_text", "kn-IN", "hi-IN")
        second = build_text_turn_request("c-123", "Hi", "speech_and_text", "kn-IN", "hi-IN")
        self.assertEqual(first["conversation_id"], "c-123")
        self.assertEqual(first["text"], "Hi")
        self.assertEqual(first["input_mode"], "text")
        self.assertEqual(first["output_mode"], "speech_and_text")
        self.assertNotEqual(first["client_turn_id"], second["client_turn_id"])
        self.assertNotIn("customer_id", first)
        self.assertNotIn("customer_key", first)

    def test_assistant_turn_uses_extended_timeout_for_multistep_workflows(self):
        self.session.request.return_value = self.response(200, {"assistant_text": "Ready"})
        self.api.send_assistant_turn({"conversation_id": "conversation"})
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], 300)

    def test_message_delete_uses_authenticated_conversation_turn_route(self):
        self.session.request.return_value = self.response(204)
        result = self.api.delete_conversation_turn(
            "123e4567-e89b-42d3-a456-426614174000",
            "123e4567-e89b-42d3-a456-426614174001",
        )
        self.assertIsNone(result)
        call = self.session.request.call_args
        self.assertEqual(call.args[0], "DELETE")
        self.assertIn("/api/v1/assistant/conversations/123e4567-e89b-42d3-a456-426614174000/turns/123e4567-e89b-42d3-a456-426614174001", call.args[1])

    def test_automatic_response_language_omits_explicit_override(self):
        payload = build_text_turn_request(
            "c-123", "Nanna account balance eshtu?", "text",
            language_hint="auto", response_language=None,
        )
        self.assertNotIn("response_language", payload)

    def test_text_turn_forwards_only_explicit_scam_assessment_reference(self):
        payload = build_text_turn_request(
            "c-123", "What does this scam assessment mean?", "text",
            scam_assessment_id="assessment-42",
        )
        self.assertEqual(payload["scam_assessment_id"], "assessment-42")
        unrelated = build_text_turn_request("c-123", "What is my balance?", "text")
        self.assertNotIn("scam_assessment_id", unrelated)

    def test_speech_turn_and_upload_use_contract_field(self):
        payload = build_speech_turn_request("c-123", "upload-1", "text", "auto")
        self.assertEqual(payload["audio_upload_id"], "upload-1")
        self.assertEqual(payload["input_mode"], "speech")
        self.session.request.return_value = self.response(201, {"audio_upload_id": "upload-1"})
        self.assertEqual(self.api.upload_audio("q.wav", b"RIFF", "audio/wav")["audio_upload_id"], "upload-1")
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["files"]["file"], ("q.wav", b"RIFF", "audio/wav"))
        self.assertNotIn("scam_assessment_id", payload)

    def test_scam_assessment_methods_use_authenticated_versioned_routes(self):
        self.session.request.side_effect = [
            self.response(201, {"assessment_id": "text-assessment"}),
            self.response(201, {"assessment_id": "voice-assessment"}),
        ]
        self.api.assess_scam_message("Suspicious message", "en-IN")
        self.api.assess_scam_call("upload-9", "kn-IN")
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].args[1], "http://localhost:8000/api/v1/me/scam/assessments/messages")
        self.assertEqual(calls[0].kwargs["json"], {"message_text": "Suspicious message", "language_hint": "en-IN"})
        self.assertEqual(calls[1].args[1], "http://localhost:8000/api/v1/me/scam/assessments/calls")
        self.assertEqual(calls[1].kwargs["json"], {"audio_upload_id": "upload-9", "language_hint": "kn-IN"})

    def test_transaction_risk_methods_use_authenticated_routes_and_public_refs(self):
        self.session.request.side_effect = [
            self.response(201, {"assessment_id": "public-id"}),
            self.response(200, {"items": [], "next_cursor": "next", "has_more": True}),
            self.response(200, {"assessment_id": "public-id"}),
        ]
        transaction_ref = "TR-" + "A" * 48
        self.api.assess_transaction_risk(transaction_ref)
        self.api.list_transaction_risk_assessments(limit=10, cursor="opaque-cursor")
        self.api.get_transaction_risk_assessment("123e4567-e89b-42d3-a456-426614174000")
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].args[1], "http://localhost:8000/api/v1/me/scam/assessments/transactions")
        self.assertEqual(calls[0].kwargs["json"], {"transaction_ref": transaction_ref})
        self.assertEqual(calls[1].args[1], "http://localhost:8000/api/v1/me/scam/transaction-assessments")
        self.assertEqual(calls[1].kwargs["params"], {"limit": 10, "cursor": "opaque-cursor"})
        self.assertEqual(calls[2].args[1], "http://localhost:8000/api/v1/me/scam/transaction-assessments/123e4567-e89b-42d3-a456-426614174000")
        self.assertTrue(all("customer_id" not in str(call.kwargs) for call in calls))
        with self.assertRaises(ValueError):
            self.api.assess_transaction_risk("31")
        with self.assertRaises(ValueError):
            self.api.get_transaction_risk_assessment("../../other-customer")

    def test_investigation_methods_use_authenticated_public_contracts(self):
        self.session.request.side_effect = [
            self.response(200, {"items": [], "next_cursor": "next", "has_more": True}),
            self.response(200, {"investigation_ref": "INV-" + "A" * 32}),
            self.response(201, {"investigation_ref": "INV-" + "A" * 32}),
            self.response(200, {"status": "executed"}),
        ]
        self.api.list_investigations(limit=10, cursor="cursor", status="OPEN", category="OTHER")
        self.api.get_investigation("INV-" + "A" * 32)
        self.api.create_investigation({"title": "Report", "confirmed": True})
        self.api.confirm_investigation_action(
            "123e4567-e89b-42d3-a456-426614174000", confirmed=True,
            conversation_id="123e4567-e89b-42d3-a456-426614174001",
            original_turn_id="123e4567-e89b-42d3-a456-426614174002",
            action_type="create_investigation",
        )
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].args[1], "http://localhost:8000/api/v1/me/scam/investigations")
        self.assertEqual(calls[0].kwargs["params"], {"limit": 10, "cursor": "cursor", "status": "OPEN", "category": "OTHER"})
        self.assertEqual(calls[1].args[1], "http://localhost:8000/api/v1/me/scam/investigations/INV-" + "A" * 32)
        self.assertIn("/api/v1/assistant/investigation-confirmations/", calls[3].args[1])
        self.assertNotIn("customer_id", str(calls))

    def test_demo_auth_uses_persistent_session(self):
        self.session.request.return_value = self.response(200, {"customer_key": "demo-arjun"})
        self.api.create_demo_session("demo-arjun")
        self.api.get_demo_session()
        self.api.delete_demo_session()
        self.assertEqual([call.args[0] for call in self.session.request.call_args_list], ["POST", "GET", "DELETE"])
        self.assertIn("/api/v1/auth/demo/session", self.session.request.call_args_list[0].args[1])

    def test_dashboard_methods_use_self_service_paths_without_customer_references(self):
        self.session.request.return_value = self.response(200, [])
        self.api.get_my_context()
        self.api.get_my_accounts()
        self.api.get_my_balance()
        self.api.get_my_transactions(100)
        self.api.search_my_transactions({"merchant": "Market"})
        self.api.get_my_cards()
        self.api.get_my_loans()
        self.api.get_my_financial_summary()
        self.api.get_my_financial_categories()
        self.api.get_my_financial_merchants()
        self.api.get_my_monthly_comparison()
        self.api.get_my_financial_advisory()

        paths = [call.args[1] for call in self.session.request.call_args_list]
        self.assertEqual(paths, [
            "http://localhost:8000/api/v1/me/context",
            "http://localhost:8000/api/v1/me/accounts",
            "http://localhost:8000/api/v1/me/balance",
            "http://localhost:8000/api/v1/me/transactions",
            "http://localhost:8000/api/v1/me/transactions/search",
            "http://localhost:8000/api/v1/me/cards",
            "http://localhost:8000/api/v1/me/loans",
            "http://localhost:8000/api/v1/me/financial/summary",
            "http://localhost:8000/api/v1/me/financial/categories",
            "http://localhost:8000/api/v1/me/financial/merchants",
            "http://localhost:8000/api/v1/me/financial/monthly-comparison",
            "http://localhost:8000/api/v1/me/financial/advisory",
        ])
        self.assertTrue(all("customer_ref" not in path and "customer_key" not in path for path in paths))

    def test_dashboard_context_helper_uses_self_service_method(self):
        expected = {"customer": {"name": "Arjun"}}
        self.api.get_my_context = Mock(return_value=expected)
        self.assertEqual(load_dashboard_context(self.api), expected)
        self.api.get_my_context.assert_called_once_with()

    def test_financial_page_client_methods_keep_optional_period_together(self):
        self.session.request.return_value = self.response(200, {})
        self.api.get_my_context(2026, 1)
        self.api.get_my_monthly_comparison(2026, 1)
        self.api.get_my_financial_advisory()
        context_call, comparison_call, advisory_call = self.session.request.call_args_list
        self.assertEqual(context_call.kwargs["params"], {"year": 2026, "month": 1})
        self.assertEqual(comparison_call.kwargs["params"], {"year": 2026, "month": 1})
        self.assertIsNone(advisory_call.kwargs["params"])

    def test_audio_retrieval_is_limited_to_configured_backend_audio_path(self):
        self.session.request.return_value = self.response(200, content=b"wav", headers={"Content-Type": "audio/wav"})
        content, mime = self.api.fetch_audio("/api/v1/voice/responses/audio-1")
        self.assertEqual((content, mime), (b"wav", "audio/wav"))
        with self.assertRaises(ValueError):
            self.api.fetch_audio("https://attacker.example/api/v1/voice/responses/audio-1")
        with self.assertRaises(ValueError):
            self.api.fetch_audio("/unrelated/path")

    def test_safe_error_handling_for_auth_upload_and_provider_failures(self):
        for status in (401, 404, 413, 415, 503):
            self.session.request.return_value = self.response(status, {"detail": "safe backend detail"})
            with self.subTest(status=status), self.assertRaises(APIError) as caught:
                self.api.send_assistant_turn({})
            self.assertEqual(caught.exception.status_code, status)
            self.assertEqual(str(caught.exception), "safe backend detail")

    def test_transport_failures_are_classified_without_exception_details(self):
        cases = (
            (requests.exceptions.ConnectTimeout("secret url"), "connect_timeout", "connection timed out"),
            (requests.exceptions.ReadTimeout("private payload"), "read_timeout", "did not respond"),
            (requests.exceptions.ConnectionError("cookie=secret"), "connection_error", "could not be reached"),
            (requests.exceptions.RequestException("secret body"), "request_error", "request could not be completed"),
        )
        for error, kind, expected in cases:
            self.session.request.side_effect = error
            with self.subTest(kind=kind), self.assertRaises(APIError) as caught:
                self.api.send_assistant_turn({})
            self.assertEqual(caught.exception.status_code, 0)
            self.assertEqual(caught.exception.payload["transport"], kind)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(str(error), str(caught.exception))

    def test_notification_methods_use_authenticated_routes(self):
        self.session.request.side_effect = [
            self.response(200, {"notifications": [{"notification_id": "n1", "id": "n1"}], "unread_count": 1}),
            self.response(200, {"status": "success", "notification_id": "n1"}),
            self.response(200, {"status": "success", "marked_read": 1, "unread_count": 0}),
        ]
        res_list = self.api.get_notifications(limit=15)
        self.assertEqual(res_list["unread_count"], 1)
        res_read = self.api.mark_notification_read("n1")
        self.assertEqual(res_read["status"], "success")
        res_all = self.api.mark_all_notifications_read()
        self.assertEqual(res_all["unread_count"], 0)

        calls = self.session.request.call_args_list
        self.assertEqual(calls[-3].args[1], "http://localhost:8000/api/v1/me/notifications")
        self.assertEqual(calls[-3].kwargs["params"], {"limit": 15})
        self.assertEqual(calls[-2].args[1], "http://localhost:8000/api/v1/me/notifications/n1/read")
        self.assertEqual(calls[-1].args[1], "http://localhost:8000/api/v1/me/notifications/read-all")


if __name__ == "__main__":
    unittest.main()
