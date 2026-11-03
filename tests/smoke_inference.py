#!/usr/bin/env python3
"""Real OpenCode + Chromium against a scripted LOCAL endpoint; no real model calls."""

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--opencode-bin", default="opencode")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    seen = []
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(request)
            has_tool_result = any(m.get("role") == "tool" for m in request["messages"])
            if not has_tool_result:
                tools = [x["function"]["name"] for x in request.get("tools", [])]
                if "bash" not in tools:
                    failures.append("Missing bash tool")
                code = "from pathlib import Path; Path('index.html').write_text('<!doctype html><html><body><h1>Local inference smoke</h1></body></html>')"
                command = (
                    "python3 -c " + shlex.quote(code) + " && python3 shot.py --widths 390 --wait 1"
                )
                call = {
                    "id": "smoke_call",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps(
                            {
                                "command": command,
                                "description": "Write and inspect the page",
                                "timeout": 120000,
                            }
                        ),
                    },
                }
                delta = {"role": "assistant", "tool_calls": [dict(call, index=0)]}
                reason = "tool_calls"
                message = {"role": "assistant", "content": None, "tool_calls": [call]}
            else:
                delta = {"role": "assistant", "content": "Done."}
                reason = "stop"
                message = dict(delta)
            common = {
                "id": "smoke-completion",
                "created": int(time.time()),
                "model": request["model"],
            }
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in [
                    dict(
                        common,
                        object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": delta, "finish_reason": None}],
                    ),
                    dict(
                        common,
                        object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": {}, "finish_reason": reason}],
                        usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                    ),
                ]:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        dict(
                            common,
                            object="chat.completion",
                            choices=[{"index": 0, "message": message, "finish_reason": reason}],
                            usage={
                                "prompt_tokens": 100,
                                "completion_tokens": 10,
                                "total_tokens": 110,
                            },
                        )
                    ).encode()
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="refcraft-inference-smoke-") as tmp:
            base = Path(tmp)
            # Copy only inference files: proves the runner does not import the source repository,
            # evaluator, keypoints, or the benchmark dataset.
            standalone = base / "standalone"
            shutil.copytree(
                ROOT / "refcraft/inference",
                standalone / "refcraft/inference",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (standalone / "refcraft/__init__.py").write_text('"""Standalone inference package."""\n')
            (standalone / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/generate.py", standalone / "scripts/generate.py")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/export_inputs.py"),
                    "--output",
                    str(base / "inputs"),
                    "--sample-ids",
                    "1",
                ],
                check=True,
                capture_output=True,
            )
            env = dict(os.environ, REFCRAFT_MODEL_API_KEY="local-smoke-placeholder")
            command = [
                sys.executable,
                str(standalone / "scripts/generate.py"),
                "--inputs",
                str(base / "inputs"),
                "--output",
                str(base / "run"),
                "--model",
                "smoke-vision",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "--context-limit",
                "200000",
                "--output-limit",
                "32000",
                "--max-turns",
                "5",
                "--wall-seconds",
                "120",
                "--opencode-bin",
                args.opencode_bin,
            ]
            proc = subprocess.run(
                command, cwd=base, env=env, capture_output=True, text=True, timeout=240
            )
            if proc.returncode:
                details = "\n".join(
                    p.read_text()[-3000:] for p in (base / "run/records").rglob("stderr.log")
                )
                raise RuntimeError(
                    f"Inference smoke failed: {proc.stdout}\n{proc.stderr}\n{details}"
                )
            record = base / "run/records/1/attempt-001"
            run = json.loads((record / "run.json").read_text())
            prepared = json.loads((record / "inputs.json").read_text())
            image_hashes = []
            texts = []
            for message in seen[0]["messages"]:
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if part.get("type") == "image_url":
                            image_hashes.append(
                                hashlib.sha256(
                                    base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                                ).hexdigest()
                            )
                        if part.get("type") == "text":
                            texts.append(part["text"])
                elif isinstance(content, str):
                    texts.append(content)
            assert image_hashes == [
                r["sha256"] for r in prepared["references"]
            ], "Image bytes/order mismatch"
            assert (record / "prompt.txt").read_text() in "\n".join(
                texts
            ), "Prompt changed in transport"
            assert (base / "run/responses/1/index.html").is_file()
            assert run["shots"]["n_self_shots"] == 1, run["shots"]
            assert run["usage"]["turns"] == 2, run["usage"]["turns"]
            assert not failures, failures
            assert run["usage"]["cost_usd"] is None
            report = {
                "status": "passed",
                "sample_id": 1,
                "opencode": "1.18.25",
                "endpoint": "scripted_local_http",
                "real_model_calls": 0,
                "http_requests": len(seen),
                "ordered_reference_images": len(image_hashes),
                "prompt_bytes_preserved": True,
                "real_chromium_shots": 1,
                "turns": 2,
                "standalone_inference_copy": True,
                "html_collected": True,
            }
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
