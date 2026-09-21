# TSAI 项目架构文档

> 本文档由 Claude Code 自动生成并维护，随代码变动同步更新。
> 最后更新：2026-09-22

---

## 一、项目概述

TSAI 是一个**多用户 AI 异步对话系统**，集成了 RAG（检索增强生成）、文档知识库、网络搜索增强等能力。每个对话 Session 拥有独立的知识库，用户可上传文档，系统自动分块、向量化并在对话中检索相关内容。

**技术栈一览：**

| 层次 | 技术 |
|---|---|
| 后端框架 | FastAPI |
| AI 模型 | Google Gemini（`gemini-2.5-flash` + `gemini-embedding-exp-03-07`） |
| 数据库 | PostgreSQL + pgvector（向量相似搜索） |
| 认证 | JWT（HTTP-only Cookie）+ bcrypt |
| 文档解析 | pdfplumber / pytesseract / ebooklib / python-docx |
| 前端 | Jinja2 模板 + Materialize CSS + jQuery + Marked.js |
| 网络搜索 | Google Custom Search API |

---

## 二、目录结构

```
tsai/
├── main.py               # 核心路由：聊天、Session 管理
├── account.py            # 认证路由：登录、注册、改密
├── admin.py              # 管理员路由：用户管理、邀请码
├── writing.py            # 写作模块路由（writing_router，前缀 /writing/）
├── drawing.py            # 作图模块路由（drawing_router，前缀 /drawing/）
├── settings.py           # 配置加载（.env）、全局 logger
├── backend/
│   ├── db.py             # 全部 SQL 操作与数据库 Schema
│   ├── rag.py            # 向量检索、Embedding 生成
│   └── image_gen.py      # 作图模块：调用 OpenAI 兼容 images/generations 接口
├── midware/
│   ├── tools.py          # 文档解析、分块、网络搜索
│   └── upload.py         # 文件上传与后台处理
├── templates/            # Jinja2 HTML 模板
│   ├── chat.html         # 主聊天界面（sidenav + .main 双栏）
│   ├── writing.html      # 写作模块界面（#writing-sidebar + .writing-shell 双栏，内含 1fr+300px 网格）
│   ├── drawing.html      # 作图模块界面（#drawing-sidebar + .drawing-shell 双栏，内含 1fr+300px 网格）
│   ├── account/          # 登录/注册页
│   └── admin/            # 管理员后台页
├── static/               # CSS、JS、用户上传文件
│   ├── loads/            # 用户上传文件：loads/{username}/{session_id}/
│   ├── images/           # 作图模块生成图片：images/{username}/{style_id}/{generation_id}.png
│   ├── js/drawing.js     # 作图模块前端逻辑
│   └── css/drawing.css   # 作图模块样式
├── scripts/              # 运维脚本（建管理员、生成邀请码等）
└── logs/process.log      # 应用日志
```

---

## 三、API 路由一览

### 核心聊天路由（`main.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/` | 主界面，自动初始化 null Session |
| `GET` | `/ping` | 健康检查 |
| `GET` | `/healthz` | 健康检查（同 `/ping`，供负载均衡器使用） |
| `POST` | `/chat` | 核心对话接口（含 RAG + 网络搜索） |
| `POST` | `/new_session` | 创建命名 Session |
| `POST` | `/change_session` | 重命名 Session |
| `POST` | `/del_session` | 删除 Session |
| `GET` | `/sessions` | 获取用户全部命名 Session |
| `GET` | `/messages/{session_id}` | 获取 Session 历史消息 |
| `GET` | `/collections/{session_id}` | 获取 Session 上传文件列表 |
| `GET` | `/session_persona/{session_id}` | 获取 Session 角色人格 |
| `POST` | `/session_persona` | 设置 Session 角色人格（AI 处理，后台任务） |
| `POST` | `/save_to_rag` | 将对话摘要存入知识库 |

### 认证路由（`account.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/account/login` | 登录页 |
| `GET` | `/account/invite` | 注册页 |
| `POST` | `/account/register` | 注册接口（需邀请码） |
| `POST` | `/account/token` | 登录接口（OAuth2 表单） |
| `POST` | `/account/logout` | 登出（清除 Cookie） |
| `POST` | `/account/change_password` | 修改密码 |

### 文件上传路由（`midware/upload.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `POST` | `/upload/` | 上传文件（异步后台处理） |
| `GET` | `/upload/status/{session_id}` | 查询文件处理状态 |
| `POST` | `/upload/reprocess` | 重新处理失败文件 |

### 写作模块路由（`writing.py`，前缀 `/writing/`）

所有路由（除 HTML 页面外）经 `require_write_access` 依赖校验：`is_admin` 或 `can_write=TRUE` 才放行，否则 403（HTML 页面 302 回首页）。

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/writing/` | 写作首页（自动跳转最新任务） |
| `GET` | `/writing/{task_id}` | 写作任务页面（Jinja2 HTML） |
| `POST` | `/writing/tasks` | 新建写作任务（同时创建 is_writing_session Session） |
| `GET` | `/writing/tasks` | 获取用户全部写作任务列表 |
| `GET` | `/writing/tasks/{task_id}` | 获取单个写作任务详情 |
| `PATCH` | `/writing/tasks/{task_id}` | 更新写作任务设置（title/word_count/style_req/content_req/outline/toc/reference_files，只发差异字段）。`outline`/`toc` 变更时走 `reconcile_writing_sections` 协调同步 `writing_sections`（`outline` 优先，见十一.4.1），可能返回 `{needs_confirm:true, reconcile_preview}` 而不落库，需带 `confirm_reconcile:true` 重新提交 |
| `DELETE` | `/writing/tasks/{task_id}` | 删除写作任务 |
| `GET` | `/writing/tasks/{task_id}/content` | 获取最新写作内容（+version 号） |
| `POST` | `/writing/tasks/{task_id}/content` | 保存写作内容（版本化，保留最近 3 版） |
| `POST` | `/writing/tasks/{task_id}/format_content` | 全文 Markdown 排版（Codex 优先，失败回退 Gemini，双失败保留原文），保存为新版本 |
| `GET` | `/writing/files` | 获取当前用户所有已处理完成的文件（供参考资料选择） |
| `POST` | `/writing/generate_style` | SSE 流式生成风格描述（从 URL 网页或上传文档提取文本，附 `task_id` 时保存原文供后续风格蒸馏） |
| `GET` | `/writing/tasks/{task_id}/generate_outline` | SSE 流式生成内容大纲 |
| `POST` | `/writing/tasks/{task_id}/generate_toc` | SSE 流式生成写作目录（TOC，5-10 条 `## ` 标题；有大纲则从大纲提炼，否则从任务设置生成） |
| `POST` | `/writing/tasks/{task_id}/generate_outline_from_toc` | SSE 流式：由已保存的 TOC 反向生成逐章详细大纲 |
| `POST` | `/writing/tasks/{task_id}/generate_content` | SSE 流式生成/优化写作内容（含 RAG 参考资料检索）；大纲 ≥2 章节且总字数=0 或 ≥3000 时自动切换为**逐章节生成**（`use_sectional`），每章独立调用并携带前文尾部 1200 字作衔接提示 |
| `POST` | `/writing/tasks/{task_id}/chat` | 写作 AI 对话（全文级）；AI 用 `[WRITING_UPDATE_START]...[WRITING_UPDATE_END]` 包裹修改后全文 |
| `GET` | `/writing/tasks/{task_id}/sections` | 获取任务的全部分段（`writing_sections`，按 `section_index` 排序） |
| `PATCH` | `/writing/tasks/{task_id}/sections/{section_id}` | 更新单个段落（heading/sub_outline/content/word_count_target/status）。只传 `content` 且没显式传 `heading` 时，若正文首行是 `## 新标题`，自动同步为该段新标题并重算任务的 outline/toc 派生文本（响应带 `heading_synced`），见十一.4.1。`status` 传 `confirmed`（即"确认"定稿）时，若别的段落已有内容，额外跑一次和"生成"完全同一套的大纲一致性检查，响应带 `outline_review`（无建议为 `null`），见十一.4.2 |
| `POST` | `/writing/tasks/{task_id}/sections/{section_id}/generate` | SSE 流式生成单段内容（携带上一个已生成段落的**完整**正文作衔接，而不是摘要——内容可能被人工编辑过，衔接要看真实内容），完成后段落状态置为 `draft`；若任务里还有别的段落已有内容，额外跑一次大纲一致性检查，需要调整时在流末尾追加一个 `type:"outline_review"` 的信号帧（非文本分片），见十一.4.2 |
| `POST` | `/writing/tasks/{task_id}/sections/{section_id}/apply_outline_review` | 应用一致性检查提出的大纲调整建议（`scope: "section"｜"overall"`），见十一.4.2 |
| `POST` | `/writing/tasks/{task_id}/sections/{section_id}/dismiss_stale` | 只关闭该段的"⚠ 过期"提示（`touch_section_generated_at`），不改正文/heading/status，不触发任何大纲同步或一致性检查，见十一.4.2 |
| `POST` | `/writing/tasks/{task_id}/sections/{section_id}/format` | 单段 Markdown 排版（同 Codex→Gemini 回退策略） |
| `POST` | `/writing/tasks/{task_id}/sections/{section_id}/chat` | 单段 AI 对话，AI 用 `[SECTION_UPDATE_START]...[SECTION_UPDATE_END]` 包裹修改后该段内容 |
| `GET` | `/writing/tasks/{task_id}/full_content` | 拼接所有 `draft`/`confirmed` 状态段落为完整正文（分段视图 → 全文视图），响应含 `skipped_headings`（未生成/非 draft-confirmed 的章节标题列表） |
| `POST` | `/writing/tasks/{task_id}/distill_style` | SSE 流式：将风格描述 + 参考原文蒸馏为结构化「风格技能手册」（6 节：语气腔调/句式结构/词汇风格/叙事节奏/过渡衔接/结构模式），存入 `writing_tasks.style_skills`，后续生成自动注入并优先于 `style_req` |
| `POST` | `/writing/tasks/{task_id}/evaluate` | SSE 流式质量评估 Pipeline（阅读检查 → 风格比对，串行执行，见十一.4） |
| `GET` | `/writing/tasks/{task_id}/evaluations/latest` | 获取最近一次评估结果 |

### 作图模块路由（`drawing.py`，前缀 `/drawing/`）

所有路由经 `require_draw_access` 依赖校验：`is_admin` 或 `can_draw=TRUE` 才放行（校验逻辑逐字镜像写作模块的 `require_write_access`），否则 403（HTML 页面路由 302 回首页）。详见十五「作图模块」。

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/drawing/` | 作图首页（自动跳转最新风格，无风格则渲染空态） |
| `GET` | `/drawing/{style_id}` | 作图风格页面（Jinja2 HTML；路由声明在文件末尾，避免抢先匹配 `/styles`、`/prompts` 等固定路径） |
| `POST` | `/drawing/styles` | 新建作图风格（name/prompt_ids） |
| `GET` | `/drawing/styles` | 获取当前用户全部作图风格（侧栏列表） |
| `GET` | `/drawing/styles/{id}` | 获取单个风格详情 |
| `PATCH` | `/drawing/styles/{id}` | 更新风格（name/prompt_ids/skill_package_id，只发差异字段） |
| `DELETE` | `/drawing/styles/{id}` | 删除风格（级联删除其全部生成记录，并尽力清理磁盘图片文件） |
| `GET` | `/drawing/prompts` | 获取共享 Prompt 库（全体作图用户共用，不分用户） |
| `POST` | `/drawing/prompts` | 新增 Prompt 条目（name + content） |
| `DELETE` | `/drawing/prompts/{id}` | 删除 Prompt（风格里残留的引用在拼 prompt 时按 id 过滤，不做强约束） |
| `POST` | `/drawing/styles/{id}/generate` | 核心生成接口：拼 prompt（绑 skill 包/编辑走 LLM 编译）→ 写 `status='processing'` 行 → **后台任务**调 `backend/image_gen.py` 出图 → 立即返回 `{id, status:"processing"}`（不再同步等出图） |
| `GET` | `/drawing/generations/{id}/status` | 轮询单条生成状态：`processing` / `done`（带 `image_url`）/ `failed`（带 `error_msg`） |
| `GET` | `/drawing/styles/{id}/generations` | 获取该风格的生成历史（画廊，最近 50 条） |
| `DELETE` | `/drawing/generations/{id}` | 删除单条生成记录（同步删除磁盘图片文件） |

### 管理员路由（`admin.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/admin/` | 管理员总览（用户 + 邀请码） |
| `GET` | `/admin/user/{id}` | 用户详情页 |
| `GET` | `/admin/session/{id}` | Session 详情页 |
| `POST` | `/admin/user/{id}/set_admin` | 将指定用户提升为管理员 |
| `POST` | `/admin/user/{id}/set_writing` | 授予/撤销该用户的写作模块访问权限（`can_write`） |
| `POST` | `/admin/user/{id}/set_draw` | 授予/撤销该用户的作图模块访问权限（`can_draw`） |
| `POST` | `/admin/user/{id}/max_tokens` | 设置每日 Token 配额 |
| `POST` | `/admin/user/{id}/max_file_size` | 设置最大文件大小 |
| `POST` | `/admin/user/{id}/reset_password` | 强制重置密码 |
| `POST` | `/admin/invite/generate` | 生成邀请码 |

---

## 四、数据库 Schema

### 表结构关系

```
users              ← 用户账户（含配额）
  ↓ 1:N
sessions           ← 对话 Session（含人格）
  ↓ 1:N             ↓ 1:N             ↓ 1:N              ↓ 1:1
messages           knowledge_base     upload_files        writing_tasks
（消息 + token      （RAG 知识库       （文件上传状态        （写作任务设置
 统计 + 向量索引）    向量分块）          + 处理进度）           + session 绑定）
                                                              ↓ 1:N              ↓ 1:N            ↓ 1:N
                                                          writing_contents  writing_sections  writing_evaluations
                                                          （版本化写作内容   （TOC 分段，      （质量评估记录：
                                                           最多保留 3 版）    独立生成/状态）    阅读+风格评分）

users              ← （另一条线）用户账户
  ↓ 1:N
drawing_styles     ← 作图风格（name + prompt_ids + skill_package_id）
  ↓ 1:N
drawing_generations ← 生成记录（本地磁盘 + DB 路径）

drawing_prompts    ← 共享 Prompt 库（不分用户，独立表，供所有 drawing_styles 勾选引用）

invite_codes       ← 邀请码（独立表）
```

### `users`

```sql
id              SERIAL PRIMARY KEY
username        TEXT UNIQUE NOT NULL
password_hash   TEXT NOT NULL
is_admin        BOOLEAN DEFAULT FALSE
can_write       BOOLEAN NOT NULL DEFAULT FALSE  -- 写作模块访问权限（管理员在 /admin/users 授予）
can_draw        BOOLEAN NOT NULL DEFAULT FALSE  -- 作图模块访问权限（管理员在 /admin/users 授予）
max_daily_tokens  INTEGER DEFAULT 100000   -- 0 = 不限
max_file_size_mb  INTEGER DEFAULT 10       -- 0 = 不限
created_at      TIMESTAMP DEFAULT NOW()
```

### `invite_codes`

```sql
code        UUID PRIMARY KEY
used_by     TEXT              -- NULL = 未使用
created_at  TIMESTAMP DEFAULT NOW()
used_at     TIMESTAMP
```

### `sessions`

```sql
id                        UUID PRIMARY KEY
user_id                   INTEGER REFERENCES users(id) ON DELETE CASCADE
name                      TEXT          -- NULL = 未命名（null session）
persona                   TEXT          -- 已废弃
system_instruction_origin TEXT          -- 用户原始人格输入
system_instruction        TEXT          -- AI 处理后的系统指令
is_writing_session        BOOLEAN DEFAULT FALSE  -- 写作模块专属 Session，不出现在对话列表
created_at                TIMESTAMP DEFAULT NOW()
```

索引：`idx_sessions_user_id` on `user_id`

### `messages`

```sql
id          SERIAL PRIMARY KEY
session_id  UUID
role        TEXT              -- 'user' | 'assistant'
content     TEXT
tokens_in   INTEGER DEFAULT 0
tokens_out  INTEGER DEFAULT 0
tokens_total INTEGER DEFAULT 0
embedding   vector(768)       -- 用于历史语义检索
created_at  TIMESTAMP DEFAULT NOW()
```

索引：
- `idx_messages_session_id` on `session_id`
- `idx_messages_embedding`（HNSW，余弦距离）

### `upload_files`

```sql
id                SERIAL PRIMARY KEY
session_id        UUID
filename          TEXT
filepath          TEXT    -- static/loads/{username}/{session_id}/{filename}
status            TEXT DEFAULT 'pending'   -- pending|processing|done|failed
total_chunks      INTEGER DEFAULT 0
processed_chunks  INTEGER DEFAULT 0
error_msg         TEXT
created_at        TIMESTAMP DEFAULT NOW()
```

### `knowledge_base`

```sql
id               SERIAL PRIMARY KEY
session_id       UUID REFERENCES sessions(id) ON DELETE CASCADE
content          TEXT    -- 加上下文头的富化文本（用于向量化）
original_content TEXT    -- 原始分块文本
source_file      TEXT    -- 文件名 或 "对话摘要"
chunk_index      INTEGER DEFAULT 0
embedding        vector(768)
```

索引：
- `idx_knowledge_base_session_id` on `session_id`
- `idx_knowledge_base_hnsw`（HNSW，cosine_ops）

### `writing_tasks`（写作模块）

```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE
session_id      UUID REFERENCES sessions(id) ON DELETE CASCADE  -- 写作专属 Session
title           TEXT DEFAULT '未命名写作'
word_count      INTEGER DEFAULT 0         -- 0 = 不限
style_req       TEXT DEFAULT ''           -- 风格要求（原始文字描述）
content_req     TEXT DEFAULT ''           -- 内容要求
outline         TEXT DEFAULT ''           -- 内容大纲；writing_sections 是权威源后，这是从各段
                                           -- heading+sub_outline 派生拼接、写回的产物（见下）
outline_updated_at TIMESTAMPTZ            -- 大纲最近更新时间（驱动前端"过期"标记）
toc             TEXT DEFAULT ''           -- 写作目录；同上，派生自各段 heading，不再是独立权威数据
toc_updated_at  TIMESTAMPTZ               -- TOC 最近更新时间
style_skills    TEXT DEFAULT ''           -- AI 蒸馏出的结构化「风格技能手册」，生成时优先于 style_req
style_skills_updated_at TIMESTAMPTZ
style_source_text TEXT DEFAULT ''         -- 风格参考原文（来自 generate_style 的 URL/文件抓取），供蒸馏使用
reference_files TEXT[] DEFAULT '{}'       -- 参考资料文件名列表（RAG 来源过滤）
created_at      TIMESTAMP DEFAULT NOW()
```

索引：`idx_writing_tasks_user_id` on `user_id`

### `writing_contents`（写作模块）

```sql
id          SERIAL PRIMARY KEY
task_id     UUID REFERENCES writing_tasks(id) ON DELETE CASCADE
content     TEXT
version     INTEGER DEFAULT 1
created_at  TIMESTAMP DEFAULT NOW()
```

每个 task 最多保留最近 3 个版本；`save_writing_content` 在版本数超 3 时删除最旧版本（`ORDER BY version ASC LIMIT 1`）。`get_writing_content` 返回最新版本（`ORDER BY version DESC LIMIT 1`）。

### `writing_sections`（分段写作，写作模块）

```sql
id                UUID PRIMARY KEY DEFAULT gen_random_uuid()
task_id           UUID NOT NULL REFERENCES writing_tasks(id) ON DELETE CASCADE
section_index     INTEGER NOT NULL DEFAULT 0   -- 章节顺序
heading           TEXT NOT NULL DEFAULT ''     -- 章节标题（来自 TOC）
sub_outline       TEXT DEFAULT ''              -- 该章节的详细大纲片段
content           TEXT DEFAULT ''              -- 该章节正文
word_count_target INTEGER DEFAULT 0            -- 目标字数（0 = 按任务总字数/章节数均分）
status            TEXT NOT NULL DEFAULT 'pending'  -- pending | draft | confirmed
last_generated_at TIMESTAMPTZ
created_at        TIMESTAMPTZ DEFAULT NOW()
updated_at        TIMESTAMPTZ DEFAULT NOW()
```

索引：`idx_writing_sections_task_id` on `(task_id, section_index)`

**`writing_sections` 是唯一权威源，`writing_tasks.outline`/`toc` 是它的派生视图**——两个文本字段物理存在、可直接读取展示，但内容永远是 `sync_task_derived_texts()`（`backend/db.py`）按 `section_index` 顺序把各段重新拼接、写回的产物（`toc` 固定 `## {heading}` 每行一条；`outline` 固定 `## {heading}\n{sub_outline}`，段落间空行分隔），不是各自独立维护的自由文本。任何一次改变了 heading/sub_outline/顺序/归档状态的操作之后都会调它一次。详见十一.4.1「目录/大纲/段落三方同步」。`get_writing_sections()` 统一过滤掉 `archived` 行，所以段落列表、单段生成的衔接上下文、字数目标计算都不会看到已归档的段落。单段生成携带上一个已生成段落的**完整**正文做衔接提示（不再截断摘要，见十一.4.2）；`GET .../full_content` 拼接所有 `draft`/`confirmed` 段为完整正文（未生成的段落标题收集进 `skipped_headings` 一并返回），供"分段视图 ↔ 全文视图"切换。

### `writing_evaluations`（多 Agent 质量评估，写作模块）

```sql
id                 UUID PRIMARY KEY DEFAULT gen_random_uuid()
task_id            UUID NOT NULL REFERENCES writing_tasks(id) ON DELETE CASCADE
readability_score  INTEGER DEFAULT 0     -- 阅读检查 Agent 评分（0-100）
readability_report TEXT DEFAULT ''
style_score        INTEGER DEFAULT 0     -- 风格比对 Agent 评分（0-100，无风格参考时为 0）
style_report       TEXT DEFAULT ''
overall_score      INTEGER DEFAULT 0     -- 有风格参考：两项均分；否则等于 readability_score
created_at         TIMESTAMPTZ DEFAULT NOW()
```

索引：`idx_writing_evaluations_task_id` on `(task_id, created_at DESC)`

详见十一.4「多 Agent 质量评估 Pipeline」。

### `drawing_prompts`（作图模块）

共享的 Prompt 库（原名 `drawing_skills`，已合并"风格私有 prompt"与"共享 skill 片段库"两个概念，详见十五.2），不分用户，供所有 `drawing_styles` 勾选引用。

```sql
id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
name        TEXT NOT NULL
content     TEXT NOT NULL DEFAULT ''   -- 拼入最终 prompt 的文字（原列名 snippet）
created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
created_at  TIMESTAMPTZ DEFAULT NOW()
```

### `drawing_styles`（作图模块）

对应作图页面侧栏每个 tab。

```sql
id                UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
name              TEXT NOT NULL DEFAULT '未命名风格'
prompt_ids        JSONB NOT NULL DEFAULT '[]' -- 勾选嵌入的 drawing_prompts.id 列表（原列名 skill_ids；原有的自由文本 prompt 列已删除，并入 Prompt 库）
skill_package_id  UUID REFERENCES drawing_skill_packages(id) ON DELETE SET NULL -- 绑定的第三方 skill 包，最多一个
created_at        TIMESTAMPTZ DEFAULT NOW()
updated_at        TIMESTAMPTZ DEFAULT NOW()
```

索引：`idx_drawing_styles_user_id` on `user_id`

### `drawing_generations`（作图模块）

每次生成记录，图片走本地磁盘存储、DB 记录相对路径（与 `upload_files` 的 `static/loads/...` 约定一致）。

```sql
id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
style_id     UUID NOT NULL REFERENCES drawing_styles(id) ON DELETE CASCADE
user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
user_input   TEXT NOT NULL DEFAULT ''   -- 用户本次输入的文字
full_prompt  TEXT NOT NULL DEFAULT ''   -- 风格prompt + 勾选skill片段 + user_input 拼接后实际发送的内容
image_path   TEXT DEFAULT ''            -- static/images/{username}/{style_id}/{id}.png
model        TEXT DEFAULT ''
status       TEXT NOT NULL DEFAULT 'pending'  -- pending | done | failed
error_msg    TEXT DEFAULT ''
created_at   TIMESTAMPTZ DEFAULT NOW()
```

索引：`idx_drawing_generations_style_id` on `(style_id, created_at DESC)`

删除风格或删除单条生成记录时，路由层（`drawing.py`）会在 DB 行删除后尽力同步删除磁盘上的图片文件（`_delete_image_file`，失败仅记 warning，不中断请求）。详见十五「作图模块」。

### `prompt_versions`（Phase 3a）

版本化的 system prompt 存储，支持 Agent B 自动改 + 手动回滚。

```sql
id           SERIAL PRIMARY KEY
name         TEXT NOT NULL                    -- e.g. "agent_tool_rules"
content      TEXT NOT NULL
version      INTEGER NOT NULL
is_active    BOOLEAN DEFAULT FALSE             -- 同 name 下仅一行为 TRUE
created_at   TIMESTAMP DEFAULT NOW()
created_by   TEXT DEFAULT 'manual'             -- "manual" | "bootstrap" | "agent_b"
reason       TEXT
UNIQUE(name, version)
```

索引：`idx_prompt_versions_active`（partial，only `is_active = TRUE`）

### `agent_traces`（Phase 3a）

每次 `/chat` 完成后异步落盘的完整调用 trace。Agent B 据此分析。

```sql
id                  SERIAL PRIMARY KEY
session_id          UUID
user_id             INTEGER REFERENCES users(id)
message_id          INTEGER REFERENCES messages(id)
query               TEXT
route               TEXT                       -- "rag" | "agent" | "full_context" | "empty_kb"
tools_called        JSONB                      -- [{round, tool, args, result_preview}, ...]
iterations          INTEGER DEFAULT 1
citations           JSONB
tokens_in           INTEGER
tokens_out          INTEGER
duration_ms         INTEGER
prompt_version_id   INTEGER REFERENCES prompt_versions(id)
hallucination_rate  FLOAT                       -- NULL until verified by Agent B
analyzed_at         TIMESTAMP                   -- NULL = 尚未被 Agent B 分析
created_at          TIMESTAMP DEFAULT NOW()
```

索引：
- `idx_agent_traces_pending`（partial，`analyzed_at IS NULL`）—— Agent B 拉新数据用
- `idx_agent_traces_route` on `(route, created_at DESC)`

### `subsystem_status`（Phase 3a）

机器人 / Agent B / Agent C 的启停状态 + 心跳。

```sql
component       TEXT PRIMARY KEY                -- "bot" | "agent_b" | "agent_c"
enabled         BOOLEAN DEFAULT FALSE
last_heartbeat  TIMESTAMP
last_action     TEXT
status_msg      TEXT
updated_at      TIMESTAMP DEFAULT NOW()
```

---

## 五、核心处理流程

### 5.1 聊天请求（`POST /chat`）

```
1. 验证 JWT Cookie → 获取用户信息
2. 检查今日 Token 配额（超限返回 429）
3. 保存用户消息到 messages 表
4. 估算 session 总语料 token 数（启发式：len(text)/2.5）+ 路由决策：
   ├── < FULL_CONTEXT_THRESHOLD (默认 300_000) → 全量上下文路径（5a）
   ├── ≥ THRESHOLD + AGENT_CHAT_ENABLED + needs_agent(query) → Agent 路径（5c）
   └── 其余 → RAG 路径（5b，含空知识库）

   ┌─ 5a 全量上下文（小语料）：
   │    ├── get_all_session_chunks(session_id) 拉取全部 chunk
   │    ├── 按 (source_file, chunk_index) 排序，加文件头分组
   │    └── Prompt 段落："All uploaded documents (full content)"
   │
   ├─ 5b RAG（大语料 / 简单查询 / 空知识库）：
   │    ├── 并发：Gemini Query Embedding + Google CSE 网页抓取
   │    ├── 检测回忆触发词 → 语义检索历史消息（query_history）
   │    ├── pgvector 相似度检索 knowledge_base（< 0.40，最多 20 条）
   │    └── 动态 Top-K 选择（Margin + Gap 策略）
   │
   └─ 5c Agent ReAct 循环（大语料 + 复杂查询）：
        ├── 预抓 web_info，作为 Agent 免费上下文
        ├── 调用 run_agent_chat()，最多 AGENT_MAX_ITERATIONS=6 轮
        │    每轮：Gemini 决定调哪些 tool（search_kb / read_document /
        │           list_documents / web_search / search_history）
        │           → asyncio.gather 并行执行 → 喂回结果
        │    提前退出条件：search_kb 命中 distance<0.3 时 prompt 鼓励直接作答
        └── 累计 token + citations + agent_trace 一并返回

5. 拼装 Prompt（5a/5b 路径）：[最近 12 轮] + [历史相关] + [文档段] + [网络信息]
6. 调用 Gemini（附 Google Search grounding + 角色人格）
7. 保存 AI 回复 + Token 计数到 messages 表
8. 后台任务：计算回复 Embedding，写回 messages.embedding
```

> **路径选择日志**：每次 /chat 都会输出 `tokens≈N threshold=M → FULL_CONTEXT|AGENT|RAG|EMPTY_KB`，便于观察实际触发情况。

### 5.1.1 Agent 智能路由（`needs_agent` 启发式）

只有以下任一信号触发时才进 Agent，其余复用 RAG 路径以保延迟：

- **对比类**：包含"对比 / 区别 / 比较 / vs / 差异"
- **回忆类**：匹配"还记得 / 之前 / 上次 / 我们聊过 / 你说过"等
- **开放类**：包含"分析 / 总结 / 概括 / 评价 / 怎么看 / 为什么 / 原因"
- **列举类**：包含"有哪些 / 都有什么 / 列出"
- **多问句**：句中包含 ≥ 2 个问号

且 query 长度 ≥ 15 字符（短问题大概率单次 RAG 够）。

实测预期：60-70% 大语料 query 仍走 RAG 路径，延迟与 Phase 1 一致。

### 5.2 文件上传与 RAG 索引

```
1. POST /upload/ → 校验 Session 归属 + 文件大小配额
2. 保存至 static/loads/{username}/{session_id}/
3. 写入 upload_files（status=pending）→ 立即返回 202
4. 后台任务 process_file_and_insert()：
   ├── PDF:  pdfplumber 提取文本；若质量不足则 OCR（pytesseract，中英文）
   ├── EPUB: ebooklib 解析 HTML → Markdown
   └── TXT/DOCX/DOC: 直接读取
5. 文本分块（按 ## 标题或段落，最大 800 字/块）
6. 为每块添加上下文头："[来源：xxx.pdf。开头：...。位置：第N段/共M段]"
7. 批量 Embedding（`backend/rag.py:get_embeddings_batch`，每批 50 条顺序调用）：
   ├── 429/RESOURCE_EXHAUSTED（限流）→ 指数退避重试：30s→60s→120s→...
   ├── httpx.TransportError / asyncio.TimeoutError（网络层瞬断或单批调用超过 90s 无响应）
   │    → 指数退避重试：5s→10s→20s→...（genai.Client 未配置 HTTP 超时，这层 wait_for
   │      超时兜底是唯一防线，否则单次挂起会让整批处理无限期卡在 processing）
   └── 重试 6 次仍失败 → raise，外层 except 写回 upload_files.status=failed + error_msg
8. 批量插入 knowledge_base（含 pgvector 向量）
9. 更新 upload_files.status → done
```

> **卡死兜底**：`process_file_and_insert` 跑在 FastAPI `BackgroundTasks` 里，和处理它的 worker 进程绑定——若 worker 在处理途中被重启/杀掉（部署、OOM 等），后台任务直接消失，DB 行永远停在 `processing`，没有任何代码能替它写回失败状态。`backend/db.py:fail_stale_processing_files()` 在 `main.py` 的 `startup` 事件里跑一次，把上一个进程遗留的、超过 30 分钟仍是 `processing` 的行统一标记为 `failed`（带用户可读的 `error_msg`），前端轮询到 `failed` 会展示"❌ 失败，点击重试"，不会再无限期停在"解析中"。阈值用 30 分钟而非无条件清空，是为了不误伤多 worker 滚动重启时其它 worker 正在合法处理的文件。

### 5.3 认证流程

```
注册：邀请码验证 → bcrypt 哈希密码 → 写 users → 标记邀请码已用 → 签发 JWT
登录：查 users → bcrypt 验证 → 签发 JWT → 写 HttpOnly Cookie（12 小时有效）
请求：读 Cookie → 解析 JWT → 查 users → 注入 user 依赖（含配额信息）
```

Cookie 安全属性：`httponly=True`，`secure=True`，`samesite="lax"`

---

## 六、RAG 检索细节

### 向量索引

- 类型：pgvector HNSW 索引
- 距离度量：余弦距离（`<=>` 操作符）
- `hnsw_ef_search`：100（查询时 HNSW 参数，越大越准但越慢）

### 动态 Top-K 选择算法（`rag.py:13-34`）

从最多 20 个候选（距离 < 0.40）中动态确定返回数量：

- **Margin 策略**：返回所有距离在最优值 + 0.07 范围内的结果
- **Gap 策略**：从第 4 条开始，找到距离突变最大的位置做截断
- 最终取两种策略的最大值，结果数量范围 `[4, 20]`

### 历史语义检索（`rag.py:73-104`）

- 触发条件：消息匹配"你还记得/上次/之前/我们聊过"等正则模式
- 检索范围：本 Session 内全部 assistant 消息的 embedding
- 阈值：0.55（比 RAG 更宽松）
- 排除最近上下文窗口内的消息（避免重复）

---

## 七、文档解析与分块

### 支持格式

| 格式 | 解析方式 |
|---|---|
| PDF | pdfplumber 文本提取；质量不足时 pytesseract OCR（中英文，150 DPI） |
| EPUB | ebooklib 解析 HTML，提取段落转 Markdown |
| DOCX | python-docx 逐段提取 |
| DOC | docx2txt 通过临时文件转换 |
| TXT | UTF-8 直接读取 |

### 分块策略

- **PDF/EPUB**：按 `## 标题` 分段，单段超 800 字则按段落再分；最终按 800 字/块合并
- **TXT/DOCX**：按段落分割，再合并至最大 800 字/块

### 上下文富化

每块向量化前追加头部信息：

```
[来源文件：{filename}。文档开头：{前300字}。位置：第{i+1}段，共{total}段。]

{原始分块内容}
```

---

## 八、管理员功能

`/admin/` 是板块选择页，分两个板块：

- **`/admin/users` 用户管理**
  - 展示**所有用户**（含管理员），管理员用户显示 `管理员` 徽章
  - 查看 Token 用量统计（今日 / 累计）、文件大小上限列，支持在列表页直接编辑
  - 调整每日 Token 配额和文件大小限制、强制重置密码
  - **在 UI 中将任意用户提升为管理员**（`POST /admin/user/{id}/set_admin`）
  - 邀请码：生成新邀请码（UUID 格式）、查看使用状态
- **`/admin/perf` 性能调优**（Phase 3a 上线）
  - 子系统状态：bot / agent_b / agent_c 的启停 + 心跳
  - 近期 trace 摘要（最新 50 条 `/chat` 调用，含路径、轮数、耗时、tokens）
  - Prompt 版本历史（含 active 标记、创建者、变更原因）
  - **Phase 3b/3c 上线后**：bot 启停、Agent B 分析记录、prompt 回滚按钮等
- **Session 审查**：查看任意用户的对话内容、Token 明细、文件处理状态
- **运维脚本**（`scripts/`）：

| 脚本 | 用途 |
|---|---|
| `create_admin.py` | 创建第一个管理员账户 |
| `update_admin.py` | 提升/撤销管理员权限 |
| `generate_invite.py` | 生成邀请码 |
| `clear_failed_uploads.py` | 清理失败的上传记录 |
| `clear_knowledge_base.py` | 清空指定 Session 的 RAG 向量 |
| `check_file_status.py` | 按文件名模糊查询 `upload_files` 完整状态（`show_file_errors.py` 只看 failed/pending，这个能看 processing/done 全部状态，排查卡死问题用） |
| `show_file_errors.py` | 查看文件处理错误 |
| `reset_stuck_processing.py` | 重置卡住的处理任务 |
| `migrate.py` | 执行数据库迁移 |
| `list_models.py` | 测试 Gemini 可用模型 |

---

## 九、关键配置项（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL 连接串（`postgresql+asyncpg://...`） |
| `GEMINI_API_KEY` | — | Gemini API 密钥 |
| `GOOGLE_API_KEY` | — | Google Custom Search API 密钥 |
| `GOOGLE_CX` | — | 自定义搜索引擎 ID |
| `SECRET_KEY` | — | JWT 签名密钥（≥32 字符） |
| `GEMINI_TEXT_MODEL` | `gemini-2.5-flash` | 生成模型 |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-exp-03-07` | 嵌入模型 |
| `EMBEDDING_DIM` | `768` | 向量维度 |
| `RAG_DISTANCE_THRESHOLD` | `0.40` | RAG 余弦距离阈值 |
| `TOP_K` | `4` | RAG 最少返回条数 |
| `TOP_K_MAX` | `20` | RAG 最多候选条数 |
| `TOP_K_MARGIN` | `0.07` | Margin 策略容差 |
| `TOP_K_GAP` | `0.05` | Gap 策略突变阈值 |
| `HNSW_EF_SEARCH` | `100` | HNSW 查询精度参数 |
| `MAX_HISTORY_TURNS` | `12` | Prompt 中携带的历史轮数 |
| `FULL_CONTEXT_THRESHOLD` | `300000` | session 总语料 token 数低于此阈值时走全量上下文路径（跳过 RAG 检索） |
| `AGENT_CHAT_ENABLED` | `true` | Phase 2 Agent 循环开关，仅在大语料 + 复杂查询时激活 |
| `AGENT_MAX_ITERATIONS` | `6` | Agent 单次对话最多调用工具数（含 LLM 决策轮）|
| `REDIS_URL` | `redis://localhost:6379/0` | Phase 3a Celery broker / backend |
| `BOT_USERNAME` | `机器人` | Phase 3b 机器人账号名 |
| `BOT_SOURCE_USERNAME` | `天书` | Phase 3b session 复刻源（"天书"用户的所有 named session 会被复制给机器人） |
| `BOT_RUN_HOUR` | `3` | Phase 3b 机器人每日跑 query 的小时（0-23） |
| `AGENT_B_MODEL` | `gemini-2.5-pro` | Phase 3c Agent B 用的模型（推理质量优先） |
| `AGENT_B_RUN_HOURS` | `12` | Phase 3c Agent B 触发周期（小时） |
| `AGENT_B_BATCH_SIZE` | `20` | Phase 3c 单次分析的 trace 数 |
| `AGENT_B_MIN_TRACES` | `5` | Phase 3c 不足此数时跳过本轮 |
| `AGENT_C_RUN_HOURS` | `4` | Phase 3d Agent C 验证周期（小时） |
| `AGENT_C_MIN_TRACES` | `5` | Phase 3d 每个版本至少这么多 trace 才比较 |
| `HTTP_PROXY` | — | 可选 HTTP 代理 |
| `ANTHROPIC_API_KEY` | — | Claude API 密钥（仅 `agent_system/` 子系统使用） |
| `CODEX_API_KEY` | — | 写作模块 Markdown 排版首选后端 + 作图模块图像生成共用（OpenAI 兼容端点密钥）；写作场景未配置则直接跳过，回退 Gemini |
| `CODEX_BASE_URL` | — | Codex 兼容端点 base URL（写作排版 `/chat/completions`、作图模块 `/images/generations` 共用同一中转） |
| `CODEX_MODEL` | `gpt-4o` | Codex 排版调用的模型名 |
| `CODEX_IMAGE_MODEL` | `gpt-image-1` | 作图模块生成调用的模型名（与 `CODEX_MODEL` 独立配置；中转可能静默替换为其他底层模型，见十五） |
| `CODEX_IMAGE_TIMEOUT` | `300` | 作图 images/generations、images/edits 调用的 httpx read 超时（秒）。中转出图慢，生产的 gunicorn `--timeout` / Nginx `proxy_read_timeout` 需 ≥ 此值，见 `PRODUCTION.md` |

> `agent_system/llm.py` 在导入时自动加载项目根 `.env`（通过 `python-dotenv`），与 `settings.py` 的 Pydantic Settings 加载模式一致。

---

## 十、Session 状态说明

Session 有三种状态：

- **null session**：`name IS NULL`，访问 `/` 时自动创建，用于匿名浏览，不支持 RAG 和文件上传
- **named session**：用户通过 `POST /new_session` 创建，支持 RAG、文件上传、角色人格设置
- **writing session**：`is_writing_session = TRUE`，由写作模块 `create_writing_task()` 自动创建，绑定一个写作任务，**不出现在对话列表**（`GET /sessions` 过滤）

`session_exists()` 通过 `name IS NOT NULL` 判断是否为命名 Session（writing session 有 name，也满足此条件）。

---

## 十一、写作模块（`writing.py` + `templates/writing.html`）

### 概述

写作模块是独立于对话功能的 AI 辅助写作系统，提供结构化的写作任务管理、Markdown 编辑器、TOC/分段生成、风格蒸馏、多 Agent 质量评估，以及实时 AI 对话修改写作内容的能力。**访问受 `users.can_write` 权限门控**：仅 `is_admin` 或 `can_write=TRUE` 的用户可用（`require_write_access` 依赖），管理员在 `/admin/users` 逐用户授予。

### 架构设计

- **写作任务**：每个任务对应一条 `writing_tasks` 记录 + 一个绑定的 `is_writing_session` Session
- **RAG 集成**：写作任务可选择参考文件列表，生成内容时仅从这些文件的知识库 chunk 中检索
- **AI 对话修改**：`/writing/tasks/{id}/chat`（全文级，`writing.py` 独立实现，不复用 `/chat`），默认只讨论/答疑，仅在用户确认后才用 `[WRITING_UPDATE_START]...[WRITING_UPDATE_END]` markers 包裹新内容供前端抽取更新（详见下文"AI 对话面板"）；分段视图下对应 `/sections/{id}/chat`，markers 换成 `[SECTION_UPDATE_START]...[SECTION_UPDATE_END]`
- **内容版本化**：每次保存写作内容都生成新版本，数据库保留最近 3 版
- **两种写作粒度**：全文一次性生成/编辑（`outline` + `writing_contents`），或 TOC 驱动的分段生成/编辑（`toc` + `writing_sections`），两者共享同一份 `writing_tasks` 设置（标题/字数/风格/内容要求）

### 前端布局（`templates/writing.html`）

**页面顶层结构**（镜像 `chat.html`）：`<body>` 在桌面端为 flex 行，由 `style.css` 统一控制。

```
body (flex row, ≥993px)
├── #slide-out        — 移动端滑出侧边栏（桌面隐藏），含 TSAI 品牌 + 新建写作/进入对话按钮
├── #writing-sidebar  — 桌面常驻左侧面板，flex: 0 0 260px（移动端隐藏）
│   ├── .writing-sidebar-brand "TSAI"  (64px，与 nav 高度对齐，视觉交叉于左上角)
│   ├── #writing-list  (写作任务列表，flex-grow: 1)
│   └── .writing-sidebar-actions  (新建写作 pinkish + 进入对话 indigo，居中排列)
└── .writing-shell    — flex: 1，内容区
    ├── <nav>         — 白色 top-nav，结构与 chat.html 完全一致
    └── .writing-layout  (grid: 1fr 300px)
        ├── #writing-main    — Markdown 编辑器（双模式）+ 底部字数统计
        └── #writing-settings — 写作设置面板 + AI 对话面板
```

**响应式断点**：
- `≥993px`：`#writing-sidebar` 显示（260px），`#slide-out` 隐藏
- `≤992px`：`#writing-sidebar` 隐藏，`#slide-out` 可通过汉堡菜单唤出；`#writing-settings` 隐藏；`.writing-layout` 变为单列
- `≤600px`：全单列，`.writing-layout` 高度重算

**modal 样式**：所有 5 个 modal 均有 × 关闭按钮；取消按钮 `waves-red btn-flat`，确定按钮 `waves-green btn-flat`，与 `chat.html` 一致。

| 区域 | ID | 内容 |
|---|---|---|
| 左侧任务列表 | `#writing-sidebar` | TSAI 品牌 + 写作任务列表 + 新建/进入对话按钮 |
| 主窗口 | `#writing-main` | Markdown 编辑器（双模式：预览/编辑）+ 底部字数统计 |
| 右侧面板 | `#writing-settings` | 写作设置（标题/字数/风格/内容/大纲/参考资料）+ AI 对话面板 |

**双模式编辑器**：
- 预览模式（`#content-preview`）：`marked.js` 渲染 Markdown + `DOMPurify` XSS 防护；点击进入编辑
- 编辑模式（`#content-textarea`）：原始 Markdown 文本；底部显示放弃/保存按钮
- `enterEditMode()` / `exitEditMode(newContent)` 管理状态切换

**AI 对话面板**（`#ai-chat-panel`）：
- `chatSessionId` 指向当前写作任务的绑定 Session；发送消息期间显示闪烁的 `···`占位气泡（复用主对话模块 `.dots`/`@keyframes blink` 样式），收到回复/出错后移除
- 对话目标随 `viewMode` 实时切换：全文视图 → `POST /tasks/{id}/chat`；分段视图且有段落处于"放大"状态 → `POST /tasks/{id}/sections/{expandedSectionId}/chat`；分段视图但无放大段落 → 提示用户先放大一个段落，不发送请求
- **讨论优先，修改需二次确认**：`writing.py:_chat_system_instruction()`（全文/分段共用同一套规则文本）要求模型默认只回答/讨论（含写作任务之外的一般性问题，通过 Google Search grounding tool 回答），仅当检测到用户明确的修改意图时才在本轮反问确认（"是否需要我将……修改为……？"），必须在**下一轮**用户给出肯定答复（参考 `get_context()` 拉取的最近对话历史判断）后才允许输出 `[WRITING_UPDATE_START]...[WRITING_UPDATE_END]` / `[SECTION_UPDATE_START]...[SECTION_UPDATE_END]` 标记；前端检测到标记才抽取新内容 → 全文走 `exitEditMode()` + 自动保存 + 排版，分段走 `PATCH .../sections/{id}` + `loadSections()` 刷新
- **对话历史持久化**：两个 chat 端点都会把用户提问和"剥离标记后的展示文本"（`_extract_chat_display()`，与前端渲染逻辑保持一致）存入 `messages` 表（`save_message`），刷新页面后 `loadChatHistory()` 从 `GET /messages/{session_id}` 拉取即可看到完整历史；全文与分段对话共享同一条任务 session 的历史，不做范围区分

### 关键实现细节

- **写作 Session 隔离**：`GET /sessions` 增加 `AND (is_writing_session = FALSE OR is_writing_session IS NULL)` 过滤，写作 session 不出现在对话页面
- **SSE 流式输出**：写作模块所有流式端点（`generate_style`/`generate_outline`/`generate_content`/`generate_toc`/`generate_outline_from_toc`/分段 `generate`/`distill_style` 等）均返回 `StreamingResponse(media_type="text/event-stream")`；每个文本块经 `writing.py:_sse_chunk()` JSON 编码后再放入 `data: ...\n\n` 帧（而非裸文本拼接），避免模型输出中的换行符被前端按行解析的 SSE 逻辑误判为帧结束、导致内容截断；前端 `decodeSseData()` 对应解码，结束标志仍是 `data: [DONE]\n\n`
- **参考资料 RAG 过滤**：`query_rag()` 支持 `source_files` 参数，只从指定文件的 chunks 中检索
- **内容流式显示**：SSE 流式输出时用 `preview.textContent +=` 追加（安全），流完成后调用 `exitEditMode()` 渲染 Markdown
- **联动同步动作的忙碌遮罩**：确认/取消确认段落、保存大纲/目录（含触发 reconcile）、应用大纲调整建议这几个动作都有明显的后端处理耗时（reconcile 的多次 SQL、`_check_outline_drift` 的一次非流式 Gemini 调用等），且之前点击期间界面无任何反馈、可重复点击。`showBusy()`/`hideBusy()`（`#writing-busy-overlay`，全视口遮罩 + Materialize 小号 spinner）包在共用的底层函数 `finishSettingsPatch`/`applyOutlineReview`/`confirmSection` 里，而不是分散在各个按钮的 click handler——所有调用方（弹窗内"确定"、外层"保存设置"、`#modal-reconcile-confirm` 的"确定继续"、大纲建议的两个应用按钮）自动获得一致的遮罩行为

### 十一.4 TOC / 分段写作系统

**目的**：长文章（或需要精细控制每个章节）时，绕开"一次性生成全文"的模式，改为目录驱动的逐段生成/编辑/确认。

**流程**：

```
1. 生成/编写 TOC（POST generate_toc，SSE；或手动在弹窗里编辑）
   ├─ 已有详细大纲 → 从大纲提炼 5-10 条 "## 标题"
   └─ 无大纲 → 直接从任务标题/字数/内容要求生成
2. 保存 TOC（PATCH tasks/{id} 传 toc）
   └─ writing.py:_parse_toc 解析标题 → reconcile_writing_sections 两阶段对齐同步 writing_sections
      （精确文本匹配 + 剩余项按相似度配对识别"改名"，见十一.4.1；顺序调整/新增/删除都处理，
       消失的标题归档而非删除，防止已生成内容被清空；标题复现则复活归档行）
3.（可选）generate_outline_from_toc：由 TOC 反推逐章详细大纲，回填 outline 字段
4. 每个 section 卡片独立操作：
   ├─ generate：SSE 生成本段内容，携带"上一个已生成段落结尾 800 字"做衔接，完成后 status→draft
   ├─ format：Codex→Gemini 排版（同全文排版逻辑）
   ├─ chat：段落级 AI 对话修改（[SECTION_UPDATE_START]标记，规则见上文"AI 对话面板"的"讨论优先，修改需二次确认"）
   ├─ 放大/缩小：内容预览右下角（与"目标字数"同一行）图标，`max-height` 在默认 220px 与不限之间切换；
   │  同一时刻只允许一个段落处于放大状态，`expandedSectionId` 跨 `loadSections()` 重渲染保持
   ├─ 编辑（手动编辑浮窗）：仅当该段已放大且非 confirmed 时可点；`sectionDraftEdit = {sectionId, draftContent}`
   │  为纯前端草稿（不落库），在独立于 `#sections-container` 的 `#section-edit-portal` 浮层中渲染成一个
   │  textarea——`#sections-container` 收窄为 `min(820px, 50%-10px)`（`.split-active`），浮窗按原卡片
   │  `getBoundingClientRect()` 动态定位在其右侧，两者作为整体在 `#content-area` 内居中
   │  （`positionSectionEditPortal()`）；textarea 内容随输入实时同步回 `draftContent`，只有点击
   │  "替换原文"才 PATCH 持久化（正文首行若是 `## 新标题` 会自动同步为本段标题，见十一.4.1），
   │  "取消替换"则整个丢弃
   └─ 手动切换 status：pending → draft → confirmed
5. full_content：拼接所有 draft/confirmed 段落 → 完整正文（跳过的标题列入 skipped_headings）；
   前端"合并为全文"点击时若已有全文内容会先 `confirm()` 提示"将完全替换、不可撤销"，确认后保存并自动触发排版+质量评估
```

#### 十一.4.1 目录 / 大纲 / 段落三方同步

**权威源是 `writing_sections`，`outline`/`toc` 是派生视图，不是三份各自独立的数据**。段落标题、目录条目、大纲分段互为同一份数据的三种呈现：改段落正文首行标题 → 同步目录/大纲；改目录（文字/顺序/增删）→ 同步段落标题/顺序/新建/归档，且大纲对应行也跟着更新；改大纲（保存时按 `## ` 切分成逐段）→ 段落的 `heading`/`sub_outline` 同步更新，目录也跟着重排。三个入口最终都落到同一套函数：

- `backend/db.py:_align_headings(pool, new_headings)`：纯函数，两阶段标题对齐算法。
  - **阶段一·精确文本匹配（与位置无关，含 archived 行）**：FIFO 队列按标题文本分桶（活跃行优先入队，archived 行排后面，同名时优先复活/复用"还活着"的那行），按新标题顺序逐个消费——这一步就是"任意重排序/新增/删除都不误伤不变标题"的核心能力。**不能直接用位置对齐的 LCS**：`[A,B,C]` 纯重排成 `[C,A,B]`，标准 LCS（下标必须单调递增）只能找到 `[A,B]`，会把纯重排误判成删除+新增。
  - **阶段二·剩余项按原相对顺序配对为"改名"**：阶段一消费剩下的旧标题（仅限未 archived）和新标题，按各自相对顺序一一配对，`difflib.SequenceMatcher(...).ratio() >= 0.3` 判定为改名（heading 更新，content/status 原样保留），阈值以下当成两个独立事件（删除+新增）——加这道相似度闸门是为了防止把内容完全无关的新标题错配到旧段落上、静默"继承"旧段落已经写好的正文和 confirmed 状态，比误判成删除更隐蔽。
- `backend/db.py:reconcile_writing_sections(task_id, new_headings, new_sub_outlines, confirm=False)`：目录/大纲保存的统一协调入口（取代旧的 `upsert_writing_sections`）。`new_sub_outlines=None` 表示只保存目录（不动 `sub_outline`）；传入列表表示保存大纲，连细纲一起同步。先只读比对：`deleted` 集合里 `status != 'pending'` 或 `content` 非空的行数（`risky_archive_count`）**超过 3** 时，若调用方未带 `confirm=True`，直接返回 `{needs_confirm:true, ...预览}` 不写库；否则真正执行 UPDATE/INSERT/归档，调用 `sync_task_derived_texts` 写回派生文本。改名/纯新增不计入风险计数——改名内容会保留、新增不会丢东西，只有真会让已有内容"消失"的删除才拦。和库内现状完全等价（无改名/归档/新建、顺序不变、sub_outline 不变）时直接跳过写库，避免误刷 `outline_updated_at`/`toc_updated_at` 触发所有段落的"过期"徽章。
- `backend/db.py:sync_task_derived_texts(task_id)`：按 `section_index` 顺序读 `get_writing_sections`（已过滤 archived），拼出规范化 `toc`（`## {heading}` 每行一条）和 `outline`（`## {heading}\n{sub_outline}`，段落间空行），写回 `writing_tasks` 并刷新两个 `_updated_at` 时间戳。任何一次改变 heading/sub_outline/顺序/归档状态的操作后都要调它一次。
  - **踩过的坑（真实事故）**：`sub_outline` 这一列在本方案上线前从来没被写入过（旧的 `upsert_writing_sections` 永远传空字符串），所以任何一个"还没做过一次大纲保存"的老任务，它名下所有段落的 `sub_outline` 全是空的——即便 `writing_tasks.outline` 里躺着一大段用户手写/AI 生成的详细大纲。`sync_task_derived_texts` 一旦在这种任务上被调用（哪怕只是保存目录、或单段改名），会把 `outline` 现场重建成"只剩标题、正文全部消失"。修复：`backend/db.py:backfill_missing_sub_outlines(task_id)` 在任何会触发 `sync_task_derived_texts` 的操作之前，先按标题把 `writing_tasks.outline` 里躺着的历史内容回填进对应段落的空 `sub_outline`（幂等，只填空的，不覆盖已有内容）——`reconcile_writing_sections` 一开始就调用它；`patch_section` 的改名分支必须在**改 heading 之前**调用（回填是按当前/旧标题去匹配现有 outline 文本，heading 一旦先改掉，那一行就再也找不到自己原来的内容了）。
- `writing.py:patch_task`：`outline`/`toc` 都出现在同一次 PATCH（比如"保存设置"一次性提交了都改过的两者）时，**以 outline 为准**（信息量更全，同时带 sub_outline），toc 原始文本不再单独处理。若解析不出任何 `## ` 标题（用户还在写没分章节的草稿），走"引导阶段"例外：原样存文本，不触发 reconcile，避免把随手写的笔记误判成"清空所有章节"。
- `writing.py:patch_section`：只传 `content` 且未显式传 `heading` 时，正则匹配正文首行 `^##\s+(.+?)\s*$`，若解析出的标题和当前 `heading` 不同就一并更新（响应带 `heading_synced`），并调用一次 `sync_task_derived_texts`——单段改名是无歧义的 1:1 关系，不跑对齐算法、不触发确认阈值。
  - **踩过的坑（改名把自己标成"过期"）**：`update_writing_section` 保存 content/heading 时会把这一段的 `last_generated_at` 打成 `NOW()`；紧接着 `sync_task_derived_texts` 又把 `outline_updated_at`/`toc_updated_at` 打成另一个 `NOW()`——两条先后执行的 SQL，第二个时间戳必然比第一个晚。前端 `isSectionStale()`（下方"过期"徽章）判断依据正是"大纲更新时间 > 本段最后生成时间"，于是刚改完标题的这一段会被自己这次编辑误标成"过期"。修复：`backend/db.py:touch_section_generated_at(section_id, task_id)` 在 `sync_task_derived_texts` 之后再把这一段的 `last_generated_at` 重新打一次 `NOW()`，确保它不早于刚刚一起更新的 outline/toc 时间戳。
- 前端 `templates/writing.html`：`#modal-reconcile-confirm` 弹窗（仿 `#modal-del-writing` 样式）展示"识别为改名/将被归档/新增空段落"三组明细；`patchTaskWithReconcile()` 统一处理 PATCH 响应里的 `needs_confirm`（暂存 payload 到 `pendingReconcilePatch`、开弹窗），`btn-confirm-reconcile` 带上 `confirm_reconcile:true` 重新提交；`finishSettingsPatch()` 是 `btn-save-toc`/`btn-save-settings`/确认按钮共用的收尾（把 reconcile 返回的规范化 `outline`/`toc` 一起写回 `localSettings`/`savedSettings`——哪怕只编辑了其中一个，另一个也可能因顺序/改名同步而变化，必须两个都刷新，否则编辑框会显示过期内容、脏检查会误判）。

#### 十一.4.2 段落生成时的大纲漂移检测

**内容以人工编辑为准，大纲是从属描述**：`templates/writing.html` 段落卡片只在 `!hasContent` 时才显示 `sub_outline` 片段——已经有内容（生成过或被人工编辑替换过）的段落，实际内容可能早就偏离了当初的大纲，继续展示这份大纲容易误导，且不强制要求两者保持一致。

**生成时内容优先于大纲**：`generate_section_content`（`writing.py`）衔接上下文从"上一段结尾 800 字"改成上一段**完整**正文（`prev_full`，不截断），prompt 里明确"如果实际内容与大纲描述有出入，以实际内容为准"——大纲只在当前要生成的这一段本身没有内容时才是唯一依据（`sec_outline`），对已经写出来的相邻段落，真实内容才是权威衔接依据。

**已确认段落是更高权重的风格参考**：紧邻的上一段如果 `status == 'confirmed'`，`context_hint` 的措辞会换成"已被作者确认定稿，代表本文目前被认可的文字表达/语气/行文风格，请先仔细阅读这段定稿内容"，比普通草稿的"衔接依据"权重更高（`prev_section` 变量记录了上一段本身，不只是它的正文）。此外，**不限于紧邻段落**：所有其它 `confirmed` 的段落（排除紧邻上一段，避免正文在 prompt 里重复出现）会各自截断到 1500 字拼成一个独立的"作者已确认定稿的其它章节内容"参考块，插在 `_style_block`（风格技能手册/`style_req`）后面——只作风格参考，不要求承接。这几处都只读取 `status`，不修改任何数据，纯粹是 prompt 拼装层面的调整。

**生成完成后的一次性一致性检查**（`_check_outline_drift`）：仅当任务里还有别的段落已经有内容时才触发（第一段生成、没有可比对对象时跳过，省一次调用）。触发点有两处——紧邻 `generate_section_content` 之前（生成完成后），以及 `patch_section` 把 `status` 改成 `confirmed` 时（见下）；两处调的是同一个函数，行为完全一致。额外发起一次非流式 Gemini 调用：给模型看大纲全文 + 每个其它段落的当前状态（有内容的段落给**真实内容**、没内容的段落给它的 `sub_outline`）+ 刚生成的这段内容，要求按严格标签格式（沿用 `_READABILITY_PROMPT_TMPL` 一类的纯文本标签 + 正则提取的项目既有约定，不用 `response_mime_type=json`）判断大纲是否需要调整：
```
需要调整：是/否
本段大纲建议：<...或"无">
整体大纲需要调整：是/否
整体大纲建议：<完整新大纲全文，或"无">
其它段落大纲回填：
- 标题：<原文一字不差> | 新大纲：<...>
```
prompt 明确要求"优先只给本段建议，非必要不提整体建议"、"回填标题必须逐字复制原文，不得意译"。解析失败/判定"否"/调用异常统统返回 `None`——这一步纯属锦上添花，绝不影响本次生成已经成功保存的正文，外层用 `try/except` 包一层。

**信号帧不走文本分片通道**：判定需要调整时，在 SSE 流末尾、`[DONE]` 之前，多 yield 一帧 `data: {"type":"outline_review", ...}\n\n`——注意不经过 `_sse_chunk()`（那个是把字符串套一层 JSON 编码，用于正文分片），这里直接 `json.dumps` 一个**对象**。前端 `generateSection()` 手写的 SSE 读取循环里，`decodeSseData(raw)` 解出来是字符串就走原来的正文累加逻辑，是对象且 `type==='outline_review'` 就单独摘出来、不计入正文字数——两种帧共用同一条 SSE 通道，靠 JS 的 `typeof` 区分，不用引入具名的 SSE `event:` 字段（项目里所有手写读取循环都不解析它）。这个"用 `type` 字段区分帧类型"的约定和 `evaluate_content`/`gen_eval()` 的 `{'type':'stage',...}`/`{'type':'complete',...}` 是同一套。

**应用调整**（`POST .../sections/{section_id}/apply_outline_review`）：`retrofits`（顺带回填的其它段落）无论选哪个 scope 都会先应用，只改那些段落的 `sub_outline`、绝不碰它们的 `content`/`status`。`scope=section` 只更新当前段自己的 `sub_outline` 再调 `sync_task_derived_texts`；`scope=overall` 把模型给的完整新大纲丢给 `_parse_outline_sections` 解析后直接复用整套 `reconcile_writing_sections` 管线（十一.4.1）——不额外跳过它的 `>3` 高风险确认阈值，一次 AI 提议的整体重写不该比人工编辑更值得信任，若触发确认，前端复用同一个 `#modal-reconcile-confirm` 弹窗（`pendingOutlineReviewRetry` 和 `pendingReconcilePatch` 二选一，谁非空就是这次弹窗该重放谁）。整体大纲解析不出任何 `## ` 标题（模型输出格式跑偏）时直接 400，不调用 reconcile——避免把"清空所有段落"这种危险操作当成正常输入执行。

- **踩过的坑（`scope=overall` 漏了 `touch_section_generated_at`，导致"刚生成的这段反被标成过期"）**：`scope=section` 分支应用调整后会调 `touch_section_generated_at(section_id, task_id)`（注释里明确写了原因），但 `scope=overall` 分支只调了 `reconcile_writing_sections`（内部会调 `sync_task_derived_texts` 把 `outline_updated_at`/`toc_updated_at` 打成 `NOW()`）就直接返回，漏了这一步。后果：这次整体大纲重写明明是因为当前这段刚生成/编辑/确认的内容才触发的，`isSectionStale()` 却会把它判成"大纲改了、这段过期了"——对自己触发的变更倒打一耙。修复：`scope=overall` 分支在 `reconcile_writing_sections` 成功后也补一次 `touch_section_generated_at(section_id, task_id)`，只补触发这次重写的当前段，其它被这次重写改了 `sub_outline` 的段落该标"过期"继续标（提醒用户"计划变了，回头看看"是正确信号，不受这次修复影响）。

**AI 面板与"放大段落"联动**：分段视图下，AI 对话面板/质量评估面板作用于当前放大的段落（`expandedSectionId`），而非全文；没有段落处于放大状态时，对话面板发送前提示、评估面板直接跳过不请求。切回全文视图后两者自动恢复为全文目标（判断逻辑在调用时读取 `viewMode`，无需额外状态同步）。"更新内容"按钮（全文一次性重新生成）在分段视图下点击会提示先"合并为全文"，而不是静默按全文大纲重新生成、丢弃分段草稿。

**前端**（`templates/writing.html`）：写作目录弹窗（AI 生成 + "双向生成"——大纲↔TOC 互相推导）；分段卡片视图，含状态徽章、生成/排版/确认/放大操作、大纲变更后的"⚠过期"标记（比较 `outline_updated_at`/`toc_updated_at` 时间戳）；分段/全文视图切换；合并结果的 toast 提示会附上被跳过的章节标题。

**过期检测**：`outline`、`toc` 每次更新都各自打时间戳（`outline_updated_at`/`toc_updated_at`），前端据此判断"大纲改了但 TOC/分段还没同步"，提示用户重新生成。

**忽略过期提示**（`POST .../sections/{section_id}/dismiss_stale`）：`isSectionStale()` 是纯时间戳比较，没有独立的"已忽略"状态，之前唯一能关掉"⚠ 过期"的办法是编辑正文或点"确认"——但很多时候这段内容根本不需要改，点"确认"还会顺带触发一次大纲一致性检查（十一.4.2），检查若建议改大纲，会让其它段落又重新标"过期"，容易形成死循环。`dismiss_stale` 复用 `touch_section_generated_at`（只把这段的 `last_generated_at` 打成 `NOW()`，不碰 content/heading/status，不触发任何大纲同步或一致性检查）。前端点"⚠ 过期"徽章本身即触发（`event.stopPropagation()` 避免连带展开/收起卡片）。

**已知坑：flex 容器内 `<textarea>` 不会自动撑满交叉轴高度**——`.section-edit-window-body`（编辑浮窗内容区）是 `display:flex` 的行容器，早期只给内部 `<textarea>` 设了 `flex:1`（只影响主轴/宽度），导致 `<textarea>` 退回浏览器默认 `rows` 高度（约 45px）而非撑满父容器；`draftContent`/`textarea.value` 数据其实完整，只是超出这 45px 的部分被裁进不可见的 `overflow-y:auto` 滚动区域，看起来像"内容只剩标题一行"。修复：显式给 `.section-edit-window-body textarea` 加 `height: 100%`（`<textarea>` 作为表单控件不会像普通块级元素一样被 `align-items:stretch` 自动拉伸）。

### 十一.5 风格蒸馏 + 多 Agent 质量评估 Pipeline

**目的**：把模糊的"风格要求"文字转成结构化、可执行的规则手册；生成/修改内容后自动跑质量检查，给出可操作的改进建议。

**风格蒸馏**（`POST /distill_style`，SSE）：

- 输入来源二选一或都给：`style_req`（用户文字描述）+ `style_source_text`（`generate_style` 从 URL/上传文档抓取的参考原文，最多 6000 字，随 `task_id` 存入 `writing_tasks.style_source_text`）
- 输出：结构化「风格技能手册」（6 节，每节 2-3 条可操作规则）——语气与腔调 / 句式结构 / 词汇风格 / 叙事节奏 / 过渡与衔接 / 结构模式
- 存入 `writing_tasks.style_skills`；此后 `generate_content` / `generate_section_content` 都优先注入 `style_skills`（而非原始 `style_req`）

**质量评估 Pipeline**（`POST /evaluate`，可选 query 参数 `section_id`，SSE，两个 Agent 串联执行）：

- 不传 `section_id`：评估 `writing_contents` 最新版本全文，结果写入 `writing_evaluations`（`GET .../evaluations/latest` 展示的即此记录）
- 传 `section_id`：改为评估该段落 `writing_sections.content`，**不写入** `writing_evaluations`（该表只承载全文评估历史，避免段落评估污染"最近一次全文评估"的语义）；前端分段视图下点"运行质量评估"时自动带上当前放大段落的 id

```
Stage 1 — 阅读检查 Agent（始终执行）
  评估维度：逻辑连贯性 / 段落长度（150-300字建议）/ 重复表达 / 标题内容一致性 / 整体流畅度
  输出：0-100 评分 + 分段位置具体问题列表 + 1-2 句总结

Stage 2 — 风格比对 Agent（仅当存在风格参考时执行：style_skills / style_req / style_source_text 任一非空）
  参考素材优先级：style_skills > style_req，再叠加 style_source_text 节选 + reference_files 的 RAG 片段
  评估维度（各 0-100，四维平均为总分）：语气腔调匹配度 / 句式结构相似度 / 词汇风格一致性 / 叙事节奏吻合度
  输出：四维分项分析 + 3 条重点改进建议（附原文改法示例）

overall_score = 有风格参考 → (readability + style) // 2；否则 = readability
结果写入 writing_evaluations，前端展示评分卡片 + 展开式报告
```

- SSE 消息为 JSON（非纯文本 delta）：`{"type":"stage","stage":"readability"|"style","status":"running"|"done",...}` → `{"type":"complete","overall_score":N,"has_style":bool}`
- 评分从报告文本用正则提取（如 `评分[：:]\s*(\d+)`），解析失败时回退 70 分、异常时记 0 分并附错误信息
- 内容保存/排版完成后前端自动触发一次评估；任务加载时展示上次评估结果（`GET .../evaluations/latest`）

### 十一.6 Markdown 排版（Codex 优先 + Gemini 回退）

`format_content` / 单段 `format` 均调用 `writing.py:_format_markdown_sync`：

```
1. _split_for_format：优先按 "## " 章节边界切块；单块超 2200 字时在句末（。？！…\n）就近再切
2. 每块 <30 字直接跳过（保留原样，不值得调用模型）
3. 逐块尝试 Codex（OpenAI 兼容端点，CODEX_API_KEY/CODEX_BASE_URL/CODEX_MODEL 未配置则直接跳过）
4. Codex 失败 → 回退项目 Gemini 客户端（同步 generate_content，system_instruction 为排版规则）
5. 两者都失败 → 保留原文块，仅记 warning 日志（不中断整体流程）
6. 各块用 "\n\n" 拼接，覆盖保存为新版本
```

排版规则（`_FORMAT_SYSTEM`）：段落间必须空行、章节标题独占一行前后空行、完整保留原文不增删内容、超 150 字长段按叙事逻辑分段。`asyncio.to_thread` 包装同步调用，避免阻塞事件循环。

---

## 十二、Agent 子系统（`agent_system/`）

**目标**：自动读取、修改、测试、迭代 TSAI 项目代码——完整的 plan→act→observe→reflect→repeat 闭环（Harness 模式），并分阶段输出带时间戳的进度反馈。

使用 Claude API（Anthropic），不依赖也不修改 TSAI 主体业务逻辑以外的部分。

### 结构

```
agent_system/
├── __init__.py
├── main.py          # CLI 入口：--root 指定项目根目录
├── llm.py           # LLM facade：转发到选中的 provider
├── orchestrator.py  # 薄层 Orchestrator（项目上下文 + 异常级回滚 + 摘要 + memory）
├── harness.py       # TaskHarness：plan→act→observe→reflect→repeat 主循环
├── memory.py        # MemoryManager（跨 session JSON 持久化）
├── workspace.py     # Workspace（文件备份 / diff / 回滚）
├── tools.py         # TOOL_DEFINITIONS + ToolExecutor（文件和 Shell 工具）
├── memory.json      # 运行时生成：跨 session 记忆（已加入 .gitignore）
├── requirements.txt # anthropic / google-genai / openai / python-dotenv
├── providers/                      # 多后端 LLM 适配层
│   ├── __init__.py                 # get_provider() 工厂，按 LLM_PROVIDER 选择
│   ├── base.py                     # LLMProvider ABC + LLMError
│   ├── anthropic_provider.py       # Claude（adaptive thinking + cache_control）
│   ├── gemini_provider.py          # Gemini（thinking_config 自动预算）
│   └── openai_provider.py          # OpenAI（gpt-4o，无 thinking）
└── agents/
    ├── coding_agent.py  # ACT：tool-use 循环直接编辑项目文件
    ├── test_agent.py    # OBSERVE：collect_observation（py_compile / pytest / ruff / import）
    ├── review_agent.py  # REVIEW：分析 diff 的安全漏洞和代码质量
    └── reflect_agent.py # REFLECT：聚合 observation+review，产出 Reflection
```

### LLM Provider 切换

通过 `.env` 选择后端，**默认 `gemini`**（性价比最高，且 TSAI 已有 `GEMINI_API_KEY`）：

```bash
# .env
LLM_PROVIDER=gemini        # gemini | anthropic | openai
# LLM_MODEL=               # 可选：覆盖默认模型
GEMINI_API_KEY=...         # 已有
# ANTHROPIC_API_KEY=...    # 切到 anthropic 时填
# OPENAI_API_KEY=...       # 切到 openai 时填
```

| Provider | 默认 Model | 流式 | Tool-use | Adaptive Thinking | Prompt Cache |
|---|---|---|---|---|---|
| gemini（默认） | `gemini-2.5-flash` | ✅ | ✅（协议转换） | ✅ `thinking_config(budget=-1)` | ⚠️ 隐式 |
| anthropic | `claude-opus-4-7` | ✅ | ✅ 原生 | ✅ 原生 | ✅ 显式 cache_control |
| openai | `gpt-4o` | ✅ | ✅（协议转换） | ❌（gpt-4o 无 thinking） | ⚠️ 隐式 |

`tools.py` 的 `TOOL_DEFINITIONS` 维持 Anthropic 格式作为权威格式，gemini/openai provider 在内部做协议转换。

### 运行方式

```bash
pip install -r agent_system/requirements.txt   # 装齐三家 SDK；用哪家就只需要哪家的 key
# .env 中 LLM_PROVIDER=gemini 即可，无需额外 export

# 从项目根目录运行（project_root 自动推断为 tsai/）
python -m agent_system.main "为 /chat 接口增加输入长度校验"
python -m agent_system.main "重构 backend/rag.py 使 embedding 调用改为异步"

# 或显式指定项目根目录
python -m agent_system.main --root /path/to/tsai "your goal"
```

### Harness 主循环

每轮迭代由四个阶段组成；OBSERVE 与 REVIEW 通过 `asyncio.gather` 并行执行（前者纯 shell、后者 LLM，相互独立）。

```
用户输入 goal
  └─ [Orchestrator] 注入项目上下文（文件树 + ARCHITECTURE.md + 历史 memory）
      └─ Workspace（文件备份基线）+ ToolExecutor
          └─ [TaskHarness.run]
              ├─ PLANNING：[Claude] 生成具体编码任务 plan
              └─ 主循环 × MAX_ITERATIONS=6
                  ├─ ACT：[CodingAgent] tool-use 循环（最多 40 轮）
                  │     ├─ read_file / edit_file / write_file / run_shell / git_diff
                  │     └─ 完成后输出变更摘要
                  ├─ OBSERVE + REVIEW（asyncio.gather 并行）
                  │     ├─ collect_observation：py_compile / pytest / ruff / import 检查
                  │     └─ ReviewAgent：diff 安全/质量分析 → JSON
                  ├─ REFLECT：[ReflectAgent] 聚合 observation+review+history
                  │     → Reflection { status, assessment, next_steps, remaining_issues }
                  └─ Status 分发：
                      ├─ done    → 退出循环（成功）
                      ├─ stuck   → workspace.restore_all() + 退出
                      ├─ replan  → workspace.restore_all() + 重新生成 plan（最多 1 次）
                      └─ continue→ next_steps 反馈进入下一轮 ACT
              └─ TaskState（plan + iterations[] + status）
      └─ [Claude] 生成交付摘要
          └─ [MemoryManager] record_session（status→verdict 映射）
```

### 阶段性进度反馈

每个阶段开始和结束时打印带时间戳的状态行：

```
[15:32:01] ── PLANNING ────────────────────────────────────────
<Claude 流式输出>
[15:32:08] ✓ PLANNING (7s)

[15:32:08] ── ACT [iter 1/6] ──────────────────────────────────
── CodingAgent [round 1] ──
  → read_file(['backend/db.py'])
  → edit_file(['backend/db.py', ...])
  [run_shell] python -m py_compile backend/db.py
[15:34:22] ✓ ACT (134s) · 2 file(s) modified/created

[15:34:22] ── OBSERVE + REVIEW [iter 1] ───────────────────────
  [observe] python -m py_compile "backend/db.py"
  [observe] python -m pytest --tb=short -q
[15:34:52] ✓ OBSERVE + REVIEW (30s) · review=warn

[15:34:52] ── REFLECT [iter 1] ────────────────────────────────
[15:34:55] ✓ REFLECT (3s) · [DONE] Implemented length check; tests skipped (no suite)

[15:34:55] ── SUMMARY ─────────────────────────────────────────
[15:34:58] ✓ SUMMARY (3s)

[15:34:58] ══ Pipeline complete (177s) — DONE after 1 iteration(s) ══
```

### 数据结构（`harness.py`）

```python
@dataclass
class Iteration:
    number: int
    act_summary: str
    observation: str
    review: dict
    reflection: Reflection

@dataclass
class TaskState:
    goal: str
    plan: str
    iterations: list[Iteration]
    status: str          # running | done | stuck | replan | failed
    replanned: bool
```

### Reflection schema（`agents/reflect_agent.py`）

```python
@dataclass
class Reflection:
    status: str              # continue | done | replan | stuck
    assessment: str          # 1-2 句迭代结论
    next_steps: list[str]    # 下一轮 ACT 的具体步骤（文件 + 函数级）
    remaining_issues: list[str]
```

`status` 触发规则：

| 值 | 含义 | Harness 行为 |
|---|---|---|
| `done` | 目标达成；syntax OK；tests pass 或合理跳过；diff 合理 | 退出循环 |
| `continue` | 有进展但未完成 | next_steps 注入下一轮 ACT |
| `replan` | 当前方案根本错了 | 回滚 workspace + 重新规划（仅一次，第二次升级为 stuck） |
| `stuck` | 同一错误连续两轮 / "No changes made" 连续两轮 | 回滚 workspace + 退出 |

### 文件工具（`tools.py`）

| 工具 | 用途 |
|---|---|
| `read_file` | 读取项目文件（上限 20 000 chars） |
| `write_file` | 创建新文件 |
| `edit_file` | 精确字符串替换（old_string 必须唯一） |
| `list_files` | Glob 匹配枚举文件 |
| `run_shell` | 在项目根目录运行 shell 命令（默认 60s timeout） |
| `git_diff` | 返回本次 session 的 unified diff |

安全限制：路径使用 `Path.relative_to()` 校验（修复了 startswith 的路径逃逸漏洞）；`git push` 系列命令被屏蔽。

### Workspace（`workspace.py`）

首次写入文件前自动快照原始内容。提供：

- `get_diff()` → unified diff（供 ReviewAgent 和输出展示）
- `restore_all()` → 回滚所有修改
- `changed_files()` → 已修改文件列表（供 OBSERVE 针对性验证）
- `has_changes()` / `stats()` → 元信息

回滚触发条件：

- **stuck**：ReflectAgent 判定卡死时，Harness 内部 `restore_all()`
- **replan**：重新规划前先 `restore_all()`，让新 plan 从干净状态出发
- **异常保护**：Pipeline 任意位置发生未捕获异常时，Orchestrator 触发 emergency rollback

### OBSERVE 验证链（`agents/test_agent.py:collect_observation`）

实际运行以下命令并汇总输出（无 LLM 调用，结果交给 ReflectAgent）：

1. **diff stats**：变更行数（diff 为空时使用确切短语 `"No changes made"` 以触发 stuck 检测）
2. `python -m py_compile <file>` — 每个被修改的 `.py` 文件
3. `pytest --tb=short -q` — 如果项目有 `tests/` 或 `test_*.py`（否则跳过）
4. `ruff check <files>` — 如果 ruff 已安装（否则跳过）
5. `python -c 'import sys; sys.path.insert(0, "."); import <module>'` — 导入检查（cwd 添加 `.` 到 sys.path，TSAI 模块可正确导入）

### ReviewAgent

接收 Workspace diff（非全文件）进行安全和质量分析，聚焦 diff 引入的变更，输出 `{verdict, summary, security_issues, quality_issues, suggestions}`。

### Agent 接口签名

| Agent | 类 / 函数 | 签名 | 输出 |
|---|---|---|---|
| Orchestrator | `Orchestrator(project_root, memory_path)` | `async run(goal) -> str` | 完整格式化结果 |
| TaskHarness | `TaskHarness()` | `async run(*, goal, project_context, workspace, executor, memory_context) -> TaskState` | TaskState |
| CodingAgent | `CodingAgent` | `run(task, context, executor, max_rounds=40) -> str` | 变更摘要文本 |
| OBSERVE | `collect_observation` | `(project_root, changed_files, diff) -> str` | 拼接报告 |
| ReviewAgent | `ReviewAgent` | `run(diff, task) -> dict` | JSON 报告 |
| ReflectAgent | `ReflectAgent` | `run(*, goal, plan, history, observation, review, iteration) -> Reflection` | Reflection |

### LLM 封装（`llm.py` + `providers/`）

`llm.py` 是薄壳，导入时由 `providers.get_provider()` 工厂根据 `LLM_PROVIDER` 选好后端，对外仍暴露 `complete()` / `complete_with_tools()` 两个函数：

| 函数 | 用途 | 特性 |
|---|---|---|
| `complete()` | 单轮调用（流式） | `verbose=True` 流式打印；JSON-output agents 设 `verbose=False` |
| `complete_with_tools()` | 多轮 tool-use 循环 | 非流式，每轮打印工具调用 |

各 provider 内部差异：

| Provider | thinking | 系统提示词缓存 | tool 协议 |
|---|---|---|---|
| anthropic | `thinking={"type":"adaptive"}` | 显式 `cache_control: ephemeral` | 原生 Anthropic 格式 |
| gemini | `ThinkingConfig(thinking_budget=-1)`（仅 2.5+） | 隐式 | 转 `Tool(function_declarations=...)` |
| openai | 无（gpt-4o 不支持） | 隐式 | 转 `{type:"function", function:{...}}` |

任何后端的失败都抛 `LLMError`（`agent_system.providers.base.LLMError`，`llm.py` 重新导出）。

### MemoryManager（`memory.py`）

跨 session 的 JSON 文件持久化（`agent_system/memory.json`）：

- `context_for_prompt()` → 在 Planning 步骤注入最近 3 次 session 的 goal + verdict + issues
- `record_session()` → 最后持久化，保留最多 20 条 session，滚动更新 `project_context`

Verdict 映射（`orchestrator._status_to_verdict`）：

| TaskState.status | review verdict | 写入 memory |
|---|---|---|
| `done` | `pass` | `pass` |
| `done` | `warn` | `warn` |
| `stuck` / `failed` | * | `fail` |

---

## 十三、pgvector 特殊访问方式

`databases` 库不支持 pgvector 原生类型，每次向量读写前需手动从连接池获取 asyncpg 原始连接并注册 codec：

```python
conn = await database._backend._pool.acquire()
await register_vector(conn)
# ... 执行向量操作 ...
```

相关代码位于 `backend/db.py` 中所有涉及 `embedding` 列的函数。

---

## 十四、自主调优子系统（Phase 3）

> **接手者请优先阅读 13.4** —— 那里记录了所有暂定决策、已知设计盲点和"等运行数据再决定的事"。

### 目标

让 TSAI 的 Agent prompt 能"自己优化自己"——每天机器人跑测试 query → trace 落盘 → Agent B 周期性分析失败模式 → 自动修改 prompt → Agent C 验证效果（坏就回滚）。

### 三阶段渐进上线

| Phase | 范围 | 状态 |
|---|---|---|
| **3a** | 基础设施：DB 三表、prompt 搬到 DB、trace 自动落盘、admin 页面拆板块、Celery+Redis 骨架 | ✅ 已上线 |
| **3b** | 机器人用户：复刻"天书"的 session、每日 5 个 query、性能调优页展示数据 | ✅ 已上线 |
| **3c** | Agent B：每 12 小时分析 agent trace、自动改 prompt（含护栏与回滚）| ✅ 已上线 |
| **3d** | Agent C：每 4 小时验证 prompt 改动效果、score 下降即自动回滚 | ✅ 已上线 |

### 评分公式（Agent C 用）

```
score = -iterations - 5 * hallucination_rate - 0.001 * latency_ms
```

prompt 变更前后跑同样验证集，新版分数显著低于旧版 → 自动 rollback。

### Prompt 加载机制

`backend/agent_chat.py` 不再硬编码 `_AGENT_TOOL_RULES`，改为：

```python
async def build_system_prompt(persona: str | None) -> tuple[str, int]:
    rules, version_id = await _get_cached_rules()  # 30s in-memory cache
    return f"{identity}\n\n{rules}", version_id
```

- `_get_cached_rules()` 从 `prompt_versions WHERE is_active=TRUE` 拉取
- 内存缓存 30 秒，避免每次 /chat 都查 DB
- Agent B 改完后调用 `invalidate_prompt_cache()` 让所有进程下次请求重新拉
- DB 里没有任何版本时（首次启动）→ 自动从代码兜底常量种入 v1

### 调度框架（Celery + Redis）

```
backend/celery_app.py    Celery 实例 + Redis broker/backend 配置
backend/tasks.py         任务定义（3a 仅 ping）

启动 worker：
    celery -A backend.celery_app worker --loglevel=info

启动 beat（周期任务，3b 后才需要）：
    celery -A backend.celery_app beat --loglevel=info
```

`.env` 配置：`REDIS_URL=redis://localhost:6379/0`

### 14.1 机器人子系统（Phase 3b）

**目标**：自动产生测试流量验证 prompt 调教效果，无需人工每日手动测。

**关键文件**：
- `backend/bot.py` — 用户管理、session snapshot、query 生成、内部直跑模式
- `backend/tasks.py` — `bot_run_daily_queries` Celery 任务
- `scripts/setup_bot.py` — 一次性初始化脚本

**初始化流程**（部署后执行一次）：

```bash
# 1. 创建机器人用户 + 复刻"天书"的 sessions
python -m scripts.setup_bot

# 2. 启动 Celery worker（执行任务）
celery -A backend.celery_app worker -l info -D    # -D 后台运行

# 3. 启动 Celery beat（按 BOT_RUN_HOUR 触发任务）
celery -A backend.celery_app beat -l info -D

# 4. 进 /admin/perf 点击「启用每日自动 query」
```

**每日任务流程**（默认每天 03:00 触发）：

```
1. 检查 subsystem_status.bot.enabled —— 关掉就跳过
2. 按 day_of_year % N 选 1 个 bot session（每天轮换）
3. 用 day_of_year 作 seed 从 10 个 query 模板里随机抽 5 个
4. 顺序跑 5 条 query（不并发，避免打爆 Gemini quota）
5. trace 自然落进 agent_traces，与真人 query 同一张表
6. heartbeat_subsystem("bot", ...) 更新心跳
```

**Query 模板（10 个）**：覆盖 `small_talk` / `rag` / `agent` 三种路径的典型场景，含寒暄、列举、总结、对比、回忆、开放性、多问句、反幻觉。具体见 `backend/bot.py:_QUERY_TEMPLATES`。

**机器人控制端点**（`/admin/perf` 页面按钮）：

| Endpoint | 作用 |
|---|---|
| `POST /admin/bot/start` | 启用每日自动 query |
| `POST /admin/bot/stop` | 停用 |
| `POST /admin/bot/snapshot` | 触发一次性 session 复刻 |
| `POST /admin/bot/run_now` | 立即触发 1 次每日任务（异步，Celery） |
| `GET  /admin/bot/recent_queries` | 最近 20 条机器人 query 的 JSON |

### 14.2 Agent B：自动 prompt 调优（Phase 3c）

**目标**：每 12 小时扫"未分析的 agent trace"，识别失败模式，**自动**修改 prompt（含安全护栏）。

**关键文件**：
- `backend/agent_b.py` — 核心：Redis 锁 / Gemini 调用 / patch 应用器 / 5 项护栏 / 24h 频率门控
- `backend/tasks.py:agent_b_analyze_pending_traces` — Celery 任务包装
- `backend/celery_app.py` — beat schedule：`agent-b-periodic-analysis` 每 `AGENT_B_RUN_HOURS` 小时一次

**完整流程**：

```
1. Redis SETNX("agent_b:lock", ex=600s) → 拿不到就跳过
2. subsystem_status.agent_b.enabled 检查 → 关掉就跳过
3. agent_b_runs 表 INSERT 一行（记录这次运行）
4. fetch_pending_agent_traces(limit=20) → route='agent' AND analyzed_at IS NULL
   不足 AGENT_B_MIN_TRACES（默认 5）则跳过本轮
5. 当前 active prompt + 20 条 trace → gemini-2.5-pro
6. Gemini 输出 JSON：issues_found / should_change_prompt / patch
7. 若 should_change_prompt=true：
   ├─ has_recent_agent_b_change(24h) ？ → 是则拒绝（频率门控）
   ├─ apply_patch_to_prompt() → new_content
   ├─ 5 项护栏校验：
   │    ├─ 长度 [500, 5000]
   │    ├─ 必备 section: # 工具 / # 决策优先级 / # 引用纪律 / # 禁止
   │    ├─ 必备 tool 名: search_kb / read_document / list_documents / web_search / search_history
   │    └─ diff 大小 ≤ 100% 现版本
   └─ 通过 → upsert_prompt_version + invalidate_prompt_cache
8. mark_traces_analyzed(ids) 标记这批 trace
9. 更新 agent_b_runs 完成行（含失败原因）
10. heartbeat_subsystem("agent_b", ...)
11. finally: 释放 Redis 锁
```

**6 类失败模式标签**（Gemini 用这些分类问题）：

| category | 触发条件 |
|---|---|
| `hallucination` | citations 引用了 tools_called result_preview 中找不到的 source/chunk |
| `wrong_tool` | 该用 search_kb 时用了 web_search（或反过来）|
| `over_search` | iterations ≥ 5 |
| `under_search` | iterations=1 且无 tool 调用，但 query 显然需要检索 |
| `verbatim_query` | search_kb 的 query 跟用户原话 ≥ 80% 相似 |
| `repeated_call` | 同 tool 同参数 ≥ 2 次 |

**Patch 策略**（Gemini 必须返回的格式）：

```json
{
  "issues_found": [{"category": "hallucination", "frequency": 5, "trace_ids": [...]}],
  "should_change_prompt": true,
  "patch": {
    "strategy": "tighten_section",   // 或 rewrite_section / additive
    "target_section": "# 引用纪律（硬性规则）",
    "new_section_content": "# 引用纪律（硬性规则）\n（改进的全文）",
    "reasoning": "5/20 trace 出现 hallucination，原 section 缺乏 verification 步骤..."
  }
}
```

**Agent B 控制端点**：

| Endpoint | 作用 |
|---|---|
| `POST /admin/agent_b/start` | 启用周期分析 |
| `POST /admin/agent_b/stop` | 停用 |
| `POST /admin/agent_b/run_now` | 立即触发 1 次分析（异步） |
| `POST /admin/prompt/rollback/{version_id}` | 紧急回滚到指定版本 |

### 14.3 Agent C：自动验证 + 回滚（Phase 3d）

**目标**：Agent B 改 prompt 后，自动评估新版本是不是真的更好——不是就回滚。

**关键文件**：
- `backend/agent_c.py` — 核心：Redis 锁 / hallucination 反查 / score 计算 / 决策 / rollback
- `backend/tasks.py:agent_c_verify_prompt_change` — Celery 任务包装
- `backend/celery_app.py` — beat schedule：`agent-c-periodic-verification` 每 `AGENT_C_RUN_HOURS` 小时一次

**完整流程**：

```
1. Redis SETNX("agent_c:lock", ex=600s) → 拿不到就跳过
2. subsystem_status.agent_c.enabled 检查 → 关掉就跳过
3. 拿当前 active 版本 v_new 和它前一个 v_old
4. 仅当 v_new.created_by = 'agent_b' 才验证（manual / bootstrap 不动）
5. fetch_traces_by_version(route='agent') 各拿两侧的 trace
6. 任一侧 < AGENT_C_MIN_TRACES（默认 5）→ insufficient_data 跳过
7. 对每条 trace（hallucination_rate IS NULL 的）：
     • 遍历 citations 中含 (source, chunk) 的引用
     • is_kb_chunk_real(session, source, chunk) 反查 knowledge_base
     • rate = fake_count / verifiable_count
     • update_trace_hallucination_rate 写回
8. 算两侧均分：
     score = -iterations - 5*hallucination_rate - 0.001*latency_ms
9. delta = new_avg - old_avg
   ├─ delta < 0 → activate_prompt_version(v_old) + invalidate_prompt_cache，decision='rolled_back'
   └─ delta ≥ 0 → 保留，decision='kept'
10. agent_c_runs 落盘 + heartbeat
11. finally: 释放锁
```

**Agent C 不调任何 LLM**——纯基于已有 trace 数据 + KB 反查，零成本运行。

**与 Agent B 的协作**：

```
Agent B（每 12h，且 24h 内最多 1 次成功改）   Agent C（每 4h）
         ↓                                          ↓
    创建 v2 active                          检测到 v2 是 agent_b 创建
         ↓                                          ↓
    bot/真人产生 v2 trace               拉 v_new+v_old trace 算 score
         ↓                                          ↓
                                         delta < 0 → 回滚 v2 → v1 active
                                                    ↓
                                          24h 后 Agent B 可以再尝试
```

**Agent C 控制端点**：

| Endpoint | 作用 |
|---|---|
| `POST /admin/agent_c/start` | 启用周期验证 |
| `POST /admin/agent_c/stop` | 停用 |
| `POST /admin/agent_c/run_now` | 立即触发 1 次验证（异步） |

### 14.4 已知限制 + 暂定决策 + 待改进项

记录 Phase 3 全期决策的"暂定 / 妥协 / 留作未来改进"事项。每条都注明 **影响**、**当前对策** 和 **触发改进的信号**。未来接手者请优先阅读本节。

#### 14.4.1 Phase 3d.1 — Hallucination 检测设计盲点（**优先级：中**）

**现象**：`agent_c.py:_compute_hallucination_rate` 用"反查 knowledge_base 是否存在 (source, chunk)"作为幻觉判定。但 `agent_traces.citations` 字段记录的是**工具调用返回的 chunks**，这些 chunks 全部来自数据库查询——必然存在。所以 `hallucination_rate` 在实际数据中**几乎恒为 0**。

**真正的"数据层幻觉"应该是**：agent 答案文本里写了"（来源：xxx，第 N 段）"，但 N 段**不在本轮任何 tool 调用结果中**——也就是 agent 凭空编了引用。当前代码没做这个解析。

**影响**：score 公式实际只在用 `iter` + `latency_ms` 两个信号，hallucination 维度名存实亡。多数 Agent B patch（改善引用纪律 / 减少过度搜索）会同时优化 iter 和 latency，所以**当前的判断仍然多数情况下正确**。但当 Agent B 改"答得更准但更慢"这类 patch 时，会误判为更差并回滚。

**暂定对策**：不修。等观察到 `agent_c_runs.decision='rolled_back'` 占比 > 30% 时再修。

**修复方案**（约 50 行代码）：在 `_compute_hallucination_rate` 中：
1. 拿 `message_id` 对应的答案文本
2. 用正则解析"（来源：X，第 N 段）"模式
3. 对照 `tools_called` 各 `result_preview` 中提取出的 `(source, chunk)` 集合
4. 答案中出现但 tool 结果中没出现的 = 真幻觉

#### 14.4.2 Score 公式的 latency 主导问题（**优先级：低**）

```
score = -iterations - 5 * hallucination_rate - 0.001 * latency_ms
                                                ─────────────────
                                                典型贡献 -5 到 -30
```

各项典型贡献：

| 维度 | 典型范围 | 对 score 贡献 |
|---|---|---|
| iterations | 1-6 | -1 到 -6 |
| hallucination_rate | 0-1 | 0 到 -5 |
| latency_ms | 5000-30000 | -5 到 -30 |

**latency 数量级最大**，意味着 prompt 改动如果导致延迟轻微上升、但准确性大幅提升，可能仍被判定为更差。

**暂定对策**：暂用现公式。修复 13.4.1 后如果发现 latency 仍然过度主导，调权重：`5*halluc → 10*halluc` 或 `0.001*ms → 0.0005*ms`。

**触发改进信号**：观察一段时间，发现"显然变好的 patch 被回滚了"——log 里 `delta < 0` 但人工评估 v_new 答案明显更好。

#### 14.4.3 needs_agent 启发式覆盖不全（**优先级：低**）

`agent_chat.py:needs_agent` 用关键词匹配判断是否走 Agent 路径。已知漏点：

| 用户原话 | 期望路径 | 实际路径 | 漏掉的关键词 |
|---|---|---|---|
| 我都上传了哪些**文档**？ | agent → list_documents | rag | "哪些文档"未匹配 `_LIST_KEYS = ("有哪些","都有什么","列出","列表")` |
| 我有什么**资料**？ | agent → list_documents | rag | 同上 |

**影响**：本应该用 `list_documents` 工具的 query 走了 RAG，得到的答案靠拼凑文档片段，不如直接列文件名清晰。

**暂定对策**：不修。Phase 3c 上线后如果 Agent B 自己识别到这个失败模式（user_under_search 类型），会自动改 prompt 规则补救。

**修复方案**（5 行代码）：在 `_LIST_KEYS` 中加 `"哪些文"` / `"哪些资料"` / `"哪些文档"` / `"哪些文件"`。但要小心过度触发（"哪些"两字过于宽泛）。

#### 14.4.4 Bot session 选择无"exclude last"（**优先级：很低**）

`bot.py:run_bot_daily` 用 `random.choice(non_empty_sessions)`。3 个 session 时，连续两次"立即跑一次"撞同 session 的概率约 33%。

**影响**：观察某个 session 的 trace 时，可能发现"最近 3 次 run 都在它上面跑"，覆盖率不均。

**暂定对策**：接受。Bot 的目标是"长期覆盖所有 session 产生多样数据"，短期偶尔重复可接受。

**修复方案**：用 `subsystem_status.last_action` 字段记录上次选中的 session_id，下次抽样时排除它。约 10 行。

#### 14.4.5 Bot query 模板池小（**优先级：很低**）

`_QUERY_TEMPLATES` 共 10 条，每次 `sample(5)`，理论组合 C(10,5)=252。多次"立即跑一次"虽然每组 5 条不同，但**跨组完全相同**的概率不为零。

**影响**：长期看 bot 的 query 多样性受限。已通过"`这份资料`"模板泛化（commit `56c1453`）让模板对各种 session 内容都适用，缓解了重复感。

**暂定对策**：不动。10 条够 Phase 3 验证。

**修复方案**：3 条路径：
1. 扩展模板池到 20+（人工增补）
2. 让 Gemini 基于 session 内容动态生成 query（成本高）
3. 记录最近 N 次 query，强制不重复

#### 14.4.6 Bot snapshot 非幂等（**优先级：低，但要小心**）

`/admin/bot/snapshot` 重复点击会**追加**新的 session 副本，不去重。当前 admin 页面按钮点击有 confirm 弹窗提醒，但没有强校验。

**影响**：误点会产生大量重复 session（"[bot] 历史"、"[bot] 历史"、"[bot] 历史"……），机器人轮换会被稀释。

**暂定对策**：靠 confirm 弹窗 + admin 自觉。生产环境只点过一次。

**修复方案**：在 `snapshot_user_sessions` 入口检查目标用户是否已有 `[bot]` 前缀的 session，有就拒绝（或提供 `--force` 参数）。

#### 14.4.7 Phase 2 步骤 2（SSE 流式 UX）未做（**优先级：低**）

Phase 2 第 1 步（Agent 后端循环 + JSON 响应）已上线。第 2 步是把响应改成 Server-Sent Events，让用户看到 agent 的中间步骤（"正在搜索..."等）实时滚动出来。

**当前体感**：复杂 query 用户等 10-30 秒沉默，然后答案一次性出。**主要靠 needs_agent 路由让 60-70% 简单 query 走 RAG 不进 Agent** 来缓解延迟感知。

**暂定对策**：不做。Phase 2 第 1 步加上路由+提前退出+并行 tool 已经覆盖大部分体感问题。

**触发改进信号**：用户反馈"等太久不知道在干嘛"。

#### 14.4.8 Bot 不复刻消息历史（**设计选择，非缺陷**）

`snapshot_user_sessions` 只复制 `sessions` + `knowledge_base`，**不复制 messages**。机器人持有"天书的 session 副本"+"干净对话历史"。

**理由**：bot 用来产生测试 trace，对话历史从零开始更可控；如果连历史也复刻，"recall" 类 query 测的就是"天书过去聊过什么"——超出测试范围。

**这是设计选择，不打算改。** 写在这里以防有人想改。

#### 14.4.9 Celery 多 worker 共享 broker（**已缓解，无需进一步改进**）

prod 上 TSAI 和另一项目 `mine` 共享 Redis broker。已通过 **独立 queue (`-Q tsai`) + 独立 node 名 (`-n tsai@%h`)** 隔离。`celery inspect ping` 仍显示两个 node 是设计行为（同 broker 广播），**任务路由完全隔离**。

**进一步隔离方案**（如果未来需要）：用不同 Redis DB（`REDIS_URL=redis://localhost:6379/1`），inspect 也只看到自己。

#### 14.4.10 待评估（基于运行数据）

观察期 7-14 天后再决定：

- [ ] `agent_c_runs.decision='rolled_back'` 比例多少？> 30% 触发 13.4.1 修复
- [ ] Agent B 改动方向是否过窄？（总是改"引用纪律"或"过度搜索"）
- [ ] Bot 5 个 query/天够不够 Agent B 看出模式？数据少时 Agent B 跳过的次数多不多？
- [ ] 24h 频率门控是否过严？真有质量倒退时是否要急于回滚而不等 24h？

### 14.5 Phase 3 之外的"智能化"思路（待规划）

Phase 1（full-context 路径）和 Phase 2 第 1 步（Agent ReAct 循环）已落地。完整的 6 个智能化方向中**剩余 4 个**待规划：

| 思路 | 方向 | 适合时机 |
|---|---|---|
| 思路 2 | 层级化索引（document → section → chunk）| 单文档 > 500 chunks 时显著提升 |
| 思路 3 | Hybrid search（BM25 + 向量）+ Cross-encoder rerank | 当前 RAG 召回质量明显不足时 |
| 思路 5 | 会话长程记忆（滚动摘要 + 实体笔记本）| 长对话出现"前后记不住"问题时 |
| 思路 6 | 答案验证 & 引用 grounding（拆 claim 反查）| 用户反馈幻觉问题严重时 |

这些方向都是**当 Phase 3 自主调优系统跑稳后**，根据观察到的真实失败模式来选择性引入的。**不要为了上而上**。

---

## 十五、作图模块（`drawing.py` + `templates/drawing.html`）

### 概述

作图模块是与对话、写作并列的第三个板块：左侧栏按「作图风格」分 tab（每个风格是一条可新建/编辑/删除的记录，可从共享的 Prompt 库勾选任意多条拼入最终 prompt），右侧是作图工作区——输入文字，用当前风格调用 `gpt-image-1`（经 CODEX 中转）生成图片，历史生成结果以画廊形式展示。**访问受 `users.can_draw` 权限门控**：仅 `is_admin` 或 `can_draw=TRUE` 的用户可用（`require_draw_access` 依赖，逐字镜像写作模块的 `require_write_access`），管理员在 `/admin/users` 逐用户授予（`POST /admin/user/{id}/set_draw`）。

设计上以写作模块为直接模板（同为独立板块、同样是"侧栏列表 + 右侧工作区"结构），复用其路由注册、权限校验、DB 初始化、文件存储等约定。两处刻意偏差：JS/CSS 放在独立的 `static/js/drawing.js` / `static/css/drawing.css`，而不是像 `writing.html` 那样把上千行样式和脚本内联进模板；生成接口不用 SSE（图像生成没有可展示的逐字流式中间态），走一次性 JSON 请求/响应，前端显示 loading 占位即可。

### 图像生成后端：`backend/image_gen.py`

复用写作模块已在用的 `CODEX_API_KEY`/`CODEX_BASE_URL`（第三方 OpenAI 兼容中转，`https://gpt.hinature.cn/v1`），新增独立的 `CODEX_IMAGE_MODEL`（默认 `gpt-image-1`）配置项，与写作排版用的 `CODEX_MODEL=gpt-5.5` 互不影响。

```python
async def generate_image(prompt: str, *, size: str = "1024x1024") -> bytes:
    # POST {CODEX_BASE_URL}/images/generations
    # 解析 {"data": [{"b64_json": ...}]}（OpenAI 标准形状）→ base64 解码 → 返回 PNG bytes
```

用原生 `httpx.AsyncClient`（不像 `writing.py:_codex_format_sync` 那样借用 `asyncio.to_thread` 包同步调用），配置统一走 `settings.py`（不像 `_codex_format_sync` 那样绕开 settings 直接 `os.getenv`）。

**已验证**：中转端点确实代理了 `images/generations`，返回体是标准 OpenAI 形状 `{"data":[{"b64_json","revised_prompt"}], "model", ...}`。**已知怪癖**：请求 `model="gpt-image-1"` 时，中转返回的 `model` 字段实测为 `"gpt-image-2-codex"`——中转会静默替换成其他底层模型，不影响调用方式和响应形状，但如果未来该中转下线 `gpt-image-1` 映射导致报错，需要现场调整或切换到官方 `api.openai.com`（届时只需新增 `OPENAI_IMAGE_API_KEY`/`OPENAI_IMAGE_BASE_URL` 并切换 `image_gen.py` 里读取的配置字段，调用与解析逻辑不变）。

### 核心生成流程（`POST /drawing/styles/{id}/generate`）

```
1. 校验风格归属（_ensure_style_owner）
2. 按 style.prompt_ids 从 drawing_prompts 取出已勾选条目的 content（过滤已删除的 id），拼成 style_context
3. 拼接 full_prompt = style_context + "\n\n" + user_input（绑 skill 包 / 编辑场景走 LLM 编译，见十五.4）
4. create_drawing_generation() 插入行 → 立即 update 为 status='processing'
5. background_tasks.add_task(_run_generation_job, ...)：接口**立即返回** {"id","status":"processing","full_prompt","compile_degraded"}
6. 后台 _run_generation_job：
   ├── await image_gen.edit_image(...) 或 generate_image(...)（出图经第三方中转，常见 1-3 分钟）
   ├── 成功：写入 static/images/{username}/{style_id}/{generation_id}.png（DB 存相对路径），
   │        update_drawing_generation_result(status='done', image_path=..., model=...)
   └── 失败：update_drawing_generation_result(status='failed', error_msg=<面向用户的中文说明>)
7. 前端拿到 processing 后，每 3s 轮询 GET /drawing/generations/{id}/status 直到 done/failed
   （最多轮询 12 分钟；切走页面再回来，loadGallery 按 processing 记录自动续上轮询）
```

> **为什么改成后台任务 + 轮询**：`gpt-image-1` 经中转出图要 1-3 分钟、偶尔更久，同步 HTTP 请求会被生产的 gunicorn（默认 `--timeout 30`）/ Nginx 杀掉返回 502。改成"提交即返回 + `generation_id` 轮询"后，长耗时挪到 `BackgroundTasks` 里，不占 HTTP 连接；`backend/image_gen.py` 的 httpx read 超时由 `.env` 的 `CODEX_IMAGE_TIMEOUT`（默认 300s）控制。**代价**：后台任务在进程内跑，部署/重启会丢，残留 `processing` 需手动清（见 `PRODUCTION.md`）。
>
> **错误分类**：`ImageGenError` 带 `kind` 字段（`config`/`upstream`/`timeout`/`network`/`auth`/`bad_request`/`decode`）。中转或其上游返回 4xx/5xx（如整体故障时的 `404 Upstream request failed`）→ `kind='upstream'`，`error_msg` 明确写"第三方图像服务暂时不可用…这不是本站的问题…请反馈给中转服务提供商"，前端 toast + 失败占位卡都展示这条。

> **实现时踩过的坑**：`drawing_prompts.id`/`drawing_styles.id` 是 `UUID` 列，asyncpg 读出来是 `uuid.UUID` 对象；而 `drawing_styles.prompt_ids`（jsonb 数组）里存的是字符串。用 `{p["id"]: p for p in prompts}` 直接建字典再拿字符串 id 去查会**静默查不到**（`pid in all_prompts` 恒为 False，不报错，只是这条 Prompt 悄悄没被拼进最终 prompt）。修复：建字典时 `str(p["id"])` 统一转字符串。

> **路由注册顺序踩过的坑**：`GET /{style_id}` 这类单段通配路由必须放在文件**最后**声明。FastAPI/Starlette 按注册顺序匹配路由，若 `/{style_id}` 声明在 `/prompts`、`/styles` 等固定路径之前，会抢先把 `GET /drawing/prompts` 当成 `style_id="prompts"` 处理，实测直接导致 asyncpg UUID 解析报错、请求 500。`writing.py` 的 `/{task_id}` 页面路由本就声明在文件末尾，这是同一个坑的既有解法，作图模块照做即可。

### Prompt 库

早期版本里"风格自己的一段 prompt 文本"和"共享的 skill 规则片段库"是两套概念（`drawing_styles.prompt` 自由文本 + `drawing_skills`/`skill_ids` 共享片段），但两者在生成时的效果完全一样——都只是原样拼进最终 `full_prompt`，没有优先级或语义差异，纯属"私有一段 vs. 共享片段库"的组织方式不同，容易混淆。已合并为统一的 **Prompt 库**（`drawing_prompts` 表，原 `drawing_skills` 改名，`snippet` 列改名为 `content`；`drawing_styles.skill_ids` 改名为 `prompt_ids`，原 `drawing_styles.prompt` 自由文本列整列删除）：不分用户的共享表，所有有作图权限的用户共同维护、共同复用，一个风格可以勾选任意多条（0~N），自动拼入最终 prompt（见上文流程第 2-3 步）。删除一条 Prompt 不会强制解除各风格的引用，只是拼 prompt 时按当前存在的 id 过滤，行为上是"悄悄失效"而非报错。新建风格时不预选任何 Prompt，勾选统一放到创建后的设置面板里做。

### 前端（`templates/drawing.html` + `static/js/drawing.js` + `static/css/drawing.css`）

结构镜像写作模块：`#drawing-sidebar`（桌面常驻风格列表，`flex:0 0 260px`）+ `.drawing-shell`（`nav` + `.drawing-layout` grid `1fr 300px`）。响应式断点与写作模块相同（`≥993px` 桌面栏可见 / `≤992px` 收起变单列）。

| 区域 | ID | 内容 |
|---|---|---|
| 左侧风格列表 | `#drawing-sidebar` | 风格列表 + 新建风格 / 进入对话 / 进入写作按钮 |
| 主区 | `#drawing-main` | `#generation-gallery`（历史链列表，见十五.4） + `#prompt-input-box`（底部固定文字输入 + 生成按钮） |
| 右侧设置 | `#drawing-settings` | 风格名称 / Prompt 库勾选列表 + 管理入口 / 绑定 Skill 包下拉 + 管理入口 / 保存设置按钮 |

**交互模式**：侧栏切换风格 = 整页跳转 `/drawing/{style_id}`（镜像写作模块的既有模式，服务端渲染注入 `styleId`），不引入 SPA 状态管理。风格/图片数据通过 `<script id="drawing-init-data" type="application/json">` 注入初始 `styleId`，`drawing.js` 首帧读取后发起后续 fetch。

「生成」按钮提交时短暂显示 loading 态（提交很快返回），随后画廊里出现一张 **processing 占位卡**（spinner + "出图中…可能需要 1-3 分钟，可离开本页面稍后回来查看"）；`drawing.js` 用 `activePolls` Map 管理每条生成的 3s 轮询，`loadGallery()` 每次重渲染后会按 `status==='processing'` 的记录自动 `startPolling`（切走再回来也能续上），完成/失败后再整块重渲染。失败卡展示 `error_msg` + 删除按钮。进度提示只出现在对应生成所在行的占位卡里，不在输入框附近放常驻说明。完成后整块重新拉取画廊数据渲染（`loadGallery()`），不做增量 DOM patch。点击历史步骤缩略图弹出 lightbox 展示原图 + 实际发送的 prompt 文本，便于核对。

`chat.html`、`writing.html` 侧栏底部各加一条 `{% if can_draw %}` 包裹的「进入作图」入口；`drawing.html` 侧栏同样加了 `{% if can_write %}` 包裹的「进入写作」入口，三个板块可互相跳转。

### 十五.4 图片迭代编辑（历史链）

**目的**：作图不是一次性生成就完事——用户经常需要针对已生成的图片连续追加修改（"加一只凤凰"→"再让光线暖一点"），且要能看到每一步改了什么、能单独撤销某一步而不影响其余历史。

**数据模型**：`drawing_generations` 新增自引用外键 `parent_generation_id UUID REFERENCES drawing_generations(id) ON DELETE CASCADE`（可空）。`parent_generation_id IS NULL` = 一条编辑历史链的根（最初创作）；非空 = 对某个节点的一次编辑。正常使用下每条链是线性的，但表结构本身不限制分叉（没有为分叉做任何特殊设计，纯粹是"允许多个子节点"这个约束的自然结果）。

**画廊 = 折叠的历史链列表**：`#generation-gallery` 从网格缩略图改成竖直列表，每一行对应一条链的**当前最新一步（叶子节点）**，不是每一次生成都单独占一行。取"叶子节点"的查询是 `NOT EXISTS (SELECT 1 FROM drawing_generations c WHERE c.parent_generation_id = g.id)`（`backend/db.py:get_drawing_generation_tips`）。折叠态：左缩略图 + 右侧该行最近一次输入文字。点击整行原地展开，用 `WITH RECURSIVE` 沿 `parent_generation_id` 回溯到根、按时间正序返回整条链（`get_drawing_generation_lineage`），渲染成纵向步骤列表。**同一时刻只允许一行展开**——`static/js/drawing.js` 里的 `expandedGenerationId` 单例变量逐字镜像 `writing.html` 分段写作的 `expandedSectionId` 模式（见十一.4）。没有任何行展开时，底部输入框行为是"新建一条链"（`POST /generate` 不带 `parent_generation_id`）；有行展开时，输入框目标切换为"基于这条链最新一步继续编辑"（带上 `parent_generation_id`），成功后自动把展开目标指向新产出的这一步，不需要用户重新点选就能连续追加指令。

**编辑调用**：`backend/image_gen.py` 新增 `edit_image(image_bytes, prompt) -> bytes`，POST 到 `{CODEX_BASE_URL}/images/edits`（multipart：`image` 文件 + `prompt` + `model`），响应形状与 `images/generations` 一致，已用真实请求验证可用。

**编辑指令不是简单字符串拼接**：`drawing.py:_compose_edit_instruction()` 用项目已有的 Gemini `client` 做一次非流式调用，把「风格设定 + 链的根节点 prompt（最初创作意图）+ 父节点 prompt（上一轮编辑指令）+ 用户本次新指令」合成为一条单一、清晰、可直接执行的编辑指令，再连同**父节点产出的图片文件**一起传给 `edit_image()`。图片本身的最新视觉状态才是编辑最重要的输入（模型直接"看得到"当前长什么样），文字合成只是为了避免多轮编辑后指令上下文丢失导致风格漂移。Gemini 调用失败时静默回退成用户原始输入（`except Exception` 兜底，不阻断生成流程）。

**删除的两种语义**：
- `DELETE /drawing/generations/{id}/step`（`delete_drawing_generation_step`）——单步删除，在 `database.transaction()` 里先把待删节点的子节点接到它的父节点上（"拼接"），再删除该节点本身；链上其余步骤不受影响。若待删节点既无父也无子（链上唯一一步），这个操作自然退化成删掉整条链，不需要特殊分支。用在展开视图里每一步的删除按钮。
- `DELETE /drawing/generations/{id}`（`delete_drawing_generation_lineage`）——整条链删除，走 `WITH RECURSIVE` 定位到根节点后删除，`ON DELETE CASCADE` 级联清空所有子孙。用在折叠行的删除按钮，前端会先 `confirm('删除这张图片将同时删除其全部创作历史，确定继续？')`。**注意**：该函数区分"链不存在返回 `None`"和"链存在但没有任何图片文件返回 `[]`"两种情况（供路由层正确返回 404 而不是误判成功），不要简单用 `if not result` 判断。

> **实现时踩过的坑（同一类 UUID/str 比较问题的第三次出现）**：`generate()` 路由里校验"待编辑的图片是否属于当前风格"时写成 `parent["style_id"] != style_id`——`parent["style_id"]` 是 asyncpg 返回的 `uuid.UUID` 对象，`style_id` 是来自 URL 路径的字符串，两者永远不相等，导致合法编辑请求 100% 报 404。修复：`str(parent["style_id"]) != style_id`。这是本模块第三次踩到"UUID 列在 Python 里比较/查字典前忘记转字符串"的坑（前两次见十五.2 的 skill_ids 查找），以后任何 `asyncpg` 返回的 UUID 列要跟外部传入的字符串做比较/查字典，一律先 `str()`。

**验证**：真实调用过完整链路——新建风格→首次生成→连续编辑两次（第二次编辑不重新指定 `parent_generation_id`，验证"继续编辑"体验）→确认 `get_drawing_generation_tips` 只返回最新一步、`get_drawing_generation_lineage` 按顺序返回三步→删除中间步骤确认前后节点正确拼接、磁盘文件同步删除→删除整条链确认级联清空、DB 行清零、重复删除返回 404。

### 十五.5 绑定第三方 Skill 包（档位 A：指令包型）

**背景**：Prompt 库（十五.2）只能拼几句零散规则，不足以承载真正意义上的第三方"skill"——例如 `SKILL.md` + `references/*.md` 这种完整方法论文档（固定规则、可变规则、prompt 编译模板、禁止项），本质是给 agent 读的指令包，不是一段静态 prompt。本节新增的机制让一个"风格"可以绑定这样一整套方法论，生成时用它来**编译**最终 prompt，而不是简单字符串拼接。

只实现了"指令包型"（`kind='instruction'`，纯靠现有 Gemini client 做 LLM 编排，不涉及外部工具调用）。预留了 `kind='mcp'` 和 `mcp_config JSONB` 字段给"真正连接第三方 MCP server"的档位 B，但本期未实现——调研的两个真实 skill 仓库（`gc-minimal-zine-poster`、`photo-abstract-editorial`）均不依赖 MCP，纯粹是"读文档 → LLM 编译出最终 image-gen prompt"模式，档位 A 已经够用；档位 B 涉及运行不受信任的第三方代码，安全和工程成本显著更高，暂无真实需求，值得单独立项。

**数据模型**：`drawing_skill_packages` 表（不分用户，全体作图用户共享，镜像 Prompt 库的共享约定）存一份 skill 全文快照（`instructions` 字段），`drawing_styles.skill_package_id`（外键，`ON DELETE SET NULL`）指向最多一个包——单值外键天然保证"最多绑一个"，不需要额外校验。**与 Prompt 库（`prompt_ids`）并存、语义不同**：`prompt_ids` 是零散的、自己攒的 Prompt 条目，`skill_package_id` 是完整第三方方法论，风格设置里两者可以同时配置，互不冲突（勾选的 Prompt 内容拼成 `style_context`，作为"补充要求"传给编译层，见下文）。

**Skill 全文录入是"抓取预览 → 人工确认 → 保存"两段式**（`_fetch_skill_package_from_github`，`drawing.py`）：从 `repo_url` 解析 `owner/repo` → GitHub API 取默认分支 → 拉取 `SKILL.md` + `references/` 目录下所有 `.md` 文件 → 按 `# SKILL.md\n{...}\n\n# references/xxx.md\n{...}` 拼接成一段文本**返回给前端预览**，不直接落库；用户在文本框里可编辑后再点"保存"才真正写入 `drawing_skill_packages`。这样存的是仓库某一时刻的快照，不依赖第三方仓库长期存活，也给用户一个保存前检查/精简内容的机会。删除一个包，绑定它的风格通过 `ON DELETE SET NULL` 自动解绑，不报错。

**勾选的 Prompt 库内容与绑定的 skill 不是二选一，而是都作为上下文喂给同一次 LLM 编译调用**（`_compile_with_skill_package`，`drawing.py`，紧挨着既有的 `_compose_edit_instruction`，是同一设计模式的推广）：

```python
async def _compile_with_skill_package(
    *, skill_instructions: str, style_context: str,
    user_input: str, base_image_bytes: bytes | None = None,
    initial_prompt: str = "", previous_prompt: str = "",
) -> tuple[str, bool]:
    # parts = [skill_instructions 全文（要求"严格遵循其中的规则、术语和结构"）,
    #          style_context（风格勾选的 Prompt 库条目拼接，"补充要求，在不违反 skill 规则的前提下尽量满足"）,
    #          initial_prompt / previous_prompt（编辑场景的历史上下文，同 _compose_edit_instruction）,
    #          user_input（本次创作/修改要求）]
    # 有 base_image_bytes 则用 gtypes.Part.from_bytes(data=..., mime_type="image/png") 把图片一起传给
    # client.aio.models.generate_content —— 走 vision 输入（编辑场景，模型能"看到"当前图片状态）
    # 编译失败或返回空文本，回退为 style_context + user_input 直接拼接，不阻断生成流程；
    # 返回值第二项 degraded=True 标记这次发生了回退，调用方把它透传给前端提示用户
```

skill 全文是"必须遵循的规则"，`style_context` 是"在不违反 skill 规则前提下尽量满足的补充要求"——两者不冲突，因为决定权交给 LLM 在同一次调用里权衡，而不是代码层面做规则合并/覆盖。这也是为什么"有的 skill 本身就要求必须有输入照片"（如 `photo-abstract-editorial`）不需要代码特殊处理：绑定到纯文本生成场景时编译质量取决于 skill 内容本身对无图情况的适应性，属于 skill 设计的局限，不是编排层的 bug。

**编译降级的可见性**：`_compile_with_skill_package` 和 `_compose_edit_instruction`（十五.4）都会调用 Gemini 做一次 LLM 合成，两者都有"调用失败或返回空文本就静默回退成直接拼接"的兜底逻辑——这个设计本身没问题（保证 Gemini 抖动时生成流程不被卡死），但曾经完全没有对外暴露：用户看到的是"生成成功、图也出来了"，实际上风格/skill 规则根本没生效，只有翻服务端日志（`skill 编译失败，回退为...`）才能发现。真实踩过一次：Gemini 返回 `503 UNAVAILABLE`，某次"照片抽象" skill 编辑请求静默退化成直接把用户输入当 prompt，图片正常生成但完全没有 skill 效果。修复：两个函数都改成返回 `(prompt, degraded: bool)` 元组，`generate()` 路由把 `degraded` 存进响应体的 `compile_degraded` 字段，前端 `runGenerate()` 收到 `compile_degraded: true` 时弹出橙色提示"本次未成功应用风格/Skill 规则……可重新生成一次"，让用户当场就能发现要不要重试，不用等到看着不对劲再回头查日志。

**`generate()` 路由的分支逻辑**（`drawing.py`）：先统一算出编辑相关的上下文（`base_image_bytes`/`initial_prompt`/`previous_prompt`，无论是否绑定 skill 都要算，因为编辑场景在两条路径下都要用到父节点图片和历史 prompt），再判断 `style.skill_package_id` 是否有值：

```
若 style.skill_package_id：
    full_prompt = await _compile_with_skill_package(...)   # 统一走 LLM 编译，无论新建还是编辑
否则（现状完全不变，一行代码路径都不变）：
    若是编辑 → full_prompt = await _compose_edit_instruction(...)
    若是新建 → full_prompt = style_context + "\n\n" + user_input
```

不绑定 skill 的现有风格行为和之前完全一致；绑定后无论首次生成还是后续编辑都统一走 skill 编译。**历史链、展开/收起、单步删除、整链删除、上传按钮全都不用改**——它们操作的是生成结果那一层，不关心 prompt 怎么来的。

**PATCH 语义踩过的坑**：`UpdateStyleRequest` 用 `payload.dict(exclude_none=True)` 过滤未提供字段，这意味着永远没法把 `skill_package_id` 显式 PATCH 回 `null`（会被 `exclude_none` 吞掉，等同于"不改动"）。解决：前端解绑时显式发送空字符串 `""`（不是 `null`），路由层收到 `""` 时转成 `None` 再写库——有值=绑定，`""`=解绑，字段整体缺失=不改动，三种状态互不冲突。这是本项目第二次用这个"空字符串当显式清空信号"的约定（第一次是写作模块的类似字段）。

**已验证**（全部端到端跑通，非仅编译层）：真实抓取 `https://github.com/LiamGvchi/gc-minimal-zine-poster`（`SKILL.md` + 5 个 `references/*.md`，共 31869 字符）走通"抓取预览→保存"；绑定后首次生成（纯文本，无输入图片），`full_prompt` 是正确融合 skill 规则的四段式 prompt（而非原始输入直接透传），实际生成出图片文件并落盘；对同一张图做绑定 skill 的编辑（走 `base_image_bytes` / `gtypes.Part.from_bytes` vision 输入分支），`full_prompt` 正确体现"调暖光线"的编辑意图且延续了 skill 的版式规则，同样实际生成出图片文件并落盘；PATCH `skill_package_id=""` 解绑后再次生成，`full_prompt` 变回未经 LLM 编译的直接拼接（验证了解绑回退）；浏览器里确认设置面板下拉框正确回显已绑定的包、管理弹窗正确展示已有包列表和新增表单。开发环境里 CODEX 图片生成中转偶发连接超时（`generate_image`/`edit_image` 请求本身没变化，属于中转网络层的间歇性问题，不是本功能代码缺陷），生产环境如遇到同类超时，属于已知的外部依赖不稳定性，不代表编排逻辑有误。

Prompt 库与 `style.prompt` 合并为统一概念后（见十五.2），`_compile_with_skill_package` 的 `style_context` 单参数版本已重新端到端验证：绑定 2 条 Prompt 库条目 + 不绑 skill 包生成，`full_prompt` 正确等于两条 Prompt 内容拼接 + 用户输入；删除其中一条已勾选的 Prompt 后再次生成，被删条目正确从 `full_prompt` 中静默消失（不报错）；同一风格改绑 skill 包后生成，LLM 编译结果里正确体现了剩余那条 Prompt 的规则内容（例如"四角留白"被编译进最终 prompt 里"四角留空"的具体描述），三种场景均实际生成出图片文件。

---

## 十六、地图模块（`map.py` + `templates/map.html`）

### 概述

与「对话 / 写作 / 作图」并列的第四个模块，参考 [MapStage](https://github.com/hopechen067/MapStage) 及其 Demo（`https://hopechen067.github.io/MapStage/`）。做到 Demo 的「看图 / 调参 / 存预设」程度，**外加** TSAI 自研的**点 / 线 / 名称标注编辑器**。**不含** HyperFrames → MP4。

- **访问受 `users.can_map` 门控**：`is_admin` 或 `can_map=TRUE`（`map.py:require_map_access`，逐字镜像 `drawing.py:require_draw_access`；HTML 页面路由无权限 302 回首页，API 403；`_PAGE_ENDPOINTS = {"map_page","map_document_page"}`）。管理员在 `/admin/users` 逐用户授予（「开地图 / 撤地图」按钮 → `POST /admin/user/{id}/set_map`）。
- **一张地图 = `map_documents` 一行**，`preset`（JSONB）= `{ version:3, style:{…}, annotations:{points,links} }`。`preset` 实时防抖 PATCH **原地更新**。
- **零 AI / LLM 调用。** 唯一的服务端第三方出站是 `GET /map/geocode`（地名搜索代理到 OpenStreetMap Nominatim，因为 Nominatim 使用政策要求可标识的 User-Agent，浏览器 `fetch` 设不了）。瓦片 / DEM / 矢量 / 字体仍全部由**用户浏览器直连**第三方，不经服务器、无 API key（见「第三方依赖」）。
- **设计文档**：`MAP-MODULE-DESIGN.md`（含逐条决策与备选）。

### API 路由（`map.py`，前缀 `/map/`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/map/` | 首页：有地图则 302 到最新一张，无则空态 |
| `GET` | `/map/tile-config` | 把瓦片端点 + 署名下发给前端（唯一配置类接口，`settings.MAP_TILE_CONFIG`） |
| `GET` | `/map/geocode?q=` | 地名搜索：代理到 Nominatim（`_GEOCODE_UA` 标识本应用），返回 `{results:[{name,short,lat,lng,type,category}]}`，最多 8 条；上游失败抛 502 + 友好文案。**唯一的服务端第三方出站接口。** |
| `POST` | `/map/documents` | 新建地图（用 `map.py:DEFAULT_PRESET`） |
| `GET` | `/map/documents` | 当前用户地图列表 |
| `GET` | `/map/documents/{id}` | 单张地图完整信息（含 `preset`，JSONB 已 `json.loads`） |
| `PATCH` | `/map/documents/{id}` | 实时保存 `{name?, preset?}`（防抖，只发差异；原地更新，**不入版本表**） |
| `DELETE` | `/map/documents/{id}` | 删除（含尽力删缩略图） |
| `GET` | `/map/documents/{id}/versions` | 版本列表 |
| `POST` | `/map/documents/{id}/versions` | 存一个快照（`note` ∈ `open-diff` / `checkpoint` / `manual`），超 3 版删最旧 |
| `POST` | `/map/documents/{id}/versions/{version}/restore` | 回滚：把该版 `preset` 拷回 `map_documents.preset` |
| `POST` | `/map/documents/{id}/thumb` | 可选：前端 `canvas.toDataURL()` → `static/maps/{username}/{id}.png` |
| `GET` | `/map/{map_id}` | 地图页（Jinja2 HTML；**声明在文件末尾**，避免抢先匹配 `/documents`、`/tile-config`，同 `drawing.py:/{style_id}` 的坑） |

### 数据库

`backend/db.py:init_map_tables()`（startup 幂等调用）：

```sql
users.can_map BOOLEAN NOT NULL DEFAULT FALSE

map_documents(id UUID PK, user_id INT→users ON DELETE CASCADE, name TEXT,
              preset JSONB, thumb_path TEXT, created_at, updated_at)   -- idx: user_id

map_preset_versions(id SERIAL PK, map_id UUID→map_documents ON DELETE CASCADE,
                    preset JSONB, version INT, note TEXT, created_at)  -- idx: (map_id, version DESC)
```

DB 函数（`backend/db.py`，全部照 `drawing_*` / `writing_contents` 抄）：`create_map_document / get_map_documents / get_map_document / map_document_owned_by / update_map_document / delete_map_document / set_map_thumb / list_map_versions / get_map_version / snapshot_map_version / latest_map_version_preset / update_user_map_permission`（`_MAP_VERSIONS_KEEP=3`，`snapshot_map_version` 插入后删多余版本）。JSONB 列 asyncpg 读出是 `str`，`get_map_document` / `get_map_version` 里 `json.loads`。`get_all_users_with_stats` 的 SELECT 已带 `u.can_map`（供 `/admin/users` 用）。

### `preset` 结构（version 3）

样式（Tab A）+ 标注（Tab B）同存一个 JSON，一次快照 / 回滚覆盖两者。与 MapStage preset schema 在 `style` 部分同构 → 在 MapStage Demo 里调好的「复制 JSON」可直接粘进 TSAI（「粘贴 JSON」按钮，只吸收 `style`；`annotations` 有则一并）。

- `style.view` `"map" | "globe"`；`style.camera`（`center / zoom / pitch / bearing`，**默认 `pitch: 0`**，实时随 `moveend` 回写但**不计入快照变更**，见「版本历史」）；`style.basemap`（`satellite / relief / isolate / isolateRegion` 开关汇总）
- `style.admin`：`{enabled, boundary, place, road}` —— 行政区划图层组的主开关 + 3 个子开关（边界 / 地名 / 道路），数据来自 `openmaptiles` 矢量瓦片的 `boundary` / `place` / `transportation` source-layer
- `style.mapstage`（`backgroundColor / terrainExaggeration / satellite / hillshade / water`）
- `style.css`（sepia/saturate/contrast/brightness/hueRotate/warmTint*/vignetteStrength/`enabled`，作为 DOM 滤镜叠层加在 `.maplibregl-canvas` 上，**不在 MapLibre paint 里、也不加在容器上**——加容器会栅格化 marker 子树，缩放时 marker 抖动）
- `annotations.points[]`：`{id,name,lng,lat,tier,shape,size,markerColor,label{…}}`
  - `label` 是 badge 结构：`{show, fontSize, color, box:{show,bg,border,padX,padY}, prefix:{type,icon,text,bg,fg,fontSize}}`，`prefix.type` ∈ `none / icon / text`（`icon` 从 `PREFIX_ICONS` 内联 SVG 取），参考 `static/images/map_demo.png` 的地名徽标样式；旧的 `label{show,color}` 与 `callout{…}` 字段已废弃，`ensureLabel()` 打开时自动迁移
  - `tier` ∈ `capital / commandery / city / pass / station / custom`
- `annotations.links[]`：`{id,from,to,directed,color,width,dash,curve,bend,name,label{…}}`
  - **连线与点位完全解耦**：`from` / `to` 是**独立的 `[lng,lat]` 坐标对**（连线时从两端点复制一次坐标，之后互不影响）；`migrateLinks()` 把旧的「点位 id 字符串」自动迁移成坐标对
  - `curve` ∈ `straight / arc`（旧值 `geodesic` → `arc`）；`arc` 时 `bend`（可正负，0 = 直线）控制二次贝塞尔弧度，`arcCoords()` 前端插值
  - `color` 默认 `#000000`（黑色）
  - `name`：连线自己的名称，`ensureLink()` 首次补默认值（当前起终点的经纬度格式，`fmtLL(from) + " ⇢ " + fmtLL(to)`），之后完全独立、可编辑，不随拖动端点自动重算；列表 / 组成员行都显示 `name` 而非坐标
  - `label`（连线名称标注，`ensureLinkLabel()` 归一化）：`{show, fontSize, color, angle}`，默认 `show:false`；`angle` 默认取当前 `bearingDeg(from,to)` 四舍五入，之后独立可调（配合走势，同样不随端点拖动自动重算，面板有「对齐连线方向」按钮可手动重新对齐）
- `annotations.groups[]`：`{id,name,members:[点/线 id 混装],props:{markerColor,labelColor,boxBg,boxBorder,nameFontSize,prefixBg,prefixFg,prefixFontSize,linkColor,linkWidth,linkCurve,arrowStyle,arrowSize}}`（`arrowStyle`/`arrowSize` 对组内所有连线生效，与连线是否勾了「有向」无关——字段本身一直存在，只是不有向时不显示箭头）
  - **一个点/线只能属于一个组**（`moveToGroup()` 保证的不变量，见前端「列表」一节）
- `annotations.order[]`：顶层列表顺序，元素是「组 id」或「未分组的点/线 id」；已进组的点/线不出现在这里，顺序改由所在组的 `members` 决定。`ensureOrder()` 有自愈能力（见前端一节），旧 preset 没有这个字段会自动从现有 points/links/groups 推导补齐


  - **组 = 统一修改，不是样式叠层。** 调组里任一控件 → `setGroupProp()` 立刻把该值**写进组内每个成员自己的字段**（`GROUP_POINT_APPLY` 写点、`GROUP_LINK_APPLY` 写线），渲染时不做任何叠加 / 继承。单独改某个点 / 线照常生效，会让 `group.props` 变「过时」——这是允许的，两者无优先级。
  - `group.props` 只在打开组面板时用于回显和「应用到全部成员」（`applyAllGroupProps()`），**永不在加载时自动重放**（否则组就成了有优先级的样式层）。`groupDivergence()` 检测「成员当前值 ≠ 组设定值」并在面板里提示可「拉齐」。
  - 删点 / 删线时 `pruneGroupMember()` 清理各组的 `members`；`ensureGroups()` 在 `bootMap()` 里补默认值，旧 preset 无 `groups` 字段自动补 `[]`。

### 前端（`templates/map.html` + `static/css/map.css` + `static/js/map.js`）

镜像 `drawing.html` 布局：`#map-sidebar`（地图列表 + 新建）+ `.map-shell`（nav + `.map-layout` grid `1fr 340px`）+ `#map-main`（`#maplibre-map` + 左上浮层工具条）+ `#map-settings`（顶部 2-Tab）。**用项目 Materialize + `style.css` 风格，不引 MapStage 的前端。**

- **Tab A「地图效果」**：图层开关（卫星底图 / **行政区划**（主开关 `#tg-admin` + `#admin-sub` 里的边界 / 地名 / 道路 3 个子开关，`ensureAdmin()` / `_adminVis()` / `applyAdminVis()`）/ 海拔设色 / 水系 / 拆出+区域）、卫星层 6 参、山影 5 参 + 地形夸张、水系颜色、CSS 古卷滤镜、背景色。开关一律 `<label class="map-switch"><input type=checkbox></label>`（Materialize 会把裸 checkbox 设成 `opacity:0;pointer-events:none`，故自绘 `::before` 轨 + `::after` 钮 + `:has(input:checked)` 变色）。
- **Tab B「点 / 线 / 标注」**：工具条只有 **选择 / 加点 / 连线**（无「删除」——删除走列表行里的真 `<button class="annot-del-btn">`）+ 可折叠属性编辑区 + 列表。`renderList()` 按当前工具过滤：**加点** Tab 只列点位、**连线** Tab 只列连线，**选择** Tab（两者都不是）两种都列；地图上的既有点线渲染（`renderPoints()`/`renderLinks()`）不受此过滤影响，一直全量显示。
  - **加点 3 种方式**（`#add-point-panel`，仅「加点」模式显示）：① 地图点击选点 ② 输入经纬度（`#add-lat` / `#add-lng`，校验 ±90 / ±180）③ 地名搜索（`#geocode-q` → `authFetch("/map/geocode?q=")` → `#geocode-results` 候选列表，多结果让用户点选）。三条路径最终都走 `addPointAt(lng,lat,name)`。输入框需带 `browser-default` 类（否则 Materialize `input[type=number/search]` 强制 `height:3rem`）+ 一堆 `data-*-ignore` 关掉 1Password 弹窗。
  - 点位：HTML `maplibregl.Marker`（`anchor:"center"`）+ CSS 图形（方 / 圆 / 菱 / 关门 / 星）+ 可选**名称徽标**（`.map-pt-badge`，`position:absolute` 挂在图形外面，尺寸变化不挪锚点）。仅**选择**模式可拖拽（`draggable: annotMode==="select"`），`dragend` 回写 `pt.lng/lat` 并重画连线 / 列表 / 属性。
    - **名称标注方位（8 方向）+ 间距**：`pt.label.pos`（`ensureLabel()` 归一化，缺省/脏值兜底 `"bottom"`）∈ 上/下/左/右 + 四个对角，`makePointEl()` 给徽章加 `.pos-<值>` 类，落地规则全在 CSS（`map.css`）：四正方向贴边居中（如 `.pos-bottom { top:calc(100% + var(--pos-gap,4px)); left:50%; transform:translateX(-50%) }`），四对角贴角外扩、两个方向都留同一个间距不居中（如 `.pos-top-left { bottom:calc(100% + var(--pos-gap,4px)); right:calc(100% + var(--pos-gap,4px)) }`）。这些百分比坐标都相对 `.map-pt`（只包住 shape 本身的盒子）算，所以方位规则和点位 `size` 无关。
      - **间距可调**：那个写死的 `4px` 现在是 CSS 自定义属性 `--pos-gap` 的兜底值，真正的值来自 `pt.label.margin`（`ensureLabel()` 缺省 `LABEL_MARGIN_DEFAULT = 4`）——`makePointEl()` 用 `badge.style.setProperty("--pos-gap", margin + "px")` 挂到徽章自己的 inline style 上，所以每个点位可以各自设置不同的间距，八个方位共用同一个间距值（不是每个方位单独一个值）。属性面板对应一个「标注间距（像素）」的 `num` 输入（0–60）。
      - 属性面板用 `posPicker()` 画一个 3x3 方位选择器（中间格空着，`LABEL_POS_GRID` 定义八个方向的箭头符号）。**`labelPos` / `labelMargin` 故意不进组公共属性**（不在 `GROUP_PROP_DEFAULTS`/`GROUP_POINT_APPLY` 里，`groupDivergence()` 也不比对）——方位和间距是每个点位自己的摆放微调，不是「一批点该长一个样」的样式，同一组里不同点为了互相让位置，摆放方向往往正好相反，所以只在点位自己的属性面板（`pointEditor`）里调，组统一修改 / 组属性面板都碰不到这两项。
  - **`.map-pt { position: absolute }` 是硬约束**：`map.css` 在 `maplibre-gl.css` 之后加载，若为 `relative` 会盖掉 `.maplibregl-marker{position:absolute}`，marker 落回文档流、按各自 badge 尺寸层层错位（曾导致最大 badge 的点缩放时漂移）。
  - 连线：`renderLinks()` 生成 LineString 喂 `annot-link-solid` / `annot-link-dash` 两个 line 图层（`line-dasharray` 不支持数据驱动，按 `["==",["get","dash"],true]` 拆两层，虚线 `[2,2]`）；**有向箭头** = `annot-arrowheads` GeoJSON（LineString 末点 + `bearing` + `icon` + `sizeMul`）喂 `annot-link-arrow` symbol 图层，`icon-image:["get","icon"]` + `icon-rotate` + `icon-color`（不再用字形 `➤`，Noto Sans 里没有）。
    - **箭头样式 + 大小**：`lk.arrowStyle` ∈ `ARROW_STYLES`（`triangle` 宽三角缺口 / `narrow` 窄三角 / `chevron` 描边 ">"，默认 `triangle`）、`lk.arrowSize` 数值倍率（默认 1，0.5~3 可调）。三种样式各自是 `makeArrowImage(style, 24)` 画的朝北 canvas 图形，`map.on("load")` 时循环注册成 `annot-arrow-triangle`/`annot-arrow-narrow`/`annot-arrow-chevron` 三张 `addImage(…, {sdf:true})` 图标；图层的 `icon-size` 用每条连线自己的 `sizeMul` 倍率乘上原有的缩放插值曲线。只在「有向（箭头）」勾选时，属性面板才显示「箭头样式」「箭头大小」两个控件。
      - **踩过的坑（`["zoom"]` 表达式位置）**：`icon-size` 最初写成 `["*", ["get","sizeMul"], ["interpolate",["linear"],["zoom"],...]]`——把 zoom 插值包在乘法里面。MapLibre style-spec 规定 `["zoom"]` 只能作为 `interpolate`/`step` 的**顶层**输入，不能嵌在其他表达式里，这个写法在浏览器控制台只报一条不起眼的黄色 warning（`"zoom" expression may only be used as input to a top-level "step" or "interpolate" expression`），但后果是**整份 style 校验失败、一个图层都加不上**——地图区域退化成 `#map-main` 自己的 CSS 背景色（纯色 `#c8c2b4`），右侧设置面板照常能用（这部分是纯 DOM/JS，不依赖 MapLibre 画布），看起来像“地图打不开”而不是报错。正确写法：把 zoom 插值留在最外层，倍率乘法挪进 `interpolate` 每个缩放档位的**输出值**里——`["interpolate",["linear"],["zoom"], 3, ["*",["get","sizeMul"],0.5], 12, ["*",["get","sizeMul"],1]]`。这类问题以后先看浏览器控制台的黄色 warning（不只是红色 error），`onMapError()` 目前对这类非 `sourceId` 的样式校验错误只是 `console.warn`，不会弹用户可见的提示。
  - **连线名称 + 名称标注**：连线自带可编辑 `name`（默认经纬度格式，属性面板「名称」输入框），列表 / 组成员行都显示它而非坐标。可选的名称标注走单独的 `annot-link-labels` GeoJSON（各连线的中点，直线取端点中点、弧线取 `arcCoords` 插值后的中间点）+ `annot-link-label` symbol 图层，`text-field:["get","text"]` + `text-size`/`text-color` 数据驱动 + `text-rotate:["get","angle"]`（`text-rotation-alignment:"map"`，与 `bearingDeg()` 同一角度约定：0=正北顺时针）。字号 / 颜色 / 角度都在属性面板里调，角度默认等于连线当前方位角、之后独立可调（面板「对齐连线方向」按钮可手动重新对齐到当前方位角）。
  - **选择模式下拖动连线端点**：选中连线时在 `from` / `to` 各放一个 `.link-handle` marker（`m.on("drag")` 实时改 `lk[key]` + 重画）；与点位重合时靠 DOM 顺序 + `.maplibregl-marker:has(.link-handle){z-index:5}` 消歧。
  - **属性面板折叠**：`#annot-editor-head` 点击切 `.collapsed` + `#annot-editor` 显隐，状态存 `localStorage["map.annotEditorCollapsed"]`。
  - **列表 = 点 / 线 / 组统一一张表，组是列表里的条目、不是单独板块**。`#annot-list` 标题栏右上是「＋ 新建组」（`startCreateGroup`）；不再有独立的「组」settings-title / `#annot-groups` 区块，也不再按工具 Tab 隐藏——组本来就混在列表里，列表本身该怎么按 Tab 过滤（加点 Tab 只看点、连线 Tab 只看线）组也照此规则过滤（组内没有匹配类型成员时，那个组在该 Tab 下不占位）。
    - **顺序与层级**：`preset.annotations.order` 是顶层条目 id 数组（元素是「组 id」或「未分组的点/线 id」；已进组的点/线不出现在这里，顺序由所属组自己的 `members` 决定）。`renderList()` 按 `order` 遍历渲染：组头（`buildGroupRow`，加粗 + 淡靛蓝底 + `▣` 前缀 + 折叠箭头 `.grp-collapse-btn` + 解散）永远排在它所有子条目（`buildPointRow`/`buildLinkRow`，`isChild:true` 时加 `.child-row` 左缩进）前面；不属于任何组的条目和组头左边对齐、不缩进。折叠状态 `collapsedGroups[g.id]` 是纯前端内存变量，不进 preset（不占版本快照，也不跨端同步，等同一次性视图偏好）。
    - **`ensureOrder()` 自愈**：每次 `renderList()` 前都会跑一遍——去掉已经不存在、或已被某组吞掉的 id；把新出现但还没登记的点/线/组追加到末尾。所以新建点/线/组、删点/删线之后 `order` 的一致性完全不需要在各自的创建/删除逻辑里手动维护，只有「解散组」为了让子条目**原地**变回未分组（而不是被自愈逻辑扔到列表末尾）单独处理了一次插入位置。
    - **单一归属**：一个点/线只能属于一个组。`moveToGroup(id, targetGroupId, insertBeforeMemberId)` 是唯一的归属变更入口——先把 id 从所有组里摘掉、从顶层 `order` 里摘掉，再按需塞进目标组的 `members`（`targetGroupId` 为空则只是变成未分组）。组编辑器里勾选成员（`groupMemberRow`）和拖拽排序都走这一个函数，保证任何时候「一个 id 最多在一个组的 members 里」这条不变量。**归属变化本身绝不读写组的公共属性**（`markerColor` 等）——加入/离开组既不会被套上组的属性，也不会因为离开而丢失自己原有的属性值，这条和「组 = 统一修改，不是样式叠层」的核心设计是同一件事。
      - **候选列表过滤，不是锁勾选框**：组属性面板的成员列表（`groupEditor()`）在渲染前先用 `groupOfMember(id)` 过滤 `preset.annotations.points/links`，只保留「本组成员」和「未分配到任何组」的条目——属于别的组的条目**根本不出现在列表里**，不是曾经的做法（列出来但把勾选框锁死、旁边挂「已在「X」」标签）。想把某个点/线转到别的组：拖拽跨组移动（`handleRowDrop`）仍然是唯一的"直接转组"路径；否则只能先去它当前所属组的面板取消勾选（退回未分组），再到目标组的面板里勾选。这样列表不会因为别的组的成员而变长。
    - **拖拽排序**（原生 HTML5 draggable，鼠标）：每行 `wireRowDrag(row, id, isGroup, groupId)` 挂 `dragstart/dragover/drop`；`dragover` 时按鼠标 Y 相对行高的上下半判断插入在目标前还是后（`.drag-over-before`/`.drag-over-after` 描边提示）。落点语义在 `handleRowDrop()`：拖组只能在顶层挪位置（落在别人的子条目上会换算成落在那个子条目所属组块的边界）；拖点/线落在子条目上＝加入（或留在）那条子条目所属的组；落在组头上半＝变成未分组、插到该组前面，下半＝加入该组成为第一个子条目；落在未分组条目上＝变成未分组、插到它前/后。`anchorAfterExcluding()` 处理了一个边界情况：把 X 拖到「紧跟在它原本后一位的 Y」后面（几乎等于原地不动）时，如果直接找“Y 后面那个”会找到 X 自己，导致 X 被错误地弹到列表末尾——所以查找前先假装把 X 从列表里摘掉再算。
    - **新建组：先选子条目、不允许空组**：点「＋ 新建组」只是把 `pendingGroupPick` 从 `null` 变成 `[]`（`startCreateGroup`），并不立即建组；这之后 `renderList()` 在每个点/线行前面插入勾选框（`pickCheckbox`，复用 `.map-switch`），并在列表顶部露出 `#annot-group-pick-bar`（已选 N 个 / 确定建组 / 取消）。**这个流程里已有的组和它们的成员完全不渲染**（`renderList()` 遇到 `pendingGroupPick` truthy 时直接跳过组条目）——候选列表只剩未分组的点/线，不会被别的组的成员撑长。「确定建组」（`confirmCreateGroup`）在 `pendingGroupPick` 为空时直接 toast 拒绝；非空时才真正 `push` 一个新组、把组头插到这批被选条目里原本顶层位置最靠前的那个位置、再逐个 `moveToGroup` 挪进去。picking 状态跨工具 Tab 保留（不会因为切到「连线」去挑几条线又跳回「加点」而被打断），且此时列表行的拖拽和删除按钮都临时隐藏，避免和勾选手势冲突。
    - **组的属性面板不变**：`groupEditor()` 依然是打开一个组时属性区显示的内容——组名、成员勾选列表（列表里的复选框和上面的建组勾选框是两套独立 UI，但都收敛到同一个 `moveToGroup`）、点位公共属性（颜色 / 名称文字色 / 信息框底色+边线色 / 名称字号 / 前缀底色+文字色+字号）、连线公共属性（颜色 / 线宽 / 线形 / **箭头样式 / 箭头大小**）、「应用到全部成员」（`applyAllGroupProps`，成员被单独改过导致和组设定不一致时 `groupDivergence()` 会提示）、「解散该组」。`selectedId` 三态：点 id（`p_`）/ 线 id（`l_`）/ 组 id（`g_`），前缀不冲突，`renderEditor()` 据此分发到 `pointEditor`/`linkEditor`/`groupEditor`。
    - **踩过的坑（按钮样式漏挂）**：「应用到全部成员」按钮最初复用了 `.annot-add-btn` 类，但那份视觉样式在 CSS 里写成 `#add-point-panel .annot-add-btn`——限定了父级选择器，组面板不在 `#add-point-panel` 下，class 挂了等于没挂，按钮退化成浏览器默认丑样式。改法：`.grp-apply-btn` 自己带全套颜色 / 圆角 / 字号（跟「按坐标添加」「＋ 新建组」视觉一致），不再依赖那条限定了父级的规则。
- **下载图片**（工具条「下载图片」，`downloadMapImage()`）：把当前地图（底图/矢量/地形，MapLibre 渲染在 WebGL canvas 里）+ 点位/名称标注（DOM 覆盖层，`maplibregl.Marker`）合成一张 PNG 下载。
  - **两层内容分开处理**：连线 / 箭头 / 连线名称标注早就是 MapLibre 样式图层，随底图一起在 canvas 里，`ctx.drawImage(map.getCanvas(),0,0)` 直接就有；点位形状 + 名称标注（含信息框、前缀、八方位摆放、间距，见上文）是 DOM，不在 canvas 里。**不重新写一套 canvas 绘图逻辑去手画形状/flex 布局**（容易和 CSS 实际效果对不上、CSS 一改这里要跟着改），而是把当前这批 `.maplibregl-marker`（`collectMarkerHtml()`，跳过 `.link-handle` 拖拽手柄，清掉 `.sel`/`.in-group` 选中态类）原样序列化进一个 SVG `<foreignObject>`、连同 `map.css` 全文一起塞进一个 `data:image/svg+xml` 的 `Image`，用浏览器真正的排版引擎光栅化，再 `drawImage` 叠到底图上——天然和屏幕一致，不用维护第二套样式逻辑。`data:` URI 图片不会给 canvas 加跨域污点，之后 `toBlob()` 正常可用。
  - **canvas 读取前提**：`_createMap()` 建图时要开 `preserveDrawingBuffer: true`，否则 WebGL 后台缓冲区可能已被清空，`getCanvas()` 读出来是黑图/空图。
  - **「做旧」CSS 滤镜要手动补回**：`applyCssOverlays()` 的 sepia/饱和度/对比度/亮度/色相旋转是挂在 `.maplibregl-canvas` 元素自己的 CSS `filter` 上、`#map-warm-tint`（`mix-blend-mode:soft-light` 暖色调）和 `#map-main::after`（`radial-gradient` 暗角，`--vignette` 驱动）都是页面上单独的 DOM/伪元素叠层——这三样都不在 `getCanvas()` 的像素数据里，直接 `drawImage` 会丢。`downloadMapImage()` 用 Canvas 2D 的等价能力补回：画底图时用同语法的 `ctx.filter` 顶替 CSS filter；暖色调用 `ctx.globalCompositeOperation="soft-light"` + `fillRect`；暗角用 `ctx.scale` 把坐标系压成画布长宽比、`createRadialGradient` 画一个贴合椭圆的径向渐变。**层序对应页面真实的 z-index**（`#map-warm-tint`/`::after` 的 z-index 比地图容器高，会连点位一起罩住）：底图 → 点位标注 → 暖色调 → 暗角，不是先叠色再画点位。
- **自动保存**：`map.js:scheduleSave()` 防抖 800ms → `PATCH /map/documents/{id}`；地图 `moveend` 防抖回写 `style.camera` 并触发保存。
- **地形网格（`setTerrain` 3D mesh）只在地球视图或 `pitch > 4` 时开**（`applyTerrain()`）；平视地图只用 hillshade / color-relief 图层，避免 marker 贴着地形起伏漂移。
- **版本历史（做法2）**：`map_documents.preset` 实时自动保存；`map_preset_versions` 只在检查点写快照——①**首次编辑前**把「打开时的 preset」存 `open-diff` 版 ②每 120s 若 preset 变化存 `checkpoint` 版 ③工具条「存快照」存 `manual` 版。变更检测用 `snapshotKey(p)`——序列化前 `delete c.style.camera`，所以**缩放 / 平移 / 俯仰 / 旋转不算「有变化」**（相机仍实时写进 `preset` 和库，只是不触发新快照）。「存快照」按钮会先 `syncCameraNow()` 把当前视角拉进 `preset` 再 POST，故手动快照能存下当前视角。留 3 版；「历史」弹窗可回滚（回滚后整页 reload）。

### 第三方依赖

除地名搜索一项走服务端代理外，其余全部浏览器端直连，无 key、无 `.env` 配置。

| 依赖 | 位置 / 端点 | 用途 |
|---|---|---|
| **MapLibre GL JS + CSS**（MapStage 的 **patched 5.6.0**，含 `__ANTIQUE_TERRAIN_CLIP_PATCH`） | 本地 vendor `static/js/maplibre/maplibre-gl.{js,css}` | 地图引擎 + 拆出所需的地形裁剪补丁；`/map/` 整页锁死用这份 |
| 卫星栅格瓦片 | `tiles.maps.eox.at`（EOX Sentinel-2 cloudless，`{z}/{y}/{x}`） | 卫星底图 raster source |
| 地形 DEM 瓦片 | `tiles.mapterhorn.com/{z}/{x}/{y}.webp`（`encoding: terrarium`） | 山影 + `setTerrain` 3D 地形（仅地球 / 俯视时开 mesh） |
| 矢量瓦片 + glyphs | `tiles.openfreemap.org/planet` + `/fonts/{fontstack}/{range}.pbf` | 水系几何、行政区划（边界 / 地名 / 道路）、字形 |
| **OSM Nominatim 地名搜索** | `nominatim.openstreetmap.org/search` —— **经服务端 `GET /map/geocode` 代理**（带 `_GEOCODE_UA`），非浏览器直连 | Tab B「加点」的地名搜索选点 |
| **拆出整包**（vendor，原样不改） | `static/js/maplibre/mapstage/`：`map-fx.js` / `vector-paint.js` / `region-isolate.js` / `region-isolate-data.js`(1.9MB) / `terrain-island.js` / `isolate-workbench.js` / `polar-ice.geojson` + `NOTICE.md` | 区域地形岛（`AntiqueIsolateWorkbench.mount(...)`，集成路径逐字参考 MapStage `index.html:setupGlobeAndIsolate()`；mount 时 `enabled:false`、不传 `selectEl`，自己的 `#isolate-region` change 里 `setRegion(id,{frame:false})`，避免拆出把视角强行怼成 `pitch:46/bearing:-16`） |

**署名（许可硬性）**：MapLibre attribution 控件显示 `Sentinel-2 cloudless © EOX`、`© Mapterhorn`、`© OpenStreetMap contributors, OpenMapTiles`；拆出开启另注 `Natural Earth (public domain) · © OpenStreetMap contributors (ODbL)`。

**兜底**（`map.js:onMapError()`）：按 `e.sourceId` 分派——`basemapRaster` / `terrain` / `openmaptiles` 失败 → 一次性友好 toast + 自动关掉受影响图层 / 开关（`openmaptiles` 分支连带禁掉行政区划图层）；glyph 报错 → 「箭头可能不显示」提示；MapLibre 引擎脚本加载失败（`<script onerror>` 置 `window.__MAPLIBRE_LOAD_FAILED`）→ `#map-main` 整块提示，不初始化地图。

**隐藏标签页兜底**：`document.hidden` 时 MapLibre `Style.loadJSON` 靠 `requestAnimationFrame` 推迟的 `_load` 不会执行 → 地图白屏。`initMapLibre()` 检测到 `document.hidden` 就等 `visibilitychange` 再 `_createMap()`。（副作用：浏览器自动化标签页恒为 hidden，无法用它可视化验证地图渲染。）

### 模块切换下拉（需求 1）

`templates/_module_switch.html` 片段——**自绘的向上弹出小菜单，不用 Materialize `M.Dropdown`**（它在 `position:absolute` 的 `.sidenav-footer` 里会被裁切 / 错位）。`base.js` 里一个 `document` 级 click 委托：点 `.module-switch-btn` 开关同级 `.module-switch` 的 `.open` 类，点菜单外收起；CSS（`.module-switch` / `.module-switch-menu` / `.module-switch.open .module-switch-menu`）在 `style.css`（`?v=5`）。`chat.html` / `writing.html` / `drawing.html` / `map.html` 侧栏底部（`#slide-out` 移动端 + 桌面 sidebar-actions 两处，`ms_dd_id` 各不同）把原来的 2~3 个兄弟跳转按钮替换为一个「切换模块」下拉，列出当前模块之外、且该用户有权限的模块（对话恒显，写作 / 作图 / 地图按 `can_write` / `can_draw` / `can_map`）。「新建X」按钮保留。`main.py:index()` 与 `writing.py` / `drawing.py` / `map.py` 的页面路由 context 均传 `can_write / can_draw / can_map`（`account.get_user` 是 `SELECT *`，加列自动带出）。
