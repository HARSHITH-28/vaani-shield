"""Deterministic, offline scam-risk analysis for message and transcript text.

This module deliberately has no database, network, LLM, or customer-context
dependencies. It extracts contextual indicators and produces cautious guidance;
it does not determine that a person or organization is fraudulent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
import unicodedata
from typing import Any, Iterable


ENGINE_VERSION = "1.0.0"
MAX_INPUT_LENGTH = 12_000
SUPPORTED_LANGUAGES = ("en-IN", "kn-IN", "hi-IN")


class RiskLevel(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EvidenceStrength(str, Enum):
    NONE = "NONE"
    LIMITED = "LIMITED"
    MODERATE = "MODERATE"
    STRONG = "STRONG"


@dataclass(frozen=True, slots=True)
class ScamIndicator:
    category: str
    evidence: tuple[str, ...]
    severity: RiskLevel
    explanation: str


@dataclass(frozen=True, slots=True)
class ScamRiskResult:
    risk_level: RiskLevel
    indicators: tuple[ScamIndicator, ...]
    explanation: str
    evidence_strength: EvidenceStrength
    warning: str
    recommended_steps: tuple[str, ...]
    detected_languages: tuple[str, ...]
    is_code_mixed: bool
    language_status: str
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly structure without adding identity or IDs."""
        result = asdict(self)
        result["risk_level"] = self.risk_level.value
        result["evidence_strength"] = self.evidence_strength.value
        result["indicators"] = [
            {
                **indicator,
                "severity": indicator["severity"].value,
            }
            for indicator in result["indicators"]
        ]
        return result


@dataclass(frozen=True, slots=True)
class _Pattern:
    category: str
    expression: re.Pattern[str]


def _patterns(category: str, expressions: Iterable[str]) -> tuple[_Pattern, ...]:
    return tuple(_Pattern(category, re.compile(item, re.IGNORECASE)) for item in expressions)


# These patterns look for an action in context (for example, asking someone to
# share an OTP), not just the appearance of a security-related word.
_PATTERNS = {
    "credential_request": _patterns("credential_request", (
        r"\b(?:share|send|tell|give|provide|read(?:\s+out)?|reply\s+with|forward|asked(?:\s+me)?\s+for|asking(?:\s+me)?\s+for|requested|demanded|wants(?:\s+me\s+to\s+(?:share|give|send))?)\b.{0,45}\b(?:your\s+|my\s+|the\s+)?(?:one[- ]time\s+password|otp|upi\s+pin|card\s+pin|atm\s+pin|mpin|pin|cvv|password|passcode|security\s+code)\b",
        r"\b(?:one[- ]time\s+password|otp|upi\s+pin|card\s+pin|atm\s+pin|mpin|pin|cvv|password|passcode|security\s+code)\b.{0,45}\b(?:share|send|tell|give|provide|read(?:\s+out)?|reply\s+with|forward|asked\s+for|requested|demanded)\b",
        r"\b(?:batao|bataye|bhejo|share\s+karo|send\s+karo|manga|maanga|pucha)\b.{0,35}\b(?:otp|upi\s*pin|pin|cvv|password)\b|\b(?:otp|upi\s*pin|pin|cvv|password)\b.{0,35}\b(?:batao|bataye|bhejo|share\s+karo|send\s+karo|manga|maanga|pucha)\b",
        r"(?:ಒಟಿಪಿ|ಒನ್\s*ಟೈಮ್\s*ಪಾಸ್‌ವರ್ಡ್|ಯುಪಿಐ\s*ಪಿನ್|ಪಿನ್|ಸಿವಿವಿ|ಪಾಸ್‌ವರ್ಡ್).{0,35}(?:ಹೇಳಿ|ತಿಳಿಸಿ|ಹಂಚಿ|ಕಳುಹಿಸಿ|ಕೇಳಿದರು|ಕೇಳುತ್ತಿದ್ದಾರೆ)|(?:ಹೇಳಿ|ತಿಳಿಸಿ|ಹಂಚಿ|ಕಳುಹಿಸಿ|ಕೇಳಿದರು|ಕೇಳುತ್ತಿದ್ದಾರೆ).{0,35}(?:ಒಟಿಪಿ|ಒನ್\s*ಟೈಮ್\s*ಪಾಸ್‌ವರ್ಡ್|ಯುಪಿಐ\s*ಪಿನ್|ಪಿನ್|ಸಿವಿವಿ|ಪಾಸ್‌ವರ್ಡ್)",
        r"(?:ओटीपी|वन\s*टाइम\s*पासवर्ड|यूपीआई\s*पिन|पिन|सीवीवी|पासवर्ड).{0,35}(?:बताएं|बताओ|साझा\s*करें|भेजें|बताइए|मांगा|पूछा)|(?:बताएं|बताओ|साझा\s*करें|भेजें|बताइए|मांगा|पूछा).{0,35}(?:ओटीपी|वन\s*टाइम\s*पासवर्ड|यूपीआई\s*पिन|पिन|सीवीवी|पासवर्ड)",
    )),
    "authority_impersonation": _patterns("authority_impersonation", (
        r"\b(?:we are|i am|this is|calling from|speaking from|claiming\s+to\s+be\s+from|claimed\s+to\s+be\s+from|pretending\s+to\s+be\s+from|saying\s+they\s+are\s+from)\b.{0,35}\b(?:your\s+|my\s+|the\s+)?(?:bank|rbi|reserve bank|government|police|income tax|tax department|court|customs|telecom authority|bank support|cyber\s*crime)\b",
        r"\b(?:bank|rbi|reserve bank|government|police|income tax|tax department|court|customs|telecom authority|cyber\s*crime)\s+(?:officer|agent|department|support|team|employee|representative|executive|official)\b",
        r"\b(?:bank se|bank ka officer|rbi se|sarkari adhikari)\b|(?:ನಿಮ್ಮ ಬ್ಯಾಂಕ್|ಬ್ಯಾಂಕ್ ಅಧಿಕಾರಿ|ಆರ್‌ಬಿಐ ಅಧಿಕಾರಿ|ಸರ್ಕಾರಿ ಅಧಿಕಾರಿ)|(?:बैंक अधिकारी|बैंक से|आरबीआई अधिकारी|सरकारी अधिकारी)",
    )),
    "account_threat": _patterns("account_threat", (
        r"\b(?:your\s+)?(?:bank\s+)?account\b.{0,45}\b(?:will be|is being|gets?)\s*(?:blocked|suspended|frozen|closed|disabled)\b",
        r"\b(?:block|suspend|freeze|close|disable)\b.{0,30}\b(?:your\s+)?(?:bank\s+)?account\b",
        r"\b(?:reverse|cancel|stop|refund|block)\s+(?:a\s+|the\s+|your\s+|an?\s+)?(?:suspicious|unauthorized|fraudulent|wrong|pending)?\s*(?:₹\s*[\d,]+(?:\.\d+)?\s+|rs\.?\s*[\d,]+(?:\.\d+)?\s+|inr\s*[\d,]+(?:\.\d+)?\s+)?(?:transaction|payment|debit|transfer)\b",
        r"\b(?:account|khata)\s+(?:block|band|freeze)\s+(?:ho\s+)?(?:jayega|jaayega|hoga)|\b(?:khata|account)\s+band\s+kar\s+denge\b",
        r"(?:ಖಾತೆ|ಅಕೌಂಟ್).{0,35}(?:ಬ್ಲಾಕ್|ನಿರ್ಬಂಧ|ಸ್ಥಗಿತ|ಮುಚ್ಚು).{0,25}(?:ಮಾಡಲಾಗುತ್ತದೆ|ಆಗುತ್ತದೆ|ಮಾಡುತ್ತೇವೆ|ಮಾಡುತ್ತೇವೆಂದು)|(?:ಬ್ಲಾಕ್|ನಿರ್ಬಂಧ).{0,25}(?:ಖಾತೆ|ಅಕೌಂಟ್)",
        r"(?:खाता|अकाउंट).{0,35}(?:ब्लॉक|बंद|निलंबित|फ्रीज़).{0,25}(?:होगा|कर|किया जाएगा|कर देंगे)|(?:ब्लॉक|बंद).{0,25}(?:खाता|अकाउंट)",
    )),
    "kyc_threat": _patterns("kyc_threat", (
        r"\bkyc\b.{0,55}\b(?:expire|expired|update|verify|suspend|block|blocked|incomplete|fail)\b|\b(?:update|verify)\s+(?:your\s+)?kyc\b.{0,35}\b(?:or|otherwise|else)\b.{0,25}\b(?:block|suspend|close)\b",
        r"\bkyc\s+(?:update|verify)\s+(?:nahi|nahin|nahi\s+kiya)\b.{0,35}\b(?:account|khata)\s+(?:block|band)\b|\bkyc\s+update\s+karo\s+warna\b",
        r"(?:ಕೆವೈಸಿ|ಕೆವೈಸಿ).{0,50}(?:ನವೀಕರಿಸಿ|ಅಪ್‌ಡೇಟ್|ಪರಿಶೀಲಿಸಿ).{0,35}(?:ಖಾತೆ|ಅಕೌಂಟ್).{0,25}(?:ಬ್ಲಾಕ್|ಮುಚ್ಚು|ನಿರ್ಬಂಧ)|(?:ಕೆವೈಸಿ).{0,35}(?:ಅಪೂರ್ಣ|ಅವಧಿ ಮುಗಿಯುತ್ತದೆ)",
        r"(?:केवाईसी|केवाइसी|केवाईसी).{0,50}(?:अपडेट|पूरा|सत्यापित|समाप्त).{0,35}(?:खाता|अकाउंट).{0,25}(?:ब्लॉक|बंद|निलंबित)|(?:केवाईसी).{0,35}(?:नहीं किया|नहीं हुई).{0,25}(?:खाता|अकाउंट)",
    )),
    "urgency_or_intimidation": _patterns("urgency_or_intimidation", (
        r"\b(?:immediately|right now|within\s+\d+\s*(?:minutes?|hours?)|today only|urgent|act now|final warning|last chance|or else|you will be arrested|legal action|police case)\b",
        r"\b(?:abhi|turant|foran|aaj\s+hi|warna|nahi\s+to|police\s+case|arrest)\b",
        r"(?:ತಕ್ಷಣ|ಈಗಲೇ|ಕೂಡಲೇ|ಇಲ್ಲದಿದ್ದರೆ|ಇಲ್ಲವಾದರೆ|ಪೊಲೀಸ್\s*ಪ್ರಕರಣ|ಬಂಧನ)",
        r"(?:तुरंत|अभी|तत्काल|आज\s*ही|वरना|नहीं\s*तो|पुलिस\s*केस|गिरफ्तार)",
    )),
    "payment_request": _patterns("payment_request", (
        r"\b(?:transfer|send|pay|deposit|wire)\b.{0,50}\b(?:money|funds|rs\.?|inr|rupees?|upi|fee|fine|penalty|account|qr)\b|\b(?:pay|send|transfer)\s+(?:₹|inr|rs\.?\s*)?\d",
        r"\b(?:paisa|paise|rakam)\s+(?:bhejo|transfer\s+karo|jama\s+karo)|\b(?:bhejo|bhugtan\s+karo|payment\s+karo)\b.{0,35}\b(?:paisa|paise|fees?|upi|account)\b",
        r"(?:ಹಣ|ಮೊತ್ತ|ಪಾವತಿ).{0,35}(?:ಕಳುಹಿಸಿ|ವರ್ಗಾಯಿಸಿ|ಪಾವತಿಸಿ|ಜಮಾ\s*ಮಾಡಿ)|(?:ಕಳುಹಿಸಿ|ವರ್ಗಾಯಿಸಿ|ಪಾವತಿಸಿ).{0,35}(?:ಹಣ|ಮೊತ್ತ|ಶುಲ್ಕ)",
        r"(?:पैसे|रकम|भुगतान).{0,35}(?:भेजें|ट्रांसफर|जमा|करें)|(?:भेजें|ट्रांसफर|भुगतान\s*करें).{0,35}(?:पैसे|रकम|शुल्क)",
    )),
    "remote_access_request": _patterns("remote_access_request", (
        r"\b(?:install|download|open|allow|connect|share your screen using)\b.{0,65}\b(?:anydesk|teamviewer|quick\s*support|remote\s*access|screen\s*share|screen[- ]sharing|remote[- ]control|apk)\b|\b(?:anydesk|teamviewer|quick\s*support|remote\s*access|screen[- ]sharing)\b.{0,50}\b(?:install|download|open|allow|connect)\b",
        r"\b(?:app|application)\s+(?:install|karo|karna)\b.{0,45}\b(?:screen\s*share|remote|anydesk|teamviewer)\b|\b(?:anydesk|teamviewer|remote\s*access)\s+(?:install\s+karo|download\s+karo)",
        r"(?:ಆ್ಯಪ್|ಅಪ್ಲಿಕೇಶನ್).{0,35}(?:ಸ್ಥಾಪಿಸಿ|ಇನ್‌ಸ್ಟಾಲ್).{0,40}(?:ಸ್ಕ್ರೀನ್\s*ಶೇರ್|ರಿಮೋಟ್|ಅನಿಡೆಸ್ಕ್|ಟೀಮ್‌ವ್ಯೂವರ್)|(?:ಅನಿಡೆಸ್ಕ್|ಟೀಮ್‌ವ್ಯೂವರ್|ರಿಮೋಟ್\s*ಆಕ್ಸೆಸ್).{0,35}(?:ಸ್ಥಾಪಿಸಿ|ಇನ್‌ಸ್ಟಾಲ್)",
        r"(?:ऐप|एप्लिकेशन).{0,35}(?:इंस्टॉल|डाउनलोड).{0,40}(?:स्क्रीन\s*शेयर|रिमोट|एनीडेस्क|टीमव्यूअर)|(?:एनीडेस्क|ಟೀಮ್‌ವ್ಯೂವರ್|रिमोट\s*एक्सेस).{0,35}(?:इंस्टॉल|डाउनलोड)",
    )),
    "suspicious_link_action": _patterns("suspicious_link_action", (
        r"\b(?:click|open|visit|tap|log in at|verify at)\b.{0,80}(?:https?://|www\.|bit\.ly|tinyurl|t\.co|link\b)|(?:https?://|www\.|bit\.ly|tinyurl|t\.co).{0,80}\b(?:verify|login|log in|kyc|claim|pay|update)\b",
        r"\b(?:link|url)\b.{0,50}\b(?:click|open|visit|login|verify|kyc)\b|\b(?:click|open)\s+(?:the\s+)?link\b",
        r"\b(?:sent|shared|forwarded)\s+(?:me\s+)?(?:a\s+|the\s+|this\s+)?(?:suspicious\s+|verification\s+|banking\s+|reversal\s+|kyc\s+)?(?:link|url|apk|file)\b",
        r"(?:ಲಿಂಕ್|ಕೊಂಡಿ).{0,45}(?:ತೆರೆಯಿರಿ|ಕ್ಲಿಕ್|ಒತ್ತಿ|ಪರಿಶೀಲಿಸಿ|ಕಳುಹಿಸಿದ್ದಾರೆ|ಕಳುಹಿಸಿದರು)|(?:ತೆರೆಯಿರಿ|ಕ್ಲಿಕ್|ಕಳುಹಿಸಿದ್ದಾರೆ|ಕಳುಹಿಸಿದರು).{0,35}(?:ಲಿಂಕ್|ಕೊಂಡಿ)",
        r"(?:लिंक|कड़ी).{0,45}(?:खोलें|क्लिक|दबाएं|सत्यापित|भेजा|भेजी)|(?:खोलें|क्लिक|भेजा|भेजी).{0,35}(?:लिंक|कड़ी)",
    )),
    "prize_refund_bait": _patterns("prize_refund_bait", (
        r"\b(?:you(?:'ve| have)? won|winner|prize|lottery|cashback|refund|reimbursement|reward)\b",
        r"\b(?:inaam|lottery|prize|refund|cashback|gift)\b.{0,50}\b(?:mila|jeeta|claim|receive|pay|fees?)\b|\b(?:mila|jeeta)\b.{0,35}\b(?:inaam|lottery|prize|refund)\b",
        r"(?:ಬಹುಮಾನ|ಲಾಟರಿ|ರಿಫಂಡ್|ಮರುಪಾವತಿ|ಬಹುಮಾನ ಗೆದ್ದಿರಿ|ಹಣ ವಾಪಸ್)",
        r"(?:इनाम|लॉटरी|पुरस्कार|रिफंड|वापसी|कैशबैक|जीत गए|जीता है)",
    )),
    "upfront_fee_request": _patterns("upfront_fee_request", (
        r"\b(?:pay|send|transfer|deposit)\b.{0,45}\b(?:processing|release|claim|delivery|verification|activation)?\s*(?:fee|charge|tax|customs|deposit)\b|\b(?:fee|charge|tax)\b.{0,35}\b(?:release|claim|receive|prize|refund)\b",
        r"\b(?:fees?|charge)\b.{0,35}\b(?:refund|prize|inaam|release|claim)\b|\b(?:refund|prize|inaam)\b.{0,35}\b(?:fees?|charge|pay)\b",
        r"(?:ಶುಲ್ಕ|ಫೀ|ಚಾರ್ಜ್).{0,35}(?:ಬಹುಮಾನ|ರಿಫಂಡ್|ಬಿಡುಗಡೆ|ಪಡೆಯಲು)|(?:ಬಹುಮಾನ|ರಿಫಂಡ್).{0,35}(?:ಶುಲ್ಕ|ಫೀ|ಪಾವತಿಸಿ)",
        r"(?:शुल्क|फीस|चार्ज|टैक्स).{0,35}(?:इनाम|रिफंड|राशि|जारी|पाने)|(?:इनाम|रिफंड).{0,35}(?:शुल्क|फीस|भुगतान)",
    )),
    "fake_support_context": _patterns("fake_support_context", (
        r"\b(?:customer support|bank support|helpdesk|call centre|call center|technical support|bank agent|support agent)\b",
        r"\b(?:customer care|bank help|support team|customer support)\b|\b(?:customer care|support)\s+(?:se|ka|wala)\b",
        r"(?:ಗ್ರಾಹಕ\s*ಸೇವೆ|ಬ್ಯಾಂಕ್\s*ಸಹಾಯ|ಬೆಂಬಲ\s*ತಂಡ|ಕಾಲ್\s*ಸೆಂಟರ್)",
        r"(?:ग्राहक\s*सेवा|बैंक\s*सहायता|सपोर्ट\s*टीम|कॉल\s*सेंटर)",
    )),
}

_NEGATION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:never|don't|do not|doesn't|does not|didn't|did not|won't|will not|shouldn't|should not|must not|avoid)\b.{0,45}\b(?:share|send|tell|give|provide|install|download|click|open|pay|transfer)\b",
    r"\b(?:banks?|rbi|government)\s+(?:will\s+)?never\s+(?:ask|request)\b",
    r"(?:ಹಂಚಿಕೊಳ್ಳಬೇಡಿ|ಹೇಳಬೇಡಿ|ಕಳುಹಿಸಬೇಡಿ|ಮಾಡಬೇಡಿ|ಕೇಳುವುದಿಲ್ಲ|ಕೂಡದು|ಬೇಡ)",
    r"(?:कभी\s+न\s+बताएं|न\s+बताएं|मत\s+बताएं|मत\s+भेजें|साझा\s+न\s+करें|नहीं\s+मांगता|नहीं\s+पूछता|नहीं\s+करें)",
    r"\b(?:mat\s+ batao|kabhi\s+mat|nahi\s+batana|share\s+mat\s+karo|bhejo\s+mat)\b",
))

_EDUCATIONAL_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:what is|explain|how do scammers|scam awareness|fraud awareness|training example|educational example|example of a scam|learn about scams|protect yourself from)\b",
    r"(?:ಜಾಗೃತಿ|ಶಿಕ್ಷಣ|ತಡೆಗಟ್ಟಲು|ಉದಾಹರಣೆ|ವಿವರಿಸಿ|ಹೇಗೆ\s*ತಪ್ಪಿಸಿಕೊಳ್ಳುವುದು)",
    r"(?:जागरूकता|शिक्षा|बचाव|उदाहरण|समझाइए|कैसे\s*बचें|जानकारी\s*के\s*लिए)",
))

_REPORTED_MESSAGE = re.compile(
    r"\b(?:i (?:received|got)|this (?:message|sms|email) says|the (?:message|sms|email) says|caller said|they told me|someone asked me|message reads)\b|(?:ನನಗೆ\s*(?:ಸಂದೇಶ|ಮೆಸೇಜ್)\s*ಬಂದಿತು|ಸಂದೇಶದಲ್ಲಿ\s*ಹೇಳಿದೆ)|(?:मुझे\s*(?:संदेश|मैसेज)\s*मिला|संदेश\s*में\s*लिखा\s*है)",
    re.IGNORECASE,
)

_LANGUAGE_NAMES = {
    "en-IN": "English",
    "kn-IN": "Kannada",
    "hi-IN": "Hindi",
}


def _unknown_result(
    reason: str,
    languages: tuple[str, ...] = (),
    *,
    language_status: str = "uncertain",
) -> ScamRiskResult:
    return ScamRiskResult(
        risk_level=RiskLevel.UNKNOWN,
        indicators=(),
        explanation=reason,
        evidence_strength=EvidenceStrength.NONE,
        warning="This text could not be assessed reliably. Do not share an OTP, PIN, password, or approve an unexpected payment.",
        recommended_steps=(
            "If the message asks for credentials, remote access, or money, stop and verify through your bank's official app or a known official number.",
        ),
        detected_languages=languages,
        is_code_mixed=len(languages) > 1,
        language_status=language_status,
    )


def _metadata_languages(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = value
    else:
        return ()
    found = []
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("language")
        elif not isinstance(item, str) and hasattr(item, "language"):
            item = getattr(item, "language", None)
        if isinstance(item, str) and item in SUPPORTED_LANGUAGES and item not in found:
            found.append(item)
    return tuple(found)


def _language_info(text: str, metadata: Any) -> tuple[tuple[str, ...], bool]:
    declared = _metadata_languages(metadata)
    scripts: set[str] = set()
    unsupported_alpha = False
    has_letters = False
    for character in text:
        if not unicodedata.category(character).startswith("L"):
            continue
        has_letters = True
        codepoint = ord(character)
        if 0x0C80 <= codepoint <= 0x0CFF:
            scripts.add("kn-IN")
        elif 0x0900 <= codepoint <= 0x097F:
            scripts.add("hi-IN")
        elif character.isascii():
            scripts.add("en-IN")
        else:
            unsupported_alpha = True

    # When a provider explicitly identifies romanized Hindi/Kannada, Latin
    # script alone should not overwrite that metadata. Actual non-Latin scripts
    # and strong transliteration indicators are still retained below.
    if declared and scripts == {"en-IN"} and any(code != "en-IN" for code in declared):
        scripts.clear()
    languages = set(declared) | scripts
    lower = text.casefold()
    if re.search(r"\b(?:batao|bataye|bhejo|abhi|turant|khata|paisa|inaam|karo|karna)\b", lower):
        languages.add("hi-IN")
    if re.search(r"\b(?:heli|hēḷi|maadi|madi|agutte|beku|illa|kaluhisi)\b", lower):
        languages.add("kn-IN")
    ordered = tuple(language for language in SUPPORTED_LANGUAGES if language in languages)
    return ordered, unsupported_alpha or not has_letters


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?।])\s+|[\r\n]+", text) if part.strip()]


def _match_sentences(text: str, languages: tuple[str, ...]) -> dict[str, list[str]]:
    signals: dict[str, list[str]] = {category: [] for category in _PATTERNS}
    educational_frame = _is_educational(text) and not _REPORTED_MESSAGE.search(text)
    for sentence in _sentences(text):
        if educational_frame or (
            _is_educational(sentence) and not _REPORTED_MESSAGE.search(sentence)
        ):
            continue
        for category, pattern_list in _PATTERNS.items():
            for item in pattern_list:
                if not _pattern_language_allowed(item.expression, languages):
                    continue
                for match in item.expression.finditer(sentence):
                    if _is_negated(sentence, match.start(), match.end()):
                        continue
                    # Preserve a bounded, exact sentence excerpt as evidence.
                    excerpt = sentence.strip()
                    if len(excerpt) > 320:
                        left = max(0, match.start() - 100)
                        right = min(len(sentence), match.end() + 100)
                        excerpt = sentence[left:right].strip()
                    if excerpt not in signals[category]:
                        signals[category].append(excerpt)
                    break
    return {key: value[:3] for key, value in signals.items()}


def _pattern_language_allowed(pattern: re.Pattern[str], languages: tuple[str, ...]) -> bool:
    # English and Romanized patterns can be useful in mixed speech. Kannada and
    # Devanagari expressions are selected only when their language is detected.
    source = pattern.pattern
    has_kannada = any(0x0C80 <= ord(char) <= 0x0CFF for char in source)
    has_devanagari = any(0x0900 <= ord(char) <= 0x097F for char in source)
    if has_kannada or has_devanagari:
        return (has_kannada and "kn-IN" in languages) or (
            has_devanagari and "hi-IN" in languages
        )
    return True


def _is_educational(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _EDUCATIONAL_PATTERNS)


def _is_negated(sentence: str, start: int, end: int) -> bool:
    context = sentence[max(0, start - 55):min(len(sentence), end + 40)]
    return any(pattern.search(context) for pattern in _NEGATION_PATTERNS)


def _severity_for_signals(signals: dict[str, list[str]]) -> RiskLevel:
    has = lambda name: bool(signals[name])
    credential = has("credential_request")
    remote = has("remote_access_request")
    authority = has("authority_impersonation")
    threat = has("account_threat") or has("kyc_threat")
    urgency = has("urgency_or_intimidation")
    payment = has("payment_request")
    prize = has("prize_refund_bait")
    fee = has("upfront_fee_request")
    link = has("suspicious_link_action")
    support = has("fake_support_context")

    # Critical is reserved for combinations of directly actionable, high-impact
    # requests and coercive/impersonation context; no single word can trigger it.
    if (
        credential and (authority or support) and (threat or urgency)
        or remote and (credential or payment)
        or payment and threat and urgency
        or credential and remote
        or prize and fee and urgency
    ):
        return RiskLevel.CRITICAL

    if (
        credential
        or remote and (authority or support or urgency or payment)
        or payment and (urgency or threat or authority or prize or fee)
        or threat and (authority or urgency or credential)
        or link and (credential or threat or payment)
        or prize and (fee or payment)
        or support and (credential or remote or payment)
    ):
        return RiskLevel.HIGH

    if (
        authority and (urgency or threat)
        or threat and urgency
        or prize and link
        or prize and urgency
        or link and urgency
    ):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _indicators(signals: dict[str, list[str]], risk: RiskLevel) -> tuple[ScamIndicator, ...]:
    definitions = (
        ("credential_request", "credential_request", "The text asks the recipient to disclose an authentication secret."),
        ("authority_impersonation", "authority_impersonation", "The sender claims to represent a bank or authority; the claim is not verified."),
        ("account_threat", "account_threat", "The text uses an account-blocking or suspension threat."),
        ("kyc_threat", "kyc_threat", "The text links KYC action to an account consequence."),
        ("remote_access_request", "remote_access_request", "The text asks the recipient to install or enable remote/screen access."),
        ("suspicious_link_action", "suspicious_link", "The text urges interaction with an unverified link in an urgent security or payment context. No external domain reputation or threat-intelligence lookup was performed."),
        ("payment_request", "payment_request", "The text requests a money transfer or payment."),
        ("prize_refund_bait", "prize_or_refund_claim", "The text uses a prize, lottery, refund, or reward claim."),
        ("upfront_fee_request", "upfront_fee_request", "The text requests a fee or payment to release a benefit."),
        ("urgency_or_intimidation", "urgency_or_intimidation", "The text applies urgency or intimidation."),
        ("fake_support_context", "support_impersonation_context", "The text invokes a support role; the role is not verified."),
    )
    severity = risk if risk in {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL} else RiskLevel.LOW
    return tuple(
        ScamIndicator(
            category=output_category,
            evidence=tuple(signals[signal_name]),
            severity=severity,
            explanation=description,
        )
        for signal_name, output_category, description in definitions
        if signals[signal_name]
    )


def _explanation(risk: RiskLevel, indicators: tuple[ScamIndicator, ...]) -> str:
    if risk == RiskLevel.LOW:
        return "No strong actionable scam pattern was identified in the analyzed text. This is not proof that the message or sender is safe."
    names = ", ".join(item.category.replace("_", " ") for item in indicators)
    if risk == RiskLevel.MEDIUM:
        return f"Some contextual warning signs were found ({names}), but the text alone is not enough to determine intent. Verify independently."
    if risk == RiskLevel.HIGH:
        return f"The text combines a potentially harmful request with suspicious context ({names}). Treat it cautiously and verify through an official channel. This is an advisory security assessment and does not establish who sent it."
    if risk == RiskLevel.CRITICAL:
        return f"The text combines multiple high-impact requests or threats ({names}). Do not follow the instructions until independently verified. This is an advisory security assessment and does not establish who sent it."
    return "The text could not be assessed reliably."


def _recommendations(signals: dict[str, list[str]], risk: RiskLevel) -> tuple[str, ...]:
    if risk == RiskLevel.UNKNOWN:
        return _unknown_result("Input was not assessable.").recommended_steps
    recommendations: list[str] = []
    if signals["credential_request"]:
        recommendations.append("Do not share an OTP, UPI PIN, PIN, CVV, password, or security code, including with someone claiming to be from your bank or support.")
    if signals["remote_access_request"]:
        recommendations.append("Do not install the requested remote-access app or grant screen-sharing/control permissions.")
    if signals["suspicious_link_action"]:
        recommendations.append("Do not open unverified links or enter credentials on external pages. No external domain reputation check was performed; access banking services only through verified official apps or known bank URLs.")
    if signals["payment_request"] or signals["upfront_fee_request"]:
        recommendations.append("Do not transfer money or pay a fee in response to this message until you verify the request independently.")
    if signals["account_threat"] or signals["kyc_threat"] or signals["authority_impersonation"] or signals["fake_support_context"]:
        recommendations.append("Contact the bank directly using its official app or the verified number on your card; do not rely on contact details supplied in the message or call.")
    if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        recommendations.append("If you already shared credentials, installed an app, or lost money, contact your bank immediately through an official channel and report suspected cyber fraud to 1930 (India's National Cybercrime Helpline) or cybercrime.gov.in.")
    if not recommendations:
        recommendations.append("Verify unexpected requests through an official channel before acting, and never share an OTP, PIN, CVV, or password.")
    return tuple(dict.fromkeys(recommendations))


def analyze_scam_text(
    text: Any,
    detected_languages: Any = None,
) -> ScamRiskResult:
    """Analyze a message or transcript without external services or side effects.

    Unsupported scripts and malformed/empty/oversized input produce UNKNOWN,
    not LOW, so absence of analysis is not presented as a safety result.
    """
    if not isinstance(text, str):
        return _unknown_result("The input was not valid text and could not be assessed.")
    try:
        normalized = unicodedata.normalize("NFC", text)
        normalized.encode("utf-8", errors="strict")
    except (UnicodeError, TypeError):
        return _unknown_result("The input contained malformed text and could not be assessed.")
    if len(normalized) > MAX_INPUT_LENGTH:
        return _unknown_result(f"The input exceeds the {MAX_INPUT_LENGTH}-character assessment limit.")
    normalized = normalized.strip()
    if not normalized:
        return _unknown_result("No message text was provided for assessment.")

    languages, unsupported_script = _language_info(normalized, detected_languages)
    if unsupported_script:
        return _unknown_result(
            "The text contains a language or script outside the engine's supported English, Kannada, and Hindi coverage; no reliable risk level was assigned.",
            languages,
            language_status="unsupported",
        )
    if not languages:
        return _unknown_result("The text language could not be identified reliably.")

    signals = _match_sentences(normalized, languages)
    risk = _severity_for_signals(signals)
    indicators = _indicators(signals, risk)
    if risk == RiskLevel.LOW:
        strength = EvidenceStrength.NONE
        warning = "No strong scam pattern was identified. This does not prove the message or sender is safe; verify unusual requests independently."
    elif risk == RiskLevel.MEDIUM:
        strength = EvidenceStrength.LIMITED
        warning = "Some warning signs were found. Pause and verify the request through an official channel before acting."
    elif risk == RiskLevel.HIGH:
        strength = EvidenceStrength.MODERATE
        warning = "High-risk warning: do not share credentials, install remote-access software, open suspicious links, or send money based on this message."
    else:
        strength = EvidenceStrength.STRONG
        warning = "Critical-risk warning: stop interacting and do not follow the requested instructions. Independently contact your bank using an official channel."

    return ScamRiskResult(
        risk_level=risk,
        indicators=indicators,
        explanation=_explanation(risk, indicators),
        evidence_strength=strength,
        warning=warning,
        recommended_steps=_recommendations(signals, risk),
        detected_languages=languages,
        is_code_mixed=len(languages) > 1,
        language_status="supported",
    )


assess_scam_text = analyze_scam_text

