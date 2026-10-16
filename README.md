# JARVIS Dispatch

A voice-first AI dispatcher that turns a customer's call into a clear, traceable, human-approved work order.
Demo company: **Brightline Heating & Air** (fictional). All customers, technicians and work orders are synthetic.

## Run it (Mac, Python 3.10+)

```bash
git clone https://github.com/joshwasooriar-glitch/jarvis-dispatch.git
cd jarvis-dispatch
./run.sh
```

Then open http://localhost:8000 in your browser.

Works with **zero API keys**: every sponsor sits behind a small adapter with a local mock, and the header chips show LIVE or MOCK for each one.

### Add keys
Copy `.env.example` to `.env` (run.sh does this), fill in what you have, restart (`Ctrl+C`, then `source .venv/bin/activate && python -m jarvis.app`).

### Make Anchor Browser live (needs a public URL for the DOCS ONLY)
Anchor's cloud browser can't reach `localhost`. Run the separate docs-only server and tunnel **that** (port 8001).
**Never tunnel port 8000**: the dispatch app and its API have no auth and stay on localhost.

```bash
# terminal 2
source .venv/bin/activate && python -m jarvis.docs_server      # serves /site only, on 127.0.0.1:8001
# terminal 3
brew install cloudflared
cloudflared tunnel --url http://localhost:8001
# copy the https://xxxx.trycloudflare.com URL into .env as PUBLIC_BASE_URL, restart the main app
```

Only the doc path is sent to the browser; no customer data goes through the tunnel. The browser adapter only allows that host and the `/site/` path.

## The pipeline (one call)

| # | Step | Service | What makes it safe |
|---|------|---------|--------------------|
| 1 | Call received | ElevenLabs voice out, browser mic in | Card/SSN/API-key patterns and the app's own secrets redacted before storage or tracing (names/addresses are not; demo data is synthetic). No phone numbers in traces |
| 2 | Safety triggers | Deterministic guardrail | Runs on raw transcript **before** the LLM. Positive fires; negated ("I don't smell gas") and past ("leak last year") do not; uncertain goes to a human. Config in `jarvis/data/triggers.json` |
| 3 | Understand request | Claude | Transcript treated as untrusted. Model can **raise** risk, never lower it |
| 4 | Customer + history | SurrealDB graph | `customer -> requested -> work_order -> assigned_to -> technician` |
| 5 | Service procedure | Anchor Browser | Allowlisted company docs domain only |
| 6 | Dispatch decision | Daytona sandbox | Vetted scorer (`dispatch_logic.py`), LLM never writes it, egress blocked |
| 7 | Response | Verbatim script or Claude | Life-safety calls get the approved script exactly, nothing appended. Scripts only claim what the system does ("marked as an emergency"). Output guard on the final reply blocks prices, granted refunds, free work, diagnoses, "it's safe" |
| 8 | Evaluate | Arize AX | Rule evals + LLM judge, every step is an OpenInference span |
| 9 | Work order | SurrealDB | **DRY RUN**. Dispatch and SMS need a human Approve click. Every call gets a Decision Receipt; Replay re-runs the rules and the Daytona scorer on the recorded inputs and proves the decision fingerprint matches |

## 3 minute demo script

1. **(20s) Problem.** Service businesses miss calls, and the dangerous ones (gas, CO) can't be handled by a chatbot that improvises.
2. **(50s) Gas call.** Click *Heat out + strange smell*, Answer call. Point at: step 2 turns red before the LLM even runs; JARVIS speaks the exact escalation script; Daytona picks the on-call gas-certified tech; Approve dispatch; receipts say DRY RUN. Say it plainly: "this is a dry run, nobody was contacted."
2b. **(20s) Receipt + Replay.** Open the Decision Receipt, show the highlighted phrase that fired, then Replay: same inputs, identical decision fingerprint.
2c. **(15s) Negation.** Click *"I don't smell gas"*: no false emergency, and the receipt shows the negated phrase.
3. **(25s) Attack call.** Click *Prompt injection + refund*. Card number is redacted, no refund promised, billing handoff + security flag created, evals all green.
4. **(25s) Arize.** Open the trace for the gas call: the guardrail, tool, and evaluator spans show exactly why it decided what it did.
5. **(15s) Close.** Every sponsor does a real job; swap the HVAC config for plumbing, electrical, or property management.

## Tests

```bash
python tests/smoke_test.py   # zero keys: 14 trigger cases, reply guard, 6 calls, receipts, replay, approval gate, exact-script, no PII in traces
```

## Layout

```
jarvis/app.py            FastAPI server (NDJSON streaming of steps)
jarvis/pipeline.py       9-step orchestration + evals
jarvis/safety.py         triggers, redaction, output guard, browser allowlist, action policy
jarvis/dispatch_logic.py vetted decision code shipped into Daytona
jarvis/adapters.py       Claude, Daytona, Anchor, ElevenLabs adapters with mocks
jarvis/store.py          SurrealDB graph store (+ in-memory fallback)
jarvis/tracing.py        Arize AX / OpenInference spans
jarvis/docs_server.py    docs-only server (the ONLY thing you tunnel)
jarvis/data/             seed.json (synthetic data), triggers.json (safety config)
site/                    fictional company service-docs site the browser reads
static/index.html        dashboard
```
