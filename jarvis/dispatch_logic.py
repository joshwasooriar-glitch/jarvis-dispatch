"""Bounded dispatch decision. Pure standard-library Python.

This exact file is shipped into an isolated Daytona sandbox and executed with
the call data as JSON input. The LLM never writes or edits this code, so the
decision is deterministic, reproducible, and auditable. Locally (mock mode)
the same function runs in-process.
"""
import datetime as _dt
import math

PRIORITIES = {
    "P0": {"label": "EMERGENCY", "sla_hours": 1.5},
    "P1": {"label": "URGENT", "sla_hours": 4},
    "P2": {"label": "SAME/NEXT DAY", "sla_hours": 24},
    "P3": {"label": "ROUTINE", "sla_hours": 72},
}

SKILL_FOR_CATEGORY = {
    "heating": ["furnace"],
    "cooling": ["ac"],
    "heat_pump": ["heat_pump"],
    "electrical": ["electrical"],
    "other": [],
}


def _round_up_30(dt):
    minutes = math.ceil((dt.minute + dt.second / 60) / 30) * 30
    return dt.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(minutes=minutes)


def decide(p):
    intake = p["intake"]
    trigger = p.get("trigger")
    review = p.get("review")  # uncertain or model-raised hazard: human must look
    cust = p["customer"]
    techs = p["technicians"]
    travel = p["travel_minutes"]
    now = _dt.datetime.fromisoformat(p["now"])
    reasons = []

    # 1. urgency ---------------------------------------------------------
    cat = intake.get("issue_category", "other")
    outage = bool(intake.get("system_down"))
    temp = intake.get("indoor_temp_f")
    vulnerable = bool(intake.get("vulnerable_occupants"))
    if trigger:
        prio = "P0"
        reasons.append(f"Safety trigger '{trigger['id']}' forces EMERGENCY priority")
    elif review:
        prio = "P1"
        reasons.append(f"Possible hazard ({review['id']}) is uncertain: URGENT plus mandatory human review")
    elif outage and cat == "heating" and ((temp is not None and temp <= 60) or vulnerable):
        prio = "P1"
        reasons.append("Heating outage with low indoor temperature or vulnerable occupant")
    elif outage:
        prio = "P2"
        reasons.append("System outage without immediate risk factors")
    else:
        prio = "P3"
        reasons.append("System still running; degraded performance only")
    if cust.get("plan") == "Comfort Club" and prio in ("P2", "P3"):
        reasons.append("Comfort Club member: front of queue within priority tier")
    sla = PRIORITIES[prio]["sla_hours"]

    # 2. eligibility -----------------------------------------------------
    req_skills = list(dict.fromkeys(SKILL_FOR_CATEGORY.get(cat, []) + (trigger or review or {}).get("required_skills", [])))
    req_certs = list((trigger or review or {}).get("required_certs", []))
    ranking = []
    for t in techs:
        missing = [s for s in req_skills if s not in t["skills"]] + [c for c in req_certs if c not in t["certs"]]
        travel_min = travel.get(t["zone"], {}).get(cust["zone"], 30)
        free_h = t["next_free_hours"]
        if prio == "P0" and t.get("on_call"):
            free_h = min(free_h, 0.25)  # on-call techs can break off non-urgent work
        eta_h = round(free_h + travel_min / 60, 2)
        eligible = not missing
        notes = []
        if missing:
            notes.append("missing " + ", ".join(missing))
        if prio == "P0" and not t.get("on_call"):
            notes.append("not on call")
        within = eta_h <= sla
        ranking.append({
            "id": t["id"], "name": t["name"], "zone": t["zone"], "eligible": eligible,
            "eta_hours": eta_h, "travel_min": travel_min, "within_sla": within,
            "rating": t["rating"], "on_call": t.get("on_call", False), "notes": notes,
        })

    def key(r):
        # eligible first, then within SLA, then on-call for emergencies, then ETA, then rating
        return (not r["eligible"], not r["within_sla"], prio == "P0" and not r["on_call"], r["eta_hours"], -r["rating"])

    ranking.sort(key=key)
    chosen = ranking[0] if ranking and ranking[0]["eligible"] else None

    # 3. appointment -------------------------------------------------------
    appt = None
    if chosen:
        start = _round_up_30(now + _dt.timedelta(hours=chosen["eta_hours"]))
        if prio in ("P2", "P3"):
            # business hours 8:00-18:00
            if start.hour >= 17 or start.hour < 8:
                start = (start + _dt.timedelta(days=1 if start.hour >= 17 else 0)).replace(hour=8, minute=0)
        window = 1 if prio in ("P0", "P1") else 2
        appt = {"start": start.isoformat(timespec="minutes"),
                "end": (start + _dt.timedelta(hours=window)).isoformat(timespec="minutes"),
                "window_hours": window}
        reasons.append(f"{chosen['name']} selected: ETA {chosen['eta_hours']}h vs SLA {sla}h"
                       + (" (on call)" if chosen["on_call"] else ""))
    else:
        reasons.append("No eligible technician: escalate to human dispatcher")

    return {
        "priority": prio,
        "priority_label": PRIORITIES[prio]["label"],
        "sla_hours": sla,
        "required_skills": req_skills,
        "required_certs": req_certs,
        "chosen": chosen,
        "appointment": appt,
        "ranking": ranking,
        "reasons": reasons,
        "needs_human": prio == "P0" or bool(review) or chosen is None,
    }
