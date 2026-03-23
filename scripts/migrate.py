"""
数据库迁移脚本：为已有库补充新增字段。
幂等操作，重复执行安全。
用法：python -m scripts.migrate
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


MIGRATIONS = [
    # upload_files 表：新增文件处理状态字段
    ("upload_files.status",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'"),
    ("upload_files.total_chunks",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS total_chunks INTEGER DEFAULT 0"),
    ("upload_files.processed_chunks",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS processed_chunks INTEGER DEFAULT 0"),
    ("upload_files.error_msg",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS error_msg TEXT"),

    # knowledge_base 表：新增语义分块相关字段
    ("knowledge_base.original_content",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS original_content TEXT"),
    ("knowledge_base.source_file",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_file TEXT"),
    ("knowledge_base.chunk_index",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS chunk_index INTEGER DEFAULT 0"),

    # messages 表：新增 token 统计字段
    ("messages.tokens_in",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_in INTEGER DEFAULT 0"),
    ("messages.tokens_out",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_out INTEGER DEFAULT 0"),
    ("messages.tokens_total",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_total INTEGER DEFAULT 0"),

    # users 表：新增管理员与每日配额字段
    ("users.is_admin",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"),
    ("users.max_daily_tokens",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_daily_tokens INTEGER DEFAULT 200000"),
]


async def main():
    await database.connect()
    print("开始迁移...")
    for name, sql in MIGRATIONS:
        try:
            await database.execute(sql)
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            await database.disconnect()
            sys.exit(1)
    # 将现有用户中 max_daily_tokens = 0 的统一更新为新默认值
    result = await database.execute(
        "UPDATE users SET max_daily_tokens = 200000 WHERE max_daily_tokens = 0 AND is_admin = FALSE"
    )
    print(f"  ✓ 已将旧默认值 0 的用户配额更新为 200000")

    await database.disconnect()
    print("迁移完成。")


if __name__ == "__main__":
    asyncio.run(main())
