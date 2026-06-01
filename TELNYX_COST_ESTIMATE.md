# Telnyx Weekly Cost Estimate

Based on Telnyx public pricing (2026): outbound US voice ~**$0.005/min**, Premium AMD
**$0.005 per call leg**, SMS **$0.004/message** (in + out), DIDs **~$1/number/month**,
number lookup ~**$0.004**. A2P 10DLC: one-time brand ~$4 + campaign vetting, then a
small monthly/carrier campaign fee (passed through at cost).
Sources: [Voice API pricing](https://telnyx.com/pricing/voice-api) ·
[Messaging pricing](https://telnyx.com/pricing/messaging) ·
[10DLC fees](https://support.telnyx.com/en/articles/5634625-10dlc-fees-and-charges)

## Assumptions (realistic, 2 agents)
- **2 agents** (Chris + Will), dialing ~**4 sessions/week** each.
- **~250 dials per agent per day-session**, so ~**1,000 dials/agent/week** → **~2,000 dials/week** total.
- Connect math from your real data: ~33% no-answer (short/unbilled-ish), ~50% reach a
  machine or person, avg **~0.6 billed minutes per dial** blended (most calls are short;
  a few live ones run long). Premium AMD on each answered call.
- **Texts:** with automation on, ~1 follow-up text per voicemail/no-answer + some
  reminders ≈ **~1,200 texts/week**.
- **Emails:** sent via Gmail/SMTP (≈ $0), not Telnyx.

## Weekly cost

| Item | Volume / wk | Rate | Weekly |
|---|---|---|---|
| Outbound voice minutes | ~2,000 dials × 0.6 min = **1,200 min** | $0.005/min | **$6.00** |
| Premium AMD | ~1,000 answered legs | $0.005/leg | **$5.00** |
| SMS (after A2P) | **~1,200 msgs** | $0.004 | **$4.80** |
| Number lookup (optional pre-screen) | ~2,000 | $0.004 | $8.00 *(optional)* |
| **Weekly total (no lookup)** | | | **≈ $16/week** |
| **Weekly total (with lookup)** | | | **≈ $24/week** |

### Fixed monthly (not weekly)
| Item | Monthly |
|---|---|
| DIDs (4 numbers × ~$1) | ~$4 |
| A2P 10DLC campaign (carrier passthrough) | ~$2–10 |
| DigitalOcean droplet (Command Center host) | ~$6 *(covered by your $200 student credit ≈ 33 months free)* |

## Bottom line
- **Running cost: roughly $16–24/week** in Telnyx usage at 2-agent volume
  (~**$70–105/month**), plus ~$10–14/month in fixed fees.
- **One-time:** A2P 10DLC brand + campaign registration (~$5–50 depending on vetting).
- Email is effectively free at this volume.
- Scales linearly: if you double dial volume, usage roughly doubles; there are no
  per-seat fees and no minimums.

> This is far cheaper than any commercial dialer/CRM (Close ~$99–139/user/mo, GoHighLevel
> ~$97–297/mo + usage) — you keep ~3-second connects at carrier cost.

**Cost levers:** AMD is your second-biggest line — if a session's `not_sure` rate is low
you can leave it on; the ring-timeout trim and instant mode (already in the MAX dialer)
keep voice minutes down. Number lookup is optional (skip it to save ~$8/wk; turn it on if
your lists are dirty).
