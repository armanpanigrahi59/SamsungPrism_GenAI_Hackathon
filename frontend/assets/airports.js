// Airport explorer (/airports): every airport in the offline world flight
// model, filterable by continent / country / free text, with a details
// drawer listing where each airport flies and one-click links into the
// flight search (/flights?from=...&to=...).
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const fmt = (n) => (typeof n === 'number' ? n.toLocaleString() : '—');
  const PAGE_SIZE = 48;

  const state = { continent: '', country: '', q: '', sort: 'routes', page: 1, routesOnly: true };
  let continentNames = {};
  let seq = 0;            // ignore responses that arrive after a newer request
  let pages = 1;

  async function getJson(url) {
    const resp = await fetch(url, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return resp.json();
  }

  function flag(cc) {
    if (!/^[A-Z]{2}$/.test(cc || '')) return '';
    return String.fromCodePoint(...[...cc].map((c) => 0x1f1a5 + c.charCodeAt(0)));
  }

  // ---- URL <-> state (so filters survive reload and can be shared) -------
  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.continent = (p.get('continent') || '').toUpperCase().slice(0, 2);
    state.country = (p.get('country') || '').toUpperCase().slice(0, 2);
    state.q = (p.get('q') || '').slice(0, 80);
    if (['routes', 'name', 'code'].includes(p.get('sort'))) state.sort = p.get('sort');
    state.page = Math.max(1, parseInt(p.get('page'), 10) || 1);
    if (p.get('all') === '1') state.routesOnly = false;
  }

  function writeUrl() {
    const p = new URLSearchParams();
    if (state.continent) p.set('continent', state.continent);
    if (state.country) p.set('country', state.country);
    if (state.q) p.set('q', state.q);
    if (state.sort !== 'routes') p.set('sort', state.sort);
    if (state.page > 1) p.set('page', String(state.page));
    if (!state.routesOnly) p.set('all', '1');
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  // ---- header stats + filters -------------------------------------------
  async function loadStats() {
    try {
      const v = await getJson('/api/version');
      $('statAirports').textContent = fmt(v.airports);
      $('statCountries').textContent = fmt(v.countries);
      $('statRoutes').textContent = fmt(v.routes);
      $('statAirlines').textContent = fmt(v.airlines);
    } catch { /* the version banner already explains a dead server */ }
  }

  function renderContinentTabs() {
    const tabs = $('continentTabs');
    const entries = [['', 'All'], ...Object.entries(continentNames)];
    tabs.innerHTML = entries.map(([code, name]) => {
      const on = code === state.continent;
      return `<button type="button" role="tab" data-continent="${esc(code)}" class="${on ? 'active' : ''}"
        aria-selected="${on}">${esc(name)}</button>`;
    }).join('');
  }

  async function loadCountries() {
    const select = $('countrySelect');
    try {
      const data = await getJson('/api/countries?continent=' + encodeURIComponent(state.continent));
      continentNames = data.continents || continentNames;
      renderContinentTabs();
      const list = (data.countries || []).slice().sort((a, b) => a.name.localeCompare(b.name));
      select.innerHTML = '<option value="">All countries</option>' + list.map((c) =>
        `<option value="${esc(c.code)}">${esc(c.name)} (${c.airports})</option>`).join('');
      if (state.country && !list.some((c) => c.code === state.country)) state.country = '';
      select.value = state.country;
    } catch {
      renderContinentTabs();
    }
  }

  // ---- grid ---------------------------------------------------------------
  function card(a) {
    const badge = a.has_routes
      ? `<span class="badge">${fmt(a.routes)} route${a.routes === 1 ? '' : 's'}</span>`
      : '<span class="badge none">No scheduled flights</span>';
    const fly = a.has_routes
      ? `<a class="primary" href="/flights?from=${esc(a.iata)}">Fly from</a>
         <a href="/flights?to=${esc(a.iata)}">Fly to</a>`
      : '';
    return `<div class="airport-card" data-iata="${esc(a.iata)}">
      <div class="top"><span class="code">${esc(a.iata)}</span>${badge}</div>
      <div class="city">${esc(a.city)}</div>
      <div class="name">${esc(a.name)}<br>${flag(a.country)} ${esc(a.country_name)}</div>
      <div class="actions">${fly}<button type="button" data-details="${esc(a.iata)}">Details</button></div>
    </div>`;
  }

  async function load({ scroll = false } = {}) {
    const my = ++seq;
    writeUrl();
    const grid = $('airportGrid');
    grid.classList.add('loading');
    const params = new URLSearchParams({
      continent: state.continent, country: state.country, q: state.q, sort: state.sort,
      page: String(state.page), page_size: String(PAGE_SIZE), routes_only: state.routesOnly ? '1' : '0',
    });
    let data;
    try {
      data = await getJson('/api/airports/browse?' + params);
    } catch (err) {
      if (my !== seq) return;
      grid.classList.remove('loading');
      $('exploreCount').textContent = 'Could not load airports.';
      grid.innerHTML = `<div class="explorer-empty">The server didn't answer (${esc(err.message)}).
        <button type="button" class="btn-ghost small" id="retryLoad">Try again</button></div>`;
      $('pager').hidden = true;
      return;
    }
    if (my !== seq) return;
    grid.classList.remove('loading');
    state.page = data.page;
    pages = data.pages;
    const start = data.total ? (data.page - 1) * data.page_size + 1 : 0;
    const end = Math.min(data.total, data.page * data.page_size);
    const where = [state.country ? $('countrySelect').selectedOptions[0]?.textContent.replace(/\s*\(\d+\)$/, '') : '',
      !state.country && state.continent ? continentNames[state.continent] : ''].filter(Boolean).join('');
    $('exploreCount').textContent = data.total
      ? `Showing ${fmt(start)}–${fmt(end)} of ${fmt(data.total)} airports${where ? ' in ' + where : ''}`
        + (state.q ? ` matching “${state.q}”` : '')
      : '';
    if (!data.total) {
      grid.innerHTML = `<div class="explorer-empty">No airports match${state.q ? ` “${esc(state.q)}”` : ''}
        with these filters.
        <button type="button" class="btn-ghost small" id="clearFilters">Clear filters</button></div>`;
    } else {
      grid.innerHTML = data.airports.map(card).join('');
    }
    $('pager').hidden = data.pages <= 1;
    $('pageInfo').textContent = `Page ${data.page} of ${data.pages}`;
    $('prevPage').disabled = data.page <= 1;
    $('nextPage').disabled = data.page >= data.pages;
    if (scroll) $('continentTabs').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function resetFilters() {
    Object.assign(state, { continent: '', country: '', q: '', page: 1, routesOnly: true });
    $('exploreSearch').value = '';
    $('routesOnly').checked = true;
    loadCountries().then(() => load());
  }

  // ---- details drawer -------------------------------------------------------
  let lastFocus = null;
  function openDrawer() {
    lastFocus = document.activeElement;
    $('drawer').classList.add('open');
    $('drawerBackdrop').classList.add('open');
    $('drawer').setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    $('drawerClose').focus();
  }
  function closeDrawer() {
    if (!$('drawer').classList.contains('open')) return;
    $('drawer').classList.remove('open');
    $('drawerBackdrop').classList.remove('open');
    $('drawer').setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  async function showDetails(code) {
    $('drawerTitle').textContent = code;
    $('drawerSub').textContent = 'Loading…';
    $('drawerBody').innerHTML = '';
    openDrawer();
    let a;
    try {
      a = await getJson('/api/airport/' + encodeURIComponent(code) + '?n=25');
    } catch (err) {
      $('drawerSub').textContent = `Could not load ${code} (${err.message}).`;
      return;
    }
    $('drawerTitle').textContent = `${a.iata} — ${a.city}`;
    $('drawerSub').innerHTML = `${esc(a.name)}<br>${flag(a.country)} ${esc(a.country_name)}
      · ${esc(continentNames[a.continent] || a.continent)} · ${a.lat.toFixed(2)}, ${a.lon.toFixed(2)}`;
    const dests = a.destinations || [];
    const head = a.has_routes
      ? `<div class="drawer-actions">
           <a class="btn-primary small" href="/flights?from=${esc(a.iata)}">Fly from ${esc(a.iata)}</a>
           <a class="btn-ghost small" href="/flights?to=${esc(a.iata)}">Fly to ${esc(a.iata)}</a>
         </div>
         <h4 class="drawer-h">Top destinations · ${fmt(a.routes)} routes in total</h4>`
      : '<p class="hint">The route network has no scheduled passenger flights from this airport.</p>';
    const rows = dests.map((d) => `<div class="dest-row">
        <div><b>${esc(d.iata)}</b> ${esc(d.city)}<small>${esc(d.country)} · ${fmt(d.weekly_flights)} flights/week
          · ${esc((d.airlines || []).join(', '))}</small></div>
        <a href="/flights?from=${esc(a.iata)}&to=${esc(d.iata)}">Search →</a>
      </div>`).join('');
    $('drawerBody').innerHTML = head + rows;
  }

  // ---- wiring ----------------------------------------------------------------
  function init() {
    if (window.PrismAssistant) window.PrismAssistant.checkServerVersion();
    readUrl();
    $('exploreSearch').value = state.q;
    $('sortSelect').value = state.sort;
    $('routesOnly').checked = state.routesOnly;
    renderContinentTabs();

    $('continentTabs').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-continent]');
      if (!b || b.dataset.continent === state.continent) return;
      state.continent = b.dataset.continent;
      state.country = '';
      state.page = 1;
      renderContinentTabs();
      loadCountries().then(() => load());
    });
    $('countrySelect').addEventListener('change', (e) => { state.country = e.target.value; state.page = 1; load(); });
    $('sortSelect').addEventListener('change', (e) => { state.sort = e.target.value; state.page = 1; load(); });
    $('routesOnly').addEventListener('change', (e) => { state.routesOnly = e.target.checked; state.page = 1; load(); });
    let typing = null;
    $('exploreSearch').addEventListener('input', (e) => {
      clearTimeout(typing);
      typing = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; load(); }, 180);
    });
    $('exploreSearch').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { clearTimeout(typing); state.q = e.target.value.trim(); state.page = 1; load(); }
    });
    $('prevPage').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; load({ scroll: true }); } });
    $('nextPage').addEventListener('click', () => { if (state.page < pages) { state.page += 1; load({ scroll: true }); } });

    $('airportGrid').addEventListener('click', (e) => {
      const d = e.target.closest('[data-details]');
      if (d) { showDetails(d.dataset.details); return; }
      if (e.target.closest('#clearFilters')) { resetFilters(); return; }
      if (e.target.closest('#retryLoad')) { loadCountries().then(() => load()); }
    });
    $('drawerClose').addEventListener('click', closeDrawer);
    $('drawerBackdrop').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

    loadStats();
    loadCountries().then(() => load());
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
