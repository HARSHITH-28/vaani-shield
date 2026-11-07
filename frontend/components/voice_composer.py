"""Voice and text input controls for the assistant page."""

from __future__ import annotations


def render_voice_input(st, epoch: int, *, key_prefix="assistant"):
    """Return a newly captured WAV payload, or None until the user records."""
    return st.audio_input(
        "Ask by voice",
        sample_rate=16000,
        key=f"{key_prefix}-audio-{epoch}",
        help="Record a short question in Kannada, English, Hindi, or a mix.",
    )


def render_language_controls(st, state):
    col_lang1, col_lang2 = st.columns(2)
    with col_lang1:
        st.selectbox(
            "Spoken language",
            ["Automatic", "Kannada", "English", "Hindi"],
            index={"auto": 0, "kn-IN": 1, "en-IN": 2, "hi-IN": 3}.get(state.get("language_hint"), 0),
            key="language_display",
            help="Automatic detection supports code-mixed speech where the provider can identify it.",
        )
        state["language_hint"] = {
            "Automatic": "auto", "Kannada": "kn-IN", "English": "en-IN", "Hindi": "hi-IN"
        }[state["language_display"]]

    with col_lang2:
        from frontend.session_state import (
            RESPONSE_LANGUAGE_OPTIONS_FULL,
            on_composer_language_change,
        )
        st.selectbox(
            "Response language",
            RESPONSE_LANGUAGE_OPTIONS_FULL,
            key="response_language_display",
            on_change=on_composer_language_change,
            help="Language in which Vaani AI will compose answers and generate voice output.",
        )

