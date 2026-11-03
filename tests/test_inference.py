"""Regression checks for limits, missing evidence, input boundaries and reference audit."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refcraft.inference.self_shot import ShotServer
from refcraft.inference.runner import read_inputs, usage_report, child_env, run_task
from refcraft.inference import harness as H


class InferenceTests(unittest.TestCase):
    def test_turn_count_and_missing_usage(self):
        tally = H.RunTally()
        for _ in range(4):
            tally.feed({"type": "tool_use", "part": {"tool": "bash"}})
        tally.feed({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 10}}})
        self.assertEqual(tally.turns, 1)
        report = usage_report(tally)
        self.assertEqual(report["tokens"]["input"], 10)
        self.assertIsNone(report["tokens"]["output"])
        self.assertIsNone(report["cost_usd"])

    def run_process(self, code, **caps):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            start = time.monotonic()
            result = H.run_opencode(
                argv=[sys.executable, "-u", "-c", code],
                env=dict(os.environ),
                prompt="Quoted `prompt` $x\n中文",
                cwd=p,
                caps={"max_turns": 2, "wall_clock_sec": 1, "max_stall_credit_sec": 0, **caps},
                stdout_path=p / "events.jsonl",
                stderr_path=p / "stderr.log",
            )
            self.assertLess(time.monotonic() - start, 8)
            return result

    def test_turn_cap_kills_process(self):
        code = "import sys,json,time; sys.stdin.read(); print(json.dumps({'type':'step_finish','part':{'reason':'tool-calls'}})); print(json.dumps({'type':'step_finish','part':{'reason':'tool-calls'}})); time.sleep(30)"
        result = self.run_process(code, wall_clock_sec=10)
        self.assertEqual(result["killed_by"], H.STOP_MAX_TURNS)
        self.assertEqual(result["tally"].turns, 2)

    def test_closed_stdout_does_not_bypass_wall_limit(self):
        result = self.run_process(
            "import os,sys,time; sys.stdin.read(); os.close(1); time.sleep(30)"
        )
        self.assertEqual(result["killed_by"], H.STOP_WALL)
        self.assertNotEqual(result["returncode"], 0)

    def test_enforced_cap_precedes_prior_length(self):
        tally = H.RunTally()
        tally.feed({"type": "step_finish", "part": {"reason": "length"}})
        self.assertEqual(
            H.classify_stop(tally=tally, killed_by=H.STOP_WALL, returncode=-9, artifact_ok=True)[0],
            H.STOP_WALL,
        )

    def test_reference_copy_is_distinct_from_own_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            (wd / "own.png").write_bytes(b"own screenshot")
            kwargs = dict(
                workdir=wd,
                whitelist=[],
                tally=H.RunTally(),
                ref_hashes=[hashlib.sha256(b"reference").hexdigest()],
            )
            self.assertFalse(H.audit_run(**kwargs)["tainted"])
            (wd / "copied.png").write_bytes(b"reference")
            self.assertTrue(H.audit_run(**kwargs)["tainted"])

    def test_manifest_hash_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "inputs"
            root.mkdir()
            (root / "prompt.txt").write_text("prompt")
            (root / "ref.png").write_bytes(b"image")
            row = {
                "sample_id": 2,
                "language": "en",
                "prompt": "prompt.txt",
                "references": [{"path": "ref.png", "sha256": hashlib.sha256(b"image").hexdigest()}],
            }
            (root / "manifest.json").write_text(json.dumps([row]))
            self.assertEqual(read_inputs(root)[0]["sample_id"], 2)
            (root / "ref.png").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                read_inputs(root)
            (root.parent / "secret.png").write_bytes(b"image")
            row["references"][0]["path"] = "../secret.png"
            (root / "manifest.json").write_text(json.dumps([row]))
            with self.assertRaisesRegex(ValueError, "inside"):
                read_inputs(root)

    def test_private_environment_and_screenshot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd = root / "work"
            wd.mkdir()
            env = child_env(root / "oc.json", root, 32000, "placeholder")
            self.assertEqual(env["REFCRAFT_MODEL_API_KEY"], "placeholder")
            self.assertEqual(env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"], "32000")
            self.assertNotIn("SSH_AUTH_SOCK", env)
            (root / "outside.html").write_text("<html/>")
            (wd / "linked.html").symlink_to(root / "outside.html")
            server = ShotServer(wd, renderer_dir=root / "renderer")
            with self.assertRaises(PermissionError):
                server._resolve("linked.html")

    def test_schema_errors_are_not_retried(self):
        self.assertFalse(
            H.is_retryable_transport_failure(H.STOP_API_ERROR, "HTTP 400 invalid request schema")
        )
        self.assertTrue(
            H.is_retryable_transport_failure(H.STOP_GATEWAY, "HTTP 503 service unavailable")
        )

    def test_public_transport_retry_policy(self):
        for message, retry in (
            ("HTTP 401 unauthorized", False),
            ("HTTP 403 organization disabled", False),
            ("HTTP 404 endpoint not found", False),
            ("HTTP 400 invalid model schema", False),
            ("HTTP 429 too many requests", True),
            ("HTTP 503 service unavailable", True),
            ("socket connection reset", True),
        ):
            with self.subTest(message=message):
                tally = H.RunTally()
                tally.feed({"type": "error", "error": {"message": message}})
                stop, _ = H.classify_stop(
                    tally=tally, killed_by="", returncode=1, artifact_ok=False
                )
                self.assertEqual(H.is_retryable_transport_failure(stop, tally.error_blob), retry)

    def test_clean_retry_preserves_evidence_without_reusing_partial_html(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "prompt.txt").write_text("Make a page")
            Image.new("RGB", (8, 8), "red").save(inputs / "ref.png")
            binary = root / "fake-opencode"
            binary.write_text("#!" + sys.executable + """
import json, pathlib, sys
if '--version' in sys.argv:
    print('1.18.25'); raise SystemExit(0)
sys.stdin.read()
if pathlib.Path.cwd().name == 'attempt-001':
    pathlib.Path('index.html').write_text('partial output')
    print(json.dumps({'type':'error','error':{'message':'HTTP 503 local-retry-key'}}))
    raise SystemExit(1)
print(json.dumps({'type':'step_finish','part':{'reason':'stop'}}))
""")
            binary.chmod(0o755)
            row = {
                "sample_id": 2,
                "language": "en",
                "prompt": "prompt.txt",
                "references": [{"path": "ref.png", "source_name": "screenshot_1.png"}],
            }
            settings = {
                "inputs": str(inputs),
                "output": str(root / "output"),
                "dry_run": False,
                "max_turns": 50,
                "wall_seconds": 10,
                "retries": 1,
                "retry_delay": 1,
                "runtime": H.opencode_runtime_metadata(str(binary)),
                "base_url": "http://127.0.0.1:1/v1",
                "model": "mock",
                "context_limit": 200000,
                "output_limit": 32000,
                "model_options": {},
                "reasoning_effort": "high",
                "api": "chat-completions",
            }
            with patch.dict(os.environ, {"REFCRAFT_MODEL_API_KEY": "local-retry-key"}):
                result = run_task(row, settings)
            self.assertEqual(result["attempts"], 2)
            self.assertFalse(result["complete"])
            self.assertFalse(result["artifact_written"])
            record = root / "output/records/2/attempt-001"
            self.assertEqual((record / "index.html").read_text(), "partial output")
            self.assertNotIn("local-retry-key", (record / "events.jsonl").read_text())
            self.assertFalse((root / "output/responses/2/index.html").exists())


if __name__ == "__main__":
    unittest.main()
