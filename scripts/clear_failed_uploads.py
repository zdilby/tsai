"""
清除所有处理失败的上传文档。
对每条失败记录：删除磁盘文件、清除 knowledge_base 残留向量、删除 upload_files 记录。
清除后用户可重新上传同名文件。

用法：python -m scripts.clear_failed_uploads
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database
from settings import settings


async def main():
    await database.connect()

    rows = await database.fetch_all("""
        SELECT f.session_id, f.filename, f.filepath, f.error_msg
        FROM upload_files f
        WHERE f.status = 'failed'
        ORDER BY f.session_id, f.filename
    """)

    if not rows:
        print("没有失败的文档记录。")
        await database.disconnect()
        return

    print(f"找到 {len(rows)} 条失败记录：")
    for r in rows:
        print(f"  [{r['session_id']}] {r['filename']}  —  {r['error_msg'] or '无错误信息'}")

    print()
    confirm = input("确认全部清除？(y/N) ").strip().lower()
    if confirm != 'y':
        print("已取消。")
        await database.disconnect()
        return

    deleted_files = 0
    deleted_kb = 0
    deleted_records = 0

    for r in rows:
        session_id = r['session_id']
        filename = r['filename']
        filepath = r['filepath']

        # 1. 删除磁盘文件
        if filepath:
            full_path = settings.base_dir / filepath
            if full_path.exists():
                full_path.unlink()
                deleted_files += 1
                print(f"  已删除文件: {filepath}")
            # 同时删除可能生成的 .md 文件（PDF/EPUB 转换产物）
            md_path = full_path.with_suffix('.md')
            if md_path.exists():
                md_path.unlink()
                print(f"  已删除中间文件: {md_path.name}")

        # 2. 清除 knowledge_base 中该文件的残留向量
        result = await database.execute(
            "DELETE FROM knowledge_base WHERE session_id = :sid AND source_file = :fn",
            values={"sid": str(session_id), "fn": filename}
        )
        kb_count = result if isinstance(result, int) else 0
        if kb_count:
            deleted_kb += kb_count
            print(f"  已清除向量: {filename} ({kb_count} 条)")

        # 3. 删除 upload_files 记录
        await database.execute(
            "DELETE FROM upload_files WHERE session_id = :sid AND filename = :fn",
            values={"sid": str(session_id), "fn": filename}
        )
        deleted_records += 1

    await database.disconnect()
    print()
    print(f"完成：删除记录 {deleted_records} 条，删除文件 {deleted_files} 个，清除向量 {deleted_kb} 条。")
    print("请重新上传这些文档。")


if __name__ == "__main__":
    asyncio.run(main())
