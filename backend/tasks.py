"""
Celery 任务定义 — Phase 3a 仅一个 ping 任务用于联调验证。

后续阶段添加：
  bot_run_daily_queries        Phase 3b
  agent_b_analyze_pending_traces Phase 3c
  agent_c_verify_prompt_change Phase 3d
"""
from .celery_app import celery_app


@celery_app.task(name="ping")
def ping() -> str:
    """
    联调验证任务。
    用法：
        from backend.tasks import ping
        result = ping.delay()
        print(result.get(timeout=5))   # → "pong"
    或命令行：
        celery -A backend.celery_app call ping
    """
    return "pong"
