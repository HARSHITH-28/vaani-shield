"""Authenticated Customer Support views for synthetic demo tickets."""

from __future__ import annotations

from frontend.api_client import APIError
from frontend.session_state import clear_support_ticket_cache, sign_out


CATEGORIES = {
    "Account": "account",
    "Card": "card",
    "Transaction": "transaction",
    "Digital Banking": "digital_banking",
    "Loan": "loan",
    "Deposit": "deposit",
    "Other": "other",
}
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")
STATUSES = ("OPEN", "IN_PROGRESS", "WAITING_FOR_CUSTOMER", "RESOLVED", "CLOSED")


def build_ticket_request(category, subject, priority, description=""):
    """Validate the form's required fields and match the existing API contract."""
    category_value = CATEGORIES.get(category, category)
    if category_value not in CATEGORIES.values():
        raise ValueError("Choose a valid ticket category.")
    if priority not in PRIORITIES:
        raise ValueError("Choose a valid ticket priority.")
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("Enter a short subject for your support request.")
    description = (description or "").strip() or "No additional description provided."
    if len(subject) > 160 or len(description) > 4000:
        raise ValueError("The subject or description is longer than allowed.")
    return {
        "category": category_value,
        "subject": subject,
        "priority": priority,
        "description": description,
    }


def submit_support_ticket(api, *, category, subject, priority, description=""):
    """Create a ticket only after the customer submits the support form."""
    return api.create_support_ticket(
        build_ticket_request(category, subject, priority, description)
    )


def close_ticket_after_confirmation(api, ticket_ref, confirmed):
    """Call the closure API only after a deliberate confirmation control."""
    if not confirmed:
        raise ValueError("Confirm closure before submitting the request.")
    return api.close_support_ticket(ticket_ref)


def _handle_api_error(st, state, exc, *, operation="support"):
    if exc.status_code == 401:
        sign_out(state)
        st.warning("Your demo session expired. Sign in again to continue.")
        st.rerun()
    messages = {
        400: "The request could not be validated. Review the details and try again.",
        404: "This ticket or confirmation is unavailable in this demo session.",
        409: "This action has expired or was already completed. Refresh the page to see the latest status.",
        422: "Some support details are invalid. Review the form and try again.",
        0: "The VaaniShield API could not be reached. No success was recorded.",
    }
    st.error(messages.get(exc.status_code, f"The {operation} request could not be completed."))


def _render_create_form(st, api, state):
    st.markdown("### Create a support ticket")
    st.caption("Submitting this form creates a synthetic demo support ticket.")

    review_data = state.get("support_ticket_review_data")
    created_ticket = state.get("support_ticket_created_success")

    if created_ticket:
        with st.container(border=True):
            st.success(f"🎉 Support Ticket Created Successfully!")
            st.markdown(f"**Ticket Reference:** `{created_ticket.get('ticket_ref')}`")
            st.markdown(f"**Subject:** {created_ticket.get('subject')}")
            st.markdown(f"**Category:** {created_ticket.get('category', '').title()} · **Priority:** {created_ticket.get('priority')}")
            st.caption("Synthetic demo case. Recorded in your authenticated self-service account.")
            c1, c2 = st.columns(2, vertical_alignment="center")
            if c1.button("📋 View in My Support Tickets", key="view_created_ticket_btn", type="primary"):
                state["support_selected_ticket_ref"] = created_ticket.get("ticket_ref")
                state["support_ticket_created_success"] = None
                from frontend.navigation import request_navigation
                request_navigation(state, "Customer Support", "Track Complaints")
                st.rerun()
            if c2.button("➕ Create Another Ticket", key="create_another_ticket_btn"):
                state["support_ticket_created_success"] = None
                st.rerun()
        return

    if review_data:
        with st.container(border=True):
            st.markdown("#### 🔍 Review Support Ticket Details")
            st.info("Please review the ticket parameters before final submission.")
            st.write(f"**Category:** {review_data['category'].title()}")
            st.write(f"**Subject:** {review_data['subject']}")
            st.write(f"**Priority:** {review_data['priority']}")
            st.write(f"**Description:** {review_data['description'] or 'No additional description provided.'}")
            st.caption("Illustrative synthetic demo data. Submitting creates a demonstration case in your profile.")

            btn_col1, btn_col2 = st.columns(2, vertical_alignment="center")
            if btn_col1.button("Confirm & Submit Ticket", key="confirm_support_ticket_btn", type="primary"):
                try:
                    with st.spinner("Submitting support ticket…"):
                        ticket = submit_support_ticket(
                            api,
                            category=review_data["category"],
                            subject=review_data["subject"],
                            priority=review_data["priority"],
                            description=review_data["description"],
                        )
                    state["support_ticket_review_data"] = None
                    state["support_ticket_created_success"] = ticket
                    state["support_selected_ticket_ref"] = ticket["ticket_ref"]
                    state["support_ticket_filter_key"] = None
                    st.rerun()
                except ValueError as exc:
                    st.warning(str(exc))
                except APIError as exc:
                    _handle_api_error(st, state, exc, operation="ticket creation")

            if btn_col2.button("Edit / Cancel", key="cancel_support_ticket_btn"):
                state["support_ticket_review_data"] = None
                st.rerun()
        return

    with st.form("support-ticket-create-form", clear_on_submit=False):
        category_label = st.selectbox("Category", list(CATEGORIES))
        subject = st.text_input("Subject", max_chars=160, placeholder="Brief summary of the issue (e.g. UPI payment debited but not received)")
        priority = st.selectbox("Priority", list(PRIORITIES), index=1)
        description = st.text_area("Description (optional)", max_chars=4000, placeholder="Detailed context including transaction reference, amount, or merchant details…")
        reviewed = st.form_submit_button("Review Ticket Details", type="primary")

    if reviewed:
        if not subject or len(subject.strip()) < 3:
            st.warning("Please enter a descriptive subject (at least 3 characters).")
        else:
            state["support_ticket_review_data"] = {
                "category": category_label,
                "subject": subject.strip(),
                "priority": priority,
                "description": description.strip() if description else "",
            }
            st.rerun()


def _render_ticket_detail(st, api, state, ticket_ref):
    try:
        with st.spinner("Loading ticket details…"):
            ticket = api.get_support_ticket(ticket_ref)
            events = api.get_support_ticket_events(ticket_ref)
    except APIError as exc:
        _handle_api_error(st, state, exc, operation="ticket details")
        return

    with st.container(border=True):
        st.markdown(f"### {ticket['subject']}")
        st.caption(f"{ticket['ticket_ref']} · Synthetic demo ticket")
        left, middle, right = st.columns(3)
        left.metric("Status", ticket["status"].replace("_", " "))
        middle.metric("Priority", ticket["priority"])
        right.metric("Category", ticket["category"].replace("_", " ").title())
        st.write(ticket.get("description") or "No description provided.")
        st.caption(f"Created: {ticket.get('created_at', '—')} · Updated: {ticket.get('updated_at', '—')}")
        if ticket.get("is_synthetic_demo"):
            st.info("Synthetic VaaniShield demo case. This is not a real bank support ticket.")
        st.markdown("#### Status history")
        if events:
            for event in events:
                old = event.get("from_status") or "Created"
                st.write(
                    f"{event.get('occurred_at', '')} · {old.replace('_', ' ')} → "
                    f"{event.get('to_status', '').replace('_', ' ')}"
                )
        else:
            st.caption("No status events are available.")

        if ticket["status"] not in {"CLOSED", "RESOLVED"}:
            with st.form(f"close-support-ticket-{ticket_ref}"):
                confirmed = st.checkbox(
                    f"I confirm that I want to close ticket {ticket_ref}.",
                    key=f"close-confirm-{ticket_ref}",
                )
                close_requested = st.form_submit_button("Confirm ticket closure")
            if close_requested:
                try:
                    with st.spinner("Submitting ticket closure…"):
                        close_ticket_after_confirmation(api, ticket_ref, confirmed)
                    state["support_ticket_filter_key"] = None
                    st.success(f"Ticket {ticket_ref} is now closed.")
                    st.rerun()
                except ValueError as exc:
                    st.warning(str(exc))
                except APIError as exc:
                    _handle_api_error(st, state, exc, operation="ticket closure")


def render(st, subfeature="Ask Support Assistant", api=None, state=None):
    st.title("Customer Support")
    st.caption("Authenticated self-service for synthetic VaaniShield demo tickets.")
    if api is None or state is None:
        st.info("Sign in to view and manage your demo support tickets.")
        return
    current_customer_key = (state.get("customer") or {}).get("customer_key")
    if state.get("support_ticket_cache_customer_key") != current_customer_key:
        clear_support_ticket_cache(state)
        state["support_ticket_cache_customer_key"] = current_customer_key

    if subfeature == "Ask Support Assistant":
        st.markdown("### Support overview")
        st.write("Ask Vaani AI to explain support options, find your tickets, or prepare a ticket proposal.")
        st.info("Vaani AI proposals are not submitted until you explicitly confirm them in the conversation.")
        if st.button("Open Vaani AI", key="support-open-assistant", type="primary"):
            state["assistant_open"] = True
            st.rerun()
    elif subfeature == "Create Support Ticket":
        _render_create_form(st, api, state)
    elif subfeature == "Resolution Assistance":
        st.markdown("### Resolution assistance")
        st.info("VaaniShield can show ticket history and prepare informational suggestions. It cannot mark a real bank issue resolved.")

    st.divider()
    st.markdown("### My support tickets")
    state.setdefault("support_ticket_filters", {"status": "All statuses", "category": "All categories"})
    with st.container(border=True):
        filter_left, filter_middle, filter_right = st.columns(3, vertical_alignment="center")
        with filter_left:
            status_label = st.selectbox(
                "Filter by status", ["All statuses", *STATUSES],
                key="support-status-filter",
            )
        with filter_right:
            category_label = st.selectbox(
                "Filter by category", ["All categories", *CATEGORIES],
                key="support-category-filter",
            )
        with filter_middle:
            priority_label = st.selectbox(
                "Filter by priority", ["All priorities", *PRIORITIES],
                key="support-priority-filter",
            )
    state["support_ticket_filters"] = {
        "status": status_label, "category": category_label, "priority": priority_label,
    }
    filters = (
        None if status_label == "All statuses" else status_label,
        None if category_label == "All categories" else CATEGORIES[category_label],
        None if priority_label == "All priorities" else priority_label,
    )
    if state.get("support_ticket_filter_key") != filters:
        state["support_ticket_filter_key"] = filters
        state["support_ticket_items"] = None
        state["support_ticket_cursor"] = None
        state["support_ticket_has_more"] = False
    try:
        if state.get("support_ticket_items") is None:
            with st.spinner("Loading your support tickets…"):
                page = api.list_support_tickets(
                    limit=25, status=filters[0], category=filters[1], priority=filters[2]
                )
            state["support_ticket_items"] = page.get("items", [])
            state["support_ticket_cursor"] = page.get("next_cursor")
            state["support_ticket_has_more"] = bool(page.get("has_more"))
            state["support_ticket_cache_customer_key"] = current_customer_key
    except APIError as exc:
        _handle_api_error(st, state, exc, operation="ticket list")
        return

    tickets = state.get("support_ticket_items") or []
    if not tickets:
        st.info("No support tickets match these filters.")
        return
    st.caption(f"Showing {len(tickets)} synthetic ticket(s).")
    refs = [item["ticket_ref"] for item in tickets]
    current = state.get("support_selected_ticket_ref")
    selected = st.selectbox(
        "Select a ticket", refs,
        index=refs.index(current) if current in refs else 0,
        format_func=lambda ref: next(
            (f"{ref} · {item.get('subject', '')} · {item.get('status', '')}"
             for item in tickets if item["ticket_ref"] == ref), ref
        ),
        key="support-ticket-selector",
    )
    state["support_selected_ticket_ref"] = selected
    _render_ticket_detail(st, api, state, selected)
    if state.get("support_ticket_has_more") and st.button(
        "Load more tickets", key="support-load-more-tickets"
    ):
        try:
            with st.spinner("Loading more tickets…"):
                page = api.list_support_tickets(
                    limit=25,
                    cursor=state.get("support_ticket_cursor"),
                    status=filters[0], category=filters[1], priority=filters[2],
                )
            state["support_ticket_items"].extend(page.get("items", []))
            state["support_ticket_cursor"] = page.get("next_cursor")
            state["support_ticket_has_more"] = bool(page.get("has_more"))
            st.rerun()
        except APIError as exc:
            _handle_api_error(st, state, exc, operation="ticket list")
