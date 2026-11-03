# -*- coding: utf-8 -*-
"""种子 → checklist 条目：把 query set 的 `ref_checklist_seeds` 转成程序条目。

docs/设计总纲.md §3.3 / §4.6：风格 / 归属 / spec 条目在**生成题面时**就已经定好了
（哪一面取自哪一份、题面点名的硬指标是什么、反面参考是哪一份），因此这些条目由程序
出全，VLM 只补 `ref_aspect == ""` 的内容 / 结构 / 导航 / 功能条目。这样做闭掉三个坑：

  1. §2.2 —— specify 的真值来自**题面**，不再被 judge 拿截图覆盖（种子把它标成 spec，
     basis 里写死判法，`answer_source="题面"`）。
  2. §3.6 —— 送图路由天然正确：每一条要送参考图的条目都来自种子，`ref_aspect` 必填，
     `judge_facts.item_ref_labels` 直接能算出送哪几张（VLM 现猜的归属条目常漏填 ref_aspect，
     只能"保守全送"）。
  3. 可核 —— 归属条目带 `source_sample_id` / `wrong_sample_id` 溯源，比 VLM 看着题面猜出来的
     "配色是否一致"更能被人复查。

条目字段与 utils/checklist/judged.py 的程序条目对齐；多出来的元字段（source / seed_kind / axis /
dial / ref_directive / spec_code / answer_source）全部要进三张隐藏名单
（step4b_score._HIDDEN_FIELDS、utils.score.axes._SPEC_META_KEYS、step4a_verify 的拷贝表），
否则会泄漏给评分 judge 当"这条有人盯着"的暗号。

本模块是**纯函数、零模型、零 IO**，用 tools/qa/check_checklist_seeds.py 钉死。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..query.ref_directives import ASPECT_ZH, critical_quota

# 与 utils/checklist/judged.py 的 _CATEGORY_OF 保持一致（aspect → 评分类目）。这里独立定义
# 只为让本模块能脱离 judged 单测；两处都改的风险很低（侧面词表是固定的）。
_CATEGORY_OF = {
    "layout": "Layout", "typography": "Visual", "component": "ComponentStyle",
    "atmosphere": "Visual", "interaction": "Interaction", "palette": "Visual",
}

# 每种种子的判分深度（§2.6 有没有 / 对不对 / 准不准）。
#   - spec 是题面点名的**硬指标**（border-radius ≥ 半高之类），准不准 → precision。
#   - 其余归属 / 反面 / 单参考都是"跟对调性 / 没跟错份"的方向判断 → correctness。
#   - existence（有没有这个板块）是 VLM 内容条目的活，种子不出。
_SEED_DEPTH = {
    "spec": "precision",
    "attribution_positive": "correctness",
    "attribution_negative": "correctness",
    "avoidance": "correctness",
    "single_copy": "correctness",
    "single_adapt": "correctness",
    "single_invert": "correctness",
}

# step5 分轴报分用的难度轴。dial 由样本级 `dials` 决定，这里只挂轴名；轴名要和
# utils/checklist/judged.py 出的程序条目对得上（bind/neg → ref_binding，invert → invert），
# 这样种子条目和 refspec 富集来的量化条目落进同一个桶。
_SEED_AXIS = {
    "spec": "specify",
    "attribution_positive": "ref_binding",
    "attribution_negative": "ref_binding",
    "avoidance": "invert",
    "single_invert": "invert",
    # single_copy / single_adapt 是单参考基线，不属于任何难度 trick → 不挂轴
}

_SEED_SUBCAT = {
    "spec": "Spec (题面硬指标)",
    "attribution_positive": "Reference binding",
    "attribution_negative": "Reference binding (negative)",
    "avoidance": "Avoidance (反面参考)",
    "single_copy": "Single-ref follow",
    "single_adapt": "Single-ref follow",
    "single_invert": "Single-ref invert",
}

# judged.py 出的、和种子归属重叠的两类轴。合并时这两类不另出条目，只把量化证据并进
# 对应种子的 basis；cross_form / ref_gap / coupling 是种子不覆盖的 trick，照原样加。
_JUDGED_OVERLAP_AXES = {"ref_binding", "invert"}

VALID_SEED_KINDS = frozenset(_SEED_SUBCAT)


def _aspect_zh(aspect: str) -> str:
    return ASPECT_ZH.get(aspect, aspect).split("（")[0]


def _seed_basis(seed: Dict[str, Any]) -> str:
    """给种子条目写 basis —— judge 判分的证据来源（可见，不进隐藏名单）。"""
    kind = seed.get("kind")
    aspect = _aspect_zh(seed.get("aspect", ""))
    if kind == "attribution_positive":
        return (f"分派表把「{aspect}」指给第「{seed.get('source_label')}」份"
                f"「{seed.get('source_handle', '')}」：{seed.get('source_desc', '')}。"
                f"产出这一面应神似这一份（{seed.get('directive')}）。")
    if kind == "attribution_negative":
        return (f"「{seed.get('source_label')}」份负责「{aspect}」；"
                f"错误来源「{seed.get('wrong_label')}」份「{seed.get('wrong_handle', '')}」"
                f"在这一面是：{seed.get('wrong_source_desc', '')}。"
                f"产出出现这些特征即为跟错份（不通过）。")
    if kind == "avoidance":
        return (f"用户明确要求「{aspect}」不要做成「{seed.get('avoid_handle', '')}」那样："
                f"{seed.get('avoid_desc', '')}。做成别的任何合理样子都算通过；"
                f"做成这一份的样子即不通过。")
    if kind == "spec":
        return (f"题面点名的硬指标（{seed.get('spec_code')}）。判法：{seed.get('spec_check', '')}。"
                f"真值来自**题面**，不看参考图。")
    if kind in ("single_copy", "single_adapt", "single_invert"):
        verb = {"single_copy": "严格跟随", "single_adapt": "借调性、允许调整",
                "single_invert": "明确反着来"}[kind]
        return f"单参考样本：本侧面要求{verb}参考截图。"
    return ""


def seed_to_item(seed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """一条种子 → 一条 checklist 条目。种子非法（缺 question_zh / 未知 kind）返回 None。"""
    if not isinstance(seed, dict):
        return None
    kind = seed.get("kind")
    q = (seed.get("question_zh") or "").strip()
    if kind not in VALID_SEED_KINDS or not q:
        return None
    aspect = (seed.get("aspect") or "").strip().lower()
    imp = seed.get("importance", "medium")
    if imp not in ("critical", "high", "medium", "low"):
        imp = "medium"

    item: Dict[str, Any] = {
        # id 用 seed_ 前缀，step1 合并时统一重编号成整数（和 judged 的 bind_/neg_ 一样）
        "id": f"seed_{kind}",
        "question": q,
        "category": _CATEGORY_OF.get(aspect, "Visual"),
        "subcategory": _SEED_SUBCAT.get(kind, ""),
        "expected_answer": "Yes",          # 所有种子都是"是否…？"正向问法，Yes = 通过
        "importance": imp,
        "depth": _SEED_DEPTH.get(kind, "correctness"),
        "ref_aspect": aspect,
        "basis": _seed_basis(seed),
        # ── 隐藏元字段（评分 judge 看不到，只服务 step5 分轴 / step1b 溯源）──
        "axis": _SEED_AXIS.get(kind),
        "dial": None,
        "deterministic": False,
        "source": "seed",
        "seed_kind": kind,
        "ref_directive": seed.get("directive", ""),
    }
    if kind == "spec":
        # spec 的答案来自题面：留给 step1a 的探针认领（spec_code → probe），
        # 现在还没接探针，先当 VLM 判的硬条目，但把判法/来源都带上。
        item["spec_code"] = seed.get("spec_code")
        item["answer_source"] = seed.get("answer_source", "题面")
        if seed.get("spec_check"):
            item["spec_check"] = seed["spec_check"]
    return item


def seed_items(prompt_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """样本的全部种子 → 条目列表（保持种子原序，纯函数、可复现）。"""
    out: List[Dict[str, Any]] = []
    for s in (prompt_data.get("ref_checklist_seeds") or []):
        it = seed_to_item(s)
        if it is not None:
            out.append(it)
    return out


def _polarity(item: Dict[str, Any]) -> str:
    """归属/反面条目的正负向：正向=跟对指定份，负向=没跟错份/不像。"""
    k = item.get("seed_kind")
    if k in ("attribution_negative", "avoidance", "single_invert"):
        return "neg"
    if k in ("attribution_positive", "single_copy", "single_adapt"):
        return "pos"
    # judged 条目按 id 前缀判
    _id = str(item.get("id", ""))
    if _id.startswith("neg") or item.get("axis") == "invert":
        return "neg"
    if _id.startswith("bind"):
        return "pos"
    return "pos"


def merge_judged(seeds: Sequence[Dict[str, Any]],
                 judged: Sequence[Dict[str, Any]]
                 ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把 step1a 的 `judged` 条目并进种子条目。

    judged 里的 ref_binding / invert 条目和种子的归属/反面**重叠**（同一侧面判两遍会
    重复占权重 —— 榜单被必赢/重复条目压平的坑踩过一次）。所以这两类不另出条目，只把
    judged 那条 basis 里的**量化实测**（"圆角 0px vs 24px、字号 18 vs 14"）并进同侧面、
    同正负向的种子条目的 basis，把 prose-only 的"判不动"升级成"有值可比"。

    cross_form / ref_gap / coupling 是种子不覆盖的难度 trick（§1 表里 harness 闭不了的
    gap），原样加进来。

    judged 为空（step1a 还没跑）时是 no-op：直接返回 seeds 的拷贝。
    返回 (合并后的条目, 记账 notes)。
    """
    items = [dict(it) for it in seeds]
    notes: List[str] = []

    # 建索引：(ref_aspect, polarity) → 种子条目，用于并 basis
    idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for it in items:
        if it.get("axis") in _JUDGED_OVERLAP_AXES:
            idx[(it.get("ref_aspect", ""), _polarity(it))] = it

    for jd in (judged or []):
        if not isinstance(jd, dict):
            continue
        axis = jd.get("axis")
        if axis in _JUDGED_OVERLAP_AXES:
            key = (jd.get("ref_aspect", ""), _polarity(jd))
            tgt = idx.get(key)
            if tgt is not None:
                ev = (jd.get("basis") or "").strip()
                if ev and ev not in (tgt.get("basis") or ""):
                    tgt["basis"] = (tgt.get("basis") or "").rstrip("。") + \
                        f"。程序实测：{ev}"
                notes.append(f"judged {jd.get('id')} 的量化证据并进种子（{key}）")
            else:
                # 种子没覆盖到的归属/反面（老数据或分派表差异）→ 原样加，别丢
                items.append(dict(jd))
                notes.append(f"judged {jd.get('id')} 无对应种子，原样保留")
        else:
            # cross_form / ref_gap / coupling：种子不覆盖，直接加
            items.append(dict(jd))
    return items, notes


def seed_critical_count(items: Sequence[Dict[str, Any]]) -> int:
    return sum(1 for it in items if it.get("importance") == "critical")


def remaining_critical_quota(prompt_data: Dict[str, Any],
                             seeds: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    """种子已经占掉一部分 critical 名额后，留给 VLM 的 critical 配额 (lo, hi)。

    种子每样本出 0-6 条 critical（spec 和 copy 归属）。VLM 只补内容/结构条目，配额
    应是"总配额减去种子已占"。lo 不为负；种子已超上限时 VLM 配额为 (0, 0)。
    """
    lo, hi = critical_quota(prompt_data)
    nc = seed_critical_count(seeds)
    return max(0, lo - nc), max(0, hi - nc)
