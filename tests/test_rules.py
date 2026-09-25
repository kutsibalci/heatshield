"""HeatShield kural motoru testleri — saf fonksiyonlar (ağ yok, saat parametre)."""
from datetime import datetime, timedelta, timezone

import pytest

from rules import Config, JURISDICTIONS, QATAR, SAUDI, UAE
from rules import heatshield as R

CFG = Config()
UTC = timezone.utc
# 2026-08-17 09:00 UTC = 12:00 Doha (yaz yasağı 10:00–15:30 içinde)
NOON = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
MORNING = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)     # 06:00 Doha
WINTER = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)      # yaz yasağı dışı


# ------------------------------------------------------------------ 1) site durumu
def test_below_limit_outside_ban_is_not_breached():
    st = R.site_state(30.0, MORNING, QATAR, CFG)
    assert st["breached"] is False and st["level"] == "none" and st["severity"] == 0.0
    assert "no authority to query" in st["reason"]


def test_summer_ban_hours_breach_even_when_cool():
    st = R.site_state(28.0, NOON, QATAR, CFG)
    assert st["breached"] and st["ban_breach"] and not st["wbgt_breach"]
    assert st["severity"] == 1.0 and st["level"] == "marginal"


def test_wbgt_above_limit_is_breach_and_severity_scales():
    marginal = R.site_state(32.6, MORNING, QATAR, CFG)
    severe = R.site_state(35.0, MORNING, QATAR, CFG)
    assert marginal["level"] == "marginal" and severe["level"] == "severe"
    assert severe["severity"] > marginal["severity"] > 1.0


def test_winter_still_breaches_on_wbgt_year_round():
    st = R.site_state(33.0, WINTER, QATAR, CFG)
    assert st["breached"] and st["wbgt_breach"] and not st["ban_breach"]


def test_jurisdictions_differ_at_the_same_instant():
    at = datetime(2026, 8, 17, 8, 45, tzinfo=UTC)  # 11:45 Doha/Riyad · 12:45 Dubai
    assert R.site_state(31.0, at, QATAR, CFG)["breached"] is True     # 10:00–15:30
    assert R.site_state(31.0, at, SAUDI, CFG)["breached"] is False    # 12:00–15:00
    assert R.site_state(31.0, at, UAE, CFG)["breached"] is True       # 12:30–15:00, UTC+4
    assert set(JURISDICTIONS) == {"QA", "SA", "AE"}


def test_sweep_interval_tightens_with_severity():
    assert R.sweep_interval_minutes("marginal", CFG) == 10
    assert R.sweep_interval_minutes("severe", CFG) == 2


# ------------------------------------------------------------------ 2) maruziyet skoru
def _worker(**kw):
    base = {"masked": "+99999***0001", "worker_id": "W-1", "last_signal_at": None, "first_day_on_site": None,
            "prior_incident": False, "shift": "day"}
    base.update(kw)
    return base


def test_first_week_worker_outranks_veteran():
    b = NOON - timedelta(minutes=20)
    rookie = R.exposure_score(_worker(first_day_on_site=(NOON - timedelta(days=2)).isoformat()), b, NOON, CFG, 1.0)
    veteran = R.exposure_score(_worker(first_day_on_site=(NOON - timedelta(days=500)).isoformat()), b, NOON, CFG, 1.0)
    assert rookie == pytest.approx(veteran * CFG.vulnerability.first_week)


def test_staleness_pushes_unseen_worker_to_the_top():
    b = NOON - timedelta(minutes=30)
    fresh = R.exposure_score(_worker(last_signal_at=(NOON - timedelta(minutes=2)).isoformat()), b, NOON, CFG)
    stale = R.exposure_score(_worker(last_signal_at=(NOON - timedelta(minutes=25)).isoformat()), b, NOON, CFG)
    assert stale > fresh


def test_severity_multiplies_score():
    b = NOON - timedelta(minutes=10)
    assert R.exposure_score(_worker(), b, NOON, CFG, 2.0) == pytest.approx(2 * R.exposure_score(_worker(), b, NOON, CFG, 1.0))


def test_exposure_detail_explains_every_factor():
    d = R.exposure_detail(_worker(prior_incident=True), NOON - timedelta(minutes=15), NOON, CFG, 1.5)
    signals = [e.signal for e in d.explain]
    assert {"severity", "exposure_minutes", "staleness", "vulnerability"} <= set(signals)
    assert d.meta["vulnerability"] == CFG.vulnerability.prior_incident


def test_exposure_clock_starts_at_the_workers_own_entry():
    """İhlal 12:00'de başladı, işçi 12:30'da girdi → 13:00'te maruziyeti 60 değil 30 dakika."""
    breach = NOON - timedelta(minutes=60)
    late = _worker(worker_id="W-LATE", entered_at=(NOON - timedelta(minutes=30)).isoformat(),
                   last_signal_at=(NOON - timedelta(minutes=30)).isoformat())
    early = _worker(worker_id="W-EARLY", entered_at=(NOON - timedelta(hours=4)).isoformat(),
                    last_signal_at=(NOON - timedelta(minutes=30)).isoformat())
    _, late_min, src, _ = R.exposure_window(late, breach, NOON)
    _, early_min, early_src, _ = R.exposure_window(early, breach, NOON)
    assert late_min == 30 and src == "entered_at"
    assert early_min == 60 and early_src == "breach_started_at"   # ihlalden önce sahadaydı
    # aynı bayatlık + aynı kırılganlık: skor farkı yalnızca maruziyetten gelir
    assert R.exposure_score(late, breach, NOON, CFG) == pytest.approx(R.exposure_score(early, breach, NOON, CFG) / 2)


def test_missing_entry_signal_never_underestimates_exposure():
    """Giriş anını bildiren alan yoksa ihlal başlangıcı kullanılır (temkinli taraf)."""
    breach = NOON - timedelta(minutes=45)
    start, minutes, src, why = R.exposure_window(_worker(last_signal_at=(NOON - timedelta(minutes=5)).isoformat()), breach, NOON)
    assert start == breach and minutes == 45 and src == "breach_started_at" and "cautious" in why
    d = R.exposure_detail(_worker(entered_at=(NOON - timedelta(minutes=15)).isoformat()), breach, NOON, CFG)
    assert d.meta["exposure_minutes"] == 15 and d.meta["exposure_source"] == "entered_at"
    assert any(e.signal == "exposure_start" and e.triggered for e in d.explain)


def test_severity_stays_a_site_wide_factor():
    """WBGT saha özelliğidir: aynı sahada iki işçinin severity'si aynı olmalı (tasarım gereği)."""
    b = NOON - timedelta(minutes=20)
    a1 = _worker(worker_id="W-A", entered_at=(NOON - timedelta(minutes=10)).isoformat())
    a2 = _worker(worker_id="W-B", prior_incident=True)
    sev = [e.value for w in (a1, a2) for e in R.exposure_detail(w, b, NOON, CFG, 2.4).explain if e.signal == "severity"]
    assert sev == [2.4, 2.4]


# ------------------------------------------------------------------ 3) doğrulama planı
def _cand(i, score, **kw):
    w = _worker(worker_id=f"W-{i:03d}", masked=f"+99999***{i:04d}", score=score)
    w.update(kw)
    return w


def test_plan_spends_from_the_top_and_stops_at_budget():
    workers = [_cand(i, 100 - i) for i in range(1, 21)]
    plan = R.plan_verification(workers, 10, CFG, NOON)
    assert len([a for a in plan if a.pool == "main"]) == 9   # 10 bütçe − 1 rezerv
    assert [a.worker["worker_id"] for a in plan][:3] == ["W-001", "W-002", "W-003"]
    assert all(a.api == "location_verify" for a in plan)


def test_plan_climbs_cost_ladder_only_when_needed():
    verified = _cand(1, 50, verified_inside=True, last_verified_at=(NOON - timedelta(minutes=1)).isoformat())
    plan = R.plan_verification([verified], 10, CFG, NOON)
    assert plan[0].api == "reachability"
    escalated = _cand(2, 50, escalated=True)
    assert R.plan_verification([escalated], 10, CFG, NOON)[0].api == "location_retrieve"


def test_fresh_verification_is_not_repeated():
    w = _cand(1, 50, verified_inside=True, last_verified_at=(NOON - timedelta(seconds=30)).isoformat(),
              reachable=True, last_reachability_at=(NOON - timedelta(seconds=10)).isoformat())
    assert R.plan_verification([w], 10, CFG, NOON) == []


def test_cleared_worker_is_dropped_from_the_queue():
    assert R.plan_verification([_cand(1, 99, cleared=True)], 10, CFG, NOON) == []


def test_equal_scores_are_broken_by_staleness_not_by_the_alphabet():
    """Skorlar eşitlendiğinde sıra alfabetik telefona düşmemeli: en bayat sinyal önce."""
    fresh = _cand(1, 40.0, last_signal_at=(NOON - timedelta(minutes=2)).isoformat())    # +99999***0001
    stale = _cand(9, 40.0, last_signal_at=(NOON - timedelta(minutes=40)).isoformat())   # +99999***0009
    never = _cand(5, 40.0, last_signal_at=None)                                          # hiç sinyal yok
    plan = R.plan_verification([fresh, stale, never], 10, CFG, NOON)
    assert [a.worker["worker_id"] for a in plan] == ["W-005", "W-009", "W-001"]
    tb = [e for e in plan[0].explain if e.signal == "tiebreak"]
    assert tb and tb[0].triggered and "alphabetical" in tb[0].note
    # eşitlik yoksa tiebreak kaydı da yok (gereksiz gürültü üretmez)
    solo = R.plan_verification([_cand(1, 40.0), _cand(2, 10.0)], 10, CFG, NOON)
    assert not [e for a in solo for e in a.explain if e.signal == "tiebreak"]


def test_reserve_rechecks_presumed_safe_workers():
    cands = [_cand(i, 10) for i in range(1, 4)]
    safe = [_cand(90 + i, 0, last_signal_at=(NOON - timedelta(hours=i)).isoformat()) for i in range(1, 4)]
    plan = R.plan_verification(cands, 10, CFG, NOON, presumed_safe=safe)
    reserve = [a for a in plan if a.pool == "reserve"]
    assert len(reserve) == 1 and reserve[0].worker["worker_id"] == "W-093"  # en bayat olan


# ------------------------------------------------------------------ 4) bayılma vs pil
def test_single_dark_device_with_reachable_neighbours_is_probable_collapse():
    r = R.classify_unreachable(_worker(vulnerability=2.0), 4, 0, "Low", {"continuous_reachable_hours": 8}, False, CFG)
    assert r["verdict"] == "probable_collapse" and r["confidence"] >= CFG.distress.collapse_min_confidence


def test_cluster_of_dark_devices_is_a_network_event():
    r = R.classify_unreachable(_worker(), 0, 3, "High", {}, False, CFG)
    assert r["verdict"] == "probable_network"
    assert R.escalate(r["verdict"], r["confidence"], CFG, "High").actions == ["provision_slice", "log_only"]


def test_recurring_silence_window_is_a_flat_battery():
    r = R.classify_unreachable(_worker(), 2, 0, "Low", {"recurring_unreachable_window": True}, False, CFG)
    assert r["verdict"] == "probable_battery"


def test_device_moving_towards_the_exit_probably_left():
    r = R.classify_unreachable(_worker(), 2, 0, "Low", {}, True, CFG)
    assert r["verdict"] == "probable_left" and R.escalate(r["verdict"], r["confidence"], CFG).actions == ["log_only"]


def test_low_confidence_collapse_is_rechecked_not_escalated():
    d = R.escalate("probable_collapse", 0.5, CFG)
    assert d.actions == ["recheck_reachability", "log_only"] and "notify_medic" not in d.actions


def test_high_confidence_collapse_calls_medic_and_qod():
    d = R.escalate("probable_collapse", 0.9, CFG, "High")
    assert d.actions[:3] == ["notify_medic", "request_qod", "location_retrieve"] and "provision_slice" in d.actions


# ------------------------------------------------------------------ config
def test_config_from_env_and_jurisdiction_switch(monkeypatch):
    monkeypatch.setenv("HS_JURISDICTION", "AE")
    monkeypatch.setenv("HS_BUDGET_PER_MIN", "40")
    monkeypatch.setenv("HS_WBGT_LIMIT_C", "31.0")
    c = Config.from_env()
    assert c.jurisdiction.code == "AE" and c.budget.queries_per_site_per_minute == 40
    assert c.jurisdiction.wbgt_limit_c == 31.0
    assert c.with_jurisdiction("SA").jurisdiction.code == "SA"
    assert "budget" in c.to_dict() and "distress" in c.to_dict()


def test_env_wbgt_limit_survives_a_jurisdiction_switch(monkeypatch):
    """Her saha `with_jurisdiction()` üzerinden kurulur; ortamdaki eşik orada kaybolmamalı."""
    monkeypatch.setenv("HS_WBGT_LIMIT_C", "30.0")
    c = Config.from_env()
    assert c.with_jurisdiction("QA").jurisdiction.wbgt_limit_c == 30.0
    assert c.with_jurisdiction("SA").jurisdiction.wbgt_limit_c == 30.0
    assert Config().with_jurisdiction("QA").jurisdiction.wbgt_limit_c == 32.1   # ortam yoksa yasal değer


def test_medic_coordinate_is_not_starved_by_unseen_workers():
    """Eskalasyon sonrası koordinat sağlıkçının koşacağı yerdir; hiç sorgulanmamışların arkasında beklemez."""
    breach = NOON - timedelta(minutes=30)
    escalated = _cand(999, 1.0, escalated=True, last_verified_at=(NOON - timedelta(minutes=5)).isoformat())
    unseen = [_cand(i, 50) for i in range(1, 41)]
    plan = R.plan_verification(unseen + [escalated], 20, CFG, NOON, breach_started_at=breach)
    assert plan[0].api == "location_retrieve" and plan[0].worker["worker_id"] == "W-999"
    assert len([a for a in plan if a.pool == "main"]) == 18          # bütçe yine aşılmaz
