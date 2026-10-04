/*
 * Flights page: search form, results, booking -- on top of assistant.js.
 *
 * Every search goes through the agent (a complete text turn over the
 * WebSocket, exactly like typing in the talk box), so the timeline and the
 * "what the agent understood" card always show what happened. Results come
 * back as "tool_result" frames. If the socket is down or the agent doesn't
 * answer within a few seconds, the page falls back to the direct REST API
 * (/api/search, /api/book) so the user still gets an answer.
 *
 * Data: the offline world flight model (agent/flights_provider.py,
 * agent/airports.py) via /api/airports, /api/airports/popular,
 * /api/resolve, /api/fares, /api/search, /api/book, /api/routes/popular,
 * /api/model.
 */
(function (global) {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const PAGE = 10;
  const FALLBACK_AFTER_MS = 7000;
  const CABIN_WORDS = { economy: 'economy', premium_economy: 'premium economy', business: 'business class', first: 'first class' };
  const CABIN_LABELS = { economy: 'Economy', premium_economy: 'Premium Economy', business: 'Business', first: 'First' };

  let api = null;
  const form = {
    trip: 'oneway', pax: 1, cabin: 'economy',
    picked: { origin: null, destination: null },   // {iata, city, label, query}
  };
  const view = {
    result: null,           // last search_flights result
    query: null,            // what was asked for (for the date strip / links)
    leg: 'out',
    selected: { out: null, ret: null },
    filters: { stops: 'any', times: new Set(), airlines: new Set() },
    sort: 'best',
    showAll: false,
    fares: null,
    booking: { status: 'idle', legs: {}, message: '' },
  };
  let pending = null;       // {source, query, timer} while waiting for search results
  let popularCache = null;
  let startupQuery = null;
  const resolveCache = new Map();

  // ---- small helpers ----------------------------------------------------
  function isoDate(d) {
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function addDays(iso, n) {
    const [y, m, d] = iso.split('-').map(Number);
    return isoDate(new Date(y, m - 1, d + n));
  }
  function daysBetween(a, b) {
    if (!a || !b) return 7;
    const [y1, m1, d1] = a.split('-').map(Number), [y2, m2, d2] = b.split('-').map(Number);
    return Math.round((Date.UTC(y2, m2 - 1, d2) - Date.UTC(y1, m1 - 1, d1)) / 86400000);
  }
  function todayIso() { return isoDate(new Date()); }
  function fmtDate(iso, opts) {
    if (!iso) return '';
    const [y, mo, d] = iso.split('-').map(Number);
    return new Date(Date.UTC(y, mo - 1, d)).toLocaleDateString(undefined,
      Object.assign({ weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }, opts || {}));
  }
  function fmtDuration(min) {
    const h = Math.floor(min / 60), m = min % 60;
    return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
  }
  function fmtPrice(p, cur) {
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD', maximumFractionDigits: 0 }).format(p); }
    catch { return `$${Math.round(p)}`; }
  }
  function carrierColor(code) {
    let h = 0;
    for (const ch of String(code)) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h}, 55%, 42%)`;
  }
  function stopsText(o) {
    if (!o.stops) return 'Nonstop';
    return `${o.stops} stop${o.stops > 1 ? 's' : ''} · ${o.via.join(', ')}`;
  }
  function timeBucket(hour) {
    if (hour >= 5 && hour < 12) return 'morning';
    if (hour >= 12 && hour < 17) return 'afternoon';
    if (hour >= 17 && hour < 21) return 'evening';
    return 'night';
  }
  async function getJson(url, opts) {
    const resp = await fetch(url, opts);
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { msg = (await resp.json()).message || msg; } catch { /* not json */ }
      throw new Error(msg);
    }
    return resp.json();
  }
  function setFormError(text) { $('formError').textContent = text || ''; }
  function emptyFilters() { return { stops: 'any', times: new Set(), airlines: new Set() }; }

  // ---- search form: trip type, passengers, cabin, dates ------------------
  function setTrip(trip) {
    form.trip = trip;
    document.querySelectorAll('#tripType [data-trip]').forEach((b) => {
      const on = b.dataset.trip === trip;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    $('dateRow').classList.toggle('one-way', trip !== 'round');
    if (trip === 'round' && !$('returnInput').value && $('dateInput').value) {
      $('returnInput').value = addDays($('dateInput').value, 7);
    }
    syncDateLimits();
  }
  function setPax(n) {
    form.pax = Math.max(1, Math.min(9, n));
    $('paxCount').textContent = String(form.pax);
    $('paxMinus').disabled = form.pax <= 1;
    $('paxPlus').disabled = form.pax >= 9;
  }
  function syncDateLimits() {
    const min = todayIso();
    const max = addDays(min, 330);
    const dep = $('dateInput'), ret = $('returnInput');
    dep.min = min; dep.max = max;
    if (!dep.value || dep.value < min) dep.value = addDays(min, 14);
    if (dep.value > max) dep.value = max;
    ret.min = dep.value; ret.max = max;
    if (ret.value && ret.value < dep.value) ret.value = addDays(dep.value, 7) > max ? dep.value : addDays(dep.value, 7);
  }

  // ---- airport comboboxes ------------------------------------------------
  const fields = {};
  async function fetchAirports(q) {
    return (await getJson('/api/airports?q=' + encodeURIComponent(q))).airports || [];
  }
  async function fetchPopular() {
    if (!popularCache) popularCache = (await getJson('/api/airports/popular?n=10')).airports || [];
    return popularCache;
  }
  function pickedFromAirport(a) {
    return { iata: a.iata, city: a.city, label: `${a.iata} — ${a.city}`, query: a.iata };
  }

  function wireAirportField(key, inputId, fieldId, dropdownId) {
    const input = $(inputId), field = $(fieldId), dropdown = $(dropdownId);
    let results = [], active = -1, seq = 0, debounce = null, mode = 'search';

    function close() {
      dropdown.classList.remove('open');
      dropdown.innerHTML = '';
      input.setAttribute('aria-expanded', 'false');
      active = -1;
    }
    function choose(picked) {
      form.picked[key] = picked;
      input.value = picked.label;
      field.classList.add('picked');
      field.classList.remove('invalid');
      setFormError('');
      close();
    }
    function clear() {
      form.picked[key] = null;
      field.classList.remove('picked');
    }
    function highlight(i) {
      const opts = dropdown.querySelectorAll('.airport-option');
      opts.forEach((o, j) => o.classList.toggle('active', j === i));
      active = i;
      if (opts[i]) opts[i].scrollIntoView({ block: 'nearest' });
    }
    function render(head, errorText) {
      dropdown.innerHTML = '';
      if (head) dropdown.insertAdjacentHTML('beforeend', `<div class="dropdown-head">${esc(head)}</div>`);
      if (errorText) {
        dropdown.insertAdjacentHTML('beforeend', `<div class="airport-empty error">${esc(errorText)}</div>`);
      } else if (!results.length) {
        dropdown.insertAdjacentHTML('beforeend', '<div class="airport-empty">No matching airports — try a city, airport name or 3-letter code.</div>');
      }
      results.forEach((a, i) => {
        const opt = document.createElement('div');
        opt.className = 'airport-option' + (a.has_routes === false ? ' no-routes' : '');
        opt.setAttribute('role', 'option');
        opt.innerHTML = `<span class="code">${esc(a.iata)}</span><span class="place">${esc(a.city)}, ${esc(a.country_name || a.country)}` +
          `<span class="aname">${esc(a.name)}${a.has_routes === false ? ' · no scheduled routes in model' : ''}</span></span>`;
        // mousedown (not click) fires before the input's blur
        opt.addEventListener('mousedown', (e) => { e.preventDefault(); choose(pickedFromAirport(a)); });
        opt.addEventListener('mousemove', () => { if (active !== i) highlight(i); });
        dropdown.appendChild(opt);
      });
      dropdown.insertAdjacentHTML('beforeend', '<a class="dropdown-foot" href="/airports">Browse all airports by country →</a>');
      dropdown.classList.add('open');
      input.setAttribute('aria-expanded', 'true');
      active = -1;
    }
    async function showPopular() {
      mode = 'popular';
      const my = ++seq;
      try {
        const found = await fetchPopular();
        if (my !== seq || document.activeElement !== input || input.value.trim()) return;
        results = found;
        render('Popular airports');
      } catch {
        if (my !== seq || document.activeElement !== input) return;
        results = [];
        render('', 'Can’t reach the airport service — is the server running?');
      }
    }
    async function runSearch(q) {
      mode = 'search';
      const my = ++seq;
      try {
        const found = await fetchAirports(q);
        if (my !== seq || document.activeElement !== input) return;
        results = found;
        render('');
      } catch {
        if (my !== seq || document.activeElement !== input) return;
        results = [];
        render('', 'Can’t reach the airport service — is the server running?');
      }
    }

    input.addEventListener('focus', () => {
      if (!input.value.trim()) showPopular();
      else if (!form.picked[key]) runSearch(input.value.trim());
      else input.select();
    });
    input.addEventListener('input', () => {
      clear();
      const q = input.value.trim();
      clearTimeout(debounce);
      if (!q) { showPopular(); return; }
      if (q.length < 2) { ++seq; results = []; close(); return; }
      debounce = setTimeout(() => runSearch(q), 120);
    });
    input.addEventListener('keydown', (e) => {
      const open = dropdown.classList.contains('open');
      if (e.key === 'ArrowDown' && open && results.length) {
        e.preventDefault(); highlight(Math.min(active + 1, results.length - 1));
      } else if (e.key === 'ArrowUp' && open && results.length) {
        e.preventDefault(); highlight(Math.max(active - 1, 0));
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (open && results.length) choose(pickedFromAirport(results[active >= 0 ? active : 0]));
        else if (form.picked[key]) doSearch();
      } else if (e.key === 'Escape') {
        close();
      } else if (e.key === 'Tab' && open && results.length && !form.picked[key] && mode === 'search') {
        choose(pickedFromAirport(results[active >= 0 ? active : 0]));
      }
    });
    input.addEventListener('blur', () => {
      setTimeout(() => {
        // typed an exact code or city and left the field: take the top match
        const q = input.value.trim().toLowerCase();
        if (!form.picked[key] && q && results.length && mode === 'search') {
          const top = results[0];
          if (top.iata.toLowerCase() === q || (top.city || '').toLowerCase() === q) choose(pickedFromAirport(top));
        }
        close();
      }, 150);
    });
    fields[key] = { input, field, choose, clear };
  }

  async function ensurePicked(key) {
    // A typed-but-not-chosen value is resolved to its best match rather
    // than refusing to search.
    if (form.picked[key]) return true;
    const f = fields[key];
    const q = f.input.value.trim();
    if (!q) { f.field.classList.add('invalid'); return false; }
    try {
      const found = await fetchAirports(q);
      if (found.length) { f.choose(pickedFromAirport(found[0])); return true; }
    } catch { /* fall through */ }
    f.field.classList.add('invalid');
    return false;
  }

  function swap() {
    const o = form.picked.origin, d = form.picked.destination;
    const ov = fields.origin.input.value, dv = fields.destination.input.value;
    form.picked.origin = d; form.picked.destination = o;
    fields.origin.input.value = dv; fields.destination.input.value = ov;
    fields.origin.field.classList.toggle('picked', !!d);
    fields.destination.field.classList.toggle('picked', !!o);
  }

  // ---- running a search --------------------------------------------------
  function buildQuery() {
    return {
      from: form.picked.origin.query, to: form.picked.destination.query,
      date: $('dateInput').value,
      ret: form.trip === 'round' ? $('returnInput').value : '',
      pax: form.pax, cabin: form.cabin,
    };
  }
  function sentenceFor(q) {
    let s = `book a flight from ${q.from} to ${q.to} on ${q.date}`;
    s += q.ret ? ` returning on ${q.ret}` : ' one way';
    s += ` for ${q.pax} passenger${q.pax > 1 ? 's' : ''} in ${CABIN_WORDS[q.cabin] || 'economy'}`;
    return s;
  }
  function queryString(q) {
    const p = new URLSearchParams({ from: q.from, to: q.to, date: q.date });
    if (q.ret) p.set('return', q.ret);
    if (q.pax > 1) p.set('pax', String(q.pax));
    if (q.cabin !== 'economy') p.set('cabin', q.cabin);
    return p.toString();
  }

  async function doSearch() {
    setFormError('');
    const okO = await ensurePicked('origin');
    const okD = await ensurePicked('destination');
    if (!okO || !okD) {
      setFormError(!okO ? 'Choose where you’re flying from.' : 'Choose where you’re flying to.');
      return;
    }
    if (form.picked.origin.query === form.picked.destination.query) {
      setFormError('From and To are the same airport.');
      return;
    }
    if (form.trip === 'round' && !$('returnInput').value) {
      setFormError('Pick a return date, or switch to one-way.');
      return;
    }
    syncDateLimits();
    const q = buildQuery();
    if (q.ret && q.ret < q.date) { setFormError('The return date is before the departure date.'); return; }
    startSearch(q, 'form');
  }

  function startSearch(q, source) {
    view.query = q;
    history.replaceState(null, '', '/flights?' + queryString(q));
    showLoading();
    loadFares(q);
    if (pending) clearTimeout(pending.timer);
    pending = { source, query: q, timer: null };
    if (api.isConnected() && api.sendTurn(sentenceFor(q))) {
      pending.timer = setTimeout(() => restSearch(q), FALLBACK_AFTER_MS);
    } else {
      restSearch(q);
    }
    const c = $('resultsCard');
    const top = c.getBoundingClientRect().top;
    if (top > window.innerHeight - 140 || top < 0) c.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function restSearch(q) {
    if (!pending || pending.query !== q) return;
    try {
      const p = new URLSearchParams(queryString(q));
      p.set('pax', String(q.pax));
      const result = await getJson('/api/search?' + p.toString());
      if (pending && pending.query === q) showResult(result, { fromForm: true });
    } catch (e) {
      if (pending && pending.query === q) {
        showStatus(`Search failed (${e.message}). If this keeps happening, restart the server with python server/app.py.`);
      }
    } finally {
      if (pending && pending.query === q) { clearTimeout(pending.timer); pending = null; }
    }
  }

  async function loadFares(q) {
    view.fares = null;
    renderDateStrip();
    try {
      const p = new URLSearchParams({ from: q.from, to: q.to, date: q.date, pax: String(q.pax), cabin: q.cabin, days: '3' });
      const fares = await getJson('/api/fares?' + p.toString());
      if (view.query && view.query.date === q.date && view.query.from === q.from && view.query.to === q.to) {
        view.fares = fares.status === 'ok' ? fares : null;
        renderDateStrip();
      }
    } catch { view.fares = null; renderDateStrip(); }
  }

  // ---- results ----------------------------------------------------------
  function hideResultChrome() {
    $('legTabs').hidden = true;
    $('filters').hidden = true;
    $('sortRow').hidden = true;
    $('bookingPanel').hidden = true;
  }
  function showLoading() {
    $('resultsCard').hidden = false;
    hideResultChrome();
    $('resultsBody').innerHTML = '<div class="results-loading"><span class="spin"></span>Searching flights…</div>';
  }
  function showStatus(text, suggestions) {
    $('resultsCard').hidden = false;
    hideResultChrome();
    const sugg = (suggestions || []).map((a, i) =>
      `<button type="button" data-suggest="${i}">${esc(a.iata)} · ${esc(a.city)}, ${esc(a.country_name || a.country)}</button>`).join('');
    $('resultsBody').innerHTML = `<div class="results-status">${esc(text)}${sugg ? `<div class="suggest">Did you mean: ${sugg}</div>` : ''}</div>`;
  }

  function syncFormFromResult(r) {
    // A search typed in the talk box fills the form too, so both inputs
    // always agree on what's being shown.
    const mk = (place, airports, query) => {
      const metro = (airports || []).length > 1;
      return { iata: place.iata, city: place.city, query: metro ? query : place.iata,
               label: metro ? `${place.city} (all airports)` : `${place.iata} — ${place.city}` };
    };
    fields.origin.choose(mk(r.origin, r.origin_airports, r.origin_query));
    fields.destination.choose(mk(r.destination, r.destination_airports, r.destination_query));
    $('dateInput').value = r.date;
    setPax(r.passengers || 1);
    form.cabin = r.cabin_code || 'economy';
    $('cabinSelect').value = form.cabin;
    if (r.return && r.return.date) { setTrip('round'); $('returnInput').value = r.return.date; }
    else setTrip('oneway');
    syncDateLimits();
  }

  function showResult(r, opts) {
    view.result = r;
    view.leg = 'out';
    view.selected = { out: null, ret: null };
    view.filters = emptyFilters();
    view.showAll = false;
    view.booking = { status: 'idle', legs: {}, message: '', names: view.booking.names };
    if (r.status !== 'ok' || !r.offers || !r.offers.length) {
      $('resultsTitle').textContent = 'Flights';
      $('resultsSub').textContent = '';
      $('dateStrip').innerHTML = '';
      showStatus(r.message || 'No flights found.', r.suggestions);
      return;
    }
    if (!(opts && opts.fromForm)) syncFormFromResult(r);
    const q = {
      from: (form.picked.origin && form.picked.origin.query) || r.origin.iata,
      to: (form.picked.destination && form.picked.destination.query) || r.destination.iata,
      date: r.date, ret: r.return ? r.return.date : '', pax: r.passengers || 1, cabin: r.cabin_code || 'economy',
    };
    const sameAsQuery = view.query && view.query.date === q.date && view.query.from === q.from && view.query.to === q.to;
    view.query = q;
    history.replaceState(null, '', '/flights?' + queryString(q));
    if (!sameAsQuery || !view.fares) loadFares(q);
    render();
  }

  function legOffers() {
    const r = view.result;
    if (!r) return [];
    return view.leg === 'ret' ? ((r.return && r.return.offers) || []) : (r.offers || []);
  }
  function filtered(offers) {
    const f = view.filters;
    return offers.filter((o) => {
      if (f.stops === '0' && o.stops > 0) return false;
      if (f.stops === '1' && o.stops > 1) return false;
      const hour = o.depart_hour != null ? o.depart_hour : Number(String(o.depart_time).slice(0, 2));
      if (f.times.size && !f.times.has(timeBucket(hour))) return false;
      if (f.airlines.size && !(o.carrier_names || [o.airline_name]).some((n) => f.airlines.has(n))) return false;
      return true;
    });
  }
  function sorted(offers) {
    const list = offers.slice();
    if (view.sort === 'price') list.sort((a, b) => a.price - b.price || a.duration_min - b.duration_min);
    else if (view.sort === 'duration') list.sort((a, b) => a.duration_min - b.duration_min || a.price - b.price);
    else if (view.sort === 'depart') list.sort((a, b) => a.depart_local.localeCompare(b.depart_local));
    else list.sort((a, b) => (a.score || 0) - (b.score || 0));
    return list;
  }

  function renderDateStrip() {
    const strip = $('dateStrip');
    if (!view.fares || !view.fares.days || !view.fares.days.length) { strip.innerHTML = ''; return; }
    strip.innerHTML = view.fares.days.map((d) => {
      const cls = [d.selected ? 'selected' : '', d.cheapest ? 'cheapest' : ''].join(' ').trim();
      const price = d.min_price != null ? fmtPrice(d.min_price, view.fares.currency) : 'No flights';
      return `<button type="button" class="${cls}" data-day="${esc(d.date)}" ${d.min_price == null ? 'disabled' : ''}
        title="${d.flights} flights${d.cheapest ? ' · cheapest day' : ''}"><span class="d">${esc(fmtDate(d.date))}</span><span class="p">${esc(price)}</span></button>`;
    }).join('');
  }

  function renderLegTabs() {
    const r = view.result;
    const tabs = $('legTabs');
    if (!r || !r.return) { tabs.hidden = true; return; }
    tabs.hidden = false;
    const pick = (o) => o ? `<span class="picked">✓ ${esc(o.depart_time)} · ${esc(fmtPrice(o.price, o.currency))}</span>` : 'Choose a flight';
    tabs.innerHTML =
      `<button type="button" data-leg="out" class="${view.leg === 'out' ? 'active' : ''}"><b>1 · Outbound · ${esc(fmtDate(r.date))}</b>${esc(r.origin.city)} → ${esc(r.destination.city)}<br>${pick(view.selected.out)}</button>` +
      `<button type="button" data-leg="ret" class="${view.leg === 'ret' ? 'active' : ''}"><b>2 · Return · ${esc(fmtDate(r.return.date))}</b>${esc(r.destination.city)} → ${esc(r.origin.city)}<br>${pick(view.selected.ret)}</button>`;
  }

  function renderFilters(offers) {
    $('filters').hidden = false;
    document.querySelectorAll('#stopsFilter [data-stops]').forEach((b) => b.classList.toggle('active', b.dataset.stops === view.filters.stops));
    document.querySelectorAll('#timeFilter [data-time]').forEach((b) => b.classList.toggle('active', view.filters.times.has(b.dataset.time)));
    const counts = new Map();
    offers.forEach((o) => (o.carrier_names || [o.airline_name]).forEach((n) => counts.set(n, (counts.get(n) || 0) + 1)));
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
    const group = $('airlineFilter');
    group.hidden = top.length < 2;
    group.innerHTML = '<span>Airlines</span>' + top.map(([n]) =>
      `<button type="button" data-airline="${esc(n)}" class="${view.filters.airlines.has(n) ? 'active' : ''}">${esc(n)}</button>`).join('');
  }

  function offerHtml(o) {
    const seg = o.segments || [];
    const meta = seg.map((s) => `${esc(s.carrier_name)} · ${esc(s.flight)}${s.aircraft ? ' · ' + esc(s.aircraft) : ''}`).join('  →  ');
    const tags = (o.tags || []).map((t) => `<span>${esc(t)}</span>`).join('');
    const seats = o.seats_left <= 4 ? `<span class="seats">Only ${o.seats_left} seat${o.seats_left === 1 ? '' : 's'} left</span>` : '';
    const chosen = view.selected[view.leg] && view.selected[view.leg].offer_id === o.offer_id;
    const multi = (o.passengers || 1) > 1
      ? `<span class="seats" style="color:var(--muted)">per person · ${esc(fmtPrice(o.total_price, o.currency))} total</span>` : '';
    return `<div class="offer${chosen ? ' selected' : ''}" data-offer="${esc(o.offer_id)}">
      <div class="airline-badge" style="background:${carrierColor(o.airline)}" title="${esc(o.airline_name)}">${esc(o.airline)}</div>
      <div>
        ${tags ? `<div class="offer-tags">${tags}</div>` : ''}
        <div class="offer-times">${esc(o.depart_time)} → ${esc(o.arrive_time)}${o.arrive_day_offset ? `<sup>+${o.arrive_day_offset}</sup>` : ''}</div>
        <div class="offer-route"><b>${esc(o.from || (seg[0] && seg[0].from))} – ${esc(o.to || (seg.length && seg[seg.length - 1].to))}</b> · ${fmtDuration(o.duration_min)} · ${esc(stopsText(o))}</div>
        <div class="offer-meta">${meta}</div>
      </div>
      <div class="offer-price"><div><b>${esc(fmtPrice(o.price, o.currency))}</b>${multi}${seats}</div>
        <button type="button" class="btn-primary" data-select="${esc(o.offer_id)}">${chosen ? 'Selected ✓' : 'Select'}</button>
      </div>
    </div>`;
  }

  function render() {
    const r = view.result;
    if (!r) return;
    $('resultsCard').hidden = false;
    $('resultsTitle').textContent = `${r.origin.city} → ${r.destination.city}${r.return ? ' and back' : ''}`;
    const n = r.passengers || 1;
    $('resultsSub').textContent = `${fmtDate(r.date)}${r.return ? ' – ' + fmtDate(r.return.date) : ''} · ${n} passenger${n > 1 ? 's' : ''} · ${r.cabin || 'Economy'} · ` +
      `${r.origin_airports.join('/')} → ${r.destination_airports.join('/')}`;
    renderDateStrip();
    renderLegTabs();
    const all = legOffers();
    renderFilters(all);
    const list = sorted(filtered(all));
    $('sortRow').hidden = false;
    $('resultCount').textContent = `${list.length} of ${all.length} flights${view.leg === 'ret' ? ' (return)' : ''}`;
    document.querySelectorAll('#sortTabs [data-sort]').forEach((x) => x.classList.toggle('active', x.dataset.sort === view.sort));
    const visible = view.showAll ? list : list.slice(0, PAGE);
    $('resultsBody').innerHTML = list.length
      ? visible.map(offerHtml).join('') + (list.length > PAGE && !view.showAll
        ? `<button type="button" class="show-more" data-more="1">Show all ${list.length} flights</button>` : '')
      : (all.length
        ? '<div class="empty-results">No flights match these filters. <button type="button" class="btn-ghost small" data-clear-filters="1">Clear filters</button></div>'
        : '<div class="empty-results">No flights on this day — try another date above.</div>');
    renderBooking();
  }

  // ---- selection + booking -------------------------------------------
  function readyToBook() {
    const r = view.result;
    return !!(r && view.selected.out && (!r.return || view.selected.ret));
  }
  function renderBooking() {
    const panel = $('bookingPanel');
    const r = view.result;
    if (!readyToBook()) { panel.hidden = true; return; }
    panel.hidden = false;
    const legs = [['Outbound', view.selected.out]].concat(r.return ? [['Return', view.selected.ret]] : []);
    const total = legs.reduce((sum, [, o]) => sum + (o.total_price || o.price), 0);
    const cur = view.selected.out.currency;
    const b = view.booking;
    const lines = legs.map(([label, o]) => `<div><span>${label}</span><b>${esc(o.flight_numbers.join(' + '))}</b>
      <span>${esc(fmtDate(o.date))} ${esc(o.depart_time)} → ${esc(o.arrive_time)}${o.arrive_day_offset ? ' +' + o.arrive_day_offset : ''} · ${esc(stopsText(o))}</span>
      <b>${esc(fmtPrice(o.total_price || o.price, o.currency))}</b></div>`).join('');
    if (b.status === 'done') {
      const pnrs = legs.map(([label, o]) => {
        const bk = b.legs[o.offer_id];
        return `${label}: <span class="pnr">${esc(bk ? bk.confirmation_id : '—')}</span>`;
      }).join(' &nbsp; ');
      panel.innerHTML = `<div class="booking-done">✓ Booked (simulated) for <b>${esc(b.names.join(', '))}</b><br>${pnrs}<br>
        Total ${esc(fmtPrice(total, cur))} · ${esc(r.cabin || 'Economy')}<br><small>Simulated booking against the offline flight model — no ticket is issued.</small>
        <div style="margin-top:10px"><button type="button" class="btn-ghost small" data-new-search="1">Start a new search</button></div></div>`;
      return;
    }
    const n = r.passengers || 1;
    const names = b.names || [];
    const inputs = Array.from({ length: n }, (_, i) =>
      `<input type="text" data-pax-name="${i}" placeholder="${i === 0 ? 'Lead passenger full name' : 'Passenger ' + (i + 1) + ' full name'}" value="${esc(names[i] || '')}" maxlength="60" />`).join('');
    panel.innerHTML = `<h3>Your trip</h3><div class="booking-legs">${lines}</div>
      <div class="pax-names">${inputs}</div>
      <div class="booking-actions"><span class="total">Total for ${n} passenger${n > 1 ? 's' : ''}: <b>${esc(fmtPrice(total, cur))}</b></span>
        <button type="button" class="btn-primary" data-book="1" ${b.status === 'pending' ? 'disabled' : ''}>${b.status === 'pending' ? 'Booking…' : 'Book now'}</button></div>
      ${b.status === 'error' ? `<div class="booking-error">${esc(b.message)}</div>` : ''}`;
  }

  function collectNames() {
    return [...document.querySelectorAll('[data-pax-name]')].map((i) => i.value.trim());
  }

  async function book() {
    const r = view.result;
    if (!readyToBook() || view.booking.status === 'pending') return;
    const names = collectNames();
    if (names.some((n) => n.length < 2)) {
      view.booking = { ...view.booking, names, status: 'error', message: 'Enter a full name for every passenger.' };
      renderBooking();
      return;
    }
    const legs = [view.selected.out].concat(r.return ? [view.selected.ret] : []);
    view.booking = { status: 'pending', legs: {}, names, message: '', waiting: new Set(legs.map((o) => o.offer_id)) };
    renderBooking();
    const passenger = names.join(', ');
    for (const o of legs) {
      if (api.isConnected() && api.send({ type: 'book', offer_id: o.offer_id, passenger_name: passenger })) continue;
      try {
        onBooking(await getJson('/api/book', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ offer_id: o.offer_id, passenger_name: passenger }) }));
      } catch (e) {
        onBooking({ ok: false, offer_id: o.offer_id, message: `Booking failed (${e.message}).` });
      }
    }
  }

  function onBooking(msg) {
    const b = view.booking;
    if (b.status !== 'pending' || !b.waiting || !b.waiting.has(msg.offer_id)) return;
    if (!msg.ok) {
      view.booking = { ...b, status: 'error', message: msg.message || 'Booking failed.' };
      renderBooking();
      return;
    }
    b.legs[msg.offer_id] = msg.booking;
    b.waiting.delete(msg.offer_id);
    if (!b.waiting.size) b.status = 'done';
    renderBooking();
  }

  function select(offerId) {
    const offer = legOffers().find((o) => o.offer_id === offerId);
    if (!offer) return;
    view.selected[view.leg] = offer;
    view.booking = { status: 'idle', legs: {}, message: '', names: view.booking.names };
    if (view.result.return && view.leg === 'out' && !view.selected.ret) {
      view.leg = 'ret';
      view.filters = emptyFilters();
      view.showAll = false;
      render();
      $('legTabs').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    render();
    if (readyToBook()) $('bookingPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function onResultsClick(e) {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    let el;
    if ((el = t.closest('[data-select]'))) { select(el.dataset.select); return; }
    if ((el = t.closest('[data-leg]'))) { view.leg = el.dataset.leg; view.showAll = false; view.filters = emptyFilters(); render(); return; }
    if ((el = t.closest('[data-stops]'))) { view.filters.stops = el.dataset.stops; render(); return; }
    if ((el = t.closest('[data-time]'))) { const s = view.filters.times; if (s.has(el.dataset.time)) s.delete(el.dataset.time); else s.add(el.dataset.time); render(); return; }
    if ((el = t.closest('[data-airline]'))) { const s = view.filters.airlines; if (s.has(el.dataset.airline)) s.delete(el.dataset.airline); else s.add(el.dataset.airline); render(); return; }
    if ((el = t.closest('[data-clear-filters]'))) { view.filters = emptyFilters(); render(); return; }
    if ((el = t.closest('[data-sort]'))) { view.sort = el.dataset.sort; render(); return; }
    if ((el = t.closest('[data-more]'))) { view.showAll = true; render(); return; }
    if ((el = t.closest('[data-book]'))) { book(); return; }
    if ((el = t.closest('[data-new-search]'))) {
      window.scrollTo({ top: $('originInput').closest('.card').offsetTop - 20, behavior: 'smooth' });
      fields.destination.input.focus();
      return;
    }
    if ((el = t.closest('[data-day]'))) {
      if (!view.query) return;
      const d = el.dataset.day;
      const q = { ...view.query, date: d };
      if (q.ret && q.ret < d) q.ret = addDays(d, Math.max(1, daysBetween(view.query.date, view.query.ret)));
      $('dateInput').value = q.date;
      if (q.ret) $('returnInput').value = q.ret;
      startSearch(q, 'form');
      return;
    }
    if ((el = t.closest('[data-suggest]'))) {
      const r = view.result;
      const a = r && r.suggestions && r.suggestions[Number(el.dataset.suggest)];
      if (!a) return;
      fields[r.status === 'unknown_origin' ? 'origin' : 'destination'].choose(pickedFromAirport(a));
      doSearch();
    }
  }

  // ---- hooks from assistant.js -------------------------------------------
  // A form search is sent to the agent as a sentence, so with an LLM NLU
  // backend (Groq / Ollama) the agent's reading of it can drift -- a
  // dropped cabin, a reformatted date, a city instead of the exact airport.
  // The form's own query is the source of truth: if the agent's result
  // doesn't match it, answer with the direct search instead.
  const foldText = (s) => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().trim();
  function placeMatches(want, r, side) {
    const w = String(want || '').trim();
    const codes = (r[side + '_airports'] || []).map((c) => String(c.iata || c).toUpperCase());
    if (/^[A-Za-z]{3}$/.test(w) && codes.length === 1 && codes[0] === w.toUpperCase()) return true;
    return foldText(r[side] && r[side].city) === foldText(w) || foldText(r[side + '_query']) === foldText(w);
  }
  function resultMatchesQuery(r, q) {
    if (!r || r.status !== 'ok' || !q) return false;
    return r.date === q.date
      && ((r.return && r.return.date) || '') === (q.ret || '')
      && (r.passengers || 1) === q.pax
      && (r.cabin_code || 'economy') === q.cabin
      && placeMatches(q.from, r, 'origin') && placeMatches(q.to, r, 'destination');
  }

  function onToolResult(msg) {
    if (msg.tool !== 'search_flights') return;
    if (pending && pending.source === 'form' && pending.query
        && (msg.status === 'error' || !resultMatchesQuery(msg.result, pending.query))) {
      clearTimeout(pending.timer);
      restSearch(pending.query);
      return;
    }
    if (pending) clearTimeout(pending.timer);
    const fromForm = !!(pending && pending.source === 'form');
    pending = null;
    if (msg.status === 'error') {
      showStatus(`The search failed: ${(msg.result && msg.result.error) || 'unknown error'}.`);
      return;
    }
    showResult(msg.result || {}, { fromForm });
  }

  function onAction(msg) {
    const p = msg.payload || {};
    if (msg.action === 'tool_call' && p.tool === 'search_flights') {
      if (!pending) pending = { source: 'talk', query: null, timer: null };
      showLoading();
    } else if (msg.action === 'clarification' && pending && pending.source === 'talk') {
      showStatus(p.question || 'I need a bit more information.');
      pending = null;
    } else if (msg.action === 'final_response' && pending && pending.source === 'talk'
               && !/^(On it|Already on it|\(from cache\))/.test(p.text || '')) {
      pending = null;
    }
  }

  function turnSent() {
    if (!pending) pending = { source: 'talk', query: null, timer: null };
  }

  function connected() {
    if (startupQuery) {
      const q = startupQuery;
      startupQuery = null;
      startSearch(q, 'form');
    }
  }

  function reset() {
    if (pending) clearTimeout(pending.timer);
    pending = null;
    view.result = null;
    view.fares = null;
    $('resultsCard').hidden = true;
  }

  // ---- trip-card sub-labels ---------------------------------------------
  async function resolve(q) {
    const key = q.trim().toLowerCase();
    if (resolveCache.has(key)) return resolveCache.get(key);
    let out = [];
    try { out = (await getJson('/api/resolve?q=' + encodeURIComponent(q))).airports || []; } catch { /* no label */ }
    resolveCache.set(key, out);
    return out;
  }
  async function decorateField(key, value, subEl) {
    subEl.textContent = '';
    if (key === 'cabin') { subEl.textContent = CABIN_LABELS[value] || ''; return; }
    if (key === 'passengers') { subEl.textContent = `${value} traveller${Number(value) > 1 ? 's' : ''}`; return; }
    if (key === 'date' || key === 'return_date') {
      if (/^\d{4}-\d{2}-\d{2}$/.test(value)) subEl.textContent = fmtDate(value, { year: 'numeric' });
      return;
    }
    if (key !== 'origin' && key !== 'destination') return;
    const found = await resolve(value);
    if (!found.length) { subEl.textContent = 'not a known airport'; return; }
    const a = found[0];
    if (found.length === 1) subEl.textContent = value.trim().toUpperCase() === a.iata ? `${a.name}, ${a.city}` : `${a.iata} · ${a.name}`;
    else subEl.textContent = `${a.city}: ${found.map((x) => x.iata).join(', ')}`;
  }

  // ---- live chips, model facts, URL params ------------------------------
  const DATE_PHRASES = ['next friday', 'tomorrow', 'on the 20th', 'this weekend', 'in 10 days'];
  async function loadPopularRoutes() {
    let routes = [];
    try { routes = (await getJson('/api/routes/popular?n=4')).routes || []; } catch { return; }
    if (routes.length < 2) return;
    const chips = $('heroChips');
    chips.querySelectorAll('.chip[data-fill]').forEach((c) => c.remove());
    routes.slice(0, 3).forEach((r, i) => {
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.dataset.fill = `book a flight from ${r.from} to ${r.to} ${DATE_PHRASES[i % DATE_PHRASES.length]}`;
      chip.textContent = `${r.from} → ${r.to}`;
      chip.title = `~${r.weekly_flights} flights a week in the model`;
      chips.appendChild(chip);
    });
    const a = routes[0], b = routes.find((r) => r.to !== a.to && r.to !== a.from) || routes[1];
    api.setDemoScript([
      { text: `book a flight from ${a.from} to ${a.to} next friday`, endOfTurn: false, waitAfterMs: 250 },
      { interrupt: true, waitAfterMs: 300 },
      { text: `actually from ${a.from} to ${b.to} next friday`, endOfTurn: true },
    ]);
  }
  async function loadModelFacts() {
    try {
      const c = (await getJson('/api/model')).counts || {};
      if (!c.airports) return;
      const n = (x) => Number(x || 0).toLocaleString();
      $('modelHint').textContent = `Type a city, airport or IATA code — ${n(c.airports)} airports in ${n(c.countries)} countries, ` +
        `${n(c.airport_pairs)} routes flown by ${n(c.carriers)} airlines. Schedules and fares come from an offline world flight model — no airline API involved.`;
    } catch { /* keep static hint */ }
  }

  async function applyUrlParams() {
    const p = new URLSearchParams(location.search);
    const from = p.get('from'), to = p.get('to');
    if (p.get('pax')) setPax(Number(p.get('pax')) || 1);
    if (p.get('cabin') && CABIN_LABELS[p.get('cabin')]) { form.cabin = p.get('cabin'); $('cabinSelect').value = form.cabin; }
    if (/^\d{4}-\d{2}-\d{2}$/.test(p.get('date') || '')) $('dateInput').value = p.get('date');
    if (/^\d{4}-\d{2}-\d{2}$/.test(p.get('return') || '')) { setTrip('round'); $('returnInput').value = p.get('return'); }
    syncDateLimits();
    const fill = async (key, value) => {
      if (!value) return false;
      try {
        const found = await fetchAirports(value);
        const exact = found.find((a) => a.iata === value.toUpperCase()) || found[0];
        if (!exact) return false;
        fields[key].choose(value.length === 3 ? pickedFromAirport(exact)
          : { iata: exact.iata, city: exact.city, label: `${exact.city} (all airports)`, query: value });
        return true;
      } catch { return false; }
    };
    const okFrom = await fill('origin', from);
    const okTo = await fill('destination', to);
    if (okFrom && okTo) {                  // date defaults to two weeks out when the link has none
      startupQuery = buildQuery();
      if (api.isConnected()) connected();
      else setTimeout(connected, 2500);   // socket still connecting: REST fallback covers it
    } else if (okFrom && !okTo) {
      fields.destination.input.focus();
    } else if (okTo && !okFrom) {
      fields.origin.input.focus();
    }
  }

  function init(assistantApi) {
    api = assistantApi;
    wireAirportField('origin', 'originInput', 'originField', 'originDropdown');
    wireAirportField('destination', 'destinationInput', 'destinationField', 'destinationDropdown');
    document.querySelectorAll('#tripType [data-trip]').forEach((b) => b.addEventListener('click', () => setTrip(b.dataset.trip)));
    $('paxMinus').addEventListener('click', () => setPax(form.pax - 1));
    $('paxPlus').addEventListener('click', () => setPax(form.pax + 1));
    $('cabinSelect').addEventListener('change', (e) => { form.cabin = e.target.value; });
    $('swapBtn').addEventListener('click', swap);
    $('dateInput').addEventListener('change', syncDateLimits);
    $('searchFlightsBtn').addEventListener('click', doSearch);
    $('resultsCard').addEventListener('click', onResultsClick);
    $('resultsCard').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.matches('[data-pax-name]')) { e.preventDefault(); book(); }
    });
    $('resultsCard').addEventListener('input', (e) => {
      if (e.target.matches('[data-pax-name]')) view.booking.names = collectNames();
    });
    $('shareBtn').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(location.href); $('shareBtn').textContent = 'Link copied ✓'; }
      catch { $('shareBtn').textContent = 'Copy the address bar'; }
      setTimeout(() => { $('shareBtn').textContent = 'Copy link'; }, 2000);
    });
    setPax(1);
    setTrip('oneway');
    syncDateLimits();
    applyUrlParams();
    loadPopularRoutes();
    loadModelFacts();
  }

  global.PrismFlights = { init, onToolResult, onAction, onBooking, reset, turnSent, connected, decorateField };
})(window);
