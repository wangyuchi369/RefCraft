#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""帧差与纵向覆盖的原始度量 —— 抓帧两侧共用的一份阈值。

为什么单独一个文件:参考侧(`tools/step0/recapture_reference.py`)和模型侧
(`utils/render/html_renderer.capture_frames`)必须按**同一套判据**决定"这一帧算不算新状态"。
两边各写一份常量的下场是:参考侧留下了一张 hover 帧、模型侧把同样的变化判成"没变化"
丢掉了,于是条目在参考侧有证据、在产物侧没证据 —— 而无证据的默认结果是给所有模型垫地板
(设计总纲 §4.1)。阈值只在这里写一次。

**这里只算数,不判对错。** `_diff`/`_local_diff` 返回的是原始像素差,进 `frames.json`
留档;"差值≈0 说明点了没反应"这种判定是下游的纯函数(§4.2 三态)。

参考侧现在还是自己那份常量(它不 import 这里 —— 参考帧不因此重拍,见 §3.5 第三条护栏)。
`tools/qa/check_frame_capture.py` 逐个数字比对两边,分叉就报错。
"""

from typing import Iterable, List, Sequence

import numpy as np
from PIL import Image

# ── 视口:主视口跟参考帧同宽同高(§3.5「主视口跟参考帧同宽」)────────────────
VW, VH = 1920, 1080

# ── 帧去重阈值(0-255 灰度)─────────────────────────────────────────────────
DIFF_MIN = 1.8          # 全图灰度均值绝对差,低于此视为「同一帧」
SCROLL_DIFF_MIN = 3.0   # 滚动帧之间要求更明显的差异
# hover 一个按钮只改动全图 1%~2% 的像素,全图均值差约 0.3,远低于 DIFF_MIN,
# 会被判成「什么都没变」。所以局部变化改用「最大分块差异」判据。阈值是实测定的:
# 静态页同状态连拍差异恰好为 0(没有噪声地板),Bootstrap 的 .btn:hover 压暗 = 10.8、
# 卡片 hover 位移 = 6.0,而只换链接颜色的 nav a:hover = 1.6、button:hover = 2.6。
# 取 4.0 正好把前两类留下、后两类挡掉。
LOCAL_DIFF_MIN = 4.0

THUMB_W, THUMB_H = 640, 360
BLOCK = 8               # 1920px 页面上的按钮缩到 640 宽约 33x13px,8x8 的块才装得下

MIN_COV_GAIN = 120      # 一帧至少要新增这么多像素的纵向覆盖才值得抓/保留


def thumb(png_bytes: bytes) -> np.ndarray:
    """截图字节 → 灰度缩略图数组。所有差值都在缩略图上算(快,且抗 1px 抖动)。"""
    import io as _io
    im = Image.open(_io.BytesIO(png_bytes)).convert("L").resize((THUMB_W, THUMB_H))
    return np.asarray(im, dtype=np.float32)


def diff(a: np.ndarray, b: np.ndarray) -> float:
    """全图灰度均值绝对差 —— 判断「整页换了」(滚动、切页、开弹层)。"""
    return float(np.abs(a - b).mean())


def local_diff(a: np.ndarray, b: np.ndarray) -> float:
    """最大分块差异 —— 判断「局部变了」(hover 态、单个输入框被填入文字)。

    把差值图按 BLOCK×BLOCK 分块求块内均值,取最大的一块。局部小改动在全图均值里
    会被摊薄到接近 0,在块均值里则保留原始强度。
    """
    d = np.abs(a - b)
    h, w = d.shape
    hb, wb = h // BLOCK, w // BLOCK
    if hb == 0 or wb == 0:
        return float(d.mean())
    d = d[:hb * BLOCK, :wb * BLOCK].reshape(hb, BLOCK, wb, BLOCK)
    return float(d.mean(axis=(1, 3)).max())


def cov_len(ys: Iterable[float], page_height: float, vh: int = VH) -> int:
    """一组滚动位置(每个覆盖 [y, y+vh])在 0..page_height 内的覆盖总长度。"""
    if page_height <= 0:
        return 0
    merged: List[List[float]] = []
    for y in sorted(ys):
        a, b = max(0.0, float(y)), min(float(page_height), float(y) + vh)
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return int(sum(b - a for a, b in merged))


def scroll_targets(page_height: float, vh: int = VH) -> List[int]:
    """按页面**实际高度**排一遍纵向扫描位置(§3.5 的 kind="scroll" 那一行)。

    需要 ceil(H/vh) 屏才盖得住,位置在 0..max_scroll 上均分。返回的是候选,
    真抓的时候还要过 MIN_COV_GAIN(重复位置不带信息)。
    """
    if page_height <= 0:
        return [0]
    need = max(1, int(-(-float(page_height) // vh)))     # ceil
    max_scroll = max(0.0, float(page_height) - vh)
    if need == 1 or max_scroll <= 0:
        return [0]
    return [int(round(i * max_scroll / (need - 1))) for i in range(need)]


def is_new_scroll_frame(cur: np.ndarray, prev: np.ndarray,
                        cov_gain: float = 0.0) -> bool:
    """滚动帧去重:画面差够大,或者新盖到了页面上没盖过的一段。"""
    return diff(cur, prev) >= SCROLL_DIFF_MIN or cov_gain >= MIN_COV_GAIN


def frame_delta(cur: np.ndarray, prev: np.ndarray) -> dict:
    """一帧相对上一帧的原始差值,直接进 frames.json。

    `delta_mean` 全图、`delta_max_block` 最大分块。两个都给是因为它们回答不同的问题:
    整页换没换、以及某一小块换没换。**不在这里判"算不算变了"** —— 那是下游的事。
    """
    return {"delta_mean": round(diff(cur, prev), 3),
            "delta_max_block": round(local_diff(cur, prev), 3)}


def pick_scroll_frames(cands: Sequence[dict], k: int, page_height: float,
                       vh: int = VH) -> List[int]:
    """帧位不够时挑滚动帧:贪心最大化纵向覆盖(不是挑差异最大的)。

    `cands` 是带 `scroll_y` 的帧记录,返回选中的下标(升序)。滚动帧的用途是
    "整页看得全",所以判据是覆盖,不是好看。
    """
    if k <= 0 or not cands:
        return []
    if len(cands) <= k:
        return list(range(len(cands)))
    chosen: List[int] = []
    ys: List[float] = []
    while len(chosen) < k:
        best, best_gain = None, -1.0
        for i, c in enumerate(cands):
            if i in chosen:
                continue
            gain = cov_len(ys + [c.get("scroll_y") or 0], page_height, vh) - cov_len(ys, page_height, vh)
            if gain > best_gain:
                best, best_gain = i, gain
        if best is None:
            break
        chosen.append(best)
        ys.append(cands[best].get("scroll_y") or 0)
    return sorted(chosen)
