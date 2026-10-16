"""FastAPI server for JARVIS Dispatch.  Run:  python -m jarvis.app"""
import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, safety
from .adapters import LLM, Browser, Sandbox, Voice
from .pipeline import Dispatcher
from .store import SEED, make_store, now_iso
from .tracing import init_tracing, set_attrs, span

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis.app")
ROOT = Path(__file__).resolve().parent.parent
STATE: dict = {}


@asynccontextmanager
async def lifespan(app):
    init_tracing()
    STATE["d"] = Dispatcher(make_store(), LLM(), Sandbox(), Browser(), Voice())
    threading.Thread(target=STATE["d"].sandbox.warm, daemon=True).start()
    log.info("JARVIS Dispatch ready on http://localhost:%s", 8000)
    yield
    STATE["d"].sandbox.close()


app = FastAPI(title="JARVIS Dispatch", lifespan=lifespan)
app.mount("/site", StaticFiles(directory=ROOT / "site", html=True), name="site")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class CallIn(BaseModel):
    transcript: str
    caller_phone: str | None = None


class DecisionIn(BaseModel):
    work_order_id: str
    approve: bool
    note: str | None = None


class TTSIn(BaseModel):
    text: str


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    d = STATE["d"]
    return {"integrations": d.statuses(), "company": SEED["company"], "sample_calls": SEED["sample_calls"],
            "public_base_url": config.PUBLIC_BASE_URL or None}


@app.post("/api/call")
async def call(body: CallIn):
    if not body.transcript.strip():
        return JSONResponse({"error": "empty transcript"}, status_code=400)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def emit(ev):
        loop.call_soon_threadsafe(q.put_nowait, ev)

    def worker():
        try:
            STATE["d"].handle_call(body.transcript[:2000], body.caller_phone, emit)
        except Exception as e:
            log.exception("call failed")
            emit({"type": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"})
        finally:
            emit({"type": "done"})

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        while True:
            ev = await q.get()
            yield json.dumps(ev, default=str) + "\n"
            if ev.get("type") == "done":
                break

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/decision")
def decision(body: DecisionIn):
    store = STATE["d"].store
    wo = store.get_work_order(body.work_order_id)
    if not wo:
        return JSONResponse({"error": "work order not found"}, status_code=404)
    if wo.get("status") != "awaiting_approval":
        return JSONResponse({"error": f"already {wo.get('status')}"}, status_code=409)
    with span("guardrail.human_approval", "GUARDRAIL", input__value=body.work_order_id) as s:
        receipts = []
        if body.approve:
            for a in wo.get("proposed_actions", []):
                receipts.append({"action": a["action"], "result": "DRY RUN: not sent to any external system",
                                 "detail": a["detail"], "at": now_iso()})
            fields = {"status": "dispatched_dry_run", "approved_at": now_iso(), "receipts": receipts}
        else:
            fields = {"status": "held_for_human", "declined_at": now_iso(), "note": body.note or "Declined by dispatcher"}
        updated = store.update_work_order(body.work_order_id, fields)
        set_attrs(s, output__value={"status": fields["status"], "actions": [x["action"] for x in receipts]}, guardrail__approved=body.approve)
    store.audit(event="human_decision", work_order=body.work_order_id, approved=body.approve, actions=len(receipts))
    return {"work_order": updated, "receipts": receipts}


class ReplayIn(BaseModel):
    work_order_id: str


@app.get("/api/receipt")
def receipt(work_order_id: str):
    wo = STATE["d"].store.get_work_order(work_order_id)
    if not wo or not wo.get("receipt"):
        return JSONResponse({"error": "receipt not found"}, status_code=404)
    r = dict(wo["receipt"])
    r["approval"] = {"status": wo.get("status"), "approved_at": wo.get("approved_at"), "declined_at": wo.get("declined_at"),
                     "receipts": wo.get("receipts", []), "proposed_actions": wo.get("proposed_actions", [])}
    r["work_order_id"] = work_order_id
    return r


@app.post("/api/replay")
async def replay(body: ReplayIn):
    out = await asyncio.to_thread(STATE["d"].replay, body.work_order_id)
    if out.get("error"):
        return JSONResponse(out, status_code=404)
    return out


@app.get("/api/workorders")
def workorders():
    store = STATE["d"].store
    return {"work_orders": store.list_work_orders(), "audit": store.list_audit()}


@app.post("/api/tts")
def tts(body: TTSIn):
    text, _ = safety.redact(body.text)
    audio = STATE["d"].voice.tts(text)
    if not audio:
        return Response(status_code=204)
    return Response(content=audio, media_type="audio/mpeg")


def main():
    import uvicorn
    uvicorn.run("jarvis.app:app", host="127.0.0.1", port=8000, log_level="info")  # local only; never tunnel this port


if __name__ == "__main__":
    main()
