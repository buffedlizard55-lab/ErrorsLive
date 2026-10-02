/* ============================================================================
   live-board.js — shared data layer for the live scoreboard and the all-games
   scoring feed.

   The two new pages (docs/scoreboard.html, docs/allgames.html) share this one
   data path so the scoreboard and the feed can never disagree about a play:

     1. GET /api/v1/schedule?sportId=1&date=…&hydrate=review,linescore,
        decisions,probablePitcher,team
        — verified live 2026-10-02: carries team abbreviations, records,
        probable pitchers, linescore runs/hits/errors/leftOnBase, the live
        count/outs/batter/pitcher and the official manager-challenge counters
        (`review.away/home.used/remaining`).
     2. GET /api/v1.1/game/{gamePk}/feed/live (fields-projected) for every
        Live game and every Final game inside the final-rescan window. The
        projection below lists every leaf field name this module and the
        shared scorer read. If the API ever renames one, the affected field
        simply stops arriving and the row says what is missing — nothing is
        guessed (see the retry-without-projection fallback in refreshFeeds).
     3. `scoreLiveFeed()` (docs/site.js) scores every captured batted ball with
        the committed model; `ScoringFeed` (docs/scoring-feed.js) provides the
        exact official-scorer pending vocabulary and the browser observation
        ledger. This module never re-implements either.

   What this module will NOT do:
     * It never invents a time for a play. The official payload carries no
       per-play timestamp, so feed rows show when THIS BROWSER first observed
       the play (`first seen`), stored per date in localStorage. That is an
       observation time, not an official one, and the pages label it as such.
     * It never guesses a scorer decision. A pending marker is the exact
       registry value `os_ruling_pending_primary` / `os_ruling_pending_prior`
       or the exact description ("Official Scorer Ruling Pending"); a missing
       `result.eventType` is shown as "no event type in this capture" and is
       never reported as a pending decision.
     * It never predicts a review outcome. Review rows show the official
       `reviewDetails` fields that are present (usually `reviewType` and
       `isOverturned`) and a label looked up live in the league's own
       `/api/v1/gameStatus` registry; when the registry cannot be fetched the
       raw code is shown instead of an invented label.

   Not affiliated with MLB or MLB Advanced Media.
   ==========================================================================*/
'use strict';

var LiveBoard = (function () {
  const MLB = 'https://statsapi.mlb.com';
  const V11 = MLB + '/api/v1.1';

  /* Every leaf field name the scorer, the pending detector and the renderers
     read from the live feed. The API's `fields` filter keeps any name in this
     list wherever it appears in the JSON tree and drops the rest. */
  const FEED_FIELDS = [
    'gameData', 'game', 'gamePk', 'status', 'abstractGameState', 'detailedState', 'codedGameState',
    'teams', 'away', 'home', 'id', 'name', 'abbreviation', 'teamName', 'venue',
    'review', 'hasChallenges', 'used', 'remaining', 'absChallenges', 'usedSuccessful', 'usedFailed',
    'liveData', 'plays', 'allPlays', 'currentPlay', 'result', 'event', 'eventType', 'description',
    'rbi', 'awayScore', 'homeScore', 'about', 'inning', 'halfInning', 'atBatIndex', 'isComplete',
    'isTopInning', 'playEvents', 'details', 'playId', 'index', 'type', 'count', 'outs', 'balls',
    'strikes', 'hitData', 'launchSpeed', 'launchAngle', 'totalDistance', 'trajectory', 'hardness',
    'matchup', 'batter', 'pitcher', 'fullName', 'batSide', 'pitchHand', 'code',
    'runners', 'movement', 'originBase', 'start', 'end', 'outBase', 'isOut', 'isScoringEvent',
    'reviewDetails', 'isOverturned', 'inProgress', 'reviewType', 'challengeTeamId',
    'linescore', 'currentInning', 'currentInningOrdinal', 'inningState', 'inningHalf',
    'offense', 'onFirst', 'onSecond', 'onThird', 'defense', 'leftOnBase', 'hits', 'errors', 'runs',
  ].join(',');

  const LIVE_POLL_MS = 30 * 1000;          // live slates: schedule unchanged, feeds re-polled
  const IDLE_POLL_MS = 5 * 60 * 1000;      // no live game and nothing recently final
  const FINAL_RESCAN_WINDOW_MS = 30 * 60 * 1000;  // re-check finals after they end (late rulings)
  const FINAL_RESCAN_POLL_MS = 30 * 1000;
  const MAX_CONCURRENT_FEEDS = 4;
  const FEED_TIMEOUT_MS = 18 * 1000;
  const SCHEDULE_TIMEOUT_MS = 15 * 1000;

  const SEEN_KEY = 'errorslive.live-board.seen.v1';
  const SEEN_DATES_KEPT = 3;
  const SEEN_PER_DATE_CAP = 4000;

  /* Human labels for the model's 11 outcome classes and the feed's own event
     types. A label is only ever a display name for a code the model or feed
     actually used; an unknown code is shown raw, never guessed at. */
  const MODEL_CLASS_LABELS = {
    single: 'Single', double: 'Double', triple: 'Triple', home_run: 'Home run',
    error: 'Error', fielders_choice: "Fielder's choice", field_out: 'Field out',
    force_out: 'Force out', double_play: 'Double play', sac_fly: 'Sacrifice fly',
    sac_bunt: 'Sacrifice bunt', hit: 'Hit', out: 'Out',
  };
  const FEED_EVENT_LABELS = {
    single: 'Single', double: 'Double', triple: 'Triple', home_run: 'Home run',
    field_error: 'Error', fielders_choice: "Fielder's choice",
    fielders_choice_out: "Fielder's choice out", field_out: 'Field out',
    force_out: 'Force out', grounded_into_double_play: 'Grounded into double play',
    double_play: 'Double play', sac_fly: 'Sacrifice fly', sac_bunt: 'Sacrifice bunt',
    sac_fly_double_play: 'Sac fly double play', triple_play: 'Triple play',
    strikeout: 'Strikeout', strikeout_double_play: 'Strikeout double play',
    walk: 'Walk', intentional_walk: 'Intentional walk', hit_by_pitch: 'Hit by pitch',
    catcher_interfence: 'Catcher interference', stolen_base_2b: 'Stolen base (2B)',
    stolen_base_3b: 'Stolen base (3B)', stolen_base_home: 'Steal of home',
    caught_stealing_2b: 'Caught stealing (2B)', caught_stealing_3b: 'Caught stealing (3B)',
    caught_stealing_home: 'Caught stealing (home)', pickoff_1b: 'Pickoff (1B)',
    pickoff_2b: 'Pickoff (2B)', wild_pitch: 'Wild pitch', passed_ball: 'Passed ball',
    balk: 'Balk', runner_out: 'Runner out', runner_double_play: 'Runner double play',
    game_advisory: 'Game advisory', other_out: 'Other out',
  };
  function ordinal(n) {
    const value = Math.round(Number(n));
    if (!Number.isFinite(value)) return '';
    const mod100 = Math.abs(value) % 100;
    if (mod100 >= 11 && mod100 <= 13) return `${value}th`;
    return `${value}${['th', 'st', 'nd', 'rd'][Math.abs(value) % 10] || 'th'}`;
  }
  function modelClassLabel(code) {
    return MODEL_CLASS_LABELS[code] || (code ? String(code).replace(/_/g, ' ') : '');
  }
  function feedEventLabel(code) {
    return FEED_EVENT_LABELS[code] || (code ? String(code).replace(/_/g, ' ') : '');
  }

  /* ------------------------------------------------------------- date/time */

  function easternDateString(now = new Date()) {
    const p = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
    const part = k => p.find(x => x.type === k).value;
    return `${part('year')}-${part('month')}-${part('day')}`;
  }

  /** YYYY-MM-DD arithmetic with no timezone conversion (dates are plain days). */
  function addDays(dateStr, delta) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr || ''));
    if (!m) return '';
    const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    d.setUTCDate(d.getUTCDate() + delta);
    return d.toISOString().slice(0, 10);
  }

  function clockLabel(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  }

  function agoLabel(iso, nowMs = Date.now()) {
    if (!iso) return '';
    const ms = nowMs - new Date(iso).getTime();
    if (!Number.isFinite(ms)) return '';
    if (ms < 0) return 'just now';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  function halfLabel(half) {
    const h = String(half || '').toLowerCase();
    return h === 'top' ? '▲' : h === 'bottom' ? '▼' : '';
  }

  /** Bases string in the same 1B,2B,3B form docs/site.js writes on model rows. */
  function basesFromRunners(runners) {
    const out = [];
    (Array.isArray(runners) ? runners : []).forEach(r => {
      const move = (r && r.movement) || {};
      const start = move.start || move.originBase;
      if (['1B', '2B', '3B'].includes(start) && move.end && move.end !== 'score' && !move.isOut &&
          ['1B', '2B', '3B'].includes(move.end)) out.push(move.end);
    });
    return [...new Set(out)].sort().join(',') || '-';
  }

  /* ---------------------------------------------------------------- network */

  async function fetchJSON(url, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function scheduleUrl(date) {
    const u = new URL(`${MLB}/api/v1/schedule`);
    u.searchParams.set('sportId', '1');
    u.searchParams.set('date', date);
    u.searchParams.set('hydrate', 'review,linescore,decisions,probablePitcher,team');
    u.searchParams.set('gameType', 'R,F,D,L,W');
    return u.toString();
  }

  function feedUrl(gamePk, projected = true) {
    const base = `${V11}/game/${encodeURIComponent(gamePk)}/feed/live`;
    return projected ? `${base}?fields=${encodeURIComponent(FEED_FIELDS)}` : base;
  }

  /* ------------------------------------------------------------ normalization */

  function teamSideOfPlay(play) {
    const about = (play && play.about) || {};
    return about.halfInning === 'top' ? 'away' : about.halfInning === 'bottom' ? 'home' : '';
  }

  function normalizeScheduleGame(g) {
    const teams = g.teams || {};
    const away = (teams.away || {}), home = (teams.home || {});
    const awayTeam = away.team || {}, homeTeam = home.team || {};
    const status = g.status || {};
    const linescore = g.linescore || {};
    const lsTeams = linescore.teams || {};
    const review = g.review || {};
    const offense = linescore.offense || {};
    const defense = linescore.defense || {};
    return {
      game_pk: String(g.gamePk || ''),
      game_date: g.gameDate || '',
      game_type: g.gameType || '',
      official_date: g.officialDate || '',
      venue: (g.venue || {}).name || '',
      description: g.description || '',
      series: g.seriesDescription || '',
      double_header: g.doubleHeader || '',
      state: status.detailedState || '',
      abstract_state: status.abstractGameState || '',
      coded_state: status.codedGameState || '',
      status_code: status.statusCode || '',
      away_name: awayTeam.name || '',
      away_abbr: awayTeam.abbreviation || '',
      away_short: awayTeam.shortName || awayTeam.teamName || '',
      away_record: away.leagueRecord ? `${away.leagueRecord.wins ?? ''}-${away.leagueRecord.losses ?? ''}` : '',
      home_name: homeTeam.name || '',
      home_abbr: homeTeam.abbreviation || '',
      home_short: homeTeam.shortName || homeTeam.teamName || '',
      home_record: home.leagueRecord ? `${home.leagueRecord.wins ?? ''}-${home.leagueRecord.losses ?? ''}` : '',
      away_score: away.score ?? null,
      home_score: home.score ?? null,
      away_probable: (away.probablePitcher || {}).fullName || '',
      home_probable: (home.probablePitcher || {}).fullName || '',
      away_hits: ((lsTeams.away || {}).hits) ?? null,
      home_hits: ((lsTeams.home || {}).hits) ?? null,
      away_errors: ((lsTeams.away || {}).errors) ?? null,
      home_errors: ((lsTeams.home || {}).errors) ?? null,
      away_lob: ((lsTeams.away || {}).leftOnBase) ?? null,
      home_lob: ((lsTeams.home || {}).leftOnBase) ?? null,
      inning: linescore.currentInning ?? null,
      inning_ordinal: linescore.currentInningOrdinal || '',
      inning_state: linescore.inningState || linescore.inningHalf || '',
      is_top: linescore.isTopInning === undefined ? null : Boolean(linescore.isTopInning),
      balls: linescore.balls ?? null,
      strikes: linescore.strikes ?? null,
      outs: linescore.outs ?? null,
      current_batter: (offense.batter || {}).fullName || '',
      current_pitcher: (defense.pitcher || {}).fullName || '',
      on_first: (offense.onFirst || {}).fullName || '',
      on_second: (offense.onSecond || {}).fullName || '',
      on_third: (offense.onThird || {}).fullName || '',
      decisions: g.decisions || null,
      review_has_challenges: review.hasChallenges ?? null,
      review_away_used: ((review.away || {}).used) ?? null,
      review_away_remaining: ((review.away || {}).remaining) ?? null,
      review_home_used: ((review.home || {}).used) ?? null,
      review_home_remaining: ((review.home || {}).remaining) ?? null,
      winner: ((g.decisions || {}).winner || {}).fullName || '',
      loser: ((g.decisions || {}).loser || {}).fullName || '',
      save: ((g.decisions || {}).save || {}).fullName || '',
    };
  }

  function normalizeSchedule(json, date) {
    const games = (json && json.dates || []).flatMap(d => d.games || []);
    return {
      date,
      source: scheduleUrl(date),
      games: games.map(normalizeScheduleGame),
      failures: [],
    };
  }

  async function fetchSchedule(date) {
    const url = scheduleUrl(date);
    const json = await fetchJSON(url, SCHEDULE_TIMEOUT_MS);
    const normalized = normalizeSchedule(json, date);
    normalized.source = url;
    return normalized;
  }

  const SLATE_LOOKBACK_DAYS = 21;

  function scheduleRangeUrl(startDate, endDate) {
    return `${MLB}/api/v1/schedule?sportId=1&startDate=${startDate}&endDate=${endDate}` +
      `&gameType=R,F,D,L,W&fields=dates,date,totalGames,totalItems`;
  }

  async function fetchScheduleMeta(startDate, endDate) {
    const url = scheduleRangeUrl(startDate, endDate);
    const json = await fetchJSON(url, SCHEDULE_TIMEOUT_MS);
    return ((json && json.dates) || []).map(d => ({
      date: d.date,
      total_games: d.totalGames ?? (d.games || []).length,
    }));
  }

  /* If the requested date has no games, resolve to the most recent date that does — in two small
     requests: a light range query to find the date, then the normal hydrated slate for it. The
     callers show the fallback in words; the pages never present another date's games silently. */
  async function resolveSlate(requestedDate, opts = {}) {
    const schedule = await fetchSchedule(requestedDate);
    if (schedule.games.length || opts.allowFallback === false) {
      return { schedule, date: requestedDate, requested_date: requestedDate, fell_back: false, note: '' };
    }
    const lookback = opts.lookbackDays ?? SLATE_LOOKBACK_DAYS;
    let candidate = null;
    try {
      const days = await fetchScheduleMeta(addDays(requestedDate, -lookback), addDays(requestedDate, -1));
      candidate = days.filter(d => d.total_games > 0 && d.date < requestedDate)
        .sort((a, b) => (a.date < b.date ? 1 : -1))[0] || null;
    } catch (error) {
      return { schedule, date: requestedDate, requested_date: requestedDate, fell_back: false, note: '' };
    }
    if (!candidate) {
      return { schedule, date: requestedDate, requested_date: requestedDate, fell_back: false, note: '' };
    }
    const fallback = await fetchSchedule(candidate.date);
    if (!fallback.games.length) {
      return { schedule, date: requestedDate, requested_date: requestedDate, fell_back: false, note: '' };
    }
    return {
      schedule: fallback, date: candidate.date, requested_date: requestedDate, fell_back: true,
      note: `No games were scheduled on ${requestedDate}; showing the most recent slate with games, ${candidate.date}.`,
    };
  }

  /* ------------------------------------------------------------- feed cache */

  function createFeedCache() {
    return new Map();
  }

  /* A Final game is only re-polled while it can plausibly have just ended: a
     regular game runs about three hours, so a game whose scheduled start is
     more than five hours old is fetched once and then left alone. Without this
     rule, opening a date from last week would re-fetch every finished game
     forever inside a rescan window that was created by the first page view. */
  const FINAL_RESCAN_MAX_GAME_AGE_MS = 5 * 60 * 60 * 1000;

  function shouldFetchFeed(game, cached, nowMs) {
    const state = game.abstract_state;
    if (state === 'Live') return true;
    if (!cached) return true;
    if (state !== 'Final') return false;
    if (cached.state !== 'Final') return true;
    const startedMs = Date.parse(game.game_date || '');
    const plausiblyJustEnded = Number.isFinite(startedMs) &&
      nowMs - startedMs < FINAL_RESCAN_MAX_GAME_AGE_MS;
    if (!plausiblyJustEnded) return false;
    return Number.isFinite(cached.firstFinalAt) &&
      nowMs - cached.firstFinalAt < FINAL_RESCAN_WINDOW_MS &&
      nowMs - (cached.lastFetchedMs || 0) >= FINAL_RESCAN_POLL_MS;
  }

  async function mapLimit(items, limit, fn) {
    let next = 0;
    const results = new Array(items.length);
    await Promise.all(Array.from({ length: Math.min(limit, Math.max(items.length, 1)) }, async () => {
      while (next < items.length) {
        const i = next++;
        try { results[i] = { ok: true, value: await fn(items[i]) }; }
        catch (error) { results[i] = { ok: false, error }; }
      }
    }));
    return results;
  }

  /**
   * Fetch the projected feed for every game that needs one. If a projected
   * feed parses but carries no plays while the schedule says the game is Live
   * or Final, retry once without the projection and flag the event — a
   * projection that silently stopped matching the API must be visible, not
   * rendered as "this game has no plays".
   */
  async function refreshFeeds(games, cache, opts = {}) {
    const nowMs = opts.nowMs ?? Date.now();
    const toFetch = games.filter(g => shouldFetchFeed(g, cache.get(g.game_pk), nowMs));
    const failures = [];
    const notes = [];
    const fetched = await mapLimit(toFetch, opts.limit ?? MAX_CONCURRENT_FEEDS, async game => {
      const url = feedUrl(game.game_pk, true);
      let feed = await fetchJSON(url, FEED_TIMEOUT_MS);
      let usedProjection = true;
      const plays = (((feed || {}).liveData || {}).plays || {}).allPlays || [];
      if (!plays.length && (game.abstract_state === 'Live' || game.abstract_state === 'Final')) {
        const fullUrl = feedUrl(game.game_pk, false);
        feed = await fetchJSON(fullUrl, FEED_TIMEOUT_MS);
        usedProjection = false;
        notes.push(`game ${game.game_pk}: the fields projection returned no plays for a ${game.abstract_state} game; ` +
          'the unprojected feed was used instead and the projection should be re-verified.');
      }
      return { game, feed, url: usedProjection ? url : feedUrl(game.game_pk, false), usedProjection };
    });
    fetched.forEach((result, i) => {
      const game = toFetch[i];
      if (!result.ok) {
        failures.push(`game ${game ? game.game_pk : '?'}: ${result.error && result.error.message ? result.error.message : result.error}`);
        return;
      }
      const { feed, url, usedProjection } = result.value;
      const previous = cache.get(game.game_pk);
      const firstFinalAt = game.abstract_state === 'Final'
        ? (previous && previous.state === 'Final' ? previous.firstFinalAt || nowMs : nowMs)
        : null;
      cache.set(game.game_pk, {
        feed, url, state: game.abstract_state, usedProjection,
        firstFinalAt, lastFetchedMs: Date.now(),
        fetchedAt: new Date().toISOString(),
      });
    });
    return { requested: toFetch.length, failures, notes };
  }

  /* ------------------------------------------------------------ per-game view */

  function teamNameForSide(game, side) {
    return side === 'away' ? game.away_name : side === 'home' ? game.home_name : '';
  }

  function teamAbbrForSide(game, side) {
    return side === 'away' ? game.away_abbr : side === 'home' ? game.home_abbr : '';
  }

  function currentPlayOf(feed) {
    const plays = ((((feed || {}).liveData || {}).plays || {}).allPlays) || [];
    for (let i = plays.length - 1; i >= 0; i -= 1) {
      const about = plays[i].about || {};
      if (about.isComplete === false) return plays[i];
    }
    const current = ((feed || {}).liveData || {}).plays && ((feed || {}).liveData || {}).plays.currentPlay;
    return current && current.about && current.about.isComplete === false ? current : null;
  }

  function reviewInfoOf(feed) {
    const gd = ((feed || {}).gameData) || {};
    return {
      review: gd.review || null,
      abs: gd.absChallenges || null,
      teamIds: {
        away: (((gd.teams || {}).away || {}).id) ?? null,
        home: (((gd.teams || {}).home || {}).id) ?? null,
      },
    };
  }

  /** First-seen observation order, not an official time (the payload has none). */
  function playOrderKey(row) {
    return Number.isFinite(Number(row.at_bat)) ? Number(row.at_bat) : -1;
  }

  function errorPercentileOf(percentileFn, score100) {
    if (typeof percentileFn !== 'function' || !Number.isFinite(Number(score100))) return null;
    return percentileFn(Number(score100) / 100);
  }

  /**
   * One play's model row enriched with everything the feed renderers need.
   * `row` comes straight from scoreLiveFeed(); nothing here changes a call.
   */
  function enrichRow(row, plays, game) {
    const source = plays.get(row.at_bat) || null;
    const runners = (source && source.runners) || [];
    const review = (source && source.reviewDetails) || {};
    return {
      ...row,
      batting_side: source ? teamSideOfPlay(source) : '',
      batting_team_abbr: source ? teamAbbrForSide(game, teamSideOfPlay(source)) : '',
      batting_team_name: source ? teamNameForSide(game, teamSideOfPlay(source)) : '',
      bases_detail: basesFromRunners(runners),
      review_reason_code: review.reviewType || '',
      review_overturned_present: Object.prototype.hasOwnProperty.call(review, 'isOverturned'),
      review_in_progress: Object.prototype.hasOwnProperty.call(review, 'inProgress') ? review.inProgress : null,
    };
  }

  function summarizeTeamRows(rows) {
    const played = rows.filter(r => r.status === 'scored');
    return {
      batted_balls: played.length,
      predictions_available: rows.filter(r => r.prediction_available === true).length,
      feed_errors: played.filter(r => r.official_call === 'error').length,
      pending: rows.filter(r => r.official_scoring_pending === true).length,
      no_event_type: rows.filter(r => r.status === 'no_event_type_yet').length,
      no_vector: rows.filter(r => r.status === 'no_vector').length,
      reviewed: rows.filter(r => r.reviewed === 1 || r.reviewed === true).length,
      overturned: rows.filter(r => r.review_overturned === true || r.review_overturned === 'true').length,
    };
  }

  function topRiskRow(rows) {
    return rows.filter(r => Number.isFinite(Number(r.score_100)) && r.prediction_available === true)
      .sort((a, b) => Number(b.score_100) - Number(a.score_100))[0] || null;
  }

  /**
   * The full per-game record both pages render: schedule facts + model rows +
   * counters. `ledger` is the ScoringFeed date state, used only to count
   * observed scoring changes per game.
   */
  function playsByAtBat(feed) {
    const map = new Map();
    (((((feed || {}).liveData || {}).plays || {}).allPlays) || []).forEach(play => {
      const about = (play && play.about) || {};
      if (about.atBatIndex !== undefined && about.atBatIndex !== null) map.set(about.atBatIndex, play);
    });
    return map;
  }

  function buildGameRecord(game, cacheEntry, model, ledger) {
    const feed = cacheEntry ? cacheEntry.feed : null;
    const plays = playsByAtBat(feed);
    const rows = feed ? scoreLiveFeed(feed, {
      gamePk: game.game_pk,
      official_feed_url: cacheEntry.url || feedUrl(game.game_pk, true),
      matchup: `${game.away_abbr || game.away_name} @ ${game.home_abbr || game.home_name}`,
    }, model).map(row => enrichRow(row, plays, game)) : [];
    const summary = summarizeTeamRows(rows);
    const info = reviewInfoOf(feed);
    const current = currentPlayOf(feed);
    const currentAbout = (current && current.about) || {};
    const currentMatchup = (current && current.matchup) || {};
    const currentCount = (current && current.count) || {};
    const currentSide = teamSideOfPlay(current);
    const changes = ((ledger && ledger.changes) || []).filter(c => String(c.game_pk) === String(game.game_pk));
    const pendings = ((ledger && ledger.pending) || []).filter(p => String(p.game_pk) === String(game.game_pk));
    const top = topRiskRow(rows);
    return {
      game,
      game_pk: game.game_pk,
      matchup: `${game.away_abbr || game.away_name} @ ${game.home_abbr || game.home_name}`,
      feed_url: cacheEntry ? cacheEntry.url : feedUrl(game.game_pk, true),
      feed_available: Boolean(feed),
      model_rows: rows,
      summary,
      ledger: { changes, pending: pendings },
      review_info: info,
      live: current ? {
        at_bat: currentAbout.atBatIndex ?? null,
        inning: currentAbout.inning ?? null,
        half: currentAbout.halfInning || '',
        batter: ((currentMatchup.batter || {}).fullName) || '',
        pitcher: ((currentMatchup.pitcher || {}).fullName) || '',
        batting_team: teamNameForSide(game, currentSide),
        balls: currentCount.balls ?? null,
        strikes: currentCount.strikes ?? null,
        outs: currentCount.outs ?? null,
        runners: basesFromRunners(current.runners || []),
        runners_detail: {
          first: (current.offense && current.offense.onFirst ? current.offense.onFirst.fullName : '') || '',
          second: (current.offense && current.offense.onSecond ? current.offense.onSecond.fullName : '') || '',
          third: (current.offense && current.offense.onThird ? current.offense.onThird.fullName : '') || '',
        },
        last_pitch: lastPitchLine(current),
      } : null,
      top_risk: top ? {
        at_bat: top.at_bat, batter: top.batter, pitcher: top.pitcher,
        description: top.description, score_100: top.score_100, event_top_pick: top.event_top_pick,
        event_top_prob: top.event_top_prob, official_call: top.official_call,
      } : null,
      change_count: changes.length,
      pending_count: pendings.length,
    };
  }

  function lastPitchLine(play) {
    const events = (play && play.playEvents) || [];
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const d = (events[i] && events[i].details) || {};
      if (d.description) return d.description;
    }
    return '';
  }

  /* ------------------------------------------------------------- review rows */

  /** code -> label from the league's own /api/v1/gameStatus registry. */
  async function fetchReviewRegistry() {
    const rows = await fetchJSON(`${MLB}/api/v1/gameStatus`, 15000);
    const out = {};
    (Array.isArray(rows) ? rows : []).forEach(row => {
      const code = row && row.statusCode;
      if (!code) return;
      out[code] = {
        detailed_state: row.detailedState || '',
        reason: row.reason || '',
        coded_state: row.codedGameState || '',
        abstract_state: row.abstractGameState || '',
      };
    });
    return out;
  }

  function reviewKind(code) {
    const c = String(code || '').toUpperCase();
    if (c === 'MJ' || c === 'NJ') return 'abs';
    if (c.charAt(0) === 'M') return 'manager';
    if (c.charAt(0) === 'N') return 'crew_chief';
    if (c === 'IH') return 'review';
    return 'other';
  }

  function reviewRegistryLabel(registry, code) {
    const row = registry && registry[code];
    if (!row) return '';
    return row.detailed_state || (row.reason ? `${row.reason}` : '');
  }

  /**
   * Review events taken only from the official payload. A play-level
   * `reviewDetails` becomes one row; a pitch-level `reviewDetails` (ABS) gets
   * its own row keyed by playId. Fields that are absent stay absent.
   */
  function extractReviewEvents(game, feed, registry, teamNames) {
    const plays = ((((feed || {}).liveData || {}).plays || {}).allPlays) || [];
    const events = [];
    plays.forEach(play => {
      const about = play.about || {};
      const result = play.result || {};
      const matchup = play.matchup || {};
      const side = teamSideOfPlay(play);
      const push = (details, keySuffix, pitchDescription) => {
        const code = details.reviewType || '';
        const kind = reviewKind(code);
        const challengeTeamId = details.challengeTeamId ?? null;
        const challenger = challengeTeamId !== null && teamNames
          ? (teamNames[challengeTeamId] || '') : '';
        events.push({
          key: `${game.game_pk}:${keySuffix}`,
          kind: kind === 'abs' ? 'abs' : 'review',
          review_kind: kind,
          game_pk: game.game_pk,
          game,
          matchup: `${game.away_abbr || game.away_name} @ ${game.home_abbr || game.home_name}`,
          inning: about.inning ?? null,
          half: about.halfInning || '',
          at_bat: about.atBatIndex ?? null,
          play_id: play.playId || '',
          batter: (matchup.batter || {}).fullName || '',
          pitcher: (matchup.pitcher || {}).fullName || '',
          description: result.description || pitchDescription || '',
          official_event_type: result.eventType || '',
          review: {
            code,
            label: reviewRegistryLabel(registry, code),
            kind,
            overturned: Object.prototype.hasOwnProperty.call(details, 'isOverturned') ? details.isOverturned : null,
            in_progress: Object.prototype.hasOwnProperty.call(details, 'inProgress') ? details.inProgress : null,
            challenge_team_id: challengeTeamId,
            challenger,
            batting_side: side,
            batting_team_abbr: teamAbbrForSide(game, side),
            batting_team_name: teamNameForSide(game, side),
          },
          scores: { away: result.awayScore ?? null, home: result.homeScore ?? null },
          official_feed_url: feedUrl(game.game_pk, true),
          savant_url: play.playId ? `https://baseballsavant.mlb.com/sporty-videos?playId=${encodeURIComponent(play.playId)}` : '',
        });
      };
      if (play.reviewDetails && typeof play.reviewDetails === 'object') {
        push(play.reviewDetails, `play-${about.atBatIndex ?? '?'}-main`, '');
      }
      (play.playEvents || []).forEach((event, index) => {
        const details = (event && event.reviewDetails) || null;
        if (!details || typeof details !== 'object') return;
        push(details, `play-${about.atBatIndex ?? '?'}-ev-${index}`,
          ((event && event.details) || {}).description || '');
      });
    });
    return events;
  }

  /* ---------------------------------------------------------- live feed rows */

  function scoresOf(row) {
    return { away: row.away_score ?? null, home: row.home_score ?? null };
  }

  function pendingFacet(row) {
    if (!row) return null;
    return {
      kind: row.scoring_pending_kind || 'description_only',
      codes: String(row.scoring_pending_codes || '').split(',').filter(Boolean),
      text: row.scoring_pending_text || '',
    };
  }

  /**
   * Build the deduped, chronological event list the all-games feed renders.
   *
   * Precedence per at-bat: an observed scoring change (from the browser
   * ledger) beats a live pending marker, which beats a completed batted ball.
   * A review on the same play is attached to whichever row exists, so a
   * reviewed batted ball keeps its model distribution. Events from the ledger
   * carry `observed_at` from the ledger; play events carry the caller-supplied
   * first-seen stamp.
   */
  function buildEvents(input) {
    const games = input.games || [];
    const model = input.model;
    const ledger = input.ledger || { pending: [], changes: [] };
    const registry = input.registry || {};
    const seen = input.seen || {};
    const nowIso = input.nowIso || new Date().toISOString();
    const percentileFn = input.percentileFn || null;
    const events = [];
    const stats = { games: games.length, feeds: 0, batted_balls: 0, predictions: 0,
      pending: 0, changes: 0, reviews: 0, abs: 0, overturned: 0, no_call: 0, no_vector: 0 };

    games.forEach(record => {
      const game = record.game || record;
      if (!record.feed_available && !record.feed) return;
      stats.feeds += 1;
      const rows = record.model_rows || [];
      const reviews = extractReviewEvents(game, record.feed || (record.cache && record.cache.feed),
        registry, record.team_names_by_id || null);
      const reviewByAtBat = new Map();
      const coveredReviewKeys = new Set();
      reviews.forEach(r => {
        if (r.at_bat !== null && r.at_bat !== undefined) {
          const list = reviewByAtBat.get(r.at_bat) || [];
          list.push(r);
          reviewByAtBat.set(r.at_bat, list);
        }
      });
      const ledgerChangeByKey = new Map();
      (ledger.changes || []).forEach(c => ledgerChangeByKey.set(String(c.key), c));
      const ledgerPendingByKey = new Map();
      (ledger.pending || []).forEach(p => ledgerPendingByKey.set(String(p.key), p));

      const base = {
        game_pk: game.game_pk,
        matchup: `${game.away_abbr || game.away_name} @ ${game.home_abbr || game.home_name}`,
        away_abbr: game.away_abbr, home_abbr: game.home_abbr,
        away_name: game.away_name, home_name: game.home_name,
        state: game.state, abstract_state: game.abstract_state,
        game_date: game.game_date,
        official_feed_url: record.feed_url || feedUrl(game.game_pk, true),
        savant_game_url: `https://baseballsavant.mlb.com/gamefeed?gamePk=${game.game_pk}`,
        gameday_url: `https://www.mlb.com/gameday/${game.game_pk}`,
      };

      rows.forEach(row => {
        const key = `${game.game_pk}:${row.at_bat !== null && row.at_bat !== undefined
          ? row.at_bat : (row.play_id || 'play')}`;
        const reviewFacet = (reviewByAtBat.get(row.at_bat) || [])[0] || null;
        if (reviewFacet) coveredReviewKeys.add(reviewFacet.key);
        const change = ledgerChangeByKey.get(key) || null;
        const ledgerPending = ledgerPendingByKey.get(key) || null;
        const kind = change ? 'change'
          : (row.official_scoring_pending || (ledgerPending && ledgerPending.status !== 'resolved'))
            ? 'pending' : (row.status === 'scored' ? 'batted_ball'
              : row.status === 'no_event_type_yet' ? 'no_call' : 'no_vector');
        const eventProbs = {};
        Object.keys(row).forEach(k => {
          if (k.startsWith('event_p_') && Number.isFinite(Number(row[k]))) eventProbs[k.slice(8)] = Number(row[k]);
        });
        const event = {
          ...base,
          key, kind,
          scores: scoresOf(row),
          facet: change ? 'change' : kind,
          at_bat: row.at_bat ?? null,
          inning: row.inning ?? null,
          half: row.half || '',
          batter: row.batter || '',
          pitcher: row.pitcher || '',
          description: row.description || row.scoring_pending_text || '',
          official_event_type: row.event_type || '',
          official_call: row.official_call || '',
          status: row.status || '',
          launch_speed: row.launch_speed ?? null,
          launch_angle: row.launch_angle ?? null,
          distance: row.distance ?? null,
          trajectory: row.trajectory || '',
          hardness: row.hardness || '',
          bases: row.runners_on || '-',
          outs_before: row.outs_before ?? null,
          run_scored: row.run_scored === 1 || row.run_scored === true,
          rbi_official: row.rbi_official ?? null,
          prediction_available: row.prediction_available === true,
          prediction_unavailable_reason: row.prediction_unavailable_reason || '',
          score_100: row.score_100 ?? null,
          error_percentile: errorPercentileOf(percentileFn, row.score_100),
          top_pick: row.top_pick || '',
          top_prob: row.top_prob ?? null,
          p_hit: row.p_hit ?? null,
          p_error_macro: row.p_error_macro ?? null,
          p_fielders_choice: row.p_fielders_choice ?? null,
          p_out: row.p_out ?? null,
          event_top_pick: row.event_top_pick || '',
          event_top_prob: row.event_top_prob ?? null,
          event_probs: eventProbs,
          pending: row.official_scoring_pending ? pendingFacet(row) : null,
          ledger_pending: ledgerPending || null,
          change,
          review: reviewFacet ? reviewFacet.review : null,
          review_key: reviewFacet ? reviewFacet.key : '',
          baserunning_review: (reviewByAtBat.get(row.at_bat) || []).slice(1).map(r => r.review),
          rbi_if_error: row.rbi_if_error || '',
          rbi_if_hit: row.rbi_if_hit || '',
          rbi_if_fc: row.rbi_if_fc || '',
          savant_url: row.savant_url || '',
          observed_at: seen[key] || nowIso,
        };
        if (event.kind === 'change') stats.changes += 1;
        if (event.kind === 'pending') stats.pending += 1;
        if (event.kind === 'batted_ball') stats.batted_balls += 1;
        if (event.kind === 'no_call') stats.no_call += 1;
        if (event.kind === 'no_vector') stats.no_vector += 1;
        if (event.prediction_available) stats.predictions += 1;
        if (event.review && (event.review.overturned === true || event.review.overturned === 'true')) stats.overturned += 1;
        events.push(event);
      });

      // Reviews without a matching model row (pitch-level ABS and non-contact
      // reviews) become their own feed rows.
      reviews.forEach(reviewEvent => {
        if (reviewEvent.kind === 'abs') stats.abs += 1; else stats.reviews += 1;
        if (coveredReviewKeys.has(reviewEvent.key)) return;
        if (reviewEvent.review.overturned === true || reviewEvent.review.overturned === 'true') {
          if (reviewEvent.kind !== 'abs') stats.overturned += 1;
        }
        events.push({
          ...base,
          ...reviewEvent,
          kind: reviewEvent.kind,
          facet: reviewEvent.kind,
          observed_at: seen[reviewEvent.key] || nowIso,
        });
      });

      // The in-progress plate appearance, when the feed has one.
      const current = currentPlayOf(record.feed || (record.cache && record.cache.feed));
      if (current && game.abstract_state === 'Live') {
        const about = current.about || {};
        const matchup = current.matchup || {};
        const count = current.count || {};
        const key = `${game.game_pk}:current`;
        events.push({
          ...base,
          key, kind: 'live_ab', facet: 'live_ab',
          at_bat: about.atBatIndex ?? null,
          inning: about.inning ?? null,
          half: about.halfInning || '',
          batter: (matchup.batter || {}).fullName || '',
          pitcher: (matchup.pitcher || {}).fullName || '',
          description: lastPitchLine(current),
          count: { balls: count.balls ?? null, strikes: count.strikes ?? null, outs: count.outs ?? null },
          bases: basesFromRunners(current.runners || []),
          observed_at: seen[key] || nowIso,
        });
      }
    });
    return { events, stats };
  }

  /** Newest observation first; ties fall back to how deep the play is in its game. */
  function sortEvents(events) {
    return [...events].sort((a, b) => {
      const ao = a.observed_at || '', bo = b.observed_at || '';
      if (ao !== bo) return ao < bo ? 1 : -1;
      return (Number(b.at_bat) || -1) - (Number(a.at_bat) || -1);
    });
  }

  function filterEvents(events, tab, gamePk, query) {
    let rows = events;
    if (tab === 'batted_ball') rows = rows.filter(e => e.kind === 'batted_ball');
    else if (tab === 'pending') rows = rows.filter(e => e.kind === 'pending' || e.ledger_pending);
    else if (tab === 'change') rows = rows.filter(e => e.kind === 'change');
    else if (tab === 'review') rows = rows.filter(e => (e.review && e.review.kind !== 'abs') || e.kind === 'review');
    else if (tab === 'abs') rows = rows.filter(e => (e.review && e.review.kind === 'abs') || e.kind === 'abs');
    else if (tab === 'live') rows = rows.filter(e => e.kind === 'live_ab');
    if (gamePk) rows = rows.filter(e => String(e.game_pk) === String(gamePk));
    if (query) {
      const q = String(query).toLowerCase();
      rows = rows.filter(e => [e.batter, e.pitcher, e.description, e.official_event_type, e.matchup]
        .some(v => String(v || '').toLowerCase().includes(q)));
    }
    return rows;
  }

  /* -------------------------------------------------- first-seen observation log */

  function readSeen(storage) {
    try {
      const parsed = JSON.parse(storage.getItem(SEEN_KEY));
      if (!parsed || parsed.version !== 1 || typeof parsed.dates !== 'object' || !parsed.dates) {
        return { version: 1, dates: {} };
      }
      return parsed;
    } catch (error) {
      return { version: 1, dates: {} };
    }
  }

  function stampSeen(storage, date, events, nowIso) {
    const state = readSeen(storage);
    const day = state.dates[date] && typeof state.dates[date] === 'object' ? state.dates[date] : {};
    events.forEach(event => {
      if (!event.key) return;
      if (!day[event.key]) day[event.key] = nowIso;
    });
    const keys = Object.keys(day);
    if (keys.length > SEEN_PER_DATE_CAP) {
      keys.sort((a, b) => String(day[a]).localeCompare(String(day[b])))
        .slice(0, keys.length - SEEN_PER_DATE_CAP)
        .forEach(key => delete day[key]);
    }
    state.dates[date] = day;
    Object.keys(state.dates).sort().reverse().slice(SEEN_DATES_KEPT)
      .forEach(oldDate => delete state.dates[oldDate]);
    try {
      storage.setItem(SEEN_KEY, JSON.stringify(state));
    } catch (error) {
      /* Storage full or blocked: first-seen stamps are best-effort only; the
         rows still render with "just now". */
    }
    return day;
  }

  /* ------------------------------------------------------------- day pipeline */

  /**
   * One poll of the whole day: schedule -> feeds -> model rows -> ledger ->
   * events. Returns everything the pages render, plus the failures that
   * happened (never smoothed over).
   */
  async function pollDay(opts) {
    const requestedDate = opts.date;
    const model = opts.model;
    const cache = opts.cache || createFeedCache();
    const storage = opts.storage || null;
    const failures = [];
    let schedule = opts.schedule || null;
    let fellBack = false;
    let slateNote = '';
    if (!schedule) {
      try {
        const resolved = await resolveSlate(requestedDate, {
          allowFallback: opts.allowFallback, lookbackDays: opts.lookbackDays,
        });
        schedule = resolved.schedule;
        fellBack = resolved.fell_back;
        slateNote = resolved.note;
      } catch (error) {
        failures.push(`schedule: ${error && error.message ? error.message : error}`);
      }
    }
    const date = (schedule && schedule.date) || requestedDate;
    const games = schedule ? schedule.games : [];
    const feedResult = await refreshFeeds(games, cache, { limit: opts.limit, nowMs: opts.nowMs });
    failures.push(...feedResult.failures);
    const notes = [...feedResult.notes];
    if (slateNote) notes.push(slateNote);

    const records = games.map(game => {
      const entry = cache.get(game.game_pk) || null;
      const teamNamesById = entry ? {
        [((((entry.feed || {}).gameData || {}).teams || {}).away || {}).id]: game.away_abbr || game.away_name,
        [((((entry.feed || {}).gameData || {}).teams || {}).home || {}).id]: game.home_abbr || game.home_name,
      } : null;
      const record = buildGameRecord(game, entry, model, opts.ledger || null);
      record.feed = entry ? entry.feed : null;
      record.cache = entry;
      record.team_names_by_id = teamNamesById;
      return record;
    });

    let ledger = opts.ledger || { pending: [], changes: [] };
    if (storage && typeof ScoringFeed !== 'undefined') {
      const observations = records.flatMap(r => (r.feed ? ScoringFeed.extractObservations(r.feed, {
        gamePk: r.game_pk, pk: r.game_pk, matchup: r.matchup, official_feed_url: r.feed_url,
      }) : []));
      const modelRows = records.flatMap(r => r.model_rows || []);
      try {
        const update = ScoringFeed.updateState(storage, date, observations, modelRows,
          opts.nowIso || new Date().toISOString());
        ledger = update.dates[date] || ledger;
        failures.push(...(update.flags || []));
      } catch (error) {
        failures.push(`scoring ledger: ${error && error.message ? error.message : error}`);
      }
      records.forEach(record => {
        record.ledger = {
          changes: (ledger.changes || []).filter(c => String(c.game_pk) === String(record.game_pk)),
          pending: (ledger.pending || []).filter(p => String(p.game_pk) === String(record.game_pk)),
        };
        record.change_count = record.ledger.changes.length;
        record.pending_count = record.ledger.pending.length;
      });
    }

    const nowIso = opts.nowIso || new Date().toISOString();
    const built = buildEvents({
      games: records, model, ledger, registry: opts.registry || {},
      seen: opts.seen || {}, nowIso, percentileFn: opts.percentileFn,
    });
    const events = sortEvents(built.events);
    const seenStamps = storage ? stampSeen(storage, date, events, nowIso) : {};
    events.forEach(event => { event.observed_at = seenStamps[event.key] || event.observed_at; });
    const sorted = sortEvents(events);
    return {
      date, requested_date: requestedDate, fell_back: fellBack,
      generated_utc: nowIso, source: scheduleUrl(date),
      failures,
      games: records, events: sorted, stats: built.stats,
      feed_failures: feedResult.failures, notes,
      cache, ledger,
    };
  }

  return Object.freeze({
    MLB, FEED_FIELDS,
    LIVE_POLL_MS, IDLE_POLL_MS, FINAL_RESCAN_WINDOW_MS, FINAL_RESCAN_POLL_MS, SEEN_KEY,
    easternDateString, addDays, clockLabel, agoLabel, halfLabel, basesFromRunners,
    MODEL_CLASS_LABELS, FEED_EVENT_LABELS, modelClassLabel, feedEventLabel, ordinal,
    fetchJSON, scheduleUrl, scheduleRangeUrl, feedUrl, fetchSchedule, fetchScheduleMeta, resolveSlate,
    normalizeScheduleGame, normalizeSchedule, SLATE_LOOKBACK_DAYS,
    createFeedCache, refreshFeeds, shouldFetchFeed, mapLimit,
    fetchReviewRegistry, reviewKind, reviewRegistryLabel, extractReviewEvents,
    buildGameRecord, buildEvents, sortEvents, filterEvents, currentPlayOf, topRiskRow,
    readSeen, stampSeen, pollDay,
  });
})();
