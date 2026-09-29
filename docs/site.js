/* Shared helpers for the LiveScoringErrors pages.
   Data files are served from ./data/. The model math here is mirrored by tools/live_score.py, and
   tests/test_pipeline.py asserts the two implementations agree to 1e-9 on every archived batted
   ball. Both sides build their feature vector from `primary.feature_spec` in model.json, so a change
   to the features cannot silently desynchronise the page from the tool. */
const NAV = [
  ['index.html', 'Score a batted ball'],
  ['live.html', 'Live board'],
  ['replays.html', 'Replays & runs'],
  ['model.html', 'Model & validation'],
  ['overturned.html', 'Archive 2014–18'],
  ['rules.html', 'Rules & RBI'],
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
  return { pErr, probs, top, x, z };
}

/* Where does this ball sit among the fitted batted balls? A 0-100 dial on a probability that never
   leaves the low single digits is misleading; the percentile is the honest scale. */
function errorPercentile(M, pErr) {
  const g = M.honesty && M.honesty.p_error_percentile_grid;
  if (!g || !g.length) return null;
  let lo = 0;
  for (let i = 0; i < g.length; i++) if (pErr >= g[i].p_error) lo = i;
  const a = g[lo], b = g[Math.min(lo + 1, g.length - 1)];
  if (a === b || b.p_error === a.p_error) return a.percentile;
  const t = (pErr - a.p_error) / (b.p_error - a.p_error);
  return Math.round(a.percentile + t * (b.percentile - a.percentile));
}

const fmtPct = (v, d = 1) => (100 * v).toFixed(d) + '%';
const num = (v, d = 2) => (v === null || v === undefined || v === '') ? '–' : Number(v).toFixed(d);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const callName = c => ({ hit: 'hit', error: 'error', fielders_choice: "fielder's choice", out: 'out' }[c] || c);

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
