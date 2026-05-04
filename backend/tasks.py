"""
Celery 任务定义。

  ping                              - 联调用
  bot_run_daily_queries             - Phase 3b：机器人每日 5 个 query
  agent_b_analyze_pending_traces    - Phase 3c：每 12 小时分析 trace + 改 prompt
  agent_c_verify_prompt_change      - Phase 3d：每 4 小时验证 prompt 改动 + 自动回滚
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


@celery_app.task(name="agent_b_analyze_pending_traces", bind=True)
def agent_b_analyze_pending_traces(self) -> dict:
    """
    Agent B 周期任务（每 12 小时由 beat 触发，也可 admin 立即跑）。
    内部走异步分析流程；DB 单例 connect/disconnect 同 bot 模式。
    """
    from .db import database
    from .agent_b import run_agent_b_analysis

    async def _run():
        await database.connect()
        try:
            return await run_agent_b_analysis()
        finally:
            await database.disconnect()

    try:
        result = asyncio.run(_run())
        logger.info("[agent_b] task finished: %s", result)
        return result
    except Exception as e:
        logger.exception("[agent_b] task failed")
        return {"error": str(e)}


@celery_app.task(name="agent_c_verify_prompt_change", bind=True)
def agent_c_verify_prompt_change(self) -> dict:
    """
    Agent C 周期任务（每 4 小时由 beat 触发，也可 admin 立即跑）。
    验证 Agent B 应用的最新 prompt 是否真好；不好就自动回滚。
    """
    from .db import database
    from .agent_c import run_agent_c_verification

    async def _run():
        await database.connect()
        try:
            return await run_agent_c_verification()
        finally:
            await database.disconnect()

    try:
        result = asyncio.run(_run())
        logger.info("[agent_c] task finished: %s", result)
        return result
    except Exception as e:
        logger.exception("[agent_c] task failed")
        return {"error": str(e)}
