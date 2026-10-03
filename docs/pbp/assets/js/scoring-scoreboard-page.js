/* ============================================================================
 * scoring-scoreboard-page.js — UI for the simplified scoreboard (index.html).
 *
 * One card per game: score, state, and the three scoring-watch counters
 * (pending rulings, errors on the board, observed rescoring). Cards link
 * into the scoring feed for that game; a small PBP link opens the full
 * play-by-play page. No ABS/challenge/review counters — this scoreboard
 * watches scoring only.
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
  let TAB = ['all', 'live', 'pending', 'final'].includes(params.get('tab')) ? params.get('tab') : 'all';

  let MODEL = null, MODEL_ERROR = '';
  let CACHE = LiveBoard.createFeedCache(), LEDGER = null;
  let DOC = null, SHOWN = DATE;
  let TIMER = null, NEXT_POLL_AT = 0, BUSY = false, COUNTDOWN_TIMER = null;

  const TABS = [
    ['all', 'All games'],
    ['live', '🔴 Live'],
    ['pending', '⏳ Has pending'],
    ['final', '✓ Final'],
  ];

  function logo(teamId, abbr) {
    // Cap logo over a same-size abbreviation fallback; if the SVG fails the
    // img removes itself and the fallback shows. No onerror guessing URLs.
    const src = teamId ? `https://www.mlbstatic.com/team-logos/team-cap-on-dark/${encodeURIComponent(teamId)}.svg` : '';
    return `<span class="sw-logo-wrap"><span class="sw-logo-fb">${esc(abbr || '?')}</span>${
      src ? `<img class="sw-logo" src="${src}" alt="" loading="lazy" onerror="this.remove()">` : ''}</span>`;
  }

  function stateChip(game) {
    if (game.abstract_state === 'Live') {
      const inning = game.inning_state ? `${esc(String(game.inning_state))} ${esc(String(game.inning ?? ''))}` : 'LIVE';
      return `<span class="sw-chip sw-chip-live"><i></i>${inning}</span>`;
    }
    if (game.abstract_state === 'Final') return '<span class="sw-chip sw-chip-final">Final</span>';
    return `<span class="sw-chip">${esc(game.state || game.abstract_state || '')}</span>`;
  }

  function badge(pill) { return pill.n > 0 ? `<span class="sw-mini ${pill.cls}" title="${esc(pill.title)}">${esc(pill.label)} ${pill.n}</span>` : ''; }

  function cardHTML(record) {
    const g = record.game;
    const b = ScoringWatch.gameBadges(record);
    const live = g.abstract_state === 'Live';
    const score = side => (side == null ? '' : String(side));
    const liveLine = record.live || null;
    const line = live
      ? [((liveLine && liveLine.batter) || g.current_batter) ? `AB: ${esc((liveLine && liveLine.batter) || g.current_batter)}` : '',
         ((liveLine && liveLine.pitcher) || g.current_pitcher) ? `P: ${esc((liveLine && liveLine.pitcher) || g.current_pitcher)}` : '',
         (liveLine && liveLine.balls != null) ? `${liveLine.balls}-${liveLine.strikes} · ${liveLine.outs} out`
           : (g.balls != null) ? `${g.balls}-${g.strikes} · ${g.outs} out` : '',
        ].filter(Boolean).join(' · ')
      : g.abstract_state === 'Final' && g.winner ? `W: ${esc(g.winner)}` : '';
    const hasPending = b.pendingNow > 0;
    const teams = [
      { side: 'away', id: ((record.feed && record.feed.gameData && record.feed.gameData.teams.away) || {}).id,
        name: g.away_name, abbr: g.away_abbr || g.away_short || (g.away_name || '').slice(0, 3).toUpperCase(),
        runs: g.away_score, record: g.away_record },
      { side: 'home', id: ((record.feed && record.feed.gameData && record.feed.gameData.teams.home) || {}).id,
        name: g.home_name, abbr: g.home_abbr || g.home_short || (g.home_name || '').slice(0, 3).toUpperCase(),
        runs: g.home_score, record: g.home_record },
    ].map(t => `<div class="sw-team">
        ${logo(t.id, t.abbr)}
        <span class="sw-team-abbr">${esc(t.abbr || '')}</span>
        <span class="sw-team-name">${esc(t.name || '')}</span>
        ${t.record ? `<span class="sw-sub">${esc(t.record)}</span>` : ''}
        <b class="sw-runs ${t.side}">${esc(score(t.runs))}</b>
      </div>`).join('');
    return `<article class="sw-card ${live ? 'sw-card-live' : ''} ${hasPending ? 'sw-card-pending' : ''}">
      <div class="sw-card-top">
        ${stateChip(g)}
        <span class="sw-card-badges">
          ${badge({ n: b.pendingNow, label: '⏳', cls: 'sw-mini-pending', title: 'official-scorer rulings not yet resolved' })}
          ${badge({ n: b.errorsOnBoard, label: '✏️ err', cls: 'sw-mini-error', title: 'plays currently ruled an error' })}
          ${badge({ n: b.changes, label: '↻', cls: 'sw-mini-change', title: 'observed scoring changes' })}
        </span>
      </div>
      <a class="sw-card-main" href="reviews.html?date=${encodeURIComponent(SHOWN)}&game=${esc(record.game_pk)}&tab=all">
        ${teams}
        ${line ? `<div class="sw-card-line">${line}</div>` : ''}
      </a>
      <div class="sw-card-foot">
        <a href="reviews.html?date=${encodeURIComponent(SHOWN)}&game=${esc(record.game_pk)}">scoring feed →</a>
        <a href="game.html?gamePk=${esc(record.game_pk)}">PBP ↗</a>
      </div>
    </article>`;
  }

  function inTab(record) {
    const b = ScoringWatch.gameBadges(record);
    if (TAB === 'live') return record.game.abstract_state === 'Live';
    if (TAB === 'final') return record.game.abstract_state === 'Final';
    if (TAB === 'pending') return b.pendingNow > 0;
    return true;
  }

  function sortRecords(records) {
    const rank = r => {
      const b = ScoringWatch.gameBadges(r);
      const live = r.game.abstract_state === 'Live' ? 2 : r.game.abstract_state === 'Final' ? 0 : 1;
      return (b.pendingNow > 0 ? 3 : 0) + live;
    };
    return [...records].sort((a, b) => rank(b) - rank(a) ||
      String(a.game.game_date).localeCompare(String(b.game.game_date)));
  }

  function render() {
    if (!DOC) return;
    SHOWN = DOC.date || DATE;
    const records = DOC.games.filter(inTab);
    const all = DOC.games;
    const totals = all.reduce((acc, r) => {
      const b = ScoringWatch.gameBadges(r);
      acc.pending += b.pendingNow; acc.changes += b.changes; acc.errors += b.errorsOnBoard;
      if (r.game.abstract_state === 'Live') acc.live += 1;
      return acc;
    }, { pending: 0, changes: 0, errors: 0, live: 0 });
    $('game-list').innerHTML = records.length ? sortRecords(records).map(cardHTML).join('')
      : `<div class="sw-empty">No games for this filter on ${esc(SHOWN)}.</div>`;
    $('feed-stats').innerHTML = `
      <span class="sw-pill">${all.length} games</span>
      <span class="sw-pill">${totals.live} live</span>
      <span class="sw-pill sw-pill-pending">⏳ pending: <b>${totals.pending}</b></span>
      <span class="sw-pill sw-pill-error">✏️ errors on board: <b>${totals.errors}</b></span>
      <span class="sw-pill sw-pill-change">rescored: <b>${totals.changes}</b></span>
      <a class="sw-pill sw-pill-cta" href="reviews.html?date=${encodeURIComponent(SHOWN)}">open the scoring feed →</a>`;
    const failures = (DOC.failures || []).length + (DOC.feed_failures || []).length;
    $('status-line').textContent = `${all.length} game(s) on ${SHOWN} · updated ${LiveBoard.clockLabel(DOC.generated_utc)}` +
      (failures ? ` · ${failures} request(s) failed` : '') +
      (MODEL_ERROR ? ' · model unavailable (badges still official observations)' : '');
    if (DOC.fell_back)
      $('banner').innerHTML = `<div class="sw-flag">No games are scheduled on ${esc(DATE)} — showing the most recent slate with games (${esc(SHOWN)}).</div>`;
    else $('banner').innerHTML = '';
  }

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
      $('date-label').textContent = SHOWN;
      $('date-picker').value = SHOWN;
      render();
    } catch (error) {
      $('status-line').textContent = `refresh failed (${error && error.message ? error.message : error})`;
      if (!DOC) $('game-list').innerHTML = '<div class="sw-empty">The official Stats API could not be read from this browser. Nothing is shown rather than an invented game.</div>';
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

  function setDate(date) {
    DATE = date;
    CACHE = LiveBoard.createFeedCache();
    DOC = null; LEDGER = null;
    const url = new URL(location.href);
    url.searchParams.set('date', DATE);
    history.replaceState(null, '', url);
    $('game-list').innerHTML = '<div class="sw-empty">Loading…</div>';
    refresh('date change');
  }

  $('date-picker').value = DATE;
  $('date-picker').addEventListener('change', e => setDate(e.target.value || DATE_DEFAULT));
  $('prev-day').addEventListener('click', () => setDate(LiveBoard.addDays(DATE, -1)));
  $('next-day').addEventListener('click', () => setDate(LiveBoard.addDays(DATE, 1)));
  $('today-btn').addEventListener('click', () => setDate(DATE_DEFAULT));
  $('refresh-btn').addEventListener('click', () => refresh('manual'));
  $('tabs').addEventListener('click', e => {
    const b = e.target.closest('button.tab');
    if (!b) return;
    TAB = b.dataset.tab;
    const url = new URL(location.href);
    if (TAB !== 'all') url.searchParams.set('tab', TAB); else url.searchParams.delete('tab');
    history.replaceState(null, '', url);
    renderTabs();
    render();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      clearTimeout(TIMER);
      refresh('tab visible');
    }
  });

  function renderTabs() {
    $('tabs').innerHTML = TABS.map(([key, label]) =>
      `<button class="tab ${TAB === key ? 'tab-on' : ''}" data-tab="${key}">${esc(label)}</button>`).join('');
  }

  renderTabs();
  refresh('initial');
})();
