import difflib
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
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
    # --- writing_tasks: toc + version timestamps ---
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS toc TEXT DEFAULT '';")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS toc_updated_at TIMESTAMPTZ;")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS outline_updated_at TIMESTAMPTZ;")
    # --- writing_sections ---
    await database.execute("""
        CREATE TABLE IF NOT EXISTS writing_sections (
          id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          task_id           UUID NOT NULL REFERENCES writing_tasks(id) ON DELETE CASCADE,
          section_index     INTEGER NOT NULL DEFAULT 0,
          heading           TEXT NOT NULL DEFAULT '',
          sub_outline       TEXT DEFAULT '',
          content           TEXT DEFAULT '',
          word_count_target INTEGER DEFAULT 0,
          status            TEXT NOT NULL DEFAULT 'pending',
          last_generated_at TIMESTAMPTZ,
          created_at        TIMESTAMPTZ DEFAULT NOW(),
          updated_at        TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_writing_sections_task_id
        ON writing_sections(task_id, section_index)
    """)

    # --- writing_section_images：段落内"[配图：xxx]"标记对应的 prompt 草稿 + 最终上传图片 ---
    await database.execute("""
        CREATE TABLE IF NOT EXISTS writing_section_images (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          section_id    UUID NOT NULL REFERENCES writing_sections(id) ON DELETE CASCADE,
          marker_text   TEXT NOT NULL,
          prompt        TEXT NOT NULL DEFAULT '',
          image_path    TEXT DEFAULT '',
          created_at    TIMESTAMPTZ DEFAULT NOW(),
          updated_at    TIMESTAMPTZ DEFAULT NOW(),
          UNIQUE (section_id, marker_text)
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_writing_section_images_section_id
        ON writing_section_images(section_id)
    """)
    # --- writing_tasks: style skills + source ---
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_skills TEXT DEFAULT '';")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_skills_updated_at TIMESTAMPTZ;")
    await database.execute("ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_source_text TEXT DEFAULT '';")
    # --- writing_evaluations ---
    await database.execute("""
        CREATE TABLE IF NOT EXISTS writing_evaluations (
          id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          task_id             UUID NOT NULL REFERENCES writing_tasks(id) ON DELETE CASCADE,
          readability_score   INTEGER DEFAULT 0,
          readability_report  TEXT DEFAULT '',
          style_score         INTEGER DEFAULT 0,
          style_report        TEXT DEFAULT '',
          overall_score       INTEGER DEFAULT 0,
          created_at          TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_writing_evaluations_task_id
        ON writing_evaluations(task_id, created_at DESC)
    """)


async def init_drawing_tables():
    await database.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_draw BOOLEAN NOT NULL DEFAULT FALSE;")
    await database.execute("""
        CREATE TABLE IF NOT EXISTS drawing_prompts (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          name        TEXT NOT NULL,
          content     TEXT NOT NULL DEFAULT '',
          created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS drawing_styles (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name        TEXT NOT NULL DEFAULT '未命名风格',
          prompt_ids  JSONB NOT NULL DEFAULT '[]',
          created_at  TIMESTAMPTZ DEFAULT NOW(),
          updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_drawing_styles_user_id ON drawing_styles(user_id)
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS drawing_generations (
          id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          style_id     UUID NOT NULL REFERENCES drawing_styles(id) ON DELETE CASCADE,
          user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          user_input   TEXT NOT NULL DEFAULT '',
          full_prompt  TEXT NOT NULL DEFAULT '',
          image_path   TEXT DEFAULT '',
          model        TEXT DEFAULT '',
          status       TEXT NOT NULL DEFAULT 'pending',
          error_msg    TEXT DEFAULT '',
          created_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_drawing_generations_style_id
        ON drawing_generations(style_id, created_at DESC)
    """)
    await database.execute("""
        ALTER TABLE drawing_generations ADD COLUMN IF NOT EXISTS
          parent_generation_id UUID REFERENCES drawing_generations(id) ON DELETE CASCADE
    """)
    await database.execute("""
        CREATE INDEX IF NOT EXISTS idx_drawing_generations_parent
        ON drawing_generations(parent_generation_id)
    """)
    await database.execute("""
        CREATE TABLE IF NOT EXISTS drawing_skill_packages (
          id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          name          TEXT NOT NULL,
          source_url    TEXT DEFAULT '',
          kind          TEXT NOT NULL DEFAULT 'instruction',
          instructions  TEXT NOT NULL DEFAULT '',
          mcp_config    JSONB,
          created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
          created_at    TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("""
        ALTER TABLE drawing_styles ADD COLUMN IF NOT EXISTS
          skill_package_id UUID REFERENCES drawing_skill_packages(id) ON DELETE SET NULL
    """)


async def init_map_tables():
    """地图模块：map_documents（一张地图 = 一份 preset：style + annotations）
    + map_preset_versions（版本历史，做法2：实时自动保存原地更新，版本表只存检查点快照，留 3 版）。
    幂等，startup 每次调用安全。"""
    await database.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS can_map BOOLEAN NOT NULL DEFAULT FALSE;")
    await database.execute("""
        CREATE TABLE IF NOT EXISTS map_documents (
          id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name        TEXT NOT NULL DEFAULT '未命名地图',
          preset      JSONB NOT NULL DEFAULT '{}',
          thumb_path  TEXT DEFAULT '',
          created_at  TIMESTAMPTZ DEFAULT NOW(),
          updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute("CREATE INDEX IF NOT EXISTS idx_map_documents_user_id ON map_documents(user_id)")
    await database.execute("""
        CREATE TABLE IF NOT EXISTS map_preset_versions (
          id          SERIAL PRIMARY KEY,
          map_id      UUID NOT NULL REFERENCES map_documents(id) ON DELETE CASCADE,
          preset      JSONB NOT NULL,
          version     INTEGER NOT NULL,
          note        TEXT DEFAULT '',
          created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await database.execute(
        "CREATE INDEX IF NOT EXISTS idx_map_preset_versions_map ON map_preset_versions(map_id, version DESC)"
    )


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
    elif status == 'done':
        # 成功完成时清掉上一次失败残留的 error_msg，避免 done 状态还挂着旧报错误导用户
        parts.append("error_msg = NULL")
    query = f"UPDATE upload_files SET {', '.join(parts)} WHERE session_id = :session_id AND filename = :filename"
    await database.execute(query, values=values)


# 启动时把上一次进程遗留的卡死文件标记为 failed（而不是让前端一直显示"解析中"）。
# process_file_and_insert 跑在 FastAPI BackgroundTasks 里，和处理它的 worker 进程绑定；
# 只要这个 worker 在处理途中被重启/杀掉（部署、OOM 等），DB 行会永远停在 processing，
# 且没有任何代码还在运行、能够替它写回失败状态——只能靠新进程启动时做一次性扫描补上。
# 用 stale_minutes 兜底（而不是无条件把所有 processing 都标失败）是因为多 worker 场景下，
# 滚动重启时其它 worker 可能正在合法地处理另一个文件，不能一刀切。
async def fail_stale_processing_files(stale_minutes: int = 30) -> int:
    rows = await database.fetch_all(
        """
        UPDATE upload_files
        SET status = 'failed',
            error_msg = '处理中断（服务重启或长时间无响应），请点击"重新处理"重试'
        WHERE status = 'processing'
          AND created_at < NOW() - make_interval(mins => :stale_minutes)
        RETURNING filename
        """,
        values={"stale_minutes": stale_minutes},
    )
    return len(rows)


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
        SELECT u.id, u.username, u.is_admin, u.can_write, u.can_draw, u.can_map, u.max_daily_tokens, u.max_file_size_mb, u.created_at,
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


async def update_user_drawing_permission(user_id: int, can_draw: bool) -> None:
    await database.execute(
        "UPDATE users SET can_draw = :v WHERE id = :id",
        values={"v": can_draw, "id": user_id}
    )


async def update_user_map_permission(user_id: int, can_map: bool) -> None:
    await database.execute(
        "UPDATE users SET can_map = :v WHERE id = :id",
        values={"v": can_map, "id": user_id}
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
                  outline, outline_updated_at, toc, toc_updated_at,
                  style_skills, style_skills_updated_at, style_source_text,
                  reference_files, created_at, updated_at
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
    allowed = {"title", "word_count", "style_req", "content_req", "outline", "toc",
               "reference_files", "style_skills", "style_skills_updated_at", "style_source_text"}
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
    if "outline" in fields:
        sets.append("outline_updated_at = NOW()")
    if "toc" in fields:
        sets.append("toc_updated_at = NOW()")
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


async def get_writing_sections(task_id: str) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, task_id, section_index, heading, sub_outline, content,
                  word_count_target, status, last_generated_at, created_at, updated_at
           FROM writing_sections WHERE task_id = :tid AND status != 'archived'
           ORDER BY section_index""",
        values={"tid": task_id},
    )
    return [dict(r) for r in rows]


async def get_writing_section(section_id: str, task_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, task_id, section_index, heading, sub_outline, content,
                  word_count_target, status, last_generated_at, created_at, updated_at
           FROM writing_sections WHERE id = :sid AND task_id = :tid""",
        values={"sid": section_id, "tid": task_id},
    )
    return dict(row) if row else None


_RENAME_SIMILARITY_THRESHOLD = 0.3
_RECONCILE_CONFIRM_THRESHOLD = 3


@dataclass
class _HeadingAlignment:
    exact: list[tuple[int, int]] = field(default_factory=list)          # (pool_index, new_index)
    renamed: list[tuple[int, int, float]] = field(default_factory=list)  # (pool_index, new_index, similarity)
    deleted: list[int] = field(default_factory=list)                    # pool_index
    inserted: list[int] = field(default_factory=list)                   # new_index


def _align_headings(pool: list[dict], new_headings: list[str]) -> _HeadingAlignment:
    """两阶段标题对齐：文本精确匹配（与位置无关）→ 剩余项按原相对顺序配对为"改名"。

    pool: 该 task 下所有 writing_sections 行（含 archived），下标即结果里的 pool_index。
    不能直接用位置对齐的 LCS——比如 [A,B,C] 纯重排成 [C,A,B]，标准 LCS（下标必须
    单调递增）只能找到 [A,B]，会把纯重排误判成删除+新增。所以先做一遍与位置无关的
    精确文本匹配（这一步就是原来 upsert_writing_sections 的核心能力：任意重排序/
    新增/删除都不会误伤不变的标题），只有精确匹配剩下的项，位置信息才有意义、才
    拿来配对"改名"——且必须过一道相似度阈值，防止把两个毫不相关的标题错配、
    让新标题静默"继承"旧段落已经写好的正文和确认状态。
    """
    result = _HeadingAlignment()

    active_order = [i for i, r in enumerate(pool) if r["status"] != "archived"]
    archived_order = [i for i, r in enumerate(pool) if r["status"] == "archived"]
    buckets: dict[str, deque] = {}
    for i in active_order + archived_order:   # 同名时优先复用/复活"还活着"的那一行
        buckets.setdefault(pool[i]["heading"], deque()).append(i)

    consumed_old: set[int] = set()
    consumed_new: set[int] = set()
    for j, heading in enumerate(new_headings):
        bucket = buckets.get(heading)
        if bucket:
            i = bucket.popleft()
            result.exact.append((i, j))
            consumed_old.add(i)
            consumed_new.add(j)

    leftover_old = [i for i in active_order if i not in consumed_old]
    leftover_new = [j for j in range(len(new_headings)) if j not in consumed_new]
    k = min(len(leftover_old), len(leftover_new))
    for n in range(k):
        i, j = leftover_old[n], leftover_new[n]
        ratio = difflib.SequenceMatcher(None, pool[i]["heading"], new_headings[j]).ratio()
        if ratio >= _RENAME_SIMILARITY_THRESHOLD:
            result.renamed.append((i, j, ratio))
        else:
            result.deleted.append(i)
            result.inserted.append(j)
    result.deleted.extend(leftover_old[k:])
    result.inserted.extend(leftover_new[k:])
    return result


async def reconcile_writing_sections(
    task_id: str,
    new_headings: list[str],
    new_sub_outlines: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """目录 / 大纲保存的统一协调入口（取代旧的 upsert_writing_sections）。

    writing_sections 是唯一权威源，toc/outline 两个自由文本字段永远是从它派生、
    重新拼接写回的产物（见 sync_task_derived_texts）。new_sub_outlines=None 表示
    这次只是保存目录——只动 heading/section_index/归档/新建，已匹配（含改名）行
    的 sub_outline 不动；传入等长列表表示保存大纲，连细纲一起同步。

    先只读比对出这次会产生的变更；如果会导致超过 _RECONCILE_CONFIRM_THRESHOLD 个
    "有实质内容"的段落找不到对应（既没精确匹配也配不成改名），且调用方还没有
    confirm=True，直接返回 needs_confirm 预览、不写库——由前端弹窗确认后带
    confirm=True 重新提交同一份数据。
    """
    await backfill_missing_sub_outlines(task_id)
    pool_rows = await database.fetch_all(
        "SELECT id, heading, status, content, sub_outline FROM writing_sections "
        "WHERE task_id = :tid ORDER BY section_index",
        values={"tid": task_id},
    )
    pool = [dict(r) for r in pool_rows]
    alignment = _align_headings(pool, new_headings)

    def _had_content(i: int) -> bool:
        row = pool[i]
        return bool(row["content"]) or row["status"] != "pending"

    risky = [i for i in alignment.deleted if _had_content(i)]
    renamed_preview = [
        {
            "section_id": pool[i]["id"], "old_heading": pool[i]["heading"], "new_heading": new_headings[j],
            "similarity": round(ratio, 3), "had_content": _had_content(i),
        }
        for i, j, ratio in alignment.renamed
    ]
    archived_preview = [
        {"section_id": pool[i]["id"], "heading": pool[i]["heading"], "had_content": _had_content(i)}
        for i in alignment.deleted
    ]
    inserted_preview = [{"heading": new_headings[j]} for j in alignment.inserted]

    if not alignment.renamed and not alignment.deleted and not alignment.inserted:
        order_before = [i for i, r in enumerate(pool) if r["status"] != "archived"]
        order_after = [i for i, _j in sorted(alignment.exact, key=lambda p: p[1])]
        sub_outline_unchanged = new_sub_outlines is None or all(
            (new_sub_outlines[j] or "") == (pool[i]["sub_outline"] or "") for i, j in alignment.exact
        )
        if order_after == order_before and sub_outline_unchanged:
            task = await database.fetch_one(
                "SELECT outline, toc FROM writing_tasks WHERE id = :tid", values={"tid": task_id}
            )
            return {
                "applied": True, "needs_confirm": False, "risky_archive_count": 0,
                "renamed": [], "archived": [], "inserted": [],
                "outline": task["outline"] if task else "", "toc": task["toc"] if task else "",
            }

    if len(risky) > _RECONCILE_CONFIRM_THRESHOLD and not confirm:
        return {
            "applied": False, "needs_confirm": True, "risky_archive_count": len(risky),
            "threshold": _RECONCILE_CONFIRM_THRESHOLD,
            "renamed": renamed_preview, "archived": archived_preview, "inserted": inserted_preview,
        }

    total_new = len(new_headings)
    wc_task = await database.fetch_one(
        "SELECT word_count FROM writing_tasks WHERE id = :tid", values={"tid": task_id}
    )
    wc_total = (wc_task["word_count"] if wc_task else 0) or 0
    per_sec = (wc_total // total_new) if (wc_total > 0 and total_new) else 0

    async def _apply_match(i: int, j: int):
        row = pool[i]
        sets = ["section_index = :idx", "heading = :heading", "updated_at = NOW()"]
        values = {"idx": j, "heading": new_headings[j], "sid": row["id"]}
        if not row["content"]:
            sets.append("word_count_target = :wc")
            values["wc"] = per_sec
        if row["status"] == "archived":
            sets.append("status = 'pending'")
        if new_sub_outlines is not None:
            sets.append("sub_outline = :sub_outline")
            values["sub_outline"] = new_sub_outlines[j]
        await database.execute(
            f"UPDATE writing_sections SET {', '.join(sets)} WHERE id = :sid", values=values,
        )

    for i, j in alignment.exact:
        await _apply_match(i, j)
    for i, j, _ratio in alignment.renamed:
        await _apply_match(i, j)
    for j in alignment.inserted:
        await database.execute(
            """INSERT INTO writing_sections
               (task_id, section_index, heading, sub_outline, word_count_target, status)
               VALUES (:tid, :idx, :heading, :sub_outline, :wc, 'pending')""",
            values={
                "tid": task_id, "idx": j, "heading": new_headings[j],
                "sub_outline": (new_sub_outlines[j] if new_sub_outlines is not None else ""),
                "wc": per_sec,
            },
        )
    for i in alignment.deleted:
        await database.execute(
            "UPDATE writing_sections SET status = 'archived', updated_at = NOW() WHERE id = :sid",
            values={"sid": pool[i]["id"]},
        )

    outline_text, toc_text = await sync_task_derived_texts(task_id)
    return {
        "applied": True, "needs_confirm": False, "risky_archive_count": len(risky),
        "renamed": renamed_preview, "archived": archived_preview, "inserted": inserted_preview,
        "outline": outline_text, "toc": toc_text,
    }


def _parse_outline_text_to_map(outline: str) -> dict[str, str]:
    """把大纲自由文本按 "## " 切成 {标题: 正文} 字典（第一个标题之前的游离文字丢弃）。

    逻辑和 writing.py:_parse_outline_sections 一致，这里单独实现一份纯文本版本，
    避免 backend/db.py 反向 import writing.py 造成循环依赖。
    """
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in outline.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


async def backfill_missing_sub_outlines(task_id: str) -> None:
    """把 sub_outline 还是空的段落，按标题从当前 writing_tasks.outline 文本里找回内容回填。

    历史遗留坑：`sub_outline` 这一列在 reconcile_writing_sections 方案上线前从来没被
    真正写入过（旧的 upsert_writing_sections 永远传空字符串），所以任何一个"还没做过
    一次大纲保存"的任务，它名下所有段落的 sub_outline 全是空的——即便
    writing_tasks.outline 里躺着一大段用户手写或 AI 生成的详细大纲。任何会触发
    sync_task_derived_texts() 重新拼接 outline 的操作（保存目录、单段改名同步）如果不
    先做这一步回填，会把这份历史内容当场清空成只剩标题——这是真实出现过的数据丢失
    事故，务必在"改动 sections 的 heading 之前"调用（用旧标题去匹配当前 outline
    文本），否则刚被改名的那一行会因为标题已经变了而找不到自己原来的内容。
    幂等：只在 sub_outline 为空时才回填，不会覆盖已经有内容的行。
    """
    task = await database.fetch_one(
        "SELECT outline FROM writing_tasks WHERE id = :tid", values={"tid": task_id}
    )
    if not task or not (task["outline"] or "").strip():
        return
    existing_map = _parse_outline_text_to_map(task["outline"])
    if not existing_map:
        return
    rows = await database.fetch_all(
        "SELECT id, heading FROM writing_sections "
        "WHERE task_id = :tid AND (sub_outline IS NULL OR sub_outline = '')",
        values={"tid": task_id},
    )
    for r in rows:
        body = existing_map.get(r["heading"])
        if body:
            await database.execute(
                "UPDATE writing_sections SET sub_outline = :so WHERE id = :sid",
                values={"so": body, "sid": r["id"]},
            )


async def sync_task_derived_texts(task_id: str) -> tuple[str, str]:
    """按 section_index 顺序把 writing_sections 重新拼成 outline/toc 文本写回 writing_tasks。

    writing_sections 是唯一权威源，outline/toc 是它的派生视图——任何一次改变了
    heading/sub_outline/顺序/归档状态的操作之后都要调它一次，这样读取任务详情
    时可以直接读字段，不用每次现拼。
    """
    rows = await get_writing_sections(task_id)   # 已按 status != 'archived' 过滤 + ORDER BY section_index
    outline_text = "\n\n".join(
        (f"## {r['heading']}\n{(r['sub_outline'] or '').strip()}".rstrip()
         if (r.get("sub_outline") or "").strip() else f"## {r['heading']}")
        for r in rows
    )
    toc_text = "\n".join(f"## {r['heading']}" for r in rows)
    await database.execute(
        """UPDATE writing_tasks
           SET outline = :outline, toc = :toc,
               outline_updated_at = NOW(), toc_updated_at = NOW(), updated_at = NOW()
           WHERE id = :tid""",
        values={"outline": outline_text, "toc": toc_text, "tid": task_id},
    )
    return outline_text, toc_text


async def update_writing_section(section_id: str, task_id: str, **kwargs) -> bool:
    allowed = {"heading", "sub_outline", "content", "word_count_target", "status"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = []
    values = {"sid": section_id, "tid": task_id}
    for key, value in fields.items():
        sets.append(f"{key} = :{key}")
        values[key] = value
    if "content" in fields:
        sets.append("last_generated_at = NOW()")
    sets.append("updated_at = NOW()")
    row = await database.fetch_one(
        f"""UPDATE writing_sections SET {', '.join(sets)}
            WHERE id = :sid AND task_id = :tid RETURNING id""",
        values=values,
    )
    return row is not None


async def touch_section_generated_at(section_id: str, task_id: str) -> None:
    """把 last_generated_at 重新打成当前时间，不改任何其它字段。

    专门给"编辑正文时顺带改了标题"这条路径收尾用：那条路径的顺序必须是先保存
    content/heading（这一步已经把 last_generated_at 打成 NOW() 了），再调用
    sync_task_derived_texts() 重新拼 outline/toc（那一步会把 outline_updated_at/
    toc_updated_at 也打成 NOW()，但因为是两条先后执行的 SQL，第二个 NOW() 必然
    比第一个晚）——不这样收尾的话，isSectionStale() 会把"大纲更新时间 > 本段
    last_generated_at"判定为真，导致刚改完标题的这一段，立刻被自己这次编辑
    标成"过期"，纯属这次编辑自己引发的、对自己的误报。
    """
    await database.execute(
        "UPDATE writing_sections SET last_generated_at = NOW(), updated_at = NOW() "
        "WHERE id = :sid AND task_id = :tid",
        values={"sid": section_id, "tid": task_id},
    )


async def delete_writing_section(section_id: str, task_id: str) -> bool:
    row = await database.fetch_one(
        "DELETE FROM writing_sections WHERE id = :sid AND task_id = :tid RETURNING id",
        values={"sid": section_id, "tid": task_id},
    )
    return row is not None


async def get_writing_section_images(section_id: str) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, section_id, marker_text, prompt, image_path, created_at, updated_at
           FROM writing_section_images WHERE section_id = :sid ORDER BY created_at""",
        values={"sid": section_id},
    )
    return [dict(r) for r in rows]


async def upsert_writing_section_image_prompt(section_id: str, marker_text: str, prompt: str) -> None:
    await database.execute(
        """INSERT INTO writing_section_images (id, section_id, marker_text, prompt)
           VALUES (:id, :sid, :marker, :prompt)
           ON CONFLICT (section_id, marker_text)
           DO UPDATE SET prompt = :prompt, updated_at = NOW()""",
        values={"id": str(uuid.uuid4()), "sid": section_id, "marker": marker_text, "prompt": prompt},
    )


async def upsert_writing_section_image_file(section_id: str, marker_text: str, image_path: str) -> None:
    await database.execute(
        """INSERT INTO writing_section_images (id, section_id, marker_text, image_path)
           VALUES (:id, :sid, :marker, :path)
           ON CONFLICT (section_id, marker_text)
           DO UPDATE SET image_path = :path, updated_at = NOW()""",
        values={"id": str(uuid.uuid4()), "sid": section_id, "marker": marker_text, "path": image_path},
    )


async def update_style_skills(task_id: str, skills_text: str, source_text: str | None = None) -> bool:
    sets = ["style_skills = :skills", "style_skills_updated_at = NOW()", "updated_at = NOW()"]
    values: dict = {"tid": task_id, "skills": skills_text}
    if source_text is not None:
        sets.append("style_source_text = :src")
        values["src"] = source_text
    row = await database.fetch_one(
        f"UPDATE writing_tasks SET {', '.join(sets)} WHERE id = :tid RETURNING id",
        values=values,
    )
    return row is not None


async def save_writing_evaluation(
    task_id: str,
    readability_score: int,
    readability_report: str,
    style_score: int,
    style_report: str,
    overall_score: int,
) -> dict:
    row = await database.fetch_one(
        """INSERT INTO writing_evaluations
           (task_id, readability_score, readability_report, style_score, style_report, overall_score)
           VALUES (:tid, :rs, :rr, :ss, :sr, :os)
           RETURNING id, overall_score, created_at""",
        values={
            "tid": task_id, "rs": readability_score, "rr": readability_report,
            "ss": style_score, "sr": style_report, "os": overall_score,
        },
    )
    return dict(row) if row else {}


async def get_latest_evaluation(task_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, task_id, readability_score, readability_report,
                  style_score, style_report, overall_score, created_at
           FROM writing_evaluations WHERE task_id = :tid
           ORDER BY created_at DESC LIMIT 1""",
        values={"tid": task_id},
    )
    return dict(row) if row else None


async def get_user_processed_files(user_id: int) -> list[str]:
    rows = await database.fetch_all(
        """SELECT DISTINCT uf.filename
           FROM upload_files uf JOIN sessions s ON uf.session_id = s.id
           WHERE s.user_id = :uid AND uf.status = 'done'
           ORDER BY uf.filename""",
        values={"uid": user_id},
    )
    return [r["filename"] for r in rows]


# ---- 作图模块 ----

def _decode_prompt_ids(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


async def create_drawing_style(user_id: int, name: str, prompt_ids: list[str] | None = None) -> str:
    style_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO drawing_styles (id, user_id, name, prompt_ids)
           VALUES (:id, :uid, :name, CAST(:prompt_ids AS jsonb))""",
        values={
            "id": style_id,
            "uid": user_id,
            "name": name or "未命名风格",
            "prompt_ids": json.dumps(prompt_ids or [], ensure_ascii=False),
        },
    )
    return style_id


async def get_drawing_styles(user_id: int) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, name, created_at, updated_at
           FROM drawing_styles WHERE user_id = :uid ORDER BY created_at DESC""",
        values={"uid": user_id},
    )
    return [dict(r) for r in rows]


async def get_drawing_style(style_id: str, user_id: int) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, user_id, name, prompt_ids, skill_package_id, created_at, updated_at
           FROM drawing_styles WHERE id = :sid AND user_id = :uid""",
        values={"sid": style_id, "uid": user_id},
    )
    if not row:
        return None
    style = dict(row)
    style["prompt_ids"] = _decode_prompt_ids(style.get("prompt_ids"))
    return style


async def drawing_style_owned_by(style_id: str, user_id: int) -> bool:
    row = await database.fetch_one(
        "SELECT 1 FROM drawing_styles WHERE id = :sid AND user_id = :uid LIMIT 1",
        values={"sid": style_id, "uid": user_id},
    )
    return row is not None


async def update_drawing_style(style_id: str, user_id: int, **kwargs) -> bool:
    allowed = {"name", "prompt_ids", "skill_package_id"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = []
    values = {"sid": style_id, "uid": user_id}
    for key, value in fields.items():
        if key == "prompt_ids":
            sets.append(f"{key} = CAST(:{key} AS jsonb)")
            values[key] = json.dumps(value or [], ensure_ascii=False)
        else:
            sets.append(f"{key} = :{key}")
            values[key] = value
    sets.append("updated_at = NOW()")
    row = await database.fetch_one(
        f"""UPDATE drawing_styles SET {', '.join(sets)}
            WHERE id = :sid AND user_id = :uid RETURNING id""",
        values=values,
    )
    return row is not None


async def delete_drawing_style(style_id: str, user_id: int) -> bool:
    row = await database.fetch_one(
        "DELETE FROM drawing_styles WHERE id = :sid AND user_id = :uid RETURNING id",
        values={"sid": style_id, "uid": user_id},
    )
    return row is not None


async def create_drawing_prompt(name: str, content: str, created_by: int) -> str:
    prompt_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO drawing_prompts (id, name, content, created_by)
           VALUES (:id, :name, :content, :uid)""",
        values={"id": prompt_id, "name": name or "", "content": content or "", "uid": created_by},
    )
    return prompt_id


async def get_drawing_prompts() -> list[dict]:
    rows = await database.fetch_all(
        "SELECT id, name, content, created_by, created_at FROM drawing_prompts ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def delete_drawing_prompt(prompt_id: str) -> bool:
    row = await database.fetch_one(
        "DELETE FROM drawing_prompts WHERE id = :pid RETURNING id",
        values={"pid": prompt_id},
    )
    return row is not None


async def update_drawing_prompt(prompt_id: str, **kwargs) -> bool:
    allowed = {"name", "content"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    row = await database.fetch_one(
        f"UPDATE drawing_prompts SET {sets} WHERE id = :pid RETURNING id",
        values={**fields, "pid": prompt_id},
    )
    return row is not None


async def create_drawing_skill_package(name: str, source_url: str, instructions: str, created_by: int) -> str:
    package_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO drawing_skill_packages (id, name, source_url, instructions, created_by)
           VALUES (:id, :name, :src, :ins, :uid)""",
        values={
            "id": package_id,
            "name": name or "",
            "src": source_url or "",
            "ins": instructions or "",
            "uid": created_by,
        },
    )
    return package_id


async def get_drawing_skill_packages() -> list[dict]:
    rows = await database.fetch_all(
        "SELECT id, name, source_url, kind, created_by, created_at FROM drawing_skill_packages ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def get_drawing_skill_package(package_id: str) -> dict | None:
    row = await database.fetch_one(
        "SELECT id, name, source_url, kind, instructions, created_by, created_at "
        "FROM drawing_skill_packages WHERE id = :pid",
        values={"pid": package_id},
    )
    return dict(row) if row else None


async def delete_drawing_skill_package(package_id: str) -> bool:
    row = await database.fetch_one(
        "DELETE FROM drawing_skill_packages WHERE id = :pid RETURNING id",
        values={"pid": package_id},
    )
    return row is not None


async def update_drawing_skill_package(package_id: str, **kwargs) -> bool:
    allowed = {"name", "source_url", "instructions"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    row = await database.fetch_one(
        f"UPDATE drawing_skill_packages SET {sets} WHERE id = :pid RETURNING id",
        values={**fields, "pid": package_id},
    )
    return row is not None


async def create_drawing_generation(
    style_id: str, user_id: int, user_input: str, full_prompt: str, parent_generation_id: str | None = None
) -> str:
    generation_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO drawing_generations (id, style_id, user_id, user_input, full_prompt, status, parent_generation_id)
           VALUES (:id, :sid, :uid, :input, :prompt, 'pending', :pid)""",
        values={
            "id": generation_id,
            "sid": style_id,
            "uid": user_id,
            "input": user_input or "",
            "prompt": full_prompt or "",
            "pid": parent_generation_id,
        },
    )
    return generation_id


async def update_drawing_generation_result(
    generation_id: str, *, status: str, image_path: str = "", model: str = "", error_msg: str = ""
) -> None:
    await database.execute(
        """UPDATE drawing_generations
           SET status = :status, image_path = :image_path, model = :model, error_msg = :error_msg
           WHERE id = :gid""",
        values={
            "gid": generation_id,
            "status": status,
            "image_path": image_path,
            "model": model,
            "error_msg": error_msg,
        },
    )


async def get_drawing_generations(style_id: str, limit: int = 50) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, style_id, user_input, full_prompt, image_path, model, status, error_msg, created_at
           FROM drawing_generations WHERE style_id = :sid
           ORDER BY created_at DESC LIMIT :limit""",
        values={"sid": style_id, "limit": limit},
    )
    return [dict(r) for r in rows]


async def get_drawing_generation(generation_id: str) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, style_id, user_id, user_input, full_prompt, image_path, model, status, error_msg, created_at
           FROM drawing_generations WHERE id = :gid""",
        values={"gid": generation_id},
    )
    return dict(row) if row else None


async def delete_drawing_generation(generation_id: str, user_id: int) -> dict | None:
    row = await database.fetch_one(
        "DELETE FROM drawing_generations WHERE id = :gid AND user_id = :uid RETURNING id, image_path",
        values={"gid": generation_id, "uid": user_id},
    )
    return dict(row) if row else None


async def get_drawing_generation_tips(style_id: str, limit: int = 50) -> list[dict]:
    """每条编辑历史链的最新一步（没有任何行以它为 parent 的行），供折叠画廊列表用。"""
    rows = await database.fetch_all(
        """SELECT g.id, g.style_id, g.user_input, g.full_prompt, g.image_path, g.model,
                  g.status, g.error_msg, g.parent_generation_id, g.created_at
           FROM drawing_generations g
           WHERE g.style_id = :sid
             AND NOT EXISTS (SELECT 1 FROM drawing_generations c WHERE c.parent_generation_id = g.id)
           ORDER BY g.created_at DESC LIMIT :limit""",
        values={"sid": style_id, "limit": limit},
    )
    return [dict(r) for r in rows]


async def get_drawing_generation_lineage(generation_id: str, user_id: int) -> list[dict]:
    """给定链上任意一个节点（通常是叶子），沿 parent_generation_id 回溯到根，按时间正序返回整条链。"""
    rows = await database.fetch_all(
        """WITH RECURSIVE chain AS (
               SELECT * FROM drawing_generations WHERE id = :gid AND user_id = :uid
               UNION ALL
               SELECT g.* FROM drawing_generations g
               JOIN chain c ON g.id = c.parent_generation_id
           )
           SELECT id, style_id, user_input, full_prompt, image_path, model, status, error_msg,
                  parent_generation_id, created_at
           FROM chain ORDER BY created_at ASC""",
        values={"gid": generation_id, "uid": user_id},
    )
    return [dict(r) for r in rows]


async def delete_drawing_generation_step(generation_id: str, user_id: int) -> dict | None:
    """删除链上单独一步：把它的子节点接到它的父节点上，保留链的其余部分。"""
    async with database.transaction():
        row = await database.fetch_one(
            "SELECT id, parent_generation_id, image_path FROM drawing_generations WHERE id = :gid AND user_id = :uid",
            values={"gid": generation_id, "uid": user_id},
        )
        if not row:
            return None
        await database.execute(
            "UPDATE drawing_generations SET parent_generation_id = :new_parent WHERE parent_generation_id = :gid",
            values={"new_parent": row["parent_generation_id"], "gid": generation_id},
        )
        deleted = await database.fetch_one(
            "DELETE FROM drawing_generations WHERE id = :gid AND user_id = :uid RETURNING id, image_path",
            values={"gid": generation_id, "uid": user_id},
        )
        return dict(deleted) if deleted else None


async def delete_drawing_generation_lineage(generation_id: str, user_id: int) -> list[str] | None:
    """删除整条编辑历史链：定位到根节点后删除，ON DELETE CASCADE 级联清空所有子孙。
    返回全部被删行的 image_path（供路由层清理磁盘文件），lineage 不存在/不属于该用户时返回 None
    （区别于"删除成功但没有任何图片文件"的正常空列表，供路由层区分 404 和成功）。"""
    chain = await get_drawing_generation_lineage(generation_id, user_id)
    if not chain:
        return None
    root_id = chain[0]["id"]
    await database.execute(
        "DELETE FROM drawing_generations WHERE id = :rid AND user_id = :uid",
        values={"rid": root_id, "uid": user_id},
    )
    return [c["image_path"] for c in chain if c.get("image_path")]


# ============================ 地图模块 ============================
# 一张地图 = map_documents 一行，preset(JSONB) = { style:{…}, annotations:{points,links} }。
# preset 实时防抖 PATCH 原地更新；版本历史见 map_preset_versions（留最近 3 版）。
_MAP_VERSIONS_KEEP = 3


def _as_json(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


async def create_map_document(user_id: int, name: str, preset: dict | None = None) -> str:
    map_id = str(uuid.uuid4())
    await database.execute(
        """INSERT INTO map_documents (id, user_id, name, preset)
           VALUES (:id, :uid, :name, CAST(:preset AS JSONB))""",
        values={"id": map_id, "uid": user_id, "name": name or "未命名地图",
                "preset": _as_json(preset or {})},
    )
    return map_id


async def get_map_documents(user_id: int) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, name, thumb_path, created_at, updated_at
           FROM map_documents WHERE user_id = :uid ORDER BY updated_at DESC""",
        values={"uid": user_id},
    )
    return [dict(r) for r in rows]


async def get_map_document(map_id: str, user_id: int) -> dict | None:
    row = await database.fetch_one(
        """SELECT id, user_id, name, preset, thumb_path, created_at, updated_at
           FROM map_documents WHERE id = :id AND user_id = :uid""",
        values={"id": map_id, "uid": user_id},
    )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("preset"), str):
        d["preset"] = json.loads(d["preset"] or "{}")
    return d


async def map_document_owned_by(map_id: str, user_id: int) -> bool:
    row = await database.fetch_one(
        "SELECT 1 FROM map_documents WHERE id = :id AND user_id = :uid",
        values={"id": map_id, "uid": user_id},
    )
    return row is not None


async def update_map_document(map_id: str, user_id: int, *, name: str | None = None,
                              preset: dict | None = None) -> bool:
    sets, values = ["updated_at = NOW()"], {"id": map_id, "uid": user_id}
    if name is not None:
        sets.append("name = :name")
        values["name"] = name
    if preset is not None:
        sets.append("preset = CAST(:preset AS JSONB)")
        values["preset"] = _as_json(preset)
    if len(sets) == 1:
        return False
    result = await database.execute(
        f"UPDATE map_documents SET {', '.join(sets)} WHERE id = :id AND user_id = :uid",
        values=values,
    )
    return True


async def delete_map_document(map_id: str, user_id: int) -> dict | None:
    row = await database.fetch_one(
        "DELETE FROM map_documents WHERE id = :id AND user_id = :uid RETURNING id, thumb_path",
        values={"id": map_id, "uid": user_id},
    )
    return dict(row) if row else None


async def set_map_thumb(map_id: str, user_id: int, thumb_path: str) -> None:
    await database.execute(
        "UPDATE map_documents SET thumb_path = :p WHERE id = :id AND user_id = :uid",
        values={"p": thumb_path, "id": map_id, "uid": user_id},
    )


async def list_map_versions(map_id: str) -> list[dict]:
    rows = await database.fetch_all(
        """SELECT id, version, note, created_at FROM map_preset_versions
           WHERE map_id = :mid ORDER BY version DESC""",
        values={"mid": map_id},
    )
    return [dict(r) for r in rows]


async def get_map_version(map_id: str, version: int) -> dict | None:
    row = await database.fetch_one(
        "SELECT id, version, note, preset, created_at FROM map_preset_versions WHERE map_id = :mid AND version = :v",
        values={"mid": map_id, "v": version},
    )
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("preset"), str):
        d["preset"] = json.loads(d["preset"] or "{}")
    return d


async def snapshot_map_version(map_id: str, preset: dict, note: str = "checkpoint") -> int:
    """把给定 preset 存为一个新版本快照；超过 _MAP_VERSIONS_KEEP 版时删最旧。返回新版本号。"""
    row = await database.fetch_one(
        "SELECT COALESCE(MAX(version), 0) AS v FROM map_preset_versions WHERE map_id = :mid",
        values={"mid": map_id},
    )
    next_version = int(row["v"]) + 1
    await database.execute(
        """INSERT INTO map_preset_versions (map_id, preset, version, note)
           VALUES (:mid, CAST(:preset AS JSONB), :v, :note)""",
        values={"mid": map_id, "preset": _as_json(preset), "v": next_version, "note": note},
    )
    old = await database.fetch_all(
        """SELECT version FROM map_preset_versions WHERE map_id = :mid
           ORDER BY version DESC OFFSET :keep""",
        values={"mid": map_id, "keep": _MAP_VERSIONS_KEEP},
    )
    for r in old:
        await database.execute(
            "DELETE FROM map_preset_versions WHERE map_id = :mid AND version = :v",
            values={"mid": map_id, "v": r["version"]},
        )
    return next_version


async def latest_map_version_preset(map_id: str) -> dict | None:
    row = await database.fetch_one(
        "SELECT preset FROM map_preset_versions WHERE map_id = :mid ORDER BY version DESC LIMIT 1",
        values={"mid": map_id},
    )
    if not row:
        return None
    p = row["preset"]
    return p if isinstance(p, dict) else json.loads(p)
