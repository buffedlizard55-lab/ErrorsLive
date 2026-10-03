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
const game = {
  gamePk: 823441,
  teams: { away: { team: { name: 'Away' } }, home: { team: { name: 'Home' } } },
  status: { abstractGameState: 'Final' },
};
const copy = value => JSON.parse(JSON.stringify(value));

function setup({ failed = false } = {}) {
  const dom = new JSDOM(read('docs/pbp/reviews.html'), {
    url: 'https://example.test/ErrorsLive/docs/pbp/reviews.html?date=2026-07-18',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  w.AbortSignal = AbortSignal;
  w.fetch = async () => ({
    ok: !failed,
    status: failed ? 503 : 200,
    json: async () => model,
  });
  for (const file of [
    'docs/site.js',
    'docs/scoring-feed.js',
    'docs/pbp/assets/js/scoring-model.js',
  ]) {
    vm.runInContext(read(file), dom.getInternalVMContext(), { filename: file });
  }
  return {
    dom,
    w,
    context: dom.getInternalVMContext(),
    adapter: vm.runInContext('ScoringModel', dom.getInternalVMContext()),
  };
}

function loadFeedPolicy() {
  const module = { exports: {} };
  const context = vm.createContext({
    module,
    window: {},
    document: { addEventListener() {} },
    localStorage: null,
    console,
    URL,
    Map,
    Set,
    Date,
    Math,
    Number,
    String,
    Object,
    Array,
    RegExp,
    Promise,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  });
  vm.runInContext(read('docs/pbp/assets/js/reviews-feed.js'), context, {
    filename: 'docs/pbp/assets/js/reviews-feed.js',
  });
  return module.exports;
}

function marker(code, index) {
  return {
    details: {
      description: 'Official Scorer Ruling Pending',
      event: 'Official Scorer Ruling Pending',
      eventType: code,
    },
    index,
    isPitch: false,
    type: 'action',
  };
}

function playWithType(template, eventType, atBatIndex = 100) {
  const play = copy(template);
  play.about.atBatIndex = atBatIndex;
  play.about.isComplete = true;
  play.about.hasReview = false;
  play.result = {
    ...play.result,
    eventType,
    event: eventType,
    description: eventType,
    isOut: eventType === 'field_out' || eventType === 'force_out',
  };
  return play;
}

function addPageScripts(dom) {
  for (const file of ['api', 'ui', 'reviews', 'feed-log', 'reviews-feed']) {
    vm.runInContext(read(`docs/pbp/assets/js/${file}.js`), dom.getInternalVMContext(), { filename: file });
  }
}

function mockFeedRequests(w, scheduleGame, getPlays) {
  w.fetch = async url => {
    const value = String(url);
    if (value.includes('/schedule')) return {
      ok: true, status: 200, json: async () => ({ dates: [{ date: '2026-07-18', games: [scheduleGame] }] }),
    };
    if (value.includes('/teams')) return {
      ok: true, status: 200, json: async () => ({ teams: [] }),
    };
    if (value.includes('/playByPlay')) return {
      ok: true, status: 200, json: async () => getPlays(),
    };
    if (value.includes('/gameStatus')) {
      assert.fail('focused feed must not start the separate replay-status watcher');
    }
    assert.fail(`Unexpected request ${value}`);
  };
}

test('actual fixture: only the official field error is listed; its score is not rescore odds', async () => {
  const { dom, adapter } = setup();
  await adapter.capture(fixture, game);

  const entries = adapter.entries();
  assert.equal(entries.length, 1, 'ordinary hits and outs are not model-feed rows');
  assert.equal(entries[0].modelRow.event_type, 'field_error');
  assert.equal(adapter.entries('errors').length, 1);
  assert.equal(adapter.entries('pending_scoring').length, 0);

  const expected = vm.runInContext(
    `scoreLiveFeed(${JSON.stringify(fixture)}, ${JSON.stringify(game)}, ${JSON.stringify(model)})`,
    dom.getInternalVMContext(),
  );
  const row = entries[0].modelRow;
  const shared = expected.find(candidate => candidate.at_bat === row.at_bat);
  assert.ok(shared);
  assert.equal(row.score_100, shared.score_100);
  for (const code of model.event_type_model.classes) {
    assert.equal(row[`event_p_${code}`], shared[`event_p_${code}`]);
  }

  const panel = adapter.panel(entries[0]);
  assert.equal(panel.querySelector('.model-probabilities').querySelectorAll('span').length, 4,
    'the compact view shows four grouped outcomes');
  assert.equal(panel.querySelector('.model-probabilities-detail').querySelectorAll('span').length,
    model.event_type_model.classes.length, 'all detailed classes remain available on demand');
  const rendered = adapter.render(entries[0]);
  assert.match(rendered.textContent, /Hit outcome score/);
  assert.match(rendered.textContent, /not a probability that the scorer will change the current Error call/i);
  assert.doesNotMatch(rendered.textContent, /probability that the error will be rescored/i);

  await adapter.capture(fixture, game);
  assert.equal(adapter.entries().length, 1, 'repeated polling replaces, not duplicates, model rows');
  adapter.clear();
  assert.equal(adapter.entries().length, 0);
  dom.window.close();
});

test('current errors remain visible without a usable Statcast vector', async () => {
  const { dom, adapter } = setup();
  const feed = copy(fixture);
  const errorPlay = feed.liveData.plays.allPlays.find(p => p.result?.eventType === 'field_error');
  assert.ok(errorPlay);
  errorPlay.playEvents = [];

  await adapter.capture(feed, game);
  const errors = adapter.entries('errors');
  assert.equal(errors.length, 1, 'official field_error remains visible without hitData');
  assert.equal(errors[0].modelRow.prediction_available, false);
  assert.match(adapter.panel(errors[0]).textContent, /No score/);
  dom.window.close();
});

test('only the exact primary scorer-pending marker is eligible for this plate-appearance feed', async () => {
  const { dom, adapter } = setup();
  const feedPolicy = loadFeedPolicy();
  const feed = copy(fixture);
  const play = feed.liveData.plays.allPlays.find(p => p.result?.eventType === 'field_error');
  assert.ok(play);
  play.playEvents.push(marker('os_ruling_pending_primary', play.playEvents.length));
  play.result.description = '<img src=x onerror=alert(1)>';
  await adapter.capture(feed, game);

  const pending = adapter.entries('pending_scoring');
  assert.equal(pending.length, 1);
  assert.equal(adapter.entries('errors').length, 0,
    'a pending primary ruling takes precedence over its provisional result.eventType');
  assert.equal(pending[0].modelRow.scoring_pending_kind, 'primary');
  assert.equal(pending[0].modelRow.prediction_available, true);
  const rendered = adapter.render(pending[0]);
  assert.equal(rendered.querySelector('img'), null, 'official text is inserted as text, not markup');
  assert.match(rendered.textContent, /Official scoring pending/);
  assert.match(rendered.textContent, /not the official ruling/i);

  const primaryReview = {
    typeKey: 'pending_scoring',
    pendingCodes: ['os_ruling_pending_primary'],
    inProgress: true,
  };
  assert.equal(feedPolicy.isPrimaryPendingReview(primaryReview), true);
  assert.equal(feedPolicy.visibleInAllFeed(primaryReview), true);

  play.playEvents = [marker('os_ruling_pending_prior', 0)];
  await adapter.capture(feed, game);
  assert.equal(adapter.entries().length, 0, 'prior base-running rulings are outside this scope');
  assert.equal(feedPolicy.isPrimaryPendingReview({
    typeKey: 'pending_scoring', pendingCodes: ['os_ruling_pending_prior'], inProgress: true,
  }), false);

  play.playEvents = [{ details: { description: 'Official Scorer Ruling Pending' }, index: 0, type: 'action' }];
  await adapter.capture(feed, game);
  assert.equal(adapter.entries().length, 0, 'a text-only or missing marker is not treated as primary');

  play.playEvents = [];
  play.result.eventType = '';
  await adapter.capture(feed, game);
  assert.equal(adapter.entries().length, 0, 'a blank result.eventType by itself is not pending');
  dom.window.close();
});

test('error-change filter accepts observed transitions involving Error, not unrelated reclassifications', () => {
  const { dom } = setup();
  const feedPolicy = loadFeedPolicy();
  const fixturePlay = fixture.liveData.plays.allPlays.find(p => p.playEvents?.some(e => e.hitData));
  assert.ok(fixturePlay);
  const first = playWithType(fixturePlay, 'single', 90);
  const baseline = feedPolicy.mergeScoringChanges(823441, [first], new Map(), 1000, {});
  assert.equal(baseline.added.length, 0, 'a first observation is only a baseline');

  const unrelated = playWithType(first, 'double', 90);
  const hitToHit = feedPolicy.mergeScoringChanges(823441, [unrelated], baseline.snapshots, 2000, {});
  assert.equal(hitToHit.added.length, 1, 'the tracker records observed classification changes');
  assert.equal(feedPolicy.isErrorScoringChange(hitToHit.added[0].review), false,
    'a hit-to-hit change is not part of the focused feed');

  const error = playWithType(first, 'field_error', 90);
  const hitToError = feedPolicy.mergeScoringChanges(823441, [error], baseline.snapshots, 3000, {});
  assert.equal(hitToError.added.length, 1);
  assert.equal(hitToError.added[0].review.initial.category, 'hit');
  assert.equal(hitToError.added[0].review.final.category, 'error');
  assert.equal(feedPolicy.isErrorScoringChange(hitToError.added[0].review), true);
  assert.equal(feedPolicy.isScoringFocusReview(hitToError.added[0].review), true);

  const manager = { typeKey: 'manager', inProgress: true };
  const abs = { typeKey: 'abs', inProgress: true };
  const priorPending = { typeKey: 'pending_scoring', pendingCodes: ['os_ruling_pending_prior'] };
  assert.equal(feedPolicy.visibleInAllFeed(manager), false);
  assert.equal(feedPolicy.visibleInAllFeed(abs), false);
  assert.equal(feedPolicy.visibleInAllFeed(priorPending), false);
  dom.window.close();
});

test('model-fetch failure preserves the official error without guessed numbers', async () => {
  const { dom, adapter } = setup({ failed: true });
  await adapter.capture(fixture, game);
  const errors = adapter.entries('errors');
  assert.equal(errors.length, 1);
  assert.equal(errors[0].modelRow.prediction_available, false);
  assert.match(adapter.panel(errors[0]).textContent, /503/);
  dom.window.close();
});

test('full feed renders one primary-pending row, suppresses its duplicate model row, and has focused tabs', async () => {
  const { dom, w, adapter } = setup();
  const feed = copy(fixture);
  const pendingPlay = feed.liveData.plays.allPlays.find(p => p.result?.eventType === 'field_error');
  assert.ok(pendingPlay);
  pendingPlay.playEvents.push(marker('os_ruling_pending_primary', pendingPlay.playEvents.length));
  feed.liveData.plays.currentPlay = pendingPlay;

  const scheduleGame = {
    ...game,
    gameDate: '2026-07-18T23:00:00Z',
    status: { abstractGameState: 'Live', detailedState: 'In Progress' },
    linescore: feed.liveData.linescore,
  };
  mockFeedRequests(w, scheduleGame, () => feed.liveData.plays);
  addPageScripts(dom);
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  await new Promise(resolve => setTimeout(resolve, 250));

  const rows = w.document.querySelectorAll('#feed-list .feed-row');
  assert.equal(rows.length, 1, 'the pending feed event replaces the same-play model row');
  assert.equal(w.document.querySelectorAll('#feed-list .feed-type-pending_scoring').length, 1);
  assert.equal(w.document.querySelectorAll('#feed-list .model-panel').length, 1,
    'the pending row shows its estimate once');
  assert.equal(adapter.entries('pending_scoring').length, 1);
  assert.match(w.document.querySelector('#feed-tabs').textContent, /Errors \+ observed changes/);
  assert.match(w.document.querySelector('#feed-tabs').textContent, /Official scoring pending/);
  assert.doesNotMatch(w.document.querySelector('#feed-tabs').textContent, /Batted balls|Challenges|ABS|Replay/);

  w.ReplayFeed.setFilter('errors');
  assert.equal(w.document.querySelectorAll('#feed-list .feed-row').length, 0);
  w.ReplayFeed.setFilter('pending_scoring');
  assert.equal(w.document.querySelectorAll('#feed-list .feed-row').length, 1);
  dom.window.close();
});

test('error reclassification remains visible even when its replay-review row is excluded', async () => {
  const { dom, w } = setup();
  const feed = copy(fixture);
  const template = feed.liveData.plays.allPlays.find(p => p.playEvents?.some(e => e.hitData));
  assert.ok(template);
  const changingPlay = playWithType(template, 'single', 200);
  changingPlay.about.hasReview = true;
  changingPlay.result.event = 'Single';
  changingPlay.result.description = 'Batter reaches on a single.';
  feed.liveData.plays = { allPlays: [changingPlay], currentPlay: null };

  const scheduleGame = {
    ...game,
    gameDate: '2026-07-18T23:00:00Z',
    status: { abstractGameState: 'Live', detailedState: 'In Progress' },
  };
  mockFeedRequests(w, scheduleGame, () => feed.liveData.plays);
  addPageScripts(dom);
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(w.document.querySelectorAll('#feed-list .feed-row').length, 0,
    'generic replay events are not visible before a scoring change is observed');

  changingPlay.result.eventType = 'field_error';
  changingPlay.result.event = 'Field Error';
  changingPlay.result.description = 'Batter reaches on a fielding error.';
  w.ReplayFeed.refresh();
  await new Promise(resolve => setTimeout(resolve, 200));

  assert.equal(w.document.querySelectorAll('#feed-list .feed-row').length, 1);
  const row = w.document.querySelector('#feed-list .feed-row');
  assert.equal(row.classList.contains('feed-type-scoring_change'), true);
  assert.match(row.textContent, /Single.*Field Error/);
  assert.equal(w.document.querySelectorAll('#feed-list .feed-type-review').length, 0,
    'the hidden replay row is not surfaced as a review event');
  assert.equal(w.document.querySelectorAll('#feed-list .model-panel').length, 0,
    'the resolved change is shown as observed, not paired with a post-ruling forecast');
  w.ReplayFeed.setFilter('errors');
  assert.equal(w.document.querySelectorAll('#feed-list .feed-row').length, 1);
  dom.window.close();
});

test('scoreboard and home page link to the focused flow without a challenge tab', () => {
  const scoreboard = read('docs/pbp/index.html');
  const home = read('docs/index.html');
  const scoreboardScript = read('docs/pbp/assets/js/scoreboard.js');
  assert.match(scoreboard, /Errors \+ pending/);
  assert.match(home, /All-games scoreboard/);
  assert.match(home, /not<\/b> a trained probability that an\s+already-issued error call will change to a hit/i);
  assert.doesNotMatch(scoreboardScript, /counts\.challenges|filter === 'challenges'|renderActiveReviewsBanner/);
});
