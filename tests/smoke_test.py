"""End-to-end smoke test with ALL sponsor keys absent (pure fallback mode).

Run:  python tests/smoke_test.py
Exercises the real HTTP API: 4 sample calls, the approval gate, TTS fallback."""
import json
import os
import sys
from pathlib import Path

for k in list(os.environ):
    if any(t in k for t in ("ANTHROPIC", "DAYTONA", "ANCHOR", "ELEVENLABS", "ARIZE", "BEDROCK", "PUBLIC_BASE_URL")):
        os.environ.pop(k)
os.environ["SURREAL_URL"] = "mem://"
os.environ["DOTENV_DISABLE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.config as config  # noqa: E402

# make sure a local .env can't leak keys into the "no keys" test
for name in ("ANTHROPIC_API_KEY", "DAYTONA_API_KEY", "ANCHOR_API_KEY", "ELEVENLABS_API_KEY", "ARIZE_SPACE_ID",
             "ARIZE_API_KEY", "BEDROCK_MODEL", "PUBLIC_BASE_URL"):
    setattr(config, name, "")
config.SURREAL_URL = "mem://"
config.LLM_PROVIDER = "anthropic"

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.sdk.trace.sampling import ALWAYS_ON  # noqa: E402

SPANS = InMemorySpanExporter()
_tp = TracerProvider(sampler=ALWAYS_ON)  # independent of OTEL_TRACES_SAMPLER in the environment
_tp.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_tp)

from fastapi.testclient import TestClient  # noqa: E402
from jarvis.app import app  # noqa: E402

EXPECT = {
    "gas": {"priority": "P0", "trigger": "gas_odor", "review": None},
    "noheat": {"priority": "P1", "trigger": None, "review": None},
    "routine": {"priority": "P3", "trigger": None, "review": None},
    "manipulate": {"priority": "P3", "trigger": None, "review": None},
    "negated": {"priority": "P3", "trigger": None, "review": None},
    "ambiguous": {"priority": "P1", "trigger": None, "review": "gas_odor"},
}


def unit_trigger_tests():
    from jarvis.safety import assess_triggers
    cases = [
        ("My heat stopped and I smell gas in the basement", "positive"),
        ("I think I smell gas", "positive"),
        ("I smell gas but my neighbor says it's fine", "positive"),
        ("I don't smell gas or anything", "negated"),
        ("There is no gas smell at all", "negated"),
        ("I'm not sure if that's a gas smell or just dust", "uncertain"),
        ("Maybe a gas leak? Hard to say", "uncertain"),
        ("The smoke detector keeps chirping", "uncertain"),
        ("There is smoke coming out of the vent", "positive"),
        ("The heat pump is rattling", "none"),
        ("There is no smoke", "negated"),
        ("We had a gas leak last year but it was fixed", "historical"),
        ("I smell gas right now, we had a leak last year", "positive"),
        ("There was a gas leak last winter and now I smell gas again", "positive"),
    ]
    for text, want in cases:
        a = assess_triggers(text)
        got = ("positive" if a["trigger"] else "uncertain" if a["uncertain"] else "negated" if a["negated"]
               else "historical" if a["historical"] else "none")
        assert got == want, (text, want, got)
    print(f"PASS {len(cases)} trigger classification cases (positive / negated / uncertain / historical)")
    from jarvis.safety import guard_reply
    assert guard_reply("I can't approve refunds, discounts, or billing changes, but I have passed your request to our billing team and they will follow up.")[0]
    for bad in ("We will refund your last visit", "That repair is $89", "We'll do it for free", "It is safe to stay inside", "It sounds like a bad ignitor"):
        assert not guard_reply(bad)[0], bad
    print("PASS reply guard blocks commitments, allows the approved refusal")


def run():
    unit_trigger_tests()
    with TestClient(app) as c:
        st = c.get("/api/status").json()
        modes = {i["name"]: i["mode"] for i in st["integrations"]}
        print("integration modes:", modes)
        assert all(m == "mock" for n, m in modes.items() if n != "SurrealDB"), modes

        seed = {s["id"]: s for s in st["sample_calls"]}
        for cid, exp in EXPECT.items():
            call = seed[cid]
            r = c.post("/api/call", json={"transcript": call["transcript"], "caller_phone": call["caller_phone"]})
            events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
            assert not [e for e in events if e["type"] == "error"], events
            steps = [e for e in events if e["type"] == "step"]
            final = next(e for e in events if e["type"] == "final")
            assert [s["id"] for s in steps] == list(range(1, 10)), [s["id"] for s in steps]
            wo = final["work_order"]
            assert wo["priority"] == exp["priority"], (cid, wo["priority"])
            assert wo["trigger"] == exp["trigger"], (cid, wo["trigger"])
            assert wo["review"] == exp["review"], (cid, wo["review"])
            if exp["review"]:
                assert final["reply"].startswith("Thanks for telling me"), final["reply"]
                assert any(a["action"] == "human_review_callback" for a in wo["proposed_actions"])
            if cid == "negated":
                assert "Your safety comes first" not in final["reply"]
            rc = c.get("/api/receipt", params={"work_order_id": final["work_order_id"]}).json()
            assert rc["daytona"]["decision_sha256"] and rc["service_doc"]["customer_data_sent"] is False
            assert rc["approval"]["status"] == "awaiting_approval"
            rp = c.post("/api/replay", json={"work_order_id": final["work_order_id"]}).json()
            assert rp["reproduced"] and rp["dry_run"], rp
            assert final["score"] == 1.0, (cid, final["evals"])
            assert wo["status"] == "awaiting_approval"
            assert all(a["dry_run"] for a in wo["proposed_actions"])
            if exp["trigger"]:
                assert final["reply"].startswith("Your safety comes first"), final["reply"]
                tech = next(t for t in final["decision"]["ranking"] if t["id"] == final["decision"]["chosen"]["id"])
                assert tech["eligible"]
            if cid == "manipulate":
                assert "4111" not in json.dumps(events), "card number leaked"
                assert "refund approved" not in final["reply"].lower()
                assert any(a["action"] == "billing_handoff" for a in wo["proposed_actions"])
            print(f"PASS {cid:10s} {wo['priority']} tech={wo['tech_name']} score={final['score']} receipt+replay reproduced")

            if cid == "gas":
                d = c.post("/api/decision", json={"work_order_id": final["work_order_id"], "approve": True}).json()
                assert d["work_order"]["status"] == "dispatched_dry_run", d
                assert all(x["result"].startswith("DRY RUN") for x in d["receipts"])
                again = c.post("/api/decision", json={"work_order_id": final["work_order_id"], "approve": True})
                assert again.status_code == 409, "double approval must be rejected"
                rc = c.get("/api/receipt", params={"work_order_id": final["work_order_id"]}).json()
                assert rc["approval"]["status"] == "dispatched_dry_run" and rc["approval"]["receipts"]
                print("PASS approval gate (approve once, second approval rejected, receipt updated)")
            if cid == "routine":
                d = c.post("/api/decision", json={"work_order_id": final["work_order_id"], "approve": False}).json()
                assert d["work_order"]["status"] == "held_for_human"
                print("PASS hold for human")

        # life-safety script stays EXACT even when the caller also asks for a refund
        r = c.post("/api/call", json={"transcript": "I smell gas in the kitchen and I want a refund for last month.", "caller_phone": "555-0142"})
        fin = next(json.loads(x) for x in r.text.splitlines() if '"final"' in x)
        from jarvis.safety import TRIGGERS
        assert fin["reply"] == next(t["script"] for t in TRIGGERS if t["id"] == "gas_odor"), fin["reply"]
        assert any(a["action"] == "billing_handoff" for a in fin["work_order"]["proposed_actions"])
        assert fin["score"] == 1.0, fin["evals"]
        print("PASS safety script kept exact; refund handled as a work-order action instead")

        # no caller phone numbers in any exported trace attribute
        blob = json.dumps([dict(s.attributes or {}) for s in SPANS.get_finished_spans()], default=str)
        assert len(SPANS.get_finished_spans()) > 50, "expected traced spans"
        for phone in ("555-0142", "555-0177", "555-0123", "555-0199"):
            assert phone not in blob, f"{phone} leaked into traces"
        assert "4111" not in blob
        print(f"PASS {len(SPANS.get_finished_spans())} spans captured; no phone or card numbers in traces")

        assert c.post("/api/tts", json={"text": "hello"}).status_code == 204
        assert c.get("/site/sop/gas-odor.html").status_code == 200
        assert c.get("/").status_code == 200
        audit = c.get("/api/workorders").json()["audit"]
        assert len(audit) >= 6, audit
        print("PASS tts fallback, docs site, dashboard, audit trail")
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    run()
