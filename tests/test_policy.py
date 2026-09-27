"""Sweep engine (agent policy) tests — with the real nac_client in fixture mode (no HTTP)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nac_client import FixtureBackend, NacClient, NacConfig
from agent import SiteRuntime, apply_geofence_event, new_worker, step, sweep_due
from rules import Config

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
T0 = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)    # 06:00 Doha — outside ban hours
NOON = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)  # 12:00 Doha
SITE = {"lat": 25.3917, "lng": 51.5299, "radius_m": 500}


class Facade:
    def __init__(self, nac):
        self.nac = nac

    def call(self, name, *a, **kw):
        return getattr(self.nac, name)(*a, **kw)


@pytest.fixture
def nac():
    fx = FixtureBackend.from_json(ROOT / "fixtures" / "profiles.json")
    return Facade(NacClient(NacConfig(mode="fixture"), fixtures=fx))


@pytest.fixture
def site():
    return SiteRuntime(site_id="site-1", name="Lusail", cfg=Config(), **SITE)


NAMED = [("W-001", "+99999910001", "z1"), ("W-002", "+99999910002", "z1"), ("W-003", "+99999910003", "z2"),
         ("W-004", "+99999910004", "z2"), ("W-005", "+99999910005", "z3"), ("W-006", "+99999910006", "z3"),
         ("W-007", "+99999910007", "z1"), ("W-008", "+99999910008", "z1"),
         ("W-009", "+99999910009", "z1")]   # uncertainty area crosses the perimeter → PARTIAL


def populate(site, enter_at=T0, count=None):
    for wid, phone, zone in NAMED[: count or len(NAMED)]:
        hist = {"continuous_reachable_hours": 8} if wid == "W-002" else ({"recurring_unreachable_window": True} if wid == "W-005" else {})
        site.workers[wid] = new_worker(wid, phone, name=wid, micro_zone=zone, device_history=hist,
                                       moving_out=(wid == "W-006"),   # site signal: turnstile exit record
                                       first_day_on_site=(enter_at - timedelta(days=1 if wid == "W-002" else 400)).date().isoformat())
        apply_geofence_event(site, wid, "enter", enter_at)
    return site


# ------------------------------------------------------------------ Move 1
def test_no_breach_spends_nothing(site, nac):
    populate(site)
    r = step(site, T0, nac, site.cfg, 30.0)
    assert r["breached"] is False and r["calls"] == [] and r["budget"]["spent"] == 0
    assert site.queries_total == 0 and site.state == "passive_watch"
    assert "NO query" in r["note"]


def test_geofence_events_are_free(site, nac):
    populate(site)
    assert site.queries_total == 0
    assert all(w.inside and w.last_signal_at == T0 for w in site.workers.values())
    entries = [e for e in site.ledger if e["event"] == "geofence_enter"]
    assert len(entries) == len(NAMED) and entries[0]["source"] == "geofencing-subscriptions"


# ------------------------------------------------------------------ Move 2
def test_breach_opens_query_authority_and_ranks(site, nac):
    populate(site)
    r = step(site, NOON, nac, site.cfg, 34.5)
    assert r["breached"] and r["level"] == "severe" and site.state in ("alert", "distress", "emergency")
    assert r["ranked"][0]["worker_id"] == "W-002"          # first-week worker at the top
    assert all(c["api"] == "location-verification" for c in r["calls"])
    assert site.breach_started_at == NOON


def test_budget_stops_the_list_not_the_other_way_round(site, nac):
    populate(site)
    cfg = Config()
    object.__setattr__(cfg.budget, "queries_per_site_per_minute", 4)
    site.cfg = cfg
    r = step(site, NOON, nac, cfg, 34.5)
    assert r["budget"]["spent"] == 3 and r["budget"]["skipped_for_budget"] == 6
    assert any(e["event"] == "budget_exhausted" for e in r["ledger_added"])


def test_second_sweep_climbs_to_reachability(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 34.5)
    r = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 34.5)
    apis = {c["api"] for c in r["calls"]}
    assert "device-reachability-status" in apis
    assert site.workers["W-001"].reachable is True and site.workers["W-002"].reachable is False


def test_exit_event_clears_worker_and_returns_budget(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 34.5)
    apply_geofence_event(site, "W-001", "exit", NOON + timedelta(minutes=1))
    r = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 34.5)
    assert site.workers["W-001"].cleared and not site.workers["W-001"].inside
    assert "W-001" not in [x["worker_id"] for x in r["ranked"]]


def test_location_verify_false_clears_worker(site, nac):
    populate(site)
    r = step(site, NOON, nac, site.cfg, 34.5)
    # W-007 is off site in the fixture → FALSE → dropped from the queue
    assert site.workers["W-007"].cleared is True and site.workers["W-007"].verified_inside is False
    assert any(e["event"] == "worker_cleared" for e in r["ledger_added"])


def test_partial_is_insufficient_evidence_not_a_movement_signal(site, nac):
    """PARTIAL is geometric: the uncertainty area intersects the zone but does not stay inside it.

    It has nothing to do with movement (operator correction, 9 September 2026). So it does not count
    as "verified", staleness keeps growing and the worker stays in the queue — unknown is not safe."""
    populate(site)
    r = step(site, NOON, nac, site.cfg, 34.5)
    w9 = site.workers["W-009"]                       # fixture returns PARTIAL
    assert w9.verified_inside is None                # counted as not verified
    assert w9.cleared is False                       # NOT dropped from the queue
    assert w9.last_signal_at is None or w9.last_signal_at < NOON   # staleness was not reset
    assert any(e["event"] == "insufficient_evidence" for e in r["ledger_added"])
    assert site.partial_verifications >= 1           # the PARTIAL rate is measured


# ------------------------------------------------------------------ Move 3 + §9
def test_collapse_escalates_to_medic_with_qod_then_one_shot_location(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    verdicts = {v["worker_id"]: v for v in r2["verdicts"]}
    assert verdicts["W-002"]["verdict"] == "probable_collapse"
    assert "notify_medic" in verdicts["W-002"]["actions"]
    assert verdicts["W-003"]["verdict"] == "probable_network"     # z2 cluster + high congestion
    assert verdicts["W-005"]["verdict"] == "probable_battery"     # recurring silence
    assert verdicts["W-006"]["verdict"] == "probable_left"        # heading to the exit
    assert "W-002" in site.qod_sessions and site.state == "emergency"
    assert site.workers["W-002"].location_retrieved is False      # coordinate not retrieved yet
    step(site, NOON + timedelta(minutes=4), nac, site.cfg, 35.5)
    assert site.workers["W-002"].location_retrieved is True       # only after escalation
    assert sum(1 for e in site.ledger if e["event"] == "location_retrieve") == 1


def test_network_event_does_not_wake_a_human(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    assert [e["worker_id"] for e in r2["escalations"]] == ["W-002"]
    net = [v for v in r2["verdicts"] if v["verdict"] == "probable_network"]
    assert net and all("notify_medic" not in v["actions"] for v in net)


def test_congestion_is_cached_per_micro_zone(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    r3 = step(site, NOON + timedelta(minutes=4), nac, site.cfg, 35.5)
    assert not [c for c in r3["calls"] if c["api"] == "congestion-insights"]  # 5 min cache


def test_breach_ends_and_query_authority_drops(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    spent = site.queries_total
    r = step(site, NOON + timedelta(hours=8), nac, site.cfg, 29.0)
    assert r["breached"] is False and site.queries_total == spent and site.breach_started_at is None
    assert all(w.state == "passive_watch" for w in site.workers.values())


def test_sweep_cadence_depends_on_severity(site):
    populate(site)
    site.level, site.last_sweep_at = "severe", NOON
    assert sweep_due(site, NOON + timedelta(minutes=1), site.cfg) is False
    assert sweep_due(site, NOON + timedelta(minutes=3), site.cfg) is True
    site.level = "marginal"
    assert sweep_due(site, NOON + timedelta(minutes=3), site.cfg) is False


# ------------------------------------------------------------------ privacy / evidence
def test_worker_dict_never_leaks_raw_phone(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    blob = str(site.to_dict())
    assert "+99999910001" not in blob and "+99999***0001" in blob
    assert len(site.workers["W-001"].phone_hash) == 64


def test_ledger_records_evidence_with_sources(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    events = {e["event"] for e in site.ledger}
    assert {"geofence_enter", "breach_started", "location_verify"} <= events
    breach = next(e for e in site.ledger if e["event"] == "breach_started")
    assert breach["explain"] and breach["explain"][0]["signal"] == "wbgt_c"
    assert all("t" in e and e["seq"] for e in site.ledger)


def test_coverage_reports_badged_but_invisible_workers(site, nac):
    populate(site)
    site.workers["W-009"] = new_worker("W-009", "+99999910012", name="Arjun", badge_in=True)
    site.workers["W-009"].inside = False
    cov = site.coverage()
    assert cov["badged_in"] == 9 and cov["missing"] == ["W-009"] and cov["coverage_pct"] == 89


# ------------------------------------------------------------------ cost ladder along the freshness axis
def test_first_verification_accepts_a_cached_answer(site, nac):
    """Rung 2: loose freshness. A cached answer is accepted — cheap on the network, a database read."""
    populate(site)
    r = step(site, NOON, nac, site.cfg, 34.5)
    verifies = [c for c in r["calls"] if c["api"] == "location-verification"]
    assert verifies and all(c["freshness"] == "loose" for c in verifies)
    assert all(c["max_age_s"] == site.cfg.budget.loose_max_age_s for c in verifies)


def test_an_ambiguous_answer_climbs_to_a_fresh_fix(site, nac):
    """Rung 3: if the cheap answer came back ambiguous (PARTIAL/UNKNOWN), a fresh fix is requested — costly rung."""
    populate(site)
    step(site, NOON, nac, site.cfg, 34.5)                      # W-009 → PARTIAL
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 34.5)
    strict = [c for c in r2["calls"] if c["api"] == "location-verification" and c["freshness"] == "strict"]
    assert strict, "belirsiz cevaptan sonra sıkı tazelikli doğrulama beklenirdi"
    assert all(c["max_age_s"] == site.cfg.budget.strict_max_age_s for c in strict)
    assert any(c["worker_id"] == "W-009" for c in strict)


def test_the_costly_rung_is_recorded_in_the_ledger(site, nac):
    populate(site)
    step(site, NOON, nac, site.cfg, 34.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 34.5)
    entries = [e for e in r2["ledger_added"] if e["event"] == "location_verify"]
    assert any("rung 3" in e["detail"] for e in entries)
    assert any("rung 2" in e["detail"] for e in entries)


# ------------------------------------------------------------------ shift roster reconciliation
def test_a_missed_entry_event_does_not_make_a_worker_invisible(site, nac):
    """A missed entry event = an invisible worker. Reconciliation turns them into 'status unknown'."""
    populate(site)
    ghost = new_worker("W-099", "+99999910099", name="W-099", micro_zone="z1",
                       first_day_on_site=(T0 - timedelta(days=2)).date().isoformat())
    site.workers["W-099"] = ghost                              # badged in, but NO geofence event at all
    assert ghost.last_signal_at is None and ghost.inside is True

    ghost.inside, ghost.cleared = False, True                  # event missed → invisible in the system
    r = step(site, NOON, nac, site.cfg, 34.5)

    assert "W-099" in r["reconciled"]
    assert ghost.inside is True and ghost.cleared is False     # taken back into the queue
    assert any(e["event"] == "roster_reconciliation" and e["worker_id"] == "W-099" for e in r["ledger_added"])
    assert "W-099" in site.coverage()["missing"]               # but the coverage metric stays honest


def test_reconciliation_does_not_recall_a_worker_with_a_real_exit_event(site, nac):
    """A worker with a real exit event is not recalled — reconciliation does not override evidence."""
    populate(site)
    apply_geofence_event(site, "W-007", "left", T0 + timedelta(minutes=5))
    r = step(site, NOON, nac, site.cfg, 34.5)
    assert "W-007" not in r["reconciled"]
    assert site.workers["W-007"].exited_at is not None


def test_reconciliation_runs_at_breach_start_and_then_periodically(site, nac):
    """Reconciliation is mandatory at the start of every breach, then at `reconcile_minutes` intervals."""
    populate(site)
    r1 = step(site, NOON, nac, site.cfg, 34.5)
    assert site.last_reconcile_at == NOON and "reconciled" in r1

    step(site, NOON + timedelta(minutes=2), nac, site.cfg, 34.5)          # interval not elapsed
    assert site.last_reconcile_at == NOON

    later = NOON + timedelta(minutes=site.cfg.budget.reconcile_minutes + 1)
    step(site, later, nac, site.cfg, 34.5)                                # interval elapsed
    assert site.last_reconcile_at == later


def test_a_new_breach_forces_reconciliation_even_inside_the_interval(site, nac):
    """If the breach ends and starts again, the interval is not awaited — every breach is a clean slate."""
    populate(site)
    step(site, NOON, nac, site.cfg, 34.5)
    # Step OUTSIDE summer ban hours (16:00 Doha) and drop below the threshold → the breach ends
    evening = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    step(site, evening, nac, site.cfg, 30.0)
    assert site.breach_started_at is None

    again = evening + timedelta(minutes=2)
    step(site, again, nac, site.cfg, 34.5)                                # new breach
    assert site.last_reconcile_at == again


# ------------------------------------------------------------------ dead WBGT feed
def test_a_missing_wbgt_reading_is_not_reported_as_no_breach(site, nac):
    """Without a reading the threshold CANNOT BE ASSESSED. Saying 'no breach' silently turns a dying
    feed into a safety statement — it would be the exact exception to the product's main claim."""
    populate(site)
    evening = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)     # OUTSIDE summer ban hours
    r = step(site, evening, nac, site.cfg, None)           # no WBGT given at all
    assert r["breached"] is False
    assert r["wbgt_known"] is False and r["level"] == "unknown"
    assert "cannot be assessed" in r["reason"]
    assert "NOT a statement that the site is safe" in r["note"]
    assert any(e["event"] == "wbgt_feed_unusable" for e in r["ledger_added"])
    assert r["budget"]["spent"] == 0                       # still no query authority arises


def test_a_stale_wbgt_reading_is_treated_as_no_reading(site, nac):
    populate(site)
    evening = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    step(site, evening, nac, site.cfg, 30.0)               # fresh reading → state is known
    later = evening + timedelta(minutes=site.cfg.budget.wbgt_max_age_minutes + 5)
    r = step(site, later, nac, site.cfg, None)             # no new reading arrived
    assert r["wbgt_stale"] is True and r["wbgt_known"] is False
    assert r["level"] == "unknown" and "stale" in r["reason"]
    assert any(e["event"] == "wbgt_feed_unusable" for e in r["ledger_added"])


def test_a_fresh_reading_below_the_limit_is_still_a_real_no_breach(site, nac):
    """The staleness check must not kill the 'no breach' verdict — a fresh reading still decides."""
    populate(site)
    evening = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    r = step(site, evening, nac, site.cfg, 30.0)
    assert r["breached"] is False and r["wbgt_known"] is True and r["level"] == "none"
    assert "no authority to query" in r["reason"]


def test_the_summer_ban_still_applies_without_any_reading(site, nac):
    """Ban hours are set by calendar and clock — a breach arises even without WBGT data."""
    populate(site)
    r = step(site, NOON, nac, site.cfg, None)              # 12:00 Doha, ban hours
    assert r["breached"] is True and r["ban_breach"] is True
    assert r["wbgt_known"] is False                        # no reading, but a decision can still be made


# ------------------------------------------------------------------ presence prior / foreign SIM
def test_roster_only_worker_joins_the_same_queue_with_a_presence_prior(site, nac):
    """No separate reserve (arbitrary split). Same queue, but exposure is weighted by probability —
    being on the shift roster is not the same as being on site."""
    populate(site)
    ghost = new_worker("W-099", "+99999910099", name="W-099", micro_zone="z1",
                       first_day_on_site=(T0 - timedelta(days=2)).date().isoformat())
    ghost.inside, ghost.cleared = False, True        # entry event missed
    site.workers["W-099"] = ghost
    r = step(site, NOON, nac, site.cfg, 34.5)

    assert "W-099" in r["reconciled"]
    assert ghost.presence_prior == site.cfg.budget.presence_prior_roster_only
    ranked = {x["worker_id"]: x for x in r["ranked"]}
    assert "W-099" in ranked                          # in the single queue, not in a separate pool
    notes = [e for e in ranked["W-099"]["explain"] if e["signal"] == "presence_prior"]
    assert notes and "not the same as" in notes[0]["note"]


def test_the_presence_prior_lowers_the_score_without_dropping_the_worker(site, nac):
    """The prior lowers the score but does NOT DROP the worker from the queue — unknown is not safe."""
    from rules import heatshield as R
    base = {"worker_id": "W-1", "masked": "+9745***0001", "last_signal_at": None}
    with_prior = {**base, "presence_prior": 0.5}
    a = R.exposure_detail(base, NOON - timedelta(minutes=30), NOON, site.cfg, 2.0)
    b = R.exposure_detail(with_prior, NOON - timedelta(minutes=30), NOON, site.cfg, 2.0)
    assert 0 < b.risk_score < a.risk_score
    assert b.meta["presence_prior"] == 0.5
    assert b.meta["effective_exposure_minutes"] == round(a.meta["exposure_minutes"] * 0.5, 1)


def test_a_worker_on_a_foreign_sim_is_counted_separately_not_as_missing(site, nac):
    """A roaming foreign SIM: queries are served by the home operator, which we technically
    cannot reach. This worker is not 'missing' but PERMANENTLY UNKNOWN — and is counted separately."""
    populate(site)
    roam = new_worker("W-088", "+99999910088", name="W-088", micro_zone="z1")
    roam.reachable_via_operator = False
    roam.inside, roam.cleared, roam.last_signal_at = False, True, None
    site.workers["W-088"] = roam

    r = step(site, NOON, nac, site.cfg, 34.5)
    cov = site.coverage()
    assert "W-088" in cov["permanently_unreachable"]
    assert "W-088" not in cov["missing"]               # "we could not find" and "we can never look" are separate
    assert cov["permanently_unreachable_pct"] > 0
    assert "W-088" not in r["reconciled"]              # no budget wasted


# ------------------------------------------------------------------ liveness probe
def test_a_fresh_fix_overrules_a_stale_unreachable_reading(site, nac):
    """device-status reads the state the network HOLDS and can be up to an hour stale.
    A strict-freshness location query pages the device; if it answers, it is NOT a collapse."""
    populate(site)
    alive = new_worker("W-013", "+99999910013", name="W-013", micro_zone="z1",
                       device_history={"continuous_reachable_hours": 7},
                       first_day_on_site=(T0 - timedelta(days=350)).date().isoformat())
    site.workers["W-013"] = alive
    apply_geofence_event(site, "W-013", "enter", T0)

    step(site, NOON, nac, site.cfg, 35.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)

    assert alive.verdict is None and alive.escalated is False   # the medic was not called for nothing
    assert alive.reachable is True                              # the stale reading was overruled
    assert any(e["event"] == "liveness_confirmed" and e["worker_id"] == "W-013"
               for e in r2["ledger_added"])
    probe = [c for c in r2["calls"] if c.get("pool") == "liveness" and c["worker_id"] == "W-013"]
    assert probe and probe[0]["freshness"] == "strict"


def test_a_device_that_cannot_produce_a_fresh_fix_is_not_counted_as_alive(site, nac):
    """A switched-off device cannot answer paging → it cannot produce a fresh fix either. Probe won't say 'alive'."""
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    w2 = site.workers["W-002"]
    assert w2.verdict == "probable_collapse"                    # the verdict stood
    # We bind to BEHAVIOUR, not to the event NAME: whatever the reason, the probe
    # MUST NOT say "alive" (liveness_unknown / liveness_inconclusive are both accepted).
    probe_events = {e["event"] for e in r2["ledger_added"] if e.get("worker_id") == "W-002"}
    assert probe_events & {"liveness_unknown", "liveness_inconclusive"}
    assert "liveness_confirmed" not in probe_events


def test_the_liveness_probe_is_recorded_as_a_query(site, nac):
    """The probe is not free — it is spent from the budget and shows up in the ledger."""
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    before = site.queries_total
    r2 = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    probes = [c for c in r2["calls"] if c.get("pool") == "liveness"]
    assert probes and site.queries_total > before
    assert all(c["max_age_s"] == site.cfg.budget.strict_max_age_s for c in probes)


def test_a_dead_wbgt_feed_does_not_close_an_open_collapse_case(site, nac):
    """The feed dying is NOT the breach ending.

    Previously a stale feed took the "breach over" path and wiped all worker state: an open
    collapse case with a medic already called was silently closed just because the weather
    data went quiet, and the ledger wrote "Breach over"."""
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    w2 = site.workers["W-002"]
    assert w2.escalated is True and w2.verdict == "probable_collapse"   # case open
    open_qod = dict(site.qod_sessions)

    # Step OUTSIDE summer ban hours (16:00 Doha) and send no new reading: the last reading was
    # at NOON+2, so it is now well past stale. The feed died — but the case is still open.
    stale_at = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
    r = step(site, stale_at, nac, site.cfg, None)

    assert r["wbgt_known"] is False and r["breached"] is False
    assert site.workers["W-002"].escalated is True                      # case NOT closed
    assert site.workers["W-002"].verdict == "probable_collapse"
    assert site.breach_started_at is not None                           # breach not counted as "over"
    assert site.qod_sessions == open_qod or site.qod_sessions           # the open channel remains
    events = {e["event"] for e in r["ledger_added"]}
    assert "wbgt_feed_unusable" in events
    assert "site_cleared" not in events, "besleme öldü diye 'Breach over' yazılmamalı"
    assert "open cases are NOT closed" in r["note"]


# ------------------------------------------------------------------ _trusted(): the product's MAIN CLAIM
# A mutation audit measured it: with `_trusted()` forced to always True, ZERO tests failed.
# The sentence "A failure never produces a 'safe' verdict" was never tested in code. The tests below lock it.
def _failing_client(fx, api_to_fail):
    """A fake client whose primary call fails but which has a fixture fallback."""
    from nac_client import NacError
    real = NacClient(NacConfig(mode="fixture"), fixtures=fx)

    class Failing:
        fx = None

        def __getattr__(self, name):
            def call(*a, **k):
                if name == api_to_fail:
                    raise NacError("timeout", api_to_fail, "injected failure")
                return getattr(real, name)(*a, **k)
            return call

    f = Failing()
    f.fx = fx
    return f


def test_a_fallback_answer_never_clears_a_worker(site, monkeypatch):
    """A FALSE answer that came from the fallback CANNOT DROP the worker from the queue.

    With the fallback on, the fixture says 'outside the zone'; that is a made-up answer. Treating it as real
    and clearing the worker would silently turn a crashing API into a safety statement."""
    monkeypatch.setenv("NAC_FALLBACK_TO_FIXTURE", "1")
    from api.common import NacFacade

    fx = FixtureBackend.from_json(ROOT / "fixtures" / "profiles.json")
    facade = NacFacade(_failing_client(fx, "location_verify"))
    assert facade.fallback_enabled is True, "bu test yedek AÇIKKEN anlamlı"

    populate(site)
    r = step(site, NOON, nac=facade, cfg=site.cfg, wbgt_c=34.5)

    # W-007 is OFF site in the fixture → the fallback returns FALSE. Because it is an untrusted source,
    # this answer must not clear the worker.
    w7 = site.workers["W-007"]
    assert w7.cleared is False, "yedeğe düşmüş bir cevap işçiyi kuyruktan düşüremez"
    assert w7.verified_inside is None
    sources = {c["source"] for c in r["calls"]}
    assert any(s.startswith("fixture-fallback(") for s in sources), "yedek yolu gerçekten çalışmalı"
    assert any(e["event"] == "untrusted_signal" for e in r["ledger_added"])


def test_a_fallback_answer_never_makes_a_device_reachable(site, monkeypatch):
    """The same rule applies to reachability: a made-up 'reachable' hides a collapse."""
    monkeypatch.setenv("NAC_FALLBACK_TO_FIXTURE", "1")
    from api.common import NacFacade

    fx = FixtureBackend.from_json(ROOT / "fixtures" / "profiles.json")
    facade = NacFacade(_failing_client(fx, "reachability"))

    populate(site)
    step(site, NOON, nac=facade, cfg=site.cfg, wbgt_c=35.5)
    r2 = step(site, NOON + timedelta(minutes=2), nac=facade, cfg=site.cfg, wbgt_c=35.5)
    reach_calls = [c for c in r2["calls"] if c["api"] == "device-reachability-status"]
    if reach_calls:
        assert all(c["result"] is None for c in reach_calls), "yedek cevabı 'erişilebilir' sayılamaz"
        assert any(e["event"] == "untrusted_signal" for e in r2["ledger_added"])


# ------------------------------------------------------------------ liveness probe freshness branch
# Same audit: with the `fresh` check forced to always True, ZERO tests failed as well — it was dead code.
def _probe_stub(result, ts_iso):
    from nac_client.client import NacResult

    class Stub:
        def call(self, name, *a, **k):
            return NacResult(api=name, data={"verificationResult": result, "lastLocationTime": ts_iso},
                             source="fixture", latency_ms=1, correlator="t")
    return Stub()


@pytest.mark.parametrize("label,result,minutes_ago", [
    ("bayat damga", "TRUE", 40),        # 40 min ago: the network did NOT produce a fresh fix
    ("belirsiz cevap", "UNKNOWN", 0),   # UNKNOWN is not a trusted signal on the main path either
])
def test_liveness_probe_refuses_weak_evidence(site, label, result, minutes_ago):
    from agent import policy as P
    w = new_worker("W-1", "+99999910001", name="W-1")
    site.workers["W-1"] = w
    ts = (NOON - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    assert P._liveness_probe(site, w, NOON, _probe_stub(result, ts), site.cfg, {"calls": []}) is None, label
    assert not any(e["event"] == "liveness_confirmed" for e in site.ledger)


def test_liveness_probe_refuses_a_future_timestamp(site):
    """A location from the future is a clock inconsistency, not evidence of liveness.

    Previously a negative age was clamped to zero; this made the freshness threshold completely
    ineffective and erased a collapse verdict held with 0.95 confidence."""
    from agent import policy as P
    w = new_worker("W-1", "+99999910001", name="W-1")
    site.workers["W-1"] = w
    ts = (NOON + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    assert P._liveness_probe(site, w, NOON, _probe_stub("TRUE", ts), site.cfg, {"calls": []}) is None
    assert any(e["event"] == "liveness_inconclusive" for e in site.ledger)


def test_liveness_probe_accepts_a_genuinely_fresh_answer(site):
    from agent import policy as P
    w = new_worker("W-1", "+99999910001", name="W-1")
    site.workers["W-1"] = w
    ts = (NOON - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    assert P._liveness_probe(site, w, NOON, _probe_stub("TRUE", ts), site.cfg, {"calls": []}) is True
    assert any(e["event"] == "liveness_confirmed" for e in site.ledger)


# ------------------------------------------------------------------ ledger honesty and its bound
def test_the_ledger_reports_what_it_actually_holds(site, nac):
    """"No location history is kept" was a FALSE claim. The ledger is a presence record — we count, not hide."""
    populate(site)
    r = step(site, NOON, nac, site.cfg, 35.5)
    pr = r["presence_record"]
    assert pr["ledger_entries"] > 0
    assert pr["worker_presence_entries"] > 0, "defter işçi bazında zaman damgalı kayıt TUTUYOR"
    assert pr["ledger_cap"] == site.cfg.budget.ledger_max_entries
    assert "that is the product" in pr["note"]


def test_the_ledger_is_bounded_and_counts_what_it_drops(site, nac):
    """An 8-hour shift with 400 workers produces 7,341 rows (measured). There is a cap and drops are counted."""
    populate(site)
    cfg = Config()
    object.__setattr__(cfg.budget, "ledger_max_entries", 20)
    site.cfg = cfg
    for k in range(6):
        step(site, NOON + timedelta(minutes=2 * k), nac, cfg, 35.5)
    assert len(site.ledger) <= 20
    assert site.ledger_pruned > 0, "budama SESSİZ olmamalı — kaç satır düştüğü sayılır"
    assert site.presence_record()["ledger_pruned"] == site.ledger_pruned


# ------------------------------------------------------------------ ledger integrity
def test_ledger_seq_stays_unique_after_pruning(site, nac):
    populate(site)
    cfg = Config()
    object.__setattr__(cfg.budget, "ledger_max_entries", 20)
    site.cfg = cfg
    seen: dict[int, tuple] = {}
    for k in range(6):
        step(site, NOON + timedelta(minutes=2 * k), nac, cfg, 35.5)
        for e in site.ledger:           # the same number must always point to the SAME row over time
            assert seen.setdefault(e["seq"], (e["t"], e["event"], e["detail"])) == (e["t"], e["event"], e["detail"])
    assert site.ledger_pruned > 0
    assert site.ledger[-1]["seq"] == site.ledger_pruned + len(site.ledger)


def test_sweep_report_lists_this_sweeps_rows_even_after_pruning(site, nac):
    populate(site)
    cfg = Config()
    object.__setattr__(cfg.budget, "ledger_max_entries", 20)
    site.cfg = cfg
    for k in range(5):
        step(site, NOON + timedelta(minutes=2 * k), nac, cfg, 35.5)
    at = NOON + timedelta(minutes=10)
    r = step(site, at, nac, cfg, 35.5)
    assert r["ledger_added"], "bu taramanın satırları rapordan düşmemeli"
    assert all(e["t"] == at.isoformat().replace("+00:00", "Z") for e in r["ledger_added"])


def test_breach_started_row_is_part_of_the_sweep_report(site, nac):
    """The trigger stamp is applied to the report rows; the row where the breach started must not be left out."""
    populate(site)
    r = step(site, NOON, nac, site.cfg, 35.5)
    assert any(e["event"] == "breach_started" for e in r["ledger_added"])


def test_missing_coordinate_is_not_stored_as_zero_zero(site, nac):
    """If the location service returns no centre, the ledger must not write a fake coordinate like (0, 0)."""
    from nac_client.client import NacResult

    class NoCentre(Facade):
        def call(self, name, *a, **kw):
            if name == "location_retrieve":
                return NacResult(api=name, data={"area": {}}, source="fixture", latency_ms=0, correlator="x")
            return super().call(name, *a, **kw)

    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    w = site.workers["W-002"]
    w.escalated, w.state = True, "distress"
    step(site, NOON + timedelta(minutes=2), NoCentre(nac.nac), site.cfg, 35.5)
    rows = [e for e in site.ledger if e["event"] == "location_retrieve"]
    assert rows and all(not e.get("coarse") for e in rows)
    assert site.presence_record()["coordinates_stored"] == 0


# ------------------------------------------------------------------ geofence ordering
def test_a_late_stale_exit_does_not_override_a_newer_entry(site):
    populate(site, count=1)
    apply_geofence_event(site, "W-001", "enter", T0 + timedelta(hours=1))
    entry = apply_geofence_event(site, "W-001", "exit", T0 + timedelta(minutes=30))   # delivered late
    w = site.workers["W-001"]
    assert w.inside is True and w.exited_at is None
    assert entry["event"] == "geofence_out_of_order"


def test_the_budget_report_separates_planned_queries_from_distress_calls(site, nac):
    """Liveness, congestion and QoD calls are outside the plan. We do not hide the total; we break it down."""
    populate(site)
    step(site, NOON, nac, site.cfg, 35.5)
    r = step(site, NOON + timedelta(minutes=2), nac, site.cfg, 35.5)
    b = r["budget"]
    assert sum(b["by_pool"].values()) == b["spent"] == len(r["calls"])
    assert b["planned_spent"] == b["by_pool"].get("main", 0) + b["by_pool"].get("reserve", 0) <= b["per_sweep"]
    assert b["outside_plan"] == b["spent"] - b["planned_spent"]
    assert b["outside_plan"] > 0, "bu senaryoda sessiz cihazlar için plan dışı çağrı yapılır"
