# -*- coding: utf-8 -*-
"""五种分数口径 / 相邻名次可分性 / 方差分解 / 判定覆盖率 / §5 验收表 —— 全是纯函数。

对应 docs/设计总纲.md（引行号，别凭印象）：

| 规矩 | 出处 | 写反了会怎样（都不报错，只让分数悄悄失真） |
|---|---|---|
| 一次运行同时给出「多口径 + 分层 + 方差分解 + 相邻可分 + 误差」 | §3.7:682-688 | 缺一项就没法回答「区分度变好了没」 |
| 判「差距拉大了」= 跨度 **除以** 误差 | §3.7:682-688、§5:901-904 | 只报跨度会选出最差的口径（实测 strict 跨度最大 65.62、跨度/SE 最小 11.0、相邻可分最小 0.524） |
| 「测不了」算缺失、从分母剔出去 | §4.2:734,496 | 折成 0 = 给所有不含那个结构的页面凭空扣分，分数散开像有了天花板空间，其实全是噪声 |
| 主口径只保留 critical 阈值闸门 | §4.3:770-780 | 历史 admission gate 会把整题额外归零 |
| importance 全部等权 | §4.3:770-780 | 权重与 critical gate 重复表达重要性 |
| 深度权重 0.5 / 1.0 / 1.5 | §4.4:783-796 | 「准不准」和「有没有」同价，最难的一层白出 |
| 饱和度只作诊断，不按当前参评模型动态改分 | §4.4:783-796 | 参评集合一变，所有已有模型分数也会变，无法独立复现 |
| 五种口径同时输出，用途是效度检查 | §4.7:820-832 | 换口径就变名次 = 结论来自口径不是能力 |
| 方差分解的单元是 模型 × **单条条目** | §5:882-885 | 按样本级实现会报出 66% 并对着「明显高于 9.2%」判达标 —— 结论正好反了 |

为什么整块抽成纯函数：这些判定以前内联在 step5a_analyze.py 的大函数里（取分兜底在
``_extract_score``、饱和过滤靠原地改 checklist、深度权重在 ``_weighted_avg`` 里顺手
乘一下），变异测试钉不住 —— 把 0.5 和 1.5 对调、把 None 当 0，代码照样跑完、报表
照样出数。抽出来之后 tools/qa/check_report_calibers.py 能逐条钉住。

本模块**不读文件、不读 config、不写日志**：只吃「已经算好的分」和「条目元信息」，
返回值里 ``None`` 一律表示「数据不足」，永远不返回 0 来充数。
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 固定种子：同一批分数重跑必须给出同一个 SE / 同一个可分性，否则报表不能做跨轮次
# diff（「这轮可分性 0.94 上轮 0.96」会分不清是题变了还是抽样变了）。
# 必须与 utils/score/axes.py:31 的 SEED 一致 —— check_report_calibers.py 会钉这一条。
SEED = 20260824

# 判分档位的唯一定义点（§4.8:857）。judge 按 0-1-2-3 四档打分（prompts/score_checklist.txt），
# 报表一律 0-100，换算只有这一个系数。
#
# 为什么必须是常量而不是各处写字面量：0-10 → 0-3 那次改制就是这么半途而废的 ——
# scoring.py / scoring_calibers.py 改成了 ×100/3，step5a_analyze.py 里三处 ×10 没人动，
# 于是维度表和 detail 切片的满分悄悄变成 30 而不是 100，报表照样跑完、数字照样像分数。
# 这类错误不报警，只让结论低 3.33 倍。改档位数就改这一行。
RAW_MAX = 3.0
RAW_TO_100 = 100.0 / RAW_MAX

# 每个 depth 档位**实际**能拿到的最高原始分。
# 这不是我们在聚分时额外加的限制 —— 是 prompts/score_checklist.txt §depth 天花板
# 那一节写进 judge prompt 的硬规则：existence / correctness 最高 2，只有 precision
# 能到 3（3 分被定义成「两侧各写出可比数值且完全相等」，而一句 existence 问句结构上
# 产生不出那种证据）。天花板本身要留着，它在真实约束 judge 手松。
#
# 但**聚分时必须按这张表归一，不能一律 ÷3**。一律 ÷3 的后果实测过：
#   一条 correctness 条目完全做对 = 2/3 = 66.7 分，量表对外说的却是「满分 100」。
#   gpt-5.5 在 vref_002 上，÷3 得 69.58、按本表归一得 82.52 —— 12.9 分的差额全是
#   天花板，跟能力无关。而且 5 个模型 × 16 条 correctness 里有 67 条精确停在 2，
#   也就是说这 12.9 分不是个别条目的边角，是大多数条目的实际落点。
# 还有一层重复计算：depth 已经通过 depth_weights(0.5/1.0/1.5) 表达过一次「哪层更值钱」
# 的偏好，再用天花板压一遍分子就是同一个偏好数了两遍。归一之后偏好只表达一次。
#
# 归一对排名的影响：全模型都顶到天花板的条目上，归一是个单调常数，排名不动；
# 有方差的条目上，差异被放大 3/2 = 1.5 倍，方向不变 —— 区分度是增强不是稀释。
DEPTH_CAP = {"existence": 2.0, "correctness": 2.0, "precision": 3.0}


def depth_cap(depth: Any, caps: Optional[Dict[str, float]] = None) -> float:
    """这条条目**实际**最高能拿几分。

    depth 缺字段（老条目没打）按 RAW_MAX 兜底 —— 不知道天花板在哪就按没有天花板算，
    宁可少送分也不要凭空把一条 precision 条目的分子放大 1.5 倍。
    """
    tbl = DEPTH_CAP if caps is None else caps
    try:
        c = float(tbl.get(str(depth or ""), RAW_MAX))
    except (TypeError, ValueError):
        return RAW_MAX
    # 配错成 0 会让 raw_to_100 变成除零；配成负数会让分数翻符号。两种都夹回来。
    return c if 0.0 < c <= RAW_MAX else RAW_MAX


def raw_to_100(v: float, depth: Any = None, *,
               cap_normalize: bool = False,
               caps: Optional[Dict[str, float]] = None) -> float:
    """一条条目的原始分 → 0-100。**0-3 折百分的唯一出口。**

    ``cap_normalize=False`` 时就是老口径 ``v * RAW_TO_100``（一律 ÷3），留给 legacy
    口径 —— 它唯一的用途是跟历史轮次对比，换了折算方式这个用途就没了。
    ``cap_normalize=True`` 时按这条自己的天花板归一：**"做对了"就是这一条的满分。**

    默认 False 是有意的：新增一个聚分点的人如果忘了传，得到的是**老行为**，
    报表里那一列会跟主口径明显对不上（几十分的差），一眼能发现；反过来默认 True
    的话，legacy 列会被悄悄改掉而没人看得出来。
    """
    cap = depth_cap(depth, caps) if cap_normalize else RAW_MAX
    # 夹到 100：judge 若违反天花板（给一条 existence 条目打了 3），归一后会是 150，
    # 那一条会静默地把整道题的加权分抬到 100 以上 —— 报表上是个看起来正常的高分。
    # 全量实测 1725 条判分里越界 0 条，所以这只是防线；真出现了要在 QA 门看到
    # （tools/qa/check_report_calibers.py 的「天花板归一」组会扫真实分数文件）。
    return min(float(v) / cap * 100.0, 100.0)

# 深度权重的唯一默认值（§4.4:783-796）。config 里给了就用 config 的，这里是兜底。
DEPTH_WEIGHTS_DEFAULT = {"existence": 0.5, "correctness": 1.0, "precision": 1.5}

# 旧口径专用的方面权重（§4.7:820-832「方面加权」）。冻结在 v3 的历史值上 ——
# 旧口径唯一的用途是跟历史轮次对比，权重一变这个用途就没了。
LEGACY_CATEGORY_WEIGHTS_DEFAULT = {
    "Structure": 2.5, "Content": 1.0, "Navigation": 1.0, "Interaction": 1.0,
    "Layout": 1.0, "Visual": 0.6, "ComponentStyle": 0.9,
}


# ── 口径声明表（§4.7:820-832 那张文档表在代码里的唯一落点）────────────────────
# 改口径定义必须改这张表，不许在 analyze() 里加 if 分支：分支一加，「谁吃饱和过滤、
# 谁乘深度权重」就散在几百行里，下一个人新增第六个口径会不明不白地继承别人的过滤
# （旧口径就是这么被污染的，见 step5 的历史写法「原地改 checklist」）。
CALIBERS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "legacy", "record_key": "score", "name": "旧口径 score(v3)",
        "use_depth": False, "exclude_critical": False,
        "apply_saturation": False, "category_weights": "legacy",
        "apply_unverifiable_filter": False,
        # cap_normalize=False：这一列的用途是跟历史轮次对比，折算方式一变用途就没了。
        "cap_normalize": False,
        "purpose": "只用来跟历史轮次对比，不当结论（§4.7:820-832）",
    },
    {
        "key": "main", "record_key": "score_v4", "name": "主口径 score_v4",
        "use_depth": True, "exclude_critical": False,
        "apply_saturation": False, "category_weights": "flat",
        "apply_unverifiable_filter": True,
        "cap_normalize": True,
        "purpose": "结论用这个：importance 等权 + critical-only 阈值闸门 + 深度权重；饱和度只作诊断（§4.7:820-832）",
    },
    {
        "key": "precision", "record_key": "precision", "name": "只看准不准",
        "use_depth": False, "exclude_critical": False,
        "apply_saturation": False, "category_weights": "flat",
        "apply_unverifiable_filter": True,
        "cap_normalize": True,
        "purpose": "只算最深那一层条目，区分度最高的切片（§4.7:820-832）",
    },
    {
        "key": "strict", "record_key": "strict", "name": "严格口径",
        "use_depth": True, "exclude_critical": False,
        "apply_saturation": False, "category_weights": "flat",
        "apply_unverifiable_filter": True,
        "cap_normalize": True,
        "purpose": "对主口径的每题得分做阶梯切割，压天花板、检验排序稳不稳（§4.7:820-832）",
    },
    {
        "key": "pairwise", "record_key": "pairwise", "name": "两两比较分",
        "use_depth": True, "exclude_critical": False,
        "apply_saturation": False, "category_weights": "flat",
        "apply_unverifiable_filter": True,
        "cap_normalize": True,
        "purpose": "从「同一道题上谁赢」算出的隐分，不受题目难易分布影响（§4.7:820-832）",
    },
)

CALIBER_BY_KEY = {c["key"]: c for c in CALIBERS}
RECORD_KEYS = tuple(c["record_key"] for c in CALIBERS)


def caliber(key: str) -> Dict[str, Any]:
    """按 key 取口径声明。拿不到就抛 —— 悄悄退回默认口径等于换了结论。"""
    if key not in CALIBER_BY_KEY:
        raise KeyError(f"未声明的口径 {key!r}；CALIBERS 里只有 {sorted(CALIBER_BY_KEY)}")
    return CALIBER_BY_KEY[key]


# ── 一条条目值多少分 ────────────────────────────────────────────────────────
# judge 自己宣布「这条测不了」时用的措辞。第三态的识别只靠这一处（§4.2:734,496）。
# 词表是白名单：judge 的 observed 是自由文本，宁可漏认一种新说法（那条继续按原分
# 计入，和改动前一样），也不要靠「像是没判出来」的模糊匹配把有证据的条目扫进来。
UNVERIFIABLE_MARKS = (
    "无法确认", "无法验证", "无法判断", "未能确认", "不可测", "测不了",
)

# observed 开头常见的装饰字符，判定前先剥掉（judge 爱写「（无法确认）」「**无法确认**」）。
_UNVERIFIABLE_STRIP = " \t\r\n\"'“「（(【[*·-—。：:"


def is_unverifiable(item: Any) -> bool:
    """judge 自己说了这条**没法判**吗 —— 只认 ``observed`` 的**开头**。

    只认开头，不做全文搜索。实测这一批 854 条评分条目：

    - 开头就是这句的 **69 条（8.1%）**，分数分布 ``{1: 27, 2: 42}`` —— 全部压在
      地板上。judge 已经写明「测不了」，分数却照样进分母，正是 §4.2:736 点名的
      「把没测说成没做到」：所有拿不到证据的条目替模型背了一次扣分。
    - 句中才提到的另有 14 条，分数 2-6，observed 里带着**真值**（例如「帧 15/16
      可见图表标题…事实块未提供 alt 属性，无法确认 alt 文案」）。那条图表是真看到
      了，「无法确认」只修饰其中一个子项。这些必须继续计分 —— 全文搜索会把它们
      一起剔出去，等于把已经量到的证据扔了，分母缩水 11.8% 而不是 8.1%。

    判定只读 judge 写的 observed 文本，不看分数：分数低可能是真的做得差。
    """
    if not isinstance(item, dict):
        return False
    txt = item.get("observed")
    if not isinstance(txt, str):
        return False
    return txt.lstrip(_UNVERIFIABLE_STRIP).startswith(UNVERIFIABLE_MARKS)


def item_value(item: Any, *, drop_unverifiable: bool = True) -> Optional[float]:
    """一条评分条目值多少分：判出来了返回 0-3 的原始分，判不出来返回 **None**。

    **整个 step5 里只有这一个函数有权把条目变成数字。**

    None = 「测不了」（§4.2:734）：探针没证据、judge 没判、字段坏了、score 不是数。
    调用方必须跳过它、**不进分母**。

    老实现（step5a_analyze.py:119-129）在这里兜了两层底：score 解析不了就看 answer，
    answer 不是 yes 就 ``return 0.0``。那条兜底正是 §4.2:736 点名的静默通道 ——
    「把没测说成没做到」：所有不含那个结构的页面被凭空扣分，分数立刻散开、看着像
    有了天花板空间，可那跟模型能力无关，只跟「这道题恰好有没有那个结构」有关。
    所以这里宁可让分母变小，也绝不返回 0。

    drop_unverifiable
        judge 在 observed 里明写「无法确认」的条目也返回 None（见 is_unverifiable）。
        **默认开**，是刻意的 opt-out 而不是 opt-in：这个函数是唯一的取分口，将来
        新增第六个口径的人忘了传参时，应该自动吃到这条过滤，而不是自动漏掉它。
        只有旧口径显式传 ``False`` —— 它唯一的用途是跟历史轮次对比，那一轮没有
        这条过滤（见 CALIBERS 的 apply_unverifiable_filter）。
    """
    if not isinstance(item, dict):
        return None
    if drop_unverifiable and is_unverifiable(item):
        # judge 判过了，判出来的结论是「测不了」→ 第三态，不进分母（§4.2:734）。
        # 这里返回 None 而不是原分，是这条过滤唯一生效的地方：分数字段本身照样留在
        # 记录里给报表当证据（§4.4:783-796 的同一个道理 —— 剔出计分，不剔出审计）。
        return None
    s = item.get("score", None)
    if s is None or isinstance(s, bool):
        # bool 也当没判出来：True 会被 float() 变成 1.0 分，那是编码事故不是判定
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def depth_weight(depth: Any, weights: Optional[Dict[str, float]] = None) -> float:
    """深度权重：有没有 0.5 / 对不对 1.0 / 准不准 1.5（§4.4:783-796）。

    depth 缺字段（老条目没打）按 1.0 算 —— 既不白送也不白扣。

    调成全 1.0、或者把 0.5 与 1.5 对调，都不会报错：只是「准不准」这一层
    ——唯一真有区分度的那层——变得和「有没有」同价甚至更便宜，§4.4 想买的
    区分度一分钱没买到，而报表上一切正常。
    """
    tbl = DEPTH_WEIGHTS_DEFAULT if weights is None else weights
    try:
        return float(tbl.get(str(depth or ""), 1.0))
    except (TypeError, ValueError):
        return 1.0


def _importance_of(iid: Any, cl_item: dict,
                   importance_override: Optional[Dict[Any, str]]) -> str:
    if importance_override and iid in importance_override:
        return importance_override[iid]
    return (cl_item or {}).get("importance", "medium")


# ── 一道题的加权分 ──────────────────────────────────────────────────────────
def weighted_sample_score(items: Iterable[dict], cl_map: Dict[Any, dict], *,
                          importance_weights: Dict[str, float],
                          category_weights: Optional[Dict[str, float]] = None,
                          exclude_critical: bool = False,
                          use_depth: bool = False,
                          depth_weights: Optional[Dict[str, float]] = None,
                          saturated_ids: Optional[Iterable[Any]] = None,
                          importance_override: Optional[Dict[Any, str]] = None,
                          drop_unverifiable: bool = True,
                          cap_normalize: bool = False,
                          ) -> Dict[str, Any]:
    """一道题的加权分（0-100）+ 四个可审计计数。

    返回 ``{'score': float|None, 'n_used', 'n_missing', 'n_saturated_skipped',
    'n_unverifiable'}``。
    一条都没用上时 score 是 **None**，不是 0 —— 「这题 0 分」和「这题没判过」是
    两件事，折成 0 会把没数据的模型排到榜底、看起来像它很差（§4.2:736）。
    四个计数是「分母对不对」的唯一可审计出口：报表里 n_missing 突然变大，说明
    是我们没量到，不是模型变差了（§5:897-898）。

    saturated_ids
        本轮被判「饱和」（全模型都过、方差近零）的条目 id，**作为参数传进来**。
        旧写法是在 main() 里原地改 checklist（step5:899），坑在于一份被改过的
        checklist 会被后面**所有**口径共用：旧口径跟着吃了饱和过滤，就再也不能
        跟历史轮次比了（实测 score 从 96.53 掉到 94.31，而且 2/3 名互换）。
        参数化之后「谁吃过滤」写在 CALIBERS 表里，一眼可查。
    importance_override
        饱和过滤的另一半：被降到 low/medium 的那些条目的新 importance。
        和 saturated_ids 一样只对主口径侧传（§4.4:783-796 的过滤只属于主口径）。
    cap_normalize
        按每条条目自己的 depth 天花板折百分（见 DEPTH_CAP）。**主口径要开、legacy
        要关**：开了之后「完全做对」就是这一条的满分，量表不再对外说满分 100 却有
        一半条目摸不到；关着是老行为（一律 ÷3），legacy 靠它保住跟历史轮次的可比性。
        谁开谁关写在 CALIBERS 表的 ``cap_normalize`` 字段里，不许在调用处加 if。
    drop_unverifiable
        judge 自己写了「无法确认」的条目剔出分母（§4.2:734，见 is_unverifiable）。
        计数记在 ``n_unverifiable``，**和 n_missing 分开** —— 两者都不进分母，但
        n_missing 是「我们没量到 / judge 没判回来」，该去修探针或补跑判分；
        n_unverifiable 是「judge 判了、结论是拿不到证据」，该去看判分材料够不够
        （实测很大一部分是因为事实块里没有对应的量）。混成一个数就分不出来了。
    """
    sat = set(saturated_ids or ())
    w_total = 0.0
    s_total = 0.0
    n_used = n_missing = n_sat = n_unver = 0
    for item in items or ():
        iid = item.get("id")
        cl_item = cl_map.get(iid) or {}
        imp = _importance_of(iid, cl_item, importance_override)
        if imp == "zero":
            # importance=zero 不占权重（准入闸/元条目走别的口子），§4.3:770-780
            continue
        if exclude_critical and imp == "critical":
            # 保留给 gate-only 消融；正式主口径的 critical 以等权方式参与基础分母。
            continue
        if iid in sat:
            n_sat += 1
            continue
        if drop_unverifiable and is_unverifiable(item):
            # judge 写明了测不了 → 第三态，单独记一笔再跳过（不往 n_missing 里堆）。
            n_unver += 1
            continue
        v = item_value(item, drop_unverifiable=drop_unverifiable)
        if v is None:
            n_missing += 1          # 测不了 → 不进分母（§4.2:734），也不算 0 分
            continue
        w = float(importance_weights.get(imp, 2))
        if category_weights:
            w *= float(category_weights.get(cl_item.get("category", "General"), 1.0))
        if use_depth:
            w *= depth_weight(cl_item.get("depth"), depth_weights)
        if w <= 0:
            continue
        n_used += 1
        w_total += w
        # 0-3 → 0-100，按这条自己的 depth 天花板归一（见 DEPTH_CAP 那段注释）。
        s_total += raw_to_100(v, cl_item.get("depth"),
                              cap_normalize=cap_normalize) * w
    return {
        "score": (s_total / w_total) if (n_used and w_total > 0) else None,
        "n_used": n_used, "n_missing": n_missing, "n_saturated_skipped": n_sat,
        "n_unverifiable": n_unver,
    }


def depth_slice_score(items: Iterable[dict], cl_map: Dict[Any, dict], depth: str, *,
                      saturated_ids: Optional[Iterable[Any]] = None,
                      importance_override: Optional[Dict[Any, str]] = None,
                      drop_unverifiable: bool = True,
                      cap_normalize: bool = False,
                      ) -> Dict[str, Any]:
    """只看某一层深度的条目（「只看准不准」口径，§4.7:820-832）。不加权、直接取均值。

    这一层没有条目时 score=None（不是 0.0）：老实现返回 0.0 再靠调用方另外问一次
    ``_has_depth``，两处只要有一处忘了问，没有 precision 条目的样本就凭空得 0 分。

    ``cap_normalize`` 对这个函数尤其要紧：一个切片里所有条目同 depth、同天花板，
    不归一的话 existence 切片满分只有 66.7、precision 切片满分 100，**两个切片的
    数字没法并排读**，而报表恰好就是并排列出来的。归一之后每一列都是「这一层做对了
    百分之多少」，可比。
    """
    sat = set(saturated_ids or ())
    vals: List[float] = []
    n_missing = n_sat = n_unver = 0
    for item in items or ():
        iid = item.get("id")
        cl_item = cl_map.get(iid) or {}
        if _importance_of(iid, cl_item, importance_override) == "zero":
            continue
        if (cl_item.get("depth") or "") != depth:
            continue
        if iid in sat:
            n_sat += 1
            continue
        if drop_unverifiable and is_unverifiable(item):
            n_unver += 1            # judge 说测不了，单列记数（§4.2:734）
            continue
        v = item_value(item, drop_unverifiable=drop_unverifiable)
        if v is None:
            n_missing += 1
            continue
        vals.append(v)
    return {
        # 切片内 depth 恒等于入参 depth，直接拿它查天花板。
        "score": (raw_to_100(sum(vals) / len(vals), depth,
                             cap_normalize=cap_normalize) if vals else None),
        "n_used": len(vals), "n_missing": n_missing, "n_saturated_skipped": n_sat,
        "n_unverifiable": n_unver,
    }


def critical_gate(items: Iterable[dict], cl_map: Dict[Any, dict],
                  threshold: float, *,
                  importance_override: Optional[Dict[Any, str]] = None,
                  cap_normalize: bool = False,
                  ) -> Optional[Dict[str, Any]]:
    """关键项阈值闸（§4.3:770-780）。

    返回 ``{'critical_avg': float, 'capped': bool, 'n': int}``；一条 critical 都
    没有（或全部测不了）时返回 **None**，不是 0 —— 判成 0 就等于把「这题没有关键项」
    当成「关键项全崩」，整道题被压成 0 分。

    ``capped=True`` 只表示「均分低于阈值」，压分动作由 gated_score 执行。
    ``cap_normalize`` 必须跟调用方那个总分的口径一致 —— 见下面循环里的注释。
    主口径让 critical 与其余 importance 等权参与基础均分。这里额外负责低于阈值时
    压顶，防止外围细节掩盖核心失败；达标时不再二次改分。
    """
    vals = []
    for item in items or ():
        iid = item.get("id")
        cl_item = cl_map.get(iid) or {}
        if _importance_of(iid, cl_item, importance_override) != "critical":
            continue
        v = item_value(item)
        if v is None:
            continue                 # 关键项测不了也是缺失，不许当 0（§4.2:736）。
                                     # judge 写「无法确认」的关键项也走这里（item_value
                                     # 默认吃过滤）：不过滤的话它们会把 critical_avg 拉低，
                                     # 而这个均分会通过 gated_score 直接压顶整道题的分。
        # **必须跟被它压的那个总分同一把尺子。** 主口径开了 cap_normalize、这里
        # 忘了开的话：总分被抬到 82，critical_avg 还停在 69 的老刻度，于是
        # gated_score 会把一大批本来该过闸的样本压下去 —— 报表照样跑完，只是闸门
        # 悄悄变严了。gate_threshold(70) 也是照 0-100 定的，两边刻度必须一致。
        vals.append(raw_to_100(v, cl_item.get("depth"),
                              cap_normalize=cap_normalize))
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return {"critical_avg": avg, "capped": avg < float(threshold), "n": len(vals)}


def gated_score(score: Optional[float], gate: Optional[dict]) -> Optional[float]:
    """把关键项闸门作用到总分上：只往下压，从不往上抬（§4.3:770-780）。"""
    if score is None:
        return None
    if gate and gate.get("capped") and gate.get("critical_avg") is not None:
        return min(float(score), float(gate["critical_avg"]))
    return float(score)


def gate_failed(items: Iterable[dict], cl_map: Dict[Any, dict], *,
                extra_gate_ids: Optional[Iterable[Any]] = None) -> bool:
    """准入闸：任何 gate 条目**判出来是 0 分** → 整道题归零（§4.3:770-780）。

    两处必须分清，写反了都是静默失真：

    - 闸门条目 **测不了**（item_value 是 None）**不算没过**。老实现用
      ``_extract_score(item) == 0.0`` 判，而那个函数把缺失也返回 0.0 ——
      于是「渲染探针崩了」会把整道题打成 0 分（§4.2:736 的最坏情形）。
      judge 写「无法确认」的闸门条目同理：item_value 默认把它变成 None，
      于是下面那一句 ``v is not None`` 自动不会开闸。
    - 闸门条目**不占权重**，但也不能直接踢出计分：人人都过的条目一旦不进总分，
      砸掉它就完全免费（utils/score/axes.py:328-339 的教训）。所以过了不加分、崩了压总分。

    extra_gate_ids 是本轮被饱和过滤降权后**转成准入闸**的条目（§4.4:783-796 允许这么转）。
    它单独传而不是写回 checklist，为的是旧口径不吃这批新闸门 —— 旧口径要跟历史比，
    历史那轮没有这批闸门。
    """
    extra = set(extra_gate_ids or ())
    for item in items or ():
        iid = item.get("id")
        cl_item = cl_map.get(iid) or {}
        if not (cl_item.get("gate") or iid in extra):
            continue
        v = item_value(item)
        if v is not None and v == 0.0:
            return True
    return False


def strict_steps(base: Optional[float], cuts: Sequence[float],
                 values: Sequence[float]) -> Optional[float]:
    """阶梯切割（§4.7:820-832）：压高分区，检验排序稳不稳。

    base 是 None（这题没判过）时返回 None，不是最低档 —— 把「没判过」映射成 0 分
    就是又一条「测不了当没做到」的通道。
    """
    if base is None:
        return None
    for cut, val in zip(cuts, values):
        if base >= cut:
            return float(val)
    return float(values[-1])


# ── 第五口径：两两比较分 ────────────────────────────────────────────────────
def pairwise_sample_points(per_sample_by_model: Dict[str, Dict[Any, float]],
                           ) -> Dict[str, Dict[Any, float]]:
    """「两两比较分」的**逐题分**：这道题上打赢了多少比例的对手 × 100（§4.7:820-832）。

    平手算 0.5。同一道题上只有一个模型有分时**不出分**（不是 100）——
    「没人跟它比」和「赢了所有人」是两件事，混起来会让只跑了少数样本的模型贴顶。

    为什么要落到逐题分而不是只出一个模型级 ELO：SE / 相邻可分 / 方差分解三套机器
    都吃 ``{模型: {题: 分}}``。只出模型级 ELO 的话，第五口径永远只有跨度没有误差，
    正是 §3.7:682-688 点名的那个坑。实测这套逐题分算出的名次与 Bradley-Terry ELO 完全一致。
    """
    by_sid: Dict[Any, List[Tuple[str, float]]] = defaultdict(list)
    for mk, per_sid in (per_sample_by_model or {}).items():
        for sid, v in (per_sid or {}).items():
            if v is not None:
                by_sid[sid].append((mk, float(v)))
    out: Dict[str, Dict[Any, float]] = defaultdict(dict)
    for sid, pairs in by_sid.items():
        if len(pairs) < 2:
            continue
        for mk, v in pairs:
            wins = 0.0
            for mk2, v2 in pairs:
                if mk2 == mk:
                    continue
                wins += 1.0 if v > v2 else (0.5 if v == v2 else 0.0)
            out[mk][sid] = wins / (len(pairs) - 1) * 100.0
    return dict(out)


# ── Agent 榜单的破约剔除（§3.4:414）────────────────────────────────────────────
def filter_untainted(records: Iterable[dict], model_key: str,
                     taint: Optional[Iterable[Tuple[Any, str]]] = None,
                     ) -> Dict[str, Any]:
    """破约（harness.tainted）的样本**不进 Agent 榜单**，单独统计报出来（§3.4:414）。

    返回 ``{'kept': [...], 'n_tainted': int, 'tainted_sids': [...]}``。

    两种错法，方向相反，都不会报错：

    - **不剔**：产物可能直接抄了参考像素（工作目录里出现过参考图），它的分和别人
      不可比，混进 Agent 均分就是把「看见了答案」算成「改得动」。
    - **记 0 分**：那是把「这条不可比」说成「它做得极差」，会人为拉低 Agent 均分，
      令 §1.3:91 的配对差值混入排除规则的影响。

    破约判据本身分壳（§3.4:459：codex 会把参考图内联进首条消息，按次数数会把每条
    都判破约），那套逻辑在 utils/answer/harness.py 里；报表侧只消费 ``harness.tainted``
    这个布尔，**绝不在这里重算破约**。
    """
    bad = {(sid, mk) for sid, mk in (taint or ())}
    kept, dropped = [], []
    for r in records or ():
        if (r.get("sample_id"), model_key) in bad:
            dropped.append(r.get("sample_id"))
        else:
            kept.append(r)
    return {"kept": kept, "n_tainted": len(dropped), "tainted_sids": dropped}


# ── 相邻名次可分性（§5:900-904）──────────────────────────────────────────────
def adjacent_separability(per_sample_by_model: Dict[str, Dict[Any, float]], *,
                          n: int = 2000, seed: int = SEED, top_k: int = 3,
                          ) -> Optional[Dict[str, Any]]:
    """**重采样题目** → 每次重算榜 → 统计相邻两名谁在上面的概率（§5:900）。

    重采样的对象必须是**题目**，不是模型内部的分数：题目才是这个基准的抽样单元，
    「换一批题会不会换名次」才是我们要问的问题。改成在模型内部重采样分数会把
    题目难易的方差算成模型的不确定度，可分性会被系统性低估（而且低估得很稳定，
    看不出来）。

    返回值把 ``range`` / ``mean_se`` / ``range_over_se`` / ``min_p`` / ``median_p``
    **捆在同一个 dict 里**：这样「只报跨度」需要主动删字段，而不是顺手就写成了。
    §5:901-903 那次教训就是只看跨度栽的 —— 换口径把最高分从 96 压到 88，看着像压低
    了天花板，可相邻名次一点没更可分，因为跨度和误差一起缩小了。

    另外单独报 top-3 段（``top_min_p``）：§5:893 说的是「主要名次段 ≥0.95」，
    而实测中下段常常轻松达标、前几名完全分不开，只报全局最小值会把这件事平均掉。

    模型 < 2 或题目 < 2 → 返回 **None**（数据不足），不是 0 —— 返回 0 会被读成
    「完全不可分」，那是结论，不是「没数据」。
    """
    models = [m for m, d in (per_sample_by_model or {}).items()
              if any(v is not None for v in (d or {}).values())]
    if len(models) < 2:
        return None
    sids = sorted({sid for m in models for sid, v in per_sample_by_model[m].items()
                   if v is not None}, key=lambda x: (str(type(x)), x))
    k = len(sids)
    if k < 2:
        return None

    rows: List[List[Optional[float]]] = []
    for m in models:
        d = per_sample_by_model[m]
        rows.append([None if d.get(sid) is None else float(d[sid]) for sid in sids])
    dense = all(v is not None for row in rows for v in row)

    def _mean(row: List[Optional[float]], idxs: Sequence[int]) -> Optional[float]:
        if dense:
            return sum(row[i] for i in idxs) / len(idxs)
        s = 0.0
        c = 0
        for i in idxs:
            v = row[i]
            if v is not None:
                s += v
                c += 1
        return s / c if c else None

    point = [_mean(row, range(k)) for row in rows]
    order_idx = sorted(range(len(models)),
                       key=lambda i: (-(point[i] if point[i] is not None else -1e18),
                                      models[i]))
    order = [models[i] for i in order_idx]

    rnd = random.Random(seed)
    boot: List[List[Optional[float]]] = [[] for _ in models]
    wins = [0] * max(0, len(models) - 1)
    valid = [0] * max(0, len(models) - 1)
    for _ in range(n):
        idxs = [rnd.randrange(k) for _ in range(k)]
        means = [_mean(row, idxs) for row in rows]
        for i, mu in enumerate(means):
            boot[i].append(mu)
        for j in range(len(order_idx) - 1):
            a, b = means[order_idx[j]], means[order_idx[j + 1]]
            if a is None or b is None:
                continue
            valid[j] += 1
            if a > b:
                wins[j] += 1
            elif a == b:
                wins[j] += 0  # 平手不算「同一个在上面」，按不可分算，宁严不宽

    ses: List[float] = []
    for col in boot:
        vals = [v for v in col if v is not None]
        if len(vals) < 2:
            continue
        mu = sum(vals) / len(vals)
        ses.append((sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5)
    mean_se = sum(ses) / len(ses) if ses else None

    pts = [p for p in point if p is not None]
    rng = (max(pts) - min(pts)) if len(pts) >= 2 else None

    pairs = []
    for j in range(len(order) - 1):
        p = (wins[j] / valid[j]) if valid[j] else None
        pairs.append({"above": order[j], "below": order[j + 1], "p": p,
                      "n_draws": valid[j]})
    ps = [d["p"] for d in pairs if d["p"] is not None]
    ps_sorted = sorted(ps)
    top_ps = [d["p"] for d in pairs[: max(0, top_k - 1)] if d["p"] is not None]
    return {
        "order": order,
        "means": {models[i]: point[i] for i in range(len(models))},
        "pairs": pairs,
        "n_pairs": len(pairs),
        "min_p": min(ps) if ps else None,
        "median_p": _median(ps_sorted) if ps else None,
        "n_ok": sum(1 for p in ps if p >= 0.95),
        "top_k": top_k,
        "top_min_p": min(top_ps) if top_ps else None,
        "mean_se": mean_se,
        "range": rng,
        "range_over_se": (rng / mean_se) if (rng is not None and mean_se) else None,
        "n_items": k,
        "n_models": len(models),
        "bootstrap_n": n,
    }


def _median(sorted_vals: Sequence[float]) -> Optional[float]:
    v = sorted(sorted_vals)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def median(vals: Iterable[Optional[float]]) -> Optional[float]:
    """中位数；一个数都没有就 None（§5:899 的通过率中位数要用）。"""
    v = sorted(x for x in (vals or []) if x is not None)
    return _median(v)


# ── 方差分解（§5:882-885）──────────────────────────────────────────────────
def variance_decomposition(cells: Dict[Tuple[Any, Tuple[Any, Any]], float],
                           ) -> Optional[Dict[str, Any]]:
    """模型 / 题目 / 残差 三份方差占比；模型那一份就是信噪比（§5:882-883）。

    cells 的 key **必须**是 ``(model, (sample_id, item_id))`` —— 分解单元是
    「模型 × **单条评分条目**」，不是「模型 × 题」。这是本文件最容易做错、做错了
    还完全看不出来的地方：同一批数据按样本级分解给 66-73%，按条目级给 11.8%
    （饱和过滤前）/ 20.3%（过滤后）。只有条目级和 §5:883 那个 9.2% 基线同一个量级，
    也只有条目级能复现 §5:894 那条机制 —— 收益几乎全部来自扔掉不带信息的条目，
    不是来自换指标（文档实测 9.2% → 18.0%）。

    按样本级实现的后果：报表会写「模型方差占比 66%」，对着 §5:894「明显高于 9.2%」
    直接判达标，一个彻底反过来的结论，而且代码跑得通、没人会发现。所以非法 key
    直接抛 ValueError，**不做兼容**。

    单元格数 < 2、或者总方差为 0（所有条目同分）→ 返回 None（数据不足）。
    """
    if not cells:
        return None
    by_model: Dict[Any, List[float]] = defaultdict(list)
    by_item: Dict[Tuple[Any, Any], List[float]] = defaultdict(list)
    vals: List[float] = []
    for key, v in cells.items():
        if (not isinstance(key, tuple)) or len(key) != 2:
            raise ValueError(
                f"variance_decomposition 的 cell key 必须是 (model, (sample_id, item_id))，"
                f"收到 {key!r} —— 见本函数 docstring：样本级分解会把信噪比报成 66%"
            )
        model, item_key = key
        if (not isinstance(item_key, tuple)) or len(item_key) != 2:
            raise ValueError(
                f"variance_decomposition 的条目键必须是 (sample_id, item_id) 二元组，"
                f"收到 {item_key!r} —— 单元是「模型 × 单条条目」，不是「模型 × 题」（§5:882）"
            )
        if v is None:
            continue                # 测不了不进分解（§4.2:734）
        fv = float(v)
        vals.append(fv)
        by_model[model].append(fv)
        by_item[item_key].append(fv)
    if len(vals) < 2:
        return None
    grand = sum(vals) / len(vals)
    ss_total = sum((v - grand) ** 2 for v in vals)
    if ss_total <= 0:
        return None
    ss_model = sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in by_model.values())
    ss_item = sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in by_item.values())
    ss_resid = max(0.0, ss_total - ss_model - ss_item)
    return {
        "model_pct": ss_model / ss_total * 100.0,
        "item_pct": ss_item / ss_total * 100.0,
        "resid_pct": ss_resid / ss_total * 100.0,
        "n_models": len(by_model), "n_items": len(by_item), "n_cells": len(vals),
    }


# ── 判定覆盖率（§4.2 / §5:896,590-591）──────────────────────────────────────
def coverage_from_verify_summaries(rows: Iterable[dict]) -> Optional[Dict[str, Any]]:
    """判定覆盖率：真的判出了过/不过的比例（§5:897）。

    吃**已经读出来的** summary 列表（``{'n','n_blocked','probes':{probe:{'n','n_blocked'}}}``），
    自己不 glob 目录 —— 这样质检脚本能用手造数据钉住它，不用摆一棵假的 results 树。

    覆盖率低必须读成「**我们**没量到」，不是「模型没做到」（§4.2:736）：
    blocked 太多时分数会散开、看着像有了天花板空间，那是噪声。所以还要按探针
    分别报 —— 全局 0.98 可以盖住某个探针塌到 0.80（实测 contrast_ratio 就是 0.799），
    不按探针报的话，没人会去修那个探针。

    rows 为空 → None（数据不足），不是 0（0 会被读成「什么都没判出来」）。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return None
    n = n_blocked = 0
    per_probe: Dict[str, Dict[str, int]] = defaultdict(lambda: {"n": 0, "n_blocked": 0})
    for r in rows:
        n += int(r.get("n") or 0)
        n_blocked += int(r.get("n_blocked") or 0)
        for probe, agg in (r.get("probes") or {}).items():
            per_probe[probe]["n"] += int((agg or {}).get("n") or 0)
            per_probe[probe]["n_blocked"] += int((agg or {}).get("n_blocked") or 0)
    if n <= 0:
        return None
    by_probe = {
        p: {"n": a["n"], "n_blocked": a["n_blocked"],
            "coverage": (a["n"] - a["n_blocked"]) / a["n"] if a["n"] else None}
        for p, a in per_probe.items()
    }
    worst = None
    cands = [(v["coverage"], p) for p, v in by_probe.items() if v["coverage"] is not None]
    if cands:
        cov, p = min(cands)
        worst = {"probe": p, "coverage": cov, "n": by_probe[p]["n"]}
    return {"n_rows": len(rows), "n": n, "n_blocked": n_blocked,
            "coverage": (n - n_blocked) / n, "by_probe": by_probe, "worst_probe": worst}


# ── 口径之间的名次一致性（§4.7:820-832 的效度检查）──────────────────────────
def rank_agreement(scores_by_caliber: Dict[str, Dict[str, Optional[float]]],
                   ) -> Dict[str, Any]:
    """五个口径两两比 Kendall tau + 逆序对数（§4.7:820-832）。

    留多种口径不是犹豫，是效度检查：**要是模型排序换个口径就变，说明结论来自口径、
    不是来自能力**。把五列摊开让人自己看不算做到了这一条 —— 报表必须直接算出
    「换口径变没变名次」，否则没人会去逐行对。
    """
    keys = [k for k, v in (scores_by_caliber or {}).items()
            if v and sum(1 for x in v.values() if x is not None) >= 2]
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = scores_by_caliber[keys[i]], scores_by_caliber[keys[j]]
            common = [m for m in a if m in b and a[m] is not None and b[m] is not None]
            if len(common) < 2:
                out.append({"a": keys[i], "b": keys[j], "n": len(common),
                            "tau": None, "inversions": None})
                continue
            conc = disc = 0
            for x in range(len(common)):
                for y in range(x + 1, len(common)):
                    m1, m2 = common[x], common[y]
                    da = a[m1] - a[m2]
                    db = b[m1] - b[m2]
                    if da == 0 or db == 0:
                        continue
                    if (da > 0) == (db > 0):
                        conc += 1
                    else:
                        disc += 1
            tot = conc + disc
            out.append({"a": keys[i], "b": keys[j], "n": len(common),
                        "tau": (conc - disc) / tot if tot else None,
                        "inversions": disc})
    taus = [d["tau"] for d in out if d["tau"] is not None]
    return {"pairs": out, "min_tau": min(taus) if taus else None,
            "n_pairs": len(out)}


# ── §5 验收表（§5:895-903）─────────────────────────────────────────────────
ACCEPTANCE_DEFAULTS = {
    "top_score": (45.0, 70.0),          # §5:897
    "bottom_score_min": 15.0,           # §5:898
    "item_pass_median": (0.3, 0.7),     # §5:899
    "adjacent_separability_min": 0.95,  # §5:893
    "model_variance_min_pct": 9.2,      # §5:894（基线；**已不再是验收行**，见 §5.1.2）
    "coverage_min": 0.90,               # §5:896 只说「不低于下限」，数写在 config 里
    # ── 定版换上来的三条（§5.1.2）。上面 adjacent_separability_min /
    #    model_variance_min_pct 两个键**故意保留**：报表口径段（1f）和方差段还在用它们
    #    做判据，config 也在装载这两个键；删掉是另一件事，不在定版范围内。
    "alpha_min": 0.90,                  # 臂均分信度 α（§5.1.2）
    "split_half_min": 0.80,             # 分半排序相关中位数（§5.1.2）
    "min_tiers": 3,                     # 分层可分至少切出几层（§5.1.2）
    "max_tier_pct": 0.5,                # 且最大一层不超过总臂数的一半（§5.1.2）
}

OK, NG, ND = "达标", "未达标", "数据不足"


def _verdict_range(v: Optional[float], lo: float, hi: float) -> str:
    if v is None:
        return ND
    return OK if lo <= v <= hi else NG


def _caliber_scope_note(ovb) -> str:
    """三条信度验收算在哪个口径上 —— 必须写出来，因为它**不是榜上那个**。

    `variance_cells` 每格是单条条目的 raw_to_100，importance 权重没进来；榜上印的
    是加权的 score_v4。于是 α / 分半 / 分层回答的是「未加权条目均分那个排序稳不稳」，
    而读者看到的是加权榜。粒度选条目级是对的（理由在 variance_cells 的 docstring），
    所以这里不换口径，只把两者的**相邻对不一致数**印出来：不一致为 0 时这三格可以
    直接当榜的结论读，不为 0 时就不能。
    """
    if not ovb:
        return ""
    n_d, n_a = ovb.get("n_discordant"), ovb.get("n_adjacent")
    if n_d is None or not n_a:
        return ""
    head = (f"；⚠️ 口径：这一格算在**未加权条目级**上（每格=单条条目 raw_to_100），"
            f"榜上印的是加权 `{ovb.get('headline')}`")
    if n_d == 0:
        return head + f"，两者相邻次序完全一致（{n_a} 对），可以按榜的结论读"
    return (head + f"，两者有 **{n_d}/{n_a}** 对相邻臂次序相反 —— "
            f"**这一格的达标/不达标说的不是榜上那个次序**，别直接搬")


def _agent_direction_note(pair_rows) -> str:
    """条件对等时，把 Agent−Direct 的**方向**也写进那一格。

    这一行的判据 2026-08-31 从「Agent ≥ Direct」改成了「运行条件已披露」，理由写在
    acceptance_table 的 docstring 里：那会儿思考档位不对等，要求 Agent ≥ Direct 是
    要求一个假命题。改得对。但改完留了一个洞 —— 条件补齐之后这一格印的是
    「Agent−Direct 可以直接解读」，**却不说解读出来是什么**。于是报表另一处
    仍按 §1.3:91 把「同模型 Agent ≥ Direct」当硬性质写，而并入程序轨之后实测
    5 个模型里 4 个是负的（claude-opus-5 −0.29、gpt-5.5 −0.81、gemini −2.20、
    doubao −2.71，只有 kimi +0.52），一条都不显著。两处对着看才发现，而没人会
    对着看。所以在这里把方向和显著性一起印出来。

    仍然**不做成 pass/fail**：n=10 时每个 CI 都跨 0，拿它当闸门是拿噪声当判据。
    这一格要的是「读者不会以为那条性质成立」。
    """
    rows = [r for r in (pair_rows or []) if r.get("delta") is not None]
    if not rows:
        return ""
    n = len(rows)
    n_ge = sum(1 for r in rows if r["delta"] >= 0)
    n_sig = sum(1 for r in rows if r.get("significant"))
    worst = min(rows, key=lambda r: r["delta"])
    tag = f"{worst.get('model')}" + ("(-refs)" if worst.get("variant") == "refs" else "")
    return (f"。**方向**：{n_ge}/{n} 条配对满足 Agent ≥ Direct，"
            f"最负的是 {tag} {worst['delta']:+.2f}；显著的 {n_sig}/{n} 条"
            + ("（一条都不显著 → 这批数据尚不能确证提升或退化；"
               "§1.3:91 的 Agent 增益需要实测，不能预设方向）"
               if n_sig == 0 else ""))


def acceptance_table(*, top_score: Optional[float] = None,
                     bottom_score: Optional[float] = None,
                     item_pass_median: Optional[float] = None,
                     separability: Optional[dict] = None,
                     model_variance_pct: Optional[float] = None,
                     alpha: Optional[dict] = None,
                     split_half: Optional[dict] = None,
                     tiers: Optional[dict] = None,
                     direct: Optional[dict] = None, agent: Optional[dict] = None,
                     direct_agent_conditions: Optional[dict] = None,
                     agent_pair_rows: Optional[List[dict]] = None,
                     order_vs_board: Optional[dict] = None,
                     coverage: Optional[float] = None,
                     no_evidence: Optional[dict] = None,
                     worst_probe_coverage: Optional[float] = None,
                     thresholds: Optional[dict] = None,
                     sep_scope: str = "全部相邻对",
                     ) -> List[Dict[str, Any]]:
    """定版的**八条**验收，逐条算出「现在多少 / 达不达标」（§5.1.1）。

    和 §5:895-903 那七条的差别（§5.1.2 逐条给了理由）：
    - **删**「模型方差占比」→ 换成「臂均分信度 α」。前者问错了问题：条目难易本来
      就该占大头（实测 69.2%），**题出得越好这个占比越低**，拿它做门槛方向是反的。
    - **删**「相邻名次可分」→ 换成「分层可分」+「分半排序相关」。前者实测 1/9，
      而且**扔条目治不了**（扔条目同时把 n 扔了，误差按 √n 涨回去，每个子集都还是 1/9）。
    - **改**「Direct/Agent 都有 headroom 且卡点在 Agent」→「运行条件差异必须逐格披露」。
      原来那条的前提是错的：两层的思考档位根本不对等（Direct 要 9/10、Agent 实发 0/10，
      §3.4:454），在不对等的条件下要求「Agent ≥ Direct」等于要求一个假命题。而且这个不对等
      **补起来分两种、且不是都补得动**（2026-08-31 修正）：走 opencode 认识的
      `reasoningEffort` 的一种，和靠 `harness.opencode_options` 把 Direct 载荷原样透传的
      一种；剩下钉了 `none` 的那几条没逐发复打过，只能披露 —— 所以这条要求
      短期内**没有**合法的达标路径，只能改成披露。
    - `separability` / `model_variance_pct` 两个入口**保留但不再出行**：报表其它段
      还在报它们，验收表不拿它们判定。

    保留的五条要求不变，但「通过」和「条目集」两个口径钉死了（§5.1.3），
    否则同一份数据能算出两个结论（单条通过率中位数 0.90 vs 0.667 就是这么来的）。

    verdict 只有三个值：达标 / 未达标 / **数据不足**。任何输入是 None 的行必须出
    「数据不足」—— 折成 0 再判「未达标」会让人去改题，而真正该做的是先把那一层
    的产物跑出来（Agent 现在就是 0 条）。这七条是这个基准的目标函数，硬编码在代码里
    的静态一行字（旧写法 step5:536）等于每次都要人肉重算。

    注意：**跨度不在这张表里**，因为跨度不是区分度（§5:901-903）。跨度和误差一起
    出现在口径对照表里，永远成对。
    """
    th = dict(ACCEPTANCE_DEFAULTS)
    th.update(thresholds or {})
    top_lo, top_hi = th["top_score"]
    pm_lo, pm_hi = th["item_pass_median"]
    sep_min = float(th["adjacent_separability_min"])
    var_min = float(th["model_variance_min_pct"])
    cov_min = float(th["coverage_min"])
    alpha_min = float(th["alpha_min"])
    sh_min = float(th["split_half_min"])
    tier_min = int(th["min_tiers"])
    tier_pct_max = float(th["max_tier_pct"])

    rows: List[Dict[str, Any]] = []

    rows.append({
        "metric": "最强模型总分", "requirement": f"{top_lo:g} – {top_hi:g}",
        "value": None if top_score is None else round(top_score, 2),
        "verdict": _verdict_range(top_score, top_lo, top_hi),
        "note": "低于下限说明题坏了，高于上限说明没留空间（§5:897）",
    })
    rows.append({
        "metric": "最弱模型总分", "requirement": f"> {th['bottom_score_min']:g}",
        "value": None if bottom_score is None else round(bottom_score, 2),
        "verdict": ND if bottom_score is None else
                   (OK if bottom_score > th["bottom_score_min"] else NG),
        "note": "贴地板同样不带信息（§5:898）",
    })
    rows.append({
        "metric": "单条通过率中位数", "requirement": f"{pm_lo:g} – {pm_hi:g}",
        "value": None if item_pass_median is None else round(item_pass_median, 4),
        "verdict": _verdict_range(item_pass_median, pm_lo, pm_hi),
        "note": "饱和过滤**之前**的全部条目（过滤后算等于自己给自己打分，§5:899）",
    })

    # ── 定版换上来的三格（§5.1.2）。被换掉的「相邻名次可分」「模型方差占比」两格
    #    不再出验收行，但它们的数仍然进 note —— 删掉数会让人以为我们没算过。
    a_val = (alpha or {}).get("alpha")
    a_note = ("单因素方差 α=(F−1)/F，F 为臂间/残差均方比（§5.1.2）；"
              "**必须在完全设计上算** —— 每条臂考的是同一份题，缺格 >5% 判数据不足")
    if alpha:
        a_note += (f"；F={alpha.get('f', 0):.2f}, df {alpha.get('df1')}/{alpha.get('df2')}"
                   f"，{alpha.get('n_models')} 臂 × {alpha.get('n_items')} 条目")
    if model_variance_pct is not None:
        a_note += (f"；同一份方差的另一种读法「模型方差占比」= {model_variance_pct:.2f}%"
                   "，**那个数低不代表榜不可靠**（§5.1.2）")
    a_note += _caliber_scope_note(order_vs_board)
    rows.append({
        "metric": "臂均分信度 α", "requirement": f"≥ {alpha_min:g}",
        "value": None if a_val is None else round(a_val, 4),
        "verdict": ND if a_val is None else (OK if a_val >= alpha_min else NG),
        "note": a_note,
    })

    sh_val = (split_half or {}).get("median")
    sh_note = ("条目随机分半各出一榜、取两榜 Spearman，重复多次取中位数（§5.1.2）；"
               "问的是「整张榜的顺序稳不稳」，和「相邻两名能不能分开」不是一件事")
    if split_half:
        sb = split_half.get("spearman_brown")
        sb_text = f"{sb:.3f}" if sb is not None else "未定义（分半相关接近 -1）"
        sh_note += (f"；10–90 分位 {split_half.get('p10', 0):.3f}–{split_half.get('p90', 0):.3f}"
                    f"，Spearman-Brown 校正 {sb_text}"
                    f"（分半只用了一半条目，未校正值会系统性低估）"
                    f"，{split_half.get('n_reps')} 次重复")
    sh_note += _caliber_scope_note(order_vs_board)
    rows.append({
        "metric": "分半排序相关（中位）", "requirement": f"≥ {sh_min:g}",
        "value": None if sh_val is None else round(sh_val, 4),
        "verdict": ND if sh_val is None else (OK if sh_val >= sh_min else NG),
        "note": sh_note,
    })

    t_n = (tiers or {}).get("n_tiers")
    t_pct = (tiers or {}).get("max_tier_pct")
    t_note = ("按分数排序、相邻切得开处切一刀（§5.1.2）；**同层内不主张先后**，"
              f"榜上每条臂都要带 95% CI。判据={(tiers or {}).get('criterion', '—')}")
    if tiers:
        t_note += (f"；层大小 {(tiers or {}).get('sizes')}"
                   f"，最大层占 {t_pct:.0%}（要求 ≤ {tier_pct_max:.0%}）")
    if sep_val_note := ((f"；被它替换掉的「相邻名次可分」最小值 "
                         f"{separability.get('min_p'):.3f}、"
                         f"{separability.get('n_ok')}/{separability.get('n_pairs')} 对达标"
                         f"（口径范围：{sep_scope}）")
                        if (separability and separability.get("min_p") is not None) else ""):
        t_note += sep_val_note
    t_note += _caliber_scope_note(order_vs_board)
    # 两个子条件都要满足：只看层数会被「9 家一层 + 两家各自一层」蒙过去
    t_ok = (t_n is not None and t_n >= tier_min
            and t_pct is not None and t_pct <= tier_pct_max)
    rows.append({
        "metric": "分层可分（层数）",
        "requirement": f"≥ {tier_min} 层，且最大层 ≤ {tier_pct_max:.0%}",
        "value": t_n,
        "verdict": ND if t_n is None else (OK if t_ok else NG),
        "note": t_note,
    })

    # ── Direct/Agent：不再要求「Agent ≥ Direct」（§5.1.1）。原来那条的前提是错的 —— 两层的
    #    思考档位根本不对等，在不对等条件下要求一个大小关系等于要求假命题。
    #    定版要求变成「差异逐格披露」：披露到了就算达标，分差在披露之后才允许解读。
    direct_top = (direct or {}).get("top")
    agent_top = (agent or {}).get("top")
    c = direct_agent_conditions or {}
    n_arms = c.get("n_arms")
    tops = (f"Direct top={'-' if direct_top is None else f'{direct_top:.1f}'} / "
            f"Agent top={'-' if agent_top is None else f'{agent_top:.1f}'}")
    if direct_top is None or agent_top is None:
        rows.append({
            "metric": "Direct/Agent 运行条件差异已逐格披露",
            "requirement": "思考档位 / 请求形状 / 禁网档位 逐臂列出",
            "value": tops, "verdict": ND,
            "note": "缺一层就没有「差异」可披露（§5.1.1）；共享任务要求仍需核验运行条件，"
                    "**不保证 Agent 单次得分更高**",
        })
    elif not n_arms:
        rows.append({
            "metric": "Direct/Agent 运行条件差异已逐格披露",
            "requirement": "思考档位 / 请求形状 / 禁网档位 逐臂列出",
            "value": tops, "verdict": NG,
            "note": "两层都有分，但**条件差异表没生成** —— 这时候把 Agent 减 Direct 当成"
                    "「自截图的效果」是错的（§3.4:454 实测 Direct 要 8/9 档、Agent 实发 0/9）",
        })
    else:
        n_gap = c.get("n_gap") or 0
        rows.append({
            "metric": "Direct/Agent 运行条件差异已逐格披露",
            "requirement": "思考档位 / 请求形状 / 禁网档位 逐臂列出",
            "value": f"{tops}；{n_arms} 条臂已列，{n_gap} 条有差异",
            "verdict": OK,
            # 这一格**必须**把落差再拆开，不能只报一个合并的「差异 N 条」——
            # 「漏配」和「网关钉了 none」的处置完全不同，合并印出来读者会以为
            # 都改不了（或都能改）。同一份报表 80 行之后的明细表报的就是这个拆法，
            # 两处不一致等于自己打自己（踩过：摘要说漏配 7 条、明细说 4/3/1）。
            # 2026-08-31 起不再有「跨渠道、没有合法达标路径」这一类 ——
            # `harness.opencode_options` 能原样透传 Direct 的载荷，逐字段实打过。
            "note": (f"思考档：Direct 要 {c.get('n_ask')}/{n_arms}、Agent 实发 {c.get('n_sent')}/{n_arms}"
                     f"（落差 {n_gap} 条 = 漏配 {c.get('n_missing', 0)} 条"
                     f" + 显式钉 none {c.get('n_gateway', 0)} 条；"
                     f"已补档的 {(c.get('n_mirror') or 0) + (c.get('n_effort_key') or 0)} 条里"
                     f" {c.get('n_mirror', 0)} 条靠 `opencode_options` 原样透传）。"
                     "**披露达标 ≠ 条件对等**：有差异时 Agent−Direct 的差里掺着这个自变量，"
                     "方向系统性不利于 Agent，不能读成「加了自截图反而变差」（§5.1.1）"
                     if n_gap else
                     f"{n_arms} 条臂两侧条件一致（其中 {c.get('n_mirror', 0)} 条靠 "
                     "`opencode_options` 把 Direct 载荷原样透传补齐），Agent−Direct 可以直接解读"
                     + _agent_direction_note(agent_pair_rows)),
        })

    cov_val = coverage
    cov_note = ("覆盖率低 = 我们没量到，不是模型没做到（§4.2:736、§5:897）。"
                "**它和「无证据条目占比」是两件事**：探针覆盖率问确定性探针跑没跑出结果，"
                "无证据占比问判分模型看没看到证据 —— 后者的实测值在下面这句备注里，"
                "必须单独披露（§5.1.4 第 2 条），不能被这一格的高覆盖率盖住")
    verdict = ND if cov_val is None else (OK if cov_val >= cov_min else NG)
    if cov_val is not None and worst_probe_coverage is not None:
        cov_note += f"；最差探针 {worst_probe_coverage:.3f}"
        if worst_probe_coverage < cov_min:
            verdict = NG           # 单个探针塌了也算未达标，全局均值会盖住它
    # 把实测的无证据占比直接写进这一格的备注：上面那句「必须单独披露」如果不带数字，
    # 读者会以为披露过了。带上数字，这一格自己就能看出「覆盖率 0.98 但近 1/10 无证据」
    # 这个反差 —— 那正是这条备注要防的误读。**只进备注、不参与判定**：无证据占比不该
    # 被塞进覆盖率的门槛里（两件事、两个分母），它是披露项，判定在 §5.1.4。
    if no_evidence:
        cov_note += (f"。**本轮无证据：单元级 {no_evidence['unit_share']:.1%}"
                     f"（{no_evidence['n_units_hit']}/{no_evidence['n_units']}）**")
        if no_evidence.get("item_share") is not None:
            cov_note += (f"，条目级 {no_evidence['item_share']:.1%}"
                         f"（{no_evidence['n_items_hot']}/{no_evidence['n_items']}）")
        _worst_cat = next(iter(no_evidence.get("by_category") or {}), None)
        if _worst_cat:
            cov_note += (f"，最重的一类是 `{_worst_cat}`"
                         f" {no_evidence['by_category'][_worst_cat]['share']:.1%}")
    rows.append({
        "metric": "判定覆盖率", "requirement": f"≥ {cov_min:g}（整体与最差探针都算）",
        "value": None if cov_val is None else round(cov_val, 4),
        "verdict": verdict, "note": cov_note,
    })
    return rows


# ── 定版新增的三条验收（§5.1.2）─────────────────────────────────────────────
# 这三个替换掉「模型方差占比」和「相邻名次可分」两格。为什么换见 §5.1.2：那两格
# 一个问错了问题（条目难易本来就该占大头，题出得越好占比越低），一个要不到
# （实测 1/9，且扔条目治不了 —— 扔条目同时把 n 扔了，误差按 √n 涨回去）。
# 三个函数都**不自己读盘**，吃上游已经算好的产物，保证和报表其它格同源。

def arm_reliability_alpha(vd: Optional[Dict[str, Any]],
                          ) -> Optional[Dict[str, Any]]:
    """臂均分信度 α（§5.1.2）：换一批题，这张榜的臂均分还剩多少是真信号。

    吃 ``variance_decomposition()`` 的返回值，**不重新读数据** —— 两个数必须同源，
    否则「模型方差占比 0.81% 但信度 0.948」这种看着自相矛盾的一对会被当成算错了。
    它们其实是同一份方差的两种读法：

    - **占比**问「臂间方差占总方差多少」。一份题里条目难易的方差本来就远大于模型
      能力的方差（实测 69.2%），所以**题出得越好这个数反而越低** —— 拿它做门槛
      是问错了问题（§5.1.2）。
    - **信度**问「臂间方差比残差大多少倍」，这才是「名次稳不稳」。

    Hoyt 口径的两因素无重复方差分析：``F = MS_arm / MS_resid``，``α = (F−1)/F``。
    只用 vd 里的占比就够了 —— 三个占比同分母（``ss_total``），做比值时分母约掉::

        F = (model_pct/(J−1)) / (resid_pct/((I−1)(J−1))) = model_pct·(I−1)/resid_pct

    所以不必为了算这个再把 SS 从 vd 里传出来（少一个字段就少一处会不同步的地方）。

    **必须是近似完全设计**：上面那个 df 只在「每条臂都答了每一条条目」时成立。
    缺格超过 5% 直接返回 None（数据不足）—— 缺格会让 MS_resid 偏大、α 偏低，
    可公式照样算得出一个数，从结果上看不出错。
    """
    if not vd:
        return None
    j = int(vd.get("n_models") or 0)        # 臂数
    i = int(vd.get("n_items") or 0)         # 条目数
    n_cells = int(vd.get("n_cells") or 0)
    resid_pct = vd.get("resid_pct")
    model_pct = vd.get("model_pct")
    if j < 2 or i < 2 or not resid_pct or model_pct is None:
        return None
    full = i * j
    if not full or n_cells < 0.95 * full:
        return None                          # 缺格太多，df 不成立
    f = float(model_pct) * (i - 1) / float(resid_pct)
    if f <= 0:
        return None
    return {"alpha": 1.0 - 1.0 / f, "f": f, "df1": j - 1, "df2": (i - 1) * (j - 1),
            "n_items": i, "n_models": j, "completeness": n_cells / full}


def _ranks(vals: Sequence[float]) -> List[float]:
    """平均秩（并列取均值）。Spearman 用得上，并列不处理会把持平算成有序。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            out[order[t]] = avg
        i = j + 1
    return out


def _spearman(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    ra, rb = _ranks(a), _ranks(b)
    n = len(ra)
    if n < 2:
        return None
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((x - mb) ** 2 for x in rb) ** 0.5
    return (num / (da * db)) if (da and db) else None


def split_half_rank_stability(cells: Dict[Tuple[Any, Tuple[Any, Any]], float], *,
                              n: int = 200, seed: int = SEED,
                              ) -> Optional[Dict[str, Any]]:
    """分半排序相关（§5.1.2）：把条目随机分成两半，各出一张榜，两张榜名次有多像。

    吃的 cells 和 ``variance_decomposition()`` 同一个（key 是
    ``(model, (sample_id, item_id))``），所以两条验收永远同源。

    和 ``adjacent_separability()`` 问的**不是**同一件事，两个都要留：
    那个问「相邻两名能不能分开」（实测 1/9，要不到）；这个问「整张榜的顺序稳不稳」
    —— 前几名内部换位不影响整体顺序时，这个数照样高。定版主张的是后者（§5.1.2）。

    报中位数和 10/90 分位，不报均值：分半相关的分布是偏的，均值会被少数极端分半拉走。
    另外报 Spearman-Brown 校正值 ``2r/(1+r)`` —— 分半只用了**一半**条目，
    直接拿它当全量信度会系统性低估；校正值才是和 α 可比的那个数。
    """
    if not cells:
        return None
    by_arm: Dict[Any, Dict[Tuple[Any, Any], float]] = defaultdict(dict)
    items: set = set()
    for (model, item_key), v in cells.items():
        if v is None:
            continue
        by_arm[model][item_key] = float(v)
        items.add(item_key)
    arms = sorted(by_arm, key=str)
    item_list = sorted(items, key=lambda x: (str(type(x)), str(x)))
    if len(arms) < 2 or len(item_list) < 4:
        return None

    def _mean_over(arm: Any, subset: Sequence[Tuple[Any, Any]]) -> Optional[float]:
        d = by_arm[arm]
        vals = [d[k] for k in subset if k in d]
        return (sum(vals) / len(vals)) if vals else None

    rnd = random.Random(seed)
    rs: List[float] = []
    for _ in range(n):
        idx = list(range(len(item_list)))
        rnd.shuffle(idx)
        half = len(idx) // 2
        left = [item_list[i] for i in idx[:half]]
        right = [item_list[i] for i in idx[half:2 * half]]
        a = [_mean_over(m, left) for m in arms]
        b = [_mean_over(m, right) for m in arms]
        keep = [i for i in range(len(arms)) if a[i] is not None and b[i] is not None]
        if len(keep) < 2:
            continue
        r = _spearman([a[i] for i in keep], [b[i] for i in keep])
        if r is not None:
            rs.append(r)
    if not rs:
        return None
    rs_sorted = sorted(rs)
    med = _median(rs_sorted)
    def _q(p: float) -> float:
        return rs_sorted[min(len(rs_sorted) - 1, max(0, int(p * len(rs_sorted))))]
    # r=-1 时校正无定义；浮点 Spearman 可能返回 -0.9999999999999998，不能把
    # 近零分母放大成 -9e15 并作为可解释的信度写进报表。
    sb = (2 * med / (1 + med)) if (med is not None and med > -1 + 1e-12) else None
    return {"median": med, "p10": _q(0.10), "p90": _q(0.90),
            "spearman_brown": sb, "n_reps": len(rs),
            "n_items": len(item_list), "n_models": len(arms)}


def tier_separation(sep: Optional[Dict[str, Any]], *,
                    p_min: float = 0.95,
                    cuts: Optional[Sequence[bool]] = None,
                    ) -> Optional[Dict[str, Any]]:
    """分层可分（§5.1.2）：相邻显著处切一刀，切出「层」；**同层内不主张先后**。

    吃 ``adjacent_separability()`` 的返回值里那份**已经按名次排好**的 ``pairs``，
    判据和被它替换掉的那一格**完全一样**（同一个 ``p_min``）—— 换的不是判据，
    是主张：不再要求「每一对相邻都能分开」（实测 1/9，要不到），只要求
    「能切出 ≥3 层」，并且明写同层内名次不可解读（§5.1.2）。

    切点判据有两个，**给出的层数不一样**，所以定版必须钉死一个（§5.1.2）：

    - ``sep['pairs']`` 里的 ``p`` 是**重采样题目**得到的胜出概率，p≥0.95 约等于
      **单侧** 0.05 的检验；本轮 2/9 对切得下去 → **3 层**。
    - 条目**配对 t 检验** |t|>1.96 是**双侧** 0.05，更严；本轮 1/9 → **2 层**。

    定版钉**严**的那个（配对 t），由 ``adjacent_paired_t()`` 算出 ``cuts`` 传进来。
    理由不是哪个数好看，恰恰相反：宽的判据多切一层、榜看着更可分，选它就是拿检验
    强度换结论。传了 ``cuts`` 就用它，``p_min`` 只在没传时兜底（并在报表里注明用的是宽判据）。

    ``max_tier_pct`` 是最大一层占总臂数的比例：定版要求它 ≤ 0.5，否则「切出 3 层」
    可以靠「9 家一层 + 两家各自一层」蒙过去，那不叫分层。
    """
    if not sep:
        return None
    order = sep.get("order") or []
    pairs = sep.get("pairs") or []
    if len(order) < 2 or len(pairs) != len(order) - 1:
        return None
    if cuts is not None and len(cuts) != len(pairs):
        return None                            # 切点数必须等于相邻对数，宁可判数据不足
    tiers: List[List[Any]] = [[order[0]]]
    for j, pr in enumerate(pairs):
        if cuts is not None:
            cut = bool(cuts[j])
        else:
            p = pr.get("p")
            cut = (p is not None and p >= p_min)
        if cut:
            tiers.append([order[j + 1]])          # 这一刀切得下去 → 开新层
        else:
            tiers[-1].append(order[j + 1])        # 分不开 → 同层
    sizes = [len(t) for t in tiers]
    return {"tiers": tiers, "n_tiers": len(tiers), "sizes": sizes,
            "max_tier": max(sizes), "n_models": len(order),
            "max_tier_pct": max(sizes) / len(order), "p_min": p_min,
            "criterion": ("配对 t |t|>1.96" if cuts is not None
                          else f"重采样胜率 p≥{p_min:g}（宽判据）")}


def adjacent_paired_t(cells: Dict[Tuple[Any, Tuple[Any, Any]], float],
                      order: Sequence[Any], *, t_min: float = 1.96,
                      ) -> Optional[Dict[str, Any]]:
    """相邻两名的**条目配对 t 检验**（§5.1.2 钉的那个严判据）。

    为什么要配对：同一条条目对所有臂都是同一道题，难易是共因。不配对（两独立样本）
    的话，条目难易的方差会全部算进误差里，t 被系统性压小 —— 而且压得很稳定，
    从结果上看不出来。配对之后误差只剩「这两条臂在这条条目上的差」。

    ``order`` 必须是**已经按分数排好**的臂序（直接用 ``adjacent_separability()``
    返回的 ``order``），这样这里算的相邻对和报表其它地方的相邻对是同一批。

    返回 ``cuts``（每个相邻对能不能切开）供 ``tier_separation()`` 直接吃。
    某一对的共同条目 < 2 → 那一对的 ``t`` 是 None、``cut`` 判 False（切不开），
    宁严不宽：数据不足不该变成「分开了」。
    """
    if not cells or len(order) < 2:
        return None
    by_arm: Dict[Any, Dict[Tuple[Any, Any], float]] = defaultdict(dict)
    for (model, item_key), v in cells.items():
        if v is not None:
            by_arm[model][item_key] = float(v)
    out_pairs: List[Dict[str, Any]] = []
    cuts: List[bool] = []
    for j in range(len(order) - 1):
        a, b = order[j], order[j + 1]
        da, db = by_arm.get(a) or {}, by_arm.get(b) or {}
        common = [k for k in da if k in db]
        diffs = [da[k] - db[k] for k in common]
        t = None
        if len(diffs) >= 2:
            mu = sum(diffs) / len(diffs)
            var = sum((d - mu) ** 2 for d in diffs) / (len(diffs) - 1)
            se = (var / len(diffs)) ** 0.5
            t = (mu / se) if se > 0 else None
        cut = (t is not None and abs(t) > t_min)
        cuts.append(cut)
        out_pairs.append({"above": a, "below": b, "t": t, "n_common": len(diffs),
                          "cut": cut})
    ts = [p["t"] for p in out_pairs if p["t"] is not None]
    return {"pairs": out_pairs, "cuts": cuts, "n_pairs": len(out_pairs),
            "n_ok": sum(cuts), "min_abs_t": min(abs(t) for t in ts) if ts else None,
            "t_min": t_min}


def complete_subdesign(cells: Dict[Tuple[Any, Tuple[Any, Any]], float],
                       arms: Optional[Sequence[Any]] = None,
                       ) -> Optional[Dict[str, Any]]:
    """把 cells 削成「这些臂都答了的那批条目」——**完全设计**，并报出削掉了什么。

    为什么必须削：信度 α 和分半相关都要求「每条臂考的是同一份题」。不削的话，
    一条只答了 300 条的臂和一条答了 1800 条的臂会被当成同一张考卷上的两个考生，
    α 的 df 直接不成立（``arm_reliability_alpha()`` 因此设了 5% 缺格上限）。

    为什么必须**报**削掉了什么：「样本悄悄变少」是本项目踩过最贵的一类 bug
    （§5.1.4 第 3 条）。所以返回值里带 ``n_items_dropped`` / ``arms_dropped``，
    报表有义务把它们印出来，而不是只印一个变好看了的 α。

    ``arms=None`` 时对 cells 里出现的所有臂取交集 —— 注意这会让**最缺的那条臂**
    决定所有人的题量，所以调用方通常应该显式传「同一层的那几条臂」（Direct 一份、
    Agent 一份），而不是把两层混在一起：两层的运行条件本来就不同（§5.1.4 第 1 条）。
    """
    if not cells:
        return None
    by_arm: Dict[Any, set] = defaultdict(set)
    for (model, item_key), v in cells.items():
        if v is not None:
            by_arm[model].add(item_key)
    want = [a for a in (arms if arms is not None else sorted(by_arm, key=str))
            if by_arm.get(a)]
    dropped_arms = [a for a in (arms or []) if not by_arm.get(a)]
    if len(want) < 2:
        return None
    union = set().union(*[by_arm[a] for a in want])
    common = set.intersection(*[by_arm[a] for a in want])
    if len(common) < 2:
        return None
    out = {(a, k): cells[(a, k)] for a in want for k in common
           if (a, k) in cells and cells[(a, k)] is not None}
    return {"cells": out, "arms": want, "n_arms": len(want),
            "n_items": len(common), "n_items_union": len(union),
            "n_items_dropped": len(union) - len(common),
            "arms_dropped": dropped_arms}


# ── 无证据条目占比（§5.1.4 第 2 条,出榜必须随附的披露之一）────────────────────
# 判分模型**自称没看到证据**的比例。它和探针覆盖率是两件事：探针覆盖率问
# 「确定性探针跑没跑出结果」，这个问「判分模型看没看到能判的东西」。整体探针覆盖率
# 0.98 完全盖不住这一项 —— 实测有 10.4% 的判分单元自称无证据（审计 §L）。
#
# 为什么必须披露：这类条目**每条臂都拿 0 分**，通过率钉在 0.000，在报表里长得和
# 「超难条目」一模一样。审计 §L 用「自称无证据率 × 通过率」的相关（r=−0.656）
# 把这两者分开了 —— 不披露的话，读者会把「我们没取到证」读成「模型做不到」。

# 强标记：句子主语只能是「证据」本身，不会指向页面缺了什么
_NO_EV_STRONG = ("无法确认", "无法验证", "无法判断", "无法观察", "未能观察",
                 "无证据", "没有证据", "不可判定", "无法从截图", "证据不全")
# 弱标记：必须带上「证据来源」这个主语才算。**裸的「未提供」不算** ——
# 实测「页面未提供可点击的 Tab 控件」「未提供图形或插画」都是模型的真失败，
# 而「事实块未提供 HTML 标签统计」才是我们没取到证。只按关键词数会把前者
# 也算成「判不了」，从而把真失败洗成基准的锅（这个方向的误判最危险）。
_NO_EV_WEAK = re.compile(
    r"(事实块|材料|截图|渲染帧|帧\s*\d*|图\s*\d+)[^。；;]{0,14}?(未提供|未包含|未覆盖|均未)")
_NO_EV_LACK = re.compile(r"(缺少|缺乏|没有)[^。；;]{0,24}?证据")


def is_no_evidence(text: Optional[str]) -> bool:
    """判分文字里是否**自称没看到证据**（§5.1.4 第 2 条）。

    口径故意偏保守（宁可漏判成「模型失败」，不可把模型失败洗成「我们没取到证」）：
    弱标记必须带「事实块 / 材料 / 截图 / 帧 / 图N」这类**证据来源**做主语才算数。
    """
    if not text:
        return False
    t = str(text)
    if any(k in t for k in _NO_EV_STRONG):
        return True
    return bool(_NO_EV_WEAK.search(t)) or bool(_NO_EV_LACK.search(t))


def no_evidence_share(units: Iterable[dict], *, item_threshold: float = 0.25,
                      min_arms: int = 4,
                      arm_score: Optional[Dict[Any, float]] = None
                      ) -> Optional[Dict[str, Any]]:
    """无证据占比：**单元级**一个数、**条目级**一个数，两个都报（§5.1.4 第 2 条）。

    ``units`` 是 ``{'arm','sample_id','item_id','category','text'}`` 的可迭代对象
    —— 自己不 glob 目录，这样质检能拿手造数据钉住它。

    两个口径必须分开报，因为它们回答不同的问题、数值也不同（实测 10.4% vs 11.8%）：

    - **单元级** = 有多少个「臂 × 条目」判分单元自称无证据。这是判分层面的直接损失。
    - **条目级** = 有多少**条目**在 ≥ ``item_threshold`` 比例的臂上都自称无证据。
      这才是「这道题我们系统性地取不到证」，也是 §5.1.3 定义「可判定子集」时用的口径
      （阈值 0.25 → 84/710 = 11.8%）。

    按 ``arm`` 和按 ``category`` 各拆一份：审计 §L.1 实测 `Interaction` 一类就占了
    条目的 19.7%、均通过率 0.26、31.6% 的判分自称无证据 —— 只报一个总数看不出
    这件事集中在哪。
    """
    rows = [u for u in (units or []) if isinstance(u, dict)]
    if not rows:
        return None
    n_tot = len(rows)
    by_arm: Dict[Any, List[int]] = defaultdict(lambda: [0, 0])       # [hit, n]
    by_cat: Dict[Any, List[int]] = defaultdict(lambda: [0, 0])
    by_item: Dict[Tuple[Any, Any], List[int]] = defaultdict(lambda: [0, 0])
    item_cat: Dict[Tuple[Any, Any], Any] = {}
    n_hit = 0
    for u in rows:
        hit = 1 if is_no_evidence(u.get("text")) else 0
        n_hit += hit
        arm = u.get("arm")
        cat = u.get("category") or "(未标)"
        ik = (u.get("sample_id"), u.get("item_id"))
        for d, k in ((by_arm, arm), (by_cat, cat), (by_item, ik)):
            d[k][0] += hit
            d[k][1] += 1
        item_cat[ik] = cat
    # ``min_arms`` 是条目级口径的**覆盖下限**，不是为了好看：只有 2 条臂判过的条目，
    # 一票就是 50%，随手过 25% 的门槛 —— 这样算出来的「系统性取不到证」全是噪声。
    # 判不了的条目单列在 n_items_thin，**不静默丢**（§5.1.4「不许静默截断」）。
    hot = [ik for ik, (h, n) in by_item.items()
           if n >= min_arms and h / n >= item_threshold]
    thin = [ik for ik, (h, n) in by_item.items() if n < min_arms]
    n_ok = len(by_item) - len(thin)
    hot_by_cat: Dict[Any, int] = defaultdict(int)
    for ik in hot:
        hot_by_cat[item_cat.get(ik, "(未标)")] += 1
    # 与主榜分的秩相关。§5.1.4 第 2 条原先断言「缺证是题目属性、对各臂同等生效」——
    # 这个数就是那句断言的检验。实测 **+0.64**：分高的臂反而更多条目被判成无证据
    # （页面越接近对，judge 越容易从两张截图上"确认不了"，而失败得明显的页面
    # judge 会直接写出偏差）。方向是**打压高分臂 → 压缩量程**，跟本轮只分出 2 层同向。
    # ⚠️ 这个符号定不了因果：既可能是"臂好 → 难取证"，也可能是"被判无证据 → 分被压"。
    rho, pairs = None, []
    if arm_score:
        pairs = [(h / n, arm_score[a]) for a, (h, n) in sorted(
            by_arm.items(), key=lambda kv: str(kv[0]))
            if n and arm_score.get(a) is not None]
        if len(pairs) >= 4:
            rho = _spearman([x for x, _ in pairs], [y for _, y in pairs])
    return {
        "rho_score": rho, "n_rho": len(pairs),
        "unit_share": n_hit / n_tot, "n_units": n_tot, "n_units_hit": n_hit,
        "item_share": len(hot) / n_ok if n_ok else None,
        "n_items": n_ok, "n_items_hot": len(hot),
        "n_items_thin": len(thin), "n_items_union": len(by_item),
        "item_threshold": item_threshold, "min_arms": min_arms,
        "by_arm": {a: {"share": h / n, "n": n, "n_hit": h}
                   for a, (h, n) in sorted(by_arm.items(), key=lambda kv: str(kv[0]))},
        "by_category": {c: {"share": h / n, "n": n, "n_hit": h,
                            "n_items_hot": hot_by_cat.get(c, 0)}
                        for c, (h, n) in sorted(by_cat.items(),
                                                key=lambda kv: -kv[1][0])},
    }
