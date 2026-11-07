"""Helpers for the existing synthetic banking dashboard."""


def load_dashboard_context(api):
    """Read the authenticated self-service dashboard context."""
    return api.get_my_context()


def render_demo_scope_notice(st, feature="Banking"):
    st.info(
        f"{feature} uses authenticated synthetic demo dashboard APIs. "
        "This data is separate from the authenticated assistant conversation context."
    )

