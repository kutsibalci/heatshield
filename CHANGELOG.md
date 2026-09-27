# Changelog

## Unreleased

Post-hackathon hardening. Each item was reproduced before it was fixed and has a regression test.
The six demo scenarios still produce the same calls, verdicts and escalations as `v0.1-hackathon`.

### Correctness (#1)
- **Medic's coordinate.** An escalated worker's one-time location retrieval now ranks first. Before, it
  could fall out of the plan on sites of about 40 workers or more.
- **Stale WBGT.** Readings posted through the API carry their timestamp, so a stale feed is detected.
- **Out-of-order geofence events** are recorded and not applied. A late exit no longer takes a worker out
  of the queue.
- **Circuit breaker.** Input errors (400/404) no longer open it, and half-open admits exactly one probe.
- **Ledger.**
  - `seq` is stable after pruning.
  - Sweep reports keep their own rows.
  - A missing centre is no longer stored as `(0, 0)`.
- **Configuration.** `HS_WBGT_LIMIT_C` and `HS_JURISDICTION` are honoured.
- **API.** A duplicate `worker_id` returns 409. Webhook dedupe uses a bounded window and also covers
  events without an id.

### Concurrency (#2)
- Per-site locks, lock-free reads (copy-on-write containers), and one lock that survives reset.
- A slow operator response on one site no longer stalls the others.
- The planner-guard demo no longer patches process-wide state.
- The budget report separates planned queries from distress calls.

### Security (#3)
- **Tokens.** `HS_API_TOKEN` guards writes and personal-data reads. The webhook token only opens the
  geofence sink.
- **No default secrets**, and live mode refuses to start without them.
- **Demo mode.** Demo scenarios need `HS_DEMO_MODE=1`, are refused against the live operator, and are
  rate-limited.
- **Input bounds.** WBGT, coordinates and text lengths are bounded. Validation errors do not echo the
  input.
- **Headers.** Security headers and a CSP are set.
- **Simulator.** It sends the sink credential back, and only delivers to local sinks (SSRF).
- **Gemini key.** It moved from the URL to a header.

### Repository (#4)
- **Build and lint.** `pyproject.toml`, pinned requirements with dev tools split out, and ruff with
  correctness rules.
- **CI.** Lint and tests on Python 3.12 and 3.13, plus a Docker smoke test.
- **Docker image.** It runs as a non-root user and holds only `apps/`, `packages/` and `fixtures/`.
- **Tools.**
  - Paths no longer depend on the clone's folder name, and output goes to `evidence/`.
  - They share one `.env` loader.
  - Live tools require an explicit `--live` (`verify_live.py`: `--mode` is required).

### Code organisation and language (#5)
- **English throughout.** Every code comment and docstring moved from Turkish to English, and so did the
  user-facing messages. Checked by `tools/check_code_unchanged.py`: the syntax tree was identical apart
  from docstrings.
- **`apps/api/main.py` split into modules.** It was 1,170 lines. Now `main.py` holds the app and core routes,
  alongside `security.py`, `runtime.py`, `models.py`, `scheduler.py` and `demo.py`.
- **`policy.step()` in named stages.** It was one 364-line function; it now calls the stages in order.
  The full demo output (7 MB of JSON) is byte-identical before and after.
- **One timestamp parser** (`rules/timeutil.py`) instead of five copies. A geofence event with an empty
  `time` is now applied at the current time instead of returning 500.

## v0.1-hackathon — 2026-09-13

The prototype submitted to the GSMA MENA Ignite Hackathon 2026 and presented at Demo Day on
21 September 2026 (Top 10). Frozen at tag `v0.1-hackathon` and branch `hackathon-2026`.
