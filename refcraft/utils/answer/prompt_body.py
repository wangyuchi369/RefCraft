#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""被测模型看到的 prompt 主体 —— Direct(step2) 和 Agent(step2b) **只能有这一份实现**。

为什么值得单独一个模块：Agent 分的用途是"同一道题、同一份指令，给它一个能自截图自改的环境
之后能好多少"（总纲 §1.3、§3.4）。主体一旦两边不一致，Agent−Direct 的差里就混进了 prompt 差异,
这个指标当场作废 —— 而两边各写一份、改了一边忘另一边，是这类失真最常见的来路。

踩过的两个坑，都写在下面的函数里：

| 坑 | 后果 |
|---|---|
| 收尾那句"输出单个 HTML 文件"无条件拼中文 | 291 个英文样本的英文指令后缀一句中文 = 指令里掺语言噪声，而中英两批最后同榜比分 |
| 截图说明按**磁盘上的帧数**算，不按**真正喂进去的图数**算 | 有一张图加载失败时，说明里"第 1 张是 screenshot_1…共 9 张"和份边界全部错位，比不给边界更糟 |

第二个坑只在 Agent 侧有过：Direct 传 `len(images_b64)`，Agent 传 `len(screenshot_paths)`。
所以本函数**只收"真正要喂进去的图数"**，调用方必须先把图加载完再来。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ..query.ref_directives import response_screenshot_note

TAIL_ZH = "\n\n最终输出为单个 HTML 文件（可内嵌 CSS 和 JS）。"
TAIL_EN = "\n\nOutput a single self-contained HTML file (CSS and JS may be inlined)."


def is_zh(lang: str) -> bool:
    """中英只判一次。

    "选哪份指令"和"收尾拼哪种语言"必须出自**同一个判断**：原来一处写 `lang == "zh"`、
    一处写 `startswith("zh")`，`language="zh-CN"` 这类值就会走成"英文指令 + 中文收尾"
    —— 语言噪声进指令，而且两边都不报错。
    """
    return str(lang or "").lower().startswith("zh")


def response_tail(lang: str) -> str:
    """收尾那句必须跟用户指令同语言（见模块 docstring 第一个坑）。"""
    return TAIL_ZH if is_zh(lang) else TAIL_EN


def build_response_prompt(template: str, prompt_data: Dict[str, Any],
                          meta: Optional[Dict[str, Any]],
                          n_images: int) -> Tuple[Optional[str], str]:
    """拼出被测模型的 prompt 主体。返回 (prompt, lang)；用户指令为空时返回 (None, lang)。

    Args:
        n_images: **真正会喂进去的图数**（load_images_base64 之后的长度），不是磁盘上的帧数。

    这里**不注入** ref_directives 矩阵：矩阵已经被 step0 编码进用户指令的散文里，
    被测模型能否读懂那段散文本身就是考点，把机读矩阵递给它等于送答案（总纲 §1.2 C3）。
    """
    lang = (prompt_data or {}).get("language", "en")
    key = "user_prompt_zh" if is_zh(lang) else "user_prompt_en"
    user_prompt = ((prompt_data or {}).get(key) or "").strip()
    if not user_prompt:
        return None, lang
    user_prompt += response_tail(lang)
    prompt = (template
              .replace("{{screenshot_note}}", response_screenshot_note(n_images, meta))
              .replace("{{user_prompt}}", user_prompt))
    return prompt, lang
