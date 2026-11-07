"""Sidebar conversation list and server-owned conversation creation."""

from datetime import datetime

from frontend.session_state import reset_conversation_state


def refresh_conversation_list(api, state, append=False):
    cursor = state.get("conversation_list_cursor") if append else None
    page = api.list_conversations(limit=25, cursor=cursor)
    items = page.get("items", [])
    state["conversation_list"] = (
        state.get("conversation_list", []) + items if append else items
    )
    state["conversation_list_cursor"] = page.get("next_cursor")
    state["conversation_list_has_more"] = bool(page.get("has_more"))


def create_new_conversation(api, state):
    conversation = api.create_conversation()
    state["conversation_id"] = conversation["conversation_id"]
    reset_conversation_state(state)
    state["conversation_id"] = conversation["conversation_id"]
    state["loaded_conversation_id"] = conversation["conversation_id"]
    state["last_audio_hash"] = None
    return conversation


def select_conversation(state, conversation_id):
    if str(state.get("conversation_id")) == str(conversation_id):
        return
    reset_conversation_state(state)
    state["conversation_id"] = str(conversation_id)


def _history_label(conversation):
    title = str(conversation.get("title") or "").strip()
    if title:
        return title
    try:
        updated = datetime.fromisoformat(str(conversation.get("updated_at", "")).replace("Z", "+00:00"))
        return f"Conversation · {updated.strftime('%b %d, %Y')}"
    except (TypeError, ValueError):
        return "New conversation"


def render_sidebar_history(st, api, state):
    st.markdown("<div class='sidebar-section'>Conversations</div>", unsafe_allow_html=True)
    if st.button("+  New conversation", width="stretch", key="new-conversation"):
        create_new_conversation(api, state)
        refresh_conversation_list(api, state)
        st.rerun()

    if not state.get("conversation_list"):
        refresh_conversation_list(api, state)

    for conversation in state.get("conversation_list", []):
        conversation_id = str(conversation["conversation_id"])
        label = _history_label(conversation)
        if conversation_id == str(state.get("conversation_id")):
            label = f"• {label}"
        if st.button(
            label,
            key=f"conversation-{conversation_id}",
            width="stretch",
        ):
            select_conversation(state, conversation_id)
            st.rerun()

    if state.get("conversation_list_has_more") and st.button(
        "Load older conversations", key="older-conversations", width="stretch"
    ):
        refresh_conversation_list(api, state, append=True)
        st.rerun()
