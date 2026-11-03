'use strict';

(() => {
  const views = {
    scores: ['name', 'overall', 'b', 'c', 'r'],
    resources: ['name', 'overall', 'turns', 'tokens', 'calls'],
    all: ['name', 'overall', 'b', 'c', 'r', 'turns', 'tokens', 'calls']
  };
  const labels = {
    name: 'Model', overall: 'Overall', b: 'Keypoints B', c: 'Core C',
    r: 'Reference R', turns: 'Turns', tokens: 'Tokens', calls: 'Tool calls'
  };
  const viewLabels = { scores: 'Evaluation scores', resources: 'Resource use', all: 'All results' };
  const metricKeys = ['overall', 'b', 'c', 'r', 'turns', 'tokens', 'calls'];
  const resourceKeys = new Set(['turns', 'tokens', 'calls']);
  const escapeHTML = value => String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[character]));
  let instance = null;

  function init() {
    const table = document.getElementById('leaderboard-table');
    const input = window.REFCRAFT?.models;
    if (!table || !Array.isArray(input) || !table.tHead || !table.tBodies[0]) return false;
    if (instance?.table === table) return true;
    instance?.destroy();

    const models = input.filter(model => typeof model.name === 'string' &&
      metricKeys.every(key => Number.isFinite(model[key]))
    ).map(model => ({ ...model }));
    if (!models.length) return false;
    const modelByName = new Map(models.map(model => [model.name, model]));
    const ranking = [...models].sort((a, b) => b.overall - a.overall || a.name.localeCompare(b.name));
    const ranks = new Map(ranking.map((model, index) => [model.name, index + 1]));
    const bestTwo = Object.fromEntries(['overall', 'b', 'c', 'r'].map(key => [
      key, [...new Set(models.map(model => model[key]))].sort((a, b) => b - a).slice(0, 2)
    ]));
    const section = table.closest('#leaderboard') || table.parentElement;
    const scrollRegion = table.closest('.table-wrap') || table.parentElement;
    const search = document.getElementById('model-search');
    const viewControls = document.getElementById('leaderboard-views');
    const count = document.getElementById('leaderboard-count');
    const status = document.getElementById('table-status');
    const controller = new AbortController();
    const state = { view: 'scores', sortKey: 'overall', direction: 'desc', query: search?.value.trim().toLowerCase() || '', expanded: null, selected: null };
    let visibleRows = [];
    let visibleWidth = 0;

    table.classList.add('refcraft-leaderboard');
    scrollRegion.classList.add('lb-scroll-region');
    viewControls?.setAttribute('role', 'group');
    if (viewControls && !viewControls.hasAttribute('aria-label')) viewControls.setAttribute('aria-label', 'Leaderboard view');
    let colgroup = table.querySelector('colgroup[data-leaderboard-columns]');
    if (!colgroup) {
      colgroup = document.createElement('colgroup');
      colgroup.dataset.leaderboardColumns = '';
      table.insertBefore(colgroup, table.tHead);
    }

    const modelButtonId = model => `lb-model-${ranks.get(model.name)}`;
    const detailId = model => `lb-detail-${ranks.get(model.name)}`;
    const format = (model, key) => key === 'tokens' ? `${model[key].toFixed(2)} M` : model[key].toFixed(resourceKeys.has(key) ? 1 : 2);
    function formattedValue(model, key) {
      const value = format(model, key);
      const position = bestTwo[key]?.indexOf(model[key]);
      if (position === 0) return `<strong class="lb-best-value">${value}</strong>`;
      if (position === 1) return `<span class="lb-second-value">${value}</span>`;
      return value;
    }

    function focusToken() {
      const active = document.activeElement;
      if (!active || !table.contains(active)) return null;
      if (active.matches('[data-sort]')) return { sort: active.dataset.sort };
      if (active.matches('[data-lb-model]')) return { model: active.dataset.lbModel };
      if (active.matches('[data-lb-close]')) return { close: active.dataset.lbClose };
      return null;
    }
    function restoreFocus(token) {
      if (!token) return;
      let target;
      if (token.sort) target = [...table.querySelectorAll('[data-sort]')].find(button => button.dataset.sort === token.sort);
      if (token.model) target = [...table.querySelectorAll('[data-lb-model]')].find(button => button.dataset.lbModel === token.model);
      if (token.close) target = [...table.querySelectorAll('[data-lb-close]')].find(button => button.dataset.lbClose === token.close) || [...table.querySelectorAll('[data-lb-model]')].find(button => button.dataset.lbModel === token.close);
      if (!target) target = viewControls?.querySelector(`[data-view="${state.view}"]`) || table.querySelector('[data-sort="overall"]');
      target?.focus({ preventScroll: true });
    }

    function header() {
      const columns = views[state.view];
      const scoreCount = columns.filter(key => ['overall', 'b', 'c', 'r'].includes(key)).length;
      const resourceCount = columns.filter(key => resourceKeys.has(key)).length;
      colgroup.innerHTML = '<col class="lb-col-rank">' + columns.map(key => `<col class="lb-col-${key}">`).join('');
      table.tHead.innerHTML = `<tr class="lb-column-groups"><th colspan="2" scope="colgroup">Model</th><th colspan="${scoreCount}" scope="colgroup">Evaluation · 0–100 ↑</th>${resourceCount ? `<th colspan="${resourceCount}" scope="colgroup">Mean resource use / task</th>` : ''}</tr><tr class="lb-column-labels"><th class="lb-rank-cell" scope="col"><span title="Rank by Overall score across all nine models">#</span></th>${columns.map(key => {
        const active = state.sortKey === key;
        const unit = key === 'tokens' ? 'million / task' : resourceKeys.has(key) ? 'per task' : '';
        return `<th scope="col" class="lb-${key}-cell${resourceKeys.has(key) && key === 'turns' ? ' lb-resource-start' : ''}" aria-sort="${active ? (state.direction === 'asc' ? 'ascending' : 'descending') : 'none'}"><button type="button" data-sort="${key}" aria-label="Sort by ${labels[key]}${unit ? `, ${unit}` : ''}"><span>${labels[key]}</span><span class="lb-sort-arrow" aria-hidden="true">${active ? (state.direction === 'desc' ? '↓' : '↑') : '↕'}</span></button>${unit ? `<small class="lb-column-unit">${unit}</small>` : ''}</th>`;
      }).join('')}</tr>`;
    }

    function detailRow(model, columnCount) {
      const components = [['b', 'Keypoint completion'], ['c', 'Core functionality'], ['r', 'Reference following']];
      return `<tr class="lb-detail-row" id="${detailId(model)}" data-detail-for="${escapeHTML(model.name)}"><td colspan="${columnCount}" class="lb-detail-cell"><div class="lb-detail" role="region" aria-labelledby="${modelButtonId(model)}"><div class="lb-detail-heading"><span>${escapeHTML(model.name)} <small>Paper results</small></span><button type="button" data-lb-close="${escapeHTML(model.name)}" aria-label="Close details for ${escapeHTML(model.name)}">Close <span aria-hidden="true">×</span></button></div><div class="lb-detail-layout"><div class="lb-components">${components.map(([key, name]) => `<div class="lb-component"><div class="lb-component-label"><span><b>${key.toUpperCase()}</b> ${name}</span><span class="lb-component-value">${formattedValue(model, key)}</span></div><div class="lb-component-track" aria-hidden="true"><span style="width:${model[key]}%"></span></div></div>`).join('')}</div><dl class="lb-resources"><div><dt>Turns / task</dt><dd>${format(model, 'turns')}</dd></div><div><dt>Tokens / task</dt><dd>${format(model, 'tokens')}<small>million tokens</small></dd></div><div><dt>Tool calls / task</dt><dd>${format(model, 'calls')}</dd></div></dl></div><p class="lb-detail-note">Overall averages the per-task minimum of B, C, and R, omitting R when inapplicable. It cannot be reconstructed by taking the minimum of these displayed component means.</p></div></td></tr>`;
    }

    function updateSelection() {
      table.querySelectorAll('.lb-model-row').forEach(row => row.classList.toggle('is-chart-selected', row.dataset.rowModel === state.selected));
    }
    function announce() {
      if (status) status.textContent = `${visibleRows.length} of ${models.length} models. ${viewLabels[state.view]}. Sorted by ${labels[state.sortKey]}, ${state.direction === 'asc' ? 'ascending' : 'descending'}.${state.expanded ? ` Details open for ${state.expanded}.` : ''}`;
    }
    function render(token = focusToken()) {
      const columns = views[state.view];
      visibleRows = models.filter(model => model.name.toLowerCase().includes(state.query)).sort((a, b) => {
        const comparison = state.sortKey === 'name' ? a.name.localeCompare(b.name) : a[state.sortKey] - b[state.sortKey];
        return (state.direction === 'asc' ? comparison : -comparison) || ranks.get(a.name) - ranks.get(b.name);
      });
      if (state.expanded && !visibleRows.some(model => model.name === state.expanded)) state.expanded = null;
      table.dataset.view = state.view;
      header();
      table.tBodies[0].innerHTML = visibleRows.length ? visibleRows.map((model, index) => {
        const expanded = state.expanded === model.name;
        const row = `<tr class="lb-model-row${index % 2 ? ' is-even' : ''}${expanded ? ' is-expanded' : ''}" data-row-model="${escapeHTML(model.name)}"><td class="lb-rank-cell">${ranks.get(model.name)}</td>${columns.map(key => {
          if (key === 'name') return `<th scope="row" class="lb-name-cell"><button type="button" class="lb-model-button" id="${modelButtonId(model)}" data-lb-model="${escapeHTML(model.name)}" data-model="${escapeHTML(model.name)}" aria-expanded="${expanded}"${expanded ? ` aria-controls="${detailId(model)}"` : ''}><span class="lb-chevron" aria-hidden="true">›</span>${model.mark ? `<img class="lb-mark" src="${model.mark}" alt="" width="18" height="18">` : ''}<span>${escapeHTML(model.name)}</span></button></th>`;
          if (key === 'overall') return `<td class="lb-overall-cell"><div class="lb-overall"><span class="lb-overall-track" aria-hidden="true"><span style="width:${model.overall}%"></span></span><span class="lb-number">${formattedValue(model, key)}</span></div></td>`;
          return `<td class="lb-${key}-cell${key === 'turns' ? ' lb-resource-start' : ''}">${formattedValue(model, key)}</td>`;
        }).join('')}</tr>`;
        return row + (expanded ? detailRow(model, columns.length + 1) : '');
      }).join('') : `<tr><td class="lb-empty-row" colspan="${columns.length + 1}">No matching models. Try a different name.</td></tr>`;
      viewControls?.querySelectorAll('button[data-view]').forEach(button => {
        const active = button.dataset.view === state.view;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      if (count) count.textContent = visibleRows.length === models.length ? `${models.length} models` : `${visibleRows.length} of ${models.length} models`;
      updateSelection();
      announce();
      restoreFocus(token);
    }

    function setView(view) {
      if (!Object.hasOwn(views, view)) return false;
      state.view = view;
      if (!views[view].includes(state.sortKey)) { state.sortKey = 'overall'; state.direction = 'desc'; }
      render();
      return true;
    }
    function selectModel(name) {
      if (!modelByName.has(name)) return false;
      state.selected = name;
      updateSelection();
      return true;
    }
    function toggleModel(name, forceClose = false) {
      if (!modelByName.has(name)) return;
      const expanded = !forceClose && state.expanded !== name;
      state.expanded = expanded ? name : null;
      if (expanded) {
        selectModel(name);
        window.RefCraftPareto?.selectModel(name);
      }
      render({ model: name });
      section.dispatchEvent(new CustomEvent('refcraft:leaderboard-toggle', { bubbles: true, detail: { name, expanded } }));
    }

    table.addEventListener('click', event => {
      const modelButton = event.target.closest('[data-lb-model]');
      if (modelButton) { toggleModel(modelButton.dataset.lbModel); return; }
      const closeButton = event.target.closest('[data-lb-close]');
      if (closeButton) { toggleModel(closeButton.dataset.lbClose, true); return; }
      const sortButton = event.target.closest('[data-sort]');
      if (!sortButton) return;
      const key = sortButton.dataset.sort;
      state.direction = state.sortKey === key ? (state.direction === 'desc' ? 'asc' : 'desc') : (key === 'name' || resourceKeys.has(key) ? 'asc' : 'desc');
      state.sortKey = key;
      render({ sort: key });
    }, { signal: controller.signal });
    table.addEventListener('keydown', event => {
      if (event.key === 'Escape' && state.expanded) { event.preventDefault(); toggleModel(state.expanded, true); }
    }, { signal: controller.signal });
    search?.addEventListener('input', () => { state.query = search.value.trim().toLowerCase(); render(); }, { signal: controller.signal });
    viewControls?.addEventListener('click', event => {
      const button = event.target.closest('button[data-view]');
      if (button) setView(button.dataset.view);
    }, { signal: controller.signal });
    document.addEventListener('refcraft:pareto-select', event => selectModel(event.detail?.name), { signal: controller.signal });

    function updateWidth() {
      const next = Math.floor(scrollRegion.clientWidth);
      if (next && next !== visibleWidth) { visibleWidth = next; table.style.setProperty('--lb-viewport-width', `${next}px`); }
    }
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateWidth) : null;
    observer?.observe(scrollRegion);
    window.addEventListener('resize', updateWidth, { signal: controller.signal });
    instance = { table, setView, selectModel, state, destroy() { controller.abort(); observer?.disconnect(); } };
    updateWidth();
    render(null);
    return true;
  }

  window.RefCraftLeaderboard = {
    init,
    setView(view) { return instance?.setView(view) || false; },
    selectModel(name) { return instance?.selectModel(name) || false; },
    getState() { return instance ? { ...instance.state } : null; }
  };
})();
