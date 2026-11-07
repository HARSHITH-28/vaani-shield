"""Feature and subfeature navigation state for the Streamlit shell."""

FEATURE_SUBFEATURES = {
    "Banking Information": (
        "Overview", "Accounts", "Transactions", "Cards", "Loans",
    ),
    "Financial Intelligence": (
        "Spending Analysis", "Category Analysis", "Merchant Analysis",
        "Monthly Comparison", "Budgets & Recommendations",
    ),
    "Scam Detection": (
        "Message Analysis", "Call Analysis", "Transaction Risk", "Investigations",
    ),
    "Customer Support": (
        "Ask Support Assistant", "Create Support Ticket", "Track Complaints",
        "Resolution Assistance",
    ),
    "Account Opening Assistant": (
        "Open New Account", "KYC Documents", "Review & Submit", "Track Applications",
    ),
}

DEFAULT_SUBFEATURE = {feature: entries[0] for feature, entries in FEATURE_SUBFEATURES.items()}


def normalize_navigation(feature: str, subfeature: str | None = None) -> tuple[str, str | None]:
    """Support smooth normalization between legacy and dedicated Account Opening feature names."""
    if feature in ("Account Opening", "Account Opening Assistant"):
        return "Account Opening Assistant", subfeature
    if feature == "Banking Information" and subfeature in (
        "Open New Account", "KYC Documents", "Review & Submit", "Track Applications"
    ):
        return "Account Opening Assistant", subfeature
    return feature, subfeature


def subfeature_state_key(feature: str) -> str:
    norm_feature, _ = normalize_navigation(feature)
    return "subfeature_" + "_".join(norm_feature.lower().split())


def ensure_subfeature(state, feature: str) -> str:
    norm_feature, _ = normalize_navigation(feature)
    options = FEATURE_SUBFEATURES[norm_feature]
    key = subfeature_state_key(norm_feature)
    if state.get(key) not in options:
        state[key] = DEFAULT_SUBFEATURE[norm_feature]
    return state[key]


def set_subfeature(state, feature: str, subfeature: str) -> None:
    norm_feature, norm_subfeature = normalize_navigation(feature, subfeature)
    if norm_feature not in FEATURE_SUBFEATURES or norm_subfeature not in FEATURE_SUBFEATURES[norm_feature]:
        raise ValueError("Unknown feature or subfeature.")
    state[subfeature_state_key(norm_feature)] = norm_subfeature


def request_navigation(
    state,
    feature: str,
    subfeature: str | None = None,
    *,
    step: str | None = None,
) -> None:
    """Stage feature/subfeature navigation safely without mutating instantiated widget keys."""
    norm_feature, norm_subfeature = normalize_navigation(feature, subfeature)
    if norm_feature not in FEATURE_SUBFEATURES:
        raise ValueError("Unknown feature or subfeature.")
    resolved = DEFAULT_SUBFEATURES_LOOKUP(norm_feature) if norm_subfeature is None else norm_subfeature
    if resolved not in FEATURE_SUBFEATURES[norm_feature]:
        raise ValueError("Unknown feature or subfeature.")
    state["pending_navigation"] = {
        "feature": norm_feature,
        "subfeature": resolved,
        "step": step,
    }
    if step is not None:
        state["account_application_step"] = step


def DEFAULT_SUBFEATURES_LOOKUP(feature: str) -> str:
    norm_feature, _ = normalize_navigation(feature)
    return DEFAULT_SUBFEATURE[norm_feature]


def apply_pending_navigation(state) -> bool:
    """Apply staged navigation before sidebar radio widgets are instantiated."""
    pending = state.get("pending_navigation")
    if not isinstance(pending, dict):
        return False
    state["pending_navigation"] = None
    feature = pending.get("feature")
    subfeature = pending.get("subfeature")
    norm_feature, norm_subfeature = normalize_navigation(feature, subfeature)
    if norm_feature not in FEATURE_SUBFEATURES:
        return False
    resolved = (
        norm_subfeature
        if norm_subfeature in FEATURE_SUBFEATURES[norm_feature]
        else DEFAULT_SUBFEATURE[norm_feature]
    )
    current_feature = state.get("main_feature")
    current_subfeature = state.get(subfeature_state_key(current_feature)) if current_feature else None
    if current_feature and (current_feature != norm_feature or current_subfeature != resolved):
        history = state.setdefault("nav_history", [])
        if not history or history[-1] != (current_feature, current_subfeature):
            history.append((current_feature, current_subfeature))
            if len(history) > 30:
                history.pop(0)

    state["main_feature"] = norm_feature
    set_subfeature(state, norm_feature, resolved)
    if pending.get("step") is not None:
        state["account_application_step"] = pending["step"]
    return True


def get_previous_navigation(state) -> tuple[str, str]:
    """Return the most recent distinct previous navigation destination."""
    history = state.get("nav_history") or []
    current_feature = state.get("main_feature")
    current_subfeature = state.get(subfeature_state_key(current_feature)) if current_feature else None
    for feat, sub in reversed(history):
        if feat != current_feature or sub != current_subfeature:
            if feat != "Account Opening Assistant":
                return (feat, sub)
    if history:
        for feat, sub in reversed(history):
            if feat != current_feature or sub != current_subfeature:
                return (feat, sub)
    return ("Banking Information", "Accounts")


def navigate_back(state) -> tuple[str, str]:
    """Safely stage navigation to previous screen without clearing draft data or cache."""
    history = state.get("nav_history") or []
    current_feature = state.get("main_feature")
    current_subfeature = state.get(subfeature_state_key(current_feature)) if current_feature else None
    target = None
    while history:
        feat, sub = history.pop()
        if feat != current_feature or sub != current_subfeature:
            target = (feat, sub)
            break
    if target is None:
        target = ("Banking Information", "Accounts")
    request_navigation(state, target[0], target[1])
    return target


def page_key(feature: str, subfeature: str) -> str:
    norm_feature, norm_subfeature = normalize_navigation(feature, subfeature)
    if norm_feature in {"Banking Information", "Financial Intelligence", "Account Opening Assistant"}:
        return norm_subfeature
    return norm_feature
