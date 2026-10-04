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
 * connHint (optional) gets a one-line status message when a send can't
 * go out right now (not connected yet / reconnecting).
 */
(function (global) {
  const MAX_INPUT_LENGTH = 2000; // matches the server-side PRISM_MAX_TEXT_LENGTH cap

  function init(config) {
    const $ = (id) => document.getElementById(id);
    const timelineEl = $('timeline');
    const emptyState = $('emptyState');
    const connPill = $('connPill');
    const connText = $('connText');
    const connHint = $('connHint');
    const genBadge = $('genBadge');
    const intentVal = $('intentVal');
    const backendChain = $('backendChain');
    const latencyVal = $('latencyVal');
    const cancelCount = $('cancelCount');
    const talk = $('talk');
    const fieldsGrid = $('fieldsGrid');
    const sendBtn = $('sendBtn');
    const interruptBtn = $('interruptBtn');
    const resetBtn = $('resetBtn');

    const fields = config.fields || [];
    let cancelled = 0;
    let turnStartedAt = null;
    let toolCards = {};
    let ws = null;
    let lastSentLength = 0;
    let demoRunning = false;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let hintTimer = null;

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
      setSending(false);
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

    // ---- WebSocket lifecycle, with auto-reconnect ---------------------
    // Each socket is tagged by identity: a late onclose from a socket
    // that's since been superseded (e.g. by the "New session" button
    // explicitly reconnecting) is ignored instead of scheduling a second,
    // duplicate reconnect loop.
    function connect() {
      clearTimeout(reconnectTimer);
      setConn(reconnectAttempts > 0 ? 'reconnecting' : 'connecting');
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(`${proto}://${location.host}/ws`);
      ws = socket;

      socket.onopen = () => {
        if (ws !== socket) return;
        reconnectAttempts = 0;
        setConn('live');
        showHint('');
        socket.send(JSON.stringify({ type: 'init', domain: config.domain || 'all' }));
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
        else if (msg.type === 'action') renderAction(msg);
        else if (msg.type === 'state') renderState(msg);
        else if (msg.type === 'error') showHint(msg.message || 'The agent hit an error on that turn.', 6000);
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
      if (cancelCount) cancelCount.textContent = '0';
      timelineEl.innerHTML = '<div class="empty-state" id="emptyState">Nothing yet — try a chip above, or type your own request.</div>';
      talk.value = '';
      lastSentLength = 0;
      setSending(false);
      reconnectAttempts = 0;
      clearTimeout(reconnectTimer);
      if (ws) { const old = ws; ws = null; old.close(); }
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
      setSending(true);
      for (const step of config.demoScript) {
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

    connect();
  }

  global.PrismAssistant = { init };
})(window);
