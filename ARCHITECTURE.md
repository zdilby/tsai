# TSAI 项目架构文档

> 本文档由 Claude Code 自动生成并维护，随代码变动同步更新。
> 最后更新：2026-05-17

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
├── settings.py           # 配置加载（.env）、全局 logger
├── backend/
│   ├── db.py             # 全部 SQL 操作与数据库 Schema
│   └── rag.py            # 向量检索、Embedding 生成
├── midware/
│   ├── tools.py          # 文档解析、分块、网络搜索
│   └── upload.py         # 文件上传与后台处理
├── templates/            # Jinja2 HTML 模板
│   ├── chat.html         # 主聊天界面（sidenav + .main 双栏）
│   ├── writing.html      # 写作模块界面（#writing-sidebar + .writing-shell 双栏，内含 1fr+300px 网格）
│   ├── account/          # 登录/注册页
│   └── admin/            # 管理员后台页
├── static/               # CSS、JS、用户上传文件
│   └── loads/            # 用户上传文件：loads/{username}/{session_id}/
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

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/writing/` | 写作首页（自动跳转最新任务） |
| `GET` | `/writing/{task_id}` | 写作任务页面（Jinja2 HTML） |
| `POST` | `/writing/tasks` | 新建写作任务（同时创建 is_writing_session Session） |
| `GET` | `/writing/tasks` | 获取用户全部写作任务列表 |
| `GET` | `/writing/tasks/{task_id}` | 获取单个写作任务详情 |
| `PATCH` | `/writing/tasks/{task_id}` | 更新写作任务设置（title/word_count/style_req/content_req/outline/reference_files） |
| `DELETE` | `/writing/tasks/{task_id}` | 删除写作任务 |
| `GET` | `/writing/tasks/{task_id}/content` | 获取最新写作内容（+version 号） |
| `POST` | `/writing/tasks/{task_id}/content` | 保存写作内容（版本化，保留最近 3 版） |
| `GET` | `/writing/files` | 获取当前用户所有已处理完成的文件（供参考资料选择） |
| `GET` | `/writing/tasks/{task_id}/generate_outline` | SSE 流式生成内容大纲 |
| `POST` | `/writing/tasks/{task_id}/generate_content` | SSE 流式生成/优化写作内容（含 RAG 参考资料检索） |

### 管理员路由（`admin.py`）

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/admin/` | 管理员总览（用户 + 邀请码） |
| `GET` | `/admin/user/{id}` | 用户详情页 |
| `GET` | `/admin/session/{id}` | Session 详情页 |
| `POST` | `/admin/user/{id}/set_admin` | 将指定用户提升为管理员 |
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
                                                              ↓ 1:N
                                                          writing_contents
                                                          （版本化写作内容
                                                           最多保留 3 版）

invite_codes       ← 邀请码（独立表）
```

### `users`

```sql
id              SERIAL PRIMARY KEY
username        TEXT UNIQUE NOT NULL
password_hash   TEXT NOT NULL
is_admin        BOOLEAN DEFAULT FALSE
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
style_req       TEXT DEFAULT ''           -- 风格要求
content_req     TEXT DEFAULT ''           -- 内容要求
outline         TEXT DEFAULT ''           -- 内容大纲（可手动修改或 AI 生成）
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
7. 批量 Embedding（每批 50 条，遇 429 指数退避重试：30s→60s→120s→...）
8. 批量插入 knowledge_base（含 pgvector 向量）
9. 更新 upload_files.status → done
```

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

写作模块是独立于对话功能的 AI 辅助写作系统，提供结构化的写作任务管理、Markdown 编辑器、AI 生成大纲 / 内容、以及实时 AI 对话修改写作内容的能力。

### 架构设计

- **写作任务**：每个任务对应一条 `writing_tasks` 记录 + 一个绑定的 `is_writing_session` Session
- **RAG 集成**：写作任务可选择参考文件列表，生成内容时仅从这些文件的知识库 chunk 中检索
- **AI 对话修改**：利用现有 `/chat` 接口，在绑定的 writing session 中对话，AI 用 `[WRITING_UPDATE_START]...[WRITING_UPDATE_END]` markers 包裹新内容，前端自动检测并更新编辑器
- **内容版本化**：每次保存写作内容都生成新版本，数据库保留最近 3 版

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
- `chatSessionId` 指向当前写作任务的绑定 Session
- 发送消息调用 `/chat` POST（FormData），携带 `session_id` + `source_files`
- 检测 `[WRITING_UPDATE_START]...[WRITING_UPDATE_END]`：自动抽取新内容 → `exitEditMode()` + 自动保存

### 关键实现细节

- **写作 Session 隔离**：`GET /sessions` 增加 `AND (is_writing_session = FALSE OR is_writing_session IS NULL)` 过滤，写作 session 不出现在对话页面
- **SSE 流式输出**：`generate_outline` 和 `generate_content` 两个端点均返回 `StreamingResponse(media_type="text/event-stream")`，格式 `data: chunk\n\n`，结束标志 `data: [DONE]\n\n`
- **参考资料 RAG 过滤**：`query_rag()` 支持 `source_files` 参数，只从指定文件的 chunks 中检索
- **内容流式显示**：SSE 流式输出时用 `preview.textContent +=` 追加（安全），流完成后调用 `exitEditMode()` 渲染 Markdown

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
