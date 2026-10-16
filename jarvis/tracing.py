"""Arize AX observability. Every agent run, LLM call, tool call, guardrail
decision and eval is an OpenInference span, so the whole decision path is
inspectable in Arize. Without keys, spans are created against a no-op provider."""
import json
import logging
from contextlib import contextmanager

from opentelemetry import trace

from . import config

log = logging.getLogger("jarvis.tracing")
STATUS = {"name": "Arize AX", "mode": "mock", "detail": "No Arize keys: traces shown in the local trace panel only"}


def init_tracing() -> None:
    if not (config.ARIZE_SPACE_ID and config.ARIZE_API_KEY):
        return
    try:
        from arize.otel import register, Transport
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        kw = {"transport": Transport.GRPC}
        if config.ARIZE_TRANSPORT == "http":  # use if the venue network blocks gRPC
            kw = {"transport": Transport.HTTP, "endpoint": "https://otlp.arize.com/v1/traces"}
        tp = register(
            space_id=config.ARIZE_SPACE_ID,
            api_key=config.ARIZE_API_KEY,
            project_name=config.ARIZE_PROJECT,
            **kw,
            verbose=False,
        )
        AnthropicInstrumentor().instrument(tracer_provider=tp)
        STATUS.update(mode="live", detail=f"Streaming traces to project '{config.ARIZE_PROJECT}'")
    except Exception as e:  # never let tracing take the app down
        log.warning("Arize init failed: %s", e)
        STATUS.update(mode="error", detail=f"Arize init failed: {str(e)[:160]}")


def _val(v):
    if isinstance(v, (str, int, float, bool)):
        return v if not isinstance(v, str) else v[:8000]
    try:
        return json.dumps(v, default=str)[:8000]
    except Exception:
        return str(v)[:8000]


@contextmanager
def span(name: str, kind: str, **attrs):
    """kind: AGENT | TOOL | GUARDRAIL | EVALUATOR | CHAIN | LLM"""
    tracer = trace.get_tracer("jarvis-ops")
    with tracer.start_as_current_span(name) as s:
        s.set_attribute("openinference.span.kind", kind)
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k.replace("__", "."), _val(v))
        yield s


def set_attrs(s, **attrs):
    for k, v in attrs.items():
        if v is not None:
            try:
                s.set_attribute(k.replace("__", "."), _val(v))
            except Exception:
                pass
