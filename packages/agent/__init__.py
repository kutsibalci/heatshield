"""agent — HeatShield agent policy (state machine + verification budget + LLM planner).

Two layers, and their order matters:

  RULES (packages/rules)     — deterministic pure functions. Threshold, score, budget, verdict.
                               Testable, explainable, predictable.
  MODEL (llm_adapter.py)     — only proposes a RE-ORDERING of the plan the rules produced.
                               It cannot add a worker, pick an API, exceed the budget or overturn a verdict.
                               If its proposal does not pass the `guard_plan()` gate it is dropped entirely.

Without a key the model is disabled and the system runs fully deterministically — the model is an
improvement, not a dependency. In both cases the decision is visible in the evidence ledger.
"""
from .llm_adapter import ChainPlanner, LLMPlanner, RulesPlanner, get_planner, guard_plan
from .policy import SiteRuntime, WorkerRuntime, apply_geofence_event, new_worker, reset_planner, step, sweep_due, STATES

__all__ = ["SiteRuntime", "WorkerRuntime", "step", "apply_geofence_event", "new_worker", "sweep_due", "STATES",
           "get_planner", "guard_plan", "RulesPlanner", "LLMPlanner", "ChainPlanner", "reset_planner"]
