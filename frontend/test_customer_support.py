"""Offline support-page and structured assistant-action tests."""

import unittest
from unittest.mock import Mock

from frontend.api_client import APIError, VaaniShieldAPI
from frontend.components.conversation import (
    messages_from_turn_response,
    render_messages,
    support_confirmation_state,
    submit_support_confirmation,
    submit_support_closure,
    _run_support_confirmation,
)
from frontend.pages.customer_support import (
    build_ticket_request,
    close_ticket_after_confirmation,
    render,
    submit_support_ticket,
)
from frontend.components.conversation_history import select_conversation


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def metric(self, *_args, **_kwargs):
        pass


class _Column(_Context):
    def button(self, label, **kwargs):
        self.root.buttons.append(kwargs.get("key"))
        return bool(self.root.clicked == kwargs.get("key"))


class FakeStreamlit:
    def __init__(self, clicked=None, selected=None):
        self.clicked = clicked
        self.selected = selected or {}
        self.infos = []
        self.warnings = []
        self.errors = []
        self.markdowns = []
        self.writes = []
        self.captions = []
        self.buttons = []
        self.root = self

    def __getattr__(self, name):
        if name in {"title", "divider", "success", "text", "audio"}:
            return lambda *args, **kwargs: None
        if name == "caption":
            return lambda value, **kwargs: self.captions.append(value)
        if name == "write":
            return lambda value, **kwargs: self.writes.append(value)
        if name == "info":
            return lambda value, **kwargs: self.infos.append(value)
        if name == "warning":
            return lambda value, **kwargs: self.warnings.append(value)
        if name == "error":
            return lambda value, **kwargs: self.errors.append(value)
        if name == "markdown":
            return lambda value, **kwargs: self.markdowns.append(value)
        if name in {"container", "form", "chat_message", "spinner", "status"}:
            return lambda *args, **kwargs: _Context()
        if name == "columns":
            return lambda count, **kwargs: [
                self._column() for _ in range(count)
            ]
        if name == "button":
            def button(_label, **kwargs):
                self.buttons.append(kwargs.get("key"))
                return bool(self.clicked == kwargs.get("key"))
            return button
        if name == "selectbox":
            return lambda label, options, index=0, **kwargs: self.selected.get(
                kwargs.get("key"), options[index]
            )
        if name == "text_input":
            return lambda *args, **kwargs: ""
        if name == "text_area":
            return lambda *args, **kwargs: ""
        if name == "form_submit_button":
            return lambda *args, **kwargs: False
        if name == "checkbox":
            return lambda *args, **kwargs: False
        if name == "rerun":
            return lambda: None
        raise AttributeError(name)

    def _column(self):
        column = _Column()
        column.root = self
        return column


class FakeSupportAPI:
    def __init__(self):
        self.calls = []
        self.page = {"items": [], "next_cursor": None, "has_more": False}

    def list_support_tickets(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.page

    def get_support_ticket(self, ticket_ref):
        self.calls.append(("detail", ticket_ref))
        return {
            "ticket_ref": ticket_ref, "category": "card", "subject": "Card issue",
            "priority": "HIGH", "status": "OPEN", "description": "Synthetic details",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "is_synthetic_demo": True,
        }

    def get_support_ticket_events(self, ticket_ref):
        self.calls.append(("events", ticket_ref))
        return [{
            "from_status": None, "to_status": "OPEN", "occurred_at": "2026-01-01T00:00:00Z",
        }]

    def create_support_ticket(self, payload):
        self.calls.append(("create", payload))
        return {"ticket_ref": "VS-" + "A" * 32, "status": "OPEN", "is_synthetic_demo": True}

    def confirm_assistant_action(self, confirmation_id, **kwargs):
        self.calls.append(("confirm", confirmation_id, kwargs))
        return {
            "confirmation_id": confirmation_id,
            "status": "executed" if kwargs["confirmed"] else "declined",
            "action_result": {"data": {"ticket_ref": "VS-" + "B" * 32}},
        }

    def close_support_ticket(self, ticket_ref):
        self.calls.append(("close", ticket_ref))
        return {"ticket_ref": ticket_ref, "status": "CLOSED"}


class CustomerSupportFrontendTests(unittest.TestCase):
    def test_support_page_loads_with_empty_state_and_api_filters(self):
        st = FakeStreamlit()
        api = FakeSupportAPI()
        state = {"support_ticket_filters": {}}
        render(st, "Track Complaints", api, state)
        self.assertTrue(any("No support tickets" in str(item) for item in st.infos))
        self.assertEqual(api.calls[0], ("list", {
            "limit": 25, "status": None, "category": None, "priority": None,
        }))

    def test_opening_ticket_form_does_not_create_a_ticket(self):
        api = FakeSupportAPI()
        render(FakeStreamlit(), "Create Support Ticket", api, {"support_ticket_filters": {}})
        self.assertFalse(any(call[0] == "create" for call in api.calls))

    def test_ticket_list_filters_map_to_supported_api_values(self):
        api = FakeSupportAPI()
        st = FakeStreamlit(selected={
            "support-status-filter": "OPEN",
            "support-category-filter": "Digital Banking",
            "support-priority-filter": "HIGH",
        })
        render(st, "Track Complaints", api, {"support_ticket_filters": {}})
        self.assertEqual(api.calls[0][1], {
            "limit": 25, "status": "OPEN", "category": "digital_banking", "priority": "HIGH",
        })

    def test_ticket_form_validation_and_optional_description_contract(self):
        payload = build_ticket_request("Digital Banking", "  Sign-in trouble  ", "HIGH", "")
        self.assertEqual(payload, {
            "category": "digital_banking",
            "subject": "Sign-in trouble",
            "priority": "HIGH",
            "description": "No additional description provided.",
        })
        with self.assertRaises(ValueError):
            build_ticket_request("Other", " ", "LOW")
        with self.assertRaises(ValueError):
            build_ticket_request("Unknown", "Issue", "LOW")

    def test_list_ticket_details_and_event_history_render(self):
        ref = "VS-" + "E" * 32
        api = FakeSupportAPI()
        api.page["items"] = [{"ticket_ref": ref, "subject": "Card issue", "status": "OPEN"}]
        st = FakeStreamlit()
        render(st, "Track Complaints", api, {"support_ticket_filters": {}})
        self.assertIn(("detail", ref), api.calls)
        self.assertIn(("events", ref), api.calls)
        self.assertTrue(any("Synthetic demo ticket" in str(item) for item in st.captions))
        self.assertTrue(any("Created" in str(item) and "OPEN" in str(item) for item in st.writes))
        self.assertTrue(any("Status history" in item for item in st.markdowns))

    def test_successful_form_creation_and_closure_require_explicit_confirmation(self):
        api = FakeSupportAPI()
        ticket = submit_support_ticket(
            api, category="Card", subject="Card issue", priority="MEDIUM"
        )
        self.assertTrue(ticket["ticket_ref"].startswith("VS-"))
        self.assertEqual(api.calls[0][0], "create")
        with self.assertRaises(ValueError):
            close_ticket_after_confirmation(api, ticket["ticket_ref"], False)
        close_ticket_after_confirmation(api, ticket["ticket_ref"], True)
        self.assertEqual(api.calls[-1], ("close", ticket["ticket_ref"]))

    def test_api_client_uses_authenticated_support_contracts(self):
        session = Mock()
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"items": []}
        api = VaaniShieldAPI("http://localhost:8000", session=session)
        api.list_support_tickets(status="OPEN", category="card", priority="HIGH")
        self.assertEqual(session.request.call_args.args[1], "http://localhost:8000/api/v1/me/support/tickets")
        self.assertEqual(session.request.call_args.kwargs["params"], {
            "limit": 25, "status": "OPEN", "category": "card", "priority": "HIGH",
        })

        session.request.return_value.status_code = 201
        session.request.return_value.json.return_value = {"ticket_ref": "VS-" + "D" * 32}
        api.create_support_ticket({"category": "card", "subject": "Card", "priority": "LOW", "description": "Demo"})
        create_call = session.request.call_args
        self.assertEqual(create_call.args[0], "POST")
        self.assertEqual(create_call.args[1], "http://localhost:8000/api/v1/me/support/tickets")
        self.assertEqual(create_call.kwargs["json"]["subject"], "Card")

        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"ticket_ref": "VS-" + "D" * 32}
        api.get_support_ticket("VS-" + "D" * 32)
        self.assertTrue(session.request.call_args.args[1].endswith("/VS-" + "D" * 32))
        api.get_support_ticket_events("VS-" + "D" * 32)
        self.assertTrue(session.request.call_args.args[1].endswith("/events"))
        api.close_support_ticket("VS-" + "D" * 32)
        close_call = session.request.call_args
        self.assertEqual(close_call.args[0], "PATCH")
        self.assertEqual(close_call.kwargs["json"], {
            "status": "CLOSED", "confirmed": True, "confirmation_method": "button",
        })
        api.confirm_assistant_action(
            "opaque-id", confirmed=True, conversation_id="c-1", original_turn_id="t-1"
        )
        confirmation_call = session.request.call_args
        self.assertEqual(confirmation_call.args[1], "http://localhost:8000/api/v1/assistant/confirmations/opaque-id")
        self.assertEqual(confirmation_call.kwargs["json"], {
            "confirmed": True, "confirmation_method": "button",
            "conversation_id": "c-1", "original_turn_id": "t-1",
            "action_type": "create_support_ticket",
        })
        self.assertNotIn("customer_id", confirmation_call.kwargs["json"])

    def test_pending_ticket_proposal_card_and_exact_confirmation_binding(self):
        confirmation_id = "confirm-opaque"
        conversation_id = "conversation-9"
        turn_id = "user-turn-7"
        response = {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "input_mode": "text",
            "assistant_text": "I prepared a draft.",
            "pending_action": {
                "confirmation_id": confirmation_id,
                "conversation_id": conversation_id,
                "original_turn_id": turn_id,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "action": {
                    "action_type": "create_support_ticket",
                    "category": "card",
                    "subject": "Card payment failed",
                    "priority": "HIGH",
                    "description": "A synthetic example.",
                },
            },
        }
        messages = messages_from_turn_response(response)
        self.assertEqual(messages[1]["pending_action"]["original_turn_id"], turn_id)
        api = FakeSupportAPI()
        state = {"conversation_id": conversation_id, "audio_cache": {}, "audio_playback_visible": {}}
        st = FakeStreamlit(clicked=f"support-confirm-create:{confirmation_id}")
        render_messages(st, messages, state, api=api)
        self.assertTrue(any("not" in str(value).lower() for value in st.warnings))

        outcome = submit_support_confirmation(api, state, response["pending_action"], confirmed=True)
        self.assertEqual(outcome["status"], "executed")
        self.assertEqual(api.calls[-1], (
            "confirm", confirmation_id, {
                "confirmed": True,
                "conversation_id": conversation_id,
                "original_turn_id": turn_id,
                "action_type": "create_support_ticket",
            },
        ))
        # A duplicate UI action after success returns the stored result.
        calls_before = len(api.calls)
        submit_support_confirmation(api, state, response["pending_action"], confirmed=True)
        self.assertEqual(len(api.calls), calls_before)

    def test_confirmation_buttons_render_only_for_valid_unexpired_pending_action(self):
        from datetime import datetime, timezone

        pending = {
            "confirmation_id": "confirmation-ui",
            "conversation_id": "conversation-ui",
            "original_turn_id": "turn-ui",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "action": {"action_type": "create_support_ticket", "category": "card", "subject": "Card", "priority": "HIGH"},
        }
        self.assertEqual(support_confirmation_state(
            pending, now=datetime(2026, 1, 1, tzinfo=timezone.utc)
        ), "pending")
        for status in ("executed", "declined", "expired", "rejected", "processing", "failed"):
            state = {
                "conversation_id": "conversation-ui", "audio_cache": {}, "audio_playback_visible": {},
                "support_action_outcomes": {"create:confirmation-ui": {"status": status}},
            }
            st = FakeStreamlit()
            render_messages(st, [{"role": "assistant", "pending_action": pending}], state, FakeSupportAPI())
            self.assertFalse(any(str(key).startswith(("support-confirm-", "support-decline-")) for key in st.buttons), status)

        for expiry in ("2020-01-01T00:00:00+00:00", "not-a-date", None, "2099-01-01T00:00:00", 123):
            action = dict(pending, expires_at=expiry)
            state = {"conversation_id": "conversation-ui", "audio_cache": {}, "audio_playback_visible": {}}
            st = FakeStreamlit()
            render_messages(st, [{"role": "assistant", "pending_action": action}], state, FakeSupportAPI())
            self.assertFalse(any(str(key).startswith(("support-confirm-", "support-decline-")) for key in st.buttons), repr(expiry))

        state = {"conversation_id": "conversation-ui", "audio_cache": {}, "audio_playback_visible": {}}
        st = FakeStreamlit()
        render_messages(st, [{"role": "assistant", "pending_action": pending}], state, FakeSupportAPI())
        self.assertEqual(
            {key for key in st.buttons if str(key).startswith(("support-confirm-", "support-decline-"))},
            {"support-confirm-create:confirmation-ui", "support-decline-create:confirmation-ui"},
        )

    def test_decline_and_conversation_mismatch_do_not_create_or_confirm(self):
        api = FakeSupportAPI()
        action = {
            "confirmation_id": "confirmation-2",
            "conversation_id": "c1",
            "original_turn_id": "t1",
            "action": {"action_type": "create_support_ticket"},
        }
        state = {"conversation_id": "c1"}
        result = submit_support_confirmation(api, state, action, confirmed=False)
        self.assertEqual(result["status"], "declined")
        self.assertFalse(api.calls[-1][2]["confirmed"])
        with self.assertRaises(ValueError):
            submit_support_confirmation(api, {"conversation_id": "c2"}, action, confirmed=True)
        self.assertEqual(len(api.calls), 1)

    def test_ticket_closure_proposal_uses_explicit_close_endpoint(self):
        api = FakeSupportAPI()
        state = {"conversation_id": "c1"}
        confirmation = {
            "conversation_id": "c1", "ticket_ref": "VS-" + "C" * 32,
            "confirmation_method": "button",
        }
        result = submit_support_closure(api, state, confirmation)
        self.assertEqual(result["ticket"]["status"], "CLOSED")
        self.assertEqual(api.calls, [("close", confirmation["ticket_ref"])])

    def test_expired_or_replayed_confirmation_is_reported_without_automatic_retry(self):
        class ReplayedAPI:
            def __init__(self):
                self.calls = 0

            def confirm_assistant_action(self, *_args, **_kwargs):
                self.calls += 1
                raise APIError(409, "Confirmation has already been consumed.")

        api = ReplayedAPI()
        action = {
            "confirmation_id": "confirmation-replayed", "conversation_id": "c1",
            "original_turn_id": "t1", "action": {"action_type": "create_support_ticket"},
        }
        state = {"conversation_id": "c1", "support_action_outcomes": {}}
        st = FakeStreamlit()
        _run_support_confirmation(st, api, state, action, True)
        self.assertEqual(api.calls, 1)
        self.assertEqual(state["support_action_outcomes"]["create:confirmation-replayed"]["status"], "rejected")
        self.assertTrue(any("already used" in text for text in st.warnings))

    def test_new_conversation_clears_stale_support_actions(self):
        state = {
            "conversation_id": "old",
            "conversation_messages": [{"pending_action": {"confirmation_id": "id"}}],
            "support_action_outcomes": {"create:id": {"status": "executed"}},
            "audio_cache": {},
        }
        select_conversation(state, "new")
        self.assertEqual(state["conversation_id"], "new")
        self.assertEqual(state["conversation_messages"], [])
        self.assertEqual(state["support_action_outcomes"], {})


if __name__ == "__main__":
    unittest.main()
