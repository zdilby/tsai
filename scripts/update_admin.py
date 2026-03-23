"""
修改管理员账号的用户名和/或密码。
用法：python -m scripts.update_admin
"""
import asyncio
import sys
import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def main():
    await database.connect()

    admins = await database.fetch_all(
        "SELECT id, username FROM users WHERE is_admin = TRUE ORDER BY id"
    )
    if not admins:
        print("当前没有管理员账号。")
        await database.disconnect()
        return

    print("当前管理员账号：")
    for a in admins:
        print(f"  [{a['id']}] {a['username']}")

    print()
    current = input("请输入要修改的管理员用户名: ").strip()
    if not current:
        print("用户名不能为空")
        await database.disconnect()
        return

    row = await database.fetch_one(
        "SELECT id FROM users WHERE username = :u AND is_admin = TRUE",
        values={"u": current},
    )
    if not row:
        print(f"未找到管理员「{current}」")
        await database.disconnect()
        return

    print()
    new_username = input(f"新用户名（留空保持「{current}」不变）: ").strip()
    new_password = getpass.getpass("新密码（留空不修改）: ")

    if not new_username and not new_password:
        print("未做任何修改。")
        await database.disconnect()
        return

    updates, values = [], {"id": row["id"]}
    if new_username:
        existing = await database.fetch_one(
            "SELECT id FROM users WHERE username = :u AND id != :id",
            values={"u": new_username, "id": row["id"]},
        )
        if existing:
            print(f"用户名「{new_username}」已被占用")
            await database.disconnect()
            return
        updates.append("username = :new_u")
        values["new_u"] = new_username
    if new_password:
        updates.append("password_hash = :h")
        values["h"] = pwd_context.hash(new_password)

    await database.execute(
        f"UPDATE users SET {', '.join(updates)} WHERE id = :id",
        values=values,
    )
    await database.disconnect()

    if new_username:
        print(f"用户名已改为「{new_username}」")
    if new_password:
        print("密码已更新")


if __name__ == "__main__":
    asyncio.run(main())
