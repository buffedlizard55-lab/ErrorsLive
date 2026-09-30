/* Live alert engine for the scoring-decision watcher.
 *
 * Design rule: this file is pure. `diffBoards` takes two snapshots of scored batted balls and
 * returns alert objects; it performs no DOM work, no timers and no network calls, so
 * `tests/test_pipeline.py` can exercise every rule under node and prove the alerts fire exactly
 * once, in the right order, with the right severity.
 *
 * WHAT AN ALERT MEANS — AND WHAT IT DOES NOT
 *   The MLB Stats API publishes no scorer queue. Everything here is a statement about the captured
 *   feed: a batted ball had Statcast contact data while `result.eventType` was absent, and a later
 *   capture had a different value. That is an observable feed-state change. It is not proof that an
 *   official decision was pending, who made it, when, or why. Rule 9.01(a) of the 2026 Official
 *   Baseball Rules (page 107) describes the scorer's own timeline: a "preliminary" judgment is made
 *   during play, generally no later than the start of the next plate appearance, and it becomes
 *   "final" (or is revised) within 24 hours after the game; a Club may then appeal to the
 *   Commissioner's designee within 72 hours, and no judgment decision changes after that. The feed
 *   timestamps below are capture times, not the scorer's clock.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ALERTS = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SEVERITY = { critical: 3, high: 2, medium: 1, info: 0 };
  const DEFAULT_THRESHOLD = 3;           // SCORE/100 at or above this is worth a second look
  const HIT_TYPES = new Set(['single', 'double', 'triple', 'home_run']);
  const FC_TYPES = new Set(['fielders_choice', 'fielders_choice_out']);
  const ERROR_TYPES = new Set(['field_error']);
  const OUT_TYPES = new Set(['field_out', 'force_out', 'grounded_into_double_play', 'double_play',
    'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play', 'fielders_choice_double_play']);

  function macroClass(eventType) {
    if (!eventType) return 'pending';
    if (ERROR_TYPES.has(eventType)) return 'error';
    if (HIT_TYPES.has(eventType)) return 'hit';
    if (FC_TYPES.has(eventType)) return 'fielder\'s choice';
    if (OUT_TYPES.has(eventType)) return 'out';
    return eventType.replace(/_/g, ' ');
  }

  function playKey(row) {
    return `${row.game_pk || row.gamePk || ''}:${row.at_bat !== undefined ? row.at_bat : ''}`;
  }

  function text(value) {
    return value === undefined || value === null ? '' : String(value);
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  /** Index a board (array of scored rows) by play key so two polls can be diffed. */
  function boardIndex(rows) {
    const map = new Map();
    for (const row of rows || []) map.set(playKey(row), row);
    return map;
  }

  /** Severity for a transition, with the reason, so a reader can see why it was ranked. */
  function severityOf(type, detail) {
    switch (type) {
      case 'event_type_changed': return 'critical';
      case 'rbi_changed': return 'critical';
      case 'review_overturned': return 'high';
      case 'decision_settled': return 'high';
      case 'run_at_stake': return 'high';
      case 'error_ruled': return 'high';
      case 'high_review_score': return 'medium';
      case 'pending_in_feed': return 'info';
      case 'feed_row_removed': return 'medium';
      default: return detail && detail.severity ? detail.severity : 'info';
    }
  }

  function makeAlert(type, row, detail) {
    const severity = severityOf(type, detail);
    const key = playKey(row);
    const at = (detail && detail.at) || new Date().toISOString();
    // Deduplicate on the transition itself, never on the clock: the same observation seen again on a
    // retry or a reload must not alert twice. `seq` is an optional per-play counter the caller keeps
    // across polls, so a genuine later recurrence of the same transition is still reported.
    const id = `${key}|${type}|${text(detail && detail.from)}->${text(detail && detail.to)}|` +
               `${text(row.seq || 0)}`;
    return Object.assign({
      id, type, severity, at,
      game_pk: text(row.game_pk || row.gamePk), matchup: text(row.matchup),
      inning: row.inning, half: text(row.half), batter: text(row.batter),
      play_id: text(row.play_id), description: text(row.description),
      official_call: text(row.official_call), from: '', to: '',
      score_100: num(row.score_100), top_pick: text(row.top_pick),
      rbi_official: row.rbi_official === undefined ? '' : row.rbi_official,
      runners_on: text(row.runners_on), outs_before: row.outs_before,
      feed_url: text(row.official_feed_url || row.feed_url || row.source),
      watch_url: text(row.savant_url || row.watch_url),
      // The feed carries no scorer queue; say so on every alert rather than implying one.
      note: 'Captured feed-state change. MLB publishes no open scorer-decision queue; a changed ' +
            'field value is not proof that a decision was pending or why it changed.',
    }, detail || {});
  }

  /**
   * Diff two polls of scored batted balls.
   *
   * @param {Map|Array} previous  previous board (index or rows)
   * @param {Array} nextRows      current scored rows
   * @param {Object} [opts]       {threshold, at, includeInfo, maxAlerts, seenIds}
   * @returns {Array} alerts, newest first, deduplicated by id
   */
  function diffBoards(previous, nextRows, opts) {
    const o = opts || {};
    const at = o.at || new Date().toISOString();
    const threshold = Number.isFinite(o.threshold) ? o.threshold : DEFAULT_THRESHOLD;
    const includeInfo = o.includeInfo !== false;
    // Duck-typed on purpose: `instanceof Set` is false for a Set handed in from another realm (a
    // test harness running this file in a vm context, an iframe), and silently copying it there
    // would make the caller's de-duplication set stop receiving new ids.
    const seen = (o.seenIds && typeof o.seenIds.has === 'function' &&
                  typeof o.seenIds.add === 'function') ? o.seenIds : new Set(o.seenIds || []);
    const prev = previous instanceof Map ? previous : boardIndex(previous);
    const alerts = [];
    const push = (type, row, detail) => {
      const a = makeAlert(type, row, Object.assign({ at }, detail));
      const isNew = !seen.has(a.id);
      seen.add(a.id);          // the caller's set is authoritative across polls; keep it current
      if (isNew) alerts.push(a);
      return a;
    };

    for (const row of nextRows || []) {
      const key = playKey(row);
      const was = prev.get(key);
      const eventType = text(row.event_type);
      const status = text(row.status);
      const pending = status === 'no_event_type_yet' || (!eventType && status !== 'no_vector');

      if (!was) {
        // First capture of this play. A caller that is establishing its baseline passes
        // `newRows: false` so it does not alert on every play of a game it just opened.
        if (o.newRows === false) continue;
        if (pending) {
          if (includeInfo) {
            push('pending_in_feed', row, {
              to: 'no result.eventType in the captured feed',
              message: 'Statcast contact data is present but the feed has no result.eventType yet.',
            });
          }
        } else if (errorOf(row)) {
          push('error_ruled', row, { to: eventType, message: 'The captured feed rules this an error.' });
        } else if (num(row.score_100) !== null && num(row.score_100) >= threshold) {
          push('high_review_score', row, {
            to: eventType,
            message: `SCORE/100 ${num(row.score_100).toFixed(2)} is at or above the review threshold ` +
                     `(${threshold}); the model ranks this play for a second look.`,
          });
        }
        continue;
      }

      const wasEventType = text(was.event_type);
      const wasStatus = text(was.status);
      const wasPending = wasStatus === 'no_event_type_yet' || !wasEventType;

      // 1. The observable version of "a decision landed": nothing -> something.
      if (wasPending && eventType) {
        push('decision_settled', row, {
          from: 'no result.eventType', to: eventType,
          message: `The feed gained a ruling: ${macroClass(wasEventType)} -> ${macroClass(eventType)}. ` +
                   (num(was.score_100) !== null
                     ? `The model had scored this play ${num(was.score_100).toFixed(2)}/100.` : ''),
          model_score_before: num(was.score_100),
        });
      } else if (wasEventType && eventType && wasEventType !== eventType) {
        // 2. A reclassification: this is the error -> hit / fielder's-choice case in the brief.
        const flip = `${macroClass(wasEventType)} -> ${macroClass(eventType)}`;
        push('event_type_changed', row, {
          from: wasEventType, to: eventType,
          message: `The captured ruling changed: ${wasEventType} -> ${eventType} (${flip}).`,
          error_to_non_error: wasEventType === 'field_error' && eventType !== 'field_error',
          rbi_question: wasEventType === 'field_error' && eventType !== 'field_error'
            ? ('Rule 9.04: an error-play run is not automatically without an RBI — Rule 9.04(a)(3) '
               + 'credits one when, before two are out, an error is made on a play on which a runner '
               + "from third base ordinarily would score. Check the official scorer's final ruling.")
            : '',
        });
      }

      // 3. RBI field movement (the statistic the brief cares about), independent of the label.
      const wasRbi = was.rbi_official, rbi = row.rbi_official;
      if (text(wasRbi) !== text(rbi)) {
        push('rbi_changed', row, {
          from: text(wasRbi), to: text(rbi),
          message: `The official RBI field changed: ${text(wasRbi) || '(empty)'} -> ${text(rbi) || '(empty)'}.`,
          error_to_non_error: wasEventType === 'field_error' && eventType !== 'field_error',
        });
      }

      // 4. A review appearing or being marked overturned.
      const wasOver = was.review_overturned === true || was.review_overturned === 'true';
      const isOver = row.review_overturned === true || row.review_overturned === 'true';
      if (isOver && !wasOver) {
        push('review_overturned', row, {
          to: 'overturned', review_type: text(row.review_type),
          message: `Review marked overturned${row.review_type ? ` (${row.review_type})` : ''} in the captured feed.`,
        });
      } else if (!was.reviewed && row.reviewed) {
        push('review_added', row, { to: 'reviewed', review_type: text(row.review_type) });
      }

      // 5. A run scored with a runner in scoring position: the RBI-at-stake case from the brief.
      const wasStake = Boolean(was.risp && was.run_scored);
      const isStake = Boolean(row.risp && row.run_scored);
      if (isStake && !wasStake) {
        push('run_at_stake', row, {
          from: '', to: 'run scored with a runner on 2nd/3rd',
          message: 'A run scored with a runner on second or third. The RBI depends on the final ruling: ' +
                   'Rule 9.04 credits it for a hit, infield out or fielder\'s choice, and for an error ' +
                   'before two outs when a runner from third ordinarily would score.',
        });
      }

      // 6. A row disappearing from the feed is a data anomaly worth flagging, never silently dropped.
      if (status === 'no_vector' && was.status !== 'no_vector') {
        push('feed_row_removed', row, { to: 'incomplete Statcast vector',
          message: 'This play lost a complete Statcast vector between captures.' });
      }
    }

    alerts.sort((a, b) => (SEVERITY[b.severity] - SEVERITY[a.severity]) ||
                           String(b.at).localeCompare(String(a.at)));
    const max = Number.isFinite(o.maxAlerts) ? o.maxAlerts : 200;
    return alerts.slice(0, max);
  }

  function errorOf(row) {
    return text(row.event_type) === 'field_error' || text(row.official_call) === 'error';
  }

  /** Short human label for an alert type, shared by the page and the CSV export. */
  function alertLabel(type) {
    return ({
      pending_in_feed: 'contact, no call in feed yet',
      decision_settled: 'ruling appeared',
      event_type_changed: 'ruling changed',
      rbi_changed: 'RBI field changed',
      review_overturned: 'review overturned',
      review_added: 'review opened',
      run_at_stake: 'run scored with runner in scoring position',
      error_ruled: 'error ruled',
      high_review_score: 'high model score',
      feed_row_removed: 'contact vector incomplete',
    })[type] || type;
  }

  return {
    diffBoards, boardIndex, playKey, macroClass, alertLabel, severityOf,
    SEVERITY, DEFAULT_THRESHOLD,
  };
}));
