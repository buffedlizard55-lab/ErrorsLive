/* Shared helpers: nav, CSV parse, model evaluation. Data files are served from ./data/. */
const NAV = [
  ['index.html', 'Live Score /100'],
  ['model.html', 'Model & Validation'],
  ['overturned.html', 'Overturned Calls 2014–18'],
  ['rules.html', 'Rules 9.12 / 9.16 / 10.04'],
  ['methods.html', 'Methods & Data Audit'],
  ['roadmap.html', 'Limitations & Roadmap'],
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

/* ---------- model evaluation ---------- */
const TRAJ = ['ground_ball', 'line_drive', 'fly_ball', 'popup', 'bunt_grounder'];
const HARD = ['soft', 'medium', 'hard'];
function featurize(ev, la, dist, traj, hard) {
  return [ev, la, dist, ...TRAJ.map(t => t === traj ? 1 : 0), ...HARD.map(h => h === hard ? 1 : 0)];
}
function evalModel(M, x) {
  const p = M.primary;
  const z = p.feature_names.map((_, i) => (x[i] - p.scaler_mean[i]) / p.scaler_scale[i]);
  const logit = p.intercept + z.reduce((s, v, i) => s + v * p.coef[p.feature_names[i]], 0);
  const pErr = 1 / (1 + Math.exp(-logit));
  const mc = M.multiclass; const classes = Object.keys(mc.coef);
  const exps = classes.map(c => Math.exp(mc.intercept[c] + z.reduce((s, v, i) => s + v * mc.coef[c][p.feature_names[i]], 0)));
  const tot = exps.reduce((a, b) => a + b, 0);
  const probs = Object.fromEntries(classes.map((c, i) => [c, exps[i] / tot]));
  return { pErr, probs };
}
const fmtPct = (v, d = 1) => (100 * v).toFixed(d) + '%';
