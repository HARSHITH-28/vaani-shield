# VaaniShield API Contract — v1

This document describes implemented demo-authentication, conversation,
assistant, speech, and authenticated self-service dashboard routes, alongside
proposed feature-module contracts. Legacy unversioned customer routes remain
mounted as authenticated compatibility routes.

## Current architecture and reuse

The current FastAPI application in `backend/main.py` exposes unversioned
customer and financial routes. `backend/agent_routes.py` contains demo agent
actions, and `backend/voice_routes.py` provides separate Prisma/Timbre HTTP
wrappers. Reusable data and calculations live in:

- `services/banking_service.py` — balances, transaction history/search, cards,
  loans, and card-block flow.
- `services/financial_intelligence.py` and `services/financial_advisor.py` —
  spending summaries, category/merchant totals, monthly comparisons, and
  illustrative recommendations.
- `services/agent_tools.py` — local allow-listed service dispatcher; it is not
  connected to Evon.
- `services/gnani_speech.py` — Prisma and Timbre adapters; no v1 contracts call
  the providers directly from the client.

Pydantic models for implemented and proposed contracts are in
`backend/api_models.py`. Implemented routes are identified below.

## Shared rules

- Every v1 customer-data route requires an authenticated server-side principal. Resolve the
  stable customer key from that principal; never accept a `customer_id` from a
  request, URL, or model tool arguments. Internal SQLite IDs remain inside the
  service layer.
- Use JSON for requests unless an endpoint says `multipart/form-data`. All
  timestamps are ISO 8601 with timezone information; amounts are decimal
  numbers in INR unless a response says otherwise.
- Unknown request fields are rejected. Validate date ranges, amount ranges,
  pagination bounds, upload size/type, and resource ownership before accessing
  services.
- Lists use an opaque `cursor` and `limit` (default 25, maximum 100), and return
  `items`, `next_cursor`, and `has_more`.
- Errors use `ErrorResponse`: `{ "error": { "code", "message", "request_id",
  "retryable", "field_errors" } }`. Validation is 422; unauthenticated is 401;
  forbidden is 403; missing resources are 404; invalid state/duplicate action
  is 409; rate limits are 429; oversized media is 413; provider failures are
  502/503. Do not return provider credentials, raw provider errors, or stack
  traces.
- A period is either `start_date` + `end_date` or `year` + `month`. Supplying
  only one endpoint or mixing the two forms is invalid. Omitted period means
  the latest complete calendar month for period-based financial summaries.

## Voice and assistant

### Demo authentication foundation

| Method and path | Request | Response | Purpose |
|---|---|---|---|
| `POST /api/v1/auth/demo/session` | `DemoSessionRequest` | `DemoSessionResponse` plus an HttpOnly session cookie | Start a demo session for one of the three fixed synthetic customer keys. |
| `GET /api/v1/auth/demo/session` | Session cookie | `DemoSessionResponse` | Resolve the signed session to the current server-side principal. |
| `DELETE /api/v1/auth/demo/session` | Session cookie, if present | `DemoLogoutResponse`; clears cookie | Sign out the current browser. |

This is deliberately **DEMO authentication**: it has no password or real
identity proof, and any caller can select one of the allow-listed synthetic
profiles. It must not be treated as production banking authentication. The
backend signs one-hour HMAC-SHA256 tokens using
`VAANISHIELD_DEMO_SESSION_SECRET` (minimum 32 bytes); the signing secret is
never returned. Tokens are carried only in an HttpOnly, SameSite=Strict cookie.
The host-only cookie uses path `/api` so the same authenticated session covers
versioned APIs and the retained `/api/customers...` compatibility routes.
`VAANISHIELD_DEMO_COOKIE_SECURE=true` enables Secure cookies when serving over
HTTPS; its local HTTP default is false.

The reusable FastAPI dependency resolves that cookie to a server-created
principal containing `customer_key`, session ID, issuance/expiry, and auth
method. Assistant request models reject extra customer identity fields. The
tool dispatcher takes the principal as a separate server argument, resolves
the internal SQLite ID itself, rejects identity fields in tool arguments, and
removes internal customer IDs/keys from model-visible profile/context results.
Legacy `/api/customers...` routes remain available during migration. They use
the same principal, reject path references that do not match its stable key,
and apply safe response projections. New frontend dashboard requests use the
versioned self-service routes below and do not send a customer reference.

## Authenticated self-service dashboard APIs (implemented)

All routes in this section require the demo session cookie and resolve the
customer exclusively from `get_authenticated_principal`. No request path or
query parameter selects a customer. SQLite IDs, customer keys, email and phone
are omitted; account numbers are masked. These APIs expose existing synthetic
demo data and do not constitute production banking authentication.

| Method and path | Query parameters | Response |
|---|---|---|
| `GET /api/v1/me/context` | Optional `year` and `month` together; omitted means latest transaction month | Safe aggregate matching the existing dashboard context shape |
| `GET /api/v1/me/accounts` | None | Array of account type, masked account number, balance, status |
| `GET /api/v1/me/balance` | None | Total balance and `INR` currency |
| `GET /api/v1/me/transactions` | `limit` (1-100, default 100) | Recent transaction array, newest first; includes an additive opaque `transaction_ref` for transaction-risk workflows |
| `GET /api/v1/me/transactions/search` | `merchant`, `category`, `transaction_type`, `min_amount`, `max_amount`, `start_date`, `end_date` | Matching transaction array including existing reference IDs and an additive opaque `transaction_ref`; date range and amount bounds are validated |
| `GET /api/v1/me/cards` | None | Safe card summary array; no internal card IDs |
| `GET /api/v1/me/loans` | None | Safe loan summary array; no internal loan IDs |
| `GET /api/v1/me/financial/summary` | None | Existing all-time totals plus financial insight messages |
| `GET /api/v1/me/financial/categories` | None | Existing all-time category breakdown |
| `GET /api/v1/me/financial/merchants` | `limit` (1-50, default 5) | Existing all-time top merchant breakdown |
| `GET /api/v1/me/financial/monthly-comparison` | Optional `year` and `month` together; omitted means latest transaction month | Existing comparison with previous month |
| `GET /api/v1/me/financial/advisory` | Optional `year` and `month` together; omitted means latest transaction month | Existing savings, unusual-spending, and budget report without customer ID |

Transaction search accepts the current dashboard's filters. It returns all
matches, consistent with the existing search service; recent transaction
pagination remains the existing bounded `limit` behavior. Category and summary
results remain all-time, as in the existing dashboard calculations. Monthly
comparison and advisory use the existing financial-advisor calculations.

### Transaction Risk assessments (implemented)

These authenticated endpoints use the deterministic, versioned Python risk
engine and same-account transaction history. The request supplies only an
opaque transaction reference; customer identity is derived from the signed
server-side demo principal. A missing and another customer's reference both
return the same 404. SQLite transaction, account, and customer IDs are never
returned. Existing `reference_id` fields remain unchanged for Banking API
compatibility; `transaction_ref` is a separately generated random value stored
in `transaction_public_refs`.

| Method and path | Request/query | Response |
|---|---|---|
| `POST /api/v1/me/scam/assessments/transactions` | `TransactionRiskAssessmentRequest` with `transaction_ref` | `TransactionRiskAssessmentResponse`; 201 when stored, 200 when an unchanged-input assessment is reused |
| `GET /api/v1/me/scam/transaction-assessments` | `limit` (1-100, default 25), optional opaque `cursor` | `CursorPage[TransactionRiskAssessmentSummary]`; each item includes an owner-scoped public transaction projection (reference, date, merchant, amount, type, status, category) |
| `GET /api/v1/me/scam/transaction-assessments/{assessment_id}` | UUID assessment reference | Owner-scoped `TransactionRiskAssessmentDetail` with the same public transaction projection; absent or other-customer IDs return 404 |

Transaction references use 192 bits of cryptographic randomness and are issued
when the authenticated self-service transaction list/search returns a row.
Their mapping is unique, owner-checked through the transaction's account, and
does not use the legacy account-derived `reference_id`.

Assessments store the structured engine result, engine version, owner key,
transaction FK, creation time, and a SHA-256 fingerprint of the exact selected
snapshot plus bounded history used by the engine. Repeated requests reuse the
same assessment only when that fingerprint is unchanged; a changed transaction
or history yields a new assessment instead of stale results. The fingerprint
is not a credential and is never returned. Pagination cursors are offset-based
and opaque; page size is capped at 100. Raw descriptions and duplicate
transaction snapshots are not persisted in assessment rows.

### Conversation persistence (implemented)

These routes require the demo session cookie and derive ownership only from the
authenticated server principal. Unknown or other-customer conversation IDs
return the same 404 response. List/history cursors are opaque base64-encoded
offsets; `limit` defaults to 25 and is restricted to 1–100. Lists return
`items`, `next_cursor`, and `has_more`.

| Method and path | Request | Response |
|---|---|---|
| `POST /api/v1/assistant/conversations` | JSON `ConversationCreateRequest` (`title` optional) | `ConversationDetail` (201) |
| `GET /api/v1/assistant/conversations` | Query `limit`, optional `cursor` | `CursorPage[ConversationSummary]` |
| `GET /api/v1/assistant/conversations/{conversation_id}` | UUID path | `ConversationDetail` |
| `GET /api/v1/assistant/conversations/{conversation_id}/turns` | UUID path, query `limit`, optional `cursor` | `CursorPage[ConversationTurn]` |
| `DELETE /api/v1/assistant/conversations/{conversation_id}/turns/{turn_id}` | UUID path parameters | 204; 404 for unavailable/other-owner turn |

User turn rows hold text/transcript and input type (`text`/`audio`); assistant
rows hold response text. Raw audio is not stored. Turn sequence numbers are
unique within a conversation and public history is returned in ascending
sequence order. The assistant uses a separate latest-turn query for model
context; it selects the most recent 12 persisted turns in the database and
restores chronological order. Customers may soft-delete one of their own
messages; the text is erased and the turn is excluded from public history and
future model context while its row remains to preserve sequence and foreign-key
integrity. Pending actions bound to the deleted turn are declined and scrubbed
atomically. Conversation status is `active`, `archived`, or
`closed`; updating it is currently a service operation rather than an exposed
API endpoint. The first user request supplies a concise persisted title after
obvious OTP/PIN/password, long-number, and email patterns are masked.

| Method and path | Request | Response | Purpose |
|---|---|---|---|
| `POST /api/v1/voice/uploads` | Authenticated `multipart/form-data` with `file` | `AudioUploadResponse` (201) | Store a validated, owner-bound upload for 15 minutes and return its opaque ID. |
| `GET /api/v1/voice/responses/{audio_id}` | Authenticated UUID path | WAV audio payload | Fetch the owner-bound generated audio; the artifact is removed after delivery or expiry. |
| `POST /api/v1/assistant/turns` | `AssistantTurnRequest` | `AssistantTurnResponse` | Authenticated configurable LLM tool orchestration; speech modes use configured Prisma/Timbre adapters. |
| `POST /api/v1/assistant/confirmations/{confirmation_id}` | `ConfirmationRequest` | `ConfirmationResponse` | Confirm or decline a bound support-ticket proposal; authenticated and one-time. Requires `conversation_id`, `original_turn_id`, and `action_type="create_support_ticket"`. |

`conversation_id` may be omitted to start a conversation; the response returns
the server-created ID. `client_turn_id` is a caller-generated UUID used as an
idempotency key within that conversation. A retry must use the same ID and
identical input, input mode, and output mode. Failed or stale processing turns
may be retried; an in-progress duplicate is rejected and a completed duplicate
returns its saved response without invoking the model again. A reused ID with
different content is rejected. Audio that failed before a transcript was saved
may need to be recorded and uploaded again.
For `input_mode="text"`, supply non-empty `text` and omit
`audio_upload_id`. For `input_mode="speech"`, upload audio first, then supply
`audio_upload_id` and omit `text`. Output modes are `text`, `speech`, and
`speech_and_text`, allowing the four required combinations. In speech mode the
response includes the transcript; language detection may report multiple
languages and `is_code_mixed=true` for code-mixed turns.
Successful turns always retain `assistant_text`, including `output_mode="speech"`.
`assistant_turn_id` identifies the persisted assistant message separately from
the user `turn_id`, allowing either message to be deleted independently.
For speech modes, `speech_output` may be `null` if synthesis fails; the assistant
text is still returned and persisted.

Language hints are `auto`, `kn-IN`, `en-IN`, or `hi-IN`. The server returns the
detected language set and selected response language. `auto` is the default and
must allow Kannada, English, Hindi, and code-mixed speech/text.

The current orchestration endpoint supports text and uploaded speech input and
text, speech, or combined output through local banking, financial, scam, and
support services. The configured LLM provider receives only the approved tool
schemas selected for the current intent; required data tools remain available
through sequential tool calls. It receives a bounded context and compacted
tool results when needed. The legacy rule router remains a fallback when no
LLM provider is configured. Scam intent requires the approved deterministic scam
assessment tool; when no message content is provided, the assistant asks for it
without invoking a model. Scam risk fields are returned in the structured
`scam_assessment` response from the deterministic service, not model prose.
Vaani AI can use owner-scoped support-ticket retrieval tools and prepare a
validated synthetic support-ticket proposal. Preparing a proposal does not
create a ticket; only the authenticated confirmation flow can create it.
Pending actions expire
after five minutes by default (`VAANISHIELD_PENDING_ACTION_TTL_SECONDS` may be
set from 30 through 900), bind to the authenticated customer, demo session,
conversation, and original customer `turn_id`, and are consumed atomically with
ticket creation. The original turn ID is a context reference only, not an
identity or authorization credential; it must identify a user-role turn in the
bound conversation both when proposed and confirmed. The confirmation request
must include `confirmed`, `confirmation_method="button"`, the originating
`conversation_id`, `original_turn_id`, and
`action_type="create_support_ticket"`. Decline creates
no ticket; expired payloads are cleared on startup and pending-action activity.
Actions are bound to the stable customer and originating demo session.
Expired actions return `status="expired"`; replay is rejected with 409 and an
unknown or other-session action returns the same 404. Context mismatches return
409 without consuming the pending action. Spoken confirmation is not accepted
by this endpoint. Prisma and Timbre are called only through injectable
provider-neutral interfaces. Input
audio is limited to 15 MB and WAV, MP3, OGG, or WebM containers, bound to its
uploading customer, consumed once, and removed after transcription. Audio
uploads and response artifacts expire after 15 minutes. Transcripts and
assistant response text are saved in conversation history; audio bytes are not.
This existing conversation retention also applies to scam-related assistant
turns: the general conversation may retain the user's message or transcript,
while scam-assessment records retain only redacted structured output.
The local response templates cover Kannada, English, and Hindi for the current
fixed responses. Timbre uses the existing Kannada `Shivani` default;
`VAANISHIELD_TIMBRE_VOICE_EN_IN` and `VAANISHIELD_TIMBRE_VOICE_HI_IN` must name
provider-supported voices for English and Hindi.

Tool results are structured (`tool_call_id`, `tool_name`, status, summary,
optional data). They describe approved service calls; they are not raw Evon
messages. Customer identity comes from the authenticated server context and
is not a tool argument. `backend/api_models.py` provides typed action proposals
for card blocking, transaction investigation, and support-ticket creation.

The assistant returns `pending_action` with the proposal, confirmation ID,
conversation ID, and exact original user-turn ID. Those IDs are context
references, not identity credentials. The customer must review the proposal
and explicitly confirm it; the assistant never treats text or speech such as
“yes” as authorization. For closure, the assistant returns a structured
`support_close_confirmation` but makes no status change. The existing
authenticated status endpoint must receive its button-confirmation contract.
The assistant has no tool that directly creates or closes a ticket.

## Module endpoints

### 1. Banking Information Assistant
The authenticated accounts, balance, transactions, transaction search, cards,
and loans routes are implemented as listed in the self-service table above.

Natural-language balance, banking, and transaction questions continue through
the assistant-turn endpoint, which calls existing read-only banking services.

### 2. Financial Intelligence
The authenticated summary, categories, merchants, monthly comparison, and
advisory routes are implemented as listed in the self-service table above.
Summary, categories, and merchants retain the existing all-time semantics.
Comparison and advisory accept an optional year/month pair and default to the
latest transaction month.


### 3. Scam Detection

The typed-message and voice-call assessment endpoints and owner-scoped history
are implemented. They require the demo session cookie and always scope data to
the authenticated principal. For voice, first upload audio using the
authenticated `/api/v1/voice/uploads` endpoint, then submit its
`audio_upload_id`; the configured speech adapter consumes that upload once.
Original message text, complete transcript, and raw audio are not persisted.
Redacted evidence snippets and structured results are persisted. The voice
response includes the transcript for immediate review and warns that automated
transcription may contain errors. History uses `limit` (1–100, default 25)
and an opaque offset `cursor`; results are newest first. A detail lookup for
another customer's ID returns the same 404 as an unknown ID.

| Method and path | Request / response |
|---|---|
| `POST /api/v1/me/scam/assessments/messages` | Implemented. `ScamMessageAssessmentCreate` → `ScamAssessmentDetail` (201). `message_text` is 1–12,000 characters; `language_hint` is `auto`, `en-IN`, `kn-IN`, or `hi-IN`. |
| `GET /api/v1/me/scam/assessments` | Implemented. Pagination → `CursorPage[ScamAssessmentSummary]`. |
| `GET /api/v1/me/scam/assessments/{assessment_id}` | Implemented. Returns owner-scoped `ScamAssessmentDetail`; unknown and other-customer IDs both return 404. |
| `POST /api/v1/me/scam/assessments/calls` | Implemented. `ScamCallAssessmentRequest` (`audio_upload_id`, `language_hint`) → `ScamVoiceAssessmentResponse` (201). Transcription errors are returned separately; no assessment is saved if transcription fails. |
The assistant-turn contract also supports an optional `scam_assessment_id`
resource reference. On a scam-related turn, the provider may request only the
zero-argument `get_scam_assessment` tool; the backend passes the reference from
request context and the service verifies ownership. Without a reference, scam
intent requires the zero-argument `assess_current_scam_message` tool, whose
input and authenticated principal are injected by the backend. The response
includes `scam_assessment` and a server-generated summary. Model-authored text
cannot replace or override the deterministic risk fields. This is a cautious
rule-based screen, not a verified fraud finding.

The Streamlit assistant renders the structured assessment for the current
response turn. The deterministic explanation and recommendations remain in
English; only fixed interface labels are localized. Conversation-history
responses currently contain assistant text but not the structured assessment
object, so a card is not reconstructed after reloading a prior turn.

| `POST /api/v1/me/scam/assessments/transactions` | Implemented in Transaction Risk section. |
| `GET /api/v1/me/scam/investigations` | Implemented. Authenticated owner-scoped cursor list; accepts `limit` (1–100), `cursor`, `status`, `category`, and `search`; returns `CursorPage[InvestigationSummary]`. |
| `POST /api/v1/me/scam/investigations` | Implemented. `InvestigationCreateRequest` (`title`, `category`, `summary`, optional evidence references, `confirmed=true`) → `InvestigationDetail` (201). Direct form submission is explicit customer confirmation. |
| `GET /api/v1/me/scam/investigations/{investigation_ref}` | Implemented. Public `INV-` reference; owner-scoped detail includes evidence summaries and event timeline. Other-owner and unknown references both return 404. |
| `POST /api/v1/me/scam/investigations/{investigation_ref}/evidence` | Implemented. `InvestigationEvidenceLinkRequest`; assessment ownership and source type are verified before linking. |
| `POST /api/v1/me/scam/investigations/{investigation_ref}/notes` | Implemented. `InvestigationNoteRequest`; direct form submission is explicit and sensitive values are scrubbed. |
| `PATCH /api/v1/me/scam/investigations/{investigation_ref}/status` | Implemented. `InvestigationStatusUpdateRequest`; permitted customer transitions only. |
| `POST /api/v1/me/scam/investigations/{investigation_ref}/close` | Implemented. `InvestigationCloseRequest`; customer closure only. |
| `POST /api/v1/assistant/investigation-confirmations/{confirmation_id}` | Implemented. `InvestigationConfirmationRequest`; authenticated confirmation is bound to the same principal/session, conversation, original user turn and action type, and consumed atomically once. |

Assessment is distinct from investigation creation. Starting an investigation
is a state change. Vaani AI's `propose_*_investigation` tools create short-lived
pending actions only; the customer must confirm or decline using the endpoint
above. Direct submission in the authenticated Investigations form is itself an
explicit customer confirmation. Pending action payloads are scrubbed when
consumed, declined or expired. Public investigation references are random
`INV-` values; internal SQLite IDs are never returned.
Risk scores include indicators and customer-safe next steps; they are not a
claim that fraud is confirmed.

Investigation statuses are `OPEN`, `UNDER_REVIEW`, `WAITING_FOR_INFORMATION`,
`RESOLVED`, and `CLOSED`. These are customer-managed demo tracking states;
`RESOLVED` does not represent resolution by a bank. No staff assignment,
escalation, external contact, or real-bank investigation is implemented.

### 4. Customer Support Copilot

The authenticated customer ticket persistence APIs below are implemented for
synthetic demo cases. Every response carries `is_synthetic_demo=true`; these
are not real bank support tickets. Ownership is resolved from the authenticated
principal. Public ticket references are opaque `VS-` references; internal
SQLite IDs are never returned.

| Method and path | Request / response |
|---|---|
| `POST /api/v1/me/support/tickets` | Implemented. `SupportTicketCreateRequest` → `SupportTicketDetail` (201). Direct form submission is explicit customer confirmation. Origin and synthetic label are set by the server. |
| `GET /api/v1/me/support/tickets` | Implemented. `limit` (1–100, default 25), optional `cursor`, `status`, `category`, and `priority` → `CursorPage[SupportTicketSummary]`. |
| `GET /api/v1/me/support/tickets/{ticket_ref}` | Implemented. Returns owner-scoped `SupportTicketDetail`; unknown and other-customer references both return 404. |
| `PATCH /api/v1/me/support/tickets/{ticket_ref}/status` | Implemented. `SupportTicketStatusUpdateRequest` permits only explicitly confirmed customer closure (`CLOSED`). Staff-only transitions are unavailable. |
| `GET /api/v1/me/support/tickets/{ticket_ref}/events` | Implemented. Returns owner-scoped status history without internal IDs or free-text data. |

Vaani AI owner-scoped retrieval, ticket proposal, and confirmation preparation
are implemented. Resolution assistance is not implemented. No staff identity,
escalation, staff-authored
resolution, or real bank integration exists. Customer closure records an
append-only status event. The demo case service does not expose internal case
IDs.

The assistant tool allowlist includes `list_support_tickets`,
`get_support_ticket`, `get_support_ticket_events`, `propose_support_ticket`,
and `prepare_support_ticket_close`. Ticket creation tools only persist a short-
lived draft; a successful confirmation response carries the public ticket
reference. The close preparation tool is read-only and returns a structured
button-confirmation proposal; it cannot call the status mutation route.

The Scam Detection allowlist additionally includes `list_investigations`,
`get_investigation`, and five `propose_*_investigation` mutation tools. Read
tools return owner-scoped service data only. Mutation tools create a short-lived
pending action bound to the authenticated customer/session, originating
conversation, and persisted user turn. The assistant cannot execute a mutation.
Customer confirmation uses
`POST /api/v1/assistant/investigation-confirmations/{confirmation_id}`; the
server consumes the action and applies its change within one SQLite write
transaction. Existing assessment records are linked by their public UUIDs;
their contents and raw transcripts are not copied into the tracking record.

## Current versus proposed

Demo-auth, conversation-persistence, assistant-turn, authenticated speech
upload/response, self-service dashboard, scam assessment, synthetic support
ticket persistence, and personal investigation tracking are mounted. Evon is
not integrated. Transaction-risk screening remains separate from investigation
tracking; staff lifecycle remains unavailable.
Audio upload/artifact
ownership metadata is process-local, so multi-worker deployments need shared
short-lived storage before scaling beyond one application process.

## Decisions still needed

- Authentication provider and how its principal maps to the stable customer key.
- Accepted audio formats, retention period, and upload/download authorization.
- Conversation/transcript retention and deletion policy.
- Whether customers need a public API to rename, archive, or close conversations;
  the initial persistence service supports title/status changes internally.
- Whether response language follows detected input or an explicit customer setting;
  also how code-mixed detection is represented by Prisma/Evon.
- Evon tool schema/version, and which tool outputs may be sent to it.
- Scam risk thresholds and any future staff escalation policy.
- Support ticket categories, status transitions, staff ownership, and resolution
  approval rules.
- Cursor ordering and indexing for transaction history at larger data volumes.
