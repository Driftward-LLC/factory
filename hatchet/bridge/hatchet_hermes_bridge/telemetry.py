"""Optional, metadata-only OTLP spans. Never part of bridge authority."""
from contextlib import contextmanager
import os
import re

_provider = None
_initialized = False
_PARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-(00|01)")
_ENDPOINT = re.compile(r"http://(?:127\.0\.0\.1|localhost|otel-collector):([1-9][0-9]{0,4})/v1/traces")


def _tracer():
    global _provider, _initialized
    if not _initialized:
        _initialized = True
        endpoint = os.environ.get("FACTORY_OTEL_TRACES_ENDPOINT", "")
        match = _ENDPOINT.fullmatch(endpoint)
        if not match or int(match[1]) > 65535 or any(os.environ.get(key) for key in (
            "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS")):
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        _provider = TracerProvider(resource=Resource({"service.name": "factory-bridge"}), sampler=ALWAYS_ON, shutdown_on_exit=False)
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=1),
            max_queue_size=256, max_export_batch_size=32, schedule_delay_millis=1000, export_timeout_millis=1500))
    return _provider.get_tracer("driftward.factory.execution", "1") if _provider else None


@contextmanager
def bridge_span(parent):
    span = None
    try:
        match = _PARENT.fullmatch(parent or "")
        if match and int(match[1], 16) and int(match[2], 16):
            tracer = _tracer()
            if tracer:
                from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags, set_span_in_context
                context = set_span_in_context(NonRecordingSpan(SpanContext(
                    int(match[1], 16), int(match[2], 16), True, TraceFlags(int(match[3], 16)))))
                span = tracer.start_span("factory.bridge.handle", context=context, kind=SpanKind.SERVER)
    except Exception:
        pass  # Missing/broken telemetry must not reject or replay an operation.
    try:
        yield span
    except BaseException:
        record_status(span, 500)
        raise
    finally:
        if span:
            try:
                span.end()
            except Exception:
                pass


def record_status(span, status):
    if span:
        try:
            from opentelemetry.trace import StatusCode
            span.set_attribute("http.response.status_code", status)
            if status >= 400:
                span.set_status(StatusCode.ERROR)
        except Exception:
            pass


def flush():
    if _provider:
        try:
            _provider.force_flush(timeout_millis=1500)
        except Exception:
            pass
