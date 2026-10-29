"""Exercise native request telemetry without sending data to a collector."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from support.control_plane import API_HEADERS, control_plane_app


@pytest.fixture
def telemetry(monkeypatch):
    # Replace provider lookups rather than registering process-wide singletons.
    monkeypatch.delenv("FASTAPI_OTEL_AUTO_CONFIGURE", raising=False)
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter)
    try:
        yield SimpleNamespace(spans=spans, reader=reader, tracer=tracer)
    finally:
        tracer.shutdown()
        meter.shutdown()


def test_request_spans_and_metrics_exclude_periodic_probes(settings, telemetry):
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics", headers=API_HEADERS).status_code == 200
        assert client.get("/v3/domains", headers=API_HEADERS).status_code == 200
        assert client.get("/v3/domains").status_code == 401

    spans = telemetry.spans.get_finished_spans()
    requests = [span for span in spans if span.kind is trace.SpanKind.SERVER]
    assert len(requests) == 2
    assert {span.attributes["http.route"] for span in requests} == {"/v3/domains"}
    assert {span.attributes["http.response.status_code"] for span in requests} == {
        200,
        401,
    }
    assert "fastapi.endpoint" in {span.name for span in spans}
    assert "x" * 32 not in repr([span.attributes for span in spans])

    data = telemetry.reader.get_metrics_data()
    measurements = {
        metric.name: metric.data.data_points
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    durations = measurements["http.server.request.duration"]
    assert sum(point.count for point in durations) == 2
    assert {point.attributes["http.route"] for point in durations} == {"/v3/domains"}
    assert all(
        point.value == 0 for point in measurements["http.server.active_requests"]
    )


def test_exception_details_are_not_exported(settings, telemetry, monkeypatch):
    from opentelemetry import _logs

    def refuse_logger():
        raise AssertionError("exception log export must remain disabled")

    monkeypatch.setattr(_logs, "get_logger_provider", refuse_logger)
    app = control_plane_app(settings)

    @app.get("/test-failure", include_in_schema=False)
    def failure():
        raise RuntimeError("sensitive-infrastructure-credential")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/test-failure").status_code == 500

    spans = telemetry.spans.get_finished_spans()
    request = next(span for span in spans if span.kind is trace.SpanKind.SERVER)
    assert request.status.status_code is trace.StatusCode.ERROR
    assert request.attributes["http.response.status_code"] == 500
    exported = repr([(span.attributes, span.events) for span in spans])
    assert "sensitive-infrastructure-credential" not in exported


def test_environment_enables_native_otlp_export(settings, telemetry, monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    destinations = []
    exporter = InMemorySpanExporter()

    def build_exporter(*, endpoint):
        destinations.append(endpoint)
        return exporter

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", build_exporter)
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    with TestClient(control_plane_app(settings)) as client:
        assert client.get("/v3/domains", headers=API_HEADERS).status_code == 200
    telemetry.tracer.force_flush()

    assert destinations == ["https://collector.example.com/v1/traces"]
    assert any(
        span.kind is trace.SpanKind.SERVER for span in exporter.get_finished_spans()
    )
