import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from hatchet_hermes_bridge import telemetry
from hatchet_hermes_bridge.server import BridgeHandler
from hatchet_hermes_bridge.store import connect_runtime
from test_support import connect_migrate


class TelemetryTests(unittest.TestCase):
    def test_bridge_http_propagation_auth_and_metadata_only(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider(resource=Resource({"service.name": "test"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        with tempfile.NamedTemporaryFile() as database, patch.object(telemetry, "_provider", provider), \
                patch.object(telemetry, "_initialized", True), patch.dict(os.environ, {"HATCHET_HERMES_BRIDGE_TOKEN": "SECRET_TOKEN"}):
            connect_migrate(database.name).close()
            conn = connect_runtime(database.name, check_same_thread=False)
            server = HTTPServer(("127.0.0.1", 0), BridgeHandler)
            server.conn = conn
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                parent = "00-fb9fa7e140655818af723e41a35fe39a-123456789abcdef0-01"
                headers = {"Authorization": "Bearer SECRET_TOKEN", "traceparent": parent}
                address = f"http://127.0.0.1:{server.server_port}"
                with urlopen(Request(address + "/health", headers=headers)) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError):
                    urlopen(Request(address + "/SECRET_PATH?secret=SECRET_QUERY", headers=headers))
                with self.assertRaises(HTTPError):
                    urlopen(Request(address + "/health", headers={"traceparent": parent}))
                with urlopen(Request(address + "/health", headers={**headers, "traceparent": "invalid"})):
                    pass
                spans = exporter.get_finished_spans()
                self.assertEqual(len(spans), 2)
                for span in spans:
                    self.assertEqual(span.context.trace_id, int("fb9fa7e140655818af723e41a35fe39a", 16))
                    self.assertEqual(span.parent.span_id, int("123456789abcdef0", 16))
                    self.assertEqual(set(span.attributes), {"http.response.status_code"})
                    self.assertFalse(span.events)
                self.assertEqual(spans[1].attributes["http.response.status_code"], 404)
                self.assertEqual(spans[1].status.status_code.name, "ERROR")
                self.assertNotIn("SECRET_", json.dumps([dict(s.attributes) for s in spans]))
            finally:
                server.shutdown(); thread.join(); server.server_close(); conn.close()

    def test_telemetry_failure_does_not_repeat_or_swallow_business_operation(self):
        calls = []
        failure = RuntimeError("business failure")
        with patch.object(telemetry, "_tracer", side_effect=RuntimeError("broken exporter")):
            with self.assertRaises(RuntimeError) as caught:
                with telemetry.bridge_span("00-" + "1" * 32 + "-" + "2" * 16 + "-01"):
                    calls.append(1)
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [1])

    def test_disabled_invalid_and_credential_bearing_configuration(self):
        for env in [{}, {"FACTORY_OTEL_TRACES_ENDPOINT": "https://external.test/v1/traces"},
                    {"FACTORY_OTEL_TRACES_ENDPOINT": "http://localhost:99999/v1/traces"},
                    {"FACTORY_OTEL_TRACES_ENDPOINT": "http://localhost:4318/v1/traces", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=SECRET"}]:
            with patch.dict(os.environ, env, clear=True), patch.object(telemetry, "_initialized", False), patch.object(telemetry, "_provider", None):
                self.assertIsNone(telemetry._tracer())


if __name__ == "__main__":
    unittest.main()
