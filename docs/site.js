/* Shared helpers for the LiveScoringErrors pages.
   Data files are served from ./data/. The model math here is mirrored by tools/live_score.py, and
   tests/test_pipeline.py asserts the two implementations agree to 1e-9 on every archived batted
   ball. Both sides build their feature vector from `primary.feature_spec` in model.json, so a change
   to the features cannot silently desynchronise the page from the tool. */
const NAV = [
  ['index.html', 'Score a batted ball'],
  ['scoreboard.html', 'Scoreboard'],
  ['allgames.html', 'Scoring feed'],
  ['live.html', 'Live board'],
  ['alerts.html', 'Live alerts'],
  ['replays.html', 'Replays & runs'],
  ['model.html', 'Model & validation'],
  ['overturned.html', 'Archive 2014–18'],
  ['rules.html', 'Rules & RBI'],
  ['rbi.html', 'RBI stakes, measured'],
  ['methods.html', 'Methods & audit'],
  ['roadmap.html', 'Limitations'],
];
(function buildNav() {
  const host = document.querySelector('.nav-inner');
  if (!host) return;
  host.insertAdjacentHTML('beforeend',
    '<a class="brand" href="index.html">Live<span>Scoring</span>Errors</a>' +
    NAV.map(([h, t]) => `<a class="navlink" href="${h}" data-p="${h}">${t}</a>`).join(''));
  const here = (location.pathname.split('/').pop() || 'index.html');
  document.querySelectorAll(`a.navlink[data-p="${here}"]`).forEach(a => a.classList.add('on'));
})();

function parseCSV(text) {
  /* minimal RFC4180 reader (quotes escaped as "") */
  const rows = []; let row = [], cur = '', inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else inQ = false; }
      else cur += c;
    } else if (c === '"') inQ = true;
    else if (c === ',') { row.push(cur); cur = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cur); rows.push(row); row = []; cur = '';
    } else cur += c;
  }
  if (cur.length || row.length) { row.push(cur); rows.push(row); }
  const hdr = rows.shift();
  return rows.filter(r => r.length === hdr.length).map(r => Object.fromEntries(hdr.map((h, i) => [h, r[i]])));
}

async function loadJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(url + ' -> ' + r.status); return r.json(); }
async function loadCSV(url) { const r = await fetch(url); if (!r.ok) throw new Error(url + ' -> ' + r.status); return parseCSV(await r.text()); }
async function loadMaybe(url) { try { return await loadJSON(url); } catch (e) { return null; } }

/* ---------- model evaluation (mirror of tools/live_score.py Scorer.predict_state) ---------- */
const TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder'];
const HARD = ['soft', 'medium', 'hard'];

function featureValue(ftype, st) {
  switch (ftype) {
    case 'ev': return st.ev;
    case 'la': return st.la;
    case 'dist': return st.dist;
    case 'outs': return st.outs || 0;
    case 'inning': return Math.min(st.inning || 5, 9);
  }
  const [kind, val] = ftype.split(':');
  if (kind === 'traj') return st.traj === val ? 1 : 0;
  if (kind === 'hard') return st.hard === val ? 1 : 0;
  if (kind === 'base') return (st.bases || []).includes(val) ? 1 : 0;
  if (kind === 'bat') return (st.bat || '') === val ? 1 : 0;
  if (kind === 'pitch') return (st.pitch || '') === val ? 1 : 0;
  throw new Error('unknown feature type ' + ftype);
}

function featureVector(M, st) {
  const spec = M.primary.feature_spec;
  if (spec && spec.length) return spec.map(f => featureValue(f.type, st));
  return [st.ev, st.la, st.dist, ...TRAJ.map(t => t === st.traj ? 1 : 0), ...HARD.map(h => h === st.hard ? 1 : 0)];
}

function evalModel(M, st) {
  const p = M.primary;
  const x = featureVector(M, st);
  const z = p.feature_names.map((_, i) => (x[i] - p.scaler_mean[i]) / (p.scaler_scale[i] || 1));
  const logit = p.intercept + z.reduce((s, v, i) => s + v * p.coef[p.feature_names[i]], 0);
  const pErr = 1 / (1 + Math.exp(-logit));
  const mc = M.multiclass; const classes = Object.keys(mc.coef);
  const exps = classes.map(c => Math.exp(mc.intercept[c] + z.reduce((s, v, i) => s + v * mc.coef[c][p.feature_names[i]], 0)));
  const tot = exps.reduce((a, b) => a + b, 0);
  const probs = Object.fromEntries(classes.map((c, i) => [c, exps[i] / tot]));
  const top = classes.reduce((a, b) => probs[a] >= probs[b] ? a : b);
  const eventModel = M.event_type_model;
  let eventProbs = {}, eventTop = null;
  if (eventModel && eventModel.classes && eventModel.classes.length) {
    const ex = featureVector({ primary: eventModel }, st);
    const ez = eventModel.feature_names.map((_, i) =>
      (ex[i] - eventModel.scaler_mean[i]) / (eventModel.scaler_scale[i] || 1));
    const logits = Object.fromEntries(eventModel.classes.map(c => [c,
      eventModel.intercept[c] + ez.reduce((sum, value, i) =>
        sum + value * eventModel.coef[c][eventModel.feature_names[i]], 0)]));
    const maxLogit = Math.max(...Object.values(logits));
    const exps = eventModel.classes.map(c => Math.exp(logits[c] - maxLogit));
    const eventTotal = exps.reduce((sum, value) => sum + value, 0) || 1;
    eventProbs = Object.fromEntries(eventModel.classes.map((c, i) => [c, exps[i] / eventTotal]));
    eventTop = eventModel.classes.reduce((a, b) => eventProbs[a] >= eventProbs[b] ? a : b);
  }
  return { pErr, probs, top, eventProbs, eventTop, x, z };
}

/* The direct-browser live path mirrors tools/live_score.py. It uses only fields from the MLB
   Stats API and the committed model; it does not call a local service or upload anything. */
const HIT_TYPES = new Set(['single', 'double', 'triple', 'home_run']);
const FC_TYPES = new Set(['fielders_choice', 'fielders_choice_out']);
const OUT_TYPES = new Set(['field_out', 'force_out', 'grounded_into_double_play', 'double_play',
  'sac_fly', 'sac_bunt', 'sac_fly_double_play', 'triple_play']);
function macroClass(eventType) {
  if (eventType === 'field_error') return 'error';
  if (HIT_TYPES.has(eventType)) return 'hit';
  if (FC_TYPES.has(eventType)) return 'fielders_choice';
  if (OUT_TYPES.has(eventType)) return 'out';
  return 'other';
}
/* Conditional Rule 9.04 reminder for a run that scored. String-for-string identical to
   tools/live_score.py rbi_if_ruled(); tests/test_pipeline.py asserts the two agree, and
   docs/rules.html carries the verbatim 2026 rule text the notes cite. */
function rbiRuleNote(cls, runScored, officialEvent, bases = [], outs = 0) {
  if (!runScored) return null;
  if (officialEvent === 'home_run')
    return cls === 'hit'
      ? 'The official play is a home run (a hit); check the recorded RBI field for the official result.'
      : 'not a valid alternative: the official play is a home run; this feed row cannot model a counterfactual error or fielder\'s-choice ruling';
  if (cls === 'error') {
    if (outs < 2 && bases.includes('3B'))
      return 'Rule 9.04(a)(3) can apply: fewer than two outs with a runner on third base. It ' +
             'credits an RBI when that runner "ordinarily would score" - a counterfactual the ' +
             'feed does not record, so this is not a ruling.';
    return 'Rule 9.04(a)(3) cannot apply: it requires fewer than two outs and a runner on third ' +
           'base. Rule 9.04(a)(1) requires the run to be "unaided by an error", so no RBI is ' +
           'credited for this error-dependent run.';
  }
  if (cls === 'fielders_choice')
    return "A run-scoring fielder's choice is one of the plays Rule 9.04(a)(1) lists, so an RBI " +
           "can be credited, subject to the Rule 9.04(b) exceptions and the scorer's judgment; " +
           'this feed row does not settle any exception.';
  if (cls === 'hit')
    return 'A run that scores on a hit is credited an RBI under Rule 9.04(a)(1), subject to the ' +
           "Rule 9.04(b) exceptions and the scorer's judgment; this feed row does not settle any " +
           'exception.';
  return 'RBI depends on the official scoring rule for this play.';
}

function scoreLiveFeed(feed, game, model) {
  const plays = (((feed || {}).liveData || {}).plays || {}).allPlays || [];
  const source = game.official_feed_url || '';
  const pk = game.gamePk || game.pk || '';
  const groups = (model.event_type_model || {}).event_type_groups || {};
  const eventTypeToClass = new Set(Object.values(groups).flat());
  const rows = [];
  for (const play of plays) {
    if (!play || typeof play !== 'object') continue;
    const result = play.result || {};
    const eventType = result.eventType || '';
    const pending = typeof ScoringFeed !== 'undefined'
      ? ScoringFeed.findOfficialScoringPendingPlay(play) : null;
    const pendingCodes = pending ? pending.pendingCodes : [];
    const pendingPrimary = Boolean(pending && pending.primary);
    const pendingPrior = Boolean(pending && pending.prior);
    const events = Array.isArray(play.playEvents) ? play.playEvents : [];
    const hitEvent = events.find(e => e && e.hitData && typeof e.hitData === 'object');
    const hd = hitEvent && hitEvent.hitData;
    const knownType = eventTypeToClass.size ? eventTypeToClass.has(eventType)
      : macroClass(eventType) !== 'other';
    if (!hd && !pending && !knownType) continue;
    const about = play.about || {};
    const matchup = play.matchup || {};
    const batter = matchup.batter || {};
    const pitcher = matchup.pitcher || {};
    const atBat = about.atBatIndex ?? null;
    const playId = events.map(e => e && e.playId).filter(Boolean).pop() || '';
    let pendingText = 'Official Scorer Ruling Pending';
    if (pending) {
      const marker = pending.pendingEvents[0];
      const details = marker && marker.details || {};
      pendingText = details.description || details.event ||
        (pending.atResult ? result.description || result.event : '') || pendingText;
    }
    const row = {
      game_pk: pk, official_feed_url: source, at_bat: atBat,
      inning: about.inning, half: about.halfInning,
      event_type: eventType,
      official_call: pending || !eventType ? 'pending' : macroClass(eventType),
      status: pending ? 'official_scoring_pending' : eventType ? 'no_vector' : 'no_event_type_yet',
      official_scoring_pending: Boolean(pending),
      scoring_pending_kind: pending && pending.primary && pending.prior ? 'both'
        : pendingPrimary ? 'primary' : pendingPrior ? 'prior'
        : pending ? 'description_only' : '',
      scoring_pending_codes: pendingCodes.join(','),
      scoring_pending_text: pending ? pendingText : '',
      prediction_available: false, prediction_unavailable_reason: '',
      description: result.description || result.event || pendingText,
      batter: batter.fullName || '', pitcher: pitcher.fullName || '',
      away_score: result.awayScore ?? null, home_score: result.homeScore ?? null,
      play_id: playId,
      savant_url: playId ? `https://baseballsavant.mlb.com/sporty-videos?playId=${encodeURIComponent(playId)}` : '',
      source,
    };
    const eventModelEligible = !pending || pendingPrimary;
    if (!hd) {
      if (pending && pendingPrior && !pendingPrimary) {
        row.prediction_unavailable_reason = 'The exact pending marker is for a prior base-running event, not a plate-appearance ruling.';
      } else if (!pending && !eventType) {
        continue;
      } else {
        row.prediction_unavailable_reason = 'The feed has not supplied a Statcast hitData vector.';
      }
      rows.push(row);
      continue;
    }
    const required = ['launchSpeed', 'launchAngle', 'totalDistance'];
    if (!required.every(k => hd[k] !== undefined && hd[k] !== null && hd[k] !== '')) {
      row.prediction_unavailable_reason = 'The Statcast hitData vector is incomplete.';
      rows.push(row);
      continue;
    }
    if (eventType && !pending && !knownType) {
      row.prediction_unavailable_reason = 'This result.eventType is outside the represented training categories.';
      rows.push(row);
      continue;
    }
    if (!eventModelEligible) {
      row.prediction_unavailable_reason = 'The exact pending marker is for a prior base-running event, not a plate-appearance ruling.';
      rows.push(row);
      continue;
    }
    const bases = [...new Set((play.runners || []).map(r => (r && r.movement || {}).start)
      .filter(b => ['1B', '2B', '3B'].includes(b)))].sort();
    const outs = events.map(e => e && e.count && e.count.outs).find(v => v !== undefined && v !== null);
    const state = {
      ev: Number(hd.launchSpeed), la: Number(hd.launchAngle), dist: Number(hd.totalDistance),
      traj: hd.trajectory || '', hard: hd.hardness || '', bases,
      outs: outs === undefined ? 0 : Number(outs), inning: about.inning || 5,
      bat: (matchup.batSide || {}).code || '', pitch: (matchup.pitchHand || {}).code || '',
    };
    const score = evalModel(model, state);
    const probs = score.probs;
    const eventProbs = score.eventProbs || {};
    const runScored = (play.runners || []).some(r => r && r.movement && r.movement.end === 'score');
    // An empty object is truthy in JavaScript (it is falsy in the Python mirror), so an
    // absent reviewDetails must be normalised to null before `reviewed` is derived from it.
    const reviewCandidate = play.reviewDetails ||
      (events.find(e => e && e.reviewDetails) || {}).reviewDetails || null;
    const review = reviewCandidate && typeof reviewCandidate === 'object' &&
      Object.keys(reviewCandidate).length ? reviewCandidate : null;
    const ruled = Boolean(eventType) && !pending;
    row.status = pending ? 'official_scoring_pending' : ruled ? 'scored' : 'no_event_type_yet';
    Object.assign(row, {
      prediction_available: true, prediction_unavailable_reason: '',
      launch_speed: hd.launchSpeed, launch_angle: hd.launchAngle, distance: hd.totalDistance,
      trajectory: hd.trajectory || '', hardness: hd.hardness || '',
      score_100: Math.round(score.pErr * 10000) / 100,
      p_hit: Math.round(probs.hit * 10000) / 10000,
      p_error: Math.round(score.pErr * 10000) / 10000,
      p_error_binary: Math.round(score.pErr * 10000) / 10000,
      p_error_macro: Math.round(probs.error * 10000) / 10000,
      p_fielders_choice: Math.round(probs.fielders_choice * 10000) / 10000,
      p_out: Math.round(probs.out * 10000) / 10000,
      top_pick: score.top, top_prob: Math.round(probs[score.top] * 10000) / 10000,
      event_top_pick: score.eventTop,
      event_top_prob: score.eventTop ? Math.round(eventProbs[score.eventTop] * 10000) / 10000 : null,
      model_agrees_with_call: Number(ruled && ['hit', 'error', 'fielders_choice', 'out'].includes(macroClass(eventType))
        && score.top === macroClass(eventType)),
      runners_on: bases.join(',') || '-', risp: Number(bases.some(b => b === '2B' || b === '3B')),
      outs_before: state.outs, run_scored: Number(runScored), rbi_official: result.rbi ?? 0,
      reviewed: Number(review !== null),
      review_overturned: review && Object.hasOwn(review, 'isOverturned') ? review.isOverturned : '',
      review_type: review ? review.reviewType || '' : '',
    });
    Object.entries(eventProbs).forEach(([category, probability]) => {
      row[`event_p_${category}`] = Math.round(probability * 10000) / 10000;
    });
    if (row.run_scored) {
      row.rbi_if_error = rbiRuleNote('error', true, eventType, bases, state.outs);
      row.rbi_if_hit = rbiRuleNote('hit', true, eventType, bases, state.outs);
      row.rbi_if_fc = rbiRuleNote('fielders_choice', true, eventType, bases, state.outs);
    }
    rows.push(row);
  }
  return rows;
}

/* Where does this ball sit among the fitted batted balls? A 0-100 dial on a probability that never
   leaves the low single digits is misleading; the percentile is the honest scale. */
function errorPercentile(M, pErr) {
  const g = M.honesty && M.honesty.p_error_percentile_grid;
  if (!g || !g.length) return null;
  if (pErr <= g[0].p_error) return g[0].percentile;
  if (pErr >= g[g.length - 1].p_error) return g[g.length - 1].percentile;
  let lo = 0;
  for (let i = 0; i < g.length; i++) if (pErr >= g[i].p_error) lo = i;
  const a = g[lo], b = g[Math.min(lo + 1, g.length - 1)];
  if (a === b || b.p_error === a.p_error) return a.percentile;
  const t = (pErr - a.p_error) / (b.p_error - a.p_error);
  return Math.max(0, Math.min(100, Math.round(a.percentile + t * (b.percentile - a.percentile))));
}

const fmtPct = (v, d = 1) => (100 * v).toFixed(d) + '%';
const num = (v, d = 2) => (v === null || v === undefined || v === '') ? '–' : Number(v).toFixed(d);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const callName = c => ({ hit: 'hit', error: 'error', fielders_choice: "fielder's choice", out: 'out',
  pending: 'no call in feed yet' }[c] || c);

function downloadBlob(filename, text, type = 'text/csv') {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = filename; a.click(); URL.revokeObjectURL(a.href);
}
function downloadCSV(filename, rows) {
  if (!rows.length) return;
  const keys = [...new Set(rows.flatMap(Object.keys))];
  const q = v => { v = v == null ? '' : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  downloadBlob(filename, [keys.join(','), ...rows.map(r => keys.map(k => q(r[k])).join(','))].join('\n'));
}
function downloadJSON(filename, obj) {
  downloadBlob(filename, JSON.stringify(obj, null, 1), 'application/json');
}

/* Small renderers used by more than one page. */
function kpiCard(value, label) {
  return `<div class="card"><div class="kpi">${value}<small>${label}</small></div></div>`;
}
function flagBox(flags) {
  if (!flags || !flags.length) return '';
  return `<div class="warn"><b>${flags.length} flagged irregularit${flags.length === 1 ? 'y' : 'ies'}</b> —
    recorded by the fetch that produced this data, not smoothed over:<ul style="margin:6px 0 0">
    ${flags.slice(0, 12).map(f => `<li>${esc(f)}</li>`).join('')}</ul></div>`;
}
function linkOut(url, label) {
  return url ? `<a class="chip" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)} ↗</a>` : '';
}
