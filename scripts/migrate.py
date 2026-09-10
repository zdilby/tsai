"""
数据库迁移脚本：为已有库补充新增字段。
幂等操作，重复执行安全。
用法：python -m scripts.migrate
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db import database


MIGRATIONS = [
    # upload_files 表：新增文件处理状态字段
    ("upload_files.status",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'"),
    ("upload_files.total_chunks",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS total_chunks INTEGER DEFAULT 0"),
    ("upload_files.processed_chunks",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS processed_chunks INTEGER DEFAULT 0"),
    ("upload_files.error_msg",
     "ALTER TABLE upload_files ADD COLUMN IF NOT EXISTS error_msg TEXT"),

    # knowledge_base 表：新增语义分块相关字段
    ("knowledge_base.original_content",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS original_content TEXT"),
    ("knowledge_base.source_file",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source_file TEXT"),
    ("knowledge_base.chunk_index",
     "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS chunk_index INTEGER DEFAULT 0"),

    # messages 表：新增 token 统计字段
    ("messages.tokens_in",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_in INTEGER DEFAULT 0"),
    ("messages.tokens_out",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_out INTEGER DEFAULT 0"),
    ("messages.tokens_total",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_total INTEGER DEFAULT 0"),

    # users 表：新增管理员与每日配额字段
    ("users.is_admin",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"),
    ("users.max_daily_tokens",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_daily_tokens INTEGER DEFAULT 200000"),
    ("users.max_file_size_mb",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_file_size_mb INTEGER DEFAULT 10"),

    # sessions 表：新增 persona 字段（对话机器人性格设定）
    ("sessions.persona",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS persona TEXT"),

    # 统一新用户每日 Token 默认值为 100000（修正旧默认值 200000 或 NULL）
    ("users.max_daily_tokens.default",
     "ALTER TABLE users ALTER COLUMN max_daily_tokens SET DEFAULT 100000"),
    ("users.max_daily_tokens.fix_null",
     "UPDATE users SET max_daily_tokens = 100000 WHERE max_daily_tokens IS NULL AND is_admin = FALSE"),

    # messages 表：新增历史语义检索 embedding 字段
    ("messages.embedding",
     "ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding vector(768)"),
    ("idx_messages_embedding",
     "CREATE INDEX IF NOT EXISTS idx_messages_embedding ON messages USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"),

    # sessions 表：新增 AI 处理后的 system_instruction 与原始输入字段
    ("sessions.system_instruction_origin",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS system_instruction_origin TEXT"),
    ("sessions.system_instruction",
     "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS system_instruction TEXT"),
    # 迁移已有 persona 数据到新字段（不重复处理已迁移行）
    ("sessions.migrate_persona",
     "UPDATE sessions SET system_instruction_origin = persona, system_instruction = persona WHERE persona IS NOT NULL AND system_instruction IS NULL"),

    # writing_tasks 表：分段写作所需字段
    ("writing_tasks.toc",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS toc TEXT DEFAULT ''"),
    ("writing_tasks.toc_updated_at",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS toc_updated_at TIMESTAMPTZ"),
    ("writing_tasks.outline_updated_at",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS outline_updated_at TIMESTAMPTZ"),

    # writing_sections 表：每个分段的内容与状态
    ("writing_sections.table", """
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
    """),
    ("writing_sections.idx_task_id",
     "CREATE INDEX IF NOT EXISTS idx_writing_sections_task_id ON writing_sections(task_id, section_index)"),

    # writing_tasks 表：风格技能蒸馏
    ("writing_tasks.style_skills",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_skills TEXT DEFAULT ''"),
    ("writing_tasks.style_skills_updated_at",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_skills_updated_at TIMESTAMPTZ"),
    ("writing_tasks.style_source_text",
     "ALTER TABLE writing_tasks ADD COLUMN IF NOT EXISTS style_source_text TEXT DEFAULT ''"),

    # writing_evaluations 表：质量评估结果
    ("writing_evaluations.table", """
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
    """),
    ("writing_evaluations.idx_task_id",
     "CREATE INDEX IF NOT EXISTS idx_writing_evaluations_task_id ON writing_evaluations(task_id, created_at DESC)"),

    # users 表：作图模块权限
    ("users.can_draw",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_draw BOOLEAN NOT NULL DEFAULT FALSE"),

    # drawing_skills 表：共享的固定作图 skill 库
    ("drawing_skills.table", """
        CREATE TABLE IF NOT EXISTS drawing_skills (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            snippet     TEXT NOT NULL DEFAULT '',
            created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """),

    # drawing_styles 表：作图风格（侧栏 tab）
    ("drawing_styles.table", """
        CREATE TABLE IF NOT EXISTS drawing_styles (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        TEXT NOT NULL DEFAULT '未命名风格',
            prompt      TEXT NOT NULL DEFAULT '',
            skill_ids   JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """),
    ("drawing_styles.idx_user_id",
     "CREATE INDEX IF NOT EXISTS idx_drawing_styles_user_id ON drawing_styles(user_id)"),

    # drawing_generations 表：生成记录（本地磁盘 + DB 路径）
    ("drawing_generations.table", """
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
    """),
    ("drawing_generations.idx_style_id",
     "CREATE INDEX IF NOT EXISTS idx_drawing_generations_style_id ON drawing_generations(style_id, created_at DESC)"),

    # drawing_generations 表：迭代编辑历史链（自引用父子关系）
    ("drawing_generations.parent_generation_id",
     "ALTER TABLE drawing_generations ADD COLUMN IF NOT EXISTS parent_generation_id UUID REFERENCES drawing_generations(id) ON DELETE CASCADE"),
    ("drawing_generations.idx_parent",
     "CREATE INDEX IF NOT EXISTS idx_drawing_generations_parent ON drawing_generations(parent_generation_id)"),

    # drawing_skill_packages 表：第三方"Skill 包"（完整方法论文档，非零散片段）
    ("drawing_skill_packages.table", """
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
    """),
    ("drawing_styles.skill_package_id",
     "ALTER TABLE drawing_styles ADD COLUMN IF NOT EXISTS skill_package_id UUID REFERENCES drawing_skill_packages(id) ON DELETE SET NULL"),

    # drawing_skills 改名为 drawing_prompts：合并"风格私有 prompt"和"共享 skill 片段库"为统一的 Prompt 库概念
    ("drawing_prompts.rename_from_skills", """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'drawing_skills')
               AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'drawing_prompts') THEN
                ALTER TABLE drawing_skills RENAME TO drawing_prompts;
            END IF;
        END $$
    """),
    ("drawing_prompts.table", """
        CREATE TABLE IF NOT EXISTS drawing_prompts (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """),
    ("drawing_prompts.rename_snippet_to_content", """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'drawing_prompts' AND column_name = 'snippet') THEN
                ALTER TABLE drawing_prompts RENAME COLUMN snippet TO content;
            END IF;
        END $$
    """),
    ("drawing_styles.rename_skill_ids_to_prompt_ids", """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'drawing_styles' AND column_name = 'skill_ids') THEN
                ALTER TABLE drawing_styles RENAME COLUMN skill_ids TO prompt_ids;
            END IF;
        END $$
    """),
    ("drawing_styles.drop_prompt_column",
     "ALTER TABLE drawing_styles DROP COLUMN IF EXISTS prompt"),

    # writing_section_images 表：段落内"[配图：xxx]"标记对应的 prompt 草稿 + 最终上传图片
    ("writing_section_images.table", """
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
    """),
    ("writing_section_images.idx_section_id",
     "CREATE INDEX IF NOT EXISTS idx_writing_section_images_section_id ON writing_section_images(section_id)"),
]


async def main():
    await database.connect()
    print("开始迁移...")
    for name, sql in MIGRATIONS:
        try:
            await database.execute(sql)
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            await database.disconnect()
            sys.exit(1)

    await database.disconnect()
    print("迁移完成。")


if __name__ == "__main__":
    asyncio.run(main())
