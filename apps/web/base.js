// Shared helpers: API call, explain[] rendering, badge
async function api(path, body, method) {
  const r = await fetch(path, { method: method || (body ? 'POST' : 'GET'), headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.detail || r.statusText), { status: r.status, body: j });
  return j;
}
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function renderExplain(list) {
  if (!list || !list.length) return '';
  return `<div class="explain"><h4>Why this decision? — explain[]</h4><ul>` + list.map(e =>
    `<li class="${e.triggered ? 'trig' : ''}"><span class="sig">${esc(e.signal)}</span><span>${esc(typeof e.value === 'object' ? JSON.stringify(e.value) : e.value)} — ${esc(e.note)}</span><span class="w">w=${e.weight}${e.source ? ' · ' + esc(e.source) : ''}</span></li>`
  ).join('') + `</ul></div>`;
}
function badge(level, text) { return `<span class="badge ${level}">${esc(text)}</span>`; }
async function loadHealth(elId) {
  try { const h = await api('/health'); const el = document.getElementById(elId); if (el) el.textContent = `NaC mode: ${h.nac.mode}${h.nac.base_url ? ' · ' + h.nac.base_url : ''} · profiles: ${h.profiles}`; } catch (e) { }
}
// The agent's own clock: sweeps run on a background cadence, not because somebody pressed a button.
async function loadScheduler(elId) {
  try {
    const s = await api('/v1/scheduler'); const el = document.getElementById(elId); if (!el) return;
    el.textContent = s.enabled
      ? `agent clock: ON · every ${s.tick_seconds}s · ticks ${s.ticks} · sweeps ${s.sweeps_run} · last tick ${(s.last_tick_at || '—').slice(11, 19)}`
      : `agent clock: off (${s.reason})`;
    el.title = s.reason;
  } catch (e) { }
}
