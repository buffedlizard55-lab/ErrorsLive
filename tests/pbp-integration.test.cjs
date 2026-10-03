/* Integration tests for the SIMPLIFIED ErrorsLive scoring watch
 * (docs/pbp/index.html scoreboard + docs/pbp/reviews.html scoring feed).
 *
 * What is verified here, line by line:
 *   1. ScoringWatch pure logic: the hit score aggregates exactly the model's
 *      single+double+triple+home_run classes; final eventTypes map to
 *      categories with the model's own event_type_groups; verdicts are only
 *      issued from a pre-ruling snapshot; out-of-scope finals get no verdict.
 *   2. The feed page boots against the committed official capture
 *      (data/source/feed_823441.json) and renders ONLY scoring rows: the
 *      field_error play appears with a score; the seven plays carrying
 *      reviewDetails and every ordinary batted ball do NOT appear.
 *   3. A synthetic pending → resolved cycle (explicitly labelled synthetic):
 *      the pending marker scores while undecided; after the ruling lands the
 *      row shows the verdict from the snapshot the ledger stored earlier, and
 *      the day tally matches the ledger.
 *   4. When the model file cannot be loaded, rows still render — without
 *      invented numbers.
 *   5. The scoreboard page boots and shows the per-game error badge from the
 *      same capture.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const read = p => fs.readFileSync(path.join(root, p), 'utf8');
const fixture = JSON.parse(read('data/source/feed_823441.json'));
const model = JSON.parse(read('docs/data/model.json'));
const copy = v => JSON.parse(JSON.stringify(v));

const PAGE_SCRIPTS = ['docs/site.js', 'docs/scoring-feed.js', 'docs/live-board.js',
  'docs/pbp/assets/js/scoring-watch.js'];

/* Build a jsdom around one of the simplified pages, mock fetch, run the shared
   scripts and (optionally) the page script. */
function setup(page, { modelOk = true } = {}) {
  const html = page ? read(page) : '<!DOCTYPE html><html><body></body></html>';
  const dom = new JSDOM(html, {
    url: `https://example.test/ErrorsLive/docs/${page ? page.split('docs/')[1] : 'index.html'}?date=2026-07-18`,
    runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window;
  w.AbortSignal = AbortSignal;
  let currentFeed = fixture;
  w.fetch = async url => {
    url = String(url);
    if (url.includes('model.json')) {
      if (!modelOk) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => copy(model) };
    }
    if (url.includes('/schedule')) {
      return { ok: true, status: 200, json: async () => ({ dates: [{ date: '2026-07-18', games: [{
        gamePk: 823441,
        gameDate: '2026-07-18T23:00:00Z',
        officialDate: '2026-07-18T23:00:00Z',
        teams: {
          away: { team: { id: 141, name: 'Away Names' }, leagueRecord: { wins: 1, losses: 0 }, score: 3 },
          home: { team: { id: 143, name: 'Home Names' }, leagueRecord: { wins: 0, losses: 1 }, score: 2 },
        },
        status: { abstractGameState: 'Live', detailedState: 'In Progress', codedGameState: 'I', statusCode: 'I' },
        linescore: { currentInning: 7, inningState: 'Top', teams: { away: { hits: 5, errors: 1 }, home: { hits: 3, errors: 0 } } },
        venue: { name: 'Test Park' },
      }] }] }) };
    }
    if (url.includes('/feed/live')) {
      return { ok: true, status: 200, json: async () => copy(currentFeed) };
    }
    throw new Error(`Unexpected request ${url}`);
  };
  for (const file of PAGE_SCRIPTS)
    vm.runInContext(read(file), dom.getInternalVMContext(), { filename: file });
  if (page) vm.runInContext(read(page.replace('.html', '').endsWith('reviews')
    ? 'docs/pbp/assets/js/scoring-feed-page.js' : 'docs/pbp/assets/js/scoring-scoreboard-page.js'),
    dom.getInternalVMContext(), { filename: 'page.js' });
  return { dom, w, setFeed: f => { currentFeed = f; } };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ------------------------------------------------------------------ 1 */
test('hit score aggregates exactly single+double+triple+home_run; groups match the committed model', () => {
  const { dom, w } = setup(null);
  const SW = vm.runInContext('ScoringWatch', dom.getInternalVMContext());
  const probs = { single: 0.10, double: 0.05, triple: 0.01, home_run: 0.04,
    error: 0.30, fielders_choice: 0.08, field_out: 0.30, force_out: 0.07,
    double_play: 0.04, sac_fly: 0.005, sac_bunt: 0.005 };
  const dist = SW.distribution(probs);
  const hit = dist.groups.find(g => g.key === 'hit');
  assert.equal(hit.p.toFixed(6), '0.200000');
  assert.equal(dist.groups.find(g => g.key === 'out').p.toFixed(6), '0.410000');
  assert.equal(dist.groups.find(g => g.key === 'sac_bunt').p.toFixed(6), '0.005000');
  const event = { prediction_available: true, event_probs: probs };
  assert.equal(SW.hitScore(event).toFixed(6), '0.200000');
  assert.equal(SW.hitScore({ prediction_available: false, event_probs: probs }), null);
  // final eventType mapping follows the model's own event_type_groups
  assert.equal(SW.categoryOfEvent('fielders_choice_out'), 'fc');
  assert.equal(SW.categoryOfEvent('grounded_into_double_play'), 'out');
  assert.equal(SW.categoryOfEvent('field_error'), 'error');
  assert.equal(SW.categoryOfEvent('single'), 'hit');
  assert.equal(SW.categoryOfEvent('catcher_interf'), 'other');
  assert.equal(SW.topGroup(probs).key, 'out');
  dom.window.close();
});

test('verdicts: scored only from a snapshot; unknown scope and missing snapshots never invented', () => {
  const { dom, w } = setup(null);
  const SW = vm.runInContext('ScoringWatch', dom.getInternalVMContext());
  assert.equal(SW.verdictForPending(null), null);
  assert.equal(SW.verdictForPending({ status: 'pending' }), null);
  // resolved with a snapshot: correct/incorrect from the stored pre-ruling pick
  const snap = { event_p_single: 0.5, event_p_double: 0.1, event_p_triple: 0, event_p_home_run: 0,
    event_p_error: 0.1, event_p_fielders_choice: 0.05, event_p_field_out: 0.2, event_p_force_out: 0.03,
    event_p_double_play: 0.02, event_p_sac_fly: 0, event_p_sac_bunt: 0 };
  const good = SW.verdictForPending({ status: 'resolved', resolved_event_type: 'single', prediction_snapshot: snap });
  assert.equal(good.kind, 'scored');
  assert.equal(good.called, 'hit');
  assert.equal(good.correct, true);
  assert.equal(good.saidHit, true && good.wasHit, 'saidHit/wasHit consistent for a hit final');
  const bad = SW.verdictForPending({ status: 'resolved', resolved_event_type: 'field_error', prediction_snapshot: snap });
  assert.equal(bad.correct, false);
  assert.equal(bad.saidHit, true);
  assert.equal(bad.wasHit, false);
  // out-of-scope final: no verdict, honest label
  const other = SW.verdictForPending({ status: 'resolved', resolved_event_type: 'catcher_interf', prediction_snapshot: snap });
  assert.equal(other.kind, 'out-of-scope');
  // resolved but no snapshot captured: explicitly not a verdict
  const noSnap = SW.verdictForPending({ status: 'resolved', resolved_event_type: 'single' });
  assert.equal(noSnap.kind, 'no-snapshot');
  // tally counts only scored verdicts
  const tally = SW.dayTally([
    { status: 'resolved', resolved_event_type: 'single', prediction_snapshot: snap },
    { status: 'resolved', resolved_event_type: 'field_error', prediction_snapshot: snap },
    { status: 'resolved', resolved_event_type: 'single' },
    { status: 'pending' },
  ]);
  assert.equal(tally.decided, 2);
  assert.equal(tally.topCorrect, 1);
  assert.equal(tally.saidHit, 2);
  assert.equal(tally.hitWhenSaid, 1);
  dom.window.close();
});

/* ------------------------------------------------------------------ 2 */
test('feed page renders only scoring rows from the committed capture (error yes, reviews/ordinary BIP no)', async () => {
  const { dom, w } = setup('docs/pbp/reviews.html');
  await sleep(500);
  const rows = [...w.document.querySelectorAll('.sw-row')];
  assert.ok(rows.length > 0, 'at least one scoring row renders');
  const errorRows = rows.filter(r => r.querySelector('.sw-b-error'));
  assert.equal(errorRows.length, 1, 'exactly the one field_error play renders as an error row');
  assert.match(errorRows[0].textContent, /A\.J\. Ewing reaches on a fielding error/);
  const score = errorRows[0].querySelector('.sw-num');
  assert.ok(score && /^\d+$/.test(score.textContent), `error row carries a numeric HIT score, got: ${score && score.textContent}`);
  // none of the seven review-carrying plays leak in as review rows…
  assert.equal(rows.filter(r => /Manager challenge|ABS|Boundary/i.test(r.textContent)).length, 0);
  // …and no ordinary batted ball (single/out) row renders
  assert.ok(rows.every(r => r.querySelector('.sw-badge')), 'every rendered row is a scored/badge row');
  // tabs exist with the four simplified filters
  const tabs = [...w.document.querySelectorAll('#feed-tabs .tab')].map(b => b.textContent);
  assert.equal(tabs.length, 4);
  assert.ok(tabs[0].startsWith('All'), `first tab is All, got ${tabs[0]}`);
  // summary shows the error count
  assert.match(w.document.querySelector('#feed-stats').textContent, /errors on board:\s*1/);
  dom.window.close();
});

/* ------------------------------------------------------------------ 3 */
test('synthetic pending ruling scores live, then resolves with the pre-ruling verdict and tally', async () => {
  const { dom, w, setFeed } = setup('docs/pbp/reviews.html');
  const template = fixture.liveData.plays.allPlays.find(p =>
    p.playEvents.some(e => e.hitData && e.hitData.launchSpeed != null));
  const pendingFeed = { liveData: { plays: { allPlays: [] } } };
  const play = copy(template);
  play.about.atBatIndex = 0;
  play.about.isComplete = true;
  play.about.inning = 7;
  play.about.halfInning = 'bottom';
  play.matchup = { batter: { fullName: 'Test Batter' }, pitcher: { fullName: 'Test Pitcher' } };
  play.result = { eventType: '', description: 'Ground ball to short. Official Scorer Ruling Pending.',
    awayScore: 1, homeScore: 0, rbi: 0, isOut: false };
  play.runners = play.runners || [];
  pendingFeed.liveData.plays.allPlays = [play];
  pendingFeed.liveData.plays.allPlays[0].playEvents.push({
    index: 999, details: { event: 'Official Scorer Ruling Pending',
      eventType: 'os_ruling_pending_primary', description: 'Official Scorer Ruling Pending' },
  });
  setFeed(pendingFeed);
  await sleep(500);
  let rows = [...w.document.querySelectorAll('.sw-row')];
  assert.equal(rows.length, 1, 'the pending play is the only row');
  assert.ok(rows[0].querySelector('.sw-b-pending'), 'pending badge shown');
  assert.ok(rows[0].querySelector('.sw-num'), 'pending row is scored while undecided');
  const SW = vm.runInContext('ScoringWatch', dom.getInternalVMContext());

  // resolve the ruling: a single. Same play, marker gone, classification present.
  play.result = { eventType: 'single', description: 'Reaches on a ground ball single to short.',
    awayScore: 1, homeScore: 0, rbi: 0, isOut: false };
  play.playEvents = play.playEvents.filter(e => e.index !== 999);
  await w.ScoringFeedPage.refresh('test-resolve');
  rows = [...w.document.querySelectorAll('.sw-row')];
  assert.equal(rows.length, 1);
  assert.ok(rows[0].querySelector('.sw-b-resolved'), 'resolved badge shown');
  const ledgerRow = w.ScoringFeedPage.state.LEDGER.pending[0];
  assert.equal(ledgerRow.status, 'resolved');
  assert.equal(ledgerRow.resolved_event_type, 'single');
  assert.ok(ledgerRow.prediction_snapshot, 'the ledger kept the pre-ruling snapshot');
  const v = SW.verdictForPending(ledgerRow);
  assert.equal(v.kind, 'scored');
  assert.match(rows[0].textContent, v.correct ? /✓/ : /✗/);
  assert.match(rows[0].textContent, /Ruling landed/i);
  assert.match(rows[0].textContent, new RegExp(`model lean ${SW.groupShort(v.called)}`, 'i'));
  // the day tally matches the ledger exactly
  const tally = SW.dayTally(w.ScoringFeedPage.state.LEDGER.pending);
  assert.match(w.document.querySelector('#feed-stats').textContent,
    new RegExp(`model lean correct:\\s*${tally.topCorrect}/${tally.decided}`));
  dom.window.close();
});

/* ------------------------------------------------------------------ 4 */
test('model file unavailable: rows still render, no numbers invented', async () => {
  const { dom, w } = setup('docs/pbp/reviews.html', { modelOk: false });
  await sleep(500);
  const rows = [...w.document.querySelectorAll('.sw-row')];
  assert.ok(rows.length > 0, 'official rows remain visible');
  const errorRows = rows.filter(r => r.querySelector('.sw-b-error'));
  assert.ok(errorRows.length >= 1);
  assert.match(errorRows[0].textContent, /Score unavailable/);
  assert.equal(errorRows[0].querySelector('.sw-num'), null, 'no fabricated score');
  dom.window.close();
});

/* ------------------------------------------------------------------ 5 */
test('scoreboard page boots: one card, error badge from the same capture', async () => {
  const { dom, w } = setup('docs/pbp/index.html');
  await sleep(500);
  const cards = [...w.document.querySelectorAll('.sw-card')];
  assert.equal(cards.length, 1);
  assert.match(cards[0].textContent, /Away Names|Home Names/);
  assert.ok(cards[0].querySelector('.sw-mini-error'), 'error badge visible');
  assert.match(w.document.querySelector('#feed-stats').textContent, /errors on board:\s*1/);
  const feedLinks = cards[0].querySelectorAll('a[href*="reviews.html"]');
  assert.ok(feedLinks.length >= 2, 'card links into the scoring feed');
  dom.window.close();
});
