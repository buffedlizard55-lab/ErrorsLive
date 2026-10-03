/* ErrorsLive scoring adapter.
   The model card is shown only for an official field-error result or an exact
   primary official-scorer-pending ruling. Model estimates are not persisted as
   official observations and do not forecast whether an existing call changes. */
'use strict';
const ScoringModel = (() => {
  const rows = new Map(), capturedGames = new Set();
  let model = null, failure = '', generation = 0;
  const ready = fetch('../data/model.json', { signal: AbortSignal.timeout(10000) }).then(r => {
    if (!r.ok) throw new Error(`model HTTP ${r.status}`);
    return r.json();
  }).then(m => {
    if (!m.primary || !m.multiclass || !m.event_type_model) throw new Error('Invalid model structure');
    model = m;
  }).catch(e => { failure = e.message; });

  const HIT_TYPES = new Set(['single', 'double', 'triple', 'home_run']);
  const OUT_TYPES = new Set(['field_out', 'force_out', 'double_play', 'sac_fly', 'sac_bunt']);
  const CLASS_LABELS = {
    single: 'Single', double: 'Double', triple: 'Triple', home_run: 'Home run',
    error: 'Error', fielders_choice: "Fielder's choice", field_out: 'Field out',
    force_out: 'Force out', double_play: 'Double-play group', sac_fly: 'Sacrifice fly',
    sac_bunt: 'Sacrifice bunt',
  };
  const GROUP_LABELS = {
    hit: 'Hit', error: 'Error', fielders_choice: "Fielder's choice", out: 'Out / sacrifice',
  };

  function isPrimaryPending(row) {
    if (!row || row.official_scoring_pending !== true) return false;
    const codes = String(row.scoring_pending_codes || '').split(',').filter(Boolean);
    return codes.includes('os_ruling_pending_primary') ||
      row.scoring_pending_kind === 'primary' || row.scoring_pending_kind === 'both';
  }

  function isCurrentFieldError(row) {
    return Boolean(row && !isPrimaryPending(row) && row.event_type === 'field_error' &&
      ['scored', 'no_vector'].includes(row.status));
  }

  function matches(row, key) {
    const pending = isPrimaryPending(row);
    const error = isCurrentFieldError(row);
    if (key === 'errors') return error;
    if (key === 'pending_scoring') return pending;
    return error || pending;
  }

  function numberOrNull(value) {
    const n = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(n) ? n : null;
  }

  function outcomeEstimate(row) {
    const eventModel = model && model.event_type_model;
    const classes = eventModel && Array.isArray(eventModel.classes) ? eventModel.classes : [];
    const detailed = {};
    classes.forEach(code => {
      const probability = numberOrNull(row[`event_p_${code}`]);
      if (probability !== null) detailed[code] = probability;
    });
    if (!Object.keys(detailed).length) return null;

    const grouped = { hit: 0, error: 0, fielders_choice: 0, out: 0 };
    Object.entries(detailed).forEach(([code, probability]) => {
      const group = HIT_TYPES.has(code) ? 'hit'
        : code === 'error' ? 'error'
          : code === 'fielders_choice' ? 'fielders_choice'
            : OUT_TYPES.has(code) ? 'out' : null;
      if (group) grouped[group] += probability;
    });
    const topGroup = Object.keys(grouped).reduce((best, group) =>
      grouped[group] > grouped[best] ? group : best);
    const detailTop = row.event_top_pick && Object.hasOwn(detailed, row.event_top_pick)
      ? row.event_top_pick
      : Object.keys(detailed).reduce((best, code) =>
        detailed[code] > detailed[best] ? code : best);
    return { detailed, grouped, topGroup, detailTop };
  }

  async function capture(feed, game) {
    const token = generation;
    await ready;
    if (token !== generation) return;
    const plays = [...(feed.liveData.plays.allPlays || [])];
    const current = feed.liveData.plays.currentPlay;
    if (current && !plays.some(p => p.about?.atBatIndex === current.about?.atBatIndex)) plays.push(current);
    const source = `https://statsapi.mlb.com/api/v1.1/game/${game.gamePk}/feed/live`;
    const input = { ...feed, liveData: { ...feed.liveData, plays: { allPlays: plays } } };
    if (!model) input.liveData.plays.allPlays = plays.map(p => ({
      ...p, playEvents: (p.playEvents || []).map(e => ({ ...e, hitData: undefined })),
    }));
    const scored = scoreLiveFeed(input, { gamePk: game.gamePk, official_feed_url: source }, model || {});

    // Keep current model rows only; the official feed ledger separately keeps
    // observed scorer rulings and any initial-to-final reclassification.
    capturedGames.add(game.gamePk);
    const previous = new Map(rows);
    for (const [key, entry] of rows) if (entry.gamePk === game.gamePk) rows.delete(key);
    for (const row of scored) {
      if (!Number.isInteger(row.at_bat)) continue;
      const play = plays.find(p => p.about?.atBatIndex === row.at_bat);
      const key = `${game.gamePk}:${row.at_bat}`;
      const hd = play && play.playEvents && play.playEvents.find(e => e.hitData)?.hitData;
      if (!model || (hd && !['launchSpeed', 'launchAngle', 'totalDistance'].every(k =>
        hd[k] !== null && hd[k] !== '' && Number.isFinite(Number(hd[k]))))) {
        row.prediction_available = false;
        row.prediction_unavailable_reason = !model
          ? `Model unavailable: ${failure}` : 'Invalid or incomplete Statcast measurements.';
      }
      row.runner_error = (play.runners || []).some(r => ['error', 'field_error'].includes(r.details?.eventType));
      row.is_out = play.result?.isOut;
      rows.set(key, {
        gamePk: game.gamePk, modelRow: row, game, observedAt: new Date().toISOString(),
        firstSeen: previous.get(key)?.firstSeen || Date.now(),
        review: { timestamp: play.about?.endTime || play.about?.startTime || null, typeKey: 'batted_ball' },
      });
    }
  }

  function node(tag, text, cls) {
    const n = document.createElement(tag);
    n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }

  function panel(entry) {
    const box = node('section', '', 'model-panel');
    if (!entry) {
      box.append(node('strong', 'Model estimate unavailable'));
      box.append(node('p', 'No current model capture is available for this play.'));
      return box;
    }
    const r = entry.modelRow;
    const pending = isPrimaryPending(r);
    box.append(node('strong', pending ? 'Model estimate · official scoring pending' : 'Model estimate · current call is Error'));
    if (!r.prediction_available) {
      box.append(node('p', `No score — ${r.prediction_unavailable_reason || 'model inputs missing'}`));
      return box;
    }
    const estimate = outcomeEstimate(r);
    if (!estimate) {
      box.append(node('p', 'No outcome distribution is available for this capture.'));
      return box;
    }

    const hitScore = estimate.grouped.hit;
    const topGroup = estimate.topGroup;
    const headline = pending
      ? `${GROUP_LABELS[topGroup]} · ${(100 * estimate.grouped[topGroup]).toFixed(1)} / 100`
      : `Hit outcome score · ${(100 * hitScore).toFixed(1)} / 100`;
    box.append(node('div', headline, 'model-score-headline'));
    if (!pending) {
      box.append(node('p', `Current official feed call: Error. The model estimates ${(100 * hitScore).toFixed(1)}% for a final hit class and ${(100 * estimate.grouped.error).toFixed(1)}% for Error.`));
      box.append(node('p', 'This is not a probability that the scorer will change the current Error call. The model was trained on final captured labels, not error-to-hit transitions.'));
    } else {
      const exactLabel = CLASS_LABELS[estimate.detailTop] || estimate.detailTop.replaceAll('_', ' ');
      box.append(node('p', `Most likely individual model class: ${exactLabel} · ${(100 * estimate.detailed[estimate.detailTop]).toFixed(1)}%.`));
      box.append(node('p', 'This is an estimate from the current batted-ball data, not the official ruling.'));
    }

    const groups = node('div', '', 'model-probabilities');
    ['hit', 'error', 'fielders_choice', 'out'].forEach(group => {
      groups.append(node('span', `${GROUP_LABELS[group]} ${(100 * estimate.grouped[group]).toFixed(1)}%`,
        group === topGroup ? 'model-probability-top' : ''));
    });
    box.append(groups);

    const details = node('details', '', 'model-detail-list');
    details.append(node('summary', 'Show all modeled outcomes'));
    const list = node('div', '', 'model-probabilities model-probabilities-detail');
    Object.entries(estimate.detailed).sort((a, b) => b[1] - a[1]).forEach(([code, probability]) => {
      const label = CLASS_LABELS[code] || code.replaceAll('_', ' ');
      list.append(node('span', `${label} ${(100 * probability).toFixed(1)}%`));
    });
    details.append(list);
    box.append(details);
    box.append(node('p', 'Experimental estimate; model audit is unresolved. Missing measurements produce no score.'));
    return box;
  }

  function render(entry) {
    const r = entry.modelRow, g = entry.game;
    const pending = isPrimaryPending(r);
    const box = node('article', '', 'feed-row model-event');
    const time = entry.review.timestamp;
    box.append(node('div', time
      ? new Date(time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : `First seen ${new Date(entry.firstSeen).toLocaleTimeString()}`, 'feed-time'));
    const body = node('div', '', 'feed-body');
    const away = g.teams?.away?.team?.name || 'Away';
    const home = g.teams?.home?.team?.name || 'Home';
    const link = node('a', `${away} @ ${home}`, 'feed-game');
    link.href = `game.html?gamePk=${entry.gamePk}`;
    body.append(link);
    body.append(node('h3', `${r.half || ''} ${r.inning ?? '—'} · ${pending ? 'Official scoring pending' : 'Official feed call: Error'}`));
    body.append(node('p', r.description || 'No play description supplied by the official feed.'));
    body.append(node('p', `Official score after play: ${r.away_score ?? '—'}–${r.home_score ?? '—'} · Batter: ${r.batter || '—'} · Pitcher: ${r.pitcher || '—'}`));
    body.append(panel(entry));
    const source = node('a', 'Official MLB feed ↗');
    source.href = r.source;
    source.target = '_blank';
    source.rel = 'noopener';
    body.append(source);
    if (r.savant_url) {
      const video = node('a', 'Video ↗');
      video.href = r.savant_url;
      video.target = '_blank';
      video.rel = 'noopener';
      body.append(video);
    }
    box.append(body);
    return box;
  }

  return {
    capture, panel, render, isPrimaryPending, isCurrentFieldError, outcomeEstimate,
    clear() { generation++; rows.clear(); capturedGames.clear(); },
    hasGame(pk) { return capturedGames.has(pk); },
    get(pk, atBat) { return rows.get(`${pk}:${atBat}`); },
    entries(key = 'all') { return [...rows.values()].filter(e => matches(e.modelRow, key)); },
  };
})();
