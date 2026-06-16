'use strict';

// ============================================================ estado
const state = {
  meta: null,
  premium: localStorage.getItem('premium') !== '0',
  favorites: JSON.parse(localStorage.getItem('favorites') || '[]'),
  item: null,
  itemLoadedFor: {},
  precosItem: null,
  flipsItems: [],
  venderItem: null,
  histItem: null,
  scanOpps: [],
  scanItemsScanned: 0,
  scanItemsTotal: 0,
  scanRows: [],
  dashRecs: [],
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
  let results = [], sel = -1, timer = null, req = 0, truncated = false;

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
      limit: options.limit || 150,
      group: true,  // 1 linha por item-base; encantos viram chips
    };
  }

  // monta a variante concreta (base + @N) a partir de um item agrupado
  function variant(it, e) {
    const base = it.base_id || it.id.split('@')[0];
    return { ...it, id: e > 0 ? `${base}@${e}` : base, ench: e };
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
    drop.innerHTML = results.map((it, i) => {
      const list = (it.enchants && it.enchants.length) ? it.enchants : [it.ench];
      const te = list.length > 1
        ? `<div class="te te-ench">T${it.tier} ${list.map((e) =>
            `<span class="ench-chip" data-i="${i}" data-e="${e}">.${e}</span>`).join('')}</div>`
        : `<div class="te">T${it.tier}.${list[0] ?? 0}</div>`;
      return `<div class="opt ${i === sel ? 'sel' : ''}" data-i="${i}">
        ${iconImg(it.id)}
        <div class="nm">${esc(it.pt)}<small>${itemMetaHtml(it)}</small></div>
        ${te}
      </div>`;
    }).join('');
    if (truncated) {
      drop.innerHTML += `<div class="opt empty">há mais itens — refine a busca (adicione palavras, tier ou categoria)</div>`;
    }
    drop.classList.toggle('open', results.length > 0);
    if (batchBtn) batchBtn.disabled = results.length === 0;
    drop.querySelectorAll('.opt').forEach((o) => {
      if (o.classList.contains('empty')) return;  // pula a linha de aviso
      o.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        pick(+o.dataset.i);
      });
    });
    drop.querySelectorAll('.ench-chip').forEach((c) => {
      c.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        pick(+c.dataset.i, +c.dataset.e);
      });
    });
  }

  function pick(i, e) {
    const it = results[i];
    if (!it) return;
    const chosen = (e === undefined || e === null) ? it : variant(it, e);
    input.value = it.pt;
    close();
    onSelect(chosen);
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
      truncated = found.length >= p.limit;  // bateu no teto: há mais
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
  // o imposto entra no líquido: invalida o cache das sub-abas do Item para
  // não mostrar valor com o imposto antigo, e recarrega a sub-aba ativa
  state.itemLoadedFor = {};
  if (state.item && document.getElementById('tab-item').classList.contains('active')) {
    loadItemSub(activeItemSub(), true);
  }
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
function compositeBadge(v) {
  const cls = v >= 70 ? 'score-executar' : v >= 40 ? 'score-monitorar' : 'score-cautela';
  return `<span class="score ${cls}">${(v ?? 0).toLocaleString('pt-BR', { maximumFractionDigits: 1 })}</span>`;
}

function renderRecommendationTable(tableId, statusId, rows, meta, emptyText) {
  const withCopy = rows.map((o) => ({ ...o, _copy: o.name_pt }));
  const fused = meta?.fused;
  let cols = recommendationColumns();
  if (fused) {
    // escore COMPOSTO na frente + coluna de risco/sinais; ordena por composto
    cols = [
      {
        key: 'composite', label: 'Composto', align: 'c',
        value: (o) => o.composite_score ?? 0,
        html: (o) => compositeBadge(o.composite_score ?? 0),
      },
      ...cols,
      {
        key: 'risco', label: 'Risco / sinais', align: 'l',
        value: (o) => o.vol_pct ?? 0,
        html: (o) => {
          const vol = o.vol_pct != null ? `vol ${Math.round(o.vol_pct)}%` : '';
          const rev = o.revert_signal ? ' <span class="profit-pos">↻compra</span>' : '';
          const div = o.divergence ? ' <span class="bm">demanda↑</span>' : '';
          return `<span class="muted">${vol}</span>${rev}${div}`;
        },
      },
    ];
  }
  renderTable(tableId, cols, withCopy, { sortKey: fused ? 'composite' : 'score' });
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
      fused: $('dashFused') && $('dashFused').checked,
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
  // descoberta filtrada renderiza no MESMO painel de Recomendações do Início
  const st = $('dashRecsStatus');
  st.className = 'status';
  st.textContent = 'pesquisando oportunidades no cache…';
  $('discoverRun').disabled = true;
  try {
    const res = await api('/api/recommendations', discoveryParams());
    state.dashRecs = res.opportunities || [];
    renderRecommendationTable('dashRecsTable', 'dashRecsStatus', state.dashRecs, res,
      'nenhuma oportunidade com os filtros atuais');
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

// ============================================================ aba ITEM (unificada)
// Um único picker alimenta as sub-abas Preços / Onde Vender / Item Lab.
function activeItemSub() {
  const b = document.querySelector('#itemSubtabs button.active');
  return b ? b.dataset.sub : 'precos';
}

function loadItemSub(sub, force = false) {
  if (!state.item) return;
  if (!force && state.itemLoadedFor[sub] === state.item.id) return;
  state.itemLoadedFor[sub] = state.item.id;
  if (sub === 'precos') loadPrecos();
  else if (sub === 'vender') loadVender();
  else if (sub === 'craft') loadCraft();
  else if (sub === 'origem') loadOrigin();
  else if (sub === 'lab') runHist();
}

function showItemSub(sub) {
  document.querySelectorAll('#itemSubtabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.sub === sub));
  document.querySelectorAll('#tab-item .subtab').forEach((s) =>
    s.classList.toggle('active', s.id === 'sub-' + sub));
  loadItemSub(sub);
}

function selectItem(it) {
  state.item = it;
  // revela as sub-abas do inspetor (antes disso, mostra só a busca + orientação)
  document.getElementById('tab-item').classList.add('has-item');
  // as funções de carga ainda leem destes campos — mantém todas em sincronia
  state.precosItem = it;
  state.venderItem = it;
  state.histItem = it;
  state.itemLoadedFor = {};
  // limpa as sub-abas para não exibir dados do item anterior enquanto a
  // sub-aba não-ativa não foi recarregada (evita misturar itens)
  ['precosTable', 'venderTable', 'craftTable', 'origemTable',
   'histLabSummary', 'histLabCards'].forEach((id) => {
    const el = $(id); if (el) el.innerHTML = '';
  });
  if (priceChart) { priceChart.destroy(); priceChart = null; }
  if (volChart) { volChart.destroy(); volChart = null; }
  $('itemCard').innerHTML = itemCardHtml(it, true);
  wireItemCard('itemCard', it);
  loadItemSub(activeItemSub(), true);
}

// sub-aba Craft / Refino
async function loadCraft() {
  const it = state.item;
  if (!it) return;
  const st = $('craftStatus');
  st.className = 'status';
  st.textContent = 'calculando margem de craft…';
  try {
    const res = await api('/api/craft', {
      item: it.id, premium: state.premium,
      sell_mode: $('craftSellMode').value, focus: $('craftFocus').checked,
    });
    const m = res.margins || [];
    if (!m.length) {
      st.textContent = res.category
        ? 'sem preços de insumo/produto no cache para calcular — colete e tente de novo'
        : 'este item não tem receita de craft/refino no dump';
      $('craftTable').innerHTML = '';
      return;
    }
    const ins = (res.inputs || []).map((i) => `${i.count}× ${esc(i.name_pt)}`).join(' + ');
    st.innerHTML = `Receita: ${ins} · foco ${fmt(res.focus)} · categoria <b>${esc(res.category || '—')}</b>` +
      (res.bonus_city ? ` · cidade-bônus <b>${esc(res.bonus_city)}</b>` : '');
    const cols = [
      {
        key: 'city', label: 'Cidade', align: 'l', value: (r) => r.city,
        html: (r) => cityHtml(r.city) + (r.is_bonus_city ? ' <span class="te-badge">★</span>' : ''),
      },
      { key: 'mat', label: 'Insumos', value: (r) => r.materials, html: (r) => `<span class="silver">${fmt(r.materials)}</span>` },
      { key: 'rrr', label: 'RRR %', align: 'c', value: (r) => r.rrr_pct, html: (r) => `${fmtDec(r.rrr_pct, 1)}%` },
      { key: 'eff', label: 'Custo efetivo', value: (r) => r.eff_cost, html: (r) => `<span class="silver">${fmt(r.eff_cost)}</span>` },
      { key: 'sell', label: 'Venda', value: (r) => r.sell, html: (r) => `<span class="silver">${fmt(r.sell)}</span>` },
      {
        key: 'margin', label: 'Margem', value: (r) => r.margin,
        html: (r) => `<span class="silver ${r.margin >= 0 ? 'profit-pos' : 'profit-neg'}">${fmt(r.margin)}</span>`,
      },
      { key: 'pct', label: 'Margem %', align: 'c', value: (r) => r.margin_pct, html: (r) => r.margin_pct == null ? '—' : `${fmtDec(r.margin_pct, 1)}%` },
      { key: 'pf', label: 'Prata/foco', value: (r) => r.silver_per_focus, html: (r) => r.silver_per_focus == null ? '—' : `<span class="silver">${fmtDec(r.silver_per_focus, 1)}</span>` },
    ];
    renderTable('craftTable', cols, m.map((r) => ({ ...r, _copy: it.pt })), { sortKey: 'margin' });
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

// sub-aba De onde vem (drop sources)
function cleanMob(id) {
  let s = id || '';
  const i = s.indexOf('MOB_');
  if (i >= 0) s = s.slice(i + 4);
  return s.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

async function loadOrigin() {
  const it = state.item;
  if (!it) return;
  const st = $('origemStatus');
  st.className = 'status';
  st.textContent = 'consultando fontes…';
  try {
    const res = await api('/api/origin', { item: it.id });
    const srcs = res.sources || [];
    if (!srcs.length) {
      st.textContent = `${it.pt} não tem fonte de drop conhecida — é craftado/refinado, não dropado por mob.`;
      $('origemTable').innerHTML = '';
      return;
    }
    const cols = [
      { key: 'mob', label: 'Mob / conteúdo', align: 'l', value: (r) => r.fame, html: (r) => esc(cleanMob(r.mob)) },
      { key: 'tier', label: 'Tier', align: 'c', value: (r) => r.tier, html: (r) => `T${r.tier}` },
      { key: 'cat', label: 'Tipo', align: 'l', value: (r) => r.cat || '', html: (r) => esc(r.cat || '—') },
      { key: 'fame', label: 'Fama', value: (r) => r.fame, html: (r) => fmt(r.fame) },
    ];
    renderTable('origemTable', cols, srcs, { sortKey: 'fame' });
    st.textContent = `${srcs.length} fontes de drop (ordenadas por fama do mob)`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

// favoritos e atalhos abrem a aba Item já na sub-aba certa
function selectPrecosItem(it) {
  api('/api/search', { q: it.id, limit: 1 }).then((r) => {
    activateTab('item');
    document.querySelectorAll('#itemSubtabs button').forEach((b) =>
      b.classList.toggle('active', b.dataset.sub === 'precos'));
    document.querySelectorAll('#tab-item .subtab').forEach((s) =>
      s.classList.toggle('active', s.id === 'sub-precos'));
    selectItem(r[0] || it);
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
  const total = state.scanItemsTotal || 0;
  const scanned = state.scanItemsScanned || 0;
  const st = $('scanStatus');
  let txt = `${scanned} itens escaneados · ${state.scanRows.length} oportunidades` +
    (hiddenCount ? ` · ${hiddenCount} ocultas` : '');
  if (total > scanned) {
    txt += ` · ⚠ a categoria tem ${total} itens — só os ${scanned} de maior tier foram escaneados; aumente "Máx. itens" ou refine por subcategoria/tier para cobrir o resto`;
    st.className = 'status warn';
  } else {
    st.className = 'status';
  }
  st.textContent = txt;
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
    state.scanItemsTotal = res.items_total || res.items_scanned;
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
  const it = {
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
  activateTab('item');
  document.querySelectorAll('#itemSubtabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.sub === 'lab'));
  document.querySelectorAll('#tab-item .subtab').forEach((s) =>
    s.classList.toggle('active', s.id === 'sub-lab'));
  selectItem(it);
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

let goldChart = null, goldRangeCount = 168;

function _median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}
function _stdev(xs) {
  if (xs.length < 2) return 0;
  const m = xs.reduce((a, b) => a + b, 0) / xs.length;
  return Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1));
}

async function loadGoldChart(count) {
  goldRangeCount = count || goldRangeCount;
  const st = $('goldChartStatus');
  st.className = 'status';
  st.textContent = 'carregando cotação do ouro…';
  try {
    let g = await api('/api/gold', { count: goldRangeCount });
    g = g.slice().reverse().filter((x) => x.price > 0);   // cronológico
    if (g.length < 2) { st.textContent = 'sem dados de ouro suficientes.'; return; }
    // remove pontos anômalos (z robusto: mediana + MAD) que distorcem a escala
    const prices0 = g.map((x) => x.price);
    const med = _median(prices0);
    const mad = _median(prices0.map((p) => Math.abs(p - med))) || 1;
    const clean = g.filter((x) => Math.abs(x.price - med) <= 6 * 1.4826 * mad);
    const dropped = g.length - clean.length;

    const prices = clean.map((x) => x.price);
    const silver = clean.map((x) => 1e6 / x.price);       // ouro por 1 milhão de prata
    const labels = clean.map((x) => {
      const d = new Date(x.ts + 'Z');
      return goldRangeCount <= 72
        ? `${String(d.getUTCDate()).padStart(2, '0')}/${String(d.getUTCMonth() + 1).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}h`
        : `${String(d.getUTCDate()).padStart(2, '0')}/${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    });

    // análise
    const cur = prices[prices.length - 1], first = prices[0];
    const min = Math.min(...prices), max = Math.max(...prices);
    const mean = prices.reduce((a, b) => a + b, 0) / prices.length;
    const changePct = 100 * (cur / first - 1);
    const rets = [];
    for (let i = 1; i < prices.length; i++) rets.push(Math.log(prices[i] / prices[i - 1]));
    const volDay = _stdev(rets) * Math.sqrt(24) * 100;    // pontos horários -> dia
    // tendência: regressão linear simples do preço sobre o índice
    const n = prices.length, xm = (n - 1) / 2, ym = mean;
    let num = 0, den = 0;
    prices.forEach((p, i) => { num += (i - xm) * (p - ym); den += (i - xm) ** 2; });
    const slope = den ? num / den : 0;
    const trendSpan = slope * (n - 1);                    // variação ajustada ponta a ponta
    const trend = Math.abs(trendSpan) < 0.4 * _stdev(prices)
      ? { t: 'lateral', c: '' }
      : (slope > 0 ? { t: 'em alta', c: 'up' } : { t: 'em baixa', c: 'down' });

    const up = (v) => v >= 0 ? 'up' : 'down';
    const arrow = (v) => v >= 0 ? '▲' : '▼';
    const pill = (label, value, cls) =>
      `<div class="lab-pill"><b>${label}</b><span class="v ${cls || ''}">${value}</span></div>`;
    $('goldStats').innerHTML = [
      pill('Ouro agora', `${fmt(cur)} <small>prata</small>`, ''),
      pill('Variação período', `${arrow(changePct)} ${fmtDec(Math.abs(changePct), 1)}%`, up(changePct)),
      pill('Mínimo', fmt(min), ''),
      pill('Máximo', fmt(max), ''),
      pill('Média', fmt(Math.round(mean)), ''),
      pill('Volatilidade', `${fmtDec(volDay, 1)}% <small>/dia</small>`, ''),
      pill('Tendência', trend.t, trend.c),
      pill('Prata agora', `${fmt(Math.round(1e6 / cur))} <small>ouro/1 mi</small>`, ''),
    ].join('');

    Chart.defaults.color = '#9a8c6d';
    Chart.defaults.borderColor = '#3d3322';
    if (goldChart) goldChart.destroy();
    goldChart = new Chart($('goldChart'), {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Ouro (prata / 1 ouro)', data: prices, yAxisID: 'y',
            borderColor: '#f0c860', backgroundColor: 'rgba(240,200,96,0.10)',
            borderWidth: 2, fill: true, tension: 0.25, pointRadius: 0,
            pointHoverRadius: 4,
          },
          {
            label: 'Prata (ouro / 1 milhão de prata)', data: silver, yAxisID: 'y1',
            borderColor: '#9fb4c8', borderWidth: 1.5, borderDash: [5, 4],
            fill: false, tension: 0.25, pointRadius: 0, pointHoverRadius: 4,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { labels: { boxWidth: 14, usePointStyle: true } },
          tooltip: {
            callbacks: {
              label: (c) => c.datasetIndex === 0
                ? ` Ouro: ${fmt(Math.round(c.parsed.y))} prata`
                : ` Prata: ${fmt(Math.round(c.parsed.y))} ouro / 1 mi`,
            },
          },
        },
        scales: {
          y: {
            position: 'left',
            title: { display: true, text: 'prata por 1 ouro' },
            ticks: { callback: (v) => fmt(Math.round(v)) },
          },
          y1: {
            position: 'right',
            title: { display: true, text: 'ouro por 1 mi de prata' },
            grid: { drawOnChartArea: false },
            ticks: { callback: (v) => fmt(Math.round(v)) },
          },
          x: { ticks: { maxTicksLimit: 12, autoSkip: true } },
        },
      },
    });
    st.textContent = `${clean.length} pontos · ${labels[0]} → ${labels[labels.length - 1]}`
      + (dropped ? ` · ${dropped} ponto(s) anômalo(s) filtrado(s)` : '');
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro ao carregar o ouro: ' + e.message;
  }
}

async function loadSurvival() {
  const st = $('survStatus');
  st.className = 'status';
  st.textContent = 'analisando snapshots…';
  $('survLoad').disabled = true;
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
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  } finally {
    $('survLoad').disabled = false;
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

async function loadIntel() {
  const st = $('intelStatus');
  try {
    const res = await api('/api/intel/top', {
      days: $('intelDays').value,
      inventory: $('intelInventory').checked,
      limit: 15,
    });
    const items = res.items || [];
    if (!items.length) {
      st.textContent = 'sem eventos de kill coletados ainda — o servidor coleta o killboard a cada 10 min';
      $('intelTable').innerHTML = '';
      return;
    }
    const cols = [
      {
        key: 'item', label: 'Item', align: 'l', value: (r) => r.name_pt,
        html: (r) => `<div class="cell-item">${iconImg(r.item_id)}
          <div class="nm"><span class="te-badge">T${r.tier}.${r.ench}</span> ${esc(r.name_pt)}
          <small>${esc(r.item_id)}</small></div></div>`,
      },
      { key: 'un', label: 'Unidades perdidas', value: (r) => r.unidades, html: (r) => `<b>${fmt(r.unidades)}</b>` },
      { key: 'ev', label: 'Mortes', value: (r) => r.eventos, html: (r) => fmt(r.eventos) },
      {
        key: 'preco', label: 'Preço ref.', value: (r) => r.preco_ref,
        html: (r) => r.preco_ref ? `<span class="silver">${fmt(r.preco_ref)}</span>` : '<span class="muted">sem preço</span>',
      },
      {
        key: 'valor', label: 'Valor destruído (est.)', value: (r) => r.valor_estimado,
        html: (r) => r.valor_estimado
          ? `<span class="silver profit-neg" title="pós-trash (~${Math.round((r.trash_rate ?? 0.3) * 100)}% destruído de fato): ${fmt(r.valor_trash_estimado)} — taxa configurável, não validada">${fmt(r.valor_estimado)}</span>` : '—',
      },
    ];
    renderTable('intelTable', cols, items.map((r) => ({ ...r, _copy: r.name_pt })),
      { sortKey: 'un' });
    const j = res.status?.janela_eventos || {};
    const n = res.status?.kill_events ?? 0;
    st.textContent = `${fmt(n)} kills no banco · janela: ${(j.de || '?').slice(0, 16).replace('T', ' ')} → ${(j.ate || '?').slice(0, 16).replace('T', ' ')} UTC · killboard é amostra pública, valor sem ajuste de trash`;
  } catch (e) {
    st.textContent = '';
  }
}

async function loadSignals() {
  const st = $('sigStatus');
  try {
    const res = await api('/api/intel/signals', { limit: 12 });
    const sigs = res.signals || [];
    if (!sigs.length) {
      st.textContent = res.demand_days < 3
        ? `acumulando histórico de destruição (${res.demand_days} dia(s) — precisa de ~3+ para comparar recente × base)`
        : 'nenhuma divergência relevante agora — mercado em dia com a destruição';
      $('sigTable').innerHTML = '';
      return;
    }
    const cols = [
      {
        key: 'item', label: 'Item', align: 'l', value: (r) => r.name_pt,
        html: (r) => `<div class="cell-item">${iconImg(r.item_id)}
          <div class="nm"><span class="te-badge">T${r.tier}.${r.ench}</span> ${esc(r.name_pt)}
          <small>${esc(r.item_id)}</small></div></div>`,
      },
      {
        key: 'dem', label: 'Destruição/dia', value: (r) => r.demanda_dia_recente,
        html: (r) => `<b>${fmtDec(r.demanda_dia_recente, 0)}</b> <small class="muted">antes: ${fmtDec(r.demanda_dia_base, 0)}</small>`,
      },
      {
        key: 'dratio', label: 'Δ demanda', align: 'c', value: (r) => r.demanda_ratio ?? 99,
        html: (r) => r.demanda_ratio
          ? `<span class="profit-pos">×${fmtDec(r.demanda_ratio, 2)}</span>`
          : '<span class="muted" title="sem base de comparação">nova</span>',
      },
      {
        key: 'pratio', label: 'Δ preço 7d', align: 'c', value: (r) => r.preco_ratio,
        html: (r) => `${fmtDec((r.preco_ratio - 1) * 100, 1)}%`,
      },
      { key: 'preco', label: 'Preço', value: (r) => r.preco_recente, html: (r) => `<span class="silver">${fmt(r.preco_recente)}</span>` },
      { key: 'vol', label: 'Vol. mercado/dia', value: (r) => r.volume_dia, html: (r) => fmt(r.volume_dia) },
    ];
    renderTable('sigTable', cols, sigs.map((r) => ({ ...r, _copy: r.name_pt })),
      { sortKey: 'dratio' });
    st.textContent = `${sigs.length} sinais (${res.demand_days} dias de destruição no banco) — verifique volume e frescor antes de agir`;
  } catch (e) {
    st.textContent = '';
  }
}

async function loadRisk() {
  const st = $('riskStatus');
  try {
    const res = await api('/api/intel/risk', { days: 1 });
    const cls = res.classificacao || {};
    if (!cls.total_mortes) {
      st.textContent = 'sem mortes coletadas ainda';
      $('riskTable').innerHTML = '';
      return;
    }
    st.innerHTML = (cls.classes || []).map((c) =>
      `<b>${esc(c.classe)}</b> ${c.pct}% (${fmt(c.mortes)})`).join(' · ') +
      ` — vítimas coletoras: <b>${fmt(cls.vitimas_coletoras)}</b>` +
      ` · com montaria de carga: <b>${fmt(cls.vitimas_montaria_transporte)}</b>` +
      ` · com inventário 10+: <b>${fmt(cls.vitimas_inventario_pesado)}</b>`;
    const zvz = res.zvz_recentes || [];
    if (!zvz.length) { $('riskTable').innerHTML = ''; return; }
    const cols = [
      { key: 'inicio', label: 'Início (UTC)', align: 'l', value: (r) => r.inicio, html: (r) => esc((r.inicio || '').slice(0, 16).replace('T', ' ')) },
      { key: 'zona', label: 'Zona', align: 'l', value: (r) => r.zona, html: (r) => esc(r.zona || '—') },
      { key: 'kills', label: 'Kills', value: (r) => r.kills, html: (r) => fmt(r.kills) },
      { key: 'jog', label: 'Jogadores', value: (r) => r.jogadores, html: (r) => fmt(r.jogadores) },
      { key: 'gld', label: 'Guildas', value: (r) => r.guildas, html: (r) => fmt(r.guildas) },
      { key: 'fama', label: 'Fama destruída', value: (r) => r.fama, html: (r) => `<span class="silver profit-neg">${fmt(r.fama)}</span>` },
    ];
    renderTable('riskTable', cols, zvz, { sortKey: 'fama' });
  } catch (e) {
    st.textContent = '';
  }
}

async function loadServiceOrders(regenerate = false) {
  const st = $('ordersStatus');
  try {
    const res = regenerate
      ? await apiSend('/api/service-orders/refresh', 'POST')
      : await api('/api/service-orders');
    let rows = res.orders || [];
    if (!rows.length) {
      st.textContent = 'sem ordens abertas agora — clique em Atualizar para gerar a partir dos sinais cacheados';
      $('ordersTable').innerHTML = '';
      return;
    }
    // segurança primeiro: avisos de risco no topo, depois maior lucro
    rows = [...rows].sort((a, b) =>
      (b.action_type === 'evitar_risco') - (a.action_type === 'evitar_risco')
      || (b.expected_profit || 0) - (a.expected_profit || 0));
    const cols = [
      {
        key: 'perfil', label: 'Perfil', align: 'l', value: (r) => r.profile_target,
        html: (r) => `<b>${esc(r.profile_target)}</b><br><small class="muted">${esc(r.action_type)}</small>`,
      },
      {
        key: 'item', label: 'Item', align: 'l', value: (r) => r.name_pt,
        html: (r) => r.item_id ? `<div class="cell-item">${iconImg(r.item_id)}
          <div class="nm"><span class="te-badge">T${r.tier}.${r.ench}</span> ${esc(r.name_pt)}
          <small>${esc(r.item_id)}</small></div></div>` : '<span class="muted">geral</span>',
      },
      {
        key: 'rota', label: 'Cidade/rota', align: 'l',
        value: (r) => `${r.city_from || ''} ${r.city_to || ''}`,
        html: (r) => r.city_from || r.city_to
          ? `${r.city_from ? cityHtml(r.city_from) : '<span class="muted">—</span>'} → ${r.city_to ? cityHtml(r.city_to) : '<span class="muted">—</span>'}`
          : '<span class="muted">—</span>',
      },
      { key: 'qtd', label: 'Qtd.', value: (r) => r.quantity_base || 0, html: (r) => r.quantity_base ? fmt(r.quantity_base) : '—' },
      {
        key: 'lucro', label: 'Lucro est.', value: (r) => r.expected_profit || 0,
        html: (r) => r.expected_profit ? `<span class="silver profit-pos">${fmt(r.expected_profit)}</span>` : '—',
      },
      {
        key: 'risco', label: 'Risco/conf.', align: 'c', value: (r) => r.risk_level,
        html: (r) => `<span class="${r.risk_level === 'alto' ? 'profit-neg' : ''}">${esc(r.risk_level || '—')}</span><br><small class="muted">${esc(r.confidence || '—')}</small>`,
      },
      { key: 'motivo', label: 'Motivo', align: 'l', value: (r) => r.explanation_short, html: (r) => esc(r.explanation_short || '') },
    ];
    renderTable('ordersTable', cols, rows.map((r) => ({ ...r, _copy: r.explanation_short || r.name_pt || '' })),
      {});
    st.textContent = `${rows.length} ordens abertas, geradas de dados cacheados e sinais públicos`;
    if (res.generated !== undefined) st.textContent += ` · ${res.generated} recalculadas`;
  } catch (e) {
    st.textContent = 'erro ao carregar ordens de serviço: ' + e.message;
  }
}

async function loadSignalValidation() {
  try {
    const res = await api('/api/intel/validate');
    const h1 = (res.horizons || []).find((h) => h.horizon_days === 1);
    if (h1 && h1.n > 0) {
      $('sigStatus').textContent +=
        ` · backtest dos alertas (D+1): ${h1.hit_rate_pct}% de acerto, retorno mediano ${h1.retorno_mediano_pct}% (N=${h1.n})`;
    }
  } catch (e) { /* opcional */ }
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
  makeItemPicker('itemPicker', (it) => selectItem(it));
  document.querySelectorAll('#itemSubtabs button').forEach((b) =>
    b.addEventListener('click', () => showItemSub(b.dataset.sub)));
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

  // botões
  $('glossaryOpen').addEventListener('click', () => setGlossary(true));
  $('glossaryClose').addEventListener('click', () => setGlossary(false));
  $('glossaryCloseBackdrop').addEventListener('click', () => setGlossary(false));
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') setGlossary(false);
  });
  $('dashRecsRefresh').addEventListener('click', loadDashboardRecommendations);
  $('dashFused').addEventListener('change', loadDashboardRecommendations);
  $('dashCollect').addEventListener('click', runCollect);
  document.querySelectorAll('#goldRange .chip').forEach((b) =>
    b.addEventListener('click', () => {
      document.querySelectorAll('#goldRange .chip').forEach((x) =>
        x.classList.toggle('on', x === b));
      loadGoldChart(+b.dataset.range);
    }));
  $('ordersRefresh').addEventListener('click', () => loadServiceOrders(true));
  $('survLoad').addEventListener('click', loadSurvival);
  $('histWatch').addEventListener('click', watchCurrentLabItem);
  $('craftSellMode').addEventListener('change', () => loadItemSub('craft', true));
  $('craftFocus').addEventListener('change', () => loadItemSub('craft', true));
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
  $('intelDays').addEventListener('change', loadIntel);
  $('intelInventory').addEventListener('change', loadIntel);
  setInterval(loadIntel, 5 * 60 * 1000);

  refreshWatchCount();
  loadGoldChart();
  loadDashboardRecommendations();
  loadHeatmap();
  loadIntel();
  loadSignals().then(loadSignalValidation);
  loadRisk();
  loadServiceOrders();
  setInterval(() => { loadSignals().then(loadSignalValidation); loadRisk(); },
    10 * 60 * 1000);
  setInterval(loadServiceOrders, 10 * 60 * 1000);
}

init().catch((e) => {
  document.body.insertAdjacentHTML('afterbegin',
    `<div style="background:#d96a4f;color:#fff;padding:10px 16px">Erro ao iniciar: ${esc(e.message)}</div>`);
});
