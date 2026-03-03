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
            created_at TIMESTAMP DEFAULT NOW()
        )
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


async def save_message(session_id, role, content):
    query = "INSERT INTO messages (session_id, role, content) VALUES (:session_id, :role, :content)"
    await database.execute(query, values={"session_id": session_id, "role": role, "content": content})


async def save_file(session_id, filename, filepath):
    query = "INSERT INTO upload_files (session_id, filename, filepath) VALUES (:session_id, :filename, :filepath)"
    await database.execute(query, values={"session_id": session_id, "filename": filename, "filepath": filepath})


async def get_context(session_id, limit=10):
    query = "SELECT role, content FROM messages WHERE session_id = :session_id ORDER BY id DESC LIMIT :limit"
    rows = await database.fetch_all(query, values={"session_id": session_id, "limit": limit})
    return list(reversed([dict(row) for row in rows]))


# 检查 session 是否存在
async def session_exists(session_id: str) -> bool:
    query = "SELECT 1 FROM sessions WHERE id = :session_id AND name IS NOT NULL LIMIT 1"
    row = await database.fetch_one(query, values={"session_id": session_id})
    return row is not None


# 将新抓取到的内容存进知识库（单条，兼容 /chat web 内容写入）
async def add_knowledge(content, embedding, session_id):
    query = """
        INSERT INTO knowledge_base (content, embedding, session_id)
        VALUES ($1, $2, $3)
    """
    vector = Vector(embedding)
    async with database._backend._pool.acquire() as conn:
        await register_vector(conn)
        await conn.execute(query, content, vector, session_id)


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
            (enriched, original, Vector(emb), session_id, source_file, idx)
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
