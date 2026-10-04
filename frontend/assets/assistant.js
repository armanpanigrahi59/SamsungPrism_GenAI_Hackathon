/*
 * Shared WebSocket client for every live-assistant page (flights.html,
 * support.html, ...). One copy of the protocol/rendering logic instead of
 * duplicating it per page -- each page just supplies a small config:
 *
 *   const api = PrismAssistant.init({
 *     domain: "flights",                 // sent to the server as the first
 *                                         // frame; server/app.py filters the
 *                                         // tool manifest by domain, so a
 *                                         // Flights tab's agent never even
 *                                         // sees create_support_ticket, and
 *                                         // vice versa -- not cosmetic, the
 *                                         // backend is actually scoped.
 *     fields: [{key: "origin", label: "From"}, ...],  // live-understanding
 *                                                      // card layout
 *     demoScript: [                      // optional scripted walkthrough,
 *       {text: "...", endOfTurn: false, waitAfterMs: 500},
 *       {interrupt: true, waitAfterMs: 300},
 *       {text: "...", endOfTurn: true},
 *     ],
 *     decorateField(key, value, fieldEl) // optional: add a sub-label under a
 *                                         // trip-card value
 *     onToolResult(msg, api)             // optional: a finished tool call
 *                                         // (server "tool_result" frame)
 *     onAction(msg), onBooking(msg), onReset(), onTurnSent(), onConnected()
 *   });
 *   // api: { send(obj), sendTurn(text), isConnected(), setDemoScript(steps),
 *   //        generation() }
 *
 * Expects the page to provide these element ids: talk, sendBtn,
 * interruptBtn, resetBtn, timeline, emptyState, genBadge, intentVal,
 * backendChain, latencyVal, cancelCount, connPill, connText, fieldsGrid,
 * and any number of elements with [data-demo] / [data-fill="..."].
 * connHint (optional) gets a one-line status message when a send can't
 * go out right now (not connected yet / reconnecting).
 */
(function (global) {
  const MAX_INPUT_LENGTH = 2000; // matches the server-side PRISM_MAX_TEXT_LENGTH cap

  // Everything rendered with innerHTML goes through this -- agent payloads
  // echo user text ("Got it: ..."), and airport names come from data files.
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function init(config) {
    const $ = (id) => document.getElementById(id);
    const timelineEl = $('timeline');
    const emptyStateHtml = $('emptyState') ? $('emptyState').outerHTML : '';
    const connPill = $('connPill');
    const connText = $('connText');
    const connHint = $('connHint');
    const genBadge = $('genBadge');
    const intentVal = $('intentVal');
    const backendChain = $('backendChain');
    const flightsBackendChain = $('flightsBackendChain');
    const latencyVal = $('latencyVal');
    const cancelCount = $('cancelCount');
    const talk = $('talk');
    const fieldsGrid = $('fieldsGrid');
    const sendBtn = $('sendBtn');
    const interruptBtn = $('interruptBtn');
    const resetBtn = $('resetBtn');

    const fields = config.fields || [];
    const hook = (name, ...args) => {
      try { if (typeof config[name] === 'function') return config[name](...args); } catch (e) { console.error(e); }
      return undefined;
    };
    let demoScript = config.demoScript || null;
    let cancelled = 0;
    let turnStartedAt = null;
    let toolCards = {};
    let ws = null;
    let lastSentLength = 0;
    let demoRunning = false;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let hintTimer = null;
    let latestGeneration = 0;
    let latestCallGen = {};   // tool name -> generation of its newest dispatched call

    // Build the live-understanding field cards once, from config -- so a
    // Support page shows Device/Topic/Issue instead of From/To/Date, with
    // zero per-page HTML duplication.
    if (fieldsGrid) {
      fieldsGrid.style.gridTemplateColumns = `repeat(${Math.min(fields.length, 3) || 1}, minmax(0, 1fr))`;
      fields.forEach((f) => {
        const div = document.createElement('div');
        div.className = 'field';
        div.id = 'field-' + f.key;
        div.innerHTML = `<span class="field-label">${esc(f.label)}</span><span class="field-value empty" id="val-${esc(f.key)}">—</span><span class="field-sub" id="sub-${esc(f.key)}"></span>`;
        fieldsGrid.appendChild(div);
      });
    }

    // ---- connection status -------------------------------------------
    // Four states instead of a binary live/down, so "the socket hasn't
    // finished connecting yet" and "it dropped and is retrying" both get a
    // visible, honest label instead of just looking broken.
    function setConn(state) {
      if (!connPill) return;
      connPill.classList.remove('live', 'down', 'connecting');
      if (state === 'live') {
        connPill.classList.add('live');
        connText.textContent = 'live';
      } else if (state === 'connecting') {
        connPill.classList.add('connecting');
        connText.textContent = 'connecting…';
      } else if (state === 'reconnecting') {
        connPill.classList.add('connecting');
        connText.textContent = `reconnecting (${reconnectAttempts})…`;
      } else {
        connPill.classList.add('down');
        connText.textContent = 'disconnected';
      }
    }

    function showHint(text, ms) {
      if (!connHint) return;
      connHint.textContent = text;
      clearTimeout(hintTimer);
      if (text) hintTimer = setTimeout(() => { connHint.textContent = ''; }, ms || 4000);
    }

    function setSending(active) {
      if (!sendBtn) return;
      sendBtn.disabled = active;
      sendBtn.textContent = active ? 'Thinking…' : 'Send turn ↵';
    }

    function fmtTime(ts) { return (ts % 100000).toFixed(0) + 'ms'; }

    function addEvent(kind, cls, headText, bodyHtml, meta, actionId) {
      const empty = $('emptyState');
      if (empty && empty.parentNode) empty.remove();
      const div = document.createElement('div');
      div.className = 'evt ' + cls;
      div.innerHTML = `
        <div class="evt-head"><span class="evt-kind">${esc(headText)}</span><span class="evt-ts">${fmtTime(performance.now())}</span></div>
        <div class="evt-body">${bodyHtml}</div>
        ${meta ? `<div class="evt-meta">${esc(meta)}</div>` : ''}
      `;
      timelineEl.appendChild(div);
      timelineEl.scrollTop = timelineEl.scrollHeight;
      if (actionId) toolCards[actionId] = div;
      return div;
    }

    function markTurnEnd() {
      setSending(false);
      if (turnStartedAt == null) return;
      const ms = performance.now() - turnStartedAt;
      if (latencyVal) latencyVal.textContent = ms.toFixed(0) + ' ms';
      turnStartedAt = null;
    }

    function renderAction(msg) {
      const p = msg.payload || {};
      hook('onAction', msg);
      switch (msg.action) {
        case 'filler':
          addEvent('filler', 'filler', 'Filler', `"${esc(p.text || '')}"`, p.reason || '');
          break;
        case 'tool_call': {
          latestCallGen[p.tool] = Math.max(latestCallGen[p.tool] || 0, p.generation || 0);
          const label = p.speculative ? 'Speculative call' : 'Tool call';
          const body = `<span class="spin"></span><b>${esc(p.tool)}</b>(${esc(JSON.stringify(p.args || {}))})`;
          addEvent('tool_call', 'tool_call', label, body, `gen ${p.generation} · call_id ${p.call_id}`, p.call_id);
          break;
        }
        case 'cancellation': {
          const card = toolCards[p.call_id];
          if (card) {
            card.classList.add('cancelled');
            const kindEl = card.querySelector('.evt-kind');
            if (kindEl) kindEl.textContent = 'Cancelled';
            const spin = card.querySelector('.spin');
            if (spin) spin.remove();
          }
          cancelled += 1;
          if (cancelCount) cancelCount.textContent = String(cancelled);
          addEvent('cancellation', 'cancellation', 'Cancellation',
            `Superseded: <b>${esc(p.tool)}</b> (gen ${esc(p.stale_generation)} → ${esc(p.current_generation)})`, '');
          break;
        }
        case 'clarification':
          // payload key is "question", not "text" -- see agent/protocol.py's
          // _CLARIFICATION_SCHEMA (coordinator.emit_clarification).
          addEvent('clarification', 'clarification', 'Clarification needed', esc(p.question || ''), p.field ? `missing: ${p.field}` : '');
          markTurnEnd(); // a clarification ends the turn too, same as a final_response
          break;
        case 'final_response':
          addEvent('final_response', 'final_response', 'Final response', esc(p.text || ''), '');
          markTurnEnd();
          break;
      }
    }

    // A finished (not cancelled) tool call -- server "tool_result" frame.
    // Stops the card's spinner, says what came back, and hands the result
    // to the page (e.g. the flights page renders offers) unless a newer
    // generation has already superseded it.
    function summarizeResult(msg) {
      const r = msg.result || {};
      if (msg.status === 'error') return r.error || 'error';
      if (Array.isArray(r.offers)) {
        if (r.status && r.status !== 'ok') return r.message || r.status;
        return `${r.offers.length} flight${r.offers.length === 1 ? '' : 's'} · ${r.date || ''}`;
      }
      if (msg.tool === 'nlu_extract') return `understood: ${r.intent || 'nothing yet'}${r.source ? ' (' + r.source + ')' : ''}`;
      if (r.confirmation_id) return `confirmation ${r.confirmation_id}`;
      if (r.ticket_id) return `ticket ${r.ticket_id}`;
      if (r.section) return r.section;
      const keys = Object.keys(r);
      return keys.length ? keys.slice(0, 4).join(', ') : 'done';
    }

    function renderToolResult(msg) {
      // Superseded only if a newer call to the same tool has been dispatched
      // since (e.g. a speculative search that finished just before the
      // user's correction triggered a fresh one).
      const stale = (msg.generation || 0) < (latestCallGen[msg.tool] || 0);
      const card = toolCards[msg.call_id];
      if (card) {
        const spin = card.querySelector('.spin');
        if (spin) spin.remove();
        card.classList.add(msg.status === 'error' ? 'errored' : 'done');
        if (stale) card.classList.add('stale');
        const kindEl = card.querySelector('.evt-kind');
        if (kindEl) kindEl.textContent = msg.status === 'error' ? 'Tool error' : (stale ? 'Result (stale, ignored)' : 'Result');
        const line = document.createElement('div');
        line.className = 'result-line';
        line.textContent = (msg.status === 'error' ? '⚠ ' : '✓ ') + summarizeResult(msg);
        card.appendChild(line);
      }
      if (!stale) hook('onToolResult', msg, api);
    }

    function renderState(msg) {
      latestGeneration = Math.max(latestGeneration, msg.generation || 0);
      if (genBadge) genBadge.textContent = `generation ${msg.generation}`;
      if (intentVal) intentVal.textContent = msg.intent || 'none yet';
      const slots = msg.slots || {};
      for (const f of fields) {
        const valueEl = $('val-' + f.key);
        const fieldEl = $('field-' + f.key);
        const subEl = $('sub-' + f.key);
        if (!valueEl || !fieldEl) continue;
        const v = slots[f.key];
        if (v) {
          const changed = valueEl.textContent !== String(v);
          if (changed) {
            fieldEl.classList.add('just-updated');
            setTimeout(() => fieldEl.classList.remove('just-updated'), 520);
          }
          valueEl.textContent = v;
          valueEl.classList.remove('empty');
          fieldEl.classList.add('filled');
          if (changed && subEl) hook('decorateField', f.key, String(v), subEl);
        } else {
          valueEl.textContent = '—';
          valueEl.classList.add('empty');
          fieldEl.classList.remove('filled');
          if (subEl) subEl.textContent = '';
        }
      }
    }

    // ---- WebSocket lifecycle, with auto-reconnect ---------------------
    // Each socket is tagged by identity: a late onclose from a socket
    // that's since been superseded (e.g. by the "New session" button
    // explicitly reconnecting) is ignored instead of scheduling a second,
    // duplicate reconnect loop.
    function connect() {
      clearTimeout(reconnectTimer);
      setConn(reconnectAttempts > 0 ? 'reconnecting' : 'connecting');
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      let socket;
      try {
        socket = new WebSocket(`${proto}://${location.host}/ws`);
      } catch (err) {
        // blocked by a proxy / extension / policy: pages fall back to REST
        ws = null;
        setConn('down');
        reconnectAttempts += 1;
        reconnectTimer = setTimeout(connect, Math.min(1000 * 2 ** (reconnectAttempts - 1), 8000));
        return;
      }
      ws = socket;

      socket.onopen = () => {
        if (ws !== socket) return;
        reconnectAttempts = 0;
        latestGeneration = 0;
        latestCallGen = {};
        setConn('live');
        showHint('');
        socket.send(JSON.stringify({ type: 'init', domain: config.domain || 'all' }));
        hook('onConnected');
      };

      socket.onclose = () => {
        if (ws !== socket) return; // superseded by an explicit reconnect already
        setConn('down');
        setSending(false);
        reconnectAttempts += 1;
        const delay = Math.min(1000 * 2 ** (reconnectAttempts - 1), 8000);
        showHint(`Connection dropped — retrying in ${Math.round(delay / 1000)}s…`, delay + 500);
        reconnectTimer = setTimeout(connect, delay);
      };

      socket.onerror = () => { /* onclose always follows; nothing extra to do here */ };

      socket.onmessage = (evt) => {
        if (ws !== socket) return;
        let msg;
        try { msg = JSON.parse(evt.data); } catch { return; }
        if (msg.type === 'nlu_backend' && backendChain) backendChain.textContent = msg.chain;
        else if (msg.type === 'flights_backend' && flightsBackendChain) flightsBackendChain.textContent = msg.chain;
        else if (msg.type === 'action') renderAction(msg);
        else if (msg.type === 'state') renderState(msg);
        else if (msg.type === 'tool_result') renderToolResult(msg);
        else if (msg.type === 'booking') {
          if (msg.ok && msg.booking) {
            addEvent('booking', 'booking', 'Booking (simulated)',
              `Confirmation <b>${esc(msg.booking.confirmation_id)}</b> for ${esc(msg.booking.passenger_name)}`, msg.offer_id || '');
          }
          hook('onBooking', msg);
        } else if (msg.type === 'error') showHint(msg.message || 'The agent hit an error on that turn.', 6000);
      };
    }

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
        return true;
      }
      return false;
    }

    // Only advances lastSentLength for text that was ACTUALLY transmitted.
    // (An earlier version advanced it unconditionally, so text typed while
    // the socket was still connecting -- or had dropped -- was silently
    // marked "already sent" and never actually reached the server, which
    // is exactly what made the page look unresponsive with zero feedback.)
    function sendDelta(endOfTurn) {
      const text = talk.value.slice(0, MAX_INPUT_LENGTH);
      const delta = text.slice(lastSentLength);
      if (!delta && !endOfTurn) return true;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      if (turnStartedAt == null) turnStartedAt = performance.now();
      send({ type: 'text_chunk', text: delta, end_of_turn: endOfTurn });
      lastSentLength = text.length;
      return true;
    }

    function trySendTurn() {
      clearTimeout(debounceTimer);
      const hadText = talk.value.trim().length > 0 || lastSentLength > 0;
      if (!hadText) return; // nothing typed this turn -- Enter on an empty box is a no-op
      if (!sendDelta(true)) {
        showHint('Not connected yet — your text is still in the box, try again in a second.');
        return;
      }
      talk.value = '';
      lastSentLength = 0;
      setSending(true);
      hook('onTurnSent');
    }

    let debounceTimer = null;
    if (talk) {
      talk.addEventListener('input', () => {
        if (talk.value.length > MAX_INPUT_LENGTH) {
          talk.value = talk.value.slice(0, MAX_INPUT_LENGTH);
          showHint(`Capped at ${MAX_INPUT_LENGTH} characters.`);
        }
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => sendDelta(false), 220);
      });
      talk.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          trySendTurn();
        }
      });
    }

    if (sendBtn) sendBtn.addEventListener('click', trySendTurn);

    if (interruptBtn) interruptBtn.addEventListener('click', () => {
      clearTimeout(debounceTimer);
      if (!send({ type: 'interruption' })) {
        showHint('Not connected yet — can\'t interrupt until the socket reconnects.');
        return;
      }
      talk.value = '';
      lastSentLength = 0;
      turnStartedAt = performance.now();
      setSending(false);
    });

    if (resetBtn) resetBtn.addEventListener('click', () => {
      toolCards = {};
      cancelled = 0;
      latestGeneration = 0;
      if (cancelCount) cancelCount.textContent = '0';
      timelineEl.innerHTML = emptyStateHtml;
      talk.value = '';
      lastSentLength = 0;
      setSending(false);
      renderState({ generation: 0, intent: null, slots: {} });
      hook('onReset');
      reconnectAttempts = 0;
      clearTimeout(reconnectTimer);
      if (ws) { const old = ws; ws = null; old.close(); }
      connect();
    });

    // Sends a complete turn in one frame (used by the flights page's search
    // form): same text_chunk/end_of_turn pipeline as the talk box, so the
    // trip card and timeline react identically either way.
    function sendOwnTurn(text) {
      if (!text) return false;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        showHint('Not connected yet — try again in a second.');
        return false;
      }
      clearTimeout(debounceTimer);
      talk.value = '';
      lastSentLength = 0;
      turnStartedAt = performance.now();
      send({ type: 'text_chunk', text: text, end_of_turn: true });
      setSending(true);
      hook('onTurnSent');
      return true;
    }

    // Delegated, so chips added after load (e.g. live popular routes) work too.
    document.addEventListener('click', (e) => {
      const chip = e.target.closest && e.target.closest('.chip[data-fill]');
      if (!chip || !talk) return;
      talk.value = chip.dataset.fill;
      talk.focus();
    });

    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
    async function runDemo() {
      if (demoRunning || !demoScript) return;
      demoRunning = true;
      talk.value = '';
      lastSentLength = 0;
      turnStartedAt = performance.now();
      setSending(true);
      hook('onTurnSent');
      for (const step of demoScript) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          showHint('Lost connection mid-demo — reconnecting, try the demo again once it says "live".');
          break;
        }
        if (step.interrupt) {
          send({ type: 'interruption' });
        } else {
          send({ type: 'text_chunk', text: step.text, end_of_turn: !!step.endOfTurn });
        }
        if (step.waitAfterMs) await sleep(step.waitAfterMs);
      }
      demoRunning = false;
    }
    // Disabled + relabeled while running so a second click (or an
    // impatient double-click) can't fire a second overlapping run of the
    // same script against the same session.
    document.querySelectorAll('.chip[data-demo]').forEach((chip) => {
      const originalLabel = chip.textContent;
      chip.addEventListener('click', async () => {
        if (demoRunning) return;
        chip.classList.add('disabled');
        chip.textContent = '⏳ Running…';
        await runDemo();
        chip.textContent = originalLabel;
        chip.classList.remove('disabled');
      });
    });

    const api = {
      send,
      sendTurn: sendOwnTurn,
      isConnected: () => !!(ws && ws.readyState === WebSocket.OPEN),
      setDemoScript: (steps) => { if (Array.isArray(steps) && steps.length) demoScript = steps; },
      generation: () => latestGeneration,
      esc,
    };

    checkServerVersion();
    connect();
    return api;
  }

  // The server reads page files from disk on every request, so a server
  // process started before an update serves NEW pages against its OLD API --
  // every fetch 404s and the site just looks broken. Say so, loudly.
  const API_VERSION = 3;
  async function checkServerVersion() {
    let ok = false;
    try {
      const resp = await fetch('/api/version', { cache: 'no-store' });
      if (resp.ok) ok = ((await resp.json()).api || 0) >= API_VERSION;
    } catch { ok = false; }
    if (ok || document.getElementById('serverBanner')) return ok;
    const banner = document.createElement('div');
    banner.id = 'serverBanner';
    banner.className = 'server-banner';
    banner.innerHTML = '⚠ An older server process is still answering on this port, so searches can’t work. ' +
      'Run <code>python server/app.py</code> again — it now stops the old process automatically — then reload this page. ' +
      '(Manual way on Windows: <code>netstat -ano | findstr :' + (location.port || '80') + '</code>, then <code>taskkill /PID &lt;pid&gt; /F</code>.)';
    document.body.prepend(banner);
    return ok;
  }

  global.PrismAssistant = { init, esc, checkServerVersion };
})(window);
