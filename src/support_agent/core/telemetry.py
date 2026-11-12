"""OpenTelemetry wiring: OTLP/gRPC export to the collector, plus auto-instrumentation.

Spans created by this app follow a small, stable vocabulary so dashboards and
evals can rely on them:

    chat.request
      ├── retrieval.search
      ├── llm.completion          (one per model round-trip)
      ├── tool.search_docs
      ├── tool.escalate_to_human
      └── guardrail.citations

Attributes never carry full prompts or document bodies (see the collector's
redaction processor too) — only counts, ids, scores, tokens and latencies.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from support_agent.core.config import Settings

logger = logging.getLogger(__name__)

_TRACER_NAME = "support_agent"
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None
_instrumented = False


def configure_telemetry(app: FastAPI, settings: Settings) -> None:
    """Idempotent: safe to call from tests and from the ASGI lifespan."""
    global _tracer_provider, _meter_provider, _instrumented

    if not settings.otel_enabled:
        logger.info("otel disabled by configuration")
        return
    if _instrumented:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.environment,
        }
    )

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=True,
                timeout=10,
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint,
                    insecure=True,
                    timeout=10,
                ),
                export_interval_millis=15_000,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    HTTPXClientInstrumentor().instrument()
    AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]
    RedisInstrumentor().instrument()
    _instrumented = True
    logger.info("otel configured", extra={"endpoint": settings.otel_exporter_otlp_endpoint})


def configure_worker_telemetry(settings: Settings) -> None:
    """Telemetry for long-running workers (no ASGI app to instrument).

    Shares the OTLP exporter configuration with the API but skips FastAPI
    instrumentation, so the outbox worker reports under the same service name
    with a distinct ``service.component`` attribute.
    """
    global _tracer_provider, _instrumented

    if not settings.otel_enabled or _instrumented:
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.environment,
            "service.component": "outbox-worker",
        }
    )
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=True,
                timeout=10,
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider
    _instrumented = True
    logger.info("worker otel configured", extra={"endpoint": settings.otel_exporter_otlp_endpoint})


def shutdown_telemetry() -> None:
    global _tracer_provider, _meter_provider, _instrumented
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None
    _instrumented = False


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def current_trace_id() -> str:
    """Hex trace id of the active span, or ''. Used in API responses for support."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return ""
    return format(context.trace_id, "032x")


def get_meter() -> metrics.Meter:
    return metrics.get_meter(_TRACER_NAME)
