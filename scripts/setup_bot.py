#!/usr/bin/env python3
"""
Phase 3b — 机器人初始化脚本（一次性）。

部署后执行一次：
    python -m scripts.setup_bot                  # 创建 bot user + snapshot 天书的 session
    python -m scripts.setup_bot --no-snapshot    # 仅创建用户，不复刻 session

幂等性：
    • 用户已存在不会重复创建
    • snapshot 不幂等！重复跑会产生重复副本——只在确认要重置时再跑
"""
import argparse
import asyncio

from backend.db import database, init_phase3_tables
from backend.bot import ensure_bot_user, snapshot_user_sessions
from settings import settings


async def main(do_snapshot: bool):
    await database.connect()
    try:
        # Phase 3a 表（idempotent）
        await init_phase3_tables()

        # 创建机器人用户
        bot = await ensure_bot_user()
        print(f"✓ 机器人用户：id={bot['id']}, username={bot['username']!r}")

        if not do_snapshot:
            print("跳过 snapshot（--no-snapshot）")
            return

        # 复刻
        print(f"开始复刻 {settings.bot_source_username!r} 的 named sessions...")
        result = await snapshot_user_sessions(settings.bot_source_username, bot["id"])
        print(f"✓ 复刻完成：{result['sessions_copied']} sessions / {result['chunks_copied']} chunks")
        print()
        print("接下来：")
        print("  1. 启动 Celery worker：celery -A backend.celery_app worker -l info &")
        print("  2. 启动 Celery beat：celery -A backend.celery_app beat -l info &")
        print(f"  3. 进 /admin/perf 点击「启用每日自动 query」")
        print(f"  4. 立即试跑：点击「立即跑一次」")
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="初始化 TSAI 机器人用户 + snapshot")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="只创建用户，不复刻 session")
    args = parser.parse_args()
    asyncio.run(main(do_snapshot=not args.no_snapshot))
