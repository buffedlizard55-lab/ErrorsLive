/* ============================================================================
 * scoring-watch.js — shared engine for the two simplified ErrorsLive pages:
 *   index.html   (scoreboard)  and  reviews.html  (scoring feed).
 *
 * SCOPE (deliberately narrow — this is the whole product):
 *   1. Official-scorer pending rulings (os_ruling_pending_primary /
 *      os_ruling_pending_prior) while the scorer has not decided.
 *   2. Plays the official feed currently rules an error (result.eventType
 *      field_error) — "errors on the board".
 *   3. Observed scoring changes (a play's classification differing between
 *      two captures of the official feed).
 * Everything else the upstream replay feed tracks (ABS, manager challenges,
 * umpire reviews, boundary calls, every batted ball) is OUT of this view.
 *
 * THE SCORE: one number per row — the model's probability that the batted
 * ball ends up scored a HIT, computed by docs/site.js scoreLiveFeed() with the
 * committed model (docs/data/model.json, 11-outcome head). The hit number is
 * the sum of the model's single + double + triple + home run probabilities.
 * It is a property of the batted ball, NOT the probability that an issued
 * ruling changes; the rows say so.
 *
 * Data path: LiveBoard.pollDay() (docs/live-board.js) does schedule + feeds +
 * scoring ledger; this module never re-implements detection, the model math,
 * or persistence. Verdicts use ONLY the prediction snapshot the ledger stored
 * while a ruling was still pending — never a recomputed post-hoc estimate.
 * ==========================================================================*/
'use strict';

const ScoringWatch = (() => {

  /* ---- outcome categories ------------------------------------------------
     "model" keys below are exactly the 11 classes of event_type_model in the
     committed model.json (verified 2026-10-02 against the file and against
     GET /api/v1/eventTypes). categoryOfEvent() maps a FINAL result.eventType
     to a category with the same grouping the model was trained on
     (event_type_groups). Codes outside those groups are "other" — the model
     never scored them, so no verdict is issued for them. triple_play and
     catcher interference are display-level folds (out / other). */
  const MODEL_CLASSES = ['single', 'double', 'triple', 'home_run', 'error',
    'fielders_choice', 'field_out', 'force_out', 'double_play', 'sac_fly', 'sac_bunt'];

  const GROUPS = {
    hit: { label: 'Hit', short: 'HIT', classes: ['single', 'double', 'triple', 'home_run'] },
    error: { label: 'Error', short: 'ERROR', classes: ['error'] },
    fc: { label: "Fielder's choice", short: 'FC', classes: ['fielders_choice'] },
    out: { label: 'Out', short: 'OUT', classes: ['field_out', 'force_out', 'double_play'] },
    sac_fly: { label: 'Sac fly', short: 'SAC FLY', classes: ['sac_fly'] },
    sac_bunt: { label: 'Sac bunt', short: 'SAC BUNT', classes: ['sac_bunt'] },
  };
  const GROUP_ORDER = ['hit', 'error', 'fc', 'out', 'sac_fly', 'sac_bunt'];

  /* Final eventType -> category. Same folds as event_type_groups, plus the
     display-level folds noted above. Anything unmapped returns 'other'. */
  const EVENT_TO_GROUP = {
    single: 'hit', double: 'hit', triple: 'hit', home_run: 'hit',
    field_error: 'error',
    fielders_choice: 'fc', fielders_choice_out: 'fc',
    field_out: 'out', force_out: 'out', double_play: 'out',
    grounded_into_double_play: 'out', sac_fly_double_play: 'out', triple_play: 'out',
    sac_fly: 'sac_fly', sac_bunt: 'sac_bunt',
  };

  function categoryOfEvent(eventType) {
    return EVENT_TO_GROUP[String(eventType || '')] || 'other';
  }

  function groupLabel(key) { return (GROUPS[key] || {}).label || key; }
  function groupShort(key) { return (GROUPS[key] || {}).short || String(key).toUpperCase(); }

  /* event_probs: {single: p, double: p, ...} (model classes) ->
     ordered [{key, label, p}] over GROUP_ORDER. Unknown keys are ignored
     rather than folded somewhere plausible. */
  function distribution(eventProbs) {
    const out = [];
    let known = 0;
    GROUP_ORDER.forEach(key => {
      const p = (GROUPS[key].classes || []).reduce((sum, c) =>
        sum + (Number.isFinite(Number((eventProbs || {})[c])) ? Number(eventProbs[c]) : 0), 0);
      known += p;
      out.push({ key, label: groupLabel(key), short: groupShort(key), p });
    });
    return { groups: out, covered: known };
  }

  function topGroup(eventProbs) {
    const dist = distribution(eventProbs);
    return dist.groups.reduce((a, b) => (b.p > a.p ? b : a), dist.groups[0]);
  }

  /* ---- the one score ----------------------------------------------------- */
  function hitScore(event) {
    if (!event || event.prediction_available !== true) return null;
    const probs = event.event_probs || {};
    const hit = ['single', 'double', 'triple', 'home_run'].reduce((s, c) =>
      s + (Number.isFinite(Number(probs[c])) ? Number(probs[c]) : 0), 0);
    return Number.isFinite(hit) ? hit : null;
  }

  /* Verbal band for a hit probability. Thresholds are this project's own
     display convention; the number itself is always shown alongside. */
  function hitBand(p, ruledError) {
    if (!Number.isFinite(Number(p))) return '';
    const pct = p * 100;
    if (ruledError) {
      if (pct >= 25) return 'rescore risk — the shape also fits a hit';
      if (pct >= 12) return 'borderline — a scorer could see it either way';
      return 'reads as a genuine error';
    }
    if (pct >= 60) return 'likely hit';
    if (pct >= 35) return 'too close to call';
    if (pct >= 12) return 'leaning out / error';
    return 'unlikely a hit';
  }

  /* ---- which events this simplified view shows ---------------------------
     kind comes from LiveBoard.buildEvents():
       pending    — an official-scorer pending marker is live on the play
       change     — the ledger recorded a classification change on the play
       batted_ball— a completed, classified batted ball
     plus any row the ledger attached (resolved pending rows come back as
     batted_ball rows with .ledger_pending.status === 'resolved'). */
  function isScoringEvent(event) {
    if (!event) return false;
    if (event.kind === 'pending' || event.kind === 'change') return true;
    if (event.ledger_pending) return true;
    return event.kind === 'batted_ball' && event.official_call === 'error' &&
      !(event.ledger_pending && event.ledger_pending.status === 'resolved');
  }

  function scoringEvents(events) {
    return (Array.isArray(events) ? events : []).filter(isScoringEvent);
  }

  /* ---- verdicts, from the ledger's own pre-ruling snapshot ---------------
     ScoringFeed stores prediction_snapshot on a pending row the first time it
     is observed. When the ruling lands we compare the snapshot's top category
     with the resolved classification. No snapshot (page closed at the time,
     first visit, storage cleared) means NO VERDICT — never a retroactive one. */
  function verdictForPending(ledgerPending) {
    const row = ledgerPending;
    if (!row || row.status !== 'resolved') return null;
    const snap = row.prediction_snapshot;
    const finalCode = row.resolved_event_type || '';
    if (!finalCode) return { kind: 'no-final', reason: 'no final classification was observed' };
    const finalCat = categoryOfEvent(finalCode);
    if (finalCat === 'other')
      return { kind: 'out-of-scope', finalCode, finalCat };
    if (!snap || typeof snap !== 'object')
      return { kind: 'no-snapshot', finalCode, finalCat };
    const probs = {};
    Object.entries(snap).forEach(([k, v]) => {
      if (k.startsWith('event_p_') && Number.isFinite(Number(v))) probs[k.slice(8)] = Number(v);
    });
    if (!MODEL_CLASSES.some(c => Number.isFinite(probs[c])))
      return { kind: 'no-snapshot', finalCode, finalCat };
    const top = topGroup(probs);
    return {
      kind: 'scored', finalCode, finalCat,
      called: top.key, calledProb: top.p,
      correct: top.key === finalCat,
      saidHit: top.key === 'hit', wasHit: finalCat === 'hit',
    };
  }

  /* Day tally over resolved pending rows with a real pre-ruling snapshot. */
  function dayTally(pendingRows) {
    const rows = Array.isArray(pendingRows) ? pendingRows : [];
    let decided = 0, topCorrect = 0, saidHit = 0, hitWhenSaid = 0;
    rows.forEach(row => {
      const v = verdictForPending(row);
      if (!v || v.kind !== 'scored') return;
      decided += 1;
      if (v.correct) topCorrect += 1;
      if (v.saidHit) {
        saidHit += 1;
        if (v.wasHit) hitWhenSaid += 1;
      }
    });
    return { decided, topCorrect, saidHit, hitWhenSaid };
  }

  /* ---- per-game counters for the scoreboard ------------------------------ */
  function gameBadges(record) {
    const pendingRows = (record.ledger && record.ledger.pending) || [];
    return {
      pendingNow: pendingRows.filter(p => p && p.status !== 'resolved').length,
      changes: ((record.ledger && record.ledger.changes) || []).length,
      errorsOnBoard: (record.summary && record.summary.feed_errors) || 0,
    };
  }

  /* ---- sound --------------------------------------------------------------
     A two-note chime when this page life sees a scoring event for the first
     time. Off by default; the state is one localStorage flag. Any error
     inside leaves the page exactly as it was. */
  const SOUND_KEY = 'errorslive.watch.sound.v1';
  let audioCtx = null;
  function soundOn() {
    try { return window.localStorage.getItem(SOUND_KEY) === '1'; } catch (_) { return false; }
  }
  function setSound(on) {
    try { window.localStorage.setItem(SOUND_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ }
  }
  function chime() {
    if (!soundOn()) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audioCtx = audioCtx || new Ctx();
      if (audioCtx.state === 'suspended') { audioCtx.resume().catch(() => {}); }
      const now = audioCtx.currentTime;
      [[880, 0], [1174.66, 0.12]].forEach(([freq, offset]) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.08, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.35);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + 0.4);
      });
    } catch (_) { /* sound is decorative; never let it break the poll */ }
  }

  /* ---- model -------------------------------------------------------------- */
  let modelPromise = null;
  function loadModel() {
    if (modelPromise) return modelPromise;
    modelPromise = fetch('../data/model.json', { signal: AbortSignal.timeout(10000) })
      .then(r => {
        if (!r.ok) throw new Error(`model HTTP ${r.status}`);
        return r.json();
      })
      .catch(error => {
        modelPromise = null; // allow a retry on the next poll
        throw error;
      });
    return modelPromise;
  }

  return Object.freeze({
    MODEL_CLASSES, GROUPS, GROUP_ORDER,
    categoryOfEvent, groupLabel, groupShort, distribution, topGroup,
    hitScore, hitBand, isScoringEvent, scoringEvents,
    verdictForPending, dayTally, gameBadges,
    SOUND_KEY, soundOn, setSound, chime, loadModel,
  });
})();
