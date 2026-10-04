/*
 * Shared WebSocket client for every live-assistant page (flights.html,
 * support.html, ...). One copy of the protocol/rendering logic instead of
 * duplicating it per page -- each page just supplies a small config:
 *
 *   PrismAssistant.init({
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
 *   });
 *
 * Expects the page to provide these element ids: talk, sendBtn,
 * interruptBtn, resetBtn, timeline, emptyState, genBadge, intentVal,
 * backendChain, latencyVal, cancelCount, connPill, connText, fieldsGrid,
 * and any number of elements with [data-demo] / [data-fill="..."].
 */
(function (global) {
  function init(config) {
    const $ = (id) => document.getElementById(id);
    const timelineEl = $('timeline');
    const emptyState = $('emptyState');
    const connPill = $('connPill');
    const connText = $('connText');
    const genBadge = $('genBadge');
    const intentVal = $('intentVal');
    const backendChain = $('backendChain');
    const latencyVal = $('latencyVal');
    const cancelCount = $('cancelCount');
    const talk = $('talk');
    const fieldsGrid = $('fieldsGrid');

    const fields = config.fields || [];
    let cancelled = 0;
    let turnStartedAt = null;
    let toolCards = {};
    let ws = null;
    let lastSentLength = 0;
    let demoRunning = false;

    // Build the live-understanding field cards once, from config -- so a
    // Support page shows Device/Topic/Issue instead of From/To/Date, with
    // zero per-page HTML duplication.
    if (fieldsGrid) {
      fieldsGrid.style.gridTemplateColumns = `repeat(${Math.min(fields.length, 3) || 1}, 1fr)`;
      fields.forEach((f) => {
        const div = document.createElement('div');
        div.className = 'field';
        div.id = 'field-' + f.key;
        div.innerHTML = `<span class="field-label">${f.label}</span><span class="field-value empty" id="val-${f.key}">—</span>`;
        fieldsGrid.appendChild(div);
      });
    }

    function setConn(live) {
      if (!connPill) return;
      connPill.classList.toggle('live', live);
      connPill.classList.toggle('down', !live);
      connText.textContent = live ? 'live' : 'disconnected';
    }

    function fmtTime(ts) { return (ts % 100000).toFixed(0) + 'ms'; }

    function addEvent(kind, cls, headText, bodyHtml, meta, actionId) {
      if (emptyState && emptyState.parentNode) emptyState.remove();
      const div = document.createElement('div');
      div.className = 'evt ' + cls;
      div.innerHTML = `
        <div class="evt-head"><span class="evt-kind">${headText}</span><span class="evt-ts">${fmtTime(performance.now())}</span></div>
        <div class="evt-body">${bodyHtml}</div>
        ${meta ? `<div class="evt-meta">${meta}</div>` : ''}
      `;
      timelineEl.appendChild(div);
      timelineEl.scrollTop = timelineEl.scrollHeight;
      if (actionId) toolCards[actionId] = div;
      return div;
    }

    function markTurnEnd() {
      if (turnStartedAt == null) return;
      const ms = performance.now() - turnStartedAt;
      if (latencyVal) latencyVal.textContent = ms.toFixed(0) + ' ms';
      turnStartedAt = null;
    }

    function renderAction(msg) {
      const p = msg.payload || {};
      switch (msg.action) {
        case 'filler':
          addEvent('filler', 'filler', 'Filler', `"${p.text || ''}"`, p.reason || '');
          break;
        case 'tool_call': {
          const label = p.speculative ? 'Speculative call' : 'Tool call';
          const body = `<span class="spin"></span><b>${p.tool}</b>(${JSON.stringify(p.args || {})})`;
          addEvent('tool_call', 'tool_call', label, body, `gen ${p.generation} · call_id ${p.call_id}`, p.call_id);
          break;
        }
        case 'cancellation': {
          const card = toolCards[p.call_id];
          if (card) {
            card.classList.add('cancelled');
            const kindEl = card.querySelector('.evt-kind');
            if (kindEl) kindEl.textContent = 'Cancelled';
          }
          cancelled += 1;
          if (cancelCount) cancelCount.textContent = String(cancelled);
          addEvent('cancellation', 'cancellation', 'Cancellation',
            `Superseded: <b>${p.tool}</b> (gen ${p.stale_generation} → ${p.current_generation})`, '');
          break;
        }
        case 'clarification':
          // payload key is "question", not "text" -- see agent/protocol.py's
          // _CLARIFICATION_SCHEMA (coordinator.emit_clarification).
          addEvent('clarification', 'clarification', 'Clarification needed', p.question || '', p.field ? `missing: ${p.field}` : '');
          markTurnEnd(); // a clarification ends the turn too, same as a final_response
          break;
        case 'final_response':
          addEvent('final_response', 'final_response', 'Final response', p.text || '', '');
          markTurnEnd();
          break;
      }
    }

    function renderState(msg) {
      if (genBadge) genBadge.textContent = `generation ${msg.generation}`;
      if (intentVal) intentVal.textContent = msg.intent || 'none yet';
      const slots = msg.slots || {};
      for (const f of fields) {
        const valueEl = $('val-' + f.key);
        const fieldEl = $('field-' + f.key);
        if (!valueEl || !fieldEl) continue;
        const v = slots[f.key];
        if (v) {
          if (valueEl.textContent !== String(v)) {
            fieldEl.classList.add('just-updated');
            setTimeout(() => fieldEl.classList.remove('just-updated'), 520);
          }
          valueEl.textContent = v;
          valueEl.classList.remove('empty');
          fieldEl.classList.add('filled');
        } else {
          valueEl.textContent = '—';
          valueEl.classList.add('empty');
          fieldEl.classList.remove('filled');
        }
      }
    }

    function connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        setConn(true);
        ws.send(JSON.stringify({ type: 'init', domain: config.domain || 'all' }));
      };
      ws.onclose = () => setConn(false);
      ws.onerror = () => setConn(false);
      ws.onmessage = (evt) => {
        let msg;
        try { msg = JSON.parse(evt.data); } catch { return; }
        if (msg.type === 'nlu_backend' && backendChain) backendChain.textContent = msg.chain;
        else if (msg.type === 'action') renderAction(msg);
        else if (msg.type === 'state') renderState(msg);
      };
    }

    function send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    }

    function sendDelta(endOfTurn) {
      const text = talk.value;
      const delta = text.slice(lastSentLength);
      lastSentLength = text.length;
      if (!delta && !endOfTurn) return;
      if (turnStartedAt == null) turnStartedAt = performance.now();
      send({ type: 'text_chunk', text: delta, end_of_turn: endOfTurn });
    }

    let debounceTimer = null;
    if (talk) {
      talk.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => sendDelta(false), 220);
      });
      talk.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          clearTimeout(debounceTimer);
          sendDelta(true);
          talk.value = '';
          lastSentLength = 0;
        }
      });
    }

    const sendBtn = $('sendBtn');
    if (sendBtn) sendBtn.addEventListener('click', () => {
      clearTimeout(debounceTimer);
      sendDelta(true);
      talk.value = '';
      lastSentLength = 0;
    });

    const interruptBtn = $('interruptBtn');
    if (interruptBtn) interruptBtn.addEventListener('click', () => {
      clearTimeout(debounceTimer);
      send({ type: 'interruption' });
      talk.value = '';
      lastSentLength = 0;
      turnStartedAt = performance.now();
    });

    const resetBtn = $('resetBtn');
    if (resetBtn) resetBtn.addEventListener('click', () => {
      toolCards = {};
      cancelled = 0;
      if (cancelCount) cancelCount.textContent = '0';
      timelineEl.innerHTML = '<div class="empty-state" id="emptyState">Nothing yet — try a chip above, or type your own request.</div>';
      talk.value = '';
      lastSentLength = 0;
      if (ws) ws.close();
      connect();
    });

    document.querySelectorAll('.chip[data-fill]').forEach((chip) => {
      chip.addEventListener('click', () => {
        talk.value = chip.dataset.fill;
        talk.focus();
      });
    });

    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
    async function runDemo() {
      if (demoRunning || !config.demoScript) return;
      demoRunning = true;
      talk.value = '';
      lastSentLength = 0;
      turnStartedAt = performance.now();
      for (const step of config.demoScript) {
        if (step.interrupt) {
          send({ type: 'interruption' });
        } else {
          send({ type: 'text_chunk', text: step.text, end_of_turn: !!step.endOfTurn });
        }
        if (step.waitAfterMs) await sleep(step.waitAfterMs);
      }
      demoRunning = false;
    }
    document.querySelectorAll('.chip[data-demo]').forEach((chip) => {
      chip.addEventListener('click', runDemo);
    });

    connect();
  }

  global.PrismAssistant = { init };
})(window);
