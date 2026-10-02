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
function setup(failed = false) {
  const dom = new JSDOM(read('docs/pbp/reviews.html'), {
    url: 'https://example.test/ErrorsLive/docs/pbp/reviews.html?date=2026-07-18',
    runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window;
  w.AbortSignal = AbortSignal;
  w.fetch = async () => ({ ok: !failed, status: failed ? 503 : 200, json: async () => model });
  for (const file of ['docs/site.js', 'docs/scoring-feed.js', 'docs/pbp/assets/js/scoring-model.js'])
    vm.runInContext(read(file), dom.getInternalVMContext(), {filename:file});
  return {dom, w, adapter: vm.runInContext('ScoringModel', dom.getInternalVMContext())};
}
const game = { gamePk:823441, teams:{away:{team:{name:'Away'}},home:{team:{name:'Home'}}}, status:{abstractGameState:'Final'} };
const copy = v => JSON.parse(JSON.stringify(v));
test('actual committed feed scores identically to the shared scorer and renders all 11 classes', async () => {
  const {dom, adapter} = setup();
  await adapter.capture(fixture, game);
  const entries = adapter.entries();
  assert.ok(entries.length > 0);
  const expected = vm.runInContext(`scoreLiveFeed(${JSON.stringify(fixture)}, ${JSON.stringify(game)}, ${JSON.stringify(model)})`, dom.getInternalVMContext());
  for (const e of entries) {
    const shared = expected.find(r => r.at_bat === e.modelRow.at_bat);
    assert.equal(e.modelRow.score_100, shared.score_100);
  }
  const e = entries.find(e => e.modelRow.prediction_available);
  assert.ok(e);
  assert.equal(adapter.panel(e).querySelectorAll('.model-probabilities span').length, 11);
  assert.match(adapter.render(e).textContent, /Not P\(ruling overturned\)/);
  await adapter.capture(fixture, game);
  assert.equal(adapter.entries().length, entries.length, 'polling does not duplicate rows');
  adapter.clear(); assert.equal(adapter.entries().length, 0);
  dom.window.close();
});
test('synthetic primary/prior pending, missing/invalid inputs, runner errors and safe rendering', async () => {
  const {dom, adapter} = setup();
  const feed = copy(fixture);
  const play = feed.liveData.plays.allPlays.find(p => p.playEvents.some(e => e.hitData));
  feed.liveData.plays.allPlays = [play]; delete feed.liveData.plays.currentPlay;
  play.result = {eventType:'os_ruling_pending_primary', description:'<img src=x onerror=alert(1)>'};
  await adapter.capture(feed, game);
  let e = adapter.entries()[0];
  assert.equal(e.modelRow.official_scoring_pending, true);
  assert.equal(e.modelRow.prediction_available, true);
  assert.equal(adapter.render(e).querySelector('img'), null);
  play.result.eventType = 'os_ruling_pending_prior';
  await adapter.capture(feed, game);
  assert.equal(adapter.entries()[0].modelRow.prediction_available, false);
  play.result.eventType = 'single';
  play.runners = [{details:{eventType:'field_error'},movement:{start:'1B',end:'2B'}}];
  await adapter.capture(feed, game);
  assert.equal(adapter.entries('hits').length, 1);
  assert.equal(adapter.entries('errors').length, 1);
  play.playEvents.find(e => e.hitData).hitData.launchSpeed = 'not a number';
  await adapter.capture(feed, game);
  assert.equal(adapter.entries()[0].modelRow.prediction_available, false);
  play.playEvents = [];
  await adapter.capture(feed, game);
  assert.match(adapter.panel(adapter.entries()[0]).textContent, /Score unavailable/);
  dom.window.close();
});
test('model fetch failure keeps official play visible without guessed numbers', async () => {
  const {dom, adapter} = setup(true);
  await adapter.capture(fixture, game);
  assert.ok(adapter.entries().length);
  assert.ok(adapter.entries().every(e => !e.modelRow.prediction_available));
  assert.match(adapter.panel(adapter.entries()[0]).textContent, /503/);
  dom.window.close();
});
test('full replay page boots, mixes actual fixture batted balls with reviews, and filters', async () => {
  const {dom,w,adapter} = setup();
  const errors = [];
  w.addEventListener('error', e => errors.push(e.message));
  const scheduleGame = {...game, gameDate:'2026-07-18T23:00:00Z', linescore:fixture.liveData.linescore};
  w.fetch = async url => {
    url=String(url);
    let value;
    if(url.includes('/schedule')) value={dates:[{date:'2026-07-18',games:[scheduleGame]}]};
    else if(url.includes('/teams')) value={teams:[]};
    else if(url.includes('/playByPlay')) value=fixture.liveData.plays;
    else if(url.includes('/feed/live')) value=fixture;
    else if(url.includes('/gameStatus')) value=[];
    else throw new Error(`Unexpected request ${url}`);
    return {ok:true,status:200,json:async()=>copy(value),headers:{get:()=>null}};
  };
  for (const file of ['api','ui','reviews','feed-log','reviews-feed'])
    vm.runInContext(read(`docs/pbp/assets/js/${file}.js`), dom.getInternalVMContext(), {filename:file});
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.deepEqual(errors, []);
  assert.ok(adapter.entries().length > 0);
  assert.ok(w.document.querySelectorAll('.model-event').length > 0);
  assert.match(w.document.querySelector('#feed-tabs').textContent, /Batted balls/);
  w.ReplayFeed.setFilter('hits');
  assert.equal(w.document.querySelectorAll('.model-event').length, adapter.entries('hits').length);
  w.ReplayFeed.setFilter('pending_scoring');
  assert.equal(w.document.querySelectorAll('.model-event').length, 0);
  dom.window.close();
});
test('out/FC/sacrifice filters follow official classifications, not the model top pick', async () => {
  const {dom,adapter} = setup();
  const feed = copy(fixture);
  const template = feed.liveData.plays.allPlays.find(p => p.playEvents.some(e => e.hitData));
  feed.liveData.plays.allPlays = ['sac_bunt','sac_fly','fielders_choice','field_out'].map((code,i) => {
    const p = copy(template); p.about.atBatIndex=i;
    p.result = {eventType:code, isOut:code !== 'fielders_choice'}; return p;
  });
  delete feed.liveData.plays.currentPlay;
  await adapter.capture(feed,game);
  assert.equal(adapter.entries('sacrifices').length,2);
  assert.equal(adapter.entries('outs').length,3);
  assert.equal(adapter.entries('fc').length,1);
  assert.equal(adapter.entries('hits').length,0);
  feed.liveData.plays.allPlays[0].result = {};
  await adapter.capture(feed,game);
  assert.equal(adapter.get(game.gamePk,0).modelRow.official_scoring_pending,false);
  const inFlight = adapter.capture(feed,game);
  adapter.clear(); await inFlight;
  assert.equal(adapter.entries().length,0,'late capture cannot restore a cleared date');
  dom.window.close();
});
