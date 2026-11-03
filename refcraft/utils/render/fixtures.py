# -*- coding: utf-8 -*-
"""对抗内容注入（adversarial data fixtures）。

docs/设计总纲.md §2.5 把这条轴排在"极低出题成本、Agent headroom 大"那一档：
harness 的自比循环只会看它自己造的那份漂亮假数据，**它不知道要去查** 0 条 / 300 条 /
80 字符长串 / CJK+emoji 这几种真实数据。工具再全，不查等于没有。

做法：页面照常渲染完之后，就地把它的"重复列表"换成极端数据，再量一遍几何。
不改 HTML 源码、不要求模型配合任何约定 —— 所有模型都被同一套 DOM 操作对待。

⚠️ 不适用要如实上报（`applied: false`）。找不到重复列表的页面（纯 landing page）
   上这几条探针必须记 blocked，**不能记 0**：把"没测"说成"没做到"会给所有静态页
   凭空扣分，看起来像 headroom，其实是噪声。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 80 个不带空格的 ASCII 字符：任何靠单词换行（word-wrap）活着的布局都会在这里露馅
LONG_STRING_80 = "Wxq" + "abcdefghijklmnopqrstuvwxyz0123456789" * 2 + "ABCDE"
LONG_STRING_80 = LONG_STRING_80[:80]

# CJK 全角 + emoji：字宽、行高、字体回退三件事一起考
CJK_EMOJI = "客户订单编号壹贰叁肆伍陆柒捌玖拾🎉🚀✅ 长文本换行测试用例 — 全角标点，句号。"

FIXTURE_NAMES = ("zero_rows", "many_rows_300", "long_string_80ch", "cjk_emoji")

# ── 找"重复列表"：页面里最像数据列表的那一组兄弟元素 ────────────────────────
# 判据：同一父节点下 ≥3 个签名相同（tag + class 集合）的可见子元素，按
# 「个数 × 单项面积」取最大的一组。table>tbody>tr、ul>li、grid>div.card 都能落网。
FIND_REPEAT_JS = r"""
(() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
  };
  const sig = el => el.tagName + '.' + Array.from(el.classList).sort().join('.');
  let best = null;
  const all = document.querySelectorAll('*');
  const cap = Math.min(all.length, 6000);
  for (let i = 0; i < cap; i++) {
    const p = all[i];
    const kids = Array.from(p.children).filter(vis);
    if (kids.length < 3) continue;
    const bySig = {};
    for (const k of kids) (bySig[sig(k)] = bySig[sig(k)] || []).push(k);
    for (const s in bySig) {
      const g = bySig[s];
      if (g.length < 3) continue;
      const r = g[0].getBoundingClientRect();
      const score = g.length * Math.max(1, r.width * r.height);
      if (!best || score > best.score) best = {score, parent: p, sig: s, n: g.length};
    }
  }
  if (!best) return {ok: false, why: '页面里没有 >=3 个可见同构兄弟节点的重复列表'};
  window.__vrefRepeat = best;
  const r0 = best.parent.getBoundingClientRect();
  return {ok: true, sig: best.sig, n_items: best.n,
          parent_tag: best.parent.tagName.toLowerCase(),
          parent_rect: {w: Math.round(r0.width), h: Math.round(r0.height)}};
})()
"""

# 可见文本快照（用于判空态：删空之后有没有**新**文案冒出来）
VISIBLE_TEXT_JS = r"""
(() => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n, i = 0;
  while ((n = w.nextNode()) && i < 4000) {
    const t = (n.nodeValue || '').trim();
    if (t.length < 2) continue;
    const el = n.parentElement;
    if (!el) continue;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    out.push(t.slice(0, 120));
    i++;
  }
  return out;
})()
"""

_ITEMS_JS = r"""
  const b = window.__vrefRepeat;
  if (!b || !b.parent || !b.parent.isConnected) return {applied: false, why: '重复列表已不在文档里'};
  const sig = el => el.tagName + '.' + Array.from(el.classList).sort().join('.');
  const items = Array.from(b.parent.children).filter(el => sig(el) === b.sig);
  if (items.length < 3) return {applied: false, why: '注入时重复项不足 3 个'};
"""

APPLY_JS: Dict[str, str] = {
    # 0 条：把列表清空。空态文案有没有出现，交给文本快照比对来判。
    "zero_rows": "(() => {" + _ITEMS_JS + r"""
  const n0 = items.length;
  for (const el of items) el.remove();
  return {applied: true, n_items_before: n0, n_items_after: 0};
})()""",

    # 300 条：克隆第一项到 300 个。考的是长列表下的溢出/性能/分页缺失。
    "many_rows_300": "(() => {" + _ITEMS_JS + r"""
  const n0 = items.length;
  const src = items[0];
  const frag = document.createDocumentFragment();
  for (let i = n0; i < 300; i++) {
    const c = src.cloneNode(true);
    c.setAttribute('data-vref-clone', String(i));
    frag.appendChild(c);
  }
  items[items.length - 1].after(frag);
  return {applied: true, n_items_before: n0,
          n_items_after: Array.from(b.parent.children).filter(el => sig(el) === b.sig).length};
})()""",

    # 80 字符不断行长串：塞进每一项的最长文本节点 + 页面主标题。
    "long_string_80ch": "(() => {" + _ITEMS_JS + r"""
  const S = %(LONG)s;
  let k = 0;
  const stuff = root => {
    let best = null;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = (n.nodeValue || '').trim();
      if (t.length >= 3 && (!best || t.length > best.nodeValue.trim().length)) best = n;
    }
    if (best) { best.nodeValue = S; k++; }
  };
  for (const el of items.slice(0, 12)) stuff(el);
  const h = document.querySelector('h1, h2, header, .title');
  if (h) stuff(h);
  return {applied: k > 0, n_targets: k, why: k ? '' : 'no text node to replace'};
})()""" % {"LONG": '"' + LONG_STRING_80 + '"'},

    # CJK + emoji：字宽/行高/字体回退。中英混排页面在这里最容易撑破卡片。
    "cjk_emoji": "(() => {" + _ITEMS_JS + r"""
  const S = %(CJK)s;
  let k = 0;
  const stuff = root => {
    let best = null;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = (n.nodeValue || '').trim();
      if (t.length >= 3 && (!best || t.length > best.nodeValue.trim().length)) best = n;
    }
    if (best) { best.nodeValue = S; k++; }
  };
  for (const el of items.slice(0, 12)) stuff(el);
  return {applied: k > 0, n_targets: k, why: k ? '' : 'no text node to replace'};
})()""" % {"CJK": '"' + CJK_EMOJI + '"'},
}

# 空态文案的字面证据（新出现的文案里出现这些词也算）
EMPTY_STATE_PAT = (
    "no ", "none", "empty", "nothing", "not found", "no results", "no data",
    "暂无", "没有", "空空", "无数据", "无结果", "还没有", "尚无", "未找到",
)


def empty_state_from_texts(before: List[str], after: List[str]) -> Dict[str, Any]:
    """比对删空前后的可见文案，判断有没有空态。

    两条证据都算：
      1. 出现了**之前没有**的可见文案 —— 页面本来藏着一个空态块，现在露出来了；
      2. 可见文案里有空态词（本来就写死在页面上的"暂无数据"）。
    两条都没有 → 列表没了、页面一句话都不说，判 fail。
    """
    b = set(before)
    new = [t for t in after if t not in b]
    hit = [t for t in after if any(p in t.lower() for p in EMPTY_STATE_PAT)]
    return {
        "new_texts": new[:5],
        "n_new": len(new),
        "keyword_hits": hit[:5],
        "present": bool(new or hit),
    }
