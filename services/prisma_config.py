"""Central defaults and environment parsing for Gnani Prisma v3 REST hints."""

from dataclasses import dataclass
import json
import os


MAX_KEYTERMS = 200
MAX_SPEAKER_NAMES = 4
MAX_TOPIC_LENGTH = 1600
SUPPORTED_CANDIDATE_LANGUAGES = frozenset({"en-IN", "kn-IN", "hi-IN"})
SUPPORTED_FORMATS = frozenset({"verbatim", "transcribe", "codemix", "transliterate"})

DEFAULT_LANGUAGE_CANDIDATES = ("en-IN", "kn-IN", "hi-IN")
DEFAULT_KEYTERMS = (
    "VaaniShield", "UPI", "IMPS", "NEFT", "RTGS", "IFSC", "OTP", "KYC", "EMI",
    "PhonePe", "Google Pay", "Paytm", "BHIM", "DMart", "BigBasket", "Swiggy",
    "Savings account", "current account", "account balance", "transaction history",
    "beneficiary", "fund transfer", "Debit card", "credit card", "loan", "EMI payment",
    "CIBIL", "refund", "failed transaction", "Suspicious transaction",
    "fraudulent transaction", "scam call", "phishing", "unauthorized payment",
    "Customer support", "complaint", "service request", "support ticket",
    "dispute resolution", "Financial advisor", "financial planning", "spending analysis",
    "savings recommendation",
)
DEFAULT_TOPIC = (
    "VaaniShield is a voice-first banking and financial security assistant. Audio may cover "
    "bank accounts and balances, transactions, UPI and other transfers, debit and credit "
    "cards, loans, expenses, and financial planning. It may also cover fraud prevention, "
    "suspicious payments, scam or phishing calls, customer support, complaints, service "
    "requests, and disputes. Speakers may code-switch among Kannada, English, and Hindi."
)
DEFAULT_SPEAKER_NAMES = ("Arjun Sharma", "Priya Nair", "Rahul Verma")


class PrismaConfigurationError(ValueError):
    """Safe local Prisma configuration validation failure."""


@dataclass(frozen=True)
class PrismaRequestSettings:
    output_mode: str
    language_candidates: tuple[str, ...]
    keyterms: tuple[str, ...]
    topic: str | None
    speaker_names: tuple[str, ...]

    def language_code_for(self, language_hint: str) -> str:
        if language_hint != "auto":
            return language_hint
        return ",".join(self.language_candidates) if self.language_candidates else "indic-auto"


def _read_json_string_list(env_name: str, default: tuple[str, ...], limit: int) -> tuple[str, ...]:
    raw = os.getenv(env_name)
    if raw is None:
        values = default
    elif not raw.strip():
        return ()
    else:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PrismaConfigurationError from exc
        if not isinstance(values, list):
            raise PrismaConfigurationError

    normalized = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise PrismaConfigurationError
        item = item.strip()
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > limit:
        raise PrismaConfigurationError
    return tuple(normalized)


def get_prisma_request_settings() -> PrismaRequestSettings:
    """Read and validate Prisma-only settings without exposing values in errors."""
    output_format = os.getenv("VAANISHIELD_PRISMA_FORMAT", "transcribe").strip().casefold()
    if output_format not in SUPPORTED_FORMATS:
        raise PrismaConfigurationError

    raw_candidates = os.getenv("VAANISHIELD_PRISMA_LANGUAGE_CANDIDATES")
    if raw_candidates is None:
        candidates = DEFAULT_LANGUAGE_CANDIDATES
    elif not raw_candidates.strip():
        candidates = ()
    else:
        candidates = tuple(dict.fromkeys(part.strip() for part in raw_candidates.split(",") if part.strip()))
        if (
            not candidates
            or len(candidates) > 4
            or any(code not in SUPPORTED_CANDIDATE_LANGUAGES for code in candidates)
        ):
            raise PrismaConfigurationError

    keyterms = _read_json_string_list(
        "VAANISHIELD_PRISMA_KEYTERMS", DEFAULT_KEYTERMS, MAX_KEYTERMS
    )
    speaker_names = _read_json_string_list(
        "VAANISHIELD_PRISMA_SPEAKER_NAMES", DEFAULT_SPEAKER_NAMES, MAX_SPEAKER_NAMES
    )
    raw_topic = os.getenv("VAANISHIELD_PRISMA_TOPIC")
    topic = DEFAULT_TOPIC if raw_topic is None else raw_topic.strip() or None
    if topic is not None and len(topic) > MAX_TOPIC_LENGTH:
        raise PrismaConfigurationError

    return PrismaRequestSettings(
        output_mode=output_format,
        language_candidates=candidates,
        keyterms=keyterms,
        topic=topic,
        speaker_names=speaker_names,
    )
