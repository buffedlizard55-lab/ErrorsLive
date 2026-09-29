/* Shared helpers: nav, CSV parse, model evaluation, formatting.
   Data files are served from ./data/. The model math here is mirrored byte-for-byte by
   tools/live_score.py; tests/test_pipeline.py asserts the two agree to 1e-9. */
const NAV = [
  ['index.html', 'Live Score /100'],
  ['live.html', 'Live Board'],
  ['model.html', 'Model & Validation'],
  ['overturned.html', 'Overturned Calls 2014–18'],
  ['rules.html', 'Rules & RBI'],
  ['methods.html', 'Methods & Audit'],
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

async function loadJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return r.json(); }
async function loadCSV(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return parseCSV(await r.text()); }

/* ---------- model evaluation (mirror of tools/live_score.py Scorer.predict) ---------- */
const TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder'];
const HARD = ['soft', 'medium', 'hard'];
function featurize(ev, la, dist, traj, hard) {
  return [ev, la, dist, ...TRAJ.map(t => t === traj ? 1 : 0), ...HARD.map(h => h === hard ? 1 : 0)];
}
function evalModel(M, x) {
  const p = M.primary;
  const z = p.feature_names.map((_, i) => (x[i] - p.scaler_mean[i]) / (p.scaler_scale[i] || 1));
  const logit = p.intercept + z.reduce((s, v, i) => s + v * p.coef[p.feature_names[i]], 0);
  const pErr = 1 / (1 + Math.exp(-logit));
  const mc = M.multiclass; const classes = Object.keys(mc.coef);
  const exps = classes.map(c => Math.exp(mc.intercept[c] + z.reduce((s, v, i) => s + v * mc.coef[c][p.feature_names[i]], 0)));
  const tot = exps.reduce((a, b) => a + b, 0);
  const probs = Object.fromEntries(classes.map((c, i) => [c, exps[i] / tot]));
  return { pErr, probs };
}

/* Where does this ball sit among the 1,258 archived batted balls? A 0-100 dial on a
   probability that never leaves 0-7 is misleading; the percentile is the honest scale. */
function errorPercentile(M, pErr) {
  const g = M.honesty && M.honesty.p_error_percentile_grid;
  if (!g || !g.length) return null;
  let lo = 0;
  for (let i = 0; i < g.length; i++) if (pErr >= g[i].p_error) lo = i;
  const a = g[lo], b = g[Math.min(lo + 1, g.length - 1)];
  if (a === b) return a.percentile;
  const t = (pErr - a.p_error) / ((b.p_error - a.p_error) || 1);
  return Math.round(a.percentile + t * (b.percentile - a.percentile));
}

const fmtPct = (v, d = 1) => (100 * v).toFixed(d) + '%';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function downloadCSV(filename, rows) {
  if (!rows.length) return;
  const keys = [...new Set(rows.flatMap(Object.keys))];
  const q = v => { v = v == null ? '' : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const csv = [keys.join(','), ...rows.map(r => keys.map(k => q(r[k])).join(','))].join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  a.download = filename; a.click(); URL.revokeObjectURL(a.href);
}
