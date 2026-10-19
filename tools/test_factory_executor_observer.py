"""Metadata identity checks for the reviewed native executor hook plugin."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ExecutorObserverTests(unittest.TestCase):
    def test_native_profile_binds_role_without_changing_attempt_trace(self):
        source = Path(__file__).resolve().parents[1] / "deploy/factory-observers/posthog/__init__.py"
        spec = importlib.util.spec_from_file_location("factory_executor_observer", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        attempt = "11111111-2222-4333-8444-555555555555"
        with patch.dict(os.environ, {"FACTORY_OBSERVABILITY_ATTEMPT_ID": attempt}, clear=True):
            for home, role in [("/opt/data", False), ("/opt/data/profiles/factory-executor", True)]:
                with patch.dict(sys.modules, {"hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path(home))}):
                    properties = module._factory_properties()
                    self.assertEqual(properties.get("factory.role"), "executor" if role else None)
                    self.assertEqual(properties.get("factory.profile"), "factory-executor" if role else None)
                    event = module._event({"api_request_id": "request", "model": "gpt-6.1-sol",
                                           "prompt": "must not export", "completion": "must not export"})
                    self.assertNotIn("must not export", str(event))
                    if not role:
                        trace = event["properties"]["$ai_trace_id"]
                    else:
                        self.assertEqual(event["properties"]["$ai_trace_id"], trace)
