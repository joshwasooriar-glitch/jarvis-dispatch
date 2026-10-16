"""Deterministic safety layer for JARVIS Dispatch.

1. Safety triggers (config): checked on the raw transcript BEFORE the LLM.
   The LLM may add an escalation, never remove one. Scripts are spoken verbatim.
2. Policy requests: refunds/discounts -> human handoff; instruction overrides flagged.
3. PII + secret redaction before anything is stored, traced, or sent to a model.
4. Output guard: replies containing prohibited commitments are replaced (fail closed).
5. Browser allowlist: the cloud browser may only open approved company docs.
6. Action policy: every consequential action is DRY RUN and needs human approval.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

CFG = json.loads((Path(__file__).parent / "data" / "triggers.json").read_text())


def _rx(patterns):
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in patterns) + r")\b", re.I)


TRIGGERS = [{**t, "_rx": _rx(t["patterns"])} for t in CFG["triggers"]]
POLICY = [{**p, "_rx": _rx(p["patterns"])} for p in CFG["policy_requests"]]

PII = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED-CARD]"),
    (re.compile(r"\b(sk|pk)[-_][A-Za-z0-9_\-]{16,}\b"), "[REDACTED-KEY]"),
]


REDACTION_SCOPE = "card numbers, SSNs, API keys and this app's own secrets (names, phones and addresses are NOT redacted)"


def redact(text: str) -> tuple[str, int]:
    if not isinstance(text, str):
        return text, 0
    n = 0
    for k, v in os.environ.items():  # never leak our own credentials
        if v and len(v) >= 12 and any(t in k.upper() for t in ("KEY", "SECRET", "TOKEN", "PASS")) and v in text:
            n += text.count(v)
            text = text.replace(v, "[REDACTED-SECRET]")
    for pat, repl in PII:
        text, k = pat.subn(repl, text)
        n += k
    return text, n


REVIEW_SCRIPT = CFG["review_script"]
NEGATION = re.compile(r"\b(don'?t|do not|doesn'?t|does not|didn'?t|no|not|never|without|isn'?t|nothing|zero)\b", re.I)
UNCERTAIN = re.compile(r"\b(not sure|unsure|can'?t tell|cannot tell|don'?t know if|not certain|i don'?t think|might be|maybe|possibly|could be|or just|hard to tell)\b", re.I)
HISTORICAL = re.compile(r"\b(last (year|month|week|winter|summer|fall|spring|time)|years? ago|months? ago|weeks? ago|back in \d{4}|used to|previously|in the past|was fixed|got fixed|been fixed|were fixed)\b", re.I)
CURRENT = re.compile(r"\b(right now|now|today|tonight|this (morning|afternoon|evening)|currently|still|again|is back|came back)\b", re.I)
CLAUSE_BREAK = re.compile(r"[.!?;]|\bbut\b|\balthough\b", re.I)


def _snippet(text, s, e):
    """Clause around the match, trimmed to whole words."""
    a = max(0, s - 45)
    b = min(len(text), e + 30)
    left = text[a:s]
    if a > 0:
        left = left.split(" ", 1)[-1] if " " in left else left
    left = re.split(r"[.!?]\s+", left)[-1]
    right = text[e:b]
    if b < len(text):
        right = right.rsplit(" ", 1)[0] if " " in right else right
    right = re.split(r"[.!?]", right)[0]
    return (left + text[s:e] + right).strip(" ,")


def _trigger_view(t, m, text):
    s, e = m.span()
    return {"id": t["id"], "severity": t["severity"], "matched": m.group(0), "span": [s, e],
            "evidence": _snippet(text, s, e),
            "script": t["script"], "required_skills": t["required_skills"],
            "required_certs": t["required_certs"], "doc": t["doc"]}


def assess_triggers(text: str) -> dict:
    """Classify every hazard mention as positive, negated, or uncertain.

    positive  -> fire the trigger (verbatim script, P0)
    negated   -> "I don't smell gas": no trigger, recorded as evidence
    historical-> "we had a gas leak last year": no trigger, recorded as evidence
    uncertain -> "not sure if that's gas": no script, routed to a human (P1)
    Any positive mention anywhere wins (fail safe)."""
    positive, negated, uncertain, historical = [], [], [], []
    for t in TRIGGERS:  # config order = priority
        for m in t["_rx"].finditer(text):
            s, e = m.span()
            before = text[max(0, s - 50):s]
            parts = CLAUSE_BREAK.split(before)
            before = parts[-1] if parts else before
            after = CLAUSE_BREAK.split(text[e:e + 40])[0]
            after_short = after.split(",")[0]
            view = _trigger_view(t, m, text)
            device = t["id"] == "electrical_fire" and m.group(0).lower() == "smoke" and re.match(r"\s*(detector|alarm)", after, re.I)
            if device or UNCERTAIN.search(before) or UNCERTAIN.search(after):
                uncertain.append({**view, "classification": "uncertain"})
            elif NEGATION.search(before):
                negated.append({**view, "classification": "negated"})
            elif not CURRENT.search(before + " " + after_short) and (HISTORICAL.search(before) or HISTORICAL.search(after_short)):
                historical.append({**view, "classification": "historical"})
            else:
                positive.append({**view, "classification": "positive"})
    return {"trigger": positive[0] if positive else None, "positive": positive,
            "negated": negated, "uncertain": uncertain, "historical": historical}


def check_triggers(transcript: str, extra_hazards: list[str] | None = None) -> dict | None:
    """Backward-compatible: the first positive trigger, if any."""
    return assess_triggers(transcript + " " + " ".join(extra_hazards or []))["trigger"]


def hazard_trigger_for(hazards: list[str]) -> dict | None:
    """Map model-raised hazard words to the closest trigger config (for skills/doc), without firing it."""
    text = " ".join(hazards).lower()
    for t in TRIGGERS:
        key = {"gas_odor": "gas", "carbon_monoxide": "carbon", "electrical_fire": "electr"}[t["id"]]
        if key in text or t["_rx"].search(text) or (t["id"] == "electrical_fire" and re.search(r"smoke|burn|spark|fire", text)):
            return {"id": t["id"], "required_skills": t["required_skills"], "required_certs": t["required_certs"], "doc": t["doc"]}
    return None


def check_policy_requests(transcript: str) -> list[dict]:
    hits = []
    for p in POLICY:
        m = p["_rx"].search(transcript)
        if m:
            hits.append({"id": p["id"], "matched": m.group(0), "action": p["action"], "say": p["say"]})
    return hits


PROHIBITED = [re.compile(p, re.I) for p in CFG["prohibited_reply_patterns"]]


def guard_reply(reply: str) -> tuple[bool, list[str]]:
    """Commitment-level check (prices, refunds granted, free work, guarantees, safety
    assurances, diagnoses). Pre-approved refusal text like "I can't approve refunds" passes."""
    bad = [m.group(0) for p in PROHIBITED if (m := p.search(reply or ""))]
    return (not bad), bad


def browser_allowed(url: str, allowed_hosts: set[str]) -> tuple[bool, str]:
    try:
        p = urlparse(url)
    except Exception:
        return False, "malformed URL"
    if p.scheme not in ("http", "https"):
        return False, f"scheme {p.scheme} not allowed"
    host = (p.hostname or "").lower()
    if host not in allowed_hosts:
        return False, f"{host} is not an approved company docs domain"
    if not p.path.startswith("/site/"):
        return False, "only the /site/ service-docs section is approved"
    return True, "approved company docs domain"


def action_policy(action: str, priority: str) -> dict:
    """Every external action is DRY RUN; consequential ones need approval."""
    high = {"dispatch_technician", "notify_customer_sms", "page_on_call", "human_review_callback"}
    return {"action": action, "dry_run": True, "requires_approval": action in high,
            "risk": "high" if priority == "P0" else ("medium" if action in high else "low")}
