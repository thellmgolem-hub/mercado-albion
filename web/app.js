'use strict';

// ============================================================ estado
const state = {
  meta: null,
  premium: localStorage.getItem('premium') !== '0',
  favorites: JSON.parse(localStorage.getItem('favorites') || '[]'),
  precosItem: null,
  flipsItems: [],
  venderItem: null,
  histItem: null,
  scanOpps: [],
  scanItemsScanned: 0,
  scanRows: [],
  dashRecs: [],
  discoverRows: [],
  hidden: new Set(JSON.parse(localStorage.getItem('hiddenFlips') || '[]')),
};

const $ = (id) => document.getElementById(id);

// ============================================================ utilitários
async function api(path, params = {}) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== '') usp.set(k, v);
  }
  const res = await fetch(path + (usp.toString() ? '?' + usp : ''));
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* corpo não-JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

async function apiSend(path, method = 'POST', params = {}) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== '') usp.set(k, v);
  }
  const res = await fetch(path + (usp.toString() ? '?' + usp : ''), { method });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* corpo não-JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

const fmt = (n) => n === null || n === undefined ? '—'
  : Math.round(n).toLocaleString('pt-BR');

const fmtDec = (n, d = 1) => n === null || n === undefined ? '—'
  : Number(n).toLocaleString('pt-BR', { maximumFractionDigits: d });

const fmtPct = (n) => n === null || n === undefined ? '—' : `${fmtDec(n, 1)}%`;

function esc(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function ageHtml(min) {
  if (min === null || min === undefined) return '<span class="age age-none">—</span>';
  const cls = min <= 30 ? 'age-good' : min <= 120 ? 'age-warn' : 'age-bad';
  const txt = min < 60 ? `${min}min` : min < 1440 ? `${Math.round(min / 60)}h`
    : `${Math.round(min / 1440)}d`;
  return `<span class="age ${cls}" title="dado coletado há ${min} minutos">${txt}</span>`;
}

const qName = (q) => state.meta ? state.meta.qualities[q] : q;
const qHtml = (q) => `<span class="q${q}" title="qualidade">${esc(qName(q))}</span>`;

const iconUrl = (id, q = 0) =>
  `https://render.albiononline.com/v1/item/${encodeURIComponent(id)}.png?size=64` +
  (q > 1 ? `&quality=${q}` : '');

// fallback: se o serviço de render bloquear o navegador, usa o proxy local
window.__iconFb = (img, id, q) => {
  if (img.dataset.fb === '2') { img.style.visibility = 'hidden'; return; }
  if (img.dataset.fb === '1') {
    img.dataset.fb = '2';
    img.style.visibility = 'hidden';
    return;
  }
  img.dataset.fb = '1';
  img.src = `/icon/${encodeURIComponent(id)}?quality=${q || 0}&size=64`;
};

const iconImg = (id, q = 0) =>
  `<img loading="lazy" src="${iconUrl(id, q)}" ` +
  `onerror="__iconFb(this,'${esc(id)}',${q || 0})" alt="">`;

function cityHtml(c) {
  const label = state.meta?.city_labels?.[c] || c;
  return c === 'Black Market' ? `<span class="bm">${esc(label)}</span>` : esc(label);
}

const teBadge = (it) => `<span class="te-badge">T${it.tier}.${it.ench}</span>`;

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
}

function copyText(txt) {
  navigator.clipboard?.writeText(txt).then(
    () => toast(`copiado: ${txt}`),
    () => toast('não foi possível copiar'));
}

function setGlossary(open) {
  const modal = $('glossaryModal');
  if (!modal) return;
  modal.classList.toggle('open', open);
  modal.setAttribute('aria-hidden', open ? 'false' : 'true');
}

// ============================================================ componentes
function makeItemPicker(containerId, onSelect, placeholder = 'buscar item… ex.: bolsa 4.1', options = {}) {
  const box = $(containerId);
  const batchLabel = options.batchLabel || 'Adicionar visíveis';
  box.innerHTML = `<div class="item-browser">
    <div class="item-filter-grid">
      <select data-filter="cat" aria-label="Categoria"></select>
      <select data-filter="sub" aria-label="Subcategoria" disabled></select>
      <select data-filter="tier" aria-label="Tier"></select>
      <select data-filter="ench" aria-label="Encantamento"></select>
    </div>
    <div class="item-search-row">
      <input type="text" placeholder="${esc(placeholder)}" autocomplete="off">
      <button type="button" class="btn secondary filter-clear" title="Limpar filtros">Limpar</button>
      ${options.onBatchSelect ? `<button type="button" class="btn secondary batch-add" disabled>${esc(batchLabel)}</button>` : ''}
    </div>
    <div class="drop"></div>
  </div>`;
  const input = box.querySelector('input');
  const drop = box.querySelector('.drop');
  const catSel = box.querySelector('[data-filter="cat"]');
  const subSel = box.querySelector('[data-filter="sub"]');
  const tierSel = box.querySelector('[data-filter="tier"]');
  const enchSel = box.querySelector('[data-filter="ench"]');
  const clearBtn = box.querySelector('.filter-clear');
  const batchBtn = box.querySelector('.batch-add');
  let results = [], sel = -1, timer = null, req = 0;

  catSel.innerHTML = '<option value="">Todas categorias</option>' +
    Object.entries(state.meta.categories).map(([id, c]) =>
      `<option value="${esc(id)}">${esc(c.label)}</option>`).join('');
  tierSel.innerHTML = '<option value="">Todos tiers</option>' +
    [1, 2, 3, 4, 5, 6, 7, 8].map((t) => `<option value="${t}">Tier ${t}</option>`).join('');
  enchSel.innerHTML = '<option value="">Todos encantos</option>' +
    [0, 1, 2, 3, 4].map((e) => `<option value="${e}">.${e}</option>`).join('');

  function fillSubs() {
    const cat = catSel.value;
    const subs = cat ? state.meta.categories[cat]?.subs || {} : {};
    subSel.disabled = !cat;
    subSel.innerHTML = '<option value="">Todas subcategorias</option>' +
      Object.entries(subs).map(([id, s]) =>
        `<option value="${esc(id)}">${esc(s.label)}</option>`).join('');
  }
  fillSubs();

  function close() { drop.classList.remove('open'); sel = -1; }

  function searchParams() {
    const tier = tierSel.value;
    return {
      q: input.value.trim(),
      cat: catSel.value,
      sub: subSel.value,
      tier_min: tier,
      tier_max: tier,
      ench: enchSel.value,
      limit: options.limit || 80,
    };
  }

  function hasActiveSearch(p) {
    return p.q.length >= 2 || p.cat || p.sub || p.tier_min || p.ench !== '';
  }

  function itemMetaHtml(it) {
    const cat = state.meta.categories?.[it.cat];
    const catLabel = cat?.label || it.cat || '';
    const subLabel = cat?.subs?.[it.sub]?.label || it.sub || '';
    return `${esc(it.en)} · ${esc(it.id)} · ${esc(catLabel)}${subLabel ? ' / ' + esc(subLabel) : ''}`;
  }

  function render(message = '') {
    if (message) {
      drop.innerHTML = `<div class="opt empty">${esc(message)}</div>`;
      drop.classList.add('open');
      if (batchBtn) batchBtn.disabled = true;
      return;
    }
    drop.innerHTML = results.map((it, i) => `
      <div class="opt ${i === sel ? 'sel' : ''}" data-i="${i}">
        ${iconImg(it.id)}
        <div class="nm">${esc(it.pt)}<small>${itemMetaHtml(it)}</small></div>
        <div class="te">T${it.tier}.${it.ench}</div>
      </div>`).join('');
    drop.classList.toggle('open', results.length > 0);
    if (batchBtn) batchBtn.disabled = results.length === 0;
    drop.querySelectorAll('.opt').forEach((o) => {
      o.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        pick(+o.dataset.i);
      });
    });
  }

  function pick(i) {
    const it = results[i];
    if (!it) return;
    input.value = it.pt;
    close();
    onSelect(it);
  }

  async function runSearch() {
    const p = searchParams();
    if (!hasActiveSearch(p)) {
      results = [];
      render();
      close();
      return;
    }
    const thisReq = ++req;
    render('buscando…');
    try {
      const found = await api('/api/search', p);
      if (thisReq !== req) return;
      results = found;
      sel = -1;
      render(results.length ? '' : 'nenhum item encontrado');
    } catch (e) {
      if (thisReq !== req) return;
      results = [];
      render('erro na busca');
    }
  }

  function scheduleSearch(delay = 160) {
    clearTimeout(timer);
    timer = setTimeout(runSearch, delay);
  }

  input.addEventListener('input', () => scheduleSearch());
  input.addEventListener('keydown', (ev) => {
    if (!drop.classList.contains('open')) return;
    if (!results.length) {
      if (ev.key === 'Escape') close();
      return;
    }
    if (ev.key === 'ArrowDown') { sel = Math.min(sel + 1, results.length - 1); render(); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); render(); ev.preventDefault(); }
    else if (ev.key === 'Enter') { pick(sel >= 0 ? sel : 0); ev.preventDefault(); }
    else if (ev.key === 'Escape') close();
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
  catSel.addEventListener('change', () => {
    fillSubs();
    scheduleSearch(0);
  });
  [subSel, tierSel, enchSel].forEach((selEl) =>
    selEl.addEventListener('change', () => scheduleSearch(0)));
  clearBtn.addEventListener('click', () => {
    input.value = '';
    catSel.value = '';
    tierSel.value = '';
    enchSel.value = '';
    fillSubs();
    results = [];
    render();
    close();
    input.focus();
  });
  if (batchBtn) {
    batchBtn.addEventListener('mousedown', (ev) => ev.preventDefault());
    batchBtn.addEventListener('click', () => {
      if (!results.length) return;
      options.onBatchSelect(results);
    });
  }
  return { input };
}

function makeChips(containerId, options, { selected = [], multi = true, onChange = null } = {}) {
  const box = $(containerId);
  const on = new Set(selected.map(String));
  box.innerHTML = '';
  for (const opt of options) {
    const b = document.createElement('span');
    b.className = 'chip' + (on.has(String(opt.value)) ? ' on' : '');
    b.innerHTML = opt.html || esc(opt.label);
    b.title = opt.title || '';
    b.addEventListener('click', () => {
      const v = String(opt.value);
      if (multi) {
        if (on.has(v)) on.delete(v); else on.add(v);
      } else {
        on.clear(); on.add(v);
      }
      box.querySelectorAll('.chip').forEach((c, i) =>
        c.classList.toggle('on', on.has(String(options[i].value))));
      if (onChange) onChange([...on]);
    });
    box.appendChild(b);
  }
  return {
    get: () => [...on],
    set: (vals) => {
      on.clear();
      vals.map(String).forEach((v) => on.add(v));
      box.querySelectorAll('.chip').forEach((c, i) =>
        c.classList.toggle('on', on.has(String(options[i].value))));
    },
  };
}

// tabela genérica ordenável
function renderTable(containerId, columns, rows, { sortKey = null, sortDir = -1, rowAttrs = null } = {}) {
  const box = $(containerId);
  let sk = sortKey, sd = sortDir;

  function draw() {
    const sorted = [...rows];
    if (sk) {
      const col = columns.find((c) => c.key === sk);
      sorted.sort((a, b) => {
        const va = col.value(a), vb = col.value(b);
        if (va === null || va === undefined) return 1;
        if (vb === null || vb === undefined) return -1;
        return (va < vb ? -1 : va > vb ? 1 : 0) * sd;
      });
    }
    box.innerHTML = `<table><thead><tr>${
      columns.map((c) => `<th class="${c.align || ''} ${c.key === sk ? 'sorted' : ''}" data-k="${c.key}">${
        esc(c.label)}${c.key === sk ? (sd < 0 ? ' ▼' : ' ▲') : ''}</th>`).join('')
    }</tr></thead><tbody>${
      sorted.map((r, i) => `<tr data-i="${i}" ${rowAttrs ? rowAttrs(r) : ''}>${
        columns.map((c) => `<td class="${c.align || ''}">${c.html(r)}</td>`).join('')
      }</tr>`).join('')
    }</tbody></table>`;

    box.querySelectorAll('th').forEach((th) => th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (sk === k) sd = -sd; else { sk = k; sd = -1; }
      draw();
    }));
    box.querySelectorAll('tbody tr').forEach((tr) => {
      const r = sorted[+tr.dataset.i];
      tr.addEventListener('click', (ev) => {
        if (ev.target.closest('.x-btn')) return;
        if (ev.target.closest('.hist-btn')) return;
        if (r._copy) copyText(r._copy);
      });
      const x = tr.querySelector('.x-btn');
      if (x) x.addEventListener('click', () => {
        hideFlip(r);
        tr.remove();
        if (containerId === 'scanTable') {
          state.scanRows = state.scanRows.filter((row) => flipKey(row) !== flipKey(r));
          updateScanStatus();
        }
      });
      const hist = tr.querySelector('.hist-btn');
      if (hist) hist.addEventListener('click', (ev) => {
        ev.stopPropagation();
        openHistoryForOpportunity(r);
      });
    });
  }
  draw();
}

function flipKey(o) {
  return [o.item_id, o.quality, o.buy_city, o.sell_city, o.buy_mode, o.sell_mode].join('|');
}

function hideFlip(o) {
  state.hidden.add(flipKey(o));
  localStorage.setItem('hiddenFlips', JSON.stringify([...state.hidden].slice(-500)));
  updateHiddenControls();
}

// colunas compartilhadas das tabelas de flip
function flipColumns(withVolume) {
  const cols = [
    {
      key: 'item', label: 'Item', align: 'l',
      value: (o) => o.name_pt,
      html: (o) => `<div class="cell-item">
        ${iconImg(o.item_id, o.quality)}
        <div class="nm">${teBadge(o)} ${esc(o.name_pt)} ${qHtml(o.quality)}
        <small>${esc(o.item_id)}</small></div></div>`,
    },
    {
      key: 'route', label: 'Rota', align: 'l',
      value: (o) => o.buy_city + o.sell_city,
      html: (o) => `<div class="route">${cityHtml(o.buy_city)} <span class="arr">→</span> ${
        cityHtml(o.sell_city)}${o.bm_order_quality && o.bm_order_quality !== o.quality
        ? ` <small class="muted" title="sua peça preenche uma ordem do Mercado Negro de qualidade menor">(ordem ${esc(qName(o.bm_order_quality))})</small>` : ''}</div>`,
    },
    {
      key: 'buy', label: 'Compra',
      value: (o) => o.buy_price,
      html: (o) => `<span class="silver">${fmt(o.buy_price)}</span> ${ageHtml(o.buy_age_min)}`,
    },
    {
      key: 'sell', label: 'Venda',
      value: (o) => o.sell_price,
      html: (o) => `<span class="silver">${fmt(o.sell_price)}</span> ${ageHtml(o.sell_age_min)}`,
    },
    {
      key: 'cost', label: 'Custo', value: (o) => o.cost,
      html: (o) => `<span class="silver muted">${fmt(o.cost)}</span>`,
    },
    {
      key: 'revenue', label: 'Líquido', value: (o) => o.revenue,
      html: (o) => `<span class="silver muted">${fmt(o.revenue)}</span>`,
    },
    {
      key: 'profit', label: 'Lucro', value: (o) => o.profit,
      html: (o) => `<span class="silver ${o.profit >= 0 ? 'profit-pos' : 'profit-neg'}">${fmt(o.profit)}</span>`,
    },
    {
      key: 'roi', label: 'ROI %', value: (o) => o.roi_pct,
      html: (o) => `${o.roi_pct.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`,
    },
    {
      key: 'conf', label: 'Conf.', align: 'c',
      value: (o) => o.confidence_score ?? 0,
      html: (o) => {
        const label = o.confidence_label || 'baixa';
        const cls = label.replace(/\s+/g, '-');
        return `<span class="conf conf-${esc(cls)}" title="${esc(o.confidence_notes || '')}">${esc(label)}</span>`;
      },
    },
    {
      key: 'ppk', label: 'Lucro/kg', value: (o) => o.profit_per_kg,
      html: (o) => o.profit_per_kg === null ? '—' : `<span class="silver">${fmt(o.profit_per_kg)}</span>`,
    },
  ];
  if (withVolume) {
    cols.push(
      {
        key: 'vol', label: 'Vol/dia', align: 'c',
        value: (o) => Math.min(o.vol_buy_day ?? -1, o.vol_sell_day ?? -1),
        html: (o) => `<span class="muted" title="volume diário: origem / destino">${
          o.vol_buy_day ?? '?'} / ${o.vol_sell_day ?? '?'}</span>`,
      },
      {
        key: 'pot', label: 'Pot./dia', value: (o) => o.daily_realistic ?? o.daily_potential,
        html: (o) => o.daily_potential === null || o.daily_potential === undefined
          ? '—' : `<span class="silver profit-pos" title="com captura de ${
            Math.round((o.capture_rate ?? 1) * 100)}%; teto teórico: ${fmt(o.daily_potential)}">${
            fmt(o.daily_realistic ?? o.daily_potential)}</span>`,
      });
  }
  cols.push({
    key: 'x', label: '', align: 'c', value: () => 0,
    html: () => `<span class="x-btn" title="ocultar (flip já executado)">✕</span>`,
  });
  return cols;
}

function recommendationColumns() {
  return [
    {
      key: 'score', label: 'Score', align: 'c',
      value: (o) => o.opportunity_score ?? 0,
      html: (o) => `<span class="score score-${esc(o.opportunity_label || 'cautela')}">` +
        `${(o.opportunity_score ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}</span>`,
    },
    {
      key: 'item', label: 'Item', align: 'l',
      value: (o) => o.name_pt,
      html: (o) => `<div class="cell-item">
        ${iconImg(o.item_id, o.quality)}
        <div class="nm">${teBadge(o)} ${esc(o.name_pt)} ${qHtml(o.quality)}
        <small>${esc(o.item_id)}</small></div></div>`,
    },
    {
      key: 'route', label: 'Rota', align: 'l',
      value: (o) => o.buy_city + o.sell_city,
      html: (o) => `<div class="route">${cityHtml(o.buy_city)} <span class="arr">→</span> ${cityHtml(o.sell_city)}</div>`,
    },
    {
      key: 'buy', label: 'Compra',
      value: (o) => o.buy_price,
      html: (o) => `<span class="silver">${fmt(o.buy_price)}</span> ${ageHtml(o.buy_age_min)}`,
    },
    {
      key: 'sell', label: 'Venda',
      value: (o) => o.sell_price,
      html: (o) => `<span class="silver">${fmt(o.sell_price)}</span> ${ageHtml(o.sell_age_min)}`,
    },
    {
      key: 'profit', label: 'Lucro', value: (o) => o.profit,
      html: (o) => `<span class="silver ${o.profit >= 0 ? 'profit-pos' : 'profit-neg'}">${fmt(o.profit)}</span>`,
    },
    {
      key: 'roi', label: 'ROI %', value: (o) => o.roi_pct,
      html: (o) => `${o.roi_pct.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`,
    },
    {
      key: 'liq', label: 'Liq./dia', value: (o) => o.liquidity_day,
      html: (o) => `<span class="silver">${fmt(o.liquidity_day)}</span>`,
    },
    {
      key: 'freq', label: 'Freq.', align: 'c', value: (o) => o.sales_days_min,
      html: (o) => `<span class="muted">${o.sales_days_min ?? 0}/${o.history_days ?? '?'}</span>`,
    },
    {
      key: 'sold', label: 'Vendidos', value: (o) => o.volume_min_total,
      html: (o) => `<span class="silver" title="total no período: origem ${fmt(o.vol_buy_total)} / destino ${fmt(o.vol_sell_total)}">${fmt(o.volume_min_total)}</span>`,
    },
    {
      key: 'pot', label: 'Pot./dia', value: (o) => o.daily_realistic ?? o.daily_potential,
      html: (o) => `<span class="silver profit-pos" title="com captura de ${
        Math.round((o.capture_rate ?? 1) * 100)}%; teto teórico: ${fmt(o.daily_potential)}">${
        fmt(o.daily_realistic ?? o.daily_potential)}</span>`,
    },
    {
      key: 'conf', label: 'Conf.', align: 'c',
      value: (o) => o.confidence_score ?? 0,
      html: (o) => {
        const label = o.confidence_label || 'baixa';
        const cls = label.replace(/\s+/g, '-');
        return `<span class="conf conf-${esc(cls)}" title="${esc(o.confidence_notes || '')}">${esc(label)}</span>`;
      },
    },
    {
      key: 'chart', label: 'Gráfico', align: 'c', value: () => 0,
      html: () => '<button type="button" class="mini-btn hist-btn">abrir</button>',
    },
  ];
}

function prepFlipRows(opps) {
  return opps
    .filter((o) => !state.hidden.has(flipKey(o)))
    .map((o) => ({ ...o, _copy: o.name_pt }));
}

function updateHiddenControls() {
  const b = $('scanClearHidden');
  if (b) b.disabled = state.hidden.size === 0;
}

// ============================================================ abas
document.querySelectorAll('#tabs button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#tabs button').forEach((x) => x.classList.remove('active'));
    document.querySelectorAll('section.tab').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    $('tab-' + b.dataset.tab).classList.add('active');
  });
});

// ============================================================ premium
function premiumLabel() {
  const lbl = document.querySelector('.premium-toggle');
  lbl.childNodes[lbl.childNodes.length - 1].textContent =
    ` Premium (imposto ${state.premium ? '4%' : '8%'})`;
}
$('premiumToggle').checked = state.premium;
$('premiumToggle').addEventListener('change', (e) => {
  state.premium = e.target.checked;
  localStorage.setItem('premium', state.premium ? '1' : '0');
  premiumLabel();
  toast(`imposto de venda: ${state.premium ? '4%' : '8%'} — recalcule as abas abertas`);
});

// ============================================================ favoritos
function saveFavs() {
  localStorage.setItem('favorites', JSON.stringify(state.favorites));
  renderFavs();
}

function renderFavs() {
  const box = $('favList');
  if (!state.favorites.length) {
    box.innerHTML = '<span class="muted">nenhum — use a ☆ no card do item</span>';
    return;
  }
  box.innerHTML = '';
  for (const f of state.favorites) {
    const c = document.createElement('span');
    c.className = 'chip';
    c.textContent = f.pt;
    c.title = f.id;
    c.addEventListener('click', () => selectPrecosItem(f));
    box.appendChild(c);
  }
}

function itemCardHtml(it, favButton) {
  const fav = state.favorites.some((f) => f.id === it.id);
  const catLabel = state.meta?.categories?.[it.cat]?.label || it.cat;
  return `<div class="item-card">
    ${iconImg(it.id)}
    <div>
      <h2>${teBadge(it)} ${esc(it.pt)} ${favButton
        ? `<span class="fav-star ${fav ? 'on' : ''}" id="favStar" title="favoritar">${fav ? '★' : '☆'}</span>` : ''}</h2>
      <div class="sub">${esc(it.en)} ·
        <span class="copy-id" title="clique para copiar o id">${esc(it.id)}</span> ·
        ${esc(catLabel)} · peso ${it.w} kg</div>
    </div>
  </div>`;
}

function wireItemCard(boxId, it) {
  const box = $(boxId);
  box.querySelector('.copy-id')?.addEventListener('click', () => copyText(it.id));
  box.querySelector('#favStar')?.addEventListener('click', () => {
    const i = state.favorites.findIndex((f) => f.id === it.id);
    if (i >= 0) state.favorites.splice(i, 1);
    else state.favorites.push({ id: it.id, pt: it.pt });
    saveFavs();
    box.innerHTML = itemCardHtml(it, true);
    wireItemCard(boxId, it);
  });
}

// ============================================================ recomendações
function renderRecommendationTable(tableId, statusId, rows, meta, emptyText) {
  const withCopy = rows.map((o) => ({ ...o, _copy: o.name_pt }));
  renderTable(tableId, recommendationColumns(), withCopy, { sortKey: 'score' });
  const st = $(statusId);
  const hist = meta?.history_until ? ` · histórico até ${meta.history_until.slice(0, 10)}` : '';
  const cov = meta?.coverage;
  const coverage = cov
    ? ` · cobertura: ${fmt(cov.price_items)}/${fmt(cov.catalog_items)} itens com preço, ${fmt(cov.history_items)} com histórico`
    : '';
  st.textContent = rows.length
    ? `${rows.length} recomendações · ${meta?.items_considered ?? 0} itens avaliados${coverage}${hist}`
    : `${emptyText}${coverage}`;
}

async function loadDashboardRecommendations() {
  const st = $('dashRecsStatus');
  st.className = 'status';
  st.textContent = 'calculando recomendações…';
  $('dashRecsRefresh').disabled = true;
  try {
    const res = await api('/api/recommendations', {
      premium: state.premium,
      min_daily_volume: 20,
      min_active_days: 2,
      max_age_buy: 720,
      max_age_sell: 720,
      min_profit: 0,
      limit: 10,
    });
    state.dashRecs = res.opportunities || [];
    renderRecommendationTable('dashRecsTable', 'dashRecsStatus', state.dashRecs, res,
      'sem recomendações com os filtros atuais');
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('dashRecsRefresh').disabled = false;
  }
}

function fillDiscoverySubs() {
  const cat = $('discCat').value;
  const subs = cat ? state.meta.categories[cat]?.subs || {} : {};
  $('discSub').disabled = !cat;
  $('discSub').innerHTML = '<option value="">todas</option>' +
    Object.entries(subs).map(([id, s]) =>
      `<option value="${esc(id)}">${esc(s.label)} (${s.count})</option>`).join('');
}

function discoveryParams() {
  return {
    cat: $('discCat').value,
    sub: $('discSub').value,
    tier_min: $('discTierMin').value,
    tier_max: $('discTierMax').value,
    ench: $('discEnch').value,
    qualities: $('discQuality').value,
    premium: state.premium,
    min_daily_volume: $('discMinVolume').value || 0,
    min_active_days: $('discMinActiveDays').value || 0,
    max_age_buy: $('discMaxAgeBuy').value,
    max_age_sell: $('discMaxAgeSell').value,
    min_profit: $('discMinProfit').value || 0,
    min_roi: $('discMinRoi').value,
    history_days: $('discHistoryDays').value,
    limit: $('discLimit').value,
  };
}

async function runDiscover() {
  const st = $('discoverStatus');
  st.className = 'status';
  st.textContent = 'pesquisando oportunidades no cache…';
  $('discoverRun').disabled = true;
  try {
    const res = await api('/api/recommendations', discoveryParams());
    state.discoverRows = res.opportunities || [];
    renderRecommendationTable('discoverTable', 'discoverStatus', state.discoverRows, res,
      'nenhuma oportunidade encontrada');
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('discoverRun').disabled = false;
  }
}

// ============================================================ aba PREÇOS
let precosQuals;

async function loadPrecos(fresh = false) {
  const it = state.precosItem;
  if (!it) return;
  const st = $('precosStatus');
  st.className = 'status';
  st.textContent = 'consultando preços…';
  try {
    const rows = await api('/api/prices', {
      items: it.id,
      qualities: precosQuals.get().join(','),
      max_age: fresh ? 0 : undefined,
    });
    const cityOrder = state.meta.cities;
    rows.sort((a, b) => cityOrder.indexOf(a.city) - cityOrder.indexOf(b.city)
      || a.quality - b.quality);
    const cols = [
      { key: 'city', label: 'Cidade', align: 'l', value: (r) => cityOrder.indexOf(r.city), html: (r) => cityHtml(r.city) },
      { key: 'q', label: 'Qualidade', align: 'l', value: (r) => r.quality, html: (r) => qHtml(r.quality) },
      {
        key: 'sell', label: 'Venda mín.', value: (r) => r.sell_price_min || null,
        html: (r) => r.sell_price_min > 0
          ? `<span class="silver">${fmt(r.sell_price_min)}</span> ${ageHtml(r.sell_age_min)}`
          : '<span class="muted">sem dado</span>',
      },
      {
        key: 'buy', label: 'Compra máx.', value: (r) => r.buy_price_max || null,
        html: (r) => r.buy_price_max > 0
          ? `<span class="silver">${fmt(r.buy_price_max)}</span> ${ageHtml(r.buy_age_min)}`
          : '<span class="muted">sem dado</span>',
      },
      {
        key: 'spread', label: 'Margem interna', title: 'venda mín − compra máx',
        value: (r) => r.sell_price_min > 0 && r.buy_price_max > 0
          ? r.sell_price_min - r.buy_price_max : null,
        html: (r) => r.sell_price_min > 0 && r.buy_price_max > 0
          ? `<span class="silver muted">${fmt(r.sell_price_min - r.buy_price_max)}</span>` : '—',
      },
    ];
    renderTable('precosTable', cols, rows, { sortKey: 'city', sortDir: 1 });
    st.textContent = `${rows.length} linhas (cidade × qualidade)`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

function selectPrecosItem(it) {
  api('/api/search', { q: it.id, limit: 1 }).then((r) => {
    state.precosItem = r[0] || it;
    $('precosCard').innerHTML = itemCardHtml(state.precosItem, true);
    wireItemCard('precosCard', state.precosItem);
    loadPrecos();
  });
}

// ============================================================ aba FLIPS
let flipsQuals;

function renderFlipsItems() {
  const box = $('flipsItems');
  box.innerHTML = state.flipsItems.length ? '' : '<span class="muted">nenhum</span>';
  state.flipsItems.forEach((it, i) => {
    const c = document.createElement('span');
    c.className = 'item-chip';
    c.innerHTML = `${iconImg(it.id)} T${it.tier}.${it.ench} ${esc(it.pt)}
      <span class="rm" title="remover">✕</span>`;
    c.querySelector('.rm').addEventListener('click', () => {
      state.flipsItems.splice(i, 1);
      renderFlipsItems();
    });
    box.appendChild(c);
  });
}

async function runFlips() {
  if (!state.flipsItems.length) { toast('adicione ao menos um item'); return; }
  const st = $('flipsStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  $('flipsRun').disabled = true;
  try {
    const opps = await api('/api/flips', {
      items: state.flipsItems.map((i) => i.id).join(','),
      qualities: flipsQuals.get().join(','),
      premium: state.premium,
      buy_mode: $('flipsBuyMode').value,
      sell_mode: $('flipsSellMode').value,
      min_profit: $('flipsMinProfit').value || 0,
      same_city: $('flipsSameCity').checked,
    });
    const rows = prepFlipRows(opps);
    renderTable('flipsTable', flipColumns(false), rows, { sortKey: 'profit' });
    st.textContent = `${rows.length} oportunidades (lucro líquido, imposto ${state.premium ? '4%' : '8%'})`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('flipsRun').disabled = false;
  }
}

// ============================================================ aba SCANNER
let scanEnch, scanQuals, scanBuyCities, scanSellCities;

function fillScanSubs() {
  const cat = $('scanCat').value;
  const subs = state.meta.categories[cat]?.subs || {};
  $('scanSub').innerHTML = '<option value="">todas</option>' +
    Object.entries(subs).map(([id, s]) =>
      `<option value="${esc(id)}">${esc(s.label)} (${s.count})</option>`).join('');
}

function updateScanStatus() {
  const hiddenCount = state.scanOpps.length - state.scanRows.length;
  $('scanStatus').textContent = `${state.scanItemsScanned} itens escaneados · ${state.scanRows.length} oportunidades` +
    (hiddenCount ? ` · ${hiddenCount} ocultas` : '');
  $('scanExport').disabled = state.scanRows.length === 0;
  updateHiddenControls();
}

function renderScanResults() {
  state.scanRows = prepFlipRows(state.scanOpps);
  renderTable('scanTable', flipColumns($('scanVolume').checked), state.scanRows,
    { sortKey: 'profit' });
  updateScanStatus();
}

async function runScan() {
  const st = $('scanStatus');
  st.className = 'status';
  const maxItems = +$('scanMaxItems').value;
  st.textContent = `escaneando até ${maxItems} itens — a primeira vez pode levar de 10 s a 1 min (limite da API)…`;
  $('scanRun').disabled = true;
  $('scanExport').disabled = true;
  try {
    const res = await api('/api/scan', {
      cat: $('scanCat').value,
      sub: $('scanSub').value,
      tier_min: $('scanTierMin').value,
      tier_max: $('scanTierMax').value,
      ench: scanEnch.get().join(','),
      qualities: scanQuals.get().join(','),
      buy_cities: scanBuyCities.get().join(','),
      sell_cities: scanSellCities.get().join(','),
      premium: state.premium,
      buy_mode: $('scanBuyMode').value,
      sell_mode: $('scanSellMode').value,
      min_profit: $('scanMinProfit').value || 0,
      min_roi: $('scanMinRoi').value,
      max_age_buy: $('scanMaxAgeBuy').value,
      max_age_sell: $('scanMaxAgeSell').value,
      same_city: $('scanSameCity').checked,
      max_items: maxItems,
      with_volume: $('scanVolume').checked,
      limit: 200,
    });
    state.scanOpps = res.opportunities;
    state.scanItemsScanned = res.items_scanned;
    renderScanResults();
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('scanRun').disabled = false;
  }
}

function clearHiddenFlips() {
  if (!state.hidden.size) {
    toast('nenhum flip oculto');
    return;
  }
  state.hidden.clear();
  localStorage.removeItem('hiddenFlips');
  if (state.scanOpps.length) renderScanResults();
  updateHiddenControls();
  toast('flips ocultos restaurados');
}

function exportScanCsv() {
  const cols = ['item_id', 'name_pt', 'tier', 'ench', 'quality', 'buy_city', 'sell_city',
    'buy_mode', 'sell_mode', 'buy_price', 'sell_price', 'cost', 'revenue', 'profit',
    'roi_pct', 'confidence_score', 'confidence_label', 'confidence_notes',
    'profit_per_kg', 'vol_buy_day', 'vol_sell_day', 'daily_potential',
    'daily_realistic', 'capture_rate', 'buy_age_min', 'sell_age_min'];
  const lines = [cols.join(';')];
  for (const r of state.scanRows) {
    lines.push(cols.map((c) => {
      const v = r[c];
      if (v === null || v === undefined) return '';
      return typeof v === 'string' ? `"${v.replace(/"/g, '""')}"` : String(v).replace('.', ',');
    }).join(';'));
  }
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `flips_albion_${new Date().toISOString().slice(0, 16).replace(/[T:]/g, '-')}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

const SCAN_PRESETS = {
  bm: () => {
    scanBuyCities.set(state.meta.royal_cities);
    scanSellCities.set(['Black Market']);
    $('scanBuyMode').value = 'instant';
    $('scanSellMode').value = 'instant';
    $('scanCat').value = 'weapons';
    fillScanSubs();
    $('scanMinProfit').value = 5000;
    $('scanSameCity').checked = false;
    toast('preset: comprar nas cidades reais, vender ao Mercado Negro');
  },
  royal: () => {
    scanBuyCities.set(state.meta.royal_cities);
    scanSellCities.set(state.meta.royal_cities);
    $('scanBuyMode').value = 'instant';
    $('scanSellMode').value = 'order';
    $('scanMinProfit').value = 2000;
    $('scanSameCity').checked = false;
    toast('preset: transporte entre cidades reais (compra instantânea, venda por ordem)');
  },
  mm: () => {
    const all = state.meta.cities.filter((c) => c !== 'Black Market');
    scanBuyCities.set(all);
    scanSellCities.set(all);
    $('scanBuyMode').value = 'order';
    $('scanSellMode').value = 'order';
    $('scanMinProfit').value = 1000;
    $('scanSameCity').checked = true;
    toast('preset: market making — ordens de compra e venda no mesmo mercado');
  },
};

// ============================================================ aba ONDE VENDER
let venderQuals;

async function loadVender(fresh = false) {
  const it = state.venderItem;
  if (!it) return;
  const st = $('venderStatus');
  st.className = 'status';
  st.textContent = 'consultando…';
  try {
    const res = await api('/api/sell', {
      items: it.id,
      qualities: venderQuals.get().join(','),
      premium: state.premium,
      max_age: fresh ? 0 : undefined,
    });
    const rows = [];
    for (const r of res) {
      for (const o of r.options) {
        rows.push({ ...o, quality: r.quality, _copy: r.name_pt });
      }
    }
    const cols = [
      { key: 'q', label: 'Qualidade', align: 'l', value: (r) => r.quality, html: (r) => qHtml(r.quality) },
      { key: 'city', label: 'Cidade', align: 'l', value: (r) => r.city, html: (r) => cityHtml(r.city) },
      {
        key: 'method', label: 'Método', align: 'l', value: (r) => r.method,
        html: (r) => r.method === 'instant'
          ? `Instantânea${r.bm_order_quality && r.bm_order_quality !== r.quality
            ? ` <small class="muted">(ordem ${esc(qName(r.bm_order_quality))})</small>` : ''}`
          : 'Ordem de venda',
      },
      { key: 'price', label: 'Preço', value: (r) => r.price, html: (r) => `<span class="silver">${fmt(r.price)}</span>` },
      {
        key: 'net', label: 'Líquido', value: (r) => r.net,
        html: (r) => `<span class="silver profit-pos">${fmt(r.net)}</span>`,
      },
      { key: 'age', label: 'Idade', align: 'c', value: (r) => r.age_min, html: (r) => ageHtml(r.age_min) },
    ];
    renderTable('venderTable', cols, rows, { sortKey: 'net' });
    st.textContent = `${rows.length} opções de venda (imposto ${state.premium ? '4%' : '8%'})`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

// ============================================================ aba HISTÓRICO
let histCities, priceChart = null, volChart = null;

const CITY_COLORS = {
  'Bridgewatch': '#d9a14f', 'Caerleon': '#d96a4f', 'Fort Sterling': '#e8e8e8',
  'Lymhurst': '#6fbf5f', 'Martlock': '#5f9fd9', 'Thetford': '#a86fd9',
  'Brecilien': '#d95fb8', 'Black Market': '#999999',
};

function renderItemLab(analysis) {
  const summary = $('histLabSummary');
  const cards = $('histLabCards');
  const comp = analysis?.comparison || {};
  const series = analysis?.series || [];
  if (!series.length) {
    summary.innerHTML = '<span class="muted">Sem amostra histórica local suficiente para análise estatística.</span>';
    cards.innerHTML = '';
    return;
  }
  summary.innerHTML = `
    <div class="lab-pill"><b>Mais barata</b><span>${cityHtml(comp.cheapest_city || '—')} · ${fmtDec(comp.cheapest_vwap)}</span></div>
    <div class="lab-pill"><b>Mais cara</b><span>${cityHtml(comp.most_expensive_city || '—')} · ${fmtDec(comp.most_expensive_vwap)}</span></div>
    <div class="lab-pill"><b>Spread VWAP</b><span>${fmtPct(comp.vwap_spread_pct)}</span></div>
    <div class="lab-pill"><b>Mais líquida</b><span>${cityHtml(comp.most_liquid_city || '—')} · ${fmt(comp.most_liquid_avg_daily_volume)}/dia</span></div>
    <div class="lab-pill"><b>Melhor dado</b><span>${cityHtml(comp.best_data_city || '—')} · ${fmtDec(comp.best_data_score)}</span></div>
  `;
  cards.innerHTML = series.map((s) => {
    const interp = s.interpretation || {};
    const notes = (interp.notes || []).slice(0, 3).map((n) => `<li>${esc(n)}</li>`).join('');
    const qCls = (s.data_quality_score || 0) >= 75 ? 'good' : (s.data_quality_score || 0) >= 50 ? 'warn' : 'bad';
    return `<div class="lab-card">
      <div class="lab-card-head">
        <h3>${cityHtml(s.city)}</h3>
        <span class="lab-stance">${esc(interp.stance || 'monitorar')}</span>
      </div>
      <div class="lab-metrics">
        <div><span>Qualidade</span><b class="${qCls}">${fmtDec(s.data_quality_score)}</b></div>
        <div><span>Preço atual</span><b>${fmtDec(s.latest_price)}</b></div>
        <div><span>VWAP</span><b>${fmtDec(s.vwap)}</b></div>
        <div><span>Z-score</span><b>${fmtDec(s.z_score, 2)}</b></div>
        <div><span>Momentum</span><b>${fmtPct(s.momentum_pct)}</b></div>
        <div><span>Volume/dia</span><b>${fmt(s.avg_daily_volume)}</b></div>
        <div><span>Freq.</span><b>${s.active_days}/${analysis.days}</b></div>
        <div><span>Percentil</span><b>${fmtDec(s.price_percentile)}</b></div>
      </div>
      ${s.naive_forecast_next ? `<div class="forecast">Próximo ponto provável: <b>${fmtDec(s.naive_forecast_next.price)}</b>
        <span>${fmtDec(s.naive_forecast_next.low)}–${fmtDec(s.naive_forecast_next.high)}</span></div>` : ''}
      <ul class="lab-notes">${notes}</ul>
    </div>`;
  }).join('');
}

async function runHist() {
  const it = state.histItem;
  if (!it) { toast('escolha um item'); return; }
  const st = $('histStatus');
  st.className = 'status';
  st.textContent = 'carregando histórico…';
  $('histRun').disabled = true;
  const cacheOnly = !$('histFetchNew').checked;
  if (!cacheOnly) st.textContent = 'buscando dados novos na API…';
  try {
    const series = await api('/api/history', {
      items: it.id,
      cities: histCities.get().join(','),
      quality: $('histQuality').value,
      time_scale: $('histScale').value,
      days: $('histDays').value,
      cache_only: cacheOnly,
    });
    const analysis = await api('/api/item-analysis', {
      item: it.id,
      cities: histCities.get().join(','),
      quality: $('histQuality').value,
      time_scale: $('histScale').value,
      days: $('histDays').value,
      cache_only: true,
    });
    renderItemLab(analysis);
    const allTs = [...new Set(series.flatMap((s) => s.data.map((p) => p.ts)))].sort();
    const fmtTs = (ts) => {
      const d = new Date(ts);
      const dd = `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
      return $('histScale').value === '24' ? dd : `${dd} ${String(d.getHours()).padStart(2, '0')}h`;
    };
    const labels = allTs.map(fmtTs);
    const mkDatasets = (field, type) => series.map((s) => {
      const byTs = Object.fromEntries(s.data.map((p) => [p.ts, p[field]]));
      const color = CITY_COLORS[s.city] || '#d4a843';
      return {
        label: state.meta.city_labels[s.city] || s.city,
        data: allTs.map((ts) => byTs[ts] ?? null),
        borderColor: color,
        backgroundColor: type === 'bar' ? color + '99' : color,
        spanGaps: true,
        pointRadius: 2,
        tension: 0.25,
      };
    });
    if (priceChart) priceChart.destroy();
    if (volChart) volChart.destroy();
    Chart.defaults.color = '#9a8c6d';
    Chart.defaults.borderColor = '#3d3322';
    priceChart = new Chart($('histPriceChart'), {
      type: 'line',
      data: { labels, datasets: mkDatasets('avg_price', 'line') },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { title: { display: true, text: `Preço médio — ${it.pt} (${qName(+$('histQuality').value)})` } },
        scales: { y: { ticks: { callback: (v) => fmt(v) } } },
      },
    });
    volChart = new Chart($('histVolChart'), {
      type: 'bar',
      data: { labels, datasets: mkDatasets('item_count', 'bar') },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { title: { display: true, text: 'Volume vendido (itens)' } },
      },
    });
    st.textContent = `${series.length} séries · ${allTs.length} pontos`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('histRun').disabled = false;
  }
}

function activateTab(tabId) {
  document.querySelectorAll('#tabs button').forEach((x) =>
    x.classList.toggle('active', x.dataset.tab === tabId));
  document.querySelectorAll('section.tab').forEach((x) =>
    x.classList.toggle('active', x.id === 'tab-' + tabId));
}

function openHistoryForOpportunity(o) {
  state.histItem = {
    id: o.item_id,
    pt: o.name_pt || o.item_id,
    en: o.name_en || o.item_id,
    tier: o.tier,
    ench: o.ench,
    cat: o.cat,
    sub: o.sub,
    w: o.weight,
  };
  histCities.set([o.buy_city, o.sell_city].filter(Boolean));
  $('histQuality').value = String(o.quality || 1);
  $('histDays').value = '7';
  $('histScale').value = '24';
  activateTab('historico');
  runHist();
  toast('gráfico histórico carregado');
}

// ============================================================ ouro
async function loadGold() {
  try {
    const g = await api('/api/gold', { count: 2 });
    if (g.length) {
      const cur = g[0].price, prev = g[1]?.price;
      const delta = prev ? cur - prev : 0;
      $('goldTicker').innerHTML = `1 ouro = <b>${fmt(cur)}</b> prata ` +
        (delta ? `<span class="${delta > 0 ? 'profit-pos' : 'profit-neg'}">${delta > 0 ? '▲' : '▼'}${fmt(Math.abs(delta))}</span>` : '');
    }
  } catch (e) { $('goldTicker').textContent = ''; }
}

async function loadSurvival() {
  const st = $('survStatus');
  try {
    const res = await api('/api/survival');
    const sidePt = { sell: 'venda (anúncio)', buy: 'compra (ordem)' };
    const rows = [];
    for (const [side, data] of Object.entries(res.sides || {})) {
      for (const b of data.buckets || []) {
        if (b.pairs) rows.push({ ...b, side: sidePt[side] || side });
      }
    }
    if (!rows.length) {
      st.textContent = 'ainda sem pares de coleta suficientes — use "Coletar watchlist" algumas vezes ao longo do dia';
      $('survTable').innerHTML = '';
      return;
    }
    const cols = [
      { key: 'side', label: 'Lado', align: 'l', value: (r) => r.side, html: (r) => esc(r.side) },
      { key: 'age', label: 'Idade da ordem', align: 'l', value: (r) => r.age_min, html: (r) => esc(r.age_label) },
      { key: 'pairs', label: 'Pares', value: (r) => r.pairs, html: (r) => fmt(r.pairs) },
      {
        key: 'rate', label: 'Persistiu %', value: (r) => r.rate_pct,
        html: (r) => {
          const cls = r.rate_pct >= 70 ? 'profit-pos' : r.rate_pct >= 40 ? '' : 'profit-neg';
          return `<span class="${cls}">${fmtDec(r.rate_pct, 1)}%</span>`;
        },
      },
      { key: 'gap', label: 'Gap mediano', align: 'c', value: (r) => r.median_gap_min, html: (r) => `${fmtDec(r.median_gap_min, 0)} min` },
    ];
    renderTable('survTable', cols, rows, { sortKey: 'age', sortDir: 1 });
    st.textContent = `${res.snapshot_pairs} pares de coleta analisados — ordens velhas que "persistem" pouco são os flips fantasmas`;
  } catch (e) {
    st.textContent = '';
  }
}

function fmtCompact(n) {
  if (n >= 1e6) return (n / 1e6).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3).toLocaleString('pt-BR') + 'k';
  return fmt(n);
}

async function loadHeatmap() {
  const st = $('heatStatus');
  try {
    const res = await api('/api/recommendations', {
      premium: state.premium, limit: 200, min_daily_volume: 5,
      min_active_days: 2, max_age_buy: 720, max_age_sell: 720,
    });
    const opps = res.opportunities || [];
    if (!opps.length) {
      st.textContent = 'sem rotas com os dados atuais — colete mais algumas vezes';
      $('heatTable').innerHTML = '';
      return;
    }
    const routes = {};
    for (const o of opps) {
      const r = routes[o.buy_city + '|' + o.sell_city] ||= {
        buy: o.buy_city, sell: o.sell_city, total: 0, n: 0, top: null,
      };
      r.total += o.daily_realistic || 0;
      r.n += 1;
      if (!r.top || (o.daily_realistic || 0) > (r.top.daily_realistic || 0)) r.top = o;
    }
    const buys = [...new Set(Object.values(routes).map((r) => r.buy))].sort();
    const sells = [...new Set(Object.values(routes).map((r) => r.sell))].sort();
    const max = Math.max(...Object.values(routes).map((r) => r.total), 1);
    let html = '<table><thead><tr><th class="l">compra ↓ / venda →</th>' +
      sells.map((s) => `<th>${cityHtml(s)}</th>`).join('') + '</tr></thead><tbody>';
    for (const b of buys) {
      html += `<tr><td class="l">${cityHtml(b)}</td>`;
      for (const s of sells) {
        const r = routes[b + '|' + s];
        if (!r) { html += '<td class="muted c">—</td>'; continue; }
        const alpha = (0.12 + 0.55 * r.total / max).toFixed(2);
        const meta = state.meta;
        html += `<td class="c" style="background:rgba(212,168,67,${alpha})" ` +
          `title="${esc(r.top.name_pt)}: ${fmt(r.top.daily_realistic)}/dia · ${r.n} oportunidades">` +
          `<b>${fmtCompact(r.total)}</b><br><small class="muted">${r.n} itens</small></td>`;
      }
      html += '</tr>';
    }
    $('heatTable').innerHTML = html + '</tbody></table>';
    st.textContent = `${opps.length} oportunidades agregadas em ${Object.keys(routes).length} rotas (passe o mouse para ver o item líder)`;
  } catch (e) {
    st.textContent = '';
  }
}

async function refreshWatchCount() {
  try {
    const w = await api('/api/watchlist');
    $('dashCollect').textContent = `Coletar watchlist (${w.count})`;
    $('dashCollect').disabled = w.count === 0;
  } catch (e) { /* opcional */ }
}

async function runCollect() {
  const b = $('dashCollect');
  b.disabled = true;
  const original = b.textContent;
  b.textContent = 'coletando…';
  try {
    const res = await apiSend('/api/collect', 'POST');
    toast(`coletados ${res.items} itens · ${res.price_rows} preços · ${res.history_series} séries`);
    loadStatus();
    loadDashboardRecommendations();
    loadSurvival();
    loadHeatmap();
  } catch (e) {
    toast('erro na coleta: ' + e.message);
  } finally {
    b.textContent = original;
    refreshWatchCount();
  }
}

async function watchCurrentLabItem() {
  if (!state.histItem) { toast('escolha um item primeiro'); return; }
  try {
    const res = await apiSend('/api/watchlist/' + encodeURIComponent(state.histItem.id), 'POST');
    toast(`${state.histItem.pt} na watchlist (${res.count} itens)`);
    refreshWatchCount();
  } catch (e) {
    toast('erro: ' + e.message);
  }
}

async function loadStatus() {
  try {
    const s = await api('/api/status');
    const prices = s.tables?.prices?.rows ?? 0;
    const latest = s.tables?.prices?.latest || 'sem cache';
    $('cacheStatus').textContent = `cache: ${fmt(prices)} preços`;
    $('cacheStatus').title = `última coleta de preços: ${latest}`;
  } catch (e) {
    $('cacheStatus').textContent = '';
  }
}

// ============================================================ init
async function init() {
  state.meta = await api('/api/meta');
  premiumLabel();
  renderFavs();
  loadGold();
  loadStatus();
  setInterval(loadGold, 5 * 60 * 1000);
  setInterval(loadStatus, 5 * 60 * 1000);

  const qualOpts = Object.entries(state.meta.qualities)
    .map(([v, l]) => ({ value: v, label: l, html: `<span class="q${v}">${esc(l)}</span>` }));
  const allQ = Object.keys(state.meta.qualities);
  precosQuals = makeChips('precosQuals', qualOpts, { selected: allQ, onChange: () => loadPrecos() });
  flipsQuals = makeChips('flipsQuals', qualOpts, { selected: allQ });
  scanQuals = makeChips('scanQuals', qualOpts, { selected: allQ });
  venderQuals = makeChips('venderQuals', qualOpts, { selected: allQ, onChange: () => loadVender() });

  const enchOpts = [0, 1, 2, 3, 4].map((e) => ({ value: e, label: '.' + e }));
  scanEnch = makeChips('scanEnch', enchOpts, { selected: [0, 1, 2, 3, 4] });

  const cityOpts = state.meta.cities.map((c) => ({ value: c, label: state.meta.city_labels[c] || c }));
  const buyCityOpts = cityOpts.filter((c) => c.value !== 'Black Market');
  scanBuyCities = makeChips('scanBuyCities', buyCityOpts, { selected: state.meta.royal_cities });
  scanSellCities = makeChips('scanSellCities', cityOpts, { selected: state.meta.cities });
  histCities = makeChips('histCities', cityOpts, { selected: ['Caerleon', 'Lymhurst'] });

  // descoberta de flips
  $('discCat').innerHTML = '<option value="">todas</option>' + Object.entries(state.meta.categories)
    .map(([id, c]) => `<option value="${esc(id)}">${esc(c.label)} (${c.count})</option>`).join('');
  fillDiscoverySubs();
  $('discCat').addEventListener('change', fillDiscoverySubs);
  for (const sel of ['discTierMin', 'discTierMax']) {
    $(sel).innerHTML = '<option value="">todos</option>' +
      [1, 2, 3, 4, 5, 6, 7, 8].map((t) => `<option value="${t}">${t}</option>`).join('');
  }
  $('discEnch').innerHTML = '<option value="">todos</option>' +
    [0, 1, 2, 3, 4].map((e) => `<option value="${e}">.${e}</option>`).join('');
  $('discQuality').innerHTML = '<option value="">todas</option>' + qualOpts
    .map((q) => `<option value="${q.value}">${esc(q.label)}</option>`).join('');
  $('discQuality').value = '1';

  // categorias do scanner
  $('scanCat').innerHTML = Object.entries(state.meta.categories)
    .map(([id, c]) => `<option value="${esc(id)}">${esc(c.label)} (${c.count})</option>`).join('');
  $('scanCat').value = 'weapons';
  fillScanSubs();
  $('scanCat').addEventListener('change', fillScanSubs);

  for (const sel of ['scanTierMin', 'scanTierMax']) {
    $(sel).innerHTML = [1, 2, 3, 4, 5, 6, 7, 8].map((t) => `<option>${t}</option>`).join('');
  }
  $('scanTierMin').value = '4';
  $('scanTierMax').value = '8';

  $('histQuality').innerHTML = qualOpts
    .map((q) => `<option value="${q.value}">${esc(q.label)}</option>`).join('');

  // pickers
  makeItemPicker('precosPicker', (it) => selectPrecosItem(it));
  makeItemPicker('flipsPicker', (it) => {
    if (!state.flipsItems.some((x) => x.id === it.id)) state.flipsItems.push(it);
    renderFlipsItems();
  }, 'buscar item… ex.: arco 4.1', {
    batchLabel: 'Adicionar visíveis',
    onBatchSelect: (items) => {
      let added = 0;
      for (const it of items) {
        if (!state.flipsItems.some((x) => x.id === it.id)) {
          state.flipsItems.push(it);
          added += 1;
        }
      }
      renderFlipsItems();
      toast(added ? `${added} itens adicionados` : 'itens já estavam na lista');
    },
  });
  makeItemPicker('venderPicker', (it) => {
    state.venderItem = it;
    $('venderCard').innerHTML = itemCardHtml(it, false);
    wireItemCard('venderCard', it);
    loadVender();
  });
  makeItemPicker('histPicker', (it) => { state.histItem = it; runHist(); });

  // botões
  $('glossaryOpen').addEventListener('click', () => setGlossary(true));
  $('glossaryClose').addEventListener('click', () => setGlossary(false));
  $('glossaryCloseBackdrop').addEventListener('click', () => setGlossary(false));
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') setGlossary(false);
  });
  $('dashRecsRefresh').addEventListener('click', loadDashboardRecommendations);
  $('dashCollect').addEventListener('click', runCollect);
  $('histWatch').addEventListener('click', watchCurrentLabItem);
  $('discoverRun').addEventListener('click', runDiscover);
  $('precosRefresh').addEventListener('click', () => loadPrecos(true));
  $('flipsRun').addEventListener('click', runFlips);
  $('scanRun').addEventListener('click', runScan);
  $('scanExport').addEventListener('click', exportScanCsv);
  $('scanClearHidden').addEventListener('click', clearHiddenFlips);
  $('venderRefresh').addEventListener('click', () => loadVender(true));
  $('histRun').addEventListener('click', runHist);
  $('histDays').addEventListener('change', () => {
    if ($('histDays').value === '1') $('histScale').value = '1';
  });
  document.querySelectorAll('[data-preset]').forEach((b) =>
    b.addEventListener('click', () => SCAN_PRESETS[b.dataset.preset]()));

  renderFlipsItems();
  updateHiddenControls();
  refreshWatchCount();
  loadDashboardRecommendations();
  loadSurvival();
  loadHeatmap();
  runDiscover();
}

init().catch((e) => {
  document.body.insertAdjacentHTML('afterbegin',
    `<div style="background:#d96a4f;color:#fff;padding:10px 16px">Erro ao iniciar: ${esc(e.message)}</div>`);
});
