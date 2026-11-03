#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""确定性测量：一次 Playwright 会话，把「能程序判的东西」全量量出来，落成 measure.json。

为什么要有这一层（见 docs/设计总纲.md §3.5）：
让 VLM 判「配色是否与参考一致」，它倾向给 7-8；判「响应式」时手里连窄屏截图都没有，
只会保底给 5。**能程序判的一律不给 VLM 判** —— 但程序判分的前提是先有证据。
这个模块只负责**取证**，不负责判定：判定在 utils/render/probes.py，规格在 step1a/step4a。

刻意分成"取证 / 判定"两层的原因：取证要起浏览器（慢、可能崩），判定要能在改了阈值
之后零成本重算。measure.json 存的都是原始数值，不存 pass/fail。

铁律：**量不到 ≠ 0 分**。任何一项拿不到证据（比如背景是图片，算不出对比度），
这里必须把对应字段写成 null 并给出 `why`，让 probes 记 `blocked`。
把"没量到"当成"没做到"会凭空造出区分度，比不判还糟。

用法（调试单个 HTML）：
    python -m utils.render.measure path/to/index.html /tmp/out --viewports 1920,1280,768,390
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from .fixtures import (
    APPLY_JS,
    FIND_REPEAT_JS,
    FIXTURE_NAMES,
    VISIBLE_TEXT_JS,
    empty_state_from_texts,
)
from ..core.log import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 3       # v3: 多了 anchors / load_anim 两个顶层块

# 每个宽度配一个真实设备高度：tap target / 首屏相关的量只有在真实高宽比下才有意义
VIEWPORT_HEIGHTS = {1920: 1080, 1280: 720, 1024: 768, 768: 1024,
                    414: 896, 390: 844, 375: 667}
DEFAULT_HEIGHT = 720

# 对比度门槛（WCAG AA/AAA）：这里只量 ratio，门槛留给 probes 决定
WCAG_AA_NORMAL = 4.5
TAP_TARGET_MIN = 44          # WCAG 2.5.5 / iOS HIG
SMALL_TEXT_PX = 12.0


def _vp_height(w: int) -> int:
    return VIEWPORT_HEIGHTS.get(int(w), DEFAULT_HEIGHT)


# ─────────────────────────── 页内 JS ───────────────────────────
# 共享前缀：选择器路径、可见性判断、颜色解析、相对亮度。
# 每段 JS 都独立自包含（Playwright 每次 evaluate 是独立表达式），所以用字符串拼接
# 而不是往 window 上挂函数 —— 挂了会被被测页面的脚本覆盖或与之冲突。
_PRELUDE = r"""
const __px = v => { const f = parseFloat(v); return Number.isFinite(f) ? f : 0; };
// 合法标识符才敢拼进选择器：tailwind 那种 `md:flex` / `w-1/2` 拼进去会让整条选择器报错
const __ident = s => /^[A-Za-z_][\w-]*$/.test(s);
const __step = (e) => {
  let s = e.tagName.toLowerCase();
  const cls = (e.getAttribute('class') || '').trim().split(/\s+/).filter(__ident)[0];
  if (cls) s += '.' + cls;
  const par = e.parentElement;
  if (par) {   // 同标签兄弟 → 加 nth-of-type，否则两个 button.tab 会指向同一条路径
    const same = Array.from(par.children).filter(c => c.tagName === e.tagName);
    if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(e) + 1) + ')';
  }
  return s;
};
// 便宜版：只作证据展示用，热路径（每个文字节点都要算）不做唯一性查询
const __cssPath = (el) => {
  const parts = [];
  let e = el, depth = 0;
  while (e && e.nodeType === 1 && depth < 5) {
    if (e.id && __ident(e.id)) { parts.unshift('#' + e.id); break; }
    parts.unshift(__step(e));
    e = e.parentElement; depth++;
  }
  return parts.join('>');
};
// 贵版：给点击探针用 —— 选择器必须能唯一定位，否则 querySelector 点的是另一个元素，
// 「点击后 DOM 不变」的结论就落在了错的元素上
const __uniqPath = (el) => {
  const parts = [];
  let e = el, depth = 0;
  while (e && e.nodeType === 1 && depth < 8) {
    if (e.id && __ident(e.id)) { parts.unshift('#' + e.id); }
    else parts.unshift(__step(e));
    const p = parts.join('>');
    try { if (document.querySelectorAll(p).length === 1) return p; } catch (err) { return p; }
    if (e.id && __ident(e.id)) return p;
    e = e.parentElement; depth++;
  }
  return parts.join('>');
};
const __visible = (el, st) => {
  st = st || getComputedStyle(el);
  if (st.display === 'none' || st.visibility === 'hidden') return false;
  if (__px(st.opacity) < 0.05) return false;
  return true;
};
// 解析 rgb()/rgba()/hex/颜色名（浏览器 computed style 只会给 rgb/rgba）
const __rgba = (s) => {
  if (!s) return null;
  const m = String(s).match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const p = m[1].split(',').map(x => parseFloat(x.trim()));
  if (p.length < 3 || p.some(x => !Number.isFinite(x))) return null;
  return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
};
const __hex = (c) => c ? '#' + [c.r, c.g, c.b].map(v =>
  Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('') : null;
const __lum = (c) => {
  const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
};
const __ratio = (a, b) => {
  const l1 = __lum(a), l2 = __lum(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
};
const __over = (fg, bg) => {  // fg 半透明时压到 bg 上
  const a = fg.a === undefined ? 1 : fg.a;
  if (a >= 0.999) return fg;
  return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a),
           b: fg.b * a + bg.b * (1 - a), a: 1 };
};
// 有效背景色：往上找第一个不透明背景。中途遇到背景图就认"量不到"（返回 unknown）,
// 不能拿白色顶上去 —— 那会把深色背景上的浅字算成 1:1 的假失败。
const __effBg = (el) => {
  let e = el, guard = 0, acc = null, img = false;
  while (e && e.nodeType === 1 && guard < 24) {
    const st = getComputedStyle(e);
    if (st.backgroundImage && st.backgroundImage !== 'none') img = true;
    const c = __rgba(st.backgroundColor);
    if (c && c.a > 0.02) {
      acc = acc ? __over(acc, c) : c;
      if ((acc.a === undefined ? 1 : acc.a) > 0.98) return { color: acc, image: img, ok: !img };
    }
    e = e.parentElement; guard++;
  }
  // 走到根还没有不透明底色 → 画布是白的（浏览器默认），但若中途见过背景图则不可信
  const white = { r: 255, g: 255, b: 255, a: 1 };
  const base = acc ? __over(acc, white) : white;
  return { color: base, image: img, ok: !img };
};
const __clipped = (el) => {   // 祖先有横向裁剪 → 它伸出去不算页面溢出
  let e = el.parentElement, guard = 0;
  while (e && e.nodeType === 1 && guard < 24) {
    const ox = getComputedStyle(e).overflowX;
    if (ox === 'hidden' || ox === 'clip' || ox === 'auto' || ox === 'scroll') return true;
    e = e.parentElement; guard++;
  }
  return false;
};
const __hash = (s) => { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0; return h; };
"""

GEOM_JS = "(MAXN) => {" + _PRELUDE + r"""
  const de = document.documentElement, b = document.body;
  const vw = window.innerWidth;
  const sw = Math.max(de.scrollWidth || 0, b ? (b.scrollWidth || 0) : 0);
  const sh = Math.max(de.scrollHeight || 0, b ? (b.scrollHeight || 0) : 0);
  const nodes = [];
  const els = document.querySelectorAll('*');
  const lim = Math.min(els.length, MAXN);
  for (let i = 0; i < lim; i++) {
    const el = els[i];
    const st = getComputedStyle(el);
    if (!__visible(el, st)) continue;
    if (st.position === 'fixed') continue;      // fixed 不进文档流，不产生页面横向溢出
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const right = r.right + window.scrollX;
    if (right > vw + 1 && !__clipped(el)) {
      nodes.push({ sel: __cssPath(el), right: Math.round(right), w: Math.round(r.width),
                   tag: el.tagName.toLowerCase() });
    }
  }
  nodes.sort((x, y) => y.right - x.right);
  return { viewport_width: vw, scroll_width: Math.round(sw), scroll_height: Math.round(sh),
           client_width: de.clientWidth, overflow_px: Math.max(0, Math.round(sw - vw)),
           n_elements: els.length, truncated: els.length > MAXN, overflow_nodes: nodes.slice(0, 8) };
}"""

# 文字 + 对比度一次走完（两遍 TreeWalker 太贵，而且样式可能在两遍之间变）
TEXT_JS = "(MAXN) => {" + _PRELUDE + r"""
  const out = [];
  const walker = document.createTreeWalker(document.body || document.documentElement,
                                           NodeFilter.SHOW_TEXT, null);
  let n = 0, unknownBg = 0;
  while (walker.nextNode() && n < MAXN) {
    const tn = walker.currentNode;
    const txt = (tn.nodeValue || '').trim();
    if (txt.length < 2) continue;
    const el = tn.parentElement;
    if (!el) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'title') continue;
    const st = getComputedStyle(el);
    if (!__visible(el, st)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    n++;
    const px = __px(st.fontSize);
    const bg = __effBg(el);
    const fgRaw = __rgba(st.color);
    let ratio = null;
    if (fgRaw && bg.ok) ratio = __ratio(__over(fgRaw, bg.color), bg.color);
    if (!bg.ok) unknownBg++;
    out.push({ sel: __cssPath(el), tag: tag, px: Math.round(px * 10) / 10,
               weight: st.fontWeight, len: txt.length,
               fg: __hex(fgRaw), bg: __hex(bg.color), bg_image: bg.image,
               ratio: ratio === null ? null : Math.round(ratio * 100) / 100,
               w: Math.round(r.width), h: Math.round(r.height),
               text: txt.slice(0, 60) });
  }
  return { nodes: out, n_text_nodes: n, n_unknown_bg: unknownBg, truncated: n >= MAXN };
}"""

TAP_JS = "() => {" + _PRELUDE + r"""
  const SEL = 'a[href],button,input,select,textarea,summary,[role=button],[role=tab],' +
              '[role=link],[role=checkbox],[role=switch],[onclick],[tabindex]';
  const out = [];
  document.querySelectorAll(SEL).forEach(el => {
    const st = getComputedStyle(el);
    if (!__visible(el, st)) return;
    if (st.pointerEvents === 'none') return;
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName === 'INPUT' && (t === 'hidden')) return;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    out.push({ sel: __cssPath(el), tag: el.tagName.toLowerCase(),
               w: Math.round(r.width), h: Math.round(r.height),
               label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40) });
  });
  return out.slice(0, 400);
}"""

COLORS_JS = "(MAXN) => {" + _PRELUDE + r"""
  const bgArea = {}, accent = {}, textArea = {};
  const els = document.querySelectorAll('*');
  const lim = Math.min(els.length, MAXN);
  const ACC = 'button,a,[role=button],.btn,.button,input[type=submit],input[type=button],' +
              '.badge,.tag,.chip,.cta,header,nav';
  for (let i = 0; i < lim; i++) {
    const el = els[i];
    const st = getComputedStyle(el);
    if (!__visible(el, st)) continue;
    const r = el.getBoundingClientRect();
    const area = Math.max(0, Math.min(r.width, 4000)) * Math.max(0, Math.min(r.height, 20000));
    if (area < 16) continue;
    const c = __rgba(st.backgroundColor);
    if (c && (c.a === undefined ? 1 : c.a) > 0.1) {
      const k = __hex(c);
      bgArea[k] = (bgArea[k] || 0) + area;
    }
    const fc = __rgba(st.color);
    if (fc) { const k = __hex(fc); textArea[k] = (textArea[k] || 0) + area * 0.05; }
    if (el.matches && el.matches(ACC)) {
      const ac = (c && (c.a === undefined ? 1 : c.a) > 0.1) ? c : fc;
      if (ac) {
        const mx = Math.max(ac.r, ac.g, ac.b), mn = Math.min(ac.r, ac.g, ac.b);
        if (mx - mn > 18) {          // 只把有彩度的算成品牌色，灰白黑不算
          const k = __hex(ac);
          accent[k] = (accent[k] || 0) + 1;
        }
      }
    }
  }
  const top = o => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, 8)
                        .map(([k, v]) => [k, Math.round(v)]);
  return { bg_by_area: top(bgArea), text_by_area: top(textArea),
           accent_by_count: top(accent), n_distinct_bg: Object.keys(bgArea).length };
}"""

TOKENS_JS = "(MAXN) => {" + _PRELUDE + r"""
  const S = { font_family: new Set(), font_size: new Set(), font_weight: new Set(),
              radius: new Set(), shadow: new Set(), color: new Set(), bg: new Set(),
              gap: new Set(), pad: new Set() };
  const els = document.querySelectorAll('*');
  const lim = Math.min(els.length, MAXN);
  for (let i = 0; i < lim; i++) {
    const el = els[i];
    const st = getComputedStyle(el);
    if (!__visible(el, st)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    S.font_family.add((st.fontFamily || '').split(',')[0].trim().replace(/["']/g, ''));
    S.font_size.add(st.fontSize);
    S.font_weight.add(st.fontWeight);
    if (st.borderRadius && st.borderRadius !== '0px') S.radius.add(st.borderRadius);
    if (st.boxShadow && st.boxShadow !== 'none') S.shadow.add(st.boxShadow);
    S.color.add(st.color);
    const bc = __rgba(st.backgroundColor);
    if (bc && (bc.a === undefined ? 1 : bc.a) > 0.1) S.bg.add(st.backgroundColor);
    if (st.gap && st.gap !== 'normal' && st.gap !== '0px') S.gap.add(st.gap);
    if (st.paddingTop && st.paddingTop !== '0px') S.pad.add(st.paddingTop);
  }
  let vars = 0;
  try {
    const rs = document.documentElement.style;
    for (const sheet of document.styleSheets) {
      let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
      for (const rule of rules) {
        if (rule.style) for (const p of rule.style) if (String(p).startsWith('--')) vars++;
      }
    }
  } catch (e) {}
  const o = {};
  for (const k in S) o[k] = S[k].size;
  o.css_vars = vars;
  o.font_families = Array.from(S.font_family).slice(0, 8);
  o.font_sizes = Array.from(S.font_size).slice(0, 24);
  // 阶梯的**取值**（不只是个数）：refspec 要拿参考页面的字号/间距/圆角阶梯去出题，
  // 只有个数是出不了题的（"圆角有 4 种"没法核对，"圆角档位是 4/8/16px"才行）。
  // 对候选侧是纯新增字段，token_consistency 只读上面那些个数，不受影响。
  o.font_weights = Array.from(S.font_weight).slice(0, 12);
  o.radius_values = Array.from(S.radius).slice(0, 16);
  o.shadow_values = Array.from(S.shadow).slice(0, 8);
  o.gap_values = Array.from(S.gap).slice(0, 16);
  o.pad_values = Array.from(S.pad).slice(0, 24);
  return o;
}"""

# hover-only 可用性：靠 :hover 才出现的东西，触屏上等于不存在。
# ⚠️ 必须在 PHASE1_REVEAL_JS 之前跑 —— reveal 会把 opacity:0 全掀开，掀完就看不出
# 谁本来是藏着的了。
HOVER_JS = "() => {" + _PRELUDE + r"""
  const PROPS = ['display', 'visibility', 'opacity', 'max-height', 'height',
                 'transform', 'pointer-events'];
  const found = [];
  let nRules = 0, sheetsBlocked = 0;
  const seen = new Set();
  let sheets = [];
  try { sheets = Array.from(document.styleSheets); } catch (e) { sheets = []; }
  for (const sheet of sheets) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { sheetsBlocked++; continue; }
    if (!rules) continue;
    const walk = (list) => {
      for (const rule of list) {
        if (rule.cssRules && !rule.selectorText) { walk(rule.cssRules); continue; }
        if (!rule.selectorText || rule.selectorText.indexOf(':hover') < 0) continue;
        nRules++;
        const touches = PROPS.filter(p => rule.style && rule.style.getPropertyValue(p));
        if (!touches.length) continue;
        for (const part of rule.selectorText.split(',')) {
          if (part.indexOf(':hover') < 0) continue;
          const base = part.replace(/:hover/g, '').trim();
          if (!base || seen.has(base)) continue;
          seen.add(base);
          let els = [];
          try { els = Array.from(document.querySelectorAll(base)); } catch (e) { continue; }
          for (const el of els.slice(0, 6)) {
            const st = getComputedStyle(el);
            const hidden = st.display === 'none' || st.visibility === 'hidden' ||
                           __px(st.opacity) < 0.05 || st.pointerEvents === 'none' ||
                           (st.maxHeight === '0px') || (el.getBoundingClientRect().height < 1);
            if (hidden) {
              found.push({ sel: __cssPath(el), base: base.slice(0, 80),
                           props: touches, initially_hidden: true });
              break;
            }
          }
        }
      }
    };
    walk(rules);
  }
  return { hover_rules: nRules, sheets_blocked: sheetsBlocked,
           hidden_until_hover: found.slice(0, 12) };
}"""

# 点击探针的候选：只挑「点了不该跳走」的东西
CLICK_CANDIDATES_JS = "() => {" + _PRELUDE + r"""
  // 分两档：真正的交互控件（点了该有状态变化）和纯锚点（点了只是滚动，DOM 本来就不该变）。
  // 混在一起会把"页面只有导航锚点"误判成"交互是死的" —— 那是探针的错，不是页面的错。
  const STRONG = '[data-filter],[role=tab],[aria-controls],[data-toggle],[data-target],' +
                 'summary,button,.tab,.tabs a,.accordion,.accordion-header,.faq-question,' +
                 '[onclick],[role=switch],[role=checkbox]';
  const SEL = STRONG + ',nav a[href^="#"],a[href^="#"]';
  const out = [], seen = new Set();
  document.querySelectorAll(SEL).forEach(el => {
    const st = getComputedStyle(el);
    if (!__visible(el, st) || st.pointerEvents === 'none') return;
    if (el.tagName === 'A') {
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('#') && !h.startsWith('javascript:')) return;  // 会导航，跳过
      if (el.getAttribute('target') === '_blank') return;
    }
    if (el.tagName === 'BUTTON' && (el.getAttribute('type') || '').toLowerCase() === 'submit') {
      if (el.closest('form') && el.closest('form').getAttribute('action')) return;
    }
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    const s = __uniqPath(el);
    if (seen.has(s)) return;
    seen.add(s);
    let kind = 'anchor';
    try { if (el.matches(STRONG)) kind = 'toggle'; } catch (e) {}
    out.push({ sel: s, tag: el.tagName.toLowerCase(), kind: kind,
               label: (el.textContent || '').trim().slice(0, 30) });
  });
  // 强控件排前面：探针预算有限（max_click_probes），别全花在锚点上
  out.sort((a, b) => (a.kind === b.kind) ? 0 : (a.kind === 'toggle' ? -1 : 1));
  return out;
}"""

# DOM 指纹：类名 / aria 状态 / 可见性 / 可见文字。点击后指纹不动 = 交互是死的。
DOM_SIG_JS = "(MAXN) => {" + _PRELUDE + r"""
  const els = document.querySelectorAll('*');
  const lim = Math.min(els.length, MAXN);
  let s = '', vis = 0, txt = '';
  for (let i = 0; i < lim; i++) {
    const el = els[i];
    const st = getComputedStyle(el);
    const v = __visible(el, st) ? 1 : 0;
    vis += v;
    s += el.tagName + '|' + (el.getAttribute('class') || '') + '|' +
         (el.getAttribute('aria-expanded') || '') + (el.getAttribute('aria-selected') || '') +
         (el.getAttribute('aria-hidden') || '') + (el.checked ? '1' : '') + '|' + v + ';';
    if (v && el.children.length === 0) txt += (el.textContent || '').trim().slice(0, 40) + ';';
  }
  return { sig: __hash(s), text_sig: __hash(txt), n_visible: vis, n_elements: els.length };
}"""

CLICK_ONE_JS = """(sel) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return {ok:false, why:'bad-selector'}; }
  if (!el) return {ok:false, why:'not-found'};
  try {
    el.scrollIntoView({block:'center'});
    if (el.tagName === 'SUMMARY' && el.parentElement && el.parentElement.tagName === 'DETAILS') {
      el.parentElement.open = !el.parentElement.open;
    }
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
    el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
    el.click();
  } catch (e) { return {ok:false, why:String(e).slice(0,80)}; }
  return {ok:true};
}"""


# Do not call window.scrollTo here.  A submitted page is allowed to define a
# global function named ``scrollTo``; in browsers that replaces the native
# window method.  One real output used scrollTo(selector), so our numeric
# ``window.scrollTo(0, 0)`` became querySelector(0) and aborted measurement.
# Assigning the scrolling element directly is isolated from page globals.
SCROLL_DOCUMENT_JS = """(y) => {
  const top = Math.max(0, Number(y) || 0);
  const root = document.scrollingElement || document.documentElement || document.body;
  if (root) root.scrollTop = top;
  if (document.documentElement) document.documentElement.scrollTop = top;
  if (document.body) document.body.scrollTop = top;
  return root ? root.scrollTop : 0;
}"""


# 「短数字」解析：计数动画盯的就是这种元素（"1,234" / "87%" / "共 12 条" / "$980"）。
# 长句子一律不算 —— 否则一次列表重渲染会把整页文字都记成"数字在动"。
_NUM_OF_JS = r"""
const __numOf = (s) => {
  const t = String(s == null ? '' : s).trim();
  if (!t || t.length > 16) return null;
  // 前缀只认符号，**不认字母**：放成 [^\d]{0,2} 的话 "S1"/"Q3" 这种小标题也会被当成
  // 数字指标 —— 分母虚高，judge 会以为有几个指标没动（实测踩过：一个 3 指标的页面报 5 个）。
  const m = t.replace(/[,\u00a0\s]/g, '')
             .match(/^[$\u20ac\u00a3\u00a5\uffe5#~\u2248\u7ea6\u5171\u7b2c]{0,2}([+-]?\d+(?:\.\d+)?)[^\d]{0,4}$/);
  return m ? parseFloat(m[1]) : null;
};
"""

# 页内锚点是否真的落到东西上。为什么必须程序量：judge 手里只有图和事实块，
# 「href="#pricing" 指向的 id 存不存在」在截图上根本看不出来 —— 不量就只能判
# 「无法确认」，而那一条会从分母里剔出去（§4.2 第三态），等于白出一道题。
SEMANTICS_JS = r"""() => {
  // 语义标签普查 + 标题层级。为什么必须有这一块（2026-09-02 审计）：
  // checklist 里有一类条目直接问"是否用 main/section 组织内容、标题层级是否合理"，
  // 而 judge prompt 明令「你看不到源码，凭空说源码里有什么就是编造 observed」——
  // 于是这类条目**全员判 0 且 observed=无法确认**（sid7/21、sid7/26、sid4/31 三条实测如此）。
  // 页面结构是确定性可测的，不该靠 VLM 猜，所以在这里量出来交给判分侧。
  const tags = ['main','header','footer','nav','section','article','aside','figure',
                'figcaption','h1','h2','h3','h4','h5','h6','ul','ol','table','form',
                'button','label','fieldset','legend','dialog','details','summary'];
  const out = {counts: {}, n_landmark: 0, has_main: false, single_main: false,
               headings: [], heading_skips: 0, n_h1: 0, outline_ok: false,
               section_titled: 0, n_section: 0, div_soup_ratio: null};
  for (const t of tags) {
    const n = document.getElementsByTagName(t).length;
    if (n) out.counts[t] = n;
  }
  const mains = document.getElementsByTagName('main');
  out.has_main = mains.length > 0;
  out.single_main = mains.length === 1;
  out.n_landmark = ['main','header','footer','nav','aside','section','article']
      .reduce((a, t) => a + document.getElementsByTagName(t).length, 0);

  // 标题层级：按文档顺序取 h1..h6，数"跳级"次数（h1→h3 算一次跳）
  const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  let prev = 0, skips = 0;
  for (const h of hs) {
    const lvl = +h.tagName[1];
    const txt = (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    out.headings.push({level: lvl, text: txt});
    if (prev && lvl > prev + 1) skips++;
    prev = lvl;
  }
  out.heading_skips = skips;
  out.n_h1 = document.getElementsByTagName('h1').length;
  out.outline_ok = out.n_h1 === 1 && skips === 0;

  // section/article 里有没有自己的标题（"三个板块各由标题标识"这类问题要的就是这个）
  const secs = Array.from(document.querySelectorAll('section,article'));
  out.n_section = secs.length;
  out.section_titled = secs.filter(s =>
      s.querySelector('h1,h2,h3,h4,h5,h6') || s.getAttribute('aria-label') ||
      s.getAttribute('aria-labelledby')).length;

  // div 汤指标：div 占所有元素的比例。高 = 基本没用语义标签
  const nAll = document.getElementsByTagName('*').length;
  const nDiv = document.getElementsByTagName('div').length;
  out.div_soup_ratio = nAll ? Math.round(1000 * nDiv / nAll) / 1000 : null;
  return out;
}"""


ANCHORS_JS = r"""() => {
  const out = {n_links: 0, n_hash: 0, n_resolved: 0, n_broken: 0, n_placeholder: 0,
               n_ids: 0,
               // in_nav 与顶层四个计数**同构**：少一格 judge 就得拿两套口径读同一个问题
               in_nav: {n_links: 0, n_hash: 0, n_resolved: 0, n_placeholder: 0}};
  const as = Array.from(document.querySelectorAll('a'));
  out.n_links = as.length;
  const navs = Array.from(document.querySelectorAll('nav,[role=navigation]'));
  const inNav = (el) => navs.some(n => n.contains(el));
  const resolves = (frag) => {
    let id = frag;
    try { id = decodeURIComponent(frag); } catch (e) {}
    // #top / #TOP 由 HTML 规范保证回到顶部，没有同名元素也算落到点
    if (id.toLowerCase() === 'top') return true;
    if (document.getElementById(id)) return true;
    try {
      if (typeof CSS !== 'undefined' && CSS.escape &&
          document.querySelector('[name=' + CSS.escape(id) + ']')) return true;
    } catch (e) {}
    return false;
  };
  for (const a of as) {
    const raw = a.getAttribute('href') || '';
    const nav = inNav(a);
    if (nav) out.in_nav.n_links++;
    if (raw.charAt(0) !== '#') continue;
    const frag = raw.slice(1);
    if (!frag) {                                   // href="#" 是占位，不是页内锚点
      out.n_placeholder++;
      if (nav) out.in_nav.n_placeholder++;
      continue;
    }
    out.n_hash++;
    if (nav) out.in_nav.n_hash++;
    if (resolves(frag)) {
      out.n_resolved++;
      if (nav) out.in_nav.n_resolved++;
    }
  }
  out.n_broken = out.n_hash - out.n_resolved;
  out.n_ids = document.querySelectorAll('[id]').length;
  return out;
}"""

# 加载期的数字动画（count up）。**只能在 goto 之前挂**：动画在 load 那一刻就跑完了，
# 事后再看只剩终值 —— 那正是"计数动画"条目全模型判不了的原因（终值和静态写死的
# 终值一模一样）。所以这段以 init script 注入，在页面自己的脚本之前装好观察者。
#
# 只记计数和毫秒数，一个页面文案都不留：事实块是白名单（utils/score/judge_facts.py），
# 这里留了字符串下游也带不出去，但先在源头就不采，省得下一个人以为能用。
LOAD_ANIM_INIT_JS = r"""(() => {
  try {
    if (window.__vrefAnim) return;
    const pf = () => ((typeof performance !== 'undefined' && performance.now)
                      ? performance.now() : 0);
    const S = {t0: pf(), n_events: 0, first_ms: null, last_ms: null,
               dom_loaded_ms: null, load_ms: null, capped: false, els: new Map()};
    window.__vrefAnim = S;
""" + _NUM_OF_JS + r"""
    const now = () => pf() - S.t0;
    const rec = (el, val, prev) => {
      if (!el || el.nodeType !== 1 || val === null) return;
      let r = S.els.get(el);
      if (!r) {
        if (S.els.size >= 200) { S.capped = true; return; }
        const base = (prev === null || prev === undefined) ? val : prev;
        r = {last: base, up: 0, down: 0, n: 0};
        S.els.set(el, r);
        // 没有旧值时，第一次见到只立基线：一次性写入终值（静态显示）不该算成"在动"
        if (prev === null || prev === undefined) return;
      }
      if (val > r.last) r.up++;
      else if (val < r.last) r.down++;
      else return;
      r.last = val;
      r.n++;
      S.n_events++;
      const t = now();
      if (S.first_ms === null) S.first_ms = t;
      S.last_ms = t;
    };
    const obs = new MutationObserver((muts) => {
      if (S.n_events > 20000) { S.capped = true; return; }
      for (const mu of muts) {
        try {
          if (mu.type === 'characterData') {
            const el = mu.target.parentElement;
            if (el && el.children.length === 0) {
              rec(el, __numOf(mu.target.data), __numOf(mu.oldValue));
            }
          } else if (mu.type === 'childList') {
            const el = mu.target;
            if (el && el.nodeType === 1 && el.children.length === 0) {
              rec(el, __numOf(el.textContent), null);
            }
          }
        } catch (e) {}
      }
    });
    obs.observe(document, {subtree: true, childList: true,
                           characterData: true, characterDataOldValue: true});
    document.addEventListener('DOMContentLoaded', () => { S.dom_loaded_ms = now(); });
    window.addEventListener('load', () => { S.load_ms = now(); });
  } catch (e) {
    try { window.__vrefAnimError = String(e).slice(0, 120); } catch (e2) {}
  }
})();"""

# 读数：n_numeric_nodes 是分母（页面上现在有几个短数字元素），n_changed 是分子。
# 分母必须一起给 —— 只报 n_changed=0 的话，"6 个指标一个都没动"和"页面根本没有数字"
# 长得一模一样，前者是没做到、后者是没量到，判分方向正好相反。
LOAD_ANIM_READ_JS = "() => {" + _PRELUDE + _NUM_OF_JS + r"""
  let n_num = 0;
  const els = document.querySelectorAll('*');
  const lim = Math.min(els.length, 4000);
  for (let i = 0; i < lim; i++) {
    const el = els[i];
    if (el.children.length) continue;
    if (__numOf(el.textContent) === null) continue;
    if (!__visible(el)) continue;
    n_num++;
  }
  const S = window.__vrefAnim;
  if (!S) {
    return {n_numeric_nodes: n_num,
            why: window.__vrefAnimError ? ('observer-failed: ' + window.__vrefAnimError)
                                        : 'observer-missing'};
  }
  let n_changed = 0, n_up_only = 0, n_down = 0, n_steps = 0;
  S.els.forEach((r) => {
    if (r.n <= 0) return;
    n_changed++;
    n_steps += r.n;
    if (r.up > 0 && r.down === 0) n_up_only++;
    if (r.down > 0) n_down++;
  });
  const ms = (v) => (v === null || v === undefined) ? null : Math.round(v);
  const pf = () => ((typeof performance !== 'undefined' && performance.now)
                    ? performance.now() : 0);
  return {n_numeric_nodes: n_num, n_touched: S.els.size, n_changed: n_changed,
          n_count_up: n_up_only, n_with_decrease: n_down, n_steps: n_steps,
          first_change_ms: ms(S.first_ms), last_change_ms: ms(S.last_ms),
          dom_loaded_ms: ms(S.dom_loaded_ms), load_ms: ms(S.load_ms),
          read_at_ms: ms(pf() - S.t0), capped: !!S.capped};
}"""



# ─────────────────────────── Python 侧 ───────────────────────────
def _stats(vals: Sequence[float]) -> Dict[str, Any]:
    v = sorted(float(x) for x in vals if x is not None)
    if not v:
        return {"n": 0, "min": None, "p05": None, "median": None, "max": None}
    def q(p):
        i = max(0, min(len(v) - 1, int(round(p * (len(v) - 1)))))
        return v[i]
    return {"n": len(v), "min": v[0], "p05": q(0.05), "median": q(0.5), "max": v[-1]}


def _text_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把逐节点的文字度量压成可判定的几个数（原始节点只留最坏的几个当证据）。"""
    nodes = (raw or {}).get("nodes") or []
    # 只有实体文字才算：一两个字符的图标、装饰字不该决定"最小字号"
    body = [n for n in nodes if n.get("len", 0) >= 8]
    px = [n["px"] for n in body if n.get("px")]
    ratios = [n["ratio"] for n in body if n.get("ratio") is not None]
    small = sorted([n for n in body if n.get("px") and n["px"] < SMALL_TEXT_PX],
                   key=lambda n: n["px"])[:6]
    worst = sorted([n for n in body if n.get("ratio") is not None],
                   key=lambda n: n["ratio"])[:6]
    # body_text：取"占文字节点最多的那个字号档"里最差的对比度，代表正文
    by_px: Dict[float, List[dict]] = {}
    for n in body:
        if n.get("px"):
            by_px.setdefault(round(n["px"]), []).append(n)
    body_ratio = None
    body_px = None
    if by_px:
        body_px = max(by_px, key=lambda k: sum(x.get("len", 0) for x in by_px[k]))
        rs = [n["ratio"] for n in by_px[body_px] if n.get("ratio") is not None]
        body_ratio = min(rs) if rs else None
    n_unknown = int((raw or {}).get("n_unknown_bg") or 0)
    return {
        "n_text_nodes": (raw or {}).get("n_text_nodes", 0),
        "n_body_nodes": len(body),
        "truncated": bool((raw or {}).get("truncated")),
        "font_px": _stats(px),
        "small_text_nodes": small,
        "contrast": {
            **_stats(ratios),
            "n_unknown_bg": n_unknown,
            # 背景是图片就算不出对比度；比例高时整项证据不可信 → probes 记 blocked
            "unknown_ratio": round(n_unknown / max(1, len(nodes)), 3),
            "worst_nodes": worst,
            "body_px": body_px,
            "body_text_ratio": body_ratio,
        },
    }


def _tap_summary(taps: Optional[List[dict]]) -> Dict[str, Any]:
    taps = taps or []
    sides = [min(t.get("w", 0), t.get("h", 0)) for t in taps]
    small = sorted([t for t in taps if min(t.get("w", 0), t.get("h", 0)) < TAP_TARGET_MIN],
                   key=lambda t: min(t.get("w", 0), t.get("h", 0)))[:8]
    return {"n_targets": len(taps), "min_side": min(sides) if sides else None,
            "n_below_44": len(small), "small_targets": small,
            "side": _stats(sides)}


def _measure_one_viewport(page, width: int, max_nodes: int,
                          reveal_js: str, wait_ms: int,
                          shot_prefix: Optional[str], out_dir: str,
                          n_frames: int, quality: int) -> Dict[str, Any]:
    """在已经 goto 完成的 page 上，量一个视口。"""
    res: Dict[str, Any] = {"width": width, "height": _vp_height(width)}

    # 1) reveal 之前：hover-only（reveal 会掀开所有 opacity:0，掀完就查不出来了）
    try:
        res["hover"] = page.evaluate(HOVER_JS)
    except Exception as e:
        res["hover"] = {"error": str(e)[:120]}

    # 2) reveal 之前的溢出：这是"原样"状态
    try:
        page.evaluate(SCROLL_DOCUMENT_JS, 0)
        raw_geom = page.evaluate(GEOM_JS, max_nodes)
    except Exception as e:
        raw_geom = {"error": str(e)[:120]}

    # 3) reveal（与 step3 渲染帧同一套语义），再量一遍
    try:
        page.evaluate(reveal_js)
        page.wait_for_timeout(300)
    except Exception:
        pass
    try:
        page.evaluate(SCROLL_DOCUMENT_JS, 0)
        geom = page.evaluate(GEOM_JS, max_nodes)
    except Exception as e:
        geom = {"error": str(e)[:120]}

    # 溢出取两个状态的**较小值**：reveal 会把折叠的抽屉/轮播掀出来，那不是真溢出。
    # 只有"原样"和"掀开后"都溢出才算真的溢出 —— 宁可漏判，不可错判。
    a = (raw_geom or {}).get("overflow_px")
    b = (geom or {}).get("overflow_px")
    cands = [x for x in (a, b) if isinstance(x, (int, float))]
    res["geometry"] = {**(geom if isinstance(geom, dict) else {}),
                       "overflow_px_raw": a, "overflow_px_revealed": b,
                       "overflow_px": min(cands) if cands else None}

    for key, js, arg in (("text_raw", TEXT_JS, max_nodes),
                         ("taps_raw", TAP_JS, None),
                         ("colors", COLORS_JS, max_nodes),
                         ("tokens", TOKENS_JS, max_nodes)):
        try:
            res[key] = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except Exception as e:
            res[key] = {"error": str(e)[:120]}

    tr = res.pop("text_raw", None)
    res["text"] = _text_summary(tr if isinstance(tr, dict) else None)
    taps = res.pop("taps_raw", None)
    res["tap_targets"] = _tap_summary(taps if isinstance(taps, list) else None)

    # 4) 截图：窄屏证据。judge 手里必须有窄屏图，否则响应式条目还是在无证据打分。
    frames: List[str] = []
    if shot_prefix and n_frames > 0:
        gh = (res["geometry"] or {}).get("scroll_height") or _vp_height(width)
        vh = _vp_height(width)
        max_scroll = max(0, min(int(gh), 30000) - vh)
        positions = [0] if (n_frames == 1 or max_scroll == 0) else [
            int(max_scroll * i / (n_frames - 1)) for i in range(n_frames)]
        for i, pos in enumerate(positions):
            try:
                page.evaluate(SCROLL_DOCUMENT_JS, pos)
                page.wait_for_timeout(400)
                p = os.path.join(out_dir, f"{shot_prefix}{i:02d}.jpg")
                page.screenshot(path=p, type="jpeg", quality=quality)
                if os.path.exists(p):
                    frames.append(os.path.basename(p))
            except Exception:
                break
        page.evaluate(SCROLL_DOCUMENT_JS, 0)
    res["frames"] = frames
    return res


def _anchors(page) -> Dict[str, Any]:
    """页内锚点落点。取不到就写 why，让探针记 blocked —— 不许填 0（那是"量到了、都断了"）。"""
    try:
        got = page.evaluate(ANCHORS_JS)
    except Exception as e:                                   # noqa: BLE001
        return {"why": f"eval-failed: {type(e).__name__}: {str(e)[:80]}"}
    return got if isinstance(got, dict) else {"why": "bad-return"}


def _semantics(page) -> Dict[str, Any]:
    """语义标签普查 + 标题层级。取不到写 why —— 和 _anchors 同一套约定：
    不许把"量不到"填成 0，那会被读成"页面一个语义标签都没有"。"""
    try:
        got = page.evaluate(SEMANTICS_JS)
    except Exception as e:                                   # noqa: BLE001
        return {"why": f"eval-failed: {type(e).__name__}: {str(e)[:80]}"}
    return got if isinstance(got, dict) else {"why": "bad-return"}


def _load_anim(page) -> Dict[str, Any]:
    """加载期数字动画的读数。必须在 reveal 之前读：reveal 会把滚动触发的动画也点着，
    那时"页面加载时是否 count up"就答不清了。"""
    try:
        got = page.evaluate(LOAD_ANIM_READ_JS)
    except Exception as e:                                   # noqa: BLE001
        return {"why": f"eval-failed: {type(e).__name__}: {str(e)[:80]}"}
    return got if isinstance(got, dict) else {"why": "bad-return"}


def click_status(why: str) -> str:
    """点击探针失败时该记哪一档 —— 单独一个纯函数，为的是能被质检脚本直接钉住。

    「页面上根本没有这个元素」和「量的时候崩了」必须分开（总纲 §3.5 护栏 2）：

    | why | 记成 | 为什么 |
    |---|---|---|
    | `not-found` | `absent` | 该有的东西没有 → 下游判**不过**，证据就是"选择器落不到地" |
    | `bad-selector` | `blocked` | 是**我们**规格里的选择器写错了，模型无责 → 真的没证据 |
    | 其他（异常/超时/点不动） | `blocked` | 量的时候崩了 → 没证据（§4.2） |

    混成一个 `blocked` 的后果是**奖励缺功能**：没做这个组件 → 点不到 → 不进分母 → 白捡。
    """
    return "absent" if str(why) == "not-found" else "blocked"


def _click_probes(context, file_url: str, wait_ms: int, timeout_ms: int,
                  max_probes: int, max_nodes: int,
                  extra_selectors: Optional[List[str]] = None) -> Dict[str, Any]:
    """点击探针：点了 DOM 一动不动 = 交互是死的（源码里有 onclick 不算）。

    每个探针都重新 goto 一次：一次点击可能改掉整页状态（切 tab、开弹窗），
    在脏状态上接着点，第二个探针的结论就不可信了。
    """
    out: Dict[str, Any] = {"probes": [], "n_candidates": 0, "error": None}
    page = None
    try:
        page = context.new_page()
        page.goto(file_url, wait_until="load", timeout=timeout_ms)
        page.wait_for_timeout(min(wait_ms, 1200))
        cands = page.evaluate(CLICK_CANDIDATES_JS) or []
        out["n_candidates"] = len(cands)
        out["n_toggle_candidates"] = sum(1 for c in cands if c.get("kind") == "toggle")
        wanted: List[Dict[str, Any]] = []
        for sel in (extra_selectors or []):     # 规格里点名的选择器优先
            wanted.append({"sel": sel, "tag": "?", "label": "",
                           "kind": "toggle", "from_spec": True})
        for c in cands:
            if len(wanted) >= max_probes:
                break
            if all(c["sel"] != w["sel"] for w in wanted):
                wanted.append(c)
        page.close()
        page = None

        for c in wanted[:max_probes]:
            rec = {"selector": c["sel"], "label": c.get("label", ""),
                   "kind": c.get("kind", "toggle"),
                   "from_spec": bool(c.get("from_spec"))}
            try:
                page = context.new_page()
                page.goto(file_url, wait_until="load", timeout=timeout_ms)
                page.wait_for_timeout(min(wait_ms, 1000))
                before = page.evaluate(DOM_SIG_JS, max_nodes)
                clicked = page.evaluate(CLICK_ONE_JS, c["sel"])
                if not (clicked or {}).get("ok"):
                    why = (clicked or {}).get("why", "click-failed")
                    rec.update({"status": click_status(why), "why": why})
                else:
                    page.wait_for_timeout(700)
                    after = page.evaluate(DOM_SIG_JS, max_nodes)
                    changed = (before["sig"] != after["sig"]) or (before["text_sig"] != after["text_sig"])
                    rec.update({
                        "status": "ok", "changed": bool(changed),
                        "sig_changed": before["sig"] != after["sig"],
                        "text_changed": before["text_sig"] != after["text_sig"],
                        "visible_delta": after["n_visible"] - before["n_visible"],
                        "element_delta": after["n_elements"] - before["n_elements"],
                    })
            except Exception as e:
                rec.update({"status": "blocked", "why": str(e)[:100]})
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = None
            out["probes"].append(rec)
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
    return out


def _fixture_run(context, file_url: str, name: str, width: int,
                 wait_ms: int, timeout_ms: int, max_nodes: int,
                 out_dir: str, shot: bool, quality: int) -> Dict[str, Any]:
    """对抗内容注入：0 条 / 300 条 / 80 字符长串 / CJK+emoji，注入后重量一遍。

    注入不适用（页面里没有重复列表）时，`applied=False` —— 对应探针必须记
    blocked 而不是 0：页面没列表不等于它抗不住长数据。
    """
    rec: Dict[str, Any] = {"fixture": name, "width": width, "applied": False}
    page = None
    try:
        page = context.new_page()
        page.set_viewport_size({"width": width, "height": _vp_height(width)})
        page.goto(file_url, wait_until="load", timeout=timeout_ms)
        page.wait_for_timeout(min(wait_ms, 1200))
        repeat = page.evaluate(FIND_REPEAT_JS)
        rec["repeat"] = repeat
        before_texts = page.evaluate(VISIBLE_TEXT_JS) or []
        before_geom = page.evaluate(GEOM_JS, max_nodes)
        applied = page.evaluate(APPLY_JS[name])
        rec["apply"] = applied
        if not (applied or {}).get("applied"):
            rec["why"] = (applied or {}).get("why", "not-applicable")
            return rec
        rec["applied"] = True
        page.wait_for_timeout(600)
        after_geom = page.evaluate(GEOM_JS, max_nodes)
        after_texts = page.evaluate(VISIBLE_TEXT_JS) or []
        rec["overflow_px_before"] = (before_geom or {}).get("overflow_px")
        rec["overflow_px"] = (after_geom or {}).get("overflow_px")
        rec["overflow_nodes"] = (after_geom or {}).get("overflow_nodes", [])[:5]
        rec["scroll_height_before"] = (before_geom or {}).get("scroll_height")
        rec["scroll_height"] = (after_geom or {}).get("scroll_height")
        if name == "zero_rows":
            rec["empty_state"] = empty_state_from_texts(before_texts, after_texts)
        if shot:
            p = os.path.join(out_dir, f"fx_{name}_{width}.jpg")
            try:
                page.screenshot(path=p, type="jpeg", quality=quality, full_page=False)
                if os.path.exists(p):
                    rec["frame"] = os.path.basename(p)
            except Exception:
                pass
    except Exception as e:
        rec["error"] = str(e)[:150]
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
    return rec


def measure_html(
    html_code: str,
    out_dir: str,
    viewports: Sequence[int] = (1920, 1280, 768, 390),
    fixtures: Sequence[str] = FIXTURE_NAMES,
    max_nodes: int = 4000,
    wait_ms: int = 1500,
    timeout_ms: int = 60000,
    max_click_probes: int = 6,
    screenshot_fixtures: bool = True,
    quality: int = 80,
    extra_viewport_frames: int = 2,
    main_viewport_frames: int = 0,
    click_selectors: Optional[List[str]] = None,
    rendered_frames: Optional[List[str]] = None,
    write_json: bool = True,
) -> Dict[str, Any]:
    """一次会话量完所有确定性证据，返回（并可选写出）measure.json。

    Args:
        viewports: 要量的宽度，第一个当作"主视口"（与 step3 渲染帧一致的那个）。
        main_viewport_frames: 主视口截几张。默认 0 —— step3 已经出了 frame_NN.jpg，
            不重复截；独立调试时可以设 3。
        rendered_frames: 已有的主视口帧路径，用来算 palette_sig（判"配色跟了哪一份"）。
        click_selectors: 规格里点名的选择器，强制探。
    """
    os.makedirs(out_dir, exist_ok=True)
    result: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "error": None,
        "html_bytes": len(html_code or ""),
        "viewports": {},
        "fixtures": {},
        "interaction": {},
        "network": {"external_requests": [], "n_external": 0},
        "console": {"errors": [], "page_errors": []},
    }
    if not (html_code or "").strip():
        result["error"] = "empty html"
        if write_json:
            _write(out_dir, result)
        return result

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["error"] = "playwright 未安装"
        if write_json:
            _write(out_dir, result)
        return result

    from .html_renderer import PHASE1_REVEAL_JS, _FONT_BLOCK_PATTERNS

    vps = [int(v) for v in (viewports or [1920, 1280, 768, 390])]
    main_w = vps[0]
    tmp_html = None
    try:
        tmp_html = tempfile.NamedTemporaryFile(suffix=".html", delete=False,
                                               mode="w", encoding="utf-8")
        tmp_html.write(html_code)
        tmp_html.close()
        file_url = f"file://{os.path.abspath(tmp_html.name)}"

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-software-rasterizer",
                      "--force-color-profile=srgb", "--font-render-hinting=none"],
            )
            try:
                for w in vps:
                    ctx = browser.new_context(
                        viewport={"width": w, "height": _vp_height(w)},
                        device_scale_factor=1,
                        java_script_enabled=True,
                    )
                    ctx.set_default_timeout(timeout_ms)
                    if w == main_w:
                        # 必须在任何 goto 之前：计数动画在 load 那一刻就跑完了
                        try:
                            ctx.add_init_script(LOAD_ANIM_INIT_JS)
                        except Exception as e:               # noqa: BLE001
                            logger.warning("挂不上加载期动画观察者：%s", str(e)[:80])
                    ext: List[str] = []
                    errs: List[str] = []
                    perrs: List[str] = []

                    # 外链一律拦掉并记账：一是可复现（网速不该影响分数），
                    # 二是 self_contained 本身就是一条可判条目（单文件 HTML 的要求）。
                    def _route(route):
                        try:
                            u = route.request.url
                            if u.startswith("http://") or u.startswith("https://"):
                                if len(ext) < 40:
                                    ext.append(u[:200])
                                return route.abort()
                            return route.continue_()
                        except Exception:
                            try:
                                return route.abort()
                            except Exception:
                                return None
                    ctx.route("**/*", _route)

                    page = ctx.new_page()
                    page.on("console", lambda m: (errs.append(f"{m.type}: {m.text[:160]}")
                                                  if m.type == "error" and len(errs) < 20 else None))
                    page.on("pageerror", lambda e: perrs.append(str(e)[:160]) if len(perrs) < 20 else None)
                    for pat in _FONT_BLOCK_PATTERNS:
                        try:
                            page.route(pat, lambda route: route.abort())
                        except Exception:
                            pass
                    try:
                        page.goto(file_url, wait_until="load", timeout=timeout_ms)
                        page.wait_for_timeout(wait_ms)
                    except Exception:
                        try:
                            page.goto(file_url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass

                    if w == main_w:
                        # 顺序有讲究：这两块都要在 _measure_one_viewport 之前读 ——
                        # 那里会跑 PHASE1_REVEAL_JS 把滚动触发的动画全点着
                        result["load_anim"] = _load_anim(page)
                        result["anchors"] = _anchors(page)
                        result["semantics"] = _semantics(page)

                    n_frames = main_viewport_frames if w == main_w else extra_viewport_frames
                    prefix = None if n_frames <= 0 else (
                        f"mainvp_{w}_" if w == main_w else f"vp_{w}_")
                    result["viewports"][str(w)] = _measure_one_viewport(
                        page, w, max_nodes, PHASE1_REVEAL_JS, wait_ms,
                        prefix, out_dir, n_frames, quality)
                    page.close()

                    if w == main_w:
                        result["interaction"] = _click_probes(
                            ctx, file_url, wait_ms, timeout_ms,
                            max_click_probes, max_nodes, click_selectors)
                        ctx.close()
                        # 关掉 context 之后再结账：同一个 URL 会被重试多次，
                        # 去重后才是"这页依赖几个外部资源"
                        uniq = list(dict.fromkeys(ext))
                        result["network"]["external_requests"] = uniq[:40]
                        result["network"]["n_external"] = len(uniq)
                        result["network"]["n_requests"] = len(ext)
                        result["console"]["errors"] = list(errs)
                        result["console"]["page_errors"] = list(perrs)
                    else:
                        ctx.close()

                # 对抗数据只在最宽和最窄各跑一遍：中间宽度加不出新信息，只加时间
                fx_widths = sorted({main_w, min(vps)})
                unknown_fx = [n for n in (fixtures or []) if n not in APPLY_JS]
                if unknown_fx:
                    # 静默 continue 的代价：config.measure.fixtures 里写错一个名字，
                    # 这一类对抗证据就整批消失，而 step1a 照样按这个名字建了程序轨条目
                    # → probes.py 拿到空 fixtures → 条目判「无法确认」→ 被 caliber
                    # 从分母里剔掉。分数看起来还挺正常，只是少考了一整面。
                    logger.warning(
                        f"measure: 不认识的 fixture 名 {unknown_fx}（认得的是 "
                        f"{sorted(APPLY_JS)}）—— 这几类对抗证据不会被量到，依赖它们的"
                        f"程序轨条目会判「无法确认」并被剔出分母。改 config.measure.fixtures")
                    result.setdefault("warnings", []).append(
                        {"kind": "unknown_fixture", "names": unknown_fx})
                for name in (fixtures or []):
                    if name not in APPLY_JS:
                        continue
                    per_w = {}
                    for w in fx_widths:
                        ctx = browser.new_context(viewport={"width": w, "height": _vp_height(w)},
                                                  device_scale_factor=1)
                        ctx.set_default_timeout(timeout_ms)
                        ctx.route("**/*", lambda route: (
                            route.abort() if route.request.url.startswith("http") else route.continue_()))
                        per_w[str(w)] = _fixture_run(
                            ctx, file_url, name, w, wait_ms, timeout_ms, max_nodes,
                            out_dir, screenshot_fixtures and w == main_w, quality)
                        ctx.close()
                    result["fixtures"][name] = per_w
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        # 产出自己的配色签名：判"配色到底跟了哪一份参考"用的就是它
        frames = list(rendered_frames or [])
        if not frames:
            vp = result["viewports"].get(str(main_w)) or {}
            frames = [os.path.join(out_dir, f) for f in (vp.get("frames") or [])]
        if frames:
            try:
                from ..query.palette import palette_sig
                result["palette_sig"] = palette_sig([f for f in frames if os.path.exists(f)])
                result["palette_sig_frames"] = [os.path.basename(f) for f in frames][:3]
            except Exception as e:
                result["palette_sig"] = None
                result["palette_sig_error"] = str(e)[:120]

        result["ok"] = any(
            isinstance((result["viewports"].get(str(w)) or {}).get("geometry", {}).get("scroll_width"), int)
            for w in vps)
        if not result["ok"]:
            result["error"] = result["error"] or "所有视口都没量到几何信息"
    except Exception as e:
        result["error"] = f"measure 失败: {str(e)[:300]}"
    finally:
        if tmp_html and os.path.exists(tmp_html.name):
            try:
                os.remove(tmp_html.name)
            except OSError:
                pass

    if write_json:
        _write(out_dir, result)
    return result


def _write(out_dir: str, data: Dict[str, Any]) -> str:
    p = os.path.join(out_dir, "measure.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="确定性测量单个 HTML（调试用）")
    ap.add_argument("html")
    ap.add_argument("out_dir")
    ap.add_argument("--viewports", default="1920,1280,768,390")
    ap.add_argument("--fixtures", default=",".join(FIXTURE_NAMES))
    ap.add_argument("--main-frames", type=int, default=3)
    args = ap.parse_args()
    with open(args.html, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    r = measure_html(html, args.out_dir,
                     viewports=[int(x) for x in args.viewports.split(",") if x.strip()],
                     fixtures=[x for x in args.fixtures.split(",") if x.strip()],
                     main_viewport_frames=args.main_frames)
    print(json.dumps({k: v for k, v in r.items() if k != "viewports"}, ensure_ascii=False, indent=1)[:2000])
    for w, vp in r["viewports"].items():
        g = vp.get("geometry", {})
        print(f"  vp {w}: overflow={g.get('overflow_px')} (raw={g.get('overflow_px_raw')}) "
              f"minpx={vp.get('text', {}).get('font_px', {}).get('min')} "
              f"contrast_min={vp.get('text', {}).get('contrast', {}).get('min')} "
              f"tap_min={vp.get('tap_targets', {}).get('min_side')} frames={vp.get('frames')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
