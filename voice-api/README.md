# Retell voice API

The `/t65` frontend is a static GitHub Pages site. Its domain cannot execute
POST API routes. The Python app in this repository also does not execute Next.js
route files. Use a server runtime for the handlers, then set its real URLs in Retell.

The shared handlers use standard Request/Response, fetch and Web Crypto. They have
no npm dependencies. The Next.js adapters are retained for a future Node host.
Supabase entrypoints are provided under `supabase/functions/voice-*` for the
existing T65 Daily Command Center project. Do not deploy a static export as an API.

## Configuration

Copy the variable names in `.env.example` into the server's secret manager. Never
commit real values. Supabase already provides `SUPABASE_URL` and the service-role
key inside hosted functions. Keep all administrative keys out of Retell and the
frontend. Generate at least 32 random bytes for `VOICE_API_SECRET`.

Google OAuth requires an OAuth client and offline authorization for the owner's
primary calendar. Use scopes `calendar.events` and `calendar.events.freebusy`.
Google OAuth Playground must use your own OAuth client credentials; configure
`https://developers.google.com/oauthplayground` as that client's authorized
redirect URI. A refresh token from a testing consent app may expire after seven
days. Check the app's publishing status before relying on unattended booking.

`RETELL_WEBHOOK_SECRET` is the existing Retell API key with the webhook badge,
not a newly invented webhook password. The signature includes a timestamp and
digest. Only `call_analyzed` is processed; retries upsert the same activity ID.

## Retell functions

Both functions POST to the deployed calendar endpoint. Set header
`x-voice-secret` to the same `VOICE_API_SECRET`. Leave args-only mode off: Retell
sends `{ "name": "check_availability", "args": { "date": "YYYY-MM-DD" }, "call": {} }`.
The handler also accepts args-only requests with an explicit `action` field.

* `check_availability`: `date` string in YYYY-MM-DD format.
* `book_appointment`: `date`, `time` in HH:MM Eastern, `lead_name`, `lead_phone`,
  optional `notes`, optional `lead_id` (actual CRM UUID or empty).

Retain the 30-minute meeting duration. Current scheduling hours inherit the
supplied implementation: 09:00-17:00 Eastern daily, with starts through 16:30.
Only offer returned slots. Ask for explicit agreement before booking. Confirm a
booking only when the API returns `booked: true` and `event_id`. If the service
fails, record a callback request without promising a reservation. Christian sends
confirmation texts personally; the agent has no SMS function.

Stable event IDs prevent duplicate AI bookings for one slot. The handler also
checks Google free/busy immediately before creation. Google does not offer an
atomic free/busy-and-insert operation, so a simultaneous manual booking remains a
possible race. Rebooking a cancelled deterministic event needs manual review.

Post-call URL is the deployed `voice-post-call` endpoint. Select **Call analyzed
only** in Retell. Configure an analysis selector named `disposition` with booked,
interested_callback, not_interested, voicemail_left, no_answer, wrong_number,
do_not_call. Outbound calls should pass the CRM UUID in `metadata.lead_id`.
Unmatched/ambiguous callers are retained as unassigned activity rather than
attributing a call to the wrong lead. Full transcripts are retained. DNC requests
set the matched lead's requested-DNC fields. No calendar event is created by the
post-call webhook.

## Verify before use

Run `node --test --test-isolation=none tests/voice-api.test.mjs` with Node 22+.
The tests use synthetic requests and mocked Google/CRM receivers. They do not
touch a real calendar, call a lead, or modify live CRM data.

After deployment, verify missing/bad credentials are rejected. Then authorize
Google and test availability against the primary calendar. Use a clearly marked
synthetic appointment for the first booking. Verify one Google event, one CRM
activity per call, and the full transcript after a real Retell test to Christian's
own phone. Local tests and a successful deployment alone do not prove those flows.
