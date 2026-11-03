"""OpenCode configuration, event accounting, limits and audit; derived from the original harness.

Platform routing and credentials are deliberately supplied by the caller.
"""

from __future__ import annotations
from typing import Any, Dict, Iterable, List, Optional, Tuple
from pathlib import Path
import time
import threading
import subprocess
import shutil
import signal
import re
import queue
import os
import json
import hashlib
import functools
import contextlib
import logging
import math

logger = logging.getLogger(__name__)
_ACTIVE_OC_PGID = 0


def kill_active_oc_process_group(sig: int = signal.SIGKILL) -> None:
    global _ACTIVE_OC_PGID
    pgid = int(_ACTIVE_OC_PGID or 0)
    _ACTIVE_OC_PGID = 0
    if not pgid:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


def install_worker_signal_forwarding() -> None:

    def _forward(signum, _frame):
        kill_active_oc_process_group(signal.SIGKILL)
        raise SystemExit(128 + int(signum))

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _forward)


NPM_OPENAI_COMPAT = "@ai-sdk/openai-compatible"
NPM_OPENAI = "@ai-sdk/openai"
NPM_ANTHROPIC = "@ai-sdk/anthropic"


def oc_tool_profile(wire: str) -> str:
    return "gpt5_reasoning" if "gpt-5" in str(wire or "").lower() else "default"


def oc_request_shape(
    provider_id: str = "refcraft", npm: str = NPM_OPENAI_COMPAT, wire: str = ""
) -> str:
    return f"{provider_id}:{npm.split('/')[-1]}/{oc_tool_profile(wire)}"


REFSOURCE_DENY_GLOBS: Tuple[str, ...] = (
    "*keypoints*",
    "*refspec*",
    "*query_sets*",
    "*checklists*",
    "*ref_checklist*",
    "*all_scores*",
    "*results/responses*",
    "*.refcraft.env*",
    "*REFCRAFT_MODEL_API_KEY*",
    "*.claude*",
    "*refcraft_attachments*",
)
_STREAM_TAINT_WORDS: Tuple[str, ...] = (
    "refspec",
    "query_sets",
    "checklists",
    "keypoints",
    "REFCRAFT_MODEL_API_KEY",
    ".claude",
)
_DENIED_MARK = "specified a rule which prevents"
_NET_DENY_GLOBS: Tuple[str, ...] = (
    "*curl*",
    "*wget*",
    "*http://*",
    "*https://*",
    "*aria2c*",
    "*httpie*",
    "*nc *",
    "*ncat*",
    "*telnet*",
    "*ssh *",
    "*scp *",
    "*rsync *",
    "*pip install*",
    "*pip3 install*",
    "*npm install*",
    "*npm i *",
    "*npx *",
    "*yarn add*",
    "*pnpm add*",
    "*git clone*",
    "*git fetch*",
    "*git pull*",
    "*apt-get*",
    "*apt install*",
    "*urllib*",
    "*requests.get*",
    "*socket.socket*",
)


def bash_permission(allow_network: bool = True, refsource_guard: bool = True) -> Dict[str, str]:
    perm: Dict[str, str] = {"*": "allow"}
    if refsource_guard:
        for g in REFSOURCE_DENY_GLOBS:
            perm[g] = "deny"
    if not allow_network:
        for g in _NET_DENY_GLOBS:
            perm[g] = "deny"
    return perm


def network_denial_level(deny_network: Any) -> Any:
    return "tools+bash" if deny_network else False


def build_oc_config(
    *,
    base_url: str,
    wire: str,
    context_limit: int,
    output_limit: int,
    provider_id: str = "refcraft",
    key_env: str = "REFCRAFT_MODEL_API_KEY",
    options: Optional[dict] = None,
    reasoning_effort: str = "",
    allow_network: bool = True,
    refsource_guard: bool = True,
    npm: str = NPM_OPENAI_COMPAT,
) -> dict:
    try:
        context_limit = int(context_limit)
        output_limit = int(output_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenCode context/output limits 必须是整数") from exc
    if context_limit <= 0 or output_limit <= 0:
        raise ValueError(
            f"OpenCode context/output limits 必须是显式正整数；实得 context={context_limit}, output={output_limit}"
        )
    opts = dict(options or {})
    if reasoning_effort:
        opts.setdefault("reasoningEffort", reasoning_effort)
    model_entry: Dict[str, Any] = {
        "name": wire,
        "attachment": True,
        "reasoning": True,
        "tool_call": True,
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "options": opts,
    }
    model_entry["limit"] = {"context": context_limit, "output": output_limit}
    net = "allow" if allow_network else "deny"
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            provider_id: {
                "npm": npm,
                "name": provider_id,
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:%s}" % key_env,
                    "timeout": 900000,
                },
                "models": {wire: model_entry},
            }
        },
        "enabled_providers": [provider_id],
        "model": f"{provider_id}/{wire}",
        "small_model": f"{provider_id}/{wire}",
        "agent": {"title": {"disable": True}},
        "permission": {
            "*": "allow",
            "question": "deny",
            "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny"},
            "external_directory": {"*": "deny"},
            "webfetch": net,
            "websearch": net,
            "bash": bash_permission(allow_network, refsource_guard),
        },
        "instructions": [],
        "autoupdate": False,
        "share": "disabled",
    }


def write_oc_config(cfg_dir: Path, cfg: dict) -> Path:
    cfg_dir = Path(cfg_dir)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / "opencode.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(p, 384)
    return p


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


@functools.lru_cache(maxsize=8)
def opencode_runtime_metadata(bin_path: str = "opencode") -> dict:
    requested = str(bin_path or "opencode")
    found = shutil.which(requested) or (requested if os.path.isfile(requested) else "")
    if not found:
        raise FileNotFoundError(f"OpenCode executable not found: {requested}")
    resolved = Path(found).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"OpenCode executable is not a file: {resolved}")
    stat = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 << 20), b""):
            digest.update(chunk)
    try:
        proc = subprocess.run(
            [str(resolved), "--version"], text=True, capture_output=True, timeout=15, check=False
        )
        version = (proc.stdout or proc.stderr or "").strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        raise RuntimeError(f"Unable to identify OpenCode runtime: {exc}") from exc
    if not version:
        raise RuntimeError(f"OpenCode --version returned no version (rc={proc.returncode})")
    return {
        "requested_binary": requested,
        "resolved_binary": str(resolved),
        "version": version,
        "binary_sha256": digest.hexdigest(),
        "binary_size": stat.st_size,
        "binary_mtime_ns": stat.st_mtime_ns,
        "binary_device": stat.st_dev,
        "binary_inode": stat.st_ino,
    }


def assert_opencode_runtime_unchanged(runtime: dict) -> None:
    path = Path(str(runtime.get("resolved_binary") or ""))
    try:
        stat = path.stat()
    except OSError as exc:
        raise RuntimeError(f"OpenCode runtime disappeared after preflight: {path}") from exc
    got = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
    want = tuple(
        (
            runtime.get(key)
            for key in ("binary_size", "binary_mtime_ns", "binary_device", "binary_inode")
        )
    )
    if got != want:
        raise RuntimeError(f"OpenCode runtime changed after preflight: {path}")


OC_EVENT_TYPES = frozenset({"tool_use", "step_start", "step_finish", "text", "reasoning", "error"})
# Public transport failures only; provider account/routing errors are not retries.
_GATEWAY_PAT = re.compile(
    r"\b(?:408|500|502|503|504)\b|bad\s*gateway|gateway\s*time|"
    r"service(?:\s+temporarily)?\s*unavailable|"
    r"econnreset|econnrefused|epipe|etimedout|eai_again|enotfound|"
    r"socket hang up|connection (?:was\s+)?(?:reset|closed|refused)|"
    r"fetch failed|providerheadertimeouterror|provider response headers? timed out|"
    r"(?:the )?(?:operation|request) timed out|temporary failure in name resolution",
    re.I,
)
_RATELIMIT_PAT = re.compile(
    r"\b429\b|rate.?limit(?:ed|ing)?|too many requests|throttl|resource_exhausted|"
    r"requests have exceeded the throughput limit|"
    r"insufficient[_\s-]*quota|(?:allocation[_\s-]*)?quota[_\s-]*(?:exceeded|exhausted)",
    re.I,
)
_PERMANENT_HTTP_PAT = re.compile(r"\b(?:400|401|403|404|405|409|410|422)\b")
_REFUSAL_PAT = re.compile(
    "content[_\\- ]?filter|safety|prohibited_content|blocklist|recitation|i (?:can(?:no|')t|won'?t) (?:help|assist|comply)|我(?:不能|无法)(?:提供|协助|帮)",
    re.I,
)


def _is_retryable_gateway_blob(blob: str) -> bool:
    return bool(_GATEWAY_PAT.search(blob)) and not _PERMANENT_HTTP_PAT.search(blob)


def parse_event(line: str) -> Optional[dict]:
    s = (line or "").strip()
    if not s or s[0] != "{":
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


class RunTally:

    def __init__(self) -> None:
        self.n_lines = 0
        self.n_json = 0
        self.turns = 0
        self.session_id = ""
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        self.cost = 0.0
        self.usage_missing = {k: False for k in self.tokens}
        self.tool_calls: Dict[str, int] = {}
        self.denied: List[dict] = []
        self.errors: List[dict] = []
        self.texts: List[str] = []
        self.text_chars_total = 0
        self.text_chars_retained = 0
        self.n_truncated_text_events = 0
        self.reasoning_chars = 0
        self.unknown_types: Dict[str, int] = {}
        self.finish_reasons: List[str] = []
        self.last_finish_reason = ""
        self.n_length_finishes = 0
        self.first_event_timestamp_ms: Optional[int] = None
        self.last_event_timestamp_ms: Optional[int] = None
        self.step_usage: List[dict] = []
        self._finish_feed_index = 0
        self._last_finish_key = (-1, -1)

    def _add_usage(self, part: dict) -> None:
        tk = part.get("tokens") or {}
        cache_values = tk.get("cache") or {}
        for key in self.tokens:
            value = cache_values.get(key[6:]) if key.startswith("cache_") else tk.get(key)
            if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
                self.usage_missing[key] = True
        for src, dst in (("input", "input"), ("output", "output"), ("reasoning", "reasoning")):
            try:
                self.tokens[dst] += int(tk.get(src) or 0)
            except (TypeError, ValueError):
                pass
        cache = tk.get("cache") or {}
        for src, dst in (("read", "cache_read"), ("write", "cache_write")):
            try:
                self.tokens[dst] += int(cache.get(src) or 0)
            except (TypeError, ValueError):
                pass
        try:
            self.cost += float(part.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass

    def feed(self, evt: dict) -> None:
        self.n_json += 1
        try:
            timestamp = int(evt.get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp:
            self.first_event_timestamp_ms = min(
                int(self.first_event_timestamp_ms or timestamp), timestamp
            )
            self.last_event_timestamp_ms = max(
                int(self.last_event_timestamp_ms or timestamp), timestamp
            )
        t = str(evt.get("type") or "")
        if evt.get("sessionID") and (not self.session_id):
            self.session_id = str(evt["sessionID"])
        if t not in OC_EVENT_TYPES:
            self.unknown_types[t] = self.unknown_types.get(t, 0) + 1
            return
        part = evt.get("part") if isinstance(evt.get("part"), dict) else {}
        if t == "step_finish":
            self.turns += 1
            self._finish_feed_index += 1
            self._add_usage(part)
            reason = str(part.get("reason") or "").strip()
            finish_key = (
                timestamp if timestamp else int(time.time() * 1000),
                self._finish_feed_index,
            )
            if finish_key >= self._last_finish_key:
                self._last_finish_key = finish_key
                self.last_finish_reason = reason
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}

            def _integer(value) -> Optional[int]:
                try:
                    return int(value) if value is not None else None
                except (TypeError, ValueError, OverflowError):
                    return None

            try:
                step_cost = float(part.get("cost") or 0.0)
            except (TypeError, ValueError):
                step_cost = 0.0
            self.step_usage.append(
                {
                    "_finish_key": finish_key,
                    "timestamp": timestamp or None,
                    "reason": reason,
                    "tokens": {
                        "input": _integer(tokens.get("input")),
                        "output": _integer(tokens.get("output")),
                        "reasoning": _integer(tokens.get("reasoning")),
                        "cache_read": _integer(cache.get("read")),
                        "cache_write": _integer(cache.get("write")),
                    },
                    "cost_usd": step_cost,
                }
            )
            self.finish_reasons = [
                str(row.get("reason") or "")
                for row in sorted(self.step_usage, key=lambda row: row.get("_finish_key") or (0, 0))
            ]
            if reason == "length":
                self.n_length_finishes += 1
        elif t == "tool_use":
            name = str(part.get("tool") or "?")
            self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
            state = part.get("state") or {}
            if str(state.get("status") or "") == "error":
                msg = str(state.get("error") or state.get("output") or "")
                if _DENIED_MARK in msg:
                    self.denied.append(
                        {
                            "tool": name,
                            "message": msg[:600],
                            "input": json.dumps(state.get("input") or {}, ensure_ascii=False)[:400],
                        }
                    )
        elif t == "text":
            txt = str(part.get("text") or "")
            if txt.strip():
                self.text_chars_total += len(txt)
                if len(txt) > 12000:
                    txt = txt[:6000] + "\n…[text event truncated in tally]…\n" + txt[-6000:]
                    self.n_truncated_text_events += 1
                self.texts.append(txt)
                if len(self.texts) > 40:
                    self.texts = self.texts[-40:]
                self.text_chars_retained = sum((len(value) for value in self.texts))
        elif t == "reasoning":
            self.reasoning_chars += len(str(part.get("text") or ""))
        elif t == "error":
            err = evt.get("error") or {}
            if isinstance(err, dict):
                name = str(err.get("name") or "")
                data = err.get("data") if isinstance(err.get("data"), dict) else {}
                msg = str((data or {}).get("message") or err.get("message") or "")
            else:
                name, msg = ("ProviderError", str(err))
            self.errors.append({"name": name, "message": msg[:800]})

    @property
    def error_blob(self) -> str:
        return " | ".join((f"{e.get('name', '')}: {e.get('message', '')}" for e in self.errors))

    @property
    def last_text(self) -> str:
        return self.texts[-1] if self.texts else ""

    def as_dict(self) -> dict:
        ordered_usage = sorted(self.step_usage, key=lambda row: row.get("_finish_key") or (0, 0))
        public_usage = [
            {"turn": index, **{key: value for key, value in row.items() if key != "_finish_key"}}
            for index, row in enumerate(ordered_usage, start=1)
        ]
        finish_reasons = [str(row.get("reason") or "") for row in public_usage]
        return {
            "turns": self.turns,
            "session_id": self.session_id,
            "tokens": dict(self.tokens),
            "cost_usd": round(self.cost, 4),
            "tool_calls": dict(sorted(self.tool_calls.items())),
            "n_denied": len(self.denied),
            "denied": self.denied[:20],
            "errors": self.errors[:20],
            "text_chars_total": self.text_chars_total,
            "text_chars_retained": self.text_chars_retained,
            "n_truncated_text_events": self.n_truncated_text_events,
            "reasoning_chars": self.reasoning_chars,
            "n_events": self.n_json,
            "n_stdout_lines": self.n_lines,
            "unknown_event_types": dict(self.unknown_types),
            "finish_reasons": finish_reasons,
            "last_finish_reason": self.last_finish_reason,
            "n_length_finishes": self.n_length_finishes,
            "first_event_timestamp_ms": self.first_event_timestamp_ms,
            "last_event_timestamp_ms": self.last_event_timestamp_ms,
            "event_span_seconds": (
                round((self.last_event_timestamp_ms - self.first_event_timestamp_ms) / 1000.0, 3)
                if self.first_event_timestamp_ms is not None
                and self.last_event_timestamp_ms is not None
                else None
            ),
            "step_usage": public_usage,
        }


STOP_MAX_TURNS = "max_turns"
STOP_WALL = "wall_clock_timeout"
STOP_BUDGET = "max_budget"
STOP_RATELIMIT = "rate_limited"
STOP_GATEWAY = "gateway_stall"
STOP_END = "end_turn"
STOP_REFUSAL = "refusal"
STOP_API_ERROR = "api_error"
STOP_OUTPUT_LEN = "max_output_length"


def classify_stop(
    *, tally: RunTally, killed_by: str, returncode: int, artifact_ok: bool
) -> Tuple[str, str]:
    if killed_by:
        return (killed_by, f"Runner stopped the process: {killed_by}")
    if tally.last_finish_reason == "length":
        return (STOP_OUTPUT_LEN, "final step_finish reason=length")
    blob = tally.error_blob
    if blob:
        if re.search("\\b429\\b", blob):
            return (STOP_RATELIMIT, blob[:200])
        if "MessageOutputLengthError" in blob:
            return (STOP_OUTPUT_LEN, blob[:200])
        if _REFUSAL_PAT.search(blob):
            return (STOP_REFUSAL, blob[:200])
        if _is_retryable_gateway_blob(blob):
            return (STOP_GATEWAY, blob[:200])
        if _RATELIMIT_PAT.search(blob):
            return (STOP_RATELIMIT, blob[:200])
        return (STOP_API_ERROR, blob[:200])
    if returncode != 0:
        return (STOP_API_ERROR, f"opencode 非 0 退出（rc={returncode}），see stderr.log")
    if tally.turns == 0:
        return (STOP_GATEWAY, "0 轮就结束了（一发都没打出去/网关静默）")
    if not artifact_ok and _REFUSAL_PAT.search(tally.last_text or ""):
        return (STOP_REFUSAL, (tally.last_text or "")[:200])
    return (STOP_END, "")


def is_gateway_failure(stop_reason: str) -> bool:
    return stop_reason == STOP_GATEWAY


def is_retryable_transport_failure(stop_reason: str, error_blob: str = "") -> bool:
    if _PERMANENT_HTTP_PAT.search(error_blob):
        return False
    return stop_reason in (STOP_GATEWAY, STOP_RATELIMIT)


_DENY_REFS_MARKS: Tuple[str, ...] = ("refcraft_attachments", "screenshot_")
_DENY_ANSWER_MARKS: Tuple[str, ...] = (
    "refspec",
    "query_sets",
    "checklists",
    "keypoints",
    ".claude",
    "REFCRAFT_MODEL_API_KEY",
)


def classify_denied(denied: Iterable[dict]) -> Dict[str, int]:
    out = {"refs": 0, "answer": 0, "other": 0}
    for d in denied or ():
        txt = str(d.get("input") or "").lower()
        if any((m in txt for m in _DENY_ANSWER_MARKS)):
            out["answer"] += 1
        elif any((m in txt for m in _DENY_REFS_MARKS)):
            out["refs"] += 1
        else:
            out["other"] += 1
    return out


IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


def _sha256_file(path: Path, cap: int = 64 << 20) -> str:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
            if n >= cap:
                break
    return h.hexdigest()


def audit_run(
    *,
    workdir: Path,
    whitelist: Iterable[str],
    tally: RunTally,
    artifact_name: str = "index.html",
    kind: str = "opencode",
    ref_hashes: Iterable[str] = (),
    refs_in_workdir: bool = False,
) -> dict:
    wl = {os.path.basename(str(x)) for x in whitelist or []}
    wl.add(artifact_name)
    refh = {str(x) for x in ref_hashes or [] if x}
    extra: List[str] = []
    derived: List[str] = []
    wd = Path(workdir)
    if wd.exists():
        for p in sorted(wd.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(wd))
            if rel.startswith("shots/") or rel.startswith(".opencode/"):
                continue
            base = p.name
            low = base.lower()
            if "refspec" in low or low in ("meta.json", "checklist.json"):
                extra.append(rel)
                continue
            if low.endswith(IMG_EXTS):
                try:
                    is_ref_bytes = bool(refh) and _sha256_file(p) in refh
                except OSError:
                    is_ref_bytes = False
                if is_ref_bytes and (not refs_in_workdir):
                    extra.append(rel)
                elif is_ref_bytes or base in wl:
                    pass
                else:
                    derived.append(rel)
                continue
            if base in wl or base in ("shot.py", artifact_name):
                continue
    hits: List[str] = []
    hay = " \n".join(
        tally.texts
        + [json.dumps(tally.tool_calls, ensure_ascii=False)]
        + [d.get("input", "") for d in tally.denied]
    )
    for w in _STREAM_TAINT_WORDS:
        if w.lower() in hay.lower():
            hits.append(w)
    why: List[str] = []
    if extra:
        why.append(f"工作目录里多了 {len(extra)} 个不该有的文件: {extra[:5]}")
    kinds = classify_denied(tally.denied) if tally.denied else {"refs": 0, "answer": 0, "other": 0}
    if tally.denied:
        why.append(
            f"{len(tally.denied)} 次调用被 deny 拦下（tool={sorted({d['tool'] for d in tally.denied})}；摸参考图 {kinds['refs']} 次、摸答案键 {kinds['answer']} 次、说不清 {kinds['other']} 次）"
        )
    if hits:
        why.append(f"事件流里出现答案键相关的词: {sorted(set(hits))}")
    severity = "contaminated" if extra else "attempted" if kinds["answer"] or hits else ""
    return {
        "tainted": bool(extra),
        "taint_severity": severity,
        "taint_why": "；".join(why),
        "taint_extra_files": extra[:20],
        "n_derived_images": len(derived),
        "derived_images": derived[:20],
        "n_denied_refs": kinds["refs"],
        "n_denied_answer": kinds["answer"],
        "n_denied_other": kinds["other"],
        "audit_kind": f"{kind}/workdir+stream+refhash",
    }


def build_argv(
    *,
    bin_path: str,
    workdir: Path,
    provider_id: str,
    wire: str,
    image_paths: Iterable[str] = (),
    thinking: bool = True,
    log_level: str = "WARN",
) -> List[str]:
    argv = [
        bin_path,
        "run",
        "--format",
        "json",
        "--auto",
        "--pure",
        "--dir",
        str(workdir),
        "-m",
        f"{provider_id}/{wire}",
        "--log-level",
        log_level,
        "--print-logs",
    ]
    if thinking:
        argv.append("--thinking")
    for p in image_paths:
        argv += ["-f", str(p)]
    return argv


def _reader(stream, q: "queue.Queue") -> None:
    try:
        for line in stream:
            q.put(line)
    except Exception as e:
        q.put(f"\x00reader-error {type(e).__name__}: {e}")
    finally:
        q.put(None)


def run_opencode(
    *,
    argv: List[str],
    env: Dict[str, str],
    prompt: str,
    cwd: Path,
    caps: dict,
    stdout_path: Path,
    stderr_path: Path,
    on_tick=None,
) -> dict:
    global _ACTIVE_OC_PGID
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    max_turns = int(caps.get("max_turns") or 50)
    wall = float(caps.get("wall_clock_sec") or 7200)
    grace = float(caps.get("stall_grace_sec") or 240)
    max_credit = float(caps.get("max_stall_credit_sec", 1800))
    budget = float(caps.get("max_budget_usd") or 0.0)
    tally = RunTally()
    t0 = time.time()
    credit = 0.0
    last_evt = t0
    killed_by = ""
    with open(stderr_path, "w", encoding="utf-8") as ferr, open(
        stdout_path, "w", encoding="utf-8"
    ) as fout:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=ferr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        _ACTIVE_OC_PGID = proc.pid
        try:
            proc.stdin.write(prompt)
            proc.stdin.flush()
        except Exception as e:
            tally.errors.append({"name": "StdinWriteError", "message": str(e)[:400]})
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        q: "queue.Queue" = queue.Queue()
        th = threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True)
        th.start()

        def _kill(reason: str) -> None:
            nonlocal killed_by
            if killed_by:
                return
            killed_by = reason
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

        done = False
        while not done:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                line = ""
            now = time.time()
            if line is None:
                done = proc.poll() is not None
            elif line:
                if line.startswith("\x00reader-error"):
                    tally.errors.append({"name": "StdoutReadError", "message": line[1:][:400]})
                else:
                    fout.write(line if line.endswith("\n") else line + "\n")
                    fout.flush()
                    tally.n_lines += 1
                    evt = parse_event(line)
                    if evt is not None:
                        tally.feed(evt)
                    gap = now - last_evt
                    if gap > grace:
                        credit = min(max_credit, credit + (gap - grace))
                    last_evt = now
            open_gap = now - last_evt
            live_credit = min(max_credit, credit + max(0.0, open_gap - grace))
            spent = now - t0 - live_credit
            if not killed_by:
                if tally.turns >= max_turns:
                    _kill(STOP_MAX_TURNS)
                elif budget > 0 and tally.cost >= budget:
                    _kill(STOP_BUDGET)
                elif spent >= wall:
                    _kill(STOP_WALL)
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(tally, spent)
            if proc.poll() is not None and q.empty() and (not th.is_alive()):
                done = True
        with contextlib.suppress(Exception):
            proc.wait(timeout=30)
        th.join(timeout=5)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    proc.stdout.close()
    rc = proc.returncode if proc.returncode is not None else -1
    _ACTIVE_OC_PGID = 0
    now = time.time()
    credit = min(max_credit, credit + max(0.0, now - last_evt - grace))
    return {
        "tally": tally,
        "returncode": rc,
        "killed_by": killed_by,
        "wall_seconds": round(now - t0, 1),
        "stall_credit_sec": round(credit, 1),
        "billed_seconds": round(now - t0 - credit, 1),
    }
