# Command Center — Agent API Guide

The Command Center is the **shared brain**. AI agents read from it and write to it
over a simple, token-authed JSON API. This is how a multi-agent pipeline runs:

```
[Lead-puller agent] --import--> Command Center <--call-queue / score-- [Call agent]
                                     |   ^
                        outreach-queue   message/log, opt-out
                                     v   |
                              [Messaging agent]
```

## Auth
Every `/agent/*` call needs a bearer token (set `CC_AGENT_TOKEN` on the server):
```
Authorization: Bearer <CC_AGENT_TOKEN>
```
Base URL: `https://68.183.116.10.sslip.io`  (locally: `http://localhost:5055`)

## Self-discovery (point any agent here first)
- `GET /agent/manifest` — the full contract: every endpoint, body, and return shape
- `GET /agent/schema` — field dictionary, valid stages & dispositions

## 1) Lead-puller agent — drop a new list
```
POST /agent/leads/import
{ "source":"T65_June2026", "owner":"chris",
  "leads":[ {"name":"Jane Doe","phone":"3365550101","city":"Greensboro",
             "county":"Guilford","birthday":"1962-03-04","email":"jane@x.com"} ] }
-> { "imported":1, "duplicates":0, "suppressed":0, "person_ids":[123] }
```
New leads land under stage `new`. Duplicates (same name+city+birthday, or same phone)
are auto-detected and enriched — never doubled. Any number previously opted-out/DNC is
**skipped on import** and counted in `suppressed` (a re-pulled list never resurrects a STOP).

## 2) Call agent — work the best leads, push winners up
```
GET  /agent/leads/call-queue?limit=100&owner=chris     # prioritized: callbacks first, then score desc
POST /agent/leads/score        { "scores":[{"person_id":123,"lead_score":95}] }
POST /agent/leads/disposition  { "person_id":123,"disposition":"callback_requested",
                                 "agent":"chris","live_talk_seconds":80,"note":"wants Tue AM" }
```
Dispositions auto-advance the stage (`callback_requested`→callback, `do_not_call`→suppressed).
Valid dispositions: see `/agent/schema`.

## 3) Messaging agent — right message, right lead
```
GET  /agent/leads/outreach-queue?channel=email&source=T65_June2026
  -> leads due for email, each with .context (first_name, city, source, stage,
     last_call_outcome, times_messaged) to personalize. Opt-outs excluded automatically.
POST /agent/messages/log  { "person_id":123,"channel":"email","subject":"Your Medicare options",
                            "body":"Hi Jane...","template_id":"t65_intro","status":"sent" }
POST /agent/leads/opt-out { "phone":"3365550101","reason":"opt_out" }   # or person_id, or email
```
Always honor STOP/unsubscribe via `/opt-out` — it suppresses the person across all channels
and marks DNC, so no agent contacts them again. Accepts **person_id, phone, OR email** (a STOP
text only gives you a number); a STOP from an unknown number is still recorded so it's never texted.

## Bulk export (any agent / analytics)
```
GET /agent/leads/export?stage=callback&updated_since=2026-05-30T00:00:00
-> { "count":N, "people":[ {full record + primary phone} ] }
```

## Why this is safe with many agents at once
- SQLite WAL + unique keys = concurrent reads/writes, no corruption, no double-dials.
- Every write is recorded in `audit_log` (who/what/when).
- Suppressions enforced centrally — one agent's opt-out protects all of them.
- The dialer's live results sync in continuously, so call history is always current.
