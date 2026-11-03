"""Offline HTTP contract test; the local fake endpoint is not a model judge."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.evaluation.client import Judge


class ClientTests(unittest.TestCase):
    def test_request_receipt_and_truncation_rejection(self):
        received = []
        auth_headers = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                auth_headers.append(self.headers.get("Authorization"))
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                finish = "stop" if len(received) == 1 else "length"
                payload = {
                    "choices": [
                        {
                            "finish_reason": finish,
                            "message": {"content": '{"status":"unresolved","score":null}'},
                        }
                    ]
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"REFCRAFT_JUDGE_API_KEY": "synthetic-test-token"}
            ):
                directory = Path(tmp)
                judge = Judge(
                    f"http://127.0.0.1:{server.server_port}/v1", "fake-judge-for-test", directory
                )
                result = judge.ask("valid", "test policy", {"query": "synthetic fixture"})
                self.assertIsNone(result["score"])
                receipt = json.loads((directory / "valid.json").read_text())
                self.assertIsNone(receipt["usage"])
                self.assertNotIn("synthetic-test-token", (directory / "valid.json").read_text())
                with self.assertRaisesRegex(RuntimeError, "invalid_or_truncated"):
                    judge.ask("truncated", "test policy", {})
                self.assertFalse((directory / "truncated.json").exists())
                self.assertEqual(received[0]["messages"][0]["role"], "system")
                self.assertEqual(auth_headers, ["Bearer synthetic-test-token"] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_explicit_key_ignores_machine_auth_and_proxy(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
                    ).encode()
                )

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                auth = root / "netrc"
                auth.write_text(
                    "machine 127.0.0.1 login implicit-user password implicit-password\n"
                )
                auth.chmod(0o600)
                with patch.dict(
                    os.environ,
                    {
                        "NETRC": str(auth),
                        "HTTP_PROXY": "http://127.0.0.1:1",
                        "http_proxy": "http://127.0.0.1:1",
                        "NO_PROXY": "",
                        "no_proxy": "",
                        "REFCRAFT_JUDGE_API_KEY": "explicit-test-key",
                    },
                ):
                    Judge(f"http://127.0.0.1:{server.server_port}/v1", "fixture", root).ask(
                        "test", "policy", {}
                    )
                self.assertEqual(received, ["Bearer explicit-test-key"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_reject_ambiguous_or_credentialed_urls(self):
        for url in (
            "https:///v1",
            "https://user:secret@example.com/v1",
            "https://example.com/v1?key=value",
            "https://example.com/v1#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Judge(url, "fixture", Path("."))

    def test_redirect_does_not_make_another_authenticated_request(self):
        requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests_seen.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/unexpected-target")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"REFCRAFT_JUDGE_API_KEY": "test-key"}
            ):
                judge = Judge(f"http://127.0.0.1:{server.server_port}/v1", "fixture", Path(tmp))
                with self.assertRaisesRegex(RuntimeError, "judge_http_307"):
                    judge.ask("test", "policy", {})
                self.assertEqual(requests_seen, ["/v1/chat/completions"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
