# Viato Voice — Integration & Deployment

Viato Voice is this repo (dograh) rebranded and embedded inside **Viato CRM** as an
AI phone system. The CRM frames the Voice UI in an iframe, passes the current
**Clerk** session through as auth, bills calls against the CRM **token system**, and
provisions Twilio numbers from its existing Twilio wiring.

```
Viato CRM (Clerk)  ──iframe + Clerk JWT (postMessage)──▶  Viato Voice (this app)
        ▲  /api/voice/usage (post-call debit)                FastAPI: AUTH_PROVIDER=clerk
        │  /api/voice/balance-check (pre-call gate)           verifies Clerk JWT vs JWKS
        └──────────────── HMAC JWT (VOICE_USAGE_WEBHOOK_SECRET) ──────────────┘
```

## 1. Deploy this app as `voice.viato.ai`

Run the full stack (UI + FastAPI + ARQ worker + Postgres + Redis + MinIO/S3 + TURN)
behind one public HTTPS host. It must serve the UI, the API, Twilio webhooks, and the
**WSS media** socket (`wss://voice.viato.ai/api/v1/telephony/ws/...`). `docker-compose.yaml`
(or Railway, like Presenton) works.

### Backend env (`api/.env`)
```
AUTH_PROVIDER=clerk
CLERK_ISSUER=https://clerk.viato.ai            # your Clerk instance issuer (comma-sep ok)
# CLERK_JWKS_URL=                              # optional; defaults to {issuer}/.well-known/jwks.json
# CLERK_AUDIENCE=                              # optional
# CLERK_AUTHORIZED_PARTIES=https://viato.ai,https://app.viato.ai

# Model keys (LLM via Viato's OpenRouter key; STT/TTS are separate providers):
OPENROUTER_API_KEY=...
VIATO_VOICE_LLM_MODEL=openai/gpt-4.1-mini
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...

# Billing bridge back to the CRM:
VIATO_BILLING_ENABLED=true
VIATO_CRM_URL=https://app.viato.ai
VOICE_USAGE_WEBHOOK_SECRET=<shared-secret>     # MUST match the CRM value
BACKEND_API_ENDPOINT=https://voice.viato.ai    # used to build the inbound webhook URL
```

### Frontend env (`ui/.env`)
```
EMBED_FRAME_ANCESTORS='self' https://viato.ai https://*.viato.ai
EMBED_MIC_ALLOWLIST=self "https://viato.ai" "https://app.viato.ai"
```
These set `Content-Security-Policy: frame-ancestors` (so the CRM can iframe the app —
no `X-Frame-Options` is sent) and `Permissions-Policy: microphone` (for browser test
calls). Configured in `ui/next.config.ts`.

## 2. Clerk setup (in the Viato CRM Clerk instance)

Create a **JWT template named `voice`** that includes `org_id` and `email` claims (the
default session token may omit them). The CRM mints this token with
`getToken({ template: "voice" })` and posts it to the iframe; this app validates it
against `CLERK_ISSUER`'s JWKS and maps `sub → user`, `org_id → org` (falling back to a
per-user org when there's no active Clerk org).

## 3. Shared secret

Generate one secret and set it as `VOICE_USAGE_WEBHOOK_SECRET` on **both** apps. This
app signs HS256 JWTs (iss `viato-voice`, aud `viato-crm`) for the two CRM callbacks;
the CRM verifies them with the same secret.

## 4. Viato CRM env (`.env.local`)
```
NEXT_PUBLIC_VIATO_VOICE_URL=https://voice.viato.ai   # iframe src + link-number target
# VIATO_VOICE_URL=                                    # optional server-only override
VOICE_USAGE_WEBHOOK_SECRET=<same shared secret>
```
No CRM CSP change is needed (the CRM frames Voice, not the reverse).

## 5. What the CRM gained

- Sidebar **Voice** tab → `/voice` (gated by the existing `voice` permission feature).
- `POST/DELETE /api/voice/number` — assign/unassign a Twilio number to Voice (pushes
  creds+number via this app's authenticated `POST /api/v1/telephony/link-number`, then
  repoints the number's Twilio VoiceUrl). A number is either a CRM softphone line OR a
  Voice agent line.
- `POST /api/voice/usage` + `POST /api/voice/balance-check` — server-to-server billing,
  charging the same per-minute rate as the CRM's existing calling (`calculateCallCost`).

## 6. Verify end to end

1. Open the CRM **Voice** tab → the iframe loads signed-in (no Voice login), and API
   calls carry the Clerk Bearer (check the Network tab / backend logs).
2. Assign a Twilio number to an agent; confirm its Twilio VoiceUrl points at
   `https://voice.viato.ai/api/v1/telephony/inbound/run`.
3. Inbound + outbound test calls connect (media over WSS).
4. Zero-balance user is blocked pre-call (402 / hangup); after a real call,
   `/api/voice/usage` debits the right pool with `itemized('call_minute', {source:'viato_voice'})`
   metadata, deduped on `workflow_run_id`.
5. No "Dograh" strings appear in the embedded UI.

## 7. Follow-ups (not in this pass)

- Set the real Viato URLs/support email in `ui/src/constants/branding.ts`.
- Run the backend tests (`api/tests/test_clerk_auth.py`) once a venv exists; run
  `tsc`/build in `ui/` once its `node_modules` are installed.
- Optional: `reserveTokens`/`settleHold` holds for long calls; brand-color push to the
  embedded UI; a CRM picker that lists this app's agents when assigning a number.
