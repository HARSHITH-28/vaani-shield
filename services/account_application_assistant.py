"""Deterministic Account Opening Assistant turns for Vaani AI.

Account-opening intent is recognised by server-side rules, every action runs
through the approved backend dispatcher (``execute_tool``), and the reply is
rendered from the structured tool results. No language model is called for
these turns, so no application detail, identity document or personal field
ever leaves the backend, and the model cannot decide that an application is
complete or submitted. Text and speech share this path: speech arrives here
already transcribed.
"""

import logging
import re
from uuid import uuid4

from backend.api_models import ToolExecutionResult
from services.account_application_service import (
    ApplicationError,
    ApplicationNotFound,
    ApplicationValidationError,
    has_active_application,
)

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = ("en-IN", "hi-IN", "kn-IN")

# --------------------------------------------------------------------------
# Intent classification
# --------------------------------------------------------------------------

_GLOBAL_BLOCK = re.compile(
    r"\b(?:tickets?|complaints?|investigat\w*|scam\w*|fraud\w*|otp|phishing|suspicious|"
    r"loans?|emi|clos(?:e|ing)|block\w*|delete)\b"
)
_GLOBAL_BLOCK_NATIVE = (
    "शिकायत", "टिकट", "धोखा", "ठगी", "स्कैम", "लोन", "ऋण", "बंद",
    "ದೂರು", "ಟಿಕೆಟ್", "ವಂಚನೆ", "ಮೋಸ", "ಸಾಲ", "ಮುಚ್ಚ",
)
_OPEN_BLOCK = re.compile(r"\b(?:statement|balance|passbook|transactions?|history|number)\b")
_OPEN_BLOCK_NATIVE = ("शेष", "बैलेंस", "ಬ್ಯಾಲೆನ್ಸ್")

_FILLER = (
    r"(?:a|an|the|my|new|another|additional|more|one|bank|savings?|salary|current|"
    r"joint|digital|me|for|with|to|help|please|also)"
)
_OPEN_EN = (
    re.compile(
        r"\b(?:open|opening|create|creating|apply|applying|setup|set\s+up|setting\s+up|start|starting)\b"
        r"(?:\s+" + _FILLER + r")*\s+accounts?\b"
    ),
    re.compile(r"\b(?:new|another|additional)\s+(?:(?:bank|savings?|salary|current|joint|digital)\s+)*accounts?\b"),
    re.compile(r"\baccount\s+(?:opening|application)\b"),
    re.compile(r"\bapply\s+for\s+(?:a|an)\s+(?:savings?|salary|current)\b"),
)
_STATUS_EN = re.compile(
    r"\b(?:status|track|tracking|progress|update|updates|where)\b[^.?!]{0,50}\bapplication\b|"
    r"\bapplication\b[^.?!]{0,40}\b(?:status|progress|approved|submitted|stands?)\b"
)
_ACCOUNT_WORD = re.compile(r"\b(?:account|accounts|savings?|salary|current|kyc|bank)\b")
_OPTIONS_EN = re.compile(
    r"\b(?:what|which)\b[^.?!]{0,30}\b(?:types?|kinds?|options?)\b[^.?!]{0,30}\baccounts?\b"
    r"[^.?!]{0,30}\b(?:open|offer|available|choose)\b|"
    r"\b(?:types?|kinds?)\s+of\s+accounts?\s+(?:can\s+i\s+open|do\s+you\s+offer|are\s+available|available)\b|"
    r"\baccount\s+options\b"
)
_REMAINING_EN = re.compile(
    r"\bwhat(?:'s|\s+is|\s+else)?\s*(?:remaining|left|missing|pending|next)\b|"
    r"\b(?:remaining|anything\s+missing|anything\s+else\s+(?:needed|required))\b|"
    r"\bwhat\s+(?:do|should)\s+i\s+(?:do|need)(?:\s+next)?\b|\bnext\s+step\b|\bwhat\s+next\b|"
    r"\bhow\s+much\s+(?:is\s+)?left\b"
)
_REMAINING_BLOCK = re.compile(r"\b(?:balance|budget|tenure|spend\w*|expenses?)\b")
_UPLOAD_EN = re.compile(
    r"\b(?:upload|attach)\b[^.?!]{0,50}\b(?:aadhaar|aadhar|pan|kyc|documents?|address\s+proof|proof)\b|"
    r"\b(?:aadhaar|aadhar|pan(?:\s+card)?|kyc|documents?|address\s+proof)\b[^.?!]{0,50}\b(?:upload|attach|where)\b|"
    r"\bwhere\b[^.?!]{0,40}\b(?:upload|submit\s+my\s+(?:document|aadhaar|pan))"
)
_REVIEW_EN = re.compile(
    r"\bsubmit\b|\breview\b[^.?!]{0,30}\b(?:application|details|summary)\b|"
    r"\b(?:finish|complete|finali[sz]e)\b[^.?!]{0,25}\b(?:application|account|process)\b|"
    r"\bready\s+to\s+(?:submit|apply)\b"
)
_LATER_EN = re.compile(
    r"\b(?:finish|continue|complete|resume|come\s+back|do\s+(?:this|it))\b[^.?!]{0,25}\blater\b|"
    r"\bsave\s+(?:my\s+)?(?:progress|application|draft)\b|\b(?:pause|stop)\s+(?:for\s+now|here)\b"
)
_EMPLOYMENT_EN = (
    ("GOVERNMENT_EMPLOYEE", re.compile(r"\bgovernment\s+(?:employee|job|servant)\b")),
    ("SELF_EMPLOYED", re.compile(r"\bself[\s-]?employed\b|\bfreelanc\w+")),
    ("BUSINESS_OWNER", re.compile(r"\bbusiness\s+owner\b|\bi\s+(?:own|run)\s+a\s+business\b")),
    ("SALARIED", re.compile(r"\bsalaried\b")),
    ("STUDENT", re.compile(r"\bstudent\b")),
    ("RETIRED", re.compile(r"\bretired\b")),
)
_TYPE_PATTERNS = (
    ("SAVINGS", re.compile(r"\bsavings?\b|बचत|ಉಳಿತಾಯ")),
    ("SALARY", re.compile(r"\bsalary\b|वेतन|सैलरी|ವೇತನ")),
    ("CURRENT", re.compile(r"\bcurrent\s+account\b|\bcurrent\b(?=\s*$)|चालू|ಚಾಲ್ತಿ")),
)
_SELECTION_WORDS = frozenset({
    "i", "want", "would", "like", "to", "choose", "select", "pick", "prefer", "a", "an",
    "the", "account", "please", "lets", "let's", "go", "with", "make", "it", "do", "ok",
    "okay", "yes", "then", "take", "need", "new", "खाता", "खाते", "चाहिए", "चुनें",
    "चुनना", "चाहता", "चाहती", "हूँ", "हूं", "मुझे", "ಖಾತೆ", "ಬೇಕು", "ಆರಿಸಿ", "ನನಗೆ",
    "ಬಯಸುತ್ತೇನೆ",
})
_EMPLOYMENT_WORDS = frozenset({
    "i", "am", "i'm", "im", "a", "an", "the", "my", "employment", "category", "is", "it's",
    "its", "work", "as", "are", "currently", "please", "job", "status", "self", "employed",
    "self-employed", "government", "employee", "servant", "business", "owner", "student",
    "retired", "freelancer", "freelance", "salaried",
})
_TOKEN_SPLIT = re.compile(r"[\s,.;:!?\"()\[\]।]+")


def _mentioned_type(normalized):
    found = [key for key, pattern in _TYPE_PATTERNS if pattern.search(normalized)]
    return found[0] if len(found) == 1 else None


def classify_account_intent(text):
    """Return ``{"action", "needs", "args"}`` or None for non-account turns.

    ``needs`` is None, ``"active"`` (an unfinished application must exist) or
    ``"bare"`` (a status question without an explicit account word).
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return None
    normalized = text.casefold()
    if _GLOBAL_BLOCK.search(normalized) or any(term in normalized for term in _GLOBAL_BLOCK_NATIVE):
        return None
    account_type = _mentioned_type(normalized)
    type_args = {"account_type": account_type} if account_type else {}

    native_account = any(word in normalized for word in ("खाता", "खाते", "ಖಾತೆ"))
    if (
        _STATUS_EN.search(normalized)
        or (("आवेदन" in normalized) and any(w in normalized for w in ("स्थिति", "कहां", "कहाँ")))
        or (("ಅರ್ಜಿ" in normalized) and any(w in normalized for w in ("ಸ್ಥಿತಿ", "ಎಲ್ಲಿ")))
    ):
        has_account_word = bool(_ACCOUNT_WORD.search(normalized)) or native_account
        return {
            "action": "STATUS",
            "needs": None if has_account_word else "bare",
            "args": type_args,
        }

    open_block = _OPEN_BLOCK.search(normalized) or any(term in normalized for term in _OPEN_BLOCK_NATIVE)
    if not open_block:
        english_open = any(pattern.search(normalized) for pattern in _OPEN_EN)
        hindi_open = native_account and any(
            word in normalized for word in ("खोल", "खुलव", "बनाना", "बनाएं", "बनवा", "आवेदन")
        )
        kannada_open = "ಖಾತೆ" in normalized and any(word in normalized for word in ("ತೆರೆ", "ಅರ್ಜಿ"))
        if english_open or hindi_open or kannada_open:
            return {"action": "OPEN", "needs": None, "args": type_args}

    if _OPTIONS_EN.search(normalized):
        return {"action": "OPTIONS", "needs": None, "args": {}}

    # The remaining intents only make sense while a draft exists.
    if _LATER_EN.search(normalized) or (
        any(word in normalized for word in ("बाद में", "बाद मे", "ನಂತರ")) and
        any(word in normalized for word in ("पूरा", "जारी", "सेव", "ಪೂರ್ಣ", "ಮುಂದುವರಿ", "ಉಳಿಸ"))
    ):
        return {"action": "SAVE_LATER", "needs": "active", "args": {}}
    if _UPLOAD_EN.search(normalized) or (
        any(word in normalized for word in ("अपलोड", "ಅಪ್ಲೋಡ್", "ಅಪ್\u200cಲೋಡ್"))
        and any(word in normalized for word in ("आधार", "पैन", "दस्तावेज", "ಆಧಾರ್", "ಪ್ಯಾನ್", "ದಾಖಲೆ"))
    ):
        return {"action": "UPLOAD_HELP", "needs": "active", "args": {}}
    if _REVIEW_EN.search(normalized) or any(
        word in normalized for word in ("सबमिट", "जमा करें", "जमा कर", "समीक्षा", "ಸಲ್ಲಿಸ", "ಸಬ್ಮಿಟ್", "ಪರಿಶೀಲಿಸ")
    ):
        return {"action": "REVIEW", "needs": "active", "args": {}}
    if (_REMAINING_EN.search(normalized) and not _REMAINING_BLOCK.search(normalized)) or any(
        phrase in normalized for phrase in (
            "क्या बाकी", "आगे क्या", "अगला चरण", "ಏನು ಬಾಕಿ", "ಮುಂದೆ ಏನು", "ಮುಂದಿನ ಹಂತ",
        )
    ):
        return {"action": "REMAINING", "needs": "active", "args": {}}

    tokens = [token for token in _TOKEN_SPLIT.split(normalized) if token]
    # Short, selection-only replies while a draft exists. Requiring every word
    # to be a selection word keeps "show my savings" and similar questions
    # about existing finances from being read as a choice.
    if account_type and tokens and len(tokens) <= 8 and all(
        token in _SELECTION_WORDS or _mentioned_type(token) == account_type for token in tokens
    ):
        return {"action": "SELECT_TYPE", "needs": "active", "args": {"account_type": account_type}}
    if tokens and len(tokens) <= 10 and all(token in _EMPLOYMENT_WORDS for token in tokens):
        for category, pattern in _EMPLOYMENT_EN:
            if pattern.search(normalized):
                return {"action": "SET_EMPLOYMENT", "needs": "active", "args": {"employment_category": category}}
    return None


# --------------------------------------------------------------------------
# Localised reply text (English, Hindi, Kannada)
# --------------------------------------------------------------------------

_TYPE_LABEL = {
    "SAVINGS": {"en-IN": "savings", "hi-IN": "बचत", "kn-IN": "ಉಳಿತಾಯ"},
    "SALARY": {"en-IN": "salary", "hi-IN": "वेतन", "kn-IN": "ವೇತನ"},
    "CURRENT": {"en-IN": "current", "hi-IN": "चालू", "kn-IN": "ಚಾಲ್ತಿ"},
}
_FIELD_LABEL = {
    "account_type": {"en-IN": "account type", "hi-IN": "खाते का प्रकार", "kn-IN": "ಖಾತೆಯ ವಿಧ"},
    "date_of_birth": {"en-IN": "date of birth", "hi-IN": "जन्म तिथि", "kn-IN": "ಜನ್ಮ ದಿನಾಂಕ"},
    "phone": {"en-IN": "phone number", "hi-IN": "फ़ोन नंबर", "kn-IN": "ಫೋನ್ ಸಂಖ್ಯೆ"},
    "address_line": {"en-IN": "address", "hi-IN": "पता", "kn-IN": "ವಿಳಾಸ"},
    "city": {"en-IN": "city", "hi-IN": "शहर", "kn-IN": "ನಗರ"},
    "state": {"en-IN": "state", "hi-IN": "राज्य", "kn-IN": "ರಾಜ್ಯ"},
    "postal_code": {"en-IN": "PIN code", "hi-IN": "पिन कोड", "kn-IN": "ಪಿನ್ ಕೋಡ್"},
    "employment_category": {"en-IN": "employment category", "hi-IN": "रोज़गार श्रेणी", "kn-IN": "ಉದ್ಯೋಗ ವರ್ಗ"},
}
_DOCUMENT_LABEL = {
    "AADHAAR_DEMO": {
        "en-IN": "Aadhaar demonstration document",
        "hi-IN": "आधार डेमो दस्तावेज़",
        "kn-IN": "ಆಧಾರ್ ಡೆಮೊ ದಾಖಲೆ",
    },
    "PAN_DEMO": {
        "en-IN": "PAN demonstration document",
        "hi-IN": "पैन डेमो दस्तावेज़",
        "kn-IN": "ಪ್ಯಾನ್ ಡೆಮೊ ದಾಖಲೆ",
    },
    "ADDRESS_PROOF_DEMO": {
        "en-IN": "address proof demonstration document",
        "hi-IN": "पते के प्रमाण का डेमो दस्तावेज़",
        "kn-IN": "ವಿಳಾಸ ಪುರಾವೆ ಡೆಮೊ ದಾಖಲೆ",
    },
}
_EMPLOYMENT_LABEL = {
    "SALARIED": {"en-IN": "salaried", "hi-IN": "वेतनभोगी", "kn-IN": "ವೇತನದಾರ"},
    "GOVERNMENT_EMPLOYEE": {"en-IN": "government employee", "hi-IN": "सरकारी कर्मचारी", "kn-IN": "ಸರ್ಕಾರಿ ನೌಕರ"},
    "SELF_EMPLOYED": {"en-IN": "self-employed", "hi-IN": "स्वरोजगार", "kn-IN": "ಸ್ವಯಂ ಉದ್ಯೋಗಿ"},
    "BUSINESS_OWNER": {"en-IN": "business owner", "hi-IN": "व्यवसाय मालिक", "kn-IN": "ವ್ಯಾಪಾರ ಮಾಲೀಕ"},
    "STUDENT": {"en-IN": "student", "hi-IN": "छात्र", "kn-IN": "ವಿದ್ಯಾರ್ಥಿ"},
    "RETIRED": {"en-IN": "retired", "hi-IN": "सेवानिवृत्त", "kn-IN": "ನಿವೃತ್ತ"},
    "OTHER": {"en-IN": "other", "hi-IN": "अन्य", "kn-IN": "ಇತರೆ"},
}
_STATUS_LABEL = {
    "DRAFT": {"en-IN": "a saved draft", "hi-IN": "सहेजा हुआ ड्राफ़्ट", "kn-IN": "ಉಳಿಸಿದ ಕರಡು"},
    "PENDING_CONFIRMATION": {
        "en-IN": "waiting for your confirmation",
        "hi-IN": "आपकी पुष्टि की प्रतीक्षा में",
        "kn-IN": "ನಿಮ್ಮ ದೃಢೀಕರಣಕ್ಕಾಗಿ ಕಾಯುತ್ತಿದೆ",
    },
    "SUBMITTED": {"en-IN": "submitted", "hi-IN": "जमा किया गया", "kn-IN": "ಸಲ್ಲಿಸಲಾಗಿದೆ"},
}
_STATUS_NOTE = {
    "DRAFT": {
        "en-IN": "You can resume and finish it any time.",
        "hi-IN": "आप इसे कभी भी फिर से शुरू करके पूरा कर सकते हैं।",
        "kn-IN": "ನೀವು ಇದನ್ನು ಯಾವಾಗ ಬೇಕಾದರೂ ಮುಂದುವರಿಸಿ ಪೂರ್ಣಗೊಳಿಸಬಹುದು.",
    },
    "PENDING_CONFIRMATION": {
        "en-IN": "Nothing is submitted until you press the Confirm button yourself.",
        "hi-IN": "जब तक आप स्वयं पुष्टि बटन नहीं दबाते, कुछ भी जमा नहीं होता।",
        "kn-IN": "ನೀವೇ ದೃಢೀಕರಣ ಬಟನ್ ಒತ್ತುವವರೆಗೆ ಏನೂ ಸಲ್ಲಿಕೆಯಾಗುವುದಿಲ್ಲ.",
    },
    "SUBMITTED": {
        "en-IN": "It was submitted to this synthetic demo workflow. No real bank review or approval takes place.",
        "hi-IN": "इसे इस डेमो वर्कफ़्लो में जमा किया गया है। कोई वास्तविक बैंक समीक्षा या स्वीकृति नहीं होती।",
        "kn-IN": "ಇದನ್ನು ಈ ಡೆಮೊ ವರ್ಕ್‌ಫ್ಲೋಗೆ ಸಲ್ಲಿಸಲಾಗಿದೆ. ನಿಜವಾದ ಬ್ಯಾಂಕ್ ಪರಿಶೀಲನೆ ಅಥವಾ ಅನುಮೋದನೆ ನಡೆಯುವುದಿಲ್ಲ.",
    },
}

_TEXT = {
    "options": {
        "en-IN": (
            "I can help you open an account in this synthetic demo. The available types are a savings account, "
            "a salary account and a current account. A savings or salary account needs the Aadhaar and PAN "
            "demonstration documents, and a current account also needs an address proof demonstration document. "
            "Applicants must be at least 18 under the demo rules. These are demonstration rules only, not real bank terms. "
            "Which account would you like?"
        ),
        "hi-IN": (
            "मैं इस डेमो में खाता खुलवाने में आपकी मदद कर सकता हूँ। उपलब्ध प्रकार बचत खाता, वेतन खाता और चालू खाता हैं। "
            "बचत या वेतन खाते के लिए आधार और पैन डेमो दस्तावेज़ चाहिए, और चालू खाते के लिए पते के प्रमाण का डेमो दस्तावेज़ भी चाहिए। "
            "डेमो नियमों में आवेदक की आयु कम से कम 18 वर्ष होनी चाहिए। ये केवल डेमो नियम हैं, असली बैंक की शर्तें नहीं। "
            "आप कौन सा खाता चाहेंगे?"
        ),
        "kn-IN": (
            "ಈ ಡೆಮೊದಲ್ಲಿ ಖಾತೆ ತೆರೆಯಲು ನಾನು ನಿಮಗೆ ಸಹಾಯ ಮಾಡುತ್ತೇನೆ. ಲಭ್ಯವಿರುವ ವಿಧಗಳು ಉಳಿತಾಯ ಖಾತೆ, ವೇತನ ಖಾತೆ ಮತ್ತು ಚಾಲ್ತಿ ಖಾತೆ. "
            "ಉಳಿತಾಯ ಅಥವಾ ವೇತನ ಖಾತೆಗೆ ಆಧಾರ್ ಮತ್ತು ಪ್ಯಾನ್ ಡೆಮೊ ದಾಖಲೆಗಳು ಬೇಕು, ಚಾಲ್ತಿ ಖಾತೆಗೆ ವಿಳಾಸ ಪುರಾವೆ ಡೆಮೊ ದಾಖಲೆಯೂ ಬೇಕು. "
            "ಡೆಮೊ ನಿಯಮಗಳ ಪ್ರಕಾರ ಅರ್ಜಿದಾರರಿಗೆ ಕನಿಷ್ಠ 18 ವರ್ಷ ಆಗಿರಬೇಕು. ಇವು ಕೇವಲ ಡೆಮೊ ನಿಯಮಗಳು, ನಿಜವಾದ ಬ್ಯಾಂಕ್ ನಿಯಮಗಳಲ್ಲ. "
            "ನಿಮಗೆ ಯಾವ ಖಾತೆ ಬೇಕು?"
        ),
    },
    "started": {
        "en-IN": "I have started your {type} account application. This is a synthetic demo and your progress is saved as a draft.",
        "hi-IN": "मैंने आपका {type} खाता आवेदन शुरू कर दिया है। यह एक डेमो है और आपकी प्रगति ड्राफ़्ट के रूप में सहेजी गई है।",
        "kn-IN": "ನಾನು ನಿಮ್ಮ {type} ಖಾತೆ ಅರ್ಜಿಯನ್ನು ಪ್ರಾರಂಭಿಸಿದ್ದೇನೆ. ಇದು ಡೆಮೊ ಆಗಿದ್ದು, ನಿಮ್ಮ ಪ್ರಗತಿಯನ್ನು ಕರಡಾಗಿ ಉಳಿಸಲಾಗಿದೆ.",
    },
    "started_untyped": {
        "en-IN": "I have started a draft account application. This is a synthetic demo and your progress is saved.",
        "hi-IN": "मैंने खाता आवेदन का ड्राफ़्ट शुरू कर दिया है। यह एक डेमो है और आपकी प्रगति सहेजी गई है।",
        "kn-IN": "ನಾನು ಖಾತೆ ಅರ್ಜಿಯ ಕರಡನ್ನು ಪ್ರಾರಂಭಿಸಿದ್ದೇನೆ. ಇದು ಡೆಮೊ ಆಗಿದ್ದು, ನಿಮ್ಮ ಪ್ರಗತಿಯನ್ನು ಉಳಿಸಲಾಗಿದೆ.",
    },
    "resumed": {
        "en-IN": "I found your unfinished account application and resumed it. Your progress is saved.",
        "hi-IN": "मुझे आपका अधूरा खाता आवेदन मिला और मैंने उसे फिर शुरू कर दिया। आपकी प्रगति सहेजी गई है।",
        "kn-IN": "ನಿಮ್ಮ ಅಪೂರ್ಣ ಖಾತೆ ಅರ್ಜಿ ಸಿಕ್ಕಿತು, ಅದನ್ನು ಮುಂದುವರಿಸಿದ್ದೇನೆ. ನಿಮ್ಮ ಪ್ರಗತಿಯನ್ನು ಉಳಿಸಲಾಗಿದೆ.",
    },
    "choose_type": {
        "en-IN": "Please choose an account type first: savings, salary or current.",
        "hi-IN": "कृपया पहले खाते का प्रकार चुनें: बचत, वेतन या चालू।",
        "kn-IN": "ದಯವಿಟ್ಟು ಮೊದಲು ಖಾತೆಯ ವಿಧವನ್ನು ಆರಿಸಿ: ಉಳಿತಾಯ, ವೇತನ ಅಥವಾ ಚಾಲ್ತಿ.",
    },
    "next_details": {
        "en-IN": (
            "Next step: open Account Services, then Open New Account, and fill in these details: {fields}. "
            "Your name is taken from your demo profile."
        ),
        "hi-IN": (
            "अगला चरण: अकाउंट सर्विसेज में नया खाता खोलें सेक्शन खोलें और ये जानकारी भरें: {fields}। "
            "आपका नाम आपकी डेमो प्रोफ़ाइल से लिया गया है।"
        ),
        "kn-IN": (
            "ಮುಂದಿನ ಹಂತ: ಅಕೌಂಟ್ ಸರ್ವಿಸಸ್‌ನಲ್ಲಿ ಹೊಸ ಖಾತೆ ತೆರೆಯಿರಿ ವಿಭಾಗವನ್ನು ತೆರೆದು ಈ ವಿವರಗಳನ್ನು ಭರ್ತಿ ಮಾಡಿ: {fields}. "
            "ನಿಮ್ಮ ಹೆಸರನ್ನು ನಿಮ್ಮ ಡೆಮೊ ಪ್ರೊಫೈಲ್‌ನಿಂದ ತೆಗೆದುಕೊಳ್ಳಲಾಗಿದೆ."
        ),
    },
    "next_details_fix": {
        "en-IN": "Some of your details need correcting. Open Account Services, then Open New Account, and review the highlighted fields.",
        "hi-IN": "आपकी कुछ जानकारी सुधारनी होगी। अकाउंट सर्विसेज में नया खाता खोलें सेक्शन खोलकर चिह्नित फ़ील्ड देखें।",
        "kn-IN": "ನಿಮ್ಮ ಕೆಲವು ವಿವರಗಳನ್ನು ಸರಿಪಡಿಸಬೇಕು. ಅಕೌಂಟ್ ಸರ್ವಿಸಸ್‌ನ ಹೊಸ ಖಾತೆ ತೆರೆಯಿರಿ ವಿಭಾಗದಲ್ಲಿ ಗುರುತಿಸಿದ ಕ್ಷೇತ್ರಗಳನ್ನು ಪರಿಶೀಲಿಸಿ.",
    },
    "next_documents": {
        "en-IN": (
            "Your details are complete. Next, open the KYC Documents section and upload your demo documents: {docs}. "
            "I will check that each file meets the demo type and size rules. These are synthetic demo documents, "
            "and nothing is checked with any government system."
        ),
        "hi-IN": (
            "आपकी जानकारी पूरी है। अब केवाईसी डॉक्यूमेंट्स सेक्शन खोलें और अपने डेमो दस्तावेज़ अपलोड करें: {docs}। "
            "मैं जाँचूँगा कि हर फ़ाइल डेमो के प्रकार और आकार के नियमों को पूरा करती है। ये सिंथेटिक डेमो दस्तावेज़ हैं, "
            "और किसी सरकारी प्रणाली से कुछ भी सत्यापित नहीं किया जाता।"
        ),
        "kn-IN": (
            "ನಿಮ್ಮ ವಿವರಗಳು ಪೂರ್ಣವಾಗಿವೆ. ಈಗ ಕೆವೈಸಿ ಡಾಕ್ಯುಮೆಂಟ್ಸ್ ವಿಭಾಗವನ್ನು ತೆರೆದು ನಿಮ್ಮ ಡೆಮೊ ದಾಖಲೆಗಳನ್ನು ಅಪ್‌ಲೋಡ್ ಮಾಡಿ: {docs}. "
            "ಪ್ರತಿ ಫೈಲ್ ಡೆಮೊ ಪ್ರಕಾರ ಮತ್ತು ಗಾತ್ರದ ನಿಯಮಗಳನ್ನು ಪೂರೈಸುತ್ತದೆಯೇ ಎಂದು ನಾನು ಪರಿಶೀಲಿಸುತ್ತೇನೆ. ಇವು ಕೃತಕ ಡೆಮೊ ದಾಖಲೆಗಳು, "
            "ಯಾವುದೇ ಸರ್ಕಾರಿ ವ್ಯವಸ್ಥೆಯಿಂದ ಏನನ್ನೂ ಪರಿಶೀಲಿಸಲಾಗುವುದಿಲ್ಲ."
        ),
    },
    "next_review": {
        "en-IN": (
            "Everything required is complete. Open Review and Submit to see your full summary. Nothing is submitted until "
            "you press the Confirm button yourself, and I cannot submit it for you."
        ),
        "hi-IN": (
            "ज़रूरी सब कुछ पूरा है। पूरा सारांश देखने के लिए समीक्षा और जमा करें सेक्शन खोलें। जब तक आप स्वयं पुष्टि बटन नहीं दबाते, "
            "कुछ भी जमा नहीं होता, और मैं आपकी ओर से इसे जमा नहीं कर सकता।"
        ),
        "kn-IN": (
            "ಅಗತ್ಯವಿರುವ ಎಲ್ಲವೂ ಪೂರ್ಣವಾಗಿದೆ. ಪೂರ್ಣ ಸಾರಾಂಶ ನೋಡಲು ಪರಿಶೀಲನೆ ಮತ್ತು ಸಲ್ಲಿಕೆ ವಿಭಾಗವನ್ನು ತೆರೆಯಿರಿ. ನೀವೇ ದೃಢೀಕರಣ ಬಟನ್ ಒತ್ತುವವರೆಗೆ "
            "ಏನೂ ಸಲ್ಲಿಕೆಯಾಗುವುದಿಲ್ಲ, ಮತ್ತು ನಾನು ನಿಮ್ಮ ಪರವಾಗಿ ಸಲ್ಲಿಸಲು ಸಾಧ್ಯವಿಲ್ಲ."
        ),
    },
    "next_submitted": {
        "en-IN": "This application is already submitted. Open Track Applications to see it.",
        "hi-IN": "यह आवेदन पहले ही जमा हो चुका है। देखने के लिए आवेदन ट्रैक करें सेक्शन खोलें।",
        "kn-IN": "ಈ ಅರ್ಜಿಯನ್ನು ಈಗಾಗಲೇ ಸಲ್ಲಿಸಲಾಗಿದೆ. ನೋಡಲು ಅರ್ಜಿ ಟ್ರ್ಯಾಕ್ ವಿಭಾಗವನ್ನು ತೆರೆಯಿರಿ.",
    },
    "type_set": {
        "en-IN": "Done. I have set your account type to {type}.",
        "hi-IN": "हो गया। मैंने आपके खाते का प्रकार {type} कर दिया है।",
        "kn-IN": "ಆಯಿತು. ನಿಮ್ಮ ಖಾತೆಯ ವಿಧವನ್ನು {type} ಎಂದು ಹೊಂದಿಸಿದ್ದೇನೆ.",
    },
    "employment_set": {
        "en-IN": "I have saved your employment category as {category}.",
        "hi-IN": "मैंने आपकी रोज़गार श्रेणी {category} के रूप में सहेज ली है।",
        "kn-IN": "ನಿಮ್ಮ ಉದ್ಯೋಗ ವರ್ಗವನ್ನು {category} ಎಂದು ಉಳಿಸಿದ್ದೇನೆ.",
    },
    "employment_ineligible": {
        "en-IN": "That choice does not fit this account type under the synthetic demo rules. Please choose a different category or account type. Nothing was changed.",
        "hi-IN": "डेमो नियमों के अनुसार यह विकल्प इस खाते के प्रकार के लिए उपयुक्त नहीं है। कृपया दूसरी श्रेणी या खाते का प्रकार चुनें। कुछ भी नहीं बदला गया।",
        "kn-IN": "ಡೆಮೊ ನಿಯಮಗಳ ಪ್ರಕಾರ ಈ ಆಯ್ಕೆ ಈ ಖಾತೆಯ ವಿಧಕ್ಕೆ ಹೊಂದುವುದಿಲ್ಲ. ದಯವಿಟ್ಟು ಬೇರೆ ವರ್ಗ ಅಥವಾ ಖಾತೆಯ ವಿಧವನ್ನು ಆರಿಸಿ. ಏನೂ ಬದಲಾಗಿಲ್ಲ.",
    },
    "missing_fields": {
        "en-IN": "Missing details: {fields}.",
        "hi-IN": "बाकी जानकारी: {fields}।",
        "kn-IN": "ಬಾಕಿ ವಿವರಗಳು: {fields}.",
    },
    "missing_documents": {
        "en-IN": "Missing documents: {docs}.",
        "hi-IN": "बाकी दस्तावेज़: {docs}।",
        "kn-IN": "ಬಾಕಿ ದಾಖಲೆಗಳು: {docs}.",
    },
    "nothing_missing": {
        "en-IN": "Nothing is missing.",
        "hi-IN": "कुछ भी बाकी नहीं है।",
        "kn-IN": "ಏನೂ ಬಾಕಿಯಿಲ್ಲ.",
    },
    "not_ready": {
        "en-IN": "Your application is not ready to review yet.",
        "hi-IN": "आपका आवेदन अभी समीक्षा के लिए तैयार नहीं है।",
        "kn-IN": "ನಿಮ್ಮ ಅರ್ಜಿ ಇನ್ನೂ ಪರಿಶೀಲನೆಗೆ ಸಿದ್ಧವಾಗಿಲ್ಲ.",
    },
    "upload_help": {
        "en-IN": (
            "Open the KYC Documents section. You will see an upload area for each required document, including the "
            "Aadhaar demonstration document. Select your demo file there and upload it, and I will check that it meets "
            "the demo file type and size rules. {status}"
        ),
        "hi-IN": (
            "केवाईसी डॉक्यूमेंट्स सेक्शन खोलें। वहाँ हर ज़रूरी दस्तावेज़ के लिए अपलोड का क्षेत्र दिखेगा, जिसमें आधार डेमो दस्तावेज़ भी है। "
            "वहाँ अपनी डेमो फ़ाइल चुनकर अपलोड करें, और मैं जाँचूँगा कि वह डेमो फ़ाइल प्रकार और आकार के नियमों को पूरा करती है। {status}"
        ),
        "kn-IN": (
            "ಕೆವೈಸಿ ಡಾಕ್ಯುಮೆಂಟ್ಸ್ ವಿಭಾಗವನ್ನು ತೆರೆಯಿರಿ. ಅಲ್ಲಿ ಪ್ರತಿ ಅಗತ್ಯ ದಾಖಲೆಗೆ ಅಪ್‌ಲೋಡ್ ಪ್ರದೇಶ ಕಾಣುತ್ತದೆ, ಆಧಾರ್ ಡೆಮೊ ದಾಖಲೆಯೂ ಅದರಲ್ಲಿದೆ. "
            "ಅಲ್ಲಿ ನಿಮ್ಮ ಡೆಮೊ ಫೈಲ್ ಆಯ್ಕೆ ಮಾಡಿ ಅಪ್‌ಲೋಡ್ ಮಾಡಿ, ಅದು ಡೆಮೊ ಫೈಲ್ ಪ್ರಕಾರ ಮತ್ತು ಗಾತ್ರದ ನಿಯಮಗಳನ್ನು ಪೂರೈಸುತ್ತದೆಯೇ ಎಂದು ನಾನು ಪರಿಶೀಲಿಸುತ್ತೇನೆ. {status}"
        ),
    },
    "upload_still": {
        "en-IN": "Still needed: {docs}.",
        "hi-IN": "अभी भी चाहिए: {docs}।",
        "kn-IN": "ಇನ್ನೂ ಬೇಕಾಗಿದೆ: {docs}.",
    },
    "upload_done": {
        "en-IN": "All required documents are uploaded.",
        "hi-IN": "सभी ज़रूरी दस्तावेज़ अपलोड हो चुके हैं।",
        "kn-IN": "ಎಲ್ಲಾ ಅಗತ್ಯ ದಾಖಲೆಗಳು ಅಪ್‌ಲೋಡ್ ಆಗಿವೆ.",
    },
    "upload_needs_type": {
        "en-IN": "Please choose an account type first, because the required documents depend on it.",
        "hi-IN": "कृपया पहले खाते का प्रकार चुनें, क्योंकि ज़रूरी दस्तावेज़ उसी पर निर्भर करते हैं।",
        "kn-IN": "ದಯವಿಟ್ಟು ಮೊದಲು ಖಾತೆಯ ವಿಧವನ್ನು ಆರಿಸಿ, ಏಕೆಂದರೆ ಅಗತ್ಯ ದಾಖಲೆಗಳು ಅದರ ಮೇಲೆ ಅವಲಂಬಿತವಾಗಿವೆ.",
    },
    "saved_later": {
        "en-IN": (
            "Yes. Your application progress is saved as a draft. You can come back later and resume it from Account Services, "
            "Open New Account, or simply ask me to continue."
        ),
        "hi-IN": (
            "हाँ। आपके आवेदन की प्रगति ड्राफ़्ट के रूप में सहेजी गई है। आप बाद में अकाउंट सर्विसेज के नया खाता खोलें सेक्शन से "
            "इसे फिर शुरू कर सकते हैं, या मुझसे जारी रखने को कह सकते हैं।"
        ),
        "kn-IN": (
            "ಹೌದು. ನಿಮ್ಮ ಅರ್ಜಿಯ ಪ್ರಗತಿಯನ್ನು ಕರಡಾಗಿ ಉಳಿಸಲಾಗಿದೆ. ನೀವು ನಂತರ ಅಕೌಂಟ್ ಸರ್ವಿಸಸ್‌ನ ಹೊಸ ಖಾತೆ ತೆರೆಯಿರಿ ವಿಭಾಗದಿಂದ ಮುಂದುವರಿಸಬಹುದು, "
            "ಅಥವಾ ಮುಂದುವರಿಸಲು ನನಗೆ ಹೇಳಬಹುದು."
        ),
    },
    "status_found": {
        "en-IN": "Your {type}account application {ref} is {status}. {note}",
        "hi-IN": "आपका {type}खाता आवेदन {ref} {status} है। {note}",
        "kn-IN": "ನಿಮ್ಮ {type}ಖಾತೆ ಅರ್ಜಿ {ref} {status}. {note}",
    },
    "status_none": {
        "en-IN": "I could not find an account application for you yet. You can tell me you want to open a new account to start one.",
        "hi-IN": "मुझे आपका कोई खाता आवेदन अभी नहीं मिला। शुरू करने के लिए आप कह सकते हैं कि आप नया खाता खोलना चाहते हैं।",
        "kn-IN": "ನಿಮ್ಮ ಯಾವುದೇ ಖಾತೆ ಅರ್ಜಿ ಇನ್ನೂ ಸಿಕ್ಕಿಲ್ಲ. ಪ್ರಾರಂಭಿಸಲು ಹೊಸ ಖಾತೆ ತೆರೆಯಲು ಬಯಸುವುದಾಗಿ ಹೇಳಿ.",
    },
    "error": {
        "en-IN": "I could not complete that step, and your application was not changed. Please try again.",
        "hi-IN": "मैं यह चरण पूरा नहीं कर सका, और आपका आवेदन नहीं बदला गया। कृपया फिर कोशिश करें।",
        "kn-IN": "ನಾನು ಈ ಹಂತವನ್ನು ಪೂರ್ಣಗೊಳಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ, ನಿಮ್ಮ ಅರ್ಜಿ ಬದಲಾಗಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
    },
}

_STEP_NAVIGATION = {
    "account_type": "Open New Account",
    "details": "Open New Account",
    "documents": "KYC Documents",
    "review": "Review & Submit",
    "submitted": "Track Applications",
}
_TOOL_SUMMARY = {
    "start_account_application": "Started or resumed the synthetic account application draft.",
    "get_account_application": "Retrieved the account application progress.",
    "update_account_application": "Saved the selected choice on the draft application.",
    "validate_account_application": "Validated the draft application.",
    "get_required_application_documents": "Listed the synthetic demo account options.",
    "get_application_document_status": "Checked which demo documents are uploaded.",
    "prepare_account_application_submission": "Prepared the application review; nothing was submitted.",
    "get_account_application_status": "Retrieved the account application status.",
}


def _language(language):
    return language if language in SUPPORTED_LANGUAGES else "en-IN"


def _t(key, language, **values):
    return _TEXT[key][_language(language)].format(**values)


def _labels(table, keys, language):
    return ", ".join(table[key][_language(language)] for key in keys if key in table)


def _next_step_text(view, language):
    step = view["progress"]["current_step"]
    validation = view["validation"]
    if step == "account_type":
        return _t("choose_type", language)
    if step == "details":
        fields = [name for name in validation["missing_fields"] if name != "account_type"]
        if fields:
            return _t("next_details", language, fields=_labels(_FIELD_LABEL, fields, language))
        return _t("next_details_fix", language)
    if step == "documents":
        return _t("next_documents", language, docs=_labels(_DOCUMENT_LABEL, validation["missing_documents"], language))
    if step == "review":
        return _t("next_review", language)
    return _t("next_submitted", language)


def _missing_text(view, language):
    validation = view["validation"]
    parts = []
    fields = [name for name in validation["missing_fields"] if name != "account_type"]
    if view.get("account_type") is None:
        parts.append(_t("choose_type", language))
    if fields:
        parts.append(_t("missing_fields", language, fields=_labels(_FIELD_LABEL, fields, language)))
    if validation["missing_documents"]:
        parts.append(_t("missing_documents", language, docs=_labels(_DOCUMENT_LABEL, validation["missing_documents"], language)))
    return " ".join(parts)


def _navigation(view):
    step = (view.get("progress") or {}).get("current_step")
    subfeature = _STEP_NAVIGATION.get(step)
    if subfeature is None:
        return None
    return {"target": "account_services", "subfeature": subfeature, "step": step}


# --------------------------------------------------------------------------
# Turn execution
# --------------------------------------------------------------------------

def run_account_application_turn(principal, text, language, execute, *, support_create_active=False):
    """Run one account-opening turn; return ``(text, tool_results)`` or None.

    ``execute`` is the approved dispatcher (``execute_tool``). Returning None
    means the turn is not an account-opening turn and normal routing applies.
    """
    if support_create_active:
        return None
    intent = classify_account_intent(text)
    if intent is None:
        return None
    action, needs, args = intent["action"], intent["needs"], dict(intent["args"])
    if needs == "active":
        try:
            if not has_active_application(principal):
                return None
        except PermissionError:
            return None
    lang = _language(language)
    results = []
    failure = {"error": None}

    def call(name, arguments):
        result_id = uuid4()
        try:
            data = execute(name, principal, arguments)
        except ApplicationValidationError as exc:
            failure["error"] = exc
            results.append(ToolExecutionResult(
                tool_call_id=result_id, tool_name=name, status="failed",
                summary="The request did not meet the demo validation rules.",
                error_code="VALIDATION_FAILED",
            ))
            return None
        except (ApplicationError, ApplicationNotFound, ValueError, PermissionError) as exc:
            failure["error"] = exc
            results.append(ToolExecutionResult(
                tool_call_id=result_id, tool_name=name, status="failed",
                summary="The request could not be completed.", error_code="REQUEST_REJECTED",
            ))
            return None
        except Exception as exc:  # noqa: BLE001 - never expose provider/storage detail
            logger.warning("Account application tool failed (%s)", type(exc).__name__)
            failure["error"] = exc
            results.append(ToolExecutionResult(
                tool_call_id=result_id, tool_name=name, status="failed",
                summary="The account application service is unavailable.",
                error_code="SERVICE_UNAVAILABLE",
            ))
            return None
        results.append(ToolExecutionResult(
            tool_call_id=result_id, tool_name=name, status="succeeded",
            summary=_TOOL_SUMMARY.get(name, "Completed."), data={"result": data},
        ))
        return data

    def attach_navigation(view):
        navigation = _navigation(view) if isinstance(view, dict) and "progress" in view else None
        if navigation and results and results[-1].status == "succeeded":
            results[-1].data = {**(results[-1].data or {}), "ui_navigation": navigation}

    def error_text():
        error = failure["error"]
        if isinstance(error, ApplicationValidationError) and any(
            item.get("code") == "EMPLOYMENT_NOT_ELIGIBLE" for item in error.errors
        ):
            return _t("employment_ineligible", lang)
        return _t("error", lang)

    if action == "OPTIONS":
        data = call("get_required_application_documents", {})
        return (_t("options", lang) if data is not None else error_text()), results

    if action == "OPEN":
        view = call("start_account_application", args)
        if view is None:
            return error_text(), results
        attach_navigation(view)
        if view.get("resumed"):
            lead = _t("resumed", lang)
        elif view.get("account_type"):
            lead = _t("started", lang, type=_TYPE_LABEL[view["account_type"]][lang])
        else:
            lead = _t("started_untyped", lang)
        follow = _t("options", lang) if view["progress"]["current_step"] == "account_type" else _next_step_text(view, lang)
        return f"{lead} {follow}", results

    if action in {"SELECT_TYPE", "SET_EMPLOYMENT"}:
        view = call("update_account_application", args)
        if view is None:
            return error_text(), results
        attach_navigation(view)
        if action == "SELECT_TYPE":
            lead = _t("type_set", lang, type=_TYPE_LABEL[view["account_type"]][lang])
        else:
            lead = _t("employment_set", lang, category=_EMPLOYMENT_LABEL[args["employment_category"]][lang])
        return f"{lead} {_next_step_text(view, lang)}", results

    if action == "REMAINING":
        view = call("validate_account_application", {})
        if view is None:
            return error_text(), results
        attach_navigation(view)
        missing = _missing_text(view, lang)
        lead = missing if missing else _t("nothing_missing", lang)
        return f"{lead} {_next_step_text(view, lang)}", results

    if action == "UPLOAD_HELP":
        data = call("get_application_document_status", {})
        if data is None:
            return error_text(), results
        if data.get("account_type") is None:
            return _t("upload_needs_type", lang), results
        missing = data.get("missing_documents") or []
        status = (
            _t("upload_still", lang, docs=_labels(_DOCUMENT_LABEL, missing, lang)) if missing
            else _t("upload_done", lang)
        )
        results[-1].data = {
            **(results[-1].data or {}),
            "ui_navigation": {"target": "account_services", "subfeature": "KYC Documents", "step": "documents"},
        }
        return _t("upload_help", lang, status=status), results

    if action == "REVIEW":
        data = call("prepare_account_application_submission", {})
        if data is None:
            return error_text(), results
        view = data["application"]
        attach_navigation(view)
        if data.get("ready"):
            return _t("next_review", lang), results
        return f"{_t('not_ready', lang)} {_missing_text(view, lang)} {_next_step_text(view, lang)}".replace("  ", " "), results

    if action == "SAVE_LATER":
        view = call("get_account_application", {})
        if view is None:
            return error_text(), results
        return _t("saved_later", lang), results

    # STATUS
    data = call("get_account_application_status", args)
    if data is None:
        return error_text(), results
    if not data.get("found"):
        if needs == "bare":
            return None
        return _t("status_none", lang), results
    account_type = data.get("account_type")
    type_prefix = f"{_TYPE_LABEL[account_type][lang]} " if account_type in _TYPE_LABEL else ""
    results[-1].data = {
        **(results[-1].data or {}),
        "ui_navigation": {"target": "account_services", "subfeature": "Track Applications", "step": "status"},
    }
    return _t(
        "status_found", lang, type=type_prefix, ref=data["application_ref"],
        status=_STATUS_LABEL[data["status"]][lang], note=_STATUS_NOTE[data["status"]][lang],
    ), results
