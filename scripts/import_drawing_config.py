"""
把 export_drawing_config 导出的 JSON 导入到当前数据库（在服务器上运行）。

特点：
- 沿用原始 UUID 插入，风格里的 prompt_ids / skill_package_id 引用无需重新映射；
- 幂等：已存在（同 UUID）的记录默认跳过，可放心重复运行；加 --overwrite 则用文件内容覆盖已存在的记录；
- 风格按用户隔离：默认按"同名用户"映射到服务器用户，也可用 --user 统一指定；找不到用户的风格会被跳过并提示；
- 只导入配置，不含图片/生成历史。

用法（服务器项目根目录，使用与 gunicorn 相同的 venv）：
    python -m scripts.import_drawing_config drawing_config.json --dry-run
    python -m scripts.import_drawing_config drawing_config.json --user alice
    python -m scripts.import_drawing_config drawing_config.json --user alice --overwrite
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database, init_drawing_tables


def _ts(v):
    return datetime.fromisoformat(v) if v else None


async def _exists(table: str, id_: str) -> bool:
    return bool(await database.fetch_one(f"SELECT 1 FROM {table} WHERE id = :id", values={"id": id_}))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--user", help="服务器上的用户名；所有风格都归到该用户（不指定则按同名用户映射）")
    ap.add_argument("--overwrite", action="store_true", help="已存在的记录用文件内容覆盖（默认跳过）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    a = ap.parse_args()

    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        sys.exit(f"不支持的文件版本：{data.get('version')}")

    await database.connect()
    try:
        await init_drawing_tables()  # 幂等；确保表存在

        async def uid(username: str):
            r = await database.fetch_one("SELECT id FROM users WHERE username = :u", values={"u": username})
            return r["id"] if r else None

        forced_uid = None
        if a.user:
            forced_uid = await uid(a.user)
            if forced_uid is None:
                sys.exit(f"服务器上不存在用户「{a.user}」")

        stats = {"prompts": [0, 0, 0], "skill_packages": [0, 0, 0], "styles": [0, 0, 0]}  # 新增/覆盖/跳过
        skipped_users = set()

        async def upsert(kind, table, id_, insert_sql, update_sql, values):
            exists = await _exists(table, id_)
            if exists and not a.overwrite:
                stats[kind][2] += 1
                return
            stats[kind][1 if exists else 0] += 1
            if a.dry_run:
                return
            await database.execute(update_sql if exists else insert_sql, values=values)

        for p in data["prompts"]:
            await upsert(
                "prompts", "drawing_prompts", p["id"],
                """INSERT INTO drawing_prompts (id, name, content, created_by, created_at)
                   VALUES (:id, :name, :content, :by, COALESCE(:ts, NOW()))""",
                "UPDATE drawing_prompts SET name = :name, content = :content WHERE id = :id",
                {"id": p["id"], "name": p["name"], "content": p["content"], "by": forced_uid, "ts": _ts(p.get("created_at"))},
            )

        for s in data["skill_packages"]:
            mcp = json.dumps(s["mcp_config"], ensure_ascii=False) if s.get("mcp_config") is not None else None
            await upsert(
                "skill_packages", "drawing_skill_packages", s["id"],
                """INSERT INTO drawing_skill_packages
                     (id, name, source_url, kind, instructions, mcp_config, created_by, created_at)
                   VALUES (:id, :name, :src, :kind, :ins, CAST(:mcp AS jsonb), :by, COALESCE(:ts, NOW()))""",
                """UPDATE drawing_skill_packages SET name = :name, source_url = :src, kind = :kind,
                     instructions = :ins, mcp_config = CAST(:mcp AS jsonb) WHERE id = :id""",
                {"id": s["id"], "name": s["name"], "src": s.get("source_url") or "", "kind": s.get("kind") or "instruction",
                 "ins": s["instructions"], "mcp": mcp, "by": forced_uid, "ts": _ts(s.get("created_at"))},
            )

        for st in data["styles"]:
            owner_id = forced_uid or await uid(st["owner"])
            if owner_id is None:
                skipped_users.add(st["owner"])
                stats["styles"][2] += 1
                continue
            vals = {"id": st["id"], "uid": owner_id, "name": st["name"],
                    "pids": json.dumps(st.get("prompt_ids") or [], ensure_ascii=False),
                    "sk": st.get("skill_package_id"), "ts": _ts(st.get("created_at"))}
            await upsert(
                "styles", "drawing_styles", st["id"],
                """INSERT INTO drawing_styles (id, user_id, name, prompt_ids, skill_package_id, created_at)
                   VALUES (:id, :uid, :name, CAST(:pids AS jsonb), :sk, COALESCE(:ts, NOW()))""",
                """UPDATE drawing_styles SET name = :name, prompt_ids = CAST(:pids AS jsonb),
                     skill_package_id = :sk, updated_at = NOW() WHERE id = :id AND user_id = :uid""",
                vals,
            )

        label = {"prompts": "Prompt", "skill_packages": "Skill 包", "styles": "风格"}
        print(("【dry-run，未写库】" if a.dry_run else "导入完成：") + "（新增 / 覆盖 / 跳过）")
        for k, (n, o, s) in stats.items():
            print(f"  {label[k]}: {n} / {o} / {s}")
        if skipped_users:
            print(f"以下本地用户在服务器上不存在，其风格已跳过：{', '.join(sorted(skipped_users))}。可用 --user 指定目标用户重跑。")
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
