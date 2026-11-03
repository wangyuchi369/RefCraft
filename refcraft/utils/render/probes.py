#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""确定性探针：读 measure.json，给出 pass / fail / **blocked**。

分工（docs/设计总纲.md §3.5、§4.1）：
    utils/render/measure.py  取证（起浏览器，慢，可能崩）→ measure.json 只存原始数值
    utils/render/probes.py   判定（纯函数，零成本重算）→ 改阈值不用重跑浏览器
    step1a            出规格（与 query 同源，判不了的要求就不出）
    step4a            把判定结果折成 0/RAW_MAX 的条目分

三态而不是两态，是这一层唯一的硬要求：
    pass    有证据且达标
    fail    有证据且不达标        ← 只有这一档能扣分
    blocked 没证据（背景是图片算不出对比度 / 页面里没有列表可注数据 / 量的时候崩了）
**blocked 绝不能当 fail**。把"没量到"当"没做到"会凭空造出区分度 —— 比不判更糟，
因为它看起来像信号。blocked 的条目在 step4a 里按"缺失"处理，不进分母。

反过来也有一条同样硬的（总纲 §3.5 护栏 2）:**"找不到落点"不是没证据,是不过。**
"页面上没有这个选择器 / 一个可交互控件都没有" —— 证据恰恰是"扫过了,没有"。
把它记成 blocked 就成了奖励缺功能:没做这个组件 → 点不到 → 不进分母 → 白捡。
量到的 status 因此分三种:`ok`(点了,看反应)、`absent`(元素不存在 → fail)、
`blocked`(选择器是我们写错了 / 浏览器崩了 → 真的没证据)。

新增探针的规矩：写进 PROBE_SCHEMA（step1a 只会出这里列出的探针），
并保证 needs 里声明的证据字段在 measure.json 里真的存在。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# 分值档位的唯一定义点在 utils/score/scoring_calibers（判分与聚合同源，
# 避免程序轨和 VLM 轨各写一个字面量后悄悄错位）。
from ..score.scoring_calibers import RAW_MAX

PASS, FAIL, BLOCKED = "pass", "fail", "blocked"

# 默认阈值。改这里就等于改判分口径，所以每条都写清出处。
DEFAULTS = {
    "overflow_tol_px": 2,          # 1px 级差是亚像素舍入，不算溢出
    "contrast_min": 4.5,           # WCAG AA 正文
    "contrast_min_aaa": 7.0,       # a11y 档 2/3（"给我爸妈用"）
    "font_min_px": 14.0,
    "tap_min_px": 44.0,            # WCAG 2.5.5（AAA）
    "tap_min_px_aa": 24.0,         # WCAG 2.2 AA 2.5.8
    "palette_dist_follow_margin": 0.10,   # 跟对了：离目标那份要比离别的份近这么多
    "palette_dist_unlike_min": 0.50,      # 明确不像：等于配对阶段的可分辨门槛
    "delta_e_tol": 15.0,           # ΔE76 > 15 = 肉眼明显不同色
    "unknown_bg_max": 0.50,        # 背景图占比过半 → 对比度整项 blocked
    "fold_tol": 1.15,              # 首屏内：允许 15% 溢出（滚动条/边距）
    "fixture_overflow_tol_px": 8,  # 注入数据后新增的横向溢出容忍
}

# step1a 只能从这张表里出题：写不进这张表的要求，就是判不了的要求。
PROBE_SCHEMA: Dict[str, Dict[str, Any]] = {
    "no_horizontal_overflow": {
        "desc": "指定视口下没有横向溢出（scrollWidth ≤ 视口宽）",
        "params": {"viewports": "list[int]，默认全部量过的视口", "tol": "int px"},
        "needs": ["viewports.*.geometry.overflow_px"],
    },
    "contrast_ratio": {
        "desc": "文字对比度不低于 min（WCAG）",
        "params": {"min": "float", "scope": "body_text | any_text", "viewport": "int"},
        "needs": ["viewports.*.text.contrast"],
    },
    "min_font_size": {
        "desc": "正文字号（全页文字中位数）不小于 min_px",
        "params": {"min_px": "float", "viewport": "int"},
        "needs": ["viewports.*.text.font_px"],
    },
    "tap_target_min": {
        "desc": "大多数可点元素的短边（中位数）不小于 min_px（触屏可用性）",
        "params": {"min_px": "float", "viewport": "int，默认最窄视口"},
        "needs": ["viewports.*.tap_targets"],
    },
    "no_hover_only_affordance": {
        "desc": "没有只能靠 :hover 才出现的功能（触屏上等于不存在）",
        "params": {"max_allowed": "int，默认 0"},
        "needs": ["viewports.*.hover"],
    },
    "dom_changes_on_click": {
        "desc": "点击后 DOM 真的变了（源码里有事件绑定不算）",
        "params": {"selector": "可选，指定要点的元素", "min_changed": "int，默认 1",
                   "min_ratio": "float，可选：有变化的控件占比下限（查'死按钮'）"},
        "needs": ["interaction.probes"],
    },
    "no_overflow_with_data": {
        "desc": "注入对抗数据后不横向溢出",
        "params": {"fixture": "zero_rows|many_rows_300|long_string_80ch|cjk_emoji",
                   "viewports": "list[int]", "tol": "int px"},
        "needs": ["fixtures.*"],
    },
    "empty_state_present": {
        "desc": "数据清空后有空态提示（不是白屏）",
        "params": {"fixture": "默认 zero_rows"},
        "needs": ["fixtures.zero_rows.*.empty_state"],
    },
    "self_contained": {
        "desc": "单文件自包含，不依赖外部网络资源",
        "params": {"max_external": "int，默认 0"},
        "needs": ["network"],
    },
    "no_js_errors": {
        "desc": "运行时没有未捕获的 JS 异常",
        "params": {"max_allowed": "int，默认 0"},
        "needs": ["console.page_errors"],
    },
    "palette_delta_e": {
        "desc": "指定色位与目标色的 ΔE76 在容差内",
        "params": {"target": "#rrggbb", "tol": "float", "scope": "accent|primary_bg|body_text"},
        "needs": ["viewports.*.colors"],
    },
    "palette_follows_ref": {
        "desc": "配色确实取自点名的那份参考，而不是别的份（多参考绑定）",
        "params": {"ref": "label", "others": "list[label]", "margin": "float"},
        "needs": ["palette_sig", "ctx.ref_sigs"],
    },
    "palette_unlike_ref": {
        "desc": "配色明确**不像**点名的那份（invert / 反面参考）",
        "params": {"ref": "label", "min_dist": "float"},
        "needs": ["palette_sig", "ctx.ref_sigs"],
    },
    "token_consistency": {
        "desc": "设计 token 收敛（字体族/字号档/圆角/阴影的变体数不超上限）",
        "params": {"max_font_families": "int", "max_font_sizes": "int",
                   "max_radius": "int", "max_shadow": "int", "viewport": "int"},
        "needs": ["viewports.*.tokens"],
    },
    "content_fits_fold": {
        "desc": "内容落在一屏内（不产生纵向滚动）",
        "params": {"viewport": "int", "tol": "float 倍数"},
        "needs": ["viewports.*.geometry.scroll_height"],
    },
    "anchors_resolve": {
        "desc": '页内锚点链接（href="#id"）都指向真实存在的 id',
        "params": {"scope": "page | nav（nav 只看 <nav>/[role=navigation] 里的）",
                   "min_links": "int，少于这个数就是没做目录/锚点导航 → fail",
                   "max_broken": "int，允许几个指不到落点的"},
        "needs": ["anchors.n_hash", "anchors.n_resolved"],
    },
}


# ─────────────────────────── 小工具 ───────────────────────────
def _r(spec: dict, status: str, value=None, target=None, why: str = "",
       detail: Optional[dict] = None) -> Dict[str, Any]:
    out = {
        "id": (spec or {}).get("id"),
        "probe": (spec or {}).get("probe"),
        "status": status,
        "value": value,
        "target": target,
        "why": why,
    }
    if detail:
        out["detail"] = detail
    return out


def _vp(measure: dict, want: Optional[int] = None) -> tuple:
    """取一个视口的测量结果。want=None → 主视口（最宽的那个，与 step3 渲染帧同宽）。"""
    vps = (measure or {}).get("viewports") or {}
    if not vps:
        return None, None
    keys = sorted((int(k) for k in vps), reverse=True)
    if want is None:
        k = keys[0]
    else:
        want = int(want)
        k = want if want in keys else min(keys, key=lambda x: abs(x - want))
        if abs(k - want) > 64:      # 差太多就别硬凑：拿宽屏数据判 390 的响应式是错的
            return None, None
    return k, vps[str(k)]


def _all_vp_widths(measure: dict) -> List[int]:
    return sorted((int(k) for k in ((measure or {}).get("viewports") or {})), reverse=True)


def _num(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and x == x else None


# ─────────────────────────── 各探针 ───────────────────────────
def _p_no_horizontal_overflow(spec, measure, ctx):
    tol = int(spec.get("tol", DEFAULTS["overflow_tol_px"]))
    wanted = spec.get("viewports") or _all_vp_widths(measure)
    per, worst, worst_w, missing = {}, None, None, []
    for w in wanted:
        k, vp = _vp(measure, w)
        if not vp:
            missing.append(w)
            continue
        ov = _num((vp.get("geometry") or {}).get("overflow_px"))
        if ov is None:
            missing.append(w)
            continue
        per[str(k)] = ov
        if worst is None or ov > worst:
            worst, worst_w = ov, k
    if not per:
        return _r(spec, BLOCKED, why=f"没有这些视口的几何证据: {wanted}")
    nodes = []
    if worst_w is not None:
        _, vpw = _vp(measure, worst_w)
        nodes = ((vpw or {}).get("geometry") or {}).get("overflow_nodes", [])[:3]
    ok = worst <= tol
    return _r(spec, PASS if ok else FAIL, value=worst, target=f"≤{tol}px",
              why=("" if ok else f"{worst_w}px 视口下横向溢出 {worst:.0f}px"),
              detail={"per_viewport": per, "missing_viewports": missing,
                      "worst_viewport": worst_w, "overflow_nodes": nodes})


def _p_contrast_ratio(spec, measure, ctx):
    lo = float(spec.get("min", DEFAULTS["contrast_min"]))
    scope = spec.get("scope", "body_text")
    k, vp = _vp(measure, spec.get("viewport"))
    c = ((vp or {}).get("text") or {}).get("contrast") or {}
    if not c or c.get("n") in (None, 0):
        return _r(spec, BLOCKED, why="没有文字对比度证据")
    unknown = _num(c.get("unknown_ratio")) or 0.0
    if unknown > DEFAULTS["unknown_bg_max"]:
        return _r(spec, BLOCKED, target=f"≥{lo}",
                  why=f"{unknown:.0%} 的文字压在背景图上，算不出对比度",
                  detail={"unknown_ratio": unknown})
    val = _num(c.get("body_text_ratio")) if scope == "body_text" else _num(c.get("min"))
    if val is None:
        val = _num(c.get("min"))
    if val is None:
        return _r(spec, BLOCKED, why="对比度取值缺失")
    ok = val >= lo
    return _r(spec, PASS if ok else FAIL, value=round(val, 2), target=f"≥{lo}",
              why=("" if ok else f"{scope} 对比度 {val:.2f}:1 < {lo}:1"),
              detail={"scope": scope, "viewport": k, "body_px": c.get("body_px"),
                      "worst_nodes": (c.get("worst_nodes") or [])[:3],
                      "unknown_ratio": unknown})


def _p_min_font_size(spec, measure, ctx):
    lo = float(spec.get("min_px", DEFAULTS["font_min_px"]))
    k, vp = _vp(measure, spec.get("viewport"))
    t = ((vp or {}).get("text") or {})
    st = t.get("font_px") or {}
    if not st.get("n"):
        return _r(spec, BLOCKED, why="没有文字字号证据（页面里没取到实体文字）")
    # 判**中位数**，也就是这个探针名义上要查的"正文字号"。
    # 原来判 p05，本意是"一个 10px 的版权行不该判整页不合格"，但实测这个缓解是
    # 空的：页面的文字节点中位数只有 40 个，p05 就是第 2 小的那个节点 —— 跟直接
    # 判最小值差不了多少（可点元素那边更极端，81% 的页面 p05 == min）。后果是
    # 一个 8px 的 SVG 标注就能否掉整页，1375 个产物里 p05≥14px 的只有 15%，
    # 整轴变成 doc §5 说的地板效应（全模型一起 0 分 = 没有区分度）。
    # 改判中位数后同一门槛的通过率是 0.466，落在 §5 的 0.2-0.6 可用带内。
    # 最小值/p05 仍然写进 detail，judge 和报告照样看得见最糟的节点。
    val = _num(st.get("median"))
    ok = val is not None and val >= lo
    return _r(spec, PASS if ok else FAIL, value=val, target=f"≥{lo}px",
              why=("" if ok else f"正文字号中位 {val}px < {lo}px"),
              detail={"viewport": k, "min": st.get("min"), "p05": st.get("p05"),
                      "median": st.get("median"),
                      "small_text_nodes": (t.get("small_text_nodes") or [])[:3]})


def _p_tap_target_min(spec, measure, ctx):
    lo = float(spec.get("min_px", DEFAULTS["tap_min_px"]))
    want = spec.get("viewport")
    if want is None:                      # 触屏可用性看最窄视口
        ws = _all_vp_widths(measure)
        want = ws[-1] if ws else None
    k, vp = _vp(measure, want)
    tt = (vp or {}).get("tap_targets") or {}
    if not tt.get("n_targets"):
        return _r(spec, BLOCKED, why="页面里没有可点元素，无从判断点击区域")
    side = tt.get("side") or {}
    # 同 min_font_size：可点元素中位数才 14 个，p05 在 81% 的页面上就等于最小值，
    # 于是"正文里一条内联链接"就能否掉整页 —— 44px 门槛下 1363 个产物 0.089 通过，
    # 是地板不是难度。判中位数（= 大多数可点元素）后 44px 通过 0.326、24px 通过
    # 0.928，正好给这一轴做出 AAA / AA 两级台阶。
    val = _num(side.get("median")) or _num(tt.get("min_side"))
    ok = val is not None and val >= lo
    return _r(spec, PASS if ok else FAIL, value=val, target=f"≥{lo}px",
              why=("" if ok else f"可点元素短边中位 {val}px < {lo}px"
                                 f"（{tt.get('n_below_44')}/{tt['n_targets']} 个 < 44px）"),
              detail={"viewport": k, "min_side": tt.get("min_side"),
                      "p05": side.get("p05"), "median": side.get("median"),
                      "n_below_44": tt.get("n_below_44"),
                      "small_targets": (tt.get("small_targets") or [])[:3]})


def _p_no_hover_only_affordance(spec, measure, ctx):
    mx = int(spec.get("max_allowed", 0))
    k, vp = _vp(measure, spec.get("viewport"))
    h = (vp or {}).get("hover") or {}
    if h.get("error"):
        return _r(spec, BLOCKED, why=f"读不到样式表: {h['error']}")
    if h.get("sheets_blocked") and not h.get("hover_rules"):
        return _r(spec, BLOCKED, why="样式表跨域不可读，查不了 hover 规则")
    found = h.get("hidden_until_hover") or []
    ok = len(found) <= mx
    return _r(spec, PASS if ok else FAIL, value=len(found), target=f"≤{mx}",
              why=("" if ok else f"{len(found)} 处功能只有 hover 才出现，触屏上不可达"),
              detail={"hover_rules": h.get("hover_rules"), "cases": found[:4]})


def _p_dom_changes_on_click(spec, measure, ctx):
    """点了 DOM 有没有动。

    这条只在**题面点名了状态型功能**时才出（step1a.interaction()：`state_hooks` 命中
    或要求深浅色切换），所以"页面上压根没有这种控件"是**没做到**，不是"没量到" ——
    见总纲 §3.5 护栏 2。三档来源：
      - `absent` 探针 / 扫出 0 个候选 / 只有页内锚点 → fail（该有的东西没有）
      - 点了不动                                  → fail（有但不动）
      - 量的时候崩了 / 规格里的选择器写错了        → blocked（真的没证据）
    """
    inter = (measure or {}).get("interaction")
    if not isinstance(inter, dict) or inter.get("error"):
        return _r(spec, BLOCKED, why="交互测量没跑成" + (f"：{inter.get('error')}" if isinstance(inter, dict) and inter.get("error") else ""))
    probes = inter.get("probes") or []
    n_cand = inter.get("n_candidates")
    if n_cand == 0:
        # 扫过整页（CLICK_CANDIDATES_JS 覆盖 button/[role]/details/input/锚点），一个都没有。
        return _r(spec, FAIL, value=0, target="≥1 个可交互控件",
                  why="题面点名了状态型功能，但整页扫不出一个可点的控件（button/tab/toggle 都没有）",
                  detail={"n_candidates": 0})
    if not probes:
        return _r(spec, BLOCKED, why="有候选但一个探针都没跑到（探针预算/异常）",
                  detail={"n_candidates": n_cand})

    sel = spec.get("selector")
    if sel:
        hit = [p for p in probes if p.get("selector") == sel]
        if not hit:
            hit = [p for p in probes if sel in str(p.get("selector", ""))]
        if not hit:
            # 规格点名的选择器会被 measure 排到探针队首（from_spec=True），到不了这儿；
            # 真到了说明是没把规格选择器传进 measure（管道漏了）→ 没证据，别算模型的账。
            return _r(spec, BLOCKED, target=sel, why=f"没探到指定选择器 {sel}（规格选择器没进 measure）")
        probes = hit

    absent = [p for p in probes if p.get("status") == "absent"]
    usable = [p for p in probes if p.get("status") == "ok"]
    if absent and not usable:
        return _r(spec, FAIL, value=0, target=sel or "≥1 个能点到的控件",
                  why="要点的元素在页面上不存在（选择器落不到地）：" +
                      "、".join(str(p.get("selector"))[:40] for p in absent[:3]),
                  detail={"absent": [p.get("selector") for p in absent[:4]],
                          "n_candidates": n_cand})
    if not usable:
        return _r(spec, BLOCKED, why="所有点击探针都没跑成（浏览器侧失败）",
                  detail={"probes": probes[:4]})
    if not sel:
        # 纯锚点（点了只滚动）本来就不该改 DOM。但这条题是"题面点名了状态功能"才出的，
        # 所以"页面上只有锚点"= 那个功能没做 → fail，不是 blocked（总纲 §3.5 护栏 2）。
        toggles = [p for p in usable if p.get("kind", "toggle") == "toggle"]
        if not toggles:
            return _r(spec, FAIL, value=0, target="≥1 个 tab/toggle/按钮",
                      why="页面里只有页内锚点，题面点名的那个状态型功能没做出来",
                      detail={"n_probed": len(usable),
                              "kinds": sorted({p.get("kind", "?") for p in usable})})
        usable = toggles

    n_changed = sum(1 for p in usable if p.get("changed"))
    # absent 的控件算进分母：它们是"该有没有"，和"有但不动"一起算才不会奖励缺功能
    n_total = len(usable) + len(absent)
    need = int(spec.get("min_changed", 1))
    ratio = spec.get("min_ratio")
    dead = [p.get("selector") for p in usable if not p.get("changed")][:4]
    missing = [p.get("selector") for p in absent[:3]]
    if ratio is not None:
        # 查"死按钮"：只要求至少一个能动太松了 —— 一半控件是死的也能过
        r = n_changed / max(1, n_total)
        ok = r >= float(ratio)
        return _r(spec, PASS if ok else FAIL, value=round(r, 3), target=f"≥{ratio}",
                  why=("" if ok else
                       f"{n_total - n_changed}/{n_total} 个控件没反应：点了不动 {dead}" +
                       (f"；不存在 {missing}" if missing else "")),
                  detail={"n_probed": len(usable), "n_absent": len(absent),
                          "n_changed": n_changed, "dead": dead, "absent": missing,
                          "probes": [{k: p.get(k) for k in ("selector", "kind", "changed")}
                                     for p in usable[:6]]})
    ok = n_changed >= need
    return _r(spec, PASS if ok else FAIL, value=n_changed, target=f"≥{need}",
              why=("" if ok else f"{len(usable)} 个可点元素点下去 DOM 全无变化（交互是死的）" +
                   (f"；另有 {len(absent)} 个选择器落不到地" if absent else "")),
              detail={"n_probed": len(usable), "n_absent": len(absent), "dead": dead,
                      "absent": missing,
                      "probes": [{k: p.get(k) for k in ("selector", "kind", "changed", "visible_delta")}
                                 for p in usable[:5]]})

def _p_no_overflow_with_data(spec, measure, ctx):
    """注入对抗数据后不横向溢出。

    判的是**增量**：基线本来就溢出（响应式没做好）由 no_horizontal_overflow 扣，
    这里只问"多塞数据以后是不是被撑破了"。同一个毛病扣两次会让权重失真。
    """
    name = spec.get("fixture", "long_string_80ch")
    tol = int(spec.get("tol", DEFAULTS["fixture_overflow_tol_px"]))
    fx = ((measure or {}).get("fixtures") or {}).get(name) or {}
    if not fx:
        return _r(spec, BLOCKED, why=f"没有 {name} 注入证据")
    wanted = [str(w) for w in (spec.get("viewports") or sorted(fx.keys(), key=int))]
    worst_delta, worst_w, per, blocked_why = None, None, {}, []
    for w in wanted:
        rec = fx.get(str(w))
        if not rec:
            continue
        if not rec.get("applied"):
            blocked_why.append(f"{w}px: {rec.get('why') or rec.get('error') or '注入不适用'}")
            continue
        after, before = _num(rec.get("overflow_px")), _num(rec.get("overflow_px_before"))
        if after is None:
            blocked_why.append(f"{w}px: 注入后没量到溢出")
            continue
        delta = after - (before or 0.0)
        per[str(w)] = {"before": before, "after": after, "delta": delta}
        if worst_delta is None or delta > worst_delta:
            worst_delta, worst_w = delta, w
    if worst_delta is None:
        # 页面里没有可注入的重复列表 → 记 blocked。"没列表"不等于"抗不住长数据"。
        return _r(spec, BLOCKED, why="；".join(blocked_why) or "注入未生效",
                  detail={"fixture": name})
    ok = worst_delta <= tol
    return _r(spec, PASS if ok else FAIL, value=round(worst_delta, 1), target=f"新增 ≤{tol}px",
              why=("" if ok else f"{name} 注入后 {worst_w}px 视口新增横向溢出 {worst_delta:.0f}px"),
              detail={"fixture": name, "per_viewport": per, "not_applied": blocked_why,
                      "overflow_nodes": ((fx.get(str(worst_w)) or {}).get("overflow_nodes") or [])[:3]})


def _p_empty_state_present(spec, measure, ctx):
    name = spec.get("fixture", "zero_rows")
    fx = ((measure or {}).get("fixtures") or {}).get(name) or {}
    if not fx:
        return _r(spec, BLOCKED, why=f"没有 {name} 注入证据")
    best, per, blocked_why = None, {}, []
    for w, rec in sorted(fx.items(), key=lambda kv: -int(kv[0])):
        if not rec.get("applied"):
            blocked_why.append(f"{w}px: {rec.get('why') or '注入不适用'}")
            continue
        es = rec.get("empty_state")
        if not es:
            blocked_why.append(f"{w}px: 没采到空态证据")
            continue
        per[w] = {"present": es.get("present"), "n_new": es.get("n_new"),
                  "keyword_hits": (es.get("keyword_hits") or [])[:3],
                  "new_texts": (es.get("new_texts") or [])[:3]}
        if es.get("present"):
            best = w
    if not per:
        return _r(spec, BLOCKED, why="；".join(blocked_why) or "清空数据未生效",
                  detail={"fixture": name})
    ok = best is not None
    return _r(spec, PASS if ok else FAIL, value=bool(ok), target="有空态提示",
              why=("" if ok else "数据清空后既没有新出现的提示文案，也没有空态关键词（白屏）"),
              detail={"per_viewport": per, "not_applied": blocked_why})


def _p_self_contained(spec, measure, ctx):
    mx = int(spec.get("max_external", 0))
    net = (measure or {}).get("network")
    if net is None:
        return _r(spec, BLOCKED, why="没有网络请求证据")
    n = int(net.get("n_external") or 0)
    ok = n <= mx
    return _r(spec, PASS if ok else FAIL, value=n, target=f"≤{mx} 个外部请求",
              why=("" if ok else f"依赖 {n} 个外部资源，离线打开会缺件"),
              detail={"external_requests": (net.get("external_requests") or [])[:6]})


def _p_no_js_errors(spec, measure, ctx):
    mx = int(spec.get("max_allowed", 0))
    con = (measure or {}).get("console")
    if con is None:
        return _r(spec, BLOCKED, why="没有控制台证据")
    # 只算未捕获异常。console.error 里混着"外链被我们主动拦掉"的报错，
    # 那是 self_contained 的事，不该在这里重复扣。
    errs = con.get("page_errors") or []
    ok = len(errs) <= mx
    return _r(spec, PASS if ok else FAIL, value=len(errs), target=f"≤{mx}",
              why=("" if ok else f"{len(errs)} 个未捕获 JS 异常"),
              detail={"page_errors": errs[:4]})


def _p_palette_delta_e(spec, measure, ctx):
    from ..query.palette import delta_e76
    target = str(spec.get("target") or "").strip()
    tol = float(spec.get("tol", DEFAULTS["delta_e_tol"]))
    scope = spec.get("scope", "accent")
    if not target.startswith("#") or len(target) not in (4, 7):
        return _r(spec, BLOCKED, why=f"目标色不合法: {target!r}")
    k, vp = _vp(measure, spec.get("viewport"))
    colors = (vp or {}).get("colors") or {}
    field = {"accent": "accent_by_count", "primary_bg": "bg_by_area",
             "body_text": "text_by_area"}.get(scope, "accent_by_count")
    cands = [c for c, _ in (colors.get(field) or [])]
    if not cands:
        return _r(spec, BLOCKED, why=f"没取到 {scope} 色（页面里没有对应色位）")
    scored = sorted(((delta_e76(target, c), c) for c in cands), key=lambda x: x[0])
    best_d, best_c = scored[0]
    ok = best_d <= tol
    return _r(spec, PASS if ok else FAIL, value=round(best_d, 1), target=f"ΔE ≤{tol}",
              why=("" if ok else f"{scope} 最近色 {best_c} 与目标 {target} 的 ΔE={best_d:.1f} > {tol}"),
              detail={"scope": scope, "viewport": k, "nearest": best_c,
                      "candidates": [[c, round(d, 1)] for d, c in scored[:5]]})


def _ref_sig(ctx: dict, label: str):
    sigs = ((ctx or {}).get("ref_sigs") or {})
    return sigs.get(label) or sigs.get(str(label))


def _p_palette_follows_ref(spec, measure, ctx):
    """多参考绑定：配色是不是取自点名的那份，而不是别的份。

    判的是**相对距离**而不是绝对距离 —— 绝对阈值会把"整体偏亮一档但明显跟了 A"
    误判成失败；而"离 A 比离 B 近"正好就是绑定对不对的定义。
    """
    from ..query.palette import palette_dist
    own = (measure or {}).get("palette_sig")
    if not own:
        return _r(spec, BLOCKED, why="没有产出侧的配色签名（渲染帧缺失）")
    ref = spec.get("ref")
    sig_ref = _ref_sig(ctx, ref)
    if not sig_ref:
        return _r(spec, BLOCKED, why=f"缺参考 {ref} 的配色签名")
    others = [o for o in (spec.get("others") or []) if o != ref]
    d_ref = palette_dist(own, sig_ref)
    d_others = {}
    for o in others:
        s = _ref_sig(ctx, o)
        if s:
            d_others[o] = round(palette_dist(own, s), 4)
    if d_ref is None:
        return _r(spec, BLOCKED, why="配色距离算不出")
    margin = float(spec.get("margin", DEFAULTS["palette_dist_follow_margin"]))
    if not d_others:
        # 没有可比的别份 → 只能退成绝对判定，说清楚这是弱判定
        ok = d_ref <= DEFAULTS["palette_dist_unlike_min"]
        return _r(spec, PASS if ok else FAIL, value=round(d_ref, 4),
                  target=f"距 {ref} ≤{DEFAULTS['palette_dist_unlike_min']}",
                  why=("" if ok else f"配色与 {ref} 差得远（dist={d_ref:.3f}），且没有别份可比"),
                  detail={"mode": "absolute", "d_ref": round(d_ref, 4)})
    nearest = min(d_others, key=lambda x: d_others[x])
    ok = d_ref + margin < d_others[nearest]
    return _r(spec, PASS if ok else FAIL, value=round(d_ref, 4),
              target=f"距 {ref} 比距其它份近 {margin}",
              why=("" if ok else
                   f"配色更像 {nearest}（dist={d_others[nearest]:.3f}）而不是点名的 {ref}"
                   f"（dist={d_ref:.3f}）—— 参考绑定错了"),
              detail={"mode": "relative", "d_ref": round(d_ref, 4), "d_others": d_others,
                      "nearest": nearest})


def _p_palette_unlike_ref(spec, measure, ctx):
    """invert / 反面参考：这一维要明确**不像**点名的那份。

    这是唯一"工具越强越容易错"的一类（harness 自比循环的梯度天然指向更像参考），
    所以判定方向必须写死：像了 = 不通过；不像成什么样都算通过。
    """
    from ..query.palette import palette_dist
    own = (measure or {}).get("palette_sig")
    if not own:
        return _r(spec, BLOCKED, why="没有产出侧的配色签名（渲染帧缺失）")
    ref = spec.get("ref")
    sig_ref = _ref_sig(ctx, ref)
    if not sig_ref:
        return _r(spec, BLOCKED, why=f"缺参考 {ref} 的配色签名")
    lo = float(spec.get("min_dist", DEFAULTS["palette_dist_unlike_min"]))
    d = palette_dist(own, sig_ref)
    if d is None:
        return _r(spec, BLOCKED, why="配色距离算不出")
    ok = d >= lo
    return _r(spec, PASS if ok else FAIL, value=round(d, 4), target=f"距 {ref} ≥{lo}",
              why=("" if ok else f"用户明确说了别做成 {ref} 那样的配色，产出却跟它很近（dist={d:.3f}）"),
              detail={"d_ref": round(d, 4)})


def _p_token_consistency(spec, measure, ctx):
    k, vp = _vp(measure, spec.get("viewport"))
    tk = (vp or {}).get("tokens") or {}
    if tk.get("error") or not tk:
        return _r(spec, BLOCKED, why="没有 token 证据")
    limits = {
        "font_family": int(spec.get("max_font_families", 3)),
        "font_size": int(spec.get("max_font_sizes", 12)),
        "radius": int(spec.get("max_radius", 4)),
        "shadow": int(spec.get("max_shadow", 4)),
    }
    over = {kk: [tk.get(kk), lim] for kk, lim in limits.items()
            if isinstance(tk.get(kk), int) and tk[kk] > lim}
    ok = not over
    return _r(spec, PASS if ok else FAIL,
              value={kk: tk.get(kk) for kk in limits}, target=limits,
              why=("" if ok else "token 发散：" + "、".join(
                  f"{kk} {v[0]} 种 > 上限 {v[1]}" for kk, v in over.items())),
              detail={"viewport": k, "over": over, "css_vars": tk.get("css_vars"),
                      "font_families": tk.get("font_families")})


def _p_content_fits_fold(spec, measure, ctx):
    k, vp = _vp(measure, spec.get("viewport"))
    g = (vp or {}).get("geometry") or {}
    sh = _num(g.get("scroll_height"))
    vh = _num((vp or {}).get("height"))
    if sh is None or not vh:
        return _r(spec, BLOCKED, why="没有页面高度证据")
    tol = float(spec.get("tol", DEFAULTS["fold_tol"]))
    ok = sh <= vh * tol
    return _r(spec, PASS if ok else FAIL, value=int(sh), target=f"≤{int(vh * tol)}px",
              why=("" if ok else f"{k}px 视口下页面高 {int(sh)}px，超出一屏（{int(vh)}px）"),
              detail={"viewport": k, "viewport_height": vh, "scroll_height": sh})


def _p_anchors_resolve(spec, measure, ctx):
    """锚点有没有落点。judge 手里只有图和事实块，而"href 指的 id 存不存在"在截图上
    根本看不出来 —— 这一条历史上全模型都写"无法确认"，从分母里掉出去（§4.2 第三态）。

    护栏 2（总纲 §3.5）：一个页内锚点都没有 → **fail**，不是 blocked。
    "扫过了，页面上没有目录"是证据；记 blocked 就等于奖励不做这个组件。
    真正的 blocked 只有一种：这一格量都没量到（老 schema 的 measure.json / 量的时候崩了）。
    """
    a = (measure or {}).get("anchors")
    if not isinstance(a, dict):
        return _r(spec, BLOCKED, why="没有锚点证据（measure schema < 3，重跑 step3 才有这一格）")
    if a.get("why"):
        return _r(spec, BLOCKED, why=f"锚点没量到：{a.get('why')}")

    scope = str(spec.get("scope") or "page").strip().lower()
    src = (a.get("in_nav") or {}) if scope == "nav" else a
    if scope == "nav" and not isinstance(a.get("in_nav"), dict):
        return _r(spec, BLOCKED, why="没有 nav 范围的锚点证据")

    n_hash = int(src.get("n_hash") or 0)
    n_res = int(src.get("n_resolved") or 0)
    n_ph = int(src.get("n_placeholder") or 0)
    # 占位 href="#" 算不合格的锚点：它形式上是链接，点下去不去任何板块。
    n_bad = max(0, n_hash - n_res) + n_ph
    min_links = int(spec.get("min_links", 2))
    max_broken = int(spec.get("max_broken", 0))
    where = "nav 内" if scope == "nav" else "页面里"

    if n_hash + n_ph < min_links:
        return _r(spec, FAIL, value=n_hash + n_ph, target=f"≥{min_links} 个页内锚点",
                  why=f"{where}只找到 {n_hash + n_ph} 个 href=\"#…\" 链接（要求 ≥{min_links}）",
                  detail=dict(src, scope=scope, n_ids=a.get("n_ids")))

    ok = n_bad <= max_broken
    return _r(spec, PASS if ok else FAIL, value=f"{n_res}/{n_hash + n_ph}",
              target=f"≤{max_broken} 个指不到落点",
              why=("" if ok else f"{where} {n_bad} 个锚点指不到落点"
                                 f"（{max(0, n_hash - n_res)} 个 id 不存在、{n_ph} 个是占位 #）"),
              detail=dict(src, scope=scope, n_ids=a.get("n_ids"), n_bad=n_bad))


PROBES = {
    "no_horizontal_overflow": _p_no_horizontal_overflow,
    "contrast_ratio": _p_contrast_ratio,
    "min_font_size": _p_min_font_size,
    "tap_target_min": _p_tap_target_min,
    "no_hover_only_affordance": _p_no_hover_only_affordance,
    "dom_changes_on_click": _p_dom_changes_on_click,
    "no_overflow_with_data": _p_no_overflow_with_data,
    "empty_state_present": _p_empty_state_present,
    "self_contained": _p_self_contained,
    "no_js_errors": _p_no_js_errors,
    "palette_delta_e": _p_palette_delta_e,
    "palette_follows_ref": _p_palette_follows_ref,
    "palette_unlike_ref": _p_palette_unlike_ref,
    "token_consistency": _p_token_consistency,
    "content_fits_fold": _p_content_fits_fold,
    "anchors_resolve": _p_anchors_resolve,
}
assert set(PROBES) == set(PROBE_SCHEMA), "PROBES 与 PROBE_SCHEMA 必须一一对应"


def run_probe(spec: dict, measure: Optional[dict], ctx: Optional[dict] = None) -> Dict[str, Any]:
    """跑一条规格。任何异常都收成 blocked —— 探针崩了不等于产出不合格。"""
    name = (spec or {}).get("probe")
    fn = PROBES.get(name)
    if fn is None:
        return _r(spec, BLOCKED, why=f"未知探针 {name!r}（不在 PROBE_SCHEMA 里）")
    if not measure:
        return _r(spec, BLOCKED, why="没有 measure.json")
    if not measure.get("ok"):
        return _r(spec, BLOCKED, why=f"测量失败: {measure.get('error') or '未知'}")
    try:
        return fn(spec, measure, ctx or {})
    except Exception as e:      # noqa: BLE001
        return _r(spec, BLOCKED, why=f"探针异常: {type(e).__name__}: {str(e)[:100]}")


def run_probes(specs: Sequence[dict], measure: Optional[dict],
               ctx: Optional[dict] = None) -> Dict[str, Any]:
    results = [run_probe(s, measure, ctx) for s in (specs or [])]
    n = len(results)
    n_pass = sum(1 for r in results if r["status"] == PASS)
    n_fail = sum(1 for r in results if r["status"] == FAIL)
    n_blocked = n - n_pass - n_fail
    return {
        "results": results,
        "summary": {
            "n": n, "n_pass": n_pass, "n_fail": n_fail, "n_blocked": n_blocked,
            # 覆盖率低说明证据没取到，这时候的通过率是不可比的，别拿去排行榜
            "coverage": round((n_pass + n_fail) / n, 3) if n else 0.0,
            "pass_rate": round(n_pass / (n_pass + n_fail), 3) if (n_pass + n_fail) else None,
        },
    }


def to_score_items(probe_results: Sequence[dict], pass_score: float = RAW_MAX,
                   fail_score: float = 0.0) -> List[Dict[str, Any]]:
    """折成 step4/step5 认的条目分。

    blocked 的条目 **score=None**（缺失），不是 0 —— 缺失在聚合层按"这条不进分母"处理，
    与 step4 里 judge 没给出判定的条目走同一条路。
    """
    out = []
    for r in probe_results or []:
        sc = {PASS: pass_score, FAIL: fail_score}.get(r["status"])
        out.append({
            "id": r.get("id"),
            "probe": r.get("probe"),
            "score": sc,
            "status": r["status"],
            "verdict": "程序判定：" + (r.get("why") or
                                   (f"达标（{r.get('value')} vs {r.get('target')}）"
                                    if r["status"] == PASS else "无证据")),
            "value": r.get("value"),
            "target": r.get("target"),
            "deterministic": True,
        })
    return out


def main() -> int:
    """调试：python -m utils.render.probes measure.json specs.json"""
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("measure")
    ap.add_argument("specs", nargs="?", default=None,
                    help="规格 JSON（list 或 {verifiable:[...]}）；省略则跑一套默认体检")
    ap.add_argument("--ctx", default=None, help="参考签名等上下文 JSON")
    args = ap.parse_args()
    with open(args.measure, "r", encoding="utf-8") as f:
        m = json.load(f)
    if args.specs:
        with open(args.specs, "r", encoding="utf-8") as f:
            raw = json.load(f)
        specs = raw.get("verifiable") if isinstance(raw, dict) else raw
    else:
        specs = [
            {"id": "rsp_1", "probe": "no_horizontal_overflow"},
            {"id": "a11y_1", "probe": "contrast_ratio", "min": 4.5},
            {"id": "a11y_2", "probe": "min_font_size", "min_px": 14},
            {"id": "a11y_3", "probe": "tap_target_min"},
            {"id": "a11y_4", "probe": "no_hover_only_affordance"},
            {"id": "int_1", "probe": "dom_changes_on_click"},
            {"id": "adv_1", "probe": "no_overflow_with_data", "fixture": "long_string_80ch"},
            {"id": "adv_2", "probe": "no_overflow_with_data", "fixture": "many_rows_300"},
            {"id": "adv_3", "probe": "empty_state_present"},
            {"id": "sc_1", "probe": "self_contained"},
            {"id": "js_1", "probe": "no_js_errors"},
            {"id": "tok_1", "probe": "token_consistency"},
        ]
    ctx = {}
    if args.ctx:
        with open(args.ctx, "r", encoding="utf-8") as f:
            ctx = json.load(f)
    out = run_probes(specs, m, ctx)
    for r in out["results"]:
        mark = {PASS: "✓", FAIL: "✗", BLOCKED: "·"}[r["status"]]
        print(f"{mark} {r['id']:<8} {r['probe']:<26} value={r['value']!s:<28} "
              f"{r['why'] or r['target'] or ''}")
    print(json.dumps(out["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
