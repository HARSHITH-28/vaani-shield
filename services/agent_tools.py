
"""
VaaniShield Agent Tool Router

Provides controlled access to banking, financial intelligence, scam, and
customer-support business services.

The LLM cannot directly access or modify the database. Customer scope is
provided by a server-created principal, never by tool arguments.
"""

from datetime import datetime, timezone

from services.banking_service import (
    get_customer_profile,
    get_customer_profile_by_key,
    get_account_balance,
    get_recent_transactions,
    search_transactions,
    get_transaction_by_public_reference,
    get_card_details,
    get_loan_details,
    calculate_emi,
)
from services.database import DEMO_CUSTOMER_KEYS
from services.principal import CustomerPrincipal

from services.financial_intelligence import (
    get_spending_summary,
    get_category_breakdown,
    get_top_merchants,
    find_large_transactions,
    get_monthly_transaction_summary,
)

from services.financial_advisor import (
    compare_monthly_spending,
    calculate_savings_rate,
    detect_unusual_spending,
    generate_budget_recommendations,
    generate_financial_advisory_report,
)

from services.customer_context import get_customer_context
from services.scam_assessment_service import create_assessment, get_assessment
from services.customer_support_service import (
    CUSTOMER_CLOSE_ELIGIBLE,
    get_support_case,
    get_support_case_events,
    list_support_cases,
)
from services.pending_action_service import create_pending_action
from services.transaction_risk_persistence import (
    create_transaction_assessment,
    ensure_transaction_public_refs,
    get_transaction_assessment,
    list_transaction_assessments,
)
from services.investigation_service import (
    create_pending_investigation_action, get_investigation, list_investigations,
)
from services import account_application_service as account_applications


# Account-opening tools. All are owner-scoped and return personal-data-free
# projections; none exposes document contents, and none can submit.
ACCOUNT_APPLICATION_TOOL_NAMES = frozenset({
    "start_account_application", "get_account_application",
    "update_account_application", "validate_account_application",
    "get_required_application_documents", "get_application_document_status",
    "prepare_account_application_submission", "get_account_application_status",
})

_ACCOUNT_APPLICATION_ARGUMENTS = {
    "start_account_application": ["account_type"],
    "get_account_application": ["application_ref"],
    "update_account_application": ["application_ref", "account_type", "employment_category"],
    "validate_account_application": ["application_ref"],
    "get_required_application_documents": ["account_type"],
    "get_application_document_status": ["application_ref"],
    "prepare_account_application_submission": ["application_ref"],
    "get_account_application_status": ["application_ref", "account_type"],
}


def _execute_account_application_tool(tool_name, principal, arguments):
    validate_arguments(arguments, _ACCOUNT_APPLICATION_ARGUMENTS[tool_name])
    for key, value in arguments.items():
        if not isinstance(value, str):
            raise ValueError(f"Invalid {key}.")
    if tool_name == "start_account_application":
        return account_applications.start_application(
            principal, arguments.get("account_type"), include_personal=False,
        )
    if tool_name == "get_account_application":
        return account_applications.get_application(
            principal, arguments.get("application_ref"), include_personal=False,
        )
    if tool_name == "update_account_application":
        changes = {k: v for k, v in arguments.items() if k != "application_ref"}
        if not changes:
            raise ValueError("Provide a value to update.")
        ref = arguments.get("application_ref")
        if not ref:
            current = account_applications.get_current_application(principal, include_personal=False)
            ref = current["application_ref"]
        return account_applications.update_application(
            principal, ref, changes, include_personal=False,
        )
    if tool_name == "validate_account_application":
        return account_applications.validate_application(
            principal, arguments.get("application_ref"), include_personal=False,
        )
    if tool_name == "get_required_application_documents":
        return account_applications.get_required_documents(arguments.get("account_type"))
    if tool_name == "get_application_document_status":
        return account_applications.get_document_status(principal, arguments.get("application_ref"))
    if tool_name == "prepare_account_application_submission":
        prepared = account_applications.prepare_submission(
            principal, arguments.get("application_ref"), include_personal=False,
        )
        # The review digest is a client-only confirmation binding.
        return {key: value for key, value in prepared.items() if key != "review_digest"}
    return account_applications.get_application_status(
        principal, arguments.get("application_ref"), arguments.get("account_type"),
    )


# --------------------------------------------------
# TOOL DEFINITIONS
# --------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "assess_current_scam_message",
        "description": "Run the deterministic scam assessment on the current authenticated user's message or transcript. Takes no arguments.",
        "parameters": {},
    },
    {
        "name": "get_scam_assessment",
        "description": "Retrieve the scam assessment explicitly referenced by the current authenticated assistant request. Takes no arguments.",
        "parameters": {},
    },
    {
        "name": "assess_transaction_risk",
        "description": "Run the deterministic transaction-risk engine for an authenticated customer's transaction using its opaque transaction_ref. Never infer or invent a reference; first retrieve actual transactions if needed.",
        "parameters": {"transaction_ref": "opaque transaction reference"},
    },
    {
        "name": "list_transaction_risk_assessments",
        "description": "List recent transaction-risk assessments belonging to the authenticated customer.",
        "parameters": {"limit": "integer, optional", "cursor": "string, optional"},
    },
    {
        "name": "get_transaction_risk_assessment",
        "description": "Retrieve an authenticated customer's transaction-risk assessment by its public assessment_id.",
        "parameters": {"assessment_id": "UUID public assessment identifier"},
    },
    {
        "name": "get_customer_profile",
        "description": "Retrieve the authenticated customer's profile."
    },
    {
        "name": "get_account_balance",
        "description": "Retrieve balances of the customer's accounts."
    },
    {
        "name": "get_recent_transactions",
        "description": "Retrieve recent account transactions.",
        "parameters": {
            "limit": "integer, optional"
        }
    },
    {
        "name": "search_transactions",
        "description": "Search the authenticated customer's transactions by merchant, category, type, amount, or date range. Results use opaque transaction references.",
        "parameters": {
            "merchant": "string, optional",
            "category": "string, optional",
            "transaction_type": "DEBIT or CREDIT, optional",
            "min_amount": "number, optional",
            "max_amount": "number, optional",
            "start_date": "YYYY-MM-DD, optional",
            "end_date": "YYYY-MM-DD, optional",
        }
    },
    {
        "name": "get_transaction_details",
        "description": "Retrieve one transaction by an opaque transaction_ref previously supplied or returned by an owner-scoped transaction list. Never guess a reference.",
        "parameters": {"transaction_ref": "opaque transaction reference"},
    },
    {
        "name": "get_card_details",
        "description": "Retrieve the customer's card details."
    },
    {
        "name": "get_loan_details",
        "description": "Retrieve the customer's loan details, including active/closed loans, monthly EMI, next EMI due date, remaining tenure, and outstanding amount."
    },
    {
        "name": "calculate_emi",
        "description": "Calculate the EMI for a hypothetical loan.",
        "parameters": {
            "principal": "number",
            "annual_interest_rate": "number",
            "tenure_months": "integer"
        }
    },
    {
        "name": "get_spending_summary",
        "description": "Retrieve all-time totals over recorded successful credits and debits only; these are synthetic demo records, not live bank data."
    },
    {
        "name": "get_monthly_transaction_summary",
        "description": "Retrieve successful credit/debit totals and transaction counts for one calendar month.",
        "parameters": {"year": "integer", "month": "integer"},
    },
    {
        "name": "get_category_breakdown",
        "description": "Retrieve successful debit spending grouped by category, sorted from highest spending category to lowest."
    },
    {
        "name": "get_top_merchants",
        "description": "Retrieve all-time successful debit spending aggregated by merchant.",
        "parameters": {
            "limit": "integer, optional"
        }
    },
    {
        "name": "find_large_transactions",
        "description": "Find large successful debit transactions.",
        "parameters": {
            "threshold": "number, optional",
            "limit": "integer, optional; return the highest matching transactions first",
        }
    },
    {
        "name": "compare_monthly_spending",
        "description": "Compare spending with the previous month.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "calculate_savings_rate",
        "description": "Calculate estimated savings and savings rate.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "detect_unusual_spending",
        "description": "Identify unusual category-level spending increases.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "generate_budget_recommendations",
        "description": "Generate illustrative budget recommendations.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "generate_financial_advisory_report",
        "description": "Generate a complete financial advisory report.",
        "parameters": {
            "year": "integer",
            "month": "integer"
        }
    },
    {
        "name": "get_customer_context",
        "description": "Retrieve consolidated banking and financial context.",
        "parameters": {
            "year": "integer, optional",
            "month": "integer, optional"
        }
    },
    {
        "name": "list_support_tickets",
        "description": "List the authenticated customer's synthetic support tickets. No customer identifier is accepted.",
        "parameters": {
            "limit": "integer between 1 and 50, optional",
            "status": "OPEN, IN_PROGRESS, WAITING_FOR_CUSTOMER, RESOLVED, or CLOSED, optional",
            "category": "account, card, transaction, digital_banking, loan, deposit, or other, optional",
            "priority": "LOW, MEDIUM, HIGH, or URGENT, optional",
        },
    },
    {
        "name": "get_support_ticket",
        "description": "Retrieve one of the authenticated customer's support tickets by its public ticket reference.",
        "parameters": {"ticket_ref": "VS-prefixed public ticket reference"},
    },
    {
        "name": "get_support_ticket_events",
        "description": "Retrieve status history for one of the authenticated customer's support tickets.",
        "parameters": {"ticket_ref": "VS-prefixed public ticket reference"},
    },
    {
        "name": "propose_support_ticket",
        "description": "Prepare, but do not submit, a synthetic support ticket proposal. Use only when the customer asks to raise a complaint. The backend will validate it and require explicit button confirmation before creation.",
        "parameters": {
            "category": "account, card, transaction, digital_banking, loan, deposit, or other",
            "priority": "LOW, MEDIUM, HIGH, or URGENT",
            "subject": "short summary of the complaint",
            "description": "optional detailed complaint description",
        },
    },
    {
        "name": "prepare_support_ticket_close",
        "description": "Prepare a button-confirmation prompt to close the authenticated customer's own eligible ticket. This tool never closes a ticket.",
        "parameters": {"ticket_ref": "VS-prefixed public ticket reference"},
    },
    {"name": "list_investigations", "description": "List the authenticated customer's personal synthetic security investigations.", "parameters": {"limit": "integer, optional", "cursor": "string, optional", "status": "string, optional", "category": "string, optional"}},
    {"name": "get_investigation", "description": "Retrieve an owner-scoped personal security investigation by its public reference.", "parameters": {"investigation_ref": "public investigation reference"}},
    {"name": "propose_create_investigation", "description": "Prepare a customer investigation draft and ask for explicit button confirmation. Never creates a record directly.", "parameters": {"title": "string", "category": "string", "summary": "string", "evidence": "array of owned assessment references, optional"}},
    {"name": "propose_link_investigation_evidence", "description": "Prepare a proposal to link an existing owned scam or transaction-risk assessment.", "parameters": {"investigation_ref": "public investigation reference", "evidence_type": "assessment type", "assessment_id": "public assessment UUID"}},
    {"name": "propose_investigation_note", "description": "Prepare a proposal to add customer-reported information to an owned investigation.", "parameters": {"investigation_ref": "public investigation reference", "note": "customer-provided note"}},
    {"name": "propose_investigation_status", "description": "Prepare a status change proposal for an owned investigation; explicit confirmation is required.", "parameters": {"investigation_ref": "public investigation reference", "status": "UNDER_REVIEW, WAITING_FOR_INFORMATION, or RESOLVED"}},
    {"name": "propose_close_investigation", "description": "Prepare a proposal to close an owned personal investigation; explicit confirmation is required.", "parameters": {"investigation_ref": "public investigation reference"}},
    {
        "name": "start_account_application",
        "description": "Start or resume the authenticated customer's synthetic demo account-opening draft. Never submits anything.",
        "parameters": {"account_type": "SAVINGS, SALARY or CURRENT, optional"},
    },
    {
        "name": "get_account_application",
        "description": "Retrieve the progress of the customer's current synthetic account application (no personal data returned).",
        "parameters": {},
    },
    {
        "name": "update_account_application",
        "description": "Save an enumerated choice on the customer's draft application. Free-text personal details are entered only in the application form.",
        "parameters": {"account_type": "SAVINGS, SALARY or CURRENT, optional", "employment_category": "enumerated employment category, optional"},
    },
    {
        "name": "validate_account_application",
        "description": "Run the deterministic validation service on the customer's draft application and list what is missing.",
        "parameters": {},
    },
    {
        "name": "get_required_application_documents",
        "description": "List the synthetic demo account types or the demo documents one account type requires.",
        "parameters": {"account_type": "SAVINGS, SALARY or CURRENT, optional"},
    },
    {
        "name": "get_application_document_status",
        "description": "Show which demo KYC documents are uploaded or missing. Never returns document contents.",
        "parameters": {},
    },
    {
        "name": "prepare_account_application_submission",
        "description": "Validate the draft and prepare a review. This never submits; submission needs the customer's explicit Confirm button.",
        "parameters": {},
    },
    {
        "name": "get_account_application_status",
        "description": "Retrieve the status of the customer's own account application by optional public reference or account type.",
        "parameters": {"application_ref": "VSA-prefixed public reference, optional", "account_type": "SAVINGS, SALARY or CURRENT, optional"},
    },
]


# --------------------------------------------------
# ARGUMENT VALIDATION
# --------------------------------------------------

def validate_month(year, month):
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError("Invalid year.")

    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")


def validate_arguments(arguments, allowed, required=None):
    required = required or []

    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a dictionary.")

    unknown = set(arguments) - set(allowed)

    if unknown:
        raise ValueError(
            f"Unsupported arguments: {sorted(unknown)}"
        )

    missing = set(required) - set(arguments)

    if missing:
        raise ValueError(
            f"Missing required arguments: {sorted(missing)}"
        )


def _without_customer_identity(value):
    """Keep internal identity fields out of model-visible tool results."""
    if isinstance(value, dict):
        return {
            key: _without_customer_identity(item)
            for key, item in value.items()
            if key not in {
                "customer_id", "customer_key", "case_id", "event_id",
                "investigation_id", "evidence_id", "internal_id",
            }
        }
    if isinstance(value, list):
        return [_without_customer_identity(item) for item in value]
    return value


def _agent_transaction_rows(principal, rows):
    """Replace internal and legacy transaction identifiers with opaque refs."""
    if not isinstance(rows, list):
        return []
    refs = ensure_transaction_public_refs(
        principal.customer_key,
        [row.get("transaction_id") for row in rows if isinstance(row, dict)],
    )
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        transaction_id = row.get("transaction_id")
        safe = {
            key: value for key, value in row.items()
            if key not in {"transaction_id", "account_id", "customer_id", "customer_key", "reference_id"}
        }
        if transaction_id in refs:
            safe["transaction_ref"] = refs[transaction_id]
        result.append(safe)
    return result


# --------------------------------------------------
# TOOL EXECUTION
# --------------------------------------------------

def execute_tool(
    tool_name, principal, arguments=None, *, current_user_text=None,
    detected_languages=None, is_code_mixed=False, assessment_reference=None,
    source_mode="typed_message", conversation_id=None, original_turn_id=None,
):
    """
    Execute an approved business-service tool. State changes are limited to
    storing a pending support-ticket proposal; tickets are created only by
    the separate authenticated confirmation flow.

    Customer identity is supplied as server-created principal context, never
    as an LLM-generated customer ID or customer key argument.
    """

    if not isinstance(principal, CustomerPrincipal):
        raise ValueError("An authenticated customer principal is required.")

    allowed_customer_keys = frozenset(DEMO_CUSTOMER_KEYS.values())
    if (
        principal.auth_method != "demo_cookie"
        or principal.customer_key not in allowed_customer_keys
        or principal.expires_at <= datetime.now(timezone.utc)
    ):
        raise ValueError("Invalid or expired authenticated customer principal.")

    profile = get_customer_profile_by_key(principal.customer_key)
    if profile is None:
        raise ValueError("Authenticated customer is unavailable.")

    customer_id = profile["customer_id"]

    arguments = arguments or {}

    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be a dictionary.")

    if tool_name == "list_support_tickets":
        validate_arguments(arguments, ["limit", "status", "category", "priority"])
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("Invalid support ticket limit.")
        return _without_customer_identity(list_support_cases(
            principal,
            limit=limit,
            status=arguments.get("status"),
            category=arguments.get("category"),
            priority=arguments.get("priority"),
        ))
    elif tool_name in ACCOUNT_APPLICATION_TOOL_NAMES:
        # Draft/read tools only. There is deliberately no submit tool: final
        # submission is an authenticated button action on the REST API.
        return _execute_account_application_tool(tool_name, principal, arguments)
    elif tool_name == "list_investigations":
        validate_arguments(arguments, ["limit", "cursor", "status", "category"])
        limit = arguments.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Invalid investigation limit.")
        return _without_customer_identity(list_investigations(
            principal, limit=limit, cursor=arguments.get("cursor"),
            status=arguments.get("status"), category=arguments.get("category"),
        ))
    elif tool_name == "get_investigation":
        validate_arguments(arguments, ["investigation_ref"], required=["investigation_ref"])
        return _without_customer_identity(get_investigation(principal, arguments["investigation_ref"]))
    elif tool_name in {
        "propose_create_investigation", "propose_link_investigation_evidence",
        "propose_investigation_note", "propose_investigation_status",
        "propose_close_investigation",
    }:
        if conversation_id is None or original_turn_id is None:
            raise ValueError("The investigation confirmation context is unavailable.")
        action_types = {
            "propose_create_investigation": "create_investigation",
            "propose_link_investigation_evidence": "link_investigation_evidence",
            "propose_investigation_note": "add_investigation_note",
            "propose_investigation_status": "change_investigation_status",
            "propose_close_investigation": "close_investigation",
        }
        if tool_name == "propose_link_investigation_evidence":
            action = {
                "action_type": action_types[tool_name],
                "investigation_ref": arguments.get("investigation_ref"),
                "evidence": {
                    "evidence_type": arguments.get("evidence_type"),
                    "assessment_id": arguments.get("assessment_id"),
                },
            }
        else:
            action = {"action_type": action_types[tool_name], **arguments}
        pending = create_pending_investigation_action(
            principal, conversation_id, original_turn_id, action,
        )
        return pending.model_dump(mode="json")
    elif tool_name == "get_support_ticket":
        validate_arguments(arguments, ["ticket_ref"], required=["ticket_ref"])
        return _without_customer_identity(
            get_support_case(principal, arguments["ticket_ref"])
        )
    elif tool_name == "get_support_ticket_events":
        validate_arguments(arguments, ["ticket_ref"], required=["ticket_ref"])
        return _without_customer_identity(
            get_support_case_events(principal, arguments["ticket_ref"])
        )
    elif tool_name == "propose_support_ticket":
        validate_arguments(
            arguments,
            ["category", "priority", "subject", "description", "language", "amount"],
            required=["category", "priority", "subject"],
        )
        if conversation_id is None or original_turn_id is None:
            raise ValueError("The support proposal context is unavailable.")
        action = {"action_type": "create_support_ticket", **arguments}
        if action.get("language") is None and detected_languages:
            action["language"] = detected_languages[0]
        pending = create_pending_action(
            principal, conversation_id, original_turn_id, action
        )
        return pending.model_dump(mode="json")
    elif tool_name == "prepare_support_ticket_close":
        validate_arguments(arguments, ["ticket_ref"], required=["ticket_ref"])
        if conversation_id is None or original_turn_id is None:
            raise ValueError("The support confirmation context is unavailable.")
        ticket = get_support_case(principal, arguments["ticket_ref"])
        if ticket["status"] not in CUSTOMER_CLOSE_ELIGIBLE:
            return {
                "ticket_ref": ticket["ticket_ref"],
                "subject": ticket["subject"],
                "current_status": ticket["status"],
                "is_synthetic_demo": True,
                "customer_close_eligible": False,
            }
        return {
            "ticket_ref": ticket["ticket_ref"],
            "subject": ticket["subject"],
            "current_status": ticket["status"],
            "is_synthetic_demo": True,
            "customer_close_eligible": True,
        }

    # Scam tool context is injected by the trusted orchestration layer. The
    # model schemas intentionally expose no text, customer or assessment IDs.
    if tool_name == "assess_current_scam_message":
        validate_arguments(arguments, [])
        if not isinstance(current_user_text, str) or not current_user_text.strip():
            raise ValueError("Current user message is required for assessment.")
        if source_mode not in {"typed_message", "voice_call"}:
            raise ValueError("Unsupported assessment source.")
        return create_assessment(
            principal,
            current_user_text,
            "auto",
            _source_mode=source_mode,
            _detected_languages=detected_languages,
            _is_code_mixed=is_code_mixed,
        )
    elif tool_name == "get_scam_assessment":
        validate_arguments(arguments, [])
        if assessment_reference is None:
            raise ValueError("A trusted assessment reference is required.")
        return get_assessment(principal, assessment_reference)
    elif tool_name == "assess_transaction_risk":
        validate_arguments(arguments, ["transaction_ref"], required=["transaction_ref"])
        if not isinstance(arguments["transaction_ref"], str):
            raise ValueError("Invalid transaction reference.")
        return create_transaction_assessment(principal, arguments["transaction_ref"])
    elif tool_name == "list_transaction_risk_assessments":
        validate_arguments(arguments, ["limit", "cursor"])
        limit = arguments.get("limit", 10)
        cursor = arguments.get("cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Invalid transaction-risk assessment limit.")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
            raise ValueError("Invalid transaction-risk assessment cursor.")
        return list_transaction_assessments(principal, limit=limit, cursor=cursor)
    elif tool_name == "get_transaction_risk_assessment":
        validate_arguments(arguments, ["assessment_id"], required=["assessment_id"])
        try:
            from uuid import UUID
            public_assessment_id = UUID(arguments["assessment_id"])
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid public assessment identifier.") from None
        return get_transaction_assessment(principal, public_assessment_id)

    # Customer profile
    if tool_name == "get_customer_profile":
        validate_arguments(arguments, [])
        return _without_customer_identity(get_customer_profile(customer_id))

    # Account balance
    elif tool_name == "get_account_balance":
        validate_arguments(arguments, [])
        return get_account_balance(customer_id)

    # Recent transactions
    elif tool_name == "get_recent_transactions":
        validate_arguments(arguments, ["limit"])
        limit = arguments.get("limit", 10)

        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100.")

        return _agent_transaction_rows(
            principal, get_recent_transactions(customer_id, limit)
        )

    # Transaction search
    elif tool_name == "search_transactions":
        validate_arguments(
            arguments,
            ["merchant", "category", "transaction_type", "min_amount", "max_amount", "start_date", "end_date"]
        )

        merchant = arguments.get("merchant")
        category = arguments.get("category")
        transaction_type = arguments.get("transaction_type")
        min_amount = arguments.get("min_amount", 0)
        max_amount = arguments.get("max_amount")
        start_date = arguments.get("start_date")
        end_date = arguments.get("end_date")

        if merchant is not None and (not isinstance(merchant, str) or len(merchant) > 160):
            raise ValueError("Merchant filter is invalid.")

        if category is not None and not isinstance(category, str):
            raise ValueError("Category must be a string.")

        if transaction_type is not None and transaction_type not in {"DEBIT", "CREDIT"}:
            raise ValueError("Transaction type is invalid.")

        if (
            isinstance(min_amount, bool)
            or not isinstance(min_amount, (int, float))
            or min_amount < 0
        ):
            raise ValueError("Minimum amount must be non-negative.")

        if max_amount is not None and (
            isinstance(max_amount, bool) or not isinstance(max_amount, (int, float))
            or max_amount < 0 or max_amount < min_amount
        ):
            raise ValueError("Maximum amount is invalid.")

        if (start_date is None) != (end_date is None):
            raise ValueError("A complete date range is required.")
        if start_date is not None:
            from datetime import date
            try:
                date.fromisoformat(start_date)
                date.fromisoformat(end_date)
            except (TypeError, ValueError):
                raise ValueError("Date filter is invalid.") from None
            if start_date > end_date:
                raise ValueError("Date range is invalid.")

        return _agent_transaction_rows(
            principal,
            search_transactions(
                customer_id,
                merchant=merchant,
                category=category,
                transaction_type=transaction_type,
                min_amount=min_amount,
                max_amount=max_amount,
                start_date=start_date,
                end_date=end_date,
            ),
        )

    elif tool_name == "get_transaction_details":
        validate_arguments(arguments, ["transaction_ref"], required=["transaction_ref"])
        reference = arguments["transaction_ref"]
        if not isinstance(reference, str) or not __import__("re").fullmatch(r"TR-[A-F0-9]{48}", reference):
            raise ValueError("Invalid transaction reference.")
        transaction = get_transaction_by_public_reference(customer_id, reference)
        if transaction is not None:
            transaction["transaction_ref"] = reference
        return transaction

    # Cards
    elif tool_name == "get_card_details":
        validate_arguments(arguments, [])
        return get_card_details(customer_id)

    # Loans
    elif tool_name == "get_loan_details":
        validate_arguments(arguments, [])
        return get_loan_details(customer_id)

    # EMI calculation
    elif tool_name == "calculate_emi":
        validate_arguments(
            arguments,
            [
                "principal",
                "annual_interest_rate",
                "tenure_months"
            ],
            required=[
                "principal",
                "annual_interest_rate",
                "tenure_months"
            ]
        )

        return calculate_emi(
            arguments["principal"],
            arguments["annual_interest_rate"],
            arguments["tenure_months"]
        )

    # Spending summary
    elif tool_name == "get_spending_summary":
        validate_arguments(arguments, [])
        return get_spending_summary(customer_id)

    elif tool_name == "get_monthly_transaction_summary":
        validate_arguments(arguments, ["year", "month"], required=["year", "month"])
        validate_month(arguments["year"], arguments["month"])
        return get_monthly_transaction_summary(customer_id, arguments["year"], arguments["month"])

    # Category breakdown
    elif tool_name == "get_category_breakdown":
        validate_arguments(arguments, [])
        return get_category_breakdown(customer_id)

    # Top merchants
    elif tool_name == "get_top_merchants":
        validate_arguments(arguments, ["limit"])
        limit = arguments.get("limit", 5)

        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Limit must be between 1 and 50.")

        return get_top_merchants(customer_id, limit)

    # Large transactions
    elif tool_name == "find_large_transactions":
        validate_arguments(arguments, ["threshold", "limit"])
        threshold = arguments.get("threshold", 10000)
        limit = arguments.get("limit")

        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or threshold < 0
        ):
            raise ValueError("Threshold must be non-negative.")

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise ValueError("Limit must be between 1 and 100.")
        return _agent_transaction_rows(
            principal, find_large_transactions(customer_id, threshold, limit=limit)
        )

    # Monthly spending comparison
    elif tool_name == "compare_monthly_spending":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return compare_monthly_spending(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Savings analysis
    elif tool_name == "calculate_savings_rate":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return calculate_savings_rate(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Unusual spending
    elif tool_name == "detect_unusual_spending":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return detect_unusual_spending(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Budget recommendations
    elif tool_name == "generate_budget_recommendations":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return generate_budget_recommendations(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Complete advisory report
    elif tool_name == "generate_financial_advisory_report":
        validate_arguments(
            arguments,
            ["year", "month"],
            ["year", "month"]
        )

        validate_month(arguments["year"], arguments["month"])

        return generate_financial_advisory_report(
            customer_id,
            arguments["year"],
            arguments["month"]
        )

    # Complete customer context
    elif tool_name == "get_customer_context":
        validate_arguments(arguments, ["year", "month"])

        year = arguments.get("year")
        month = arguments.get("month")

        if (year is None) != (month is None):
            raise ValueError(
                "Provide both year and month, or neither."
            )

        if year is not None:
            validate_month(year, month)

        return _without_customer_identity(
            get_customer_context(customer_id, year, month)
        )

    else:
        raise ValueError(
            f"Tool '{tool_name}' is not authorized."
        )


def get_available_tools():
    """Return the approved tool definitions."""

    return TOOL_DEFINITIONS.copy()
