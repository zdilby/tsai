"""
创建管理员账号。
用法：python -m scripts.create_admin
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
    username = input("管理员用户名: ").strip()
    if not username:
        print("用户名不能为空")
        return
    password = getpass.getpass("管理员密码: ")
    if not password:
        print("密码不能为空")
        return

    await database.connect()
    existing = await database.fetch_one(
        "SELECT id FROM users WHERE username = :u", values={"u": username}
    )
    if existing:
        # 已存在则升级为管理员
        await database.execute(
            "UPDATE users SET is_admin = TRUE, password_hash = :h WHERE username = :u",
            values={"h": pwd_context.hash(password), "u": username}
        )
        print(f"已将用户「{username}」更新为管理员")
    else:
        await database.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (:u, :h, TRUE)",
            values={"u": username, "h": pwd_context.hash(password)}
        )
        print(f"管理员账号「{username}」创建成功")
    await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
