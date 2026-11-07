# Gnani Prisma v3 configuration

Prisma is selected independently with `VAANISHIELD_STT_PROVIDER=prisma`. The
authenticated assistant uploads the audio through `/api/v1/voice/uploads`; the
server consumes the owner-bound temporary file and sends the transcription
request from `services/gnani_speech.py`. Audio is not included in logs or
conversation history.

## Environment settings

All settings below are optional. Defaults are defined once in
`services/prisma_config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `VAANISHIELD_PRISMA_FORMAT` | `transcribe` | Prisma v3 output style: `verbatim`, `transcribe`, `codemix`, or `transliterate`. |
| `VAANISHIELD_PRISMA_LANGUAGE_CANDIDATES` | `en-IN,kn-IN,hi-IN` | Preferred locales for automatic language detection. Set blank to send `indic-auto` for all supported languages. |
| `VAANISHIELD_PRISMA_KEYTERMS` | Built-in VaaniShield banking vocabulary | Optional JSON string array; maximum 200 terms. Set blank to omit keyterms. |
| `VAANISHIELD_PRISMA_TOPIC` | Built-in banking/security context | Plain-text topic up to 1,600 characters. Set blank to omit it. |
| `VAANISHIELD_PRISMA_SPEAKER_NAMES` | `Arjun Sharma`, `Priya Nair`, `Rahul Verma` | Optional JSON string array of fictional demo names; maximum four. Set blank to omit names. |

The built-in keyterm vocabulary is maintained centrally in
`services/prisma_config.py` and covers the banking, payments, fraud, customer
support, and financial-planning phrases supplied for VaaniShield. An override
replaces the default list rather than appending to it.

## REST request

VaaniShield sends `POST https://api.vachana.ai/stt/v3`, authenticated by the
existing `X-API-Key-ID` header sourced from `GNANI_API_KEY`. The multipart fields
are `audio_file`, `model=prisma-v3.0`, `language_code`, and `format`; non-empty
context adds `keyterms` and `speaker_names` as JSON-encoded arrays and `topic` as
plain text. With automatic language selection, the preferred locale list is
joined into `language_code` (for example `en-IN,kn-IN,hi-IN`); when no list is
configured the value is `indic-auto`. Explicit UI language hints are sent as a
single `language_code`.

Prisma REST accepts WAV, MP3, OGG, FLAC, AAC, or M4A clips up to 30 seconds.
VaaniShield's existing upload route accepts WAV, MP3, OGG, and WebM; the Prisma
adapter rejects WebM before sending it to Gnani. For a live microphone test,
keep the clip within 30 seconds.
