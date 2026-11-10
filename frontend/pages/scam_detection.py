"""Authenticated message and call assessment entry points."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from uuid import UUID

from frontend.api_client import APIError
from frontend.components.conversation import (
    render_scam_assessment,
    render_transaction_risk_assessment,
)
from frontend.components.conversation_history import create_new_conversation
from frontend.components.voice_composer import render_voice_input
from frontend.session_state import bind_pending_scam_assessment, clear_pending_scam_assessment, sign_out


_TRANSACTION_REF = re.compile(r"^TR-[A-F0-9]{48}$")
_TRANSACTION_PAGE_SIZE = 10
_TRANSACTION_API_LIMIT = 100
_ASSESSMENT_PAGE_SIZE = 10
_REPORT_PAGE_SIZE = 25
_REPORT_SOURCES = {
    "All sources": None,
    "Manual Message": "MANUAL_MESSAGE",
    "Incoming SMS": "INCOMING_SMS",
    "Voice Call": "VOICE_CALL",
}
_REPORT_RISKS = {
    "All risks": None, "Critical": "CRITICAL", "High": "HIGH",
    "Medium": "MEDIUM", "Low": "LOW", "Unknown": "UNKNOWN",
}
_TRANSACTION_RISK_STATE_KEYS = (
    "transaction_risk_transactions", "transaction_risk_tx_page",
    "transaction_risk_selected_ref", "transaction_risk_latest_result",
    "transaction_risk_transactions_error", "transaction_risk_history_items",
    "transaction_risk_history_cursor", "transaction_risk_history_has_more",
    "transaction_risk_history_error", "transaction_risk_detail",
    "transaction_risk_detail_id", "transaction_risk_detail_error",
    "transaction_risk_follow_up_assessment_id",
    "transaction_risk_follow_up_conversation_id",
)


def _reset_transaction_risk_state(state):
    defaults = {
        "transaction_risk_transactions": None,
        "transaction_risk_tx_page": 0,
        "transaction_risk_selected_ref": None,
        "transaction_risk_latest_result": None,
        "transaction_risk_transactions_error": None,
        "transaction_risk_history_items": None,
        "transaction_risk_history_cursor": None,
        "transaction_risk_history_has_more": False,
        "transaction_risk_history_error": None,
        "transaction_risk_detail": None,
        "transaction_risk_detail_id": None,
        "transaction_risk_detail_error": None,
        "transaction_risk_follow_up_assessment_id": None,
        "transaction_risk_follow_up_conversation_id": None,
    }
    for key in _TRANSACTION_RISK_STATE_KEYS:
        state[key] = defaults[key]


def _safe_transaction_rows(rows):
    """Keep only public refs and fields required by the Transaction Risk UI."""
    if not isinstance(rows, list):
        return []
    safe = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        reference = row.get("transaction_ref")
        if not isinstance(reference, str) or not _TRANSACTION_REF.fullmatch(reference):
            continue
        item = {"transaction_ref": reference}
        for field in ("transaction_date", "merchant", "transaction_type", "status", "category"):
            value = row.get(field)
            item[field] = str(value) if value is not None else None
        amount = row.get("amount")
        item["amount"] = amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) else None
        safe.append(item)
    return safe


def load_transaction_risk_transactions(api, state):
    """Load the authenticated customer's supported transaction selection window."""
    customer_key = (state.get("customer") or {}).get("customer_key")
    if state.get("transaction_risk_customer_key") != customer_key:
        _reset_transaction_risk_state(state)
        state["transaction_risk_customer_key"] = customer_key
    if state.get("transaction_risk_transactions") is None:
        state["transaction_risk_transactions"] = _safe_transaction_rows(
            api.get_my_transactions(limit=_TRANSACTION_API_LIMIT)
        )
        state["transaction_risk_transactions_error"] = None
    return state["transaction_risk_transactions"]


def assess_selected_transaction(api, state, transaction_ref):
    """Assess only an opaque reference present in the authenticated transaction list."""
    if not isinstance(transaction_ref, str) or not _TRANSACTION_REF.fullmatch(transaction_ref):
        raise ValueError("Select a transaction with a valid public reference.")
    known_refs = {
        item["transaction_ref"]
        for item in (state.get("transaction_risk_transactions") or [])
        if isinstance(item, dict)
    }
    if transaction_ref not in known_refs:
        raise ValueError("That transaction is not in the authenticated transaction list. Refresh the list and try again.")
    result = api.assess_transaction_risk(transaction_ref)
    state["transaction_risk_latest_result"] = result
    state["transaction_risk_history_items"] = None
    state["transaction_risk_history_cursor"] = None
    state["transaction_risk_history_has_more"] = False
    state["transaction_risk_detail"] = None
    state["transaction_risk_detail_id"] = None
    return result


def load_transaction_risk_history(api, state, *, append=False):
    cursor = state.get("transaction_risk_history_cursor") if append else None
    page = api.list_transaction_risk_assessments(
        limit=_ASSESSMENT_PAGE_SIZE, cursor=cursor,
    )
    items = page.get("items", []) if isinstance(page, dict) else []
    state["transaction_risk_history_items"] = (
        (state.get("transaction_risk_history_items") or []) + items
        if append else items
    )
    state["transaction_risk_history_cursor"] = page.get("next_cursor")
    state["transaction_risk_history_has_more"] = bool(page.get("has_more"))
    state["transaction_risk_history_error"] = None
    return state["transaction_risk_history_items"]


def open_transaction_risk_assessment(api, state, assessment_id):
    try:
        canonical_id = str(UUID(str(assessment_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Select a valid public assessment identifier.") from None
    known_ids = {
        str(item.get("assessment_id"))
        for item in (state.get("transaction_risk_history_items") or [])
        if isinstance(item, dict) and item.get("assessment_id")
    }
    if canonical_id not in known_ids:
        raise ValueError("That assessment is not in the authenticated history list. Refresh history and try again.")
    detail = api.get_transaction_risk_assessment(canonical_id)
    state["transaction_risk_detail"] = detail
    state["transaction_risk_detail_id"] = canonical_id
    state["transaction_risk_detail_error"] = None
    return detail


def begin_transaction_risk_discussion(api, state, assessment_id):
    """Open Vaani AI in a new conversation with an owner-scoped public assessment reference."""
    try:
        public_id = str(UUID(str(assessment_id)))
    except (ValueError, TypeError, AttributeError):
        return None
    conversation = create_new_conversation(api, state)
    state["transaction_risk_follow_up_assessment_id"] = public_id
    state["transaction_risk_follow_up_conversation_id"] = conversation["conversation_id"]
    state["assistant_open"] = True
    return conversation


def begin_assessment_discussion(api, state, assessment_id=None):
    """Open a fresh assistant conversation explicitly scoped to this result."""
    assessment = state.get("latest_scam_assessment") or {}
    assessment_id = assessment_id or assessment.get("assessment_id")
    if not assessment_id:
        return None
    conversation = create_new_conversation(api, state)
    bind_pending_scam_assessment(
        state, assessment_id, conversation["conversation_id"],
    )
    state["assistant_open"] = True
    return conversation


def _start_new_assessment(state):
    clear_pending_scam_assessment(state)
    state["latest_scam_assessment"] = None
    state["scam_assessment_error"] = None


def _invalidate_analysis_reports(state):
    """Refresh cached historical reports after creating a new assessment."""
    state["analysis_reports_cache_key"] = None
    state["analysis_reports_items"] = None
    state["analysis_reports_cursor"] = None
    state["analysis_reports_has_more"] = False
    state["analysis_reports_detail"] = None
    state["analysis_reports_detail_key"] = None


def assess_message(api, state, message_text):
    """Create and display the exact result returned for this text submission."""
    if not isinstance(message_text, str) or not message_text.strip():
        raise ValueError("Paste the message text first.")
    _start_new_assessment(state)
    assessment = api.assess_scam_message(
        message_text.strip(), state.get("language_hint", "auto"),
    )
    if not isinstance(assessment, dict) or not assessment.get("assessment_id"):
        raise ValueError("The assessment service returned an incomplete report.")
    # The response is a per-request JSON object. Copy its top-level envelope so
    # UI state cannot alias a fake/provider-owned response object across turns.
    state["latest_scam_assessment"] = deepcopy(assessment)
    state["scam_assessment_error"] = None
    _invalidate_analysis_reports(state)
    return state["latest_scam_assessment"]


def assess_voice_recording(api, state, filename, content, media_type):
    """Upload once, then let the call-assessment route consume that upload."""
    if not content:
        raise ValueError("No audio was captured.")
    _start_new_assessment(state)
    upload = api.upload_audio(filename, content, media_type)
    assessment = api.assess_scam_call(
        upload["audio_upload_id"], state.get("language_hint", "auto"),
    )
    state["latest_scam_assessment"] = deepcopy(assessment)
    _invalidate_analysis_reports(state)
    return assessment


def _show_result(st, api, state):
    assessment = state.get("latest_scam_assessment")
    if not isinstance(assessment, dict):
        if state.get("scam_assessment_error"):
            st.error(state["scam_assessment_error"])
        return
    transcript = assessment.get("transcript")
    if transcript:
        st.markdown("**Transcript for review**")
        st.text(transcript)
        if assessment.get("transcription_warning"):
            st.caption(assessment["transcription_warning"])
    render_scam_assessment(st, assessment)
    if st.button("Discuss this assessment with Vaani AI", key="discuss-scam-assessment"):
        try:
            begin_assessment_discussion(api=api, state=state)
            st.rerun()
        except APIError as exc:
            _handle_error(st, state, exc)


def _handle_error(st, state, exc):
    if exc.status_code == 401:
        sign_out(state)
        st.warning("Your demo session expired. Sign in again to continue.")
        st.rerun()
    messages = {
        404: "The audio upload is no longer available. Record it again to assess it.",
        413: "That recording is too large. Please record a shorter message.",
        415: "This recording format is not supported.",
        422: "The message could not be assessed. Check that text or speech was provided.",
        503: "Scam assessment is temporarily unavailable. Please try again later.",
        0: "The VaaniShield API could not be reached.",
    }
    st.error(messages.get(exc.status_code, "The assessment could not be completed."))


def _pairing_error_message(status_code):
    """Return safe, user-facing pairing errors without backend/session details."""
    messages = {
        0: "The VaaniShield API could not be reached. Check the connection and try again.",
        401: "Your demo session has expired. Sign in again to generate a pairing code.",
        429: "Too many pairing-code requests. Please wait a few minutes and try again.",
    }
    return messages.get(status_code, "A pairing code could not be generated. Please try again.")


def _render_android_sms_pairing(st, api, state):
    """Render pairing controls; ownership is always resolved from the API session."""
    st.subheader("Android SMS Companion")
    st.caption(
        "Generate a short-lived code, then enter it in the VaaniShield Android companion. "
        "The code is tied to the customer signed in to this session."
    )

    customer_key = (state.get("customer") or {}).get("customer_key")
    if state.get("android_sms_pairing_customer_key") != customer_key:
        state.pop("android_sms_pairing", None)
        state["android_sms_pairing_customer_key"] = customer_key

    if st.button("Generate Pairing Code", key="android-sms-generate-pairing-code"):
        try:
            result = api.create_android_sms_pairing_code()
            code = result.get("pairing_code") if isinstance(result, dict) else None
            expires_at = result.get("expires_at") if isinstance(result, dict) else None
            if not isinstance(code, str) or not code or not isinstance(expires_at, str):
                st.error("The server returned an incomplete pairing response. Please try again.")
            else:
                state["android_sms_pairing"] = {
                    "pairing_code": code,
                    "expires_at": expires_at,
                }
        except APIError as exc:
            message = _pairing_error_message(exc.status_code)
            if exc.status_code == 401:
                sign_out(state)
                st.warning(message)
                st.rerun()
            st.error(message)

    pairing = state.get("android_sms_pairing")
    if not isinstance(pairing, dict):
        return

    st.markdown("**Enter this code in the Android companion**")
    st.code(pairing["pairing_code"], language=None)
    try:
        expires = datetime.fromisoformat(pairing["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
        expiry_text = expires.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        if remaining:
            countdown = f"{remaining // 60}m {remaining % 60}s remaining"
            st.caption(f"Expires at {expiry_text} · {countdown} (updates when the page reruns).")
        else:
            st.warning(f"This code expired at {expiry_text}. Generate a new code to pair a device.")
    except (TypeError, ValueError, OverflowError):
        st.caption("The server supplied an expiry time that could not be displayed.")


def _render_message_analysis(st, api, state):
    st.caption("Paste a suspicious SMS, email, or chat message. Never include an OTP, PIN, or password.")
    message = st.text_area("Message to assess", key="scam-message-input", max_chars=12000)
    if st.button("Analyze message", key="analyze-scam-message", type="primary"):
        if not message or not message.strip():
            _start_new_assessment(state)
            st.warning("Paste the message text first.")
        else:
            try:
                assess_message(api, state, message)
            except APIError as exc:
                state["scam_assessment_error"] = "The message could not be assessed. Please try again."
                _handle_error(st, state, exc)
    _show_result(st, api, state)


def _render_call_analysis(st, api, state):
    st.caption("Record the suspicious call or message. The recording is uploaded once and consumed by the authenticated scam-assessment endpoint.")
    audio = render_voice_input(
        st, int(state.get("scam_audio_epoch", 0)), key_prefix="scam",
    )
    if audio is not None:
        raw = audio.getvalue()
        digest = hashlib.sha256(raw).hexdigest() if raw else None
        if digest and digest != state.get("last_scam_audio_hash"):
            state["last_scam_audio_hash"] = digest
            try:
                assess_voice_recording(
                    api,
                    state,
                    getattr(audio, "name", "scam-call.wav") or "scam-call.wav",
                    raw,
                    getattr(audio, "type", None) or "audio/wav",
                )
            except APIError as exc:
                state["scam_assessment_error"] = "The recording could not be assessed. Record a new clip and try again."
                _handle_error(st, state, exc)
            finally:
                state["scam_audio_epoch"] = int(state.get("scam_audio_epoch", 0)) + 1
                state["last_scam_audio_hash"] = None
            st.rerun()
    _show_result(st, api, state)


def _transaction_label(transaction):
    merchant = transaction.get("merchant") or "Merchant unavailable"
    amount = transaction.get("amount")
    amount_label = f"₹{amount:,.2f}" if amount is not None else "Amount unavailable"
    date = transaction.get("transaction_date") or "Date unavailable"
    return f"{date} · {merchant} · {amount_label} · {transaction.get('transaction_type') or 'Type unknown'}"


def _transaction_map(state):
    return {
        item.get("transaction_ref"): item
        for item in (state.get("transaction_risk_transactions") or [])
        if isinstance(item, dict) and item.get("transaction_ref")
    }


def _transaction_api_error(st, state, exc, context):
    if exc.status_code == 401:
        sign_out(state)
        st.warning("Your demo session expired. Sign in again to continue.")
        st.rerun()
    if context == "transactions":
        message = {
            0: "The authenticated transaction service could not be reached.",
            503: "Transactions are temporarily unavailable. Please try again.",
        }.get(exc.status_code, "Your transactions could not be loaded.")
        state["transaction_risk_transactions_error"] = message
    elif context == "history":
        message = {
            0: "The assessment history service could not be reached.",
            503: "Assessment history is temporarily unavailable. Please try again.",
        }.get(exc.status_code, "Assessment history could not be loaded.")
        state["transaction_risk_history_error"] = message
    else:
        message = {
            0: "The assessment service could not be reached.",
            404: "This transaction or assessment is not available in your account.",
            422: "The selected transaction reference could not be validated.",
            503: "Transaction-risk assessment is temporarily unavailable. Please try again.",
        }.get(exc.status_code, "The transaction-risk request could not be completed.")
        state["transaction_risk_detail_error"] = message
    st.error(message)


def _render_transaction_risk(st, api, state):
    st.caption(
        "Automated screening of synthetic demo transactions. A risk result is not a "
        "bank-authorized fraud determination and does not confirm fraud."
    )
    customer_key = (state.get("customer") or {}).get("customer_key")
    if state.get("transaction_risk_customer_key") != customer_key:
        _reset_transaction_risk_state(state)
        state["transaction_risk_customer_key"] = customer_key

    try:
        with st.spinner("Loading your authenticated transactions…"):
            transactions = load_transaction_risk_transactions(api, state)
    except APIError as exc:
        _transaction_api_error(st, state, exc, "transactions")
        if st.button("Retry transaction list", key="transaction-risk-retry-transactions"):
            state["transaction_risk_transactions"] = None
            st.rerun()
        return

    if not transactions:
        st.info("No assessable transactions are available in your authenticated demo account.")
    else:
        st.markdown("#### Select a transaction")
        st.caption("Only transactions returned by your authenticated self-service API are shown. Public transaction references are used for assessment.")
        page_count = max(1, (len(transactions) + _TRANSACTION_PAGE_SIZE - 1) // _TRANSACTION_PAGE_SIZE)
        page_index = min(max(0, int(state.get("transaction_risk_tx_page", 0))), page_count - 1)
        state["transaction_risk_tx_page"] = page_index
        start = page_index * _TRANSACTION_PAGE_SIZE
        visible = transactions[start:start + _TRANSACTION_PAGE_SIZE]
        for transaction in visible:
            with st.container(border=True):
                st.markdown(f"**{_transaction_label(transaction)}**")
                st.caption(
                    f"{transaction.get('transaction_type') or 'Type unknown'} · "
                    f"{transaction.get('status') or 'Status unknown'} · "
                    f"Public reference: {transaction['transaction_ref']}"
                )
        if len(transactions) > _TRANSACTION_PAGE_SIZE:
            left, right = st.columns(2, vertical_alignment="center")
            if left.button("Previous transactions", disabled=page_index == 0, key="transaction-risk-prev"):
                state["transaction_risk_tx_page"] = page_index - 1
                st.rerun()


            if right.button("Next transactions", disabled=page_index >= page_count - 1, key="transaction-risk-next"):
                state["transaction_risk_tx_page"] = page_index + 1
                st.rerun()
            st.caption(f"Page {page_index + 1} of {page_count}. The authenticated transaction API currently returns at most {_TRANSACTION_API_LIMIT} records per request.")

        transaction_by_ref = _transaction_map(state)
        page_refs = [item["transaction_ref"] for item in visible]
        select_key = f"transaction-risk-select-{page_index}"
        selected_ref = st.selectbox(
            "Transaction to assess", page_refs,
            format_func=lambda ref: _transaction_label(transaction_by_ref[ref]),
            key=select_key,
        )
        state["transaction_risk_selected_ref"] = selected_ref
        if st.button("Assess transaction risk", key="transaction-risk-assess", type="primary"):
            try:
                with st.spinner("Running the backend transaction-risk assessment…"):
                    assess_selected_transaction(api, state, selected_ref)
            except APIError as exc:
                _transaction_api_error(st, state, exc, "assessment")
            except ValueError as exc:
                st.warning(str(exc))

    result = state.get("transaction_risk_latest_result")
    transaction_by_ref = _transaction_map(state)
    if isinstance(result, dict):
        st.divider()
        render_transaction_risk_assessment(
            st, result, transaction_by_ref.get(
                (result.get("transaction") or {}).get("transaction_ref")
            ),
        )
        assessment_id = result.get("assessment_id")
        if assessment_id and st.button("Ask Vaani AI about this assessment", key="transaction-risk-discuss-latest"):
            conversation = begin_transaction_risk_discussion(api, state, assessment_id)
            if conversation:
                st.rerun()

    st.divider()
    st.markdown("#### Assessment history")
    if state.get("transaction_risk_history_items") is None:
        try:
            with st.spinner("Loading assessment history…"):
                load_transaction_risk_history(api, state)
        except APIError as exc:
            _transaction_api_error(st, state, exc, "history")
    history = state.get("transaction_risk_history_items") or []
    if not history and not state.get("transaction_risk_history_error"):
        st.info("No transaction-risk assessments have been recorded yet.")
    for item in history:
        if not isinstance(item, dict):
            continue
        ref = item.get("transaction_ref")
        transaction = item.get("transaction") or transaction_by_ref.get(ref, {})
        risk = str(item.get("risk_level", "UNKNOWN")).upper()
        with st.container(border=True):
            st.markdown(
                f"**{risk} · {transaction.get('merchant') or 'Transaction'}** · "
                f"{('₹' + format(transaction['amount'], ',.2f')) if transaction.get('amount') is not None else 'Amount unavailable'}"
            )
            st.caption(
                f"Assessment: {item.get('assessment_id')} · {item.get('created_at')} · "
                f"Transaction reference: {ref or 'Unavailable'}"
            )
            assessment_id = item.get("assessment_id")
            if assessment_id and st.button(
                "Open assessment", key=f"transaction-risk-open-{assessment_id}"
            ):
                try:
                    open_transaction_risk_assessment(api, state, assessment_id)
                except APIError as exc:
                    _transaction_api_error(st, state, exc, "detail")
                except ValueError as exc:
                    st.warning(str(exc))
        if assessment_id and state.get("transaction_risk_detail_id") == str(assessment_id):
            detail = state.get("transaction_risk_detail")
            if detail:
                render_transaction_risk_assessment(
                    st, detail, transaction_by_ref.get(detail.get("transaction_ref")),
                )
                if st.button("Ask Vaani AI about this assessment", key=f"transaction-risk-discuss-{assessment_id}"):
                    conversation = begin_transaction_risk_discussion(api, state, assessment_id)
                    if conversation:
                        st.rerun()
    if state.get("transaction_risk_history_has_more"):
        if st.button("Load more assessments", key="transaction-risk-history-more"):
            try:
                with st.spinner("Loading more assessments…"):
                    load_transaction_risk_history(api, state, append=True)
                st.rerun()
            except APIError as exc:
                _transaction_api_error(st, state, exc, "history")
    if state.get("transaction_risk_history_error"):
        if st.button("Retry assessment history", key="transaction-risk-history-retry"):
            state["transaction_risk_history_items"] = None
            state["transaction_risk_history_error"] = None
            st.rerun()


def load_investigations(api, state, *, append=False):
    """Load the authenticated customer's investigation page only."""
    customer_key = (state.get("customer") or {}).get("customer_key")
    if state.get("investigation_customer_key") != customer_key:
        for key, value in {
            "investigation_items": None, "investigation_cursor": None,
            "investigation_has_more": False, "investigation_detail": None,
            "investigation_detail_ref": None, "investigation_action_outcomes": {},
            "investigation_evidence_items": None,
            "investigation_transaction_evidence_items": None,
        }.items():
            state[key] = value.copy() if isinstance(value, (dict, list)) else value
        state["investigation_customer_key"] = customer_key
    cursor = state.get("investigation_cursor") if append else None
    status_filter = state.get("investigation_status_filter", "All statuses")
    category_filter = state.get("investigation_category_filter", "All categories")
    search = str(state.get("investigation_search", "")).strip()
    filter_key = (status_filter, category_filter, search.casefold())
    if not append and state.get("investigation_filter_key") != filter_key:
        state["investigation_items"] = None
        cursor = None
    page = api.list_investigations(
        limit=10, cursor=cursor,
        status=None if status_filter == "All statuses" else status_filter,
        category=None if category_filter == "All categories" else category_filter,
        search=search or None,
    )
    state["investigation_filter_key"] = filter_key
    items = page.get("items", []) if isinstance(page, dict) else []
    state["investigation_items"] = (state.get("investigation_items") or []) + items if append else items
    state["investigation_cursor"] = page.get("next_cursor")
    state["investigation_has_more"] = bool(page.get("has_more"))
    state["investigation_error"] = None
    return state["investigation_items"]


def open_investigation(api, state, investigation_ref):
    if not isinstance(investigation_ref, str) or not re.fullmatch(r"INV-[A-F0-9]{32}", investigation_ref):
        raise ValueError("Select an investigation with a valid public reference.")
    known = {item.get("investigation_ref") for item in (state.get("investigation_items") or []) if isinstance(item, dict)}
    if investigation_ref not in known:
        raise ValueError("That investigation is not in your loaded list. Refresh and try again.")
    detail = api.get_investigation(investigation_ref)
    state["investigation_detail"] = detail
    state["investigation_detail_ref"] = investigation_ref
    return detail


def _investigation_error(st, state, exc):
    if exc.status_code == 401:
        sign_out(state)
        st.warning("Your demo session expired. Sign in again to continue.")
        st.rerun()
    message = {
        0: "The investigation service could not be reached.",
        404: "The investigation or assessment is not available in your account.",
        409: "The investigation changed or the requested transition is not allowed. Refresh its details.",
        422: "Review the investigation details and try again.",
        503: "Investigation tracking is temporarily unavailable.",
    }.get(exc.status_code, "The investigation request could not be completed.")
    state["investigation_error"] = message
    st.error(message)


def _render_investigation_detail(st, api, state, detail):
    from frontend.components.conversation import render_investigation_detail
    render_investigation_detail(st, detail)
    ref = detail.get("investigation_ref")
    if not ref or detail.get("status") == "CLOSED":
        return
    evidence_choices = _available_investigation_evidence(state)
    if evidence_choices:
        with st.form(f"investigation-evidence-{ref}"):
            selected_evidence = st.selectbox(
                "Assessment to link",
                list(evidence_choices),
                format_func=lambda key: evidence_choices[key]["label"],
                key=f"investigation-evidence-choice-{ref}",
            )
            link_evidence = st.form_submit_button("Link assessment")
        if link_evidence:
            try:
                api.link_investigation_evidence(ref, evidence_choices[selected_evidence]["reference"])
                state["investigation_detail"] = api.get_investigation(ref)
                st.rerun()
            except APIError as exc:
                _investigation_error(st, state, exc)
    with st.form(f"investigation-note-{ref}"):
        note = st.text_area("Add a customer-reported update", max_chars=1500, key=f"investigation-note-input-{ref}")
        add_note = st.form_submit_button("Submit update")
    if add_note:
        try:
            api.add_investigation_note(ref, note)
            state["investigation_detail"] = api.get_investigation(ref)
            st.rerun()
        except APIError as exc:
            _investigation_error(st, state, exc)
    cols = st.columns(4)
    allowed_transitions = {
        "OPEN": ("UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED"),
        "UNDER_REVIEW": ("WAITING_FOR_INFORMATION", "RESOLVED"),
        "WAITING_FOR_INFORMATION": ("UNDER_REVIEW", "RESOLVED"),
        "RESOLVED": (), "CLOSED": (),
    }.get(detail.get("status"), ())
    transitions = allowed_transitions
    for col, target in zip(cols[:3], transitions):
        if col.button(f"Mark {target.replace('_', ' ').title()}", key=f"investigation-status-{ref}-{target}"):
            try:
                api.update_investigation_status(ref, target)
                state["investigation_detail"] = api.get_investigation(ref)
                st.rerun()
            except APIError as exc:
                _investigation_error(st, state, exc)
    if cols[3].button("Close investigation", key=f"investigation-close-{ref}"):
        try:
            api.close_investigation(ref)
            state["investigation_detail"] = api.get_investigation(ref)
            st.rerun()
        except APIError as exc:
            _investigation_error(st, state, exc)


def _render_investigations(st, api, state):
    st.caption("Personal synthetic security tracking. This does not contact bank staff or represent an official bank investigation.")
    if state.get("investigation_evidence_items") is None and hasattr(api, "list_scam_assessments"):
        try:
            page = api.list_scam_assessments(limit=25)
            state["investigation_evidence_items"] = page.get("items", []) if isinstance(page, dict) else []
        except APIError:
            # Evidence history is optional; investigation tracking remains available.
            state["investigation_evidence_items"] = []
    if state.get("investigation_transaction_evidence_items") is None and hasattr(api, "list_transaction_risk_assessments"):
        try:
            page = api.list_transaction_risk_assessments(limit=25)
            state["investigation_transaction_evidence_items"] = page.get("items", []) if isinstance(page, dict) else []
        except APIError:
            state["investigation_transaction_evidence_items"] = []
    with st.expander("Create an investigation", expanded=not bool(state.get("investigation_items"))):
        with st.form("create-personal-investigation"):
            title = st.text_input("Title", max_chars=120)
            category = st.selectbox("Category", ["MESSAGE_SCAM", "CALL_SCAM", "TRANSACTION_RISK", "ACCOUNT_SECURITY", "OTHER"])
            summary = st.text_area("What would you like to track?", max_chars=1500)
            evidence_choices = _available_investigation_evidence(state)
            selected_evidence = st.multiselect(
                "Link existing assessments (optional)", list(evidence_choices),
                format_func=lambda key: evidence_choices[key]["label"],
                key="investigation-create-evidence",
            ) if evidence_choices else []
            submit = st.form_submit_button("Create synthetic investigation", type="primary")
        if submit:
            try:
                created = api.create_investigation({
                    "title": title, "category": category, "summary": summary,
                    "evidence": [evidence_choices[key]["reference"] for key in selected_evidence],
                    "confirmed": True,
                })
                state["investigation_detail"] = created
                state["investigation_detail_ref"] = created.get("investigation_ref")
                state["investigation_items"] = None
                st.success("Personal synthetic investigation created.")
                st.rerun()
            except APIError as exc:
                _investigation_error(st, state, exc)
            except ValueError:
                st.warning("Provide a title and summary before creating the record.")
    filters = st.columns(3)
    state["investigation_status_filter"] = filters[0].selectbox(
        "Status", ["All statuses", "OPEN", "UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED", "CLOSED"],
        key="investigation-status-filter",
    )
    state["investigation_category_filter"] = filters[1].selectbox(
        "Category", ["All categories", "MESSAGE_SCAM", "CALL_SCAM", "TRANSACTION_RISK", "ACCOUNT_SECURITY", "OTHER"],
        key="investigation-category-filter",
    )
    state["investigation_search"] = filters[2].text_input("Search", max_chars=100, key="investigation-search-filter")
    filter_key = (
        state["investigation_status_filter"], state["investigation_category_filter"],
        state["investigation_search"].strip().casefold(),
    )
    if state.get("investigation_filter_key") != filter_key:
        state["investigation_items"] = None
        state["investigation_cursor"] = None
        state["investigation_has_more"] = False
    if state.get("investigation_items") is None:
        try:
            load_investigations(api, state)
        except APIError as exc:
            _investigation_error(st, state, exc)
            return
    items = state.get("investigation_items") or []
    if not items:
        st.info("No personal investigations have been recorded yet.")
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.get('title', 'Investigation')}** · {item.get('status', 'UNKNOWN')}")
            st.caption(f"{item.get('category', 'OTHER')} · {item.get('updated_at', '')} · {item.get('evidence_count', 0)} linked assessment(s)")
            ref = item.get("investigation_ref")
            if ref and st.button("Open timeline", key=f"investigation-open-{ref}"):
                try:
                    open_investigation(api, state, ref)
                    st.rerun()
                except APIError as exc:
                    _investigation_error(st, state, exc)
    if state.get("investigation_has_more") and st.button("Load more investigations", key="investigation-more"):
        try:
            load_investigations(api, state, append=True)
            st.rerun()
        except APIError as exc:
            _investigation_error(st, state, exc)
    detail = state.get("investigation_detail")
    if isinstance(detail, dict):
        st.divider()
        _render_investigation_detail(st, api, state, detail)


def _available_investigation_evidence(state):
    choices = {}
    scam = state.get("latest_scam_assessment")
    if isinstance(scam, dict) and scam.get("assessment_id"):
        source = "CALL_ASSESSMENT" if scam.get("source_type") == "voice_call" else "MESSAGE_ASSESSMENT"
        reference = {"evidence_type": source, "assessment_id": scam["assessment_id"]}
        key = f"{source}:{scam['assessment_id']}"
        choices[key] = {"reference": reference, "label": f"{source.replace('_', ' ').title()} · {scam.get('risk_level', 'UNKNOWN')} · {scam['assessment_id']}"}
    for item in state.get("investigation_evidence_items") or []:
        if isinstance(item, dict) and item.get("assessment_id"):
            source = "CALL_ASSESSMENT" if item.get("source_type") == "voice_call" else "MESSAGE_ASSESSMENT"
            assessment_id = str(item["assessment_id"])
            key = f"{source}:{assessment_id}"
            choices[key] = {
                "reference": {"evidence_type": source, "assessment_id": assessment_id},
                "label": f"{source.replace('_', ' ').title()} · {item.get('risk_level', 'UNKNOWN')} · {assessment_id}",
            }
    risk_items = (state.get("transaction_risk_history_items") or []) + (state.get("investigation_transaction_evidence_items") or [])
    for item in risk_items:
        if isinstance(item, dict) and item.get("assessment_id"):
            assessment_id = str(item["assessment_id"])
            key = f"TRANSACTION_RISK_ASSESSMENT:{assessment_id}"
            choices[key] = {
                "reference": {"evidence_type": "TRANSACTION_RISK_ASSESSMENT", "assessment_id": assessment_id},
                "label": f"Transaction-risk assessment · {item.get('risk_level', 'UNKNOWN')} · {assessment_id}",
            }
    return choices


def sms_report_id_from_notification(notification):
    """Extract a strictly validated SMS event reference from a notification hint."""
    if not isinstance(notification, dict):
        return None
    event_ref = notification.get("event_ref")
    candidates = []
    if isinstance(event_ref, str) and event_ref.startswith("sms-threat-"):
        candidates.append(event_ref[len("sms-threat-"):])
    metadata = notification.get("metadata")
    if not isinstance(metadata, dict):
        raw = notification.get("metadata_json")
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else {}
        except (ValueError, TypeError):
            metadata = {}
    if isinstance(metadata, dict) and metadata.get("source") == "android_sms_threat_intelligence":
        candidates.append(metadata.get("sms_event_id"))
    for candidate in candidates:
        try:
            return str(UUID(str(candidate)))
        except (ValueError, TypeError, AttributeError):
            continue
    return None


def queue_sms_report_navigation(state, notification):
    """Stage report navigation; API detail fetch still enforces customer ownership."""
    report_id = sms_report_id_from_notification(notification)
    if report_id is None:
        return False
    from frontend.navigation import request_navigation
    state["analysis_reports_pending_detail"] = {
        "source_type": "INCOMING_SMS", "report_id": report_id,
    }
    request_navigation(state, "Scam Detection", "Analysis Reports")
    return True


def _report_filter_key(state):
    filters = state.get("analysis_reports_filters") or {}
    return (
        filters.get("source_type"), filters.get("risk_level"),
        filters.get("date_from"), filters.get("date_to"),
        (filters.get("search") or "").strip().casefold(),
    )


def load_analysis_reports(api, state, *, append=False):
    """Load one backend cursor page and cache it for the active customer/filters."""
    customer_key = (state.get("customer") or {}).get("customer_key")
    key = (customer_key, *_report_filter_key(state))
    if state.get("analysis_reports_cache_key") != key:
        state["analysis_reports_items"] = None
        state["analysis_reports_cursor"] = None
        state["analysis_reports_has_more"] = False
        state["analysis_reports_detail"] = None
        state["analysis_reports_detail_key"] = None
        state["analysis_reports_cache_key"] = key
    cursor = state.get("analysis_reports_cursor") if append else None
    filters = state.get("analysis_reports_filters") or {}
    page = api.list_scam_reports(
        limit=_REPORT_PAGE_SIZE, cursor=cursor,
        source_type=filters.get("source_type"), risk_level=filters.get("risk_level"),
        date_from=filters.get("date_from"), date_to=filters.get("date_to"),
        search=(filters.get("search") or "").strip() or None,
    )
    incoming = page.get("items", []) if isinstance(page, dict) else []
    if not isinstance(incoming, list):
        incoming = []
    if append:
        existing = state.get("analysis_reports_items") or []
        seen = {str(item.get("report_id")) for item in existing if isinstance(item, dict)}
        incoming = [item for item in incoming if isinstance(item, dict) and str(item.get("report_id")) not in seen]
        state["analysis_reports_items"] = existing + incoming
    else:
        state["analysis_reports_items"] = incoming
    state["analysis_reports_cursor"] = page.get("next_cursor") if isinstance(page, dict) else None
    state["analysis_reports_has_more"] = bool(page.get("has_more")) if isinstance(page, dict) else False
    state["analysis_reports_error"] = None
    return state["analysis_reports_items"]


def open_analysis_report(api, state, source_type, report_id):
    detail = api.get_scam_report(source_type, report_id)
    state["analysis_reports_detail"] = detail
    state["analysis_reports_detail_key"] = (source_type, str(report_id))
    state["analysis_reports_detail_error"] = None
    return detail


def report_content_notice(detail):
    source = detail.get("source_type") if isinstance(detail, dict) else None
    availability = detail.get("content_availability") if isinstance(detail, dict) else None
    if source == "MANUAL_MESSAGE":
        return "The original message content is unavailable. Message text is not retained in analysis reports."
    if source == "VOICE_CALL" and availability == "TRANSCRIPT_EXPIRED":
        return "This redacted transcript expired after 30 days and is no longer available. The analysis report remains available."
    if source == "VOICE_CALL" and availability != "REDACTED_TRANSCRIPT":
        return "The historical call transcript is unavailable. It was not retained for this report."
    return None


def _analysis_reports_error(st, state, exc, *, detail=False):
    if exc.status_code == 401:
        sign_out(state)
        st.warning("Your demo session expired. Sign in again to view analysis reports.")
        st.rerun()
    message = _analysis_reports_error_text(exc.status_code)
    state["analysis_reports_detail_error" if detail else "analysis_reports_error"] = message
    st.error(message)


def _analysis_reports_error_text(status_code):
    return {
        0: "The analysis reports service could not be reached.",
        400: "Review the report filters and try again.",
        404: "This report is not available in your authenticated account.",
        503: "Analysis reports are temporarily unavailable. Please try again.",
    }.get(status_code, "Analysis reports could not be loaded.")


def _risk_badge(st, risk):
    colors = {
        "CRITICAL": ("#7f1d1d", "#fee2e2"), "HIGH": ("#9a3412", "#ffedd5"),
        "MEDIUM": ("#854d0e", "#fef9c3"), "LOW": ("#166534", "#dcfce7"),
        "UNKNOWN": ("#475569", "#e2e8f0"),
    }
    risk = str(risk or "UNKNOWN").upper()
    foreground, background = colors.get(risk, colors["UNKNOWN"])
    st.markdown(
        f'<span style="display:inline-block;padding:.2rem .6rem;border-radius:999px;'
        f'font-size:.78rem;font-weight:700;color:{foreground};background:{background};">{risk}</span>',
        unsafe_allow_html=True,
    )


def _render_report_detail(st, detail):
    st.divider()
    st.markdown("### Report details")
    headline = st.columns([1, 3])
    with headline[0]:
        _risk_badge(st, detail.get("risk_level"))
    with headline[1]:
        score = detail.get("risk_score")
        st.markdown(f"**{detail.get('source_type', 'Report').replace('_', ' ').title()}**")
        st.caption(f"Analyzed {detail.get('analysis_timestamp', 'time unavailable')}" + (f" · Risk score {score}" if score is not None else ""))
    st.caption(f"Report ID: {detail.get('report_id', 'Unavailable')}")
    st.markdown("**Explanation**")
    st.write(detail.get("explanation") or "No explanation was recorded.")
    if detail.get("warning"):
        st.warning(detail["warning"])
    st.markdown("**Detected indicators**")
    indicators = detail.get("indicators") or []
    if indicators:
        for indicator in indicators:
            if isinstance(indicator, dict):
                st.markdown(f"- **{indicator.get('category', 'Indicator')}** · {indicator.get('severity', 'UNKNOWN')} — {indicator.get('explanation', '')}")
    else:
        st.caption("No threat indicators were recorded.")
    actions = detail.get("recommended_actions") or []
    if actions:
        st.markdown("**Recommended actions**")
        for action in actions:
            st.markdown(f"- {action}")
    languages = detail.get("detected_languages") or []
    st.markdown("**Language information**")
    st.caption(
        f"Languages: {', '.join(languages) if languages else 'Unavailable'} · "
        f"Status: {detail.get('language_status') or 'Unavailable'} · "
        f"Code mixed: {'Yes' if detail.get('is_code_mixed') else 'No'}"
    )
    source = detail.get("source_type")
    availability = detail.get("content_availability")
    if source == "INCOMING_SMS" and detail.get("redacted_message_preview"):
        st.markdown("**Redacted SMS preview**")
        st.text(detail["redacted_message_preview"])
        if detail.get("sender"):
            st.caption(f"Sender: {detail['sender']}")
    elif source == "VOICE_CALL":
        transcript = detail.get("redacted_call_transcript")
        if transcript and availability == "REDACTED_TRANSCRIPT":
            st.markdown("**Redacted call transcript**")
            st.text(transcript)
            if detail.get("transcript_expires_at"):
                st.caption(f"Transcript retained until {detail['transcript_expires_at']}.")
        elif availability == "TRANSCRIPT_EXPIRED":
            st.info(report_content_notice(detail))
        else:
            st.info(report_content_notice(detail))
    elif source == "MANUAL_MESSAGE":
        st.info(report_content_notice(detail))
    st.caption(f"Analysis engine: {detail.get('engine_version') or 'Unavailable'}")


def _render_analysis_reports(st, api, state):
    st.markdown("## Analysis Reports")
    st.caption("Authenticated history of manual message, incoming SMS, and call analysis. Historical reports are displayed without rerunning analysis.")
    if "analysis_reports_filters" not in state:
        state["analysis_reports_filters"] = {"source_type": None, "risk_level": None, "date_from": None, "date_to": None, "search": ""}
    current = state["analysis_reports_filters"]
    with st.form("analysis-reports-filters", clear_on_submit=False):
        col_source, col_risk, col_start, col_end = st.columns(4)
        source_labels = list(_REPORT_SOURCES)
        risk_labels = list(_REPORT_RISKS)
        source_default = next((i for i, value in enumerate(_REPORT_SOURCES.values()) if value == current.get("source_type")), 0)
        risk_default = next((i for i, value in enumerate(_REPORT_RISKS.values()) if value == current.get("risk_level")), 0)
        selected_source = col_source.selectbox("Source", source_labels, index=source_default)
        selected_risk = col_risk.selectbox("Risk level", risk_labels, index=risk_default)
        selected_from = col_start.date_input("From", value=date.fromisoformat(current["date_from"]) if current.get("date_from") else None)
        selected_to = col_end.date_input("To", value=date.fromisoformat(current["date_to"]) if current.get("date_to") else None)
        search = st.text_input("Search persisted report details", value=current.get("search", ""), max_chars=200)
        submitted = st.form_submit_button("Apply filters", type="primary")
    if submitted:
        current.update({
            "source_type": _REPORT_SOURCES[selected_source],
            "risk_level": _REPORT_RISKS[selected_risk],
            "date_from": selected_from.isoformat() if isinstance(selected_from, date) else None,
            "date_to": selected_to.isoformat() if isinstance(selected_to, date) else None,
            "search": search.strip(),
        })
        state["analysis_reports_items"] = None
        state["analysis_reports_cursor"] = None
        state["analysis_reports_has_more"] = False
        state["analysis_reports_error"] = None
    try:
        if state.get("analysis_reports_items") is None or state.get("analysis_reports_cache_key") != ((state.get("customer") or {}).get("customer_key"), *_report_filter_key(state)):
            with st.spinner("Loading your analysis reports…"):
                load_analysis_reports(api, state)
    except APIError as exc:
        _analysis_reports_error(st, state, exc)
        if st.button("Retry reports", key="analysis-reports-retry"):
            state["analysis_reports_items"] = None
            st.rerun()
        return

    items = state.get("analysis_reports_items") or []
    counts = {risk: sum(1 for item in items if item.get("risk_level") == risk) for risk in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")}
    metrics = st.columns(5)
    metrics[0].metric("Reports loaded", len(items))
    for col, risk in zip(metrics[1:], ("CRITICAL", "HIGH", "MEDIUM", "LOW")):
        col.metric(risk.title(), counts[risk])
    st.caption("Counts and distributions reflect the report pages currently loaded. The API does not provide a total count.")
    source_counts = {source: sum(1 for item in items if item.get("source_type") == source) for source in ("MANUAL_MESSAGE", "INCOMING_SMS", "VOICE_CALL")}
    left, right = st.columns(2)
    with left:
        st.markdown("**Source distribution · loaded reports**")
        st.bar_chart(source_counts)
    with right:
        st.markdown("**Risk distribution · loaded reports**")
        st.bar_chart(counts)

    pending = state.pop("analysis_reports_pending_detail", None)
    if isinstance(pending, dict):
        state["analysis_reports_detail"] = None
        state["analysis_reports_detail_key"] = None
        try:
            with st.spinner("Opening the authenticated SMS report…"):
                open_analysis_report(api, state, pending.get("source_type"), pending.get("report_id"))
        except APIError as exc:
            _analysis_reports_error(st, state, exc, detail=True)
    detail = state.get("analysis_reports_detail")
    if isinstance(detail, dict):
        _render_report_detail(st, detail)
        if st.button("Close report details", key="analysis-reports-close-detail"):
            state["analysis_reports_detail"] = None
            state["analysis_reports_detail_key"] = None
            st.rerun()

    st.markdown("### Latest reports")
    if not items:
        st.info("No reports match these filters yet. Try a different source, risk level, date range, or search term.")
    for item in items:
        if not isinstance(item, dict):
            continue
        report_id = item.get("report_id")
        source = item.get("source_type")
        with st.container(border=True):
            badge_col, main_col, action_col = st.columns([1, 5, 1])
            with badge_col:
                _risk_badge(st, item.get("risk_level"))
            with main_col:
                st.markdown(f"**{str(source or 'REPORT').replace('_', ' ').title()}** · {item.get('analysis_timestamp', 'Time unavailable')}")
                if item.get("risk_score") is not None:
                    st.caption(f"Risk score: {item['risk_score']}")
                st.write(item.get("explanation") or "No explanation was recorded.")
                indicators = item.get("indicators") or []
                st.caption("Indicators: " + (", ".join(str(x.get("category", "Indicator")) for x in indicators if isinstance(x, dict)) or "None recorded"))
                st.caption(f"Report ID: {report_id or 'Unavailable'}")
            with action_col:
                if report_id and st.button("View", key=f"analysis-report-open-{source}-{report_id}"):
                    state["analysis_reports_detail"] = None
                    state["analysis_reports_detail_key"] = None
                    try:
                        with st.spinner("Loading report details…"):
                            open_analysis_report(api, state, source, report_id)
                        st.rerun()
                    except APIError as exc:
                        _analysis_reports_error(st, state, exc, detail=True)
    if state.get("analysis_reports_has_more") and st.button("Load more reports", key="analysis-reports-more"):
        try:
            with st.spinner("Loading more reports…"):
                load_analysis_reports(api, state, append=True)
            st.rerun()
        except APIError as exc:
            _analysis_reports_error(st, state, exc)


def render(st, subfeature="Message Analysis", api=None, state=None):
    st.markdown(
        "<div class='security-banner' style='margin-bottom:1.2rem;'>"
        "<div style='display:flex;align-items:center;gap:12px;'>"
        "<span style='font-size:1.8rem;'>🛡️</span>"
        "<div>"
        "<b style='font-size:1.05rem;color:#F5F3FF;font-family:\"Space Grotesk\",sans-serif;'>VaaniShield Security Intelligence Guard</b>"
        "<div style='font-size:0.75rem;color:#C4B5FD;margin-top:2px;'>Real-time AI behavioral screening · Synthetic Demo Threat Analysis</div>"
        "</div>"
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption("DEMO security screening · Automated results are advisory and are not confirmation of fraud.")
    if api is None or state is None:
        st.info("Sign in to use authenticated scam assessment.")
        return
    if subfeature == "Message Analysis":
        _render_android_sms_pairing(st, api, state)
        _render_message_analysis(st, api, state)
    elif subfeature == "Call Analysis":
        _render_call_analysis(st, api, state)
    elif subfeature == "Transaction Risk":
        _render_transaction_risk(st, api, state)
    elif subfeature == "Investigations":
        _render_investigations(st, api, state)
    elif subfeature == "Analysis Reports":
        _render_analysis_reports(st, api, state)
    else:
        st.info("Investigation tracking is planned. No investigation is created here.")
