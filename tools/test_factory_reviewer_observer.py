from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/independent-reviewer"))
import native_agent
import review_observer as telemetry

METADATA = {"role": "reviewer", "review_id": "review-1", "head_sha": "a" * 40,
            "pass_number": 1, "repository": "Driftward-LLC/factory"}
SESSION = "factory-reviewer:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TOOLS = {"factory_source_read_file"}


class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_): pass


class ReviewerObserverTests(unittest.TestCase):
    def test_named_profile_metadata_survives_native_child_home(self):
        named = '/opt/data/profiles/factory-independent-reviewer'
        for home, expected in [(named, True), (named+'/profiles/factory-reviewer-'+str(uuid.uuid4()), True),
                               (named+'/profiles/factory-canary-ab12cd34', True),
                               ('/opt/data/factory-reviewer-auth/profiles/factory-reviewer-'+str(uuid.uuid4()), False),
                               (named+'/profiles/../other', False), (named+'/profiles/unrelated', False)]:
            with self.subTest(home=home), patch.dict(os.environ, {'HERMES_HOME': home}):
                observer = telemetry.ReviewObserver(METADATA, SESSION, TOOLS, {})
                self.assertEqual(observer.profile, 'factory-independent-reviewer' if expected else None)

    def test_review_trace_deterministic_separate_pass_head_and_generator(self):
        trace = telemetry.trace_id(METADATA)
        self.assertEqual(uuid.UUID(trace).version, 5)
        self.assertEqual(trace, telemetry.trace_id(dict(reversed(list(METADATA.items())))))
        for field, value in [("pass_number", 2), ("head_sha", "b" * 40), ("review_id", "review-2")]:
            self.assertNotEqual(trace, telemetry.trace_id({**METADATA, field: value}))
        generator = uuid.uuid5(uuid.NAMESPACE_URL, "driftward-hermes:trace:attempt:" + SESSION)
        self.assertNotEqual(trace, str(generator))
        self.assertEqual(telemetry.review_metadata({**METADATA, "generator_session_id": "private"}), {})
        frame = native_agent.request_frame(SESSION, "only the prompt", METADATA)
        request = native_agent.read_request(io.BytesIO(frame))
        self.assertEqual(request["telemetry"], METADATA)
        self.assertNotIn("review-1", request["prompt"])

    def test_metadata_privacy_request_hierarchy_and_start_timestamp(self):
        delivered = []
        class Opener:
            def open(self, request, timeout):
                self_test.assertEqual(timeout, 1)
                delivered.extend(json.loads(request.data)["batch"])
                return Response()
        self_test = self
        observer = telemetry.ReviewObserver(METADATA, SESSION, TOOLS,
            {"POSTHOG_API_KEY": "synthetic-export-key"})
        private = {"request": "private request", "assistant_message": "private completion",
                   "tool_args": {"secret": "private arguments"}, "result": "private source",
                   "error_message": "private error", "requirements": "private requirements", "findings": "private findings"}
        common = {"session_id": SESSION, "api_request_id": "native:turn:api:0", **private}
        with patch.object(telemetry, "build_opener", return_value=Opener()), patch.object(telemetry.time, "time", return_value=1000):
            observer.capture("pre_api_request", common)
            observer.capture("post_api_request", {**common, "api_duration": 2, "usage": {"input_tokens": 9, "output_tokens": 4}})
            observer.capture("post_tool_call", {**common, "tool_name": "factory_source_read_file", "tool_call_id": "native-call",
                                               "status": "error", "duration_ms": 500})
            observer.close()
        self.assertEqual(len(delivered), 2)
        generation, tool = [event["properties"] for event in delivered]
        self.assertEqual(tool["$ai_parent_id"], generation["$ai_span_id"])
        self.assertEqual(generation["$ai_input_tokens"], 9)
        self.assertEqual(delivered[0]["timestamp"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(delivered[1]["timestamp"], "1970-01-01T00:16:39.500000+00:00")
        raw = json.dumps(delivered)
        for secret in ["private", "synthetic-export-key", "requirements", "findings", "tool_args", "native-call"]:
            self.assertNotIn(secret, raw)

    def test_missing_config_invalid_host_and_failing_export_are_non_authoritative(self):
        observer = telemetry.ReviewObserver(METADATA, SESSION, TOOLS, {})
        observer.capture("post_api_request", {"session_id": SESSION, "api_request_id": "request"})
        self.assertIsNone(observer.worker)
        self.assertEqual(observer.events.qsize(), 0)
        for host in ["http://posthog.com", "https://user:key@posthog.com", "https://posthog.com?key=x",
                     "https://posthog.com#fragment", "https://posthog.com/path", "https://posthog.com:444"]:
            self.assertIsNone(telemetry.attach_observer(METADATA, SESSION, TOOLS,
                {"POSTHOG_API_KEY": "synthetic-export-key", "POSTHOG_HOST": host}))
        self.assertIsNone(telemetry.NoRedirect().redirect_request(None, None))
        observer = telemetry.ReviewObserver(METADATA, SESSION, TOOLS, {"POSTHOG_API_KEY": "synthetic-export-key"})
        class Failing:
            def open(self, *_args, **_kwargs): raise RuntimeError("private failure")
        with patch.object(telemetry, "build_opener", return_value=Failing()):
            observer.capture("api_request_error", {"session_id": SESSION, "api_request_id": "request",
                                                   "error_message": "private failure"})
            before = time.monotonic()
            observer.close()
            self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(observer.events.unfinished_tasks, 0)

    def test_callback_queue_and_pending_state_are_bounded(self):
        observer = telemetry.ReviewObserver(METADATA, SESSION, TOOLS, {"POSTHOG_API_KEY": "synthetic-export-key"})
        observer.worker = object()  # Leave the queue undrained to exercise overflow without network.
        for index in range(1000):
            observer.capture("pre_api_request", {"session_id": SESSION, "api_request_id": "r" + str(index)})
        self.assertEqual(len(observer.pending), 64)
        before = time.monotonic()
        for index in range(1000):
            observer.capture("post_api_request", {"session_id": SESSION, "api_request_id": "r" + str(index)})
        self.assertEqual(observer.events.qsize(), 128)
        self.assertLess(time.monotonic() - before, 1)


if __name__ == "__main__":
    unittest.main()
