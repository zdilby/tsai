"""
Celery 任务定义。

  ping                  - 联调用
  bot_run_daily_queries - Phase 3b：机器人每日 5 个 query

后续阶段添加：
  agent_b_analyze_pending_traces Phase 3c
  agent_c_verify_prompt_change   Phase 3d
"""
import asyncio

from settings import logger
from .celery_app import celery_app


@celery_app.task(name="ping")
def ping() -> str:
    """联调验证任务。celery -A backend.celery_app call ping → 'pong'"""
    return "pong"


@celery_app.task(name="bot_run_daily_queries", bind=True)
def bot_run_daily_queries(self) -> dict:
    """
    机器人每日入口任务。Celery beat 按 beat_schedule 触发。
    内部完整跑一次 backend.bot.run_bot_daily()——异步流程通过 asyncio.run 桥接同步 Celery。
    """
    # 延迟导入：tasks.py 在 Celery 启动时就被加载，但 backend.bot 的依赖（fastapi/databases）
    # 不应该在导入时就连 DB——这里到执行时才引入并连接。
    from databases import Database
    from settings import settings as _settings
    from .bot import run_bot_daily

    async def _run():
        db = Database(_settings.database_url)
        await db.connect()
        # 全局 database 引用要共享同一个连接池
        from . import db as db_mod
        db_mod.database = db
        try:
            return await run_bot_daily()
        finally:
            await db.disconnect()

    try:
        result = asyncio.run(_run())
        logger.info("[bot] daily task finished: %s", result)
        return result
    except Exception as e:
        logger.exception("[bot] daily task failed")
        return {"error": str(e)}
