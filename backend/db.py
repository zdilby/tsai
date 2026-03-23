import os
from databases import Database
from settings import settings
from pgvector.asyncpg import register_vector, Vector

DATABASE_URL = settings.database_url
database = Database(DATABASE_URL)


async def init_db():
    await database.connect()
    await database.execute("""
        CREATE EXTENSION IF NOT EXISTS vector
    """)
    await init_account_tables()
    # 创建 session 表
    await database.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id UUID PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)
    """)
    # 创建 messages 表
    await database.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            session_id UUID,
            role TEXT,
            content TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            tokens_total INTEGER DEFAULT 0,
            embedding vector(768),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id)
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_embedding
          ON messages USING hnsw (embedding vector_cosine_ops)
          WHERE embedding IS NOT NULL
    """)
    # 创建 upload_files 表
    await database.execute("""
        CREATE TABLE IF NOT EXISTS upload_files (
            id SERIAL PRIMARY KEY,
            session_id UUID,
            filename TEXT,
            filepath TEXT,
            status TEXT DEFAULT 'pending',
            total_chunks INTEGER DEFAULT 0,
            processed_chunks INTEGER DEFAULT 0,
            error_msg TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # 迁移已有库: ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending';
    # ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS total_chunks INTEGER DEFAULT 0;
    # ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS processed_chunks INTEGER DEFAULT 0;
    # ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS error_msg TEXT;

    # 创建 knowledge_base 表，注意 vector 类型
    await database.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_base (
            id SERIAL PRIMARY KEY,
            session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
            content TEXT,
            original_content TEXT,
            source_file TEXT,
            chunk_index INTEGER DEFAULT 0,
            embedding vector(768)
        )
    """)
    # 迁移已有库: ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS original_content TEXT;
    # ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_file TEXT;
    # ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS chunk_index INTEGER DEFAULT 0;

    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_base_session_id ON knowledge_base(session_id)
    """)
    # HNSW 向量索引（cosine）
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_knowledge_base_hnsw
          ON knowledge_base USING hnsw (embedding vector_cosine_ops)
    """)


async def init_account_tables():
    await database.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT FALSE,
            max_daily_tokens INTEGER DEFAULT 100000,
            max_file_size_mb INTEGER DEFAULT 10,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS invite_codes (
            code UUID PRIMARY KEY,
            used_by TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            used_at TIMESTAMP
        )
    """)


async def save_message(session_id, role, content, tokens_in=0, tokens_out=0, tokens_total=0) -> int:
    query = """INSERT INTO messages (session_id, role, content, tokens_in, tokens_out, tokens_total)
               VALUES (:session_id, :role, :content, :tokens_in, :tokens_out, :tokens_total)
               RETURNING id"""
    return await database.execute(query, values={
        "session_id": session_id, "role": role, "content": content,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_total": tokens_total,
    })


async def update_message_embedding(message_id: int, embedding):
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        await conn.execute(
            "UPDATE messages SET embedding = $1 WHERE id = $2",
            Vector(embedding), message_id
        )


async def save_file(session_id, filename, filepath):
    query = "INSERT INTO upload_files (session_id, filename, filepath) VALUES (:session_id, :filename, :filepath)"
    await database.execute(query, values={"session_id": session_id, "filename": filename, "filepath": filepath})


async def get_context(session_id, limit=10):
    query = "SELECT id, role, content FROM messages WHERE session_id = :session_id ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"session_id": session_id, "limit": limit})
    return list(reversed([dict(row) for row in rows]))


# 检查 session 是否存在（已命名）
async def session_exists(session_id: str) -> bool:
    query = "SELECT 1 FROM sessions WHERE id = :session_id AND name IS NOT NULL LIMIT 1"
    row = await database.fetch_one(query, values={"session_id": session_id})
    return row is not None


# 检查 session 是否属于指定用户
async def session_owned_by(session_id: str, user_id: int) -> bool:
    query = "SELECT 1 FROM sessions WHERE id = :session_id AND user_id = :user_id LIMIT 1"
    row = await database.fetch_one(query, values={"session_id": session_id, "user_id": user_id})
    return row is not None


# 将新抓取到的内容存进知识库（单条，兼容 /chat web 内容写入）
async def add_knowledge(content, embedding, session_id, source_file: str = None):
    query = """
        INSERT INTO knowledge_base (content, embedding, session_id, source_file)
        VALUES ($1, $2, $3, $4)
    """
    vector = Vector(embedding)
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        await conn.execute(query, content, vector, session_id, source_file)


# 批量写入知识库（文件上传专用，单连接 executemany）
async def add_knowledge_batch(
    items: list,   # list of (enriched_content, original_content, embedding)
    session_id: str,
    source_file: str
):
    query = """
        INSERT INTO knowledge_base
          (content, original_content, embedding, session_id, source_file, chunk_index)
        VALUES ($1, $2, $3, $4, $5, $6)
    """
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        await conn.executemany(query, [
            (enriched.replace('\x00', ''), original.replace('\x00', ''), Vector(emb), session_id, source_file, idx)
            for idx, (enriched, original, emb) in enumerate(items)
        ])


# 更新文件处理状态
async def update_file_status(session_id: str, filename: str, status: str,
                              total: int = None, processed: int = None, error: str = None):
    parts = ["status = :status"]
    values = {"session_id": session_id, "filename": filename, "status": status}
    if total is not None:
        parts.append("total_chunks = :total")
        values["total"] = total
    if processed is not None:
        parts.append("processed_chunks = :processed")
        values["processed"] = processed
    if error is not None:
        parts.append("error_msg = :error")
        values["error"] = error
    query = f"UPDATE upload_files SET {', '.join(parts)} WHERE session_id = :session_id AND filename = :filename"
    await database.execute(query, values=values)


# 查询文件处理状态列表
async def get_file_statuses(session_id: str) -> list:
    query = """
        SELECT filename, status, total_chunks, processed_chunks, error_msg
        FROM upload_files
        WHERE session_id = :session_id
        ORDER BY created_at DESC
    """
    rows = await database.fetch_all(query, values={"session_id": session_id})
    return [dict(row) for row in rows]


# ── Admin 相关查询 ─────────────────────────────────────────────

async def get_user_today_tokens(user_id: int) -> int:
    query = """
        SELECT COALESCE(SUM(m.tokens_total), 0)
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE s.user_id = :user_id AND DATE(m.created_at) = CURRENT_DATE
    """
    row = await database.fetch_one(query, values={"user_id": user_id})
    return int(row[0]) if row else 0


async def get_all_users_with_stats() -> list:
    query = """
        SELECT u.id, u.username, u.is_admin, u.max_daily_tokens, u.created_at,
               COUNT(DISTINCT s.id) AS session_count,
               COALESCE(SUM(m.tokens_total), 0) AS total_tokens,
               COALESCE(SUM(CASE WHEN DATE(m.created_at) = CURRENT_DATE THEN m.tokens_total ELSE 0 END), 0) AS today_tokens
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id AND s.name IS NOT NULL
        LEFT JOIN messages m ON m.session_id = s.id
        WHERE u.is_admin = FALSE
        GROUP BY u.id
        ORDER BY u.id
    """
    rows = await database.fetch_all(query)
    return [dict(r) for r in rows]


async def get_user_by_id(user_id: int):
    row = await database.fetch_one("SELECT * FROM users WHERE id = :id", values={"id": user_id})
    return dict(row) if row else None


async def get_user_sessions_with_stats(user_id: int) -> list:
    query = """
        SELECT s.id, s.name, s.created_at,
               COUNT(m.id) AS message_count,
               COALESCE(SUM(m.tokens_total), 0) AS total_tokens
        FROM sessions s
        LEFT JOIN messages m ON m.session_id = s.id
        WHERE s.user_id = :user_id AND s.name IS NOT NULL
        GROUP BY s.id
        ORDER BY s.created_at DESC
    """
    rows = await database.fetch_all(query, values={"user_id": user_id})
    return [dict(r) for r in rows]


async def get_user_daily_tokens(user_id: int) -> list:
    query = """
        SELECT DATE(m.created_at) AS date,
               SUM(m.tokens_total) AS tokens
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE s.user_id = :user_id
        GROUP BY DATE(m.created_at)
        ORDER BY date DESC
        LIMIT 30
    """
    rows = await database.fetch_all(query, values={"user_id": user_id})
    return [dict(r) for r in rows]


async def get_session_messages_detail(session_id: str) -> list:
    query = """
        SELECT role, content, tokens_in, tokens_out, tokens_total, created_at
        FROM messages
        WHERE session_id = :sid
        ORDER BY created_at
    """
    rows = await database.fetch_all(query, values={"sid": session_id})
    return [dict(r) for r in rows]


async def get_session_files(session_id: str) -> list:
    query = """
        SELECT filename, filepath, status, total_chunks, processed_chunks, error_msg, created_at
        FROM upload_files
        WHERE session_id = :session_id
        ORDER BY created_at
    """
    rows = await database.fetch_all(query, values={"session_id": session_id})
    return [dict(r) for r in rows]


async def get_session_daily_tokens(session_id: str) -> list:
    query = """
        SELECT DATE(created_at) AS date,
               SUM(tokens_total) AS tokens,
               COUNT(*) AS message_count
        FROM messages
        WHERE session_id = :sid
        GROUP BY DATE(created_at)
        ORDER BY date DESC
    """
    rows = await database.fetch_all(query, values={"sid": session_id})
    return [dict(r) for r in rows]


async def get_session_info(session_id: str):
    query = """
        SELECT s.id, s.name, s.created_at, u.username,
               COALESCE(SUM(m.tokens_total), 0) AS total_tokens
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        LEFT JOIN messages m ON m.session_id = s.id
        WHERE s.id = :sid
        GROUP BY s.id, u.username
    """
    row = await database.fetch_one(query, values={"sid": session_id})
    return dict(row) if row else None


async def update_user_max_tokens(user_id: int, max_tokens: int):
    await database.execute(
        "UPDATE users SET max_daily_tokens = :v WHERE id = :id",
        values={"v": max_tokens, "id": user_id}
    )


async def update_user_max_file_size(user_id: int, max_file_size_mb: int):
    await database.execute(
        "UPDATE users SET max_file_size_mb = :v WHERE id = :id",
        values={"v": max_file_size_mb, "id": user_id}
    )


async def update_user_password(user_id: int, new_hash: str):
    await database.execute(
        "UPDATE users SET password_hash = :h WHERE id = :id",
        values={"h": new_hash, "id": user_id}
    )


async def get_all_invite_codes() -> list:
    rows = await database.fetch_all(
        "SELECT code, used_by, created_at, used_at FROM invite_codes ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def create_invite_code(code: str):
    await database.execute(
        "INSERT INTO invite_codes (code) VALUES (:code)",
        values={"code": code}
    )
