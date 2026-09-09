"""agent — HeatShield ajan politikası (durum makinesi + doğrulama bütçesi + LLM planlayıcı).

İki katman, ve sırası önemli:

  KURALLAR (packages/rules)  — deterministik saf fonksiyonlar. Eşik, skor, bütçe, hüküm.
                               Test edilebilir, açıklanabilir, öngörülebilir.
  MODEL (llm_adapter.py)     — kuralların ürettiği planı yalnızca YENİDEN SIRALAMAYI önerir.
                               Yeni işçi ekleyemez, API seçemez, bütçeyi aşamaz, hüküm bozamaz.
                               Önerisi `guard_plan()` kapısından geçmezse bütünüyle düşer.

Anahtar yoksa model devre dışıdır ve sistem tamamen deterministik çalışır — model bir
iyileştirmedir, bağımlılık değil. Her iki durumda da karar kanıt defterinde görünür.
"""
from .llm_adapter import LLMPlanner, RulesPlanner, get_planner, guard_plan
from .policy import SiteRuntime, WorkerRuntime, apply_geofence_event, new_worker, reset_planner, step, sweep_due, STATES

__all__ = ["SiteRuntime", "WorkerRuntime", "step", "apply_geofence_event", "new_worker", "sweep_due", "STATES",
           "get_planner", "guard_plan", "RulesPlanner", "LLMPlanner", "reset_planner"]
