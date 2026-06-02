"""
Command Center — Lead scoring.

A 0-100 conversion-probability score that gets written to `people.lead_score`
so the ranked queue sorts good leads to the top. Two layers:

  1) Deterministic heuristic baseline (always runs, free, fast).
     Composes: source quality + closest-city + completeness + line type +
     prior-call patterns + birthday-target match.
  2) Optional Gemini refinement (free tier; runs as a layer on top of the
     heuristic). Returns a tuned score and a short rationale that becomes
     a person_note (author='ai_score').

Rules:
  * Never overwrite a manually set score (`source_of_score='manual'` in notes).
  * Skip leads scored within the last SCORING_TTL_DAYS unless force=True.
  * Cap leads-per-batch (LEAD_SCORING_BATCH_MAX) so a runaway call can't drain
    the Gemini quota.
  * Fail-open: any AI error -> heuristic-only score still lands.
  * Audit every score change.

Trigger paths:
  * agent_api.leads_import fires score_batch on a background thread after each
    import batch.
  * POST /agent/admin/score allows manual rescoring of pending or specific ids.
  * The OSCR ingestion agent can call score_lead inline (low-overhead) — the
    helper accepts an open db connection.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import db
import geo_policy

log = logging.getLogger(__name__)


# ----------------------------------------------------- config

SCORING_TTL_DAYS = int(os.getenv("LEAD_SCORING_TTL_DAYS", "7"))
BATCH_MAX = int(os.getenv("LEAD_SCORING_BATCH_MAX", "200"))


def _ai_provider_and_model():
    """Same provider hierarchy as post_call.py."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return ("anthropic", os.getenv("AI_SCORING_MODEL", "claude-haiku-4-5"))
    if os.getenv("OPENAI_API_KEY"):
        return ("openai", os.getenv("AI_SCORING_MODEL", "gpt-4o-mini"))
    if os.getenv("GROQ_API_KEY"):
        return ("groq", os.getenv("AI_SCORING_MODEL", "llama-3.3-70b-versatile"))
    if os.getenv("GEMINI_API_KEY"):
        return ("gemini", os.getenv("AI_SCORING_MODEL", "gemini-2.0-flash"))
    return (None, None)


def ai_enabled() -> bool:
    if os.getenv("ENABLE_AI_LEAD_SCORING", "").strip().lower() in {"1", "true", "yes", "on"}:
        return _ai_provider_and_model()[0] is not None
    return False


# ----------------------------------------------------- heuristic baseline

# Source-quality priors. OSCR hot lists run higher because they're internal
# Bankers Life lead assignments; bulk T65 birthday lists are cold and lower.
SOURCE_PRIORS = {
    "OSCR": 75,
    "Dialer_Existing": 55,
    # default for sources we don't recognize:
    None: 50,
}


def _source_prior(source: str) -> int:
    s = (source or "").strip()
    if s in SOURCE_PRIORS:
        return SOURCE_PRIORS[s]
    if s.startswith("OSCR"):
        return 70
    if s.startswith("T65_"):
        return 50
    return SOURCE_PRIORS[None]


def _bday_target_bonus(birthday: str) -> int:
    """+10 if the prospect's birth year matches a configured T65 target year."""
    try:
        if geo_policy.birthday_is_target(birthday):
            return 10
    except Exception:
        return 0
    return 0


def _drive_bonus(city: str) -> int:
    """Closer cities score higher. Greensboro = +10, edges of the whitelist = 0,
    outside whitelist = -20 (will usually be filtered out before scoring, but
    the score still reflects reality)."""
    try:
        if not geo_policy.city_is_allowed(city):
            return -20
        minutes = geo_policy.drive_minutes(city)
    except Exception:
        return 0
    if minutes <= 10:
        return 10
    if minutes <= 20:
        return 6
    if minutes <= 30:
        return 3
    return 0


def _completeness_bonus(person_row: dict) -> int:
    """Reward complete contact records — they're easier to sell."""
    bonus = 0
    if (person_row.get("address") or "").strip():
        bonus += 2
    if (person_row.get("birthday") or "").strip():
        bonus += 2
    if (person_row.get("email") or "").strip():
        bonus += 3
    full_name = (person_row.get("full_name") or "").strip()
    if len(full_name.split()) >= 2:
        bonus += 2
    return bonus


def _line_type_adjustment(line_type: str) -> int:
    """Penalize landline / VoIP. 'mobile' is neutral; we already weighted the
    source. Unknown is neutral until the number-lookup pass populates it."""
    t = (line_type or "").strip().lower()
    if t in {"landline", "fixed_line"}:
        return -30
    if t == "voip":
        return -10
    return 0


def _call_history_signals(conn, person_id: int) -> tuple[int, dict]:
    """Use prior dispositions to nudge score. Most important signal: did this
    person ever answer? That's a multiplier on future answer probability."""
    if not person_id:
        return 0, {"prior_calls": 0}
    row = conn.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN disposition_category='live_conversation' THEN 1 ELSE 0 END) AS connects,
              SUM(CASE WHEN disposition_category='no_answer' THEN 1 ELSE 0 END) AS no_answers,
              SUM(CASE WHEN disposition_category='voicemail' THEN 1 ELSE 0 END) AS voicemails,
              SUM(CASE WHEN disposition_category='callback' THEN 1 ELSE 0 END) AS callbacks
           FROM call_attempts WHERE person_id=?""",
        (person_id,),
    ).fetchone()
    if not row:
        return 0, {"prior_calls": 0}
    total = row["total"] or 0
    connects = row["connects"] or 0
    no_ans = row["no_answers"] or 0
    callbacks = row["callbacks"] or 0
    adj = 0
    if connects > 0:
        adj += 20
    if callbacks > 0:
        adj += 15
    if total >= 3 and connects == 0:
        adj -= 10
    if total >= 4 and connects == 0:
        adj -= 10
    return adj, {
        "prior_calls": total, "connects": connects, "no_answers": no_ans,
        "voicemails": row["voicemails"] or 0, "callbacks": callbacks,
    }


def heuristic_score(person_row: dict, conn=None, line_type: str = "") -> tuple[int, dict]:
    """Pure-Python deterministic score in [0, 100] with explanation dict."""
    prior = _source_prior(person_row.get("source", ""))
    bday = _bday_target_bonus(person_row.get("birthday", ""))
    drive = _drive_bonus(person_row.get("city", ""))
    completeness = _completeness_bonus(person_row)
    line = _line_type_adjustment(line_type)
    history_adj, history_signals = (
        _call_history_signals(conn, person_row.get("id")) if conn else (0, {"prior_calls": 0})
    )

    raw = prior + bday + drive + completeness + line + history_adj
    score = max(0, min(100, raw))
    signals = {
        "source_prior": prior,
        "birthday_target_bonus": bday,
        "drive_bonus": drive,
        "completeness_bonus": completeness,
        "line_type_adjustment": line,
        "history_adjustment": history_adj,
        "history_signals": history_signals,
        "raw_total": raw,
        "final_score": score,
    }
    return score, signals


# ----------------------------------------------------- AI layer (optional)

SYSTEM_PROMPT = """You are a Medicare T65 sales operations analyst. You're given \
one lead's bio + a heuristic score and signal breakdown. Your job is to OUTPUT \
a single JSON object refining the conversion-likelihood score (0-100, integer) \
and a one-line rationale. Be conservative; do not invent facts. Return JSON ONLY.

Schema:
{
  "score": 0-100,
  "rationale": "one short sentence",
  "confidence": 0.0-1.0
}"""


def _call_ai(system: str, user: str, max_tokens: int = 300) -> Optional[str]:
    provider, model = _ai_provider_and_model()
    if not provider:
        return None
    try:
        if provider == "anthropic":
            import anthropic  # type: ignore
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            resp = client.messages.create(model=model, max_tokens=max_tokens,
                                          system=system,
                                          messages=[{"role": "user", "content": user}])
            return resp.content[0].text
        import openai  # type: ignore
        base_urls = {
            "groq":   "https://api.groq.com/openai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
        }
        key_env = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
        kwargs = {"api_key": os.getenv(key_env)}
        if provider in base_urls:
            kwargs["base_url"] = base_urls[provider]
        client = openai.OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"} if provider in {"openai", "groq"} else None,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        log.warning("lead_scoring AI call failed (%s/%s): %s", provider, model, exc)
        return None


def _coerce_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    text = re.sub(r"^json\s*\n", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def ai_refine(person_row: dict, heuristic: int, signals: dict, line_type: str = "") -> Optional[dict]:
    if not ai_enabled():
        return None
    bio = {
        "full_name": person_row.get("full_name"),
        "city": person_row.get("city"),
        "county": person_row.get("county"),
        "birthday": person_row.get("birthday"),
        "source": person_row.get("source"),
        "line_type": line_type or "",
        "heuristic_score": heuristic,
        "signals": signals,
    }
    raw = _call_ai(SYSTEM_PROMPT, json.dumps(bio))
    parsed = _coerce_json(raw or "")
    if not isinstance(parsed, dict):
        return None
    try:
        score = max(0, min(100, int(parsed.get("score", heuristic))))
    except Exception:
        score = heuristic
    return {
        "score": score,
        "rationale": str(parsed.get("rationale") or "").strip()[:300],
        "confidence": float(parsed.get("confidence") or 0.0),
    }


# ----------------------------------------------------- write path

def _now():
    return datetime.now().isoformat(timespec="seconds")


def _last_ai_score_at(conn, person_id: int) -> Optional[str]:
    row = conn.execute(
        """SELECT MAX(created_at) AS last FROM person_notes
           WHERE person_id=? AND author='ai_score'""",
        (person_id,),
    ).fetchone()
    return row["last"] if row else None


def _has_manual_override(conn, person_id: int) -> bool:
    """A manual entry in admin_menu writes author='admin_menu'. If the most
    recent score-related note is from a human, do not stomp on it."""
    row = conn.execute(
        """SELECT author FROM person_notes
           WHERE person_id=? AND body LIKE '%lead_score%'
           ORDER BY created_at DESC LIMIT 1""",
        (person_id,),
    ).fetchone()
    return bool(row and row["author"] and row["author"] != "ai_score")


def _line_type_for(conn, person_id: int) -> str:
    row = conn.execute(
        """SELECT line_type FROM phone_numbers WHERE person_id=?
           ORDER BY status='active' DESC, id ASC LIMIT 1""",
        (person_id,),
    ).fetchone()
    return (row["line_type"] or "") if row else ""


def score_person(conn, person_id: int, force: bool = False, use_ai: Optional[bool] = None) -> dict:
    """Score a single person inside an open db connection. Returns a result dict."""
    row = conn.execute(
        "SELECT id, full_name, city, county, birthday, source, address, email FROM people WHERE id=?",
        (person_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "skipped": "not_found"}

    person = dict(row)
    if not force:
        if _has_manual_override(conn, person_id):
            return {"ok": False, "skipped": "manual_override"}
        last_at = _last_ai_score_at(conn, person_id)
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at)
                if datetime.now() - last_dt < timedelta(days=SCORING_TTL_DAYS):
                    return {"ok": False, "skipped": "recent_score"}
            except Exception:
                pass

    line_type = _line_type_for(conn, person_id)
    heuristic, signals = heuristic_score(person, conn=conn, line_type=line_type)
    refined = None
    if use_ai is None:
        use_ai = ai_enabled()
    if use_ai:
        refined = ai_refine(person, heuristic, signals, line_type=line_type)
    final_score = refined["score"] if refined else heuristic

    note_lines = [f"lead_score -> {final_score} (heuristic={heuristic})"]
    if refined:
        note_lines.append(f"AI: {refined.get('rationale', '')} (conf={refined.get('confidence', 0):.2f})")
    note_lines.append(f"signals: {json.dumps(signals)}")
    note_body = "\n".join(note_lines)

    conn.execute("UPDATE people SET lead_score=?, last_activity_at=? WHERE id=?",
                 (final_score, _now(), person_id))
    conn.execute(
        """INSERT INTO person_notes(person_id, body, author, created_at) VALUES (?,?,?,?)""",
        (person_id, note_body, "ai_score", _now()),
    )
    try:
        db.audit(conn, "person", person_id, "scored",
                 f"score={final_score} heur={heuristic} ai={'yes' if refined else 'no'}", "lead_scoring")
    except Exception:
        pass
    return {
        "ok": True, "person_id": person_id, "score": final_score,
        "heuristic": heuristic, "refined": refined is not None,
    }


def score_batch(person_ids: list, force: bool = False, use_ai: Optional[bool] = None) -> dict:
    """Score many people in one connection. Caps at BATCH_MAX so a runaway call
    never drains AI quota or blocks the DB."""
    person_ids = list(person_ids or [])[:BATCH_MAX]
    summary = {"requested": len(person_ids), "scored": 0, "skipped": 0, "errors": 0, "results": []}
    if not person_ids:
        return summary
    with db.connect() as conn:
        for pid in person_ids:
            try:
                r = score_person(conn, pid, force=force, use_ai=use_ai)
                summary["results"].append(r)
                if r.get("ok"):
                    summary["scored"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                summary["errors"] += 1
                summary["results"].append({"ok": False, "person_id": pid, "error": str(exc)})
    return summary


def score_pending(limit: int = 100, force: bool = False, use_ai: Optional[bool] = None) -> dict:
    """Find people who have never been AI-scored (or are stale) and score them."""
    limit = min(int(limit or 100), BATCH_MAX)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT p.id FROM people p
               LEFT JOIN (
                   SELECT person_id, MAX(created_at) AS last
                   FROM person_notes WHERE author='ai_score'
                   GROUP BY person_id
               ) n ON n.person_id = p.id
               WHERE p.stage NOT IN ('dnc', 'closed')
                 AND (n.last IS NULL OR n.last < ?)
               ORDER BY p.first_seen_at DESC
               LIMIT ?""",
            ((datetime.now() - timedelta(days=SCORING_TTL_DAYS)).isoformat(timespec="seconds"), limit),
        ).fetchall()
        pids = [r["id"] for r in rows]
    return score_batch(pids, force=force, use_ai=use_ai)
