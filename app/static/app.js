'use strict';
// TeamBots web UI. No build step and no dependencies: plain DOM, one file.

// ---------------------------------------------------------------- helpers
const $ = (tag, props, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'data') Object.assign(e.dataset, v);
    else e[k] = v;
  }
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) e.append(k);
  return e;
};
const el = id => document.getElementById(id);
const clean = kids => kids.flat(Infinity).filter(k => k != null && k !== false);
const fill = (node, ...kids) => { node.replaceChildren(...clean(kids)); return node; };
const put = (node, ...kids) => { node.append(...clean(kids)); return node; };
const ICONS = {
  plus: 'M12 5v14M5 12h14', search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3', side: 'M4 5h16v14H4zM9.5 5v14',
  up: 'M12 19V5M6 11l6-6 6 6', down: 'M12 5v14M6 13l6 6 6-6', sliders: 'M4 7h10M18 7h2M4 17h2M10 17h10M16 4.5v5M8 14.5v5',
  at: 'M16 12a4 4 0 1 0-8 0 4 4 0 0 0 8 0zm0 0v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-3.5 7.1',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1', check: 'M5 12.5l4.5 4.5L19 7.5', chev: 'M6 9l6 6 6-6', x: 'M6 6l12 12M18 6L6 18',
  hash: 'M5 9h14M5 15h14M10 4L8 20M16 4l-2 16', pulse: 'M3 12h4l3-8 4 16 3-8h4',
  bot: 'M12 3v3M6 6h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2zM9 12v2M15 12v2',
  plug: 'M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4', user: 'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
  users: 'M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20a6.5 6.5 0 0 1 13 0M16 4.2a3.5 3.5 0 0 1 0 6.6M18 14.5a6.5 6.5 0 0 1 3.5 5.5',
  out: 'M9 4H5v16h4M15 8l4 4-4 4M19 12H9', sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5',
  term: 'M4 5h16v14H4zM8 10l3 2.5L8 15M13 15h3', file: 'M7 3h7l4 4v14H7zM14 3v4h4', key: 'M8 15a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 11h9M18 11v3M15 11v2',
  zap: 'M13 3L5 13h6l-1 8 8-10h-6z', grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  home: 'M4 11l8-7 8 7v9H15v-6h-6v6H4z', trash: 'M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13',
  play: 'M8 5l11 7-11 7z', clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2', coin: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12h8M12 8v8', screen: 'M3 5h18v11H3zM9 20h6M12 16v4',
  globe: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18', book: 'M5 4h13v16H7a2 2 0 0 1-2-2zM5 18a2 2 0 0 1 2-2h11',
  lock: 'M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3', edit: 'M4 20h4L19 9l-4-4L4 16zM13 7l4 4', stop: 'M7 7h10v10H7z', menu: 'M4 7h16M4 12h16M4 17h16', dots: 'M5 12h.01M12 12h.01M19 12h.01', pause: 'M9 6v12M15 6v12',
};
function icon(name, size = 18) {
  const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg'), p = document.createElementNS(ns, 'path');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size); s.setAttribute('class', 'ic'); s.setAttribute('aria-hidden', 'true');
  p.setAttribute('d', ICONS[name] || ICONS.dots); if (name === 'dots') p.setAttribute('stroke-width', '2.6');
  s.append(p); return s;
}
const ib = (name, title, onclick, size) => $('button', { className: 'ib', type: 'button', title, ariaLabel: title, onclick }, icon(name, size));
const avatar = (name, cls = '') => $('span', { className: 'av ' + cls, textContent: (name || '?').trim().charAt(0) || '?' });
/** The TeamBots mark (same shapes as icon.svg), drawn in the colours of the place it sits in. */
function logo() {
  const ns = 'http://www.w3.org/2000/svg', mk = (tag, attrs) => { const n = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const s = mk('svg', { viewBox: '86 94 340 324', width: 22, height: 22, 'aria-hidden': 'true' });
  s.append(mk('rect', { x: 196, y: 120, width: 204, height: 168, rx: 48, fill: 'none', stroke: 'currentColor', 'stroke-opacity': '.45', 'stroke-width': 26 }),
    mk('rect', { x: 98, y: 164, width: 280, height: 208, rx: 62, fill: 'var(--btn)' }), mk('path', { d: 'M141 330v66a8 8 0 0 0 13 6l62-46z', fill: 'currentColor' }),
    mk('rect', { x: 112, y: 178, width: 252, height: 180, rx: 48, fill: 'currentColor' }), mk('circle', { cx: 196, cy: 268, r: 21, fill: 'var(--btn)' }), mk('circle', { cx: 280, cy: 268, r: 21, fill: 'var(--btn)' }));
  return $('span', { className: 'logo' }, s);
}
const ts = s => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z');
const clock = d => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const dayName = d => { const t = new Date(), y = new Date(Date.now() - 864e5); return d.toDateString() === t.toDateString() ? 'Today' : d.toDateString() === y.toDateString() ? 'Yesterday' : d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' }); };
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } } };

const IDLE = { running: [], queued: [], delivery: {} };
const S = { state: IDLE, details: store.get('tb_details') === '1', token: store.get('tb_token'), me: null, teams: [], team: null, bots: [], channels: [], members: [], endpoints: [],
  conv: null, bot: null, page: 'projects', msgs: [], approvals: [], ctx: { skills: [], secrets: [], mem: null }, files: { team: 0, at: 0, list: [] }, drafts: {}, es: null };

async function api(method, path, body) {
  const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json', ...(S.token ? { Authorization: 'Bearer ' + S.token } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && S.me) { signOut(); throw new Error('Your session ended. Sign in again.'); }
  if (!r.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : (data.detail && data.detail.message) || r.statusText || 'Request failed'), { detail: data.detail });
  return data;
}
const withToken = path => '/api' + path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(S.token);
function toast(text, kind = '') {
  let box = el('toasts'); if (!box) document.body.append(box = $('div', { id: 'toasts' }));
  const t = $('div', { className: 'toast ' + kind, textContent: text, role: 'status' }); box.append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 6000 : 3200);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, 'bad'); } };
async function copyText(text, what = 'Copied') { try { await navigator.clipboard.writeText(text); toast(what); } catch { toast('Copying is blocked by the browser', 'bad'); } }

// ---------------------------------------------------------------- dialogs and menus
function openDialog(cls, ...kids) {
  const d = $('dialog', { className: 'dlg ' + cls }, ...kids);
  document.body.append(d);
  d.addEventListener('close', () => d.remove());
  d.addEventListener('mousedown', e => { if (e.target === d) d.close(); });
  d.showModal();
  return d;
}
/** A form in a dialog. Resolves with the values (or what onSubmit returns), or null when cancelled. */
function form({ title, intro, fields, ok = 'Save', onSubmit }) {
  return new Promise(resolve => {
    let result = null; const inputs = {}, err = $('div', { className: 'err', role: 'alert' });
    const values = () => { const v = {}; for (const f of fields) v[f.name] = f.type === 'checkbox' ? inputs[f.name].checked : inputs[f.name].value.trim(); return v; };
    const needed = (f, v) => typeof f.required === 'function' ? !!f.required(v) : !!f.required;
    const rows = fields.map(f => {
      let input;
      if (f.type === 'textarea') input = $('textarea', { rows: f.rows || 4, value: f.value || '', placeholder: f.placeholder || '' });
      else if (f.type === 'select') input = $('select', {}, f.options.map(o => $('option', { value: o.value, textContent: o.label, selected: o.value === f.value })));
      else if (f.type === 'checkbox') input = $('input', { type: 'checkbox', checked: !!f.value });
      else input = $('input', { type: f.type || 'text', value: f.value || '', placeholder: f.placeholder || '', autocomplete: f.type === 'password' ? 'new-password' : 'off', spellcheck: !f.mono });
      if (f.mono) input.classList.add('mono');
      if (f.required && typeof f.required !== 'function') input.required = true;
      inputs[f.name] = input;
      if (f.oninput) input.addEventListener('input', () => f.oninput(input.value, inputs));
      if (f.type === 'select') input.addEventListener('change', applyWhen);
      return f.type === 'checkbox'
        ? $('label', { className: 'check' }, input, $('span', {}, f.label, f.hint && $('small', { textContent: f.hint })))
        : $('label', { className: 'field' }, $('span', { textContent: f.label }), input, f.hint && $('small', { textContent: f.hint }));
    });
    function applyWhen() {
      const v = values();
      fields.forEach((f, i) => {
        const on = !f.when || !!f.when(v);
        rows[i].hidden = !on;
        inputs[f.name].required = on && needed(f, v);
      });
    }
    applyWhen();
    const submit = $('button', { className: 'btn primary', type: 'submit', textContent: ok });
    const fm = $('form', { className: 'pad' }, $('h2', { textContent: title }), intro && $('p', { className: 'intro', textContent: intro }), rows, err,
      $('div', { className: 'dfoot' }, $('button', { className: 'btn', type: 'button', textContent: 'Cancel', onclick: () => d.close() }), submit));
    fm.onsubmit = async e => {
      e.preventDefault();
      const v = values();
      submit.disabled = true; err.textContent = '';
      try { result = onSubmit ? ((await onSubmit(v)) ?? v) : v; d.close(); } catch (x) { err.textContent = x.message; submit.disabled = false; }
    };
    const d = openDialog('', fm);
    d.addEventListener('close', () => resolve(result));
    const first = fields.find(f => f.type !== 'checkbox' && f.type !== 'select' && (!f.when || f.when(values()))); if (first) inputs[first.name].focus();
  });
}
function confirmBox({ title, text, ok = 'Confirm', danger }) {
  return new Promise(resolve => {
    let yes = false;
    const d = openDialog('', $('div', { className: 'pad' }, $('h2', { textContent: title }), $('p', { className: 'intro', textContent: text }),
      $('div', { className: 'dfoot' }, $('button', { className: 'btn', textContent: 'Cancel', onclick: () => d.close() }),
        $('button', { className: 'btn primary' + (danger ? ' danger' : ''), textContent: ok, onclick: () => { yes = true; d.close(); } }))));
    d.addEventListener('close', () => resolve(yes));
  });
}
function closePop() { if (S.pop) { S.pop.remove(); S.pop = null; document.removeEventListener('mousedown', popOutside, true); } }
function popOutside(e) { if (S.pop && !S.pop.contains(e.target)) closePop(); }
function popmenu(anchor, items) {
  closePop();
  const m = $('div', { className: 'pop', role: 'menu' }, items.map(it => !it ? null : it.sep ? $('div', { className: 'sep' }) : it.head ? $('div', { className: 'pophead', textContent: it.head })
    : $('button', { className: 'popitem' + (it.danger ? ' danger' : ''), role: 'menuitem', onclick: () => { closePop(); it.onclick(); } }, it.icon && icon(it.icon, 16), $('span', { textContent: it.label }), it.hint && $('small', { textContent: it.hint }), it.checked && icon('check', 15))));
  document.body.append(m);
  const r = anchor.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + 'px';
  m.style.top = (r.bottom + 6 + h > innerHeight - 8 ? Math.max(8, r.top - h - 6) : r.bottom + 6) + 'px';
  S.pop = m; setTimeout(() => document.addEventListener('mousedown', popOutside, true), 0);
}

// ---------------------------------------------------------------- sign in
function signOut() { stopConv(); store.set('tb_token', null); S.token = null; S.me = null; closePop(); showAuth(); }
function showAuth() {
  let create = false;
  const u = $('input', { placeholder: 'Username', autocomplete: 'username', autocapitalize: 'none', required: true }), p = $('input', { type: 'password', placeholder: 'Password', autocomplete: 'current-password', required: true });
  const d = $('input', { placeholder: 'Display name', autocomplete: 'name' }), dWrap = $('label', { className: 'field', hidden: true }, d);
  const err = $('div', { className: 'err', role: 'alert' }), title = $('h1'), sub = $('p'), go = $('button', { className: 'btn primary', type: 'submit' }), swapText = $('span'), swapBtn = $('button', { type: 'button' });
  const paint = () => { title.textContent = create ? 'Create your account' : 'Welcome back'; sub.textContent = create ? 'Bots for your team, on your own model endpoint.' : 'Sign in to TeamBots.';
    go.textContent = create ? 'Create account' : 'Sign in'; dWrap.hidden = !create; p.placeholder = create ? 'Password (8+ characters)' : 'Password'; p.autocomplete = create ? 'new-password' : 'current-password';
    swapText.textContent = create ? 'Already have an account? ' : 'New here? '; swapBtn.textContent = create ? 'Sign in' : 'Create an account'; err.textContent = ''; };
  swapBtn.onclick = () => { create = !create; paint(); u.focus(); };
  const swap = $('div', { className: 'swap' }, swapText, swapBtn), providers = $('div', { className: 'providers' });
  const fm = $('form', { className: 'authbox' }, logo(), title, sub, $('label', { className: 'field' }, u), $('label', { className: 'field' }, p), dWrap, err, go, providers, swap);
  fm.onsubmit = async e => { e.preventDefault(); go.disabled = true;
    try { const r = await api('POST', create ? '/register' : '/login', { username: u.value.trim().toLowerCase(), password: p.value, display_name: d.value.trim() }); S.token = r.token; store.set('tb_token', r.token); await boot(); }
    catch (x) { err.textContent = x.message; go.disabled = false; } };
  // Sign in with GitHub or Google, when configured. A new person pastes the invite code they were given.
  (async () => { try { const info = await api('GET', '/auth/providers'); if (!info.providers || !info.providers.length) return;
    const inv = $('input', { placeholder: 'Invite code (new? paste it here)', autocapitalize: 'none', autocomplete: 'off' });
    providers.append($('div', { className: 'authsep' }, $('span', { textContent: 'or' })),
      ...info.providers.map(p => $('button', { className: 'btn', type: 'button', textContent: 'Continue with ' + p.name,
        onclick: () => { const code = inv.value.trim(); location.href = '/api/auth/' + p.id + '/start' + (code ? '?invite=' + encodeURIComponent(code) : ''); } })),
      $('label', { className: 'field', style: 'margin-top:6px' }, inv));
  } catch (e) { /* providers not configured: password sign-in only */ } })();
  paint(); if (S.authError) { err.textContent = S.authError; S.authError = null; }
  document.body.replaceChildren($('div', { id: 'auth' }, fm)); u.focus();
}

// ---------------------------------------------------------------- shell and sidebar
async function boot() {
  if (!S.token) return showAuth();
  try { S.me = await api('GET', '/me'); } catch { S.token = null; store.set('tb_token', null); return showAuth(); }
  S.teams = await api('GET', '/teams');
  S.team = S.teams.find(t => String(t.id) === store.get('tb_team')) || S.teams[0] || null;
  stopConv(); S.conv = null; S.bot = null;
  document.body.replaceChildren($('div', { id: 'app', className: store.get('tb_side') === '0' ? 'noside' : '' },
    $('aside', { id: 'side' }), $('div', { id: 'scrim', onclick: () => el('app').classList.remove('sideopen') }),
    $('main', { id: 'main' }, $('header', { id: 'top' }), $('div', { id: 'stage' }, $('div', { id: 'view' }, $('div', { id: 'col' })), $('div', { id: 'dock' }))),
    $('aside', { id: 'drawer' })));
  el('view').addEventListener('scroll', updateJump, { passive: true });
  el('col').addEventListener('click', e => { const s = e.target.closest('summary'); if (s) s.parentElement.dataset.user = s.parentElement.open ? '0' : '1'; });
  await loadTeam();
  if (store.get('tb_last_home') === '1') {
    try { return await openOverview(); } catch { /* fall through */ }
  }
  const last = S.team && store.get('tb_last_' + S.team.id), [kind, id] = (last || '').split(':');
  const bot = S.bots.find(b => b.id === +id), ch = S.channels.find(c => c.id === +id);
  try { if (kind === 'dm' && bot) return await openBot(bot); if (kind === 'feed' && bot) return await openFeed(bot); if (kind === 'ch' && ch) return await openConv(ch, null); } catch { /* fall through to projects */ }
  await showHome();
}
async function loadTeam() {
  if (S.team) [S.bots, S.channels, S.members, S.endpoints] = await Promise.all(['bots', 'channels', 'members', 'endpoints'].map(x => api('GET', `/teams/${S.team.id}/${x}`)));
  else S.bots = S.channels = S.members = S.endpoints = [];
  S.botCosts = {};
  if (S.me && S.me.can_view_costs) {
    try {
      const c = await api('GET', '/costs/bots?period=30d');
      for (const b of c.bots || []) S.botCosts[b.id] = b;
    } catch { /* cost badges are optional */ }
  }
  renderSide();
}
const switchTeam = guard(async id => { store.set('tb_team', String(id)); await boot(); });
const isNarrow = () => matchMedia('(max-width: 860px)').matches;
function toggleSide() { const a = el('app'); if (isNarrow()) a.classList.toggle('sideopen'); else { a.classList.toggle('noside'); store.set('tb_side', a.classList.contains('noside') ? '0' : '1'); } renderTop(); }
const sideHidden = () => isNarrow() || el('app').classList.contains('noside');
const afterNav = () => el('app') && el('app').classList.remove('sideopen');
const botTag = b => !b.is_team ? null : !b.published ? $('span', { className: 'tag warn', textContent: 'draft' }) : $('span', { className: 'tag', textContent: 'team' });
const botCostTag = b => {
  const c = S.botCosts && S.botCosts[b.id];
  if (!c) return null;
  const label = (!c.cost || c.cost.amount == null) && c.unknown_calls ? 'cost ?' : (c.cost && c.cost.amount != null ? fmtMoney(c.cost) : null);
  return label ? $('span', { className: 'tag', title: 'Last 30 days', textContent: label }) : null;
};
function fmtMoney(d) {
  if (!d || d.amount == null) return 'Price unknown';
  const sym = d.currency === 'EUR' ? '€' : '$';
  const n = Number(d.amount), a = Math.abs(n);
  const digits = a >= 100 ? 2 : a >= 1 ? 3 : a > 0 ? 4 : 2;
  return sym + n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function modelCombo({ value, placeholder, getEndpointId }) {
  const input = $('input', { value: value || '', placeholder: placeholder || '', className: 'mono', autocomplete: 'off', spellcheck: false });
  const list = $('div', { className: 'combo-list', hidden: true });
  const status = $('small', { className: 'combo-status' });
  const box = $('div', { className: 'combo' }, input, list);
  let models = [], open = false;
  const idsOf = arr => (arr || []).map(m => typeof m === 'string' ? m : (m && m.id)).filter(Boolean);
  const paint = () => {
    const q = input.value.trim().toLowerCase();
    const ids = idsOf(models);
    const def = { id: '', label: 'Default — the endpoint’s default model' };
    const rows = [def, ...ids.map(id => ({ id, label: id }))].filter(r => !q || r.label.toLowerCase().includes(q) || !r.id);
    fill(list, rows.length ? rows.map(r => $('button', { type: 'button', className: 'combo-opt' + (r.id === input.value.trim() ? ' on' : ''), onclick: ev => { ev.preventDefault(); input.value = r.id; close(); input.dispatchEvent(new Event('change')); } }, r.label)) : $('div', { className: 'combo-empty', textContent: q ? 'No match — you can still use this model id.' : 'No catalog yet. Type a model id.' }));
  };
  const close = () => { open = false; list.hidden = true; };
  const load = async () => {
    const id = getEndpointId && getEndpointId();
    if (!id) { models = []; status.textContent = ''; paint(); return; }
    const ep = [...(S.endpoints || []), ...((S.home && S.home.endpoints) || [])].find(e => e.id === +id);
    if (ep && ep.models && ep.models.length) { models = ep.models; status.textContent = ep.models.length + ' models from the last catalog. You can also type another id.'; paint(); }
    else status.textContent = 'Loading models…';
    try {
      const r = await api('GET', `/endpoints/${id}/models`);
      models = r.models || [];
      if (ep) ep.models = models;
      status.textContent = models.length ? (models.length + ' models from the endpoint. You can also type another id.') : (r.detail && r.detail !== 'ok' ? r.detail : 'No catalog from this endpoint — type a model id.');
    } catch (e) {
      if (!models.length) status.textContent = e.message || 'Could not list models. Type a model id.';
    }
    paint();
  };
  input.addEventListener('focus', () => { open = true; list.hidden = false; load(); });
  input.addEventListener('input', () => { if (open) paint(); });
  input.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  document.addEventListener('mousedown', e => { if (open && !box.contains(e.target)) close(); });
  return { box, input, status, reload: load };
}


function renderSide() {
  const side = el('side'); if (!side) return;
  const t = S.team, c = S.conv, sec = (label, title, add) => $('div', { className: 'sec' }, label, add && ib('plus', title, guard(add), 15));
  const item = (on, onclick, lead, text, ...tail) => $('div', { className: 'item' + (on ? ' on' : ''), role: 'button', tabIndex: 0, onclick: guard(onclick), onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) e.currentTarget.click(); } }, lead, $('span', { className: 'grow', textContent: text }), tail);
  const list = $('div', { className: 'scroll' });
  if (t) {
    list.append(sec('Bots', 'New bot', newBot));
    if (!S.bots.length) list.append($('div', { className: 'emptyhint', textContent: S.endpoints.length ? 'No bots yet.' : 'Add an endpoint, then create a bot.' }));
    const more = items => ib('dots', 'Rename and more', e => { e.stopPropagation(); popmenu(e.currentTarget, items); }, 15);
    for (const b of S.bots) list.append(item(c && c.kind === 'dm' && c.bot_id === b.id, () => openBot(b), avatar(b.name, 'bot'), b.name, botTag(b), botCostTag(b),
      more([b.role !== 'collaborator' && { icon: 'edit', label: 'Rename', onclick: guard(() => renameBot(b)) }, { icon: 'sliders', label: 'Settings', onclick: guard(async () => { await openBot(b); openDrawer(b); }) }])));
    list.append(sec('Channels', 'New channel', newChannel));
    if (!S.channels.length) list.append($('div', { className: 'emptyhint', textContent: 'Work with people and bots together.' }));
    for (const ch of S.channels) list.append(item(c && c.id === ch.id, () => openConv(ch, null), $('span', { className: 'lead' }, icon('hash', 16)), ch.name,
      more([{ icon: 'edit', label: 'Rename', onclick: guard(() => renameChannel(ch)) }, { icon: 'users', label: 'People and bots', onclick: guard(async () => { await openConv(ch, null); await channelMembers(); }) }])));
    const feeds = S.bots.filter(b => b.is_team && b.published);
    if (feeds.length) list.append(sec('Activity'));
    for (const b of feeds) list.append(item(c && c.kind === 'feed' && c.bot_id === b.id, () => openFeed(b), $('span', { className: 'lead' }, icon('pulse', 16)), b.name));
    list.append(sec('Endpoints', 'New endpoint', newEndpoint));
    for (const e of S.endpoints) list.append(item(false, () => endpointDialog(e), $('span', { className: 'lead' }, icon('plug', 16)), e.name, $('span', { className: 'tag', textContent: e.default_model })));
  }
  side.replaceChildren(
    $('div', { className: 'brand' }, logo(), $('strong', { textContent: 'TeamBots' }), ib('side', 'Hide sidebar', toggleSide)),
    $('button', { className: 'nav', onclick: openPalette }, icon('search'), $('span', { className: 'grow', textContent: 'Search' }), $('kbd', { textContent: 'Ctrl K' })),
    $('button', { className: 'nav' + (c && c.kind === 'home' ? ' on' : ''), onclick: guard(openOverview) }, icon('home'), $('span', { className: 'grow', textContent: 'Overview' })),
    $('button', { className: 'nav' + (!c && S.page === 'projects' ? ' on' : ''), onclick: guard(showHome) }, icon('grid'), $('span', { className: 'grow', textContent: 'Projects' })),
    $('button', { className: 'nav' + (!c && S.page === 'routines' ? ' on' : ''), onclick: guard(showRoutines) }, icon('clock'), $('span', { className: 'grow', textContent: 'Routines' })),
    S.me && S.me.can_view_costs && $('button', { className: 'nav' + (!c && S.page === 'costs' ? ' on' : ''), onclick: guard(showCosts) }, icon('coin'), $('span', { className: 'grow', textContent: 'Costs' })),
    $('button', { className: 'project', title: 'Switch project', onclick: e => projectMenu(e.currentTarget) }, $('span', { className: 'pname' }, $('small', { textContent: t ? (t.client || 'Project') : 'No project yet' }), $('b', { textContent: t ? t.name : 'Create a project' })), icon('chev', 16)),
    list,
    $('button', { className: 'me', onclick: e => userMenu(e.currentTarget) }, avatar(S.me.display_name), $('span', { className: 'grow', textContent: S.me.display_name }), icon('dots', 16)));
}
function projectMenu(anchor) {
  popmenu(anchor, [
    S.teams.length && { head: 'Projects' },
    ...S.teams.map(t => ({ icon: 'grid', label: (t.client ? t.client + ' / ' : '') + t.name, checked: S.team && t.id === S.team.id, onclick: () => switchTeam(t.id) })),
    S.teams.length && { sep: true },
    S.team && { icon: 'users', label: 'People and invite code', onclick: guard(projectDialog) },
    { icon: 'plus', label: 'New project', onclick: guard(newProject) },
    { icon: 'key', label: 'Join with an invite code', onclick: guard(joinProject) }]);
}
function userMenu(anchor) {
  const cur = store.get('tb_theme') || 'system', set = v => () => { store.set('tb_theme', v === 'system' ? null : v); if (v === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = v; };
  popmenu(anchor, [{ head: 'Signed in as ' + S.me.username }, { head: 'Theme' },
    ...['system', 'dark', 'light'].map(v => ({ icon: 'sun', label: v[0].toUpperCase() + v.slice(1), checked: cur === v, onclick: set(v) })),
    { sep: true }, { icon: 'term', label: 'Show what bots do in detail', hint: S.details ? 'on' : 'off', checked: S.details, onclick: () => { S.details = !S.details; store.set('tb_details', S.details ? '1' : null); for (const d of document.querySelectorAll('#col details[data-user]')) delete d.dataset.user; if (S.conv) renderConv(); } },
    S.install && { icon: 'down', label: 'Install TeamBots as an app', onclick: async () => { const ev = S.install; S.install = null; ev.prompt(); await ev.userChoice.catch(() => null); } },
    { sep: true }, { icon: 'key', label: 'API tokens for voice', onclick: guard(tokenDialog) },
    { sep: true }, { icon: 'out', label: 'Sign out', onclick: signOut }]);
}
async function tokenDialog() {
  let d;
  const again = guard(async () => { if (d) d.close(); await tokenDialog(); });
  const rows = await api('GET', '/tokens');
  const item = r => {
    const when = r.created_at ? new Date(r.created_at).toLocaleString() : '';
    const used = r.last_used_at ? 'Last used ' + new Date(r.last_used_at).toLocaleString() : 'Not used yet';
    return $('div', { className: 'field', style: 'margin:10px 0' },
      $('span', {}, r.name + (r.revoked ? ' (revoked)' : ''), $('small', { textContent: r.prefix + '… · ' + r.scope + ' · created ' + when + ' · ' + used })),
      r.revoked ? null : $('button', { className: 'btn sm danger', textContent: 'Revoke', onclick: guard(async () => {
        if (!await confirmBox({ title: 'Revoke this token?', text: 'Anything using it (Grok Bot voice, a Tesla, a script) will stop working.', ok: 'Revoke', danger: true })) return;
        await api('DELETE', '/tokens/' + r.id); again();
      })}));
  };
  d = openDialog('wide', $('div', { className: 'pad' },
    $('h2', { textContent: 'API tokens for voice' }),
    $('p', { className: 'intro', textContent: 'Create a token after you have signed in, then paste it into Grok Bot voice or a Tesla voice agent as a Bearer token. It can only talk to your Overview bot. The secret is shown once.' }),
    rows.length ? rows.map(item) : $('p', { className: 'intro', textContent: 'No tokens yet.' }),
    $('div', { className: 'dfoot' },
      $('button', { className: 'btn primary', textContent: 'New token', onclick: guard(async () => {
        const r = await form({ title: 'New API token', ok: 'Create token', intro: 'Give it a name you will recognise, for example “Tesla” or “Grok voice”.', fields: [{ name: 'name', label: 'Name', required: true, value: 'Voice' }],
          onSubmit: v => api('POST', '/tokens', { name: v.name, scope: 'home-bot' }) });
        if (!r || !r.token) return;
        await form({ title: 'Copy this token now', ok: 'I have copied it', intro: 'This is the only time the full token is shown. Store it in the voice assistant. If you lose it, revoke it and create a new one.', fields: [{ name: 'token', label: 'Token', value: r.token, mono: true }] });
        again();
      }) }),
      $('button', { className: 'btn', textContent: 'Done', onclick: () => d.close() }))));
}
// ---------- renaming
async function renameBot(b) {
  const r = await form({ title: 'Rename bot', fields: [{ name: 'name', label: 'Name', required: true, value: b.name },
    { name: 'handle', label: 'Handle', required: true, mono: true, value: b.handle, hint: 'Changing the handle changes how people and bots mention it. Earlier messages keep the old @handle.' }],
    onSubmit: v => api('PATCH', `/bots/${b.id}`, v) });
  if (!r) return;
  Object.assign(b, r); if (S.bot && S.bot.id === b.id) Object.assign(S.bot, r);
  if (S.conv && S.conv.bot_id === b.id) S.conv.name = S.conv.kind === 'feed' ? b.name + ' activity' : b.name;
  renderSide(); renderTop(); if (S.conv && S.conv.bot_id === b.id && S.conv.kind === 'dm') renderDock();
  if (el('drawer').classList.contains('open') && S.bot && S.bot.id === b.id) openDrawer(S.bot);
}
async function renameChannel(ch) {
  const r = await form({ title: 'Rename channel', fields: [{ name: 'name', label: 'Name', required: true, value: ch.name }], onSubmit: v => api('PATCH', `/conversations/${ch.id}`, v) });
  if (!r) return;
  ch.name = r.name; if (S.conv && S.conv.id === ch.id) S.conv.name = r.name;
  renderSide(); renderTop();
}
async function renameProject() {
  const t = S.team;
  const v = await form({ title: 'Rename project', fields: [{ name: 'name', label: 'Project name', required: true, value: t.name }, { name: 'client', label: 'Client (optional)', value: t.client }], onSubmit: async x => { await api('PATCH', `/teams/${t.id}`, x); } });
  if (!v) return;
  Object.assign(t, v); renderSide(); if (!S.conv) showHome();
}
function isCopilot(v) { return (v.provider || 'openai') === 'github_copilot'; }
function endpointFields(e) {
  return [
    { name: 'name', label: 'Name', required: true, value: e ? e.name : '', placeholder: 'dgx-spark' },
    { name: 'provider', label: 'Provider', type: 'select', value: (e && e.provider) || 'openai', options: [
      { value: 'openai', label: 'OpenAI-compatible' },
      { value: 'github_copilot', label: 'GitHub Copilot' }] },
    { name: 'base_url', label: 'Base URL', required: true, value: (e && e.base_url) || 'http://localhost:8000/v1', mono: true, when: v => !isCopilot(v) },
    { name: 'github_domain', label: 'GitHub domain', required: true, value: (e && e.github_domain) || 'github.com', mono: true,
      hint: 'github.com, or a data-residency tenant such as org.ghe.com.', when: isCopilot },
    { name: 'default_model', label: 'Default model', required: true, value: e ? e.default_model : '', mono: true, placeholder: 'claude-sonnet-5' },
    { name: 'api_key', label: e ? 'New token / API key' : 'Token / API key', type: 'password', required: v => !e && isCopilot(v),
      hint: e && e.has_key ? 'Leave empty to keep the stored key.' : 'For Copilot, a GitHub OAuth token (ghu_…). Otherwise optional. Stored encrypted, never shown again.' },
    { name: 'shared', label: 'Share with the project', type: 'checkbox', value: e && e.shared, hint: 'Other members can use this endpoint for their bots.' },
  ];
}
function endpointPayload(v) {
  if (!v.api_key) delete v.api_key;
  if (isCopilot(v)) delete v.base_url; else delete v.github_domain;
  return v;
}
async function editEndpoint(e) {
  const r = await form({ title: 'Edit endpoint', fields: endpointFields(e),
    onSubmit: v => api('PATCH', `/endpoints/${e.id}`, endpointPayload(v)) });
  if (r) { await loadTeam(); toast('Saved'); }
}
async function newProject() {
  await form({ title: 'New project', intro: 'A project is a workspace with its own people, bots and one shared computer.', ok: 'Create project',
    fields: [{ name: 'name', label: 'Project name', required: true, placeholder: 'Website relaunch' }, { name: 'client', label: 'Client (optional)', placeholder: 'Groups projects on the home page' }],
    onSubmit: async v => { const t = await api('POST', '/teams', v); store.set('tb_team', String(t.id)); await boot(); } });
}
async function joinProject() {
  await form({ title: 'Join a project', intro: 'Ask a project admin for the invite code.', ok: 'Join', fields: [{ name: 'invite_code', label: 'Invite code', required: true, mono: true }],
    onSubmit: async v => { const t = await api('POST', '/teams/join', v); store.set('tb_team', String(t.id)); await boot(); } });
}
function projectDialog() {
  const t = S.team, admin = t.role === 'admin';
  const sharing = $('select', { onchange: guard(async () => { await api('PATCH', `/teams/${t.id}`, { bot_sharing: sharing.value }); t.bot_sharing = sharing.value; toast('Saved'); }) },
    $('option', { value: 'everyone', textContent: 'Every member can publish Team Bots', selected: t.bot_sharing === 'everyone' }), $('option', { value: 'admins', textContent: 'Only admins can publish Team Bots', selected: t.bot_sharing === 'admins' }));
  const d = openDialog('', $('div', { className: 'pad' }, $('h2', { textContent: t.name }), $('p', { className: 'intro', textContent: (t.client ? t.client + ' · ' : '') + `${S.members.length} ${S.members.length === 1 ? 'person' : 'people'}` }),
    admin && $('div', { className: 'field' }, $('span', { textContent: 'Invite code' }), $('div', { className: 'copyrow' }, $('code', { textContent: t.invite_code }), $('button', { className: 'btn sm', textContent: 'Copy', onclick: () => copyText(t.invite_code, 'Invite code copied') }))),
    admin && $('label', { className: 'field' }, $('span', { textContent: 'Bot sharing' }), sharing),
    $('div', { className: 'field' }, $('span', { textContent: 'People' }), S.members.map(m => $('div', { className: 'li' }, avatar(m.display_name), $('div', { className: 'tx' }, m.display_name, $('small', { textContent: '@' + m.username })), m.role === 'admin' && $('span', { className: 'tag', textContent: 'admin' })))),
    $('div', { className: 'dfoot' }, admin && $('button', { className: 'btn', textContent: 'Rename', onclick: () => { d.close(); guard(renameProject)(); } }), $('button', { className: 'btn primary', textContent: 'Done', onclick: () => d.close() }))));
}

// ---------------------------------------------------------------- home
const topLead = () => sideHidden() ? [ib('menu', 'Show sidebar', toggleSide)] : [];
async function showHome() {
  store.set('tb_last_home', null);
  stopConv(); closeDrawer(); S.conv = null; S.bot = null; S.page = 'projects'; if (S.team) store.set('tb_last_' + S.team.id, null);
  renderSide(); afterNav(); renderTop();
  const h = new Date().getHours(), col = el('col');
  const page = $('div', { className: 'home', data: { key: 'home' } }, $('h1', { textContent: `Good ${h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'}, ${S.me.display_name.split(' ')[0]}` }));
  col.replaceChildren(page);
  if (!S.team) {
    put(page, $('p', { className: 'muted', textContent: 'Start by creating a project, or join one with an invite code.' }),
      $('div', { className: 'rowbtns' }, $('button', { className: 'btn primary', onclick: guard(newProject) }, icon('plus', 16), 'New project'), $('button', { className: 'btn', onclick: guard(joinProject) }, 'Join with a code')));
    return;
  }
  put(page, $('p', { className: 'muted', textContent: (S.team.client ? S.team.client + ' / ' : '') + S.team.name }));
  const todo = (n, done, title, text, label, fn) => $('div', { className: 'todo' + (done ? ' done' : '') }, $('span', { className: 'n' }, done ? icon('check', 14) : String(n)), $('div', { className: 'tt' }, title, $('small', { textContent: text })), !done && $('button', { className: 'btn sm', textContent: label, onclick: guard(fn) }));
  if (!S.endpoints.length || !S.bots.length) put(page, $('h2', { textContent: 'Get started' }),
    todo(1, S.endpoints.length > 0, 'Connect a model endpoint', 'Any OpenAI-compatible /v1 endpoint: vLLM, Ollama, LiteLLM.', 'Add endpoint', newEndpoint),
    todo(2, S.bots.length > 0, 'Create your first bot', 'A personal bot, or a Team Bot the whole project can use.', 'New bot', newBot));
  if (S.bots.length) put(page, $('h2', { textContent: 'Chat with a bot' }), $('div', { className: 'cards' }, S.bots.map(b => $('button', { className: 'card', onclick: guard(() => openBot(b)) },
    $('div', { className: 'hd' }, avatar(b.name, 'bot'), $('span', { className: 'grow', textContent: b.name }), botTag(b)), $('div', { className: 'ds', textContent: b.instructions.trim().split('\n')[0] || '@' + b.handle })))));
  const projects = await api('GET', '/overview'); if (S.conv || !page.isConnected) return;
  put(page, $('h2', { textContent: 'All projects' }), $('div', { className: 'cards' }, projects.map(p => $('button', { className: 'card', onclick: () => p.id === S.team.id ? null : switchTeam(p.id) },
    $('div', { className: 'hd' }, $('span', { className: 'grow', textContent: p.name }), p.id === S.team.id && $('span', { className: 'tag ok', textContent: 'open' })),
    $('div', { className: 'ds', textContent: `${p.client || 'No client'} · ${p.people} ${p.people === 1 ? 'person' : 'people'} · ${p.bots.length ? p.bots.map(b => '@' + b.handle).join(' ') : 'no bots'}` }),
    p.activity.slice(0, 2).map(a => $('div', { className: 'act', textContent: `${a.bot}: ${a.content}` }))))));
}


function editRoutine(r, after) {
  return form({ title: 'Edit routine', ok: 'Save', fields: [
    { name: 'name', label: 'Name', required: true, value: r.name },
    { name: 'cron', label: 'Schedule (crontab)', required: true, value: r.cron, mono: true, hint: (r.cron_text ? r.cron_text + '. ' : '') + 'minute hour day month weekday, in Europe/Amsterdam.' },
    { name: 'prompt', label: 'What should the bot do each time?', type: 'textarea', required: true, value: r.prompt }],
    onSubmit: v => api('PATCH', `/routines/${r.id}`, v) }).then(v => { if (v) { toast('Saved'); if (after) after(); } return v; });
}
async function showRoutines() {
  store.set('tb_last_home', null);
  stopConv(); closeDrawer(); S.conv = null; S.bot = null; S.page = 'routines'; if (S.team) store.set('tb_last_' + S.team.id, null);
  renderSide(); afterNav(); renderTop();
  const page = $('div', { className: 'home routines' }, $('h1', { textContent: 'Routines' }));
  el('col').replaceChildren(page);
  const rows = await api('GET', '/routines');
  if (S.page !== 'routines' || S.conv) return;
  if (!rows.length) {
    put(page, $('p', { className: 'muted', textContent: 'You have no routines yet. Open a bot, go to Routines in its settings, and schedule one. They run as you, in the conversation where you created them.' }));
    return;
  }
  const again = () => { if (S.page === 'routines' && !S.conv) showRoutines(); };
  const groups = new Map();
  for (const r of rows) { const key = r.project || 'Other'; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(r); }
  for (const [project, list] of groups) {
    put(page, $('h2', { textContent: project }));
    for (const r of list) {
      const on = $('input', { type: 'checkbox', checked: r.enabled, onchange: guard(async () => { await api('PATCH', `/routines/${r.id}`, { enabled: on.checked }); again(); }) });
      const where = ['@' + r.bot_handle, r.conversation_name].filter(Boolean).join(' · ');
      put(page, $('div', { className: 'routine' + (r.enabled ? '' : ' off') },
        $('div', { className: 'tx' }, r.name, $('small', { textContent: where })),
        $('div', { className: 'meta' },
          $('div', { textContent: `${r.cron_text || r.cron} (${r.cron})` }),
          $('div', { textContent: `Next: ${r.enabled ? (r.next_run_text || '—') : 'paused'} · Last: ${r.last_run_text || 'never'}` }),
          r.prompt && $('div', { className: 'prompt', textContent: r.prompt })),
        $('div', { className: 'acts' },
          $('label', { className: 'switch', title: r.enabled ? 'Enabled' : 'Paused' }, on),
          ib('play', 'Run now', guard(async () => { await api('POST', `/routines/${r.id}/run`); toast('Started'); }), 15),
          ib('edit', 'Edit', guard(() => editRoutine(r, again)), 15),
          ib('trash', 'Remove', guard(async () => {
            if (!await confirmBox({ title: 'Remove this routine?', text: 'This cannot be undone.', ok: 'Remove', danger: true })) return;
            await api('DELETE', `/routines/${r.id}`); again();

}

async function showCosts() {
  store.set('tb_last_home', null);
  stopConv(); closeDrawer(); S.conv = null; S.bot = null; S.page = 'costs'; if (S.team) store.set('tb_last_' + S.team.id, null);
  renderSide(); afterNav(); renderTop();
  const page = $('div', { className: 'home costs' }, $('h1', { textContent: 'Costs' }),
    $('p', { className: 'muted', textContent: 'What the models actually used, with official prices where we have them. Unknown models are not guessed.' }));
  el('col').replaceChildren(page);
  let period = store.get('tb_costs_period') || '30d';
  let teamFilter = store.get('tb_costs_team') || '';
  const body = $('div', { className: 'costs-body' });
  put(page, body);
  const again = () => { if (S.page === 'costs' && !S.conv) showCosts(); };
  const table = (headers, rows, empty) => {
    if (!rows.length) return $('div', { className: 'none', textContent: empty || 'Nothing in this period.' });
    return $('div', { className: 'ctable-wrap' }, $('table', { className: 'ctable' },
      $('thead', {}, $('tr', {}, headers.map(h => $('th', { textContent: h })))),
      $('tbody', {}, rows.map(r => $('tr', {}, r.map(c => $('td', typeof c === 'string' || typeof c === 'number' ? { textContent: String(c) } : c)))))));
  };
  const moneyCell = (row) => {
    if (row.cost_usd == null || row.unknown_calls === row.calls && row.calls) return { className: 'unk', textContent: 'Price unknown' };
    const d = disp(row.cost_usd);
    const label = fmtMoney(d);
    return { className: row.unknown_calls ? 'partial' : '', textContent: label + (row.unknown_calls ? ' + unknown' : '') };
  };
  let data;
  const disp = usd => data && data.display ? (data.settings && data.settings.display_currency === 'EUR' && data.settings.eur_per_usd
    ? { amount: usd * Number(data.settings.eur_per_usd), currency: 'EUR' } : { amount: usd, currency: (data.display && data.display.currency) || 'USD' }) : { amount: usd, currency: 'USD' };
  const draw = async () => {
    body.textContent = 'Loading…';
    const q = `/costs?period=${encodeURIComponent(period)}` + (teamFilter ? `&team_id=${encodeURIComponent(teamFilter)}` : '');
    try { data = await api('GET', q); } catch (e) { body.textContent = e.message; return; }
    if (S.page !== 'costs' || S.conv) return;
    const t = data.totals || {}, cur = (data.display && data.display.currency) || 'USD';
    const periods = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days']];
    const teamSel = $('select', { onchange: () => { teamFilter = teamSel.value; store.set('tb_costs_team', teamFilter || null); draw(); } },
      $('option', { value: '', textContent: 'All my projects', selected: !teamFilter }),
      (S.teams || []).filter(x => x.role === 'admin').map(x => $('option', { value: String(x.id), textContent: x.name, selected: String(x.id) === String(teamFilter) })));
    const periodBtns = $('div', { className: 'seg' }, periods.map(([id, label]) => $('button', { type: 'button', className: 'btn sm' + (period === id ? ' primary' : ''), textContent: label, onclick: () => { period = id; store.set('tb_costs_period', id); draw(); } })));
    const totalLabel = t.unknown_calls && (t.cost_usd == null || t.cost_usd === 0) && t.calls ? 'Price unknown' : fmtMoney(data.display || { amount: t.cost_usd, currency: cur });
    const cards = $('div', { className: 'ccards' },
      $('div', { className: 'ccard featured' }, $('div', { className: 'clabel', textContent: 'Spend' }), $('div', { className: 'cval', textContent: totalLabel }), t.unknown_calls ? $('div', { className: 'csub', textContent: t.unknown_calls + ' calls have no official price' }) : null),
      $('div', { className: 'ccard' }, $('div', { className: 'clabel', textContent: 'Calls' }), $('div', { className: 'cval', textContent: String(t.calls || 0) }), $('div', { className: 'csub', textContent: (t.errors || 0) + ' errors' })),
      $('div', { className: 'ccard' }, $('div', { className: 'clabel', textContent: 'Tokens in / out' }), $('div', { className: 'cval', textContent: `${t.prompt_tokens || 0} / ${t.completion_tokens || 0}` }), (t.cached_tokens || t.reasoning_tokens) && $('div', { className: 'csub', textContent: `Cached ${t.cached_tokens || 0} · reasoning ${t.reasoning_tokens || 0}` })),
      (t.premium_requests ? $('div', { className: 'ccard' }, $('div', { className: 'clabel', textContent: 'Premium requests' }), $('div', { className: 'cval', textContent: String(t.premium_requests) })) : null));
    const maxDay = Math.max(0, ...((data.by_day || []).map(d => d.cost_usd || 0)));
    const chart = $('div', { className: 'cchart', role: 'img', ariaLabel: 'Spend per day' },
      (data.by_day || []).map(d => {
        const h = maxDay ? Math.max(3, Math.round(100 * (d.cost_usd || 0) / maxDay)) : 3;
        return $('div', { className: 'cbar', title: `${d.date}: ${fmtMoney(disp(d.cost_usd || 0))} · ${d.calls} calls` },
          $('div', { className: 'cbar-fill', style: `height:${d.calls ? h : 3}%` }),
          $('span', { textContent: d.date.slice(5) }));
      }));
    const budgets = $('div', { className: 'cbudgets' }, (data.budgets || []).map(b => {
      const pct = b.ratio != null ? Math.min(100, Math.round(b.ratio * 100)) : 0;
      const spentLabel = b.spent == null ? 'set an EUR/USD rate to compare' : `${b.spent} ${b.currency}`;
      return $('div', { className: 'cbudget ' + (b.level || 'ok') },
        $('div', { className: 'crow' }, $('b', { textContent: b.name }), $('span', { textContent: b.budget != null ? `${spentLabel} of ${b.budget} ${b.currency}` : 'No monthly budget' })),
        b.budget != null && $('div', { className: 'cbarline' }, $('div', { style: `width:${pct}%` })),
        b.level === 'warn' && $('div', { className: 'cwarn', textContent: 'This project has used 80% of its monthly budget.' }),
        b.level === 'over' && $('div', { className: 'cwarn', textContent: b.block ? 'Budget reached — new model calls are blocked.' : 'This project is over its monthly budget. Calls are not blocked.' }));
    }));
    const admin = (S.teams || []).some(x => x.role === 'admin');
    const budgetEdits = admin && S.team && S.team.role === 'admin' && (() => {
      const amt = $('input', { type: 'number', min: '0', step: '0.01', value: S.team.monthly_budget == null ? '' : S.team.monthly_budget, placeholder: 'No budget' });
      const curSel = $('select', {}, ['USD', 'EUR'].map(c => $('option', { value: c, textContent: c, selected: (S.team.budget_currency || 'USD') === c })));
      const block = $('input', { type: 'checkbox', checked: !!S.team.budget_block });
      return $('div', { className: 'csettings' }, $('h2', { textContent: 'Budget for ' + S.team.name }),
        $('p', { className: 'hint', textContent: 'A warning is shown at 80% and 100%. Blocking new calls at 100% is off by default.' }),
        $('div', { className: 'crow' }, $('label', { className: 'field' }, $('span', { textContent: 'Monthly budget' }), amt), $('label', { className: 'field' }, $('span', { textContent: 'Currency' }), curSel),
          $('label', { className: 'check' }, block, $('span', { textContent: 'Block new calls when the budget is reached' }))),
        $('button', { className: 'btn sm', textContent: 'Save budget', onclick: guard(async () => {
          const monthly_budget = amt.value === '' ? null : Number(amt.value);
          const body = { monthly_budget, budget_currency: curSel.value, budget_block: block.checked };
          await api('PATCH', `/teams/${S.team.id}`, body);
          Object.assign(S.team, body); toast('Budget saved'); again();
        }) }));
    })();
    const s = data.settings || {};
    const settingsBox = admin && (() => {
      const bill = $('select', {}, [['token', 'Token prices (current official Copilot billing)'], ['premium_request', 'Legacy premium requests']].map(([v, l]) => $('option', { value: v, textContent: l, selected: s.copilot_billing === v })));
      const pr = $('input', { type: 'number', min: '0', step: '0.001', value: s.premium_request_price_usd || '', className: 'mono' });
      const curr = $('select', {}, ['USD', 'EUR'].map(c => $('option', { value: c, textContent: c, selected: s.display_currency === c })));
      const fx = $('input', { type: 'number', min: '0', step: '0.0001', value: s.eur_per_usd || '', placeholder: 'e.g. 0.92', className: 'mono' });
      const elecOn = $('input', { type: 'checkbox', checked: s.electricity_enabled === '1' });
      const elec = $('input', { type: 'number', min: '0', step: '0.01', value: s.electricity_per_hour || '', placeholder: 'Off' });
      const elecCur = $('select', {}, ['EUR', 'USD'].map(c => $('option', { value: c, textContent: c, selected: (s.electricity_currency || 'EUR') === c })));
      return $('details', { className: 'csettings' }, $('summary', { textContent: 'Prices and display' }),
        $('p', { className: 'hint', textContent: 'Since 1 June 2026 GitHub Copilot (Business/Enterprise included) bills per token. Premium-request multipliers apply only to leftover annual Copilot Pro/Pro+ plans. Local endpoints (Spark, mock) are 0. Electricity estimates are off unless you set a rate. Official prices are USD; euros need a rate you enter yourself.' }),
        $('label', { className: 'field' }, $('span', { textContent: 'Copilot billing' }), bill),
        $('label', { className: 'field' }, $('span', { textContent: 'Price per premium request (USD)' }), pr),
        $('label', { className: 'field' }, $('span', { textContent: 'Display currency' }), curr),
        $('label', { className: 'field' }, $('span', { textContent: 'EUR per 1 USD' }), fx, $('small', { textContent: 'Required to show euros. Not filled in for you.' })),
        $('label', { className: 'check' }, elecOn, $('span', { textContent: 'Estimate electricity for local endpoints' })),
        $('div', { className: 'crow' }, $('label', { className: 'field' }, $('span', { textContent: 'Electricity per GPU-hour' }), elec), $('label', { className: 'field' }, $('span', { textContent: 'Currency' }), elecCur)),
        $('button', { className: 'btn sm', textContent: 'Save settings', onclick: guard(async () => {
          await api('PATCH', '/costs/settings', {
            copilot_billing: bill.value, premium_request_price_usd: pr.value === '' ? undefined : Number(pr.value),
            display_currency: curr.value, eur_per_usd: fx.value, electricity_enabled: elecOn.checked,
            electricity_per_hour: elec.value, electricity_currency: elecCur.value });
          toast('Saved'); again();
        }) }),
        $('p', { className: 'hint' }, 'The price table is editable under ', $('button', { className: 'link', type: 'button', textContent: 'Edit prices', onclick: guard(editPrices) }), '.'));
    })();
    fill(body,
      $('div', { className: 'cbar-head' }, periodBtns, teamSel),
      cards, budgets, budgetEdits,
      $('h2', { textContent: 'Per day' }), chart,
      $('h2', { textContent: 'Per bot' }), table(['Bot', 'Calls', 'Cost'], (data.by_bot || []).map(r => [r.name, r.calls, moneyCell(r)])),
      $('h2', { textContent: 'Per model' }), table(['Model', 'Provider', 'Calls', 'Cost'], (data.by_model || []).map(r => [r.model, r.provider, r.calls, moneyCell(r)])),
      $('h2', { textContent: 'Per endpoint' }), table(['Endpoint', 'Calls', 'Cost'], (data.by_endpoint || []).map(r => [r.name, r.calls, moneyCell(r)])),
      $('h2', { textContent: 'Per origin' }), table(['Origin', 'Calls', 'Cost'], (data.by_origin || []).map(r => [r.origin, r.calls, moneyCell(r)])),
      $('h2', { textContent: 'Most expensive conversations' }), table(['Conversation', 'Calls', 'Cost'], (data.expensive_conversations || []).map(r => [r.name, r.calls, moneyCell(r)])),
      settingsBox);
  };
  async function editPrices() {
    const { prices } = await api('GET', '/costs/prices');
    const rows = prices.map(p => {
      const inp = (name, val, step) => $('input', { type: 'number', step: step || '0.01', value: val == null ? '' : val, className: 'mono', data: { k: name } });
      return $('tr', { data: { id: p.id } },
        $('td', { textContent: p.provider }), $('td', { className: 'mono', textContent: p.model }), $('td', { textContent: p.billing_mode }),
        $('td', {}, inp('input_per_million', p.input_per_million)),
        $('td', {}, inp('output_per_million', p.output_per_million)),
        $('td', {}, inp('cached_per_million', p.cached_per_million)),
        $('td', {}, inp('premium_multiplier', p.premium_multiplier, '0.01')),
        $('td', {}, $('a', { href: p.source_url, target: '_blank', rel: 'noopener', textContent: p.source_date || '—' })));
    });
    const d = openDialog('wide', $('div', { className: 'pad' }, $('h2', { textContent: 'Model prices' }),
      $('p', { className: 'intro', textContent: 'Official USD prices per 1 million tokens, or a premium-request multiplier. Editing marks the row as yours so a later official refresh will not overwrite it.' }),
      $('div', { className: 'ctable-wrap', style: 'max-height:60vh' }, $('table', { className: 'ctable' },
        $('thead', {}, $('tr', {}, ['Provider', 'Model', 'Mode', 'Input / 1M', 'Output / 1M', 'Cached / 1M', 'Multiplier', 'Source'].map(h => $('th', { textContent: h })))),
        $('tbody', {}, rows))),
      $('div', { className: 'dfoot' }, $('button', { className: 'btn primary', textContent: 'Save changes', onclick: guard(async () => {
        for (const tr of rows) {
          const id = +tr.dataset.id, body = {};
          for (const i of tr.querySelectorAll('input')) { const v = i.value.trim(); body[i.dataset.k] = v === '' ? null : Number(v); }
          await api('PATCH', `/costs/prices/${id}`, body);
        }
        d.close(); toast('Prices saved'); again();
      }) }), $('button', { className: 'btn', textContent: 'Close', onclick: () => d.close() }))));
  }
  await draw();
}

// ---------------------------------------------------------------- create things
async function newEndpoint() {
  const e = await form({ title: 'New endpoint', intro: 'An OpenAI-compatible chat completions API, or GitHub Copilot. The model needs tool calling.', ok: 'Add endpoint',
    fields: endpointFields(null), onSubmit: v => api('POST', `/teams/${S.team.id}/endpoints`, endpointPayload(v)) });
  if (!e) return;
  await loadTeam(); if (!S.conv) showHome();
  const t = await api('POST', `/endpoints/${e.id}/test`);
  toast(t.reachable && t.tool_calling ? `${e.name} works, tool calling included` : `${e.name}: ${t.detail}`, t.reachable && t.tool_calling ? '' : 'bad');
}
function endpointDialog(e) {
  const out = $('div', { className: 'err' }), mine = e.owner_id === S.me.id, copilot = isCopilot(e);
  const test = $('button', { className: 'btn', textContent: 'Test connection', onclick: async () => { test.disabled = true; out.style.color = 'var(--muted)'; out.textContent = 'Testing…';
    try { const t = await api('POST', `/endpoints/${e.id}/test`); const good = t.reachable && t.tool_calling; out.style.color = good ? 'var(--ok)' : 'var(--bad)'; out.textContent = good ? 'Reachable, and the model calls tools.' : !t.reachable ? 'Not reachable: ' + t.detail : 'Reachable, but no tool calling: ' + t.detail; }
    catch (x) { out.style.color = 'var(--bad)'; out.textContent = x.message; } test.disabled = false; } });
  const modelsBtn = $('button', { className: 'btn', textContent: 'List models', onclick: async () => {
    modelsBtn.disabled = true; out.style.color = 'var(--muted)'; out.textContent = 'Listing models…';
    try {
      const r = await api('GET', `/endpoints/${e.id}/models`);
      const ids = (r.models || []).map(m => m.id).filter(Boolean);
      out.style.color = ids.length ? 'var(--ok)' : 'var(--muted)';
      out.textContent = ids.length ? ids.join(', ') : (r.detail && r.detail !== 'ok' ? r.detail : 'No model list from this endpoint.');
    } catch (x) { out.style.color = 'var(--bad)'; out.textContent = x.message; }
    modelsBtn.disabled = false;
  }});
  const d = openDialog('', $('div', { className: 'pad' }, $('h2', { textContent: e.name }),
    $('dl', { className: 'kv', style: 'margin-top:14px' },
      $('dt', { textContent: 'Provider' }), $('dd', { textContent: copilot ? 'GitHub Copilot' : 'OpenAI-compatible' }),
      copilot ? [$('dt', { textContent: 'GitHub domain' }), $('dd', { className: 'mono', textContent: e.github_domain || 'github.com' })]
              : [$('dt', { textContent: 'Base URL' }), $('dd', { className: 'mono', textContent: e.base_url })],
      $('dt', { textContent: 'Default model' }), $('dd', { className: 'mono', textContent: e.default_model }),
      $('dt', { textContent: copilot ? 'GitHub token' : 'API key' }), $('dd', { textContent: e.has_key ? 'Stored (hidden)' : 'None' }),
      $('dt', { textContent: 'Sharing' }), $('dd', { textContent: e.shared ? 'Everyone in the project' : 'Only you' })),
    mine ? out : $('div', { className: 'err muted', textContent: 'Only the person who added this endpoint can test or remove it.' }),
    $('div', { className: 'dfoot' }, mine && $('button', { className: 'btn danger', textContent: 'Remove', onclick: guard(async () => { if (!await confirmBox({ title: 'Remove endpoint?', text: `"${e.name}" is removed for everyone. Bots must be moved to another endpoint first.`, ok: 'Remove', danger: true })) return; await api('DELETE', `/endpoints/${e.id}`); d.close(); await loadTeam(); }) }),
      mine && $('button', { className: 'btn', textContent: 'Edit', onclick: () => { d.close(); guard(editEndpoint)(e); } }), mine && test, mine && modelsBtn, $('button', { className: 'btn primary', textContent: 'Done', onclick: () => d.close() }))));
}
async function newBot() {
  if (!S.endpoints.length) { toast('Add a model endpoint first'); return newEndpoint(); }
  let touched = false;
  const b = await form({ title: 'New bot', ok: 'Create bot', fields: [
    { name: 'name', label: 'Name', required: true, placeholder: 'Helper', oninput: (v, i) => { if (!touched) i.handle.value = v.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32); } },
    { name: 'handle', label: 'Handle', required: true, mono: true, hint: 'People and bots mention it as @handle. Lowercase letters, digits, - and _.', oninput: () => { touched = true; } },
    { name: 'endpoint_id', label: 'Model endpoint', type: 'select', value: String(S.endpoints[0].id), options: S.endpoints.map(e => ({ value: String(e.id), label: `${e.name} · ${e.default_model}` })) },
    { name: 'is_team', label: 'Who is it for', type: 'select', value: '', options: [{ value: '', label: 'Personal: only me' }, { value: '1', label: 'Team Bot: can be shared with the project' }] },
    { name: 'instructions', label: 'Instructions', type: 'textarea', placeholder: 'What this bot does and how it should behave.' }],
    onSubmit: v => api('POST', `/teams/${S.team.id}/bots`, { ...v, endpoint_id: +v.endpoint_id, is_team: !!v.is_team }) });
  if (!b) return;
  await loadTeam(); await openBot(S.bots.find(x => x.id === b.id) || b);
  if (b.is_team) { openDrawer(S.bot, 'sharing'); toast('Created as a draft. Publish it under Sharing when it is ready.'); }
}
async function newChannel() {
  const c = await form({ title: 'New channel', intro: 'People and bots in one conversation. Every bot of the project joins and replies to each message; you can remove a bot or set it to mention only afterwards.', ok: 'Create channel', fields: [
    { name: 'name', label: 'Name', required: true, placeholder: 'general' },
    ...S.members.filter(m => m.user_id !== S.me.id).map(m => ({ name: 'u' + m.user_id, label: m.display_name, type: 'checkbox' })),],
    onSubmit: v => api('POST', `/teams/${S.team.id}/channels`, { name: v.name, user_ids: S.members.filter(m => v['u' + m.user_id]).map(m => m.user_id), bot_ids: [] }) });
  if (c) { await loadTeam(); await openConv(c, null); }
}

// ---------------------------------------------------------------- conversation
function stopConv() { if (S.es) { S.es.close(); S.es = null; } S.dockSync = null; clearInterval(S.screenTimer); }
const openBot = async b => openConv(await api('POST', `/bots/${b.id}/dm`), b);
const openFeed = async b => openConv(await api('GET', `/bots/${b.id}/feed`), b);
async function openOverview() {
  const h = await api('GET', '/home');
  S.home = h;
  await openConv(h.conversation, h.bot);
}
async function openConv(conv, bot) {
  stopConv(); closeDrawer(); afterNav();
  Object.assign(S, { conv, bot: bot || null, page: null, msgs: [], approvals: [], state: IDLE, dockSync: null, ctx: { skills: [], secrets: [], mem: null }, first: true, chain: null });
  if (conv.kind === 'home') store.set('tb_last_home', '1');
  else {
    store.set('tb_last_home', null);
    store.set('tb_last_' + conv.team_id, (conv.kind === 'channel' ? 'ch:' + conv.id : conv.kind + ':' + conv.bot_id));
  }
  renderSide(); renderTop(); el('col').replaceChildren(); el('dock').replaceChildren(); el('stage').classList.remove('hero');
  await loadCtx(conv); if (S.conv !== conv) return;
  renderTop(); renderDock();
  await refresh(); if (S.conv !== conv) return;
  let timer; const poke = () => { clearTimeout(timer); timer = setTimeout(refresh, 40); };
  S.es = new EventSource(withToken(`/conversations/${conv.id}/events`)); S.es.onmessage = poke; S.es.onopen = poke;
}
/** What the '@' menu and the header need to know about this conversation. */
async function loadCtx(conv) {
  const ctx = { skills: [], secrets: [], mem: null }, quiet = p => p.catch(() => []);
  if (conv.kind === 'channel') {
    ctx.mem = await api('GET', `/channels/${conv.id}/members`);
    const lists = await Promise.all(ctx.mem.bots.map(b => quiet(api('GET', `/bots/${b.id}/skills`))));
    ctx.mem.bots.forEach((b, i) => lists[i].forEach(k => ctx.skills.push({ ...k, of: b.handle })));
  } else if (conv.kind === 'dm' && S.bot) {
    [ctx.skills, ctx.secrets] = await Promise.all([quiet(api('GET', `/bots/${S.bot.id}/skills`)), S.bot.role !== 'collaborator' ? quiet(api('GET', `/bots/${S.bot.id}/secrets`)) : []]);
  }
  if (S.conv === conv) S.ctx = ctx;
}
function refresh() { return S.chain = (S.chain || Promise.resolve()).then(doRefresh).catch(e => console.warn(e)); }
async function doRefresh() {
  const conv = S.conv; if (!conv) return;
  const last = S.msgs.length ? S.msgs[S.msgs.length - 1].id : 0;
  const [more, approvals, state] = await Promise.all([api('GET', `/conversations/${conv.id}/messages?after=${last}`), conv.kind === 'feed' ? [] : api('GET', `/conversations/${conv.id}/approvals`), conv.kind === 'feed' ? IDLE : api('GET', `/conversations/${conv.id}/state`)]);
  if (S.conv !== conv) return;
  S.msgs.push(...more.filter(m => m.id > last)); S.approvals = approvals; S.state = state; renderConv();
}
function renderTop() {
  const top = el('top'); if (!top) return;
  const c = S.conv, lead = topLead();
  if (!c) return fill(top, ...lead, $('span', { className: 'title', textContent: S.page === 'routines' ? 'Routines' : S.page === 'costs' ? 'Costs' : 'Projects' }), $('span', { className: 'grow' }), $('button', { className: 'btn sm', onclick: openPalette }, icon('search', 15), 'Search'));
  if (c.kind === 'feed') return fill(top, ...lead, icon('pulse'), $('span', { className: 'title', textContent: c.name }), $('span', { className: 'sub grow', textContent: 'Short summaries of finished tasks. The private chats stay private.' }));
  if (c.kind === 'dm' || c.kind === 'home') {
    const b = S.bot, shared = b && b.is_team && b.published && c.kind === 'dm';
    const cb = $('input', { type: 'checkbox', checked: c.share_activity, onchange: guard(async () => { Object.assign(c, await api('PATCH', `/conversations/${c.id}`, { share_activity: cb.checked })); toast(cb.checked ? 'Task summaries go to the activity feed' : 'Nothing from this chat is shared'); }) });
    const canRename = b && b.role !== 'collaborator';
    return fill(top, ...lead, avatar(c.name, 'bot'), $(canRename ? 'button' : 'span', { className: 'title', textContent: c.name, title: canRename ? 'Rename' : '', onclick: canRename ? guard(() => renameBot(b)) : null }), b && $('span', { className: 'sub', textContent: '@' + b.handle }),
      $('span', { className: 'pill hidesm', title: 'Only you can read this chat. Not even the owner of the bot can.' }, icon('lock', 12), 'Private'), $('span', { className: 'grow' }),
      shared && $('label', { className: 'switch hidesm', title: 'When on, a short summary of each finished task is posted to the shared activity feed. The chat itself is never shared.' }, cb, 'Share summaries'),
      b && b.computer_use && $('button', { className: 'btn sm', title: 'Interactive screen with mouse and keyboard', onclick: () => window.open(withToken(`/bots/${b.id}/screen/vnc/`), '_blank', 'noopener') }, icon('screen', 15), 'Open scherm'),
      b && ib('sliders', 'Bot settings', () => el('drawer').classList.contains('open') ? closeDrawer() : openDrawer(b)));
  }
  const m = S.ctx.mem, people = m ? m.users.map(u => u.display_name).join(', ') + (m.bots.length ? ' · ' + m.bots.map(b => '@' + b.handle + (b.mention_only ? ' (mention only)' : '')).join(' ') : '') : '';
  fill(top, ...lead, icon('hash'), $('button', { className: 'title', textContent: c.name, title: 'Rename', onclick: guard(() => renameChannel(S.channels.find(x => x.id === c.id) || c)) }), $('span', { className: 'sub grow', textContent: people, title: people }),
    $('button', { className: 'btn sm', onclick: guard(channelMembers) }, icon('users', 15), m ? String(m.users.length + m.bots.length) : ''));
}
async function addToChannel(body) {
  await api('POST', `/channels/${S.conv.id}/members`, body);
  await loadCtx(S.conv); renderTop();
}
/** People and bots of a channel: add, remove a bot, or make a bot answer only when mentioned. */
async function channelMembers() {
  const c = S.conv, again = guard(async () => { d.close(); await loadCtx(c); renderTop(); renderDock(); await channelMembers(); });
  const m = S.ctx.mem, inU = new Set(m.users.map(u => u.id)), inB = new Set(m.bots.map(b => b.id)), admin = S.team.role === 'admin';
  const people = S.members.filter(x => !inU.has(x.user_id)), bots = S.bots.filter(b => !inB.has(b.id) && b.role !== 'collaborator'), picks = [];
  const pick = (kind, id, text) => { const cb = $('input', { type: 'checkbox' }); picks.push({ kind, id, cb }); return $('label', { className: 'check', style: 'margin:8px 0' }, cb, $('span', { textContent: text })); };
  const botRow = b => {
    const mine = admin || S.bots.some(x => x.id === b.id && x.role !== 'collaborator');
    const sw = $('input', { type: 'checkbox', checked: b.mention_only, onchange: guard(async () => { await api('PATCH', `/channels/${c.id}/members/bots/${b.id}`, { mention_only: sw.checked }); b.mention_only = sw.checked; renderTop(); renderDock(); }) });
    return $('div', { className: 'li' }, avatar(b.name, 'bot'), $('div', { className: 'tx' }, b.name, $('small', { textContent: '@' + b.handle })),
      mine ? $('label', { className: 'switch', title: 'On: the bot answers only when someone mentions its @handle. Off: it replies to every message.' }, sw, 'Mention only') : $('span', { className: 'tag', textContent: b.mention_only ? 'mention only' : 'replies to all' }),
      mine && ib('trash', 'Remove from this channel', guard(async () => { if (!await confirmBox({ title: `Remove ${b.name} from # ${c.name}?`, text: 'The bot stops reading and answering here. You can add it again later.', ok: 'Remove', danger: true })) return; await api('DELETE', `/channels/${c.id}/members/bots/${b.id}`); again(); }), 15));
  };
  const d = openDialog('', $('div', { className: 'pad' }, $('h2', { textContent: '# ' + c.name }), $('p', { className: 'intro', textContent: 'A bot replies to every message here, unless it is set to mention only. Mention a bot to address just that one.' }),
    $('div', { className: 'field' }, $('span', { textContent: 'Bots' }), m.bots.length ? m.bots.map(botRow) : $('div', { className: 'none', textContent: 'No bots in this channel.' })),
    $('div', { className: 'field' }, $('span', { textContent: 'People' }), m.users.map(u => $('div', { className: 'li' }, avatar(u.display_name), $('div', { className: 'tx', textContent: u.display_name })))),
    (people.length || bots.length) ? $('div', { className: 'field' }, $('span', { textContent: 'Add' }), people.map(x => pick('u', x.user_id, x.display_name)), bots.map(b => pick('b', b.id, `${b.name} (@${b.handle})`))) : null,
    $('div', { className: 'dfoot' }, (people.length || bots.length) ? $('button', { className: 'btn', textContent: 'Add selected', onclick: guard(async () => { const on = picks.filter(x => x.cb.checked); if (!on.length) return toast('Tick who you want to add'); await api('POST', `/channels/${c.id}/members`, { user_ids: on.filter(x => x.kind === 'u').map(x => x.id), bot_ids: on.filter(x => x.kind === 'b').map(x => x.id) }); again(); }) }) : null,
      $('button', { className: 'btn primary', textContent: 'Done', onclick: () => d.close() }))));
}

// ---------- messages
const TOOLS = { run_command: ['term', 'Run command'], write_file: ['file', 'Write file'], read_file: ['file', 'Read file'], http_request: ['globe', 'HTTP request'], remember: ['book', 'Remember'], forget: ['book', 'Forget'],
  save_skill: ['zap', 'Save skill'], use_skill: ['zap', 'Use skill'], ask_bot: ['bot', 'Ask another bot'], create_routine: ['clock', 'Create routine'], list_workspace: ['grid', 'List projects'], get_overview: ['home', 'Overview'], read_messages: ['book', 'Read messages'], post_to_channel: ['hash', 'Post to channel'], list_routines: ['clock', 'List routines'], update_routine: ['edit', 'Update routine'], delete_routine: ['trash', 'Delete routine'], run_routine: ['play', 'Run routine'] };
function toolInfo(name) {
  if (TOOLS[name]) return TOOLS[name];
  if (name.startsWith('screen_')) return ['screen', 'Screen: ' + name.slice(7)];
  if (name.startsWith('mcp_')) return ['plug', name.slice(4).replace(/_/g, ' ')];
  return ['zap', name];
}
function parseArgs(tc) { try { const a = JSON.parse(tc.function.arguments || '{}'); return a && typeof a === 'object' ? a : {}; } catch { return {}; } }
const argLine = a => String(a.command ?? a.path ?? a.url ?? (a.handle ? '@' + a.handle : null) ?? a.name ?? a.content ?? a.text ?? a.keys ?? (a.x != null ? `${a.x}, ${a.y}` : null) ?? a.amount ?? '').split('\n')[0];
const isBusy = () => S.state.running.length > 0;
/** How a message sent while the bot was busy was handled, as a line under the message. */
function deliveryNote(m) {
  if (S.state.queued.includes(m.id)) return ['q', 'clock', 'Queued. Runs when the current task is done.'];
  const d = S.state.delivery[m.id];
  return d === 'steer' ? ['s', 'check', 'Added to the running task'] : d === 'interrupt' ? ['i', 'stop', 'Stopped the running task for this'] : d === 'dropped' ? ['d', 'x', 'Not run, because you stopped the bot'] : d === 'stopped' ? ['x', 'stop', 'You stopped the bot here'] : null;
}
/** Mock-LLM test format: a user message that only asks the scripted model to call a tool. */
function toolDirective(text) {
  const m = /^\s*TOOL:\s*(\{[\s\S]*\})\s*$/.exec(text || '');
  if (!m) return null;
  try { const o = JSON.parse(m[1]); return o && o.name ? o : null; } catch { return null; }
}
/** Mock-LLM final answer (`RESULT: …` / `ECHO[mock-1]: …`) must never appear as a chat bubble. */
function isMockLeak(text) { return /^\s*(RESULT:|ECHO\[mock[-\w]*\]:)/.test(text || ''); }
function blocks() {
  const c = S.conv, out = [];
  if (!S.msgs.length) return [{ key: 'hero', sig: c.kind + S.ctx.skills.length, make: heroNode }];
  const res = new Map(), appr = new Map(S.approvals.map(a => [a.tool_call_id, a])), seen = new Set(), busy = isBusy();
  for (const m of S.msgs) if (m.role === 'tool') res.set(m.tool_call_id, m);
  // Consecutive tool calls of one bot fold into a single line; the details stay one click away.
  let day = '', group = null, working = false;
  const flush = () => { if (!group) return; const g = group; group = null; working = g.calls.some(x => !x.r && !x.a);
    out.push({ key: 'g' + g.id, sig: g.calls.map(x => x.r ? 'r' + x.r.id : x.a ? 'a' + x.a.id : busy ? 'run' : 'idle').join() + (S.details ? 'D' : ''), make: () => workNode(g, busy) }); };
  for (const m of S.msgs) {
    if (m.role === 'tool') continue;
    // Raw TOOL:/RESULT:/ECHO[mock] text is test format, not a chat message. Tool calls already fold into a step.
    if (m.role === 'user' && toolDirective(m.content)) continue;
    if (m.role === 'assistant' && isMockLeak(m.content) && !(m.tool_calls && m.tool_calls.length)) continue;
    const d = ts(m.created_at), dn = d.toDateString();
    if (dn !== day) { flush(); if (day || dn !== new Date().toDateString()) out.push({ key: 'd' + dn, sig: '', make: () => $('div', { className: 'daysep', textContent: dayName(d) }) }); day = dn; }
    if (m.content) { flush(); working = false; const note = m.role === 'user' ? deliveryNote(m) : null; out.push({ key: 'm' + m.id, sig: note ? note[0] : '', make: () => msgNode(m, note) }); }
    if (m.tool_calls.length) {
      if (!group || group.bot !== m.author_bot_id) { flush(); group = { id: m.id, bot: m.author_bot_id, author: m.author, calls: [] }; }
      for (const tc of m.tool_calls) { seen.add(tc.id); group.calls.push({ tc, r: res.get(tc.id), a: appr.get(tc.id) }); }
    }
  }
  flush();
  for (const a of S.approvals) if (!seen.has(a.tool_call_id)) out.push({ key: 'a' + a.id, sig: '', make: () => $('div', { className: 'steps' }, stepNode({ tc: { id: a.tool_call_id, function: { name: a.tool_name, arguments: JSON.stringify(a.arguments) } }, a }, false)) });
  if (busy && !working) out.push({ key: 'busy', sig: '', make: () => $('div', { className: 'busy', title: 'Working…', role: 'status', ariaLabel: 'The bot is working' }, $('i'), $('i'), $('i')) });
  return out;
}
function renderConv() {
  const col = el('col'), view = el('view'), c = S.conv; if (!c) return;
  el('stage').classList.toggle('hero', !S.msgs.length && c.kind !== 'feed');
  const stick = S.first || view.scrollHeight - view.scrollTop - view.clientHeight < 160;
  const old = new Map([...col.children].map(n => [n.dataset.key, n]));
  const nodes = blocks().map(b => {
    const o = old.get(b.key); if (o && o.dataset.sig === b.sig) return o;
    const n = b.make(); n.dataset.key = b.key; n.dataset.sig = b.sig;
    // what the person opened or closed by hand stays that way when a block is redrawn
    if (o) for (const d of o.querySelectorAll('details[data-user]')) { const t = n.querySelector(`details[data-call="${CSS.escape(d.dataset.call)}"]`); if (t) { t.open = d.dataset.user === '1'; t.dataset.user = d.dataset.user; } }
    return n;
  });
  col.replaceChildren(...nodes);
  if (stick) view.scrollTop = view.scrollHeight;
  S.first = false; updateJump(); if (S.dockSync) S.dockSync();
}
function updateJump() { const v = el('view'), j = el('jump'); if (j) j.classList.toggle('show', v.scrollHeight - v.scrollTop - v.clientHeight > 300); }
function heroNode() {
  const c = S.conv, b = S.bot;
  if (c.kind === 'feed') return $('div', { className: 'herobox', style: 'padding-top:16vh' }, $('p', { textContent: 'No shared activity yet. When this bot finishes a task for someone, a short summary shows up here.' }));
  if (c.kind === 'dm' || c.kind === 'home') return $('div', { className: 'herobox' }, avatar(c.name, 'bot lg'), $('h1', { textContent: 'What can I do for you?' }),
    $('p', { textContent: c.kind === 'home' ? 'I can see every project, bot and channel you have access to.' : ((b && b.instructions.trim().split('\n')[0]) || `${c.name} works on the project computer and remembers what matters.`)}));
  return $('div', { className: 'herobox' }, $('h1', { textContent: '# ' + c.name }), $('p', { textContent: (S.ctx.mem && S.ctx.mem.bots.length) ? 'The bots here reply to every message. Mention one with @ to address only that bot.' : 'There is no bot in this channel. Type @ to add one.' }));
}
function msgNode(m, note) {
  const c = S.conv, when = ts(m.created_at), time = $('time', { textContent: clock(when), title: when.toLocaleString() });
  if (c.kind === 'feed') return $('div', { className: 'feed' }, time, $('div', { className: 'body' }, md(m.content)));
  if (m.role === 'user') {
    const mine = m.author_user_id === S.me.id;
    return $('div', { className: 'msg user' + (mine ? ' mine' : '') }, !mine && $('div', { className: 'who' }, avatar(m.author), m.author || 'Someone', time), plain(m.content, $('div', { className: 'body', title: mine ? when.toLocaleString() : '' })),
      note && $('div', { className: 'note' }, icon(note[1], 13), note[2]));
  }
  const direct = c.kind === 'dm' || c.kind === 'home';
  const body = $('div', { className: 'body' }, md(m.content)), done = ib('copy', 'Copy', async e => { const btn = e.currentTarget; await copyText(m.content); btn.replaceChildren(icon('check')); setTimeout(() => btn.replaceChildren(icon('copy')), 1500); }, 15);
  return $('div', { className: 'msg bot' + (/^Endpoint error: /.test(m.content) ? ' error' : '') }, !direct && $('div', { className: 'who' }, avatar(m.author, 'bot'), m.author, time), body, $('div', { className: 'acts' }, done, direct && $('span', { className: 'muted', style: 'font-size:12px;align-self:center;margin-left:4px', textContent: clock(when) })));
}
const failed = r => !!r && /^(denied|cancelled|stopped|error)|^exit code [1-9]/.test(r.content);
/** One quiet line for everything a bot did in a row. Commands and file contents stay folded away. */
function workNode(g, busy) {
  const n = g.calls.length, asks = g.calls.filter(x => x.a).length, open = g.calls.filter(x => !x.r && !x.a), bad = g.calls.filter(x => failed(x.r)).length;
  const running = busy && open.length > 0, who = (S.conv.kind === 'dm' || S.conv.kind === 'home') ? '' : g.author + ' · ';
  const kinds = [...new Set(g.calls.map(x => toolInfo(x.tc.function.name)[1]))];
  const text = asks ? 'Needs your approval' : running ? toolInfo(open[open.length - 1].tc.function.name)[1] : `${n} ${n === 1 ? 'step' : 'steps'}` + (bad ? ` · ${bad} did not succeed` : '') + (open.length ? ' · stopped' : '');
  const lead = asks ? icon('pause', 15) : running ? $('span', { className: 'spin' }) : icon(bad || open.length ? 'dots' : 'check', 15);
  return $('details', { className: 'work' + (asks ? ' ask' : ''), data: { call: 'g' + g.id }, open: S.details || asks > 0 },
    $('summary', {}, lead, $('span', { className: 'tn', textContent: who + text }), !asks && !running && $('span', { className: 'targ', textContent: kinds.slice(0, 3).join(', ') + (kinds.length > 3 ? '…' : '') }), $('span', { className: 'chev' }, icon('chev', 15))),
    $('div', { className: 'steps' }, g.calls.map(x => stepNode(x, busy))));
}
function stepNode({ tc, r, a }, busy) {
  const name = tc.function.name, args = parseArgs(tc), [ic, label] = toolInfo(name), txt = r ? r.content : '';
  const bad = /^(denied|cancelled|stopped)/.test(txt), fail = failed(r);
  const st = r ? (bad ? ['bad', 'x', txt.split(':')[0]] : fail ? ['bad', 'x', 'failed'] : ['ok', 'check', 'done']) : a ? ['ask', 'pause', 'needs approval'] : busy ? ['', null, 'running'] : ['', null, 'stopped'];
  const d = $('details', { className: 'step', data: { call: tc.id }, open: !!a },
    $('summary', {}, icon(ic, 16), $('span', { className: 'tn', textContent: label }), $('span', { className: 'targ', textContent: argLine(args) }),
      $('span', { className: 'st ' + st[0] }, st[1] ? icon(st[1], 13) : busy && !r ? $('span', { className: 'spin' }) : null, st[2]), $('span', { className: 'chev' }, icon('chev', 15))));
  const inn = $('div', { className: 'in' });
  if (a) {
    const act = (allow, always) => guard(async e => { for (const b of e.currentTarget.parentElement.querySelectorAll('button')) b.disabled = true; await api('POST', `/approvals/${a.id}`, { allow, always }); refresh(); });
    inn.append($('div', { className: 'askbox' }, $('div', { className: 't', textContent: a.mine ? `${a.bot} wants to do this. Check it before you allow it.` : `Waiting for ${a.requester} to decide.` }),
      a.mine && [$('button', { className: 'btn sm primary', textContent: 'Allow once', onclick: act(true, false) }), $('button', { className: 'btn sm', textContent: 'Always allow', title: 'Do not ask again for this tool and this bot', onclick: act(true, true) }), $('button', { className: 'btn sm danger', textContent: 'Deny', onclick: act(false, false) })]));
  }
  const keys = Object.keys(args), single = keys.length === 1 && typeof args[keys[0]] === 'string';
  if (keys.length) inn.append($('div', { className: 'lbl', textContent: single ? keys[0] : 'Arguments' }), $('pre', { textContent: single ? args[keys[0]] : JSON.stringify(args, null, 2) }));
  if (r) { inn.append($('div', { className: 'lbl', textContent: 'Result' }), $('pre', { textContent: r.content || '(empty)' }));
    if (r.has_image) inn.append($('img', { src: withToken(`/messages/${r.id}/image`), alt: 'Screenshot of the bot screen', loading: 'lazy' })); }
  d.append(inn); return d;
}

// ---------- markdown (builds DOM nodes; nothing is ever set as HTML)
const MENTION = /(?<![\w@])@(?:skill|file):(?:"[^"\n]+"|[^\s"]*[^\s".,;:!?)])|(?<![\w@./])@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*|\{\{secret:[A-Z_][A-Z0-9_]*\}\}/g;
function knownNames() { return new Set([...S.bots.map(b => b.handle), ...S.members.map(m => m.username), ...((S.ctx.mem || {}).bots || []).map(b => b.handle)]); }
function splitMentions(text) {
  const out = [], known = knownNames(); let last = 0;
  for (const m of text.matchAll(MENTION)) {
    const s = m[0]; if (s[0] === '@' && !/^@(skill|file):/.test(s) && !known.has(s.slice(1))) continue;
    if (m.index > last) out.push({ s: text.slice(last, m.index) }); out.push({ s, tok: true }); last = m.index + s.length;
  }
  if (last < text.length) out.push({ s: text.slice(last) });
  return out;
}
function plain(text, into) { for (const p of splitMentions(text)) into.append(p.tok ? $('span', { className: 'tok', textContent: p.s }) : p.s); return into; }
const INLINE = /(`[^`\n]+`)|(\*\*[^\n]+?\*\*)|(\*[^\s*][^*\n]*\*)|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"')\]]*[^\s<>"')\].,;:!?])/g;
function inline(text, into) {
  let last = 0; const link = (href, label) => $('a', { href, target: '_blank', rel: 'noopener noreferrer', textContent: label });
  for (const m of text.matchAll(INLINE)) {
    plain(text.slice(last, m.index), into); last = m.index + m[0].length;
    if (m[1]) into.append($('code', { textContent: m[1].slice(1, -1) }));
    else if (m[2]) into.append(inline(m[2].slice(2, -2), $('strong')));
    else if (m[3]) into.append(inline(m[3].slice(1, -1), $('em')));
    else into.append(m[4] ? link(m[5], m[4]) : link(m[6], m[6]));
  }
  return plain(text.slice(last), into);
}
function codeBlock(code, lang) {
  const btn = $('button', { className: 'btn sm', style: 'border:0', textContent: 'Copy', onclick: async () => { await copyText(code); btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy'; }, 1500); } });
  return $('div', { className: 'codeblock' }, $('div', { className: 'bar' }, $('span', { textContent: lang || 'text' }), btn), $('pre', {}, $('code', { textContent: code, style: 'background:none;padding:0;font-size:inherit' })));
}
function md(text) {
  const root = document.createDocumentFragment(), lines = text.replace(/\r/g, '').split('\n');
  const isList = l => /^\s*(?:[-*+]|\d+[.)])\s+\S/.test(l), isRule = l => /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(l), isFence = l => /^\s*```/.test(l);
  const isStart = l => isFence(l) || /^#{1,4}\s/.test(l) || /^\s*>/.test(l) || isList(l) || isRule(l);
  const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  let i = 0, m;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    if (isFence(l)) { const buf = []; i++; while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]); i++; root.append(codeBlock(buf.join('\n'), l.trim().slice(3).trim())); continue; }
    if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) { root.append(inline(m[2], $('h' + (m[1].length + 2)))); i++; continue; }
    if (isRule(l)) { root.append($('hr')); i++; continue; }
    if (/^\s*>/.test(l)) { const buf = []; while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, '')); root.append($('blockquote', {}, md(buf.join('\n')))); continue; }
    if (isList(l)) {
      const stack = [];
      while (i < lines.length && isList(lines[i])) {
        const x = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]), indent = x[1].length, ordered = /\d/.test(x[2]);
        while (stack.length > 1 && indent < stack[stack.length - 1].indent) stack.pop();
        let cur = stack[stack.length - 1];
        if (!cur || indent > cur.indent) {
          const list = $(ordered ? 'ol' : 'ul'); if (ordered) list.start = parseInt(x[2], 10);
          if (cur && cur.el.lastElementChild) cur.el.lastElementChild.append(list); else root.append(list);
          stack.push(cur = { indent, el: list });
        }
        cur.el.append(inline(x[3], $('li'))); i++;
      }
      continue;
    }
    if (l.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      const head = cells(l), rows = []; i += 2; while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(cells(lines[i++]));
      root.append($('div', { className: 'tablewrap' }, $('table', {}, $('thead', {}, $('tr', {}, head.map(x => inline(x, $('th'))))), $('tbody', {}, rows.map(r => $('tr', {}, r.map(x => inline(x, $('td')))))))));
      continue;
    }
    const buf = [l]; i++; while (i < lines.length && lines[i].trim() && !isStart(lines[i])) buf.push(lines[i++]);
    root.append(inline(buf.join('\n'), $('p')));
  }
  return root;
}

// ---------------------------------------------------------------- composer and the '@' menu
const quote = n => /\s/.test(n) ? `"${n}"` : n;
/** Everything a person can add to a message here, in the order the menu shows it. */
function mentionItems() {
  const c = S.conv, ch = c.kind === 'channel', mem = S.ctx.mem, items = [], add = (group, icon_, label, desc, insert, action) => items.push({ group, icon: icon_, label, desc, insert, action });
  if (ch && mem) {
    const inB = new Set(mem.bots.map(b => b.id)), inU = new Set(mem.users.map(u => u.id));
    for (const b of mem.bots) add('Bots in this channel', 'bot', b.name, b.mention_only ? 'Answers only when mentioned' : 'Mention to address only this bot', '@' + b.handle);
    for (const b of S.bots) if (!inB.has(b.id) && b.role !== 'collaborator') add('Add a bot', 'plus', b.name, 'Add to this channel and mention', '@' + b.handle, () => addToChannel({ bot_ids: [b.id] }));
    for (const m of S.members) if (m.user_id !== S.me.id && inU.has(m.user_id)) add('People', 'user', m.display_name, 'In this channel', '@' + m.username);
    for (const m of S.members) if (!inU.has(m.user_id)) add('Add people', 'plus', m.display_name, 'Add to this channel and mention', '@' + m.username, () => addToChannel({ user_ids: [m.user_id] }));
  }
  for (const k of S.ctx.skills) add('Skills', 'zap', k.name, (k.of ? `@${k.of} · ` : '') + (k.description || 'Saved steps for a task'), '@skill:' + quote(k.name));
  if (!ch) for (const b of S.bots) if (b.id !== c.bot_id) add('Other bots', 'bot', b.name, 'Hand over part of the task', '@' + b.handle);
  for (const f of S.files.team === c.team_id ? S.files.list : []) { const cut = f.lastIndexOf('/'); add(c.kind === 'home' ? 'Files on your Overview computer' : 'Files on the project computer', 'file', f.slice(cut + 1), cut > 0 ? f.slice(0, cut) : 'workspace', '@file:' + quote(f)); }
  if (!ch) for (const m of S.members) if (m.user_id !== S.me.id) add('People', 'user', m.display_name, 'Mention a teammate', '@' + m.username);
  for (const s of S.ctx.secrets) add('Secrets', 'key', s.name, s.hosts ? 'Only sent to ' + s.hosts : 'No host bound yet', `{{secret:${s.name}}}`);
  return items;
}
async function loadFiles(then) {
  const team = S.conv.kind === 'home' ? 'home' : S.conv.team_id; if (S.files.team === team && Date.now() - S.files.at < 20e3) return;
  S.files = { team, at: Date.now(), list: S.files.team === team ? S.files.list : [] };
  try {
    const list = await api('GET', S.conv.kind === 'home' ? '/home/files' : `/teams/${team}/files`);
    if (S.files.team === team) { S.files.list = list; then(); }
  } catch { /* the menu works without files */ }
}
function renderDock() {
  const c = S.conv, dock = el('dock');
  if (c.kind === 'feed') return dock.replaceChildren();
  const direct = c.kind === 'dm' || c.kind === 'home';
  const ta = $('textarea', { rows: 1, ariaLabel: 'Message', placeholder: direct ? `Message ${c.name}` : 'Message the channel, or @mention one bot', value: S.drafts[c.id] || '' });
  const hl = $('div', { className: 'hl', ariaHidden: 'true' }), send = $('button', { className: 'send', title: 'Send (Enter)', ariaLabel: 'Send' }, icon('up', 18));
  // While a bot works you can keep typing. The mode decides what a new message does to the running task.
  const MODES = { auto: ['Auto', 'decides for you'], steer: ['Steer', 'adds to this task'], queue: ['Queue', 'runs afterwards'], interrupt: ['Interrupt', 'stops this task'] };
  let mode = 'auto';
  const hint = $('span', { className: 'hint' }), modeLabel = $('span');
  const modeBtn = $('button', { className: 'cbtn', type: 'button', hidden: true, title: 'What a new message does while the bot is working', onclick: () => popmenu(modeBtn, [{ head: 'While the bot is working' }, ...Object.entries(MODES).map(([k, [label, sub]]) => ({ label, hint: sub, checked: mode === k, onclick: () => { mode = k; S.dockSync(); ta.focus(); } }))]) }, modeLabel, icon('chev', 14));
  const stopBtn = $('button', { className: 'cbtn stop', type: 'button', hidden: true, title: 'Stop the bot', ariaLabel: 'Stop the bot', onclick: guard(async () => { await api('POST', `/conversations/${c.id}/stop`); refresh(); }) }, icon('stop', 14), 'Stop');
  const box = $('div', { className: 'composer' }, $('div', { className: 'ta' }, hl, ta),
    $('div', { className: 'crow' }, $('button', { className: 'cbtn', type: 'button', title: 'Add a bot, skill, file or person', onclick: () => atButton() }, icon('at', 16), 'Add'), hint, modeBtn, stopBtn, send));
  S.dockSync = () => {
    const run = S.state.running, names = run.map(r => r.bot).join(' and ');
    if (!run.length) mode = 'auto';
    hint.textContent = run.length ? `${names} ${run.length > 1 ? 'are' : 'is'} working. You can keep typing.` : 'Type @ to add bots, skills and files';
    modeBtn.hidden = !run.length; modeLabel.textContent = MODES[mode][0]; stopBtn.hidden = !run.some(r => r.mine);
  };
  let menu = null, shown = [], sel = 0, dismissed = -1;

  const sync = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 220) + 'px'; hl.replaceChildren(); plain(ta.value + (ta.value.endsWith('\n') ? ' ' : ''), hl); hl.scrollTop = ta.scrollTop; send.disabled = !ta.value.trim(); S.drafts[c.id] = ta.value; };
  const state = () => { if (ta.selectionStart !== ta.selectionEnd) return null; const pos = ta.selectionStart, m = /(^|[\s(])@([^\s@]*)$/.exec(ta.value.slice(0, pos)); return m ? { start: pos - m[2].length - 1, pos, q: m[2].toLowerCase() } : null; };
  const closeMenu = () => { if (menu) { menu.remove(); menu = null; } };
  const pick = guard(async it => {
    const st = state(); closeMenu(); if (!st) return;
    if (it.action) await it.action();
    const after = ta.value.slice(st.pos), text = it.insert + (after.startsWith(' ') ? '' : ' ');
    ta.value = ta.value.slice(0, st.start) + text + after; const at = st.start + text.length + (after.startsWith(' ') ? 1 : 0);
    ta.focus(); ta.setSelectionRange(at, at); sync();
  });
  const drawMenu = () => {
    const st = state(); if (!st || st.start === dismissed) return closeMenu();
    dismissed = -1; loadFiles(() => { if (menu) drawMenu(); });
    const q = st.q, rank = it => (it.insert.toLowerCase().replace(/^(@|\{\{)/, '').startsWith(q) || it.label.toLowerCase().startsWith(q)) ? 0 : 1;
    const hits = mentionItems().filter(it => !q || (it.insert + ' ' + it.label + ' ' + it.desc).toLowerCase().includes(q));
    const groups = []; for (const it of hits) { let g = groups.find(x => x.name === it.group); if (!g) groups.push(g = { name: it.group, items: [] }); g.items.push(it); }
    const list = $('div', { className: 'list', role: 'listbox' }); shown = [];
    for (const g of groups) {
      g.items.sort((a, b) => rank(a) - rank(b)); const cap = q ? 8 : 5, part = g.items.slice(0, cap);
      list.append($('div', { className: 'gh', textContent: g.name }));
      for (const it of part) { const n = shown.push(it) - 1; list.append($('button', { className: 'mrow', type: 'button', role: 'option', data: { n }, onmousemove: () => { if (sel !== n) { sel = n; mark(); } }, onmousedown: e => { e.preventDefault(); pick(it); } },
        $('span', { className: 'mi' }, icon(it.icon, 15)), $('span', { className: 'ml', textContent: it.label }), $('span', { className: 'md', textContent: it.desc }), $('span', { className: 'mt', textContent: it.insert }))); }
      if (g.items.length > cap) list.append($('div', { className: 'more', textContent: `${g.items.length - cap} more, keep typing to narrow down` }));
    }
    if (!shown.length) list.append($('div', { className: 'none', textContent: q ? `Nothing matches "@${st.q}".` : 'Nothing to add here yet. Skills, files and other bots show up in this list.' }));
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    const fresh = $('div', { className: 'mmenu' }, list, $('div', { className: 'keys' }, $('span', {}, $('kbd', { textContent: '↑↓' }), 'move'), $('span', {}, $('kbd', { textContent: 'Enter' }), 'add'), $('span', {}, $('kbd', { textContent: 'Esc' }), 'close')));
    if (menu) menu.replaceWith(fresh); else { box.append(fresh); sel = 0; } menu = fresh; mark();
  };
  const mark = () => { if (!menu) return; for (const r of menu.querySelectorAll('.mrow')) { const on = +r.dataset.n === sel; r.classList.toggle('sel', on); r.ariaSelected = on; if (on) r.scrollIntoView({ block: 'nearest' }); } };
  const atButton = () => { ta.focus(); if (menu) return closeMenu(); const p = ta.selectionStart, before = ta.value.slice(0, p), ins = (before && !/\s$/.test(before) ? ' ' : '') + '@'; ta.setRangeText(ins, p, ta.selectionEnd, 'end'); dismissed = -1; sync(); drawMenu(); };
  const submit = guard(async allow => {
    const text = ta.value.trim(); if (!text) return;
    ta.value = ''; closeMenu(); sync(); S.first = true;
    try { await api('POST', `/conversations/${c.id}/messages`, { content: text, mode, allow_secret: allow === true }); refresh(); }
    catch (e) {
      if (!ta.value) { ta.value = text; sync(); }
      if (!e.detail || e.detail.code !== 'secret') throw e;
      // The server held the message back. Nothing was stored; the person decides what happens with it.
      secretGuard(e.detail.found, { swap: (value, placeholder) => { ta.value = ta.value.split(value).join(placeholder); sync(); }, send: yes => submit(yes), focus: () => ta.focus() });
    }
  });
  ta.addEventListener('input', () => { sel = 0; sync(); drawMenu(); });
  ta.addEventListener('scroll', () => { hl.scrollTop = ta.scrollTop; });
  ta.addEventListener('click', drawMenu);
  ta.addEventListener('keyup', e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) drawMenu(); });
  ta.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== ta) closeMenu(); }, 120));
  ta.addEventListener('keydown', e => {
    if (e.isComposing) return;
    if (menu) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (shown.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length; mark(); } return; }
      if ((e.key === 'Enter' || e.key === 'Tab') && shown.length) { e.preventDefault(); return pick(shown[sel]); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); dismissed = state() ? state().start : -1; return closeMenu(); }
    }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  send.onclick = submit;
  const insert = text => { ta.focus(); ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end'); sync(); };
  const chips = $('div', { id: 'chips' });
  if (c.kind === 'dm') for (const k of S.ctx.skills.slice(0, 4)) chips.append($('button', { className: 'chip', title: k.description, onclick: () => insert('@skill:' + quote(k.name) + ' ') }, icon('zap', 15), $('span', { textContent: k.name })));
  else for (const b of (S.ctx.mem ? S.ctx.mem.bots : []).slice(0, 4)) chips.append($('button', { className: 'chip', onclick: () => insert('@' + b.handle + ' ') }, icon('bot', 15), $('span', { textContent: 'Ask ' + b.name })));
  chips.append($('button', { className: 'chip', onclick: atButton }, icon('at', 15), $('span', { textContent: 'See what you can add' })));
  dock.replaceChildren($('button', { id: 'jump', title: 'Jump to the latest message', ariaLabel: 'Jump to the latest message', onclick: () => { el('view').scrollTop = el('view').scrollHeight; } }, icon('down', 16)), box, chips,
    $('div', { className: 'foot', textContent: (c.kind === 'dm' || c.kind === 'home') ? 'Bots can be wrong. Check what matters before you allow or use it.' : 'Everyone in this channel reads what is said here.' }));
  sync(); S.dockSync(); if (!isNarrow()) ta.focus();
}

/** A message that seems to hold a credential: keep it out of the chat and give the bot a placeholder instead. */
function secretGuard(found, { swap, send, focus }) {
  const c = S.conv, b = S.bot, canSave = c.kind === 'dm' && b && b.role !== 'collaborator';
  const veil = v => v.startsWith('-----') ? 'key block' : v.length > 12 ? v.slice(0, 4) + '…' + v.slice(-2) : '•'.repeat(6);
  let open = found.length;
  const done = (row, name) => { row.querySelector('.act').replaceChildren($('span', { className: 'tag ok', textContent: `{{secret:${name}}}` })); if (--open === 0) { d.close(); send(false); } };
  const rows = found.map(f => {
    const row = $('div', { className: 'li' }, $('div', { className: 'tx' }, f.kind[0].toUpperCase() + f.kind.slice(1), $('small', { className: 'mono', textContent: veil(f.value) })), $('span', { className: 'act' }));
    const act = row.lastChild;
    if (f.name) act.append($('button', { className: 'btn sm primary', textContent: 'Use placeholder', title: `This is the stored secret ${f.name}`, onclick: () => { swap(f.value, `{{secret:${f.name}}}`); done(row, f.name); } }));
    else if (canSave) act.append($('button', { className: 'btn sm primary', textContent: 'Save as secret', onclick: guard(async () => {
      const v = await form({ title: 'Save as secret', intro: 'The value is stored encrypted outside the sandbox. The bot only gets the placeholder; the app fills in the real value when the bot sends a request to one of these hosts.', ok: 'Save secret',
        fields: [{ name: 'name', label: 'Name', required: true, mono: true, value: f.suggest, hint: 'Capitals, digits and underscores.' }, { name: 'hosts', label: 'Hosts it may be sent to', mono: true, value: f.hosts, placeholder: 'api.example.com', hint: 'Comma-separated. Without a host the secret cannot be used.' }],
        onSubmit: async x => { x.name = x.name.toUpperCase(); await api('POST', `/bots/${b.id}/secrets`, { ...x, value: f.value }); } });
      if (!v) return;
      swap(f.value, `{{secret:${v.name}}}`); await loadCtx(c); done(row, v.name); }) }));
    else act.append($('span', { className: 'tag warn', textContent: 'remove it' }));
    return row;
  });
  const d = openDialog('', $('div', { className: 'pad' }, $('h2', { textContent: 'This looks like a secret' }),
    $('p', { className: 'intro', textContent: 'Your message was not sent. A secret in the chat is stored with the conversation and read by the model. ' + (canSave ? 'Save it as a secret instead: the bot gets a placeholder, and the real value is filled in outside the sandbox.' : found.some(f => f.name) ? 'Use the placeholder instead of the value.' : 'Take it out of the message. An owner or manager of the bot can add it under Secrets, and then you use the placeholder.') }),
    rows, $('div', { className: 'dfoot', style: 'margin-top:16px' }, $('button', { className: 'btn danger', textContent: 'Send anyway', title: 'It is not a secret, or you accept that it ends up in the chat', onclick: () => { d.close(); send(true); } }),
      $('button', { className: 'btn primary', textContent: 'Edit message', onclick: () => d.close() }))));
  d.addEventListener('close', focus);
}

// ---------------------------------------------------------------- bot settings
function closeDrawer() { const d = el('drawer'); if (d) { d.classList.remove('open'); d.replaceChildren(); } clearInterval(S.screenTimer); }
async function openDrawer(bot, tab = 'setup') {
  const p = el('drawer'), manage = bot.role !== 'collaborator', again = t => openDrawer(bot, t || tab);
  clearInterval(S.screenTimer);
  const tabs = [['setup', 'Setup'], manage && bot.is_team && ['sharing', 'Sharing'], ['knowledge', 'Memory and skills'], ['routines', 'Routines'], manage && ['connections', 'Secrets and tools'], bot.computer_use && ['screen', 'Screen']].filter(Boolean);
  const body = $('div', { className: 'dbody' });
  p.replaceChildren($('div', { className: 'dhead' }, avatar(bot.name, 'bot'), $('div', { className: 'nm' }, $('b', { textContent: bot.name }), $('small', { textContent: `@${bot.handle} · ${bot.is_team ? 'Team Bot' : 'Personal bot'} · you are ${bot.role}` })), ib('x', 'Close', closeDrawer)),
    $('div', { className: 'tabs', role: 'tablist' }, tabs.map(([id, label]) => $('button', { className: id === tab ? 'on' : '', role: 'tab', textContent: label, onclick: () => again(id) }))), body);
  p.classList.add('open');
  const head = (title, addLabel, add) => $('h4', {}, title, add && $('button', { className: 'btn sm', onclick: guard(add) }, icon('plus', 14), addLabel));
  const hint = text => $('p', { className: 'hint', textContent: text });
  const li = (text, sub, ...tail) => $('div', { className: 'li' }, $('div', { className: 'tx' }, text, sub && $('small', { textContent: sub })), tail);
  const del = (path, what) => ib('trash', 'Remove', guard(async () => { if (!await confirmBox({ title: `Remove ${what}?`, text: 'This cannot be undone.', ok: 'Remove', danger: true })) return; await api('DELETE', path); if (S.conv) await loadCtx(S.conv); again(); }), 15);
  const none = text => $('div', { className: 'none', textContent: text });
  const get = path => api('GET', `/bots/${bot.id}/${path}`);

  if (tab === 'setup') {
    if (!manage) { put(body, head('Instructions'), $('p', { className: 'hint', style: 'white-space:pre-wrap', textContent: bot.instructions || 'No instructions written.' }), hint('Only the owner and managers can change this bot.')); return; }
    const ins = $('textarea', { rows: 9, value: bot.instructions, placeholder: 'What this bot does and how it should behave.' });
    const eps = (bot.is_home && S.home && S.home.endpoints) ? S.home.endpoints : S.endpoints;
    const name = $('input', { value: bot.name }), ep = $('select', {}, (eps || []).map(e => $('option', { value: e.id, textContent: `${e.name}${e.project ? ' (' + e.project + ')' : ''} · ${e.default_model}`, selected: e.id === bot.endpoint_id })));
    if (!(eps || []).some(e => e.id === bot.endpoint_id)) ep.prepend($('option', { value: bot.endpoint_id, textContent: 'Endpoint of another member', selected: true }));
    const combo = modelCombo({ value: bot.model, placeholder: 'Empty = the default model of the endpoint', getEndpointId: () => ep.value });
    const model = combo.input;
    ep.addEventListener('change', () => combo.reload());
    combo.reload();
    const mode = $('select', {}, $('option', { value: 'ask', textContent: 'Ask before risky actions', selected: bot.approval_mode === 'ask' }), $('option', { value: 'auto', textContent: 'Run everything without asking', selected: bot.approval_mode === 'auto' }));
    const cu = $('input', { type: 'checkbox', checked: bot.computer_use }), fld = (label, input, small) => $('label', { className: 'field' }, $('span', { textContent: label }), input, small && $('small', { textContent: small }));
    put(body, fld('Name', name), fld('Instructions', ins), fld('Model endpoint', ep), $('label', { className: 'field' }, $('span', { textContent: 'Model override' }), combo.box, combo.status), fld('Approvals', mode, 'Risky: shell commands, writing files, HTTP requests, routines, untrusted MCP tools and acting on the screen.'),
      $('label', { className: 'check' }, cu, $('span', {}, 'Own screen and computer use', $('small', { textContent: 'A virtual screen with a browser. Needs a model with image input and the Docker sandbox.' }))),
      $('div', { className: 'rowbtns' }, $('button', { className: 'btn primary', textContent: 'Save', onclick: guard(async () => {
        if (bot.is_home) {
          const body = { model: model.value.trim(), computer_use: cu.checked }; if (+ep.value !== bot.endpoint_id) body.endpoint_id = +ep.value;
          const h = await api('PATCH', '/home', body); Object.assign(bot, h.bot); S.home = h;
          Object.assign(bot, await api('PATCH', `/bots/${bot.id}`, { name: name.value.trim() || bot.name, instructions: ins.value, approval_mode: mode.value }));
        } else {
          const data = { name: name.value.trim() || bot.name, instructions: ins.value, model: model.value.trim(), approval_mode: mode.value, computer_use: cu.checked }; if (+ep.value !== bot.endpoint_id) data.endpoint_id = +ep.value;
          Object.assign(bot, await api('PATCH', `/bots/${bot.id}`, data));
        }
        if (S.conv && (S.conv.kind === 'dm' || S.conv.kind === 'home') && S.conv.bot_id === bot.id) S.conv.name = bot.name; renderSide(); renderTop(); toast('Saved'); again(); }) }),
        bot.role === 'owner' && !bot.is_home && $('button', { className: 'btn danger', textContent: 'Delete bot', onclick: guard(async () => { if (!await confirmBox({ title: `Delete ${bot.name}?`, text: 'The bot is removed for everyone, with all its chats, memory, skills and routines. This cannot be undone.', ok: 'Delete bot', danger: true })) return; await api('DELETE', `/bots/${bot.id}`); store.set('tb_last_' + bot.team_id, null); await boot(); }) })));
  }
  if (tab === 'sharing') {
    const sh = await get('share'), pub = $('input', { type: 'checkbox', checked: sh.published });
    const acc = $('select', {}, $('option', { value: 'team', textContent: 'Everyone in the project', selected: sh.access === 'team' }), $('option', { value: 'selected', textContent: 'Only the people chosen below', selected: sh.access === 'selected' }));
    const rows = S.members.filter(m => m.user_id !== bot.owner_id).map(m => { const cur = sh.members.find(x => x.user_id === m.user_id);
      const r = $('select', { style: 'width:150px', data: { uid: m.user_id } }, [['', 'Default'], ['collaborator', 'Collaborator'], ['manager', 'Manager']].map(([v, t]) => $('option', { value: v, textContent: t, selected: (cur ? cur.role : '') === v })));
      return li(m.display_name, '@' + m.username, r); });
    put(body, head('Publish'), $('label', { className: 'switch', style: 'margin:6px 0 14px' }, pub, 'Published'), hint('A draft is visible only to its owner. Once published, each person gets a private chat with the bot that nobody else can read.'),
      head('Who can use it'), acc, hint('Collaborators chat with the bot. Managers can also change it, see its secrets list and approve team learnings.'), rows.length ? rows : none('You are the only person in this project.'),
      $('div', { className: 'rowbtns', style: 'margin-top:14px' }, $('button', { className: 'btn primary', textContent: 'Save sharing', onclick: guard(async () => {
        await api('PUT', `/bots/${bot.id}/share`, { published: pub.checked, access: acc.value, members: rows.map(r => r.querySelector('select')).filter(s => s.value).map(s => ({ user_id: +s.dataset.uid, role: s.value })) });
        bot.published = pub.checked; bot.access = acc.value; renderSide(); renderTop(); toast(pub.checked ? 'Published' : 'Saved as draft'); }) })));
  }
  if (tab === 'knowledge') {
    const [mems, skills] = await Promise.all([get('memories'), get('skills')]);
    const review = (m, approve) => guard(async () => { await api('POST', `/memories/${m.id}/review`, { approve }); again(); });
    put(body, head('Memory', 'Add', async () => { if (await form({ title: 'Add a memory', ok: 'Remember', fields: [{ name: 'content', label: 'What should the bot remember?', type: 'textarea', required: true }, { name: 'scope', label: 'For whom', type: 'select', value: 'personal', options: [{ value: 'personal', label: 'Only me' }, { value: 'team', label: 'Everyone who uses this bot' + (manage ? '' : ' (needs review)') }] }], onSubmit: v => api('POST', `/bots/${bot.id}/memories`, v) })) again(); }),
      hint('Team memory is shared. Personal memory is only yours. Team learnings from collaborators wait for review by an owner or manager.'),
      mems.length ? mems.map(m => li(m.content, m.status === 'pending' ? 'Learning proposed by ' + m.proposed_by : m.scope === 'team' ? 'Team' : 'Only you',
        m.status === 'pending' && manage && [ib('check', 'Approve', review(m, true), 15), ib('x', 'Reject', review(m, false), 15)], m.status === 'pending' && !manage && $('span', { className: 'tag warn', textContent: 'in review' }),
        m.status !== 'pending' && (m.scope === 'personal' || manage) && del(`/memories/${m.id}`, 'this memory'))) : none('Nothing remembered yet.'),
      head('Skills', manage && 'Add', manage && (async () => { if (await form({ title: 'New skill', intro: 'Steps for a task that comes back. Add it to a message with @skill.', ok: 'Save skill', fields: [{ name: 'name', label: 'Name', required: true, placeholder: 'weekly-report' }, { name: 'description', label: 'One-line description' }, { name: 'instructions', label: 'Step-by-step instructions', type: 'textarea', rows: 7, required: true }], onSubmit: v => api('POST', `/bots/${bot.id}/skills`, v) })) { await loadCtx(S.conv); again(); } })),
      hint('The bot saves a skill itself after a task worked, or you write one here.'),
      skills.length ? skills.map(k => li(k.name, k.description, ib('copy', 'Copy the @skill mention', () => copyText('@skill:' + quote(k.name), 'Mention copied'), 15), manage && del(`/skills/${k.id}`, 'this skill'))) : none('No skills yet.'));
  }
  if (tab === 'routines') {
    const rs = await get('routines'), here = S.conv && S.conv.kind !== 'feed';
    put(body, head('My routines', here && 'Add', here && (async () => { if (await form({ title: 'New routine', intro: `Runs on a schedule in "${S.conv.kind === 'dm' ? S.conv.name : '# ' + S.conv.name}", the conversation that is open now.`, ok: 'Schedule', fields: [{ name: 'name', label: 'Name', required: true, placeholder: 'standup' }, { name: 'cron', label: 'Schedule (crontab)', required: true, value: '30 8 * * 1-5', mono: true, hint: 'minute hour day month weekday. The default is 08:30 on weekdays.' }, { name: 'prompt', label: 'What should the bot do each time?', type: 'textarea', required: true }], onSubmit: v => api('POST', `/bots/${bot.id}/routines`, { ...v, conversation_id: S.conv.id }) })) again(); })),
      hint('A routine is yours: it runs as you, in the conversation where you created it. Times are Europe/Amsterdam.'),
      rs.length ? rs.map(r => { const on = $('input', { type: 'checkbox', checked: r.enabled, onchange: guard(async () => { await api('PATCH', `/routines/${r.id}`, { enabled: on.checked }); again(); }) });
        const sub = `${r.cron_text || r.cron} (${r.cron}) · ${r.enabled ? ('Next ' + (r.next_run_text || '—')) : 'paused'} · Last ${r.last_run_text || 'never'} · ${r.prompt}`;
        return li(r.name, sub, $('label', { className: 'switch', title: r.enabled ? 'Enabled' : 'Paused' }, on), ib('play', 'Run now', guard(async () => { await api('POST', `/routines/${r.id}/run`); toast('Started'); }), 15), ib('edit', 'Edit', guard(() => editRoutine(r, again)), 15), del(`/routines/${r.id}`, 'this routine')); }) : none('No routines yet.'));
  }
  if (tab === 'connections') {
    const [secrets, mcp] = await Promise.all([get('secrets'), get('mcp')]);
    put(body, head('Secrets', 'Add', async () => { if (await form({ title: 'New secret', intro: 'The value never enters the sandbox or the model. The bot uses it by name in an HTTP request and it is filled in outside the sandbox.', ok: 'Save secret', fields: [{ name: 'name', label: 'Name', required: true, mono: true, placeholder: 'API_TOKEN', hint: 'Capitals, digits and underscores.' }, { name: 'value', label: 'Value', type: 'password', required: true, hint: 'Stored encrypted. It is never shown again.' }, { name: 'hosts', label: 'Hosts it may be sent to', mono: true, placeholder: 'api.github.com', hint: 'Comma-separated. Without a host the secret cannot be used.' }], onSubmit: v => api('POST', `/bots/${bot.id}/secrets`, { ...v, name: v.name.toUpperCase() }) })) { await loadCtx(S.conv); again(); } }),
      hint('Everyone who can use the bot can make requests with its secrets to the bound hosts.'),
      secrets.length ? secrets.map(s => li(s.name, s.hosts ? 'Only sent to ' + s.hosts : 'No host bound yet', del(`/secrets/${s.id}`, 'this secret'))) : none('No secrets.'),
      head('MCP servers', 'Add', async () => { const r = await form({ title: 'New MCP server', intro: 'A streamable-HTTP MCP server. Its tools become tools of this bot.', ok: 'Connect', fields: [{ name: 'name', label: 'Name', required: true, mono: true, placeholder: 'tickets', hint: 'Lowercase letters, digits, - and _.' }, { name: 'url', label: 'URL', required: true, mono: true, value: 'http://localhost:9000/mcp' }, { name: 'trusted', label: 'Trusted', type: 'checkbox', hint: 'Its tools run without an approval card.' }], onSubmit: v => api('POST', `/bots/${bot.id}/mcp`, v) }); if (r) { toast(r.error || (r.tools.length ? 'Tools: ' + r.tools.join(', ') : 'Connected, but the server lists no tools'), r.error ? 'bad' : ''); again(); } }),
      mcp.length ? mcp.map(s => li(s.name, s.url, $('span', { className: 'tag', textContent: s.trusted ? 'trusted' : 'asks first' }), del(`/mcp/${s.id}`, 'this server'))) : none('No MCP servers.'));
  }
  if (tab === 'screen') {
    const img = $('img', { className: 'shot', alt: 'Live view of the bot screen' }), note = hint('Loading…');
    const tick = () => { const n = new Image(); n.onload = () => { img.src = n.src; note.textContent = 'Live view, refreshed every 3 seconds. Everyone who can use this bot can watch.'; }; n.onerror = () => { note.textContent = 'The screen is not available. It needs the Docker sandbox.'; }; n.src = withToken(`/bots/${bot.id}/screen.jpg?t=${Date.now()}`); };
    const open = $('button', { className: 'btn sm', title: 'Interactive screen with mouse and keyboard, for example to sign in to the browser yourself', onclick: () => window.open(withToken(`/bots/${bot.id}/screen/vnc/`), '_blank', 'noopener') }, icon('screen', 14), 'Open scherm');
    put(body, $('h4', {}, 'Screen', open), img, note); tick(); S.screenTimer = setInterval(tick, 3000);
  }
}

// ---------------------------------------------------------------- search (Ctrl K)
function openPalette() {
  if (!S.me || document.querySelector('dialog.palette')) return;
  closePop();
  const input = $('input', { placeholder: 'Search bots, channels, projects and actions', ariaLabel: 'Search' }), list = $('div', { className: 'plist' });
  const go = fn => () => { d.close(); guard(fn)(); };
  const all = [
    ...S.bots.map(b => ({ g: 'Bots', icon: 'bot', label: b.name, hint: '@' + b.handle, run: () => openBot(b) })),
    ...S.channels.map(c => ({ g: 'Channels', icon: 'hash', label: c.name, run: () => openConv(c, null) })),
    ...S.bots.filter(b => b.is_team && b.published).map(b => ({ g: 'Activity', icon: 'pulse', label: b.name + ' activity', run: () => openFeed(b) })),
    ...S.teams.filter(t => !S.team || t.id !== S.team.id).map(t => ({ g: 'Projects', icon: 'grid', label: (t.client ? t.client + ' / ' : '') + t.name, hint: 'Switch', run: () => switchTeam(t.id) })),
    { g: 'Actions', icon: 'grid', label: 'Home', run: showHome },
    { g: 'Actions', icon: 'clock', label: 'Routines', run: showRoutines },
    ...(S.me && S.me.can_view_costs ? [{ g: 'Actions', icon: 'coin', label: 'Costs', run: showCosts }] : []),
    ...(S.team ? [{ g: 'Actions', icon: 'plus', label: 'New bot', run: newBot }, { g: 'Actions', icon: 'plus', label: 'New channel', run: newChannel }, { g: 'Actions', icon: 'plug', label: 'New endpoint', run: newEndpoint }, { g: 'Actions', icon: 'users', label: 'People and invite code', run: projectDialog }] : []),
    { g: 'Actions', icon: 'plus', label: 'New project', run: newProject }, { g: 'Actions', icon: 'key', label: 'Join a project with an invite code', run: joinProject }, { g: 'Actions', icon: 'out', label: 'Sign out', run: signOut }];
  let shown = [], sel = 0;
  const draw = () => {
    const q = input.value.trim().toLowerCase(); shown = all.filter(x => !q || (x.label + ' ' + (x.hint || '') + ' ' + x.g).toLowerCase().includes(q)); sel = Math.min(sel, Math.max(0, shown.length - 1));
    list.replaceChildren(); let g = '';
    shown.forEach((x, n) => { if (x.g !== g) list.append($('div', { className: 'gh', textContent: g = x.g }));
      list.append($('button', { className: 'mrow' + (n === sel ? ' sel' : ''), onmousemove: () => { if (sel !== n) { sel = n; draw(); } }, onclick: go(x.run) }, $('span', { className: 'mi' }, icon(x.icon, 15)), $('span', { className: 'ml', textContent: x.label }), $('span', { className: 'md', textContent: x.hint || '' }))); });
    if (!shown.length) list.append($('div', { className: 'none', style: 'padding:14px 12px', textContent: 'Nothing found.' }));
    const cur = list.querySelector('.sel'); if (cur) cur.scrollIntoView({ block: 'nearest' });
  };
  input.oninput = () => { sel = 0; draw(); };
  input.onkeydown = e => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (shown.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length; draw(); } } else if (e.key === 'Enter' && shown[sel]) { e.preventDefault(); go(shown[sel].run)(); } };
  const d = openDialog('palette', $('div', { className: 'pin' }, icon('search'), input), list);
  draw(); input.focus();
}
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
  else if (e.key === 'Escape' && !document.querySelector('dialog[open]')) { if (S.pop) closePop(); else if (el('drawer') && el('drawer').classList.contains('open')) closeDrawer(); }
});
matchMedia('(max-width: 860px)').addEventListener('change', () => { if (el('app')) { el('app').classList.remove('sideopen'); renderTop(); } });

// Installable app: the browser offers it once the service worker is in place (needs https or localhost).
addEventListener('beforeinstallprompt', e => { e.preventDefault(); S.install = e; });
addEventListener('appinstalled', () => { S.install = null; toast('TeamBots is installed'); });
if ('serviceWorker' in navigator) addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));

// After a sign-in redirect the token (or an error) arrives in the URL fragment; take it, then clean the URL.
(() => { try { if (!location.hash) return; const h = new URLSearchParams(location.hash.slice(1));
  const t = h.get('tb_oauth'), e = h.get('tb_oauth_error');
  if (t) { store.set('tb_token', t); S.token = t; }
  if (e) S.authError = e;
  if (t || e) history.replaceState(null, '', location.pathname + location.search);
} catch (x) { /* ignore */ } })();

boot().catch(e => { document.body.replaceChildren($('div', { id: 'auth' }, $('div', { className: 'authbox' }, $('h1', { textContent: 'TeamBots is not reachable' }), $('p', { textContent: e.message }), $('button', { className: 'btn primary', textContent: 'Try again', onclick: () => location.reload() })))); });
