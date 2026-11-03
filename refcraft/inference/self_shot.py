"""Local screenshot helper and host renderer, independent of any platform."""

from __future__ import annotations
from typing import Dict, List, Optional
from pathlib import Path
import uuid
import time
import threading
import subprocess
import os
import fcntl
import contextlib
import json
import sys
import tempfile
import logging

logger = logging.getLogger(__name__)
VENV_PY = sys.executable
SHOTS_DIRNAME = "shots"
HELPER_NAME = "shot.py"
DEFAULT_WIDTHS = (1920, 768, 390)
_MIN_W, _MAX_W = (320, 2560)
_MAX_WIDTHS_PER_REQ = 4
_GLOBAL_RENDER_SLOTS = max(1, int(os.environ.get("REFCRAFT_SHOT_CONCURRENCY", "2")))
_GLOBAL_RENDER_LOCK_ROOT = Path(tempfile.gettempdir()) / f"refcraft_shot_slots_{os.getuid()}"


@contextlib.contextmanager
def _global_render_slot(timeout_sec: float):
    _GLOBAL_RENDER_LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    held = None
    while held is None and time.monotonic() < deadline:
        for idx in range(_GLOBAL_RENDER_SLOTS):
            fd = os.open(
                _GLOBAL_RENDER_LOCK_ROOT / f"slot-{idx:02d}.lock", os.O_RDWR | os.O_CREAT, 420
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = fd
                break
            except BlockingIOError:
                os.close(fd)
        if held is None:
            time.sleep(0.2)
    if held is None:
        raise TimeoutError(f"等待全局截图槽超时（{_GLOBAL_RENDER_SLOTS} slots）")
    try:
        yield
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


HELPER_SRC = '''\
#!/usr/bin/env python3
"""给你自己写的页面拍照，看看它实际长什么样。

    python3 shot.py                          # 拍 index.html，三个宽度（桌面/平板/手机）
    python3 shot.py --file index.html --widths 1920,390
    python3 shot.py --wait 3000              # 页面有动画/延迟加载时多等一会（毫秒）

拍完的图会出现在 shots/ 下，路径会打印出来 —— 用 Read 工具打开它们就能看见页面。
渲染在这台机器上代跑（这个脚本本身只发请求），一次最多等 %(timeout)d 秒。
"""
import argparse, json, os, sys, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "%(shots)s")
TIMEOUT = %(timeout)d

ap = argparse.ArgumentParser()
ap.add_argument("--file", default="%(artifact)s")
ap.add_argument("--widths", default="%(widths)s")
ap.add_argument("--wait", type=int, default=1500, help="页面加载后额外等待的毫秒数")
a = ap.parse_args()

os.makedirs(SHOTS, exist_ok=True)
rid = uuid.uuid4().hex[:12]
req = {"id": rid, "file": a.file, "wait_ms": a.wait,
       "widths": [int(x) for x in a.widths.replace(" ", "").split(",") if x]}
tmp = os.path.join(SHOTS, ".req-%%s.tmp" %% rid)
with open(tmp, "w") as f:
    json.dump(req, f)
os.rename(tmp, os.path.join(SHOTS, ".req-%%s.json" %% rid))   # 原子出现，别让服务端读到半个

res_path = os.path.join(SHOTS, ".res-%%s.json" %% rid)
t0 = time.time()
while time.time() - t0 < TIMEOUT + 20:
    if os.path.exists(res_path):
        with open(res_path) as f:
            res = json.load(f)
        if res.get("ok"):
            print("截图完成：")
            for p in res.get("shots", []):
                print("  " + p)
            d = res.get("diagnostics") or {}
            print("页面高度 %%s px；控制台报错 %%s 条；请求外部资源 %%s 次"
                  %% (d.get("page_height"), d.get("console_errors"), d.get("external_requests")))
            sys.exit(0)
        print("截图失败：" + str(res.get("error")), file=sys.stderr)
        sys.exit(1)
    time.sleep(0.4)
print("截图超时（%%ds 没等到结果）" %% (TIMEOUT + 20), file=sys.stderr)
sys.exit(1)
'''
RENDERER_SRC = r"""
import json, sys
from playwright.sync_api import sync_playwright

req = json.load(open(sys.argv[1]))
out = {"shots": [], "diagnostics": {}}
console_errors = 0
external = 0

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True,
                                args=["--no-sandbox", "--disable-gpu",
                                      "--disable-software-rasterizer"])
    try:
        for w in req["widths"]:
            ctx = browser.new_context(viewport={"width": int(w), "height": 1080},
                                      java_script_enabled=True)
            ctx.set_default_timeout(60000)
            page = ctx.new_page()
            # 跟 step3a 的正式渲染同口径：只挡 Google Fonts
            for pat in ("*fonts.googleapis.com*", "*fonts.gstatic.com*"):
                page.route(pat, lambda route: route.abort())

            def _console(msg):
                global console_errors
                if msg.type == "error":
                    console_errors += 1

            def _request(r):
                global external
                if r.url.startswith(("http://", "https://")):
                    external += 1

            page.on("console", _console)
            page.on("request", _request)
            try:
                page.goto(req["url"], wait_until="load", timeout=60000)
            except Exception:
                page.goto(req["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(int(req.get("wait_ms") or 1500))
            h = page.evaluate("document.documentElement.scrollHeight")
            dst = req["out_prefix"] + "_%d.jpg" % int(w)
            page.screenshot(path=dst, full_page=True, type="jpeg", quality=82)
            out["shots"].append(dst)
            out["diagnostics"]["page_height"] = h
            ctx.close()
    finally:
        browser.close()

out["diagnostics"]["console_errors"] = console_errors
out["diagnostics"]["external_requests"] = external
print(json.dumps(out))
"""


def install_helper(
    workdir: Path,
    *,
    artifact_name: str = "index.html",
    widths=DEFAULT_WIDTHS,
    timeout_sec: int = 180,
) -> Path:
    workdir = Path(workdir)
    (workdir / SHOTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    src = HELPER_SRC % {
        "shots": SHOTS_DIRNAME,
        "artifact": artifact_name,
        "widths": ",".join((str(int(w)) for w in widths)),
        "timeout": int(timeout_sec),
    }
    p = workdir / HELPER_NAME
    p.write_text(src, encoding="utf-8")
    os.chmod(p, 493)
    return p


class ShotServer:

    def __init__(
        self,
        workdir: Path,
        *,
        renderer_dir: Path,
        timeout_sec: int = 180,
        max_requests: int = 40,
        poll_sec: float = 0.4,
    ):
        self.workdir = Path(workdir).resolve()
        self.shots_dir = self.workdir / SHOTS_DIRNAME
        self.timeout_sec = int(timeout_sec)
        self.max_requests = int(max_requests)
        self.poll_sec = float(poll_sec)
        self.n_served = 0
        self.n_requests = 0
        self.violations: List[dict] = []
        self.errors: List[str] = []
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self.renderer_dir = Path(renderer_dir)
        self.renderer_dir.mkdir(parents=True, exist_ok=True)
        self.renderer = self.renderer_dir / "_render_shot.py"
        self.renderer.write_text(RENDERER_SRC, encoding="utf-8")

    def _resolve(self, rel: str) -> Path:
        raw = str(rel or "").strip() or "index.html"
        cand = self.workdir / raw if not os.path.isabs(raw) else Path(raw)
        real = Path(os.path.realpath(str(cand)))
        if not str(real).startswith(str(self.workdir) + os.sep) and real != self.workdir:
            raise PermissionError(f"只能渲染工作目录内的文件，拒绝: {raw}")
        if real.suffix.lower() not in (".html", ".htm"):
            raise PermissionError(f"只能渲染 .html/.htm，拒绝: {raw}")
        if not real.is_file():
            raise FileNotFoundError(f"文件不存在: {raw}")
        return real

    @staticmethod
    def _widths(raw) -> List[int]:
        ws = []
        for x in (raw or list(DEFAULT_WIDTHS))[:_MAX_WIDTHS_PER_REQ]:
            try:
                w = int(x)
            except (TypeError, ValueError):
                continue
            if _MIN_W <= w <= _MAX_W:
                ws.append(w)
        return ws or list(DEFAULT_WIDTHS)

    def _serve(self, req_path: Path) -> None:
        try:
            req = json.loads(req_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.errors.append(f"请求文件读不动 {req_path.name}: {e}")
            req_path.unlink(missing_ok=True)
            return
        rid = str(req.get("id") or uuid.uuid4().hex[:12])
        if not rid.isalnum() or len(rid) > 64:
            req_path.unlink(missing_ok=True)
            self.violations.append({"why": "invalid request id"})
            return
        res_path = self.shots_dir / f".res-{rid}.json"
        req_path.unlink(missing_ok=True)
        self.n_requests += 1

        def fail(msg: str, violation: bool = False) -> None:
            if violation:
                self.violations.append({"id": rid, "file": str(req.get("file")), "why": msg})
            res_path.write_text(
                json.dumps({"ok": False, "error": msg}, ensure_ascii=False), encoding="utf-8"
            )

        if self.n_requests > self.max_requests:
            return fail(f"这次运行的截图次数已用满（上限 {self.max_requests} 次）")
        try:
            target = self._resolve(req.get("file"))
        except PermissionError as e:
            return fail(str(e), violation=True)
        except Exception as e:
            return fail(str(e))
        prefix = self.shots_dir / f"shot_{self.n_served + 1:02d}"
        payload = {
            "url": f"file://{target}",
            "widths": self._widths(req.get("widths")),
            "wait_ms": min(int(req.get("wait_ms") or 1500), 10000),
            "out_prefix": str(prefix),
        }
        job = self.renderer_dir / f"_job-{rid}.json"
        job.write_text(json.dumps(payload), encoding="utf-8")
        started = time.monotonic()
        try:
            with _global_render_slot(max(5, self.timeout_sec * 0.6)):
                remaining = max(5.0, self.timeout_sec - (time.monotonic() - started))
                r = subprocess.run(
                    [VENV_PY, str(self.renderer), str(job)],
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                )
        except (subprocess.TimeoutExpired, TimeoutError):
            return fail(f"渲染/排队超时（{self.timeout_sec}s）")
        finally:
            job.unlink(missing_ok=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-400:]
            self.errors.append(f"渲染失败 rc={r.returncode}: {tail}")
            return fail(f"渲染失败: {tail[-200:]}")
        try:
            out = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception as e:
            return fail(f"渲染器输出读不动: {e}")
        self.n_served += 1
        rel = [os.path.relpath(p, str(self.workdir)) for p in out.get("shots", [])]
        res_path.write_text(
            json.dumps(
                {"ok": True, "shots": rel, "diagnostics": out.get("diagnostics") or {}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.shots_dir.is_dir():
                    for p in sorted(self.shots_dir.glob(".req-*.json")):
                        self._serve(p)
            except Exception as e:
                self.errors.append(f"轮询本身抛了 {type(e).__name__}: {e}")
            self._stop.wait(self.poll_sec)

    def start(self) -> "ShotServer":
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=5)

    def report(self) -> Dict[str, object]:
        return {
            "n_self_shots": self.n_served,
            "n_shot_requests": self.n_requests,
            "shot_violations": self.violations[:10],
            "n_shot_violations": len(self.violations),
            "shot_errors": self.errors[:5],
        }
