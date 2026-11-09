"""Persistent floating Vaani AI panel shared across feature pages."""

from datetime import datetime

from frontend.api_client import APIError
from frontend.components.conversation_history import create_new_conversation, select_conversation
from frontend.pages.assistant import render as render_assistant
from frontend.session_state import sign_out


def toggle_assistant_panel(state):
    state["assistant_open"] = not bool(state.get("assistant_open"))


def assistant_dock_is_visible(state):
    """Keep the desktop dock visible until the user explicitly closes it."""
    return bool(state.get("assistant_open")) or not bool(state.get("assistant_dock_closed"))


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
    is_dock_visible = assistant_dock_is_visible(state)

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

    if is_open:
        responsive_key = "assistant-responsive-open"
    elif state.get("assistant_dock_closed"):
        responsive_key = "assistant-responsive-closed"
    else:
        responsive_key = "assistant-desktop-dock-open"

    with st.container(key=responsive_key):
        with st.container(key=panel_key, border=True):
            with st.container(key="floating-assistant-header"):
                heading, action = st.columns([4.1, 0.65], vertical_alignment="center")
                with heading:
                    st.markdown(
                        """
                        <div class="chatgpt-header-brand">
                          <div class="chatgpt-avatar-icon">🛡️</div>
                          <div class="chatgpt-title-box">
                            <span class="chatgpt-header-title">Vaani AI</span>
                        <span class="chatgpt-header-subtitle">Intelligent Banking Companion</span>
                            <span class="chatgpt-online-status"><span class="chatgpt-status-dot"></span>Ready</span>
                          </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                with action:
                    if st.button("✕", key="floating-assistant-close"):
                        state["assistant_open"] = False
                        state["assistant_dock_closed"] = True
                        st.rerun()

            language_col, mode_col = st.columns(2, gap="medium")
            with language_col:
                from frontend.session_state import (
                    RESPONSE_LANGUAGE_OPTIONS_SHORT,
                    on_dock_language_change,
                )
                st.selectbox(
                    "Language",
                    RESPONSE_LANGUAGE_OPTIONS_SHORT,
                    key="dock_copilot_language_selector",
                    on_change=on_dock_language_change,
                    label_visibility="visible",
                )
            with mode_col:
                from frontend.session_state import on_dock_response_mode_change
                st.selectbox(
                    "Response Mode",
                    ["Speech", "Text", "Speech + Text"],
                    key="dock_response_mode_selector",
                    on_change=on_dock_response_mode_change,
                    label_visibility="visible",
                )

            st.markdown(
                "<div class='vaani-waveform' aria-hidden='true'>" + "".join("<i></i>" for _ in range(25)) + "</div>",
                unsafe_allow_html=True,
            )

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

