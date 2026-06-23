'use strict';

// ============================================================ estado
const state = {
  auth: null,
  csrf: null,
  roles: {},
  profilesCatalog: {},
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
// normaliza o corpo de erro do FastAPI: detail pode ser string OU array de
// validação (422) — sem isso a UI mostrava "erro: [object Object]"
async function apiError(res) {
  let msg = res.statusText;
  let code = 'http_error';
  try {
    const body = await res.json();
    const d = body.detail;
    code = body.code || code;
    if (Array.isArray(d)) {
      msg = d.map((x) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ');
    } else if (d) {
      msg = d;
    }
  } catch (e) { /* corpo não-JSON */ }
  const error = new Error(msg);
  error.status = res.status;
  error.code = code;
  return error;
}

async function api(path, params = {}) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== '') usp.set(k, v);
  }
  const res = await fetch(path + (usp.toString() ? '?' + usp : ''), {
    credentials: 'same-origin',
  });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

async function apiSend(path, method = 'POST', params = {}) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== '') usp.set(k, v);
  }
  const headers = state.csrf ? {'X-CSRF-Token': state.csrf} : {};
  const res = await fetch(path + (usp.toString() ? '?' + usp : ''), {
    method, headers, credentials: 'same-origin',
  });
  if (!res.ok) throw await apiError(res);
  return res.json();
}

async function apiJson(path, method = 'POST', body = null, withCsrf = true) {
  const headers = {'Content-Type': 'application/json'};
  if (withCsrf && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const res = await fetch(path, {
    method, headers, credentials: 'same-origin',
    body: body === null ? null : JSON.stringify(body),
  });
  if (!res.ok) throw await apiError(res);
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
  `data-icon-id="${esc(id)}" data-icon-q="${q || 0}" alt="">`;

// Delegado para ser compatível com CSP sem liberar JavaScript inline.
document.addEventListener('error', (ev) => {
  const img = ev.target;
  if (img instanceof HTMLImageElement && img.dataset.iconId) {
    window.__iconFb(img, img.dataset.iconId, Number(img.dataset.iconQ || 0));
  }
}, true);

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

// ============================================================ contas e segurança
const OPERATOR_ROLES = new Set(['admin', 'guild_leader', 'treasurer', 'economic_officer']);

function setAccountModal(open, forced = false) {
  state.forcePassword = forced || (state.forcePassword && open);
  const modal = $('accountModal');
  modal.classList.toggle('open', open);
  modal.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (open) $('currentPassword').focus();
}

function accountLabel(account) {
  return account.display_name || account.albion_nick || account.username;
}

function applyAuth(payload) {
  state.auth = payload.account;
  state.csrf = payload.csrf;
  state.roles = payload.roles || {};
  state.profilesCatalog = payload.profiles_catalog || {};
  $('newRole').innerHTML = Object.entries(state.roles).map(([id, label]) =>
    `<option value="${esc(id)}">${esc(label)}</option>`).join('');
  $('newRole').value = 'member';
  $('newProfiles').innerHTML = profileChecks();
  $('accountOpen').hidden = false;
  $('accountOpen').textContent = accountLabel(state.auth);
  $('adminTabButton').hidden = state.auth.role !== 'admin';
  $('accountSummary').innerHTML = `<div class="account-summary">
    <div><span>Usuário</span>${esc(state.auth.username)}</div>
    <div><span>Papel</span>${esc(state.roles[state.auth.role] || state.auth.role)}</div>
    <div><span>Nick Albion</span>${esc(state.auth.albion_nick || 'não informado')}</div>
    <div><span>IPs (máx. 2)</span>${esc((state.auth.ips || []).map((x) => x.ip).join(', ') || 'nenhum ainda')}</div>
  </div>`;
}

function showLogin(message = '') {
  document.body.classList.add('auth-pending');
  document.body.classList.remove('authenticated');
  $('loginError').textContent = message;
  $('loginPassword').value = '';
  setTimeout(() => $('loginUsername').focus(), 0);
}

function profileChecks(selected = [], prefix = 'profile') {
  const have = new Set(selected);
  return Object.entries(state.profilesCatalog).map(([id, label]) =>
    `<label><input type="checkbox" name="${esc(prefix)}" value="${esc(id)}" ` +
    `${have.has(id) ? 'checked' : ''}> ${esc(label)}</label>`).join('');
}

function showTemporarySecret(username, password) {
  $('temporarySecretUser').textContent = `Conta: ${username}`;
  $('temporarySecretValue').textContent = password;
  $('temporarySecret').hidden = false;
}

function formatAccountDate(value) {
  return value ? new Date(value * 1000).toLocaleString('pt-BR') : 'nunca';
}

async function loadAccounts() {
  if (state.auth?.role !== 'admin') return;
  $('accountsStatus').textContent = 'carregando contas…';
  try {
    const data = await api('/api/admin/accounts');
    $('accountsStatus').textContent = `${data.accounts.length} conta(s)`;
    $('accountsTable').innerHTML = `<table><thead><tr>
      <th>Conta</th><th>Papel</th><th>Perfis</th><th>Dispositivo / último login</th><th>Ações</th>
    </tr></thead><tbody>${data.accounts.map((a) => `<tr data-account-id="${a.id}">
      <td><b>${esc(accountLabel(a))}</b><small>${esc(a.username)}${a.albion_nick ? ' · ' + esc(a.albion_nick) : ''}</small>
        ${a.active ? '' : '<span class="account-status-off">desativada</span>'}
        ${a.must_change_password ? '<small>senha temporária</small>' : ''}</td>
      <td><select class="account-role-select" data-role>${Object.entries(state.roles).map(([id, label]) =>
        `<option value="${esc(id)}" ${id === a.role ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select></td>
      <td><details class="account-profiles-edit"><summary>${a.profiles.length ? esc(a.profiles.map((p) => state.profilesCatalog[p] || p).join(', ')) : 'nenhum'}</summary>
        <div class="account-profile-grid">${profileChecks(a.profiles, `profiles-${a.id}`)}</div>
        <button type="button" class="mini-btn" data-account-action="save-profiles">Salvar perfis</button></details></td>
      <td>${esc((a.ips || []).map((x) => x.ip).join(', ') || '—')}<small>login: ${esc(formatAccountDate(a.last_login_at))}</small></td>
      <td><div class="account-actions">
        <button type="button" class="mini-btn" data-account-action="reset-password">Nova senha</button>
        <button type="button" class="mini-btn" data-account-action="reset-device">Liberar IPs</button>
        <button type="button" class="mini-btn" data-account-action="toggle-active">${a.active ? 'Desativar' : 'Reativar'}</button>
      </div></td>
    </tr>`).join('')}</tbody></table>`;
    const audit = await api('/api/admin/audit', {limit: 100});
    const actionLabels = {
      bootstrap_admin: 'Administrador inicial', account_created: 'Conta criada',
      account_updated: 'Conta alterada', password_reset: 'Senha redefinida',
      password_changed: 'Senha alterada', ip_reset: 'IPs liberados',
      ip_limit: 'IP recusado (limite)', login_success: 'Login',
      login_failed: 'Login recusado', logout: 'Logout',
    };
    $('accountAuditTable').innerHTML = `<table><thead><tr><th>Quando</th><th>Evento</th><th>Autor</th><th>Conta</th></tr></thead><tbody>${audit.events.map((e) =>
      `<tr><td>${esc(formatAccountDate(e.created_at))}</td><td>${esc(actionLabels[e.action] || e.action)}</td><td>${esc(e.actor || 'sistema')}</td><td>${esc(e.target || '—')}</td></tr>`
    ).join('')}</tbody></table>`;
  } catch (e) {
    $('accountsStatus').textContent = `erro: ${e.message}`;
  }
}

function setupAuthUi() {
  $('loginForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    $('loginError').textContent = 'verificando…';
    try {
      await apiJson('/api/auth/login', 'POST', {
        username: $('loginUsername').value,
        password: $('loginPassword').value,
      }, false);
      location.reload();
    } catch (e) {
      $('loginError').textContent = e.message;
    }
  });
  $('accountOpen').addEventListener('click', () => setAccountModal(true));
  document.querySelectorAll('[data-account-close]').forEach((el) =>
    el.addEventListener('click', () => {
      if (!state.forcePassword) setAccountModal(false);
    }));
  $('logoutButton').addEventListener('click', async () => {
    try { await apiJson('/api/auth/logout', 'POST'); } finally { location.reload(); }
  });
  $('passwordForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    $('passwordError').textContent = '';
    if ($('newPassword').value !== $('newPasswordConfirm').value) {
      $('passwordError').textContent = 'As novas senhas não coincidem.';
      return;
    }
    try {
      await apiJson('/api/auth/change-password', 'POST', {
        current_password: $('currentPassword').value,
        new_password: $('newPassword').value,
      });
      alert('Senha alterada. Entre novamente com a nova senha.');
      location.reload();
    } catch (e) { $('passwordError').textContent = e.message; }
  });
  $('adminTabButton').addEventListener('click', loadAccounts);
  $('adminReload').addEventListener('click', loadAccounts);
  $('temporarySecretCopy').addEventListener('click', () =>
    copyText($('temporarySecretValue').textContent));
  $('temporarySecretClose').addEventListener('click', () => {
    $('temporarySecretValue').textContent = '';
    $('temporarySecret').hidden = true;
  });
  $('accountCreateForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const result = await apiJson('/api/admin/accounts', 'POST', {
        username: $('newUsername').value,
        display_name: $('newDisplayName').value || null,
        albion_nick: $('newAlbionNick').value || null,
        role: $('newRole').value,
        profiles: [...$('newProfiles').querySelectorAll('input:checked')].map((x) => x.value),
      });
      showTemporarySecret(result.account.username, result.temporary_password);
      ev.target.reset();
      await loadAccounts();
    } catch (e) { $('accountsStatus').textContent = `erro: ${e.message}`; }
  });
  $('accountsTable').addEventListener('change', async (ev) => {
    if (!ev.target.matches('[data-role]')) return;
    const row = ev.target.closest('[data-account-id]');
    try {
      await apiJson(`/api/admin/accounts/${row.dataset.accountId}`, 'PATCH', {role: ev.target.value});
      await loadAccounts();
    } catch (e) { toast(e.message); await loadAccounts(); }
  });
  $('accountsTable').addEventListener('click', async (ev) => {
    const button = ev.target.closest('[data-account-action]');
    if (!button) return;
    const row = button.closest('[data-account-id]');
    const id = row.dataset.accountId;
    const action = button.dataset.accountAction;
    try {
      if (action === 'reset-password') {
        const out = await apiJson(`/api/admin/accounts/${id}/reset-password`, 'POST');
        showTemporarySecret(row.querySelector('small').textContent.split(' · ')[0], out.temporary_password);
      } else if (action === 'reset-device') {
        await apiJson(`/api/admin/accounts/${id}/reset-device`, 'POST');
        toast('dispositivo liberado e sessões encerradas');
      } else if (action === 'toggle-active') {
        const inactive = row.querySelector('.account-status-off');
        await apiJson(`/api/admin/accounts/${id}`, 'PATCH', {active: Boolean(inactive)});
      } else if (action === 'save-profiles') {
        const profiles = [...row.querySelectorAll('.account-profiles-edit input:checked')].map((x) => x.value);
        await apiJson(`/api/admin/accounts/${id}`, 'PATCH', {profiles});
        toast('perfis atualizados');
      }
      await loadAccounts();
    } catch (e) { toast(e.message); }
  });
}

async function bootAuth() {
  setupAuthUi();
  try {
    const status = await api('/api/auth/bootstrap-status');
    if (!status.ready) {
      $('bootstrapHelp').hidden = false;
      $('bootstrapHelp').innerHTML = `Primeiro acesso ainda não configurado. Execute no PowerShell:<code>${esc(status.command)}</code>`;
      showLogin('O administrador inicial precisa ser criado localmente.');
      return;
    }
    try {
      const session = await api('/api/auth/me');
      applyAuth(session);
      document.body.classList.add('authenticated');
      if (state.auth.must_change_password) {
        setAccountModal(true, true);
        $('passwordError').textContent = 'Troque a senha temporária para liberar a plataforma.';
        return;
      }
      document.body.classList.remove('auth-pending');
      init().catch((e) => showLogin(`Erro ao iniciar: ${e.message}`));
    } catch (e) { showLogin(e.status === 401 ? '' : e.message); }
  } catch (e) { showLogin(`Servidor indisponível: ${e.message}`); }
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

function makeChips(containerId, options, { selected = [], multi = true, onChange = null, allToggle = false } = {}) {
  const box = $(containerId);
  const on = new Set(selected.map(String));
  box.innerHTML = '';
  const sync = () => {
    box.querySelectorAll('.chip-opt').forEach((c, i) =>
      c.classList.toggle('on', on.has(String(options[i].value))));
    const allChip = box.querySelector('.chip-all');
    if (allChip) allChip.classList.toggle('on', on.size >= options.length);
  };
  if (allToggle && multi) {
    const all = document.createElement('span');
    all.className = 'chip chip-all';
    all.textContent = 'todas';
    all.title = 'marcar todas / limpar';
    all.addEventListener('click', () => {
      if (on.size >= options.length) on.clear();
      else options.forEach((o) => on.add(String(o.value)));
      sync();
      if (onChange) onChange([...on]);
    });
    box.appendChild(all);
  }
  for (const opt of options) {
    const b = document.createElement('span');
    b.className = 'chip chip-opt' + (on.has(String(opt.value)) ? ' on' : '');
    b.innerHTML = opt.html || esc(opt.label);
    b.title = opt.title || '';
    b.addEventListener('click', () => {
      const v = String(opt.value);
      if (multi) {
        if (on.has(v)) on.delete(v); else on.add(v);
      } else {
        on.clear(); on.add(v);
      }
      sync();
      if (onChange) onChange([...on]);
    });
    box.appendChild(b);
  }
  return {
    get: () => [...on],
    set: (vals) => {
      on.clear();
      vals.map(String).forEach((v) => on.add(v));
      sync();
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
      columns.map((c) => `<th class="${c.align || ''} ${c.key === sk ? 'sorted' : ''}" data-k="${c.key}"${c.title ? ` title="${esc(c.title)}"` : ''}>${
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
        if (ev.target.closest('.wiki-link')) return;   // navegação, não copia
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
      html: (o) => o.roi_pct == null ? '—' : `${o.roi_pct.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`,
    },
    {
      key: 'conf', label: 'Conf.', align: 'c',
      value: (o) => o.confidence_score ?? 0,
      html: (o) => {
        const label = o.confidence_label || 'baixa';
        const cls = label.replace(/\s+/g, '-');
        const labelPt = label === 'media' ? 'média' : label;   // acento consistente
        return `<span class="conf conf-${esc(cls)}" title="${esc(o.confidence_notes || '')}">${esc(labelPt)}</span>`;
      },
    },
    {
      key: 'flipscore', label: 'Prioridade', align: 'c',
      value: (o) => o.flip_score ?? o.profit ?? 0,
      html: (o) => o.flip_score == null ? '—' :
        `<span class="silver profit-pos" title="lucro PONDERADO pela confiança (idade do dado): um lucro alto em dado velho cai aqui. É por esta coluna que a lista é ordenada.">${fmt(o.flip_score)}</span>`,
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
      html: (o) => o.roi_pct == null ? '—' : `${o.roi_pct.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}%`,
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
        const labelPt = label === 'media' ? 'média' : label;   // acento consistente
        return `<span class="conf conf-${esc(cls)}" title="${esc(o.confidence_notes || '')}">${esc(labelPt)}</span>`;
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
    if (b.dataset.tab === 'avancado' && !state.avancadoLoaded) {
      state.avancadoLoaded = true;
      state.avSubLoaded = { micro: true };
      loadAvancado();
    }
    if (b.dataset.tab === 'ilha' && !state.islandLoaded) {
      state.islandLoaded = true;
      showIslandSub('laborers');
    }
    if (b.dataset.tab === 'linhaProducao' && !state.prodLineLoaded) {
      state.prodLineLoaded = true;
      initProdLine();
    }
    // o gráfico do ouro precisa do canvas VISÍVEL para dimensionar — carrega
    // ao abrir a aba (não no init, quando a aba está oculta)
    if (b.dataset.tab === 'moedas' && !state.moedasLoaded) {
      state.moedasLoaded = true;
      loadGoldChart();
    }
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
  // o imposto entra no LÍQUIDO de quase tudo: invalida caches e recarrega o que
  // está à vista (Item, Avançado, recomendações e mapa de rotas do Início)
  state.itemLoadedFor = {};
  state.avSubLoaded = {};
  if (state.item && document.getElementById('tab-item').classList.contains('active')) {
    loadItemSub(activeItemSub(), true);
  }
  if (document.getElementById('tab-avancado').classList.contains('active')) {
    const a = document.querySelector('#avSubtabs button.active');
    if (a) showAvSub(a.dataset.av);
  }
  loadDashboardRecommendations();
  if (state.prodLine && document.getElementById('tab-linhaProducao').classList.contains('active')) {
    prodRecompute();
  }
  toast(`imposto de venda: ${state.premium ? '4%' : '8%'} — análises recalculadas`);
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
  // Estado vazio claro e ACIONÁVEL (em vez de tabela em branco). Distingue
  // "sem histórico coletado" (causa mais comum) de "filtros apertados".
  if (!rows.length) {
    const cov = meta?.coverage || {};
    const noHist = (cov.history_items || 0) === 0;
    const body = noHist
      ? `<b>Coletando dados de mercado…</b><br>A plataforma atualiza preços e
         <b>histórico</b> automaticamente ao abrir (a 1ª coleta leva ~1–2 min pelo
         limite da API do jogo). <b>Esta tela se atualiza sozinha</b> — ou clique
         em <b>Atualizar</b>. Se demorar, rode <code>python analyze.py collect --cat bags</code>.
         <br><br>Enquanto isso, a aba <b>Flips</b> já funciona — usa só os preços atuais.`
      : `<b>${esc(emptyText)}.</b><br>Tente afrouxar os filtros (volume/dia, idade dos dados,
         ROI) ou ampliar a categoria/tier.`;
    $(tableId).innerHTML = `<div class="empty-state"><div class="ico">📭</div><div>${body}</div></div>`;
    const cov2 = cov.price_items != null
      ? ` · cobertura: ${fmt(cov.price_items)}/${fmt(cov.catalog_items)} com preço, ${fmt(cov.history_items)} com histórico`
      : '';
    const st0 = $(statusId);
    st0.className = 'status';
    st0.textContent = `0 recomendações${cov2}`;
    return;
  }
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
    // enquanto vazio por falta de histórico (coleta automática rodando), tenta
    // de novo sozinho — a tela "se atualiza sozinha" como promete a mensagem.
    clearTimeout(state.dashRetryTimer);
    const noHist = (res.coverage?.history_items || 0) === 0;
    if (!state.dashRecs.length && noHist && (state.dashRetries || 0) < 6) {
      state.dashRetries = (state.dashRetries || 0) + 1;
      state.dashRetryTimer = setTimeout(() => loadDashboardRecommendations(), 45000);
    } else if (state.dashRecs.length) {
      state.dashRetries = 0;
    }
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
    fused: $('dashFused') ? $('dashFused').checked : false,
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
  else if (sub === 'cadeia') loadWiki();
  else if (sub === 'origem') loadOrigin();
  else if (sub === 'risco') loadRisco();
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
  ['precosTable', 'venderTable', 'craftTable', 'origemTable', 'riscoBody',
   'histLabSummary', 'histLabCards', 'wikiDetails', 'wikiRecipe', 'wikiUsedIn',
   'wikiTree', 'wikiTreeMeta', 'wikiShopping'].forEach((id) => {
    const el = $(id); if (el) el.innerHTML = '';
  });
  if (priceChart) { priceChart.destroy(); priceChart = null; }
  if (volChart) { volChart.destroy(); volChart = null; }
  $('itemCard').innerHTML = itemCardHtml(it, true);
  wireItemCard('itemCard', it);
  loadItemSub(activeItemSub(), true);
}

// sub-aba Estúdio de Craft
async function loadCraft() {
  const it = state.item;
  if (!it) return;
  const st = $('craftStatus');
  st.className = 'status';
  st.textContent = 'calculando estúdio de craft…';
  const focus = $('craftFocus').checked;
  const sourcing = $('craftSourcing').checked;
  try {
    const res = await api('/api/craft', {
      item: it.id, premium: state.premium,
      sell_mode: $('craftSellMode').value, focus,
      spec_fce: +($('craftSpec').value || 0),
      daily_bonus: +($('craftDaily').value || 0),
      focus_budget: $('craftFocusBudget').value || '',
      station_fee: +($('craftFee').value || 0),
      same_city: !sourcing,
    });
    const rows = res.rows || [];
    if (!rows.length) {
      st.textContent = res.category
        ? 'sem preços de insumo/produto no cache/API para calcular — tente de novo em instantes'
        : 'este item não tem receita de craft/refino no dump';
      $('craftPlan').innerHTML = '';
      $('craftTable').innerHTML = '';
      return;
    }
    const ins = (res.inputs || []).map((i) => `${i.count}× ${esc(i.name_pt)}`).join(' + ');
    st.innerHTML = `Receita: ${ins} · foco base ${fmt(res.focus)} · categoria <b>${esc(res.category || '—')}</b>` +
      (res.bonus_city ? ` · cidade-bônus <b>${esc(res.bonus_city)}</b>` : ' · sem cidade-bônus');

    // plano recomendado = melhor linha (maior margem)
    const best = rows[0];
    const nameOf = (id) => (res.inputs.find((i) => i.id === id) || {}).name_pt || id;
    const buyLine = (best.sourcing || []).map((s) =>
      `${s.count}× ${esc(s.name_pt || nameOf(s.id))} em <b>${esc(s.buy_city)}</b> <span class="muted">(${fmt(s.unit_price)})</span>`).join(' · ');
    const pill = (label, val) => `<div class="lab-pill"><b>${label}</b><span>${val}</span></div>`;
    let plan = `<div class="craft-plan-head">Plano ótimo${sourcing ? '' : ' (tudo numa cidade)'}: craftar em <b>${esc(best.craft_city)}</b>${best.is_bonus_city ? ' ★' : ''}, vender em <b>${esc(best.sell_city)}</b></div>`;
    plan += `<div class="craft-plan-buy">Comprar: ${buyLine || '—'}</div>`;
    plan += '<div class="lab-summary" style="margin-top:8px">' +
      pill('RRR', `${fmtDec(best.rrr_pct, 1)}%`) +
      pill('Insumos', fmt(best.materials)) +
      pill('Custo efetivo', fmt(best.eff_cost)) +
      pill(`Vende ×${best.output}`, fmt(best.revenue)) +
      pill('Margem/craft', `<span class="${best.margin >= 0 ? 'profit-pos' : 'profit-neg'}">${fmt(best.margin)}</span>`) +
      (best.margin_pct != null ? pill('Margem %', `${fmtDec(best.margin_pct, 1)}%`) : '') +
      (focus && best.silver_per_focus != null ? pill('Prata/foco', fmtDec(best.silver_per_focus, 1)) : '') +
      (best.focus_cost_eff != null ? pill('Foco/craft', fmtDec(best.focus_cost_eff, 1)) : '') +
      (best.crafts_per_day != null ? pill('Crafts/dia', fmt(best.crafts_per_day)) : '') +
      (best.items_per_day != null ? pill('Itens/dia', fmt(best.items_per_day)) : '') +
      (best.resource_saved_per_day != null ? pill('Recurso poupado/dia', fmt(best.resource_saved_per_day)) : '') +
      '</div>';
    $('craftPlan').innerHTML = plan;

    const cols = [
      {
        key: 'city', label: 'Craftar em', align: 'l', value: (r) => r.craft_city,
        html: (r) => cityHtml(r.craft_city) + (r.is_bonus_city ? ' <span class="te-badge">★</span>' : ''),
      },
      { key: 'rrr', label: 'RRR %', align: 'c', value: (r) => r.rrr_pct, html: (r) => `${fmtDec(r.rrr_pct, 1)}%` },
      { key: 'mat', label: 'Insumos', value: (r) => r.materials, html: (r) => `<span class="silver">${fmt(r.materials)}</span>` },
      { key: 'eff', label: 'Custo efetivo', value: (r) => r.eff_cost, html: (r) => `<span class="silver">${fmt(r.eff_cost)}</span>` },
      {
        key: 'sellc', label: 'Vender em', align: 'l', value: (r) => r.sell_city,
        html: (r) => cityHtml(r.sell_city),
      },
      { key: 'rev', label: `Receita ×${best.output}`, value: (r) => r.revenue, html: (r) => `<span class="silver">${fmt(r.revenue)}</span>` },
      {
        key: 'margin', label: 'Margem', value: (r) => r.margin,
        html: (r) => `<span class="silver ${r.margin >= 0 ? 'profit-pos' : 'profit-neg'}">${fmt(r.margin)}</span>`,
      },
      { key: 'pct', label: 'Margem %', align: 'c', value: (r) => r.margin_pct, html: (r) => r.margin_pct == null ? '—' : `${fmtDec(r.margin_pct, 1)}%` },
      { key: 'pf', label: 'Prata/foco', value: (r) => r.silver_per_focus, html: (r) => r.silver_per_focus == null ? '—' : `<span class="silver">${fmtDec(r.silver_per_focus, 1)}</span>` },
    ];
    renderTable('craftTable', cols, rows.map((r) => ({ ...r, _copy: it.pt })), { sortKey: 'margin' });
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

// sub-aba Cadeia / Wiki ----------------------------------------------------
function wikiLink(id, name) {
  return `<a class="wiki-link" data-id="${esc(id)}">${esc(name)}</a>`;
}

function treeNodeHtml(node, root = false) {
  const price = node.best_unit != null
    ? `<span class="silver">${fmt(node.best_unit)}</span>`
    : '<span class="muted">s/ preço</span>';
  const dec = root ? '' : ` <span class="chain-${node.decision === 'fazer' ? 'make' : 'buy'}">${node.decision}</span>`;
  const cnt = node.count != null ? ` <span class="chain-cnt">×${fmt(node.count)}</span>` : '';
  const rrr = (node.decision === 'fazer' && node.rrr_pct != null)
    ? ` <span class="muted">RRR ${fmtDec(node.rrr_pct, 0)}%${node.craft_city ? ' @ ' + esc(node.craft_city) : ''}</span>` : '';
  const head = `${iconImg(node.id, 0)} ${wikiLink(node.id, node.name_pt)}${cnt}${dec} ${price}${rrr}`;
  if (node.children && node.children.length) {
    return `<details ${root ? 'open' : ''} class="chain-node" data-id="${esc(node.id)}"><summary>${head}</summary><ul>${
      node.children.map((c) => `<li>${treeNodeHtml(c)}</li>`).join('')}</ul></details>`;
  }
  return `<div class="chain-leaf">${head}${node.is_raw ? ' <span class="te-badge">bruto</span>' : ''}</div>`;
}

async function navigateToItem(id) {
  // alias de refinado encantado X@n -> id real X_LEVELn@n (navegável/buscável)
  const m = id.match(/^(.+)@([1-9]\d*)$/);
  const cand = (m && !/_LEVEL\d/.test(m[1])) ? [id, `${m[1]}_LEVEL${m[2]}@${m[2]}`] : [id];
  try {
    for (const q of cand) {
      const r = await api('/api/search', { q });
      const list = Array.isArray(r) ? r : (r.results || r.items || []);
      const it = list.find((x) => x.id === q);   // só match EXATO (nada de list[0])
      if (it) { selectItem(it); showItemSub('cadeia'); return; }
    }
    toast('item não encontrado: ' + id);
  } catch (e) { /* ignora */ }
}

async function loadWiki() {
  const it = state.item;
  if (!it) return;
  const st = $('wikiStatus');
  st.className = 'status';
  st.textContent = 'montando a cadeia de produção…';
  $('wikiTreeMeta').textContent = '';
  // preserva quais nós da árvore o usuário tinha expandido (ajuste de foco/qtd)
  const wasOpen = new Set([...document.querySelectorAll('#wikiTree details[open]')]
    .map((dt) => dt.dataset.id));
  try {
    const res = await api('/api/wiki', {
      item: it.id, focus: $('wikiFocus').checked, qty: $('wikiQty').value || '',
    });
    if (!state.item || state.item.id !== it.id) return;   // resposta obsoleta
    const d = res.details || {}, s = d.stats || {};
    const pill = (l, v) => (v == null || v === '') ? '' : `<div class="lab-pill"><b>${l}</b><span>${v}</span></div>`;
    $('wikiDetails').innerHTML = [
      pill('Tier', d.tier != null ? 'T' + d.tier + (d.ench ? '.' + d.ench : '') : null),
      pill('Categoria', d.category ? esc(d.category) + (d.subcategory ? ' / ' + esc(d.subcategory) : '') : null),
      pill('Peso', d.weight != null ? fmtDec(d.weight, 2) + ' kg' : null),
      pill('Item Power', s.item_power), pill('Dano', s.attack_damage),
      pill('Vel. ataque', s.attack_speed), pill('Alcance', s.attack_range),
      pill('Durabilidade', s.durability != null ? fmt(s.durability) : null),
      pill('Armadura', s.armor), pill('Res. mágica', s.magic_resistance),
      pill('Slot', s.slot ? esc(s.slot) : null),
      pill('Duas mãos', s.two_handed === 'true' ? 'sim' : (s.two_handed === 'false' ? 'não' : null)),
      pill('Slots ativos', s.active_slots), pill('Slots passivos', s.passive_slots),
    ].filter(Boolean).join('') || '<div class="muted">sem detalhes no dump</div>';

    if (res.recipe) {
      const r = res.recipe;
      $('wikiRecipe').innerHTML = `<div class="wiki-chips">${
        r.inputs.map((i) => `<span class="wiki-ing">${iconImg(i.id, 0)} ${wikiLink(i.id, i.name_pt)} <b>×${fmt(i.count)}</b>${i.buy != null ? ` <span class="muted">(${fmt(i.buy)})</span>` : ''}</span>`).join('')
      }</div><div class="muted" style="margin-top:6px">Produz <b>${fmt(r.output)}</b> · foco ${fmt(r.focus)} · categoria ${esc(r.category || '—')}${r.bonus_city ? ` · cidade-bônus <b>${esc(r.bonus_city)}</b>` : ''}</div>`;
    } else {
      $('wikiRecipe').innerHTML = '<div class="muted">não é craftável (recurso bruto ou sem receita no dump)</div>';
    }

    const u = res.used_in || [];
    const extra = (res.used_in_total || u.length) - u.length;
    $('wikiUsedIn').innerHTML = u.length ? `<div class="wiki-chips">${
      u.map((x) => `<span class="wiki-ing">${iconImg(x.item_id, 0)} ${wikiLink(x.item_id, x.name_pt)}${x.count ? ` <span class="muted">×${fmt(x.count)}</span>` : ''}</span>`).join('')
    }</div>${extra > 0 ? `<div class="muted" style="margin-top:6px">+${fmt(extra)} outros consumidores não exibidos</div>` : ''}` : '<div class="muted">não é insumo de nenhuma receita</div>';

    // estado de expansão preservado: marca o nó raiz aberto + os que estavam abertos
    $('wikiTree').innerHTML = treeNodeHtml(res.tree, true);
    document.querySelectorAll('#wikiTree details').forEach((dt) => {
      if (wasOpen.size && wasOpen.has(dt.dataset.id)) dt.setAttribute('open', '');
    });
    const t = res.totals || {};
    // por unidade, fazer (make_unit, com RRR) vs comprar pronto (buy_unit) —
    // mesma base; nomeia o vencedor. A matéria-prima bruta (lote, sem RRR) fica
    // separada no status, NÃO encadeada aqui (bases diferentes).
    const mk = t.make_unit != null ? fmt(t.make_unit) : '—';
    const by = t.buy_unit != null ? fmt(t.buy_unit) : '—';
    const cheaper = (t.make_unit != null && t.buy_unit != null)
      ? (t.make_unit <= t.buy_unit ? 'fazer' : 'comprar') : null;
    const perUnit = `por unid.: fazer ${mk} · comprar pronto ${by}${cheaper ? ` · melhor: <b>${cheaper}</b>` : ''}`;
    const incompleto = (t.unpriced && t.unpriced.length) ? ` · ⚠ ${fmt(t.unpriced.length)} sem cotação` : '';
    $('wikiTreeMeta').innerHTML = `· ${perUnit}${incompleto}`;

    renderTable('wikiShopping', [
      { key: 'item', label: 'Recurso bruto', align: 'l', value: (o) => o.name_pt, html: (o) => `${iconImg(o.id, 0)} ${wikiLink(o.id, o.name_pt)}` },
      { key: 't', label: 'Tier', align: 'c', value: (o) => o.tier, html: (o) => o.tier != null ? 'T' + o.tier : '—' },
      { key: 'u', label: 'Qtd', value: (o) => o.units, html: (o) => fmt(o.units) },
      { key: 'p', label: 'Preço un', value: (o) => o.unit_price, html: (o) => o.unit_price != null ? `<span class="silver">${fmt(o.unit_price)}</span>` : '<span class="profit-neg">sem cotação</span>' },
      { key: 'sub', label: 'Subtotal', value: (o) => o.subtotal, html: (o) => o.subtotal != null ? `<span class="silver">${fmt(o.subtotal)}</span>` : '—' },
    ], res.shopping || [], { sortKey: 'sub' });
    const rawTxt = t.raw_cost != null ? fmt(t.raw_cost) + ' prata' : '—';
    st.textContent = `${(res.shopping || []).length} recursos brutos · matéria-prima p/ ${fmt(t.target_qty)} un. ≈ ${rawTxt}${t.unpriced && t.unpriced.length ? ` (parcial: ${fmt(t.unpriced.length)} sem cotação)` : ''} · sem desconto de RRR`;
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
    $('wikiTreeMeta').textContent = '';
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

// sub-aba Risco & Previsão
async function loadRisco() {
  const it = state.item;
  if (!it) return;
  const st = $('riscoStatus');
  st.className = 'status';
  st.textContent = 'analisando risco e previsão…';
  try {
    const res = await api('/api/item_signals', { item: it.id });
    const body = $('riscoBody');
    if (!res.available) {
      body.innerHTML = '';
      st.textContent = res.note || 'sem histórico suficiente para o item';
      return;
    }
    const rk = res.risk, rv = res.reversion, rg = res.regime, pr = res.predictability;
    const pill = (label, val, cls) =>
      `<div class="lab-pill"><b>${label}</b><span class="${cls || ''}">${val}</span></div>`;
    let html = '';
    if (rk) {
      const ci = rk.vol_ci_pct ? ` <small>(IC ${Math.round(rk.vol_ci_pct[0])}–${Math.round(rk.vol_ci_pct[1])})</small>` : '';
      html += '<div class="lab-summary">' +
        pill('Selo de risco', esc(rk.risk_label || '—')) +
        pill('Vol anualizada', `${fmtDec(rk.vol_annual_pct, 1)}%${ci}`) +
        pill('Vol EWMA', rk.vol_ewma_pct != null ? `${fmtDec(rk.vol_ewma_pct, 1)}%` : '—') +
        pill('Máx. drawdown', `${fmtDec(rk.max_drawdown_pct, 1)}%`, 'profit-neg') +
        pill('VaR 1-dia (5%)', `${fmtDec(rk.var_1d_pct, 1)}%`) +
        pill('Sortino', rk.sortino != null ? fmtDec(rk.sortino, 2) : '—') +
        pill('Dias de série', rk.points) +
        '</div>';
    } else {
      html += '<div class="status">série curta demais para o perfil de risco.</div>';
    }
    if (rv) {
      const dir = rv.signal
        ? (rv.direction === 'comprar'
          ? '<span class="profit-pos">comprar — preço abaixo do equilíbrio</span>'
          : '<span class="profit-neg">esperar / vender — preço acima</span>')
        : '<span class="muted">sem sinal forte agora</span>';
      html += '<div class="lab-card" style="margin-top:12px">' +
        '<div class="lab-card-head"><h3>Reversão à média</h3>' +
        `<span class="lab-stance">${rv.signal ? 'SINAL' : 'neutro'}</span></div>` +
        '<div class="lab-metrics">' +
        `<div><span>Preço atual</span><b>${fmt(rv.current)}</b></div>` +
        `<div><span>Alvo (equilíbrio)</span><b>${fmt(rv.target)}</b></div>` +
        `<div><span>Gap p/ o alvo</span><b>${fmtDec(rv.gap_pct, 1)}%</b></div>` +
        `<div><span>Meia-vida</span><b>${rv.halflife_days != null ? rv.halflife_days + 'd' : '—'}</b></div>` +
        '</div>' +
        `<div class="forecast">Banda ~68%: <b>${fmt(rv.band_low)}</b> a <b>${fmt(rv.band_high)}</b>` +
        ` · z ${fmtDec(rv.z_resid, 2)} · ${dir}</div></div>`;
    }
    const foot = [];
    if (rg) {
      foot.push(rg.significant
        ? `Quebra de regime detectada (${esc(rg.kind)}${rg.level_change_pct != null ? `, ${rg.level_change_pct}% de nível` : ''}) — confie só nos últimos <b>${rg.valid_points}</b> dias.`
        : 'Sem quebra de regime significativa (série estável).');
    }
    if (pr) foot.push(`Previsibilidade: <b>${esc(pr.label)}</b> (score ${fmtDec(pr.predictability, 2)}).`);
    if (foot.length) html += `<p class="hint">${foot.join(' ')}</p>`;
    body.innerHTML = html;
    st.textContent = `Melhor série: ${esc(res.city)} · ${res.points} dias`;
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
    renderTable('flipsTable', flipColumns(false), rows, { sortKey: 'flipscore' });
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
    { sortKey: 'flipscore' });
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

let avView = 'spread';
async function loadAvancado(view) {
  if (view) avView = view;
  const st = $('avStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/micro', {
      view: avView, premium: state.premium, min_volume: 5, limit: 40,
    });
    const itemCell = (o) => `<div class="cell-item">${iconImg(o.item_id, o.quality)}` +
      `<div class="nm">${esc(o.name_pt)} ${qHtml(o.quality)}</div></div>`;
    if (avView === 'capital') {
      renderTable('avTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: itemCell },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'yield', label: 'Rend/dia %', value: (o) => o.yield_day_pct, html: (o) => fmtDec(o.yield_day_pct, 1) + '%' },
        { key: 'net', label: 'Net/un', value: (o) => o.net_per_unit, html: (o) => fmt(o.net_per_unit) },
        { key: 'units', label: 'Unid.', value: (o) => o.units, html: (o) => fmt(o.units) },
        { key: 'cap', label: 'Capital', value: (o) => o.alloc_capital, html: (o) => fmt(o.alloc_capital) },
        { key: 'profit', label: 'Lucro/dia', value: (o) => o.profit_day, html: (o) => `<span class="profit-pos">${fmt(o.profit_day)}</span>` },
      ], res.plan || [], { sortKey: 'yield' });
      st.textContent = `Capital ${fmt(res.capital)} · usado ${fmt(res.capital_used)} · ` +
        `lucro/dia estimado ${fmt(res.profit_day_total)}` +
        (res.fill_rate ? ` · preenchimento (survival) ${Math.round(res.fill_rate * 100)}%` : '');
    } else {
      const rows = res.rows || [];
      renderTable('avTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: itemCell },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'buy', label: 'Compra', value: (o) => o.buy_price_max, html: (o) => fmt(o.buy_price_max) },
        { key: 'sell', label: 'Venda', value: (o) => o.sell_price_min, html: (o) => fmt(o.sell_price_min) },
        { key: 'net', label: 'Net/un', value: (o) => o.net_per_unit, html: (o) => `<span class="profit-pos">${fmt(o.net_per_unit)}</span>` },
        { key: 'netpct', label: 'Net %', value: (o) => o.net_pct, html: (o) => fmtDec(o.net_pct, 1) + '%' },
        { key: 'liq', label: 'Giro/dia', value: (o) => o.liquidity_day, html: (o) => fmt(o.liquidity_day) },
        { key: 'pot', label: 'Pot./dia', value: (o) => o.potential_day, html: (o) => fmt(o.potential_day) },
      ], rows, { sortKey: 'pot' });
      st.textContent = rows.length
        ? `${rows.length} oportunidades de market-making (poste compra+venda na mesma cidade)`
        : 'sem spread líquido positivo no cache agora — colete mais ou tente sem premium';
    }
  } catch (e) {
    st.className = 'status err';
    st.textContent = 'erro: ' + e.message;
  }
}

// ---- hub Avançado: sub-abas (microestrutura / produção / demanda / guild) ----
function showAvSub(av) {
  document.querySelectorAll('#avSubtabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.av === av));
  document.querySelectorAll('#tab-avancado .subtab').forEach((s) =>
    s.classList.toggle('active', s.id === 'av-' + av));
  state.avSubLoaded = state.avSubLoaded || {};
  if (state.avSubLoaded[av]) return;
  state.avSubLoaded[av] = true;
  ({ micro: loadAvancado, prod: loadAvProd,
     guild: loadAvGuild, logi: loadAvLogi, demanda: loadAvDemanda,
     risco: loadAvRisco }[av]
    || (() => {}))();
}

const avItemCell = (o) => `<div class="cell-item">${iconImg(o.item_id, o.quality)}` +
  `<div class="nm">${esc(o.name_pt || o.item_id)}</div></div>`;

let avProdView = 'focus';
async function loadAvProd(view) {
  if (view) avProdView = view;
  const st = $('prodStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/prod', { view: avProdView, premium: state.premium, limit: 50 });
    const rows = res.rows || [];
    if (avProdView === 'refine') {
      renderTable('prodTable', [
        { key: 'item', label: 'Refinado', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'prem', label: 'Prêmio %', value: (o) => o.premium_pct, html: (o) => fmtDec(o.premium_pct, 1) + '%' },
        { key: 'margin', label: 'Margem', value: (o) => o.margin, html: (o) => fmt(o.margin) },
        { key: 'rrr', label: 'RRR %', value: (o) => o.rrr_pct, html: (o) => fmtDec(o.rrr_pct, 1) + '%' },
        { key: 'verdict', label: 'Veredito', align: 'l', value: (o) => o.verdict, html: (o) => esc(o.verdict) },
      ], rows, { sortKey: 'prem' });
    } else {
      renderTable('prodTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'tipo', label: 'Tipo', align: 'l', value: (o) => o.tipo, html: (o) => esc(o.tipo) },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'spf', label: 'Prata/foco', value: (o) => o.silver_per_focus, html: (o) => fmt(o.silver_per_focus) },
        { key: 'focus', label: 'Foco', value: (o) => o.focus, html: (o) => fmt(o.focus) },
        { key: 'margin', label: 'Margem c/foco', value: (o) => o.margin_focus, html: (o) => fmt(o.margin_focus) },
      ], rows, { sortKey: 'spf' });
    }
    st.textContent = `${rows.length} ${avProdView === 'refine' ? 'refinados' : 'receitas'} cotados no cache`;
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
}

let avGuildView = 'makeorbuy';
async function loadAvGuild(view) {
  if (view) avGuildView = view;
  const st = $('guildStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/guild', { view: avGuildView, days: 7, premium: state.premium, limit: 50 });
    const rows = res.rows || [];
    if (avGuildView === 'kit') {
      const basket = res.basket || [];
      renderTable('guildTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'w', label: 'Peso na cesta', value: (o) => o.weight, html: (o) => fmtDec(100 * (o.weight || 0), 1) + '%' },
      ], basket, { sortKey: 'w' });
      const idx = (res.series && res.series.length) ? res.series[res.series.length - 1].index : null;
      st.textContent = basket.length ? `cesta de regear: ${basket.length} itens · índice de custo atual ${idx == null ? '—' : idx}` : 'sem dados de killboard ainda (rode o intel-sweep)';
      return;
    }
    if (avGuildView === 'watch') {
      renderTable('guildTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'dest', label: 'Destruídos', value: (o) => o.destroyed, html: (o) => fmt(o.destroyed) },
        { key: 'ad', label: 'Dias ativos', value: (o) => o.active_days, html: (o) => fmt(o.active_days) },
        { key: 'price', label: 'Preço', value: (o) => o.price, html: (o) => o.price == null ? '—' : fmt(o.price) },
        { key: 'score', label: 'Score', value: (o) => o.score, html: (o) => fmt(o.score) },
      ], rows, { sortKey: 'score' });
      st.textContent = rows.length ? `${rows.length} itens (ranking de destruição)` : 'sem dados de killboard ainda (rode o intel-sweep)';
    } else {
      renderTable('guildTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'dem', label: 'Demanda', value: (o) => o.demand_units, html: (o) => fmt(o.demand_units) },
        { key: 'make', label: 'Fazer', value: (o) => o.internal_cost, html: (o) => fmt(o.internal_cost) },
        { key: 'buy', label: 'Comprar', value: (o) => o.market_price, html: (o) => fmt(o.market_price) },
        { key: 'save', label: 'Economia %', value: (o) => o.save_pct == null ? 0 : o.save_pct, html: (o) => fmtDec(o.save_pct, 1) + '%' },
        { key: 'v', label: 'Veredito', align: 'l', value: (o) => o.verdict, html: (o) => esc(o.verdict) },
      ], rows, { sortKey: 'save' });
      st.textContent = rows.length ? `${rows.length} itens (fazer vs comprar)` : 'sem dados de killboard ainda (rode o intel-sweep)';
    }
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
}

let avLogiView = 'bm';
async function loadAvLogi(view) {
  if (view) avLogiView = view;
  const st = $('logiStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/logi', { view: avLogiView, premium: state.premium, days: 7, limit: 50 });
    const rows = res.rows || [];
    if (avLogiView === 'ladder') {
      renderTable('logiTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'quals', label: 'Q cotadas', align: 'l', value: (o) => o.quals, html: (o) => esc(o.quals) },
        { key: 'step', label: 'Maior salto', align: 'l', value: (o) => o.best_step, html: (o) => esc(o.best_step) },
        { key: 'pct', label: 'Prêmio %', value: (o) => o.best_premium_pct, html: (o) => fmtDec(o.best_premium_pct, 1) + '%' },
        { key: 'abs', label: 'Prêmio prata', value: (o) => o.best_premium_abs, html: (o) => fmt(o.best_premium_abs) },
      ], rows, { sortKey: 'pct' });
    } else if (avLogiView === 'restock') {
      renderTable('logiTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'dem', label: 'Demanda', value: (o) => o.demand_units, html: (o) => fmt(o.demand_units) },
        { key: 'buy', label: 'Comprar em', align: 'l', value: (o) => o.buy_city, html: (o) => cityHtml(o.buy_city) },
        { key: 'bp', label: 'Custo', value: (o) => o.buy_price, html: (o) => fmt(o.buy_price) },
        { key: 'sell', label: 'Vender em', align: 'l', value: (o) => o.sell_city, html: (o) => cityHtml(o.sell_city) },
        { key: 'profit', label: 'Lucro', value: (o) => o.profit, html: (o) => fmt(o.profit) },
        { key: 'ppk', label: 'Lucro/kg', value: (o) => o.profit_per_kg == null ? 0 : o.profit_per_kg, html: (o) => o.profit_per_kg == null ? '—' : fmtDec(o.profit_per_kg, 1) },
      ], rows, { sortKey: 'profit' });
    } else {
      renderTable('logiTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'q', label: 'Q', value: (o) => o.quality, html: (o) => 'q' + o.quality },
        { key: 'real', label: 'Melhor real', align: 'l', value: (o) => o.best_city, html: (o) => cityHtml(o.best_city) },
        { key: 'rn', label: 'Real líq', value: (o) => o.best_city_net, html: (o) => fmt(o.best_city_net) },
        { key: 'bm', label: 'BM líq', value: (o) => o.bm_net, html: (o) => fmt(o.bm_net) },
        { key: 'pa', label: 'Prêmio', value: (o) => o.premium_abs, html: (o) => fmt(o.premium_abs) },
        { key: 'pct', label: 'Prêmio %', value: (o) => o.premium_pct, html: (o) => fmtDec(o.premium_pct, 1) + '%' },
      ], rows, { sortKey: 'pa' });
    }
    st.textContent = rows.length ? `${rows.length} itens (${avLogiView})` : 'sem dados para esta visão (colete preços/killboard)';
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
}

let avRiscoView = 'profile';
async function loadAvRisco(view) {
  if (view) avRiscoView = view;
  const st = $('avRiscoStatus');
  st.className = 'status';
  st.textContent = avRiscoView === 'profile' ? 'calculando risco (bootstrap, alguns segundos)…' : 'calculando correlação…';
  try {
    const res = await api('/api/risk', { view: avRiscoView, days: 120, limit: 60 });
    const rows = res.rows || [];
    if (avRiscoView === 'corr') {
      renderTable('riscoTable', [
        { key: 'a', label: 'Item A', align: 'l', value: (o) => o.a_pt, html: (o) => esc(o.a_pt) },
        { key: 'b', label: 'Item B', align: 'l', value: (o) => o.b_pt, html: (o) => esc(o.b_pt) },
        { key: 'corr', label: 'Correl.', value: (o) => o.corr, html: (o) => fmtDec(o.corr, 2) },
        { key: 'days', label: 'Dias comuns', value: (o) => o.common_days, html: (o) => fmt(o.common_days) },
        { key: 'tipo', label: 'Leitura', align: 'l', value: (o) => o.tipo, html: (o) => esc(o.tipo) },
      ], rows, { sortKey: 'corr' });
    } else {
      const lbl = (o) => `<span class="risk-${o.risk_label === 'seguro' ? 'lo' : o.risk_label === 'médio' ? 'mid' : 'hi'}">${esc(o.risk_label)}</span>`;
      renderTable('riscoTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'city', label: 'Cidade', align: 'l', value: (o) => o.city, html: (o) => cityHtml(o.city) },
        { key: 'pts', label: 'Dias', value: (o) => o.points, html: (o) => fmt(o.points) },
        { key: 'vol', label: 'Vol %a.a.', value: (o) => o.vol_annual_pct, html: (o) => fmtDec(o.vol_annual_pct, 1) + '%' },
        { key: 'ci', label: 'IC vol', align: 'l', value: (o) => o.vol_ci, html: (o) => esc(o.vol_ci) },
        { key: 'shr', label: 'Vol ajust.', value: (o) => o.vol_shrunk_pct, html: (o) => fmtDec(o.vol_shrunk_pct, 1) + '%' },
        { key: 'dd', label: 'Max DD %', value: (o) => o.max_drawdown_pct, html: (o) => fmtDec(o.max_drawdown_pct, 1) + '%' },
        { key: 'var', label: 'VaR 1d %', value: (o) => o.var_1d_pct, html: (o) => fmtDec(o.var_1d_pct, 1) + '%' },
        { key: 'lbl', label: 'Selo', align: 'l', value: (o) => o.risk_label, html: lbl },
      ], rows, { sortKey: 'shr' });
    }
    st.textContent = rows.length ? `${rows.length} ${avRiscoView === 'corr' ? 'pares' : 'itens líquidos'}` : 'sem série suficiente no cache';
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
}

let avDemandaView = 'burn';
async function loadAvDemanda(view) {
  if (view) avDemandaView = view;
  const st = $('demandaStatus');
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/demand', { view: avDemandaView, days: 7, premium: state.premium, limit: 50 });
    const rows = res.rows || [];
    if (avDemandaView === 'quality') {
      renderTable('demandaTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'dest', label: 'Destruídos', value: (o) => o.destroyed, html: (o) => fmt(o.destroyed) },
        { key: 'q4', label: 'Share q4+ %', value: (o) => o.share_q4plus_pct, html: (o) => fmtDec(o.share_q4plus_pct, 1) + '%' },
        { key: 'ev', label: 'E[prêmio qual.]', value: (o) => o.ev_quality_premium, html: (o) => fmtDec(o.ev_quality_premium, 2) },
        { key: 'dom', label: 'Q dominante', value: (o) => o.dominant_q, html: (o) => 'q' + o.dominant_q },
      ], rows, { sortKey: 'ev' });
    } else {
      renderTable('demandaTable', [
        { key: 'item', label: 'Item', align: 'l', value: (o) => o.name_pt, html: avItemCell },
        { key: 'burn', label: 'Queimados', value: (o) => o.burned_units, html: (o) => fmt(o.burned_units) },
        { key: 'pd', label: 'Por dia', value: (o) => o.per_day, html: (o) => fmtDec(o.per_day, 1) },
        { key: 'mv', label: 'Vol mercado/dia', value: (o) => o.market_vol_day, html: (o) => o.market_vol_day == null ? '—' : fmtDec(o.market_vol_day, 1) },
        { key: 'cov', label: 'Cobertura', value: (o) => o.coverage == null ? 9e9 : o.coverage, html: (o) => o.coverage == null ? '—' : fmtDec(o.coverage, 2) },
        { key: 'spd', label: 'Prata/dia', value: (o) => o.silver_per_day, html: (o) => o.silver_per_day == null ? '—' : fmt(o.silver_per_day) },
      ], rows, { sortKey: 'spd' });
    }
    st.textContent = rows.length ? `${rows.length} itens (${avDemandaView})` : 'sem dados de killboard ainda (rode o intel-sweep)';
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
}


function showIslandSub(view) {
  document.querySelectorAll('#islandSubtabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.isl === view));
  document.querySelectorAll('#tab-ilha .subtab').forEach((s) =>
    s.classList.toggle('active', s.id === 'isl-' + view));
  state.islSubLoaded = state.islSubLoaded || {};
  if (state.islSubLoaded[view]) return;
  state.islSubLoaded[view] = true;
  loadIsland(view);
}

const islItemCell = (id, pt) => `<div class="cell-item">${iconImg(id)}<div class="nm">${esc(pt || id)}</div></div>`;

async function loadIsland(view) {
  const map = {
    laborers: { st: 'islLaborStatus', tbl: 'islLaborTable' },
    crops: { st: 'islCropStatus', tbl: 'islCropTable' },
    animals: { st: 'islAnimalStatus', tbl: 'islAnimalTable' },
  };
  const m = map[view];
  const st = $(m.st);
  st.className = 'status';
  st.textContent = 'calculando…';
  try {
    const res = await api('/api/island', { view, premium: state.premium, limit: 80 });
    const rows = res.rows || [];
    if (view === 'crops') {
      renderTable(m.tbl, [
        { key: 'crop', label: 'Cultura', align: 'l', value: (o) => o.crop_pt, html: (o) => islItemCell(o.crop, o.crop_pt) },
        { key: 'buy', label: 'Semente em', align: 'l', value: (o) => o.buy_city, html: (o) => cityHtml(o.buy_city) },
        { key: 'sp', label: 'Semente', value: (o) => o.seed_price, html: (o) => fmt(o.seed_price) },
        { key: 'yld', label: 'Colheita', value: (o) => o.crop_yield, html: (o) => fmt(o.crop_yield) },
        { key: 'sell', label: 'Vender em', align: 'l', value: (o) => o.sell_city, html: (o) => cityHtml(o.sell_city) },
        { key: 'pdn', label: 'Lucro/dia s/foco', value: (o) => o.per_day_no_focus, html: (o) => fmt(o.per_day_no_focus) },
        { key: 'pdf', label: 'Lucro/dia c/foco', value: (o) => o.per_day_focus, html: (o) => `<span class="silver profit-pos">${fmt(o.per_day_focus)}</span>` },
        { key: 'fg', label: 'Ganho do foco', value: (o) => o.focus_gain, html: (o) => fmt(o.focus_gain) },
        { key: 'wc', label: 'Capital/canteiro', value: (o) => o.working_capital, html: (o) => fmt(o.working_capital) },
      ], rows, { sortKey: 'pdf' });
    } else if (view === 'animals') {
      renderTable(m.tbl, [
        { key: 'animal', label: 'Animal', align: 'l', value: (o) => o.grown_pt, html: (o) => islItemCell(o.grown, o.grown_pt) },
        { key: 'buy', label: 'Cria em', align: 'l', value: (o) => o.buy_city, html: (o) => cityHtml(o.buy_city) },
        { key: 'bp', label: 'Cria', value: (o) => o.baby_price, html: (o) => fmt(o.baby_price) },
        { key: 'feed', label: 'Ração', value: (o) => o.feed_cost, html: (o) => fmt(o.feed_cost) },
        { key: 'sell', label: 'Vender em', align: 'l', value: (o) => o.sell_city, html: (o) => cityHtml(o.sell_city) },
        { key: 'pdn', label: 'Lucro/dia s/foco', value: (o) => o.per_day_no_focus, html: (o) => `<span class="silver profit-pos">${fmt(o.per_day_no_focus)}</span>` },
        { key: 'pdf', label: 'Lucro/dia c/foco (est.)', value: (o) => o.per_day_focus, html: (o) => fmt(o.per_day_focus) },
        { key: 'wc', label: 'Capital/animal', value: (o) => o.working_capital, html: (o) => fmt(o.working_capital) },
      ], rows, { sortKey: 'pdn' });
    } else {
      renderTable(m.tbl, [
        { key: 'fam', label: 'Diário', align: 'l', value: (o) => o.empty_pt, html: (o) => islItemCell(o.empty, o.empty_pt) },
        { key: 'buy', label: 'Vazio em', align: 'l', value: (o) => o.buy_city, html: (o) => cityHtml(o.buy_city) },
        { key: 'ep', label: 'Vazio', value: (o) => o.empty_price, html: (o) => fmt(o.empty_price) },
        { key: 'sell', label: 'Cheio em', align: 'l', value: (o) => o.sell_city, html: (o) => cityHtml(o.sell_city) },
        { key: 'fp', label: 'Cheio líq', value: (o) => o.full_net, html: (o) => fmt(o.full_net) },
        { key: 'margin', label: 'Margem (fama)', value: (o) => o.margin, html: (o) => `<span class="silver profit-pos">${fmt(o.margin)}</span>` },
        { key: 'res', label: 'Entrega', align: 'l', value: (o) => o.resource_pt || '', html: (o) => o.resource_pt ? esc(o.resource_pt) : '—' },
        { key: 'rs', label: 'Vender entrega', align: 'l', value: (o) => o.resource_sell_city || '', html: (o) => o.resource_sell_city ? cityHtml(o.resource_sell_city) : '—' },
      ], rows, { sortKey: 'margin' });
    }
    if (!rows.length) {
      st.textContent = 'sem preços de ilha no cache ainda (a coleta cobre conforme roda)';
    } else if (view === 'crops') {
      st.textContent = `${rows.length} culturas · colheita fixa; foco garante a volta da semente (ganho = semente economizada)`;
    } else if (view === 'animals') {
      st.textContent = `${rows.length} animais · ração não cai com foco; "c/foco" é estimativa (prole extra) — ranking pelo número firme s/ foco`;
    } else {
      st.textContent = `${rows.length} diários · entrega só p/ coletores (fabricante/pesca não entregam recurso bruto)`;
    }
  } catch (e) { st.className = 'status err'; st.textContent = 'erro: ' + e.message; }
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

// barra de progresso da coleta automática (item 2): mostra "coletando X/Y" e
// atualiza a Início a cada bloco que chega, pra não parecer travado.
async function pollCollectStatus() {
  let running = false;
  try {
    const s = await api('/api/collect-status');
    const el = $('collectBanner');
    if (el) {
      if (s.running && s.total > 0) {
        running = true;
        const pct = Math.round(100 * s.done / Math.max(1, s.total));
        el.hidden = false;
        el.innerHTML = `<div class="cb-fill" style="width:${pct}%"></div>` +
          `<div class="cb-txt"><span class="cb-dot"></span>` +
          `Coletando dados do mercado… <b>${fmt(s.done)}/${fmt(s.total)}</b> (${pct}%)` +
          ` <span class="muted">— a Início se preenche conforme chega</span></div>`;
        if (s.done !== state._collectDone) {   // novo bloco: atualiza a Início
          state._collectDone = s.done;
          if (document.getElementById('tab-dashboard').classList.contains('active')) {
            loadDashboardRecommendations();
          }
        }
      } else {
        if (!el.hidden) {                       // terminou: esconde + refresh final
          el.hidden = true;
          if (document.getElementById('tab-dashboard').classList.contains('active')) {
            loadDashboardRecommendations();
          }
        }
        state._collectDone = 0;
      }
    }
  } catch (e) { /* endpoint ausente (nuvem) ou sem sessão: ignora */ }
  setTimeout(pollCollectStatus, running ? 6000 : 20000);
}

// ====================================================== Linha de Produção
// Editor visual de nós: escolhe-se 1+ produtos finais, a árvore de receita
// (BOM) vem do servidor (/api/prodchain) já com RRR por cidade + preços; a
// propagação de quantidade, o make-or-buy por nó e os custos rodam aqui (ao
// vivo). Espelha albion/prodchain.solve()/make_or_buy() — mesma matemática.
const PL_NW = 188, PL_NH = 96;          // tamanho aprox. do nó (âncora de aresta)

function prodInitState() {
  return { roots: [], targets: {}, graph: { nodes: {}, cities: [], sell_mode: 'order' },
           ns: {}, stock: {}, qtyDefault: 100, stationFee: 0, specFce: 0, focusPrice: 0,
           showCosts: false, savedId: null, savedName: '', calc: null, verdict: {} };
}
function plNodes() { return (state.prodLine.graph || {}).nodes || {}; }
function plCities() { return state.prodLine.graph.cities || []; }
function cityLabel(c) { return (state.meta && state.meta.city_labels && state.meta.city_labels[c]) || c; }
function plNodeCity(it) {
  const n = plNodes()[it] || {}, s = state.prodLine.ns[it] || {};
  return s.city || n.bonus_city || n.buy_city || plCities()[0] || 'Caerleon';
}
function plIsBuy(it) {
  const n = plNodes()[it];
  if (!n || n.is_raw || !n.recipe) return true;
  return (state.prodLine.ns[it] || {}).mode === 'buy';
}
function plRrr(it) {
  const n = plNodes()[it];
  if (!n || n.is_raw || !n.recipe) return 0;
  const band = (n.rrr_by_city || {})[plNodeCity(it)] || {};
  return ((state.prodLine.ns[it] || {}).focus ? band.f : band.nf) || 0;
}
function plFocusEff(base) { return (base || 0) * Math.pow(0.5, (state.prodLine.specFce || 0) / 10000); }
function plSellNet(node) {
  if (!node || !node.sell_gross) return null;
  const tax = state.premium ? 0.04 : 0.08;
  // espelha flips.sell_revenue: taxa de anúncio (2,5%) só em ORDEM de venda;
  // venda instantânea (e Mercado Negro) não paga anúncio.
  const setup = (state.prodLine.graph.sell_mode === 'instant') ? 0 : 0.025;
  return node.sell_gross * (1 - tax - setup);
}
function prodDefaultState(it) {
  const n = plNodes()[it] || {};
  const city = n.bonus_city || n.buy_city || (n.rrr_by_city && Object.keys(n.rrr_by_city)[0]) || plCities()[0] || 'Caerleon';
  return { mode: (n.is_raw || !n.recipe) ? 'buy' : 'make', focus: false, city };
}

function prodPropagate() {                       // espelha solve(): Kahn + ceil
  const nodes = plNodes(), pl = state.prodLine, stock = pl.stock || {};
  const gross = {}, net = {}, crafts = {}, indeg = {};
  for (const it in nodes) indeg[it] = 0;
  for (const it in nodes) {
    if (plIsBuy(it)) continue;
    for (const inp of nodes[it].recipe) indeg[inp.id] = (indeg[inp.id] || 0) + 1;
  }
  for (const r of pl.roots) gross[r] = (gross[r] || 0) + (pl.targets[r] || 0);
  const ready = [];
  for (const it in nodes) if ((indeg[it] || 0) === 0) ready.push(it);
  const seen = new Set();
  while (ready.length) {
    const it = ready.shift();
    if (seen.has(it)) continue;
    seen.add(it);
    const n = nodes[it];
    // estoque declarado abate da demanda: só se produz/compra o que FALTA
    // (clamp >=0 p/ estoque negativo nunca inflar a demanda)
    const eff = Math.max(0, (gross[it] || 0) - Math.max(0, stock[it] || 0));
    net[it] = eff;
    if (!plIsBuy(it) && eff > 0) {
      const b = Math.ceil(eff / (n.output || 1));
      crafts[it] = b;
      const rrr = plRrr(it);
      for (const inp of n.recipe) gross[inp.id] = (gross[inp.id] || 0) + b * inp.count * (1 - rrr);
    }
    if (!plIsBuy(it)) for (const inp of (n.recipe || [])) {
      indeg[inp.id] -= 1;
      if (indeg[inp.id] === 0) ready.push(inp.id);
    }
  }
  return { demand: net, gross, crafts };
}

function prodComputeCosts(prop) {
  const nodes = plNodes(), pl = state.prodLine, { demand, crafts, gross } = prop;
  const shopping = {}; let focusPoints = 0, stationTotal = 0;
  for (const it in nodes) {
    const q = demand[it] || 0;
    if (q <= 0) continue;
    if (plIsBuy(it)) {
      const qty = Math.ceil(q), c = plNodeCity(it);
      const unit = (nodes[it].buy_by_city || {})[c] ?? nodes[it].buy_price ?? null;
      shopping[it] = { qty, unit, cost: unit != null ? qty * unit : null,
                       city: nodes[it].is_raw ? (nodes[it].buy_city || c) : c,
                       priced: unit != null };
    } else {
      const b = crafts[it] || 0;
      stationTotal += b * pl.stationFee;
      if ((pl.ns[it] || {}).focus) focusPoints += b * plFocusEff(nodes[it].focus_base);
    }
  }
  let buyCost = 0; const missing = [];
  for (const it in shopping) { if (shopping[it].cost) buyCost += shopping[it].cost; else missing.push(it); }
  const focusSilver = focusPoints * pl.focusPrice;
  let revenue = 0, totalTarget = 0;
  const missingSell = [];
  for (const r of pl.roots) {
    const t = pl.targets[r] || 0; totalTarget += t;
    const net = plSellNet(nodes[r]);
    if (net) revenue += net * t;
    else missingSell.push(r);   // produto final sem cotação de venda
  }
  const profit = revenue - buyCost - stationTotal - focusSilver;
  return { shopping, buyCost, stationTotal, focusPoints, focusSilver, revenue,
           profit, perUnit: totalTarget ? profit / totalTarget : 0,
           roi: buyCost ? 100 * profit / buyCost : null, missing, missingSell,
           demand, gross, crafts };
}

function prodMakeOrBuy() {                        // veredito intrínseco por nó
  const nodes = plNodes(), pl = state.prodLine, memo = {}, verdict = {};
  function unit(it) {
    if (it in memo) return memo[it];
    memo[it] = null;
    const n = nodes[it], c = plNodeCity(it);
    const buy = (n.buy_by_city || {})[c] ?? n.buy_price ?? null;
    if (n.is_raw || !n.recipe) { memo[it] = buy; return buy; }
    const rrr = plRrr(it); let inp = 0, ok = true;
    for (const i of n.recipe) { const uc = unit(i.id); if (uc == null) { ok = false; break; } inp += uc * i.count; }
    let make = null;
    if (ok) {
      const foc = (pl.ns[it] || {}).focus ? plFocusEff(n.focus_base) * pl.focusPrice : 0;
      make = (inp * (1 - rrr) + pl.stationFee + foc) / (n.output || 1);
    }
    verdict[it] = {
      make_unit: make != null ? Math.round(make) : null,
      buy_unit: buy != null ? Math.round(buy) : null,
      verdict: (make != null && (buy == null || make < buy)) ? 'make' : (buy != null ? 'buy' : null),
      savings: (make != null && buy != null) ? Math.round(Math.abs(buy - make)) : null,
    };
    const opts = [buy, make].filter((x) => x != null);
    memo[it] = opts.length ? Math.min(...opts) : null;
    return memo[it];
  }
  for (const it in nodes) unit(it);
  return verdict;
}

function prodLayout() {
  const nodes = plNodes(), ns = state.prodLine.ns, depth = {};
  const visit = (it, d, seen) => {
    if (depth[it] === undefined || d > depth[it]) depth[it] = d;
    const n = nodes[it];
    if (!n || !n.recipe || seen.has(it)) return;
    const s2 = new Set(seen); s2.add(it);
    for (const inp of n.recipe) visit(inp.id, d + 1, s2);
  };
  for (const r of state.prodLine.roots) visit(r, 0, new Set());
  const maxD = Math.max(0, ...Object.values(depth));
  const byLayer = {};
  for (const it in depth) (byLayer[depth[it]] ||= []).push(it);
  const COLW = 224, ROWH = 116, PADX = 16, PADY = 14;
  for (const d in byLayer) {
    byLayer[d].sort();
    byLayer[d].forEach((it, i) => {
      const s = ns[it] || (ns[it] = prodDefaultState(it));
      if (s.x === undefined || s.x === null) {
        s.x = PADX + (maxD - d) * COLW;     // bruto à esquerda, final à direita
        s.y = PADY + i * ROWH;
      }
    });
  }
}

async function prodFetchGraph() {
  const pl = state.prodLine;
  if (!pl.roots.length) {
    pl.graph = { nodes: {}, cities: [], sell_mode: 'order' }; pl.calc = null;
    prodRenderProducts(); prodRenderResult(null); prodRenderShopping(null);
    prodRenderChainView(); $('plStatus').textContent = ''; return;
  }
  $('plStatus').className = 'status'; $('plStatus').textContent = 'montando cadeia…';
  try {
    const g = await api('/api/prodchain', { item: pl.roots.join(','), premium: state.premium });
    pl.graph = g;
    for (const it in g.nodes) if (!pl.ns[it]) pl.ns[it] = prodDefaultState(it);
    for (const it of Object.keys(pl.ns)) if (!g.nodes[it]) delete pl.ns[it];
    for (const r of pl.roots) if (pl.targets[r] == null) pl.targets[r] = pl.qtyDefault;
    for (const r of Object.keys(pl.targets)) if (!pl.roots.includes(r)) delete pl.targets[r];
    prodLayout();
    prodRenderProducts();
    prodRecompute();
    $('plStatus').textContent = '';
  } catch (e) { $('plStatus').className = 'status err'; $('plStatus').textContent = 'erro: ' + e.message; }
}

function prodRecompute() {
  const prop = prodPropagate();
  state.prodLine.calc = prodComputeCosts(prop);
  state.prodLine.verdict = prodMakeOrBuy();
  prodRenderResult(state.prodLine.calc);
  prodRenderShopping(state.prodLine.calc);
  prodRenderChainView();
}

function prodNodeHtml(it, demand, crafts) {
  const pl = state.prodLine, n = pl.graph.nodes[it], s = pl.ns[it] || {};
  const isRoot = pl.roots.includes(it), buy = plIsBuy(it), active = demand > 0;
  const calc = pl.calc || {}, verdict = (pl.verdict || {})[it];
  const te = `T${n.tier || '?'}${n.enchant ? '.' + n.enchant : ''}`;
  const qtyHtml = isRoot
    ? `<input class="pl-target" type="number" min="1" value="${pl.targets[it] || pl.qtyDefault}" data-id="${esc(it)}" title="quantidade final desejada">`
    : `<span class="pl-x">×${fmt(Math.ceil(demand))}</span>`;
  const citySel = `<select class="pl-city" data-id="${esc(it)}" title="cidade da etapa">${
    plCities().map((c) => `<option value="${esc(c)}" ${c === plNodeCity(it) ? 'selected' : ''}>${esc(cityLabel(c))}</option>`).join('')}</select>`;
  let ctrl;
  if (!n.is_raw && n.recipe) {
    ctrl = `<div class="pl-toggles">
      <button class="pl-tg ${buy ? '' : 'on'}" data-act="make" data-id="${esc(it)}">Fabricar</button>
      <button class="pl-tg ${buy ? 'on' : ''}" data-act="buy" data-id="${esc(it)}">Comprar</button>
      <button class="pl-tg ${s.focus ? 'on focus' : ''}" data-act="focus" data-id="${esc(it)}" ${buy ? 'disabled' : ''} title="usar foco nesta etapa">Foco</button>
    </div>${citySel}`;
  } else {
    ctrl = citySel;
  }
  let costLine;
  if (buy) {
    const sh = (calc.shopping || {})[it];
    costLine = sh ? (sh.priced ? `<span class="silver">${fmt(sh.cost)}</span> p/ comprar` : '<span class="muted">sem cotação</span>') : '';
  } else {
    costLine = `RRR ${fmtDec(plRrr(it) * 100, 0)}%${crafts ? ` · ${fmt(crafts)} crafts` : ''}`;
  }
  let badge = '';
  if (verdict && verdict.verdict) {
    const txt = verdict.verdict === 'make' ? 'fabricar' : 'comprar';
    const tip = (verdict.verdict === 'make' ? 'fabricar é mais barato' : 'comprar é mais barato') +
      (verdict.savings != null ? ` · economia ${fmt(verdict.savings)}/un` : '');
    badge = `<span class="pl-badge ${verdict.verdict}" title="${esc(tip)}">${txt}${verdict.savings != null ? ' ' + fmt(verdict.savings) : ''}</span>`;
  }
  return `<div class="pl-node ${buy ? 'mode-buy' : 'mode-make'} ${isRoot ? 'is-root' : ''} ${active ? '' : 'inactive'}" style="left:${s.x || 0}px;top:${s.y || 0}px" data-id="${esc(it)}">
    <div class="pl-node-head" data-drag="1">${iconImg(it)}<div class="pl-nm">${esc(n.name_pt || it)}<span class="pl-te">${te}</span></div>${isRoot ? `<button class="pl-rm" data-rm="${esc(it)}" title="remover produto final">×</button>` : ''}</div>
    <div class="pl-node-qty">${qtyHtml}${badge}</div>
    <div class="pl-node-cost">${costLine}</div>
    <div class="pl-node-ctrl">${ctrl}</div>
  </div>`;
}

function prodRender() {
  const pl = state.prodLine, nodes = pl.graph.nodes || {}, ns = pl.ns;
  const nodesEl = $('plNodes'), edgesEl = $('plEdges');
  if (!nodesEl) return;
  let maxX = 0, maxY = 0;
  for (const it in nodes) { const s = ns[it] || {}; maxX = Math.max(maxX, (s.x || 0) + PL_NW); maxY = Math.max(maxY, (s.y || 0) + PL_NH); }
  const W = Math.max(maxX + 30, 640), H = Math.max(maxY + 30, 320);
  edgesEl.setAttribute('viewBox', `0 0 ${W} ${H}`);
  edgesEl.style.width = nodesEl.style.width = W + 'px';
  edgesEl.style.height = nodesEl.style.height = H + 'px';
  const calc = pl.calc || { demand: {}, crafts: {} };
  if (!Object.keys(nodes).length) {
    nodesEl.innerHTML = '<div class="pl-empty">Busque um produto final acima para montar a cadeia. Ex.: <b>espada 4.1</b>, <b>poção de cura</b>, <b>bolsa T6</b>.</div>';
    edgesEl.innerHTML = '';
    return;
  }
  nodesEl.innerHTML = Object.keys(nodes).map((it) => prodNodeHtml(it, calc.demand[it] || 0, calc.crafts[it] || 0)).join('');
  nodesEl.querySelectorAll('.pl-node').forEach((el) => prodWireNode(el));
  prodRenderEdges();
}

function prodRenderEdges() {
  const nodes = plNodes(), ns = state.prodLine.ns, svg = $('plEdges');
  if (!svg) return;
  let out = '';
  for (const it in nodes) {
    const n = nodes[it];
    if (!n.recipe || plIsBuy(it)) continue;            // só ramos que fabricam
    const ps = ns[it]; if (!ps) continue;
    const px = (ps.x || 0), py = (ps.y || 0) + PL_NH / 2;
    for (const inp of n.recipe) {
      const cs = ns[inp.id]; if (!cs) continue;
      const cx = (cs.x || 0) + PL_NW, cy = (cs.y || 0) + PL_NH / 2;
      const mx = (px + cx) / 2;
      out += `<path class="pl-edge" d="M ${cx} ${cy} C ${mx} ${cy} ${mx} ${py} ${px} ${py}"/>`;
      out += `<text class="pl-edge-lbl" x="${(cx + px) / 2}" y="${(cy + py) / 2 - 3}">${inp.count}×</text>`;
    }
  }
  svg.innerHTML = out;
}

function prodWireNode(el) {
  const id = el.dataset.id;
  el.querySelectorAll('.pl-tg').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const s = state.prodLine.ns[id] || (state.prodLine.ns[id] = prodDefaultState(id));
    const act = b.dataset.act;
    if (act === 'make') s.mode = 'make';
    else if (act === 'buy') s.mode = 'buy';
    else if (act === 'focus' && s.mode !== 'buy') s.focus = !s.focus;
    prodRecompute();
  }));
  const sel = el.querySelector('.pl-city');
  if (sel) sel.addEventListener('change', (e) => {
    (state.prodLine.ns[id] || (state.prodLine.ns[id] = prodDefaultState(id))).city = e.target.value;
    prodRecompute();
  });
  const tgt = el.querySelector('.pl-target');
  if (tgt) tgt.addEventListener('change', (e) => {
    state.prodLine.targets[id] = Math.max(1, +e.target.value || 1);
    prodRecompute();
  });
  const rm = el.querySelector('.pl-rm');
  if (rm) rm.addEventListener('click', (e) => { e.stopPropagation(); removeProdRoot(rm.dataset.rm); });
  const head = el.querySelector('[data-drag]');
  if (head) head.addEventListener('mousedown', (e) => prodStartDrag(e, el, id));
}

function prodStartDrag(e, el, id) {
  if (e.target.closest('button, select, input')) return;
  e.preventDefault();
  const s = state.prodLine.ns[id]; if (!s) return;
  const sx = e.clientX, sy = e.clientY, ox = s.x || 0, oy = s.y || 0;
  el.classList.add('dragging');
  const move = (ev) => {
    s.x = Math.max(0, ox + (ev.clientX - sx));
    s.y = Math.max(0, oy + (ev.clientY - sy));
    el.style.left = s.x + 'px'; el.style.top = s.y + 'px';
    prodRenderEdges();
  };
  const up = () => {
    el.classList.remove('dragging');
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
  };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

// chips de produto final (PASSO 1) — onde se digita a QUANTIDADE
function prodRenderProducts() {
  const el = $('plProducts'); if (!el) return;
  const pl = state.prodLine;
  if (!pl.roots.length) {
    el.innerHTML = '<div class="pl-empty-hint">↑ Busque um produto acima (ex.: <b>espada</b>, <b>poção de cura</b>, <b>bolsa</b>) e ele aparece aqui com o campo de quantidade.</div>';
    return;
  }
  el.innerHTML = pl.roots.map((id) => {
    const n = pl.graph.nodes[id] || {};
    const te = `T${n.tier || '?'}${n.enchant ? '.' + n.enchant : ''}`;
    return `<div class="pl-prod" data-id="${esc(id)}">
      ${iconImg(id)}
      <div class="pl-prod-nm">${esc(n.name_pt || id)}<span>${te}</span></div>
      <label class="pl-prod-qtylbl">fazer <input type="number" class="pl-prod-qty" min="1" value="${pl.targets[id] || 100}" data-id="${esc(id)}"> un.</label>
      <button class="pl-prod-rm" data-rm="${esc(id)}" title="remover produto">×</button>
    </div>`;
  }).join('');
  el.querySelectorAll('.pl-prod-qty').forEach((inp) => inp.addEventListener('change', (e) => {
    state.prodLine.targets[e.target.dataset.id] = Math.max(1, +e.target.value || 1);
    prodRecompute();
  }));
  el.querySelectorAll('.pl-prod-rm').forEach((b) =>
    b.addEventListener('click', () => removeProdRoot(b.dataset.rm)));
}

// cartões de resultado (lucro/ROI/custo…) no topo, bem visível
// setores da infraestrutura produtiva da guild (ordem: do produto ao bruto)
const PL_SECTORS = [
  { key: 'craft', icon: '🔨', label: 'Fabricação', hint: 'itens montados na estação de craft' },
  { key: 'refine', icon: '⚙️', label: 'Refino', hint: 'barras, tábuas, tecido, couro, pedra' },
  { key: 'farm', icon: '🌾', label: 'Ilha / Fazenda', hint: 'culturas, comida e produtos de animais' },
  { key: 'raw', icon: '🪓', label: 'Coleta', hint: 'recursos brutos (minério, madeira, fibra…)' },
  { key: 'buy', icon: '🛒', label: 'Comprar pronto', hint: 'insumos sem receita própria' },
];

// traduz a cadeia em INFRAESTRUTURA física da guild (fazendas, refino, craft)
function prodInfraHtml(calc) {
  const pl = state.prodLine, nodes = pl.graph.nodes || {};
  let farmRows = [], totalHarvests = 0, refineOps = 0, craftOps = 0, rawUnits = 0, animals = 0;
  for (const it in nodes) {
    const dem = Math.ceil((calc.demand || {})[it] || 0); if (dem <= 0) continue;
    const n = nodes[it], sec = n.sector;
    if (sec === 'craft') craftOps += calc.crafts[it] || 0;
    else if (sec === 'refine') refineOps += calc.crafts[it] || 0;
    else if (sec === 'raw') rawUnits += dem;
    else if (sec === 'farm') {
      if (n.farm && n.farm.yield) {
        const h = Math.ceil(dem / n.farm.yield);
        totalHarvests += h;
        farmRows.push({ name: n.name_pt || it, h, cyc: n.farm.cycle_days });
      } else animals += dem;
    }
  }
  const secs = [];
  if (farmRows.length) {
    farmRows.sort((a, b) => b.h - a.h);
    const detail = farmRows.map((f) => `${esc(f.name)}: <b>${fmt(f.h)}</b> colh.`).join(' · ');
    secs.push(`<div class="pl-infra-sec"><span class="pl-infra-ic">🌾</span><b>Fazenda</b> —
      <b>${fmt(totalHarvests)}</b> colheitas no total <span class="muted">(≈ ${fmt(totalHarvests)} canteiros p/ fazer tudo em 1 ciclo; 1 canteiro = 1 colheita/ciclo)</span>
      <div class="pl-infra-detail">${detail}</div></div>`);
  }
  if (animals) secs.push(`<div class="pl-infra-sec"><span class="pl-infra-ic">🐄</span><b>Pecuária</b> — ${fmt(animals)} produtos de animal</div>`);
  if (refineOps) secs.push(`<div class="pl-infra-sec"><span class="pl-infra-ic">⚙️</span><b>Refino</b> — <b>${fmt(refineOps)}</b> operações de refino na estação</div>`);
  if (craftOps) secs.push(`<div class="pl-infra-sec"><span class="pl-infra-ic">🔨</span><b>Fabricação</b> — <b>${fmt(craftOps)}</b> crafts na estação</div>`);
  if (rawUnits) secs.push(`<div class="pl-infra-sec"><span class="pl-infra-ic">🪓</span><b>Coleta</b> — <b>${fmt(rawUnits)}</b> unidades de recurso bruto</div>`);
  if (!secs.length) return '';
  return `<details class="pl-collapse pl-infra" open><summary>🏛️ Infraestrutura da guild para esta produção</summary>${secs.join('')}</details>`;
}

function prodRenderResult(calc) {
  const el = $('plResult'); if (!el) return;
  const pl = state.prodLine, nodes = pl.graph.nodes || {};
  if (!calc || !pl.roots.length) { el.innerHTML = ''; return; }
  const card = (v, k, cls = '') => `<div class="pl-statcard ${cls}"><div class="pl-stat-v">${v}</div><div class="pl-stat-k">${k}</div></div>`;
  // RESUMO DA INFRAESTRUTURA: quantas etapas em cada setor da guild
  const cnt = { craft: 0, refine: 0, farm: 0, raw: 0, buy: 0 };
  const gd = calc.gross || calc.demand || {};   // conta por BRUTO (igual à lista de etapas)
  for (const it in nodes) if ((gd[it] || 0) > 0) cnt[nodes[it].sector || 'buy'] = (cnt[nodes[it].sector || 'buy'] || 0) + 1;
  let infra = `<div class="pl-cards">
    ${card(cnt.craft, '🔨 Fabricação')}
    ${card(cnt.refine, '⚙️ Refino')}
    ${card(cnt.farm, '🌾 Ilha')}
    ${card(cnt.raw + cnt.buy, '🪓 Coleta/compra')}
    ${card(`${fmt(calc.focusPoints)} pts`, 'Foco total')}
  </div>`;
  // CUSTOS (opcional): só quando o usuário liga "preços de mercado"
  let costs = '', warns = '';
  if (pl.showCosts) {
    costs = `<div class="pl-cards pl-cost-cards">
      ${card(`<span class="silver">${fmt(calc.profit)}</span>`, 'Lucro líquido', 'big ' + (calc.profit >= 0 ? 'pos' : 'neg'))}
      ${card(calc.roi == null ? '—' : fmtPct(calc.roi), 'ROI')}
      ${card(`<span class="silver">${fmt(calc.revenue)}</span>`, 'Receita (venda)')}
      ${card(`<span class="silver">${fmt(calc.buyCost)}</span>`, 'Custo de compra')}
    </div>`;
    if (calc.missingSell && calc.missingSell.length)
      warns += `<div class="pl-warn">⚠ ${calc.missingSell.length} produto(s) final(is) sem preço de venda no cache.</div>`;
    if (calc.missing.length)
      warns += `<div class="pl-warn">${calc.missing.length} insumo(s) sem cotação de compra.</div>`;
  }
  el.innerHTML = infra + prodInfraHtml(calc) + costs + warns;
}

// PASSO 2, visão ETAPAS: agrupada por SETOR da infraestrutura (Fabricação /
// Refino / Ilha / Coleta / Comprar). Quantidade-primeiro; custos só com o toggle.
function prodRenderStepsList() {
  const el = $('plListView'); if (!el) return;
  const pl = state.prodLine, nodes = pl.graph.nodes || {}, calc = pl.calc || { demand: {}, crafts: {} };
  const show = pl.showCosts;
  const gross = calc.gross || calc.demand || {};
  const items = Object.keys(nodes).filter((it) => (gross[it] || 0) > 0);
  if (!items.length) { el.innerHTML = '<div class="pl-empty-hint">Escolha um produto para ver as etapas.</div>'; return; }
  let html = '';
  for (const sec of PL_SECTORS) {
    const list = items.filter((it) => (nodes[it].sector || 'buy') === sec.key)
      .sort((a, b) => (nodes[b].tier || 0) - (nodes[a].tier || 0) || a.localeCompare(b));
    if (!list.length) continue;
    const rows = list.map((it) => {
      const n = nodes[it], buy = plIsBuy(it), dem = Math.ceil(calc.demand[it] || 0);
      const isRoot = pl.roots.includes(it), canCraft = !(n.is_raw || !n.recipe);
      const st = (pl.stock || {})[it] || 0, covered = (gross[it] || 0) > 0 && dem === 0;
      const te = `T${n.tier || '?'}${n.enchant ? '.' + n.enchant : ''}`;
      const ctrl = canCraft ? `<div class="pl-rowctrl">
          <button class="pl-tg ${buy ? '' : 'on'}" data-act="make" data-id="${esc(it)}">Fabricar</button>
          <button class="pl-tg ${buy ? 'on' : ''}" data-act="buy" data-id="${esc(it)}">Comprar</button>
          <label class="pl-foco-chk" title="usar foco nesta etapa"><input type="checkbox" data-act="focus" data-id="${esc(it)}" ${(pl.ns[it] || {}).focus ? 'checked' : ''} ${buy ? 'disabled' : ''}> foco</label>
        </div>` : '<span class="pl-tag-buy">adquirir</span>';
      let costCell = '';
      if (show) {
        const v = (pl.verdict || {})[it], sh = (calc.shopping || {})[it];
        const rec = (v && v.verdict) ? `<span class="pl-badge ${v.verdict}">${v.verdict === 'make' ? 'fabricar' : 'comprar'}${v.savings != null ? ' +' + fmt(v.savings) : ''}</span>` : '';
        const c = buy ? (sh && sh.priced ? `<span class="silver">${fmt(sh.cost)}</span>` : '<span class="muted">—</span>')
                      : (calc.crafts[it] ? `<span class="muted">${fmt(calc.crafts[it])} crafts</span>` : '');
        costCell = `<td class="l">${rec}</td><td>${c}</td>`;
      }
      return `<tr class="${isRoot ? 'pl-row-root' : ''} ${covered ? 'pl-covered-row' : ''}">
        <td class="l"><div class="cell-item">${iconImg(it)}<div class="nm">${esc(n.name_pt || it)} <span class="muted">${te}</span></div></div></td>
        <td class="pl-qty">${covered ? '<span class="pl-covered">✓ em estoque</span>' : '<b>×' + fmt(dem) + '</b>'}</td>
        <td class="pl-stock"><input type="number" class="pl-stock-in" data-id="${esc(it)}" min="0" value="${st || ''}" placeholder="tenho 0" title="quanto você já tem em estoque"></td>
        <td class="l">${ctrl}</td>${costCell}</tr>`;
    }).join('');
    html += `<div class="pl-sector"><div class="pl-sector-head"><span class="pl-sector-ic">${sec.icon}</span>${sec.label}<span class="pl-sector-n">${list.length}</span><span class="pl-sector-hint">${sec.hint}</span></div>
      <table class="pl-steps"><tbody>${rows}</tbody></table></div>`;
  }
  el.innerHTML = html;
  el.querySelectorAll('.pl-tg').forEach((b) => b.addEventListener('click', () => {
    const it = b.dataset.id, s = state.prodLine.ns[it] || (state.prodLine.ns[it] = prodDefaultState(it));
    if (b.dataset.act === 'make') s.mode = 'make'; else if (b.dataset.act === 'buy') s.mode = 'buy';
    prodRecompute();
  }));
  el.querySelectorAll('input[data-act="focus"]').forEach((c) => c.addEventListener('change', () => {
    const it = c.dataset.id, s = state.prodLine.ns[it] || (state.prodLine.ns[it] = prodDefaultState(it));
    if (s.mode !== 'buy') s.focus = c.checked;
    prodRecompute();
  }));
  el.querySelectorAll('.pl-stock-in').forEach((inp) => inp.addEventListener('change', (e) => {
    const it = e.target.dataset.id, v = Math.max(0, +e.target.value || 0);
    if (v) state.prodLine.stock[it] = v; else delete state.prodLine.stock[it];
    prodRecompute();
  }));
}

// alterna entre a TABELA (padrão, fácil) e o DIAGRAMA visual
function prodRenderChainView() {
  const pl = state.prodLine, listEl = $('plListView'), graphEl = $('plGraphView');
  if (!listEl) return;
  if (pl.view === 'graph') {
    listEl.hidden = true; if (graphEl) graphEl.hidden = false;
    prodRender();
  } else {
    if (graphEl) graphEl.hidden = true; listEl.hidden = false;
    prodRenderStepsList();
  }
}

function prodSetAllFocus(on) {
  const pl = state.prodLine;
  for (const it in pl.graph.nodes) {
    const n = pl.graph.nodes[it];
    if (n.is_raw || !n.recipe) continue;
    const s = pl.ns[it] || (pl.ns[it] = prodDefaultState(it));
    if (s.mode !== 'buy') s.focus = on;
  }
  prodRecompute();
}

function prodRenderShopping(calc) {
  const wrap = $('plShopping'); if (!wrap) return;
  const pl = state.prodLine, show = pl.showCosts, nodes = pl.graph.nodes || {};
  if (!calc || !Object.keys(calc.shopping || {}).length) { wrap.innerHTML = ''; return; }
  const rows = Object.entries(calc.shopping).map(([id, s]) => ({
    id, name: (nodes[id] || {}).name_pt || id, qty: s.qty, unit: s.unit,
    cost: s.cost, city: s.city, priced: s.priced,
  })).sort((a, b) => show ? (b.cost || 0) - (a.cost || 0) : (b.qty || 0) - (a.qty || 0));
  const cols = [
    { key: 'it', label: 'Matéria-prima', align: 'l', value: (o) => o.name, html: (o) => `<div class="cell-item">${iconImg(o.id)}<div class="nm">${esc(o.name)}</div></div>` },
    { key: 'qty', label: 'Quantidade', value: (o) => o.qty, html: (o) => `<b>${fmt(o.qty)}</b>` },
  ];
  if (show) cols.push(
    { key: 'city', label: 'Comprar em', align: 'l', value: (o) => o.city || '', html: (o) => o.city ? cityHtml(o.city) : '—' },
    { key: 'unit', label: 'Preço un', value: (o) => o.unit || 0, html: (o) => o.priced ? fmt(o.unit) : '<span class="muted">—</span>' },
    { key: 'cost', label: 'Custo', value: (o) => o.cost || 0, html: (o) => o.priced ? `<span class="silver">${fmt(o.cost)}</span>` : '—' });
  wrap.innerHTML = `<h3 class="pl-sh-title">🛒 Matéria-prima a adquirir (${rows.length}) — recursos brutos + comprados prontos${show ? '' : ' · ative “Custos” p/ ver preços'}</h3><div class="table-wrap" id="plShopTable"></div>`;
  renderTable('plShopTable', cols, rows, { sortKey: show ? 'cost' : 'qty' });
}

function addProdRoot(id) {
  const pl = state.prodLine;
  if (pl.roots.includes(id)) { toast('esse produto já está na cadeia'); return; }
  if (pl.roots.length >= 12) { toast('máximo de 12 produtos finais'); return; }
  pl.roots.push(id); pl.targets[id] = pl.qtyDefault;
  prodFetchGraph();
}
function removeProdRoot(id) {
  const pl = state.prodLine;
  pl.roots = pl.roots.filter((r) => r !== id);
  delete pl.targets[id];
  prodFetchGraph();
}

async function prodSaveChain() {
  const pl = state.prodLine;
  if (!pl.roots.length) { toast('monte uma cadeia antes de salvar'); return; }
  const name = ($('plName').value || '').trim() || 'Cadeia sem nome';
  const payload = { v: 1, roots: pl.roots, targets: pl.targets, stock: pl.stock,
    qtyDefault: pl.qtyDefault, stationFee: pl.stationFee, specFce: pl.specFce,
    focusPrice: pl.focusPrice, ns: pl.ns };
  try {
    const r = await apiJson('/api/prodchain/chains', 'POST', { name, payload, id: pl.savedId || null });
    pl.savedId = r.id; pl.savedName = name;
    toast('cadeia salva na conta'); prodLoadList();
  } catch (e) { toast('erro ao salvar: ' + e.message); }
}
async function prodLoadList() {
  const sel = $('plSavedSel'); if (!sel) return;
  try {
    const r = await api('/api/prodchain/chains');
    sel.innerHTML = '<option value="">— abrir cadeia salva —</option>' +
      (r.chains || []).map((c) => `<option value="${c.id}" ${c.id === state.prodLine.savedId ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
  } catch (e) { /* sem login/sem acesso: ignora silenciosamente */ }
}
async function prodLoadChain(id) {
  try {
    const r = await api(`/api/prodchain/chains/${id}`);
    const p = r.chain.payload || {}, pl = state.prodLine;
    pl.roots = p.roots || [];
    // valida targets carregados (payload pode estar adulterado): qty > 0 finita
    pl.targets = {};
    for (const [k, v] of Object.entries(p.targets || {})) {
      const q = +v; if (q > 0 && isFinite(q)) pl.targets[k] = q;
    }
    pl.stock = {};   // estoque declarado (só qty > 0 finita; 0 = sem estoque)
    for (const [k, v] of Object.entries(p.stock || {})) {
      const q = +v; if (q > 0 && isFinite(q)) pl.stock[k] = q;
    }
    pl.qtyDefault = p.qtyDefault || 100; pl.stationFee = p.stationFee || 0;
    pl.specFce = p.specFce || 0; pl.focusPrice = p.focusPrice || 0;
    pl.ns = p.ns || {}; pl.savedId = r.chain.id; pl.savedName = r.chain.name;
    if ($('plName')) $('plName').value = r.chain.name;
    if ($('plStation')) $('plStation').value = pl.stationFee;
    if ($('plSpec')) $('plSpec').value = pl.specFce;
    if ($('plFocusPrice')) $('plFocusPrice').value = pl.focusPrice;
    if ($('plQty')) $('plQty').value = pl.qtyDefault;
    await prodFetchGraph();
  } catch (e) { toast('erro ao abrir: ' + e.message); }
}
async function prodDeleteSelected() {
  const id = +($('plSavedSel').value || 0);
  if (!id) { toast('selecione uma cadeia salva para excluir'); return; }
  try {
    await apiJson(`/api/prodchain/chains/${id}`, 'DELETE');
    if (state.prodLine.savedId === id) state.prodLine.savedId = null;
    toast('cadeia excluída'); prodLoadList();
  } catch (e) { toast('erro ao excluir: ' + e.message); }
}
function prodClear() {
  const view = state.prodLine ? state.prodLine.view : 'list';
  state.prodLine = prodInitState();
  state.prodLine.view = view || 'list';
  if ($('plName')) $('plName').value = '';
  if ($('plStation')) $('plStation').value = 0;
  if ($('plSpec')) $('plSpec').value = 0;
  if ($('plFocusPrice')) $('plFocusPrice').value = 0;
  prodRenderProducts(); prodRenderResult(null); prodRenderShopping(null);
  prodRenderChainView(); $('plStatus').textContent = ''; prodLoadList();
}

// liga os controles estáticos (HTML já está no index.html)
function prodWireControls() {
  const pl = state.prodLine;
  makeItemPicker('plPicker', (it) => addProdRoot(it.id),
                 'buscar produto… ex.: espada, poção de cura, bolsa');
  for (const [id, key] of [['plStation', 'stationFee'], ['plSpec', 'specFce'], ['plFocusPrice', 'focusPrice']]) {
    const inp = $(id); if (!inp) continue;
    inp.value = pl[key];
    inp.addEventListener('change', (e) => { state.prodLine[key] = Math.max(0, +e.target.value || 0); prodRecompute(); });
  }
  const vt = $('plViewTabs');
  if (vt) vt.querySelectorAll('.pl-vt').forEach((b) => b.addEventListener('click', () => {
    vt.querySelectorAll('.pl-vt').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    state.prodLine.view = b.dataset.view;
    prodRenderChainView();
  }));
  const sc = $('plShowCosts');
  if (sc) { sc.checked = !!pl.showCosts; sc.addEventListener('change', (e) => {
    state.prodLine.showCosts = e.target.checked;
    document.getElementById('tab-linhaProducao').classList.toggle('pl-costs-on', e.target.checked);
    prodRecompute();
  }); }
  if ($('plAllFocus')) $('plAllFocus').addEventListener('click', () => prodSetAllFocus(true));
  if ($('plNoFocus')) $('plNoFocus').addEventListener('click', () => prodSetAllFocus(false));
  if ($('plSaveBtn')) $('plSaveBtn').addEventListener('click', prodSaveChain);
  if ($('plDelBtn')) $('plDelBtn').addEventListener('click', prodDeleteSelected);
  if ($('plClearBtn')) $('plClearBtn').addEventListener('click', prodClear);
  if ($('plSavedSel')) $('plSavedSel').addEventListener('change', (e) => { if (e.target.value) prodLoadChain(+e.target.value); });
  if ($('plName')) $('plName').value = pl.savedName || '';
}

function initProdLine() {
  if (!state.prodLine) state.prodLine = prodInitState();
  state.prodLine.view = state.prodLine.view || 'list';
  prodWireControls();
  prodRenderProducts();
  prodRenderResult(null);
  prodRenderShopping(null);
  prodRenderChainView();
  prodLoadList();
}

// ============================================================ init
async function init() {
  state.meta = await api('/api/meta');
  premiumLabel();
  renderFavs();
  loadGold();
  loadStatus();
  pollCollectStatus();
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
  histCities = makeChips('histCities', cityOpts, {
    selected: state.meta.royal_cities, allToggle: true });

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
  document.querySelectorAll('#avView .chip').forEach((b) =>
    b.addEventListener('click', () => {
      document.querySelectorAll('#avView .chip').forEach((x) =>
        x.classList.toggle('on', x === b));
      loadAvancado(b.dataset.view);
    }));
  document.querySelectorAll('#avSubtabs button').forEach((b) =>
    b.addEventListener('click', () => showAvSub(b.dataset.av)));
  document.querySelectorAll('#islandSubtabs button').forEach((b) =>
    b.addEventListener('click', () => showIslandSub(b.dataset.isl)));
  [['#prodView', loadAvProd], ['#guildView', loadAvGuild],
   ['#logiView', loadAvLogi], ['#demandaView', loadAvDemanda],
   ['#riscoView', loadAvRisco]]
    .forEach(([sel, loader]) =>
      document.querySelectorAll(sel + ' .chip').forEach((b) =>
        b.addEventListener('click', () => {
          document.querySelectorAll(sel + ' .chip').forEach((x) =>
            x.classList.toggle('on', x === b));
          loader(b.dataset.view);
        })));
  document.querySelectorAll('#goldRange .chip').forEach((b) =>
    b.addEventListener('click', () => {
      document.querySelectorAll('#goldRange .chip').forEach((x) =>
        x.classList.toggle('on', x === b));
      loadGoldChart(+b.dataset.range);
    }));
  ['craftSellMode', 'craftFocus', 'craftSpec', 'craftDaily', 'craftFocusBudget',
   'craftFee', 'craftSourcing'].forEach((id) =>
    $(id).addEventListener('change', () => loadItemSub('craft', true)));
  ['wikiFocus', 'wikiQty'].forEach((id) =>
    $(id).addEventListener('change', () => loadItemSub('cadeia', true)));
  // navegação tipo-wiki: clicar em qualquer item abre a ficha dele (delegado,
  // sobrevive a re-render de tabela/árvore)
  document.addEventListener('click', (e) => {
    const a = e.target.closest('.wiki-link');
    if (a) { e.stopPropagation(); navigateToItem(a.dataset.id); }
  });
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

  loadDashboardRecommendations();
}

bootAuth();
