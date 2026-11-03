"""Run exported generation inputs through the original OpenCode execution core."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit

from . import harness as H
from . import self_shot as SS
from .images import load_image_base64
from .prompt import env_brief

ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.18.25"
PROVIDERS = {
    "chat-completions": H.NPM_OPENAI_COMPAT,
    "responses": H.NPM_OPENAI,
    "anthropic": H.NPM_ANTHROPIC,
}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def contained(root, relative):
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("Input must be an existing file inside the exported inputs")
    return path


def read_inputs(root, selection=None):
    rows = json.loads((root / "manifest.json").read_text())
    ids = [r["sample_id"] for r in rows]
    if any(type(sid) is not int or sid < 1 for sid in ids) or len(ids) != len(set(ids)):
        raise ValueError("Manifest task IDs must be unique positive integers")
    if selection is not None and (
        len(selection) != len(set(selection)) or not set(selection) <= set(ids)
    ):
        raise ValueError("Requested IDs must be unique members of the input manifest")
    chosen = [r for r in rows if selection is None or r["sample_id"] in selection]
    if not chosen:
        raise ValueError("No tasks selected")
    for row in chosen:
        prompt = contained(root, row["prompt"])
        if row.get("prompt_sha256") and sha(prompt) != row["prompt_sha256"]:
            raise ValueError(f'Prompt hash mismatch: {row["sample_id"]}')
        if row.get("language") not in ("en", "zh", "zh-CN", "zh-cn"):
            raise ValueError("Re-export inputs: manifest must include task language")
        if not row["references"]:
            raise ValueError("Each task must include reference images")
        names = []
        for i, ref in enumerate(row["references"], 1):
            path = contained(root, ref["path"])
            if sha(path) != ref["sha256"]:
                raise ValueError(f'Reference hash mismatch: {row["sample_id"]}')
            name = Path(ref.get("source_name", f"screenshot_{i}.png")).stem
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Invalid reference basename")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("Duplicate reference basenames")
    return chosen


def child_env(config_path, private, output_limit, key):
    # Explicit allowlist: unrelated shell credentials and user configuration never propagate.
    env = {
        k: os.environ[k]
        for k in (
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS",
        )
        if k in os.environ
    }
    # Use a fresh per-attempt HOME; this does not change the operator's environment.
    env.update(
        {
            "HOME": str(private / "home"),
            "XDG_CONFIG_HOME": str(private / "config"),
            "XDG_DATA_HOME": str(private / "data"),
            "XDG_CACHE_HOME": str(private / "cache"),
            "TMPDIR": str(private / "tmp"),
            "REFCRAFT_MODEL_API_KEY": key,
            "OPENCODE_CONFIG": str(config_path),
            "OPENCODE_DB": str(private / "opencode.db"),
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": str(output_limit),
            "CI": "1",
            "NO_COLOR": "1",
        }
    )
    for name in ("home", "config", "data", "cache", "tmp"):
        (private / name).mkdir()
    return env


def redact_files(directory, secret):
    if not secret:
        return
    # Provider errors or tool output may echo credentials; keep values out of published receipts.
    for path in directory.rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.suffix in (".json", ".jsonl", ".log", ".txt", ".html")
        ):
            data = path.read_bytes()
            for value in {secret.encode(), json.dumps(secret)[1:-1].encode()}:
                if value in data:
                    data = data.replace(value, b"[REDACTED]")
            path.write_bytes(data)


def usage_report(tally):
    """Unknown or incomplete provider usage is null, rather than reported as free."""
    result = tally.as_dict()
    for name, missing in tally.usage_missing.items():
        if missing or tally.turns == 0:
            result["tokens"][name] = None
    result["cost_usd"] = None  # Custom provider pricing is not configured by this runner.
    for row in result["step_usage"]:
        row["cost_usd"] = None
    return result


def run_task(row, settings):
    sid = row["sample_id"]
    inputs, output = Path(settings["inputs"]), Path(settings["output"])
    receipts = output / "records" / str(sid)
    receipts.mkdir(parents=True)
    history = []
    secret = "" if settings["dry_run"] else os.environ["REFCRAFT_MODEL_API_KEY"]
    caps = {
        "max_turns": settings["max_turns"],
        "wall_clock_sec": settings["wall_seconds"],
        "stall_grace_sec": 240,
        "max_stall_credit_sec": 1800,
        "max_budget_usd": 0,
    }
    for attempt in range(1, settings["retries"] + 2):
        record = receipts / f"attempt-{attempt:03d}"
        record.mkdir()
        workdir = output / "workspaces" / str(sid) / f"attempt-{attempt:03d}"
        workdir.mkdir(parents=True)
        started = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="refcraft_attachments_") as temp:
                private = Path(temp)
                images = []
                for i, ref in enumerate(row["references"], 1):
                    src = contained(inputs, ref["path"])
                    encoded = load_image_base64(str(src), quality=80, max_height=4096)
                    if encoded is None:
                        raise ValueError("Reference preprocessing failed; no images may be dropped")
                    dst = private / (
                        Path(ref.get("source_name", f"screenshot_{i}.png")).stem + ".jpg"
                    )
                    dst.write_bytes(base64.b64decode(encoded))
                    images.append(dst)
                image_hashes = [sha(p) for p in images]
                prompt = contained(inputs, row["prompt"]).read_text() + env_brief(
                    workdir=workdir,
                    ref_mode="conv",
                    ref_names=[p.name for p in images],
                    max_turns=settings["max_turns"],
                    lang=row["language"],
                )
                (record / "prompt.txt").write_text(prompt)
                SS.install_helper(workdir)
                write(
                    record / "inputs.json",
                    {
                        "sample_id": sid,
                        "ref_mode": "conv",
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "references": [
                            {"name": p.name, "sha256": h} for p, h in zip(images, image_hashes)
                        ],
                    },
                )
                if settings["dry_run"]:
                    result = {"attempt": attempt, "status": "prepared", "model_calls": 0}
                    history.append(result)
                    write(record / "run.json", result)
                    break
                runtime = settings["runtime"]
                H.assert_opencode_runtime_unchanged(runtime)
                cfg = H.build_oc_config(
                    base_url=settings["base_url"],
                    wire=settings["model"],
                    context_limit=settings["context_limit"],
                    output_limit=settings["output_limit"],
                    provider_id="refcraft",
                    key_env="REFCRAFT_MODEL_API_KEY",
                    options=settings["model_options"],
                    reasoning_effort=settings["reasoning_effort"],
                    npm=PROVIDERS[settings["api"]],
                    allow_network=True,
                )
                # Deny release, input and receipt roots by name. This is a tool
                # guard, not an OS sandbox; use a disposable container for independent runs.
                cfg["permission"]["bash"].update(
                    {f"*{p}*": "deny" for p in (ROOT, inputs, receipts.parent)}
                )
                config_path = H.write_oc_config(private / "oc_config", cfg)
                env = child_env(config_path, private, settings["output_limit"], secret)
                write(record / "opencode.json", cfg)  # Contains only an environment placeholder.
                argv = H.build_argv(
                    bin_path=runtime["resolved_binary"],
                    workdir=workdir,
                    provider_id="refcraft",
                    wire=settings["model"],
                    image_paths=images,
                )
                # Unlike the old wall-only TTL fallback, never delete before ingestion evidence.
                first_event = [None]

                def tick(tally, spent):
                    if tally.n_json and first_event[0] is None:
                        first_event[0] = time.monotonic()
                    if first_event[0] is not None and time.monotonic() - first_event[0] >= 5:
                        for path in images:
                            path.unlink(missing_ok=True)

                shots = SS.ShotServer(workdir, renderer_dir=private / "renderer").start()
                try:
                    run = H.run_opencode(
                        argv=[str(x) for x in argv],
                        env=env,
                        prompt=prompt,
                        cwd=workdir,
                        caps=caps,
                        stdout_path=record / "events.jsonl",
                        stderr_path=record / "stderr.log",
                        on_tick=tick,
                    )
                finally:
                    H.kill_active_oc_process_group()
                    shots.stop()
                artifact = workdir / "index.html"
                fallback = False
                if not artifact.exists():
                    candidates = [
                        p for p in workdir.glob("*.htm*") if p.is_file() and not p.is_symlink()
                    ]
                    if len(candidates) == 1:
                        artifact, fallback = candidates[0], True
                artifact_ok = (
                    artifact.is_file() and not artifact.is_symlink() and artifact.stat().st_size > 0
                )
                tally = run.pop("tally")
                stop, why = H.classify_stop(
                    tally=tally,
                    killed_by=run["killed_by"],
                    returncode=run["returncode"],
                    artifact_ok=artifact_ok,
                )
                audit = H.audit_run(
                    workdir=workdir,
                    whitelist=[p.name for p in images],
                    tally=tally,
                    ref_hashes=image_hashes,
                    refs_in_workdir=False,
                )
                result = {
                    "attempt": attempt,
                    "status": "finished",
                    "stop_reason": stop,
                    "stop_detail": why,
                    "artifact_written": artifact_ok,
                    "artifact_fallback": fallback,
                    "started_at_unix": started,
                    **run,
                    "usage": usage_report(tally),
                    "audit": audit,
                    "shots": shots.report(),
                }
                if artifact_ok:
                    shutil.copyfile(artifact, record / "index.html")
                retry = attempt <= settings["retries"] and H.is_retryable_transport_failure(
                    stop, tally.error_blob
                )
                result["retry_scheduled"] = retry
                history.append(result)
                write(record / "run.json", result)
                if not retry:
                    if artifact_ok:
                        destination = output / "responses" / str(sid) / "index.html"
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(artifact, destination)
                    break
                time.sleep(settings["retry_delay"])
        except Exception as exc:
            H.kill_active_oc_process_group()
            message = str(exc).replace(secret, "[REDACTED]") if secret else str(exc)
            result = {
                "attempt": attempt,
                "status": "runner_error",
                "error": type(exc).__name__,
                "detail": message,
            }
            history.append(result)
            write(record / "run.json", result)
            break
        finally:
            redact_files(record, secret)
            redact_files(workdir, secret)
    final = history[-1]
    summary = {
        "sample_id": sid,
        "attempts": len(history),
        "status": final["status"],
        "stop_reason": final.get("stop_reason"),
        "artifact_written": final.get("artifact_written", False),
        "tainted": final.get("audit", {}).get("tainted", False),
        "complete": final.get("stop_reason") == H.STOP_END
        and final.get("artifact_written", False)
        and not final.get("audit", {}).get("tainted", False),
    }
    write(receipts / "summary.json", summary)
    redact_files(output / "responses" / str(sid), secret)
    return summary


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument(
        "--output", type=Path, required=True, help="New directory outside the release and inputs"
    )
    ap.add_argument("--model", required=True, help="Provider model ID, passed through unchanged")
    ap.add_argument("--api", choices=PROVIDERS, default="chat-completions")
    ap.add_argument("--base-url", default=os.environ.get("REFCRAFT_MODEL_BASE_URL", ""))
    ap.add_argument("--context-limit", type=positive, required=True)
    ap.add_argument("--output-limit", type=positive, required=True)
    ap.add_argument("--reasoning-effort", default="high")
    ap.add_argument("--model-options", type=Path, help="JSON model options (no credentials)")
    ap.add_argument("--sample-ids", help="Comma-separated paper task IDs (1–300); default all exported tasks")
    ap.add_argument("--workers", type=positive, default=1)
    ap.add_argument("--max-turns", type=positive, default=50)
    ap.add_argument("--wall-seconds", type=positive, default=7200)
    ap.add_argument(
        "--retries", type=int, default=0, help="Bounded clean retries for transport errors only"
    )
    ap.add_argument("--retry-delay", type=positive, default=60)
    ap.add_argument("--opencode-bin", default="opencode")
    ap.add_argument("--allow-version-override", action="store_true")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and prepare prompts; no model/CLI calls",
    )
    args = ap.parse_args(argv)
    try:
        inputs, output = args.inputs.resolve(), args.output.resolve()
        if args.retries < 0:
            raise ValueError("Retries cannot be negative")
        for forbidden in (ROOT, inputs):
            if output == forbidden or forbidden in output.parents or output in forbidden.parents:
                raise ValueError(
                    "Output must be outside and separate from the release and input directories"
                )
        if args.output.exists():
            raise ValueError(
                "Output already exists; choose a new directory to preserve previous evidence"
            )
        selection = list(map(int, args.sample_ids.split(","))) if args.sample_ids else None
        rows = read_inputs(inputs, selection)
        options = json.loads(args.model_options.read_text()) if args.model_options else {}
        if not isinstance(options, dict) or re.search(
            r'"(?:api[_-]?key|authorization|password|secret|access[_-]?token)"\s*:',
            json.dumps(options),
            re.I,
        ):
            raise ValueError("Model options must be an object without credential fields")
        json.dumps(options, allow_nan=False)
        parsed = urlsplit(args.base_url)
        if args.base_url and (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Base URL must be HTTP(S), without credentials, query or fragment")
        runtime = None
        if not args.dry_run:
            if not args.base_url or not os.environ.get("REFCRAFT_MODEL_API_KEY"):
                raise ValueError("Set REFCRAFT_MODEL_BASE_URL and REFCRAFT_MODEL_API_KEY")
            runtime = H.opencode_runtime_metadata(args.opencode_bin)
            if runtime["version"] != VERSION and not args.allow_version_override:
                raise ValueError(f'Expected OpenCode {VERSION}; found {runtime["version"]}')
        settings = vars(args).copy()
        settings.update(
            inputs=str(inputs), output=str(output), runtime=runtime, model_options=options
        )
        for key in ("inputs", "output", "model_options"):
            assert not isinstance(settings[key], Path)
        output.mkdir(parents=True)
        plan = {k: v for k, v in settings.items() if k not in ("inputs", "output")}
        plan.update(
            schema="refcraft.inference.v1",
            sample_ids=[r["sample_id"] for r in rows],
            ref_mode="conv",
            jpeg={"quality": 80, "max_height": 4096, "max_bytes": 1500000},
            input_manifest_sha256=sha(inputs / "manifest.json"),
            pricing_configured=False,
            retry_policy="bounded_clean_restart",
            effective_caps={
                "max_turns": args.max_turns,
                "wall_clock_sec": args.wall_seconds,
                "stall_grace_sec": 240,
                "max_stall_credit_sec": 1800,
                "max_budget_usd": None,
                "max_shot_requests": 40,
                "shot_timeout_sec": 180,
                "network_denied": False,
            },
            inference_code_sha256=H.canonical_json_sha256(
                {p.name: sha(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
            ),
        )
        write(output / "run.json", plan)
        results = []
        if args.workers == 1:
            H.install_worker_signal_forwarding()
            for row in rows:
                result = run_task(row, settings)
                results.append(result)
                print(json.dumps(result), flush=True)
        else:
            with ProcessPoolExecutor(
                max_workers=args.workers, initializer=H.install_worker_signal_forwarding
            ) as pool:
                futures = {pool.submit(run_task, row, settings): row["sample_id"] for row in rows}
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    print(json.dumps(result), flush=True)
        lookup = {r["sample_id"]: r for r in results}
        results = [lookup[r["sample_id"]] for r in rows]
        write(
            output / "summary.json",
            {
                "tasks": len(rows),
                "complete": sum(r["complete"] for r in results),
                "stop_reasons": dict(Counter(r["stop_reason"] for r in results)),
                "results": results,
                "dry_run": args.dry_run,
                "score": None,
            },
        )
        return (
            0
            if all(r["complete"] or (args.dry_run and r["status"] == "prepared") for r in results)
            else 2
        )
    except (ValueError, OSError) as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
