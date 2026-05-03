#!/usr/bin/env python3
"""
清空机器人的对话历史（messages + agent_traces）。

保留：
  • bot 用户
  • bot 的 sessions（snapshot 来的副本不动）
  • knowledge_base（每个 session 的文档 chunk 不动）
  • prompt_versions（prompt 版本历史不动）

清理：
  • messages 表中所有 bot session 下的对话
  • agent_traces 表中所有 bot user_id 的 trace
  • （可选 --also-clear-agent-b）agent_b_runs 表

用法：
    python -m scripts.clear_bot_history                    # 交互确认
    python -m scripts.clear_bot_history --yes              # 跳过确认
    python -m scripts.clear_bot_history --also-clear-agent-b   # 同时清 Agent B 运行历史
"""
import argparse
import asyncio

from backend.db import database
from settings import settings


async def main(skip_confirm: bool, also_agent_b: bool):
    await database.connect()
    try:
        # 找 bot 用户
        bot = await database.fetch_one(
            "SELECT id, username FROM users WHERE username = :n",
            values={"n": settings.bot_username},
        )
        if not bot:
            print(f"机器人用户 {settings.bot_username!r} 不存在，无需清理。")
            return

        # 拿 bot 所有 session id
        sessions = await database.fetch_all(
            "SELECT id, name FROM sessions WHERE user_id = :uid",
            values={"uid": bot["id"]},
        )
        session_ids = [r["id"] for r in sessions]

        # 统计待删除数据
        if session_ids:
            msg_count = await database.fetch_val(
                "SELECT COUNT(*) FROM messages WHERE session_id = ANY(:sids)",
                values={"sids": session_ids},
            )
        else:
            msg_count = 0

        trace_count = await database.fetch_val(
            "SELECT COUNT(*) FROM agent_traces WHERE user_id = :uid",
            values={"uid": bot["id"]},
        )
        agent_b_count = (
            await database.fetch_val("SELECT COUNT(*) FROM agent_b_runs")
            if also_agent_b else 0
        )

        sep = "=" * 60
        print()
        print(sep)
        print(f"将要清理机器人 (username={bot['username']!r}, id={bot['id']}) 的：")
        print(f"  • messages       : {msg_count} 条")
        print(f"  • agent_traces   : {trace_count} 条")
        if also_agent_b:
            print(f"  • agent_b_runs   : {agent_b_count} 条")
        print()
        print("保留：bot 用户、sessions、knowledge_base、prompt_versions、subsystem_status")
        print(sep)
        print()

        if not skip_confirm:
            ans = input("确认清理？输入 yes 继续，其它任意键取消：").strip().lower()
            if ans != "yes":
                print("已取消")
                return

        # 执行清理（按 FK 依赖顺序：先 traces 再 messages）
        async with database.transaction():
            await database.execute(
                "DELETE FROM agent_traces WHERE user_id = :uid",
                values={"uid": bot["id"]},
            )
            if session_ids:
                await database.execute(
                    "DELETE FROM messages WHERE session_id = ANY(:sids)",
                    values={"sids": session_ids},
                )
            if also_agent_b:
                await database.execute("DELETE FROM agent_b_runs")

        print(f"✓ 清理完成")
        print(f"  agent_traces 删除：{trace_count}")
        print(f"  messages 删除：{msg_count}")
        if also_agent_b:
            print(f"  agent_b_runs 删除：{agent_b_count}")
        print()
        print("现在去 /admin/perf 点几次「立即跑一次」，然后查 trace 看效果：")
        print()
        print("  psql \"$(grep '^DATABASE_URL' .env | cut -d= -f2- | sed 's|+asyncpg||')\" -c \"")
        print("  SELECT TO_CHAR(t.created_at, 'HH24:MI:SS') AS time,")
        print("         s.name AS session, LEFT(t.query, 40) AS query")
        print("  FROM agent_traces t JOIN users u ON u.id = t.user_id")
        print("  LEFT JOIN sessions s ON s.id = t.session_id")
        print(f"  WHERE u.username = '{settings.bot_username}'")
        print("  ORDER BY t.created_at DESC LIMIT 15;\"")

    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清空机器人对话历史")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument(
        "--also-clear-agent-b", action="store_true",
        help="同时清 Agent B 运行历史（agent_b_runs 表）",
    )
    args = parser.parse_args()
    asyncio.run(main(skip_confirm=args.yes, also_agent_b=args.also_clear_agent_b))
