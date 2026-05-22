import json
import os
import uuid
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
            persona TEXT,
            system_instruction_origin TEXT,
            system_instruction TEXT,
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


# Phase 3a — 自主调优子系统所需的三张表。幂等创建，可在每次 startup 安全调用。
async def init_phase3_tables():
    # 1. prompt_versions —— 版本化的 prompt 存储 + 回滚支持
    await database.execute("""
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            version INTEGER NOT NULL,
            is_active BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            created_by TEXT DEFAULT 'manual',
            reason TEXT,
            UNIQUE(name, version)
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_prompt_versions_active
          ON prompt_versions(name) WHERE is_active = TRUE
    """)

    # 2. agent_traces —— 每次 /chat 留痕（所有路径）
    await database.execute("""
        CREATE TABLE IF NOT EXISTS agent_traces (
            id SERIAL PRIMARY KEY,
            session_id UUID,
            user_id INTEGER REFERENCES users(id),
            message_id INTEGER REFERENCES messages(id),
            query TEXT,
            route TEXT,
            tools_called JSONB DEFAULT '[]'::jsonb,
            iterations INTEGER DEFAULT 1,
            citations JSONB DEFAULT '[]'::jsonb,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            duration_ms INTEGER,
            prompt_version_id INTEGER REFERENCES prompt_versions(id),
            hallucination_rate FLOAT,
            analyzed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_traces_pending
          ON agent_traces(created_at) WHERE analyzed_at IS NULL
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_traces_route
          ON agent_traces(route, created_at DESC)
    """)

    # 3. subsystem_status —— 子系统启停 + 心跳
    await database.execute("""
        CREATE TABLE IF NOT EXISTS subsystem_status (
            component TEXT PRIMARY KEY,
            enabled BOOLEAN DEFAULT FALSE,
            last_heartbeat TIMESTAMP,
            last_action TEXT,
            status_msg TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # 三个组件的初始记录（幂等）
    for component in ("bot", "agent_b", "agent_c"):
        await database.execute(
            """INSERT INTO subsystem_status (component, enabled)
               VALUES (:c, FALSE) ON CONFLICT (component) DO NOTHING""",
            {"c": component},
        )

    # 4. agent_b_runs (Phase 3c) —— 每次 Agent B 分析的全貌记录
    await database.execute("""
        CREATE TABLE IF NOT EXISTS agent_b_runs (
            id SERIAL PRIMARY KEY,
            started_at TIMESTAMP DEFAULT NOW(),
            finished_at TIMESTAMP,
            traces_analyzed INTEGER DEFAULT 0,
            issues_found JSONB DEFAULT '[]'::jsonb,
            proposed_change BOOLEAN DEFAULT FALSE,
            applied BOOLEAN DEFAULT FALSE,
            new_prompt_version_id INTEGER REFERENCES prompt_versions(id),
            error_message TEXT
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_b_runs_started
          ON agent_b_runs(started_at DESC)
    """)

    # 5. agent_c_runs (Phase 3d) —— 验证 + 自动回滚的运行记录
    await database.execute("""
        CREATE TABLE IF NOT EXISTS agent_c_runs (
            id SERIAL PRIMARY KEY,
            started_at TIMESTAMP DEFAULT NOW(),
            finished_at TIMESTAMP,
            new_version_id INTEGER REFERENCES prompt_versions(id),
            old_version_id INTEGER REFERENCES prompt_versions(id),
            new_traces_count INTEGER DEFAULT 0,
            old_traces_count INTEGER DEFAULT 0,
            new_avg_score FLOAT,
            old_avg_score FLOAT,
            score_delta FLOAT,
            decision TEXT,
            decision_reason TEXT,
            error_message TEXT
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_c_runs_started
          ON agent_c_runs(started_at DESC)
    """)


async def init_writing_tables():
    await database.execute("""
        ALTER TABLE sessions ADD COLUMN IF NOT EXISTS
          is_writing_session BOOLEAN NOT NULL DEFAULT FALSE
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS writing_tasks (
          id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          session_id       UUID REFERENCES sessions(id) ON DELETE SET NULL,
          title            TEXT NOT NULL DEFAULT '',
          word_count       INTEGER DEFAULT 0,
          style_req        TEXT DEFAULT '',
          content_req      TEXT DEFAULT '',
          outline          TEXT DEFAULT '',
          reference_files  JSONB NOT NULL DEFAULT '[]',
          created_at       TIMESTAMP DEFAULT NOW(),
          updated_at       TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS word_count INTEGER DEFAULT 0;")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_req TEXT DEFAULT '';")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS content_req TEXT DEFAULT '';")
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_writing_tasks_user_id ON writing_tasks(user_id)
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS writing_contents (
          id         SERIAL PRIMARY KEY,
          task_id    UUID NOT NULL REFERENCES writing_tasks(id) ON DELETE CASCADE,
          content    TEXT NOT NULL DEFAULT '',
          version    INTEGER NOT NULL DEFAULT 1,
          created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_writing_contents_task_id ON writing_contents(task_id)
    """)
    await database.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_write BOOLEAN NOT NULL DEFAULT FALSE;")


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
        SELECT u.id, u.username, u.is_admin, u.can_write, u.max_daily_tokens, u.max_file_size_mb, u.created_at,
               COUNT(DISTINCT CASE WHEN s.name IS NOT NULL THEN s.id END) AS session_count,
               COALESCE(SUM(m.tokens_total), 0) AS total_tokens,
               COALESCE(SUM(CASE WHEN DATE(m.created_at) = CURRENT_DATE THEN m.tokens_total ELSE 0 END), 0) AS today_tokens
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id
        LEFT JOIN messages m ON m.session_id = s.id
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
        WITH dates AS (
            SELECT generate_series(
                CURRENT_DATE - INTERVAL '29 days',
                CURRENT_DATE,
                '1 day'::interval
            )::date AS date
        ),
        daily AS (
            SELECT DATE(m.created_at) AS date,
                   SUM(m.tokens_total) AS tokens
            FROM messages m
            JOIN sessions s ON m.session_id = s.id
            WHERE s.user_id = :user_id
            GROUP BY DATE(m.created_at)
        )
        SELECT d.date, COALESCE(daily.tokens, 0) AS tokens
        FROM dates d
        LEFT JOIN daily ON d.date = daily.date
        ORDER BY d.date DESC
    """
    rows = await database.fetch_all(query, values={"user_id": user_id})
    return [dict(r) for r in rows]


async def get_user_total_tokens(user_id: int) -> int:
    query = """
        SELECT COALESCE(SUM(m.tokens_total), 0)
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE s.user_id = :user_id
    """
    row = await database.fetch_one(query, values={"user_id": user_id})
    return int(row[0]) if row else 0


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
               s.system_instruction_origin, s.system_instruction,
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


async def update_user_admin_status(user_id: int, is_admin_status: bool):
    await database.execute(
        "UPDATE users SET is_admin = :v WHERE id = :id",
        values={"v": is_admin_status, "id": user_id}
    )


async def update_user_writing_permission(user_id: int, can_write: bool) -> None:
    await database.execute(
        "UPDATE users SET can_write = :v WHERE id = :id",
        values={"v": can_write, "id": user_id}
    )


async def get_session_persona(session_id: str) -> str:
    """返回 AI 处理后的 system_instruction，用于 Gemini 调用。"""
    row = await database.fetch_one(
        "SELECT system_instruction FROM sessions WHERE id = :sid",
        values={"sid": session_id}
    )
    return (row["system_instruction"] or "") if row else ""


async def get_session_persona_origin(session_id: str) -> str:
    """返回用户原始输入，用于前端编辑回显。"""
    row = await database.fetch_one(
        "SELECT system_instruction_origin FROM sessions WHERE id = :sid",
        values={"sid": session_id}
    )
    return (row["system_instruction_origin"] or "") if row else ""


async def update_session_persona(session_id: str, user_id: int, origin: str, processed: str):
    await database.execute(
        """UPDATE sessions
           SET system_instruction_origin = :origin, system_instruction = :processed
           WHERE id = :sid AND user_id = :uid""",
        values={
            "origin": origin or None,
            "processed": processed or None,
            "sid": session_id,
            "uid": user_id,
        }
    )


async def save_persona_origin(session_id: str, user_id: int, origin: str):
    """仅保存原始输入，不触碰 system_instruction。"""
    await database.execute(
        "UPDATE sessions SET system_instruction_origin = :origin WHERE id = :sid AND user_id = :uid",
        values={"origin": origin or None, "sid": session_id, "uid": user_id}
    )


async def update_session_instruction(session_id: str, instruction: str):
    """仅更新 AI 处理后的 system_instruction。"""
    await database.execute(
        "UPDATE sessions SET system_instruction = :v WHERE id = :sid",
        values={"v": instruction or None, "sid": session_id}
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


# ── Phase 3a — 自主调优子系统辅助函数 ────────────────────────────────────────

async def get_active_prompt(name: str) -> tuple[str, int] | None:
    """返回 (content, version_id)，没有 active 版本则返回 None。"""
    row = await database.fetch_one(
        "SELECT id, content FROM prompt_versions "
        "WHERE name = :n AND is_active = TRUE LIMIT 1",
        values={"n": name},
    )
    return (row["content"], row["id"]) if row else None


async def upsert_prompt_version(
    name: str,
    content: str,
    created_by: str = "manual",
    reason: str | None = None,
) -> int:
    """新建版本并标记为 active，旧版自动 demote。返回新 version_id。"""
    async with database.transaction():
        next_v_row = await database.fetch_one(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next "
            "FROM prompt_versions WHERE name = :n",
            values={"n": name},
        )
        next_v = next_v_row["next"]
        await database.execute(
            "UPDATE prompt_versions SET is_active = FALSE WHERE name = :n",
            values={"n": name},
        )
        new_id = await database.fetch_val(
            """INSERT INTO prompt_versions (name, content, version, is_active, created_by, reason)
               VALUES (:n, :c, :v, TRUE, :b, :r)
               RETURNING id""",
            values={"n": name, "c": content, "v": next_v, "b": created_by, "r": reason},
        )
        return new_id


async def list_prompt_versions(name: str, limit: int = 50) -> list:
    """按版本号倒序列出某 prompt 的历史版本（admin 页面 + 回滚用）。"""
    rows = await database.fetch_all(
        "SELECT id, version, is_active, created_at, created_by, reason, "
        "       LEFT(content, 200) AS preview "
        "FROM prompt_versions WHERE name = :n "
        "ORDER BY version DESC LIMIT :lim",
        values={"n": name, "lim": limit},
    )
    return [dict(r) for r in rows]


async def activate_prompt_version(version_id: int) -> bool:
    """显式回滚：把指定 version_id 设为 active，同 name 下其余 demote。"""
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT name FROM prompt_versions WHERE id = :id",
            values={"id": version_id},
        )
        if not row:
            return False
        name = row["name"]
        await database.execute(
            "UPDATE prompt_versions SET is_active = FALSE WHERE name = :n",
            values={"n": name},
        )
        await database.execute(
            "UPDATE prompt_versions SET is_active = TRUE WHERE id = :id",
            values={"id": version_id},
        )
        return True


async def record_trace(
    *,
    session_id: str,
    user_id: int,
    message_id: int | None,
    query: str,
    route: str,
    tools_called: list | None = None,
    iterations: int = 1,
    citations: list | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_ms: int = 0,
    prompt_version_id: int | None = None,
) -> int:
    """异步落盘一次 /chat 调用的完整 trace。返回 trace id。"""
    new_id = await database.fetch_val(
        """INSERT INTO agent_traces (
                session_id, user_id, message_id, query, route,
                tools_called, iterations, citations,
                tokens_in, tokens_out, duration_ms, prompt_version_id
            ) VALUES (
                :sid, :uid, :mid, :q, :r,
                CAST(:tc AS jsonb), :it, CAST(:cit AS jsonb),
                :ti, :to, :dur, :pv
            ) RETURNING id""",
        values={
            "sid": session_id,
            "uid": user_id,
            "mid": message_id,
            "q": query,
            "r": route,
            "tc": json.dumps(tools_called or [], ensure_ascii=False),
            "it": iterations,
            "cit": json.dumps(citations or [], ensure_ascii=False),
            "ti": tokens_in,
            "to": tokens_out,
            "dur": duration_ms,
            "pv": prompt_version_id,
        },
    )
    return new_id


async def get_subsystem_status(component: str) -> dict | None:
    row = await database.fetch_one(
        "SELECT * FROM subsystem_status WHERE component = :c",
        values={"c": component},
    )
    return dict(row) if row else None


async def get_all_subsystem_status() -> list:
    rows = await database.fetch_all(
        "SELECT * FROM subsystem_status ORDER BY component"
    )
    return [dict(r) for r in rows]


async def set_subsystem_enabled(component: str, enabled: bool, status_msg: str | None = None):
    await database.execute(
        """UPDATE subsystem_status
           SET enabled = :e, status_msg = :m, updated_at = NOW()
           WHERE component = :c""",
        values={"c": component, "e": enabled, "m": status_msg},
    )


async def heartbeat_subsystem(component: str, last_action: str | None = None):
    await database.execute(
        """UPDATE subsystem_status
           SET last_heartbeat = NOW(), last_action = :a, updated_at = NOW()
           WHERE component = :c""",
        values={"c": component, "a": last_action},
    )


# ── Phase 3c — Agent B 分析所需的 trace / runs 辅助 ──────────────────────────

async def fetch_pending_agent_traces(limit: int = 20) -> list[dict]:
    """
    拉 route='agent' 且尚未被 Agent B 分析的 trace。
    按 created_at 升序，确保老 trace 优先分析。
    """
    rows = await database.fetch_all(
        """SELECT id, session_id, query, tools_called, iterations,
                  citations, tokens_in, tokens_out, duration_ms,
                  prompt_version_id, created_at
           FROM agent_traces
           WHERE route = 'agent' AND analyzed_at IS NULL
           ORDER BY created_at ASC LIMIT :lim""",
        values={"lim": limit},
    )
    return [dict(r) for r in rows]


async def mark_traces_analyzed(trace_ids: list[int]):
    """批量标记 trace 已分析（Agent B 完成后调用）。"""
    if not trace_ids:
        return
    await database.execute(
        """UPDATE agent_traces SET analyzed_at = NOW()
           WHERE id = ANY(:ids)""",
        values={"ids": trace_ids},
    )


async def create_agent_b_run() -> int:
    """开始一次 Agent B 分析；返回 run_id。"""
    return await database.fetch_val(
        "INSERT INTO agent_b_runs DEFAULT VALUES RETURNING id"
    )


async def update_agent_b_run(
    run_id: int,
    *,
    traces_analyzed: int | None = None,
    issues_found: list | None = None,
    proposed_change: bool | None = None,
    applied: bool | None = None,
    new_prompt_version_id: int | None = None,
    error_message: str | None = None,
    finished: bool = False,
):
    """部分更新 agent_b_runs 一行（finished=True 时同时设 finished_at）。"""
    import json as _json
    sets: list[str] = []
    values: dict = {"id": run_id}
    if traces_analyzed is not None:
        sets.append("traces_analyzed = :ta")
        values["ta"] = traces_analyzed
    if issues_found is not None:
        sets.append("issues_found = CAST(:if AS jsonb)")
        values["if"] = _json.dumps(issues_found, ensure_ascii=False)
    if proposed_change is not None:
        sets.append("proposed_change = :pc")
        values["pc"] = proposed_change
    if applied is not None:
        sets.append("applied = :ap")
        values["ap"] = applied
    if new_prompt_version_id is not None:
        sets.append("new_prompt_version_id = :npv")
        values["npv"] = new_prompt_version_id
    if error_message is not None:
        sets.append("error_message = :em")
        values["em"] = error_message
    if finished:
        sets.append("finished_at = NOW()")
    if not sets:
        return
    await database.execute(
        f"UPDATE agent_b_runs SET {', '.join(sets)} WHERE id = :id",
        values=values,
    )


async def has_recent_agent_b_change(hours: int = 24) -> bool:
    """24 小时频率门控：检查最近 hours 内是否已经成功改过 prompt。"""
    row = await database.fetch_one(
        """SELECT 1 FROM prompt_versions
           WHERE created_by = 'agent_b'
             AND created_at > NOW() - make_interval(hours := :h)
           LIMIT 1""",
        values={"h": hours},
    )
    return row is not None


async def list_agent_b_runs(limit: int = 30) -> list[dict]:
    """admin 页面用，列出最近 N 次 Agent B 分析。"""
    rows = await database.fetch_all(
        """SELECT id, started_at, finished_at, traces_analyzed,
                  proposed_change, applied, new_prompt_version_id, error_message,
                  jsonb_array_length(issues_found) AS issue_count
           FROM agent_b_runs
           ORDER BY started_at DESC LIMIT :lim""",
        values={"lim": limit},
    )
    return [dict(r) for r in rows]


# ── Phase 3d — Agent C 验证 + 回滚所需的辅助 ─────────────────────────────────

async def fetch_active_prompt_version_id(name: str) -> int | None:
    row = await database.fetch_one(
        "SELECT id FROM prompt_versions WHERE name = :n AND is_active = TRUE LIMIT 1",
        values={"n": name},
    )
    return row["id"] if row else None


async def fetch_prompt_version_by_id(version_id: int) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, name, version, is_active, content, created_by, reason, created_at
           FROM prompt_versions WHERE id = :id""",
        values={"id": version_id},
    )
    return dict(row) if row else None


async def fetch_previous_prompt_version_id(name: str, current_id: int) -> int | None:
    """同 name 下、id 小于 current_id 的最近一个版本——即被 current_id 替换掉的版本。"""
    row = await database.fetch_one(
        """SELECT id FROM prompt_versions
           WHERE name = :n AND id < :cur
           ORDER BY id DESC LIMIT 1""",
        values={"n": name, "cur": current_id},
    )
    return row["id"] if row else None


async def fetch_traces_by_version(version_id: int, route: str = "agent") -> list[dict]:
    """拿某 prompt 版本下指定 route 的所有 trace。"""
    rows = await database.fetch_all(
        """SELECT id, session_id, message_id, query, tools_called, iterations, citations,
                  tokens_in, tokens_out, duration_ms, hallucination_rate,
                  created_at
           FROM agent_traces
           WHERE prompt_version_id = :v AND route = :r
           ORDER BY created_at""",
        values={"v": version_id, "r": route},
    )
    return [dict(r) for r in rows]


async def get_message_content(message_id: int) -> str | None:
    """返回指定 message 的文本内容，Agent C 幻觉检测用。"""
    row = await database.fetch_one(
        "SELECT content FROM messages WHERE id = :id",
        values={"id": message_id},
    )
    return row["content"] if row else None


async def update_trace_hallucination_rate(trace_id: int, rate: float):
    """Agent C 反查 KB 后写入一条 trace 的 hallucination_rate。"""
    await database.execute(
        "UPDATE agent_traces SET hallucination_rate = :r WHERE id = :id",
        values={"r": rate, "id": trace_id},
    )


async def is_kb_chunk_real(session_id: str, source_file: str, chunk_index: int) -> bool:
    """检查 (session, source_file, chunk_index) 是否真在 knowledge_base 中存在。"""
    row = await database.fetch_one(
        """SELECT 1 FROM knowledge_base
           WHERE session_id = :sid AND source_file = :s AND chunk_index = :c
           LIMIT 1""",
        values={"sid": session_id, "s": source_file, "c": chunk_index},
    )
    return row is not None


async def create_agent_c_run(new_version_id: int, old_version_id: int) -> int:
    return await database.fetch_val(
        """INSERT INTO agent_c_runs (new_version_id, old_version_id)
           VALUES (:n, :o) RETURNING id""",
        values={"n": new_version_id, "o": old_version_id},
    )


async def update_agent_c_run(
    run_id: int,
    *,
    new_traces_count: int | None = None,
    old_traces_count: int | None = None,
    new_avg_score: float | None = None,
    old_avg_score: float | None = None,
    score_delta: float | None = None,
    decision: str | None = None,
    decision_reason: str | None = None,
    error_message: str | None = None,
    finished: bool = False,
):
    sets: list[str] = []
    values: dict = {"id": run_id}
    if new_traces_count is not None:
        sets.append("new_traces_count = :ntc"); values["ntc"] = new_traces_count
    if old_traces_count is not None:
        sets.append("old_traces_count = :otc"); values["otc"] = old_traces_count
    if new_avg_score is not None:
        sets.append("new_avg_score = :nas"); values["nas"] = new_avg_score
    if old_avg_score is not None:
        sets.append("old_avg_score = :oas"); values["oas"] = old_avg_score
    if score_delta is not None:
        sets.append("score_delta = :sd"); values["sd"] = score_delta
    if decision is not None:
        sets.append("decision = :dec"); values["dec"] = decision
    if decision_reason is not None:
        sets.append("decision_reason = :dr"); values["dr"] = decision_reason
    if error_message is not None:
        sets.append("error_message = :em"); values["em"] = error_message
    if finished:
        sets.append("finished_at = NOW()")
    if not sets:
        return
    await database.execute(
        f"UPDATE agent_c_runs SET {', '.join(sets)} WHERE id = :id",
        values=values,
    )


async def list_agent_c_runs(limit: int = 30) -> list[dict]:
    """admin 页面用，列出最近 N 次 Agent C 验证决策。"""
    rows = await database.fetch_all(
        """SELECT c.id, c.started_at, c.finished_at,
                  c.new_version_id, c.old_version_id,
                  pn.version AS new_version_num, po.version AS old_version_num,
                  c.new_traces_count, c.old_traces_count,
                  c.new_avg_score, c.old_avg_score, c.score_delta,
                  c.decision, c.decision_reason, c.error_message
           FROM agent_c_runs c
           LEFT JOIN prompt_versions pn ON pn.id = c.new_version_id
           LEFT JOIN prompt_versions po ON po.id = c.old_version_id
           ORDER BY c.started_at DESC LIMIT :lim""",
        values={"lim": limit},
    )
    return [dict(r) for r in rows]


async def create_writing_task(
    user_id: int,
    title: str = "",
    word_count: int = 0,
    style_req: str = "",
    content_req: str = "",
) -> tuple[str, str]:
    session_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO sessions (id, user_id, name, is_writing_session)
           VALUES (:sid, :uid, :name, TRUE)""",
        values={"sid": session_id, "uid": user_id, "name": "[writing] " + (title or "")},
    )
    await database.execute(
        """INSERT INTO writing_tasks (id, user_id, session_id, title, word_count, style_req, content_req)
           VALUES (:tid, :uid, :sid, :title, :word_count, :style_req, :content_req)""",
        values={
            "tid": task_id,
            "uid": user_id,
            "sid": session_id,
            "title": title or "",
            "word_count": word_count or 0,
            "style_req": style_req or "",
            "content_req": content_req or "",
        },
    )
    system_prompt = (
        f"你是一个写作助手。当前写作任务标题：{title}。\n"
        "若用户请求你修改写作内容，请先输出修改后的完整内容，格式如下：\n"
        "[WRITING_UPDATE_START]\n"
        "（修改后的完整 Markdown 内容）\n"
        "[WRITING_UPDATE_END]\n"
        "然后再用一两句话说明做了哪些修改。\n"
        "若用户只是讨论写作内容，无需输出上述标记，正常回复即可。"
    )
    await update_session_instruction(session_id, system_prompt)
    return task_id, session_id


async def get_writing_tasks(user_id: int) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, title, word_count, created_at, updated_at
           FROM writing_tasks WHERE user_id = :uid ORDER BY created_at DESC""",
        values={"uid": user_id},
    )
    return [dict(r) for r in rows]


async def get_writing_task(task_id: str, user_id: int) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, user_id, session_id, title, word_count, style_req, content_req,
                  outline, reference_files, created_at, updated_at
           FROM writing_tasks WHERE id = :tid AND user_id = :uid""",
        values={"tid": task_id, "uid": user_id},
    )
    if not row:
        return None
    task = dict(row)
    refs = task.get("reference_files")
    if refs is None:
        task["reference_files"] = []
    elif isinstance(refs, str):
        task["reference_files"] = json.loads(refs)
    return task


async def writing_task_owned_by(task_id: str, user_id: int) -> bool:
    row = await database.fetch_one(
        "SELECT 1 FROM writing_tasks WHERE id = :tid AND user_id = :uid LIMIT 1",
        values={"tid": task_id, "uid": user_id},
    )
    return row is not None


async def update_writing_task(task_id: str, user_id: int, **kwargs) -> bool:
    allowed = {"title", "word_count", "style_req", "content_req", "outline", "reference_files"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = []
    values = {"tid": task_id, "uid": user_id}
    for key, value in fields.items():
        if key == "reference_files":
            sets.append(f"{key} = CAST(:{key} AS jsonb)")
            values[key] = json.dumps(value or [], ensure_ascii=False)
        else:
            sets.append(f"{key} = :{key}")
            values[key] = value
    sets.append("updated_at = NOW()")
    row = await database.fetch_one(
        f"""UPDATE writing_tasks SET {', '.join(sets)}
            WHERE id = :tid AND user_id = :uid RETURNING id""",
        values=values,
    )
    return row is not None


async def delete_writing_task(task_id: str, user_id: int) -> bool:
    row = await database.fetch_one(
        "SELECT session_id FROM writing_tasks WHERE id = :tid AND user_id = :uid",
        values={"tid": task_id, "uid": user_id},
    )
    if not row:
        return False
    session_id = row["session_id"]
    deleted = await database.fetch_one(
        "DELETE FROM writing_tasks WHERE id = :tid AND user_id = :uid RETURNING id",
        values={"tid": task_id, "uid": user_id},
    )
    if deleted and session_id:
        await database.execute("DELETE FROM sessions WHERE id = :sid", values={"sid": session_id})
    return deleted is not None


async def get_writing_content(task_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, content, version, created_at FROM writing_contents
           WHERE task_id = :tid ORDER BY version DESC LIMIT 1""",
        values={"tid": task_id},
    )
    return dict(row) if row else None


async def save_writing_content(task_id: str, content: str) -> int:
    row = await database.fetch_one(
        "SELECT COALESCE(MAX(version), 0) AS max_version FROM writing_contents WHERE task_id = :tid",
        values={"tid": task_id},
    )
    new_version = int(row["max_version"] or 0) + 1 if row else 1
    await database.execute(
        """INSERT INTO writing_contents (task_id, content, version)
           VALUES (:tid, :content, :version)""",
        values={"tid": task_id, "content": content or "", "version": new_version},
    )
    count = await database.fetch_val(
        "SELECT COUNT(*) FROM writing_contents WHERE task_id = :tid",
        values={"tid": task_id},
    )
    if count and int(count) > 3:
        await database.execute(
            """DELETE FROM writing_contents
               WHERE id = (
                 SELECT id FROM writing_contents
                 WHERE task_id = :tid ORDER BY version ASC, created_at ASC LIMIT 1
               )""",
            values={"tid": task_id},
        )
    return new_version


async def get_user_processed_files(user_id: int) -> list[str]:
    rows = await database.fetch_all(
        """SELECT DISTINCT uf.filename
           FROM upload_files uf JOIN sessions s ON uf.session_id = s.id
           WHERE s.user_id = :uid AND uf.status = 'done'
           ORDER BY uf.filename""",
        values={"uid": user_id},
    )
    return [r["filename"] for r in rows]
