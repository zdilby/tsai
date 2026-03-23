"""
清除所有知识库向量数据，并用磁盘上已有的文件重新生成 embedding 入库。
适用于更换 embedding 模型后需要全量重建的场景。

用法：python -m scripts.clear_knowledge_base
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database
from midware.upload import process_file_and_insert


async def main():
    await database.connect()

    rows = await database.fetch_all("""
        SELECT session_id, filename, filepath
        FROM upload_files
        WHERE status IN ('done', 'failed', 'processing')
        ORDER BY session_id, filename
    """)

    kb_count = await database.fetch_one("SELECT COUNT(*) FROM knowledge_base")
    print(f"知识库向量数：{kb_count[0]}")
    print(f"待重建文件数：{len(rows)}")

    if not rows:
        print("没有已处理的文件记录，无需操作。")
        await database.disconnect()
        return

    print()
    for r in rows:
        print(f"  [{r['session_id']}] {r['filename']}")

    print()
    confirm = input("确认清除所有向量并重新解析以上文件？(y/N) ").strip().lower()
    if confirm != 'y':
        print("已取消。")
        await database.disconnect()
        return

    # 1. 清除知识库
    await database.execute("DELETE FROM knowledge_base")
    print(f"\n已清除 {kb_count[0]} 条向量数据。")

    # 2. 逐文件重新解析
    ok, fail = 0, 0
    for r in rows:
        filepath = r['filepath']
        session_id = str(r['session_id'])
        filename = r['filename']

        if not filepath:
            print(f"  ⚠ 跳过（无文件路径）: {filename}")
            fail += 1
            continue

        # filepath 存储的是相对于项目根目录的路径，如 static/loads/...
        from settings import settings
        full_path = settings.base_dir / filepath
        if not full_path.exists():
            print(f"  ✗ 文件不存在，跳过: {filepath}")
            fail += 1
            continue

        print(f"  ⏳ 重新解析: {filename} ...")
        try:
            await process_file_and_insert(full_path, session_id)
            print(f"  ✓ 完成: {filename}")
            ok += 1
        except Exception as e:
            print(f"  ✗ 失败: {filename} — {e}")
            fail += 1

    await database.disconnect()
    print(f"\n完成：成功 {ok} 个，失败 {fail} 个。")


if __name__ == "__main__":
    asyncio.run(main())
