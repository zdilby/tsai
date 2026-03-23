"""
将卡在"处理中"状态的文件重置为"等待处理"，以便重新触发解析。
服务重启后原有后台任务已终止，但数据库状态仍停留在 processing。

用法：python -m scripts.reset_stuck_processing
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


async def main():
    await database.connect()

    rows = await database.fetch_all("""
        SELECT f.session_id, f.filename, s.name AS session_name
        FROM upload_files f
        LEFT JOIN sessions s ON s.id = f.session_id
        WHERE f.status = 'processing'
        ORDER BY f.session_id, f.filename
    """)

    if not rows:
        print("没有卡住的文件。")
        await database.disconnect()
        return

    print(f"发现 {len(rows)} 个卡在「解析中」的文件：")
    for r in rows:
        print(f"  [{r['session_name'] or r['session_id']}] {r['filename']}")

    print()
    confirm = input("将以上文件重置为「等待处理」？(y/N) ").strip().lower()
    if confirm != 'y':
        print("已取消。")
        await database.disconnect()
        return

    await database.execute("""
        UPDATE upload_files
        SET status = 'pending', total_chunks = 0, processed_chunks = 0, error_msg = NULL
        WHERE status = 'processing'
    """)

    await database.disconnect()
    print(f"已重置 {len(rows)} 个文件，前端点击「等待处理」即可重新解析。")


if __name__ == "__main__":
    asyncio.run(main())
