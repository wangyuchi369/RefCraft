"""HTML 渲染:把一份产物 HTML 抓成一组主视口帧 + 一份帧清单。

**只有一条抓帧路径**(`capture_frames`),语义见 docs/设计总纲.md §3.5「帧怎么跟参考帧对上」:

  - 主视口 1920×1080,**跟参考帧同宽同高**。不同宽的两张图放在一起判"像不像",
    量到的是断点选择而不是跟随;响应式由 step3b 的窄屏帧另考。
  - 首屏一张,之后**事件驱动**:每个状态动作(hover/click/type/…)之后一张,标
    kind="interaction";重放结束后按页面**实际高度**做一次纵向覆盖扫描,每落一个
    新位置一张,标 kind="scroll"。只有这两种 kind。
  - 逐帧写进 `frames.json`:kind / scroll_y / time_ms / 对应哪几个评分条目 /
    相对上一帧的原始像素差。judge 的图序说明照这份清单生成,所以"这张帧拍的是什么"
    是写下来的事实,不是靠帧号猜的约定。

**旧口径已删除**:原来是"动作的标称耗时之和 = 一条虚拟时间轴,N 张帧均匀落在轴上",
配一个 `_action_ms` 耗时表和 `align_for_frames` 那套灌 wait 的对齐补偿。删掉的理由是
逐帧号对齐在数据上从来没成立过(参考侧 500 样本 5877 帧、均 11.75 张、2..15 不等,
模型侧上限 10),而它的代价是真实的:所有帧都可能落在动作**之间**,于是评分表要的
hover 态一张也没拍到。

阈值(帧算不算"新状态"、覆盖够不够)全部来自 `utils/render/frame_diff.py`,和参考侧抓取器
共用一份 —— 两边各写一份的下场是同一个变化在参考侧留了帧、在产物侧被判成"没变化"。

**别把 time_ms 写进任何指纹。** 它是墙钟,每次跑都不一样;能力探针那边踩过这个坑
(指纹含 time_ms → 22% 的重渲染必判过期)。跳过与否只认 `plan_fingerprint`。
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import List, Optional, Tuple

from . import frame_diff as FD
from ..core.log import get_logger

logger = get_logger(__name__)


# JS: 去除 preloader, 显示 scroll 动画元素, 冻结 CSS 动画
PHASE1_REVEAL_JS = """
() => {
    const actions = [];
    const preloaderSelectors = [
        '.preloader', '.loader', '.loading', '.page-loader',
        '#preloader', '#loader', '#loading', '.spinner-wrapper',
        '.loading-screen', '.load-screen', '.page-loading'
    ];
    let removedPreloader = false;
    preloaderSelectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
            el.style.setProperty('display', 'none', 'important');
            removedPreloader = true;
        });
    });
    document.body.classList.add('loaded', 'page-loaded');
    if (removedPreloader) actions.push('Removed preloader/loading overlay');

    let revealCount = 0;
    document.querySelectorAll('*').forEach(el => {
        const style = getComputedStyle(el);
        if (style.opacity === '0' && el.offsetHeight > 0) {
            el.style.setProperty('opacity', '1', 'important');
            el.style.setProperty('transform', 'none', 'important');
            revealCount++;
        }
    });
    if (revealCount > 0) actions.push(`Revealed ${revealCount} hidden elements`);

    const revealClasses = ['reveal', 'fade-in', 'fade-in-up', 'fade-in-down',
        'slide-up', 'slide-in', 'slide-in-left', 'slide-in-right',
        'animate-on-scroll', 'scroll-animate', 'wow',
        'not-visible', 'is-hidden'];
    revealClasses.forEach(cls => {
        document.querySelectorAll('.' + cls).forEach(el => {
            el.classList.add('visible', 'active', 'show', 'animated',
                             'aos-animate', 'in-view', 'is-visible');
            el.style.setProperty('opacity', '1', 'important');
            el.style.setProperty('transform', 'none', 'important');
            el.style.setProperty('visibility', 'visible', 'important');
        });
    });

    if (typeof AOS !== 'undefined') {
        document.querySelectorAll('[data-aos]').forEach(el => {
            el.classList.add('aos-animate');
            el.style.setProperty('opacity', '1', 'important');
            el.style.setProperty('transform', 'none', 'important');
        });
    }

    return actions;
}
"""

# 外部字体 URL 模式
_FONT_BLOCK_PATTERNS = [
    "*fonts.googleapis.com*",
    "*fonts.gstatic.com*",
]


def _safe_evaluate(page, expression, arg=None):
    try:
        if arg is None:
            return page.evaluate(expression)
        return page.evaluate(expression, arg)
    except Exception:
        return None


def _kill_child_chromium() -> None:
    import subprocess
    pid = os.getpid()
    try:
        subprocess.run(["pkill", "-9", "-P", str(pid), "-f", "chrom"],
                       capture_output=True, timeout=5)
    except Exception:
        pass


# 有的页面把 body 设成 overflow:hidden，真正的滚动条在某个内层容器上，直接
# window.scrollBy 一动不动 —— 那样整条轨迹的帧全长一个样。先试窗口，没动就找
# 页面里最大的可滚动容器，再退一步强开 overflow。
_JS_SCROLL = """(d) => {
  const y = () => window.scrollY || document.documentElement.scrollTop || 0;
  const before = y();
  window.scrollBy(0, d);
  if (Math.abs(y() - before) >= 2) return "window";
  let best = null, bs = 0;
  for (const el of document.querySelectorAll("body *")) {
    const s = el.scrollHeight - el.clientHeight;
    if (s > bs && el.clientHeight > 200) {
      const ov = getComputedStyle(el).overflowY;
      if (ov === "auto" || ov === "scroll" || ov === "overlay") { bs = s; best = el; }
    }
  }
  if (best) { const b = best.scrollTop; best.scrollTop += d;
              if (Math.abs(best.scrollTop - b) >= 2) return "inner"; }
  document.documentElement.style.overflow = "visible";
  document.body.style.overflow = "visible";
  window.scrollBy(0, d);
  return Math.abs(y() - before) >= 2 ? "forced" : "stuck";
}"""


# 绝对位置滚动（scroll_to_top / scroll_to_bottom 用）。和 _JS_SCROLL 一样要兼顾
# "body overflow:hidden、真滚动条在内层容器"的页面，否则滚不动、帧全一样。
_JS_SCROLL_TO = """(y) => {
  const target = Math.min(y, 1e9);
  const doc = document.documentElement;
  const before = window.scrollY || doc.scrollTop || 0;
  window.scrollTo(0, target);
  if (Math.abs((window.scrollY || doc.scrollTop || 0) - before) >= 2
      || (target === 0 && before === 0)) return "window";
  let best = null, bs = 0;
  for (const el of document.querySelectorAll("body *")) {
    const s = el.scrollHeight - el.clientHeight;
    if (s > bs && el.clientHeight > 200) {
      const ov = getComputedStyle(el).overflowY;
      if (ov === "auto" || ov === "scroll" || ov === "overlay") { bs = s; best = el; }
    }
  }
  if (best) { best.scrollTop = target; return "inner"; }
  doc.style.overflow = "visible";
  document.body.style.overflow = "visible";
  window.scrollTo(0, target);
  return "forced";
}"""


def _normalize_actions(actions) -> List[dict]:
    """把 action 规格归一成 list[dict]。

    actions.yaml 写出来是 {"actions": [...]}，而执行循环要裸 list。直接遍历 dict
    会拿到 key 字符串，报 "'str' object has no attribute 'get'"。两种形状都接。
    """
    if actions is None:
        return []
    if isinstance(actions, dict):
        actions = actions.get("actions") or actions.get("steps") or []
    if not isinstance(actions, list):
        return []
    return [a for a in actions if isinstance(a, dict)]


def _apply_action(page, a: dict, _depth: int = 0) -> None:
    t = a.get("type")
    try:
        if t == "hover":
            page.hover(a["selector"], timeout=1500)
        elif t == "scroll":
            amt = int(a.get("amount", 400))
            if a.get("direction") == "up":
                amt = -amt
            _safe_evaluate(page, _JS_SCROLL, amt)
        elif t == "scroll_to_bottom":
            # 一次 scrollTo 到底对懒加载页面不够（滚到底会再撑长），滚两次
            for _ in range(2):
                _safe_evaluate(page, _JS_SCROLL_TO, 1 << 30)
                page.wait_for_timeout(150)
        elif t == "scroll_to_top":
            _safe_evaluate(page, _JS_SCROLL_TO, 0)
        elif t == "type":
            page.fill(a["selector"], a.get("text", ""), timeout=1500)
        elif t == "click":
            page.click(a["selector"], timeout=1500)
        elif t == "press_key":
            for _ in range(int(a.get("count", 1))):
                page.keyboard.press(a["key"])
        elif t == "hold_key":
            key = a.get("key")
            if key:
                page.keyboard.down(key)
                page.wait_for_timeout(int(a.get("duration_ms", 400)))
                page.keyboard.up(key)
        elif t == "mouse_move":
            page.mouse.move(float(a.get("x", 0)), float(a.get("y", 0)))
        elif t == "drag":
            if a.get("source") and a.get("target"):
                page.drag_and_drop(a["source"], a["target"], timeout=2000)
            else:
                page.mouse.move(float(a.get("from_x", 0)), float(a.get("from_y", 0)))
                page.mouse.down()
                page.mouse.move(float(a.get("to_x", 0)), float(a.get("to_y", 0)), steps=10)
                page.mouse.up()
        elif t == "execute_js":
            script = a.get("script") or a.get("js") or a.get("code")
            if script:
                _safe_evaluate(page, script)
        elif t in ("repeat", "sequence") and _depth < 3:
            inner = [x for x in (a.get("actions") or a.get("steps") or [])
                     if isinstance(x, dict)]
            for _ in range(int(a.get("count", 1)) if t == "repeat" else 1):
                for x in inner:
                    _apply_action(page, x, _depth + 1)
    except Exception:
        pass  # 选择器失配不该中断整条轨迹：模型生成的页面结构本来就不保证匹配


# ── 抓帧 ──────────────────────────────────────────────────────────────────────

# 执行器真正实现了的动作类型。计划层(utils/render/action_plan.PLAN_ACTION_TYPES)只许排
# 这里面的,多排的会被 _apply_action 当 no-op 静默吞掉 —— 那就成了"计划里写着悬停、
# 实际什么都没做",而判分侧看不出区别。tools/qa/check_action_plan.py 钉这两份词表。
EXECUTOR_ACTION_TYPES = {
    "hover", "click", "type", "press_key", "hold_key", "mouse_move", "drag",
    "execute_js", "scroll", "scroll_to_top", "scroll_to_bottom",
    "wait", "repeat", "sequence",
}

# 会改变页面状态的动作:这几种之后必须抓一帧(它们是交互类条目唯一的证据)。
# 和参考侧抓取器(tools/step0/recapture_reference.py)的同名集合必须一字不差。
STATE_ACTIONS = {"hover", "click", "type", "press_key", "execute_js",
                 "drag", "hold_key", "mouse_move"}
SCROLL_ACTIONS = {"scroll", "scroll_to_top", "scroll_to_bottom"}

# 长 wait 本身就是一个叙事节拍:纯 CSS/JS 动画页面整份 actions.yaml 只有 wait,
# 三段动画全写在 description 里。不在长 wait 之后抓,这类页面只剩一张首屏。
WAIT_SHOOT_MS = 1500
STATE_SETTLE_MS = 280        # 状态动作后面没跟 wait 时,至少等这么久让过渡走完
SCROLL_SETTLE_MS = 450       # 滚动之后等这么久(懒加载/视差要落位)
MAX_WAIT_MS = 4000           # 单个 wait 最多真等这么久,防一条 wait 60s 卡死整批
# 抓帧口径版本。改了它 = 老帧作废(见 §3.5)。
# 2 → 3:扫描帧的 note 原来写的是**请求的**那个 y("补覆盖 scrollY=960"),而页面可能滚不到
# 那儿(实测有一份停在 1125)。那句话会跟着 frames.json 进 judge 的图序说明,让它看着
# 一张 y=1125 的图读"scrollY=960" —— 说明是假的比没有说明更糟,所以按 y 记的老帧要重拍。
CAPTURE_VERSION = 3


def _page_height(page, viewport_height: int) -> int:
    """页面实际高度。取几个来源的最大值:懒加载页面 body.scrollHeight 会偏小。"""
    h = _safe_evaluate(page, """() => {
        const d = document.documentElement, b = document.body;
        return Math.max(d.scrollHeight||0, d.offsetHeight||0,
                        b ? b.scrollHeight||0 : 0, b ? b.offsetHeight||0 : 0);
    }""")
    if not isinstance(h, (int, float)) or h <= 0:
        return int(viewport_height)
    return int(min(max(int(h), int(viewport_height)), 30000))


def _scroll_y(page) -> int:
    y = _safe_evaluate(page, "() => window.scrollY || document.documentElement.scrollTop || 0")
    return int(y) if isinstance(y, (int, float)) else 0


def capture_frames(
    html_code: str,
    output_dir: str,
    actions=None,
    max_frames: int = 10,
    viewport_width: int = FD.VW,
    viewport_height: int = FD.VH,
    quality: int = 80,
    wait_ms: int = 2000,
    timeout_ms: int = 60000,
    budget_ms: int = 180000,
    block_fonts: bool = True,
) -> Tuple[bool, Optional[str], dict]:
    """事件驱动抓帧。返回 (success, error, manifest)。

    manifest 就是 `frames.json` 的内容:逐帧的 kind/scroll_y/time_ms/对应条目/像素差,
    加上页面高度、纵向覆盖、以及**帧账**(拍了几张、留了几张、为什么丢的、
    计划里哪几步没拍到)。**只记数,不判对错** —— 判定是下游纯函数(§4.2)。

    `actions` 可以为空:那就只剩首屏 + 纵向扫描,这是"这条题没有状态条目"的正常形态,
    不是失败,也不需要另一条 fallback 代码路径。

    整页打不开/超时才是失败(success=False);单个动作找不到落点由计划层记账,
    这里照旧往下走 —— 那是"记不过",不是"测不了"(§3.5 第二条护栏)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright 未安装", {}

    acts = _normalize_actions(actions)
    os.makedirs(output_dir, exist_ok=True)
    # 抓帧是权威的:每次都重写完整一套。留着上次的残帧会让编号不连续,
    # 而下游 loader 按连续编号读图(踩过一次:编号断档 = 静默丢图)。
    for f in os.listdir(output_dir):
        if (f.startswith("frame_") or f.startswith("_shot_")) and f.endswith(".jpg"):
            try:
                os.remove(os.path.join(output_dir, f))
            except OSError:
                pass

    shots: List[dict] = []          # 按拍摄顺序:{path, kind, scroll_y, time_ms, note, step_id, item_ids, thumb, delta_*}
    tmp_html = None
    t0 = time.time()

    def _elapsed_ms() -> float:
        return (time.time() - t0) * 1000.0

    try:
        tmp_html = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        )
        tmp_html.write(html_code)
        tmp_html.close()
        file_url = f"file://{os.path.abspath(tmp_html.name)}"

        truncated = False

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-software-rasterizer"],
            )
            context = browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
                java_script_enabled=True,
            )
            context.set_default_timeout(timeout_ms)
            page = context.new_page()

            if block_fonts:
                for pattern in _FONT_BLOCK_PATTERNS:
                    page.route(pattern, lambda route: route.abort())

            try:
                page.goto(file_url, wait_until="load", timeout=timeout_ms)
                page.wait_for_timeout(wait_ms)
            except Exception:
                try:
                    page.goto(file_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

            _safe_evaluate(page, PHASE1_REVEAL_JS)

            def shoot(kind: str, note: str = "", step: Optional[dict] = None) -> None:
                """拍一张,连同它相对上一帧的原始像素差一起记账。"""
                idx = len(shots)
                path = os.path.join(output_dir, f"_shot_{idx:03d}.jpg")
                try:
                    buf = page.screenshot(type="jpeg", quality=quality,
                                          animations="disabled")
                except Exception as e:
                    logger.debug("screenshot 失败(%s),跳过这一帧", str(e)[:80])
                    return
                try:
                    with open(path, "wb") as fh:
                        fh.write(buf)
                    th = FD.thumb(buf)
                except Exception as e:
                    logger.debug("落盘/缩略图失败(%s)", str(e)[:80])
                    return
                rec = {"path": path, "kind": kind, "scroll_y": _scroll_y(page),
                       "time_ms": round(_elapsed_ms(), 1), "note": note,
                       "step_id": (step or {}).get("step_id"),
                       "item_ids": list((step or {}).get("item_ids") or []),
                       "_thumb": th}
                rec.update(FD.frame_delta(th, shots[-1]["_thumb"]) if shots
                           else {"delta_mean": None, "delta_max_block": None})
                shots.append(rec)

            # 首屏:整页看的基准帧,永远保留
            shoot("scroll", "初始首屏")

            # ── 重放动作 ──────────────────────────────────────────────────
            # 状态动作后面紧跟的那条 wait 是计划层排的"过渡停顿"(state_hold_ms),
            # 要把它**当成这一步的等待时间用掉**再拍,而不是拍完再等、或者拍两张:
            # 拍两张会让"计划几步 / 拍到几步"的账对不上,先拍则拍到的是过渡中间态。
            prev_kind = "scroll"
            i = 0
            while i < len(acts):
                if _elapsed_ms() > budget_ms:
                    truncated = True
                    logger.warning("抓帧超预算 %.0fms,剩下 %d 个动作不再重放",
                                   budget_ms, len(acts) - i)
                    break
                a = acts[i]
                t = a.get("type")
                i += 1
                if t == "wait":
                    ms = int(a.get("ms", 500) or 0)
                    page.wait_for_timeout(min(ms, MAX_WAIT_MS))
                    if ms >= WAIT_SHOOT_MS:
                        # 沿用上一帧的 kind:一条独立的长 wait 只是"等动画落位",
                        # 它没有对应的状态动作,标成 interaction 会让它既躲过去重
                        # 又在帧账里冒出一个没有条目归属的交互帧。
                        shoot(prev_kind, a.get("description") or f"等待 {ms}ms 之后")
                    continue
                if t in SCROLL_ACTIONS:
                    _apply_action(page, a)
                    page.wait_for_timeout(SCROLL_SETTLE_MS)
                    shoot("scroll", a.get("description") or f"{t} 之后", a)
                    prev_kind = "scroll"
                    continue
                if t in STATE_ACTIONS:
                    _apply_action(page, a)
                    settle = STATE_SETTLE_MS
                    if i < len(acts) and acts[i].get("type") == "wait":
                        settle = max(settle, min(int(acts[i].get("ms", 0) or 0), MAX_WAIT_MS))
                        i += 1                      # 这条 wait 被这一步用掉了
                    page.wait_for_timeout(settle)
                    shoot("interaction", a.get("description") or f"{t} 之后", a)
                    prev_kind = "interaction"
                    continue
                # repeat / sequence 之类:执行,但不单独占帧位(内部动作没有条目归属)
                _apply_action(page, a)
                page.wait_for_timeout(120)

            # ── 纵向覆盖扫描 ──────────────────────────────────────────────
            page_height = _page_height(page, viewport_height)
            covered = [s["scroll_y"] for s in shots]
            for ty in FD.scroll_targets(page_height, viewport_height):
                if _elapsed_ms() > budget_ms:
                    truncated = True
                    break
                gain = (FD.cov_len(covered + [ty], page_height, viewport_height)
                        - FD.cov_len(covered, page_height, viewport_height))
                if gain < FD.MIN_COV_GAIN:
                    continue        # 这一屏已经被别的帧盖住了,重复位置不带信息
                _safe_evaluate(page, _JS_SCROLL_TO, ty)
                page.wait_for_timeout(SCROLL_SETTLE_MS)
                # note 里**不写请求的那个 y**:页面可能滚不到那儿(scroll-snap、
                # 平滑滚动没停、动作之后页高变了),实测到的位置在 rec["scroll_y"] 里。
                # 两个数都进 frames.json、都进 judge 的图序说明,写一个没兑现的目标值
                # 等于让 judge 看着"y=1125"的图读"scrollY=960"的说明。
                shoot("scroll", "纵向覆盖扫描")
                covered.append(shots[-1]["scroll_y"])

            page.close()
            context.close()
            browser.close()

    except Exception as e:
        _kill_child_chromium()
        return False, f"渲染失败: {str(e)[:300]}", {}
    finally:
        if tmp_html and os.path.exists(tmp_html.name):
            try:
                os.remove(tmp_html.name)
            except OSError:
                pass

    if not shots:
        return False, "截图失败:没有生成任何帧", {}

    kept, dropped = _select_frames(shots, max_frames, page_height, viewport_height)
    frames = _finalize_frames(kept, output_dir)
    for d in dropped:
        try:
            os.remove(d["path"])
        except OSError:
            pass

    # 帧账认的是"计划排了 step_id 的动作":状态步骤要落到选择器上,scroll 步骤是页级的,
    # 两种都必须能对上一张帧,否则它服务的那几个条目就是在无证据下被判分(§3.5)。
    planned = [a for a in acts if a.get("step_id")]
    shot_ids = {f.get("step_id") for f in frames if f.get("step_id")}
    # 没拍到的那几步要写出**卡在哪一段**:帧预算砍掉的和压根没拍成的,下游读账的人
    # 要能分开(前者是我们的配额问题,后者才可能是页面的问题)。
    _drop_why = {str(d.get("step_id")): d["why"] for d in dropped if d.get("step_id")}
    manifest = {
        "capture_version": CAPTURE_VERSION,
        "viewport": {"width": int(viewport_width), "height": int(viewport_height)},
        "page_height": int(page_height),
        "coverage": {
            "covered_px": FD.cov_len([f["scroll_y"] for f in frames], page_height, viewport_height),
            "page_height": int(page_height),
            "ratio": round(FD.cov_len([f["scroll_y"] for f in frames], page_height, viewport_height)
                           / max(1, page_height), 3),
        },
        "counts": {
            "captured": len(shots),
            "kept": len(frames),
            "interaction": sum(1 for f in frames if f["kind"] == "interaction"),
            "scroll": sum(1 for f in frames if f["kind"] == "scroll"),
        },
        # 帧账(§3.5「计划了几步、找到落点几步、真拍到几步」的后两个数)。
        # 落点数在 action_landing.json 里,step3 汇总时把三个数并到一起。
        "steps": {
            "in_script": len(planned),
            "shot": len(shot_ids),
            "unshot": [
                {"step_id": a.get("step_id"), "item_ids": list(a.get("item_ids") or []),
                 "why": _drop_why.get(str(a.get("step_id")),
                                      "动作在脚本里但没有对应帧(超预算或截图失败)")}
                for a in planned if a.get("step_id") not in shot_ids
            ],
        },
        "dropped": [{"kind": d["kind"], "scroll_y": d["scroll_y"],
                     "step_id": d.get("step_id"), "item_ids": d.get("item_ids") or [],
                     "why": d["why"]} for d in dropped],
        "truncated_by_deadline": bool(truncated),
        "frames": frames,
    }
    return True, None, manifest


def _select_frames(shots: List[dict], max_frames: int, page_height: int,
                   viewport_height: int) -> Tuple[List[dict], List[dict]]:
    """选帧。返回 (留下的, 丢掉的+为什么)。

    三条规矩,都在 §3.5 里定了:
      1. **首屏永远留**(整页看的基准)。
      2. **计划帧一律留,不去重**(带 `step_id` 的那些,交互帧全在里面)。
         差值≈0 的交互帧恰恰是"点了没反应"的证据,按"看起来没变"丢掉它,
         等于把失败证据删了当成没测到;计划里的 scroll 步骤同理 —— 它是为某几个
         条目排的,去重会把"这一帧服务哪几条"这个归属一起抹掉。
      3. **只对没有条目归属的滚动帧去重**(首屏、扫描补的那些):重复位置不带信息。
    帧位不够时先砍滚动帧(按覆盖贪心保留),砍到只剩一张还不够才砍交互帧 ——
    砍掉的交互帧连它对应的条目一起记进 dropped,这条题的那个条目就成了"没有证据"。
    """
    dropped: List[dict] = []
    first, rest = shots[0], shots[1:]

    inter = [s for s in rest if s["kind"] == "interaction" or s.get("step_id")]
    scrolls: List[dict] = []
    prev = first
    covered = [first["scroll_y"]]
    for s in rest:
        if s["kind"] != "scroll" or s.get("step_id"):
            continue                      # 计划帧不参与去重(见 docstring 第 2 条)
        gain = (FD.cov_len(covered + [s["scroll_y"]], page_height, viewport_height)
                - FD.cov_len(covered, page_height, viewport_height))
        if FD.is_new_scroll_frame(s["_thumb"], prev["_thumb"], gain):
            scrolls.append(s)
            covered.append(s["scroll_y"])
            prev = s
        else:
            dropped.append(dict(s, why=f"滚动帧与上一帧几乎相同(覆盖新增 {int(gain)}px)"))

    budget = max(1, int(max_frames)) - 1        # 首屏占一格
    min_scroll = 1 if page_height > viewport_height and scrolls else 0
    n_inter = min(len(inter), max(0, budget - min_scroll))
    n_scroll = max(0, budget - n_inter)

    for s in inter[n_inter:]:
        dropped.append(dict(s, why=f"帧预算不够:上限 {max_frames} 张"))
    inter = inter[:n_inter]

    pick = set(FD.pick_scroll_frames(scrolls, n_scroll, page_height, viewport_height))
    for j, s in enumerate(scrolls):
        if j not in pick:
            dropped.append(dict(s, why=f"帧预算不够:上限 {max_frames} 张(按纵向覆盖挑)"))
    scrolls = [s for j, s in enumerate(scrolls) if j in pick]

    kept = [first] + sorted(inter + scrolls, key=lambda s: s["time_ms"])
    return kept, dropped


def _finalize_frames(kept: List[dict], output_dir: str) -> List[dict]:
    """把留下的帧改名成连续的 frame_NN.jpg,并产出 frames.json 里的逐帧记录。

    编号必须连续:下游 loader 按连续编号读图,断档就是静默丢图(踩过一次)。所以编号跟着
    **已经落盘的张数**走(`len(out)`),不跟输入下标走 —— 中间有一张搬不动(磁盘满、
    临时文件被清了),按下标编号就会跳过一个号,loader 正好在那个缺口处停住,
    后面的帧连同它们对应的条目一起静默消失。tools/qa/check_frame_capture.py 钉这条。
    """
    out: List[dict] = []
    for s in kept:
        k = len(out)
        name = f"frame_{k:02d}.jpg"
        dst = os.path.join(output_dir, name)
        try:
            os.replace(s["path"], dst)
        except OSError as e:
            logger.warning(f"帧 {s.get('step_id') or s['kind']}@{s['scroll_y']} 搬不动,"
                           f"这一帧丢了:{e}")
            continue
        out.append({"name": name, "index": k, "kind": s["kind"],
                    "scroll_y": s["scroll_y"], "time_ms": s["time_ms"],
                    "note": s["note"], "step_id": s.get("step_id"),
                    "item_ids": list(s.get("item_ids") or []),
                    "delta_mean": s.get("delta_mean"),
                    "delta_max_block": s.get("delta_max_block")})
    return out
