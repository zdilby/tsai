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
    内部跑 backend.bot.run_bot_daily()——异步流程通过 asyncio.run 桥接同步 Celery。

    注意：必须 connect/disconnect 全局 `backend.db.database` 单例，不能创建新实例
    替换它——其他模块（bot.py / rag.py / agent_chat.py）在 import 时已经各自缓存
    了对原对象的引用，替换不会生效。
    """
    # 延迟导入：避免 Celery worker 启动时就触发完整的业务模块加载
    from .db import database
    from .bot import run_bot_daily

    async def _run():
        await database.connect()
        try:
            return await run_bot_daily()
        finally:
            await database.disconnect()

    try:
        result = asyncio.run(_run())
        logger.info("[bot] daily task finished: %s", result)
        return result
    except Exception as e:
        logger.exception("[bot] daily task failed")
        return {"error": str(e)}
