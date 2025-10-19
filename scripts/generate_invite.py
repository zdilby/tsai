#!/usr/bin/env python3
import asyncio
import uuid
from backend.db import database, init_db, save_message, get_context, add_knowledge, session_exists

async def main():
    # 生成邀请码
    code = str(uuid.uuid4())
    await database.connect()
    await database.execute(
        "INSERT INTO invite_codes (code) VALUES (:c)",
        values={"c": code}
    )
    await database.disconnect()
    print("生成成功的邀请码：", code)

if __name__ == "__main__":
    asyncio.run(main())
