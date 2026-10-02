/* ============================================================================
 * scoring-feed-page.js — UI for the simplified scoring feed (reviews.html).
 *
 * Shows ONLY:
 *   ⏳ official-scorer pending rulings (live + awaiting result + resolved),
 *   ✏️ observed scoring changes (classification differed between captures),
 *   ✏️ errors currently on the board (result.eventType = field_error).
 * Each row carries ONE number: the model's hit score (see scoring-watch.js).
 * No ABS, no manager challenges, no umpire reviews, no every-batted-ball rows.
 * ==========================================================================*/
'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const DATE_DEFAULT = LiveBoard.easternDateString();
  let DATE = /^\d{4}-\d{2}-\d{2}$/.test(params.get('date') || '') ? params.get('date') : DATE_DEFAULT;
  let GAME_FILTER = (params.get('game') || '').replace(/\D/g, '');
  let TAB = ['all', 'pending', 'errors', 'changes'].includes(params.get('tab')) ? params.get('tab') : 'all';

  let MODEL = null, MODEL_ERROR = '';
  let CACHE = LiveBoard.createFeedCache(), LEDGER = null;
  let DOC = null, EVENTS = [], SHOWN = DATE;
  let TIMER = null, NEXT_POLL_AT = 0, BUSY = false, COUNTDOWN_TIMER = null;
  let SEEN_KEYS = null; // null until the first poll completes; chime only after that

  const TABS = [
    ['all', 'All'],
    ['pending', '⏳ Pending'],
    ['errors', '✏️ Errors on board'],
    ['changes', '🔁 Rescored'],
  ];

  const pctText = (p, d = 1) => Number.isFinite(Number(p)) ? `${(100 * Number(p)).toFixed(d)}%` : '—';

  /* ---------------------------------------------------------------- rows */

  function rowKind(event) {
    if (event.change) return 'change';
    if (event.kind === 'pending') {
      const status = event.ledger_pending && event.ledger_pending.status;
      return status === 'awaiting_result' ? 'awaiting' : 'pending';
    }
    if (event.ledger_pending && event.ledger_pending.status === 'resolved') return 'resolved';
    if (event.kind === 'batted_ball' && event.official_call === 'error') return 'error';
    return event.kind;
  }

  function scoreHTML(event) {
    if (!event.prediction_available) {
      return `<div class="sw-score sw-score-none">Score unavailable — ${esc(event.prediction_unavailable_reason || 'model inputs missing')}${MODEL_ERROR ? ` · model file: ${esc(MODEL_ERROR)}` : ''}</div>`;
    }
    const hit = ScoringWatch.hitScore(event);
    const dist = ScoringWatch.distribution(event.event_probs);
    const top = ScoringWatch.topGroup(event.event_probs);
    const kind = rowKind(event);
    const isErrorRow = kind === 'error';
    const sub = kind === 'pending' || kind === 'awaiting'
      ? 'chance the final call is a hit'
      : isErrorRow ? 'could a scorer see this as a hit?'
      : 'current-capture hit chance';
    const segs = dist.groups.filter(g => g.p > 0.0005).map(g =>
      `<i class="sw-seg sw-g-${esc(g.key)}" style="width:${(100 * g.p).toFixed(2)}%" title="${esc(g.label)} ${esc(pctText(g.p))}"></i>`).join('');
    const parts = dist.groups.filter(g => g.p >= 0.001).map(g =>
      `<span>${esc(g.label)} <b>${esc(pctText(g.p, 1))}</b></span>`).join('');
    const hitParts = ['single', 'double', 'triple', 'home_run']
      .filter(c => Number(event.event_probs[c]) >= 0.001)
      .map(c => `<span>${esc(LiveBoard.modelClassLabel(c))} <b>${esc(pctText(event.event_probs[c], 1))}</b></span>`);
    return `<div class="sw-score">
      <div class="sw-big"><span class="sw-num">${Number.isFinite(hit) ? (hit * 100).toFixed(0) : '—'}</span><span class="sw-unit">% HIT</span></div>
      <div class="sw-score-side">
        <div class="sw-scorelabel">${esc(sub)}</div>
        <div class="sw-band">${esc(ScoringWatch.hitBand(hit, isErrorRow))}</div>
        <div class="sw-bar" role="img" aria-label="${esc(dist.groups.map(g => `${g.label} ${pctText(g.p)}`).join(', '))}">${segs}</div>
        <div class="sw-parts">${parts}</div>
        ${hitParts.length ? `<div class="sw-parts sw-parts-hit">${hitParts.join('')}</div>` : ''}
      </div>
      <div class="sw-top">model lean: <b>${esc(ScoringWatch.groupShort(top.key))}</b> ${esc(pctText(top.p))}</div>
    </div>`;
  }

  function verdictHTML(event) {
    const ledger = event.ledger_pending;
    if (!ledger) return '';
    const v = ScoringWatch.verdictForPending(ledger);
    if (ledger.status === 'resolved') {
      let line;
      if (!v) line = '<span class="sw-verdict sw-v-unknown">no pre-ruling score captured in this browser — no verdict</span>';
      else if (v.kind === 'scored') {
        const hitSnap = ['single', 'double', 'triple', 'home_run'].reduce((s, c) =>
          s + (Number((ledger.prediction_snapshot || {})['event_p_' + c]) || 0), 0);
        line = v.correct
          ? `<span class="sw-verdict sw-v-good">✓ model lean ${esc(ScoringWatch.groupShort(v.called))} (${esc(pctText(v.calledProb, 0))}) — final ${esc(ScoringWatch.groupShort(v.finalCat))}</span>`
          : `<span class="sw-verdict sw-v-bad">✗ model lean ${esc(ScoringWatch.groupShort(v.called))} (${esc(pctText(v.calledProb, 0))}) — final ${esc(ScoringWatch.groupShort(v.finalCat))}</span> <span class="sw-sub">hit was ${esc(pctText(hitSnap, 0))} at the pending marker</span>`;
      } else if (v.kind === 'no-snapshot')
        line = `<span class="sw-verdict sw-v-unknown">resolved ${esc(LiveBoard.feedEventLabel(v.finalCode))} — no score was captured before the ruling</span>`;
      else if (v.kind === 'out-of-scope')
        line = `<span class="sw-verdict sw-v-unknown">resolved ${esc(v.finalCode)} — outside the model's categories, no verdict</span>`;
      else line = `<span class="sw-verdict sw-v-unknown">no final classification observed yet</span>`;
      return `<div class="sw-resolved">Ruling landed: <b>${esc(LiveBoard.feedEventLabel(ledger.resolved_event_type || ''))}</b>
        <code>${esc(ledger.resolved_event_type || '')}</code> ${line}</div>`;
    }
    if (ledger.status === 'awaiting_result' && ledger.latest_note)
      return `<div class="sw-note">${esc(ledger.latest_note)}</div>`;
    return '';
  }

  function pendingScopeHTML(event) {
    const p = event.pending || (event.ledger_pending ? {
      kind: event.ledger_pending.pending_kind || 'description_only',
      codes: event.ledger_pending.pending_codes || [],
    } : null);
    if (!p) return '';
    const kindLabel = { primary: 'plate-appearance ruling', prior: 'prior base-running event',
      both: 'primary + prior-event markers', description_only: 'exact registry description' }[p.kind] || 'exact marker';
    const priorOnly = p.kind === 'prior';
    return `<div class="sw-scope">Official scorer ruling pending — ${esc(kindLabel)}
      ${p.codes && p.codes.length ? `<code>${esc(p.codes.join(', '))}</code>` : ''}
      ${priorOnly ? '<div class="sw-note">A prior-event pending marker is a base-running call; the batter-outcome model does not apply to it, so this row carries no hit score.</div>' : ''}</div>`;
  }

  function changeHTML(event) {
    const c = event.change;
    if (!c) return '';
    const fromCat = ScoringWatch.categoryOfEvent(c.from_event_type);
    const toCat = ScoringWatch.categoryOfEvent(c.to_event_type);
    const isErrorRescore = fromCat === 'error' || toCat === 'error';
    return `<div class="sw-change ${isErrorRescore ? 'sw-change-err' : ''}">
      <b>Rescored:</b> ${esc(LiveBoard.feedEventLabel(c.from_event_type) || c.from_event_type || 'not captured')}
      <span class="sw-arrow">→</span> <b>${esc(LiveBoard.feedEventLabel(c.to_event_type) || c.to_event_type || 'not captured')}</b>
      <div class="sw-sub">first seen “${esc(c.initial_description || 'not captured')}”
        ${c.first_observed_utc ? `(${esc(LiveBoard.clockLabel(c.first_observed_utc))})` : ''}
        · final “${esc(c.final_description || 'not captured')}”
        ${c.changed_observed_utc ? `(changed ${esc(LiveBoard.clockLabel(c.changed_observed_utc))})` : ''}</div>
      <div class="sw-sub">${esc(c.note || '')}</div></div>`;
  }

  function badgeHTML(kind) {
    const map = {
      pending: ['⏳ PENDING RULING', 'sw-b-pending'],
      awaiting: ['⏳ MARKER CLEARED', 'sw-b-pending'],
      resolved: ['✓ RULING LANDED', 'sw-b-resolved'],
      error: ['✏️ ERROR ON BOARD', 'sw-b-error'],
      change: ['🔁 RESCORED', 'sw-b-change'],
    };
    const [label, cls] = map[kind] || [kind, ''];
    return `<span class="sw-badge ${cls}">${esc(label)}</span>`;
  }

  function rowHTML(event) {
    const kind = rowKind(event);
    const when = event.observed_at ? LiveBoard.clockLabel(event.observed_at) : '';
    const newest = event.observed_at && (Date.now() - new Date(event.observed_at).getTime()) < 120000;
    const away = event.scores ? event.scores.away : null;
    const home = event.scores ? event.scores.home : null;
    const head = `${esc(event.away_abbr || '')} ${away == null ? '' : esc(String(away))}–${home == null ? '' : esc(String(home))} ${esc(event.home_abbr || '')}`;
    const inning = event.inning ? `${esc(LiveBoard.halfLabel(event.half))} ${esc(String(event.inning))}` : '';
    const vector = event.launch_speed != null
      ? `${esc(String(event.launch_speed))} mph · ${esc(String(event.launch_angle))}° · ${esc(String(event.distance))} ft${event.trajectory ? ` · ${esc(String(event.trajectory).replace(/_/g, ' '))}` : ''}` : '';
    const base = event.bases && event.bases !== '-'
      ? ` · runners ${esc(event.bases)} · ${esc(String(event.outs_before ?? 0))} out` : '';
    return `<article class="feed-row sw-row sw-r-${esc(kind)}">
      <div class="feed-head">
        <span class="sw-when">${esc(when)}${newest ? ' <span class="sw-new">NEW</span>' : ''}
          <span class="sw-sub">${event.observed_at ? 'first seen by this browser' : ''}</span></span>
        <a class="sw-game" href="reviews.html?date=${encodeURIComponent(SHOWN)}&game=${esc(event.game_pk)}">${esc(event.matchup || '')} <span class="sw-sub">${head}</span></a>
        <span class="sw-inn">${inning}${event.at_bat != null ? ` · PA ${esc(String(event.at_bat))}` : ''}</span>
        ${badgeHTML(kind)}
      </div>
      <div class="sw-desc"><b>${esc(event.batter || '')}</b>${event.pitcher ? ` vs ${esc(event.pitcher)}` : ''} — ${esc(event.description || 'No description supplied in the payload.')}</div>
      <div class="sw-meta">${vector}${base}</div>
      ${pendingScopeHTML(event)}
      ${changeHTML(event)}
      ${scoreHTML(event)}
      ${verdictHTML(event)}
      <div class="sw-foot">
        ${event.savant_url ? `<a href="${esc(event.savant_url)}" target="_blank" rel="noopener">video ↗</a>` : ''}
        <a href="${esc(event.official_feed_url)}" target="_blank" rel="noopener">official feed ↗</a>
        <a href="game.html?gamePk=${esc(event.game_pk)}">game PBP →</a>
        ${event.state ? `<span class="sw-sub">${esc(event.state)}</span>` : ''}
      </div>
    </article>`;
  }

  /* ------------------------------------------------------------ filters */

  function inTab(event, tab) {
    const kind = rowKind(event);
    if (tab === 'pending') return kind === 'pending' || kind === 'awaiting';
    if (tab === 'errors') return kind === 'error' || (kind === 'change' &&
      (ScoringWatch.categoryOfEvent(event.change.from_event_type) === 'error' ||
       ScoringWatch.categoryOfEvent(event.change.to_event_type) === 'error'));
    if (tab === 'changes') return kind === 'change';
    return true;
  }

  function filtered() {
    return ScoringWatch.scoringEvents(EVENTS)
      .filter(e => inTab(e, TAB))
      .filter(e => !GAME_FILTER || String(e.game_pk) === String(GAME_FILTER));
  }

  /* ------------------------------------------------------------- render */

  function renderTabs() {
    const counts = {};
    const all = ScoringWatch.scoringEvents(EVENTS);
    TABS.forEach(([key]) => {
      counts[key] = all.filter(e => inTab(e, key))
        .filter(e => !GAME_FILTER || String(e.game_pk) === String(GAME_FILTER)).length;
    });
    $('feed-tabs').innerHTML = TABS.map(([key, label]) =>
      `<button class="tab ${TAB === key ? 'tab-on' : ''}" data-tab="${key}">${esc(label)} (${counts[key]})</button>`).join('');
    $('feed-tabs').querySelectorAll('button.tab').forEach(b => {
      b.addEventListener('click', () => { TAB = b.dataset.tab; syncURL(); renderTabs(); render(); });
    });
  }

  function tallyHTML() {
    const t = ScoringWatch.dayTally((LEDGER && LEDGER.pending) || []);
    if (!t.decided) return '<span class="sw-pill">no pending rulings decided yet today</span>';
    return `<span class="sw-pill">pending rulings decided: <b>${t.decided}</b></span>
      <span class="sw-pill">model lean correct: <b>${t.topCorrect}/${t.decided}</b></span>
      <span class="sw-pill">said HIT → was a hit: <b>${t.hitWhenSaid}/${t.saidHit}</b></span>`;
  }

  function render() {
    if (!DOC) return;
    SHOWN = DOC.date || DATE;
    const rows = filtered();
    const all = ScoringWatch.scoringEvents(EVENTS);
    const pendingNow = all.filter(e => rowKind(e) === 'pending' || rowKind(e) === 'awaiting').length;
    const errors = all.filter(e => rowKind(e) === 'error').length;
    const changes = all.filter(e => rowKind(e) === 'change').length;
    $('feed-stats').innerHTML = `
      <span class="sw-pill sw-pill-pending">⏳ pending now: <b>${pendingNow}</b></span>
      <span class="sw-pill sw-pill-error">✏️ errors on board: <b>${errors}</b></span>
      <span class="sw-pill sw-pill-change">rescored: <b>${changes}</b></span>
      ${tallyHTML()}
      <span class="sw-pill">${DOC.games.length} games scanned</span>`;
    $('feed-list').innerHTML = rows.length ? rows.map(rowHTML).join('')
      : `<div class="sw-empty">No scoring events for this filter on ${esc(SHOWN)}.
         Pending rulings and scoring changes appear here the moment a poll of the
         official feed captures them${EVENTS.length ? '' : '; nothing has been captured yet'}.</div>`;
    const failures = (DOC.failures || []).length + (DOC.feed_failures || []).length;
    $('status-line').textContent = `${rows.length} scoring row(s) shown · updated ${LiveBoard.clockLabel(DOC.generated_utc)}` +
      (failures ? ` · ${failures} request(s) failed` : '') +
      (MODEL_ERROR ? ' · model file unavailable — rows show without scores' : '');
  }

  function renderBanner() {
    const notes = (DOC && DOC.notes) || [];
    const failures = DOC ? (DOC.failures || []).concat(DOC.feed_failures || []) : [];
    const bits = [];
    if (DOC && DOC.fell_back)
      bits.push(`No games are scheduled on ${esc(DATE)} — showing the most recent slate with games (${esc(SHOWN)}).`);
    if (MODEL_ERROR) bits.push(`The committed model file could not be loaded (${esc(MODEL_ERROR)}); rows appear without scores.`);
    if (GAME_FILTER) {
      const g = (DOC && DOC.games || []).find(r => String(r.game_pk) === GAME_FILTER);
      if (g) bits.push(`Filtered to ${esc(g.matchup)} — <a href="${esc(location.pathname)}?date=${encodeURIComponent(SHOWN)}">clear</a>.`);
    }
    $('banner').innerHTML = bits.map(b => `<div class="sw-flag">${b}</div>`).join('') ||
      (failures.length ? `<div class="sw-flag">${failures.length} request(s) failed on the last poll: ${esc(failures.slice(0, 3).join(' · '))}${failures.length > 3 ? ' …' : ''}</div>` : '');
  }

  /* -------------------------------------------------------------- poll */

  async function refresh(reason) {
    if (BUSY) return;
    BUSY = true;
    $('countdown').textContent = '…';
    try {
      if (!MODEL) {
        try { MODEL = await ScoringWatch.loadModel(); MODEL_ERROR = ''; }
        catch (error) { MODEL_ERROR = error && error.message ? error.message : String(error); }
      }
      DOC = await LiveBoard.pollDay({
        date: DATE, model: MODEL, cache: CACHE, storage: window.localStorage,
        ledger: LEDGER,
      });
      LEDGER = DOC.ledger;
      SHOWN = DOC.date || DATE;
      EVENTS = DOC.events;
      const keys = new Set(ScoringWatch.scoringEvents(EVENTS).map(e => e.key));
      if (SEEN_KEYS !== null) {
        let fresh = false;
        keys.forEach(k => { if (!SEEN_KEYS.has(k)) fresh = true; });
        if (fresh) ScoringWatch.chime();
      }
      SEEN_KEYS = keys;
      $('date-label').textContent = SHOWN;
      $('date-picker').value = SHOWN;
      renderBanner();
      renderTabs();
      render();
    } catch (error) {
      $('status-line').textContent = `refresh failed (${error && error.message ? error.message : error})`;
      if (!EVENTS.length)
        $('feed-list').innerHTML = '<div class="sw-empty">The official Stats API could not be read from this browser. Nothing is shown rather than an invented event.</div>';
    } finally {
      BUSY = false;
      scheduleNext();
    }
  }

  function anyLive() {
    return Boolean(DOC && (DOC.games || []).some(r => r.game.abstract_state === 'Live'));
  }

  function scheduleNext() {
    clearTimeout(TIMER);
    const wait = anyLive() ? LiveBoard.LIVE_POLL_MS : LiveBoard.IDLE_POLL_MS;
    NEXT_POLL_AT = Date.now() + wait;
    TIMER = setTimeout(() => {
      if (document.visibilityState === 'visible') refresh('auto');
      else scheduleNext();
    }, wait);
  }

  clearInterval(COUNTDOWN_TIMER);
  COUNTDOWN_TIMER = setInterval(() => {
    const left = Math.max(0, Math.round((NEXT_POLL_AT - Date.now()) / 1000));
    $('countdown').textContent = `${left}s`;
  }, 500);

  /* --------------------------------------------------------------- nav */

  function syncURL() {
    const url = new URL(location.href);
    url.searchParams.set('date', DATE);
    if (GAME_FILTER) url.searchParams.set('game', GAME_FILTER); else url.searchParams.delete('game');
    if (TAB !== 'all') url.searchParams.set('tab', TAB); else url.searchParams.delete('tab');
    history.replaceState(null, '', url);
  }

  function setDate(date) {
    DATE = date;
    CACHE = LiveBoard.createFeedCache();
    DOC = null; EVENTS = []; LEDGER = null; SEEN_KEYS = null;
    syncURL();
    $('feed-list').innerHTML = '<div class="sw-empty">Loading…</div>';
    refresh('date change');
  }

  function wireSoundButton() {
    const btn = $('sound-toggle-btn');
    const paint = () => {
      btn.textContent = ScoringWatch.soundOn() ? '🔔 Sound On' : '🔇 Sound Off';
      btn.classList.toggle('btn-sound-on', ScoringWatch.soundOn());
    };
    btn.addEventListener('click', () => { ScoringWatch.setSound(!ScoringWatch.soundOn()); paint(); });
    paint();
  }

  window.ScoringFeedPage = { refresh, setDate, get state() { return { EVENTS, DOC, LEDGER, TAB, GAME_FILTER, DATE, SHOWN }; } };

  /* --------------------------------------------------------------- init */
  $('date-picker').value = DATE;
  $('date-picker').addEventListener('change', e => setDate(e.target.value || DATE_DEFAULT));
  $('prev-day').addEventListener('click', () => setDate(LiveBoard.addDays(DATE, -1)));
  $('next-day').addEventListener('click', () => setDate(LiveBoard.addDays(DATE, 1)));
  $('today-btn').addEventListener('click', () => setDate(DATE_DEFAULT));
  $('refresh-btn').addEventListener('click', () => refresh('manual'));
  wireSoundButton();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      clearTimeout(TIMER);
      refresh('tab visible');
    }
  });
  refresh('initial');
})();
