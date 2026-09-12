// Shared helpers: API call, explain[] rendering, badge
async function api(path, body, method) {
  const r = await fetch(path, { method: method || (body ? 'POST' : 'GET'), headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.detail || r.statusText), { status: r.status, body: j });
  return j;
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
// Kanit degeri: nesneyse `anahtar=deger` ciftleri (JSON'daki tirnaklar juriye gurultu; bosluksuz JSON satir kiramiyordu)
function fmtValue(v) {
  if (v === null || v === undefined) return '—';
  if (Array.isArray(v)) return v.map(fmtValue).join(', ');
  if (typeof v === 'object') return Object.entries(v).map(([k, x]) => `${k}=${typeof x === 'object' && x !== null ? JSON.stringify(x) : x}`).join(' · ');
  return String(v);
}
function renderExplain(list) {
  if (!list || !list.length) return '';
  return `<div class="explain"><h4>Why this decision? — explain[]</h4><ul>` + list.map(e =>
    `<li class="${e.triggered ? 'trig' : ''}"><span class="sig">${esc(e.signal)}</span><span class="val">${esc(fmtValue(e.value))} — ${esc(e.note)}</span><span class="w">w=${e.weight}${e.source ? ' · ' + esc(e.source) : ''}</span></li>`
  ).join('') + `</ul></div>`;
}
function badge(level, text) { return `<span class="badge ${level}">${esc(text)}</span>`; }
// Where the operator data comes from — said in the jury's words; the raw mode stays in the tooltip.
async function loadHealth(elId) {
  try {
    const h = await api('/health'); const el = document.getElementById(elId); if (!el) return;
    const nac = h.nac || {}, mode = nac.mode || 'unknown';
    el.textContent = mode === 'fixture'
      ? `Operator data: recorded fixtures (${h.profiles} device profiles) — no live Nokia credentials on this public instance`
      : `Operator: Nokia Network as Code · live (${mode})${nac.base_url ? ' · ' + nac.base_url : ''} · ${h.profiles} device profiles`;
    el.title = `NaC mode: ${mode}${nac.base_url ? ' · ' + nac.base_url : ''} · profiles: ${h.profiles}`;
  } catch (e) { }
}
// The agent's own clock: sweeps run on a background cadence, not because somebody pressed a button.
// On the public demo it is paused on purpose so every replay is identical; the raw reason stays in the tooltip.
async function loadScheduler(elId) {
  try {
    const s = await api('/v1/scheduler'); const el = document.getElementById(elId); if (!el) return;
    el.textContent = s.enabled
      ? `Agent scheduler: ON — sweeps on its own every ${+s.tick_seconds} s · ticks ${s.ticks} · sweeps ${s.sweeps_run} · last ${(s.last_tick_at || '—').slice(11, 19)}`
      : 'Agent scheduler: paused for the demo — sweeps are replayed on demand so every run is identical; in live mode the agent sweeps on its own cadence (2 min severe / 10 min marginal)';
    el.title = s.reason || '';
  } catch (e) { }
}
