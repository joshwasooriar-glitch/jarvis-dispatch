"""JARVIS Dispatch pipeline: one customer call -> one traceable work order.

  1 receive + redact        (local guardrail)
  2 safety triggers         (deterministic, on the raw transcript, BEFORE any LLM)
                             positive -> verbatim script | negated -> no trigger | uncertain -> human review
  3 understand request      (Claude, untrusted input; can raise risk to human review, never lower it)
  4 customer + history      (SurrealDB graph)
  5 service procedure       (Anchor Browser, allowlisted synthetic docs site; no customer data sent)
  6 dispatch decision       (vetted scorer in a Daytona sandbox)
  7 response                (verbatim script, or Claude + output guard)
  8 evaluate                (rule evals + LLM judge, logged to Arize)
  9 work order              (SurrealDB, DRY RUN, awaiting human approval) + Decision Receipt
Replay re-runs steps 2 and 6 on the recorded inputs and proves the decision reproduces.
"""
import datetime as dt
import hashlib
import json
import logging
import time
import uuid

from . import config, safety, tracing
from .adapters import fmt_window, template_reply
from .tracing import set_attrs, span

log = logging.getLogger("jarvis.pipeline")

DOC_FOR_CATEGORY = {
    "heating": "/site/kb/furnace-no-heat.html",
    "heat_pump": "/site/kb/heat-pump-noise.html",
    "cooling": "/site/kb/ac-leak-not-cooling.html",
    "electrical": "/site/sop/electrical-burning.html",
    "other": "/site/kb/general-service.html",
}
HISTORY_QUERY = ("SELECT ->requested->work_order.{summary, status, created, priority, "
                 "tech: ->assigned_to->technician.name} AS wos FROM $customer")


def sha(obj) -> str:
    data = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def _travel():
    from .store import SEED
    return SEED["travel_minutes"]


class Dispatcher:
    def __init__(self, store, llm, sandbox, browser, voice):
        self.store, self.llm, self.sandbox, self.browser, self.voice = store, llm, sandbox, browser, voice
        self.code_sha = sha(sandbox.source)

    def statuses(self):
        return [self.llm.status, self.voice.status, self.store.status, self.browser.status, self.sandbox.status, tracing.STATUS]

    # ======================================================================
    def handle_call(self, transcript: str, caller_phone: str | None, emit) -> dict:
        call_id = "call_" + uuid.uuid4().hex[:8]
        tokens = [0, 0]
        t_start = time.time()

        def step(sid, title, sponsor, mode, status, detail, ms=None, data=None):
            emit({"type": "step", "id": sid, "title": title, "sponsor": sponsor, "mode": mode, "status": status,
                  "detail": detail, "ms": ms, "data": data})

        def add_tok(t):
            tokens[0] += t[0]
            tokens[1] += t[1]

        def ms(t0):
            return int((time.time() - t0) * 1000)

        emit({"type": "call_start", "call_id": call_id})
        with span("dispatch.call", "AGENT", session__id=call_id) as root:
            ctx = root.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if getattr(ctx, "trace_id", 0) else None

            # 1 receive + redact ---------------------------------------------
            t0 = time.time()
            clean, n_red = safety.redact(transcript.strip())
            set_attrs(root, input__value=clean)
            step(1, "Call received", "ElevenLabs", self.voice.status["mode"], "ok",
                 f"{len(clean.split())} words; " + (f"redacted {n_red} value(s) matching card/SSN/key patterns before storage and tracing"
                 if n_red else "no card, SSN or key patterns detected") + " (names and addresses are not redacted; demo data is synthetic)",
                 ms(t0), {"transcript": clean})

            # 2 safety triggers + policy (raw transcript, before any model) -----
            t0 = time.time()
            with span("guardrail.safety_triggers", "GUARDRAIL", input__value=clean) as g:
                assess = safety.assess_triggers(clean)
                trigger = assess["trigger"]
                review = None
                if not trigger and assess["uncertain"]:
                    u = assess["uncertain"][0]
                    review = {"id": u["id"], "source": "uncertain mention", "matched": u["matched"], "evidence": u["evidence"],
                              "required_skills": u["required_skills"], "required_certs": u["required_certs"], "doc": u["doc"]}
                policy_hits = safety.check_policy_requests(clean)
                set_attrs(g, output__value={"trigger": trigger and trigger["id"], "review": review and review["id"],
                                            "negated": [n["matched"] for n in assess["negated"]], "policy": [p["id"] for p in policy_hits]},
                          guardrail__triggered=bool(trigger))
            if trigger:
                detail = f"TRIGGERED {trigger['id']} on \"{trigger['matched']}\": verbatim escalation script locked in, the LLM cannot alter it"
            elif review:
                detail = f"UNCERTAIN mention of {review['id']} (\"{review['evidence']}\"): no script, routed to a human dispatcher"
            else:
                detail = "No life-safety trigger matched"
            if assess["negated"]:
                detail += "; negated mention ignored: " + ", ".join(f"\"{n['evidence']}\"" for n in assess["negated"])
            if assess["historical"]:
                detail += "; past mention ignored: " + ", ".join(f"\"{n['evidence']}\"" for n in assess["historical"])
            if policy_hits:
                detail += "; policy flags: " + ", ".join(f"{p['id']} (\"{p['matched']}\")" for p in policy_hits)
            step(2, "Safety triggers", "Guardrail", "live", "alert" if trigger else ("warn" if (review or policy_hits) else "ok"),
                 detail, ms(t0), {"assessment": {k: v for k, v in assess.items() if k != "trigger"}, "policy": policy_hits})

            # 3 understand request -----------------------------------------------
            t0 = time.time()
            with span("intake.extract", "CHAIN", input__value=clean) as s:
                intake, intake_mode, tok = self.llm.extract_intake(clean)
                add_tok(tok)
                set_attrs(s, output__value=intake)
            model_raised = False
            if not trigger and not review and intake.get("possible_hazards"):
                mapped = safety.hazard_trigger_for(intake["possible_hazards"]) or {
                    "id": "possible_hazard", "required_skills": [], "required_certs": [], "doc": DOC_FOR_CATEGORY["other"]}
                review = {**mapped, "source": "model-raised hazard", "matched": ", ".join(intake["possible_hazards"])[:120],
                          "evidence": ", ".join(intake["possible_hazards"])[:200]}
                model_raised = True
            step(3, "Understand request", "Claude", intake_mode, "warn" if model_raised else "ok",
                 f"{intake['issue_category']} | system down: {intake['system_down']} | vulnerable: {intake['vulnerable_occupants']}"
                 + (f" | indoor {intake['indoor_temp_f']}F" if intake.get("indoor_temp_f") else "")
                 + (" | model flagged a possible hazard: routed to human review" if model_raised else ""),
                 ms(t0), {"intake": intake})

            # 4 customer + history ------------------------------------------------
            t0 = time.time()
            with span("surrealdb.customer_lookup", "TOOL", tool__name="surrealdb_graph_query", input__value=HISTORY_QUERY) as s:
                cust = self.store.find_customer(phone=caller_phone, name=intake.get("caller_name"))
                new = cust is None
                if new:
                    cust = self.store.create_customer(intake.get("caller_name"), caller_phone)
                hist = self.store.history(cust["id"])
                techs = self.store.technicians()
                set_attrs(s, output__value={"customer": cust["id"], "history": len(hist)})
            step(4, "Customer + history", "SurrealDB", self.store.status["mode"], "warn" if new else "ok",
                 (f"New caller, created {cust['id']}" if new else
                  f"{cust['name']} ({cust.get('plan')}), {cust.get('equipment', {}).get('type')}, {len(hist)} past work orders via graph traversal"),
                 ms(t0), {"customer": cust, "history": hist, "graph_query": HISTORY_QUERY})

            # 5 service procedure ---------------------------------------------------
            t0 = time.time()
            path = (trigger or review or {}).get("doc") or DOC_FOR_CATEGORY.get(intake["issue_category"], DOC_FOR_CATEGORY["other"])
            with span("anchor.fetch_service_doc", "TOOL", tool__name="anchor_fetch_webpage", input__value=path) as s:
                doc, doc_meta = self.browser.get_doc(path)  # only the doc path is sent, never customer data
                set_attrs(s, output__value={"title": doc.get("title"), "chars": len(doc.get("text", ""))}, metadata=doc_meta)
            step(5, "Service procedure", "Anchor Browser", doc_meta["mode"], "ok" if not doc.get("error") else "warn",
                 (doc.get("title") or doc.get("error", "")) + f" | source {doc.get('url', path)}"
                 + (f" ({doc_meta.get('fallback_reason')})" if doc_meta["mode"] != "live" else f" | cloud browser {doc_meta.get('seconds')}s"),
                 ms(t0), {"doc": {k: v for k, v in doc.items() if k != "screenshot"}, "screenshot": doc.get("screenshot"),
                          "customer_data_sent": False})

            # 6 dispatch decision -----------------------------------------------------
            t0 = time.time()
            payload = {"intake": {k: intake.get(k) for k in ("issue_category", "system_down", "indoor_temp_f", "vulnerable_occupants")},
                       "trigger": trigger and {k: trigger[k] for k in ("id", "required_skills", "required_certs")},
                       "review": review and {k: review[k] for k in ("id", "required_skills", "required_certs")},
                       "customer": {k: cust.get(k) for k in ("zone", "plan")},
                       "technicians": techs, "travel_minutes": _travel(),
                       "now": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
            with span("daytona.dispatch_decision", "TOOL", tool__name="daytona_code_run", input__value=payload) as s:
                decision, dmeta = self.sandbox.decide(payload)
                set_attrs(s, output__value=decision, metadata=dmeta)
            decision_sha = sha(decision)
            chosen = decision.get("chosen")
            window = fmt_window(decision.get("appointment"))
            step(6, "Dispatch decision", "Daytona", dmeta["mode"], "ok" if chosen else "warn",
                 f"{decision['priority']} {decision['priority_label']} | " + (f"{chosen['name']} {window}" if chosen else "no eligible tech: human dispatcher")
                 + (f" | sandbox {str(dmeta.get('sandbox_id'))[:8]}" + (", egress blocked" if dmeta.get("egress_blocked") else "")
                    if dmeta["mode"] == "live" else f" | {dmeta.get('fallback_reason')}"),
                 ms(t0), {"decision": decision, "meta": dmeta, "code_sha256": self.code_sha, "decision_sha256": decision_sha})

            # 7 response ------------------------------------------------------------------
            t0 = time.time()
            first = ((cust.get("name") if not new else intake.get("caller_name")) or "").split(" ")[0]
            guard_note, generated = "", ""
            with span("response.compose", "CHAIN") as s:
                if trigger:
                    reply, source = trigger["script"], "verbatim safety script"
                elif review:
                    reply, source = safety.REVIEW_SCRIPT, "verbatim human-review script"
                else:
                    rctx = {"caller_first_name": first, "issue_summary": intake.get("summary"),
                            "priority_label": decision["priority_label"], "technician": chosen and chosen["name"],
                            "window": window, "next_step": "Customer gets a confirmation text after dispatcher approval."}
                    generated, rmode, tok = self.llm.phrase_reply(rctx)
                    add_tok(tok)
                    ok, bad = safety.guard_reply(generated)
                    if not ok:
                        guard_note = f"Output guard replaced model reply (contained {bad})"
                        generated = template_reply(rctx)
                    reply, source = generated, ("Claude + output guard" if rmode == "live" else "template")
                deferred = []
                for p in policy_hits:
                    if not p["say"]:
                        continue
                    if source.startswith("verbatim"):
                        deferred.append(p["id"])  # safety scripts stay exact; policy handled via work-order action
                    else:
                        reply += " " + p["say"]
                ok_final, bad_final = safety.guard_reply(reply)
                if not ok_final and not source.startswith("verbatim"):
                    guard_note = (guard_note + "; " if guard_note else "") + f"Final reply failed guard ({bad_final}); replaced with template"
                    reply = template_reply(rctx)
                if deferred:
                    guard_note = (guard_note + "; " if guard_note else "") + f"Policy reply for {deferred} deferred to keep the safety script exact"
                set_attrs(s, output__value=reply, metadata={"source": source, "guard": guard_note})
            scripted = source.startswith("verbatim")
            step(7, "Response", "Claude" if source.startswith("Claude") else "Guardrail",
                 "live" if (scripted or source.startswith("Claude")) else "mock",
                 "alert" if trigger else ("warn" if (guard_note or review) else "ok"),
                 source + (f"; {guard_note}" if guard_note else ""), ms(t0), {"reply": reply})

            # 8 evaluate ------------------------------------------------------------------
            t0 = time.time()
            evals = self._evals(trigger, review, assess, reply, source, decision, techs, transcript, clean, policy_hits, generated)
            judge, tok = (None, (0, 0)) if scripted else self.llm.judge(
                {"decision": {k: decision[k] for k in ("priority", "priority_label")}, "technician": chosen and chosen["name"],
                 "window": window, "customer_first_name": first}, generated)
            add_tok(tok)
            if judge:
                f = float(judge.get("faithfulness", 0) or 0)
                evals.append({"name": "llm_judge_faithfulness", "passed": f >= 0.7, "score": round(f, 2), "detail": judge.get("reason", "")})
            applicable = [e for e in evals if e["passed"] is not None]
            score = round(sum(e["passed"] for e in applicable) / len(applicable), 2) if applicable else None
            with span("eval.dispatch_quality", "EVALUATOR") as s:
                set_attrs(s, output__value=evals, eval__score=score, **{f"eval__{e['name']}": e["passed"] for e in applicable})
            step(8, "Evaluate", "Arize AX", tracing.STATUS["mode"], "ok" if score == 1 else "warn",
                 f"{sum(e['passed'] for e in applicable)}/{len(applicable)} checks passed"
                 + (f" | trace {trace_id[:12]}… in project {config.ARIZE_PROJECT}" if trace_id and tracing.STATUS["mode"] == "live" else ""),
                 ms(t0), {"evals": evals, "score": score, "trace_id": trace_id})

            # 9 work order + receipt ---------------------------------------------------------
            t0 = time.time()
            actions = self._actions(decision, cust, chosen, window, review, policy_hits)
            receipt = {
                "call_id": call_id,
                "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "transcript": clean,
                "evidence": {
                    "safety": {"positive": [_ev(x) for x in assess["positive"]], "negated": [_ev(x) for x in assess["negated"]],
                               "historical": [_ev(x) for x in assess["historical"]],
                               "uncertain": [_ev(x) for x in assess["uncertain"]],
                               "model_raised": intake.get("possible_hazards") if model_raised else []},
                    "intake_factors": intake.get("evidence", {}),
                    "policy": [{"id": p["id"], "matched": p["matched"]} for p in policy_hits],
                },
                "rules_applied": {"trigger": trigger and trigger["id"], "review": review and {"id": review["id"], "source": review["source"]},
                                  "priority": decision["priority"], "priority_label": decision["priority_label"],
                                  "sla_hours": decision["sla_hours"], "required": decision["required_skills"] + decision["required_certs"],
                                  "reasons": decision["reasons"], "reply_source": source},
                "service_doc": {"title": doc.get("title"), "source": doc.get("url", path), "mode": doc_meta["mode"],
                                "fallback_reason": doc_meta.get("fallback_reason"), "customer_data_sent": False},
                "daytona": {"mode": dmeta["mode"], "sandbox_id": dmeta.get("sandbox_id"), "egress_blocked": dmeta.get("egress_blocked"),
                            "seconds": dmeta.get("seconds"), "fallback_reason": dmeta.get("fallback_reason"),
                            "code_sha256": self.code_sha, "decision_sha256": decision_sha,
                            "chosen": chosen and {k: chosen[k] for k in ("id", "name", "eta_hours", "within_sla")},
                            "candidates": [{k: r[k] for k in ("name", "eligible", "eta_hours", "notes")} for r in decision["ranking"]]},
                "surrealdb": {"mode": self.store.status["mode"], "customer_id": cust["id"], "customer": cust.get("name"),
                              "equipment": cust.get("equipment"), "plan": cust.get("plan"), "history_consulted": hist,
                              "graph_query": HISTORY_QUERY},
                "arize": {"mode": tracing.STATUS["mode"], "project": config.ARIZE_PROJECT, "trace_id": trace_id,
                          "score": score, "evals": evals},
                "replay_inputs": {"caller_phone": caller_phone, "payload": payload},
                "redaction_scope": safety.REDACTION_SCOPE,
                "data_note": "DRY RUN with synthetic data: simulated company, customers and technicians. No calls, texts, pages or dispatches were sent.",
            }
            wo = {"summary": intake.get("summary", "")[:300], "issue_category": intake["issue_category"],
                  "priority": decision["priority"], "priority_label": decision["priority_label"], "sla_hours": decision["sla_hours"],
                  "trigger": trigger and trigger["id"], "review": review and review["id"], "status": "awaiting_approval",
                  "appointment": decision.get("appointment"), "window": window, "tech_name": chosen and chosen["name"],
                  "proposed_actions": actions, "reply": reply, "eval_score": score, "call_id": call_id,
                  "created": receipt["created"], "source": "jarvis", "transcript": clean, "receipt": receipt}
            with span("surrealdb.create_work_order", "TOOL", tool__name="surrealdb_create") as s:
                wid = self.store.create_work_order(wo, cust["id"], chosen and chosen["id"])
                set_attrs(s, output__value=wid)
            self.store.audit(call_id=call_id, event="work_order_proposed", work_order=wid, priority=decision["priority"],
                             trigger=trigger and trigger["id"], review=review and review["id"], eval_score=score,
                             policy=[p["id"] for p in policy_hits], trace_id=trace_id)
            step(9, "Work order (dry run)", "SurrealDB", self.store.status["mode"], "wait",
                 f"{wid} awaiting human approval: {len([a for a in actions if a['requires_approval']])} action(s) gated",
                 ms(t0), {"work_order_id": wid, "actions": actions})

            cost = round((tokens[0] * config.PRICE_IN + tokens[1] * config.PRICE_OUT) / 1e6, 4)
            set_attrs(root, output__value=reply, jarvis__priority=decision["priority"], jarvis__trigger=trigger and trigger["id"],
                      jarvis__work_order=wid, eval__score=score, llm__token_count__total=sum(tokens), jarvis__cost_usd=cost)
            result = {"type": "final", "call_id": call_id, "reply": reply, "work_order_id": wid,
                      "work_order": {k: v for k, v in wo.items() if k != "receipt"} | {"id": wid},
                      "receipt": receipt, "customer": cust, "decision": decision, "evals": evals, "score": score,
                      "cost": cost, "tokens": sum(tokens), "seconds": round(time.time() - t_start, 2)}
            emit(result)
            return result

    # ======================================================================
    def replay(self, wid: str) -> dict:
        """Deterministic, dry-run replay: re-run the safety rules and the Daytona scorer
        on the exact recorded inputs and compare fingerprints. No LLM, no side effects."""
        wo = self.store.get_work_order(wid)
        if not wo or not wo.get("receipt"):
            return {"error": "work order or receipt not found"}
        r = wo["receipt"]
        t0 = time.time()
        with span("dispatch.replay", "CHAIN", input__value=wid) as s:
            assess = safety.assess_triggers(r["transcript"])
            orig_rules = r["rules_applied"]
            trig_now = assess["trigger"] and assess["trigger"]["id"]
            review_now = (assess["uncertain"][0]["id"] if (not assess["trigger"] and assess["uncertain"]) else None)
            orig_review = (orig_rules.get("review") or {})
            rules_match = trig_now == orig_rules.get("trigger") and (
                orig_review.get("source") == "model-raised hazard" or review_now == orig_review.get("id"))
            decision, meta = self.sandbox.decide(r["replay_inputs"]["payload"])
            new_sha = sha(decision)
            code_match = sha(self.sandbox.source) == r["daytona"]["code_sha256"]
            reproduced = bool(rules_match and new_sha == r["daytona"]["decision_sha256"] and code_match)
            out = {"work_order_id": wid, "reproduced": reproduced, "dry_run": True,
                   "rules_match": rules_match, "trigger": trig_now, "review": review_now,
                   "decision_match": new_sha == r["daytona"]["decision_sha256"], "code_match": code_match,
                   "original_decision_sha256": r["daytona"]["decision_sha256"], "replay_decision_sha256": new_sha,
                   "priority": decision["priority"], "chosen": decision.get("chosen") and decision["chosen"]["name"],
                   "sandbox_mode": meta["mode"], "sandbox_id": meta.get("sandbox_id"),
                   "seconds": round(time.time() - t0, 2)}
            set_attrs(s, output__value=out, jarvis__reproduced=reproduced)
        self.store.audit(event="replay", work_order=wid, reproduced=reproduced, sandbox=meta["mode"])
        return out

    # ======================================================================
    def _actions(self, decision, cust, chosen, window, review, policy_hits):
        prio = decision["priority"]
        acts = []
        if prio == "P0":
            acts.append({**safety.action_policy("page_on_call", prio),
                         "detail": f"Page human dispatcher + on-call tech for {cust['name']} at {cust.get('address')}"})
        if review:
            acts.append({**safety.action_policy("human_review_callback", prio),
                         "detail": f"Human dispatcher calls back to assess possible {review['id'].replace('_', ' ')} ({review['source']})"})
        if chosen:
            acts.append({**safety.action_policy("dispatch_technician", prio), "detail": f"Assign {chosen['name']} ({chosen['id']}), {window}"})
            tech = chosen["name"].split()[0]
            sms = (f"Brightline EMERGENCY: please stay outside. A Brightline dispatcher will contact you; {tech} is assigned, arriving {window}."
                   if prio == "P0" else f"Brightline: {tech} is scheduled {window}. Reply C to confirm or R to reschedule.")
            acts.append({**safety.action_policy("notify_customer_sms", prio), "detail": f"SMS to {cust.get('phone') or 'caller'}: \"{sms}\""})
        for p in policy_hits:
            if p["action"] == "human_handoff":
                acts.append({**safety.action_policy("billing_handoff", prio), "detail": "Open billing ticket for refund/discount request (no commitment made)"})
            if p["id"] == "instruction_override":
                acts.append({**safety.action_policy("security_flag", prio), "detail": "Logged instruction-override attempt for review"})
        return acts

    def _evals(self, trigger, review, assess, reply, source, decision, techs, raw, clean, policy_hits, generated):
        ev = [{"name": "escalation_script_verbatim",
               "passed": (reply == trigger["script"] and source == "verbatim safety script") if trigger else None,
               "detail": "Life-safety call answered with the approved script, no model text" if trigger else "No positive trigger"}]
        ev.append({"name": "uncertain_hazard_to_human",
                   "passed": (bool(decision.get("needs_human")) and reply == safety.REVIEW_SCRIPT) if review else None,
                   "detail": f"Possible {review['id']} ({review['source']}) routed to a person, no automated reassurance" if review else "No uncertain hazard"})
        ev.append({"name": "negation_respected",
                   "passed": (not trigger and "Your safety comes first" not in reply)
                   if ((assess["negated"] or assess["historical"]) and not assess["positive"]) else None,
                   "detail": ("Caller denied or described a past hazard (\"" + (assess["negated"] + assess["historical"])[0]["evidence"]
                              + "\"); no false emergency") if (assess["negated"] or assess["historical"]) else "No negated or past hazard mention"})
        chosen = decision.get("chosen")
        if chosen:
            t = next((x for x in techs if x["id"] == chosen["id"]), {})
            ok = all(s in t.get("skills", []) for s in decision["required_skills"]) and all(c in t.get("certs", []) for c in decision["required_certs"])
            ev.append({"name": "technician_qualified", "passed": ok,
                       "detail": f"Re-verified {t.get('name')} has {decision['required_skills'] + decision['required_certs']} against the DB"})
            ev.append({"name": "sla_met", "passed": bool(chosen["within_sla"]), "detail": f"ETA {chosen['eta_hours']}h vs SLA {decision['sla_hours']}h"})
        else:
            ev.append({"name": "human_handoff_when_no_tech", "passed": decision.get("needs_human", False), "detail": "No eligible tech"})
        ok, bad = safety.guard_reply(reply)  # the FINAL spoken reply, after policy handling
        ev.append({"name": "no_prohibited_commitments", "passed": ok,
                   "detail": "Final reply has no prices, granted refunds, free work, guarantees, diagnoses or safety assurances" if ok else f"Found {bad}"})
        raw_has_pii = safety.redact(raw)[1] > 0
        ev.append({"name": "pii_redacted", "passed": (safety.redact(clean)[1] == 0) if raw_has_pii else None,
                   "detail": ("Card/SSN/key patterns removed before storage and tracing" if raw_has_pii else "No card/SSN/key patterns in call")})
        inj = any(p["id"] == "instruction_override" for p in policy_hits)
        ev.append({"name": "injection_resisted", "passed": (ok and "refund approved" not in reply.lower()) if inj else None,
                   "detail": "Caller tried to override instructions; no unauthorized commitment made" if inj else "No override attempt"})
        return ev


def _ev(x):
    return {"id": x["id"], "matched": x["matched"], "evidence": x["evidence"], "classification": x["classification"]}
