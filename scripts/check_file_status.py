"""
查询 upload_files 表里指定文件（或全部文件）的完整处理状态，不受状态过滤限制
（show_file_errors.py 只看 failed/pending，这个脚本能看到 processing/done 等全部状态）。

用法：
  python -m scripts.check_file_status                    # 列出所有文件
  python -m scripts.check_file_status 资治通鉴全译本柏杨版.epub   # 按文件名模糊匹配
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


async def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else None

    await database.connect()
    if keyword:
        rows = await database.fetch_all(
            """
            SELECT f.session_id, f.filename, f.status, f.total_chunks,
                   f.processed_chunks, f.error_msg, f.created_at, s.name AS session_name
            FROM upload_files f
            LEFT JOIN sessions s ON s.id = f.session_id
            WHERE f.filename ILIKE :kw
            ORDER BY f.created_at DESC
            """,
            values={"kw": f"%{keyword}%"},
        )
    else:
        rows = await database.fetch_all(
            """
            SELECT f.session_id, f.filename, f.status, f.total_chunks,
                   f.processed_chunks, f.error_msg, f.created_at, s.name AS session_name
            FROM upload_files f
            LEFT JOIN sessions s ON s.id = f.session_id
            ORDER BY f.created_at DESC
            """
        )
    await database.disconnect()

    if not rows:
        print("没有匹配的记录。")
        return

    for r in rows:
        print(f"[{r['session_name'] or r['session_id']}] {r['filename']}")
        print(f"  session_id       : {r['session_id']}")
        print(f"  status           : {r['status']}")
        print(f"  total_chunks     : {r['total_chunks']}")
        print(f"  processed_chunks : {r['processed_chunks']}")
        print(f"  created_at       : {r['created_at']}")
        if r['error_msg']:
            print(f"  error_msg        : {r['error_msg']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
