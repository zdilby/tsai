"""
显示所有处理失败的文件及其错误信息。
用法：python -m scripts.show_file_errors
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


async def main():
    await database.connect()
    rows = await database.fetch_all("""
        SELECT f.session_id, f.filename, f.status, f.error_msg, s.name AS session_name
        FROM upload_files f
        LEFT JOIN sessions s ON s.id = f.session_id
        WHERE f.status IN ('failed', 'pending')
        ORDER BY f.session_id, f.filename
    """)
    await database.disconnect()

    if not rows:
        print("没有失败或等待处理的文件。")
        return

    for r in rows:
        status = "❌ 失败" if r['status'] == 'failed' else "⏸ 等待"
        print(f"{status}  [{r['session_name'] or r['session_id']}]  {r['filename']}")
        if r['error_msg']:
            print(f"       错误: {r['error_msg']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
