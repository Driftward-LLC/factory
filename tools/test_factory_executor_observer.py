import importlib.util
import os
import json
import queue
from pathlib import Path
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "deploy/executor/plugins/posthog/__init__.py"
SPEC = importlib.util.spec_from_file_location("posthog_observer", MODULE_PATH)
posthog = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
# requests is installed in the executor, deliberately not in control-plane CI.
with patch.dict(sys.modules, {"requests": SimpleNamespace(post=lambda *args, **kwargs: None)}):
    SPEC.loader.exec_module(posthog)


class PostHogObserverTest(unittest.TestCase):
    def test_event_is_metadata_only_and_groups_requests_by_session(self):
        common = {
            "session_id": "session-1",
            "task_id": "task-1",
            "turn_id": "turn-1",
            "provider": "openai-codex",
            "model": "gpt-5.6",
        }
        first = posthog._event({**common, "api_request_id": "request-1", "usage": {"input_tokens": 10, "output_tokens": 5}})
        second = posthog._event({**common, "turn_id": "turn-2", "api_request_id": "request-2", "usage": {"input_tokens": 4, "output_tokens": 2}})
        props = first["properties"]

        self.assertEqual(first["event"], "$ai_generation")
        self.assertEqual(props["$ai_trace_id"], second["properties"]["$ai_trace_id"])
        self.assertNotEqual(props["$ai_span_id"], second["properties"]["$ai_span_id"])
        self.assertEqual(props["$ai_session_id"], "session-1")
        self.assertNotIn("$ai_input", props)
        self.assertNotIn("$ai_output", props)
        self.assertEqual(props["$ai_input_tokens"], 10)
        self.assertEqual(props["$ai_output_tokens"], 5)
        legacy = posthog._event({**common, "api_request_id": "request-3", "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
        self.assertEqual(legacy["properties"]["$ai_input_tokens"], 3)
        self.assertEqual(legacy["properties"]["$ai_output_tokens"], 1)

    def test_tool_spans_share_factory_trace_and_generation_parent_without_content(self):
        context = {
            "FACTORY_OBSERVABILITY_JOB_ID": "01234567-89ab-cdef-0123-456789abcdef",
            "FACTORY_OBSERVABILITY_ATTEMPT_ID": "abcdef01-2345-6789-abcd-ef0123456789",
            "FACTORY_OBSERVABILITY_LINEAR_ISSUE": "ENG-61",
            "FACTORY_OBSERVABILITY_GENERATION": "2",
        }
        common = {"session_id": "session-1", "task_id": "task-1", "turn_id": "turn-1", "api_request_id": "request-1"}
        captured = []
        with patch.dict(os.environ, context), patch.object(posthog, "_enqueue", captured.append):
            generation = posthog._event({**common, "api_call_count": 3})
            posthog.on_post_tool_call(**common, tool_name="read_file", tool_call_id="call-1", duration_ms=1250, status="ok", args={"path": "SECRET"}, result="SECRET", error_message="SECRET", middleware_trace=["SECRET"])
            posthog.on_post_tool_call(**common, tool_name="terminal", tool_call_id="call-2", duration_ms=0, status="blocked", error_type="plugin_block", error_message="SECRET")
            later = posthog._event({**common, "session_id": "session-2", "turn_id": "turn-2"})
        props = captured[0]["properties"]
        self.assertEqual(captured[0]["event"], "$ai_span")
        self.assertEqual(props["$ai_trace_id"], generation["properties"]["$ai_trace_id"])
        self.assertEqual(props["$ai_trace_id"], later["properties"]["$ai_trace_id"])
        self.assertEqual(props["$ai_parent_id"], generation["properties"]["$ai_span_id"])
        self.assertEqual(props["$ai_latency"], 1.25)
        self.assertEqual(props["factory.job_id"], context["FACTORY_OBSERVABILITY_JOB_ID"])
        self.assertEqual(props["factory.attempt_id"], context["FACTORY_OBSERVABILITY_ATTEMPT_ID"])
        self.assertEqual(props["factory.linear_issue"], "ENG-61")
        self.assertEqual(props["factory.generation"], 2)
        self.assertNotEqual(props["$ai_span_id"], captured[1]["properties"]["$ai_span_id"])
        self.assertEqual(captured[1]["properties"]["$ai_error"], "plugin_block")
        self.assertNotIn("SECRET", json.dumps(captured))
        self.assertNotIn("$ai_input_state", props)
        self.assertNotIn("$ai_output_state", props)

    def test_invalid_correlation_is_omitted_and_retries_have_distinct_traces(self):
        with patch.dict(os.environ, {
            "FACTORY_OBSERVABILITY_JOB_ID": "SECRET",
            "FACTORY_OBSERVABILITY_ATTEMPT_ID": "SECRET",
            "FACTORY_OBSERVABILITY_LINEAR_ISSUE": "ENG-61 SECRET",
            "FACTORY_OBSERVABILITY_GENERATION": "SECRET",
        }):
            self.assertEqual(posthog._factory_properties(), {})
            first = posthog._trace_id({"session_id": "session-1"})
            self.assertNotEqual(first, posthog._trace_id({"session_id": "session-2"}))
            with patch.dict(os.environ, {"FACTORY_OBSERVABILITY_ATTEMPT_ID": "abcdef01-2345-6789-abcd-ef0123456789"}):
                self.assertNotEqual(first, posthog._trace_id({"session_id": "session-1"}))
                retry = posthog._trace_id({"session_id": "session-1"})
            with patch.dict(os.environ, {"FACTORY_OBSERVABILITY_ATTEMPT_ID": "abcdef01-2345-6789-abcd-ef0123456780"}):
                self.assertNotEqual(retry, posthog._trace_id({"session_id": "session-1"}))

    def test_tool_without_request_has_trace_parent_and_missing_call_ids_do_not_collide(self):
        captured = []
        with patch.object(posthog, "_enqueue", captured.append):
            for _ in range(2):
                posthog.on_post_tool_call(session_id="session-1", tool_name="SECRET command", status="error", error_type="SECRET response")
        props = captured[0]["properties"]
        self.assertEqual(props["$ai_parent_id"], props["$ai_trace_id"])
        self.assertEqual(props["$ai_error"], "ToolError")
        self.assertNotIn("SECRET", json.dumps(captured))
        self.assertNotEqual(props["$ai_span_id"], captured[1]["properties"]["$ai_span_id"])

    def test_registration_uses_only_native_observer_hooks(self):
        hooks = {}
        class Context:
            def register_hook(self, name, handler):
                hooks[name] = handler
        posthog.register(Context())
        self.assertEqual(set(hooks), {"pre_api_request", "post_api_request", "api_request_error", "post_tool_call"})

    def test_full_queue_and_disabled_export_are_fail_open(self):
        full = queue.Queue(maxsize=1)
        full.put({})
        with patch.dict(os.environ, {"POSTHOG_API_KEY": "test"}), patch.object(posthog, "_EVENTS", full), patch.object(posthog, "_WORKER_STARTED", True):
            with self.assertLogs(posthog.logger, level="WARNING"):
                posthog._enqueue({"event": "$ai_span"})
            self.assertEqual(full.qsize(), 1)
        with patch.dict(os.environ, {"POSTHOG_API_KEY": ""}), patch.object(posthog, "_EVENTS", full):
            posthog._enqueue({"event": "$ai_span"})
            self.assertEqual(full.qsize(), 1)

    def test_error_type_is_recorded_without_error_message(self):
        captured = []
        old_enqueue = posthog._enqueue
        posthog._enqueue = captured.append
        try:
            posthog.on_api_request_error(
                session_id="session-1",
                api_request_id="request-1",
                error_type="AuthenticationError",
                error_message="sensitive provider response",
            )
        finally:
            posthog._enqueue = old_enqueue
        props = captured[0]["properties"]
        self.assertEqual(props["$ai_error"], "AuthenticationError")
        self.assertNotIn("error_message", props)

    def test_native_error_dictionary_exports_only_validated_type(self):
        captured = []
        with patch.object(posthog, "_enqueue", captured.append):
            for error_type, expected in (("AuthenticationError", "AuthenticationError"),
                                         ("SECRET provider response", "RequestError"),
                                         (None, "RequestError")):
                with self.subTest(error_type=error_type):
                    posthog.on_api_request_error(error={"type": error_type, "message": "SECRET"})
                    self.assertEqual(captured[-1]["properties"]["$ai_error"], expected)
                    self.assertTrue(captured[-1]["properties"]["$ai_is_error"])
        self.assertNotIn("SECRET", json.dumps(captured))

    def test_native_pre_request_count_survives_success_and_error_hooks(self):
        common = {"session_id": "session-1", "task_id": "task-1", "turn_id": "turn-1"}
        captured = []
        with patch.dict(os.environ, {"POSTHOG_API_KEY": "test"}), patch.object(posthog, "_PENDING", {}), patch.object(posthog, "_enqueue", captured.append):
            for request, count in (("success", 12345), ("error", 6789), ("zero", 123)):
                posthog.on_pre_api_request(**common, api_request_id=request, request_char_count=count, messages="SECRET")
            # Complete out of order; native completion hooks omit request_char_count.
            posthog.on_api_request_error(**common, api_request_id="error", error={"type": "AuthenticationError", "message": "SECRET"})
            posthog.on_post_api_request(**common, api_request_id="success")
            posthog.on_post_api_request(**common, api_request_id="zero", request_char_count=0)
            posthog.on_post_api_request(**common, api_request_id="without-pre")
            self.assertEqual([event["properties"]["hermes.input_chars"] for event in captured], [6789, 12345, 0, 0])
            self.assertEqual(posthog._PENDING, {})
        self.assertNotIn("SECRET", json.dumps(captured))

    def test_delivery_failure_is_fail_open(self):
        old_post = posthog.requests.post
        old_key = os.environ.get("POSTHOG_API_KEY")
        os.environ["POSTHOG_API_KEY"] = "test"
        posthog.requests.post = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
        try:
            posthog._deliver({"event": "$ai_generation"})
        finally:
            posthog.requests.post = old_post
            if old_key is None:
                os.environ.pop("POSTHOG_API_KEY", None)
            else:
                os.environ["POSTHOG_API_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
