"""Provider-independent orchestration for a single VaaniShield assistant turn.

Intent routing here is intentionally a small temporary ruleset, not an LLM or
Evon integration. Feature handlers are registered independently so future
scam/support implementations can be added without changing the pipeline.
"""

from datetime import datetime, timezone
import json
import logging
import math
import os
import re
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from backend.api_models import (
    AssistantTurnRequest,
    AssistantTurnResponse,
    DetectedLanguage,
    PendingAction,
    ScamAssessmentDetail,
    SupportTicketCloseConfirmation,
    SupportTicketCreateAction,
    TransactionRiskAssessmentDetail,
    ToolExecutionResult,
)
from services import conversation_service
from services.account_application_assistant import (
    classify_account_intent,
    run_account_application_turn,
)
from services.agent_tools import ACCOUNT_APPLICATION_TOOL_NAMES, execute_tool
from services.agent_tools import get_available_tools
from services.banking_service import get_customer_profile_by_key
from services.conversation_service import ConversationNotFoundError
from services.database import DEMO_CUSTOMER_KEYS, get_connection
from services.principal import CustomerPrincipal
from services.speech_adapters import (
    create_speech_synthesizer,
    create_speech_transcriber,
)
from services.speech_interfaces import (
    AssistantServiceError,
    InputProcessingUnavailable,
    OutputProcessingUnavailable,
    SpeechSynthesizer,
    SpeechTranscriber,
    Transcription,
)
from services.llm_interfaces import (
    LLMAuthenticationError, LLMConnectionError, LLMMessage, LLMProvider,
    LLMProviderError, LLMRateLimited, LLMRequestTooLarge, LLMTimeout, LLMTool, LLMToolCall,
)
from services.llm_registry import create_llm_provider, get_provider_settings
from services.customer_support_service import CATEGORIES as SUPPORT_CATEGORIES
from services.customer_support_service import PRIORITIES as SUPPORT_PRIORITIES
from services.customer_support_service import STATUSES as SUPPORT_STATUSES

logger = logging.getLogger(__name__)


def _context_budget_tokens():
    try:
        value = int(os.getenv("VAANISHIELD_LLM_CONTEXT_TOKEN_BUDGET", "10000"))
    except (TypeError, ValueError):
        value = 10000
    return min(30000, max(2000, value))


def _history_turn_limit():
    try:
        value = int(os.getenv("VAANISHIELD_LLM_HISTORY_TURNS", "12"))
    except (TypeError, ValueError):
        value = 12
    return min(30, max(2, value))


def _orchestration_round_limit(tool_count):
    maximum = len(LLM_TOOL_NAMES) + 1
    try:
        configured = int(os.getenv("VAANISHIELD_LLM_MAX_ORCHESTRATION_ROUNDS", str(maximum)))
    except (TypeError, ValueError):
        configured = maximum
    configured = min(maximum, max(2, configured))
    return min(configured, max(2, tool_count + 1))


def _estimate_messages_tokens(messages):
    return math.ceil(sum(len(message.content or "") for message in messages) / 4)


def _compact_tool_message(message, max_chars=5000):
    """Keep valid JSON tool context while making oversized row sets explicit."""
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        payload = None
    if payload is None:
        excerpt = (message.content or "")[:max_chars]
        return LLMMessage(
            role=message.role,
            content=json.dumps({
                "partial_result": True,
                "note": "Tool output was condensed for request size; do not claim complete results.",
                "excerpt": excerpt,
            }, ensure_ascii=False),
            tool_call_id=message.tool_call_id, tool_name=message.tool_name,
        )

    def trim_rows(value):
        if isinstance(value, list):
            rows = list(value)
            return rows[:8], len(rows)
        return value, None

    partial = False
    total = None
    if isinstance(payload, list):
        payload, total = trim_rows(payload)
        partial = total is not None and total > len(payload)
        payload = {"items": payload}
    elif isinstance(payload, dict):
        for key, value in list(payload.items()):
            if isinstance(value, list) and len(value) > 8:
                payload[key], total = trim_rows(value)
                partial = True
                break
    if partial:
        payload["partial_result"] = True
        payload["included_rows"] = 8
        payload["available_rows"] = total
        payload["context_note"] = "Only a subset of rows is included; do not describe this as complete."
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(content) > max_chars:
        payload = {
            "partial_result": True,
            "context_note": "Tool output was condensed for request size; do not claim complete results.",
            "excerpt": content[:max_chars],
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return LLMMessage(
        role=message.role, content=content, tool_call_id=message.tool_call_id,
        tool_name=message.tool_name,
    )


def _compact_llm_messages(messages, *, max_tokens):
    """Condense old text history and bound tool outputs without touching storage.

    Current user input and protocol-required tool-call/result messages are kept.
    Historical records remain intact and available from the conversation API.
    """
    messages = list(messages)
    if _estimate_messages_tokens(messages) <= max_tokens:
        return messages, False
    historical = [item for item in messages if item.role in {"user", "assistant"} and not item.tool_calls]
    keep_ids = {id(item) for item in historical[-4:]}
    compacted = []
    prior = []
    for item in messages:
        if item.role == "tool":
            compacted.append(_compact_tool_message(item, max_chars=2800))
            continue
        if item.tool_calls or item.role not in {"user", "assistant"}:
            compacted.append(item)
            continue
        if id(item) in keep_ids:
            compacted.append(item)
            continue
        text = re.sub(r"\s+", " ", item.content or "").strip()
        if not text:
            continue
        # Preserve each older user's intent and a short assistant anchor. The
        # marker tells the model that the earlier exchange is a compact summary.
        excerpt = text[:220]
        prior.append(f"{item.role}: {excerpt}" + ("…" if len(text) > len(excerpt) else ""))
    if prior:
        # Insert the explicitly marked summary before the recent exact messages.
        summary = LLMMessage(
            role="user",
            content="[Earlier conversation context is condensed; full history remains available. "
                    "User intents and short response anchors follow.] " + " | ".join(prior),
        )
        first_recent = next((i for i, item in enumerate(compacted) if id(item) in keep_ids), len(compacted))
        compacted.insert(first_recent, summary)
    if _estimate_messages_tokens(compacted) > max_tokens:
        latest_user = next((item for item in reversed(compacted) if item.role == "user"), None)
        bounded = []
        for item in compacted:
            if item is latest_user or item.tool_calls or item.role == "tool":
                bounded.append(item)
            elif len(item.content or "") > 1200:
                bounded.append(LLMMessage(
                    role=item.role,
                    content=(item.content or "")[:1200] + " … [older message condensed]",
                ))
            else:
                bounded.append(item)
        compacted = bounded
    return compacted, True


def _trace_event(correlation_id, event: str, **fields):
    """Emit allowlisted metadata only; never pass request/result contents here."""
    payload = {"event": event, "correlation_id": str(correlation_id)}
    for key in ("provider", "model", "step", "tool", "passed", "status", "error_type", "required_count", "has_text", "tool_call_count", "result_type", "credentials_configured", "response_length", "guarded", "context_compacted", "message_count"):
        if key in fields:
            value = fields[key]
            if isinstance(value, str):
                value = value[:100]
            payload[key] = value
    logger.info("assistant_execution_trace %s", json.dumps(payload, sort_keys=True))


_SCAM_INTENT_TERMS = (
    "is this a scam", "is this scam", "is it a scam", "could this be a scam",
    "scam message", "scam call", "scam conversation", "fraud message",
    "suspicious message", "suspicious call", "is this fake",
    "check this message", "assess this message", "analyze this message",
    "analyse this message", "assess this conversation", "analyze this conversation",
    "analyse this conversation", "review this conversation", "check this conversation",
    "is this suspicious",
    "share your otp", "send your otp", "give me your otp", "asked for my otp", "asking for my otp",
    "asked for otp", "asking for otp", "requested my otp", "upi pin", "asked for my upi pin",
    "asked for my pin", "asked for my cvv", "claiming to be from my bank", "claiming to be from the bank",
    "claimed to be from my bank", "sent me a link", "reverse a suspicious", "reverse a transaction",
    "install anydesk", "install remote access", "account will be blocked",
    "account block", "otp bhejo", "otp batao", "otp do", "otp manga",
    "otp \u0c95\u0cb3\u0cc1\u0cb9\u0cbf\u0cb8\u0cbf", "otp \u0c95\u0cc7\u0cb3\u0cbf\u0ca6\u0cb0\u0cc1",
    "kyc update", "claim your prize", "lottery winner",
    "\u0c88 \u0cb8\u0c82\u0ca6\u0cc7\u0cb6 \u0caa\u0cb0\u0cbf\u0cb6\u0cc0\u0cb2\u0cbf\u0cb8\u0cbf",
    "\u0cb8\u0c82\u0ca6\u0cc7\u0cb6 \u0caa\u0cb0\u0cbf\u0cb6\u0cc0\u0cb2\u0cbf\u0cb8\u0cbf",
    "\u0915\u094d\u092f\u093e \u092f\u0939 \u0938\u094d\u0915\u0948\u092e",
    "\u0915\u094d\u092f\u093e \u092f\u0939 \u092b\u094d\u0930\u0949\u0921",
    "\u092f\u0939 \u0938\u094d\u0915\u0948\u092e \u0939\u0948",
    "\u0c87\u0ca6\u0cc1 scam", "\u0cb8\u0ccd\u0caf\u0cbe\u0cae\u0ccd \u0c86\u0c97\u0cbf\u0ca6\u0cc7\u0caf\u0cc7",
)
_SCAM_REQUEST_ONLY = re.compile(
    r"^\s*(?:(?:hi|hello|please|can you|could you|would you)\s+)*(?:"
    r"is this (?:a )?(?:scam|fake|fraud)(?: message| call)?|"
    r"(?:check|assess|analyze|analyse) (?:this|it)(?: message| call)?(?: for a scam)?|"
    r"(?:assess|analyze|analyse|review|check) this conversation|"
    r"is this message suspicious|"
    r"(?:\u0c87\u0ca6\u0cc1|\u0c88 \u0cb8\u0c82\u0ca6\u0cc7\u0cb6)\s*(?:scam|\u0cb8\u0cc8\u0cac\u0cb0\u0ccd|\u0cb8\u0ccd\u0caf\u0cbe\u0cae\u0ccd)(?:\u0ca8\u0cbe| \u0c86\u0c97\u0cbf\u0ca6\u0cc7\u0caf\u0cc7)?|"
    r"(?:\u0915\u094d\u092f\u093e )?(?:\u092f\u0939|\u092f\u0947) (?:\u090f\u0915 )?(?:\u0938\u094d\u0915\u0948\u092e|\u092b\u094d\u0930\u0949\u0921|\u0927\u094b\u0916\u093e\u0927\u0921\u093c\u0940) \u0939\u0948)[?!.\s]*$",
    re.IGNORECASE,
)

_SUPPORT_CREATE_TERMS = (
    "raise a complaint", "file a complaint", "lodge a complaint", "make a complaint",
    "create a support ticket", "open a support ticket", "raise a support ticket",
    "prepare a customer support ticket", "prepare a support ticket",
    "create a ticket", "raise a ticket", "submit a complaint",
    "register a complaint", "register complaint", "register a ticket", "register ticket",
    "report an issue with my account", "report an issue",
    "report a problem with my banking", "report a problem with my account", "report a problem",
    "complaint दर्ज", "शिकायत दर्ज", "शिकायत करना", "टिकट बनाएं", "ticket बनाओ",
    "ದೂರು ದಾಖಲಿಸು", "ದೂರು ದಾಖಲಿಸಿ", "ದೂರು ನೀಡು", "ದೂರು ನೀಡಿ", "ದೂರು ಕೊಡಿ", "ಟಿಕೆಟ್ ತೆರೆಯಿರಿ", "ಟಿಕೆಟ್ ಮಾಡಿ", "complaint ಮಾಡಿ",
    "upi payment", "upi transaction", "transaction fail", "merchant hasn't received", "merchant has not received", "merchant not received",
    "payment debited", "payment deducted", "amount deducted", "amount debited",
    "money debited", "money deducted", "failed payment", "payment failed",
    "debited but not received", "deducted but not received",
    "help with a upi payment", "help with upi", "help with payment",
    "ಹಣ ಕಡಿತ", "ವ್ಯಾಪಾರಿಗೆ ತಲುಪಿಲ್ಲ", "ಗೆ ತಲುಪಿಲ್ಲ", "ಯುಪಿಐ ಪಾವತಿ", "ಪಾವತಿ ವಿಫಲವಾಗಿದೆ", "ಯುಪಿಐ ಸಮಸ್ಯೆ", "debit ಆಗಿದೆ", "deduct ಆಗಿದೆ", "ಪಾವತಿ ಆಗಿಲ್ಲ",
    "पैसे कट गए", "राशि कट गई", "व्यापारी को नहीं मिले", "मर्चेंट को नहीं मिले", "भुगतान विफल", "यूपीआई भुगतान", "debit हो गया",
)
_SUPPORT_CLOSE_TERMS = (
    "close my ticket", "close the ticket", "close this ticket", "close my complaint",
    "close this complaint", "टिकट बंद", "शिकायत बंद", "ಟಿಕೆಟ್ ಮುಚ್ಚು", "ದೂರು ಮುಚ್ಚು",
)
_SUPPORT_CATEGORY_TERMS = {
    "account": ("account", "\u0916\u093e\u0924\u093e", "\u0916\u093e\u0924\u0947", "\u0c96\u0cbe\u0ca4\u0cc6"),
    "card": ("card", "debit card", "credit card", "atm", "\u0915\u093e\u0930\u094d\u0921", "\u0c95\u0cbe\u0cb0\u0ccd\u0ca1"),
    "transaction": (
        "transaction", "payment", "transfer", "\u0932\u0947\u0928\u0926\u0947\u0928",
        "\u092d\u0941\u0917\u0924\u093e\u0928", "\u0cb5\u0cb9\u0cbf\u0cb5\u0cbe\u0c9f\u0cc1",
        "\u0caa\u0cbe\u0cb5\u0ca4\u0cbf", "upi", "merchant", "debited", "deducted",
    ),
    "digital_banking": (
        "digital banking", "online banking", "mobile banking", "internet banking",
        "netbanking", "upi", "app login", "sign-in", "signin",
        "\u0921\u093f\u091c\u093f\u091f\u0932 \u092c\u0948\u0902\u0915\u093f\u0902\u0917",
        "\u0911\u0928\u0932\u093e\u0907\u0928 \u092c\u0948\u0902\u0915\u093f\u0902\u0917",
        "\u092e\u094b\u092c\u093e\u0907\u0932 \u092c\u0948\u0902\u0915\u093f\u0902\u0917",
        "\u0ca1\u0cbf\u0c9c\u0cbf\u0c9f\u0cb2 \u0cac\u0ccd\u0caf\u0cbe\u0c82\u0c95\u0cbf\u0c82\u0c97",
        "\u0cae\u0cca\u0cac\u0cc8\u0cb2 \u0cac\u0ccd\u0caf\u0cbe\u0c82\u0c95\u0cbf\u0c82\u0c97",
    ),
    "loan": ("loan", "emi", "\u0932\u094b\u0928", "\u090b\u0923", "\u0cb8\u0cbe\u0cb2"),
    "deposit": (
        "deposit", "fixed deposit", "term deposit", "\u091c\u092e\u093e",
        "\u0ca0\u0cc7\u0cb5\u0ca3\u0cbf",
    ),
    "other": ("other", "miscellaneous", "\u0905\u0928\u094d\u092f", "\u0c87\u0ca4\u0cb0\u0cc6"),
}
_SUPPORT_TEXT_STOP_WORDS = frozenset({
    "a", "an", "the", "i", "my", "me", "to", "for", "of", "on", "in",
    "is", "was", "were", "be", "been", "and", "or", "please", "want",
    "need", "help", "support", "ticket", "complaint", "issue", "problem",
    "category", "subject", "priority", "description", "low", "medium", "high",
    "urgent", "urgently", "urgency", "create", "file", "raise", "lodge",
    "submit", "open", "make",
    "upi", "payment", "transaction", "amount", "money", "debited", "deducted",
    "received", "merchant", "customer", "account", "transfer", "failed", "failure",
    "not", "yes", "no", "from", "at", "by", "details", "draft", "synthetic",
    "here", "are", "reported", "stated", "swiggy", "zomato", "amazon",
})


def _support_category_was_stated(text: str, category: str) -> bool:
    terms = _SUPPORT_CATEGORY_TERMS.get(str(category).casefold())
    if not terms:
        return False
    normalized = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalized) for term in terms)


def _support_summary_is_grounded(summary: str, customer_text: str) -> bool:
    """Reject model summaries containing content words absent from the user's turns."""
    summary_words = {
        word for word in re.findall(r"\w+", summary.casefold())
        if len(word) > 2 and word not in _SUPPORT_TEXT_STOP_WORDS
    }
    if not summary_words:
        return True
    customer_words = set(re.findall(r"\w+", customer_text.casefold()))
    return summary_words.issubset(customer_words)


def _support_staff_workflow_question(text: str) -> bool:
    query = text.casefold()
    return any(term in query for term in (
        "who is assigned", "who's assigned", "assigned staff", "assigned agent",
        "staff assignment", "support agent", "escalate", "escalation",
        "escalation status", "bank has resolved", "bank resolved", "real bank resolution",
        "resolution status", "when will the bank", "agent assign", "staff assign",
        "\u0915\u093f\u0938\u0947 \u0905\u0938\u093e\u0907\u0928", "\u0915\u094c\u0928 \u0938\u093e \u090f\u091c\u0947\u0902\u091f",
        "\u0915\u093f\u0938 \u090f\u091c\u0947\u0902\u091f", "\u0915\u093f\u0938 \u0938\u094d\u091f\u093e\u092b",
        "\u090f\u0938\u094d\u0915\u0947\u0932\u0947\u091f", "\u092c\u0948\u0902\u0915 \u0928\u0947 \u0938\u092e\u093e\u0927\u093e\u0928",
        "\u0caf\u0cbe\u0cb5 \u0cb8\u0cbf\u0cac\u0ccd\u0cac\u0c82\u0ca6\u0cbf", "\u0c8e\u0cb8\u0ccd\u0c95\u0cb2\u0cc7\u0c9f\u0ccd",
    ))


def _support_unavailable_workflow_text(language: str) -> str:
    # Keep this capability statement controlled; no LLM can imply a real bank action.
    return (
        "VaaniShield uses synthetic demo support tickets. Staff assignment, staff escalation, "
        "and real-bank investigation or resolution are not available here. I can show a ticket's "
        "recorded status and history or offer general suggestions; those suggestions do not mean a bank acted."
    )


def _support_cannot_close_text(status: str, language: str) -> str:
    return (
        f"This synthetic support ticket is recorded as {status}. It is not eligible for customer closure, "
        "and no status change was made."
    )


def _is_support_create_request(text: str, history=()) -> bool:
    query = text.casefold()
    if any(phrase in query for phrase in (
        "ದೂರಿನ ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ವಿವರಗಳು ಕಾಣುತ್ತಿಲ್ಲ", "ವಿವರ ಕಾಣಿಸುತ್ತಿಲ್ಲ",
        "ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ಕಾಣುತ್ತಿಲ್ಲ",
        "शिकायत का विवरण दिखाई नहीं दे रहा", "विवरण दिखाई नहीं दे रहा", "विवरण नहीं दिख रहा",
        "cannot see the complaint details", "can't see the complaint details",
        "cannot see the details", "can't see the details", "details are not visible",
        "where are the complaint details", "where are the ticket details",
    )):
        return True
    sentences = re.split(r"[.!?;\n]+", query)
    negation_pattern = re.compile(
        r"\b(?:do\s+not|don't|dont|never|no|not)\b(?:[\W_]+\w+){0,4}[\W_]*$"
    )
    for sentence in sentences:
        for term in _SUPPORT_CREATE_TERMS:
            term = term.casefold()
            start = sentence.find(term)
            while start >= 0:
                if not negation_pattern.search(sentence[max(0, start - 120):start]):
                    return True
                start = sentence.find(term, start + len(term))
    # Speech transcripts can mention the support object before the explicit
    # action, with category or issue context in between (e.g. "support ...
    # create a ticket"). Recognize either order within one sentence while
    # refusing negated requests. The action gate still only prepares a pending
    # proposal; ticket details and customer confirmation are validated later.
    action_pattern = re.compile(r"\b(?:file|raise|lodge|submit|open|create|make|prepare|register)\b")
    target_pattern = re.compile(r"\b(?:support(?:\s+ticket)?|ticket|complaint)\b")
    for sentence in sentences:
        actions = list(action_pattern.finditer(sentence))
        targets = list(target_pattern.finditer(sentence))
        for action in actions:
            if negation_pattern.search(sentence[max(0, action.start() - 120):action.start()]):
                continue
            for target in targets:
                if action.end() <= target.start():
                    between = sentence[action.end():target.start()]
                elif target.end() <= action.start():
                    between = sentence[target.end():action.start()]
                else:
                    continue
                if len(re.findall(r"\w+", between)) <= 50:
                    return True
    # Allows multi-turn collection after the assistant's deterministic prompt.
    return bool(
        len(history) >= 2
        and history[-2].role == "assistant"
        and (
            history[-2].content.startswith((
                "To prepare a synthetic support ticket",
                "I have noted",
                "Sure. Is the amount deducted",
                "Understood. I'll prepare a transaction complaint",
                "I noticed a recent transaction",
                "ಬೆಂಬಲ ಟಿಕೆಟ್ ಕರಡು",
                "ನಿಮ್ಮ ದೂರು ವಿವರಗಳನ್ನು",
                "ನಿಮ್ಮ ವಿವರಗಳನ್ನು",
                "ಖಂಡಿತ. ನಿಮ್ಮ ಖಾತೆಯಿಂದ ಹಣ ಕಡಿತಗೊಂಡಿದೆಯೇ",
                "ಅರ್ಥವಾಯಿತು. ನಾನು ವಹಿವಾಟು ದೂರು ಸಿದ್ಧಪಡಿಸುತ್ತೇನೆ",
                "ನಾನು ",
                "सहायता टिकट का मसौदा",
                "मैंने आपकी शिकायत",
                "मैंने आपका विवरण",
                "ज़रूर। क्या आपके खाते से राशि कट गई है",
                "समझ गया। मैं लेनदेन की शिकायत तैयार करूंगा",
                "मैंने ",
            ))
            or "No ticket has been created" in history[-2].content
            or "ಇನ್ನೂ ಯಾವುದೇ ಟಿಕೆಟ್ ರಚಿಸಲಾಗಿಲ್ಲ" in history[-2].content
            or "अभी कोई टिकट नहीं बनाया गया है" in history[-2].content
            or "Would you like to mark it as high priority?" in history[-2].content
            or "ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?" in history[-2].content
            or "उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?" in history[-2].content
            or "Is the amount deducted from your account?" in history[-2].content
            or "ಪರಿಶೀಲನೆಗಾಗಿ ನಿಮ್ಮ ದೂರಿನ ವಿವರಗಳು ಇಲ್ಲಿವೆ" in history[-2].content
            or "समीक्षा के लिए आपकी शिकायत का विवरण यहाँ है" in history[-2].content
            or "Here are your support ticket details for review" in history[-2].content
            or "Here are the complaint details. Please review" in history[-2].content
            or "ದೂರಿನ ವಿವರಗಳು ಇಲ್ಲಿವೆ. ನಾನು ಸಲ್ಲಿಸುವ ಮೊದಲು" in history[-2].content
            or "शिकायत का विवरण यहाँ है। सबमिट करने से पहले" in history[-2].content
        )
    )


def _is_support_close_request(text: str) -> bool:
    query = text.casefold()
    return (
        any(term.casefold() in query for term in _SUPPORT_CLOSE_TERMS)
        or bool(re.search(r"\bclose\b.*\b(?:ticket|complaint)\b", query))
        or bool(re.search(r"\bclose\b.*\bVS-[A-F0-9]{32}\b", text, flags=re.IGNORECASE))
    )


def _find_matching_customer_transaction(principal, amount: float | None) -> dict | None:
    if amount is None or principal is None:
        return None
    try:
        from services.banking_service import get_customer_profile_by_key, get_recent_transactions
        profile = get_customer_profile_by_key(principal.customer_key)
        if not profile:
            return None
        recent = get_recent_transactions(profile["customer_id"], limit=10)
        for tx in recent:
            tx_amount = float(tx.get("amount") or 0.0)
            if abs(tx_amount - float(amount)) < 0.01:
                raw_date = str(tx.get("transaction_date", ""))
                formatted_date = raw_date
                try:
                    dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    formatted_date = dt.strftime("%d %b")
                except Exception:
                    pass
                return {
                    "merchant": tx.get("merchant") or "Merchant",
                    "amount": tx_amount,
                    "date": formatted_date,
                    "transaction_ref": tx.get("reference_id") or "",
                }
    except Exception as exc:
        logger.debug("Could not search matching transactions: %s", exc)
    return None


def _extract_support_ticket_slots(text: str) -> dict[str, str | float | None]:
    slots = {"category": None, "priority": None, "subject": None, "description": None, "amount": None}
    normalized = text.casefold()

    amt_match = re.search(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:,[0-9]+)*(?:\.[0-9]{1,2})?)\s*(?:₹|rs\.?|inr|rupees)?", normalized)
    if amt_match:
        try:
            val = float(amt_match.group(1).replace(",", ""))
            if val > 0:
                slots["amount"] = val
        except ValueError:
            pass

    priority_match = re.search(r"\bpriority\s*[:=-]?\s*(low|medium|high|urgent)\b", normalized)
    if priority_match:
        slots["priority"] = priority_match.group(1).upper()
    elif re.search(r"\b(urgent|urgently)\b", normalized) or any(t in normalized for t in ("तत्काल", "जरूरी", "ತುರ್ತು")):
        slots["priority"] = "URGENT"
    elif re.search(r"\bhigh\s*priority\b", normalized) or any(t in normalized for t in ("उच्च प्राथमिकता", "ಹೆಚ್ಚಿನ ಆದ್ಯತೆ")):
        slots["priority"] = "HIGH"
    elif any(t in normalized for t in ("mark as high", "mark it high")):
        slots["priority"] = "HIGH"
    elif re.search(r"\bmedium\b", normalized) or any(t in normalized for t in ("मध्यम", "ಮಧ್ಯಮ")):
        slots["priority"] = "MEDIUM"
    elif re.search(r"\blow\b", normalized) or any(t in normalized for t in ("कम", "ಕಡಿಮೆ")):
        slots["priority"] = "LOW"
    elif any(
        p_q in text for p_q in (
            "Would you like to mark it as high priority?",
            "ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?",
            "उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?",
        )
    ):
        if any(t in normalized for t in ("yes", "sure", "ok", "yeah", "please", "high", "ಹೌದು", "ಹೆಚ್ಚಿನ", "हाँ", "उच्च", "ज़रूर", "जरूर", "ಖಂಡಿತ")):
            slots["priority"] = "HIGH"

    cat_match = re.search(
        r"\bcategory\s*[:=-]?\s*([a-zA-Z_ ]+?)(?:,|\.|\n|priority|subject|description|$)",
        normalized,
    )
    if cat_match:
        cat_raw = cat_match.group(1).strip()
        for cat_name, terms in _SUPPORT_CATEGORY_TERMS.items():
            if any(term in cat_raw for term in terms):
                slots["category"] = cat_name
                break
    if not slots["category"]:
        if any(term in normalized for term in ("upi", "transaction", "transfer", "failed payment", "payment failed", "merchant", "debited", "deducted", "ವಹಿವಾಟು", "ಪಾವತಿ", "लेनदेन", "भुगतान", "ಯುಪಿಐ", "यूपीआई")):
            slots["category"] = "transaction"
        elif any(term in normalized for term in ("card payment", "debit card", "credit card", "atm card", "card services")):
            slots["category"] = "card"
        elif any(term in normalized for term in ("netbanking", "mobile banking", "app login", "digital banking")):
            slots["category"] = "digital_banking"
        elif any(term in normalized for term in ("loan", "emi", "ऋण", "लोन", "ಸಾಲ")):
            slots["category"] = "loan"
        elif any(term in normalized for term in ("fixed deposit", "term deposit", "deposit")):
            slots["category"] = "deposit"
        elif any(term in normalized for term in ("account", "savings account", "current account")):
            slots["category"] = "account"

    desc_match = re.search(
        r"\bdescription\s*[:=-]?\s*['\"]?(.+?)['\"]?(?:\n|subject\s*[:=-]|priority\s*[:=-]|category\s*[:=-]|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if desc_match:
        desc_val = desc_match.group(1).strip().strip(".'\"")
        if len(desc_val) >= 3:
            slots["description"] = desc_val[:1000]

    subj_match = re.search(
        r"\bsubject\s*[:=-]?\s*['\"]?(.+?)['\"]?(?:\n|description\s*[:=-]|priority\s*[:=-]|category\s*[:=-]|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if subj_match:
        subj_val = subj_match.group(1).strip().strip(".'\"")
        subj_val = re.sub(
            r"\s*(?:ಅಂತ\s+(?:ಇಡು|ಹಾಕು|ಬರೆ|ಮಾಡು|ತಿಳಿಸು|ಕೊಡು)|ಇಡು|ಹಾಕು|ಬರೆ|ರಖೋ|रखो|डालो|लिखो|करो)\s*[.!?]?\s*$",
            "",
            subj_val,
            flags=re.IGNORECASE,
        ).strip().strip(".'\"")
        if len(subj_val) >= 3:
            slots["subject"] = subj_val[:160]
    elif not slots["subject"] and not desc_match:
        has_upi_or_payment = any(
            term in normalized for term in (
                "upi", "payment", "money", "debited", "deducted",
                "ಯುಪಿಐ", "ಪಾವತಿ", "ಹಣ", "ಕಡಿತ",
                "यूपीआई", "भुगतान", "पैसे", "कट", "राशि",
            )
        )
        has_not_received = any(
            term in normalized for term in (
                "merchant hasn't received", "merchant has not received", "merchant not received",
                "not received", "didn't receive", "haven't received",
                "ತಲುಪಿಲ್ಲ", "ಸ್ವೀಕರಿಸಿಲ್ಲ",
                "नहीं मिला", "नहीं मिले", "प्राप्त नहीं हुआ",
            )
        )
        if has_upi_or_payment and has_not_received:
            slots["subject"] = "UPI payment debited but not received by merchant"

    has_debit_mention = any(
        term in normalized for term in (
            "debited", "deducted", "कटा", "कट गई", "ಕಡಿತ", "ಹಣ ಹೋಯಿತು", "ಕಡಿತಗೊಂಡಿದೆ", "yes", "ಹೌದು", "हाँ"
        )
    ) or (slots.get("amount") is not None)
    slots["debit_confirmed"] = bool(has_debit_mention)

    if slots.get("subject") and not slots.get("description"):
        amt_str = f" Amount: ₹{slots['amount']:,.2f}." if slots.get("amount") else ""
        if slots["subject"] == "UPI payment debited but not received by merchant":
            slots["description"] = f"Amount deducted from the customer's account, but merchant has not received payment.{amt_str}"
        else:
            slots["description"] = f"Customer reported: {slots['subject']}.{amt_str}"

    return slots


def _support_ticket_clarification(
    language: str,
    slots: dict | None = None,
    matching_tx: dict | None = None,
    text: str = "",
) -> str:
    slots = slots or {}
    category = slots.get("category")
    priority = slots.get("priority")
    subject = slots.get("subject")
    amount = slots.get("amount")
    debit_confirmed = slots.get("debit_confirmed", False)

    normalized = text.casefold() if text else ""
    is_upi_or_tx = (category == "transaction") or any(
        term in normalized for term in ("upi", "payment", "merchant", "deducted", "debited", "ವಹಿವಾಟು", "ಪಾವತಿ", "लेनदेन", "भुगतान")
    )

    if is_upi_or_tx:
        has_deduction = debit_confirmed or any(term in normalized for term in ("deducted", "debited", "yes", "कट गई", "कटा", "ಕಡಿತ", "ಹೌದು")) or (amount is not None)
        if not has_deduction and not amount:
            if language == "kn-IN":
                return "ಖಂಡಿತ. ನಿಮ್ಮ ಖಾತೆಯಿಂದ ಹಣ ಕಡಿತಗೊಂಡಿದೆಯೇ?"
            if language == "hi-IN":
                return "ज़रूर। क्या आपके खाते से राशि कट गई है?"
            return "Sure. Is the amount deducted from your account?"

        if not priority:
            if matching_tx:
                merchant = matching_tx.get("merchant", "the merchant")
                tx_date = matching_tx.get("date", "recently")
                tx_amt = matching_tx.get("amount") or amount or 2500.0
                if language == "kn-IN":
                    return f"ನಾನು {tx_date} ರಂದು {merchant} ನಲ್ಲಿ ₹{tx_amt:,.0f} ಇತ್ತೀಚಿನ ವಹಿವಾಟನ್ನು ಗಮನಿಸಿದ್ದೇನೆ. ನೀವು ವರದಿ ಮಾಡುತ್ತಿರುವ ವಹಿವಾಟು ಇದೆಯೇ? ಇದನ್ನು ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?"
                if language == "hi-IN":
                    return f"मैंने {tx_date} को {merchant} पर ₹{tx_amt:,.0f} का हालिया लेनदेन देखा। क्या आप इसी लेनदेन की रिपोर्ट कर रहे हैं? क्या आप इसे उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?"
                return f"I noticed a recent transaction of ₹{tx_amt:,.0f} at {merchant} on {tx_date}. Is this the transaction you're reporting? Would you like to mark it as high priority?"

            if language == "kn-IN":
                return "ಅರ್ಥವಾಯಿತು. ನಾನು ವಹಿವಾಟು ದೂರು ಸಿದ್ಧಪಡಿಸುತ್ತೇನೆ. ಇದನ್ನು ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?"
            if language == "hi-IN":
                return "समझ गया। मैं लेनदेन की शिकायत तैयार करूंगा। क्या आप इसे उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?"
            return "Understood. I'll prepare a transaction complaint. Would you like to mark it as high priority?"

    if not priority:
        if language == "kn-IN":
            return "ದೂರು ದಾಖಲಿಸಲು ಆದ್ಯತೆಯನ್ನು (LOW, MEDIUM, HIGH, ಅಥವಾ URGENT) ಹಂಚಿಕೊಳ್ಳಿ."
        if language == "hi-IN":
            return "शिकायत दर्ज करने के लिए प्राथमिकता (LOW, MEDIUM, HIGH, या URGENT) बताएं।"
        return "Please share the priority (LOW, MEDIUM, HIGH, or URGENT) for your support ticket."

    if not subject:
        category_display = str(category).capitalize() if category else None
        prio_display = str(priority).upper() if priority else None
        noted = []
        if category_display:
            noted.append(f"Category: {category_display}")
        if prio_display:
            noted.append(f"Priority: {prio_display}")
        noted_str = f" ({', '.join(noted)})" if noted else ""
        if language == "kn-IN":
            return (
                f"ನಿಮ್ಮ ದೂರು ವಿವರಗಳನ್ನು ಗುರುತಿಸಲಾಗಿದೆ{noted_str}. "
                "ಟಿಕೆಟ್ ಕರಡು ಸಿದ್ಧಪಡಿಸಲು ದಯವಿಟ್ಟು ಸಂಕ್ಷಿಪ್ತ ವಿಷಯವನ್ನು (Subject) ಹಂಚಿಕೊಳ್ಳಿ. "
                "ಇನ್ನೂ ಯಾವುದೇ ಟಿಕೆಟ್ ರಚಿಸಲಾಗಿಲ್ಲ."
            )
        if language == "hi-IN":
            return (
                f"मैंने आपकी शिकायत का विवरण नोट कर लिया है{noted_str}। "
                "टिकट का मसौदा तैयार करने के लिए कृपया एक संक्षिप्त विषय (Subject) बताएं। "
                "अभी कोई टिकट नहीं बनाया गया है।"
            )
        return (
            f"I have noted your complaint details{noted_str}. "
            "Please provide a short subject to prepare the support ticket draft. "
            "No ticket has been created yet."
        )

    if not category:
        if language == "kn-IN":
            return "ಟಿಕೆಟ್ ಕರಡು ಸಿದ್ಧಪಡಿಸಲು ದಯವಿಟ್ಟು ವರ್ಗ (Account, Card, Transaction, Digital Banking, Loan, Deposit) ಹಂಚಿಕೊಳ್ಳಿ."
        if language == "hi-IN":
            return "टिकट का मसौदा तैयार करने के लिए कृपया श्रेणी बताएं।"
        return "Please share the category (Account, Card, Transaction, Digital Banking, Loan, Deposit) to prepare the support ticket draft."

    return _support_proposal_text(
        subject,
        language,
        category=category,
        priority=priority,
        amount=amount,
        description=slots.get("description"),
    )


def _support_close_ref_prompt(language: str) -> str:
    if language == "kn-IN":
        return "ನಿಮ್ಮ ಟಿಕೆಟ್ ಮುಚ್ಚುವ ವಿನಂತಿಯನ್ನು ಪರಿಶೀಲಿಸಲು ಅದರ ಸಾರ್ವಜನಿಕ ಟಿಕೆಟ್ ಉಲ್ಲೇಖವನ್ನು ಹಂಚಿಕೊಳ್ಳಿ."
    if language == "hi-IN":
        return "टिकट बंद करने का अनुरोध जाँचने के लिए उसका सार्वजनिक टिकट संदर्भ साझा करें।"
    return "Please share the public ticket reference so I can check whether it can be closed."


def _support_proposal_text(
    subject: str,
    language: str,
    *,
    category: str | None = None,
    priority: str | None = None,
    amount: float | None = None,
    description: str | None = None,
) -> str:
    cat_str = (category or "transaction").capitalize()
    prio_str = (priority or "HIGH").upper()
    amt_str = f"₹{amount:,.2f}" if amount is not None else None
    desc_str = description or f"Customer reported: {subject}."

    if language == "kn-IN":
        cat_kn = {
            "transaction": "ವಹಿವಾಟು (Transaction)",
            "card": "ಕಾರ್ಡ್ (Card)",
            "account": "ಖಾತೆ (Account)",
            "digital_banking": "ಡಿಜಿಟಲ್ ಬ್ಯಾಂಕಿಂಗ್ (Digital Banking)",
            "loan": "ಸಾಲ (Loan)",
            "deposit": "ಠೇವಣಿ (Deposit)",
        }.get(str(category or "").lower(), cat_str)
        prio_kn = {
            "HIGH": "ಹೆಚ್ಚಿನ (HIGH)",
            "URGENT": "ತುರ್ತು (URGENT)",
            "MEDIUM": "ಮಧ್ಯಮ (MEDIUM)",
            "LOW": "ಕಡಿಮೆ (LOW)",
        }.get(prio_str, prio_str)
        amt_line = f"\n- **ಮೊತ್ತ (Amount):** {amt_str}" if amt_str else ""
        return (
            "ಪರಿಶೀಲನೆಗಾಗಿ ನಿಮ್ಮ ದೂರಿನ ವಿವರಗಳು ಇಲ್ಲಿವೆ:\n"
            f"- **ವರ್ಗ (Category):** {cat_kn}\n"
            f"- **ವಿಷಯ (Subject):** {subject}\n"
            f"- **ಆದ್ಯತೆ (Priority):** {prio_kn}"
            f"{amt_line}\n"
            f"- **ವಿವರಣೆ (Description):** {desc_str}\n\n"
            "ದಯವಿಟ್ಟು ಈ ಟಿಕೆಟ್ ಅನ್ನು ಸಲ್ಲಿಸಲು ನೀವು ಖಚಿತಪಡಿಸುತ್ತೀರಾ? ನಿಮ್ಮ ದೃಢೀಕರಣವಿಲ್ಲದೆ ಯಾವುದೇ ಟಿಕೆಟ್ ರಚಿಸಲಾಗುವುದಿಲ್ಲ."
        )
    if language == "hi-IN":
        cat_hi = {
            "transaction": "लेनदेन (Transaction)",
            "card": "कार्ड (Card)",
            "account": "खाता (Account)",
            "digital_banking": "डिजिटल बैंकिंग (Digital Banking)",
            "loan": "ऋण (Loan)",
            "deposit": "जमा (Deposit)",
        }.get(str(category or "").lower(), cat_str)
        prio_hi = {
            "HIGH": "उच्च (HIGH)",
            "URGENT": "तत्काल (URGENT)",
            "MEDIUM": "मध्यम (MEDIUM)",
            "LOW": "कम (LOW)",
        }.get(prio_str, prio_str)
        amt_line = f"\n- **राशि (Amount):** {amt_str}" if amt_str else ""
        return (
            "समीक्षा के लिए आपकी शिकायत का विवरण यहाँ है:\n"
            f"- **श्रेणी (Category):** {cat_hi}\n"
            f"- **विषय (Subject):** {subject}\n"
            f"- **प्राथमिकता (Priority):** {prio_hi}"
            f"{amt_line}\n"
            f"- **विवरण (Description):** {desc_str}\n\n"
            "कृपया पुष्टि करें कि क्या आप यह टिकट सबमिट करना चाहते हैं। आपकी पुष्टि के बिना कोई टिकट नहीं बनाया जाएगा।"
        )
    amt_line = f"\n- **Amount:** {amt_str}" if amt_str else ""
    return (
        "Here are your support ticket details for review:\n"
        f"- **Category:** {cat_str}\n"
        f"- **Subject:** {subject}\n"
        f"- **Priority:** {prio_str}"
        f"{amt_line}\n"
        f"- **Description:** {desc_str}\n\n"
        "Please confirm if you would like me to submit this ticket. No ticket has been created yet."
    )


def _support_close_text(ticket_ref: str, language: str) -> str:
    if language == "kn-IN":
        return f"ಟಿಕೆಟ್ {ticket_ref} ಮುಚ್ಚುವ ವಿನಂತಿ ಸಿದ್ಧವಾಗಿದೆ. ಯಾವುದೇ ಸ್ಥಿತಿ ಬದಲಾವಣೆ ಆಗಿಲ್ಲ. ಮುಂದುವರಿಯಲು ದೃಢೀಕರಣ ಬಟನ್ ಬಳಸಿ."
    if language == "hi-IN":
        return f"टिकट {ticket_ref} बंद करने का अनुरोध तैयार है। स्थिति नहीं बदली है। आगे बढ़ने के लिए पुष्टि बटन दबाएं।"
    return (
        f"Ticket {ticket_ref} can be closed. No status change has been made. "
        "Confirm closure with the explicit button if you want to proceed."
    )


def _support_priority_was_stated(text: str) -> bool:
    terms = (
        'कम', 'मध्यम', 'उच्च', 'तत्काल', 'ज़रूरी', 'हाँ', 'जरूर', 'ज़रूर',
        'ಕಡಿಮೆ', 'ಮಧ್ಯಮ', 'ಹೆಚ್ಚಿನ', 'ತುರ್ತು', 'ಹೌದು', 'ಖಂಡಿತ',
    )
    normalized = text.casefold()
    if any(p_q in text for p_q in (
        'Would you like to mark it as high priority?',
        'ಹೆಚ್ಚಿನ ಆದ್ಯತೆಯಾಗಿ ಗುರುತಿಸಬೇಕೇ?',
        'उच्च प्राथमिकता के रूप में चिह्नित करना चाहेंगे?',
    )):
        return True
    return bool(re.search(r'\b(?:low|medium|high|urgent|urgently|urgency)\b', normalized)) or any(
        term.casefold() in normalized for term in terms
    )


def _support_issue_was_described(text: str) -> bool:
    normalized = text.casefold()
    for phrase in _SUPPORT_CREATE_TERMS:
        normalized = normalized.replace(phrase.casefold(), ' ')
    words = re.findall(r'\w+', normalized)
    excluded = {
        'please', 'i', 'want', 'to', 'a', 'an', 'the', 'my', 'me', 'for', 'about',
        'ticket', 'support', 'complaint', 'priority', 'low', 'medium', 'high', 'urgent',
        'urgently', 'urgency', 'category', 'subject', 'description', 'and', 'with',
    }
    substantive = [word for word in words if len(word) > 2 and word not in excluded]
    return len(substantive) >= 2 or any(term in normalized for term in ('upi', 'payment', 'merchant', 'deducted', 'debited', 'failed', 'ಹಣ', 'ಕಡಿತ', 'पैसे', 'कट'))


def _has_scam_intent(text: str) -> bool:
    query = text.casefold()
    if any(term in query for term in _SCAM_INTENT_TERMS):
        return True
    # These Unicode patterns are written as escapes so source-file terminal
    # code pages cannot corrupt the Kannada/Hindi routing vocabulary.
    if re.search(r"\u092b\u094d\u0930\u0949\u0921|\u0927\u094b\u0916\u093e|\u0920\u0917\u0940|\u0938\u094d\u0915\u0948\u092e|\u0cb5\u0c82\u0c9a\u0ca8\u0cc6|\u0cae\u0ccb\u0cb8", query):
        return True
    return bool(re.search(
        r"\b(?:share|send|give|reveal)\b.{0,24}\b(?:otp|upi pin|password|pin)\b|"
        r"\b(?:otp|upi pin|password|pin)\b.{0,24}\b(?:share|send|give|reveal)\b",
        query,
    ))


def _required_tools_for_question(text: str, assessment_id=None) -> frozenset[str]:
    """Conservative rule gate for requests that need customer-specific records."""
    query = text.casefold()
    # An explicit request to prepare a support ticket must not inherit banking
    # lookups merely because the issue mentions the customer's card or payment.
    if _is_support_create_request(text):
        return frozenset()

    if "[UNTRUSTED CUSTOMER ATTACHMENT" in text or any(phrase in query for phrase in (
        "this statement", "uploaded statement", "attached statement",
        "this document", "uploaded document", "attached document",
        "this photo", "uploaded photo", "attached photo",
        "this screenshot", "uploaded screenshot", "attached screenshot",
        "this file", "uploaded file", "attached file",
        "this loan document", "uploaded loan", "attached loan",
        "summarize this document", "explain this document",
        "summarize this file", "explain this file",
        "explain this screenshot", "what this screenshot says",
    )):
        return frozenset()

    support_ref = re.search(r"\bVS-[A-F0-9]{32}\b", text, flags=re.IGNORECASE)
    support_close = _is_support_close_request(text)
    support_history = any(term in query for term in (
        "ticket history", "ticket events", "what happened to", "complaint history",
        "status history", "ticket update history", "history of my support ticket",
        "history of my ticket", "history of my complaint", "status events", "event history",
        "updates on", "update on", "timeline", "audit trail",
    )) or bool(re.search(
        r"\b(?:history|events|timeline|updates?)\s+of\s+(?:my\s+)?(?:support\s+)?(?:ticket|complaint)\b",
        query,
    )) or bool(re.search(
        r"\b(?:what\s+happened\s+(?:to|with)|how\s+was\s+\w+\s+resolved)\b",
        query,
    ))
    if support_ref:
        if support_close:
            return frozenset({"prepare_support_ticket_close"})
        if support_history or any(term in query for term in ("history", "event", "happened", "timeline", "update")):
            return frozenset({"get_support_ticket", "get_support_ticket_events"})
        return frozenset({"get_support_ticket"})

    contact_question = any(term in query for term in (
        "how do i contact", "how to contact", "how can i contact", "contact customer support",
        "contact support", "reach customer support", "support phone", "support email",
        "customer care number", "helpline", "customer support number",
    ))

    support_list = any(term in query for term in (
        "my tickets", "my complaints", "open tickets", "support tickets", "list tickets",
        "show tickets", "show my ticket", "show resolved tickets", "show closed tickets",
        "show active tickets", "resolved tickets", "closed tickets", "active tickets",
        "show complaints", "list complaints", "previous complaint", "previous ticket",
        "latest support ticket", "latest ticket", "latest complaint", "recent support ticket",
        "recent support tickets", "recent ticket", "recent tickets", "last ticket", "last complaint",
        "show the details of my last ticket", "details of my last ticket",
        "where is my ticket", "where's my ticket", "find my ticket", "lookup my ticket", "look up my ticket",
        "track my ticket", "track my complaint", "track ticket", "track complaint",
        "update on my ticket", "update on my complaint", "updates on my ticket",
        "information about my ticket", "show all information about my ticket",
        "meri shikayat", "meri ticket", "मेरी शिकायत", "मेरे टिकट",
        "शिकायत की स्थिति", "टिकट की स्थिति", "मेरी कार्ड शिकायत", "ताजा शिकायत", "हालिया टिकट",
        "पिछली शिकायत", "टिकट कहां है", "शिकायत कहां है", "शिकायत दिखाएं", "टिकट दिखाएं",
        "ನನ್ನ ದೂರು", "ನನ್ನ ಟಿಕೆಟ್", "ದೂರುಗಳ ಪಟ್ಟಿ", "ಟಿಕೆಟ್ ಸ್ಥಿತಿ", "ದೂರು ಸ್ಥಿತಿ",
        "ನನ್ನ ಕಾರ್ಡ್ ದೂರು", "ಇತ್ತೀಚಿನ ಟಿಕೆಟ್", "ಹಿಂದಿನ ದೂರು", "ಟಿಕೆಟ್ ಎಲ್ಲಿದೆ", "ದೂರು ಎಲ್ಲಿದೆ",
        "ಟಿಕೆಟ್ ತೋರಿಸು", "ದೂರು ತೋರಿಸು", "ದೂರುಗಳನ್ನು ತೋರಿಸು",
    ))
    support_status = any(term in query for term in (
        "ticket status", "status of my ticket", "check my ticket", "check ticket",
        "complaint status", "status of my complaint", "check my latest ticket", "check my latest complaint",
        "check my recent ticket", "check my recent complaint",
    )) or bool(re.search(
        r"\b(?:what(?:'s| is)\s+)?(?:the\s+)?status of (?:my\s+)?(?:\w+\s+)*(?:support\s+)?(?:ticket|complaint)\b",
        query,
    )) or bool(re.search(
        r"\b(?:what\s+is\s+the\s+status\s+of|check\s+the\s+status\s+of|status\s+of)\s+(?:my\s+)?(?:latest|recent|last|open)?\s*(?:ticket|complaint)\b",
        query,
    ))
    support_query_intent = (
        any(term in query for term in (
            "ticket", "tickets", "complaint", "complaints", "support case", "support cases",
            "shikayat", "shikayaten", "मेरी शिकायत", "मेरे टिकट", "शिकायत", "शिकायतें", "शिकायतों", "टिकट", "टिकटों",
            "ನನ್ನ ದೂರು", "ನನ್ನ ಟಿಕೆಟ್", "ದೂರ", "ದೂರು", "ದೂರುಗಳು", "ದೂರುಗಳ", "ದೂರಿನ", "ಟಿಕೆಟ್", "ಟಿಕೆಟ್‌ಗಳು", "ಟಿಕೆಟ್‌ನ",
        )) and (
            any(term in query for term in (
                "latest", "recent", "last", "previous", "open", "closed", "resolved", "active",
                "status", "check", "show", "list", "view", "find", "where", "track", "lookup", "look up",
                "details", "detail", "update", "updates", "history", "what happened", "how many",
                "my", "all", "information",
                "ಸ್ಥಿತಿ", "ಪಟ್ಟಿ", "ಎಲ್ಲಿದೆ", "ತೋರಿಸು", "ಇತ್ತೀಚಿನ", "ಹಿಂದಿನ",
                "स्थिति", "कहाँ", "दिखाएं", "हालिया", "पिछली", "ताजा",
            ))
            or bool(re.search(r"\b(?:my|mine|mera|meri|mere|nanna|ನನ್ನ|मेरी|मेरे|मेरा)\b", query))
        )
    )

    if not contact_question and (support_list or support_status or support_history or support_query_intent):
        return frozenset({"list_support_tickets"})

    if assessment_id is None and not _has_scam_intent(text):
        account_intent = classify_account_intent(text)
        if account_intent is not None and account_intent["needs"] is None:
            action = account_intent["action"]
            if action == "OPEN":
                return frozenset({"start_account_application"})
            if action == "OPTIONS":
                return frozenset({"get_required_application_documents"})
            if action == "STATUS":
                return frozenset({"get_account_application_status"})

    investigation_ref = re.search(r"\bINV-[A-F0-9]{32}\b", text, flags=re.IGNORECASE)
    if investigation_ref and any(term in query for term in ("status", "details", "detail", "history", "show", "explain")):
        return frozenset({"get_investigation"})
    investigation_terms = ("investigation", "investigations", "security case", "security report", "track this scam", "track this incident")
    if any(term in query for term in investigation_terms):
        if any(term in query for term in ("create", "open", "start", "track", "add", "link", "update", "change", "resolve", "close", "attach")):
            return frozenset()
        if any(term in query for term in (
            "list", "show", "my recent", "history", "all my", "what are my",
            "what do i have", "what investigations", "how many", "my open",
            "my closed", "my resolved",
        )):
            return frozenset({"list_investigations"})
        if investigation_ref and any(term in query for term in ("status", "details", "detail", "history", "show", "explain")):
            return frozenset({"get_investigation"})
    explicit_risk_id = re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        text,
        flags=re.IGNORECASE,
    )
    explicit_transaction_ref = re.search(r"\bTR-[A-F0-9]{48}\b", text, flags=re.IGNORECASE)
    social_engineering_cues = any(term in query for term in (
        "is this a scam", "is this scam", "is it a scam", "could this be a scam",
        "called and asked", "claiming to be from", "claimed to be from",
        "asked for my otp", "asking for my otp", "asked for otp", "share your otp",
        "send your otp", "give me your otp", "asked for my upi pin", "asked for my pin",
        "asked for my cvv", "sent me a link", "sent a link", "reverse a suspicious",
        "reverse a transaction", "install anydesk", "install teamviewer",
        "account will be blocked", "scam message", "scam call", "suspicious message",
        "suspicious call", "otp bhejo", "otp batao", "otp ಕಳುಹಿಸಿ", "otp ಕೇಳಿದರು",
        "ಇದು scam", "ಸ್ಕ್ಯಾಮ್ ಆಗಿದೆಯೇ", "क्या यह स्कैम", "यह स्कैम है",
    ))
    if social_engineering_cues and not explicit_transaction_ref and not explicit_risk_id:
        return frozenset({"assess_current_scam_message"})
    risk_terms = (
        "transaction risk", "risk assessment", "suspicious transaction",
        "transaction suspicious", "transaction for suspicious activity",
        "suspicious activity", "fraud risk", "transaction fraud", "suspicious indicators",
        "marked high", "marked critical", "risk score",
        "risk history", "risk assessments", "assessment history",
        "लेनदेन जोखिम", "संदिग्ध लेनदेन", "जोखिम आकलन",
        "ವಹಿವಾಟಿನ ಅಪಾಯ", "ಸಂದೇಹಾಸ್ಪದ ವ್ಯವಹಾರ", "ಅಪಾಯ ಮೌಲ್ಯಮಾಪನ",
    )
    has_transaction_risk_intent = any(term in query for term in risk_terms)
    has_transaction_risk_intent = has_transaction_risk_intent or (
        "transaction" in query
        and any(term in query for term in ("fraud", "scam", "suspicious", "risk"))
        and not _has_scam_intent(text)
    )
    risk_history_query = any(term in query for term in (
        "risk history", "risk assessments", "assessment history",
        "recent risk", "transaction risk history", "recent transaction risk",
        "मेरे जोखिम आकलन", "हाल के जोखिम",
        "ಅಪಾಯದ ಇತಿಹಾಸ", "ಇತ್ತೀಚಿನ ಅಪಾಯ ಮೌಲ್ಯಮಾಪನ",
    ))
    risk_detail_query = any(term in query for term in (
        "why was", "why is", "explain the indicators", "explain indicators",
        "explain this assessment", "why marked", "marked high", "marked critical",
        "risk score", "suspicious indicators", "इसका कारण", "क्यों उच्च",
        "ಇದಕ್ಕೆ ಕಾರಣ", "ಯಾಕೆ ಹೆಚ್ಚು",
    )) and has_transaction_risk_intent
    if risk_history_query:
        return frozenset({"list_transaction_risk_assessments"})
    if risk_detail_query or (explicit_risk_id and has_transaction_risk_intent):
        if explicit_risk_id:
            return frozenset({"get_transaction_risk_assessment"})
        return frozenset({"list_transaction_risk_assessments", "get_transaction_risk_assessment"})
    if has_transaction_risk_intent:
        if explicit_transaction_ref:
            return frozenset({"assess_transaction_risk"})
        return frozenset({"get_recent_transactions", "assess_transaction_risk"})
    scam_follow_up = any(term in query for term in (
        "scam", "fraud", "suspicious", "assessment", "warning", "message",
        "call", "block", "report", "safe", "threat", "phishing",
        "\u0938\u094d\u0915\u0948\u092e", "\u091c\u094b\u0916\u093f\u092e", "\u0938\u0941\u0930\u0915\u094d\u0937\u093f\u0924",
        "\u0cb8\u0ccd\u0caf\u0cbe\u0cae\u0ccd", "\u0c85\u0caa\u0cbe\u0caf", "\u0cb8\u0cc1\u0cb0\u0c95\u0ccd\u0cb7\u0cbf\u0ca4",
    ))
    has_banking = any(term in query for term in (
        "balance", "account", "accounts", "card", "cards", "loan", "loans",
        "transaction", "transactions", "spending", "spent", "budget", "saving", "savings",
        "\u0916\u093e\u0924\u093e", "\u092c\u0948\u0932\u0947\u0902\u0938", "\u0915\u093e\u0930\u094d\u0921", "\u0932\u094b\u0928", "\u0932\u0947\u0928\u0926\u0947\u0928",
        "\u0c96\u0cbe\u0ca4\u0cc6", "\u0cac\u0ccd\u0caf\u0cbe\u0cb2\u0cc6\u0ca8\u0ccd\u0cb8\u0ccd", "\u0c95\u0cbe\u0cb0\u0ccd\u0ca1\u0ccd", "\u0cb8\u0cbe\u0cb2", "\u0cb5\u0ccd\u0caf\u0cb5\u0cb9\u0cbe\u0cb0",
    ))
    if assessment_id is not None and (scam_follow_up or not has_banking):
        return frozenset({"get_scam_assessment"})
    if _has_scam_intent(text):
        return frozenset({"assess_current_scam_message"})

    public_transaction_ref = re.search(r"\bTR-[A-F0-9]{48}\b", text, re.IGNORECASE)
    if public_transaction_ref:
        return frozenset({"get_transaction_details"})

    amount_filter = _transaction_amount_filter(text)
    if amount_filter and any(term in query for term in ("transaction", "transactions", "payment", "purchase", "spent", "spending")):
        return frozenset({"search_transactions"})
    if any(term in query for term in ("search transaction", "find transaction", "transaction search")):
        return frozenset({"search_transactions"})

    requested = set()

    savings_advice_terms = (
        "ways to save", "way to save", "save money", "how to save", "how can i save", "how do i save",
        "practical ways to save", "suggest three practical ways", "suggest ways to save", "three practical ways",
        "tips to save", "budget recommendation", "budget recommendations", "recommend a budget", "budget advice",
        "saving advice", "savings advice", "reduce expenses", "cut expenses", "save more",
        "\u092c\u091a\u0924 \u0915\u0947 \u0924\u0930\u0940\u0915\u0947", "\u092c\u091a\u0924 \u0915\u0947 \u0909\u092a\u093e\u092f",
        "\u092a\u0948\u0938\u0947 \u092c\u091a\u093e\u0928\u0947", "\u092c\u091c\u091f \u0938\u0941\u091d\u093e\u0935",
        "\u0cb9\u0ca3 \u0c89\u0cb3\u0cbf\u0cb8\u0cb2\u0cc1", "\u0c89\u0cb3\u0cbf\u0ca4\u0cbe\u0caf\u0ca6 \u0cae\u0cbe\u0cb0\u0ccd\u0c97\u0c97\u0cb3\u0cc1",
        "\u0cac\u0c9c\u0cc6\u0c9f\u0ccd \u0cb8\u0cb2\u0cb9\u0cc6",
    )
    if any(term in query for term in savings_advice_terms):
        requested.add("generate_budget_recommendations")

    category_terms = (
        "highest spending category", "top spending category", "highest category", "top category",
        "category-wise", "category wise", "by category", "spending by category", "category spending",
        "spending categories", "breakdown by category", "category breakdown", "which category",
        "expenditure category", "categories", "category",
        "groceries", "grocery", "food spending", "utilities", "transport", "fuel spending", "health spending",
        "\u0916\u0930\u094d\u091a \u0936\u094d\u0930\u0947\u0923\u0940", "\u0936\u094d\u0930\u0947\u0923\u0940", "\u0936\u094d\u0930\u0947\u0923\u0940\u0935\u093e\u0930", "\u0915\u093f\u0938 \u0936\u094d\u0930\u0947\u0923\u0940", "\u0935\u093f\u092d\u093e\u0917",
        "\u0cb5\u0cb0\u0ccd\u0c97", "\u0cb5\u0cb0\u0ccd\u0c97\u0cb5\u0cbe\u0cb0\u0cc1", "\u0cb5\u0cb0\u0ccd\u0c97\u0ca6\u0cb2\u0ccd\u0cb2\u0cbf",
        "\u0cb5\u0cbf\u0cad\u0cbe\u0c97", "\u0cb5\u0cbf\u0cad\u0cbe\u0c97\u0ca6\u0cb2\u0ccd\u0cb2\u0cbf", "\u0cb5\u0cbf\u0cad\u0cbe\u0c97\u0cb5\u0cbe\u0cb0\u0cc1",
        "\u0caf\u0cbe\u0cb5 \u0cb5\u0cbf\u0cad\u0cbe\u0c97", "\u0caf\u0cbe\u0cb5 \u0cb5\u0cb0\u0ccd\u0c97", "\u0c95\u0ccd\u0caf\u0cbe\u0c9f\u0c97\u0cb0\u0cbf",
        "\u0cb9\u0cc6\u0c9a\u0ccd\u0c9a\u0cc1 \u0c96\u0cb0\u0ccd\u0c9a\u0cbe\u0ca6 \u0cb5\u0cbf\u0cad\u0cbe\u0c97",
        "\u0cb9\u0cc6\u0c9a\u0ccd\u0c9a\u0cc1 \u0c96\u0cb0\u0ccd\u0c9a\u0cbe\u0ca6 \u0cb5\u0cb0\u0ccd\u0c97",
    )
    if any(term in query for term in category_terms):
        requested.add("get_category_breakdown")

    merchant_terms = (
        "merchant", "merchants", "vendor", "vendors", "store", "stores",
        "spend the most with", "spent the most with", "spend most with", "spent most with",
        "top merchant", "top merchants", "which merchant",
        "\u0935\u094d\u092f\u093e\u092a\u093e\u0930\u0940", "\u092e\u0930\u094d\u091a\u0947\u0902\u091f", "\u0cb5\u0ccd\u0caf\u0cbe\u0caa\u0cbe\u0cb0\u0cbf", "\u0cae\u0cb0\u0ccd\u0c9a\u0cc6\u0c82\u0c9f\u0ccd",
    )
    if any(term in query for term in merchant_terms):
        requested.add("get_top_merchants")

    month_mentioned = bool(re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        query,
    ))
    if ("compare" in query or "comparison" in query or "ಹೋಲಿಕೆ" in query or "ಹೋಲಿಸಿ" in query or "तुलना" in query) and (month_mentioned or "month" in query or "ತಿಂಗಳು" in query or "महीने" in query):
        requested.add("compare_monthly_spending")

    if month_mentioned and any(term in query for term in ("credit", "debit", "income", "received", "spent")) and "compare_monthly_spending" not in requested and ("get_category_breakdown" not in requested or any(term in query for term in ("credit", "income", "received"))):
        requested.add("get_monthly_transaction_summary")

    if any(term in query for term in (
        "highest transaction", "largest transaction", "largest debit", "highest debit",
        "five highest", "top five debit", "top 5 debit", "largest expenses", "biggest transaction", "top expenses",
    )):
        requested.add("find_large_transactions")

    if any(term in query for term in (
        "savings rate", "estimated savings", "how much did i save", "savings for", "savings percentage",
        "\u092c\u091a\u0924 \u0926\u0930", "\u0c89\u0cb3\u0cbf\u0ca4\u0cbe\u0caf \u0ca6\u0cb0",
    )):
        requested.add("calculate_savings_rate")

    if any(term in query for term in ("unusual transaction", "unusual transactions", "unusual spending", "unusual expense", "suspicious expense")):
        requested.add("detect_unusual_spending")

    spending_summary_explicit = any(term in query for term in (
        "analyze my spending", "analyse my spending", "spending analysis", "analyse spending", "analyze spending",
        "spending summary", "summarize my spending", "summary of my spending", "spending pattern", "spending overview",
        "how much did i spend", "how much have i spent", "total spending", "total spent",
        "all-time credits", "all time credits", "net cash flow", "overall cash flow", "cash flow", "money out", "debits",
        "\u0916\u0930\u094d\u091a \u0915\u093e \u0935\u093f\u0936\u094d\u0932\u0947\u0937\u0923", "\u0915\u0941\u0932 \u0916\u0930\u094d\u091a",
        "\u0916\u0930\u094d\u091a \u0938\u093e\u0930\u093e\u0902\u0936", "\u0c96\u0cb0\u0ccd\u0c9a\u0cc1 \u0cb5\u0cbf\u0cb6\u0ccd\u0cb2\u0cc7\u0cb7\u0ca3\u0cc6",
        "\u0c92\u0c9f\u0ccd\u0c9f\u0cc1 \u0c96\u0cb0\u0ccd\u0c9a\u0cc1", "\u0c96\u0cb0\u0ccd\u0c9a\u0cbf\u0ca8 \u0cb8\u0cbe\u0cb0\u0cbe\u0c82\u0cb6",
    ))
    spending_general = any(term in query for term in (
        "spending", "spend", "spent", "expense", "expenses",
        "\u0916\u0930\u094d\u091a", "\u0c96\u0cb0\u0ccd\u0c9a\u0cc1",
    ))
    if spending_summary_explicit:
        requested.add("get_spending_summary")
    elif spending_general:
        if not (requested & {"get_category_breakdown", "get_top_merchants", "find_large_transactions", "generate_budget_recommendations"}):
            requested.add("get_spending_summary")

    personal_data = any(term in query for term in (
        "my", "mine", "mera", "meri", "mere", "nanna", "ನನ್ನ", "मेरे", "मेरा", "मेरी",
    )) or bool(re.search(r"\u092e\u0947\u0930\u093e|\u092e\u0947\u0930\u0940|\u092e\u0947\u0930\u0947|\u0ca8\u0ca8\u0ccd\u0ca8", query))

    card_personal = personal_data or any(term in query for term in (
        "is my debit", "is my credit", "status of debit", "status of credit",
        "how many cards", "cards do i have", "which card", "my card status",
        "card details", "cards", "show cards", "card status", "what card", "debit card", "credit card",
    ))
    if any(term in query for term in ("card", "debit card", "credit card", "atm", "\u0915\u093e\u0930\u094d\u0921", "\u0c95\u0cbe\u0cb0\u0ccd\u0ca1\u0ccd")) and card_personal:
        requested.add("get_card_details")

    loan_personal = personal_data or any(term in query for term in (
        "my education loan", "my loan", "my outstanding", "my interest rate",
        "explain my loan", "my loan status", "loan details", "loans", "show loans", "what loan", "loan status", "emi",
    ))
    if any(term in query for term in ("loan", "education loan", "outstanding amount", "interest rate", "tenure", "emi", "\u0932\u094b\u0928", "\u090b\u0923", "\u0cb8\u0cbe\u0cb2")) and loan_personal:
        requested.add("get_loan_details")

    transaction_explicit = any(term in query for term in (
        "recent transaction", "recent transactions", "recent payments", "payment history",
        "latest transactions", "last transactions", "transaction history", "show transactions", "my transactions",
        "show my recent payments", "recent payments",
        "\u0932\u0947\u0928\u0926\u0947\u0928", "\u0cb5\u0ccd\u0caf\u0cb5\u0cb9\u0cbe\u0cb0",
    ))
    if transaction_explicit:
        requested.add("get_recent_transactions")
    elif any(term in query for term in ("transaction", "transactions", "payment", "payments")) and not (
        requested & {"get_spending_summary", "get_category_breakdown", "get_top_merchants", "find_large_transactions", "generate_budget_recommendations"}
    ):
        requested.add("get_recent_transactions")

    balance_explicit = any(term in query for term in (
        "balance", "account balance", "current balance", "available balance", "bank balance", "check balance", "how much balance",
        "how much money do i have", "how much money is in", "how much is in my account", "how much in my account",
        "how much do i have in savings", "how much in savings", "money in my account", "funds in my account",
        "my balance", "show balance", "account status", "show accounts", "show my accounts", "my accounts",
        "\u092c\u0948\u0932\u0947\u0902\u0938", "\u0936\u0947\u0937 \u0930\u093e\u0936\u093f", "\u0916\u093e\u0924\u0947 \u092e\u0947\u0902 \u0915\u093f\u0924\u0928\u0947 \u092a\u0948\u0938\u0947",
        "\u0916\u093e\u0924\u093e \u0936\u0947\u0937", "\u0916\u093e\u0924\u093e \u0935\u093f\u0935\u0930\u0923",
        "\u0cac\u0ccd\u0caf\u0cbe\u0cb2\u0cc6\u0ca8\u0ccd\u0cb8\u0ccd", "\u0c96\u0cbe\u0ca4\u0cc6\u0caf\u0cb2\u0ccd\u0cb2\u0cbf \u0c8e\u0cb7\u0ccd\u0c9f\u0cc1 \u0cb9\u0ca3",
        "\u0c96\u0cbe\u0ca4\u0cc6 \u0cac\u0cbe\u0c95\u0cbf", "\u0cac\u0cbe\u0c95\u0cbf \u0cb9\u0ca3",
    )) or bool(re.search(
        r"\b(?:what(?:'s| is)|show|check|tell)\s+(?:me\s+)?(?:my\s+)?(?:current\s+)?(?:account\s+)?balance\b",
        query,
    )) or bool(re.search(
        r"\bhow much\s+(?:money\s+)?(?:do i have\s+)?in\s+(?:my\s+)?savings\b",
        query,
    ))
    if not balance_explicit and personal_data and any(term in query for term in (
        "account number", "account status", "account details", "account type",
        "savings account", "current account",
    )) and "generate_budget_recommendations" not in requested:
        balance_explicit = True

    if balance_explicit:
        requested.add("get_account_balance")

    return frozenset(requested)


def _transaction_amount_filter(query):
    match = re.search(
        r"\b(above|over|more than|greater than|at least|below|under|less than)\s+"
        r"(?:inr\s*|rs\.?\s*|₹\s*)?([0-9][0-9,]*(?:\.\d{1,2})?)",
        query.casefold(),
    )
    if not match:
        return None
    comparator = match.group(1)
    value = float(match.group(2).replace(",", ""))
    field = "max_amount" if comparator in {"below", "under", "less than"} else "min_amount"
    # Amounts are stored to paise precision. Return the requested threshold for
    # model-argument validation plus a strictness flag; execution adjusts only
    # strict comparisons by one paise.
    strict = comparator != "at least"
    return field, value, strict


def _is_latest_transaction_risk_request(text: str) -> bool:
    query = text.casefold()
    return any(term in query for term in (
        "latest transaction", "most recent transaction", "last transaction",
        "latest payment", "most recent payment", "check my latest",
        "मेरे सबसे हाल के लेनदेन", "सबसे नया लेनदेन",
        "ನನ್ನ ಇತ್ತೀಚಿನ ವ್ಯವಹಾರ", "ಇತ್ತೀಚಿನ ವಹಿವಾಟು",
    ))


def _transaction_risk_assessment_from_results(tool_results):
    """Extract authoritative tool data for the structured turn response."""
    for result in reversed(tool_results):
        if result.status != "succeeded" or result.tool_name not in {
            "assess_transaction_risk", "get_transaction_risk_assessment",
        }:
            continue
        data = result.data or {}
        if result.tool_name == "assess_transaction_risk":
            data = {
                "assessment_id": data.get("assessment_id"),
                "transaction_ref": (data.get("transaction") or {}).get("transaction_ref"),
                "transaction": data.get("transaction"),
                "assessment": data.get("assessment"),
                "created_at": data.get("created_at"),
            }
        try:
            return TransactionRiskAssessmentDetail.model_validate(data)
        except Exception:
            # A malformed service result must never be presented as an
            # authoritative structured assessment.
            return None
    return None

# Banking tools are owner-scoped server functions. Only masked card and loan
# projections are returned; customer identity never comes from tool arguments.
LLM_TOOL_NAMES = frozenset({
    "get_account_balance", "get_recent_transactions", "search_transactions",
    "get_transaction_details",
    "get_card_details", "get_loan_details",
    "calculate_emi", "get_spending_summary", "get_category_breakdown",
    "get_monthly_transaction_summary",
    "get_top_merchants", "find_large_transactions", "compare_monthly_spending",
    "calculate_savings_rate", "detect_unusual_spending",
    "generate_budget_recommendations", "generate_financial_advisory_report",
    "assess_current_scam_message", "get_scam_assessment",
    "list_support_tickets", "get_support_ticket", "get_support_ticket_events",
    "propose_support_ticket", "prepare_support_ticket_close",
    "assess_transaction_risk", "list_transaction_risk_assessments",
    "get_transaction_risk_assessment",
    "list_investigations", "get_investigation", "propose_create_investigation",
    "propose_link_investigation_evidence", "propose_investigation_note",
    "propose_investigation_status", "propose_close_investigation",
}) | ACCOUNT_APPLICATION_TOOL_NAMES
_APPLICATION_REF_SCHEMA = {"type": "string", "pattern": r"^VSA-[A-F0-9]{16}$"}
_ACCOUNT_TYPE_SCHEMA = {"type": "string", "enum": ["SAVINGS", "SALARY", "CURRENT"]}
_TOOL_PARAMETERS = {
    "get_recent_transactions": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
    "search_transactions": {
        "merchant": {"type": "string", "maxLength": 160},
        "category": {"type": "string", "maxLength": 80},
        "transaction_type": {"type": "string", "enum": ["DEBIT", "CREDIT"]},
        "min_amount": {"type": "number", "minimum": 0},
        "max_amount": {"type": "number", "minimum": 0},
        "start_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "end_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    },
    "get_transaction_details": {
        "transaction_ref": {"type": "string", "pattern": r"^TR-[A-F0-9]{48}$"},
    },
    "calculate_emi": {
        "principal": {"type": "number", "minimum": 0},
        "annual_interest_rate": {"type": "number", "minimum": 0},
        "tenure_months": {"type": "integer", "minimum": 1},
    },
    "get_top_merchants": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    "find_large_transactions": {
        "threshold": {"type": "number", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}
_TOOL_PARAMETERS.update({
    "assess_transaction_risk": {
        "transaction_ref": {"type": "string", "pattern": r"^TR-[A-F0-9]{48}$"},
    },
    "list_transaction_risk_assessments": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "cursor": {"type": "string", "maxLength": 512},
    },
    "get_transaction_risk_assessment": {
        "assessment_id": {"type": "string", "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"},
    },
    "list_support_tickets": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "status": {"type": "string", "enum": sorted(SUPPORT_STATUSES)},
        "category": {"type": "string", "enum": sorted(SUPPORT_CATEGORIES)},
        "priority": {"type": "string", "enum": sorted(SUPPORT_PRIORITIES)},
    },
    "get_support_ticket": {
        "ticket_ref": {"type": "string", "pattern": r"^VS-[A-F0-9]{32}$"},
    },
    "get_support_ticket_events": {
        "ticket_ref": {"type": "string", "pattern": r"^VS-[A-F0-9]{32}$"},
    },
    "propose_support_ticket": {
        "category": {"type": "string", "enum": sorted(SUPPORT_CATEGORIES)},
        "priority": {"type": "string", "enum": sorted(SUPPORT_PRIORITIES)},
        "subject": {"type": "string", "minLength": 1, "maxLength": 160},
        "description": {"type": "string", "minLength": 1, "maxLength": 4000},
        "language": {"type": "string", "enum": ["kn-IN", "en-IN", "hi-IN"]},
        "amount": {"type": "number", "minimum": 0},
    },
    "prepare_support_ticket_close": {
        "ticket_ref": {"type": "string", "pattern": r"^VS-[A-F0-9]{32}$"},
    },
})
_INVESTIGATION_REF_SCHEMA = {"type": "string", "pattern": r"^INV-[A-F0-9]{32}$"}
_TOOL_PARAMETERS.update({
    "list_investigations": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "cursor": {"type": "string", "maxLength": 512},
        "status": {"type": "string", "enum": ["OPEN", "UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED", "CLOSED"]},
        "category": {"type": "string", "enum": ["MESSAGE_SCAM", "CALL_SCAM", "TRANSACTION_RISK", "ACCOUNT_SECURITY", "OTHER"]},
    },
    "get_investigation": {"investigation_ref": _INVESTIGATION_REF_SCHEMA},
    "propose_create_investigation": {
        "title": {"type": "string", "minLength": 3, "maxLength": 120},
        "category": {"type": "string", "enum": ["MESSAGE_SCAM", "CALL_SCAM", "TRANSACTION_RISK", "ACCOUNT_SECURITY", "OTHER"]},
        "summary": {"type": "string", "minLength": 8, "maxLength": 1500},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "evidence_type": {"type": "string", "enum": ["MESSAGE_ASSESSMENT", "CALL_ASSESSMENT", "TRANSACTION_RISK_ASSESSMENT"]},
            "assessment_id": {"type": "string", "format": "uuid"},
        }, "required": ["evidence_type", "assessment_id"], "additionalProperties": False}, "maxItems": 5},
    },
    "propose_link_investigation_evidence": {
        "investigation_ref": _INVESTIGATION_REF_SCHEMA,
        "evidence_type": {"type": "string", "enum": ["MESSAGE_ASSESSMENT", "CALL_ASSESSMENT", "TRANSACTION_RISK_ASSESSMENT"]},
        "assessment_id": {"type": "string", "format": "uuid"},
    },
    "propose_investigation_note": {"investigation_ref": _INVESTIGATION_REF_SCHEMA, "note": {"type": "string", "minLength": 3, "maxLength": 1500}},
    "propose_investigation_status": {"investigation_ref": _INVESTIGATION_REF_SCHEMA, "status": {"type": "string", "enum": ["UNDER_REVIEW", "WAITING_FOR_INFORMATION", "RESOLVED"]}},
    "propose_close_investigation": {"investigation_ref": _INVESTIGATION_REF_SCHEMA},
    "start_account_application": {"account_type": _ACCOUNT_TYPE_SCHEMA},
    "get_account_application": {"application_ref": _APPLICATION_REF_SCHEMA},
    "update_account_application": {
        "application_ref": _APPLICATION_REF_SCHEMA,
        "account_type": _ACCOUNT_TYPE_SCHEMA,
        "employment_category": {
            "type": "string",
            "enum": [
                "SALARIED", "SELF_EMPLOYED", "BUSINESS_OWNER",
                "STUDENT", "RETIRED", "GOVERNMENT_EMPLOYEE", "OTHER",
            ],
        },
    },
    "validate_account_application": {"application_ref": _APPLICATION_REF_SCHEMA},
    "get_required_application_documents": {"account_type": _ACCOUNT_TYPE_SCHEMA},
    "get_application_document_status": {"application_ref": _APPLICATION_REF_SCHEMA},
    "prepare_account_application_submission": {"application_ref": _APPLICATION_REF_SCHEMA},
    "get_account_application_status": {
        "application_ref": _APPLICATION_REF_SCHEMA,
        "account_type": _ACCOUNT_TYPE_SCHEMA,
    },
})
for _tool_name in (
    "get_monthly_transaction_summary", "compare_monthly_spending", "calculate_savings_rate", "detect_unusual_spending",
    "generate_budget_recommendations", "generate_financial_advisory_report",
):
    _TOOL_PARAMETERS[_tool_name] = {
        "year": {"type": "integer", "minimum": 2000, "maximum": 2100},
        "month": {"type": "integer", "minimum": 1, "maximum": 12},
    }


def _llm_tool_catalog():
    catalog = []
    for definition in get_available_tools():
        if definition["name"] not in LLM_TOOL_NAMES:
            continue
        properties = _TOOL_PARAMETERS.get(definition["name"], {})
        tool_name = definition["name"]
        if tool_name == "calculate_emi":
            required = ["principal", "annual_interest_rate", "tenure_months"]
        elif tool_name in {
            "get_monthly_transaction_summary", "compare_monthly_spending", "calculate_savings_rate", "detect_unusual_spending",
            "generate_budget_recommendations", "generate_financial_advisory_report",
        }:
            required = ["year", "month"]
        elif tool_name in {"get_support_ticket", "get_support_ticket_events", "prepare_support_ticket_close"}:
            required = ["ticket_ref"]
        elif tool_name == "get_transaction_details":
            required = ["transaction_ref"]
        elif tool_name == "propose_support_ticket":
            required = ["category", "priority", "subject"]
        elif tool_name == "assess_transaction_risk":
            required = ["transaction_ref"]
        elif tool_name == "get_transaction_risk_assessment":
            required = ["assessment_id"]
        elif tool_name == "get_investigation":
            required = ["investigation_ref"]
        elif tool_name == "propose_create_investigation":
            required = ["title", "category", "summary"]
        elif tool_name == "propose_link_investigation_evidence":
            required = ["investigation_ref", "evidence_type", "assessment_id"]
        elif tool_name == "propose_investigation_note":
            required = ["investigation_ref", "note"]
        elif tool_name == "propose_investigation_status":
            required = ["investigation_ref", "status"]
        elif tool_name == "propose_close_investigation":
            required = ["investigation_ref"]
        else:
            required = []
        schema = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        description = definition["description"]
        if definition["name"] == "find_large_transactions":
            description = (
                "Find successful debit transactions ordered largest first. Set threshold to 0 "
                "when asked for the highest transaction or the largest expenses."
            )
        catalog.append(LLMTool(definition["name"], description, schema))
    return tuple(catalog)


def _validate_llm_arguments(tool_name: str, arguments: dict[str, Any]) -> bool:
    schema = next((tool.parameters for tool in _llm_tool_catalog() if tool.name == tool_name), None)
    if schema is None or set(arguments) - set(schema.get("properties", {})):
        return False
    if set(schema.get("required", [])) - set(arguments):
        return False
    for key, value in arguments.items():
        field = schema["properties"][key]
        expected = field["type"]
        valid_type = (
            isinstance(value, str) if expected == "string" else
            isinstance(value, int) and not isinstance(value, bool) if expected == "integer" else
            isinstance(value, (int, float)) and not isinstance(value, bool) if expected == "number" else
            isinstance(value, list) if expected == "array" else
            False
        )
        if not valid_type or ("minimum" in field and value < field["minimum"]) or ("maximum" in field and value > field["maximum"]):
            return False
        if "enum" in field and value not in field["enum"]:
            return False
        if expected == "array":
            if "maxItems" in field and len(value) > field["maxItems"]:
                return False
            item_schema = field.get("items", {})
            if item_schema.get("type") == "object":
                for item in value:
                    if not isinstance(item, dict) or set(item) - set(item_schema.get("properties", {})):
                        return False
                    if set(item_schema.get("required", [])) - set(item):
                        return False
                    for item_key, item_value in item.items():
                        item_field = item_schema["properties"][item_key]
                        if not isinstance(item_value, str):
                            return False
                        if "enum" in item_field and item_value not in item_field["enum"]:
                            return False
                        if item_field.get("format") == "uuid":
                            try:
                                UUID(item_value)
                            except (ValueError, TypeError, AttributeError):
                                return False
        if expected == "string":
            if "minLength" in field and len(value) < field["minLength"]:
                return False
            if "maxLength" in field and len(value) > field["maxLength"]:
                return False
            if "pattern" in field and not re.fullmatch(field["pattern"], value):
                return False
            if field.get("format") == "uuid":
                try:
                    UUID(value)
                except (ValueError, TypeError, AttributeError):
                    return False
    return True


class FeatureUnavailable(AssistantServiceError):
    pass


class IntentRouter(Protocol):
    def route(self, text: str) -> tuple[str, str, str, dict[str, Any]]: ...


class RuleBasedIntentRouter:
    """Temporary keyword routing; replaceable by an Evon-backed router later."""

    RULES = (
        ("banking", "confirmation_required", ("block my card", "block card", "transfer money", "create a ticket", "investigate transaction", "close my account")),
        ("financial_intelligence", "budget_recommendations", ("budget", "ಬಜೆಟ್", "बजट", "ಉಳಿತಾಯ", "बचत")),
        ("financial_intelligence", "monthly_comparison", ("monthly comparison", "compare months", "month over month", "ಹೋಲಿಕೆ", "तुलना")),
        ("financial_intelligence", "merchant_analysis", ("merchant", "ಮರ್ಚೆಂಟ್", "ವ್ಯಾಪಾರಿ", "व्यापारी", "मर्चेंट")),
        ("financial_intelligence", "category_spending", ("category", "ಕ್ಯಾಟಗರಿ", "ವರ್ಗ", "ವಿಭಾಗ", "श्रेणी", "विभाग")),
        ("financial_intelligence", "financial_summary", ("spending", "expenses", "ಖರ್ಚು", "खर्च", "summary")),
        ("scam_detection", "transaction_assessment", ("suspicious transaction", "fraudulent transaction")),
        ("scam_detection", "message_assessment", ("scam", "ಸೈಬರ್", "ಸಂದೇಶ", "फ्रॉड", "स्कैम")),
        ("customer_support", "support_query", ("complaint", "ticket", "support", "ದೂರು", "शिकायत")),
        ("banking", "transaction_search", ("search transactions", "find transaction", "transaction search")),
        ("banking", "transaction_history", ("transaction", "transactions", "history", "ವಹಿವಾಟು", "लेनदेन")),
        ("banking", "account_balance", ("balance", "ಬ್ಯಾಲೆನ್ಸ್", "शेष")),
    )

    def route(self, text: str) -> tuple[str, str, str, dict[str, Any]]:
        normalized = text.casefold()
        for feature, intent, phrases in self.RULES:
            if any(phrase.casefold() in normalized for phrase in phrases):
                if intent == "budget_recommendations":
                    current = datetime.now(timezone.utc)
                    args = {"year": current.year, "month": current.month}
                elif intent == "monthly_comparison":
                    current = datetime.now(timezone.utc)
                    args = {"year": current.year, "month": current.month}
                else:
                    args = {}
                return feature, intent, _tool_name(intent), args
        return "banking", "general_banking_query", "assistant_help", {}


def _tool_name(intent: str) -> str:
    return {
        "account_balance": "get_account_balance",
        "transaction_history": "get_recent_transactions",
        "transaction_search": "search_transactions",
        "financial_summary": "get_spending_summary",
        "category_spending": "get_category_breakdown",
        "merchant_analysis": "get_top_merchants",
        "budget_recommendations": "generate_budget_recommendations",
        "monthly_comparison": "compare_monthly_spending",
    }.get(intent, intent)


def _safe_value(value):
    """Remove internal identity fields recursively before model/API exposure."""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in {
                "customer_id", "customer_key", "internal_id", "account_id",
                "transaction_id", "card_id", "loan_id", "investigation_id",
                "evidence_id", "event_id", "application_id", "document_id",
                "storage_id", "review_digest",
            }:
                continue
            if normalized in {"account_number", "masked_account_number"}:
                digits = "".join(character for character in str(item) if character.isdigit())
                safe["masked_account_number"] = f"••••{digits[-4:]}" if digits else None
                continue
            safe[str(key)] = _safe_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class AgentToolFeatureHandler:
    """Adapter to existing allow-listed local banking/financial read tools."""

    TOOLS = {
        "banking": {"account_balance", "transaction_history", "transaction_search"},
        "financial_intelligence": {
            "financial_summary", "category_spending", "merchant_analysis",
            "budget_recommendations", "monthly_comparison",
        },
    }

    def supports(self, feature: str, intent: str) -> bool:
        return intent in self.TOOLS.get(feature, set())

    def execute(self, principal, intent, tool_name, arguments):
        result = execute_tool(tool_name, principal, arguments)
        return _safe_value(result)


class FeatureRegistry:
    def __init__(self):
        self._handlers: dict[str, list[Any]] = {}

    def register(self, feature: str, handler):
        self._handlers.setdefault(feature, []).append(handler)

    def dispatch(self, feature, intent, principal, tool_name, arguments):
        for handler in self._handlers.get(feature, []):
            if handler.supports(feature, intent):
                return handler.execute(principal, intent, tool_name, arguments)
        raise FeatureUnavailable


def _script_languages(text: str) -> tuple[str, ...]:
    found = set()
    for character in text:
        code = ord(character)
        if 0x0C80 <= code <= 0x0CFF:
            found.add("kn-IN")
        elif 0x0900 <= code <= 0x097F:
            found.add("hi-IN")
        elif character.isascii() and character.isalpha():
            found.add("en-IN")
    return tuple(language for language in ("kn-IN", "en-IN", "hi-IN") if language in found)


_BANKING_CODE_MIX_LOANWORDS = frozenset({
    "upi", "transaction", "transactions", "account", "accounts", "debit", "debited",
    "credit", "credited", "subject", "priority", "category", "description",
    "high", "medium", "low", "urgent", "payment", "payments", "failed", "fail",
    "merchant", "bank", "card", "cards", "loan", "loans", "emi", "balance",
    "status", "ticket", "complaint", "support", "otp", "pin", "cvv", "inr", "rs",
    "swiggy", "zomato", "amazon", "flipkart", "dmart", "bigbasket", "apollo",
    "bescom", "jio", "airtel", "uber", "ola", "netflix",
})


def _preferred_response_language(text: str, languages=()) -> str:
    """Choose a cautious automatic response language for supported scripts and transliteration."""
    script_counts = {"kn-IN": 0, "hi-IN": 0, "en-IN": 0}
    for character in text:
        code = ord(character)
        if 0x0C80 <= code <= 0x0CFF:
            script_counts["kn-IN"] += 1
        elif 0x0900 <= code <= 0x097F:
            script_counts["hi-IN"] += 1
        elif character.isascii() and character.isalpha():
            script_counts["en-IN"] += 1
    ascii_words = re.findall(r"[a-z]+", text.casefold())
    en_non_loanword_chars = sum(
        len(word) for word in ascii_words if word not in _BANKING_CODE_MIX_LOANWORDS
    )
    native = max(("kn-IN", "hi-IN"), key=lambda item: script_counts[item])
    if script_counts[native] >= 3 and (
        script_counts[native] >= script_counts["en-IN"]
        or script_counts[native] >= en_non_loanword_chars
    ):
        return native

    words = set(ascii_words)
    hindi = len(words & {"mera", "meri", "mere", "mein", "hai", "hain", "kitna", "kya", "dikhao", "batao", "kripya", "mujhe", "ka", "ki", "ke"})
    kannada = len(words & {"nanna", "nimma", "alli", "ide", "beku", "eshtu", "yenu", "helri", "torisi", "maadi", "ge", "inda", "kodi", "agide"})
    if hindi >= 2 and hindi > kannada:
        return "hi-IN"
    if kannada >= 2 and kannada > hindi:
        return "kn-IN"
    if languages and len(set(languages)) == 1:
        return languages[0]
    return "en-IN"


def _response_language_for_text(text: str, requested: str) -> str:
    """Report the dominant observable response language/script, not the request hint."""
    counts = {"kn-IN": 0, "hi-IN": 0, "en-IN": 0}
    for char in text:
        code = ord(char)
        if 0x0C80 <= code <= 0x0CFF:
            counts["kn-IN"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["hi-IN"] += 1
        elif char.isascii() and char.isalpha():
            counts["en-IN"] += 1
    if requested in {"kn-IN", "hi-IN"} and counts[requested] >= 3:
        return requested
    dominant = max(counts, key=counts.get)
    if counts[dominant] >= 3 and counts[dominant] >= max(counts.values()) * 0.6:
        return dominant
    words = set(re.findall(r"[a-z]+", text.casefold()))
    hindi = len(words & {"hai", "hain", "aap", "mera", "meri", "kripya", "kijiye", "nahi", "ke", "ki"})
    kannada = len(words & {"nanna", "nimma", "ide", "beku", "eshtu", "maadi", "illa", "dayavittu", "sari"})
    if hindi >= 2 and hindi > kannada:
        return "hi-IN"
    if kannada >= 2 and kannada > hindi:
        return "kn-IN"
    return "en-IN" if counts["en-IN"] else requested


def speech_friendly_text(text: str) -> str:
    """Convert visual Markdown into a compact, natural TTS representation."""
    lines = str(text or "").splitlines()
    rendered = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-+]\s+|\d+[.)]\s+)", "", line)
        if "|" in line and index + 1 < len(lines) and re.fullmatch(r"\|?[\s:|\-]+\|?", lines[index + 1].strip()):
            headers = [cell.strip() for cell in line.strip("|").split("|")]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                parts = [f"{headers[i]}: {cells[i]}" for i in range(min(len(headers), len(cells))) if cells[i]]
                if parts:
                    rendered.append(". ".join(parts) + ".")
                index += 1
            continue
        if line.startswith("```"):
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                index += 1
            index += 1
            continue
        rendered.append(line)
        index += 1
    value = " ".join(rendered)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_~`>#]", "", value)
    def readable_date(match):
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%B %d, %Y")
        except ValueError:
            return match.group(1)
    value = re.sub(
        r"\b(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        readable_date, value,
    )
    return re.sub(r"\s+", " ", value).strip()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class AssistantOrchestrator:
    def __init__(
        self,
        *,
        intent_router: IntentRouter | None = None,
        registry: FeatureRegistry | None = None,
        transcriber: SpeechTranscriber | None = None,
        synthesizer: SpeechSynthesizer | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self.intent_router = intent_router or RuleBasedIntentRouter()
        self.registry = registry or FeatureRegistry()
        if registry is None:
            self.registry.register("banking", AgentToolFeatureHandler())
            self.registry.register("financial_intelligence", AgentToolFeatureHandler())
        self.transcriber = transcriber or create_speech_transcriber()
        self.synthesizer = synthesizer or create_speech_synthesizer()
        self._provider_settings = get_provider_settings()
        self._llm_provider_error = None
        if llm_provider is not None:
            self.llm_provider = llm_provider
            self._provider_settings = {
                "provider": getattr(llm_provider, "provider_name", type(llm_provider).__name__.casefold()),
                "model": getattr(llm_provider, "model_name", getattr(llm_provider, "model", "injected")),
                "credential_configured": True,
            }
        else:
            try:
                self.llm_provider = create_llm_provider(self._provider_settings)
            except LLMProviderError as exc:
                # Defer configuration failures until a user turn can be safely
                # persisted, rather than making API import/startup fail.
                self.llm_provider = None
                self._llm_provider_error = exc

    def provider_diagnostics(self):
        """Return safe startup diagnostics for the configured provider."""
        return dict(self._provider_settings)

    def process_turn(self, principal: CustomerPrincipal, request: AssistantTurnRequest) -> AssistantTurnResponse:
        correlation_id = uuid4()
        settings = self.provider_diagnostics()
        _trace_event(correlation_id, "turn_started")
        _trace_event(correlation_id, "provider_selected", **settings)
        try:
            response = self._process_turn(principal, request, correlation_id)
        except Exception as exc:
            _trace_event(correlation_id, "turn_failed", error_type=type(exc).__name__)
            raise
        _trace_event(correlation_id, "turn_completed")
        return response

    def _llm_response(
        self, principal, conversation_id, response_language, input_text,
        correlation_id, *, assessment_reference=None, input_languages=(),
        is_code_mixed=False, source_mode="typed_message", original_turn_id=None,
        doc_context="", has_attachments=False,
    ):
        # Explicit ticket creation takes precedence over contextual banking
        # follow-up routing, including lookups inherited from earlier turns.
        support_create_active = _is_support_create_request(input_text)
        required_tools = (
            frozenset() if (support_create_active or has_attachments)
            else _required_tools_for_question(input_text, assessment_reference)
        )
        transaction_risk_tools = {
            "assess_transaction_risk", "list_transaction_risk_assessments",
            "get_transaction_risk_assessment",
        }
        transaction_risk_required = bool(required_tools & transaction_risk_tools)
        investigation_read_tools = {"list_investigations", "get_investigation"}
        investigation_mutation_tools = {
            "propose_create_investigation", "propose_link_investigation_evidence",
            "propose_investigation_note", "propose_investigation_status",
            "propose_close_investigation",
        }
        investigation_tools = investigation_read_tools | investigation_mutation_tools
        investigation_request = bool(
            re.search(r"\b(?:investigation|investigations|security case|security report)\b", input_text, re.I)
            or re.search(r"\bINV-[A-F0-9]{32}\b", input_text, re.I)
            or re.search(r"\b(?:track this scam|track this incident|security report)\b", input_text, re.I)
        )
        investigation_mutation_request = investigation_request and bool(re.search(
            r"\b(?:create|open|start|track|add|link|update|change|mark|resolve|close|attach)\b", input_text, re.I
        ))
        investigation_required = bool(required_tools & investigation_read_tools)
        latest_transaction_risk = _is_latest_transaction_risk_request(input_text)
        scam_tool_required = bool(required_tools & {
            "assess_current_scam_message", "get_scam_assessment"
        })
        if (
            scam_tool_required
            and assessment_reference is None
            and _SCAM_REQUEST_ONLY.fullmatch(input_text)
        ):
            return (
                "Please share the suspicious message or the relevant words from the call so I can assess that content. Do not include any OTP, PIN, password, or other secret.",
                [], None, None, None,
            )
        # The user turn is persisted before this call, so the recent-turn query
        # already includes the current input exactly once. Do not append it again.
        history = conversation_service.get_recent_conversation_turns(
            principal, conversation_id, limit=_history_turn_limit()
        )

        from services.support_draft_service import (
            get_active_draft,
            save_or_update_draft,
            mark_proposed,
            mark_submitted,
            cancel_draft,
        )
        active_draft = get_active_draft(principal.customer_key, str(conversation_id))
        if active_draft is not None:
            support_create_active = True
            required_tools = frozenset()

        support_context_start = 0 if active_draft else max(0, len(history) - 1)
        if not active_draft:
            for index, turn in enumerate(history):
                customer_text = str(turn.get("user_text_or_transcript") or "")
                if turn.get("role") == "user" and _is_support_create_request(customer_text):
                    support_context_start = index
                    break

        customer_context_text = " ".join(
            str(turn.get("user_text_or_transcript") or "")
            for turn in history[support_context_start:]
            if turn.get("role") == "user"
        )
        full_support_context = " ".join(
            str(turn.get("user_text_or_transcript") or turn.get("assistant_response_text") or "")
            for turn in history[support_context_start:]
        )

        turn_slots = _extract_support_ticket_slots(input_text)
        context_slots = _extract_support_ticket_slots(full_support_context)
        merged_cat = turn_slots.get("category") or (active_draft.get("category") if active_draft else None) or context_slots.get("category")
        merged_prio = turn_slots.get("priority") or (active_draft.get("priority") if active_draft else None) or context_slots.get("priority")
        merged_subj = turn_slots.get("subject") or (active_draft.get("subject") if active_draft else None) or context_slots.get("subject")
        merged_amt = turn_slots.get("amount") or (active_draft.get("amount") if active_draft else None) or context_slots.get("amount")
        merged_debit = turn_slots.get("debit_confirmed") or (active_draft.get("debit_confirmed") if active_draft else None) or context_slots.get("debit_confirmed")
        merged_desc = turn_slots.get("description") or (active_draft.get("description") if active_draft else None) or context_slots.get("description")

        if support_create_active and (merged_cat or merged_prio or merged_subj or merged_amt or merged_debit):
            if not merged_desc and merged_subj:
                amt_str = f" Amount: ₹{merged_amt:,.2f}." if merged_amt else ""
                if "merchant" in merged_subj.lower() or "upi" in merged_subj.lower():
                    merged_desc = f"Amount deducted from the customer's account, but merchant has not received payment.{amt_str}"
                else:
                    merged_desc = f"Customer reported: {merged_subj}.{amt_str}"
            active_draft = save_or_update_draft(
                principal.customer_key,
                str(conversation_id),
                category=merged_cat,
                priority=merged_prio,
                subject=merged_subj,
                description=merged_desc,
                amount=merged_amt,
                debit_confirmed=merged_debit,
            )

        # Check for explicit verbal confirmation of an active proposed draft
        is_explicit_confirm = any(phrase in input_text.casefold() for phrase in (
            "ಹೌದು, ಸಲ್ಲಿಸು", "ಸಲ್ಲಿಸು", "ದೃಢೀಕರಿಸು", "ಖಚಿತಪಡಿಸು", "ಹಾಕು", "ಮುಂದುವರಿಸು",
            "हाँ, सबमिट", "सबमिट करो", "सबमिट करें", "पुष्टि करें", "दर्ज करें",
            "confirm", "yes submit", "please submit", "submit the ticket", "submit ticket", "proceed",
        ))
        if is_explicit_confirm and active_draft and active_draft.get("status") == "proposed" and active_draft.get("confirmation_id"):
            from services.pending_action_service import confirm_pending_action
            from backend.api_models import ConfirmationRequest
            try:
                conn = get_connection()
                p_row = conn.execute(
                    "SELECT original_turn_id FROM pending_actions WHERE confirmation_id = ? AND status = 'pending'",
                    (active_draft["confirmation_id"],),
                ).fetchone()
                conn.close()
                if p_row:
                    conf_res = confirm_pending_action(
                        principal,
                        active_draft["confirmation_id"],
                        ConfirmationRequest(
                            conversation_id=UUID(str(conversation_id)),
                            original_turn_id=UUID(str(p_row["original_turn_id"])),
                            action_type="create_support_ticket",
                            confirmation_method="voice",
                            confirmed=True,
                        ),
                    )
                    if conf_res.status == "executed":
                        ticket_ref = "ticket"
                        if conf_res.action_result:
                            if isinstance(conf_res.action_result, dict):
                                ticket_ref = (conf_res.action_result.get("data") or {}).get("ticket_ref") or "ticket"
                            elif hasattr(conf_res.action_result, "data") and isinstance(conf_res.action_result.data, dict):
                                ticket_ref = conf_res.action_result.data.get("ticket_ref") or "ticket"
                        mark_submitted(principal.customer_key, str(conversation_id), ticket_ref)
                        if response_language == "kn-IN":
                            msg = f"ನಿಮ್ಮ ಬೆಂಬಲ ಟಿಕೆಟ್ {ticket_ref} ಯಶಸ್ವಿಯಾಗಿ ರಚಿಸಲಾಗಿದೆ. ನಮ್ಮ ಗ್ರಾಹಕ ಬೆಂಬಲ ತಂಡವು ಶೀಘ್ರದಲ್ಲೇ ಪರಿಶೀಲಿಸಲಿದೆ."
                        elif response_language == "hi-IN":
                            msg = f"आपकी सहायता टिकट {ticket_ref} सफलतापूर्वक बना दी गई है। हमारी टीम जल्द ही इसकी समीक्षा करेगी।"
                        else:
                            msg = f"Your support ticket {ticket_ref} has been created successfully. Our support team will review it shortly."
                        return msg, [], None, None, None
            except Exception as exc:
                logger.warning("Could not execute verbal ticket confirmation: %s", exc)

        # Check for user asking to re-view/re-display details of active draft
        is_cant_see_details = any(phrase in input_text.casefold() for phrase in (
            "ದೂರಿನ ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ವಿವರಗಳು ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ವಿವರಗಳು ಕಾಣುತ್ತಿಲ್ಲ", "ವಿವರ ಕಾಣಿಸುತ್ತಿಲ್ಲ",
            "ಕಾಣಿಸುತ್ತಿಲ್ಲ", "ಕಾಣುತ್ತಿಲ್ಲ",
            "शिकायत का विवरण दिखाई नहीं दे रहा", "विवरण दिखाई नहीं दे रहा", "विवरण नहीं दिख रहा",
            "cannot see the complaint details", "can't see the complaint details",
            "cannot see the details", "can't see the details", "details are not visible",
            "where are the complaint details", "where are the ticket details",
        ))
        if is_cant_see_details and active_draft:
            subj = active_draft.get("subject") or "Support ticket"
            cat = active_draft.get("category") or "transaction"
            prio = active_draft.get("priority") or "HIGH"
            amt = active_draft.get("amount")
            desc = active_draft.get("description") or f"Customer reported: {subj}."
            text = _support_proposal_text(
                subj, response_language,
                category=cat, priority=prio, amount=amt, description=desc,
            )
            pending_action = None
            if active_draft.get("confirmation_id"):
                try:
                    conn = get_connection()
                    p_row = conn.execute(
                        "SELECT * FROM pending_actions WHERE confirmation_id = ? AND status = 'pending'",
                        (active_draft["confirmation_id"],),
                    ).fetchone()
                    conn.close()
                    if p_row:
                        pending_action = PendingAction(
                            confirmation_id=UUID(p_row["confirmation_id"]),
                            conversation_id=UUID(p_row["conversation_id"]),
                            original_turn_id=UUID(p_row["original_turn_id"]),
                            summary=f"Create synthetic support ticket: {subj}",
                            action=SupportTicketCreateAction.model_validate(json.loads(p_row["action_json"])),
                            expires_at=datetime.fromisoformat(p_row["expires_at"].replace("Z", "+00:00")),
                        )
                except Exception:
                    pass
            return text, [], None, pending_action, None
        current_user_turn = None
        if original_turn_id:
            current_user_turn = next(
                (turn for turn in history if str(turn.get("turn_id")) == str(original_turn_id)),
                None,
            )
        if current_user_turn is None:
            current_user_turn = next(
                (turn for turn in reversed(history) if turn.get("role") == "user"),
                None,
            )
        current_user_seq = current_user_turn.get("sequence_number") if current_user_turn else None

        messages = []
        for turn in history:
            if turn.get("role") == "assistant":
                if turn.get("processing_status") == "processing":
                    continue
                if turn.get("assistant_response_text") == "Retrying the saved request.":
                    continue
                if current_user_seq is not None and turn.get("sequence_number", 0) > current_user_seq:
                    continue
            text = turn.get("user_text_or_transcript") if turn.get("role") == "user" else turn.get("assistant_response_text")
            if text:
                messages.append(LLMMessage(
                    role="user" if turn.get("role") == "user" else "assistant",
                    content=text,
                ))
        if not support_create_active and not required_tools and re.search(
            r"\b(?:what about (?:it|that|the same)|is it|are they|does it|do they|"
            r"why is it|how about (?:it|that)|and (?:is|are|what about)|"
            r"active|blocked|status)\b", input_text, re.IGNORECASE,
        ):
            prior_user_turns = [turn for turn in history[:-1] if turn.get("role") == "user"]
            for previous in reversed(prior_user_turns):
                inherited = _required_tools_for_question(
                    str(previous.get("user_text_or_transcript") or "")
                )
                if inherited:
                    required_tools = inherited
                    transaction_risk_required = bool(required_tools & transaction_risk_tools)
                    investigation_required = bool(required_tools & investigation_read_tools)
                    latest_transaction_risk = _is_latest_transaction_risk_request(input_text)
                    scam_tool_required = bool(required_tools & {
                        "assess_current_scam_message", "get_scam_assessment"
                    })
                    break
        support_create_active = _is_support_create_request(input_text, messages)
        support_ticket_ref = re.search(
            r"\bVS-[A-F0-9]{32}\b", input_text, flags=re.IGNORECASE
        )
        support_staff_question = _support_staff_workflow_question(input_text)
        support_reads_for_turn = required_tools & {
            "list_support_tickets", "get_support_ticket", "get_support_ticket_events",
        }
        if support_staff_question and (
            support_ticket_ref is None or not support_reads_for_turn
        ):
            return _support_unavailable_workflow_text(response_language), [], None, None, None
        if _is_support_close_request(input_text) and support_ticket_ref is None:
            return _support_close_ref_prompt(response_language), [], None, None, None
        if self._llm_provider_error is not None:
            if support_create_active:
                support_slots = _extract_support_ticket_slots(full_support_context)
                matching = _find_matching_customer_transaction(principal, support_slots.get("amount"))
                return (
                    _support_ticket_clarification(response_language, support_slots, matching_tx=matching, text=full_support_context),
                    [], None, None, None,
                )
            raise self._llm_provider_error
        if (
            assessment_reference is None
            and not scam_tool_required
            and not transaction_risk_required
            and not investigation_request
        ):
            account_turn = run_account_application_turn(
                principal, input_text, response_language, execute_tool,
                support_create_active=support_create_active,
            )
            if account_turn is not None:
                account_text, account_results = account_turn
                for step_index, item in enumerate(account_results, start=1):
                    _trace_event(correlation_id, "tool_call_requested", step=step_index, tool=item.tool_name)
                    _trace_event(correlation_id, "tool_validation_passed", step=step_index, tool=item.tool_name, passed=True)
                    _trace_event(
                        correlation_id, "tool_execution_completed", step=step_index, tool=item.tool_name,
                        status=item.status, result_type="object",
                    )
                _trace_event(
                    correlation_id, "final_response_generated",
                    provider="account_application_service", guarded=True,
                    response_length=len(account_text),
                )
                return account_text, account_results, None, None, None
        allowed_tool_names = set(required_tools)
        if support_create_active:
            allowed_tool_names.add("propose_support_ticket")
            support_slots = _extract_support_ticket_slots(full_support_context)
            if support_slots.get("category") and support_slots.get("priority") and support_slots.get("subject"):
                required_tools = frozenset({"propose_support_ticket"})
                allowed_tool_names.add("propose_support_ticket")
        if investigation_mutation_request:
            lowered = input_text.casefold()
            if any(term in lowered for term in ("create", "open", "start", "track")):
                allowed_tool_names.add("propose_create_investigation")
            if any(term in lowered for term in ("link", "attach")):
                allowed_tool_names.add("propose_link_investigation_evidence")
            if "note" in lowered:
                allowed_tool_names.add("propose_investigation_note")
            if any(term in lowered for term in ("update", "change", "mark", "resolve")):
                allowed_tool_names.add("propose_investigation_status")
            if "close" in lowered:
                allowed_tool_names.add("propose_close_investigation")
        tool_catalog = tuple(
            item for item in _llm_tool_catalog() if item.name in allowed_tool_names
        )
        messages, context_compacted = _compact_llm_messages(
            messages, max_tokens=max(1000, _context_budget_tokens() - 3000)
        )
        if context_compacted:
            _trace_event(
                correlation_id, "conversation_context_compacted",
                provider=self._provider_settings["provider"],
                message_count=len(messages), context_compacted=True,
            )
        completed_required_tools = set()
        all_results = []
        scam_assessment = None
        available_transaction_refs = set(re.findall(r"\bTR-[A-F0-9]{48}\b", input_text, re.IGNORECASE))
        for turn in history:
            historical_text = (
                turn.get("user_text_or_transcript") if turn.get("role") == "user"
                else turn.get("assistant_response_text")
            ) or ""
            available_transaction_refs.update(
                reference.upper() for reference in re.findall(
                    r"\bTR-[A-F0-9]{48}\b", historical_text, re.IGNORECASE
                )
            )
        available_assessment_ids = set(re.findall(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            input_text, re.IGNORECASE,
        ))
        available_investigation_refs = set(re.findall(r"\bINV-[A-F0-9]{32}\b", input_text, re.I))
        latest_transaction_ref = None
        pending_action = None
        support_close_confirmation = None
        scam_tool_cache = {}
        # Sequential providers (including Groq GPT-OSS) execute one tool call
        # per round. Reserve one additional round for the final answer after
        # every required tool has completed. The previous fixed five-round
        # cap failed requests with five independent requirements after their
        # fifth lookup, before the model could compose the answer.
        max_rounds = _orchestration_round_limit(len(tool_catalog))
        for step in range(max_rounds):
            pending_required = required_tools - completed_required_tools
            # Once all server-selected requirements are satisfied, the final
            # answer request has no tools. This prevents a second call to an
            # already executed service and saves the otherwise redundant
            # tool schemas from that last provider request.
            model_tools = tool_catalog if (pending_required or not required_tools) else ()
            _trace_event(
                correlation_id, "model_request_started",
                provider=self._provider_settings["provider"], model=self._provider_settings["model"],
                step=step + 1, required_count=len(pending_required),
            )
            try:
                stage = "initial" if step == 0 else "follow_up"
                try:
                    response = self.llm_provider.generate(
                        messages, model_tools, response_language=response_language,
                        required_tools=frozenset(pending_required), request_stage=stage,
                    )
                except LLMRequestTooLarge:
                    reduced, did_compact = _compact_llm_messages(
                        messages,
                        max_tokens=max(1000, min(
                            _context_budget_tokens() // 2,
                            _estimate_messages_tokens(messages) // 2,
                        )),
                    )
                    if not did_compact or reduced == messages:
                        raise
                    messages = reduced
                    _trace_event(
                        correlation_id, "provider_413_context_retry",
                        provider=self._provider_settings["provider"],
                        model=self._provider_settings["model"], step=step + 1,
                        context_compacted=True,
                    )
                    response = self.llm_provider.generate(
                        messages, model_tools, response_language=response_language,
                        required_tools=frozenset(pending_required), request_stage=stage,
                    )
            except LLMProviderError:
                raise
            except Exception as exc:
                logger.warning("LLM provider failed (%s)", type(exc).__name__)
                raise LLMProviderError(
                    "The assistant language service could not complete the request. Please try again."
                ) from None
            _trace_event(
                correlation_id, "model_response_received", step=step + 1,
                has_text=bool(response.text), tool_call_count=len(response.tool_calls),
            )
            if not response.tool_calls:
                if support_create_active:
                    turn_slots = _extract_support_ticket_slots(input_text)
                    context_slots = _extract_support_ticket_slots(full_support_context)
                    merged_cat = turn_slots.get("category") or (active_draft.get("category") if active_draft else None) or context_slots.get("category")
                    merged_prio = turn_slots.get("priority") or (active_draft.get("priority") if active_draft else None) or context_slots.get("priority")
                    merged_subj = turn_slots.get("subject") or (active_draft.get("subject") if active_draft else None) or context_slots.get("subject")
                    merged_amt = turn_slots.get("amount") or (active_draft.get("amount") if active_draft else None) or context_slots.get("amount")
                    merged_debit = turn_slots.get("debit_confirmed") or (active_draft.get("debit_confirmed") if active_draft else None) or context_slots.get("debit_confirmed")
                    merged_desc = turn_slots.get("description") or (active_draft.get("description") if active_draft else None) or context_slots.get("description")

                    if not merged_desc and merged_subj:
                        amt_str = f" Amount: ₹{merged_amt:,.2f}." if merged_amt else ""
                        if "merchant" in merged_subj.lower() or "upi" in merged_subj.lower():
                            merged_desc = f"Amount deducted from the customer's account, but merchant has not received payment.{amt_str}"
                        else:
                            merged_desc = f"Customer reported: {merged_subj}.{amt_str}"

                    active_draft = save_or_update_draft(
                        principal.customer_key,
                        str(conversation_id),
                        category=merged_cat,
                        priority=merged_prio,
                        subject=merged_subj,
                        description=merged_desc,
                        amount=merged_amt,
                        debit_confirmed=merged_debit,
                    )
                    merged_slots = {
                        "category": merged_cat,
                        "priority": merged_prio,
                        "subject": merged_subj,
                        "description": merged_desc,
                        "amount": merged_amt,
                        "debit_confirmed": merged_debit,
                    }

                    if merged_cat and merged_prio and merged_subj:
                        tool_args = {
                            "category": merged_cat,
                            "priority": merged_prio,
                            "subject": merged_subj,
                            "description": merged_desc,
                            "language": response_language,
                        }
                        if merged_amt:
                            tool_args["amount"] = merged_amt
                        data = execute_tool(
                            "propose_support_ticket", principal, tool_args,
                            detected_languages=input_languages,
                            conversation_id=conversation_id,
                            original_turn_id=original_turn_id,
                        )
                        pending_action = PendingAction.model_validate(data)
                        mark_proposed(principal.customer_key, str(conversation_id), str(pending_action.confirmation_id))
                        result = ToolExecutionResult(
                            tool_call_id=uuid4(), tool_name="propose_support_ticket", status="needs_confirmation",
                            summary="Prepared a support ticket proposal; customer confirmation is required.",
                            data={"confirmation_id": pending_action.confirmation_id, "expires_at": pending_action.expires_at},
                        )
                        all_results.append(result)
                        text = _support_proposal_text(
                            pending_action.action.subject,
                            response_language,
                            category=pending_action.action.category,
                            priority=pending_action.action.priority,
                            amount=getattr(pending_action.action, "amount", None),
                            description=pending_action.action.description,
                        )
                        _trace_event(
                            correlation_id, "support_ticket_proposal_prepared",
                            provider=self._provider_settings["provider"],
                            guarded=True, response_length=len(text),
                        )
                        return text, all_results, scam_assessment, pending_action, None

                    matching = _find_matching_customer_transaction(principal, merged_amt)
                    clarification = _support_ticket_clarification(
                        response_language, merged_slots, matching_tx=matching, text=full_support_context,
                    )
                    _trace_event(
                        correlation_id, "support_ticket_details_requested",
                        provider=self._provider_settings["provider"],
                        response_length=len(clarification),
                    )
                    return clarification, all_results, scam_assessment, None, None
                if pending_required:
                    if (
                        transaction_risk_required
                        and "get_recent_transactions" in completed_required_tools
                        and latest_transaction_ref is None
                    ):
                        empty_text = {
                            "kn-IN": "ಮೌಲ್ಯಮಾಪನ ಮಾಡಲು ಇತ್ತೀಚಿನ ವಹಿವಾಟು ಕಂಡುಬಂದಿಲ್ಲ.",
                            "hi-IN": "आकलन के लिए कोई हाल का लेनदेन नहीं मिला।",
                        }.get(response_language, "No recent transaction was found to assess.")
                        return empty_text, all_results, scam_assessment, None, None
                    if (
                        transaction_risk_required
                        and "list_transaction_risk_assessments" in completed_required_tools
                        and not available_assessment_ids
                        and "get_transaction_risk_assessment" in pending_required
                    ):
                        no_assessment = {
                            "kn-IN": "ವಿವರಿಸಲು ಹಿಂದಿನ ವಹಿವಾಟು ಅಪಾಯ ಮೌಲ್ಯಮಾಪನ ಕಂಡುಬಂದಿಲ್ಲ.",
                            "hi-IN": "समझाने के लिए कोई पिछला लेनदेन जोखिम आकलन नहीं मिला।",
                        }.get(response_language, "No previous transaction-risk assessment was found to explain.")
                        return no_assessment, all_results, scam_assessment, None, None
                    if transaction_risk_required and any(
                        item.status == "failed" and item.tool_name in transaction_risk_tools
                        for item in all_results
                    ):
                        unavailable = {
                            "kn-IN": "ವಿನಂತಿಸಿದ ವಹಿವಾಟು ಅಪಾಯ ಮಾಹಿತಿಯನ್ನು ಮಾಲೀಕತ್ವ ಪರಿಶೀಲನೆಯೊಂದಿಗೆ ಪಡೆಯಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ.",
                            "hi-IN": "मांगी गई लेनदेन जोखिम जानकारी को स्वामित्व जाँच के साथ प्राप्त नहीं कर सके।",
                        }.get(response_language, "I could not verify that transaction-risk information for this customer.")
                        return unavailable, all_results, scam_assessment, None, None
                    if investigation_required and any(
                        item.status == "failed" and item.tool_name in investigation_read_tools
                        for item in all_results
                    ):
                        return "I could not retrieve that investigation from your account.", all_results, scam_assessment, None, None
                    _trace_event(
                        correlation_id, "required_tool_missing", step=step + 1,
                        required_count=len(pending_required),
                    )
                    raise LLMProviderError(
                        "I could not verify that information from your account. Please try again."
                    )
                if transaction_risk_required:
                    final_text = self._format_transaction_risk_results(
                        required_tools, all_results, response_language
                    )
                    _trace_event(
                        correlation_id, "final_response_generated",
                        provider="transaction_risk_engine", guarded=True,
                        response_length=len(final_text),
                    )
                    return final_text, all_results, scam_assessment, None, None
                support_reads = required_tools & {
                    "list_support_tickets", "get_support_ticket",
                    "get_support_ticket_events",
                }
                if support_reads:
                    final_text = self._format_support_read_results(
                        all_results, response_language, input_text=input_text
                    )
                    if support_staff_question:
                        final_text += "\n\n" + _support_unavailable_workflow_text(response_language)
                    _trace_event(
                        correlation_id, "final_response_generated",
                        provider="customer_support_service", guarded=True,
                        response_length=len(final_text),
                    )
                    return final_text, all_results, scam_assessment, None, None
                if required_tools == {"get_loan_details"}:
                    # The database stores the contract's original tenure, not
                    # the remaining number of instalments. Keep this answer
                    # deterministic so a model cannot relabel tenure_months.
                    final_text = self._format_loan_details(all_results)
                    _trace_event(
                        correlation_id, "final_response_generated",
                        provider="banking_service", guarded=True,
                        response_length=len(final_text),
                    )
                    return final_text, all_results, scam_assessment, None, None
                if required_tools & investigation_read_tools:
                    if any(item.status == "failed" and item.tool_name in investigation_read_tools for item in all_results):
                        return "I could not retrieve that investigation from your account.", all_results, scam_assessment, None, None
                    final_text = self._format_investigation_results(all_results, response_language)
                    _trace_event(correlation_id, "final_response_generated", provider="investigation_service", guarded=True, response_length=len(final_text))
                    return final_text, all_results, scam_assessment, None, None
                if response.text:
                    final_text, guarded = self._guard_balance_response(
                        response.text, required_tools, all_results, response_language
                    )
                    final_text = self._ensure_loan_emi_in_response(
                        final_text, required_tools, all_results, response_language
                    )
                    _trace_event(
                        correlation_id, "final_response_generated", provider=self._provider_settings["provider"],
                        guarded=guarded, response_length=len(final_text),
                    )
                    return final_text, all_results, scam_assessment, pending_action, support_close_confirmation
                raise LLMProviderError("The assistant returned an empty response. Please try again.")
            messages.append(LLMMessage(
                role="assistant", content=response.text or "",
                tool_calls=tuple(response.tool_calls),
            ))
            for call in response.tool_calls:
                result_id = uuid4()
                safe_tool = call.name if call.name in LLM_TOOL_NAMES else "unregistered"
                scam_tool = call.name in {
                    "assess_current_scam_message", "get_scam_assessment"
                }
                support_tool = call.name in {
                    "list_support_tickets", "get_support_ticket",
                    "get_support_ticket_events", "propose_support_ticket",
                    "prepare_support_ticket_close",
                }
                investigation_tool = call.name in investigation_tools
                transaction_risk_tool = call.name in transaction_risk_tools
                support_tool_allowed = (
                    call.name in required_tools
                    or (call.name == "propose_support_ticket" and support_create_active)
                )
                investigation_tool_allowed = (
                    call.name in required_tools
                    or (investigation_mutation_request and call.name in investigation_mutation_tools)
                )
                if call.name == "propose_support_ticket" and isinstance(call.arguments, dict) and active_draft:
                    call_args_dict = dict(call.arguments)
                    for k in ("category", "priority", "subject", "description"):
                        if not call_args_dict.get(k) and active_draft.get(k):
                            call_args_dict[k] = active_draft[k]
                    if not call_args_dict.get("description") and call_args_dict.get("subject"):
                        call_args_dict["description"] = f"Customer reported: {call_args_dict['subject']}."
                    if not call_args_dict.get("amount") and active_draft.get("amount"):
                        call_args_dict["amount"] = active_draft["amount"]
                    call = LLMToolCall(call_id=call.call_id, name=call.name, arguments=call_args_dict)

                _trace_event(correlation_id, "tool_call_requested", step=step + 1, tool=safe_tool)
                if (
                    call.name not in LLM_TOOL_NAMES
                    or (scam_tool and call.name not in required_tools)
                    or (scam_tool_required and not scam_tool)
                    or (transaction_risk_tool and call.name not in required_tools)
                    or (support_tool and not support_tool_allowed)
                    or (investigation_tool and not investigation_tool_allowed)
                    or (bool(required_tools & {
                        "list_support_tickets", "get_support_ticket",
                        "get_support_ticket_events", "prepare_support_ticket_close",
                    }) and not support_tool)
                ):
                    _trace_event(correlation_id, "tool_validation_failed", step=step + 1, tool="unregistered", passed=False)
                    result = ToolExecutionResult(
                        tool_call_id=result_id, tool_name=call.name[:100] or "unknown",
                        status="failed", summary="This tool is not available to the assistant.",
                        error_code="INVALID_TOOL",
                    )
                    model_result = {"error": "Tool is not available."}
                elif (
                    not isinstance(call.arguments, dict)
                    or not _validate_llm_arguments(call.name, call.arguments)
                    or (
                        call.name == "assess_transaction_risk"
                        and (
                            call.arguments.get("transaction_ref", "").upper()
                            not in available_transaction_refs
                            or (
                                latest_transaction_risk
                                and call.arguments.get("transaction_ref", "").upper()
                                != latest_transaction_ref
                            )
                        )
                    )
                    or (
                        call.name == "get_transaction_details"
                        and call.arguments.get("transaction_ref", "").upper()
                        not in available_transaction_refs
                    )
                    or (
                        call.name == "search_transactions"
                        and _transaction_amount_filter(input_text) is not None
                        and call.arguments.get(
                            _transaction_amount_filter(input_text)[0]
                        ) != _transaction_amount_filter(input_text)[1]
                    )
                    or (
                        call.name == "get_transaction_risk_assessment"
                        and call.arguments.get("assessment_id", "").casefold()
                        not in available_assessment_ids
                    )
                ):
                    _trace_event(correlation_id, "tool_validation_failed", step=step + 1, tool=safe_tool, passed=False)
                    result = ToolExecutionResult(
                        tool_call_id=result_id, tool_name=call.name,
                        status="failed", summary="Tool arguments were invalid.",
                        error_code="INVALID_ARGUMENTS",
                    )
                    model_result = {"error": "Tool arguments were invalid."}
                else:
                    _trace_event(correlation_id, "tool_validation_passed", step=step + 1, tool=safe_tool, passed=True)
                    try:
                        tool_arguments = dict(call.arguments)
                        amount_filter = (
                            _transaction_amount_filter(input_text)
                            if call.name == "search_transactions" else None
                        )
                        if amount_filter and amount_filter[2]:
                            field, threshold, _ = amount_filter
                            tool_arguments[field] = (
                                threshold + 0.01 if field == "min_amount"
                                else max(0.0, threshold - 0.01)
                            )
                        if scam_tool and call.name in scam_tool_cache:
                            data = scam_tool_cache[call.name]
                        elif scam_tool:
                            data = execute_tool(
                                call.name, principal, call.arguments,
                                current_user_text=input_text,
                                detected_languages=input_languages,
                                is_code_mixed=is_code_mixed,
                                assessment_reference=assessment_reference,
                                source_mode=source_mode,
                            )
                            scam_tool_cache[call.name] = data
                        elif support_tool:
                            if (
                                call.name == "propose_support_ticket"
                                and (
                                    (not _support_priority_was_stated(full_support_context) and not (active_draft and active_draft.get("priority")))
                                    or (not _support_issue_was_described(customer_context_text) and not (active_draft and active_draft.get("subject")))
                                    or (not _support_category_was_stated(
                                        customer_context_text, call.arguments.get("category", "")
                                    ) and not (active_draft and active_draft.get("category")))
                                    or (not _support_summary_is_grounded(
                                        call.arguments.get("subject", ""), customer_context_text
                                    ) and not (active_draft and active_draft.get("subject")))
                                    or (
                                        call.arguments.get("description")
                                        and not _support_summary_is_grounded(
                                            call.arguments["description"], customer_context_text
                                        )
                                        and not (active_draft and active_draft.get("description"))
                                    )
                                )
                    or (
                        call.name == "get_investigation"
                        and call.arguments.get("investigation_ref", "").upper() not in available_investigation_refs
                    )
                    or (
                        call.name in investigation_mutation_tools
                        and call.arguments.get("investigation_ref") is not None
                        and call.arguments.get("investigation_ref", "").upper() not in available_investigation_refs
                    )
                            ):
                                support_slots = _extract_support_ticket_slots(full_support_context)
                                matching = _find_matching_customer_transaction(principal, support_slots.get("amount"))
                                return (
                                    _support_ticket_clarification(
                                        response_language, support_slots, matching_tx=matching, text=full_support_context,
                                    ),
                                    all_results, scam_assessment, None, None,
                                )
                            call_args = dict(call.arguments)
                            if call.name == "propose_support_ticket" and active_draft:
                                for k in ("category", "priority", "subject", "description", "amount"):
                                    if not call_args.get(k) and active_draft.get(k):
                                        call_args[k] = active_draft[k]
                            data = execute_tool(
                                call.name, principal, call_args,
                                detected_languages=input_languages,
                                conversation_id=conversation_id,
                                original_turn_id=original_turn_id,
                            )
                        elif investigation_tool:
                            data = execute_tool(
                                call.name, principal, call.arguments,
                                conversation_id=conversation_id,
                                original_turn_id=original_turn_id,
                            )
                        else:
                            data = execute_tool(call.name, principal, tool_arguments)
                        if call.name in {
                            "get_recent_transactions", "search_transactions", "find_large_transactions",
                        } and isinstance(data, list):
                            discovered_refs = [
                                row.get("transaction_ref") for row in data
                                if isinstance(row, dict)
                                and isinstance(row.get("transaction_ref"), str)
                            ]
                            available_transaction_refs.update(ref.upper() for ref in discovered_refs)
                            if call.name == "get_recent_transactions" and discovered_refs:
                                latest_transaction_ref = discovered_refs[0].upper()
                        if call.name == "list_transaction_risk_assessments" and isinstance(data, dict):
                            available_assessment_ids.update(
                                str(item.get("assessment_id", "")).casefold()
                                for item in data.get("items", [])
                                if isinstance(item, dict) and item.get("assessment_id")
                            )
                        if call.name == "list_investigations" and isinstance(data, dict):
                            available_investigation_refs.update(
                                str(item.get("investigation_ref", "")).upper()
                                for item in data.get("items", [])
                                if isinstance(item, dict) and item.get("investigation_ref")
                            )
                        data = _safe_value(data)
                        data = data if isinstance(data, dict) else {"result": data}
                        if call.name in pending_required:
                            completed_required_tools.add(call.name)
                        _trace_event(
                            correlation_id, "tool_execution_completed", step=step + 1,
                            tool=safe_tool, status="succeeded",
                            result_type="list" if isinstance(data.get("result"), list) else "object",
                        )
                        result = ToolExecutionResult(
                            tool_call_id=result_id, tool_name=call.name, status="succeeded",
                            summary=f"Completed {call.name.replace('_', ' ')} request.", data=data,
                        )
                        model_result = {"result": data}
                        if scam_tool:
                            scam_assessment = data
                        if call.name == "propose_support_ticket":
                            pending_action = PendingAction.model_validate(data)
                            if (
                                pending_action.conversation_id != UUID(str(conversation_id))
                                or pending_action.original_turn_id != UUID(str(original_turn_id))
                            ):
                                raise ValueError("Pending support action context did not match.")
                            result.status = "needs_confirmation"
                            result.summary = "Prepared a support ticket proposal; customer confirmation is required."
                            result.data = {
                                "confirmation_id": pending_action.confirmation_id,
                                "expires_at": pending_action.expires_at,
                            }
                            all_results.append(result)
                            mark_proposed(principal.customer_key, str(conversation_id), str(pending_action.confirmation_id))
                            text = _support_proposal_text(
                                pending_action.action.subject,
                                response_language,
                                category=pending_action.action.category,
                                priority=pending_action.action.priority,
                                amount=getattr(pending_action.action, "amount", None),
                                description=pending_action.action.description,
                            )
                            _trace_event(
                                correlation_id, "support_ticket_proposal_prepared",
                                provider=self._provider_settings["provider"],
                                guarded=True, response_length=len(text),
                            )
                            return text, all_results, scam_assessment, pending_action, None
                        if call.name in investigation_mutation_tools:
                            pending_action = PendingAction.model_validate(data)
                            if (
                                pending_action.conversation_id != UUID(str(conversation_id))
                                or pending_action.original_turn_id != UUID(str(original_turn_id))
                            ):
                                raise ValueError("Pending investigation action context did not match.")
                            result.status = "needs_confirmation"
                            result.summary = "Prepared an investigation change proposal; explicit customer confirmation is required."
                            result.data = {"confirmation_id": pending_action.confirmation_id, "expires_at": pending_action.expires_at}
                            all_results.append(result)
                            text = self._investigation_proposal_text(pending_action.action)
                            _trace_event(correlation_id, "investigation_proposal_prepared", guarded=True, response_length=len(text))
                            return text, all_results, scam_assessment, pending_action, None
                        if call.name == "prepare_support_ticket_close":
                            if not data.get("customer_close_eligible", True):
                                all_results.append(result)
                                text = _support_cannot_close_text(
                                    data.get("current_status", "UNKNOWN"), response_language
                                )
                                _trace_event(
                                    correlation_id, "support_ticket_close_unavailable",
                                    provider="customer_support_service", guarded=True,
                                    response_length=len(text),
                                )
                                return text, all_results, scam_assessment, None, None
                            support_close_confirmation = SupportTicketCloseConfirmation(
                                ticket_ref=data["ticket_ref"],
                                subject=data["subject"],
                                current_status=data["current_status"],
                                conversation_id=UUID(str(conversation_id)),
                                confirmation_method="button",
                            )
                            result.status = "needs_confirmation"
                            result.summary = "Prepared a ticket closure request; explicit button confirmation is required."
                            result.data = {
                                "ticket_ref": support_close_confirmation.ticket_ref,
                                "current_status": support_close_confirmation.current_status,
                            }
                            all_results.append(result)
                            text = _support_close_text(
                                support_close_confirmation.ticket_ref, response_language
                            )
                            _trace_event(
                                correlation_id, "support_ticket_close_prepared",
                                provider=self._provider_settings["provider"],
                                guarded=True, response_length=len(text),
                            )
                            return text, all_results, scam_assessment, None, support_close_confirmation
                    except (TypeError, ValueError):
                        _trace_event(correlation_id, "tool_execution_failed", step=step + 1, tool=safe_tool, error_type="ValidationError")
                        result = ToolExecutionResult(
                            tool_call_id=result_id, tool_name=call.name, status="failed",
                            summary="Tool arguments were invalid or the request was not authorized.",
                            error_code="INVALID_ARGUMENTS",
                        )
                        model_result = {"error": "Arguments were invalid or the request was not authorized."}
                    except Exception as exc:
                        logger.warning("Assistant tool failed (%s)", type(exc).__name__)
                        _trace_event(correlation_id, "tool_execution_failed", step=step + 1, tool=safe_tool, error_type=type(exc).__name__)
                        result = ToolExecutionResult(
                            tool_call_id=result_id, tool_name=call.name, status="failed",
                            summary="The requested banking information is unavailable.",
                            error_code="SERVICE_UNAVAILABLE",
                        )
                        model_result = {"error": "The requested banking information is unavailable."}
                all_results.append(result)
                if support_tool and result.status == "failed":
                    if call.name == "propose_support_ticket":
                        support_slots = _extract_support_ticket_slots(full_support_context)
                        matching = _find_matching_customer_transaction(principal, support_slots.get("amount"))
                        safe_text = _support_ticket_clarification(
                            response_language, support_slots, matching_tx=matching, text=full_support_context,
                        )
                    elif call.name == "prepare_support_ticket_close":
                        safe_text = (
                            "I could not prepare that ticket closure. No ticket status was changed."
                            if response_language == "en-IN" else
                            "ಟಿಕೆಟ್ ಮುಚ್ಚುವ ವಿನಂತಿಯನ್ನು ಸಿದ್ಧಪಡಿಸಲಾಗಲಿಲ್ಲ. ಸ್ಥಿತಿಯಲ್ಲಿ ಬದಲಾವಣೆ ಆಗಿಲ್ಲ."
                            if response_language == "kn-IN" else
                            "टिकट बंद करने का अनुरोध तैयार नहीं हो सका। स्थिति में कोई बदलाव नहीं हुआ।"
                        )
                    else:
                        safe_text = (
                            "I could not retrieve that support information right now. No ticket action was taken."
                            if response_language == "en-IN" else
                            "ಬೆಂಬಲ ಮಾಹಿತಿಯನ್ನು ಪಡೆಯಲಾಗಲಿಲ್ಲ. ಯಾವುದೇ ಟಿಕೆಟ್ ಕ್ರಮ ಕೈಗೊಂಡಿಲ್ಲ."
                            if response_language == "kn-IN" else
                            "सहायता जानकारी नहीं मिल सकी। टिकट पर कोई कार्रवाई नहीं हुई।"
                        )
                    return safe_text, all_results, scam_assessment, None, None
                if not scam_tool_required:
                    messages.append(LLMMessage(
                        role="tool",
                        content=json.dumps(model_result, ensure_ascii=False, default=str),
                        tool_call_id=call.call_id,
                        tool_name=call.name,
                    ))
                    _trace_event(correlation_id, "tool_result_handed_back", step=step + 1, tool=safe_tool)
            if scam_tool_required:
                successful = next((item for item in all_results if item.tool_name in {
                    "assess_current_scam_message", "get_scam_assessment"
                } and item.status == "succeeded"), None)
                if successful is None or scam_assessment is None:
                    return (
                        "I could not retrieve or assess that message safely right now. Please try again.",
                        all_results, None, None, None,
                    )
                assessment = ScamAssessmentDetail.model_validate(scam_assessment)
                text = self._scam_assessment_summary(assessment)
                _trace_event(
                    correlation_id, "final_response_generated",
                    provider="deterministic_scam_engine", guarded=True,
                    response_length=len(text),
                )
                # Ignore any model prose accompanying the tool call and do not
                # ask the model to paraphrase authoritative risk fields.
                return text, all_results, assessment, None, None
        raise LLMProviderError("The assistant could not complete the requested steps. Please try again.")

    @staticmethod
    def _format_transaction_risk_results(required_tools, results, language):
        """Render the deterministic risk service output without LLM-authored facts."""
        result = next((item for item in reversed(results) if item.status == "succeeded" and item.tool_name in {
            "assess_transaction_risk", "get_transaction_risk_assessment",
        }), None)
        if result is not None:
            data = result.data or {}
            assessment = data.get("assessment")
            if not isinstance(assessment, dict):
                return "I could not retrieve the verified transaction-risk result. Please try again."
            intro = {
                "kn-IN": "ವಹಿವಾಟಿನ ಮಾದರಿ ಪರಿಶೀಲನೆಯ ಫಲಿತಾಂಶ (ಇದು ವಂಚನೆ ನಡೆದಿದೆಯೆಂಬ ದೃಢೀಕರಣವಲ್ಲ):",
                "hi-IN": "लेनदेन पैटर्न जाँच का परिणाम (यह धोखाधड़ी की पुष्टि नहीं है):",
            }.get(language, "Deterministic transaction-pattern assessment (not proof that fraud occurred):")
            explanation_note = {
                "kn-IN": "ಎಂಜಿನ್‌ನ ವಿವರಣೆ ಮತ್ತು ಶಿಫಾರಸುಗಳು ಇಂಗ್ಲಿಷ್‌ನಲ್ಲಿ ಇವೆ; ಅವುಗಳನ್ನು ಬದಲಾಯಿಸದೆ ನೀಡಲಾಗಿದೆ.",
                "hi-IN": "इंजन की व्याख्या और सुझाव अंग्रेज़ी में हैं; उन्हें बिना बदलाव के दिखाया गया है।",
            }.get(language)
            lines = [
                intro,
                f"Risk level: {assessment.get('risk_level', 'UNKNOWN')}; rule score: {assessment.get('risk_score', 0)} (not a probability).",
                f"Evidence strength: {assessment.get('evidence_strength', 'NONE')}.",
            ]
            if explanation_note:
                lines.append(explanation_note)
            if assessment.get("explanation"):
                lines.append(str(assessment["explanation"]))
            indicators = assessment.get("indicators") or []
            if indicators:
                lines.append("Observed indicators:")
                for indicator in indicators:
                    if not isinstance(indicator, dict):
                        continue
                    lines.append(
                        f"- {indicator.get('indicator_code')}: {indicator.get('explanation')} "
                        f"Evidence: {'; '.join(indicator.get('evidence') or [])}"
                    )
            if assessment.get("warning"):
                lines.append(f"Warning: {assessment['warning']}")
            recommendations = assessment.get("recommended_actions") or []
            if recommendations:
                lines.append("Recommended actions:")
                lines.extend(f"- {action}" for action in recommendations)
            lines.append("A high score is a screening signal, not a confirmation of fraud.")
            return "\n".join(lines)

        listing = next((item for item in reversed(results) if item.status == "succeeded" and item.tool_name == "list_transaction_risk_assessments"), None)
        if listing is not None:
            data = listing.data or {}
            items = data.get("items") or []
            if not items:
                return {
                    "kn-IN": "ಇನ್ನೂ ಯಾವುದೇ ವಹಿವಾಟು ಅಪಾಯ ಮೌಲ್ಯಮಾಪನಗಳು ದಾಖಲಾಗಿಲ್ಲ.",
                    "hi-IN": "अभी तक कोई लेनदेन जोखिम आकलन दर्ज नहीं है।",
                }.get(language, "No transaction-risk assessments have been recorded yet.")
            title = {
                "kn-IN": "ಇತ್ತೀಚಿನ ವಹಿವಾಟು ಅಪಾಯ ಮೌಲ್ಯಮಾಪನಗಳು (ಸ್ಕ್ರೀನಿಂಗ್ ಮಾತ್ರ):",
                "hi-IN": "हाल के लेनदेन जोखिम आकलन (केवल स्क्रीनिंग):",
            }.get(language, "Recent transaction-risk assessments (screening only):")
            lines = [title]
            for item in items:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"{item.get('transaction_ref')}: {item.get('risk_level')} "
                    f"(rule score {item.get('risk_score')}, evidence {item.get('evidence_strength')}, "
                    f"created {item.get('created_at')})."
                )
            lines.append("These results do not confirm fraud.")
            return "\n".join(lines)
        return "I could not retrieve the requested transaction-risk information safely. Please try again."

    @staticmethod
    def _format_loan_details(results):
        """Report only stored loan facts; never infer remaining tenure when absent."""
        result = next((item for item in results if
                       item.tool_name == "get_loan_details" and item.status == "succeeded"), None)
        loans = ((result.data or {}).get("result") if result else None)
        if not isinstance(loans, list):
            return "I could not retrieve verified loan details right now."
        if not loans:
            return "No loan records were found for this demo customer."
        lines = ["Here are the loan details available in the demo records:"]
        has_remaining_tenure = False
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            parts = [str(loan.get("loan_type") or "Loan")]
            if loan.get("principal_amount") is not None:
                parts.append(f"principal amount ₹{float(loan['principal_amount']):,.2f}")
            if loan.get("outstanding_amount") is not None:
                parts.append(f"outstanding amount ₹{float(loan['outstanding_amount']):,.2f}")
            if loan.get("interest_rate") is not None:
                parts.append(f"interest rate {float(loan['interest_rate']):g}%")
            if loan.get("tenure_months") is not None:
                parts.append(f"original tenure {int(loan['tenure_months'])} months")
            if loan.get("remaining_tenure_months") is not None:
                has_remaining_tenure = True
                parts.append(f"remaining tenure {int(loan['remaining_tenure_months'])} months")
            if loan.get("monthly_emi") is not None and float(loan.get("monthly_emi") or 0) > 0:
                parts.append(f"monthly EMI ₹{float(loan['monthly_emi']):,.2f}")
            if loan.get("next_emi_date"):
                parts.append(f"next EMI due date {loan['next_emi_date']}")
            if loan.get("completion_date"):
                parts.append(f"completion date {loan['completion_date']}")
            if loan.get("status"):
                parts.append(f"status {loan['status']}")
            lines.append("- " + "; ".join(parts) + ".")
        if not has_remaining_tenure:
            lines.append("Remaining tenure is not available in the stored loan data.")
        return "\n".join(lines)

    @staticmethod
    def _ensure_loan_emi_in_response(text, required_tools, tool_results, language):
        """Ensure combined queries that include get_loan_details always report active loan EMI facts."""
        if "get_loan_details" not in required_tools:
            return text
        result = next(
            (item for item in tool_results if item.tool_name == "get_loan_details" and item.status == "succeeded"),
            None,
        )
        loans = ((result.data or {}).get("result") if result else None)
        if not isinstance(loans, list) or not loans:
            return text
        active_loans = [
            loan for loan in loans
            if isinstance(loan, dict) and str(loan.get("status") or "").upper() == "ACTIVE" and loan.get("next_emi_date")
        ]
        if not active_loans:
            return text
        primary = active_loans[0]
        due_date = str(primary.get("next_emi_date") or "")
        emi_val = primary.get("monthly_emi")
        rem_tenure = primary.get("remaining_tenure_months")
        outstanding = primary.get("outstanding_amount")
        loan_type = str(primary.get("loan_type") or "PERSONAL")
        lowered = text.casefold()
        has_due_date = (
            due_date in text
            or "december 5, 2026" in lowered
            or "dec 5, 2026" in lowered
            or "5 december 2026" in lowered
            or "05-12-2026" in lowered
            or "5th december 2026" in lowered
        )
        false_unavailable = bool(re.search(
            r"\b(?:emi|next emi|due date|remaining tenure)\b[^.!?\n]{0,50}\b(?:unavailable|not available|not stored|cannot be verified)\b",
            lowered,
        ))
        if has_due_date and not false_unavailable:
            return text
        cleaned_lines = [
            line for line in text.splitlines()
            if not re.search(
                r"\b(?:emi|next emi|due date|remaining tenure)\b[^.!?\n]{0,50}\b(?:unavailable|not available|not stored|cannot be verified)\b",
                line.casefold(),
            )
        ]
        cleaned_text = "\n".join(cleaned_lines).strip()
        emi_str = f"₹{float(emi_val):,.2f}" if emi_val is not None else "N/A"
        out_str = f"₹{float(outstanding):,.2f}" if outstanding is not None else "N/A"
        rem_str = f"{int(rem_tenure)} months" if rem_tenure is not None else "N/A"
        if language == "kn-IN":
            loan_section = (
                f"\n\n**ಸಕ್ರಿಯ ಸಾಲ ಮತ್ತು ಇಎಂಐ ವಿವರಗಳು ({loan_type} Loan):**\n"
                f"- ಮುಂದಿನ ಇಎಂಐ ದಿನಾಂಕ: {due_date}\n"
                f"- ಮಾಸಿಕ ಇಎಂಐ ಮೊತ್ತ: {emi_str}\n"
                f"- ಉಳಿದ ಅವಧಿ: {rem_str}\n"
                f"- ಬಾಕಿ ಮೊತ್ತ: {out_str}"
            )
        elif language == "hi-IN":
            loan_section = (
                f"\n\n**सक्रिय ऋण और ईएमआई विवरण ({loan_type} Loan):**\n"
                f"- अगली ईएमआई देय तिथि: {due_date}\n"
                f"- मासिक ईएमआई: {emi_str}\n"
                f"- शेष अवधि: {rem_str}\n"
                f"- बकाया राशि: {out_str}"
            )
        else:
            loan_section = (
                f"\n\n**Active Loan & EMI Details ({loan_type} Loan):**\n"
                f"- Next EMI Due Date: {due_date}\n"
                f"- Monthly EMI: {emi_str}\n"
                f"- Remaining Tenure: {rem_str}\n"
                f"- Outstanding Balance: {out_str}"
            )
        return (cleaned_text + loan_section).strip()

    @staticmethod
    def _format_support_read_results(results, language, input_text: str = ""):
        """Render owner-scoped ticket facts from service results, not model claims."""
        query = input_text.casefold() if input_text else ""
        query_latest = any(term in query for term in (
            "just raised", "latest complaint", "latest ticket", "complaint i just raised",
            "ticket i just raised", "complaint i created", "ticket i created", "that ticket",
            "most recent complaint", "most recent ticket", "last complaint", "last ticket",
            "recent complaint", "recent ticket", "status of the complaint", "status of that ticket",
            "ताजा शिकायत", "हालिया टिकट", "पिछली शिकायत", "ಇತ್ತೀಚಿನ ಟಿಕೆಟ್", "ಹಿಂದಿನ ದೂರು",
        )) or bool(re.search(
            r"\b(?:complaint|ticket)\s+(?:i\s+)?(?:just\s+)?(?:raised|created|filed|opened|lodged)\b",
            query,
        ))
        query_all = any(term in query for term in (
            "my tickets", "my complaints", "all tickets", "all complaints", "show tickets",
            "list tickets", "show me my support tickets", "support tickets", "open tickets",
            "दिखाएं", "पार्टी", "ಪಟ್ಟಿ", "ದೂರುಗಳ ಪಟ್ಟಿ", "मेरे टिकट", "मेरी शिकायतें",
        ))
        lines = []
        for item in results:
            if item.status != "succeeded":
                continue
            data = item.data or {}
            if item.tool_name == "list_support_tickets":
                tickets = data.get("items") or []
                if not tickets:
                    lines.append({
                        "kn-IN": "ನಿಮ್ಮ ಸಿಂಥೆಟಿಕ್ ಬೆಂಬಲ ಟಿಕೆಟ್‌ಗಳು ಕಂಡುಬಂದಿಲ್ಲ.",
                        "hi-IN": "आपके कोई सिंथेटिक सहायता टिकट नहीं मिले।",
                    }.get(language, "No synthetic support tickets were found."))
                    continue
                if query_latest:
                    latest = tickets[0]
                    if language == "kn-IN":
                        lines.append("ಇತ್ತೀಚಿನ ದೂರು / ಟಿಕೆಟ್:")
                        lines.append(f"- ಉಲ್ಲೇಖ: {latest.get('ticket_ref')}")
                        lines.append(f"- ವಿಷಯ: {latest.get('subject')}")
                        lines.append(f"- ವರ್ಗ: {latest.get('category')}")
                        lines.append(f"- ಆದ್ಯತೆ: {latest.get('priority')}")
                        lines.append(f"- ಸ್ಥಿತಿ: {latest.get('status')}")
                        lines.append(f"- ದಿನಾಂಕ: {latest.get('created_at')}")
                    elif language == "hi-IN":
                        lines.append("हालिया शिकायत / टिकट:")
                        lines.append(f"- संदर्भ: {latest.get('ticket_ref')}")
                        lines.append(f"- विषय: {latest.get('subject')}")
                        lines.append(f"- श्रेणी: {latest.get('category')}")
                        lines.append(f"- प्राथमिकता: {latest.get('priority')}")
                        lines.append(f"- स्थिति: {latest.get('status')}")
                        lines.append(f"- दिनांक: {latest.get('created_at')}")
                    else:
                        lines.append("Latest complaint / ticket:")
                        lines.append(f"- Reference: {latest.get('ticket_ref')}")
                        lines.append(f"- Subject: {latest.get('subject')}")
                        lines.append(f"- Category: {latest.get('category')}")
                        lines.append(f"- Priority: {latest.get('priority')}")
                        lines.append(f"- Status: {latest.get('status')}")
                        lines.append(f"- Created: {latest.get('created_at')}")

                    if (query_all or len(tickets) > 1) and len(tickets) > 1:
                        lines.append("")
                        if language == "kn-IN":
                            lines.append("ಇತರ ಸಿಂಥೆಟಿಕ್ ಬೆಂಬಲ ಟಿಕೆಟ್‌ಗಳು:")
                        elif language == "hi-IN":
                            lines.append("अन्य सिंथेटिक सहायता टिकट:")
                        else:
                            lines.append("Other synthetic support tickets:")
                        for ticket in tickets[1:]:
                            lines.append(
                                f"- {ticket.get('ticket_ref')}: {ticket.get('subject')} — "
                                f"{ticket.get('status')} ({ticket.get('priority')}, {ticket.get('category')})."
                            )
                else:
                    lines.append({
                        "kn-IN": "ನಿಮ್ಮ ಸಿಂಥೆಟಿಕ್ ಬೆಂಬಲ ಟಿಕೆಟ್‌ಗಳು:",
                        "hi-IN": "आपके सिंथेटिक सहायता टिकट:",
                    }.get(language, "Your synthetic support tickets:"))
                    for ticket in tickets:
                        lines.append(
                            f"{ticket.get('ticket_ref')}: {ticket.get('subject')} — "
                            f"{ticket.get('status')} ({ticket.get('priority')}, {ticket.get('category')})."
                        )
            elif item.tool_name == "get_support_ticket":
                lines.append(
                    f"Ticket {data.get('ticket_ref')} ({data.get('subject')}) is "
                    f"{data.get('status')}. Priority: {data.get('priority')}; "
                    f"category: {data.get('category')}. This is a synthetic demo ticket."
                )
            elif item.tool_name == "get_support_ticket_events":
                events = data.get("result") or []
                lines.append({
                    "kn-IN": "ದಾಖಲಾದ ಟಿಕೆಟ್ ಸ್ಥಿತಿ ಇತಿಹಾಸ:",
                    "hi-IN": "दर्ज टिकट स्थिति इतिहास:",
                }.get(language, "Recorded ticket status history:"))
                for event in events:
                    previous = event.get("from_status") or "CREATED"
                    lines.append(
                        f"{previous} → {event.get('to_status')} "
                        f"({event.get('occurred_at')}; {event.get('reason_code')})."
                    )
                if not events:
                    lines.append("No status events were recorded.")
        return "\n".join(lines) if lines else "I could not retrieve support information right now."

    @staticmethod
    def _scam_assessment_summary(assessment):
        lines = [
            "Deterministic scam-screening assessment (not a verified finding of fraud or criminal identity).",
            f"Risk level: {assessment.risk_level}.",
            f"Evidence strength: {assessment.evidence_strength}.",
            assessment.explanation,
            f"Warning: {assessment.warning}",
        ]
        for indicator in assessment.indicators:
            evidence = "; ".join(indicator.evidence)
            evidence_text = f" Evidence: {evidence}." if evidence else ""
            lines.append(
                f"Indicator ({indicator.severity}, {indicator.category}): "
                f"{indicator.explanation}{evidence_text}"
            )
        if assessment.recommended_steps:
            lines.append("Recommended next steps:")
            lines.extend(f"- {step}" for step in assessment.recommended_steps)
        return "\n".join(lines)

    @staticmethod
    def _guard_balance_response(text, required_tools, tool_results, language):
        if "get_account_balance" not in required_tools:
            return text, False
        result = next((item for item in tool_results if item.tool_name == "get_account_balance" and item.status == "succeeded"), None)
        accounts = ((result.data or {}).get("result") if result else None)
        if not isinstance(accounts, list):
            return "I could not verify an account balance from the available records.", True
        balances = [float(item.get("balance") or 0) for item in accounts if isinstance(item, dict)]
        total = round(sum(balances), 2)
        amounts = []
        for match in re.findall(r"(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, flags=re.IGNORECASE):
            try:
                amounts.append(round(float(match.replace(",", "")), 2))
            except ValueError:
                continue
        # In a multi-tool response, rupee figures may legitimately come from
        # monthly totals, transaction rows, categories, comparisons, savings,
        # or loans. The old guard allowed only balance values and therefore
        # replaced a complete answer with the balance-only fallback.
        money_fields = {
            "balance", "total_balance", "amount", "total_amount", "total_spent",
            "total_credited", "total_debits", "total_credits", "successful_credits",
            "successful_debits", "net_cash_flow", "current_spending", "previous_spending",
            "difference", "estimated_savings", "monthly_income", "monthly_spending",
            "outstanding_amount", "principal_amount", "monthly_emi", "historical_average",
            "increase", "credit_limit", "available_limit", "recommended_budget",
            "potential_saving",
        }
        allowed = {total, *(round(value, 2) for value in balances)}

        def collect_money(value, key=None):
            if isinstance(value, dict):
                for child_key, child in value.items():
                    collect_money(child, str(child_key).casefold())
            elif isinstance(value, list):
                for child in value:
                    collect_money(child, key)
            elif key in money_fields and isinstance(value, (int, float)) and not isinstance(value, bool):
                allowed.add(round(float(value), 2))

        for result_item in tool_results:
            if result_item.status == "succeeded":
                collect_money(result_item.data or {})

        if total in amounts and all(value in allowed for value in amounts):
            return text, False
        return AssistantOrchestrator._format_reply(
            "banking", "account_balance", accounts, language
        ), True

    def _process_turn(
        self, principal: CustomerPrincipal, request: AssistantTurnRequest, correlation_id
    ) -> AssistantTurnResponse:
        allowed_customer_keys = frozenset(DEMO_CUSTOMER_KEYS.values())
        if (
            not isinstance(principal, CustomerPrincipal)
            or principal.auth_method != "demo_cookie"
            or principal.customer_key not in allowed_customer_keys
            or principal.expires_at <= datetime.now(timezone.utc)
            or get_customer_profile_by_key(principal.customer_key) is None
        ):
            raise PermissionError("An authenticated principal is required.")

        conversation_id = request.conversation_id
        if conversation_id is None:
            conversation = conversation_service.create_conversation(principal)
            conversation_id = UUID(conversation["conversation_id"])
        else:
            # Ownership check precedes any writes or feature dispatch.
            conversation_service.get_conversation(principal, conversation_id)

        existing_turn = conversation_service.get_user_turn_by_client_id(
            principal, conversation_id, request.client_turn_id
        )
        if existing_turn and existing_turn.get("deleted_at") is not None:
            raise AssistantServiceError(
                "This message was deleted and cannot be retried.", code="TURN_CONFLICT",
            )
        if existing_turn and existing_turn["processing_status"] == "completed":
            previous_response = conversation_service.get_completed_response_for_user_turn(
                principal, conversation_id, existing_turn["turn_id"]
            )
            if previous_response is None:
                raise AssistantServiceError("This assistant request already completed.", code="TURN_ALREADY_COMPLETED")
            input_text = existing_turn.get("user_text_or_transcript") or ""
            try:
                saved_languages = json.loads(existing_turn.get("detected_languages_json") or "[]")
            except (TypeError, ValueError):
                saved_languages = []
            languages = tuple(saved_languages) or ((existing_turn["input_language"],) if existing_turn.get("input_language") else _script_languages(input_text))
            response_language = previous_response.get("response_language") or request.response_language or _preferred_response_language(input_text, languages)
            return AssistantTurnResponse(
                conversation_id=conversation_id,
                turn_id=UUID(existing_turn["turn_id"]),
                assistant_turn_id=UUID(previous_response["turn_id"]),
                input_mode=request.input_mode,
                output_mode=request.output_mode,
                transcript=input_text if request.input_mode == "speech" else None,
                detected_languages=[DetectedLanguage(language=code) for code in languages],
                is_code_mixed=bool(existing_turn.get("is_code_mixed")) or len(languages) > 1,
                assistant_text=previous_response["assistant_response_text"] or "",
                response_language=response_language,
                speech_output=None,
                tool_results=[],
                created_at=_timestamp(previous_response["created_at"]),
            )
        elif existing_turn and existing_turn["processing_status"] not in {"failed", "processing"}:
            raise AssistantServiceError("This assistant request cannot be retried.", code="TURN_CONFLICT")

        if (
            existing_turn and existing_turn["processing_status"] == "processing"
            and not conversation_service._turn_is_stale(existing_turn)
        ):
            raise AssistantServiceError("This assistant request is already processing.", code="TURN_IN_PROGRESS")

        if existing_turn:
            if existing_turn.get("input_type") != ("audio" if request.input_mode == "speech" else "text"):
                raise AssistantServiceError("The retry does not match the original request.", code="TURN_CONFLICT")
            input_text = existing_turn.get("user_text_or_transcript") or ""
            if request.input_mode == "text" and input_text != (request.text or ""):
                raise AssistantServiceError("The retry does not match the original request.", code="TURN_CONFLICT")
            try:
                saved_languages = json.loads(existing_turn.get("detected_languages_json") or "[]")
            except (TypeError, ValueError):
                saved_languages = []
            input_languages = tuple(saved_languages) or ((existing_turn["input_language"],) if existing_turn.get("input_language") else _script_languages(input_text))
            provider_code_mixed = bool(existing_turn.get("is_code_mixed"))
            transcript = input_text if request.input_mode == "speech" else None
        elif request.input_mode == "text":
            input_text = request.text or ""
            input_languages = _script_languages(input_text)
            provider_code_mixed = False
            transcript = None
        else:
            try:
                transcription = self.transcriber.transcribe(
                    principal, request.audio_upload_id, request.language_hint
                )
            except AssistantServiceError:
                raise
            except Exception:
                raise InputProcessingUnavailable(
                    "Speech transcription is unavailable right now."
                ) from None
            if not transcription.text.strip():
                raise AssistantServiceError("No speech was recognized.")
            input_text = transcription.text.strip()
            input_languages = transcription.languages or _script_languages(input_text)
            provider_code_mixed = transcription.is_code_mixed
            transcript = input_text

        if not input_languages and request.language_hint != "auto":
            input_languages = (request.language_hint,)
        input_languages = tuple(dict.fromkeys(input_languages))
        detected = [DetectedLanguage(language=code) for code in input_languages]
        code_mixed = len(input_languages) > 1 or provider_code_mixed
        response_language = request.response_language
        if response_language is None:
            previous_turns = conversation_service.get_recent_conversation_turns(
                principal, conversation_id, limit=_history_turn_limit()
            )
            previous_language = next((
                turn.get("response_language") for turn in reversed(previous_turns)
                if turn.get("role") == "assistant" and turn.get("response_language")
            ), None)
            response_language = _preferred_response_language(input_text, input_languages)
            if not input_languages and len(re.sub(r"\W", "", input_text)) <= 2 and previous_language:
                response_language = previous_language
            elif (
                previous_language in {"kn-IN", "hi-IN"}
                and response_language == "en-IN"
                and not re.search(r"\b(?:in english|english please|respond in english|reply in english)\b", input_text, re.I)
            ):
                prior_support = any(
                    _is_support_create_request(str(t.get("user_text_or_transcript") or ""))
                    for t in previous_turns if t.get("role") == "user"
                )
                extracted_slots = _extract_support_ticket_slots(input_text)
                has_slot_values = any(
                    extracted_slots.get(k) for k in ("category", "priority", "subject", "description")
                )
                short_reply = len(input_text.split()) <= 6 and not input_text.strip().endswith("?")
                if prior_support and (has_slot_values or short_reply):
                    response_language = previous_language
        if request.input_mode == "speech" and transcript is None:
            transcript = input_text

        doc_context = ""
        persisted_text = input_text
        has_attachments = bool(getattr(request, "attachments", None))
        if has_attachments:
            from services.document_processing_service import format_document_context_for_prompt
            doc_context = format_document_context_for_prompt(request.attachments)
            if doc_context:
                persisted_text = f"{doc_context}\n\nCustomer Message:\n{input_text}"

        output_mode = {
            "speech_and_text": "both",
            "speech": "speech",
            "text": "text",
        }[request.output_mode]
        user_turn = conversation_service.save_user_turn(
            principal,
            conversation_id,
            input_type="audio" if request.input_mode == "speech" else "text",
            text_or_transcript=persisted_text,
            input_language=input_languages[0] if input_languages else None,
            requested_output_mode=output_mode,
            processing_status="processing",
            client_turn_id=request.client_turn_id,
            detected_languages=input_languages,
            is_code_mixed=code_mixed,
        )

        scam_assessment = None
        transaction_risk_assessment = None
        pending_action = None
        support_close_confirmation = None
        if self.llm_provider is not None or self._llm_provider_error is not None:
            try:
                assistant_text, tool_results, scam_assessment, pending_action, support_close_confirmation = self._llm_response(
                    principal, conversation_id, response_language, input_text, correlation_id,
                    assessment_reference=request.scam_assessment_id,
                    input_languages=input_languages,
                    is_code_mixed=code_mixed,
                    source_mode="voice_call" if request.input_mode == "speech" else "typed_message",
                    original_turn_id=user_turn["turn_id"],
                    doc_context=doc_context,
                    has_attachments=has_attachments,
                )
                transaction_risk_assessment = _transaction_risk_assessment_from_results(
                    tool_results
                )
            except LLMProviderError as exc:
                if isinstance(exc, LLMRequestTooLarge):
                    safe_message, safe_code = (
                        "The assistant request was too large for the provider, even after safely condensing context. Your message is saved; retry or ask a shorter question.",
                        "PROVIDER_REQUEST_TOO_LARGE",
                    )
                elif isinstance(exc, LLMTimeout):
                    safe_message, safe_code = (
                        "The assistant took too long to respond. Your message is saved; you can retry it.",
                        "PROVIDER_TIMEOUT",
                    )
                elif isinstance(exc, LLMRateLimited):
                    safe_message, safe_code = (
                        "The assistant service is temporarily rate limited. Your message is saved; please retry shortly.",
                        "PROVIDER_RATE_LIMITED",
                    )
                elif isinstance(exc, LLMAuthenticationError):
                    safe_message, safe_code = (
                        "The assistant provider rejected its configured credentials or permissions. Ask the administrator to check provider configuration.",
                        "PROVIDER_AUTHENTICATION_FAILED",
                    )
                elif isinstance(exc, LLMConnectionError):
                    safe_message, safe_code = (
                        "The assistant could not connect to its provider. Your message is saved; you can retry it.",
                        "PROVIDER_CONNECTION_FAILED",
                    )
                else:
                    safe_message, safe_code = (
                        "The assistant provider is temporarily unavailable. Your message is saved; you can retry it.",
                        "PROVIDER_UNAVAILABLE",
                    )
                conversation_service.save_assistant_turn(
                    principal, conversation_id, response_text=safe_message,
                    response_language=response_language, requested_output_mode=output_mode,
                    input_type="audio" if request.input_mode == "speech" else "text",
                    processing_status="failed",
                    for_user_turn_id=user_turn["turn_id"],
                )
                conversation_service.update_turn_status(
                    principal, conversation_id, user_turn["turn_id"], "failed"
                )
                raise AssistantServiceError(safe_message, code=safe_code) from None
        else:
            account_turn = (
                run_account_application_turn(
                    principal, input_text, response_language, execute_tool,
                    support_create_active=_is_support_create_request(input_text),
                )
                if request.scam_assessment_id is None and not _has_scam_intent(input_text)
                else None
            )
            if account_turn is not None:
                assistant_text, tool_results = account_turn
                for step_index, item in enumerate(tool_results, start=1):
                    _trace_event(correlation_id, "tool_call_requested", step=step_index, tool=item.tool_name)
                    _trace_event(correlation_id, "tool_validation_passed", step=step_index, tool=item.tool_name, passed=True)
                    _trace_event(
                        correlation_id, "tool_execution_completed", step=step_index, tool=item.tool_name,
                        status=item.status, result_type="object",
                    )
                tool_result = tool_results[0] if tool_results else ToolExecutionResult(
                    tool_call_id=uuid4(), tool_name="start_account_application",
                    status="succeeded", summary="Completed account application turn.", data={},
                )
            else:
                feature, intent, tool_name, arguments = self.intent_router.route(input_text)
                tool_call_id = uuid4()
                trace_tool = tool_name if tool_name in LLM_TOOL_NAMES else "legacy_router"
                if intent not in {"confirmation_required", "general_banking_query"}:
                    _trace_event(correlation_id, "tool_call_requested", step=1, tool=trace_tool)
                if intent == "confirmation_required":
                    tool_result = ToolExecutionResult(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        status="failed",
                        summary="No action was taken. State changes require explicit confirmation.",
                        error_code="CONFIRMATION_FLOW_UNAVAILABLE",
                    )
                    assistant_text = self._localized(
                        response_language,
                        "No action was taken. This request may change account state and requires explicit confirmation. Action execution is not available in this version.",
                        "ಯಾವುದೇ ಕ್ರಮ ಕೈಗೊಂಡಿಲ್ಲ. ಖಾತೆ ಬದಲಾವಣೆಗೆ ಸ್ಪಷ್ಟ ದೃಢೀಕರಣ ಅಗತ್ಯ. ಈ ಆವೃತ್ತಿಯಲ್ಲಿ ಕ್ರಮ ಕೈಗೊಳ್ಳುವಿಕೆ ಲಭ್ಯವಿಲ್ಲ.",
                        "कोई कार्रवाई नहीं की गई। खाते में बदलाव के लिए स्पष्ट पुष्टि आवश्यक है। इस संस्करण में कार्रवाई उपलब्ध नहीं है।",
                    )
                elif intent == "general_banking_query":
                    tool_result = ToolExecutionResult(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        status="succeeded",
                        summary="Provided available assistant topics.",
                        data={},
                    )
                    assistant_text = self._localized(
                        response_language,
                        "I can help with account balances, recent transactions, and spending questions using the available synthetic demo data.",
                        "ಲಭ್ಯವಿರುವ ಡೆಮೊ ಮಾಹಿತಿಯಿಂದ ಖಾತೆ ಬಾಕಿ, ಇತ್ತೀಚಿನ ವಹಿವಾಟುಗಳು ಮತ್ತು ಖರ್ಚಿನ ಬಗ್ಗೆ ಸಹಾಯ ಮಾಡಬಹುದು.",
                        "उपलब्ध डेमो डेटा से मैं खाते की शेष राशि, हाल के लेनदेन और खर्च के बारे में मदद कर सकता हूँ।",
                    )
                else:
                    try:
                        _trace_event(correlation_id, "tool_validation_passed", step=1, tool=trace_tool, passed=True)
                        result = self.registry.dispatch(
                            feature, intent, principal, tool_name, arguments
                        )
                        result = _safe_value(result)
                        _trace_event(
                            correlation_id, "tool_execution_completed", step=1, tool=trace_tool,
                            status="succeeded", result_type="list" if isinstance(result, list) else "object",
                        )
                        tool_result = ToolExecutionResult(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            status="succeeded",
                            summary=f"Completed {intent.replace('_', ' ')} request.",
                            data=result if isinstance(result, dict) else {"result": result},
                        )
                        assistant_text = self._format_reply(feature, intent, result, response_language)
                    except FeatureUnavailable:
                        _trace_event(correlation_id, "tool_execution_failed", step=1, tool=trace_tool, error_type="FeatureUnavailable")
                        tool_result = ToolExecutionResult(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            status="failed",
                            summary=f"The {feature.replace('_', ' ')} capability is not available yet.",
                            error_code="FEATURE_UNAVAILABLE",
                        )
                        assistant_text = self._localized(
                            response_language,
                            f"The {feature.replace('_', ' ')} capability is not available yet. No action was taken.",
                            "ಈ ಸೌಲಭ್ಯ ಇನ್ನೂ ಲಭ್ಯವಿಲ್ಲ. ಯಾವುದೇ ಕ್ರಮ ಕೈಗೊಂಡಿಲ್ಲ.",
                            "यह सुविधा अभी उपलब्ध नहीं है। कोई कार्रवाई नहीं की गई।",
                        )
                    except Exception:
                        _trace_event(correlation_id, "tool_execution_failed", step=1, tool=trace_tool, error_type="ServiceError")
                        # Do not leak SQL errors, internal IDs, or provider/service details.
                        tool_result = ToolExecutionResult(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            status="failed",
                            summary="I could not complete that request right now.",
                            error_code="SERVICE_UNAVAILABLE",
                        )
                        assistant_text = self._localized(
                            response_language,
                            "I could not complete that request right now. Please try again.",
                            "ಈಗ ವಿನಂತಿಯನ್ನು ಪೂರ್ಣಗೊಳಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
                            "अभी अनुरोध पूरा नहीं हो सका। कृपया फिर से कोशिश करें।",
                        )

        if request.response_language is None:
            response_language = _response_language_for_text(assistant_text, response_language)
        else:
            response_language = request.response_language
        speech_output = None
        if request.output_mode in {"speech", "speech_and_text"}:
            try:
                speech_output = self.synthesizer.synthesize(
                    principal, speech_friendly_text(assistant_text), response_language
                )
            except Exception as exc:
                _trace_event(
                    correlation_id,
                    "speech_synthesis_failed",
                    error_type=type(exc).__name__,
                )
                # Text generation succeeded. Keep and persist it even if TTS fails.

        response_text = assistant_text
        persisted_assistant = conversation_service.save_assistant_turn(
            principal,
            conversation_id,
            response_text=response_text,
            response_language=response_language,
            requested_output_mode=output_mode,
            input_type="audio" if request.input_mode == "speech" else "text",
            processing_status="completed",
            for_user_turn_id=user_turn["turn_id"],
        )
        conversation_service.update_turn_status(
            principal, conversation_id, user_turn["turn_id"], "completed"
        )
        response = AssistantTurnResponse(
            conversation_id=conversation_id,
            turn_id=UUID(user_turn["turn_id"]),
            assistant_turn_id=UUID(persisted_assistant["turn_id"]),
            input_mode=request.input_mode,
            output_mode=request.output_mode,
            transcript=transcript,
            detected_languages=detected,
            is_code_mixed=code_mixed,
            assistant_text=assistant_text,
            response_language=response_language,
            speech_output=speech_output,
            tool_results=tool_results if (self.llm_provider is not None or self._llm_provider_error is not None) else [tool_result],
            scam_assessment=scam_assessment,
            transaction_risk_assessment=transaction_risk_assessment,
            pending_action=pending_action,
            support_close_confirmation=support_close_confirmation,
            created_at=_timestamp(persisted_assistant["created_at"]),
        )
        return response

    @staticmethod
    def _investigation_proposal_text(action):
        if action.action_type == "create_investigation":
            return (
                "Proposed personal security investigation (synthetic demo tracking):\n"
                f"Title: {action.title}\nCategory: {action.category}\nSummary: {action.summary}\n"
                f"Linked assessments: {len(action.evidence)}\n"
                "Nothing has been created yet. Review and explicitly confirm below to save it."
            )
        descriptions = {
            "link_investigation_evidence": f"Link assessment {action.evidence.assessment_id} to {action.investigation_ref}.",
            "add_investigation_note": f"Add this customer-reported note to {action.investigation_ref}: {action.note}",
            "change_investigation_status": f"Change {action.investigation_ref} status to {action.status}.",
            "close_investigation": f"Close personal investigation {action.investigation_ref}.",
        }
        return "Proposed personal investigation update:\n" + descriptions.get(action.action_type, "Review the proposed investigation update.") + "\nNo change has been made. Confirm below to proceed."

    @staticmethod
    def _format_investigation_results(results, language):
        lines = []
        for item in results:
            if item.status != "succeeded":
                continue
            data = item.data or {}
            if item.tool_name == "list_investigations":
                entries = data.get("items") or []
                if not entries:
                    lines.append("No personal security investigations were found in this synthetic demo account.")
                else:
                    lines.append("Your personal security investigations (synthetic demo tracking):")
                    for entry in entries:
                        lines.append(
                            f"{entry.get('investigation_ref')}: {entry.get('title')} — "
                            f"{entry.get('status')} ({entry.get('category')}); "
                            f"{entry.get('evidence_count', 0)} linked assessment(s)."
                        )
            elif item.tool_name == "get_investigation":
                lines.append(
                    f"Investigation {data.get('investigation_ref')} is {data.get('status')} "
                    f"({data.get('category')}). {data.get('title')}: {data.get('summary')}"
                )
                for event in data.get("events") or []:
                    lines.append(f"{event.get('occurred_at')}: {event.get('summary')}")
                lines.append("This is customer-managed synthetic tracking; no bank staff review or resolution is represented.")
        return "\n".join(lines) or "I could not retrieve investigation information safely."

    @staticmethod
    def _format_reply(feature: str, intent: str, result: Any, language: str) -> str:
        if intent == "account_balance":
            entries = result if isinstance(result, list) else []
            if not entries:
                return AssistantOrchestrator._localized(
                    language,
                    "I could not find an account balance for this demo customer.",
                    "ಈ ಡೆಮೊ ಗ್ರಾಹಕರ ಖಾತೆಯ ಬಾಕಿ ಕಂಡುಬಂದಿಲ್ಲ.",
                    "इस डेमो ग्राहक के खाते की शेष राशि नहीं मिली।",
                )
            total = sum(float(item.get("balance") or 0) for item in entries if isinstance(item, dict))
            return AssistantOrchestrator._localized(
                language,
                f"Your total available balance across {len(entries)} account(s) is ₹{total:,.2f}.",
                f"ನಿಮ್ಮ {len(entries)} ಖಾತೆಗಳ ಒಟ್ಟು ಲಭ್ಯವಿರುವ ಬಾಕಿ ₹{total:,.2f}.",
                f"आपके {len(entries)} खातों की कुल उपलब्ध शेष राशि ₹{total:,.2f} है।",
            )
        if intent == "transaction_history":
            count = len(result) if isinstance(result, list) else 0
            return AssistantOrchestrator._localized(
                language, f"I found {count} recent transaction(s) for your account.",
                f"ನಿಮ್ಮ ಖಾತೆಯ {count} ಇತ್ತೀಚಿನ ವಹಿವಾಟುಗಳು ಕಂಡುಬಂದಿವೆ.",
                f"आपके खाते के {count} हाल के लेनदेन मिले।",
            )
        if intent == "transaction_search":
            count = len(result) if isinstance(result, list) else 0
            return AssistantOrchestrator._localized(
                language, f"I found {count} transaction(s) matching that search.",
                f"ಆ ಹುಡುಕಾಟಕ್ಕೆ ಹೊಂದುವ {count} ವಹಿವಾಟುಗಳು ಕಂಡುಬಂದಿವೆ.",
                f"उस खोज से मेल खाते {count} लेनदेन मिले।",
            )
        if intent == "financial_summary" and isinstance(result, dict):
            spent = result.get("total_spent", result.get("total_debits", 0))
            return AssistantOrchestrator._localized(
                language,
                f"Your recorded spending summary is ₹{float(spent or 0):,.2f}.",
                f"ನಿಮ್ಮ ದಾಖಲಾದ ಒಟ್ಟು ಖರ್ಚು ₹{float(spent or 0):,.2f}.",
                f"आपका दर्ज कुल खर्च ₹{float(spent or 0):,.2f} है।",
            )
        if intent == "category_spending":
            count = len(result) if isinstance(result, list) else 0
            return AssistantOrchestrator._localized(
                language, f"I found spending information across {count} categories.",
                f"{count} खर्चದ ವರ್ಗಗಳ ಮಾಹಿತಿ ಕಂಡುಬಂದಿದೆ.",
                f"{count} खर्च श्रेणियों की जानकारी मिली।",
            )
        if intent == "merchant_analysis":
            count = len(result) if isinstance(result, list) else 0
            return AssistantOrchestrator._localized(
                language, f"I found information for {count} top merchants.",
                f"{count} ಪ್ರಮುಖ ವ್ಯಾಪಾರಿಗಳ ಮಾಹಿತಿ ಕಂಡುಬಂದಿದೆ.",
                f"{count} प्रमुख व्यापारियों की जानकारी मिली।",
            )
        if intent == "monthly_comparison" and isinstance(result, dict):
            current = float(result.get("current_spending") or 0)
            previous = float(result.get("previous_spending") or 0)
            return AssistantOrchestrator._localized(
                language,
                f"Your spending was ₹{current:,.2f} this month and ₹{previous:,.2f} the previous month.",
                f"ಈ ತಿಂಗಳ ನಿಮ್ಮ ಖರ್ಚು ₹{current:,.2f}; ಹಿಂದಿನ ತಿಂಗಳ ಖರ್ಚು ₹{previous:,.2f}.",
                f"इस महीने आपका खर्च ₹{current:,.2f} और पिछले महीने ₹{previous:,.2f} था।",
            )
        if intent == "budget_recommendations":
            return AssistantOrchestrator._localized(
                language,
                "I prepared illustrative budget guidance from your existing demo financial data.",
                "ನಿಮ್ಮ ಡೆಮೊ ಹಣಕಾಸು ಮಾಹಿತಿಯಿಂದ ಉದಾಹರಣೆಯ ಬಜೆಟ್ ಸಲಹೆಗಳನ್ನು ಸಿದ್ಧಪಡಿಸಿದ್ದೇನೆ.",
                "आपके डेमो वित्तीय डेटा के आधार पर उदाहरणात्मक बजट सुझाव तैयार किए हैं।",
            )
        return AssistantOrchestrator._localized(
            language,
            "I can help with account balances, transactions, and spending questions using the available demo data.",
            "ಡೆಮೊ ಮಾಹಿತಿಯಿಂದ ಖಾತೆ ಬಾಕಿ, ವಹಿವಾಟುಗಳು ಮತ್ತು ಖರ್ಚಿನ ಬಗ್ಗೆ ಸಹಾಯ ಮಾಡಬಹುದು.",
            "डेमो डेटा से खाते की शेष राशि, लेनदेन और खर्च के बारे में मदद कर सकता हूँ।",
        )

    @staticmethod
    def _localized(language: str, english: str, kannada: str, hindi: str) -> str:
        return {"kn-IN": kannada, "hi-IN": hindi}.get(language, english)


orchestrator = AssistantOrchestrator()


def process_assistant_turn(principal: CustomerPrincipal, request: AssistantTurnRequest):
    return orchestrator.process_turn(principal, request)
