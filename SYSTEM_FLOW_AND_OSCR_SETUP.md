# Command Center — How Everything Flows + OSCR Setup

This is the master reference for the whole machine: how leads are sorted, ranked,
called, texted, and emailed — and the exact steps to plug in OSCR when you have access.

---

## The full loop (what happens, automatically)

```
  OSCR / T65 list
        │  (lead-puller agent -> /agent/leads/import)
        ▼
  ┌─────────────────────────  COMMAND CENTER (the brain)  ─────────────────────────┐
  │  new leads land as 'new'                                                        │
  │  scoring agent ranks them (/agent/leads/score) + auto source-quality bonus     │
  │  build_dialer_queue.py  ──writes prioritized──►  dialer contacts.xlsx           │
  └────────────────────────────────────────────────────────────────────────────────┘
        │                                                        ▲
        ▼                                                        │ results sync back
   THE DIALER  ──calls in priority order──►  outcome per call ──┘ (every 30s)
        │
        ▼
  Outcome decides the NEXT action automatically (automation engine):
     human reached      -> 'contacted', auto-chase stops
     callback requested -> scheduled call at the exact time they asked
     voicemail          -> follow-up TEXT (~10min) + redial in 2 days + nurture email
     no answer          -> retry in the next good window (escalating, capped)
     STOP / not interested -> opt-out, suppress everywhere, cancel pending
        │
        ▼
  Worker (every 60s) fires due tasks: sends texts/emails, surfaces calls on the
  right day. Learning loop measures what works and tunes timing + scoring.
```

**Nothing is manual.** New leads → ranked → dialed → outcome → next action → repeat.

---

## 1. Lead ranking — which to call first

The call queue is ordered (top = call first):

| Priority | Group | Why |
|---|---|---|
| 1 | **Scheduled calls that are DUE** | A prospect asked for this exact time (or a redial came due). Warmest possible. |
| 2 | **Callbacks (warm stage)** | They engaged; strike while warm. |
| 3 | **New leads** — by `lead_score` desc | Fresh, never-called; best-scoring first. |
| 4 | **Retry** (no-answer / voicemail) — by score, oldest first | Keep working them without over-calling. |

**Held back:** anyone with a callback scheduled in the FUTURE is kept OUT of today's
list until their time arrives (so we never call before they asked). DNC + opted-out +
already-reached are excluded entirely.

**Lead score (0–100)** starts at 50 and is adjusted by:
- outcome history (callback +, contacted +, repeated no-answers −),
- **source quality** — the learning loop raises scores for sources that actually convert,
- freshness (newer leads rank above stale ones at the same score).

You (or the scoring agent) can override any score via `/agent/leads/score`.

---

## 2. When to CALL

- **Best windows** (senior T65 demographic, Eastern): Tue–Thu and Sat are favored.
- The system **learns your real best hour/day** from your data (shown on the Automations
  tab) and nudges scheduling toward it. Today it already shows e.g. "Best hour 2pm,
  Best day Thursday."
- Retries space out automatically: 1 day → 2 → 3 → 5, then rest (capped, never spammy).

## 3. When to TEXT

- **After a voicemail:** a follow-up text ~10 minutes later (warm memory) — *"Hey Sharon,
  Christian here, I gave you a call yesterday…"*
- **Day 7:** one warmer follow-up if still no contact.
- **Callback/appointment:** confirmation + reminders (24h before, morning-of).
- **Caps:** max 1 text per person per day; quiet hours 8am–8pm; instant STOP handling.
- **Texts send from the same local DID that called them** — feels like one person.

## 4. When to EMAIL

- **Day ~4** after a voicemail/no-answer, IF we have a valid email — a no-pressure intro.
- Lighter legal load than SMS (CAN-SPAM, not TCPA), so it can run sooner.
- Same personalization + caps.

---

## 5. OSCR setup — what to do when you get access

OSCR is a Bankers portal with **no public API**, so there are 3 ways in, easiest first:

### Option A — Manual export → drop in (works day one, zero code)
1. In OSCR, export the T65/lead list (xlsx or csv).
2. Run the example puller (or hand it to an agent):
   ```
   set CC_CLOUD_URL=https://<your-domain>
   set CC_AGENT_TOKEN=<from DEPLOY_SECRETS.txt>
   python example_lead_puller.py path\to\oscr_export.xlsx --source OSCR_T65_June --owner chris
   ```
   …or just drag the file into the dashboard's **Lead Book → Import**.
3. Build the dialer queue and dial:
   ```
   python build_dialer_queue.py --owner chris --out "C:\dialer\MyDialer_TODAY_READY\MyDialer\contacts.xlsx"
   ```
   (Wrap these two in one .bat so it's a single double-click — see `REFRESH_AND_DIAL.bat`.)

### Option B — Downloads watcher (drop-free)
A small watcher notices any new OSCR export in your Downloads folder and auto-imports +
rebuilds the queue. You still click "export" in OSCR; nothing else.

### Option C — Full auto pull (Playwright bot) — the stretch goal
A browser bot logs into OSCR on a schedule, exports, and imports automatically. This is
the only truly hands-off path, but it's brittle (portal logins, 2FA, ToS) — build it last,
**after I can see the OSCR export screens**. Keep Option A as the always-working fallback.

**What I need from you to wire OSCR:** a screenshot/description of the OSCR export page
(what the button is, what columns the file has, whether login has 2FA). With that I can
finish A→B→C.

---

## 6. The agent API (for the AI agents that drive it)

Self-describing at `GET /agent/manifest`. Key endpoints:
- `POST /agent/leads/import` — drop new leads (dedup + suppression-safe)
- `GET  /agent/leads/call-queue` — prioritized leads to dial
- `POST /agent/leads/score` — push best to the top
- `POST /agent/leads/disposition` — log an outcome (auto-schedules next action)
- `GET  /agent/leads/outreach-queue` — who's due for text/email + personalization context
- `POST /agent/messages/log` — record a sent message
- `POST /agent/inbound` — a reply comes in ("call me 10am Wed" / "STOP") → auto-handled
- `POST /agent/leads/opt-out` — honor STOP by phone/email/id
- `GET  /agent/leads/export` — bulk pull

Full guide: `AGENT_API_GUIDE.md`.

---

## 7. Daily operating rhythm (once live)
1. New OSCR leads come in (import / auto-pull) → ranked automatically.
2. `build_dialer_queue` writes each agent's prioritized list.
3. Dial the session → results sync back within 30s.
4. The machine schedules every follow-up (text/call/email) on its own.
5. Replies ("call me Wed") reschedule calls automatically; STOP opts out.
6. Next session's queue is already rebuilt with callbacks-due on top.

You mostly just **dial and take the warm calls.** The system does the rest.
