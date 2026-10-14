"""
导出作图模块的配置（Prompt 库、Skill 包、作图风格）到一个 JSON 文件，供 import_drawing_config 在
服务器上一次性导入。只导出配置，不含已生成的图片/历史链（drawing_generations）。

用法（在本地项目根目录，使用与 gunicorn 相同的 venv）：
    python -m scripts.export_drawing_config                       # 输出 drawing_config.json
    python -m scripts.export_drawing_config -o my.json
    python -m scripts.export_drawing_config --user alice          # 只导出该用户的风格
"""
import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


def _ser(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v) if hasattr(v, "hex") and not isinstance(v, (str, bytes)) else v  # uuid.UUID → str


def _row(r, jsonb_cols=()):
    d = {k: _ser(v) for k, v in dict(r).items()}
    for c in jsonb_cols:
        if isinstance(d.get(c), str):
            d[c] = json.loads(d[c])
    return d


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="drawing_config.json")
    ap.add_argument("--user", help="只导出该用户名的风格（Prompt/Skill 是共享库，始终全量导出）")
    a = ap.parse_args()

    await database.connect()
    try:
        prompts = await database.fetch_all(
            "SELECT id, name, content, created_at FROM drawing_prompts ORDER BY created_at"
        )
        skills = await database.fetch_all(
            "SELECT id, name, source_url, kind, instructions, mcp_config, created_at "
            "FROM drawing_skill_packages ORDER BY created_at"
        )
        sql = (
            "SELECT s.id, u.username AS owner, s.name, s.prompt_ids, s.skill_package_id, s.created_at "
            "FROM drawing_styles s JOIN users u ON u.id = s.user_id"
        )
        vals = {}
        if a.user:
            sql += " WHERE u.username = :u"
            vals["u"] = a.user
        styles = await database.fetch_all(sql + " ORDER BY s.created_at", values=vals)
    finally:
        await database.disconnect()

    data = {
        "version": 1,
        "prompts": [_row(r) for r in prompts],
        "skill_packages": [_row(r, ("mcp_config",)) for r in skills],
        "styles": [_row(r, ("prompt_ids",)) for r in styles],
    }
    Path(a.output).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已导出 → {a.output}：Prompt {len(data['prompts'])} 条，"
        f"Skill 包 {len(data['skill_packages'])} 个，风格 {len(data['styles'])} 个"
    )
    owners = sorted({s["owner"] for s in data["styles"]})
    if owners:
        print(f"风格所属本地用户：{', '.join(owners)}（导入时可用 --user 统一指定服务器用户）")


if __name__ == "__main__":
    asyncio.run(main())
