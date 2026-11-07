"""Persistent floating Vaani AI panel shared across feature pages."""

from datetime import datetime

from frontend.api_client import APIError
from frontend.components.conversation_history import create_new_conversation, select_conversation
from frontend.pages.assistant import render as render_assistant
from frontend.session_state import sign_out


def toggle_assistant_panel(state):
    state["assistant_open"] = not bool(state.get("assistant_open"))


def _conversation_label(item):
    title = str(item.get("title") or "").strip()
    if title:
        return title
    try:
        updated = datetime.fromisoformat(str(item.get("updated_at", "")).replace("Z", "+00:00"))
        return f"Conversation · {updated.strftime('%b %d, %Y')}"
    except (TypeError, ValueError):
        return "New conversation"


def render_floating_assistant(st, api, state, *, in_dock: bool = False):
    is_open = bool(state.get("assistant_open"))

    if not in_dock:
        if not is_open:
            with st.container(key="floating-assistant-launch"):
                if st.button(
                    "✨ Ask Vaani AI",
                    key="floating-assistant-toggle",
                    type="primary",
                    width="stretch",
                ):
                    toggle_assistant_panel(state)
                    st.rerun()
            return
        panel_key = "floating-assistant-panel"
    else:
        panel_key = "floating-assistant-dock"

    with st.container(key=panel_key, border=True):
        with st.container(key="floating-assistant-header"):
            heading, lang_col, action = st.columns([3.0, 1.4, 0.4])
            with heading:
                st.markdown(
                    """
                    <div class="chatgpt-header-brand">
                      <div class="chatgpt-avatar-icon">🛡️</div>
                      <div class="chatgpt-title-box">
                        <span class="chatgpt-header-title">Vaani AI</span>
                        <span class="chatgpt-online-status"><span class="chatgpt-status-dot"></span>Online</span>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with lang_col:
                from frontend.session_state import (
                    RESPONSE_LANGUAGE_OPTIONS_SHORT,
                    on_dock_language_change,
                )
                st.selectbox(
                    "Language",
                    RESPONSE_LANGUAGE_OPTIONS_SHORT,
                    key="dock_copilot_language_selector",
                    on_change=on_dock_language_change,
                    label_visibility="collapsed",
                    help="Response language",
                )
            with action:
                if st.button("✕", key="floating-assistant-close", help="Close assistant"):
                    state["assistant_open"] = False
                    st.rerun()

        try:
            page = api.list_conversations(limit=25)
            conversations = page.get("items", [])
            state["assistant_conversations"] = conversations
        except APIError as exc:
            conversations = state.get("assistant_conversations", [])
            if exc.status_code == 401:
                sign_out(state)
                st.rerun()
            st.caption("Conversation list is temporarily unavailable.")

        if conversations:
            history_expanded = bool(state.get("expand_history_for_demo", False))
            with st.expander("💬 Conversation history", expanded=history_expanded):
                active_id = str(state.get("conversation_id") or "")
                with st.container(height=175, border=False):
                    for item in conversations:
                        conversation_id = str(item["conversation_id"])
                        label = _conversation_label(item)
                        is_active = (conversation_id == active_id)
                        btn_label = f"●  {label}" if is_active else label
                        if st.button(
                            btn_label,
                            key=f"floating-history-{conversation_id}",
                            width="stretch",
                            type="primary" if is_active else "secondary",
                            help="Open conversation",
                        ) and not is_active:
                            select_conversation(state, conversation_id)
                            state["floating-assistant-conversation"] = conversation_id
                            st.rerun()

        if state.get("wake_triggered_greeting") or state.get("wake_activated"):
            st.success("🎙️ **Vaani AI Ready:** Hello! I'm Vaani. How can I help you today?")
            state["wake_triggered_greeting"] = False
            state["wake_activated"] = False

        render_assistant(st, api, state, compact=True)

