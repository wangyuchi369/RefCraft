# -*- coding: utf-8 -*-
"""确定性色彩签名与距离（HSV 直方图）。

⚠️ 算法与 `tools/step0/plan_mix.py::palette_sig` / `palette_dist` **必须逐字一致** ——
配对阶段用它筛"两份参考的配色分得清"，判分阶段用它判"产出的配色到底跟了哪一份"，
两边用不同的度量，配对时说分得清的题在判分时就分不清了。

为什么不直接 `from tools.step0.plan_mix import palette_sig`：plan_mix 在 import 期就会
拉起 step0_gen_user_query（读 prompts/query_recipe/*.yaml、建配方表），在 50 个渲染
worker 里各来一遍纯属浪费，而且出题侧还在改，判分侧不该被它的 import 链绑住。
所以这里放一份独立实现，并用 tools/qa/check_palette_sync.py 钉住两边一致。

用法：
    sig  = palette_sig(["frame_00.jpg", ...])          # 取前 SIG_FRAMES 帧
    dist = palette_dist(sig_a, sig_b)                  # ∈[0,1]，越大越"配色不同"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

SIG_FRAMES = 3          # 每份取前 3 帧（首屏最能代表底色）
PALETTE_DIST_MIN = 0.50  # 配对阶段"配色分得清"的阈值，判分侧沿用同一把尺


def palette_sig(paths: List[str], n_frames: int = SIG_FRAMES) -> Optional[Dict[str, Any]]:
    """一份参考/产出的配色签名：底色明度（中位 V）+ 12 档色相质量 + 平均彩度 + 灰占比。

    ⚠️ 逐字复制 tools/step0/plan_mix.py::palette_sig，改这里必须同步改那里
    （tools/qa/check_palette_sync.py 会核对两边在同一批图上给出完全相同的签名）。

    只用 HSV 直方图，不做聚类 —— 目的不是"描述配色"，而是给出一个**可复现的距离**，
    能把"米黄底红棕字"和"深蓝底青字"分开就够了。取中位 V 而不是均值：长网页里
    大片背景决定中位数，均值会被一张大图带跑。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:      # 没有 numpy/PIL 的环境（如纯判分节点）：签名相关探针记 blocked
        return None

    hues = np.zeros(12, dtype=np.float64)
    vs, ss, grays = [], [], []
    used = 0
    for p in (paths or [])[:n_frames]:
        try:
            im = Image.open(p).convert("HSV")
        except Exception:
            continue
        im.thumbnail((192, 192 * 12))     # 保住长图的纵向信息，别把整页压成一条
        a = np.asarray(im, dtype=np.float32) / 255.0
        H, S, V = a[..., 0], a[..., 1], a[..., 2]
        mass = S * V                      # 灰的不投票：无彩色像素不该影响色相分布
        idx = np.clip((H * 12).astype(np.int32), 0, 11)
        hues += np.bincount(idx.ravel(), weights=mass.ravel(), minlength=12)
        vs.append(float(np.median(V)))
        ss.append(float(S.mean()))
        grays.append(float((S < 0.12).mean()))
        used += 1
    if not used:
        return None
    tot = hues.sum()
    return {
        "hue": (hues / tot).tolist() if tot > 0 else [0.0] * 12,
        "v": sum(vs) / len(vs),
        "s": sum(ss) / len(ss),
        "gray": sum(grays) / len(grays),
        "n_frames": used,
    }


def palette_dist(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> float:
    """两份签名的配色距离 ∈[0,1]。明暗倾向占大头 —— 它最好判、也最像人说的"配色不同"。"""
    if not a or not b:
        return 0.0
    inter = sum(min(x, y) for x, y in zip(a["hue"], b["hue"]))
    d_hue = 1.0 - inter
    d_v = min(1.0, abs(a["v"] - b["v"]) / 0.35)
    d_s = min(1.0, abs(a["s"] - b["s"]) / 0.30)
    return round(0.50 * d_v + 0.35 * d_hue + 0.15 * d_s, 4)


# ── sRGB / WCAG 对比度 / ΔE（probes 与 measure 共用）─────────────────────────
def hex_to_rgb(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _srgb_to_lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = (_srgb_to_lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(rgb1, rgb2) -> float:
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 3)


def rgb_to_lab(rgb):
    """sRGB(0-255) → CIE L*a*b*（D65）。"""
    r, g, b = (_srgb_to_lin(x) for x in rgb)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    xn, yn, zn = 0.95047, 1.0, 1.08883

    def f(t):
        return t ** (1.0 / 3) if t > 0.008856 else (7.787 * t + 16.0 / 116)

    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(hex_a: str, hex_b: str) -> Optional[float]:
    """ΔE*ab（CIE76）。§3 的表用的就是这条尺：主色 ΔE=18.3 > 15 → 0 分。"""
    ra, rb = hex_to_rgb(hex_a), hex_to_rgb(hex_b)
    if not ra or not rb:
        return None
    la, lb = rgb_to_lab(ra), rgb_to_lab(rb)
    return round(sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5, 2)
