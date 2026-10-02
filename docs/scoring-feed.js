/* Exact official-scorer pending detection and browser-local observation ledger.
   Marker vocabulary is the MLB StatsAPI /api/v1/eventTypes registry. The ledger records only
   payload values observed on polls; it never infers a missing ruling or why a classification changed. */
'use strict';

var ScoringFeed = (() => {
  const PENDING_CODES = new Set(['os_ruling_pending_primary', 'os_ruling_pending_prior']);
  const PENDING_TEXT = 'Official Scorer Ruling Pending';
  const STORAGE_KEY = 'errorslive.scoring-feed.v1';
  const MAX_DATES = 7;
  const MAX_EVENTS = 500;
  const MAX_PLAY_SNAPSHOTS = 2000;

  function warn(message) {
    if (typeof console !== 'undefined' && typeof console.warn === 'function') console.warn(message);
  }

  function pendingValues(candidate) {
    if (!candidate || typeof candidate !== 'object') return [];
    const details = candidate.details && typeof candidate.details === 'object' ? candidate.details : {};
    const result = candidate.result && typeof candidate.result === 'object' ? candidate.result : {};
    return [details.eventType, details.event, details.description,
      candidate.eventType, candidate.event, candidate.type,
      result.eventType, result.event, result.description];
  }

  function isOfficialScoringPendingEvent(candidate) {
    return pendingValues(candidate).some(value => typeof value === 'string' &&
      (PENDING_CODES.has(value) || value === PENDING_TEXT));
  }

  function findOfficialScoringPendingPlay(play) {
    if (!play || typeof play !== 'object') return null;
    const pendingEvents = [];
    const pendingCodes = [];
    const noteCodes = values => values.forEach(value => {
      if (typeof value === 'string' && PENDING_CODES.has(value) && !pendingCodes.includes(value))
        pendingCodes.push(value);
    });
    (Array.isArray(play.playEvents) ? play.playEvents : []).forEach(event => {
      if (!event || typeof event !== 'object') return;
      const details = event.details && typeof event.details === 'object' ? event.details : {};
      const values = [details.eventType, details.event, details.description,
        event.eventType, event.event, event.type];
      if (values.some(value => typeof value === 'string' &&
          (PENDING_CODES.has(value) || value === PENDING_TEXT))) {
        pendingEvents.push(event);
        noteCodes(values);
      }
    });
    const result = play.result && typeof play.result === 'object' ? play.result : {};
    const resultValues = [result.eventType, result.event, result.description];
    const atResult = resultValues.some(value => typeof value === 'string' &&
      (PENDING_CODES.has(value) || value === PENDING_TEXT));
    if (atResult) noteCodes(resultValues);
    if (!pendingEvents.length && !atResult) return null;
    return {
      pendingEvents, pendingCodes,
      primary: pendingCodes.includes('os_ruling_pending_primary'),
      prior: pendingCodes.includes('os_ruling_pending_prior'), atResult,
    };
  }

  function pendingKind(info) {
    if (!info) return '';
    if (info.primary && info.prior) return 'both';
    if (info.primary) return 'primary';
    if (info.prior) return 'prior';
    return 'description_only';
  }

  function playId(play) {
    const events = Array.isArray(play && play.playEvents) ? play.playEvents : [];
    for (let i = events.length - 1; i >= 0; i -= 1) {
      if (events[i] && events[i].playId) return String(events[i].playId);
    }
    return '';
  }

  function eventReference(event) {
    if (!event || typeof event !== 'object') return '';
    if (event.playId !== undefined && event.playId !== null && event.playId !== '')
      return `playId:${String(event.playId)}`;
    if (Number.isInteger(event.index)) return `index:${event.index}`;
    return '';
  }

  function extractObservations(feed, game = {}) {
    const plays = ((((feed || {}).liveData || {}).plays || {}).allPlays) || [];
    const gamePk = game.gamePk ?? game.pk ?? game.game_pk ?? '';
    const feedUrl = game.official_feed_url || game.url || '';
    return plays.filter(play => play && typeof play === 'object').map(play => {
      const result = play.result && typeof play.result === 'object' ? play.result : {};
      const about = play.about && typeof play.about === 'object' ? play.about : {};
      const matchup = play.matchup && typeof play.matchup === 'object' ? play.matchup : {};
      const batter = matchup.batter && typeof matchup.batter === 'object' ? matchup.batter : {};
      const pitcher = matchup.pitcher && typeof matchup.pitcher === 'object' ? matchup.pitcher : {};
      const pending = findOfficialScoringPendingPlay(play);
      const playEvents = Array.isArray(play.playEvents) ? play.playEvents : [];
      const pendingEventRefs = pending ? pending.pendingEvents.map(event => ({
        event_key: eventReference(event), play_id: event && event.playId != null ? String(event.playId) : '',
        index: event && Number.isInteger(event.index) ? event.index : null,
      })).filter(event => event.event_key) : [];
      const eventStates = playEvents.map(event => {
        const details = event && event.details && typeof event.details === 'object' ? event.details : {};
        return {
          event_key: eventReference(event),
          event_type: details.eventType || (event && event.eventType) || '',
          description: details.description || '',
          official_scoring_pending: isOfficialScoringPendingEvent(event),
        };
      }).filter(event => event.event_key);
      return {
        game_pk: String(gamePk), at_bat: about.atBatIndex ?? null,
        inning: about.inning ?? null, half: about.halfInning || '',
        event_type: result.eventType || '',
        description: result.description || result.event || '',
        official_scoring_pending: Boolean(pending),
        pending_codes: pending ? pending.pendingCodes : [],
        pending_kind: pendingKind(pending),
        pending_text: pending ? PENDING_TEXT : '',
        pending_event_refs: pendingEventRefs, event_states: eventStates,
        batter: batter.fullName || '', pitcher: pitcher.fullName || '',
        matchup: game.matchup || '',
        away_score: result.awayScore ?? null, home_score: result.homeScore ?? null,
        play_id: playId(play), official_feed_url: feedUrl,
      };
    });
  }

  function eventKey(item) {
    if (!item || item.game_pk == null || item.game_pk === '') return '';
    if (item.at_bat !== null && item.at_bat !== undefined && item.at_bat !== '')
      return `${item.game_pk}:${item.at_bat}`;
    return item.play_id ? `${item.game_pk}:play:${item.play_id}` : '';
  }

  function numberOrNull(value) {
    const n = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(n) ? n : null;
  }

  function predictionSnapshot(row) {
    if (!row || row.prediction_available !== true) return null;
    const out = {};
    for (const key of ['score_100', 'p_hit', 'p_error', 'p_error_binary', 'p_error_macro',
      'p_fielders_choice', 'p_out',
      'top_pick', 'top_prob', 'event_top_pick', 'event_top_prob', 'launch_speed',
      'launch_angle', 'distance', 'trajectory', 'hardness', 'runners_on', 'outs_before']) {
      if (row[key] !== undefined && row[key] !== null) out[key] = row[key];
    }
    for (const [key, value] of Object.entries(row)) {
      if (key.startsWith('event_p_') && Number.isFinite(Number(value))) out[key] = Number(value);
    }
    return out;
  }

  function emptyDateState() {
    return { pending: [], changes: [], plays: {} };
  }

  function validString(value) { return typeof value === 'string'; }

  function sanitizeDateState(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return emptyDateState();
    const state = emptyDateState();
    if (Array.isArray(raw.pending)) {
      state.pending = raw.pending.filter(row => row && typeof row === 'object' &&
        validString(row.key) && validString(row.game_pk) &&
        ['pending', 'awaiting_result', 'resolved'].includes(row.status)).slice(-MAX_EVENTS);
    }
    if (Array.isArray(raw.changes)) {
      state.changes = raw.changes.filter(row => row && typeof row === 'object' &&
        validString(row.key) && validString(row.from_event_type) &&
        validString(row.to_event_type)).slice(-MAX_EVENTS);
    }
    if (raw.plays && typeof raw.plays === 'object' && !Array.isArray(raw.plays)) {
      for (const [key, value] of Object.entries(raw.plays).slice(-MAX_PLAY_SNAPSHOTS)) {
        if (!value || typeof value !== 'object' || !validString(value.event_type)) continue;
        state.plays[key] = {
          event_type: value.event_type, description: validString(value.description) ? value.description : '',
          first_seen_utc: validString(value.first_seen_utc) ? value.first_seen_utc : '',
          last_seen_utc: validString(value.last_seen_utc) ? value.last_seen_utc : '',
        };
      }
    }
    return state;
  }

  function mergeDateState(previous, observations, modelRows, observedUtc) {
    const state = sanitizeDateState(previous);
    const stamp = validString(observedUtc) && observedUtc ? observedUtc : new Date().toISOString();
    const pendingByKey = new Map(state.pending.map(row => [row.key, row]));
    const playSnapshots = state.plays;
    const rowsByKey = new Map();
    (Array.isArray(modelRows) ? modelRows : []).forEach(row => {
      const key = eventKey(row);
      if (key) rowsByKey.set(key, row);
    });
    const observedByKey = new Map();
    const flags = [];
    const recordClassification = (key, observation, currentType) => {
      if (!currentType) return;
      const prior = playSnapshots[key];
      if (prior && prior.event_type && prior.event_type !== currentType) {
        const id = `${key}:${prior.event_type}->${currentType}:${prior.last_seen_utc || prior.first_seen_utc}`;
        if (!state.changes.some(change => change.id === id)) {
          state.changes.push({
            id, key, game_pk: String(observation.game_pk), at_bat: observation.at_bat ?? null,
            play_id: observation.play_id || '', matchup: observation.matchup || '',
            inning: observation.inning ?? null, half: observation.half || '',
            from_event_type: prior.event_type, to_event_type: currentType,
            initial_description: prior.description || '', final_description: observation.description || '',
            first_observed_utc: prior.first_seen_utc || prior.last_seen_utc || '',
            changed_observed_utc: stamp,
            note: 'The official feed classification differed between two captures. The feed does not identify who changed it or why.',
            official_feed_url: observation.official_feed_url || '',
          });
        }
      }
      playSnapshots[key] = {
        event_type: currentType, description: observation.description || '',
        first_seen_utc: prior && prior.first_seen_utc ? prior.first_seen_utc : stamp,
        last_seen_utc: stamp,
      };
    };
    const captureResolvedComponents = (pendingRow, observation) => {
      const currentCodes = new Set(Array.isArray(observation.pending_codes) ? observation.pending_codes : []);
      // An exact description with no code still means pending; its scope is not safely identifiable.
      if (observation.official_scoring_pending === true && currentCodes.size === 0) return;
      const required = new Set(pendingRow.required_pending_codes || pendingRow.pending_codes || []);
      const resultType = validString(observation.event_type) ? observation.event_type : '';
      if (required.has('os_ruling_pending_primary') &&
          !currentCodes.has('os_ruling_pending_primary') && !pendingRow.resolved_primary_event_type &&
          resultType && !PENDING_CODES.has(resultType) && resultType !== PENDING_TEXT) {
        pendingRow.resolved_primary_event_type = resultType;
        pendingRow.resolved_primary_description = observation.description || '';
      }
      if (required.has('os_ruling_pending_prior') &&
          !currentCodes.has('os_ruling_pending_prior') && !pendingRow.resolved_prior_event_type) {
        const refs = Array.isArray(pendingRow.pending_event_refs) ? pendingRow.pending_event_refs : [];
        const eventStates = Array.isArray(observation.event_states) ? observation.event_states : [];
        for (const ref of refs) {
          if (!ref.event_key) continue;
          const event = eventStates.find(item => item && item.event_key === ref.event_key);
          const eventType = event && validString(event.event_type) ? event.event_type : '';
          if (event && event.official_scoring_pending !== true && eventType &&
              !PENDING_CODES.has(eventType) && eventType !== PENDING_TEXT) {
            pendingRow.resolved_prior_event_type = eventType;
            pendingRow.resolved_prior_description = event.description || '';
            break;
          }
        }
      }
    };

    (Array.isArray(observations) ? observations : []).forEach(observation => {
      const key = eventKey(observation);
      if (!key) {
        if (observation && observation.official_scoring_pending)
          flags.push('An official scorer pending marker had no atBatIndex or playId; it was displayed but not added to the persistent log.');
        return;
      }
      observedByKey.set(key, observation);
      const isPending = observation.official_scoring_pending === true;
      const currentType = validString(observation.event_type) ? observation.event_type : '';
      let pendingRow = pendingByKey.get(key);

      if (isPending) {
        if (!pendingRow) {
          pendingRow = {
            key, game_pk: String(observation.game_pk), at_bat: observation.at_bat ?? null,
            play_id: observation.play_id || '', matchup: observation.matchup || '',
            inning: observation.inning ?? null, half: observation.half || '',
            pending_kind: observation.pending_kind || 'description_only',
            pending_codes: Array.isArray(observation.pending_codes) ? observation.pending_codes.slice() : [],
            required_pending_codes: Array.isArray(observation.pending_codes) ? observation.pending_codes.slice() : [],
            current_pending_codes: Array.isArray(observation.pending_codes) ? observation.pending_codes.slice() : [],
            pending_event_refs: Array.isArray(observation.pending_event_refs) ? observation.pending_event_refs.slice() : [],
            status: 'pending', first_seen_utc: stamp, last_seen_utc: stamp,
            initial_description: observation.description || '',
            pending_text: observation.pending_text || PENDING_TEXT,
            initial_event_type: currentType,
            resolved_primary_event_type: '', resolved_prior_event_type: '',
            batter: observation.batter || '', pitcher: observation.pitcher || '',
            official_feed_url: observation.official_feed_url || '',
            prediction_snapshot: null,
          };
          pendingByKey.set(key, pendingRow);
          state.pending.push(pendingRow);
        } else {
          if (pendingRow.status === 'resolved') pendingRow.reopened_utc = stamp;
          pendingRow.status = 'pending';
          pendingRow.last_seen_utc = stamp;
          const currentCodes = Array.isArray(observation.pending_codes) ? observation.pending_codes.slice() : [];
          const requiredCodes = new Set([...(pendingRow.required_pending_codes || pendingRow.pending_codes || []), ...currentCodes]);
          pendingRow.required_pending_codes = [...requiredCodes];
          pendingRow.pending_codes = [...requiredCodes];
          pendingRow.current_pending_codes = currentCodes;
          const refs = new Map((pendingRow.pending_event_refs || []).filter(ref => ref && ref.event_key)
            .map(ref => [ref.event_key, ref]));
          (Array.isArray(observation.pending_event_refs) ? observation.pending_event_refs : []).forEach(ref => {
            if (ref && ref.event_key) refs.set(ref.event_key, ref);
          });
          pendingRow.pending_event_refs = [...refs.values()];
          pendingRow.pending_kind = pendingKind({
            primary: requiredCodes.has('os_ruling_pending_primary'),
            prior: requiredCodes.has('os_ruling_pending_prior'),
          }) || observation.pending_kind || pendingRow.pending_kind;
        }
        captureResolvedComponents(pendingRow, observation);
        const candidatePrediction = predictionSnapshot(rowsByKey.get(key));
        if (!pendingRow.prediction_snapshot && candidatePrediction)
          pendingRow.prediction_snapshot = candidatePrediction;
        // Preserve the exact result.eventType as an observed feed classification if it is present
        // alongside a pending marker; the pending badge remains authoritative for state.
        recordClassification(key, observation, currentType);
        return;
      }

      if (pendingRow && pendingRow.status !== 'resolved') {
        pendingRow.last_seen_utc = stamp;
        captureResolvedComponents(pendingRow, observation);
        const required = new Set(pendingRow.required_pending_codes || pendingRow.pending_codes || []);
        const primaryResolved = !required.has('os_ruling_pending_primary') ||
          Boolean(pendingRow.resolved_primary_event_type);
        const priorResolved = !required.has('os_ruling_pending_prior') ||
          Boolean(pendingRow.resolved_prior_event_type);
        const hasKnownScope = required.has('os_ruling_pending_primary') || required.has('os_ruling_pending_prior');
        if (hasKnownScope && primaryResolved && priorResolved) {
          pendingRow.status = 'resolved';
          pendingRow.resolved_utc = stamp;
          pendingRow.resolved_event_type = pendingRow.resolved_primary_event_type ||
            pendingRow.resolved_prior_event_type || '';
          pendingRow.resolved_description = pendingRow.resolved_primary_description ||
            pendingRow.resolved_prior_description || observation.description || '';
          pendingRow.resolved_score = {
            away: numberOrNull(observation.away_score), home: numberOrNull(observation.home_score),
          };
          pendingRow.resolved_feed_url = observation.official_feed_url || pendingRow.official_feed_url;
          pendingRow.latest_note = '';
        } else {
          pendingRow.status = 'awaiting_result';
          if (!hasKnownScope) {
            pendingRow.latest_note = 'The exact description marker cleared, but it did not identify primary vs prior-event scope; no final ruling is inferred.';
          } else if (required.has('os_ruling_pending_prior') && !pendingRow.resolved_prior_event_type) {
            pendingRow.latest_note = 'The prior-event marker cleared, but no non-pending eventType was observed on the same identified playEvent; result.eventType alone does not resolve a prior base-running ruling.';
          } else {
            pendingRow.latest_note = 'The primary pending marker cleared, but no non-empty final result.eventType was captured; no final ruling is inferred.';
          }
        }
      }

      // Only an observed non-empty result.eventType can establish/update a feed classification.
      recordClassification(key, observation, currentType);
    });

    // When an at-bat remains in the feed but the pending marker has cleared without a final
    // eventType, keep it visible as awaiting_result. Missing plays are not treated as resolutions.
    state.pending = state.pending.slice(-MAX_EVENTS);
    state.changes = state.changes.slice(-MAX_EVENTS);
    const playKeys = Object.keys(playSnapshots);
    if (playKeys.length > MAX_PLAY_SNAPSHOTS) {
      for (const key of playKeys.slice(0, playKeys.length - MAX_PLAY_SNAPSHOTS)) delete playSnapshots[key];
    }
    return { state, flags, observations: observedByKey.size };
  }

  function readState(storage) {
    try {
      const raw = storage.getItem(STORAGE_KEY);
      if (!raw) return { version: 1, dates: {} };
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.version !== 1 || !parsed.dates || typeof parsed.dates !== 'object') {
        warn('Discarded malformed LiveScoringErrors scoring-feed log; a new observed log will be created.');
        return { version: 1, dates: {} };
      }
      const dates = {};
      Object.entries(parsed.dates).forEach(([date, value]) => {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
        dates[date] = sanitizeDateState(value);
      });
      return { version: 1, dates };
    } catch (error) {
      warn(`Could not read the browser scoring-feed log (${error && error.message ? error.message : error}); starting a new log.`);
      return { version: 1, dates: {} };
    }
  }

  function writeState(storage, state) {
    try {
      storage.setItem(STORAGE_KEY, JSON.stringify(state));
      return true;
    } catch (error) {
      warn(`Could not save the browser scoring-feed log (${error && error.message ? error.message : error}); the live rows remain visible for this visit.`);
      return false;
    }
  }

  function updateState(storage, date, observations, modelRows, observedUtc) {
    const state = readState(storage);
    const merged = mergeDateState(state.dates[date], observations, modelRows, observedUtc);
    state.dates[date] = merged.state;
    const dates = Object.keys(state.dates).sort().reverse();
    dates.slice(MAX_DATES).forEach(oldDate => delete state.dates[oldDate]);
    let all = Object.entries(state.dates).flatMap(([d, dState]) =>
      dState.pending.map(row => ({ date: d, when: row.resolved_utc || row.last_seen_utc || row.first_seen_utc, row })));
    if (all.length > MAX_EVENTS) {
      all.sort((a, b) => String(b.when).localeCompare(String(a.when)));
      const keep = new Set(all.slice(0, MAX_EVENTS).map(item => `${item.date}|${item.row.key}`));
      Object.entries(state.dates).forEach(([d, dState]) => {
        dState.pending = dState.pending.filter(row => keep.has(`${d}|${row.key}`));
      });
    }
    const saved = writeState(storage, state);
    return { ...merged, dates: state.dates, saved };
  }

  return Object.freeze({
    STORAGE_KEY, PENDING_CODES: [...PENDING_CODES], PENDING_TEXT,
    isOfficialScoringPendingEvent, findOfficialScoringPendingPlay, pendingKind,
    extractObservations, eventKey, emptyDateState, sanitizeDateState,
    mergeDateState, readState, writeState, updateState,
  });
})();
