"""Exercise the actual Bash CLI against a synthetic private Unix endpoint."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import unittest


@unittest.skipUnless(os.geteuid() == 0, "operator CLI requires root")
class OperatorCliTests(unittest.TestCase):
    def request(self, status, body, post=False):
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while self.rfile.readline() not in (b"\r\n", b""):
                    pass
                self.wfile.write(f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "operator.env"
            credentials.write_text("HATCHET_FACTORY_OPERATOR_TOKEN=private-operator-token\n")
            credentials.chmod(0o600)
            with socketserver.UnixStreamServer(str(root / "operator.sock"), Handler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                args = ["attempts", "evidence", "attempt-id"]
                if post:
                    args = ["review-recovery", "ENG-162", "inspect", "--review", "review-id", "--source-attempt", "attempt-id", "--yes"]
                try:
                    return subprocess.run(
                        ["bash", str(Path(__file__).resolve().parents[1] / "factory/factory"), *args],
                        env={**os.environ, "FACTORY_OPERATOR_ENV": str(credentials),
                             "FACTORY_OPERATOR_SOCKET": str(root / "operator.sock"), "TMPDIR": directory},
                        capture_output=True, timeout=10,
                    )
                finally:
                    server.shutdown()
                    thread.join()
                    self.assertEqual(sorted(p.name for p in root.iterdir()), ["operator.env", "operator.sock"])

    def test_success_preserves_stdout_bytes(self):
        body = b'{"evidence": []}\n\n'
        result = self.request(200, body)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, body, b""))

    def test_error_is_bounded_sanitized_redacted_and_stderr_only(self):
        message = "fresh inspection required\n\x1b[31mprivate-operator-token " + "x" * 1000
        for post in (False, True):
            with self.subTest(post=post):
                result = self.request(400, json.dumps({"error": message, "secret": "never-print"}).encode(), post)
                self.assertEqual(result.returncode, 22)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"factory operator: fresh inspection required", result.stderr)
                self.assertIn(b"[redacted]", result.stderr)
                self.assertNotIn(b"private-operator-token", result.stderr)
                self.assertNotIn(b"never-print", result.stderr)
                self.assertNotIn(b"\x1b", result.stderr)
                self.assertLess(len(result.stderr), 700)

    def test_unstructured_errors_never_dump_response(self):
        for body in (b"<html>secret</html>", b'{"error":{"secret":"hidden"}}',
                     b'{"error":"' + b"s" * 70000 + b'"}', b"[" * 2000 + b"]" * 2000):
            with self.subTest(body=body[:30]):
                result = self.request(500, body)
                self.assertEqual(result.returncode, 22)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"factory operator:", result.stderr)


if __name__ == "__main__":
    unittest.main()
