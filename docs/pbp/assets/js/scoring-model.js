/* ErrorsLive adapter. Official review tracking remains in the upstream modules.
   Model rows are deliberately not persisted as observations of official rulings. */
'use strict';
const ScoringModel = (() => {
  const rows = new Map(), capturedGames = new Set();
  let model = null, failure = '', generation = 0;
  const ready = fetch('../data/model.json', { signal: AbortSignal.timeout(10000) }).then(r => {
    if (!r.ok) throw new Error(`model HTTP ${r.status}`);
    return r.json();
  }).then(m => { if (!m.primary || !m.multiclass || !m.event_type_model) throw new Error('Invalid model structure'); model = m; }).catch(e => { failure = e.message; });
  const categories = [
    ['batted_ball', '⚾ Batted balls'], ['hits', 'Hits'], ['errors', 'Errors'],
    ['outs', 'Outs'], ['fc', "Fielder’s choice"], ['sacrifices', 'Sacrifices'],
  ];
  function matches(row, key) {
    const code = row.event_type || '';
    if (key === 'all' || key === 'batted_ball') return true;
    if (key === 'hits') return ['single', 'double', 'triple', 'home_run'].includes(code);
    if (key === 'errors') return code === 'field_error' || row.runner_error;
    if (key === 'fc') return code.startsWith('fielders_choice');
    if (key === 'sacrifices') return code.startsWith('sac_');
    if (key === 'outs') return row.is_out === true;
    return false;
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
    if (!model) input.liveData.plays.allPlays = plays.map(p => ({ ...p, playEvents: (p.playEvents || []).map(e => ({ ...e, hitData: undefined })) }));
    const scored = scoreLiveFeed(input, { gamePk: game.gamePk, official_feed_url: source }, model || {});
    // Replace this game's current rows; do not keep vanished plays as current data.
    capturedGames.add(game.gamePk);
    const previous = new Map(rows);
    for (const [key, entry] of rows) if (entry.gamePk === game.gamePk) rows.delete(key);
    for (const row of scored) {
      if (!Number.isInteger(row.at_bat)) continue;
      const play = plays.find(p => p.about?.atBatIndex === row.at_bat);
      const key = `${game.gamePk}:${row.at_bat}`;
      const hd = play.playEvents?.find(e => e.hitData)?.hitData;
      if (!model || (hd && !['launchSpeed','launchAngle','totalDistance'].every(k =>
        hd[k] !== null && hd[k] !== '' && Number.isFinite(Number(hd[k]))))) {
        row.prediction_available = false;
        row.prediction_unavailable_reason = !model ? `Model unavailable: ${failure}` : 'Invalid or incomplete Statcast measurements.';
      }
      row.runner_error = (play.runners || []).some(r => ['error','field_error'].includes(r.details?.eventType));
      row.is_out = play.result?.isOut;
      rows.set(key, { gamePk: game.gamePk, modelRow: row, game, observedAt: new Date().toISOString(),
        firstSeen: previous.get(key)?.firstSeen || Date.now(),
        review: { timestamp: play.about?.endTime || play.about?.startTime || null, typeKey: 'batted_ball' } });
    }
  }
  function node(tag, text, cls) {
    const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n;
  }
  function panel(entry) {
    const box = node('section', '', 'model-panel');
    if (!entry) { box.append(node('p', 'Model score unavailable — no current batted-ball capture for this play.')); return box; }
    const r = entry.modelRow;
    box.append(node('strong', 'Experimental model · validation audit unresolved'));
    box.append(node('p', 'The repository baseline has 26 failing model/data/RBI audit checks. These estimates are not verified calibrated probabilities. Do not treat them as official rulings.'));
    box.append(node('p', `Inputs observed ${new Date(entry.observedAt).toLocaleString()} · current capture estimate, not a saved pre-ruling forecast.`));
    if (!r.prediction_available) {
      box.append(node('p', `Score unavailable — ${r.prediction_unavailable_reason || 'model inputs missing'}`)); return box;
    }
    box.append(node('p', `Error review score: ${Number(r.score_100).toFixed(2)} / 100 · estimated P(final captured label = error). Not P(ruling overturned).`));
    const list = node('div', '', 'model-probabilities');
    Object.entries(r).filter(([k,v]) => k.startsWith('event_p_') && Number.isFinite(v))
      .sort((a,b) => b[1]-a[1]).forEach(([k,v]) => {
        list.append(node('span', `${k.slice(8).replaceAll('_',' ')} ${(v*100).toFixed(1)}%`));
      });
    box.append(list);
    box.append(node('p', '11-outcome estimates and the binary error score are separate model heads. The model never selected error as its top multiclass outcome in held-out evaluation; use for review ranking only.'));
    return box;
  }
  function render(entry) {
    const r = entry.modelRow, g = entry.game;
    const box = node('article', '', 'feed-row model-event');
    const time = entry.review.timestamp;
    box.append(node('div', time ? new Date(time).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})
      : `First seen ${new Date(entry.firstSeen).toLocaleTimeString()}`, 'feed-time'));
    const body = node('div', '', 'feed-body');
    const link = node('a', `${g.teams?.away?.team?.name || 'Away'} @ ${g.teams?.home?.team?.name || 'Home'}`, 'feed-game');
    link.href = `game.html?gamePk=${entry.gamePk}`; body.append(link);
    body.append(node('h3', `${r.half || ''} ${r.inning ?? '—'} · ${r.official_scoring_pending ? `Pending marker observed · current classification: ${r.event_type || 'not supplied'}` : r.event_type || 'No official classification yet'}`));
    body.append(node('p', r.description));
    body.append(node('p', `Official score after play: ${r.away_score ?? '—'}–${r.home_score ?? '—'} · Batter: ${r.batter} · Pitcher: ${r.pitcher}`));
    body.append(panel(entry));
    const source = node('a', 'Official feed ↗'); source.href = r.source; source.target = '_blank'; source.rel = 'noopener'; body.append(source);
    box.append(body); return box;
  }
  return { capture, panel, render, categories, clear() { generation++; rows.clear(); capturedGames.clear(); }, hasGame(pk) { return capturedGames.has(pk); },
    get(pk, atBat) { return rows.get(`${pk}:${atBat}`); },
    entries(key = 'all') { return [...rows.values()].filter(e => matches(e.modelRow, key)); } };
})();
