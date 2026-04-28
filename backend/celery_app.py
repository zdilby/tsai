"""
Celery + Redis 调度框架 — Phase 3a 仅搭骨架，不跑任务。

后续阶段：
  Phase 3b: bot 用户每日 5 个 query → @celery_app.task 并 beat 调度
  Phase 3c: Agent B 周期性扫 agent_traces.analyzed_at IS NULL → 分析 + 改 prompt
  Phase 3d: Agent C prompt 改动后跑验证集 + 计算 score + 自动回滚

启动 worker（开发期）：
    celery -A backend.celery_app worker --loglevel=info

启动 beat（周期任务调度，3b 之后才需要）：
    celery -A backend.celery_app beat --loglevel=info
"""
import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "tsai",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["backend.tasks"],
)

celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    task_time_limit=600,           # 单任务最多 10 分钟
    task_soft_time_limit=540,      # 9 分钟软限
    worker_prefetch_multiplier=1,  # 每次拉一个任务，避免长任务阻塞
    # beat_schedule 留空，后续阶段添加
    beat_schedule={},
)
