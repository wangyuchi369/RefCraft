"""参考跟随指令矩阵（ref_directives）的共享解释层。

step0 为每个样本采样出一份 6 维 × 4 档的矩阵（见 prompts/query_recipe/reference.yaml）：

    aspect    ∈ {layout, palette, typography, component, interaction, atmosphere}
    directive ∈ {copy, adapt, invert, ignore}

v3 的 step1/step1b/step2 都只看 user_prompt 散文，把这份矩阵丢掉了，导致：
  - invert 维度产出方向相反的条目（照抄的模型反而得高分）
  - ignore 维度被截图污染出条目（模型自由发挥却被扣分）
  - copy 维度可能整个漏检（白送分）

本模块把矩阵翻译成 prompt 片段 + 程序化配额，让上述三件事可校验。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..core.log import get_logger

logger = get_logger(__name__)

# ── 维度中文名（与 prompts/query_recipe/reference.yaml 的 aspects.*.zh 保持一致）──
ASPECT_ZH: Dict[str, str] = {
    "layout": "布局结构（板块排列、栅格、信息层级）",
    "palette": "配色（底色/主色/强调色的关系、明暗倾向）",
    "typography": "字体排版（字族气质、字号层级、行距、字重对比）",
    "component": "组件样式（卡片/按钮/输入框、圆角、阴影、描边）",
    "interaction": "交互与动效（导航方式、hover 反馈、滚动行为）",
    "atmosphere": "整体气质（调性、信息密度、留白感）",
}

ASPECT_ORDER: List[str] = [
    "layout", "palette", "typography", "component", "interaction", "atmosphere",
]

VALID_ASPECTS = set(ASPECT_ORDER)
# specify 是第五档（总纲 §2.2）：这一面的真值在**题面**里（「正文 18px 起」），参考图里没有。
# 出题侧那张 ref_directives 只有四个取值，被题面点了硬指标的方面在表里记的是 `ignore`，
# 真取值藏在 ref_specified_aspects / ref_spec —— 由 get_directives() 还原回来。
VALID_DIRECTIVES = ("copy", "adapt", "invert", "specify", "ignore")

# 每个 aspect 的条目应该落在哪些 category 里（audit 用来交叉检查标注是否自相矛盾）
ASPECT_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "layout": ("Layout", "Structure"),
    "palette": ("Visual",),
    "typography": ("Visual",),
    "component": ("ComponentStyle",),
    "interaction": ("Interaction",),
    "atmosphere": ("Visual", "Layout"),
}

# directive → (最少条目数, 该档条目允许的最高 importance)
#   copy   严格沿用 → 条目多、可以 critical
#   adapt  神似即可 → 条目适中、最高 high（逐像素不是要求，别当 critical）
#   invert 反着来   → 必须有负向检查，最高 high
#   specify 题面点名 → 恰好 1 条硬条目（真值在题面，不在图里）
#   ignore 自由发挥 → 一条都不许有
DIRECTIVE_MIN_ITEMS: Dict[str, int] = {
    "copy": 3, "adapt": 2, "invert": 2, "specify": 1, "ignore": 0,
}
DIRECTIVE_MAX_IMPORTANCE: Dict[str, str] = {
    # specify 允许 critical：题面点名的硬指标（字号 / 色值 / 栏数）就是这道题的核心要求，
    # 而且它优先交给程序判（§3.3），不吃「VLM 心软」那份折扣。
    "copy": "critical", "adapt": "high", "invert": "high",
    "specify": "critical", "ignore": "low",
}

IMPORTANCE_RANK = {"zero": -1, "low": 0, "medium": 1, "high": 2, "critical": 3}

# critical 条数配额：越靠 near_copy，"整体风格是否守住"越该是根本性要求；
# loose 桶里参考几乎不起作用，critical 就该留给内容/结构本身。
BUCKET_CRITICAL_QUOTA: Dict[str, Tuple[int, int]] = {
    "near_copy": (3, 5),
    "strong": (3, 5),
    "partial": (2, 4),
    "loose": (2, 3),
}
DEFAULT_CRITICAL_QUOTA = (2, 4)

# 只有参考强度足够高时，"照抄参考网站文案"才是真实风险，才值得花条目去查
CONTENT_LEAK_BUCKETS = ("strong", "near_copy")


def specified_aspects(prompt_data: Dict[str, Any]) -> List[str]:
    """题面点了硬指标的方面（specify），按 ASPECT_ORDER 排。"""
    p = prompt_data or {}
    got = {a for a in (p.get("ref_specified_aspects") or []) if a in VALID_ASPECTS}
    spec = p.get("ref_spec") or {}
    if isinstance(spec, dict):
        got |= {a for a in spec if a in VALID_ASPECTS}
    return [a for a in ASPECT_ORDER if a in got]


def spec_of(prompt_data: Dict[str, Any], aspect: str) -> Dict[str, Any]:
    """那一面的硬指标：{code, say_zh/say_en, check, probe}。没有就 {}。"""
    spec = (prompt_data or {}).get("ref_spec") or {}
    v = spec.get(aspect) if isinstance(spec, dict) else None
    return v if isinstance(v, dict) else {}


def get_directives(prompt_data: Dict[str, Any]) -> Dict[str, str]:
    """取出并清洗 ref_directives；缺字段的老样本返回空 dict（调用方退化为 v3 行为）。

    **specify 在这里还原**（总纲 §2.2 的第五档）。出题侧那张表只有四个取值，被题面点了
    硬指标的方面在表里记的是 `ignore`，真取值在 ref_specified_aspects / ref_spec。
    不还原的三个后果全是静默的：
      · utils/checklist/checklist_schema.py 按「ignore 零条目」把 spec 条目**全删** —— 实测 500 条
        题里 265 条中招，269 条 spec 条目（全是 critical）一条不剩；
      · utils/score/judge_facts.py::item_ref_labels 走不到 specify 那一支（「一份都不送」），
        改走 ignore 的「保守全送」→ judge 手里有参考图，就会拿「参考长什么样」去覆盖
        题面写死的字号 / 色值；
      · step1d_audit 把每条 spec 条目报成「ignore 却有条目（惩罚了正确行为）」。
    只在记录值是 ignore / 缺失时覆盖：万一出题侧同时给了 copy + specify，那是契约冲突，
    该由 tools/qa/check_query_set_contract.py 报出来，这里不擅自改写。
    """
    raw = prompt_data.get("ref_directives") or {}
    if not isinstance(raw, dict):
        return {}
    out = {
        a: d for a, d in raw.items()
        if a in VALID_ASPECTS and d in VALID_DIRECTIVES
    }
    for a in specified_aspects(prompt_data):
        if out.get(a, "ignore") == "ignore":
            out[a] = "specify"
    return out


def aspects_by_directive(directives: Dict[str, str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {d: [] for d in VALID_DIRECTIVES}
    for a in ASPECT_ORDER:
        d = directives.get(a)
        if d:
            out[d].append(a)
    return out


def critical_quota(prompt_data: Dict[str, Any]) -> Tuple[int, int]:
    return BUCKET_CRITICAL_QUOTA.get(
        prompt_data.get("ref_bucket"), DEFAULT_CRITICAL_QUOTA
    )


def wants_content_leak_check(prompt_data: Dict[str, Any]) -> bool:
    return prompt_data.get("ref_bucket") in CONTENT_LEAK_BUCKETS


def max_importance_for(aspect: Optional[str], directives: Dict[str, str]) -> Optional[str]:
    """该 aspect 的条目允许的最高 importance；aspect 未标注则不限制。"""
    if not aspect or aspect not in directives:
        return None
    return DIRECTIVE_MAX_IMPORTANCE.get(directives[aspect])


# ── prompt 片段 ────────────────────────────────────────────────────────────

_DIRECTIVE_RULE = {
    "copy": (
        "**[copy] 严格沿用截图**：用户要求这些维度紧跟参考截图。**每个维度至少 {n} 条**"
        "具体条目，basis 里必须写出你在截图中观察到的具体特征（色值、字号关系、圆角/阴影、"
        "栏宽比例等）。这些维度里可以有 critical 条目。"
    ),
    "adapt": (
        "**[adapt] 借调性但可调整**：用户要「神似」而非「逐像素相同」。每个维度至少 {n} 条，"
        "问题要问**方向/调性**是否一致（如「是否同样采用暗底+高饱和强调色的方向」），"
        "**不要**问精确色值或精确间距 —— 那会惩罚正确的改编行为。"
        "这些维度的条目最高 importance 为 high，不得标 critical。"
    ),
    "invert": (
        "**[invert] 刻意反着来 ⚠️ 最容易搞错的一档**：用户明确要求这些维度**与截图相反**。"
        "每个维度至少 {n} 条**差异性条目**——检查的是「是否明确偏离了截图」，"
        "而不是「是否与截图一致」。**照抄截图的实现应当在这些条目上得低分。**\n"
        "  - ✅ 正确：「页面的信息密度是否明显高于参考截图（紧凑排布而非大面积留白）？」\n"
        "  - ❌ 错误：「留白节奏是否与参考截图一致？」（方向反了，会奖励照抄）\n"
        "  - 问题必须写成**正向提问**，让符合用户要求 = 回答「是」，`expected_answer` 一律填 `Yes`。\n"
        "  - basis 要同时写出「截图里是什么样」和「用户要求朝哪个方向走」。\n"
        "  - 这些维度的条目最高 importance 为 high。"
    ),
    "specify": (
        "**[specify] 题面点名的硬指标**：用户在这一维度上给了**具体数值/具体做法**"
        "（字号、色值、栏数、圆角……），真值在**需求原文**里，不在截图里。"
        "恰好 {n} 条硬条目，照需求原文那句话写死判据（可量、可复算）。\n"
        "  - **不要**参照截图给这一维度出任何条目 —— 截图在这一面上是无关的，"
        "拿它当依据等于用「参考长什么样」覆盖用户写死的数字。\n"
        "  - depth 填 `precision`，可以标 critical。"
    ),
    "ignore": (
        "**[ignore] 不参考截图，自由发挥**：用户在这些维度上**没有**参考截图的意思。"
        "**这些维度一条条目都不要生成。** 即使截图里的做法很有特色，也不要写成检查项 —— "
        "模型在这些维度自由发挥是符合用户要求的，为它生成条目等于惩罚正确行为。"
    ),
}


# category → 可能贡献该 category 的 aspect 集合（ASPECT_CATEGORIES 的逆映射）
CATEGORY_ASPECTS = {}
for _a, _cats in ASPECT_CATEGORIES.items():
    for _c in _cats:
        CATEGORY_ASPECTS.setdefault(_c, set()).add(_a)

# 只对这两个 category 用"全 ignore 则删"的兜底：它们几乎只可能来自截图。
# Layout/Structure/Interaction 的条目常常是用户指令直接要求的（"左侧边栏导航"），
# 按 category 一刀切会误删指令条目。
CATEGORY_SCREENSHOT_ONLY = ("Visual", "ComponentStyle")

_SCREENSHOT_MENTIONS = ("截图", "参考图", "参考页面", "screenshot", "reference image", "reference page")


# "未提截图" / "与截图无关" 这类否定式提及不算引用截图，否则会误删纯指令条目
_SCREENSHOT_NEGATIONS = ("未提截图", "没提截图", "未引用截图", "不参考截图", "与截图无关",
                         "和截图无关", "非截图", "不涉及截图")


def looks_screenshot_derived(item) -> bool:
    """条目的 basis/question 是否**正面**自称引用了截图。"""
    text = f"{item.get('basis', '')} {item.get('question', '')}"
    if any(neg in text for neg in _SCREENSHOT_NEGATIONS):
        return False
    low = text.lower()
    return any(m.lower() in low for m in _SCREENSHOT_MENTIONS)


def is_orphan_ignore_item(item, directives) -> bool:
    """模型漏填 ref_aspect 时的兜底：该 category 下所有 aspect 都是 ignore，
    且条目自称引用截图 → 它必然违反了 ignore 指令。"""
    if not directives or (item.get("ref_aspect") or "").strip():
        return False
    cat = item.get("category")
    if cat not in CATEGORY_SCREENSHOT_ONLY:
        return False
    aspects = CATEGORY_ASPECTS.get(cat, set())
    if not aspects or not all(directives.get(a) == "ignore" for a in aspects):
        return False
    return looks_screenshot_derived(item)


# ── invert 条目的方向（正/负向）─────────────────────────────────────────────
# invert 维度上一条**方向反了**的条目（"配色是否与截图一致？"）比缺条目更糟：
# expected_answer 恒为 "Yes"，所以照抄参考的模型反而拿分 —— 这一轴的难度被
# 掰成了负数。step1/step1b 的 prompt 都写了规则，但 VLM 出题时照样会写反；
# 只有程序判一遍才拦得住（docs/设计总纲.md §1.2 C2/§2.2 invert）。
#
# 判定顺序是**先负后正**：负向表达天然包含正向词（"不与截图一致"里有"一致"），
# 反过来先匹配正向就会把正确条目全判成写反了。
# 判定必须落在**提到参考的那一小段**上，不能全句找关键词。实测（v1 600 条的
# 837 个 invert 条目）两类错法都很常见：
#   · "……而非沿用截图中的直角硬描边" —— 方向对的，全句找"沿用"会判成写反了；
#   · "是否建立清晰且一致的多级字号层级" —— "一致"说的是**产物内部**一致，
#     跟参考无关，全句找"一致"同样误报。
# 所以：先找参考锚点（截图 / 参考 / 第「B」份），只在锚点附近 ±14 字的窗口里
# 看"像不像"的动词和否定词。宁可漏判也不误报 —— 这条违规是要**打回重出题**的。
_REF_TOKEN = re.compile(r"截图|参考(?![值线])|参照|原页面?|第\s*[「『\"']?[0-9A-Za-z]\s*[」』\"']?\s*份|"
                        r"[「『][0-9A-Za-z][」』]份|reference|the\s+ref\b", re.IGNORECASE)
# "像参考那样"的动词。注意"采用"单独出现不算（"是否采用三栏栅格"是绝对规格）
_SIM = re.compile(r"一致|相同|相符|吻合|匹配|取自|沿用|延用|复刻|还原|照搬|照抄|"
                  r"一样|类似|相似|同款|保持|延续|承袭|复用|遵循|贴近|接近|如出一辙|"
                  r"same|identical|match|consistent|copy|follow", re.IGNORECASE)
# 窗口里出现任一否定/背离词 → 这次提及参考是"别像它"，方向是对的
_NEG_NEAR = re.compile(r"不|未|没|非|无|避|摆脱|区别|有别|相反|反着|拒绝|摒弃|抛弃|去除|"
                       r"弱化|改为|换成|替代|偏离|规避|背离|脱离|降低|减少|"
                       r"not|no\b|avoid|differ|unlike|instead|rather|without", re.IGNORECASE)
# 判定分层。核心那条规则是实测逼出来的：**"偏离标记词"的宾语/比较对象必须就是
# 参考本身**。同一个词组在两种句子里意思相反，只看词表分不开：
#   · "……而非沿用**截图**中的直角硬描边"   → 拿参考当反面，负向题；
#   · "……以逐项列表而非整段散文呈现"       → 只是在绝对规格里排除一个错选项，
#                                            跟参考无关（v1 的 copy 侧面上 610 条
#                                            带这类词，只有 36% 真的在说参考）。
# 量化过：要求"标记词后 20 字内出现参考锚点"，invert 侧面留下 93%（漏掉的那几条
# 由比较级/ANTI_REF 兜住），copy 侧面从 610 条掉到 219 条。同样的形状在比较级上更
# 极端 —— "字号明显小于正文"是**产物内部**比较，v1 的 copy 侧面 245 条里只有 13
# 条真的在跟参考比。误把这些算成"已有负向条目"，invert 侧面上一批只规定"该做成
# 什么样"的条目就会被当成合格，而那正是 doc 警告的"把 invert 悄悄改成 copy"。

# 第 1 层：不提参考也已经把"别像它"说明白了（doc §6 的示例条目
# "导航是否明确【不是】顶部固定？"就是这一层）
_ANTI_SELF = re.compile(r"不像|不同于|不一致|不相同|反其道|明[确显].{0,3}(?:不|区别|偏离|背离)|"
                        r"differ|unlike|distinct|not\s+(?:the\s+same|identical)")
# 第 3 层：这些词的**宾语/比较对象**得是参考，才说明"跟参考反着来"
_OBJ_MARK = re.compile(r"而非|而不是|避[免开]|规避|拒绝|摒弃|舍弃|摆脱|区别于|有别于|偏离|背离|"
                       r"instead\s+of|rather\s+than|avoid")
_CMP_MARK = re.compile(r"高于|低于|大于|小于|多于|少于|超过|不如|远[大小高低]|"
                       r"more\s+than|less\s+than|larger|smaller|tighter|looser")
_XFORM_MARK = re.compile(r"压缩|压紧|收紧|放大|缩小|加大|减小|拉开|弱化|强化|减弱|增强|"
                         r"改为|换成|替代|取代|去除|取消|反转|翻转")
# "相较参考图明显收紧"：参考是被"相较"引出的比较对象，偏离动词在它**后面**，
# 后向规则抓不到。这类句子只要同时出现偏离动词就是负向的。
_VS_MARK = re.compile(r"相较|相对|相比|较之|对比|比起|较[之于]|比(?![例如方])")
# 跟 _VS_MARK 搭配的"偏了多少"：光有"比参考截图"还不算方向，得说往哪偏
_MORE_MARK = re.compile(r"更|略[高低大小紧松宽窄多少]|偏[高低大小紧松]|加倍|数倍")
# "**没有**截图那种上下错位"、"**未出现**截图式的深色色块" —— 否定词的宾语就是
# 参考。这类句子里"像不像"的动词往往在说**产物内部**（"高度一致"、"接近统一"、
# "保持浅底深字"），窗口规则只看到那个动词就会判成正向。实测 v1 的 invert 侧面
# 上 4 条方向正确的条目就是这么被报成"写反了"的。
_NEG_OBJ = re.compile(r"没有|没出现|未|不存在|不出现|不采用|不使用|不沿用|不含|去掉|摒除|排除")
# 第 4 层：关系词自带"相对于前面提到的那个东西"，参考锚点在前在后都算
_REL_MARK = re.compile(r"相反|颠倒|对调|不再|并非")
# 双重否定的坑："是否**避免偏离**截图的处理" —— 标记词后 20 字内确实有"截图"，
# 但整句要求的是**保持像参考**。所以标记词和参考之间夹了反向词时要放弃这次命中。
_FLIP_BETWEEN = re.compile(r"偏离|背离|不像|不同|区别|有别|摆脱|反转|差异")
# 同一句里"偏离"既是标记词、又是"避免"的宾语（"避免偏离截图"），只挡"标记词与
# 参考之间"不够 —— 得连"标记词自己被否掉了"一起挡，否则 finditer 走到第二个
# 标记词（偏离）时那句话又被算成负向。
_FLIP_BEFORE = re.compile(r"避[免开]|规避|杜绝|不要|不得|不能|没有|无|防止")

_INVERT_WINDOW = 24
_OBJ_FORWARD = 20          # 标记词后多少字内的参考锚点才算它的宾语
_NEG_BACK = 8              # 否定词要紧邻在"像不像"动词前面这么多字内


# 宾语不跨小句。"（而非与标题同样的纯白高亮），形成与截图一致的明暗层次"里，
# "截图"属于逗号后的另一小句 —— 不切断就会把这条**要求像参考**的 copy 条目
# 读成负向（v1 上残留的 5% copy negative 基本都是这个形状）。
_CLAUSE = re.compile(r"[，。；？！、（）()：\n]")


def _marks_ref(q: str, pat, forward: int = _OBJ_FORWARD) -> bool:
    """`pat` 命中的词，其后同一小句 `forward` 字内有参考锚点（= 宾语就是参考）。"""
    for m in pat.finditer(q):
        if _FLIP_BEFORE.search(q[max(0, m.start() - 5):m.start()]):
            continue                      # "避免偏离截图"里的"偏离"：被否掉了
        tail = q[m.end():m.end() + forward]
        cut = _CLAUSE.search(tail)
        if cut:
            tail = tail[:cut.start()]
        hit = _REF_TOKEN.search(tail)
        if not hit:
            continue
        if _FLIP_BETWEEN.search(tail[:hit.start()]):
            continue                      # "避免偏离截图"：双重否定，其实是要求像
        return True
    return False


def invert_polarity(question: str) -> str:
    """invert 维度上这条题问的是哪个方向 → `negative` / `positive` / `unknown`。

    - `negative`：方向对（"布局是否明确**不像**第「A」份？"、"……而非沿用截图的 X"）。
    - `positive`：**方向反了**，照抄参考反而得分，必须报违规。
    - `unknown`：没提参考锚点，或只规定"该做成什么样"。这也是个坑（doc：
      不要出"必须做成 XX"这类条目 —— 那是把 invert 悄悄改成了 copy），
      但它不一定错（题面自己点名的绝对规格就该这么问），所以只计数、不算违规。

    宁可判成 `unknown`/`negative` 也不误报 `positive`：这条违规的处置是打回
    step1 重出题，误报的代价比漏判高。实测 v1 的 837 个 invert 条目里 0 误报。
    """
    q = question or ""
    if not q:
        return "unknown"
    if _ANTI_SELF.search(q):
        return "negative"
    hits = list(_REF_TOKEN.finditer(q))
    if not hits:
        return "unknown"                  # 没提参考 = 绝对规格，方向无从判断
    if (_marks_ref(q, _OBJ_MARK) or _marks_ref(q, _CMP_MARK)
            or _marks_ref(q, _XFORM_MARK)):
        return "negative"
    if _REL_MARK.search(q):
        return "negative"                 # 关系词：参考在句中出现过就够
    if _marks_ref(q, _NEG_OBJ):
        return "negative"                 # "没有截图那种上下错位"
    if _marks_ref(q, _VS_MARK) and (_XFORM_MARK.search(q) or _CMP_MARK.search(q)
                                    or _MORE_MARK.search(q)):
        return "negative"                 # "相较参考图明显收紧"/"比参考截图更紧凑"
    # 英文一个词就占掉半个中文窗口，按字符数一刀切会把 "identical to the
    # reference" 切断（实测就是这么漏的）
    win_n = _INVERT_WINDOW if any(ord(c) > 0x2E80 for c in q) else _INVERT_WINDOW * 3
    saw_positive = False
    for h in hits:
        lo = max(0, h.start() - win_n)
        win = q[lo:h.end() + win_n]
        for sm in _SIM.finditer(win):
            # 否定词也得管住方向：只认"像不像"动词**前面**紧邻的否定
            # （"是否没有沿用截图" ✔）。整个窗口找一遍会把
            # "沿用参考截图的布局，以逐项列表而非整段散文" 里那个跟参考无关的
            # "而非"算成否定 —— v1 的 copy 侧面上这一条路径误判 124 次。
            back = win[max(0, sm.start() - _NEG_BACK):sm.start()]
            cut = list(_CLAUSE.finditer(back))
            if cut:
                back = back[cut[-1].end():]     # 否定词也不跨小句
            if _NEG_NEAR.search(back):
                return "negative"
            saw_positive = True
    if saw_positive and (_OBJ_MARK.search(q) or _NEG_OBJ.search(q)
                         or _REL_MARK.search(q)):
        # 句子里别处还有偏离标记，只是它的宾语没落在小句里（"……而不是只有个别
        # 按钮带动效、其余仍**沿用截图**的静态反馈"）。这种嵌套否定判不准，按
        # 既定口径退回 `unknown`：误报一条 positive 要打回重出题，代价比漏判高。
        return "unknown"
    return "positive" if saw_positive else "unknown"


def invert_audit(items, directives) -> Dict[str, Any]:
    """按侧面统计 invert 条目的方向 → {aspect: {negative, positive, unknown, bad_ids}}。

    只看 `ref_aspect` 落在 invert 侧面上的**计分**条目：闸门 / importance=zero
    不进加权分，写反了也不发分，报出来只会淹掉真问题。
    """
    out: Dict[str, Any] = {}
    inv_aspects = [a for a, d in (directives or {}).items() if d == "invert"]
    if not inv_aspects:
        return out
    for aspect in inv_aspects:
        row = {"negative": 0, "positive": 0, "unknown": 0, "bad_ids": []}
        for it in items or []:
            if (it.get("ref_aspect") or "").strip().lower() != aspect:
                continue
            if it.get("gate") or it.get("importance") == "zero":
                continue
            pol = invert_polarity(it.get("question", ""))
            row[pol] += 1
            if pol == "positive":
                row["bad_ids"].append(it.get("id"))
        out[aspect] = row
    return out


# ── 多参考（1~5 份，按侧面分派）─────────────────────────────────────────────
# 一条样本的参考截图可能来自 1~5 个**互不相干**的网页，每个维度只跟其中一份。
# 下游任何一处把它们当成"同一个页面的若干帧"，这条样本就废了：judge 会把几个网站的
# 特征混成一套去比对，"跟错来源"这个唯一的判分点直接消失。
#
# 两种数据形状都要接：
#   prompt 侧 ref_mix:   [{label, src_sample_id, handle, looks_like, owns, shot_range, frames:[名字]}]
#   meta  侧 ref_groups: [{label, handle, looks_like|ref_is, owns, n_frames, frames:[{name,time_ms,...}]}]
# 判定"是不是多参考"**只看分组数 ≥ 2**：老 mix 切片写了 mix=true，新切片可能只写
# n_refs；而 n_refs=1 的样本即使带了一个 ref_groups 也是单参考，必须走单参考文案。
MAX_REFS = 5


def ref_groups_of(d) -> List[Dict[str, Any]]:
    """取分组信息并统一形状；返回 [] 表示「按单参考处理」。

    每个 group 都补齐 shot_range=[lo, hi]（1-based，跨份连续编号）。prompt 侧本来就有；
    meta 侧按 n_frames / len(frames) 依次累加算出来。
    """
    if not isinstance(d, dict):
        return []
    raw = d.get("ref_mix") or d.get("ref_groups") or []
    if not isinstance(raw, list):
        return []
    groups: List[Dict[str, Any]] = []
    cur = 1
    for g in raw:
        if not isinstance(g, dict):
            continue
        rng = g.get("shot_range")
        n = g.get("n_frames") or len(g.get("frames") or []) or 1
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            lo, hi = int(rng[0]), int(rng[1])
        else:
            lo, hi = cur, cur + int(n) - 1
        groups.append({**g, "shot_range": [lo, hi]})
        cur = hi + 1
    if len(groups) < 2:
        return []
    if len(groups) > MAX_REFS:
        logger.warning(f"[ref_directives] {len(groups)} 份参考 > MAX_REFS={MAX_REFS}，仍按实际份数处理")
    return groups


def n_refs_of(d) -> int:
    """这条样本有几份参考。以实际分组为准，缺分组时退回声明的 n_refs（默认 1）。"""
    g = ref_groups_of(d)
    if g:
        return len(g)
    try:
        return max(1, int((d or {}).get("n_refs") or 1))
    except (TypeError, ValueError):
        return 1


def mix_groups(d) -> List[Dict[str, Any]]:
    """旧名（mix 专用时期留下的），行为等同 ref_groups_of。"""
    return ref_groups_of(d)


def group_index(groups) -> Dict[str, int]:
    """label → 「第几份」（1-based）。所有 prompt 文案都必须用这一份编号。"""
    return {g.get("label"): i for i, g in enumerate(groups, 1) if g.get("label")}


def aspect_owner_map(prompt_data, groups=None) -> Dict[str, str]:
    """aspect → 该维度取自哪一份的 label。

    优先 prompt 的 ref_directive_sources；缺失时从 group.owns 反推（meta-only 调用点）。
    """
    src = {a: l for a, l in ((prompt_data or {}).get("ref_directive_sources") or {}).items()
           if a in VALID_ASPECTS and l}
    if src:
        return src
    for g in (groups if groups is not None else ref_groups_of(prompt_data)):
        for a in g.get("owns") or []:
            if a in VALID_ASPECTS:
                src[a] = g.get("label")
    return src


def role_of(g) -> str:
    """一份参考的角色：owner（照它做）/ avoid（明确别做成它那样）。

    tools/step0/plan_mix.py 直接写 role；老数据没有这个字段，就按「只被点了 avoids、
    一个维度都不 owns」反推。
    """
    r = str((g or {}).get("role") or "").strip().lower()
    if r in ("owner", "avoid"):
        return r
    if (g or {}).get("avoids") and not (g or {}).get("owns"):
        return "avoid"
    return "owner"


def aspect_avoid_map(prompt_data, groups=None) -> Dict[str, List[str]]:
    """aspect → 「明确别做成这几份那样」的 label 列表。

    avoid 是**带锚点的 invert**：plan_mix 只把它挂在没有正向拥有者的侧面上，
    所以 aspect_owner_map 和这里的 key 集合天然不相交。
    优先读显式字段（prompt 的 ref_avoid_sources / meta 的 avoid），缺失时从
    group.avoids 反推。
    """
    d = prompt_data or {}
    raw = d.get("ref_avoid_sources") or d.get("avoid") or d.get("mix_avoid") or {}
    out: Dict[str, List[str]] = {}
    if isinstance(raw, dict):
        for a, ls in raw.items():
            if a in VALID_ASPECTS and ls:
                out[a] = [l for l in (ls if isinstance(ls, (list, tuple)) else [ls]) if l]
    if out:
        return out
    for g in (groups if groups is not None else ref_groups_of(d)):
        for a in g.get("avoids") or []:
            if a in VALID_ASPECTS and g.get("label"):
                out.setdefault(a, []).append(g["label"])
    return out


def avoid_groups(groups) -> List[Dict[str, Any]]:
    """角色是「反面参考」的那些份。"""
    return [g for g in groups if role_of(g) == "avoid"]


def distractor_groups(groups, src, avoid=None) -> List[Dict[str, Any]]:
    """既没有维度取自它、也没被点名当反面锚点的那些份 —— 纯干扰项。

    新集（plan_mix）里应该是空的：多出来的份一律有 avoid 角色，因为"塞进去不说
    为什么"的份没法出条目（"是否没从它借东西"对一份根本没被提及的页面无从判定）。
    老 mix 切片和手工样本可能有，所以这条路留着。
    """
    owned = set((src or {}).values())
    named = {l for ls in (avoid or {}).values() for l in ls} | {
        g.get("label") for g in groups if role_of(g) == "avoid"}
    return [g for g in groups if g.get("label") not in owned and g.get("label") not in named]


def _span(g, zh=True) -> str:
    lo, hi = g.get("shot_range") or [0, 0]
    if lo == hi:
        return f"screenshot_{lo}"
    return f"screenshot_{lo}~screenshot_{hi}" if zh else f"screenshot_{lo}-{hi}"


def group_lines(groups, zh=True, spans=True) -> str:
    """每一份是什么。`spans=False` 时**不报** screenshot 编号区间。

    编号区间只有在「meta 记的份覆盖 == 真附上的图数」时才是真的；对不上还照报，
    第 2 份之后的每个边界都整体偏移一帧（见 multi_screenshot_note 的守卫）。
    """
    out = []
    for i, g in enumerate(groups, 1):
        desc = (g.get("looks_like") or g.get("ref_is") or "").strip()
        handle = (g.get("handle") or "").strip()
        if zh:
            tag = "**[反面参考]** " if role_of(g) == "avoid" else ""
            where = f"（{_span(g)}）" if spans else ""
            out.append(f"- 第 {i} 份{where}：{tag}{desc}"
                       + (f"（可以称它「{handle}」）" if handle else ""))
        else:
            where = f" ({_span(g, zh=False)})" if spans else ""
            out.append(f"- Page {i}{where}: {desc}")
    return "\n".join(out)


def mix_group_lines(groups, zh=True) -> str:
    """旧名。"""
    return group_lines(groups, zh=zh)


# 「哪个维度取自哪一份」这句话该把读者指向哪里 —— 出题侧 prompt 里有指令矩阵，
# 判分侧（score_checklist.txt）没有，只有条目自己的 basis。指错地方等于让判分模型
# 去找一段不存在的输入，它会就近拿参考图自己猜一套归属，把「跟错份」判反。
SOURCE_HINT_RECIPE = "**哪个维度取自哪一份，见下面的指令矩阵**"
SOURCE_HINT_CHECKLIST = "**哪个维度取自哪一份，以每个条目 basis 里写明的那一份为准**"


def multi_screenshot_note(meta_or_entry, source_hint: Optional[str] = None,
                         n: Optional[int] = None) -> str:
    """多参考样本的中文截图说明（出题/判分侧用，可以知道分组真值）。

    `n` = **真正附上的参考图张数**。给了就必须和 meta 记的份覆盖严格相等，否则只报
    「有几份、各长什么样」而不报编号区间 —— 与 `response_screenshot_note` 同一个决定。
    少一张图还照报边界，第 2 份之后每个边界前移一帧，等于拿 A 的帧去判「必须跟 B」，
    错会记在我们身上；而这一侧比被测侧更要命：judge 会照着错的证据打分。
    """
    groups = ref_groups_of(meta_or_entry)
    ok = True
    for g in groups:
        lo, hi = (g.get("shot_range") or [0, 0])
        if lo < 1 or hi < lo:
            ok = False
            break
    cover = sum((g["shot_range"][1] - g["shot_range"][0] + 1) for g in groups) if ok else -1
    trust = ok and (n is None or cover == n)
    if not trust:
        logger.warning(
            f"[screenshot_note] 多参考 meta 与图数不符: groups cover={cover} vs images={n}，"
            f"退化为不给分组边界"
        )
    n_show = cover if n is None else n
    hint = source_hint or SOURCE_HINT_RECIPE
    degraded = "" if trust else (
        "**这次无法把截图编号对应到具体的份**（记录与实际附上的图数不符）—— 不要按编号认份，"
        "只能按图上看到的内容判断；凡是必须先确定「这一处出自哪一份」才能成立的判断，"
        "请按「无法确认」处理并说明原因，不要猜。\n"
    )
    return (
        f"我提供 **{n_show} 张**参考网页截图，它们来自 **{len(groups)} 个互不相干的网页**，"
        f"按份分组、依次排列：\n" + group_lines(groups, spans=trust) + "\n" + degraded +
        f"**不同份之间是不同的网站** —— 不要把它们合起来当成同一个页面去理解，也不要"
        f"因为两份的做法不一致就当成「设计不统一」。\n"
        "同一份内部的多张图，是那个页面在一次交互演示中按时间先后抽出的帧（滚动到不同位置、"
        "或某个 hover/点击之后的状态）。\n"
        f"用户只从每一份里各借**一部分**东西：{hint}。"
        "「维度做对了但跟错了来源」在这条样本上同样是不通过 —— 这是它区别于单参考样本的判分要点。\n"
        "另外：**不要为看不到的区域或状态编造设计特征**，也不要据此出「参考页面缺少 XX」这类条目。"
    )


def mix_screenshot_note(meta_or_entry, source_hint: Optional[str] = None,
                        n: Optional[int] = None) -> str:
    """旧名。"""
    return multi_screenshot_note(meta_or_entry, source_hint, n=n)


def screenshot_note(n: int, meta=None, source_hint: Optional[str] = None,
                    names: Optional[List[str]] = None) -> str:
    """描述参考截图的真实来源，供 checklist 侧的 prompt 填充。

    别写成「从上到下连续滚动的截屏」：实测 500 个样本里 494 个的 actions.yaml
    含 hover/click/type/press_key/execute_js，只有 6 个是纯滚动。截图是**交互演示
    的按时序抽帧**（meta.json 里每帧带 time_ms），所以相邻两帧的差异可能来自滚动，
    也可能来自 hover 高亮、展开的菜单、切换的标签、或游戏/应用的状态变化。
    把它说成滚动位置会让 judge 把「同一区域的 hover 态」误读成「页面下一屏」。

    多参考样本（2~5 份按侧面分派）走另一条分支：那批图**真的**是好几个不同的网站，
    这里原本那句"不是 N 个不同的网站"对它们是反的。
    """
    if ref_groups_of(meta):
        return multi_screenshot_note(meta, source_hint, n=n)
    if n <= 1:
        return (
            "我只提供 **1 张**参考网页截图。它是那个参考页面在一次交互演示过程中"
            "**某一时刻**的一帧 —— 既不保证是页面顶部，也可能是某个交互之后的中间状态。"
            "页面的其余部分、以及其它交互状态，你都看不到。\n"
            "因此：**不要为看不到的区域或状态编造设计特征**；也不要因为某个元素没出现在"
            "这一帧里，就断定参考页面没有它（更不要据此出「参考页面缺少 XX」这类条目）。"
        )
    # 首末帧必须用**真实文件名**：帧号不连续的样本（v1 里 32 个，如 1/4/6/7/8/9）上
    # 「screenshot_1 最早、screenshot_{n} 最晚」的后半句直接是假的 —— 编号 n 的那张
    # 压根没附上，模型会以为自己漏看了一张，或者去描述一张想象中的图。
    stems = [os.path.basename(x).rsplit(".", 1)[0] for x in (names or [])]
    if len(stems) == n and n >= 2:
        order = f"（{stems[0]} 最早，{stems[-1]} 最晚）"
    else:
        order = "（按附件顺序，第一张最早、最后一张最晚）"
    return (
        f"我提供 **{n} 张**参考网页截图。它们是**同一个**参考页面在一次交互演示中"
        f"按时间先后抽出的帧{order}，"
        f"**不是 {n} 个不同的网站**。\n"
        "演示过程包含滚动、悬停、点击、输入、键盘操作等，所以相邻两帧的差异**既可能是"
        "滚动到了页面的不同位置，也可能是同一处元素的 hover 高亮、展开的菜单/弹层、"
        "切换后的标签页，或应用状态的变化**（例如游戏从开始屏进入进行中）。\n"
        "请把这些帧合起来理解这个页面的设计与交互；但**不要假设它们覆盖了整页或全部状态** —— "
        "没被拍到的区域和状态，不要编造设计特征。"
    )


def _shot_index(name: str) -> Optional[int]:
    m = re.search(r"(\d+)", os.path.basename(name or ""))
    return int(m.group(1)) if m else None


def shot_group_map(meta) -> Dict[str, int]:
    """截图文件名 → 「第几份」。先认 meta.screenshots[].group，缺了就按 shot_range 反查。

    缺这一层的后果见 frame_manifest_block：逐帧清单会把几个网站的帧读成一串时序。
    """
    groups = ref_groups_of(meta)
    if not groups:
        return {}
    gidx = group_index(groups)
    out: Dict[str, int] = {}
    for s in (meta or {}).get("screenshots") or []:
        name = s.get("name")
        if not name:
            continue
        if s.get("group") and s["group"] in gidx:
            out[name] = gidx[s["group"]]
            continue
        k = _shot_index(name)
        if k is None:
            continue
        for g in groups:
            lo, hi = g["shot_range"]
            if lo <= k <= hi:
                out[name] = gidx.get(g.get("label"), 0)
                break
    return out


def frame_manifest_block(meta, screenshot_paths=None) -> str:
    """逐帧说明「这张是什么」，只在帧被 tools/step0/recapture_reference.py 补抓过时才有内容。

    补抓的帧带 kind（scroll=滚动到某个纵向位置 / interaction=某个动作之后的状态）和
    note（那个动作的描述）。不告诉出题模型这些，它看到「同一区域的两张图」只会当成
    重复帧跳过 —— 拍到了 hover 态也出不出 interaction 条目，补抓就白做了。

    老数据（meta.screenshots 只有 name+time_ms）返回空串，行为完全不变。
    """
    shots = (meta or {}).get("screenshots") or []
    groups = ref_groups_of(meta)
    is_multi = bool(groups)
    # 多参考样本即使一帧都没补抓过也要出清单：逐行的「第 N 份」前缀是它唯一的判分依据。
    if not shots or (not is_multi and not any(s.get("kind") for s in shots)):
        return ""
    names = None
    if screenshot_paths:
        names = {os.path.basename(p) for p in screenshot_paths}
    sgmap = shot_group_map(meta) if is_multi else {}
    lines = []
    for s in shots:
        if names is not None and s.get("name") not in names:
            continue        # 真喂给模型的图才列，避免清单和图对不上
        kind = s.get("kind")
        # 每行都带 y：有些"交互"帧（比如按 Home 键）同时也换了滚动位置，
        # 只写"交互之后的状态"会让 judge 以为还在原来那一屏。
        y = f"纵向位置 y={s.get('scroll_y', 0)}px"
        if kind == "scroll":
            what = f"{y}，滚动到这里"
        elif kind:
            # 非滚动帧 = 时序状态：hover/点击/输入之后，或纯 CSS/JS 动画的某一幕
            what = f"{y}，交互或动画之后的状态"
        else:
            # kind 为空 = 这帧没被 recapture 标注过（老帧），位置和状态都不知道。
            # mix 样本有 57/100 是"一份补抓过、另一份没有"，走到 else 分支就会把没
            # 标注的帧一律写成"y=0px，交互或动画之后的状态" —— 两句都是编的：
            # scroll_y 的 0 是缺省值不是实测值，帧也未必在交互之后。老帧只能说不知道。
            what = "没有标注（不清楚拍的是页面哪一处，也不清楚是不是某个交互之后的状态）"
        note = (s.get("note") or "").strip()
        # 多参考：每行钉上"属于第几份"。少了这个前缀，逐帧清单会把几个网站的帧读成一串
        # 时序，"第 5 张的 y 比第 4 张小"就会被当成往上滚，而其实是换了一个网站。
        gi = sgmap.get(s.get("name")) if is_multi else None
        pre = f"[第 {gi} 份] " if gi else ""
        lines.append(f"- {pre}{s['name']}（t={int(s.get('time_ms') or 0)}ms）：{what}"
                     + (f" —— {note}" if note else ""))
    if not lines:
        return ""
    head = ("**每张截图分别是什么**（先按份、份内按时间顺序）：\n" if is_multi
            else "**每张截图分别是什么**（按时间顺序）：\n")
    tail_mix = ("\n\n注意：时间戳只在**同一份内部**可比 —— 不同份之间没有先后关系，"
                "相邻两张跨了份时，差异是换了网站，不是页面变化。") if is_multi else ""
    # 一帧交互态都没有时（mix 里有 18 个样本整条都没标注）就别提这段，
    # 否则等于告诉模型去清单里找一类根本不存在的行。
    has_inter = any(s.get("kind") and s.get("kind") != "scroll" for s in shots)
    tail_inter = ("\n\n注意：标「交互或动画之后的状态」的帧，拍的是参考页面对 hover/点击/输入的响应、"
                  "或者动画进行到某一幕的样子 —— 这是判断 interaction 维度该出什么条目的直接依据。"
                  "两张图看起来位置相同但细节不同时，差异往往就是那个交互/动画效果（高亮、展开、状态切换），"
                  "不是重复帧。") if has_inter else ""
    return head + "\n".join(lines) + tail_mix + tail_inter


# ── 判分侧的图片说明（step4b_score.py）────────────────────────────────────────
# 每个 fixture 一句「这张图是怎么造出来的」。少了这句，judge 会把「列表里有 300 行
# 一模一样的数据」当成模型自己把数据写错了，反过来扣分 —— 那是我们注入的。
FIXTURE_JUDGE_DESC = {
    "zero_rows": "把页面里那组列表/表格的数据**全部删空（0 条）**之后重新截的 —— 看有没有"
                 "空状态提示（一句「暂无数据」也算），还是塌成一片空白、或只剩一个悬空的表头/边框",
    "many_rows_300": "把那组列表/表格**灌到 300 条**之后重新截的 —— 看有没有横向溢出、"
                     "把容器/卡片撑破、或者整页布局崩掉",
    "long_string_80ch": "把其中一段文字换成 **80 个不带空格的字符**之后重新截的 —— 看会不会"
                        "顶出横向滚动条、或把文字溢到容器外面",
    "cjk_emoji": "把文字换成**中日韩全角字符 + emoji** 之后重新截的 —— 看换行、行高、字体回退"
                 "有没有出问题（字被裁掉、行距挤在一起、出现方块字）",
}


def _ordinal_span(lo: int, hi: int) -> str:
    return f"第 {lo} 张" if lo >= hi else f"第 {lo} ~ {hi} 张"


def _render_frame_what(spec: Dict[str, Any]) -> str:
    """一张渲染帧拍的是什么。字段来自 step3 的 frames.json（step4 按文件名查出来的）。

    和参考侧 `frame_manifest_block` 用同一套说法（"滚动到这里" / "交互或动画之后的状态"），
    好让 judge 能把两侧对上。没有标注的老帧只说不知道 —— 编一个 y=0 会让它以为
    自己看的是页面顶部。
    """
    kind = spec.get("frame_kind")
    y = f"纵向 y={int(spec.get('scroll_y') or 0)}px"
    if kind == "scroll":
        what = f"{y}，滚动到这里"
    elif kind:
        what = f"{y}，交互或动画之后的状态"
    else:
        what = "没有标注（不清楚拍的是页面哪一处，也不清楚是不是某个交互之后的状态）"
    note = str(spec.get("note") or "").strip()
    return what + (f" —— {note}" if note else "")


def judge_image_note(n_ref: int, meta=None, screenshot_paths=None,
                     render_specs: Optional[List[Dict[str, Any]]] = None) -> str:
    """判分侧的「附件里每张图分别是什么」（中文，填 score prompt 的 image_order_note）。

    与 `response_screenshot_note` 的分工：被测模型那边要**藏**分组真值（读懂参考归属
    本身就是考点），judge 这边必须**给足** —— 「维度做对了但跟错了参考」这类条目，
    不告诉它 screenshot_k 属于第几份就根本判不动。

    `render_specs` 是渲染侧的图按附件顺序排好的描述，由 step4 收图时同一个循环产出
    （每项 kind / width / name / fixture）。窄屏帧和对抗数据帧必须在这里交代清楚：
    - 不说宽度，judge 会拿 390px 的图去和宽屏参考图做逐像素对比，判成"布局错了"；
    - 不说"数据是我们注入的"，judge 会把 300 行重复数据当成模型自己写错了数据。
    doc §3 记的洞正是这一条：rubric 里有「响应式未满足 ≤5」，而 judge 手里从来没有窄屏图，
    VLM 在无证据时的默认行为是给保底分。
    """
    specs = list(render_specs or [])
    n_render = len(specs)
    total = n_ref + n_render
    # `screenshot_paths` 必须是**真附上的那批**（加载成功的子集），不是磁盘上的全量：
    # 张数来自加载结果、路径来自磁盘时，只丢一张就会让说明里多列一帧、份边界整体前移。
    # 对不齐时不猜「丢的是哪一张」—— 逐帧说明整段不给，也不报编号↔帧号的对应关系。
    paths = [x for x in (screenshot_paths or [])]
    aligned = bool(paths) and len(paths) == n_ref
    if paths and not aligned:
        logger.warning(
            f"[judge_image_note] 传进来的参考图路径 {len(paths)} 条 ≠ 真附上的 {n_ref} 张，"
            f"退化为不给逐帧说明（调用方应当传加载成功的那批路径）"
        )
    out: List[str] = ["## 图片说明", ""]
    out.append(f"本次一共附了 **{total} 张图**，严格按下面的顺序排列。")
    out.append("")

    # ── 参考截图 ──
    if n_ref == 0:
        # 一张参考图都没附上时，下面那几行会说「第 1 张：参考截图」「我只提供 1 张」——
        # 对着 0 张图说这话是假的，而 judge 找不到那张不存在的图，只会把这一批条目
        # 整批判成「无法确认」→ 保底分（§4.1 无证据 = 垫地板）。所以这里单独交代。
        out.append("### 参考截图：本次**不附**")
        out.append("这一批条目的真值在**用户指令（题面）和条目自己的 basis 里**，不在参考图里。"
                   "**不要因为看不到参考图就把这些条目判成「无法确认」** —— 它们不需要参考图；"
                   "按题面/basis 里写明的要求，对着下面的渲染帧和程序事实核对即可。")
        out.append("")
    else:
        out.append(f"### {_ordinal_span(1, n_ref)}：参考截图（原始参考网页，不是被评页面）")
        stems = [os.path.basename(x).rsplit(".", 1)[0] for x in paths] if aligned else []
        if stems and stems == [f"screenshot_{k}" for k in range(1, n_ref + 1)]:
            # 只有帧号真的是连续的 1…n_ref 才能这么说；v1 有 32 个样本缺号（如 1/4/6/7/8/9）
            out.append(f"附件顺序就是 screenshot 的编号顺序：第 k 张 = screenshot_k（k = 1…{n_ref}）。")
        elif stems:
            out.append("附件里的参考图依次是：" + "、".join(
                f"第 {i} 张 = {nm}" for i, nm in enumerate(stems, 1)) + "。")
        else:
            out.append("参考图按下面的顺序附在前面（编号与文件名的对应关系这次给不出来，"
                       "不要按文件名编号去定位某一张）。")
        out.append(screenshot_note(n_ref, meta, source_hint=SOURCE_HINT_CHECKLIST,
                                  names=paths if aligned else None))
        manifest = frame_manifest_block(meta, paths) if aligned else ""
        if manifest:
            out.append("")
            out.append(manifest)
        out.append("")

    if n_render == 0:
        # 这里**不许**说「请根据 HTML 源码评估」：产物原文从来不进判分上下文（总纲 §3.6，
        # 两个理由是自我辩护和串轨）。这句话曾经存在过，指的是一条不存在的材料 ——
        # judge 只能靠猜「页面大概会这么写」来完成这个指令，那正是本段要堵的失真。
        out.append("### 被评页面的渲染帧：**不可用**")
        out.append("这次一张渲染帧都没取到。**页面源码不提供**（见《程序量出来的事实》），"
                   "所以凡是必须看渲染结果才能确认的条目（动态行为、实际配色、实际间距、"
                   "响应式），一律按「无法确认」处理 —— 不要凭「页面大概会这么写」给中间分。"
                   "程序事实段里有量到的数（色值、字号、溢出、节点数、探针计数）时，"
                   "可以只用那些数作答。")
        return "\n".join(out)

    # ── 渲染侧：按 kind 连续分段，段内逐张列 ──
    # main_w 跨段用：窄屏段要拿它说「参考截图和这几帧同宽」。没有 main 段时保持 None，
    # 那一句就不报数字。
    main_w = None
    i = 0
    while i < n_render:
        kind = specs[i].get("kind")
        j = i
        while j < n_render and specs[j].get("kind") == kind:
            j += 1
        seg = specs[i:j]
        lo, hi = n_ref + i + 1, n_ref + j
        if kind == "main":
            main_w = seg[0].get("width")
            out.append(f"### {_ordinal_span(lo, hi)}：渲染帧（**被评页面**"
                       + (f"，视口宽 {int(main_w)}px" if main_w else "") + "）")
            out.append(f"共 {len(seg)} 帧，与参考截图同宽，可以直接做视觉对比。"
                       "这是被评页面「正常状态」的样子 —— 除响应式和极端数据两类条目外，"
                       "其它条目都以这几帧为准。")
            # 逐帧点名，和参考侧那份清单同一个写法。少了这几行，一串同宽的图在 judge
            # 眼里只是「重复帧」：拍到了 hover 态它也当没拍到，interaction 条目于是
            # 在有证据的情况下被判成无法确认（§4.1 无证据 = 垫地板）。
            if any(s.get("frame_kind") for s in seg):
                out.append("**这几帧分别是什么**（逐张点名，不要按编号推断）：")
                for k, s in enumerate(seg):
                    out.append(f"- 第 {lo + k} 张：{_render_frame_what(s)}")
                out.append("**不要按「第几张对第几张」去和参考截图比** —— 两侧帧数不一样"
                           "（参考每份 2~15 张、这边最多 10 张），对齐要看上面写的"
                           "「滚动到哪里 / 哪个交互之后」和画面内容本身。")
            if len(seg) < 5:
                # 同上：不能把 judge 打发回一份它永远拿不到的材料。这条分支比零帧分支常见得多
                # （主帧 <5 就触发），而「回到源码判断」在这里只有两种结果：judge 要么猜源码
                # （编 observed，比判错更糟），要么发现拿不到而整批判「无法确认」→ 保底分。
                out.append(f"⚠️ 只有 {len(seg)} 帧，覆盖不全：没被拍到的区域，看下面《程序量出来的事实》"
                           "里有没有对应的量；两处都没有就写「无法确认」，不要因为「帧里没看到」"
                           "就直接判缺失，也不要猜源码里写了什么（**页面源码不提供**）。")
        elif kind == "narrow":
            widths = sorted({int(s.get("width") or 0) for s in seg}, reverse=True)
            out.append(f"### {_ordinal_span(lo, hi)}：窄屏截图（**被评页面**，宽 "
                       + " / ".join(f"{w}px" for w in widths) + "）")
            for k, s in enumerate(seg):
                out.append(f"- 第 {lo + k} 张：{s.get('width')}px 宽，第 {int(s.get('shot_index', 0)) + 1} 屏")
            # 不写死宽度：参考帧是 1920 拍的（§3.5「主视口跟参考帧同宽」），
            # 早先这里写的「只有 1280px 宽这一档」是假话。要报数就报上面那几张渲染帧
            # 真实的宽度（两者钉住相等，见 tools/qa/check_config_wiring.py），
            # 一张渲染帧都没有时就不报数 —— 宁可不说，不能说错。
            out.append("⚠️ 参考截图**只有宽屏这一档**"
                       + (f"（和上面那几帧同宽，{int(main_w)}px）" if main_w else "")
                       + "，我们没有参考页面的窄屏版本。"
                       "所以响应式条目**不要拿窄屏图去和参考图比**，只看被评页面自己在该宽度下"
                       "是否成立：有没有横向滚动条/内容被裁掉、文字或元素有没有叠在一起、"
                       "导航有没有塌成不可用、点击目标有没有小到点不动。"
                       "这几张图就是响应式条目**唯一的证据**——判得出就按看到的判，"
                       "看不出来才按无法确认处理。")
        elif kind == "fixture":
            w = seg[0].get("width")
            out.append(f"### {_ordinal_span(lo, hi)}：极端数据截图（**被评页面**"
                       + (f"，宽 {int(w)}px" if w else "") + "）")
            out.append("⚠️ 这几张**不是模型生成的另一个页面**，也不是模型把数据写错了。"
                       "它们是拿**同一个页面**、由我们在浏览器里就地把它那组列表/表格的数据"
                       "换成极端数据之后重新截的图：")
            for k, s in enumerate(seg):
                name = s.get("fixture") or ""
                desc = FIXTURE_JUDGE_DESC.get(name, "注入极端数据之后重新截的")
                out.append(f"- 第 {lo + k} 张（{name}）：{desc}")
            out.append("因此：数据内容本身的变化（条数、文字被换掉）**一律不算缺陷**，"
                       "只看**布局与提示**在这种数据下是否还成立。这几张图只用来判"
                       "「空状态 / 长列表 / 长文本 / 多语言」这类条目；页面的正常样子看前面的渲染帧。")
        else:
            out.append(f"### {_ordinal_span(lo, hi)}：被评页面的其它截图")
        out.append("")
        i = j

    return "\n".join(out).rstrip() + "\n"


# 「错误来源」最多列几份。5 份参考时逐维度把另外 4 份全列一遍 = 6 维 × 4 = 24 行，
# 出题模型会淹在里面；只列最容易混的那几份，判分力集中在真正会混的对照上。
MAX_WRONG_SOURCES = 2
_RISK_RANK = {"high": 0, "mid": 1, "medium": 1, "low": 2}


def _wrong_sources(a: str, groups, owner: str, disc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按「混淆风险」给非 owner 的份排序，取前 MAX_WRONG_SOURCES 份。

    风险优先用该 aspect 的 mix_discrim[a].confusable_risk（整条一个值，是"这个维度
    整体好不好分"），同档内按 label 稳定排序 —— 全程确定性，重跑结果一致。
    """
    others = [g for g in groups if g.get("label") != owner]
    risk = _RISK_RANK.get(str((disc.get(a) or {}).get("confusable_risk", "")).lower(), 1)
    others.sort(key=lambda g: (risk, str(g.get("label"))))
    return others[:MAX_WRONG_SOURCES]


def _attribution_rules(prompt_data, groups, gidx, src, avoid=None) -> List[str]:
    """多参考样本的归属条目要求 —— 这个切片的判分力全在这一段。

    单参考样本里「整体照着模仿一遍」是有效策略；多参考按侧面分派之后，正向条目
    （"配色是否与参考一致"）仍然可以被"几份都扫一眼、取个折中"糊过去。真正测得出
    aspect→source 绑定的是**反向条目**：这个维度有没有跟成**另外那一份**。
    所以每个被分派的维度必须成对出条目，且反向那条得把"错误来源长什么样"写进去，
    judge 才有东西可比 —— 只写"是否跟错来源"是判不动的。
    """
    disc = prompt_data.get("mix_discrim") or prompt_data.get("ref_discrim") or {}
    if avoid is None:
        avoid = aspect_avoid_map(prompt_data, groups)
    out = ["", f"### 这条样本是「多参考按侧面分派」（{len(groups)} 份，判分要点）",
           f"参考截图来自 {len(groups)} 个互不相干的网页（分组见上文）。上表每个维度都标了"
           "**取自哪一份**：维度做对了但跟成了另外那一份，同样算不通过。",
           "",
           "因此每个在上表里有来源的维度，都要出**成对**的条目：",
           "1. 正向：该维度是否跟上了**指定的那一份**；",
           "2. 反向（`importance` 至少 high）：该维度是否**没有**跟成另外那一份 —— "
           "条目里要写清那一份在这个维度上的具体样子，否则这条判不动。",
           "",
           "各维度两边分别长什么样（出条目时照这个写，别自己另编）："]
    for a in ASPECT_ORDER:
        if a not in src:
            continue
        by = ((disc.get(a) or {}).get("by_label")) or {}
        owner = src[a]
        og = next((x for x in groups if x.get("label") == owner), None)
        if not og:
            continue
        out.append(f"- **{ASPECT_ZH[a].split('（')[0]}** 取自第 {gidx[owner]} 份"
                   + (f'「{og.get("handle")}」' if og.get("handle") else "")
                   + f"：{by.get(owner, '')}")
        wrong = _wrong_sources(a, groups, owner, disc)
        for g in wrong:
            out.append(f"    · 错误来源 = 第 {gidx[g['label']]} 份"
                       + (f'「{g.get("handle")}」' if g.get("handle") else "")
                       + f"：{by.get(g['label'], '')}")
        skipped = len(groups) - 1 - len(wrong)
        if skipped > 0:
            out.append(f"    · （另有 {skipped} 份与本维度关系较远，不必逐一出条目）")

    # ── 反面参考（avoid）：带锚点的 invert ──────────────────────────────
    # 这是 harness 唯一啃不动的一类难度：自比截图的梯度永远指向"更像参考"，
    # 而这里要求的是"在这个维度上明确不像那一份"。所以条目必须写成负向，
    # 且**不能**顺手规定"那应该做成什么样" —— 那会把 invert 悄悄变成 copy。
    if avoid:
        out += ["", "#### 反面参考（用户明确说了「别做成那样」）"]
        for a, labels in avoid.items():
            by = ((disc.get(a) or {}).get("by_label")) or {}
            aname = ASPECT_ZH.get(a, a).split("（")[0]
            for l in labels:
                if l not in gidx:
                    continue
                g = next((x for x in groups if x.get("label") == l), None)
                out.append(f"- **{aname}** 不要做成第 {gidx[l]} 份"
                           + (f'「{(g or {}).get("handle")}」' if (g or {}).get("handle") else "")
                           + f"那样：{by.get(l, '')}")
        out += [f"  请为上面每一条出 **1-2 条负向条目**（`importance` 至少 high、`ref_aspect` 填该维度）："
                "页面在这个维度上是否**没有**做成那一份的样子（把那一份的具体样子写进条目，"
                "否则判不动）。",
                "  ⚠️ 判分方向：做成了那样 = 不通过；做成**别的任何说得通的样子**都算通过。"
                "不要出「必须做成 XX」这类条目 —— 那是把 invert 改成了 copy，模型照抄反而得分。"]

    # 反向分派：有的份既不owns、也没被点名当反面锚点（老数据才会出现）。
    dis = distractor_groups(groups, src, avoid)
    if dis:
        names = "、".join(
            f"第 {gidx[g['label']]} 份" + (f'「{g.get("handle")}」' if g.get("handle") else "")
            for g in dis)
        out += ["",
                f"**干扰份**：{names} 在上表里**没有任何维度**取自它 —— 用户没打算从它借东西。",
                f"请为此出 {min(len(dis), 2)} 条负向条目（`importance` 至少 high、"
                "`ref_aspect` 填最可能被误借的那个维度）：页面是否**没有**从这一份借走"
                "它的标志性做法（把那份的标志性特征具体写进条目里，否则判不动）。"]
    out += ["",
            "注意：**不要出跨份的一致性条目**（比如「两份的强调色是否统一」）—— 它们本来就是"
            "不同的网站，用户也只从每份借一部分，这种条目没有正确答案。"]
    return out


def _mix_attribution_rules(prompt_data, groups, gidx, src) -> List[str]:
    """旧名。"""
    return _attribution_rules(prompt_data, groups, gidx, src)


def response_screenshot_note(n: int, meta: Dict[str, Any] = None) -> str:
    """告诉**被测模型**它看到了什么（英文，step2 / harness 两条路共用）。

    不分语言：prompts/gen_response_vref.txt 是全英文脚手架（只有 user_prompt 那一段
    随样本可能是中文），所以这里固定输出英文。

    这些帧是参考页面**一次交互演示的按时序抽帧**（500 个样本里 494 个的 actions.yaml
    含 hover/click/type/press_key，只有 6 个是纯滚动），不是"从上到下的滚动位置"。
    模板原来那句 "consecutive scroll positions / together they show the full design"
    对绝大多数样本是假的：模型会把同一处元素的 hover 态当成"页面的下一屏"去复刻，
    还会为没拍到的区域凭空补设计。

    多参考分支只交代「哪几张属于同一个网站」这个事实（看图也能看出来）——**不交代**
    每一份长什么样、哪个侧面该跟哪一份，那正是用户指令里要它自己读懂的考点。
    """
    groups = ref_groups_of(meta)
    if groups:
        spans, ok = [], True
        for g in groups:
            lo, hi = g["shot_range"]
            if lo < 1 or hi < lo:
                ok = False
                break
            spans.append(f"screenshot_{lo}" if lo == hi else f"screenshot_{lo}-screenshot_{hi}")
        total = groups[-1]["shot_range"][1] if ok else -1
        # 份边界必须与真正喂进去的图数严格对齐。对不上（meta 与磁盘上的截图不同步、
        # 或 loader 丢了图）就绝不能报 span —— 那会把"哪几张属于同一个网站"讲反，
        # 比不讲更糟：模型会照着错的分组去分配侧面，最后错在我们身上。
        if not ok or total != n:
            logger.warning(
                f"[screenshot_note] 多参考 meta 与图数不符: groups cover={total} vs images={n}，"
                f"退化为不给分组边界"
            )
            listing = ""
        else:
            listing = "; ".join(f"page {i} = {sp}" for i, sp in enumerate(spans, 1))
        where = f" ({listing})" if listing else ""
        order = (
            "The screenshots are attached in that exact order: the 1st image is screenshot_1, "
            f"the 2nd is screenshot_2, and so on up to screenshot_{n}. "
            if listing else
            "Frames of the same page are adjacent, but you are not told where one page's frames "
            "end and the next page's begin — infer it from what you see. "
        )
        return (
            f"{n} screenshots of {len(groups)} DIFFERENT, UNRELATED existing web pages"
            f"{where}. {order}Within each page the frames are in chronological order, taken during a "
            "single interactive walkthrough of that page (scrolling, hovering, clicking, typing, "
            "pressing keys), so two frames of the same page may differ because the page was "
            "scrolled, OR because of a hover highlight, an opened menu, a switched tab, or a "
            "state change.\n"
            "These pages do NOT belong together and are NOT variations of one design: do not "
            "merge them into a single design language, and do not treat their disagreements as "
            "inconsistency to be reconciled. Read the request carefully — it tells you which "
            "page to take which aspect from, and taking an aspect from the wrong page is as "
            "wrong as ignoring the references. Nothing forces you to borrow from every page.\n"
            "The frames do not cover the whole of any page or all of its states — do not invent "
            "design features for parts you cannot see."
        )
    if n <= 1:
        return (
            "1 screenshot of an existing web page. It is a single moment from an interactive "
            "walkthrough of that page — not necessarily the top of the page, and possibly a "
            "state in the middle of some interaction. You cannot see the rest of the page or "
            "its other states, so do not invent design features for what is not shown."
        )
    return (
        f"{n} screenshots of ONE existing web page (not {n} different sites). They are frames "
        f"taken in chronological order during a single interactive walkthrough of that page "
        f"(screenshot_1 earliest, screenshot_{n} latest), attached in that order. The "
        "walkthrough includes scrolling, hovering, clicking, typing and key presses, so two "
        "adjacent frames may differ because the page was scrolled, OR because of a hover "
        "highlight, an opened menu/overlay, a switched tab, or an application state change.\n"
        "Use them together to understand one page's design and interaction, but do not assume "
        "they cover the whole page or all of its states — do not invent design features for "
        "areas or states that are not shown."
    )


def build_directive_block(prompt_data: Dict[str, Any]) -> str:
    """渲染注入 checklist 相关 prompt 的「参考跟随指令矩阵」段落。

    没有 ref_directives 的样本（老数据）返回空字符串，模板里该段自然消失。

    **种子模式**（`ref_checklist_seeds` 非空，v5 题库）：风格 / 归属 / 反面 / 题面
    硬指标条目已由程序按题面出全（见 utils/checklist/checklist_seeds.py），VLM 只出
    `ref_aspect == ""` 的内容 / 结构 / 导航 / 功能条目。此时矩阵表仅作**上下文**
    存在（让 VLM 认出哪些维度不该由它出题），不再附「各档硬性要求 / 成对归属条目 /
    ref_aspect 必填」这些让 VLM 自己出风格题的段落；critical 配额也换成扣掉种子已占
    后**留给 VLM 的余额**。
    """
    directives = get_directives(prompt_data)
    if not directives:
        return ""

    seeded = bool(prompt_data.get("ref_checklist_seeds"))
    by_d = aspects_by_directive(directives)
    lines: List[str] = [
        "以下矩阵是该样本**权威的参考跟随要求**，由数据集在生成用户指令时就已确定。",
        "用户指令的散文表述可能含糊，**矩阵优先**：矩阵与你对散文的理解冲突时，以矩阵为准。",
        "",
        "| 维度 | 指令 | 含义 |",
        "|---|---|---|",
    ]
    verb = {
        "copy": "严格沿用截图的做法",
        "adapt": "借截图的调性但按自己需要调整",
        "invert": "刻意跟截图反着来",
        "specify": "按需求里点名的硬指标办，截图在这一面不作数",
        "ignore": "不参考截图，自由发挥",
    }
    groups = ref_groups_of(prompt_data)
    src = aspect_owner_map(prompt_data, groups)
    avoid = aspect_avoid_map(prompt_data, groups)
    gidx = group_index(groups)
    if groups:
        lines[3] = "| 维度 | 指令 | 取自 / 反面 | 含义 |"
        lines[4] = "|---|---|---|---|"
    for a in ASPECT_ORDER:
        d = directives.get(a)
        if not d:
            continue
        if groups:
            g = next((x for x in groups if x.get("label") == src.get(a)), None)
            if d == "specify":
                # 这一面的来源是**题面**，不是任何一份参考。写成「第 N 份」会把
                # judge/出题模型引回图里去找真值（§2.2）。
                say = (spec_of(prompt_data, a).get("say_zh")
                       or spec_of(prompt_data, a).get("say_en") or "").strip()
                where = "题面" + (f'：「{say}」' if say else "")
            elif g:
                where = (f'第 {gidx[g["label"]]} 份'
                         + (f'「{g.get("handle")}」' if g.get("handle") else ""))
            elif avoid.get(a):
                # 反面锚点：这个维度没有"照着做"的来源，只有"别做成这几份那样"
                where = "反面 = " + "、".join(
                    f'第 {gidx[l]} 份' + (f'「{x.get("handle")}」' if (x := next(
                        (y for y in groups if y.get("label") == l), {})).get("handle") else "")
                    for l in avoid[a] if l in gidx)
            else:
                where = "—"
            lines.append(f"| {ASPECT_ZH[a]} | **{d}** | {where} | {verb.get(d, d)} |")
        else:
            lines.append(f"| {ASPECT_ZH[a]} | **{d}** | {verb.get(d, d)} |")

    bucket = prompt_data.get("ref_bucket", "?")

    if seeded:
        # 种子模式：风格/归属/反面/spec 条目程序已出全，矩阵表只作上下文。
        # 不再教 VLM 出风格题（各档硬性要求 / 成对归属 / ref_aspect 必填都撤掉），
        # 只圈定它的职责范围，并给它扣除种子占用后的 critical 余额。
        from ..checklist.checklist_seeds import (          # 延迟导入避免模块级循环
            remaining_critical_quota, seed_items,
        )
        seeds = seed_items(prompt_data)
        n_seed = len(seeds)
        n_seed_crit = sum(1 for it in seeds if it.get("importance") == "critical")
        lo, hi = remaining_critical_quota(prompt_data, seeds)
        quota_hint = (f"再出 {lo}-{hi} 条 critical" if lo != hi
                      else (f"再出 {lo} 条 critical" if lo else "**不要再出 critical**"))
        lines.append("")
        lines += [
            "### 你的职责范围（重要）",
            f"上表这些**参考风格 / 归属 / 反面 / 题面硬指标**维度的条目，"
            f"已由程序按题面出全（本样本共 {n_seed} 条，其中 {n_seed_crit} 条 critical），"
            "会在你返回后自动并入最终 checklist。",
            "**你只负责题面里的内容 / 结构 / 导航 / 功能要求**，这些条目的 "
            "`ref_aspect` 一律填空字符串 `\"\"`。",
            "",
            "⚠️ **不要**为上表任何维度（copy / adapt / invert / specify / ignore）再出条目 —— "
            "凡是 `ref_aspect` 非空的条目都会被程序删掉，白占你的产出预算。上表只是让你认出"
            "「哪些维度已经有人管了」，不是让你照它出题。",
            "",
            f"### critical 配额（ref_bucket = {bucket}）",
            f"种子已占掉 {n_seed_crit} 条 critical。你在自己的内容/结构条目里**{quota_hint}**"
            "（超出会被程序截断）。critical 只留给「定义页面是什么」的根本性内容要求"
            "（如主题、核心功能、核心板块），不要给装饰细节。",
        ]
    else:
        if groups:
            lines += _attribution_rules(prompt_data, groups, gidx, src, avoid)

        lines.append("")
        lines.append("### 各档的硬性要求")
        for d in ("copy", "adapt", "invert", "ignore"):
            if not by_d[d]:
                continue
            names = "、".join(ASPECT_ZH[a].split("（")[0] for a in by_d[d])
            rule = _DIRECTIVE_RULE[d].format(n=DIRECTIVE_MIN_ITEMS[d])
            lines.append(f"\n- 适用维度：{names}\n  {rule}")

        lines.append("")
        lines.append(
            "### 条目的 ref_aspect 标注（必填）\n"
            "凡是从参考截图推导出来的条目，必须加 `\"ref_aspect\"` 字段，"
            f"取值为 {', '.join(ASPECT_ORDER)} 之一，用于校验上面的配额。"
            "与截图无关（纯内容/结构/功能要求）的条目，`ref_aspect` 填空字符串 `\"\"`。"
        )

        lo, hi = critical_quota(prompt_data)
        lines.append(
            f"\n### critical 配额（ref_bucket = {bucket}）\n"
            f"本样本**必须恰好标记 {lo}-{hi} 条 critical**，超出会被程序截断。"
            + ("adapt / invert / ignore 维度的条目不得标 critical。"
               if (by_d["adapt"] or by_d["invert"]) else "")
        )

    if wants_content_leak_check(prompt_data):
        lines.append(
            "\n### 内容泄漏检查（本桶必加，1-2 条）\n"
            "本样本参考强度很高，模型被明确要求「只借设计语言，不复用截图里的文字/条目/行业内容」。"
            "请生成 1-2 条 Content 维度的条目检查**没有发生内容泄漏**，写成正向提问，"
            "`expected_answer` 填 `Yes`，`ref_aspect` 填 `\"\"`。\n"
            "  - 示例：「页面正文是否全部围绕用户指定的主题撰写，未出现参考截图中的品牌名、"
            "条目名称或行业术语？」"
        )

    lines.append(
        "\n### 交付形态检查（必加，恰好 1 条）\n"
        "模型被要求输出**单文件 HTML，CSS/JS 内联，不得引用任何外部资源**。"
        "请生成恰好 1 条 Structure 维度条目检查这一点（`ref_aspect` 填 `\"\"`，importance 填 `medium`）。\n"
        "  - 示例：「页面是否为自包含单文件（样式与脚本内联，无外部 css/js/字体/图片链接）？」"
    )
    return "\n".join(lines)


def build_response_directive_block(prompt_data: Dict[str, Any], lang: str = "en") -> str:
    """渲染注入 step2（被测模型）system prompt 的参考跟随说明。

    v3 的 gen_response_vref.txt 无条件说「adopt the visual design style from the
    reference screenshots」，对 ignore/invert 维度而言与用户指令直接矛盾 —— 模型两条路
    都不干净，纯加噪声。这里按矩阵逐维度给出实话。
    """
    directives = get_directives(prompt_data)
    if not directives:
        return (
            "- **Visual style**: adopt the reference screenshots' design language "
            "where the prompt asks you to, and use your own judgement elsewhere."
        )
    en = {
        "layout": "layout structure (section order, grid, information hierarchy)",
        "palette": "colour scheme (ground / primary / accent, light-vs-dark)",
        "typography": "typography (typeface character, size hierarchy, leading, weight contrast)",
        "component": "component styling (cards / buttons / inputs, radius, shadow, borders)",
        "interaction": "interaction and motion (navigation pattern, hover feedback, scroll behaviour)",
        "atmosphere": "overall character (tone, information density, sense of whitespace)",
    }
    verb = {
        "copy": "**follow the reference closely**",
        "adapt": "**borrow the reference's feel, but adjust it to fit this page**",
        "invert": "**deliberately go the OPPOSITE way from the reference** — copying it here is wrong",
        # specify 的真值来自题面，不来自任何一份参考；照抄截图在这一面反而是错的
        "specify": "**follow the exact value the request names** — the reference does NOT decide this aspect",
        "ignore": "**ignore the reference entirely and decide for yourself**",
    }
    groups = ref_groups_of(prompt_data)
    if groups:
        # 多参考：逐维度点明"跟哪一份"，并给出那一份是第几张到第几张。不给这个映射，
        # 模型只能猜，等于把一道归属题降级成"随便照一份"。
        src = aspect_owner_map(prompt_data, groups)
        gidx = group_index(groups)
        lines = [f"- The {len(groups)} reference pages are **different, unrelated websites**, "
                 f"shown in this order:"]
        lines += ["  " + l for l in group_lines(groups, zh=False).split("\n")]
        lines.append("  Each aspect below follows exactly ONE of them. Following the wrong page "
                     "for an aspect counts as failing it, and copying any single page wholesale "
                     "fails most of them.")
        for a in ASPECT_ORDER:
            d = directives.get(a)
            if not d:
                continue
            if a in src and src[a] in gidx:
                lines.append(f"- For {en[a]} — {verb.get(d, d)}, following **Page {gidx[src[a]]}** only.")
            else:
                lines.append(f"- For {en[a]} — {verb.get(d, d)}.")
        av = aspect_avoid_map(prompt_data, groups)
        for a, labels in av.items():
            which = ", ".join(f"Page {gidx[l]}" for l in labels if l in gidx)
            if which:
                lines.append(f"- For {en[a]} — make it clearly NOT like {which}. Any other "
                             f"sensible choice is fine; resembling {which} here is a failure.")
        dis = distractor_groups(groups, src, av)
        if dis:
            which = ", ".join(f"Page {gidx[g['label']]}" for g in dis if g.get("label") in gidx)
            lines.append(f"- {which}: borrow NOTHING from it — no aspect of this page is meant "
                         f"to come from there.")
        return "\n".join(lines)

    by_d = aspects_by_directive(directives)
    lines = []
    for d in ("copy", "adapt", "invert", "ignore"):
        if not by_d[d]:
            continue
        names = "; ".join(en[a] for a in by_d[d])
        lines.append(f"- For {names} — {verb.get(d, d)}.")
    return "\n".join(lines)


# ── 条目文本归一（去重用）────────────────────────────────────────────────

_PUNCT = re.compile(r"[\s，。？、；：（）()【】\[\]\"'“”‘’,.?!;:/\\|—\-_~`*#>]+")


def normalize_question(q: str) -> str:
    """归一化 question 用于去重：去标点空白、统一大小写、去掉无信息量的套话前缀。"""
    s = (q or "").strip().lower()
    s = _PUNCT.sub("", s)
    for filler in ("页面是否", "是否", "该页面", "整体"):
        if s.startswith(filler):
            s = s[len(filler):]
    return s


def dedupe_items(items: List[Dict[str, Any]], jaccard_threshold: float = 0.85
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """去重：先精确匹配归一化文本，再对剩下的做字符 3-gram Jaccard 近似去重。

    v3 只做小写精确匹配，被 verify judge 改写过的近义条目会重复计分。
    返回 (保留的条目, 丢弃条数)。
    """
    def grams(s: str) -> set:
        return {s[i:i + 3] for i in range(max(1, len(s) - 2))} if s else set()

    kept: List[Dict[str, Any]] = []
    seen_exact: set = set()
    seen_grams: List[set] = []
    dropped = 0
    for item in items:
        norm = normalize_question(item.get("question", ""))
        if not norm or norm in seen_exact:
            dropped += 1
            continue
        g = grams(norm)
        dup = False
        for prev in seen_grams:
            inter = len(g & prev)
            if inter and inter / len(g | prev) >= jaccard_threshold:
                dup = True
                break
        if dup:
            dropped += 1
            continue
        seen_exact.add(norm)
        seen_grams.append(g)
        kept.append(item)
    return kept, dropped
