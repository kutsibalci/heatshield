"""The agent's own clock: a background task that asks every site whether a sweep is due."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import suppress
from datetime import datetime

from .runtime import _iso, _now, _stamp_trigger, facade, nac, store
from agent import step, sweep_due

log = logging.getLogger("heatshield.api")


# ============================================================================ scheduler (the agent's own clock)
def _env_flag(name: str) -> bool | None:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


class Scheduler:
    """The agent's own clock — the code behind the claim "The agent decides. Nobody presses a button."

    The cadence logic (`agent.policy.sweep_due`) was written but nothing called it: sweeps were triggered
    ONLY from outside via `POST /v1/sites/{id}/sweep`. This task closes that gap — a background
    task wakes periodically, asks `sweep_due()` for each site, and runs the sweep when needed.

    Rules:
    - Each site is swept under its own lock (see `Store`); a tick does not hold the global lock throughout.
    - Blocking work (synchronous Nokia calls) runs OUTSIDE the event loop via `asyncio.to_thread`.
    - An error does NOT KILL the task: it is counted, logged, and the next tick carries on.
    - A site with no WBGT reading entered has no decision to make — no wasted work.
    - Default: OFF under tests and in fixture mode (so demos stay deterministic); explicitly turned on/off
      with `HS_SCHEDULER`, wake interval `HS_SCHEDULER_TICK_S` (default 30 s).
    """

    def __init__(self) -> None:
        self.enabled = False
        self.reason = "not started"
        self.tick_s = 30.0
        self.task: asyncio.Task | None = None
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.ticks = 0
        self.sweeps_run = 0
        self.errors = 0
        self.last_error: str | None = None

    # ---- configuration
    @staticmethod
    def tick_seconds() -> float:
        raw = os.environ.get("HS_SCHEDULER_TICK_S", "30")
        try:
            return max(0.01, float(raw))
        except ValueError:
            log.warning("HS_SCHEDULER_TICK_S is not a number: %r — falling back to 30 s", raw)
            return 30.0

    def _decide(self) -> tuple[bool, str]:
        flag = _env_flag("HS_SCHEDULER")
        mode = getattr(nac.cfg, "mode", "fixture")
        if flag is not None:
            return flag, f"HS_SCHEDULER={os.environ.get('HS_SCHEDULER')!r}"
        if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
            return False, "default: off under pytest (the test suite stays deterministic)"
        if mode == "fixture":
            return False, "default: off in fixture mode (demos stay deterministic) — set HS_SCHEDULER=1 to run it"
        return True, f"default: on for NAC_MODE={mode}"

    # ---- lifecycle
    def start(self) -> None:
        self.enabled, self.reason = self._decide()
        self.tick_s = self.tick_seconds()
        self.ticks = self.sweeps_run = self.errors = 0
        self.last_error = None
        self.last_tick_at = None
        if not self.enabled:
            log.info("scheduler disabled (%s)", self.reason)
            return
        self.started_at = _now()
        self.task = asyncio.create_task(self._loop(), name="heatshield-scheduler")
        log.info("scheduler started tick=%ss (%s)", self.tick_s, self.reason)

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("scheduler stopped after %d ticks (%d sweeps, %d errors)", self.ticks, self.sweeps_run, self.errors)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick_s)
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as e:          # noqa: BLE001 — the task NEVER dies
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("scheduler tick failed — the loop continues")

    # ---- one tick
    def tick(self, now: datetime | None = None) -> list[dict]:
        """One wake-up: ask `sweep_due()` for each site and sweep if needed. Can be called directly from tests."""
        at = now or _now()
        ran: list[dict] = []
        self.ticks += 1
        self.last_tick_at = at
        with store.lock:                               # don't see a site a running demo is still building
            sites = list(store.sites.values())
        for site in sites:
            with site.lock:                            # per site: a slow site does not hold up another
                if site.wbgt_c is None:
                    continue                                   # no reading → no decision to make
                if not sweep_due(site, at, site.cfg):
                    continue                                   # cadence not due yet
                try:
                    r = _stamp_trigger(step(site, at, facade, site.cfg, None), site, at, "scheduler")
                except Exception as e:      # noqa: BLE001 — if one site crashes the others keep being swept
                    self.errors += 1
                    self.last_error = f"{site.site_id}: {type(e).__name__}: {e}"
                    log.exception("scheduled sweep failed site=%s — the loop continues", site.site_id)
                    continue
            self.sweeps_run += 1
            ran.append({"site_id": site.site_id, "at": _iso(at), "breached": r.get("breached"),
                        "level": r.get("level"), "queries": len(r.get("calls") or []),
                        "next_due_in_min": r.get("sweep_interval_min")})
        return ran

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "reason": self.reason, "tick_seconds": self.tick_s,
            "running": bool(self.task and not self.task.done()),
            "started_at": _iso(self.started_at), "last_tick_at": _iso(self.last_tick_at),
            "ticks": self.ticks, "sweeps_run": self.sweeps_run, "errors": self.errors, "last_error": self.last_error,
            "env": {"HS_SCHEDULER": os.environ.get("HS_SCHEDULER"), "HS_SCHEDULER_TICK_S": os.environ.get("HS_SCHEDULER_TICK_S")},
        }


scheduler = Scheduler()
